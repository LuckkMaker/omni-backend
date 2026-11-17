"""USBPcap pcap 解析测试"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.usb_pcap import (  # noqa: E402
    CONTROL_STAGE_COMPLETE,
    CONTROL_STAGE_DATA,
    CONTROL_STAGE_SETUP,
    CONTROL_STAGE_STATUS,
    IrpAssembler,
    PcapFormatError,
    PcapStreamReader,
    TRANSFER_BULK,
    TRANSFER_CONTROL,
    TRANSFER_ISOCHRONOUS,
)
from usb_fixtures import (  # noqa: E402
    control_transfer_records,
    make_pcap,
    record,
)


class PcapReaderTests(unittest.TestCase):
    def test_global_header_nanosecond_big_endian(self):
        pcap = make_pcap(
            [
                record(
                    ts=10.25,
                    transfer=TRANSFER_BULK,
                    endpoint=0x81,
                    info=1,
                    data=b"X",
                    stage=None,
                    nanosecond=True,
                    big_endian=True,
                )
            ],
            nanosecond=True,
            big_endian=True,
        )
        reader = PcapStreamReader()
        packets = reader.feed(pcap)
        self.assertEqual(len(packets), 1)
        self.assertAlmostEqual(packets[0].ts, 10.25, places=6)
        self.assertTrue(reader.nanosecond_timestamps)

    def test_wrong_linktype_rejected(self):
        pcap = make_pcap([], linktype=1)
        with self.assertRaises(PcapFormatError):
            PcapStreamReader().feed(pcap)

    def test_incremental_feed_byte_by_byte(self):
        pcap = make_pcap(
            [
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x81,
                    info=0,
                    data=b"",
                    stage=None,
                ),
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x81,
                    info=1,
                    data=b"hello",
                    stage=None,
                ),
            ]
        )
        reader = PcapStreamReader()
        packets = []
        for index in range(len(pcap)):
            packets.extend(reader.feed(pcap[index : index + 1]))
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[1].payload, b"hello")
        self.assertEqual(reader.total_bytes, len(pcap))

    def test_record_offsets(self):
        first = record(transfer=TRANSFER_BULK, endpoint=0x81, data=b"a", info=1, stage=None)
        second = record(transfer=TRANSFER_BULK, endpoint=0x82, data=b"b", info=1, stage=None)
        pcap = make_pcap([first, second])
        packets = PcapStreamReader().feed(pcap)
        self.assertEqual(packets[0].offset, 24 + 16)
        self.assertEqual(packets[1].offset, 24 + len(first) + 16)

    def test_truncated_payload_marked(self):
        raw = record(
            transfer=TRANSFER_BULK,
            endpoint=0x81,
            info=1,
            data=b"abc",
            stage=None,
        )
        body = bytearray(raw[16:])
        # 把 dataLength 改大，模拟 snaplen 截断
        struct.pack_into("<I", body, 23, 10)
        truncated_record = raw[:16] + bytes(body)
        packets = PcapStreamReader().feed(make_pcap([truncated_record]))
        self.assertTrue(packets[0].truncated)

    def test_malformed_header_len(self):
        raw = record(transfer=TRANSFER_BULK, endpoint=0x81, data=b"a", stage=None)
        body = bytearray(raw[16:])
        struct.pack_into("<H", body, 0, 5)
        with self.assertRaises(PcapFormatError):
            PcapStreamReader().feed(make_pcap([raw[:16] + bytes(body)]))


class AssemblerTests(unittest.TestCase):
    def test_control_new_model(self):
        setup = bytes([0x80, 6, 0, 1, 0, 0, 18, 0])
        pcap = make_pcap(control_transfer_records(irp_id=1, setup=setup, data=b"desc"))
        packets = PcapStreamReader().feed(pcap)
        transfers = []
        assembler = IrpAssembler()
        for packet in packets:
            transfers.extend(assembler.feed(packet))
        self.assertEqual(len(transfers), 1)
        transfer = transfers[0]
        self.assertEqual(transfer.setup, setup)
        self.assertEqual(transfer.direction, "in")
        self.assertEqual(transfer.data, b"desc")
        self.assertFalse(transfer.partial)
        self.assertEqual(transfer.duration_us, 10_000)

    def test_control_legacy_stages(self):
        setup = bytes([0x80, 6, 0, 1, 0, 0, 18, 0])
        pcap = make_pcap(
            control_transfer_records(irp_id=2, setup=setup, data=b"legacy", stage_new=False)
        )
        packets = PcapStreamReader().feed(pcap)
        assembler = IrpAssembler()
        transfers = []
        for packet in packets:
            transfers.extend(assembler.feed(packet))
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].data, b"legacy")

    def test_control_out_uses_setup_payload_tail(self):
        setup = bytes([0x00, 9, 1, 0, 0, 0, 0, 0])
        out_data = b"payload"
        pcap = make_pcap(
            [
                record(
                    transfer=TRANSFER_CONTROL,
                    endpoint=0x00,
                    info=0,
                    data=setup + out_data,
                    stage=CONTROL_STAGE_SETUP,
                ),
                record(
                    transfer=TRANSFER_CONTROL,
                    endpoint=0x00,
                    info=1,
                    data=b"",
                    stage=CONTROL_STAGE_COMPLETE,
                ),
            ]
        )
        assembler = IrpAssembler()
        transfers = []
        for packet in PcapStreamReader().feed(pcap):
            transfers.extend(assembler.feed(packet))
        self.assertEqual(transfers[0].direction, "out")
        self.assertEqual(transfers[0].data, out_data)

    def test_bulk_in_and_out(self):
        pcap = make_pcap(
            [
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x81,
                    info=0,
                    data=b"",
                    stage=None,
                ),
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x81,
                    info=1,
                    data=b"in-data",
                    stage=None,
                ),
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x01,
                    info=0,
                    data=b"out-data",
                    stage=None,
                ),
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x01,
                    info=1,
                    data=b"",
                    stage=None,
                ),
            ]
        )
        assembler = IrpAssembler()
        transfers = []
        for packet in PcapStreamReader().feed(pcap):
            transfers.extend(assembler.feed(packet))
        self.assertEqual([t.data for t in transfers], [b"in-data", b"out-data"])
        self.assertEqual([t.direction for t in transfers], ["in", "out"])

    def test_isochronous(self):
        pcap = make_pcap(
            [
                record(
                    transfer=TRANSFER_ISOCHRONOUS,
                    endpoint=0x82,
                    info=0,
                    data=b"",
                    stage=None,
                    iso_start_frame=100,
                    iso_packets=[(0, 0, 0), (192, 0, 0)],
                ),
                record(
                    transfer=TRANSFER_ISOCHRONOUS,
                    endpoint=0x82,
                    info=1,
                    data=b"\x01\x02" * 96,
                    stage=None,
                    iso_start_frame=100,
                    iso_packets=[(0, 192, 0), (192, 0, 0xC0000004)],
                ),
            ]
        )
        assembler = IrpAssembler()
        transfers = []
        for packet in PcapStreamReader().feed(pcap):
            transfers.extend(assembler.feed(packet))
        self.assertEqual(len(transfers), 1)
        self.assertEqual(len(transfers[0].iso_packets), 2)
        self.assertEqual(transfers[0].iso_packets[1].status, 0xC0000004)

    def test_injected_descriptors_irp_zero_pairing(self):
        """--inject-descriptors 复用 irpId=0，按 bus/device/endpoint 顺序配对"""
        device_setup = bytes([0x80, 6, 0, 1, 0, 0, 18, 0])
        config_setup = bytes([0x80, 6, 0, 2, 0, 0, 9, 0])
        pcap = make_pcap(
            control_transfer_records(irp_id=0, setup=device_setup, data=b"D" * 18, device=1)
            + control_transfer_records(irp_id=0, setup=config_setup, data=b"C" * 9, device=1)
            + control_transfer_records(irp_id=0, setup=device_setup, data=b"E" * 18, device=2)
        )
        transfers = []
        assembler = IrpAssembler()
        for packet in PcapStreamReader().feed(pcap):
            transfers.extend(assembler.feed(packet))
        self.assertEqual(len(transfers), 3)
        self.assertEqual([t.device for t in transfers], [1, 1, 2])
        self.assertEqual(transfers[0].setup, device_setup)
        self.assertEqual(transfers[0].data, b"D" * 18)
        self.assertEqual(transfers[1].data, b"C" * 9)

    def test_pipe_control_urb_label(self):
        pcap = make_pcap(
            [
                record(
                    transfer=0xFE,
                    function=0x001E,
                    endpoint=0x81,
                    info=0,
                    data=b"",
                    stage=None,
                ),
                record(
                    transfer=0xFE,
                    function=0x001E,
                    endpoint=0x81,
                    info=1,
                    data=b"",
                    stage=None,
                ),
            ]
        )
        assembler = IrpAssembler()
        transfers = []
        for packet in PcapStreamReader().feed(pcap):
            transfers.extend(assembler.feed(packet))
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].transfer_name, "urb")

    def test_unmatched_flush(self):
        pcap = make_pcap(
            [
                record(
                    transfer=TRANSFER_BULK,
                    endpoint=0x81,
                    info=0,
                    data=b"",
                    stage=None,
                )
            ]
        )
        assembler = IrpAssembler()
        for packet in PcapStreamReader().feed(pcap):
            assembler.feed(packet)
        self.assertEqual(assembler.flush(), 1)


if __name__ == "__main__":
    unittest.main()
