"""描述符模型测试：解析、JSON 文档、生成、校验、模板"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import usb_desc  # noqa: E402

CDC_ACM_SET = bytes.fromhex(
    "09 02 4B 00 02 01 00 80 32"
    "08 0B 00 02 02 02 01 00"
    "09 04 00 00 01 02 02 01 02"
    "05 24 00 10 01"
    "05 24 01 00 01"
    "04 24 02 06"
    "05 24 06 00 01"
    "07 05 82 03 08 00 10"
    "09 04 01 00 02 0A 00 00 00"
    "07 05 01 02 40 00 00"
    "07 05 81 02 40 00 00"
)


def device_bytes(vid: int = 0xC251, pid: int = 0x0002) -> bytes:
    return bytes.fromhex("12 01 00 02 00 00 00 40") + vid.to_bytes(2, "little") + (
        pid.to_bytes(2, "little") + bytes.fromhex("00 01 01 02 03 01")
    )


class ParseTests(unittest.TestCase):
    def test_device_descriptor(self):
        device = usb_desc.parse_device_descriptor(device_bytes())
        self.assertEqual(device.vid, 0xC251)
        self.assertEqual(device.pid, 0x0002)
        self.assertEqual(device.max_packet_size0, 64)
        self.assertEqual(usb_desc.bcd_text(device.bcd_usb), "2.00")

    def test_short_device_descriptor(self):
        with self.assertRaises(usb_desc.DescriptorError):
            usb_desc.parse_device_descriptor(b"\x12\x01")

    def test_config_set(self):
        config = usb_desc.parse_config_descriptor(CDC_ACM_SET)
        self.assertEqual(config.total_length, 0x4B)
        self.assertEqual(config.num_interfaces, 2)
        self.assertEqual(config.max_power, 0x32)
        self.assertEqual(len(config.class_descriptors), 1)
        self.assertEqual(config.class_descriptors[0].kind, "IAD")
        self.assertEqual(len(config.interfaces), 2)
        control = config.interfaces[0]
        self.assertEqual(control.klass, 0x02)
        self.assertEqual([item.subtype for item in control.class_descriptors], [0, 1, 2, 6])
        self.assertEqual([ep.address for ep in control.endpoints], [0x82])
        data_if = config.interfaces[1]
        self.assertEqual([ep.address for ep in data_if.endpoints], [0x01, 0x81])

    def test_string_descriptor(self):
        raw = bytes([2 + len("OMNI Bus".encode("utf-16-le")), 3]) + "OMNI Bus".encode(
            "utf-16-le"
        )
        self.assertEqual(usb_desc.parse_string_descriptor(raw), "OMNI Bus")

    def test_hex_parsing_formats(self):
        expected = bytes([0x12, 0x01, 0x00])
        for text in ("12 01 00", "12,01,00", "120100", "0x12 0x01 0x00", "12;01;00"):
            self.assertEqual(usb_desc.parse_hex(text), expected, text)

    def test_number_parsing(self):
        self.assertEqual(usb_desc.as_int("0x80"), 0x80)
        self.assertEqual(usb_desc.as_int("128"), 128)
        self.assertEqual(usb_desc.as_int(64), 64)
        self.assertEqual(usb_desc.as_int(None, 7), 7)


class DocumentTests(unittest.TestCase):
    def test_document_shape(self):
        device = usb_desc.parse_device_descriptor(device_bytes())
        config = usb_desc.parse_config_descriptor(CDC_ACM_SET)
        doc = usb_desc.to_document(
            device, [config], {1: {0x0409: "Geehy"}, 2: {0x0409: "CDC ACM"}}
        )
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["device"]["vid"], "0xC251")
        self.assertEqual(doc["configurations"][0]["value"], 1)
        self.assertEqual(len(doc["configurations"][0]["interfaces"]), 2)
        self.assertEqual(
            doc["configurations"][0]["interfaces"][0]["alternates"][0]["endpoints"][0][
                "address"
            ],
            "0x82",
        )
        self.assertEqual(doc["strings"]["0x0409"]["1"], "Geehy")

    def test_round_trip_bytes(self):
        device = usb_desc.parse_device_descriptor(device_bytes())
        config = usb_desc.parse_config_descriptor(CDC_ACM_SET)
        doc = usb_desc.to_document(device, [config])
        built = usb_desc.build_document(doc)
        self.assertEqual(built["device"], usb_desc.hex_string(device_bytes()))
        self.assertEqual(built["configurations"][0]["hex"], usb_desc.hex_string(CDC_ACM_SET))
        self.assertEqual(built["configurations"][0]["totalLength"], len(CDC_ACM_SET))

    def test_round_trip_is_stable(self):
        """build → parse → build 必须得到同一份字节，避免生成器丢字段。"""
        config = usb_desc.parse_config_descriptor(CDC_ACM_SET)
        doc = usb_desc.to_document(None, [config])
        first = usb_desc.build_document(doc)["configurations"][0]["hex"]
        reparsed = usb_desc.parse_config_descriptor(usb_desc.parse_hex(first))
        second = usb_desc.build_document(
            usb_desc.to_document(None, [reparsed])
        )["configurations"][0]["hex"]
        self.assertEqual(first, second)

    def test_build_template(self):
        doc = usb_desc.get_template("cdc-acm")
        built = usb_desc.build_document(doc)
        self.assertEqual(len(built["configurations"]), 1)
        parsed = usb_desc.parse_config_descriptor(usb_desc.parse_hex(
            built["configurations"][0]["hex"]
        ))
        self.assertEqual(parsed.num_interfaces, 2)
        self.assertEqual(parsed.interfaces[0].klass, 0x02)
        self.assertEqual(len(parsed.class_descriptors), 1)
        self.assertEqual(parsed.class_descriptors[0].kind, "IAD")

    def test_strings_built(self):
        doc = usb_desc.get_template("winusb")
        built = usb_desc.build_document(doc)
        self.assertEqual([item["index"] for item in built["strings"]], [1, 2, 3])
        # 每个字符串都能被重新解析
        for item in built["strings"]:
            raw = usb_desc.parse_hex(item["hex"])
            self.assertEqual(raw[1], usb_desc.DT_STRING)
            self.assertEqual(len(raw), raw[0])

    def test_unknown_template(self):
        with self.assertRaises(usb_desc.DescriptorError):
            usb_desc.get_template("nope")


class DetectTests(unittest.TestCase):
    def test_device(self):
        info = usb_desc.detect(device_bytes())
        self.assertEqual(info["kind"], "device")
        self.assertEqual(info["document"]["device"]["vid"], "0xC251")

    def test_configuration(self):
        info = usb_desc.detect(CDC_ACM_SET)
        self.assertEqual(info["kind"], "configuration")
        self.assertEqual(len(info["document"]["configurations"][0]["interfaces"]), 2)

    def test_setup(self):
        info = usb_desc.detect(bytes.fromhex("80 06 00 01 00 00 12 00"))
        self.assertEqual(info["kind"], "setup")
        self.assertEqual(info["length"], 8)

    def test_string(self):
        raw = bytes([2 + len("OMNI".encode("utf-16-le")), 3]) + "OMNI".encode("utf-16-le")
        info = usb_desc.detect(raw)
        self.assertEqual(info["kind"], "string")
        self.assertEqual(info["text"], "OMNI")

    def test_raw(self):
        info = usb_desc.detect(b"\xde\xad\xbe\xef")
        self.assertEqual(info["kind"], "raw")

    def test_empty(self):
        with self.assertRaises(usb_desc.DescriptorError):
            usb_desc.detect(b"")

    def test_normalize_is_idempotent(self):
        doc = usb_desc.get_template("cdc-acm")
        once = usb_desc.normalize_document(doc)
        twice = usb_desc.normalize_document(once)
        self.assertEqual(once, twice)
        self.assertIn("protocol", once["device"])

    def test_templates_metadata(self):
        items = usb_desc.templates()
        self.assertEqual([item["name"] for item in items], sorted(usb_desc.template_names()))
        self.assertTrue(all(item["description"] for item in items))


class ValidateTests(unittest.TestCase):
    def _doc(self):
        return usb_desc.get_template("winusb")

    def test_templates_are_valid(self):
        for name in usb_desc.template_names():
            report = usb_desc.validate_document(usb_desc.get_template(name))
            if name == "device":
                # 空白骨架：故意没有配置，校验应当拒绝，避免被误当成可用设备
                self.assertFalse(report["ok"])
                self.assertIn(
                    "configuration.missing",
                    [item["code"] for item in report["errors"]],
                )
                continue
            self.assertTrue(report["ok"], f"{name}: {report['errors']}")

    def test_cdc_template_valid(self):
        report = usb_desc.validate_document(usb_desc.get_template("cdc-acm"))
        self.assertTrue(report["ok"], report["errors"])

    def test_duplicate_endpoint(self):
        doc = self._doc()
        endpoints = doc["configurations"][0]["interfaces"][0]["alternates"][0]["endpoints"]
        endpoints.append(dict(endpoints[0]))
        report = usb_desc.validate_document(doc)
        self.assertFalse(report["ok"])
        self.assertIn("endpoint.address.duplicate", [item["code"] for item in report["errors"]])

    def test_max_packet_by_speed(self):
        doc = self._doc()
        doc["configurations"][0]["interfaces"][0]["alternates"][0]["endpoints"][0][
            "maxPacketSize"
        ] = 1024
        report = usb_desc.validate_document(doc, speed="full")
        self.assertFalse(report["ok"])
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("endpoint.maxPacketSize", codes)

    def test_missing_string(self):
        doc = self._doc()
        doc["device"]["strings"]["product"] = 9
        report = usb_desc.validate_document(doc)
        self.assertFalse(report["ok"])
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("string.missing.product", codes)

    def test_interface_numbers_contiguous(self):
        doc = self._doc()
        doc["configurations"][0]["interfaces"][0]["number"] = 2
        report = usb_desc.validate_document(doc)
        self.assertFalse(report["ok"])
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("interface.number.contiguous", codes)

    def test_iad_range(self):
        doc = usb_desc.get_template("cdc-acm")
        doc["configurations"][0]["classDescriptors"][0]["interfaceCount"] = 3
        report = usb_desc.validate_document(doc)
        self.assertFalse(report["ok"])
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("iad.range", codes)

    def test_power_warning(self):
        doc = self._doc()
        doc["configurations"][0]["maxPower"] = 600
        report = usb_desc.validate_document(doc)
        self.assertTrue(report["ok"])
        self.assertIn("config.maxPower", [item["code"] for item in report["warnings"]])

    def test_bos_warning(self):
        doc = self._doc()
        doc["device"]["bcdUSB"] = "0x0210"
        report = usb_desc.validate_document(doc)
        self.assertIn("bos.missing", [item["code"] for item in report["warnings"]])


if __name__ == "__main__":
    unittest.main()
