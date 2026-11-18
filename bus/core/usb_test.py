"""USB 声明式回归用例执行器（M4a：离线 pcap）。

用例是 JSON：一段离线抓包 + 一组 ``expect`` 断言，不依赖 WinUSB / USBPcap，
可以直接进 CI（录制 pcap → 跑用例 → JSON / JUnit 报告）。

用例格式::

    {
      "name": "enumeration-smoke",
      "pcap": "C:/captures/enum.pcap",
      "steps": [
        {"name": "有传输",     "expect": {"transfers": {"atLeast": 1}}},
        {"name": "看到设备描述符", "expect": {"transfer": {"type": "control",
                                                    "summaryContains": "GET_DESCRIPTOR(DEVICE)"}}},
        {"name": "设备身份",   "expect": {"device": {"vid": "0x1234", "pid": "0x5678"}}},
        {"name": "无错误",     "expect": {"errors": {"atMost": 0}}},
        {"name": "描述符合法", "expect": {"descriptor": {"ok": True}}}
      ]
    }

M4b（需要 usb_host）会新增 ``control`` / ``write`` / ``read`` / ``echo`` 等**动作**
步骤；本模块的步骤执行与报告结构保持不变。
"""

from __future__ import annotations

import tempfile
import time
import xml.sax.saxutils as sax
from pathlib import Path
from typing import Any, Callable

from core.usb_backend import UsbCaptureManager

#: 单条用例允许的最大断言步骤（防止误用）
MAX_STEPS = 200
#: 等待离线解析完成的默认超时（秒）
DEFAULT_TIMEOUT = 60.0
#: 单次查询页大小（后端上限 2000）
PAGE_SIZE = 2000

SUPPORTED_EXPECT = {"transfers", "transfer", "device", "errors", "descriptor"}


class CaseError(ValueError):
    """用例本身不合法（缺字段、未知断言、pcap 无法解析）。"""


class _NullEvents:
    """用例执行器不需要 WebSocket 推送。"""

    def emit(self, *_args, **_kwargs) -> None:
        return None


def run_case(
    case: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    captures_dir: Path | None = None,
    stop_on_failure: bool = False,
) -> dict[str, Any]:
    """执行一条用例，返回报告（不写文件，JUnit 由 :func:`to_junit` 生成）。"""
    if not isinstance(case, dict):
        raise CaseError("用例必须是 JSON 对象")
    name = str(case.get("name") or "usb-case")
    pcap = case.get("pcap") or (case.get("source") or {}).get("pcap")
    if not pcap:
        raise CaseError("缺少 pcap 路径（M4a 只支持离线抓包用例）")
    source = Path(str(pcap)).expanduser()
    if not source.is_file():
        raise CaseError(f"pcap 不存在：{source}")

    steps = case.get("steps")
    if not isinstance(steps, list) or not steps:
        raise CaseError("steps 必须是非空数组")
    if len(steps) > MAX_STEPS:
        raise CaseError(f"steps 超过上限（{MAX_STEPS}）")

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="omni-usb-case-") as tmp:
        manager = UsbCaptureManager(_NullEvents(), captures_dir=Path(captures_dir or tmp))
        try:
            opened = manager.open_file(str(source))
            if not opened.get("success"):
                raise CaseError(opened.get("error") or "无法打开 pcap")
            _wait_loaded(manager, timeout)
            status = manager.status()
            if status.get("lastError"):
                raise CaseError(status["lastError"])

            context = _Context(
                rows=_all_rows(manager),
                documents=manager.documents(),
                totals=status.get("totals") or {},
            )
            reports = []
            for index, step in enumerate(steps):
                report = _run_step(index, step, context)
                reports.append(report)
                if stop_on_failure and not report["ok"]:
                    break
        finally:
            manager.shutdown()

    passed = sum(1 for item in reports if item["ok"])
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    report = {
        "name": name,
        "ok": passed == len(reports),
        "durationMs": duration_ms,
        "pcap": str(source),
        "source": {
            "transfers": len(context.rows),
            "devices": len(context.documents),
        },
        "steps": reports,
        "summary": {
            "steps": len(reports),
            "passed": passed,
            "failed": len(reports) - passed,
        },
    }
    return report


# ── 执行 ──────────────────────────────────────────────────

class _Context:
    def __init__(self, *, rows: list[dict], documents: list[dict], totals: dict) -> None:
        self.rows = rows
        self.documents = documents
        self.totals = totals


def _wait_loaded(manager: UsbCaptureManager, timeout: float) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if not manager.status().get("loading"):
            return
        time.sleep(0.02)
    raise CaseError(f"离线解析超时（{timeout} 秒）")


def _all_rows(manager: UsbCaptureManager) -> list[dict]:
    rows: list[dict] = []
    after = 0
    while True:
        page = manager.query(limit=PAGE_SIZE, after_id=after)
        items = page.get("items") or []
        if not items:
            break
        rows.extend(items)
        after = int(items[-1]["id"])
        if len(items) < PAGE_SIZE:
            break
    return rows


def _run_step(index: int, step: Any, context: _Context) -> dict[str, Any]:
    started = time.perf_counter()
    if not isinstance(step, dict):
        return _step_report(index, "", False, "步骤必须是对象", started)
    title = str(step.get("name") or "")
    if "expect" not in step:
        return _step_report(
            index,
            title,
            False,
            "M4a 只支持 expect 步骤；control/write/read/echo 需要 usb_host（M4b）",
            started,
        )
    expect = step.get("expect")
    if not isinstance(expect, dict) or not expect:
        return _step_report(index, title, False, "expect 必须是非空对象", started)

    unknown = sorted(set(expect) - SUPPORTED_EXPECT)
    if unknown:
        return _step_report(
            index,
            title,
            False,
            f"未知断言：{', '.join(unknown)}；可用：{', '.join(sorted(SUPPORTED_EXPECT))}",
            started,
        )

    ok = True
    details: list[str] = []
    for key, spec in expect.items():
        passed, detail = _CHECKS[key](spec, context)
        ok = ok and passed
        details.append(f"{key}: {detail}")
    return _step_report(index, title or "expect", ok, "；".join(details), started)


def _step_report(index: int, title: str, ok: bool, detail: str, started: float) -> dict:
    return {
        "index": index,
        "name": title,
        "action": "expect",
        "ok": ok,
        "durationMs": round((time.perf_counter() - started) * 1000, 2),
        "detail": detail,
    }


# ── 断言 ──────────────────────────────────────────────────

def _as_int(value: Any) -> int:
    return int(str(value), 0)


def _match_transfer(row: dict, spec: dict) -> bool:
    if "type" in spec and row.get("typeLabel") != spec["type"]:
        return False
    if "direction" in spec and row.get("direction") != spec["direction"]:
        return False
    if "endpoint" in spec and int(row.get("endpoint", -1)) != _as_int(spec["endpoint"]):
        return False
    if "minLength" in spec and int(row.get("length", 0)) < _as_int(spec["minLength"]):
        return False
    if "status" in spec:
        if spec["status"] == "ok" and not row.get("ok"):
            return False
        if spec["status"] == "error" and row.get("ok"):
            return False
    if "summaryContains" in spec and str(spec["summaryContains"]) not in (
        row.get("summary") or ""
    ):
        return False
    return True


def _check_transfers(spec: Any, ctx: _Context) -> tuple[bool, str]:
    if not isinstance(spec, dict):
        return False, "transfers 必须是对象，如 {\"atLeast\": 1}"
    count = len(ctx.rows)
    if "exactly" in spec and count != _as_int(spec["exactly"]):
        return False, f"期望恰好 {spec['exactly']} 条，实际 {count} 条"
    if "atLeast" in spec and count < _as_int(spec["atLeast"]):
        return False, f"期望至少 {spec['atLeast']} 条，实际 {count} 条"
    if "atMost" in spec and count > _as_int(spec["atMost"]):
        return False, f"期望最多 {spec['atMost']} 条，实际 {count} 条"
    return True, f"{count} 条传输"


def _check_transfer(spec: Any, ctx: _Context) -> tuple[bool, str]:
    if not isinstance(spec, dict) or not spec:
        return False, "transfer 必须是非空对象"
    matched = [row for row in ctx.rows if _match_transfer(row, spec)]
    if not matched:
        sample = "；".join(
            f"#{row.get('id')} {row.get('typeLabel')} {row.get('summary')}"
            for row in ctx.rows[:5]
        )
        return False, f"没有匹配的传输（现有：{sample or '无'}）"
    first = matched[0]
    return True, f"匹配 {len(matched)} 条，例如 #{first.get('id')} {first.get('summary')}"


def _document_device(document: dict) -> dict:
    return document.get("device") or {}


def _check_device(spec: Any, ctx: _Context) -> tuple[bool, str]:
    if not isinstance(spec, dict) or not spec:
        return False, "device 必须是非空对象"
    for item in ctx.documents:
        device = _document_device(item["document"])
        if "vid" in spec and str(device.get("vid", "")).lower() != str(spec["vid"]).lower():
            continue
        if "pid" in spec and str(device.get("pid", "")).lower() != str(spec["pid"]).lower():
            continue
        if "serial" in spec:
            strings = item["document"].get("strings") or {}
            serial_index = (device.get("strings") or {}).get("serial")
            found = None
            for table in strings.values():
                if serial_index is not None:
                    found = table.get(str(serial_index))
                    if found:
                        break
            if found != spec["serial"]:
                continue
        return True, f"bus {item['bus']} device {item['device']} 匹配"
    return False, f"没有匹配的设备（抓到 {len(ctx.documents)} 个）"


def _check_errors(spec: Any, ctx: _Context) -> tuple[bool, str]:
    if not isinstance(spec, dict):
        return False, "errors 必须是对象，如 {\"atMost\": 0}"
    errors = int(ctx.totals.get("errors", 0))
    unmatched = int(ctx.totals.get("unmatched", 0))
    limit = _as_int(spec.get("atMost", 0))
    if errors > limit or unmatched > limit:
        return False, f"错误 {errors} 条、未匹配 {unmatched} 条，超过上限 {limit}"
    return True, f"错误 {errors} 条、未匹配 {unmatched} 条"


def _check_descriptor(spec: Any, ctx: _Context) -> tuple[bool, str]:
    if not isinstance(spec, dict):
        return False, "descriptor 必须是对象，如 {\"ok\": true}"
    if not ctx.documents:
        return False, "抓包里没有可用于校验的描述符"
    from core import usb_desc

    failures: list[str] = []
    checked = 0
    for item in ctx.documents:
        document = item["document"]
        device = _document_device(document)
        if "vid" in spec and str(device.get("vid", "")).lower() != str(spec["vid"]).lower():
            continue
        if "pid" in spec and str(device.get("pid", "")).lower() != str(spec["pid"]).lower():
            continue
        checked += 1
        report = usb_desc.validate_document(document)
        if not report["ok"]:
            codes = ", ".join(item["code"] for item in report["errors"][:5])
            failures.append(f"bus {item['bus']} device {item['device']}: {codes}")
    if not checked:
        return False, "没有匹配设备可校验"
    if failures:
        return False, "；".join(failures)
    return True, f"{checked} 个设备的描述符合法"


_CHECKS: dict[str, Callable[[Any, _Context], tuple[bool, str]]] = {
    "transfers": _check_transfers,
    "transfer": _check_transfer,
    "device": _check_device,
    "errors": _check_errors,
    "descriptor": _check_descriptor,
}


# ── 报告 ──────────────────────────────────────────────────

def to_junit(report: dict[str, Any]) -> str:
    """把报告转成 JUnit XML（CI 直接消费）。"""
    name = sax.quoteattr(str(report.get("name") or "usb-case"))
    steps = report.get("steps") or []
    failures = sum(1 for item in steps if not item.get("ok"))
    seconds = round(float(report.get("durationMs") or 0) / 1000.0, 3)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={name} tests="{len(steps)}" failures="{failures}" '
        f'errors="0" time="{seconds}">',
    ]
    for step in steps:
        title = sax.quoteattr(f"{step.get('index')}-{step.get('name') or step.get('action')}")
        step_seconds = round(float(step.get("durationMs") or 0) / 1000.0, 3)
        lines.append(f'  <testcase classname="usb" name={title} time="{step_seconds}">')
        if not step.get("ok"):
            detail = sax.quoteattr(str(step.get("detail") or "断言失败"))
            lines.append(f"    <failure message={detail}/>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines)


def case_schema() -> dict[str, Any]:
    """返回一份可复制的最小用例（供 `omni usb test list` 展示）。"""
    return {
        "name": "enumeration-smoke",
        "pcap": "C:/captures/enum.pcap",
        "steps": [
            {"name": "有传输", "expect": {"transfers": {"atLeast": 1}}},
            {
                "name": "看到设备描述符",
                "expect": {
                    "transfer": {
                        "type": "control",
                        "summaryContains": "GET_DESCRIPTOR(DEVICE)",
                    }
                },
            },
            {"name": "设备身份", "expect": {"device": {"vid": "0x1234", "pid": "0x5678"}}},
            {"name": "无错误", "expect": {"errors": {"atMost": 0}}},
            {"name": "描述符合法", "expect": {"descriptor": {"ok": True}}},
        ],
    }
