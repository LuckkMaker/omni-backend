"""串口调试后端服务

基于 pyserial 实现串口管理，支持端口枚举、收发数据、
后台线程接收并通过 event_manager 推送到前端。

事件推送:
  - serial.data (JSON):   {direction, data(hex), timestamp}
  - serial.status (JSON):  {connected, port?, error?}
  - log (JSON):            日志事件（串口打开/关闭/错误等）
  - Binary 帧:             高频数据通道 (data_type=0 = serial_rx)
"""

import threading
import logging
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


class SerialBackend:
    """串口后端管理器

    管理串口连接生命周期，后台线程接收数据，
    通过 event_manager 推送到前端。
    """

    def __init__(self, event_manager):
        self._serial: serial.Serial | None = None
        self._read_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._event_manager = event_manager
        self._lock = threading.Lock()

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

    def get_status(self) -> dict:
        """获取串口状态"""
        if self.is_open() and self._serial is not None:
            return {
                "connected": True,
                "port": self._serial.port,
                "baudrate": self._serial.baudrate,
                "bytesize": self._serial.bytesize,
                "parity": self._serial.parity,
                "stopbits": self._serial.stopbits,
            }
        return {"connected": False}

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
            "connected": False,
            "port": port_name,
        })

        return {"success": True}

    def _read_loop(self):
        """后台线程：持续读取串口数据

        收到数据后同时推送 JSON 事件和 Binary 帧，
        遇到串口异常（断开等）时推送错误日志并退出。
        """
        logger.info("Serial read thread started")

        while not self._stop_event.is_set():
            try:
                if self._serial is None or not self._serial.is_open:
                    break

                n = self._serial.in_waiting
                if n > 0:
                    data = self._serial.read(n)
                    if data:
                        hex_str = data.hex().upper()
                        timestamp = datetime.now().isoformat(
                            timespec="milliseconds")

                        # JSON 事件（低频控制通道）
                        self._event_manager.emit("serial.data", {
                            "direction": "rx",
                            "data": hex_str,
                            "timestamp": timestamp,
                        })

                        # Binary 帧（高频数据通道，data_type=0 = serial_rx）
                        self._event_manager.emit_binary(data, data_type=0)
                else:
                    # 短暂等待，避免 CPU 空转；stop_event.set() 可立即唤醒
                    self._stop_event.wait(0.01)
            except SerialException as e:
                self._event_manager.log(
                    "error",
                    f"Serial 接收异常（端口可能已断开）: {e}",
                )
                self._event_manager.emit("serial.status", {
                    "connected": False,
                    "error": str(e),
                })
                break
            except Exception as e:
                self._event_manager.log("error", f"Serial 接收异常: {e}")
                break

        logger.info("Serial read thread stopped")
