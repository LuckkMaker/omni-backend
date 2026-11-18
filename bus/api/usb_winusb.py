"""WinUSB REST API

路由前缀: /api/usb/winusb

设备接口必须绑定 WinUSB（MS OS 2.0 描述符或 INF）。控制/批量/中断传输与
回环吞吐都在这里，管道控制（abort/flush/reset）同样暴露。
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.usb_winusb import MAX_TRANSFER, WinUsbManager

logger = logging.getLogger(__name__)
router = APIRouter()
winusb_manager = WinUsbManager()


class OpenRequest(BaseModel):
    id: str = Field("cli")
    path: str | None = None
    vid: int | None = Field(None, ge=0, le=0xFFFF)
    pid: int | None = Field(None, ge=0, le=0xFFFF)
    interface: int = Field(0, ge=0, le=255)
    timeout_ms: int = Field(1000, ge=1, le=60000)
    guids: list[str] | None = Field(None, description="覆盖默认设备接口 GUID")


class SessionRequest(BaseModel):
    id: str = Field("cli")


class ControlRequest(BaseModel):
    id: str = Field("cli")
    request_type: int = Field(..., ge=0, le=0xFF, description="bmRequestType，如 0x80")
    request: int = Field(..., ge=0, le=0xFF)
    value: int = Field(0, ge=0, le=0xFFFF)
    index: int = Field(0, ge=0, le=0xFFFF)
    length: int = Field(0, ge=0, le=MAX_TRANSFER)
    hex: str | None = None
    dataB64: str | None = None


class TransferRequest(BaseModel):
    id: str = Field("cli")
    pipe: int = Field(..., ge=0, le=0xFF, description="端点地址，如 0x81")
    direction: str = Field(..., pattern="^(in|out)$")
    length: int = Field(0, ge=0, le=MAX_TRANSFER, description="IN 传输长度")
    hex: str | None = None
    dataB64: str | None = None


class PipeRequest(BaseModel):
    id: str = Field("cli")
    pipe: int = Field(..., ge=0, le=0xFF)
    action: str = Field(..., pattern="^(abort|flush|reset)$")


class EchoRequest(BaseModel):
    id: str = Field("cli")
    out_pipe: int = Field(..., ge=0, le=0xFF)
    in_pipe: int = Field(..., ge=0, le=0xFF)
    size: int = Field(64, ge=1, le=MAX_TRANSFER)
    count: int = Field(100, ge=0, description="0 = 一直跑到失败或取消")
    pipeline: int = Field(1, ge=1, le=1024)
    pattern: str = Field("increment", pattern="^(increment|random|zeros|aa|ff)$")
    seed: int = 0


def _payload(hex_text: str | None, b64: str | None) -> bytes | None:
    if b64:
        try:
            return base64.b64decode(b64, validate=True)
        except Exception:  # noqa: BLE001
            return None
    if hex_text:
        try:
            return bytes.fromhex(
                hex_text.replace("0x", " ").replace(",", " ").replace(" ", "")
            )
        except ValueError:
            return None
    return b""


@router.get("/devices")
async def winusb_devices(guid: list[str] | None = None):
    """枚举绑定 WinUSB 的接口"""
    return winusb_manager.devices(guid)


@router.get("/all-devices")
async def winusb_all_devices(service: str | None = None):
    """列出所有 USB 设备及驱动服务名（判断"没枚举"还是"驱动没绑定"）"""
    return winusb_manager.all_devices(service)


@router.post("/open")
async def winusb_open(req: OpenRequest):
    return winusb_manager.open(
        req.id,
        path=req.path,
        vid=req.vid,
        pid=req.pid,
        interface=req.interface,
        timeout_ms=req.timeout_ms,
        guids=req.guids,
    )


@router.post("/close")
async def winusb_close(req: SessionRequest):
    return winusb_manager.close(req.id)


@router.get("/status")
async def winusb_status(id: str = "cli"):
    return winusb_manager.status(id)


@router.post("/control")
async def winusb_control(req: ControlRequest):
    data = _payload(req.hex, req.dataB64)
    if data is None:
        return {"success": False, "code": "winusb_invalid", "error": "hex/dataB64 无效"}
    return winusb_manager.control(
        req.id,
        request_type=req.request_type,
        request=req.request,
        value=req.value,
        index=req.index,
        length=req.length,
        data=data,
    )


@router.post("/transfer")
async def winusb_transfer(req: TransferRequest):
    if req.direction == "in":
        return winusb_manager.read(req.id, req.pipe, req.length)
    data = _payload(req.hex, req.dataB64)
    if data is None:
        return {"success": False, "code": "winusb_invalid", "error": "hex/dataB64 无效"}
    if not data:
        return {"success": False, "code": "winusb_invalid", "error": "OUT 传输需要数据"}
    return winusb_manager.write(req.id, req.pipe, data)


@router.post("/pipe")
async def winusb_pipe(req: PipeRequest):
    return winusb_manager.pipe(req.id, req.pipe, req.action)


@router.post("/echo")
async def winusb_echo(req: EchoRequest):
    return winusb_manager.echo(
        req.id,
        out_pipe=req.out_pipe,
        in_pipe=req.in_pipe,
        size=req.size,
        count=req.count,
        pipeline=req.pipeline,
        pattern=req.pattern,
        seed=req.seed,
    )
