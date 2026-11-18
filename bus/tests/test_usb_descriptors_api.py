"""描述符 API 的离线测试（from-capture / parse / build，无需 USBPcap）"""

from __future__ import annotations

import asyncio
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api import usb_descriptors  # noqa: E402
from core import usb_desc  # noqa: E402
from usb_fixtures import (  # noqa: E402
    config_descriptor,
    control_transfer_records,
    device_descriptor,
    make_pcap,
    string_descriptor,
)


def setup(bm: int, request: int, value: int, index: int, length: int) -> bytes:
    return struct.pack("<BBHHH", bm, request, value, index, length)


def build_capture(path: Path) -> None:
    records = []
    records += control_transfer_records(
        irp_id=1, setup=setup(0x80, 6, 0x0100, 0, 18), data=device_descriptor(), ts=1.0
    )
    records += control_transfer_records(
        irp_id=2, setup=setup(0x80, 6, 0x0200, 0, 25), data=config_descriptor(), ts=1.1
    )
    records += control_transfer_records(
        irp_id=3,
        setup=setup(0x80, 6, 0x0301, 0x0409, 255),
        data=string_descriptor("OMNI"),
        ts=1.2,
    )
    path.write_bytes(make_pcap(records))


class FromCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "capture.pcap"
        build_capture(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_documents(self):
        documents = usb_descriptors._documents_from_pcap(str(self.path), None, None)
        self.assertEqual(len(documents), 1)
        item = documents[0]
        self.assertEqual((item["bus"], item["device"]), (1, 2))
        doc = item["document"]
        self.assertEqual(doc["device"]["vid"], "0x1234")
        self.assertEqual(len(doc["configurations"]), 1)
        self.assertEqual(doc["strings"]["0x0409"]["1"], "OMNI")

    def test_filter_by_device(self):
        self.assertEqual(usb_descriptors._documents_from_pcap(str(self.path), 1, 2), usb_descriptors._documents_from_pcap(str(self.path), None, None))
        self.assertEqual(usb_descriptors._documents_from_pcap(str(self.path), 2, None), [])
        self.assertEqual(usb_descriptors._documents_from_pcap(str(self.path), None, 9), [])

    def test_missing_file(self):
        result = asyncio.run(
            usb_descriptors.from_capture(
                usb_descriptors.FromCaptureRequest(path=str(self.path.parent / "nope.pcap"))
            )
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "pcap_not_found")


class RouteTests(unittest.TestCase):
    def test_parse_hex_device(self):
        result = asyncio.run(
            usb_descriptors.parse(
                usb_descriptors.ParseRequest(hex=usb_desc.hex_string(device_descriptor()))
            )
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["kind"], "device")
        self.assertEqual(result["document"]["device"]["vid"], "0x1234")

    def test_parse_bad_hex(self):
        result = asyncio.run(
            usb_descriptors.parse(usb_descriptors.ParseRequest(hex="zz"))
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "desc_parse_failed")

    def test_build_and_validate(self):
        document = usb_desc.get_template("winusb")
        built = asyncio.run(
            usb_descriptors.build(usb_descriptors.DocumentRequest(document=document))
        )
        self.assertTrue(built["success"])
        self.assertTrue(built["validation"]["ok"])
        self.assertTrue(built["configurations"][0]["hex"].startswith("09 02"))

    def test_templates(self):
        result = asyncio.run(usb_descriptors.templates())
        names = [item["name"] for item in result["templates"]]
        self.assertIn("cdc-acm", names)
        self.assertIn("winusb", names)


if __name__ == "__main__":
    unittest.main()
