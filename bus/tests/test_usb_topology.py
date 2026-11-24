"""Windows USB hub 动态地址映射测试（不访问真实硬件）。"""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import usb_topology as topology  # noqa: E402


ROOT = r"USB\ROOT_HUB30\ROOT"
HUBS = [
    {
        "index": 2,
        "device": r"\\.\USBPcap2",
        "label": "USBPcap2",
        "instanceId": ROOT,
        "controller": "xHCI",
        "controllerId": "8086:51ED",
    }
]


class StructureTests(unittest.TestCase):
    def test_windows_layouts_are_packed(self):
        self.assertEqual(ctypes.sizeof(topology._USB_DEVICE_DESCRIPTOR), 18)
        self.assertEqual(
            ctypes.sizeof(topology._USB_NODE_CONNECTION_INFORMATION_EX), 35
        )
        self.assertEqual(ctypes.sizeof(topology._USB_NODE_INFORMATION), 76)

    def test_ioctl_values(self):
        self.assertEqual(topology._IOCTL_USB_GET_NODE_INFORMATION, 0x220408)
        self.assertEqual(
            topology._IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME, 0x220420
        )
        self.assertEqual(
            topology._IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, 0x220448
        )


class AnnotationTests(unittest.TestCase):
    def test_nearest_driver_key_wins_and_composite_inherits(self):
        devices = [
            {
                "instanceId": "mouse",
                "vid": "0x046D",
                "pid": "0xC542",
            },
            {
                "instanceId": "camera-interface",
                "vid": "0x04F2",
                "pid": "0xB7B4",
            },
            {
                "instanceId": ROOT,
                "vid": None,
                "pid": None,
            },
        ]
        records = [
            {
                "address": "2.47",
                "bus": 2,
                "deviceAddress": 47,
                "vid": 0x046D,
                "pid": 0xC542,
                "driverKey": "mouse-key",
                "rootHub": ROOT,
            },
            {
                "address": "2.1",
                "bus": 2,
                "deviceAddress": 1,
                "vid": 0x04F2,
                "pid": 0xB7B4,
                "driverKey": "composite-key",
                "rootHub": ROOT,
            },
            {
                "address": "2.21",
                "bus": 2,
                "deviceAddress": 21,
                "vid": 0x2109,
                "pid": 0x2822,
                "driverKey": "upstream-hub-key",
                "rootHub": ROOT,
            },
        ]
        roots = {"mouse": ROOT, "camera-interface": ROOT, ROOT: ROOT}
        driver_keys = {
            "mouse": ["mouse-key", "upstream-hub-key"],
            "camera-interface": ["interface-key", "composite-key", "upstream-hub-key"],
            ROOT: ["root-key"],
        }
        with (
            patch.object(topology, "usbpcap_root_hubs", return_value=HUBS),
            patch.object(topology, "usb_device_addresses", return_value=records),
            patch.object(topology, "root_hub_for", side_effect=lambda value: roots[value]),
            patch.object(
                topology,
                "_driver_keys_for",
                side_effect=lambda value: driver_keys[value],
            ),
        ):
            topology.annotate_devices(devices)

        self.assertEqual(devices[0]["captureAddresses"], ["2.47"])
        self.assertEqual(devices[0]["usbBus"], 2)
        self.assertEqual(devices[0]["usbDeviceAddress"], 47)
        self.assertEqual(devices[0]["addressSource"], "hub_ioctl")
        self.assertEqual(devices[1]["captureAddresses"], ["2.1"])
        self.assertEqual(devices[1]["usbDeviceAddress"], 1)
        self.assertEqual(devices[2]["captureAddresses"], [])

    def test_vid_pid_fallback_requires_unique_candidate(self):
        devices = [
            {"instanceId": "unique", "vid": "0x1234", "pid": "0x0001"},
            {"instanceId": "duplicate", "vid": "0x1234", "pid": "0x0002"},
        ]
        records = [
            {
                "address": "2.7",
                "bus": 2,
                "deviceAddress": 7,
                "vid": 0x1234,
                "pid": 0x0001,
                "driverKey": None,
                "rootHub": ROOT,
            },
            {
                "address": "2.8",
                "bus": 2,
                "deviceAddress": 8,
                "vid": 0x1234,
                "pid": 0x0002,
                "driverKey": None,
                "rootHub": ROOT,
            },
            {
                "address": "2.9",
                "bus": 2,
                "deviceAddress": 9,
                "vid": 0x1234,
                "pid": 0x0002,
                "driverKey": None,
                "rootHub": ROOT,
            },
        ]
        with (
            patch.object(topology, "usbpcap_root_hubs", return_value=HUBS),
            patch.object(topology, "usb_device_addresses", return_value=records),
            patch.object(topology, "root_hub_for", return_value=ROOT),
            patch.object(topology, "_driver_keys_for", return_value=[]),
        ):
            topology.annotate_devices(devices)

        self.assertEqual(devices[0]["captureAddresses"], ["2.7"])
        self.assertEqual(devices[1]["captureAddresses"], [])
        self.assertIsNone(devices[1]["usbDeviceAddress"])


if __name__ == "__main__":
    unittest.main()
