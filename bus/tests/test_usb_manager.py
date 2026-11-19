"""USB 捕获管理器测试（离线 pcap 路径，不依赖 USBPcap 驱动）"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.usb_backend import (  # noqa: E402
    UsbCaptureManager,
    find_usbpcap,
    is_elevated,
    is_windows,
)
from usb_fixtures import (  # noqa: E402
    config_descriptor,
    control_transfer_records,
    device_descriptor,
    make_pcap,
    record,
)


class _FakeEventManager:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, data):
        self.events.append((event, data))


def _setup(bm: int, request: int, value: int, index: int, length: int) -> bytes:
    return struct.pack("<BBHHH", bm, request, value, index, length)


def build_sample_pcap() -> bytes:
    records = []
    records += control_transfer_records(
        irp_id=1,
        setup=_setup(0x80, 6, 0x0100, 0, 18),
        data=device_descriptor(),
        ts=1.0,
    )
    records += control_transfer_records(
        irp_id=2,
        setup=_setup(0x80, 6, 0x0200, 0, 9),
        data=config_descriptor(),
        ts=1.1,
    )
    records += [
        record(ts=1.2, irp_id=3, function=0x0009, transfer=3, endpoint=0x81, info=0, data=b"", stage=None),
        record(ts=1.21, irp_id=3, function=0x0009, transfer=3, endpoint=0x81, info=1, data=b"hello", stage=None),
    ]
    records += control_transfer_records(
        irp_id=4,
        setup=_setup(0x80, 6, 0x0301, 0x0409, 255),
        data=bytes([4, 3, 0x41, 0x00]),
        status=0xC0000004,
        ts=1.3,
    )
    return make_pcap(records)


def wait_loaded(manager: UsbCaptureManager, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not manager.status()["loading"]:
            return
        time.sleep(0.01)
    raise AssertionError("离线解析超时")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sample.pcap"
        self.path.write_bytes(build_sample_pcap())
        self.em = _FakeEventManager()
        self.manager = UsbCaptureManager(self.em, captures_dir=Path(self.tmp.name))

    def tearDown(self):
        self.manager.shutdown()
        self.tmp.cleanup()

    def test_offline_pipeline(self):
        result = self.manager.open_file(str(self.path))
        self.assertTrue(result["success"])
        wait_loaded(self.manager)

        status = self.manager.status()
        self.assertFalse(status["loading"])
        self.assertEqual(status["totals"]["transfers"], 4)
        self.assertEqual(status["totals"]["errors"], 1)

        page = self.manager.query(limit=10)
        self.assertEqual(page["total"], 4)
        rows = page["items"]
        self.assertEqual([row["id"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(page["oldestId"], 1)
        self.assertEqual(page["nextId"], 5)
        self.assertEqual(page["buffered"], 4)
        self.assertEqual(rows[2]["typeLabel"], "bulk")
        self.assertEqual(rows[2]["summary"], "BULK IN 5B")
        self.assertEqual(rows[3]["statusName"], "STALL_PID")
        self.assertFalse(rows[3]["ok"])

        detail = self.manager.detail(3)
        self.assertTrue(detail["success"])
        self.assertEqual(detail["dataHex"], "68 65 6C 6C 6F")
        self.assertTrue(detail["decodeTree"])

        devices = self.manager.devices()["tree"]
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["kind"], "device")

        emitted = {name for name, _ in self.em.events}
        self.assertIn("usb.transfers", emitted)
        self.assertIn("usb.status", emitted)

    def test_filters_and_paging(self):
        self.manager.open_file(str(self.path))
        wait_loaded(self.manager)

        controls = self.manager.query(limit=10, filters={"type": ["control"]})
        self.assertEqual(controls["total"], 3)
        errors = self.manager.query(limit=10, filters={"status": "error"})
        self.assertEqual(errors["total"], 1)

        older = self.manager.query(limit=2, before_id=4)
        self.assertEqual([row["id"] for row in older["items"]], [2, 3])

        newer = self.manager.query(limit=2, after_id=1)
        self.assertEqual([row["id"] for row in newer["items"]], [2, 3])

        search = self.manager.query(limit=10, filters={"q": "BULK"})
        self.assertEqual(search["total"], 1)

        all_total = self.manager.query(limit=10)["total"]
        by_vid = self.manager.query(limit=10, filters={"vid": "0x1234"})
        self.assertEqual(by_vid["total"], all_total)
        by_pid = self.manager.query(limit=10, filters={"pid": "5678"})
        self.assertEqual(by_pid["total"], all_total)
        no_match = self.manager.query(limit=10, filters={"vid": "0x9999"})
        self.assertEqual(no_match["total"], 0)

    def test_export_and_clear(self):
        self.manager.open_file(str(self.path))
        wait_loaded(self.manager)

        target = Path(self.tmp.name) / "export.pcap"
        result = self.manager.save(str(target))
        self.assertTrue(result["success"])
        self.assertEqual(target.read_bytes(), self.path.read_bytes())

        cleared = self.manager.clear()
        self.assertTrue(cleared["success"])
        self.assertEqual(self.manager.query()["total"], 0)
        self.assertEqual(self.manager.devices()["tree"], [])

    def test_detail_unavailable_when_file_removed(self):
        self.manager.open_file(str(self.path))
        wait_loaded(self.manager)
        self.path.unlink()
        detail = self.manager.detail(1)
        self.assertFalse(detail["success"])
        self.assertEqual(detail["code"], "detail_unavailable")

    def test_pipe_control_urb_counted(self):
        path = Path(self.tmp.name) / "urb.pcap"
        path.write_bytes(
            make_pcap(
                [
                    record(
                        irp_id=77,
                        transfer=0xFE,
                        function=0x001E,
                        endpoint=0x81,
                        info=0,
                        data=b"",
                        stage=None,
                    ),
                    record(
                        irp_id=77,
                        transfer=0xFE,
                        function=0x001E,
                        endpoint=0x81,
                        info=1,
                        data=b"",
                        stage=None,
                    ),
                ]
            )
        )
        self.manager.open_file(str(path))
        wait_loaded(self.manager)
        status = self.manager.status()
        self.assertEqual(status["totals"]["other"], 1)
        row = self.manager.query()["items"][0]
        self.assertEqual(row["typeLabel"], "urb")
        self.assertEqual(row["summary"], "SYNC_RESET_PIPE_AND_CLEAR_STALL")

    def test_start_requires_elevation(self):
        if is_elevated():
            self.skipTest("当前进程已是管理员，跳过非提权校验")
        if not find_usbpcap():
            self.skipTest("本机未安装 USBPcap")
        result = self.manager.start(r"\\.\USBPcap1")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "not_elevated")

    def test_start_rejects_invalid_options(self):
        if not is_windows():
            self.skipTest("抓包参数校验仅 Windows 可用")
        result = self.manager.start(r"\\.\USBPcap1", {"bufferlen": 100})
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_bufferlen")

        result = self.manager.start(r"\\.\USBPcap1", {"bufferlen": "abc"})
        self.assertEqual(result["code"], "invalid_bufferlen")

        result = self.manager.start(r"\\.\USBPcap1", {"devices": "1,x"})
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "invalid_devices")

    def test_environment_shape(self):
        env = self.manager.environment(use_cache=False)
        self.assertIn("supported", env)
        self.assertIn("installed", env)
        self.assertIn("devices", env)
        self.assertEqual(env["supported"], is_windows())
        self.assertIsInstance(env["warnings"], list)
        self.assertIsInstance(env["elevated"], bool)

    def test_find_usbpcap_nonexistent(self):
        self.assertIsNone(find_usbpcap(str(Path(self.tmp.name) / "nope.exe")))

    def test_missing_file(self):
        result = self.manager.open_file(str(Path(self.tmp.name) / "missing.pcap"))
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "pcap_open_failed")

    def test_malformed_pcap_sets_error(self):
        bad = Path(self.tmp.name) / "bad.pcap"
        bad.write_bytes(b"not a pcap file at all, definitely longer than 24 bytes")
        result = self.manager.open_file(str(bad))
        self.assertTrue(result["success"])
        wait_loaded(self.manager)
        self.assertIsNotNone(self.manager.status()["lastError"])


if __name__ == "__main__":
    unittest.main()
