"""串口调试后端服务

基于 pyserial 实现串口管理，支持端口枚举、收发数据、
后台线程接收并通过 event_manager 推送到前端。
支持多个串口实例（多 Tab 同时调试不同设备），每个实例用 connection_id 标识。

事件推送:
  - serial.data (JSON):   {connection_id, direction, data(hex), timestamp}
  - serial.status (JSON):  {connection_id, connected, port?, error?}
  - log (JSON):            日志事件（串口打开/关闭/错误等）
  - Binary 帧:             高频数据通道 (data_type=0 = serial_rx)
"""

import threading
import logging
import time
from datetime import datetime

import serial
from serial.tools import list_ports
from serial.serialutil import SerialException

logger = logging.getLogger(__name__)

# ── 串口参数校验集合 ────────────────────────────────────────

_VALID_BYTESIZE = {5, 6, 7, 8}
_VALID_PARITY = {"N", "E", "O", "M", "S"}
_VALID_STOPBITS = {1, 1.5, 2}
_VALID_FLOW_CONTROL = {"none", "xonxoff", "rtscts"}

# 接收数据批量刷新阈值：达到该字节数即推送一批
_RX_FLUSH_SIZE = 16 * 1024
# 接收数据批量刷新间隔：距上次刷新超过该时长即推送一批（保证显示及时）
_RX_FLUSH_INTERVAL = 0.05


class SerialBackend:
    """串口后端管理器

    管理串口连接生命周期，后台线程接收数据，
    通过 event_manager 推送到前端。
    """

    def __init__(self, event_manager, connection_id: str = "default"):
        self.connection_id = connection_id
        self._serial: serial.Serial | None = None
        self._read_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._event_manager = event_manager
        self._lock = threading.Lock()
        # ── 录制状态（接收数据落盘） ──
        self._record_file = None
        self._record_lock = threading.Lock()
        self._recording_path = None
        self._record_bytes = 0

    # ── 公共方法 ──────────────────────────────────────────

    def list_ports(self) -> list[dict]:
        """枚举可用串口

        返回端口信息列表，每个端口包含:
            device, description, hwid, manufacturer, vid, pid
        """
        ports: list[dict] = []
        try:
            for port in list_ports.comports():
                ports.append({
                    "device": port.device,
                    "description": port.description,
                    "hwid": port.hwid,
                    "manufacturer": getattr(port, "manufacturer", None),
                    "vid": f"0x{port.vid:04X}" if port.vid else None,
                    "pid": f"0x{port.pid:04X}" if port.pid else None,
                })
        except Exception as e:
            logger.error("枚举串口失败: %s", e)
            self._event_manager.log("error", f"Serial 枚举端口失败: {e}")
        return ports

    def is_open(self) -> bool:
        """串口是否已打开"""
        return self._serial is not None and self._serial.is_open

    def open(self, config: dict) -> dict:
        """打开串口

        配置参数:
            port: str        - 端口名 (如 "COM3")
            baudrate: int     - 波特率 (如 115200)
            bytesize: int     - 数据位 (5/6/7/8)
            parity: str       - 校验位 ('N'/'E'/'O'/'M'/'S')
            stopbits: float   - 停止位 (1/1.5/2)
            flow_control: str - 流控 ('none'/'xonxoff'/'rtscts')

        返回: {success: bool, error?: str}
        """
        with self._lock:
            # 如果已打开，先关闭
            if self.is_open():
                self._close_internal()

            # 解析配置
            port = config.get("port")
            if not port:
                return {"success": False, "error": "未指定端口"}

            baudrate = int(config.get("baudrate", 115200))
            bytesize = int(config.get("bytesize", 8))
            parity = str(config.get("parity", "N")).upper()
            stopbits = float(config.get("stopbits", 1))
            flow_control = str(config.get("flow_control", "none")).lower()
            rts = bool(config.get("rts", False))
            dtr = bool(config.get("dtr", False))

            # 参数校验
            if bytesize not in _VALID_BYTESIZE:
                return {"success": False,
                        "error": f"无效的数据位: {bytesize}（应为 5/6/7/8）"}
            if parity not in _VALID_PARITY:
                return {"success": False,
                        "error": f"无效的校验位: {parity}（应为 N/E/O/M/S）"}
            if stopbits not in _VALID_STOPBITS:
                return {"success": False,
                        "error": f"无效的停止位: {stopbits}（应为 1/1.5/2）"}
            if flow_control not in _VALID_FLOW_CONTROL:
                return {"success": False,
                        "error": f"无效的流控: {flow_control}（应为 none/xonxoff/rtscts）"}

            xonxoff = flow_control == "xonxoff"
            rtscts = flow_control == "rtscts"

            # 打开串口
            try:
                self._serial = serial.Serial(
                    port=port,
                    baudrate=baudrate,
                    bytesize=bytesize,
                    parity=parity,
                    stopbits=stopbits,
                    xonxoff=xonxoff,
                    rtscts=rtscts,
                    timeout=0,  # 非阻塞读
                )
                # rts/dtr 不是 Serial 构造参数，需在打开后作为属性设置
                # rtscts 模式下 RTS 由硬件流控接管，忽略用户设置
                if not rtscts:
                    self._serial.rts = rts
                self._serial.dtr = dtr
                # 清空输入缓冲区，避免残留数据
                self._serial.reset_input_buffer()
            except SerialException as e:
                error_msg = str(e)
                self._serial = None
                self._event_manager.log(
                    "error", f"Serial 打开失败 [{port}]: {error_msg}")
                return {"success": False, "error": error_msg}
            except Exception as e:
                error_msg = str(e)
                self._serial = None
                self._event_manager.log(
                    "error", f"Serial 打开失败 [{port}]: {error_msg}")
                return {"success": False, "error": error_msg}

            # 启动接收线程
            self._stop_event.clear()
            self._read_thread = threading.Thread(
                target=self._read_loop,
                name="serial-read",
                daemon=True,
            )
            self._read_thread.start()

            self._event_manager.log(
                "info",
                f"Serial 已打开: {port} @ {baudrate}bps "
                f"({bytesize}{parity}{stopbits}, flow={flow_control})",
            )
            self._event_manager.emit("serial.status", {
                "connectionId": self.connection_id,
                "connected": True,
                "port": port,
            })

            return {"success": True}

    def close(self) -> dict:
        """关闭串口

        停止接收线程并释放串口资源。
        返回: {success: bool, error?: str}
        """
        with self._lock:
            return self._close_internal()

    def send(self, data: bytes) -> dict:
        """发送数据

        Args:
            data: 要发送的字节数据

        返回: {success: bool, bytes_written?: int, error?: str}
        """
        if not self.is_open():
            return {"success": False, "error": "串口未打开"}

        try:
            bytes_written = self._serial.write(data)
            if bytes_written is None:
                bytes_written = len(data)

            # 推送发送数据事件
            self._event_manager.emit("serial.data", {
                "connectionId": self.connection_id,
                "direction": "tx",
                "data": data.hex().upper(),
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            })

            return {"success": True, "bytes_written": bytes_written}
        except SerialException as e:
            error_msg = str(e)
            self._event_manager.log("error", f"Serial 发送失败: {error_msg}")
            return {"success": False, "error": error_msg}
        except (AttributeError, TypeError) as e:
            # 串口可能在发送过程中被关闭
            error_msg = f"串口已关闭: {e}"
            self._event_manager.log("error", f"Serial 发送失败: {error_msg}")
            return {"success": False, "error": error_msg}

    def update(self, patch: dict) -> dict:
        """运行时更新部分串口参数（无需重开端口）

        支持在线调整:
            baudrate: int  - 波特率
            rts: bool      - RTS 信号（rtscts 流控下由硬件接管，忽略）
            dtr: bool      - DTR 信号

        返回: {success: bool, error?: str}
        """
        with self._lock:
            if not self.is_open() or self._serial is None:
                return {"success": False, "error": "串口未打开"}
            serial_ = self._serial
            try:
                if "baudrate" in patch:
                    baudrate = int(patch["baudrate"])
                    if baudrate <= 0:
                        return {"success": False,
                                "error": f"无效的波特率: {baudrate}"}
                    serial_.baudrate = baudrate
                if "rts" in patch and not serial_.rtscts:
                    # rtscts 流控下 RTS 由硬件接管，忽略用户设置
                    serial_.rts = bool(patch["rts"])
                if "dtr" in patch:
                    serial_.dtr = bool(patch["dtr"])
            except (SerialException, ValueError) as e:
                self._event_manager.log(
                    "error", f"Serial 参数更新失败: {e}")
                return {"success": False, "error": str(e)}
            self._event_manager.log(
                "info", f"Serial 参数已更新 [{serial_.port}]")
            return {"success": True}

    def get_status(self) -> dict:
        """获取串口状态"""
        if self.is_open() and self._serial is not None:
            return {
                "connection_id": self.connection_id,
                "connected": True,
                "port": self._serial.port,
                "baudrate": self._serial.baudrate,
                "bytesize": self._serial.bytesize,
                "parity": self._serial.parity,
                "stopbits": self._serial.stopbits,
            }
        return {"connection_id": self.connection_id, "connected": False}

    # ── 接收数据录制（后端落盘，保证字节流 100% 完整） ──────────

    def start_record(self, path: str) -> dict:
        """开始录制：将接收到的原始字节写入指定文件

        录制在后端读取线程中进行，与前端显示完全解耦，
        即使前端渲染卡顿也不会丢数据。
        """
        with self._record_lock:
            # 若已有录制，先关闭
            self._close_record_unlocked()
            try:
                # 二进制写入，追加模式以兼容重复录制
                self._record_file = open(path, "ab")
                self._recording_path = path
                self._record_bytes = 0
            except Exception as e:
                self._record_file = None
                self._recording_path = None
                self._event_manager.log(
                    "error", f"Serial 打开录制文件失败 [{path}]: {e}")
                return {"success": False, "error": str(e)}
        self._event_manager.log("info", f"Serial 开始录制到 {path}")
        return {"success": True}

    def stop_record(self) -> dict:
        """停止录制：flush 并关闭文件，返回录制统计"""
        with self._record_lock:
            path = self._recording_path
            bytes_written = self._record_bytes
            self._close_record_unlocked()
        if path is not None:
            self._event_manager.log(
                "info",
                f"Serial 已停止录制 [{path}]，共 {bytes_written} 字节")
        return {"success": True, "path": path, "bytes_written": bytes_written}

    def _close_record_unlocked(self):
        """关闭录制文件（调用方需持有 _record_lock）"""
        if self._record_file is not None:
            try:
                self._record_file.flush()
                self._record_file.close()
            except Exception as e:
                self._event_manager.log(
                    "error", f"Serial 关闭录制文件异常: {e}")
            finally:
                self._record_file = None
        self._recording_path = None
        self._record_bytes = 0

    def _record_write(self, data: bytes) -> None:
        """将接收到的原始字节写入录制文件（若录制中）"""
        if not data:
            return
        with self._record_lock:
            if self._record_file is not None:
                try:
                    self._record_file.write(data)
                    self._record_bytes += len(data)
                except Exception as e:
                    self._event_manager.log(
                        "error", f"Serial 录制写入失败: {e}")
                    self._close_record_unlocked()

    @staticmethod
    def parse_hex(hex_str: str) -> bytes:
        """将 hex 字符串解析为 bytes

        支持空格/换行分隔的 hex 字符串，如 "48 65 6C 6C 6F"
        """
        cleaned = hex_str.replace(" ", "").replace("\n", "") \
                          .replace("\r", "").replace("\t", "")
        return bytes.fromhex(cleaned)

    # ── 内部方法 ──────────────────────────────────────────

    def _close_internal(self) -> dict:
        """内部关闭方法（不加锁，由调用方负责加锁）"""
        # 1. 通知接收线程停止
        self._stop_event.set()

        # 2. 等待接收线程退出
        if self._read_thread is not None and self._read_thread.is_alive():
            self._read_thread.join(timeout=2.0)
        self._read_thread = None

        # 3. 关闭串口（此时接收线程已停止，无竞态）
        port_name = None
        if self._serial is not None:
            try:
                port_name = self._serial.port
                if self._serial.is_open:
                    self._serial.close()
            except Exception as e:
                self._event_manager.log("error", f"Serial 关闭异常: {e}")
            finally:
                self._serial = None

        self._event_manager.log(
            "info", f"Serial 已关闭: {port_name or 'unknown'}")
        self._event_manager.emit("serial.status", {
            "connectionId": self.connection_id,
            "connected": False,
            "port": port_name,
        })

        # 串口关闭时若在录制，flush 并关闭录制文件
        with self._record_lock:
            self._close_record_unlocked()

        return {"success": True}

    def _read_loop(self):
        """后台线程：持续读取串口数据

        为避免高频数据时对每个读块单独推送一条 WebSocket 消息
        （导致事件循环拥堵、应用卡死、串口驱动缓冲溢出丢数据），
        将接收数据累积到缓冲区后**批量刷新**：
          达到 16KB 或距上次刷新超过 50ms 时推送一批。
        从而把海量小块消息合并为少量大块消息，显著降低吞吐压力。
        """
        logger.info("Serial read thread started")

        accum = bytearray()
        last_flush = time.monotonic()

        while not self._stop_event.is_set():
            try:
                if self._serial is None or not self._serial.is_open:
                    break

                n = self._serial.in_waiting
                if n > 0:
                    data = self._serial.read(n)
                    if data:
                        # 录制：原始字节直接落盘，保证完整（与显示解耦）
                        self._record_write(data)
                        accum.extend(data)

                now = time.monotonic()
                if accum and (len(accum) >= _RX_FLUSH_SIZE
                              or (now - last_flush) >= _RX_FLUSH_INTERVAL):
                    self._emit_rx(bytes(accum))
                    accum.clear()
                    last_flush = now

                # 无数据时等待更久，避免 CPU 空转；有数据时小幅让出
                self._stop_event.wait(0.005 if n == 0 else 0.001)
            except SerialException as e:
                self._event_manager.log(
                    "error",
                    f"Serial 接收异常（端口可能已断开）: {e}",
                )
                self._event_manager.emit("serial.status", {
                    "connectionId": self.connection_id,
                    "connected": False,
                    "error": str(e),
                })
                break
            except Exception as e:
                self._event_manager.log("error", f"Serial 接收异常: {e}")
                break

        logger.info("Serial read thread stopped")

    def _emit_rx(self, data: bytes):
        """推送一批接收数据（JSON 事件 + Binary 帧）"""
        hex_str = data.hex().upper()
        timestamp = datetime.now().isoformat(timespec="milliseconds")

        # JSON 事件（控制通道，批量推送）
        self._event_manager.emit("serial.data", {
            "connectionId": self.connection_id,
            "direction": "rx",
            "data": hex_str,
            "timestamp": timestamp,
        })

        # Binary 帧（高频数据通道，data_type=0 = serial_rx）
        self._event_manager.emit_binary(data, data_type=0)


class SerialManager:
    """串口多实例管理器

    维护 connection_id -> SerialBackend 的映射，支持多个串口
    同时打开（每个 Tab 对应一个独立串口实例）。
    """

    def __init__(self, event_manager):
        self._event_manager = event_manager
        self._backends: dict[str, SerialBackend] = {}
        self._lock = threading.Lock()

    def _get(self, connection_id: str) -> SerialBackend:
        """获取或创建指定 connection_id 的后端实例"""
        with self._lock:
            backend = self._backends.get(connection_id)
            if backend is None:
                backend = SerialBackend(self._event_manager, connection_id)
                self._backends[connection_id] = backend
            return backend

    def list_ports(self) -> list[dict]:
        """枚举可用串口"""
        return SerialBackend(self._event_manager).list_ports()

    def open(self, connection_id: str, config: dict) -> dict:
        """打开指定串口实例"""
        return self._get(connection_id).open(config)

    def close(self, connection_id: str) -> dict:
        """关闭指定串口实例"""
        with self._lock:
            backend = self._backends.get(connection_id)
            if backend is None:
                return {"success": True}
            self._backends.pop(connection_id, None)
        return backend.close()

    def send(self, connection_id: str, data: bytes) -> dict:
        """向指定串口实例发送数据"""
        return self._get(connection_id).send(data)

    def update(self, connection_id: str, patch: dict) -> dict:
        """运行时更新指定串口实例的参数"""
        with self._lock:
            backend = self._backends.get(connection_id)
        if backend is None:
            return {"success": False, "error": "串口未打开"}
        return backend.update(patch)

    def start_record(self, connection_id: str, path: str) -> dict:
        """开始录制指定实例的接收数据"""
        return self._get(connection_id).start_record(path)

    def stop_record(self, connection_id: str) -> dict:
        """停止录制指定实例的接收数据"""
        return self._get(connection_id).stop_record()

    def get_status(self, connection_id: str) -> dict:
        """获取指定串口实例状态"""
        with self._lock:
            backend = self._backends.get(connection_id)
        if backend is None:
            return {"connection_id": connection_id, "connected": False}
        return backend.get_status()

    def get_all_status(self) -> list[dict]:
        """获取所有串口实例状态"""
        with self._lock:
            backends = list(self._backends.values())
        return [b.get_status() for b in backends]

    def close_all(self) -> None:
        """关闭所有串口实例（应用退出时调用）"""
        with self._lock:
            backends = list(self._backends.items())
            self._backends.clear()
        for _, backend in backends:
            backend.close()
