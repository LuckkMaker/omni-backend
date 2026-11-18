"""USB 回归用例 REST API

路由前缀: /api/usb/test

- 用例是 JSON（pcap + expect 断言），执行器在 ``core/usb_test.py``；
- 只读离线抓包，不需要 USBPcap / WinUSB，可直接进 CI；
- ``junit=True`` 时响应里带 ``junit`` 字符串，由调用方落盘。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.usb_test import CaseError, case_schema, run_case, to_junit

logger = logging.getLogger(__name__)
router = APIRouter()

#: 用例 JSON 文件大小上限
MAX_CASE_BYTES = 1 * 1024 * 1024


class RunRequest(BaseModel):
    case: dict | None = Field(None, description="用例对象（优先于 path）")
    path: str | None = Field(None, description="用例 JSON 文件路径")
    junit: bool = Field(False, description="响应中附带 JUnit XML")
    stopOnFailure: bool = Field(False, description="首个失败后停止剩余步骤")


@router.get("/schema")
async def schema():
    """用例格式示例"""
    return {"example": case_schema()}


@router.post("/run")
def run(req: RunRequest):  # sync：解析 pcap 是阻塞操作，交给线程池
    """执行用例，返回步骤级报告"""
    case = req.case
    if case is None:
        if not req.path:
            return {
                "success": False,
                "code": "case_missing",
                "error": "需要 case 或 path 之一",
            }
        source = Path(req.path).expanduser()
        if not source.is_file():
            return {
                "success": False,
                "code": "case_not_found",
                "error": f"用例文件不存在：{req.path}",
            }
        try:
            if source.stat().st_size > MAX_CASE_BYTES:
                return {
                    "success": False,
                    "code": "case_too_large",
                    "error": f"用例文件超过上限（{MAX_CASE_BYTES} 字节）",
                }
            case = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            return {"success": False, "code": "case_read_failed", "error": str(exc)}
        except json.JSONDecodeError as exc:
            return {
                "success": False,
                "code": "case_invalid",
                "error": f"用例不是合法 JSON：{exc}",
            }

    try:
        report = run_case(case, stop_on_failure=req.stopOnFailure)
    except CaseError as exc:
        return {"success": False, "code": "case_invalid", "error": str(exc)}
    except OSError as exc:
        return {"success": False, "code": "case_read_failed", "error": str(exc)}

    payload = {"success": True, **report}
    if req.junit:
        payload["junit"] = to_junit(report)
    return payload
