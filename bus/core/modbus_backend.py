"""Modbus 调试后端服务（RTU 主站）

基于 pymodbus 实现 Modbus RTU 主站会话管理，支持：
  - 通过串口连接从站
  - 读取保持寄存器 / 输入寄存器 / 线圈 / 离散输入
  - 写入单/多个寄存器 / 线圈
  - 周期轮询（后台线程）

TCP 从站模式暂未实现（占位），后续开发。

事件推送:
  - modbus.status (JSON): {connected, transport, error?}
  - modbus.data (JSON):   {slaveId, functionCode, address, count, direction, data[]}
  - modbus.polling (JSON): {slaveId, type, address, count, data[]}
  - log (JSON):           日志事件
"""

import threading
import logging
from datetime import datetime

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

logger = logging.getLogger(__name__)

# 支持的寄存器类型（与 pymodbus 功能码对应）
REGISTER_TYPES = ("holding", "input", "coil", "discrete")

# 功能码
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_READ_COIL = 0x01
FC_READ_DISCRETE = 0x02
FC_WRITE_SINGLE_COIL = 0x05
FC_WRITE_SINGLE_REGISTER = 0x06
FC_WRITE_MULTIPLE_COILS = 0x0F
FC_WRITE_MULTIPLE_REGISTERS = 0x10

# 单次最大读取数量（Modbus 协议限制）
MAX_READ_BYTES = 2000
MAX_READ_REGISTERS = 125
MAX_READ_COILS = 2000
MAX_WRITE_REGISTERS = 123
MAX_WRITE_COILS = 1968


class ModbusBackend:
    """Modbus RTU 主站后端管理器"""

    def __init__(self, event_manager):
        self._client: ModbusSerialClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._event_manager = event_manager
        self._lock = threading.Lock()
        self._config: dict = {}
        self._polling_config: dict = {}

    # ── 状态与连接 ──────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_socket_open()

    def get_status(self) -> dict:
        """获取当前会话状态"""
        if self.is_connected:
            return {
                "connected": True,
                "transport": "rtu",
                "port": self._config.get("port"),
                "baudrate": self._config.get("baudrate"),
                "slave_id": self._config.get("slave_id"),
                "polling": self._poll_thread is not None and self._poll_thread.is_alive(),
            }
        return {"connected": False, "transport": "rtu"}

    def connect(self, config: dict) -> dict:
        """建立 RTU 连接

        配置参数:
            port: str   - 串口端口名
            baudrate    - 波特率
            bytesize    - 数据位
            parity      - 校验位
            stopbits    - 停止位
            slave_id    - 从站地址 (1-247)
            timeout     - 响应超时 (s)
        """
        with self._lock:
            if self.is_connected():
                self._disconnect_internal()

            port = config.get("port")
            if not port:
                return {"success": False, "error": "未指定串口端口"}

            slave_id = int(config.get("slave_id", 1))
            if not 1 <= slave_id <= 247:
                return {"success": False, "error": f"从站地址无效: {slave_id}（应为 1-247）"}

            try:
                self._client = ModbusSerialClient(
                    port=port,
                    baudrate=int(config.get("baudrate", 9600)),
                    bytesize=int(config.get("bytesize", 8)),
                    parity=str(config.get("parity", "N")).upper(),
                    stopbits=int(config.get("stopbits", 1)),
                    timeout=float(config.get("timeout", 1.0)),
                    retries=0,
                )
                if not self._client.connect():
                    raise ModbusException("串口连接失败（可能被占用或参数错误）")
            except Exception as e:
                error_msg = str(e)
                self._client = None
                self._event_manager.log("error", f"Modbus 连接失败 [{port}]: {error_msg}")
                return {"success": False, "error": error_msg}

            self._config = {
                "port": port,
                "baudrate": int(config.get("baudrate", 9600)),
                "slave_id": slave_id,
            }
            self._stop_event.clear()
            self._event_manager.log(
                "info",
                f"Modbus RTU 已连接: {port} @ {self._config['baudrate']}bps, slave={slave_id}",
            )
            self._event_manager.emit("modbus.status", {
                "connected": True,
                "transport": "rtu",
                "port": port,
            })
            return {"success": True}

    def disconnect(self) -> dict:
        """断开连接"""
        with self._lock:
            return self._disconnect_internal()

    def _disconnect_internal(self) -> dict:
        """内部断开（不加锁）"""
        self._stop_polling_internal()
        port_name = self._config.get("port")
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                self._event_manager.log("error", f"Modbus 关闭异常: {e}")
            finally:
                self._client = None
        self._event_manager.log("info", f"Modbus 已断开: {port_name or 'unknown'}")
        self._event_manager.emit("modbus.status", {
            "connected": False,
            "transport": "rtu",
            "port": port_name,
        })
        return {"success": True}

    # ── 读写操作 ──────────────────────────────────────────

    def read_registers(self, type_: str, address: int, count: int) -> dict:
        """读取寄存器/线圈

        Args:
            type_: holding / input / coil / discrete
            address: 起始地址
            count: 数量

        返回: {success, data?: [值], error?}
        """
        if not self.is_connected():
            return {"success": False, "error": "未连接从站"}
        if type_ not in REGISTER_TYPES:
            return {"success": False, "error": f"未知寄存器类型: {type_}"}
        if count < 1:
            return {"success": False, "error": "读取数量必须 ≥ 1"}

        slave_id = self._config["slave_id"]
        try:
            if type_ in ("holding", "input"):
                if count > MAX_READ_REGISTERS:
                    return {"success": False, "error": f"单次最多读取 {MAX_READ_REGISTERS} 个寄存器"}
                fn = (self._client.read_holding_registers
                      if type_ == "holding" else self._client.read_input_registers)
                rr = fn(address, count=count, slave=slave_id)
                self._check_response(rr, type_)
                data = list(rr.registers)
            else:
                if count > MAX_READ_COILS:
                    return {"success": False, "error": f"单次最多读取 {MAX_READ_COILS} 个线圈"}
                fn = (self._client.read_coils
                      if type_ == "coil" else self._client.read_discrete_inputs)
                rr = fn(address, count=count, slave=slave_id)
                self._check_response(rr, type_)
                data = [1 if b else 0 for b in rr.bits]

            self._emit_read(type_, address, data)
            return {"success": True, "data": data}
        except (ModbusException, Exception) as e:
            error_msg = str(e)
            self._event_manager.log("error", f"Modbus 读取失败: {error_msg}")
            return {"success": False, "error": error_msg}

    def write_registers(self, type_: str, address: int, values: list) -> dict:
        """写入寄存器/线圈

        Args:
            type_: holding / coil
            address: 起始地址
            values: 值列表（寄存器为整数，线圈为 0/1）

        返回: {success, error?}
        """
        if not self.is_connected():
            return {"success": False, "error": "未连接从站"}
        if type_ not in ("holding", "coil"):
            return {"success": False, "error": f"仅支持写入 holding / coil，收到 {type_}"}
        if not values:
            return {"success": False, "error": "写入值不能为空"}

        slave_id = self._config["slave_id"]
        try:
            if type_ == "holding":
                if len(values) > MAX_WRITE_REGISTERS:
                    return {"success": False, "error": f"单次最多写入 {MAX_WRITE_REGISTERS} 个寄存器"}
                if len(values) == 1:
                    self._client.write_register(address, int(values[0]), slave=slave_id)
                else:
                    self._client.write_registers(address, [int(v) for v in values], slave=slave_id)
            else:
                if len(values) > MAX_WRITE_COILS:
                    return {"success": False, "error": f"单次最多写入 {MAX_WRITE_COILS} 个线圈"}
                bools = [bool(v) for v in values]
                if len(values) == 1:
                    self._client.write_coil(address, bools[0], slave=slave_id)
                else:
                    self._client.write_coils(address, bools, slave=slave_id)

            self._event_manager.log(
                "info", f"Modbus 写入 {type_} @ {address} 共 {len(values)} 个: {values}")
            return {"success": True}
        except (ModbusException, Exception) as e:
            error_msg = str(e)
            self._event_manager.log("error", f"Modbus 写入失败: {error_msg}")
            return {"success": False, "error": error_msg}

    # ── 轮询 ──────────────────────────────────────────────

    def start_polling(self, config: dict) -> dict:
        """启动轮询

        配置参数:
            type_: 寄存器类型
            address: 起始地址
            count: 数量
            interval: 轮询间隔 (ms)
        """
        if not self.is_connected():
            return {"success": False, "error": "未连接从站"}
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return {"success": False, "error": "轮询已在运行"}

        type_ = config.get("type")
        if type_ not in REGISTER_TYPES:
            return {"success": False, "error": f"未知寄存器类型: {type_}"}
        address = int(config.get("address", 0))
        count = int(config.get("count", 1))
        interval = float(config.get("interval", 1000))
        if count < 1:
            return {"success": False, "error": "轮询数量必须 ≥ 1"}
        if interval < 100:
            return {"success": False, "error": "轮询间隔不能小于 100ms"}

        self._polling_config = {
            "type": type_,
            "address": address,
            "count": count,
            "interval": interval,
        }
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="modbus-poll",
            daemon=True,
        )
        self._poll_thread.start()
        self._event_manager.log(
            "info",
            f"Modbus 轮询已启动: {type_} @ {address} x {count}, 间隔 {int(interval)}ms",
        )
        return {"success": True}

    def stop_polling(self) -> dict:
        """停止轮询"""
        with self._lock:
            return self._stop_polling_internal()

    def _stop_polling_internal(self) -> dict:
        """内部停止轮询（不加锁）"""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._stop_event.set()
            self._poll_thread.join(timeout=2.0)
            self._event_manager.log("info", "Modbus 轮询已停止")
        self._poll_thread = None
        return {"success": True}

    def _poll_loop(self):
        """后台线程：周期轮询读取寄存器"""
        logger.info("Modbus poll thread started")
        cfg = self._polling_config
        while not self._stop_event.is_set():
            try:
                rr = self._do_read(cfg["type"], cfg["address"], cfg["count"])
                if rr is not None:
                    self._event_manager.emit("modbus.polling", {
                        "slaveId": self._config["slave_id"],
                        "type": cfg["type"],
                        "address": cfg["address"],
                        "data": rr,
                        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                    })
            except Exception as e:
                self._event_manager.log("error", f"Modbus 轮询异常: {e}")
            self._stop_event.wait(cfg["interval"] / 1000.0)
        logger.info("Modbus poll thread stopped")

    def _do_read(self, type_: str, address: int, count: int) -> list | None:
        """轮询读取，仅返回值列表（不额外推送 modbus.data，避免与 polling 重复）"""
        if not self.is_connected():
            return None
        try:
            if type_ in ("holding", "input"):
                fn = (self._client.read_holding_registers
                      if type_ == "holding" else self._client.read_input_registers)
                rr = fn(address, count=count, slave=self._config["slave_id"])
                self._check_response(rr, type_)
                return list(rr.registers)
            else:
                fn = (self._client.read_coils
                      if type_ == "coil" else self._client.read_discrete_inputs)
                rr = fn(address, count=count, slave=self._config["slave_id"])
                self._check_response(rr, type_)
                return [1 if b else 0 for b in rr.bits]
        except Exception:
            return None

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _check_response(rr, type_: str):
        """校验响应，异常时抛出"""
        if rr is None:
            raise ModbusException(f"读取 {type_} 无响应（可能从站地址错误或超时）")
        if rr.isError():
            raise ModbusException(f"从站返回错误: {rr}")

    def _emit_read(self, type_: str, address: int, data: list):
        """推送读取数据事件"""
        self._event_manager.emit("modbus.data", {
            "slaveId": self._config["slave_id"],
            "functionCode": {
                "holding": FC_READ_HOLDING,
                "input": FC_READ_INPUT,
                "coil": FC_READ_COIL,
                "discrete": FC_READ_DISCRETE,
            }[type_],
            "address": address,
            "count": len(data),
            "direction": "rx",
            "data": data,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        })