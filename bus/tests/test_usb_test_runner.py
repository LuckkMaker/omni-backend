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
        self.assertIn("M4b", report["steps"][0]["detail"])

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


if __name__ == "__main__":
    unittest.main()
