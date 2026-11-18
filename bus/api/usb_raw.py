"""UsbDk 原始访问 REST API（spike）

路由前缀: /api/usb/raw

只暴露"环境是否可用 + 设备列表"；接管设备后的 control/read/write 端点等
真机验证通过后再加（见 docs/usb-raw-access.md 的验证清单）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from core import usbdk

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/doctor")
async def raw_doctor():
    """UsbDk 安装/服务/DLL/设备可用性诊断"""
    return usbdk.doctor()


@router.get("/devices")
async def raw_devices():
    """UsbDk 可见的设备列表（接管前）"""
    result = usbdk.doctor()
    if not result.get("available"):
        return {
            "success": False,
            "code": "usbdk_unavailable",
            "error": result.get("error") or "UsbDk 不可用",
            "doctor": result,
        }
    return {"success": True, "devices": result.get("devices") or []}
