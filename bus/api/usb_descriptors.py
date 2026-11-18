"""USB 描述符 REST API

路由前缀: /api/usb/desc

说明：
  - 解析/生成/校验都走 ``core/usb_desc``（唯一实现），本层只做请求校验与错误映射；
  - 变更类接口统一返回 ``{success, code?, error?}``，与 ``api/usb.py`` 一致；
  - ``from-capture`` 只读离线 pcap，不需要 USBPcap 驱动或管理员权限。
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import usb_desc
from core.usb_decode import DescriptorCollector, decode_setup
from core.usb_pcap import IrpAssembler, PcapError, PcapStreamReader

logger = logging.getLogger(__name__)
router = APIRouter()

#: 单次解析/生成的输入上限（描述符不可能这么大，防误用）
MAX_INPUT_BYTES = 1 * 1024 * 1024
#: from-capture 允许的 pcap 上限（超过请用 GUI 或拆分文件）
MAX_PCAP_BYTES = 512 * 1024 * 1024


class ParseRequest(BaseModel):
    hex: str | None = Field(None, description="十六进制字节串，如 12 01 00 02 ...")
    dataB64: str | None = Field(None, description="base64 原始字节（优先于 hex）")


class DocumentRequest(BaseModel):
    document: dict = Field(..., description="描述符文档（JSON）")


class FromCaptureRequest(BaseModel):
    path: str = Field(..., description="pcap 文件路径")
    bus: int | None = Field(None, ge=0, description="只取指定 bus")
    device: int | None = Field(None, ge=0, description="只取指定设备地址")


def _payload_bytes(req: ParseRequest) -> bytes:
    if req.dataB64:
        try:
            return base64.b64decode(req.dataB64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise usb_desc.DescriptorError(f"dataB64 不是合法 base64：{exc}") from None
    if req.hex:
        return usb_desc.parse_hex(req.hex)
    raise usb_desc.DescriptorError("需要 hex 或 dataB64 之一")


# ── 模板 ──────────────────────────────────────────────────

@router.get("/templates")
async def templates():
    """内置模板清单"""
    return {"templates": usb_desc.templates()}


@router.get("/templates/{name}")
async def template(name: str):
    """单个模板的完整文档"""
    try:
        document = usb_desc.get_template(name)
    except usb_desc.DescriptorError as exc:
        return {"success": False, "code": "template_not_found", "error": str(exc)}
    return {"success": True, "name": name, "document": document}


# ── 解析 / 校验 / 生成 ────────────────────────────────────

@router.post("/parse")
async def parse(req: ParseRequest):
    """自动识别一段原始字节（Setup / 设备 / 配置 / 字符串 / BOS / 限定符）"""
    try:
        raw = _payload_bytes(req)
        if len(raw) > MAX_INPUT_BYTES:
            return {
                "success": False,
                "code": "input_too_large",
                "error": f"输入超过上限（{MAX_INPUT_BYTES} 字节）",
            }
        info = usb_desc.detect(raw)
        if info.get("kind") == "setup":
            info["setup"] = decode_setup(raw)
        info["success"] = True
        info["hex"] = usb_desc.hex_string(raw)
        return info
    except usb_desc.DescriptorError as exc:
        return {"success": False, "code": "desc_parse_failed", "error": str(exc)}
    except ValueError as exc:
        return {"success": False, "code": "desc_parse_failed", "error": f"解析失败：{exc}"}


@router.post("/validate")
async def validate(req: DocumentRequest):
    """按 USB 约束校验描述符文档"""
    try:
        report = usb_desc.validate_document(req.document)
    except usb_desc.DescriptorError as exc:
        return {"success": False, "code": "document_invalid", "error": str(exc)}
    return {"success": True, **report}


@router.post("/build")
async def build(req: DocumentRequest):
    """由 JSON 文档生成 hex（同时返回校验结果）"""
    try:
        built = usb_desc.build_document(req.document)
        report = usb_desc.validate_document(req.document)
    except usb_desc.DescriptorError as exc:
        return {"success": False, "code": "document_invalid", "error": str(exc)}
    return {"success": True, **built, "validation": report}


# ── 从抓包反推 ────────────────────────────────────────────

@router.post("/from-capture")
async def from_capture(req: FromCaptureRequest):
    """从离线 pcap 反推描述符文档（抓包 → 文档 → 改 → 生成）"""
    try:
        documents = _documents_from_pcap(req.path, req.bus, req.device)
    except FileNotFoundError:
        return {
            "success": False,
            "code": "pcap_not_found",
            "error": f"文件不存在：{req.path}",
        }
    except OSError as exc:
        return {"success": False, "code": "pcap_open_failed", "error": str(exc)}
    except PcapError as exc:
        return {"success": False, "code": "pcap_parse_error", "error": str(exc)}
    return {"success": True, "documents": documents}


def _documents_from_pcap(path: str, bus: int | None, device: int | None) -> list[dict]:
    from pathlib import Path

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(path)
    if source.stat().st_size > MAX_PCAP_BYTES:
        raise PcapError(f"文件超过上限（{MAX_PCAP_BYTES // (1024 * 1024)} MiB）")

    reader = PcapStreamReader()
    assembler = IrpAssembler()
    collector = DescriptorCollector()
    with open(source, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            for packet in reader.feed(chunk):
                for transfer in assembler.feed(packet):
                    collector.feed(transfer)

    documents = collector.documents()
    if bus is not None:
        documents = [item for item in documents if item["bus"] == bus]
    if device is not None:
        documents = [item for item in documents if item["device"] == device]
    return documents
