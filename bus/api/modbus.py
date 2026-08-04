"""Modbus 调试 REST API

提供 Modbus RTU 主站的连接、读写、轮询等 REST 接口。
路由前缀: /api/modbus
"""

import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.events import event_manager
from core.modbus_backend import ModbusBackend

logger = logging.getLogger(__name__)
router = APIRouter()

# Modbus 后端单例
modbus_backend = ModbusBackend(event_manager)


# ── 请求模型 ──────────────────────────────────────────────

class ModbusConnectRequest(BaseModel):
    """连接 Modbus RTU 从站请求"""
    port: str = Field(..., description="串口端口名，如 COM3")
    baudrate: int = Field(9600, description="波特率")
    bytesize: int = Field(8, description="数据位 (5/6/7/8)")
    parity: str = Field("N", description="校验位 (N/E/O/M/S)")
    stopbits: int = Field(1, description="停止位 (1/1.5/2)")
    slave_id: int = Field(1, description="从站地址 (1-247)")
    timeout: float = Field(1.0, description="响应超时 (s)")


class ModbusReadRequest(BaseModel):
    """读取寄存器请求"""
    type: str = Field(..., description="寄存器类型: holding/input/coil/discrete")
    address: int = Field(0, description="起始地址 (0-based)")
    count: int = Field(1, description="读取数量")


class ModbusWriteRequest(BaseModel):
    """写入寄存器请求"""
    type: str = Field(..., description="寄存器类型: holding/coil")
    address: int = Field(0, description="起始地址 (0-based)")
    values: list = Field(..., description="写入值列表（寄存器为整数，线圈为 0/1）")


class ModbusPollingRequest(BaseModel):
    """轮询请求"""
    type: str = Field(..., description="寄存器类型: holding/input/coil/discrete")
    address: int = Field(0, description="起始地址 (0-based)")
    count: int = Field(1, description="读取数量")
    interval: int = Field(1000, description="轮询间隔 (ms)，最小 100")


# ── 路由 ──────────────────────────────────────────────────

@router.post("/connect")
async def connect(req: ModbusConnectRequest):
    """连接 Modbus RTU 从站"""
    config = {
        "port": req.port,
        "baudrate": req.baudrate,
        "bytesize": req.bytesize,
        "parity": req.parity,
        "stopbits": req.stopbits,
        "slave_id": req.slave_id,
        "timeout": req.timeout,
    }
    return modbus_backend.connect(config)


@router.post("/disconnect")
async def disconnect():
    """断开连接"""
    return modbus_backend.disconnect()


@router.post("/read")
async def read_registers(req: ModbusReadRequest):
    """读取寄存器/线圈"""
    return modbus_backend.read_registers(req.type, req.address, req.count)


@router.post("/write")
async def write_registers(req: ModbusWriteRequest):
    """写入寄存器/线圈"""
    return modbus_backend.write_registers(req.type, req.address, req.values)


@router.post("/polling/start")
async def start_polling(req: ModbusPollingRequest):
    """启动轮询"""
    config = {
        "type": req.type,
        "address": req.address,
        "count": req.count,
        "interval": req.interval,
    }
    return modbus_backend.start_polling(config)


@router.post("/polling/stop")
async def stop_polling():
    """停止轮询"""
    return modbus_backend.stop_polling()


@router.get("/status")
async def get_status():
    """获取 Modbus 状态"""
    return modbus_backend.get_status()