"""系统信息 REST API

提供后端版本、运行环境等信息，供前端设置页展示。
"""

import platform
import logging
from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/system/info")
async def system_info(request: Request):
    """返回后端运行环境版本信息"""
    try:
        backend_version = request.app.version
    except Exception:
        backend_version = "unknown"

    return {
        "app_version": backend_version,
        "backend_version": backend_version,
        "python_version": platform.python_version(),
        "platform": platform.system(),
    }


@router.get("/system/serial-ports")
async def list_serial_ports():
    """列出可用串口"""
    try:
        from serial.tools import list_ports
        ports = []
        for port in list_ports.comports():
            ports.append({
                "device": port.device,
                "description": port.description,
                "hwid": port.hwid,
                "manufacturer": port.manufacturer if hasattr(port, 'manufacturer') else None,
                "vid": f"0x{port.vid:04X}" if port.vid else None,
                "pid": f"0x{port.pid:04X}" if port.pid else None,
            })
        return {"ports": ports}
    except ImportError:
        return {"ports": [], "error": "pyserial not installed"}
    except Exception as e:
        return {"ports": [], "error": str(e)}
