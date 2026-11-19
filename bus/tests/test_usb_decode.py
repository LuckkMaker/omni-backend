"""USB 解码与描述符收集测试"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.usb_decode import (  # noqa: E402
    DescriptorCollector,
    decode_line_coding,
    decode_msc,
    class_hint_for,
    decode_setup,
    parse_config_descriptor,
    parse_device_descriptor,
    parse_string_descriptor,
    status_name,
    summarize_transfer,
    urb_function_name,
)
from core.usb_pcap import IrpAssembler, PcapStreamReader  # noqa: E402
from usb_fixtures import (  # noqa: E402
    config_descriptor,
    control_transfer_records,
    device_descriptor,
    make_pcap,
    string_descriptor,
)


def assemble(records):
    assembler = IrpAssembler()
    out = []
    for packet in PcapStreamReader().feed(make_pcap(records)):
        out.extend(assembler.feed(packet))
    return out


def control_setup(bm: int, request: int, value: int, index: int, length: int) -> bytes:
    return struct.pack("<BBHHH", bm, request, value, index, length)


class SetupDecodeTests(unittest.TestCase):
    def test_get_descriptor_device(self):
        setup = control_setup(0x80, 6, 0x0100, 0, 18)
        info = decode_setup(setup)
        self.assertEqual(info["requestName"], "GET_DESCRIPTOR")
        self.assertEqual(info["decoded"]["descriptorType"], "DEVICE")
        self.assertEqual(info["direction"], "in")
        self.assertIn("GET_DESCRIPTOR(DEVICE)", info["summary"])

    def test_set_configuration(self):
        info = decode_setup(control_setup(0x00, 9, 1, 0, 0))
        self.assertEqual(info["type"], "standard")
        self.assertIn("SET_CONFIGURATION(1)", info["summary"])

    def test_rndis_encapsulated(self):
        self.assertEqual(class_hint_for(0xEF, 0x04), "rndis")
        info = decode_setup(control_setup(0x21, 0x00, 0, 0, 12), class_hint="rndis")
        self.assertEqual(info["requestName"], "SEND_ENCAPSULATED_COMMAND")
        info = decode_setup(control_setup(0xA1, 0x01, 0, 0, 16), class_hint="rndis")
        self.assertEqual(info["requestName"], "GET_ENCAPSULATED_RESPONSE")

    def test_status_names(self):
        self.assertEqual(status_name(0x80000300), "INVALID_PARAMETER")
        self.assertEqual(status_name(0xC0010000), "CANCELED")
        self.assertEqual(status_name(0xC0007000), "DEVICE_GONE")
        self.assertEqual(status_name(0xC0000005), "DEV_NOT_RESPONDING")

    def test_urb_function_names(self):
        self.assertEqual(urb_function_name(0x2A), "GET_MS_FEATURE_DESCRIPTOR")
        self.assertEqual(urb_function_name(0x38), "RESET_PIPE")
        self.assertEqual(urb_function_name(0x1E), "SYNC_RESET_PIPE_AND_CLEAR_STALL")
        self.assertEqual(urb_function_name(0x0002), "ABORT_PIPE")

    def test_set_address(self):
        info = decode_setup(control_setup(0x00, 5, 7, 0, 0))
        self.assertEqual(info["decoded"]["deviceAddress"], 7)

    def test_cdc_line_coding(self):
        coding = struct.pack("<IBBB", 115200, 0, 0, 8)
        setup = control_setup(0x21, 0x20, 0, 0, 7)
        info = decode_setup(setup, class_hint="cdc")
        self.assertEqual(info["requestName"], "SET_LINE_CODING")
        summary = summarize_transfer(2, "out", info, coding, "cdc")
        self.assertIn("115200", summary)

    def test_hid_set_report(self):
        info = decode_setup(control_setup(0x21, 0x09, (2 << 8) | 5, 0, 8), class_hint="hid")
        self.assertEqual(info["requestName"], "SET_REPORT")
        self.assertEqual(info["decoded"]["reportType"], "Output")
        self.assertEqual(info["decoded"]["reportId"], 5)

    def test_hub_port_reset(self):
        info = decode_setup(control_setup(0x23, 3, 4, 2, 0), class_hint="hub")
        self.assertEqual(info["requestName"], "SET_FEATURE")
        self.assertEqual(info["decoded"]["hubFeature"], "PORT_RESET")
        self.assertEqual(info["decoded"]["port"], 3)
        self.assertIn("SET_FEATURE(PORT_RESET, port 3)", info["summary"])

    def test_hub_port_suspend(self):
        info = decode_setup(control_setup(0x23, 3, 2, 0, 0), class_hint="hub")
        self.assertIn("PORT_SUSPEND", info["summary"])

    def test_vendor_request(self):
        info = decode_setup(control_setup(0x40, 0xA1, 0x1234, 0, 4))
        self.assertEqual(info["type"], "vendor")
        self.assertIn("VENDOR", info["requestName"])

    def test_status_names(self):
        self.assertEqual(status_name(0), "SUCCESS")
        self.assertEqual(status_name(0xC0000004), "STALL_PID")


class DescriptorTests(unittest.TestCase):
    def test_device_descriptor(self):
        parsed = parse_device_descriptor(device_descriptor(vid=0x0483, pid=0x5740))
        self.assertEqual(parsed["vid"], "0x0483")
        self.assertEqual(parsed["pid"], "0x5740")
        self.assertEqual(parsed["bcdUSB"], "2.00")
        self.assertEqual(parsed["maxPacketSize0"], 64)

    def test_config_descriptor(self):
        parsed = parse_config_descriptor(config_descriptor())
        self.assertEqual(parsed["totalLength"], 25)
        self.assertEqual(len(parsed["interfaces"]), 1)
        interface = parsed["interfaces"][0]
        self.assertEqual(interface["className"], "CDC")
        self.assertEqual(interface["endpoints"][0]["direction"], "in")
        self.assertEqual(interface["endpoints"][0]["type"], "bulk")

    def test_string_descriptor(self):
        self.assertEqual(parse_string_descriptor(string_descriptor("OMNI Bus")), "OMNI Bus")

    def test_line_coding(self):
        coding = decode_line_coding(struct.pack("<IBBB", 9600, 2, 2, 7))
        self.assertEqual(coding["baudRate"], 9600)
        self.assertEqual(coding["stopBits"], 2)
        self.assertEqual(coding["parity"], "Even")
        self.assertEqual(coding["dataBits"], 7)

    def test_msc_cbw_csw(self):
        cdb = bytes([0x28, 0, 0, 0, 0, 0, 0, 0, 1, 0])
        cbw = struct.pack("<IIIBBB16s", 0x43425355, 7, 512, 0x80, 0, len(cdb), cdb)
        decoded = decode_msc(cbw)
        self.assertEqual(decoded["kind"], "CBW")
        self.assertEqual(decoded["opcodeName"], "READ_10")
        self.assertEqual(decoded["direction"], "in")

        csw = struct.pack("<IIIB", 0x53425355, 7, 0, 0)
        decoded = decode_msc(csw)
        self.assertEqual(decoded["kind"], "CSW")
        self.assertEqual(decoded["statusName"], "Passed")


class CollectorTests(unittest.TestCase):
    def _device_records(self, *, with_strings: bool = True):
        records = []
        records += control_transfer_records(
            irp_id=1,
            setup=control_setup(0x80, 6, 0x0100, 0, 18),
            data=device_descriptor(i_manufacturer=1, i_product=2),
            ts=1.0,
        )
        records += control_transfer_records(
            irp_id=2,
            setup=control_setup(0x80, 6, 0x0200, 0, 9),
            data=config_descriptor(interface_class=0x02, endpoint_address=0x81),
            ts=1.1,
        )
        if with_strings:
            records += control_transfer_records(
                irp_id=3,
                setup=control_setup(0x80, 6, 0x0301, 0x0409, 255),
                data=string_descriptor("OMNI"),
                ts=1.2,
            )
            records += control_transfer_records(
                irp_id=4,
                setup=control_setup(0x80, 6, 0x0302, 0x0409, 255),
                data=string_descriptor("USB Serial"),
                ts=1.3,
            )
        return records

    def test_collector_tree_and_class_hint(self):
        transfers = assemble(self._device_records())
        collector = DescriptorCollector()
        for transfer in transfers:
            collector.feed(transfer)
        self.assertEqual(collector.class_hint(1, 2, 0x81), "cdc")
        tree = collector.tree()
        self.assertEqual(len(tree), 1)
        device = tree[0]
        self.assertEqual(device["kind"], "device")
        self.assertIn("USB Serial", device["title"])
        configs = device["children"]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["kind"], "configuration")
        interfaces = configs[0]["children"]
        self.assertEqual(interfaces[0]["kind"], "interface")
        self.assertEqual(interfaces[0]["children"][0]["kind"], "endpoint")

    def test_partial_config_does_not_crash(self):
        transfers = assemble(
            control_transfer_records(
                irp_id=9,
                setup=control_setup(0x80, 6, 0x0200, 0, 9),
                data=config_descriptor()[:9],
            )
        )
        collector = DescriptorCollector()
        for transfer in transfers:
            collector.feed(transfer)
        self.assertEqual(collector.tree(), [])


if __name__ == "__main__":
    unittest.main()
