"""HID 后端测试（报告缓冲区/结构体；不打开真实设备）"""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import usb_hid  # noqa: E402


class ReportBufferTests(unittest.TestCase):
    def test_pads_to_report_length(self):
        self.assertEqual(
            usb_hid.build_report_buffer(1, b"\xAA\xBB", 5),
            b"\x01\xAA\xBB\x00\x00",
        )

    def test_zero_report_id(self):
        self.assertEqual(usb_hid.build_report_buffer(0, b"\x01", 3), b"\x00\x01\x00")

    def test_truncates_oversized_payload(self):
        self.assertEqual(usb_hid.build_report_buffer(2, b"abcdef", 4), b"\x02abc")

    def test_default_length(self):
        self.assertEqual(usb_hid.build_report_buffer(0, b"ab", 0), b"\x00ab")


class StructTests(unittest.TestCase):
    def test_sizes(self):
        sizes = usb_hid.helper_sizeof()
        self.assertEqual(sizes["GUID"], 16)
        self.assertEqual(sizes["HIDP_CAPS"], 64)
        self.assertGreaterEqual(sizes["OVERLAPPED"], 24)

    def test_device_dict(self):
        device = usb_hid.HidDevice(
            path=r"\\?\hid#vid_c251",
            vid=0xC251,
            pid=0x0001,
            version=0x0100,
            product="HID Demo",
            manufacturer="Geehy",
            serial="1",
            usage_page=0xFF00,
            usage=0x0001,
            input_report_length=64,
            output_report_length=64,
        )
        payload = device.to_dict()
        self.assertEqual(payload["vid"], "0xC251")
        self.assertEqual(payload["usagePage"], "0xFF00")
        self.assertEqual(payload["inputReportLength"], 64)


class UnavailableTests(unittest.TestCase):
    def test_manager_reports_unavailable_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows 上走真实枚举，不在单测里打开设备")
        result = usb_hid.HidManager().open("t", vid=0xC251, pid=0x0001)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "hid_unavailable")

    def test_api_raises_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows 上 HID API 可用")
        with self.assertRaises(usb_hid.HidUnavailable):
            usb_hid.enumerate_devices()


if __name__ == "__main__":
    unittest.main()
