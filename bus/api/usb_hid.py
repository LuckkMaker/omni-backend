"""USB HID REST API

路由前缀: /api/usb/hid

HID 走系统 HID 驱动（免安装），只能收发报告；任意 SETUP 需要 WinUSB/UsbDk。
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.usb_hid import HidManager, HidUnavailable

logger = logging.getLogger(__name__)
router = APIRouter()
hid_manager = HidManager()

MAX_PAYLOAD = 64 * 1024


class HidOpenRequest(BaseModel):
    id: str = Field("cli", description="会话 ID")
    vid: int = Field(..., ge=0, le=0xFFFF, description="VID，如 0xC251")
    pid: int = Field(..., ge=0, le=0xFFFF, description="PID")
    serial: str | None = Field(None, description="序列号（同型号多设备时区分）")
    path: str | None = Field(None, description="设备接口路径（优先于 VID/PID）")
    timeout_ms: int = Field(1000, ge=1, le=60000, description="报告收发超时")


class HidSessionRequest(BaseModel):
    id: str = Field("cli")


class HidReadRequest(BaseModel):
    id: str = Field("cli")
    length: int | None = Field(None, ge=1, le=8192, description="默认取 InputReportByteLength")
    timeout_ms: int | None = Field(None, ge=1, le=60000)


class HidWriteRequest(BaseModel):
    id: str = Field("cli")
    hex: str | None = Field(None, description="报告载荷（不含 Report ID）")
    dataB64: str | None = None
    report_id: int = Field(0, ge=0, le=255)
    timeout_ms: int | None = Field(None, ge=1, le=60000)


class HidFeatureRequest(BaseModel):
    id: str = Field("cli")
    hex: str | None = None
    dataB64: str | None = None
    report_id: int = Field(0, ge=0, le=255)


def _payload(req) -> bytes | None:
    if req.dataB64:
        try:
            return base64.b64decode(req.dataB64, validate=True)
        except Exception:  # noqa: BLE001
            return None
    if req.hex:
        try:
            return bytes.fromhex(req.hex.replace("0x", " ").replace(",", " ").replace(" ", ""))
        except ValueError:
            return None
    return b""


@router.get("/devices")
async def hid_devices():
    """枚举 HID 设备（VID/PID/产品名/报告长度）"""
    try:
        return {"success": True, "devices": hid_manager.devices()}
    except HidUnavailable as exc:
        return {"success": False, "code": "hid_unavailable", "error": str(exc)}


@router.post("/open")
async def hid_open(req: HidOpenRequest):
    """打开 HID 设备（独占）"""
    if req.vid is None or req.pid is None:
        return {"success": False, "code": "hid_invalid", "error": "需要 vid/pid"}
    return hid_manager.open(
        req.id,
        vid=req.vid,
        pid=req.pid,
        serial=req.serial,
        path=req.path,
        timeout_ms=req.timeout_ms,
    )


@router.post("/close")
async def hid_close(req: HidSessionRequest):
    return hid_manager.close(req.id)


@router.get("/status")
async def hid_status(id: str = "cli"):
    return hid_manager.status(id)


@router.post("/read")
async def hid_read(req: HidReadRequest):
    if req.length is not None and req.length > MAX_PAYLOAD:
        return {"success": False, "code": "hid_invalid", "error": "length 过大"}
    return hid_manager.read(req.id, length=req.length, timeout_ms=req.timeout_ms)


@router.post("/write")
async def hid_write(req: HidWriteRequest):
    data = _payload(req)
    if data is None:
        return {"success": False, "code": "hid_invalid", "error": "hex/dataB64 无效"}
    return hid_manager.write(
        req.id, data, report_id=req.report_id, timeout_ms=req.timeout_ms
    )


@router.post("/feature/get")
async def hid_feature_get(req: HidFeatureRequest):
    return hid_manager.get_feature(req.id, req.report_id)


@router.post("/feature/set")
async def hid_feature_set(req: HidFeatureRequest):
    data = _payload(req)
    if data is None:
        return {"success": False, "code": "hid_invalid", "error": "hex/dataB64 无效"}
    return hid_manager.set_feature(req.id, data, req.report_id)
