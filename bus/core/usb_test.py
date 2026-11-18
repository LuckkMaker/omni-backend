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
    """用例执行器不需要 WebSocket 推送；串口后端还会调用 log。"""

    def emit(self, *_args, **_kwargs) -> None:
        return None

    def log(self, *_args, **_kwargs) -> None:
        return None


#: 动作步骤 → 通道
ACTION_KEYS = ("open", "close", "control", "write", "read", "echo")

#: 动作结果断言（与 pcap 的 expect 校验分开）
RESULT_EXPECT = {"ok", "length", "hex", "hexStartsWith", "mismatches", "bytesWritten", "minLength"}


def run_case(
    case: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    captures_dir: Path | None = None,
    stop_on_failure: bool = False,
    winusb_manager: Any = None,
    hid_manager: Any = None,
    serial_manager: Any = None,
) -> dict[str, Any]:
    """执行一条用例，返回报告（不写文件，JUnit 由 :func:`to_junit` 生成）。

    用例可以只有 ``pcap``（离线抓包断言）、只有 ``device``（真机动作），或两者都有
    （先录后跑：动作步骤执行完，再用 pcap 断言总线上的结果）。
    """
    if not isinstance(case, dict):
        raise CaseError("用例必须是 JSON 对象")
    name = str(case.get("name") or "usb-case")
    pcap = case.get("pcap") or (case.get("source") or {}).get("pcap")
    device = case.get("device")
    if not pcap and not device:
        raise CaseError("需要 pcap 或 device（device.channel = winusb/hid/cdc）")

    source = None
    if pcap:
        source = Path(str(pcap)).expanduser()
        if not source.is_file():
            raise CaseError(f"pcap 不存在：{source}")

    steps = case.get("steps")
    if not isinstance(steps, list) or not steps:
        raise CaseError("steps 必须是非空数组")
    if len(steps) > MAX_STEPS:
        raise CaseError(f"steps 超过上限（{MAX_STEPS}）")

    started = time.perf_counter()
    context = _Context()
    capture_manager = None
    live = None
    reports: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="omni-usb-case-") as tmp:
        try:
            if source is not None:
                capture_manager = UsbCaptureManager(
                    _NullEvents(), captures_dir=Path(captures_dir or tmp)
                )
                opened = capture_manager.open_file(str(source))
                if not opened.get("success"):
                    raise CaseError(opened.get("error") or "无法打开 pcap")
                _wait_loaded(capture_manager, timeout)
                status = capture_manager.status()
                if status.get("lastError"):
                    raise CaseError(status["lastError"])
                context.rows = _all_rows(capture_manager)
                context.documents = capture_manager.documents()
                context.totals = status.get("totals") or {}

            if device:
                live = _LiveContext(
                    device, winusb_manager, hid_manager, serial_manager
                )
                context.live = live

            for index, step in enumerate(steps):
                report = _run_step(index, step, context)
                reports.append(report)
                if stop_on_failure and not report["ok"]:
                    break
        finally:
            if live is not None:
                live.close()
            if capture_manager is not None:
                capture_manager.shutdown()

    passed = sum(1 for item in reports if item["ok"])
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "name": name,
        "ok": passed == len(reports),
        "durationMs": duration_ms,
        "pcap": str(source) if source is not None else None,
        "device": live.describe() if live is not None else None,
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


# ── 执行 ──────────────────────────────────────────────────

class _Context:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.documents: list[dict] = []
        self.totals: dict = {}
        self.live: "_LiveContext | None" = None


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

    actions = [key for key in ACTION_KEYS if key in step]
    if len(actions) > 1:
        return _step_report(
            index, title, False, f"一个步骤只能有一个动作：{', '.join(actions)}", started
        )
    if actions:
        action = actions[0]
        if context.live is None:
            return _step_report(
                index, title or action, False, "动作步骤需要 device（channel=winusb/hid/cdc）", started
            )
        try:
            result = getattr(context.live, action)(step.get(action) or {})
        except CaseError as exc:
            return _step_report(index, title or action, False, str(exc), started, action)
        if not isinstance(result, dict):
            result = {"success": False, "error": "动作返回了非法结果"}
        ok = bool(result.get("success", True))
        detail = str(result.get("error") or _result_summary(action, result))
        if "expect" in step:
            passed, why = _assert_result(step["expect"], result)
            ok = ok and passed
            detail = f"{detail}；{why}"
        return _step_report(index, title or action, ok, detail, started, action)

    if "expect" not in step:
        return _step_report(
            index, title, False,
            "步骤需要动作（open/control/write/read/echo/close）或 expect", started,
        )
    if not context.rows and not context.documents:
        return _step_report(
            index, title, False,
            "expect 需要 pcap 数据；真机动作请给步骤加 expect（针对返回值）", started,
        )

    expect = step.get("expect")
    if not isinstance(expect, dict) or not expect:
        return _step_report(index, title, False, "expect 必须是非空对象", started)
    unknown = sorted(set(expect) - SUPPORTED_EXPECT)
    if unknown:
        return _step_report(
            index, title, False,
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


def _result_summary(action: str, result: dict) -> str:
    if action in ("control", "read", "feature"):
        return f"{int(result.get('length') or 0)} 字节"
    if action == "write":
        return f"写入 {int(result.get('bytesWritten') or 0)} 字节"
    if action == "echo":
        return (
            f"{result.get('iterations')} 次，吞吐 {result.get('throughputKiBs')} KiB/s，"
            f"不一致 {result.get('mismatches')} 次"
        )
    if action == "open":
        device = result.get("device") or {}
        if device.get("vid"):
            return f"{device.get('vid')}:{device.get('pid')}"
        return str(device.get("port") or "已打开")
    return "完成"


def _normalize_hex(text: str) -> str:
    return bytes.fromhex(str(text).replace("0x", " ").replace(",", " ").replace(" ", "")).hex().upper()


def _assert_result(expect: Any, result: dict) -> tuple[bool, str]:
    """校验动作返回值；返回 ``(ok, 说明)``。"""
    if not isinstance(expect, dict) or not expect:
        return False, "expect 必须是非空对象"
    unknown = sorted(set(expect) - RESULT_EXPECT)
    if unknown:
        return False, f"未知断言：{', '.join(unknown)}；可用：{', '.join(sorted(RESULT_EXPECT))}"

    details: list[str] = []
    ok = True
    success = bool(result.get("success", True))
    if "ok" in expect:
        passed = success == bool(expect["ok"])
        ok = ok and passed
        details.append(f"ok={success}")
    data_hex = (result.get("dataHex") or "").replace(" ", "").upper()
    if "length" in expect:
        passed = int(result.get("length") or 0) == int(expect["length"])
        ok = ok and passed
        details.append(f"length={int(result.get('length') or 0)}")
    if "minLength" in expect:
        passed = int(result.get("length") or 0) >= int(expect["minLength"])
        ok = ok and passed
        details.append(f"length={int(result.get('length') or 0)}")
    if "hex" in expect:
        passed = data_hex == _normalize_hex(expect["hex"])
        ok = ok and passed
        details.append(f"hex={'匹配' if passed else data_hex[:40]}")
    if "hexStartsWith" in expect:
        prefix = _normalize_hex(expect["hexStartsWith"])
        passed = data_hex.startswith(prefix)
        ok = ok and passed
        details.append(f"hexStartsWith={'匹配' if passed else data_hex[:40]}")
    if "mismatches" in expect:
        passed = int(result.get("mismatches") or 0) == int(expect["mismatches"])
        ok = ok and passed
        details.append(f"mismatches={result.get('mismatches')}")
    if "bytesWritten" in expect:
        passed = int(result.get("bytesWritten") or 0) == int(expect["bytesWritten"])
        ok = ok and passed
        details.append(f"bytesWritten={result.get('bytesWritten')}")
    if not ok:
        return False, "；".join(details) or "断言失败"
    return True, "；".join(details) or "通过"


class _LiveContext:
    """真机动作会话（winusb / hid / cdc 三种通道）。"""

    def __init__(
        self,
        device: dict,
        winusb_manager: Any = None,
        hid_manager: Any = None,
        serial_manager: Any = None,
    ) -> None:
        self.device = device
        self.channel = str(device.get("channel") or "winusb").lower()
        if self.channel not in ("winusb", "hid", "cdc"):
            raise CaseError(f"未知通道 {self.channel}；可用：winusb / hid / cdc")
        self.session_id = str(device.get("id") or "case")
        self.timeout_ms = int(device.get("timeoutMs") or 1000)
        self._winusb = winusb_manager
        self._hid = hid_manager
        self._serial = serial_manager
        self._opened = False
        self._cursor = 0

    def describe(self) -> dict:
        return {"channel": self.channel, "id": self.session_id}

    # ── 会话 ──────────────────────────────────────────────
    def _winusb_manager(self) -> Any:
        if self._winusb is None:
            from core.usb_winusb import WinUsbManager

            self._winusb = WinUsbManager()
        return self._winusb

    def _hid_manager(self) -> Any:
        if self._hid is None:
            from core.usb_hid import HidManager

            self._hid = HidManager()
        return self._hid

    def _serial_manager(self) -> Any:
        if self._serial is None:
            from core.serial_backend import SerialManager

            self._serial = SerialManager(_NullEvents())
        return self._serial

    def open(self, spec: dict) -> dict:
        if self._opened:
            return {"success": False, "error": "会话已打开"}
        device = self.device
        if self.channel == "winusb":
            result = self._winusb_manager().open(
                self.session_id,
                path=spec.get("path") or device.get("path"),
                vid=_optional_int(spec.get("vid", device.get("vid"))),
                pid=_optional_int(spec.get("pid", device.get("pid"))),
                interface=_num(spec.get("interface", device.get("interface", 0))),
                timeout_ms=self.timeout_ms,
                guids=spec.get("guids"),
            )
        elif self.channel == "hid":
            result = self._hid_manager().open(
                self.session_id,
                vid=_optional_int(spec.get("vid", device.get("vid"))),
                pid=_optional_int(spec.get("pid", device.get("pid"))),
                serial=spec.get("serial", device.get("serial")),
                path=spec.get("path", device.get("path")),
                timeout_ms=self.timeout_ms,
            )
        else:
            manager = self._serial_manager()
            config = {
                "port": spec.get("port") or device.get("port"),
                "baudrate": _num(spec.get("baud", device.get("baud", 115200))),
                "bytesize": _num(spec.get("bytesize", 8)),
                "parity": spec.get("parity", "N"),
                "stopbits": float(spec.get("stopbits", 1)),
                "flow_control": spec.get("flow", "none"),
                "rts": bool(spec.get("rts", False)),
                "dtr": bool(spec.get("dtr", False)),
            }
            result = manager.open(self.session_id, config)
            if result.get("success", True):
                page = manager.read_capture(self.session_id, 0, 1)
                self._cursor = int(page.get("next_seq") or 0)
                result = dict(result)
                result["device"] = {
                    "port": config["port"],
                    "baud": config["baudrate"],
                }
        self._opened = bool(result.get("success", True))
        return result

    def close(self, _spec: dict | None = None) -> dict:
        if not self._opened:
            return {"success": True}
        try:
            if self.channel == "winusb":
                return self._winusb_manager().close(self.session_id)
            if self.channel == "hid":
                return self._hid_manager().close(self.session_id)
            return self._serial_manager().close(self.session_id)
        finally:
            self._opened = False

    # ── 传输 ──────────────────────────────────────────────
    def control(self, spec: dict) -> dict:
        if not self._opened or self.channel != "winusb":
            return {"success": False, "error": "control 需要已打开的 winusb 会话"}
        request_type = spec.get("requestType")
        if request_type is None:
            direction = 0x80 if spec.get("direction", "in") == "in" else 0
            kind = {"standard": 0x00, "class": 0x20, "vendor": 0x40}[spec.get("type", "standard")]
            recipient = {"device": 0, "interface": 1, "endpoint": 2, "other": 3}[
                spec.get("recipient", "device")
            ]
            request_type = direction | kind | recipient
        data = _hex_bytes(spec.get("data") or spec.get("hex"))
        length = int(spec.get("length") or len(data))
        return self._winusb_manager().control(
            self.session_id,
            request_type=_num(request_type),
            request=_num(spec.get("request", 0)),
            value=_num(spec.get("value", 0)),
            index=_num(spec.get("index", 0)),
            length=_num(length),
            data=data,
        )

    def write(self, spec: dict) -> dict:
        if not self._opened:
            return {"success": False, "error": "会话未打开"}
        data = _hex_bytes(spec.get("data") or spec.get("hex") or spec.get("text"))
        if not data:
            return {"success": False, "error": "write 需要 data（十六进制）"}
        if self.channel == "winusb":
            return self._winusb_manager().write(
                self.session_id, _num(spec.get("pipe", 0)), data
            )
        if self.channel == "hid":
            return self._hid_manager().write(
                self.session_id, data, report_id=_num(spec.get("reportId", 0))
            )
        result = self._serial_manager().send(self.session_id, data)
        if result.get("success", True):
            result = dict(result)
            result["bytesWritten"] = int(result.get("bytes_written") or len(data))
        return result

    def read(self, spec: dict) -> dict:
        if not self._opened:
            return {"success": False, "error": "会话未打开"}
        timeout_ms = _num(spec.get("timeoutMs") or self.timeout_ms)
        if self.channel == "winusb":
            return self._winusb_manager().read(
                self.session_id, _num(spec.get("pipe", 0)), _num(spec.get("length", 64))
            )
        if self.channel == "hid":
            return self._hid_manager().read(
                self.session_id,
                length=(_num(spec["length"]) if spec.get("length") else None),
                timeout_ms=timeout_ms,
            )
        return self._read_serial(timeout_ms, _num(spec.get("maxBytes", 4096)))

    def echo(self, spec: dict) -> dict:
        if not self._opened or self.channel != "winusb":
            return {"success": False, "error": "echo 需要已打开的 winusb 会话"}
        return self._winusb_manager().echo(
            self.session_id,
            out_pipe=_num(spec.get("outPipe", 0)),
            in_pipe=_num(spec.get("inPipe", 0)),
            size=_num(spec.get("size", 64)),
            count=_num(spec.get("count", 100)),
            pipeline=_num(spec.get("pipeline", 1)),
            pattern=str(spec.get("pattern", "increment")),
            seed=_num(spec.get("seed", 0)),
        )

    def _read_serial(self, timeout_ms: int, max_bytes: int) -> dict:
        import base64

        manager = self._serial_manager()
        deadline = time.monotonic() + max(1, timeout_ms) / 1000.0
        data = bytearray()
        while True:
            page = manager.read_capture(self.session_id, self._cursor, max_bytes)
            if not page.get("success", True):
                return page
            for chunk in page.get("chunks") or []:
                data.extend(base64.b64decode(chunk.get("data") or ""))
            self._cursor = int(page.get("cursor") or self._cursor)
            if data or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        return {
            "success": True,
            "dataHex": bytes(data).hex(" ").upper(),
            "length": len(data),
        }


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(str(value), 0)


def _num(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(str(value), 0)


def _hex_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = str(value)
    from core.usb_desc import parse_hex

    try:
        return parse_hex(text)
    except ValueError as exc:
        raise CaseError(f"无效的十六进制数据：{exc}") from None


def _step_report(
    index: int,
    title: str,
    ok: bool,
    detail: str,
    started: float,
    action: str = "expect",
) -> dict:
    return {
        "index": index,
        "name": title,
        "action": action,
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
    """返回可复制的用例样例（离线抓包 / 真机动作），供 `omni usb test list` 展示。"""
    return {
        "offline": {
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
        },
        "live": {
            "name": "winusb-echo-smoke",
            "device": {
                "channel": "winusb",
                "vid": "0xC251",
                "pid": "0x0002",
                "interface": 0,
                "timeoutMs": 1000,
            },
            "steps": [
                {"open": {}},
                {
                    "name": "读设备描述符",
                    "control": {
                        "requestType": "0x80",
                        "request": "0x06",
                        "value": "0x0100",
                        "length": 18,
                    },
                    "expect": {"ok": True, "hexStartsWith": "12 01"},
                },
                {"name": "写一笔", "write": {"pipe": "0x01", "data": "01 02 03 04"}},
                {
                    "name": "读回",
                    "read": {"pipe": "0x81", "length": 4},
                    "expect": {"hex": "01 02 03 04"},
                },
                {
                    "name": "回环吞吐",
                    "echo": {"outPipe": "0x01", "inPipe": "0x81", "size": 64, "count": 20},
                    "expect": {"mismatches": 0},
                },
                {"close": {}},
            ],
        },
    }
