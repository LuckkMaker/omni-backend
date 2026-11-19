"""USB 分析 REST API

路由前缀: /api/usb

说明：
  - 响应字段使用 camelCase，与前端 ``src/shared/usb.ts`` 对齐
  - 变更类接口统一返回 ``{success, code?, error?}``，避免前端处理 HTTP 异常
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.events import event_manager
from core.usb_backend import UsbCaptureManager

logger = logging.getLogger(__name__)
router = APIRouter()

usb_manager = UsbCaptureManager(event_manager)


# ── 请求模型 ──────────────────────────────────────────────

class CaptureStartRequest(BaseModel):
    device_id: str = Field(..., description="设备 ID，如 \\\\.\\USBPcap1")
    usbpcap_path: str | None = Field(None, description="USBPcapCMD.exe 路径覆盖")
    snaplen: int | None = Field(None, ge=64, le=16 * 1024 * 1024, description="快照长度")
    bufferlen: int | None = Field(
        None, ge=4096, le=134217728, description="USBPcap 内部缓冲字节数"
    )
    devices: str | None = Field(
        None, description="只抓指定设备地址，逗号分隔，如 1,2,3"
    )


class CaptureSaveRequest(BaseModel):
    path: str = Field(..., description="目标文件路径（.pcap）")


class CaptureOpenRequest(BaseModel):
    path: str = Field(..., description="要打开的 pcap 文件路径")


# ── 环境/状态 ─────────────────────────────────────────────

@router.get("/environment")
async def environment(usbpcap_path: str | None = Query(None, description="USBPcapCMD.exe 路径")):
    """探测 USBPcap 安装、权限与可捕获的 root hub"""
    return usb_manager.environment(usbpcap_path)


@router.get("/capture/status")
async def capture_status():
    """当前捕获/离线解析状态与统计"""
    return usb_manager.status()


# ── 捕获控制 ──────────────────────────────────────────────

@router.post("/capture/start")
async def capture_start(req: CaptureStartRequest):
    """开始实时捕获（需要 USBPcap 驱动与管理员授权）"""
    return usb_manager.start(req.device_id, {
        "usbpcapPath": req.usbpcap_path,
        "snaplen": req.snaplen,
        "bufferlen": req.bufferlen,
        "devices": req.devices,
    })


@router.post("/capture/stop")
async def capture_stop():
    """停止捕获（幂等）"""
    return usb_manager.stop()


@router.post("/capture/save")
async def capture_save(req: CaptureSaveRequest):
    """把当前抓包文件导出到指定路径"""
    return usb_manager.save(req.path)


@router.post("/capture/open")
async def capture_open(req: CaptureOpenRequest):
    """离线打开 pcap（无需 USBPcap / 管理员权限）"""
    return usb_manager.open_file(req.path)


# ── 数据查询 ──────────────────────────────────────────────

@router.get("/transfers")
async def transfers(
    limit: int = Query(200, ge=1, le=2000),
    before_id: int | None = Query(None, ge=1, description="加载更早的传输"),
    after_id: int | None = Query(None, ge=0, description="回填缺失的传输"),
    type: str | None = Query(None, description="传输类型，逗号分隔：control,bulk,interrupt,isochronous"),
    dir: str | None = Query(None, description="in / out"),
    bus: int | None = Query(None, ge=0),
    device: int | None = Query(None, ge=0),
    endpoint: int | None = Query(None, ge=0, le=255),
    status: str | None = Query(None, description="ok / error"),
    min_length: int | None = Query(None, ge=0, alias="minLength"),
    vid: str | None = Query(None, description="按设备 VID 过滤，如 0xC251"),
    pid: str | None = Query(None, description="按设备 PID 过滤，如 0x0002"),
    q: str | None = Query(None, description="摘要/设备号文本搜索"),
):
    types = [item.strip() for item in (type or "").split(",") if item.strip()]
    filters = {
        "type": types,
        "dir": dir,
        "bus": bus,
        "device": device,
        "endpoint": endpoint,
        "status": status,
        "minLength": min_length,
        "vid": vid,
        "pid": pid,
        "q": q,
    }
    return usb_manager.query(
        limit=limit, before_id=before_id, after_id=after_id, filters=filters
    )


@router.get("/transfers/{transfer_id}")
async def transfer_detail(transfer_id: int):
    """单条传输详情（hex + 解码树），按需从抓包文件回读"""
    return usb_manager.detail(transfer_id)


@router.get("/devices")
async def devices():
    """从捕获到的描述符重建的设备/配置/接口/端点树"""
    return usb_manager.devices()


@router.post("/clear")
async def clear():
    """清空当前 ring 与描述符（不删除抓包文件）"""
    return usb_manager.clear()
