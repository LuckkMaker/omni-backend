"""串口调试 REST API

提供串口枚举、开关、收发等 REST 接口。
支持多实例（多 Tab 同时调试不同串口设备），每个实例用 connection_id 标识。
路由前缀: /api/serial
"""

import logging
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.events import event_manager
from core.serial_backend import SerialBackend, SerialManager

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
    """列出可用串口"""
    ports = serial_manager.list_ports()
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
    return serial_manager.open(req.connection_id, config)


@router.post("/close")
async def close_serial(req: SerialCloseRequest):
    """关闭指定串口"""
    return serial_manager.close(req.connection_id)


@router.post("/send")
async def send_data(req: SerialSendRequest):
    """发送数据（hex 字符串）"""
    try:
        data = SerialBackend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}

    return serial_manager.send(req.connection_id, data)


@router.post("/record/start")
async def start_record(req: SerialRecordStartRequest):
    """开始录制接收数据到文件（后端落盘）"""
    return serial_manager.start_record(req.connection_id, req.path)


@router.post("/record/stop")
async def stop_record(req: SerialRecordStopRequest):
    """停止录制接收数据"""
    return serial_manager.stop_record(req.connection_id)


@router.get("/status")
async def get_status(connection_id: str = Query(default="", description="连接实例 ID")):
    """获取串口状态"""
    if connection_id:
        return serial_manager.get_status(connection_id)
    return {"instances": serial_manager.get_all_status()}