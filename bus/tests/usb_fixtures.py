"""USBPcap pcap fixture 构造工具（测试与本地回放共用）"""

from __future__ import annotations

import struct

BASE_HEADER = struct.Struct("<HQIHBHHBBI")
ISO_EXTRA = struct.Struct("<III")
ISO_PACKET = struct.Struct("<III")

GLOBAL_HEADER_USEC = struct.Struct("<IHHIIII")


def pcap_global_header(
    *, nanosecond: bool = False, big_endian: bool = False, linktype: int = 249, snaplen: int = 65535
) -> bytes:
    magic = 0xA1B23C4D if nanosecond else 0xA1B2C3D4
    body = struct.pack("<HHIIII", 2, 4, 0, 0, snaplen, linktype)
    if big_endian:
        return struct.pack(">I", magic) + struct.pack(">HHIIII", 2, 4, 0, 0, snaplen, linktype)
    return struct.pack("<I", magic) + body


def record(
    *,
    ts: float = 1.0,
    irp_id: int = 1,
    status: int = 0,
    function: int = 0x0008,
    info: int = 0,
    bus: int = 1,
    device: int = 2,
    endpoint: int = 0x80,
    transfer: int = 2,
    data: bytes = b"",
    stage: int | None = 0,
    iso_start_frame: int | None = None,
    iso_packets: list[tuple[int, int, int]] | None = None,
    nanosecond: bool = False,
    big_endian: bool = False,
) -> bytes:
    extra = b""
    if transfer == 2 and stage is not None:
        extra = bytes([stage])
    elif transfer == 0:
        packets = iso_packets or []
        extra = ISO_EXTRA.pack(
            iso_start_frame or 0, len(packets), sum(1 for p in packets if p[2] != 0)
        )
        for offset, length, pstatus in packets:
            extra += ISO_PACKET.pack(offset, length, pstatus)

    header_len = 27 + len(extra)
    body = BASE_HEADER.pack(
        header_len, irp_id, status, function, info, bus, device, endpoint, transfer, len(data)
    ) + extra + data
    sec = int(ts)
    frac = round((ts - sec) * (1_000_000_000 if nanosecond else 1_000_000))
    if big_endian:
        rec_header = struct.pack(">IIII", sec, frac, len(body), len(body))
    else:
        rec_header = struct.pack("<IIII", sec, frac, len(body), len(body))
    return rec_header + body


def make_pcap(records: list[bytes], **header_kwargs) -> bytes:
    return pcap_global_header(**header_kwargs) + b"".join(records)


def device_descriptor(
    *,
    vid: int = 0x1234,
    pid: int = 0x5678,
    device_class: int = 0,
    subclass: int = 0,
    protocol: int = 0,
    num_configs: int = 1,
    i_manufacturer: int = 0,
    i_product: int = 0,
    i_serial: int = 0,
) -> bytes:
    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        1,
        0x0200,
        device_class,
        subclass,
        protocol,
        64,
        vid,
        pid,
        0x0100,
        i_manufacturer,
        i_product,
        i_serial,
        num_configs,
    )


def config_descriptor(
    *,
    interface_class: int = 0x02,
    interface_subclass: int = 0x02,
    interface_protocol: int = 0x01,
    endpoint_address: int = 0x81,
    endpoint_type: int = 2,
    max_packet: int = 64,
    extra_descriptors: bytes = b"",
) -> bytes:
    interface = struct.pack(
        "<BBBBBBBBB",
        9,
        4,
        0,
        0,
        1,
        interface_class,
        interface_subclass,
        interface_protocol,
        0,
    )
    endpoint = struct.pack(
        "<BBBBHB", 7, 5, endpoint_address, endpoint_type, max_packet, 0
    )
    body = interface + endpoint + extra_descriptors
    total = 9 + len(body)
    header = struct.pack("<BBHBBBBB", 9, 2, total, 1, 1, 0, 0x80, 50)
    return header + body


def string_descriptor(text: str) -> bytes:
    encoded = text.encode("utf-16-le")
    return bytes([2 + len(encoded), 3]) + encoded


def control_transfer_records(
    *,
    irp_id: int,
    setup: bytes,
    data: bytes = b"",
    status: int = 0,
    ts: float = 1.0,
    bus: int = 1,
    device: int = 2,
    stage_new: bool = True,
) -> list[bytes]:
    """构造一次控制传输的 pcap 记录（新/旧 stage 模型）"""
    records = [
        record(
            ts=ts,
            irp_id=irp_id,
            status=0,
            info=0,
            bus=bus,
            device=device,
            endpoint=0x80,
            transfer=2,
            data=setup,
            stage=0,
        )
    ]
    if stage_new:
        records.append(
            record(
                ts=ts + 0.01,
                irp_id=irp_id,
                status=status,
                info=1,
                bus=bus,
                device=device,
                endpoint=0x80,
                transfer=2,
                data=data,
                stage=3,
            )
        )
    else:
        if data:
            records.append(
                record(
                    ts=ts + 0.005,
                    irp_id=irp_id,
                    status=0,
                    info=1,
                    bus=bus,
                    device=device,
                    endpoint=0x80,
                    transfer=2,
                    data=data,
                    stage=1,
                )
            )
        records.append(
            record(
                ts=ts + 0.01,
                irp_id=irp_id,
                status=status,
                info=1,
                bus=bus,
                device=device,
                endpoint=0x80,
                transfer=2,
                data=b"",
                stage=2,
            )
        )
    return records
