"""串口调试 REST API

提供串口枚举、开关、收发等 REST 接口。
支持多实例（多 Tab 同时调试不同串口设备），每个实例用 connection_id 标识。
路由前缀: /api/serial
"""

import asyncio
import logging
import time
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.events import event_manager
from core.serial_backend import SerialBackend, SerialManager
from core.serial_capture import CAPTURE_MAX_READ

logger = logging.getLogger(__name__)
router = APIRouter()

# 串口多实例管理器（每个 Tab/连接一个实例）
serial_manager = SerialManager(event_manager)


# ── 请求模型 ──────────────────────────────────────────────

class SerialOpenRequest(BaseModel):
    """打开串口请求"""
    connection_id: str = Field(..., description="连接实例 ID（对应前端 Tab）")
    port: str = Field(..., description="端口名，如 COM3")
    baudrate: int = Field(115200, description="波特率")
    bytesize: int = Field(8, description="数据位 (5/6/7/8)")
    parity: str = Field("N", description="校验位 (N/E/O/M/S)")
    stopbits: float = Field(1, description="停止位 (1/1.5/2)")
    flow_control: str = Field("none", description="流控 (none/xonxoff/rtscts)")
    rts: bool = Field(False, description="RTS 信号")
    dtr: bool = Field(False, description="DTR 信号")


class SerialSendRequest(BaseModel):
    """发送数据请求"""
    connection_id: str = Field(..., description="连接实例 ID（对应前端 Tab）")
    data: str = Field(..., description="hex 字符串，如 '48656C6C6F'")


class SerialCloseRequest(BaseModel):
    """关闭串口请求"""
    connection_id: str = Field(..., description="连接实例 ID（对应前端 Tab）")


class SerialUpdateRequest(BaseModel):
    """运行时更新串口参数请求（连接后在线调整，无需重开端口）"""
    connection_id: str = Field(..., description="连接实例 ID（对应前端 Tab）")
    baudrate: int | None = Field(None, description="波特率")
    rts: bool | None = Field(None, description="RTS 信号（rtscts 流控下忽略）")
    dtr: bool | None = Field(None, description="DTR 信号")


class SerialRecordStartRequest(BaseModel):
    """开始接收录制请求"""
    connection_id: str = Field(..., description="连接实例 ID（对应前端 Tab）")
    path: str = Field(..., description="录制文件保存路径（.dat）")


class SerialRecordStopRequest(BaseModel):
    """停止接收录制请求"""
    connection_id: str = Field(..., description="连接实例 ID（对应前端 Tab）")


# ── 路由 ──────────────────────────────────────────────────

@router.get("/ports")
async def list_ports():
    """列出可用串口（主机 COM 口枚举；WMI 查询可能耗时，放入线程执行）"""
    ports = await asyncio.to_thread(serial_manager.list_ports)
    return {"ports": ports}


@router.post("/open")
async def open_serial(req: SerialOpenRequest):
    """打开串口"""
    config = {
        "port": req.port,
        "baudrate": req.baudrate,
        "bytesize": req.bytesize,
        "parity": req.parity,
        "stopbits": req.stopbits,
        "flow_control": req.flow_control,
        "rts": req.rts,
        "dtr": req.dtr,
    }
    return await asyncio.to_thread(serial_manager.open, req.connection_id, config)


@router.post("/close")
async def close_serial(req: SerialCloseRequest):
    """关闭指定串口"""
    return await asyncio.to_thread(serial_manager.close, req.connection_id)


@router.post("/update")
async def update_serial(req: SerialUpdateRequest):
    """运行时更新串口参数（连接后在线调整波特率/RTS/DTR）"""
    patch: dict = {}
    if req.baudrate is not None:
        patch["baudrate"] = req.baudrate
    if req.rts is not None:
        patch["rts"] = req.rts
    if req.dtr is not None:
        patch["dtr"] = req.dtr
    return await asyncio.to_thread(serial_manager.update, req.connection_id, patch)


@router.post("/send")
async def send_data(req: SerialSendRequest):
    """发送数据（hex 字符串）"""
    try:
        data = SerialBackend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}

    return await asyncio.to_thread(serial_manager.send, req.connection_id, data)


@router.post("/record/start")
async def start_record(req: SerialRecordStartRequest):
    """开始录制接收数据到文件（后端落盘）"""
    return serial_manager.start_record(req.connection_id, req.path)


@router.post("/record/stop")
async def stop_record(req: SerialRecordStopRequest):
    """停止录制接收数据"""
    return serial_manager.stop_record(req.connection_id)


@router.get("/data")
async def read_data(
    connection_id: str = Query(default="cli", description="连接实例 ID"),
    since: int = Query(default=0, ge=0, description="起始 seq（0 = 从最旧可用数据开始）"),
    max_bytes: int = Query(default=65536, ge=1, description="单次返回的字节预算"),
):
    """按 seq 游标增量读取接收缓冲（CLI `omni serial read` 的数据源）。

    返回契约与 link 服务的 RTT 读取一致：
    {success, connection_id, running, oldest_seq, next_seq, last_seq, cursor,
     truncated, lost, session_reset, dropped_*, chunks:[{seq, size, data(base64)}]}
    读端记住 ``cursor`` 作为下一次的 ``since`` 即可连续读取，多客户端互不影响。
    """
    return await asyncio.to_thread(
        serial_manager.read_capture, connection_id, since, min(max_bytes, CAPTURE_MAX_READ)
    )


# ── 同步等读 ──────────────────────────────────────────────

class SerialSyncReadRequest(BaseModel):
    """同步等读请求：等到有数据或超时，一次返回"""
    connection_id: str = Field("cli", description="连接实例 ID")
    timeout_ms: int = Field(500, ge=0, le=30000, description="最长等待毫秒数")
    max_bytes: int = Field(65536, ge=1, description="单次返回的字节预算")
    since: int = Field(0, ge=0, description="起始 seq（0 = 从最旧可用数据开始）")


@router.post("/read")
async def read_sync(req: SerialSyncReadRequest):
    """同步等读：在 timeout_ms 内等待接收缓冲出现数据，首批到达即返回。

    用于“发一条、收一条”的交互（如 AT 命令式设备）：send 之后立刻 read，
    不必轮询。``since`` 语义与 GET /data 一致；超时返回空 chunks（不算错误）。
    """
    deadline = time.monotonic() + req.timeout_ms / 1000.0

    def _drain() -> dict:
        while True:
            result = serial_manager.read_capture(
                req.connection_id, req.since, min(req.max_bytes, CAPTURE_MAX_READ)
            )
            if not result.get("success"):
                return result
            if result.get("chunks"):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            time.sleep(min(0.01, remaining))

    return await asyncio.to_thread(_drain)


@router.get("/status")
async def get_status(connection_id: str = Query(default="", description="连接实例 ID")):
    """获取串口状态"""
    if connection_id:
        return serial_manager.get_status(connection_id)
    return {"instances": serial_manager.get_all_status()}