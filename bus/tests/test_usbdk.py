"""UsbDk 绑定测试（不加载真实驱动；传输路径用纯函数覆盖）"""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import usbdk  # noqa: E402


class StructLayoutTests(unittest.TestCase):
    def test_device_descriptor_size(self):
        # USB_DEVICE_DESCRIPTOR 固定 18 字节（无填充）
        self.assertEqual(ctypes.sizeof(usbdk.USB_DEVICE_DESCRIPTOR), 18)

    def test_device_info_offsets(self):
        info = usbdk.USB_DK_DEVICE_INFO
        id_size = ctypes.sizeof(usbdk.USB_DK_DEVICE_ID)
        self.assertEqual(info.FilterID.offset, id_size)
        self.assertEqual(info.Port.offset, id_size + 8)
        self.assertEqual(info.Speed.offset, id_size + 16)
        self.assertEqual(info.DeviceDescriptor.offset, id_size + 24)

    def test_transfer_request_fields(self):
        request = usbdk.USB_DK_TRANSFER_REQUEST()
        request.EndpointAddress = 0x81
        request.BufferLength = 64
        request.TransferType = usbdk.BULK_TRANSFER
        request.Result.GenResult.BytesTransferred = 5
        request.Result.GenResult.UsbdStatus = 0
        self.assertEqual(request.EndpointAddress, 0x81)
        self.assertEqual(request.Result.GenResult.BytesTransferred, 5)


class ControlBufferTests(unittest.TestCase):
    def test_control_in(self):
        buffer, direction, length = usbdk.build_control_buffer(0x80, 0x06, 0x0100, 0, 18)
        self.assertEqual(direction, "in")
        self.assertEqual(length, 18)
        self.assertEqual(len(buffer), 26)
        self.assertEqual(bytes(buffer[:8]), bytes.fromhex("80 06 00 01 00 00 12 00"))
        self.assertEqual(buffer[8:], b"\x00" * 18)

    def test_control_out(self):
        buffer, direction, length = usbdk.build_control_buffer(
            0x40, 0x01, 0x1234, 0x0002, 4, b"\x01\x02\x03\x04"
        )
        self.assertEqual(direction, "out")
        self.assertEqual(length, 4)
        self.assertEqual(bytes(buffer[:8]), bytes.fromhex("40 01 34 12 02 00 04 00"))
        self.assertEqual(bytes(buffer[8:]), b"\x01\x02\x03\x04")

    def test_control_out_length_follows_data(self):
        buffer, _direction, length = usbdk.build_control_buffer(0x40, 0x01, 0, 0, 99, b"abc")
        self.assertEqual(length, 3)
        self.assertEqual(len(buffer), 11)

    def test_payload_length(self):
        self.assertEqual(usbdk.control_payload_length(26, 18, "in"), 18)
        self.assertEqual(usbdk.control_payload_length(18, 18, "in"), 18)
        self.assertEqual(usbdk.control_payload_length(8, 0, "in"), 0)
        self.assertEqual(usbdk.control_payload_length(10, 8, "in"), 2)
        self.assertEqual(usbdk.control_payload_length(8, 4, "out"), 0)

    def test_status_name(self):
        self.assertEqual(usbdk.usbd_status_name(0xC0000004), "STALL_PID")
        self.assertTrue(usbdk.usbd_status_name(0x12345678).startswith("0x"))


class DeviceInfoTests(unittest.TestCase):
    def test_from_raw(self):
        raw = usbdk.USB_DK_DEVICE_INFO()
        raw.ID.DeviceID = r"USB\VID_C251&PID_0002\0123"
        raw.ID.InstanceID = r"USB\VID_C251&PID_0002\0123"
        raw.FilterID = 1
        raw.Port = 3
        raw.Speed = 3
        raw.DeviceDescriptor.idVendor = 0xC251
        raw.DeviceDescriptor.idProduct = 0x0002
        raw.DeviceDescriptor.bcdUSB = 0x0210
        raw.DeviceDescriptor.bNumConfigurations = 1

        device = usbdk.device_from_raw(raw)
        self.assertEqual(device.vid, 0xC251)
        self.assertEqual(device.pid, 0x0002)
        self.assertEqual(device.speed, "high")
        self.assertEqual(device.port, 3)
        payload = device.to_dict()
        self.assertEqual(payload["vid"], "0xC251")
        self.assertEqual(payload["bcdUSB"], "0x0210")


class EnvironmentTests(unittest.TestCase):
    def test_doctor_shape(self):
        result = usbdk.doctor()
        for key in ("supported", "installed", "path", "service", "available", "devices", "warnings"):
            self.assertIn(key, result)
        self.assertIsInstance(result["devices"], list)
        self.assertIsInstance(result["warnings"], list)

    def test_missing_helper_is_unavailable(self):
        if usbdk.find_helper() is not None:
            self.skipTest("本机已安装 UsbDk，跳过缺失路径")
        with self.assertRaises(usbdk.UsbDkUnavailable):
            usbdk.UsbdkLibrary()

    def test_explain(self):
        self.assertIn("1060", usbdk._explain(1060))
        self.assertIn("管理员", usbdk._explain(5))


if __name__ == "__main__":
    unittest.main()
