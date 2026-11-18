"""USB 用例执行器测试（离线 pcap，不需要 USBPcap）"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.usb_test import CaseError, run_case, to_junit  # noqa: E402
from usb_fixtures import (  # noqa: E402
    config_descriptor,
    control_transfer_records,
    device_descriptor,
    make_pcap,
    record,
)


def setup(bm: int, request: int, value: int, index: int, length: int) -> bytes:
    return struct.pack("<BBHHH", bm, request, value, index, length)


def build_pcap() -> bytes:
    records = []
    records += control_transfer_records(
        irp_id=1, setup=setup(0x80, 6, 0x0100, 0, 18), data=device_descriptor(), ts=1.0
    )
    records += control_transfer_records(
        irp_id=2, setup=setup(0x80, 6, 0x0200, 0, 25), data=config_descriptor(), ts=1.1
    )
    records += [
        record(
            ts=1.2,
            irp_id=3,
            function=0x0009,
            transfer=3,
            endpoint=0x81,
            info=0,
            data=b"",
            stage=None,
        ),
        record(
            ts=1.21,
            irp_id=3,
            function=0x0009,
            transfer=3,
            endpoint=0x81,
            info=1,
            data=b"hello",
            stage=None,
        ),
    ]
    return make_pcap(records)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pcap = Path(self.tmp.name) / "case.pcap"
        self.pcap.write_bytes(build_pcap())

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, steps):
        return {"name": "demo", "pcap": str(self.pcap), "steps": steps}

    def test_passing_case(self):
        report = run_case(
            self._case(
                [
                    {"name": "有传输", "expect": {"transfers": {"atLeast": 3}}},
                    {
                        "name": "设备描述符",
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
                ]
            )
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["summary"]["passed"], 5)
        self.assertEqual(report["source"]["devices"], 1)

    def test_failing_case(self):
        report = run_case(self._case([{"expect": {"transfers": {"exactly": 99}}}]))
        self.assertFalse(report["ok"])
        step = report["steps"][0]
        self.assertFalse(step["ok"])
        self.assertIn("99", step["detail"])

    def test_stop_on_failure(self):
        report = run_case(
            self._case(
                [
                    {"name": "先失败", "expect": {"transfers": {"exactly": 99}}},
                    {"name": "不该执行", "expect": {"transfers": {"atLeast": 1}}},
                ]
            ),
            stop_on_failure=True,
        )
        self.assertEqual(len(report["steps"]), 1)

    def test_unknown_assertion(self):
        report = run_case(self._case([{"expect": {"nope": {}}}]))
        self.assertFalse(report["ok"])
        self.assertIn("未知断言", report["steps"][0]["detail"])

    def test_action_step_rejected(self):
        report = run_case(self._case([{"control": {"request": "0x06"}}]))
        self.assertFalse(report["ok"])
        self.assertIn("需要 device", report["steps"][0]["detail"])

    def test_invalid_case(self):
        with self.assertRaises(CaseError):
            run_case({"name": "x", "pcap": str(self.pcap), "steps": []})
        with self.assertRaises(CaseError):
            run_case({"name": "x", "steps": [{"expect": {"transfers": {"atLeast": 1}}}]})
        with self.assertRaises(CaseError):
            run_case(
                {
                    "name": "x",
                    "pcap": str(Path(self.tmp.name) / "missing.pcap"),
                    "steps": [{"expect": {"transfers": {"atLeast": 1}}}],
                }
            )

    def test_junit(self):
        report = run_case(
            self._case([{"name": "少", "expect": {"transfers": {"atLeast": 1}}},
                        {"name": "多", "expect": {"transfers": {"exactly": 99}}}])
        )
        xml = to_junit(report)
        self.assertIn("<testsuite", xml)
        self.assertIn('failures="1"', xml)
        self.assertIn("<failure", xml)
        self.assertIn("多", xml)


class FakeWinUsb:
    """记录调用并返回预置结果，用来验证动作步骤编排。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.open_ok = True
        self.echo_mismatches = 0

    def open(self, session_id, **kwargs):
        self.calls.append(("open", session_id, kwargs))
        if not self.open_ok:
            return {"success": False, "code": "winusb_not_found", "error": "没有设备"}
        return {"success": True, "device": {"vid": "0xC251", "pid": "0x0002"}}

    def control(self, session_id, **kwargs):
        self.calls.append(("control", session_id, kwargs))
        return {"success": True, "dataHex": "12 01 02", "length": 3}

    def write(self, session_id, pipe, data):
        self.calls.append(("write", session_id, pipe, data))
        return {"success": True, "bytesWritten": len(data)}

    def read(self, session_id, pipe, length):
        self.calls.append(("read", session_id, pipe, length))
        return {"success": True, "dataHex": "01 02 03 04", "length": 4}

    def echo(self, session_id, **kwargs):
        self.calls.append(("echo", session_id, kwargs))
        return {
            "success": True,
            "iterations": kwargs.get("count", 0),
            "mismatches": self.echo_mismatches,
            "throughputKiBs": 10.0,
        }

    def close(self, session_id):
        self.calls.append(("close", session_id))
        return {"success": True}


def live_case(steps=None) -> dict:
    return {
        "name": "winusb-live",
        "device": {"channel": "winusb", "vid": "0xC251", "pid": "0x0002"},
        "steps": steps
        or [
            {"open": {}},
            {
                "control": {"requestType": "0x80", "request": "0x06", "value": "0x0100", "length": 18},
                "expect": {"ok": True, "hexStartsWith": "12 01"},
            },
            {"write": {"pipe": "0x01", "data": "01 02 03 04"}},
            {"read": {"pipe": "0x81", "length": 4}, "expect": {"hex": "01 02 03 04"}},
            {"echo": {"outPipe": "0x01", "inPipe": "0x81", "size": 64, "count": 5},
             "expect": {"mismatches": 0}},
            {"close": {}},
        ],
    }


class LiveActionTests(unittest.TestCase):
    def test_passing_case(self):
        fake = FakeWinUsb()
        report = run_case(live_case(), winusb_manager=fake)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(
            [call[0] for call in fake.calls],
            ["open", "control", "write", "read", "echo", "close"],
        )
        self.assertEqual(report["device"]["channel"], "winusb")

    def test_failed_assertion_still_closes(self):
        fake = FakeWinUsb()
        steps = [
            {"open": {}},
            {"read": {"pipe": "0x81", "length": 4}, "expect": {"hex": "FF FF"}},
            {"close": {}},
        ]
        report = run_case(live_case(steps), winusb_manager=fake)
        self.assertFalse(report["ok"])
        self.assertIn("hex=", report["steps"][1]["detail"])
        self.assertEqual(fake.calls[-1][0], "close")

    def test_echo_mismatch_fails(self):
        fake = FakeWinUsb()
        fake.echo_mismatches = 1
        report = run_case(live_case(), winusb_manager=fake)
        self.assertFalse(report["ok"])
        echo_step = next(item for item in report["steps"] if item["action"] == "echo")
        self.assertFalse(echo_step["ok"])

    def test_open_failure(self):
        fake = FakeWinUsb()
        fake.open_ok = False
        steps = [{"open": {}}, {"read": {"pipe": "0x81", "length": 4}}, {"close": {}}]
        report = run_case(live_case(steps), winusb_manager=fake)
        self.assertFalse(report["steps"][0]["ok"])
        self.assertIn("没有设备", report["steps"][0]["detail"])

    def test_expect_without_pcap(self):
        report = run_case(
            live_case([{"expect": {"transfers": {"atLeast": 1}}}]),
            winusb_manager=FakeWinUsb(),
        )
        self.assertFalse(report["ok"])
        self.assertIn("需要 pcap", report["steps"][0]["detail"])

    def test_unknown_channel(self):
        with self.assertRaises(CaseError):
            run_case(
                {"name": "x", "device": {"channel": "nope"}, "steps": [{"open": {}}]},
                winusb_manager=FakeWinUsb(),
            )

    def test_missing_source(self):
        with self.assertRaises(CaseError):
            run_case({"name": "x", "steps": [{"open": {}}]}, winusb_manager=FakeWinUsb())


class FakeSerial:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._data = b""

    def open(self, session_id, config):
        self.calls.append(("open", session_id, config))
        return {"success": True}

    def read_capture(self, session_id, since, max_bytes):
        self.calls.append(("read", session_id, since))
        return {"success": True, "chunks": [], "next_seq": 0, "cursor": 0}

    def send(self, session_id, data):
        self.calls.append(("send", session_id, data))
        return {"success": True, "bytes_written": len(data)}

    def close(self, session_id):
        self.calls.append(("close", session_id))
        return {"success": True}


class CdcActionTests(unittest.TestCase):
    def test_write_normalizes_bytes_written(self):
        fake = FakeSerial()
        case = {
            "name": "cdc-live",
            "device": {"channel": "cdc", "port": "COM73"},
            "steps": [
                {"open": {}, "expect": {"ok": True}},
                {"write": {"data": "68 69"}, "expect": {"bytesWritten": 2}},
                {"close": {}},
            ],
        }
        report = run_case(case, serial_manager=fake)
        self.assertTrue(report["ok"], report)
        self.assertEqual(
            [call[0] for call in fake.calls],
            ["open", "read", "send", "close"],
        )
        self.assertEqual(fake.calls[2][2], b"hi")


if __name__ == "__main__":
    unittest.main()
