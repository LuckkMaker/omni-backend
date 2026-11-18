"""WinUSB 后端测试（结构体/图样/GUID；不打开真实设备）"""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import usb_winusb as winusb  # noqa: E402


class StructTests(unittest.TestCase):
    def test_sizes(self):
        sizes = winusb.helper_sizeof()
        self.assertEqual(sizes["WINUSB_SETUP_PACKET"], 8)
        self.assertEqual(sizes["USB_INTERFACE_DESCRIPTOR"], 9)
        self.assertEqual(sizes["GUID"], 16)

    def test_setup_packet_layout(self):
        setup = winusb.WINUSB_SETUP_PACKET(0x80, 0x06, 0x0100, 0x0000, 18)
        raw = bytes(setup)
        self.assertEqual(raw[:2], b"\x80\x06")
        self.assertEqual(int.from_bytes(raw[2:4], "little"), 0x0100)
        self.assertEqual(int.from_bytes(raw[6:8], "little"), 18)


class GuidTests(unittest.TestCase):
    def test_parse(self):
        guid = winusb.guid_from_string("{12345678-1234-5678-1234-567812345678}")
        self.assertEqual(guid.Data1, 0x12345678)
        raw = bytes(guid)
        self.assertEqual(len(raw), 16)
        self.assertEqual(raw[0], 0x78)

    def test_invalid(self):
        with self.assertRaises(winusb.WinUsbError):
            winusb.guid_from_string("not-a-guid")


class PatternTests(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(winusb.build_pattern("increment", 4), b"\x00\x01\x02\x03")
        self.assertEqual(winusb.build_pattern("zeros", 3), b"\x00\x00\x00")
        self.assertEqual(winusb.build_pattern("aa", 2), b"\xAA\xAA")
        self.assertEqual(winusb.build_pattern("ff", 2), b"\xFF\xFF")
        self.assertEqual(len(winusb.build_pattern("random", 16, seed=7)), 16)
        self.assertEqual(
            winusb.build_pattern("random", 16, seed=7),
            winusb.build_pattern("random", 16, seed=7),
        )

    def test_unknown_pattern(self):
        with self.assertRaises(winusb.WinUsbError):
            winusb.build_pattern("nope", 4)


class InstanceIdTests(unittest.TestCase):
    def test_plain(self):
        parsed = winusb.parse_instance_id(r"USB\VID_C251&PID_0002\316836531147")
        self.assertEqual(parsed["vid"], 0xC251)
        self.assertEqual(parsed["pid"], 0x0002)
        self.assertEqual(parsed["serial"], "316836531147")

    def test_composite(self):
        parsed = winusb.parse_instance_id(r"USB\VID_C251&PID_0001&MI_00\6&2b8f")
        self.assertEqual(parsed["vid"], 0xC251)
        self.assertEqual(parsed["pid"], 0x0001)
        self.assertEqual(parsed["serial"], "6&2b8f")

    def test_invalid(self):
        parsed = winusb.parse_instance_id("PCI\\VEN_8086")
        self.assertIsNone(parsed["vid"])
        self.assertIsNone(parsed["pid"])


class ManagerTests(unittest.TestCase):
    def test_devices_result_shape(self):
        result = winusb.WinUsbManager().devices()
        if sys.platform != "win32":
            self.assertFalse(result["success"])
            self.assertEqual(result["code"], "winusb_unavailable")
            return
        self.assertIn("success", result)
        if result["success"]:
            self.assertIsInstance(result["devices"], list)


if __name__ == "__main__":
    unittest.main()
