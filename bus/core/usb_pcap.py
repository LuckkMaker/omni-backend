"""USBPcap pcap 解析与 IRP 重组

纯标准库实现，解析 USBPcap 驱动输出（LINKTYPE_USBPCAP = 249）并重组为
一次 IRP 对应的一次 USB 传输。

文件格式参考：
  https://desowin.org/usbpcap/captureformat.html

对外提供：
  - ``PcapStreamReader``: 增量读取 pcap 字节流（实机捕获 / 离线文件共用）
  - ``parse_record``: 解析单条 pcap 记录（详情按需回读 spool 时使用）
  - ``IrpAssembler``: submit/complete 配对，输出 ``UsbTransfer``
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable

LINKTYPE_USBPCAP = 249

PCAP_MAGIC_USEC = 0xA1B2C3D4
PCAP_MAGIC_NSEC = 0xA1B23C4D

# USBPCAP transfer type
TRANSFER_ISOCHRONOUS = 0
TRANSFER_INTERRUPT = 1
TRANSFER_CONTROL = 2
TRANSFER_BULK = 3

TRANSFER_TYPE_NAMES = {
    TRANSFER_ISOCHRONOUS: "isochronous",
    TRANSFER_INTERRUPT: "interrupt",
    TRANSFER_CONTROL: "control",
    TRANSFER_BULK: "bulk",
    # USBPcap 用 0xFE 表示非数据传输 URB（如 ABORT_PIPE 等管道控制）
    0xFE: "urb",
}

# USBPCAP control stage
CONTROL_STAGE_SETUP = 0
CONTROL_STAGE_DATA = 1
CONTROL_STAGE_STATUS = 2
CONTROL_STAGE_COMPLETE = 3

BASE_HEADER_LEN = 27
ISO_HEADER_LEN = 39
PCAP_GLOBAL_HEADER_LEN = 24
PCAP_RECORD_HEADER_LEN = 16
DEFAULT_MAX_RECORD_SIZE = 16 * 1024 * 1024
MAX_PENDING_IRPS = 4096

# ``<HQIHBHHBBI`` 对应 USBPCAP_BUFFER_PACKET_HEADER（packed，27 字节）
_BASE_HEADER = struct.Struct("<HQIHBHHBBI")
_RECORD_HEADER_LE = struct.Struct("<IIII")
_RECORD_HEADER_BE = struct.Struct(">IIII")
_ISO_EXTRA = struct.Struct("<III")
_ISO_PACKET = struct.Struct("<III")


class PcapError(Exception):
    """pcap 解析基类异常"""


class PcapFormatError(PcapError):
    """文件/记录格式不符合预期"""


@dataclass(slots=True, frozen=True)
class IsoPacket:
    """等时传输中的单个 microframe 描述"""

    offset: int
    length: int
    status: int


@dataclass(slots=True, frozen=True)
class RecordRef:
    """一条 pcap 记录在 spool 文件中的位置（用于按需回读详情）"""

    offset: int
    length: int
    ts: float


@dataclass(slots=True)
class UsbPacket:
    """USBPcap 记录解析后的单条 URB 阶段事件"""

    ts: float
    irp_id: int
    status: int
    function: int
    info: int
    bus: int
    device: int
    endpoint: int
    transfer: int
    data_length: int
    payload: bytes
    stage: int | None = None
    iso_start_frame: int | None = None
    iso_error_count: int | None = None
    iso_packets: tuple[IsoPacket, ...] = ()
    truncated: bool = False
    offset: int = 0
    record_length: int = 0

    @property
    def is_completion(self) -> bool:
        """info bit0：0 = FDO→PDO（submit），1 = PDO→FDO（complete）"""
        return bool(self.info & 0x01)

    @property
    def direction(self) -> str:
        return "in" if self.endpoint & 0x80 else "out"

    @property
    def transfer_name(self) -> str:
        return TRANSFER_TYPE_NAMES.get(self.transfer, f"unknown({self.transfer})")

    @property
    def record_ref(self) -> RecordRef:
        return RecordRef(offset=self.offset, length=self.record_length, ts=self.ts)


@dataclass(slots=True)
class UsbTransfer:
    """一次 IRP 生命周期内的完整 USB 传输（尚未解码）"""

    id: int
    ts: float
    duration_us: int | None
    bus: int
    device: int
    endpoint: int
    direction: str
    transfer_type: int
    status: int
    function: int
    setup: bytes | None
    data: bytes
    iso_packets: tuple[IsoPacket, ...]
    records: tuple[RecordRef, ...]
    partial: bool = False

    @property
    def transfer_name(self) -> str:
        return TRANSFER_TYPE_NAMES.get(self.transfer_type, f"unknown({self.transfer_type})")


def _unpack_record_header(data: bytes, endian: str) -> tuple[int, int, int, int]:
    fmt = _RECORD_HEADER_LE if endian == "<" else _RECORD_HEADER_BE
    return fmt.unpack_from(data, 0)  # type: ignore[return-value]


def parse_record(record: bytes, *, ts: float, offset: int = 0) -> UsbPacket:
    """解析单条 USBPcap pcap 记录体（不含 pcap 记录头）。

    ``record`` 长度为 pcap 记录头中的 ``incl_len``。``offset`` 为该记录体在
    spool 文件中的绝对偏移，供详情回读定位。
    """
    if len(record) < BASE_HEADER_LEN:
        raise PcapFormatError(
            f"USBPcap 记录过短：{len(record)} < {BASE_HEADER_LEN}"
        )

    (
        header_len,
        irp_id,
        status,
        function,
        info,
        bus,
        device,
        endpoint,
        transfer,
        data_length,
    ) = _BASE_HEADER.unpack_from(record, 0)

    if header_len < BASE_HEADER_LEN:
        raise PcapFormatError(f"headerLen 非法：{header_len}")
    if header_len > len(record):
        raise PcapFormatError(
            f"headerLen 超出记录长度：{header_len} > {len(record)}"
        )

    stage: int | None = None
    iso_start_frame: int | None = None
    iso_error_count: int | None = None
    iso_packets: tuple[IsoPacket, ...] = ()

    if transfer == TRANSFER_CONTROL and header_len >= BASE_HEADER_LEN + 1:
        stage = record[BASE_HEADER_LEN]
    elif transfer == TRANSFER_ISOCHRONOUS and header_len >= ISO_HEADER_LEN:
        iso_start_frame, number_of_packets, iso_error_count = _ISO_EXTRA.unpack_from(
            record, BASE_HEADER_LEN
        )
        packet_bytes = number_of_packets * _ISO_PACKET.size
        if number_of_packets > 0 and BASE_HEADER_LEN + 12 + packet_bytes <= header_len:
            items = []
            pos = ISO_HEADER_LEN
            for _ in range(number_of_packets):
                poff, plen, pstatus = _ISO_PACKET.unpack_from(record, pos)
                items.append(IsoPacket(offset=poff, length=plen, status=pstatus))
                pos += _ISO_PACKET.size
            iso_packets = tuple(items)

    available = len(record) - header_len
    take = min(data_length, available)
    payload = record[header_len : header_len + take]
    truncated = data_length > available

    return UsbPacket(
        ts=ts,
        irp_id=irp_id,
        status=status,
        function=function,
        info=info,
        bus=bus,
        device=device,
        endpoint=endpoint,
        transfer=transfer,
        data_length=data_length,
        payload=payload,
        stage=stage,
        iso_start_frame=iso_start_frame,
        iso_error_count=iso_error_count,
        iso_packets=iso_packets,
        truncated=truncated,
        offset=offset,
        record_length=len(record),
    )


class PcapStreamReader:
    """增量读取 classic pcap 字节流并产出 ``UsbPacket``。

    支持微秒/纳秒时间戳与小端/大端文件；只接受 LINKTYPE_USBPCAP。
    内部维护缓冲区与绝对偏移，记录体偏移与同时落盘的 spool 文件严格一致。
    """

    def __init__(self, max_record_size: int = DEFAULT_MAX_RECORD_SIZE):
        self._buf = bytearray()
        self._abs = 0
        self._magic: int | None = None
        self._endian = "<"
        self._ns = False
        self._snaplen = 0
        self._linktype: int | None = None
        self._header_done = False
        self._max_record_size = max_record_size

    # ── 属性 ────────────────────────────────────────────────
    @property
    def total_bytes(self) -> int:
        """已喂入的 pcap 字节总数（用于离线进度）"""
        return self._abs + len(self._buf)

    @property
    def snaplen(self) -> int:
        return self._snaplen

    @property
    def linktype(self) -> int | None:
        return self._linktype

    @property
    def nanosecond_timestamps(self) -> bool:
        return self._ns

    # ── 解析 ────────────────────────────────────────────────
    def feed(self, data: bytes) -> list[UsbPacket]:
        """喂入一段字节，返回其中已完整解析的所有记录"""
        if not data:
            return []
        self._buf.extend(data)
        out: list[UsbPacket] = []
        pos = 0
        buf_len = len(self._buf)

        while True:
            if not self._header_done:
                if buf_len - pos < PCAP_GLOBAL_HEADER_LEN:
                    break
                self._parse_global_header(bytes(self._buf[pos : pos + PCAP_GLOBAL_HEADER_LEN]))
                pos += PCAP_GLOBAL_HEADER_LEN
                self._header_done = True
                continue

            if buf_len - pos < PCAP_RECORD_HEADER_LEN:
                break

            ts_sec, ts_frac, incl_len, _orig_len = _unpack_record_header(
                bytes(self._buf[pos : pos + PCAP_RECORD_HEADER_LEN]), self._endian
            )
            if incl_len > self._max_record_size:
                raise PcapFormatError(
                    f"记录长度异常：{incl_len} > {self._max_record_size}"
                )
            if buf_len - pos - PCAP_RECORD_HEADER_LEN < incl_len:
                break

            body_start = pos + PCAP_RECORD_HEADER_LEN
            record = bytes(self._buf[body_start : body_start + incl_len])
            abs_offset = self._abs + body_start
            ts = ts_sec + ts_frac / (1_000_000_000 if self._ns else 1_000_000)
            out.append(parse_record(record, ts=ts, offset=abs_offset))
            pos = body_start + incl_len

        if pos:
            del self._buf[:pos]
            self._abs += pos
        return out

    def _parse_global_header(self, header: bytes) -> None:
        magic_le = struct.unpack_from("<I", header, 0)[0]
        magic_be = struct.unpack_from(">I", header, 0)[0]
        if magic_le in (PCAP_MAGIC_USEC, PCAP_MAGIC_NSEC):
            self._endian = "<"
            magic = magic_le
        elif magic_be in (PCAP_MAGIC_USEC, PCAP_MAGIC_NSEC):
            self._endian = ">"
            magic = magic_be
        else:
            raise PcapFormatError(
                "不是有效的 pcap 文件（magic=0x%08X/0x%08X）" % (magic_le, magic_be)
            )

        self._magic = magic
        self._ns = magic == PCAP_MAGIC_NSEC
        _major, _minor, _zone, _sigfigs, snaplen, network = struct.unpack_from(
            self._endian + "HHIIII", header, 4
        )
        self._snaplen = snaplen
        self._linktype = network
        if network != LINKTYPE_USBPCAP:
            raise PcapFormatError(
                f"不支持的链路类型 {network}（仅支持 USBPcap={LINKTYPE_USBPCAP}）"
            )


@dataclass(slots=True)
class _Pending:
    key: int
    submit: UsbPacket
    setup: bytes | None
    data_out: bytearray = field(default_factory=bytearray)
    data_in: bytearray = field(default_factory=bytearray)
    iso_packets: tuple[IsoPacket, ...] = ()
    records: list[RecordRef] = field(default_factory=list)
    last_ts: float = 0.0


class IrpAssembler:
    """把 USBPcap 的 URB 阶段事件按 irpId 重组为 ``UsbTransfer``。

    兼容 USBPcap >= 1.5.0 的 SETUP/COMPLETE 两段模型，以及旧版的
    SETUP/DATA/STATUS 三段模型。
    """

    def __init__(self, max_pending: int = MAX_PENDING_IRPS):
        self._pending: dict[int, _Pending] = {}
        self._max_pending = max_pending
        self._synthetic_id = -1
        self._next_id = 1
        self.unmatched = 0
        # USBPcap 的 --inject-descriptors 会用 irpId=0；按
        # (bus, device, endpoint) 配对同一时刻的注入描述符请求。
        self._zero_pending: dict[tuple[int, int, int], list[int]] = {}

    def reset(self, *, keep_id: bool = False) -> None:
        self._pending.clear()
        self._zero_pending.clear()
        self.unmatched = 0
        if not keep_id:
            self._next_id = 1

    def flush(self) -> int:
        """丢弃未完成的 IRP，返回丢弃数量"""
        count = len(self._pending)
        self.unmatched += count
        self._pending.clear()
        self._zero_pending.clear()
        return count

    # ── 主流程 ──────────────────────────────────────────────
    def feed(self, packet: UsbPacket) -> list[UsbTransfer]:
        key = self._key_for(packet)
        out: list[UsbTransfer] = []

        if packet.transfer == TRANSFER_CONTROL:
            out.extend(self._feed_control(key, packet))
        else:
            out.extend(self._feed_simple(key, packet))
        return out

    def _key_for(self, packet: UsbPacket) -> int:
        if packet.irp_id:
            return packet.irp_id

        ident = (packet.bus, packet.device, packet.endpoint)
        # 控制传输的旧式 DATA 阶段仍属于已有 IRP，不能在这里出队
        is_control_data_completion = (
            packet.transfer == TRANSFER_CONTROL
            and packet.is_completion
            and packet.stage == CONTROL_STAGE_DATA
        )
        queue = self._zero_pending.setdefault(ident, [])
        if packet.is_completion and not is_control_data_completion and queue:
            return queue.pop()
        key = self._new_synthetic_key()
        if not packet.is_completion:
            queue.append(key)
        return key

    def _forget_zero_key(self, key: int) -> None:
        for ident, queue in list(self._zero_pending.items()):
            if key in queue:
                self._zero_pending[ident] = [item for item in queue if item != key]
                if not self._zero_pending[ident]:
                    self._zero_pending.pop(ident, None)

    def _new_synthetic_key(self) -> int:
        self._synthetic_id -= 1
        return self._synthetic_id

    def _feed_control(self, key: int, packet: UsbPacket) -> list[UsbTransfer]:
        out: list[UsbTransfer] = []
        completion = packet.is_completion
        stage = packet.stage

        if not completion:
            if stage == CONTROL_STAGE_DATA:
                pending = self._pending.get(key)
                if pending is not None:
                    pending.data_out.extend(packet.payload)
                    pending.records.append(packet.record_ref)
                    pending.last_ts = packet.ts
                    return out
            # 新的控制传输（SETUP 或旧格式 DATA 首包）
            old = self._pending.pop(key, None)
            if old is not None:
                self.unmatched += 1
                out.append(self._finalize(old, None, partial=True))
            setup: bytes | None = None
            data_out = b""
            if stage == CONTROL_STAGE_SETUP:
                setup = bytes(packet.payload[:8]) if len(packet.payload) >= 8 else None
                if len(packet.payload) > 8:
                    data_out = bytes(packet.payload[8:])
            elif stage == CONTROL_STAGE_DATA:
                data_out = bytes(packet.payload)
            self._start_pending(key, packet, setup, data_out)
            return out

        # completion
        pending = self._pending.get(key)
        if pending is None:
            self.unmatched += 1
            pending = _Pending(
                key=key,
                submit=packet,
                setup=None,
                last_ts=packet.ts,
            )
            self._pending[key] = pending

        direction = self._direction(pending, packet)

        if stage == CONTROL_STAGE_DATA:
            if direction == "in":
                pending.data_in.extend(packet.payload)
            else:
                pending.data_out.extend(packet.payload)
            pending.records.append(packet.record_ref)
            pending.last_ts = packet.ts
            return out

        if stage == CONTROL_STAGE_COMPLETE and direction == "in":
            pending.data_in.extend(packet.payload)
        elif stage == CONTROL_STAGE_COMPLETE and direction == "out":
            pending.data_out.extend(packet.payload)
        elif stage is None and direction == "in":
            pending.data_in.extend(packet.payload)
        pending.records.append(packet.record_ref)

        self._pending.pop(key, None)
        out.append(self._finalize(pending, packet, partial=False))
        return out

    def _feed_simple(self, key: int, packet: UsbPacket) -> list[UsbTransfer]:
        out: list[UsbTransfer] = []
        if not packet.is_completion:
            old = self._pending.pop(key, None)
            if old is not None:
                self.unmatched += 1
                out.append(self._finalize(old, None, partial=True))

            direction = packet.direction
            data_out = packet.payload if direction == "out" else b""
            pending = self._start_pending(key, packet, None, data_out)
            if packet.iso_packets:
                pending.iso_packets = packet.iso_packets
            return out

        pending = self._pending.pop(key, None)
        if pending is None:
            self.unmatched += 1
            pending = _Pending(
                key=key,
                submit=packet,
                setup=None,
                last_ts=packet.ts,
            )
        direction = self._direction(pending, packet)
        if direction == "in":
            pending.data_in.extend(packet.payload)
        elif packet.payload:
            pending.data_out.extend(packet.payload)
        if packet.iso_packets:
            pending.iso_packets = packet.iso_packets
        pending.records.append(packet.record_ref)
        out.append(self._finalize(pending, packet, partial=False))
        return out

    def _start_pending(
        self, key: int, packet: UsbPacket, setup: bytes | None, data_out: bytes
    ) -> _Pending:
        # 控制传输的旧格式 DATA 包可能没有 SETUP；尽量沿用已有 setup
        pending = _Pending(
            key=key,
            submit=packet,
            setup=setup,
            data_out=bytearray(data_out),
            records=[packet.record_ref],
            last_ts=packet.ts,
        )
        if packet.iso_packets:
            pending.iso_packets = packet.iso_packets
        self._pending[key] = pending

        if len(self._pending) > self._max_pending:
            oldest_key = next(iter(self._pending))
            self._pending.pop(oldest_key)
            self._forget_zero_key(oldest_key)
            self.unmatched += 1
        return pending

    @staticmethod
    def _direction(pending: _Pending, completion: UsbPacket) -> str:
        if pending.setup and len(pending.setup) >= 1:
            return "in" if pending.setup[0] & 0x80 else "out"
        endpoint = pending.submit.endpoint or completion.endpoint
        return "in" if endpoint & 0x80 else "out"

    def _finalize(
        self, pending: _Pending, completion: UsbPacket | None, *, partial: bool
    ) -> UsbTransfer:
        direction = self._direction(pending, completion or pending.submit)
        data = bytes(pending.data_in if direction == "in" else pending.data_out)
        if not data:
            # 控制传输缺少 SETUP 时，退化为按实际捕获到的数据方向判断
            if pending.data_in and not pending.data_out:
                direction, data = "in", bytes(pending.data_in)
            elif pending.data_out and not pending.data_in:
                direction, data = "out", bytes(pending.data_out)

        status = completion.status if completion is not None else pending.submit.status
        ts = pending.submit.ts
        duration_us: int | None = None
        if completion is not None:
            duration_us = max(0, int(round((completion.ts - ts) * 1_000_000)))
            ts = completion.ts

        transfer = UsbTransfer(
            id=self._next_id,
            ts=ts,
            duration_us=duration_us,
            bus=pending.submit.bus,
            device=pending.submit.device,
            endpoint=pending.submit.endpoint,
            direction=direction,
            transfer_type=pending.submit.transfer,
            status=status,
            function=pending.submit.function,
            setup=pending.setup,
            data=data,
            iso_packets=pending.iso_packets,
            records=tuple(pending.records),
            partial=partial,
        )
        self._next_id += 1
        self._forget_zero_key(pending.key)
        return transfer
