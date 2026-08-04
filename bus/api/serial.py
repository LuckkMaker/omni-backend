"""串口调试 REST API

提供串口枚举、开关、收发等 REST 接口。
路由前缀: /api/serial
"""

import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.events import event_manager
from core.serial_backend import SerialBackend

logger = logging.getLogger(__name__)
router = APIRouter()

# 串口后端单例
serial_backend = SerialBackend(event_manager)


# ── 请求模型 ──────────────────────────────────────────────

class SerialOpenRequest(BaseModel):
    """打开串口请求"""
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
    data: str = Field(..., description="hex 字符串，如 '48656C6C6F'")


# ── 路由 ──────────────────────────────────────────────────

@router.get("/ports")
async def list_ports():
    """列出可用串口"""
    ports = serial_backend.list_ports()
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
    return serial_backend.open(config)


@router.post("/close")
async def close_serial():
    """关闭串口"""
    return serial_backend.close()


@router.post("/send")
async def send_data(req: SerialSendRequest):
    """发送数据（hex 字符串）"""
    try:
        data = SerialBackend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}

    return serial_backend.send(data)


@router.get("/status")
async def get_status():
    """获取串口状态"""
    return serial_backend.get_status()
