"""USB 描述符模型：解析、生成、校验、模板。

本模块是描述符的**唯一事实来源**：抓包解码（``usb_decode``）、CLI（``omni usb desc``）
与后续的请求/测试功能都从这里取数据，不允许再出现第二份解析实现。

设计要点：

- 解析结果是 dataclass，不是 dict；``usb_decode`` 负责把它转成 GUI 沿用的旧 dict 形状，
  这样抓包页的 JSON 契约保持不变。
- 描述符之间的**顺序**被保留：配置里的 class-specific 描述符（CDC functional、HID）
  和端点按原始顺序挂在接口的 ``entries`` 上，重新生成时能逐字节还原常见设备。
- 对外交换用 JSON 文档（``to_document`` / ``build_document``），字段名 camelCase，
  数值既接受 int 也接受 ``"0x..."`` 字符串，便于手写与 diff。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

# ── 描述符类型 ────────────────────────────────────────────
DT_DEVICE = 0x01
DT_CONFIGURATION = 0x02
DT_STRING = 0x03
DT_INTERFACE = 0x04
DT_ENDPOINT = 0x05
DT_DEVICE_QUALIFIER = 0x06
DT_OTHER_SPEED = 0x07
DT_INTERFACE_POWER = 0x08
DT_IAD = 0x0B
DT_BOS = 0x0F
DT_DEVICE_CAPABILITY = 0x10
DT_HID = 0x21
DT_HID_REPORT = 0x22
DT_CS_INTERFACE = 0x24
DT_CS_ENDPOINT = 0x25

#: 端点类型（bmAttributes 低 2 位）
TRANSFER_TYPES = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}

#: 各速度下端点最大包长的硬上限（字节）
MAX_PACKET_LIMITS = {
    "low": {0: 8, 1: 8, 2: 8, 3: 8},
    "full": {0: 64, 1: 1023, 2: 64, 3: 64},
    "high": {0: 64, 1: 1024, 2: 512, 3: 1024},
    "super": {0: 512, 1: 1024, 2: 1024, 3: 1024},
}


class DescriptorError(ValueError):
    """描述符字节流无法解析（长度不足、bLength 畸形等）。"""


# ── 解析结果 ──────────────────────────────────────────────

@dataclass
class EndpointDescriptor:
    address: int
    attributes: int
    max_packet_size: int
    interval: int
    raw: bytes = b""

    @property
    def number(self) -> int:
        return self.address & 0x0F

    @property
    def direction(self) -> str:
        return "in" if self.address & 0x80 else "out"

    @property
    def transfer_type(self) -> str:
        return TRANSFER_TYPES.get(self.attributes & 0x03, "control")

    def to_document(self) -> dict[str, Any]:
        return {
            "address": f"0x{self.address:02X}",
            "attributes": f"0x{self.attributes:02X}",
            "maxPacketSize": self.max_packet_size,
            "interval": self.interval,
        }


@dataclass
class ClassDescriptor:
    """类特定描述符（IAD / CDC functional / HID 等），保留原始字节。"""

    dtype: int
    raw: bytes
    kind: str
    subtype: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "type": f"0x{self.dtype:02X}",
            "hex": hex_string(self.raw),
        }
        if self.subtype is not None:
            doc["subtype"] = self.subtype
        if self.kind == "IAD":
            doc.update(self.fields)
        return doc


@dataclass
class InterfaceDescriptor:
    number: int
    alternate: int
    declared_endpoints: int
    klass: int
    subclass: int
    protocol: int
    i_interface: int
    entries: list[EndpointDescriptor | ClassDescriptor] = field(default_factory=list)
    raw: bytes = b""

    @property
    def endpoints(self) -> list[EndpointDescriptor]:
        return [item for item in self.entries if isinstance(item, EndpointDescriptor)]

    @property
    def class_descriptors(self) -> list[ClassDescriptor]:
        return [item for item in self.entries if isinstance(item, ClassDescriptor)]

    def to_document(self) -> dict[str, Any]:
        return {
            "alternate": self.alternate,
            "class": f"0x{self.klass:02X}",
            "subClass": self.subclass,
            "protocol": self.protocol,
            "iInterface": self.i_interface,
            "classDescriptors": [
                item.to_document() for item in self.class_descriptors
            ],
            "endpoints": [item.to_document() for item in self.endpoints],
        }


@dataclass
class ConfigurationDescriptor:
    total_length: int
    value: int
    i_configuration: int
    attributes: int
    max_power: int
    interfaces: list[InterfaceDescriptor] = field(default_factory=list)
    class_descriptors: list[ClassDescriptor] = field(default_factory=list)
    raw: bytes = b""
    #: 原始 bNumInterfaces；0 表示文档生成的对象（不做一致性校验）
    declared_num_interfaces: int = 0

    @property
    def num_interfaces(self) -> int:
        return len({item.number for item in self.interfaces})

    def interface_numbers(self) -> list[int]:
        return sorted({item.number for item in self.interfaces})

    def to_document(self) -> dict[str, Any]:
        grouped: dict[int, list[InterfaceDescriptor]] = {}
        for item in self.interfaces:
            grouped.setdefault(item.number, []).append(item)
        interfaces = []
        for number in sorted(grouped):
            interfaces.append(
                {
                    "number": number,
                    "alternates": [item.to_document() for item in grouped[number]],
                }
            )
        return {
            "value": self.value,
            "iConfiguration": self.i_configuration,
            "attributes": f"0x{self.attributes:02X}",
            "maxPower": self.max_power * 2,
            "classDescriptors": [
                item.to_document() for item in self.class_descriptors
            ],
            "interfaces": interfaces,
        }


@dataclass
class DeviceDescriptor:
    bcd_usb: int
    klass: int
    subclass: int
    protocol: int
    max_packet_size0: int
    vid: int
    pid: int
    bcd_device: int
    i_manufacturer: int
    i_product: int
    i_serial: int
    num_configurations: int
    raw: bytes = b""

    def to_document(self) -> dict[str, Any]:
        return {
            "bcdUSB": f"0x{self.bcd_usb:04X}",
            "class": self.klass,
            "subClass": self.subclass,
            "protocol": self.protocol,
            "maxPacketSize0": self.max_packet_size0,
            "vid": f"0x{self.vid:04X}",
            "pid": f"0x{self.pid:04X}",
            "bcdDevice": f"0x{self.bcd_device:04X}",
            "strings": {
                "manufacturer": self.i_manufacturer,
                "product": self.i_product,
                "serial": self.i_serial,
            },
        }


@dataclass
class BosDescriptor:
    total_length: int
    capabilities: list[ClassDescriptor] = field(default_factory=list)
    raw: bytes = b""

    def to_document(self) -> dict[str, Any]:
        return {
            "totalLength": self.total_length,
            "capabilities": [item.to_document() for item in self.capabilities],
        }


# ── 基础工具 ──────────────────────────────────────────────

def hex_string(data: bytes) -> str:
    """大写、空格分隔的十六进制（与抓包详情页一致）。"""
    return " ".join(f"{byte:02X}" for byte in data)


def parse_hex(text: str) -> bytes:
    """接受 ``12 01 00`` / ``12,01,00`` / ``0x12 0x01`` / ``120100``。"""
    cleaned = (
        text.replace("0x", " ")
        .replace("0X", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace(":", " ")
        .replace("-", " ")
    )
    out = bytearray()
    for token in cleaned.split():
        token = token.strip()
        if not token:
            continue
        if len(token) > 2:
            # 无分隔符的整串
            if len(token) % 2:
                raise DescriptorError(f"十六进制长度不是偶数：{token}")
            for index in range(0, len(token), 2):
                out.append(int(token[index : index + 2], 16))
        else:
            out.append(int(token, 16))
    return bytes(out)


def as_int(value: Any, default: int = 0) -> int:
    """把 ``"0x80"`` / ``"128"`` / ``128`` 统一成 int。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 16)


def bcd_text(value: int) -> str:
    """0x0210 -> ``2.10``（与抓包页展示一致）。"""
    return f"{value >> 8}.{(value >> 4) & 0x0F}{value & 0x0F}"


def descriptor_chain(data: bytes) -> list[tuple[int, int, bytes]]:
    """把描述符集合切成 ``(type, offset, raw)``，防御 bLength=0 与越界。"""
    out: list[tuple[int, int, bytes]] = []
    pos = 0
    while pos + 2 <= len(data):
        length = data[pos]
        if length < 2 or pos + length > len(data):
            break
        out.append((data[pos + 1], pos, data[pos : pos + length]))
        pos += length
    return out


# ── 解析 ──────────────────────────────────────────────────

def parse_device_descriptor(data: bytes) -> DeviceDescriptor:
    if len(data) < 18:
        raise DescriptorError(f"设备描述符长度不足（{len(data)} < 18）")
    (
        _length,
        dtype,
        bcd_usb,
        klass,
        subclass,
        protocol,
        max_packet0,
        vid,
        pid,
        bcd_device,
        i_manufacturer,
        i_product,
        i_serial,
        num_configs,
    ) = struct.unpack_from("<BBHBBBBHHHBBBB", data, 0)
    if dtype != DT_DEVICE:
        raise DescriptorError(f"不是设备描述符（bDescriptorType=0x{dtype:02X}）")
    return DeviceDescriptor(
        bcd_usb=bcd_usb,
        klass=klass,
        subclass=subclass,
        protocol=protocol,
        max_packet_size0=max_packet0,
        vid=vid,
        pid=pid,
        bcd_device=bcd_device,
        i_manufacturer=i_manufacturer,
        i_product=i_product,
        i_serial=i_serial,
        num_configurations=num_configs,
        raw=bytes(data[:18]),
    )


def _parse_class_descriptor(dtype: int, raw: bytes) -> ClassDescriptor:
    if dtype == DT_IAD and len(raw) >= 8:
        return ClassDescriptor(
            dtype=dtype,
            raw=bytes(raw),
            kind="IAD",
            fields={
                "firstInterface": raw[2],
                "interfaceCount": raw[3],
                "class": f"0x{raw[4]:02X}",
                "subClass": raw[5],
                "protocol": raw[6],
                "iFunction": raw[7],
            },
        )
    if dtype == DT_HID and len(raw) >= 9:
        bcd_hid = struct.unpack_from("<H", raw, 2)[0]
        return ClassDescriptor(
            dtype=dtype,
            raw=bytes(raw),
            kind="HID",
            fields={
                "bcdHID": f"0x{bcd_hid:04X}",
                "countryCode": raw[4],
                "numDescriptors": raw[5],
                "reportDescriptorType": raw[6],
                "reportDescriptorLength": struct.unpack_from("<H", raw, 7)[0],
            },
        )
    if dtype == DT_CS_INTERFACE:
        return ClassDescriptor(
            dtype=dtype,
            raw=bytes(raw),
            kind="CS_INTERFACE",
            subtype=raw[2] if len(raw) > 2 else None,
        )
    if dtype == DT_HID_REPORT:
        return ClassDescriptor(dtype=dtype, raw=bytes(raw), kind="HID_REPORT")
    return ClassDescriptor(dtype=dtype, raw=bytes(raw), kind="CS_ENDPOINT")


def parse_config_descriptor(data: bytes) -> ConfigurationDescriptor:
    """解析整段配置描述符（配置 + 接口 + 类描述符 + 端点）。"""
    if len(data) < 9:
        raise DescriptorError(f"配置描述符长度不足（{len(data)} < 9）")
    (
        _length,
        dtype,
        total_length,
        num_interfaces,
        config_value,
        i_config,
        attributes,
        max_power,
    ) = struct.unpack_from("<BBHBBBBB", data, 0)
    if dtype != DT_CONFIGURATION:
        raise DescriptorError(f"不是配置描述符（bDescriptorType=0x{dtype:02X}）")

    result = ConfigurationDescriptor(
        total_length=total_length,
        value=config_value,
        i_configuration=i_config,
        attributes=attributes,
        max_power=max_power,
        raw=bytes(data),
        declared_num_interfaces=num_interfaces,
    )

    current: InterfaceDescriptor | None = None
    for item_type, _offset, raw in descriptor_chain(data[9:]):
        if item_type == DT_INTERFACE and len(raw) >= 9:
            (
                _length,
                _dtype,
                number,
                alternate,
                declared,
                klass,
                subclass,
                protocol,
                i_interface,
            ) = struct.unpack_from("<BBBBBBBBB", raw, 0)
            current = InterfaceDescriptor(
                number=number,
                alternate=alternate,
                declared_endpoints=declared,
                klass=klass,
                subclass=subclass,
                protocol=protocol,
                i_interface=i_interface,
                raw=bytes(raw),
            )
            result.interfaces.append(current)
        elif item_type == DT_ENDPOINT and len(raw) >= 7:
            _length, _dtype, address, attrs, max_packet, interval = struct.unpack_from(
                "<BBBBHB", raw, 0
            )
            endpoint = EndpointDescriptor(
                address=address,
                attributes=attrs,
                max_packet_size=max_packet & 0x7FF,
                interval=interval,
                raw=bytes(raw),
            )
            if current is not None:
                current.entries.append(endpoint)
        else:
            parsed = _parse_class_descriptor(item_type, raw)
            if current is not None:
                current.entries.append(parsed)
            else:
                result.class_descriptors.append(parsed)

    return result


def parse_string_descriptor(data: bytes) -> str:
    if len(data) < 2:
        raise DescriptorError("字符串描述符长度不足")
    length = min(data[0], len(data))
    body = data[2:length]
    if len(body) % 2:
        body = body[:-1]
    return body.decode("utf-16-le", errors="replace").rstrip("\x00")


def parse_device_qualifier(data: bytes) -> dict[str, Any]:
    if len(data) < 10:
        raise DescriptorError("设备限定描述符长度不足")
    (
        _length,
        _dtype,
        bcd_usb,
        klass,
        subclass,
        protocol,
        max_packet0,
        num_configs,
        _reserved,
    ) = struct.unpack_from("<BBHBBBBBB", data, 0)
    return {
        "bcdUSB": bcd_usb,
        "class": klass,
        "subClass": subclass,
        "protocol": protocol,
        "maxPacketSize0": max_packet0,
        "numConfigurations": num_configs,
    }


def parse_bos_descriptor(data: bytes) -> BosDescriptor:
    if len(data) < 5:
        raise DescriptorError("BOS 描述符长度不足")
    _length, dtype, total_length, _num_caps = struct.unpack_from("<BBHB", data, 0)
    if dtype != DT_BOS:
        raise DescriptorError(f"不是 BOS 描述符（bDescriptorType=0x{dtype:02X}）")
    caps: list[ClassDescriptor] = []
    pos = 5
    while pos + 3 <= len(data):
        length = data[pos]
        if length < 3 or pos + length > len(data):
            break
        caps.append(_parse_class_descriptor(data[pos + 1], data[pos : pos + length]))
        pos += length
    return BosDescriptor(total_length=total_length, capabilities=caps, raw=bytes(data))


def config_total_length(data: bytes) -> int | None:
    if len(data) < 4:
        return None
    return struct.unpack_from("<H", data, 2)[0]


# ── 文档（JSON 中间格式） ─────────────────────────────────

DOCUMENT_VERSION = 1


def to_document(
    device: DeviceDescriptor | None = None,
    configurations: Iterable[ConfigurationDescriptor] = (),
    strings: dict[int, dict[int, str]] | None = None,
    bos: BosDescriptor | None = None,
) -> dict[str, Any]:
    """把解析结果整理成可编辑、可 diff 的 JSON 文档。"""
    doc: dict[str, Any] = {"version": DOCUMENT_VERSION}
    if device is not None:
        doc["device"] = device.to_document()
    doc["configurations"] = [item.to_document() for item in configurations]

    string_table: dict[str, dict[str, str]] = {}
    for index, by_lang in sorted((strings or {}).items()):
        for lang_id, text in sorted(by_lang.items()):
            string_table.setdefault(f"0x{lang_id:04X}", {})[str(index)] = text
    if string_table:
        doc["strings"] = string_table
    if bos is not None:
        doc["bos"] = bos.to_document()
    return doc


def _parse_endpoint_document(doc: dict[str, Any]) -> EndpointDescriptor:
    return EndpointDescriptor(
        address=as_int(doc.get("address")),
        attributes=as_int(doc.get("attributes")),
        max_packet_size=as_int(doc.get("maxPacketSize")),
        interval=as_int(doc.get("interval")),
    )


def _parse_class_document(doc: dict[str, Any]) -> ClassDescriptor:
    dtype = as_int(doc.get("type", doc.get("descriptorType", 0)))
    raw = b""
    if doc.get("hex"):
        raw = parse_hex(str(doc["hex"]))
    elif dtype == DT_IAD:
        raw = struct.pack(
            "<BBBBBBBB",
            8,
            DT_IAD,
            as_int(doc.get("firstInterface")),
            as_int(doc.get("interfaceCount")),
            as_int(doc.get("class")),
            as_int(doc.get("subClass")),
            as_int(doc.get("protocol")),
            as_int(doc.get("iFunction")),
        )
    kind = {DT_IAD: "IAD", DT_HID: "HID", DT_CS_INTERFACE: "CS_INTERFACE"}.get(
        dtype, "CS_ENDPOINT"
    )
    return _parse_class_descriptor(dtype, raw) if raw else ClassDescriptor(
        dtype=dtype, raw=b"", kind=kind
    )


def document_to_objects(
    document: dict[str, Any],
) -> tuple[
    DeviceDescriptor | None,
    list[ConfigurationDescriptor],
    dict[int, dict[int, str]],
    BosDescriptor | None,
]:
    """把 JSON 文档还原成模型对象（供校验与生成共用）。"""
    device_doc = document.get("device")
    device = None
    if device_doc:
        device = DeviceDescriptor(
            bcd_usb=as_int(device_doc.get("bcdUSB")),
            klass=as_int(device_doc.get("class")),
            subclass=as_int(device_doc.get("subClass")),
            protocol=as_int(device_doc.get("protocol")),
            max_packet_size0=as_int(device_doc.get("maxPacketSize0"), 64),
            vid=as_int(device_doc.get("vid")),
            pid=as_int(device_doc.get("pid")),
            bcd_device=as_int(device_doc.get("bcdDevice")),
            i_manufacturer=as_int(
                (device_doc.get("strings") or {}).get("manufacturer")
            ),
            i_product=as_int((device_doc.get("strings") or {}).get("product")),
            i_serial=as_int((device_doc.get("strings") or {}).get("serial")),
            num_configurations=as_int(device_doc.get("numConfigurations"))
            or len(document.get("configurations") or []),
        )

    configurations: list[ConfigurationDescriptor] = []
    for config_doc in document.get("configurations") or []:
        config = ConfigurationDescriptor(
            total_length=0,
            value=as_int(config_doc.get("value"), 1),
            i_configuration=as_int(config_doc.get("iConfiguration")),
            attributes=as_int(config_doc.get("attributes"), 0x80),
            max_power=as_int(config_doc.get("maxPower")) // 2,
            class_descriptors=[
                _parse_class_document(item)
                for item in config_doc.get("classDescriptors") or []
            ],
        )
        for interface_doc in config_doc.get("interfaces") or []:
            number = as_int(interface_doc.get("number"))
            for alt_doc in interface_doc.get("alternates") or [
                {"alternate": 0}
            ]:
                interface = InterfaceDescriptor(
                    number=number,
                    alternate=as_int(alt_doc.get("alternate")),
                    declared_endpoints=0,
                    klass=as_int(alt_doc.get("class")),
                    subclass=as_int(alt_doc.get("subClass")),
                    protocol=as_int(alt_doc.get("protocol")),
                    i_interface=as_int(alt_doc.get("iInterface")),
                )
                for item in alt_doc.get("classDescriptors") or []:
                    interface.entries.append(_parse_class_document(item))
                for ep_doc in alt_doc.get("endpoints") or []:
                    interface.entries.append(_parse_endpoint_document(ep_doc))
                interface.declared_endpoints = len(
                    [item for item in interface.entries if isinstance(item, EndpointDescriptor)]
                )
                config.interfaces.append(interface)
        configurations.append(config)

    strings: dict[int, dict[int, str]] = {}
    for lang_key, table in (document.get("strings") or {}).items():
        lang_id = as_int(lang_key)
        for index_key, text in table.items():
            strings.setdefault(int(index_key), {})[lang_id] = str(text)

    bos_doc = document.get("bos")
    bos = None
    if bos_doc:
        raw = b""
        if bos_doc.get("hex"):
            raw = parse_hex(str(bos_doc["hex"]))
        bos = BosDescriptor(
            total_length=as_int(bos_doc.get("totalLength")),
            capabilities=[
                _parse_class_document(item)
                for item in bos_doc.get("capabilities") or []
            ],
            raw=raw,
        )
    return device, configurations, strings, bos


# ── 生成 ──────────────────────────────────────────────────

def encode_device(device: DeviceDescriptor) -> bytes:
    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        DT_DEVICE,
        device.bcd_usb,
        device.klass,
        device.subclass,
        device.protocol,
        device.max_packet_size0,
        device.vid,
        device.pid,
        device.bcd_device,
        device.i_manufacturer,
        device.i_product,
        device.i_serial,
        device.num_configurations,
    )


def _encode_interface(interface: InterfaceDescriptor) -> bytes:
    endpoints = [item for item in interface.entries if isinstance(item, EndpointDescriptor)]
    out = bytearray(
        struct.pack(
            "<BBBBBBBBB",
            9,
            DT_INTERFACE,
            interface.number,
            interface.alternate,
            len(endpoints),
            interface.klass,
            interface.subclass,
            interface.protocol,
            interface.i_interface,
        )
    )
    for item in interface.entries:
        if isinstance(item, EndpointDescriptor):
            out += struct.pack(
                "<BBBBHB",
                7,
                DT_ENDPOINT,
                item.address,
                item.attributes,
                item.max_packet_size,
                item.interval,
            )
        elif item.raw:
            out += item.raw
    return bytes(out)


def encode_configuration(config: ConfigurationDescriptor) -> bytes:
    body = bytearray()
    for item in config.class_descriptors:
        if item.raw:
            body += item.raw
    for interface in config.interfaces:
        body += _encode_interface(interface)
    total_length = 9 + len(body)
    header = struct.pack(
        "<BBHBBBBB",
        9,
        DT_CONFIGURATION,
        total_length,
        config.num_interfaces,
        config.value,
        config.i_configuration,
        config.attributes,
        config.max_power,
    )
    return header + bytes(body)


def encode_string(index: int, text: str) -> bytes:
    body = text.encode("utf-16-le")
    length = 2 + len(body)
    if length > 255:
        raise DescriptorError(f"字符串 {index} 过长（{length} > 255 字节）")
    return struct.pack("<BB", length, DT_STRING) + body


def encode_bos(bos: BosDescriptor) -> bytes:
    body = bytearray()
    for item in bos.capabilities:
        if item.raw:
            body += item.raw
    total_length = 5 + len(body)
    return struct.pack("<BBHB", 5, DT_BOS, total_length, len(bos.capabilities)) + bytes(body)


def build_document(document: dict[str, Any]) -> dict[str, Any]:
    """由 JSON 文档生成 hex 分节结果（不写文件，由调用方决定落盘）。"""
    device, configurations, strings, bos = document_to_objects(document)
    result: dict[str, Any] = {
        "device": None,
        "configurations": [],
        "strings": [],
        "bos": None,
        "warnings": [],
    }
    if device is not None:
        if not device.num_configurations:
            device.num_configurations = len(configurations)
        result["device"] = hex_string(encode_device(device))
    for config in configurations:
        encoded = encode_configuration(config)
        result["configurations"].append(
            {
                "value": config.value,
                "totalLength": len(encoded),
                "hex": hex_string(encoded),
            }
        )
    for index in sorted(strings):
        for lang_id in sorted(strings[index]):
            result["strings"].append(
                {
                    "index": index,
                    "langId": f"0x{lang_id:04X}",
                    "hex": hex_string(encode_string(index, strings[index][lang_id])),
                }
            )
    if bos is not None:
        if bos.capabilities:
            result["bos"] = hex_string(encode_bos(bos))
        else:
            result["warnings"].append("bos 没有任何 capability，已跳过生成")
    return result


# ── 校验 ──────────────────────────────────────────────────

@dataclass
class Issue:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _issue(code: str, path: str, message: str) -> Issue:
    return Issue(code=code, path=path, message=message)


def validate_document(
    document: dict[str, Any], *, speed: str = "full"
) -> dict[str, Any]:
    """按 USB 2.0 的常见约束校验文档，返回 ``{ok, errors, warnings}``。"""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    device, configurations, strings, bos = document_to_objects(document)

    if device is None:
        errors.append(_issue("device.missing", "device", "缺少设备描述符"))
    else:
        if not 0 <= device.vid <= 0xFFFF or not 0 <= device.pid <= 0xFFFF:
            errors.append(_issue("device.ids", "device", "VID/PID 必须在 0..0xFFFF"))
        if device.max_packet_size0 not in (8, 16, 32, 64):
            errors.append(
                _issue(
                    "device.maxPacketSize0",
                    "device.maxPacketSize0",
                    f"bMaxPacketSize0={device.max_packet_size0}，合法值为 8/16/32/64",
                )
            )
        if not device.num_configurations:
            errors.append(
                _issue("device.numConfigurations", "device", "至少要有 1 个配置")
            )
        if device.bcd_usb < 0x0110:
            warnings.append(
                _issue("device.bcdUSB", "device.bcdUSB", "bcdUSB 低于 1.10，主机可能不识别")
            )
        for label, index in (
            ("manufacturer", device.i_manufacturer),
            ("product", device.i_product),
            ("serial", device.i_serial),
        ):
            if index and index not in strings:
                errors.append(
                    _issue(
                        f"string.missing.{label}",
                        f"device.strings.{label}",
                        f"字符串索引 {index} 未在 strings 中定义",
                    )
                )

    if not configurations:
        errors.append(_issue("configuration.missing", "configurations", "至少要有 1 个配置"))

    seen_config_values: set[int] = set()
    for ci, config in enumerate(configurations):
        path = f"configurations[{ci}]"
        if config.value <= 0:
            errors.append(_issue("config.value", f"{path}.value", "bConfigurationValue 必须 > 0"))
        if config.value in seen_config_values:
            errors.append(
                _issue("config.value.duplicate", f"{path}.value", f"配置值 {config.value} 重复")
            )
        seen_config_values.add(config.value)

        numbers = config.interface_numbers()
        if numbers != list(range(len(numbers))):
            errors.append(
                _issue(
                    "interface.number.contiguous",
                    f"{path}.interfaces",
                    f"接口号必须从 0 连续：当前 {numbers}",
                )
            )
        declared = config.declared_num_interfaces
        if declared and declared != len(numbers):
            errors.append(
                _issue(
                    "config.numInterfaces",
                    f"{path}",
                    f"bNumInterfaces={declared}，实际接口数={len(numbers)}",
                )
            )

        # 每个接口的 alt 必须从 0 连续
        for number in numbers:
            alts = sorted(
                item.alternate for item in config.interfaces if item.number == number
            )
            if alts != list(range(len(alts))):
                errors.append(
                    _issue(
                        "interface.alternate.contiguous",
                        f"{path}.interfaces[{number}]",
                        f"接口 {number} 的 alternate 必须从 0 连续：当前 {alts}",
                    )
                )

        # 端点：同一 alt 内地址唯一；速度上限；地址范围
        used_addresses: dict[tuple[int, int], int] = {}
        for interface in config.interfaces:
            ipath = f"{path}.interfaces[{interface.number}].alternates[{interface.alternate}]"
            for endpoint in interface.endpoints:
                epath = f"{ipath}.endpoints[0x{endpoint.address:02X}]"
                if not 1 <= endpoint.number <= 15:
                    errors.append(
                        _issue("endpoint.address", epath, f"端点号 {endpoint.number} 超出 1..15")
                    )
                key = (interface.number, interface.alternate, endpoint.address)
                if key in used_addresses:
                    errors.append(
                        _issue(
                            "endpoint.address.duplicate",
                            epath,
                            f"接口 {interface.number} 内端点 0x{endpoint.address:02X} 重复",
                        )
                    )
                used_addresses[key] = endpoint.address
                limit = MAX_PACKET_LIMITS.get(speed, MAX_PACKET_LIMITS["full"])[
                    endpoint.attributes & 0x03
                ]
                if endpoint.max_packet_size > limit:
                    errors.append(
                        _issue(
                            "endpoint.maxPacketSize",
                            f"{epath}.maxPacketSize",
                            f"{endpoint.transfer_type} 端点在 {speed} 下最大 {limit} 字节，"
                            f"当前 {endpoint.max_packet_size}",
                        )
                    )
                if endpoint.max_packet_size == 0:
                    errors.append(
                        _issue("endpoint.maxPacketSize.zero", epath, "wMaxPacketSize 不能为 0")
                    )

        # IAD 覆盖的接口必须存在
        for item in config.class_descriptors:
            if item.kind != "IAD":
                continue
            first = int(item.fields.get("firstInterface", 0))
            count = int(item.fields.get("interfaceCount", 0))
            missing = [
                number
                for number in range(first, first + max(count, 0))
                if number not in numbers
            ]
            if count <= 0 or missing:
                errors.append(
                    _issue(
                        "iad.range",
                        f"{path}.classDescriptors",
                        f"IAD 覆盖接口 {first}..{first + count - 1}，缺少 {missing or '有效范围'}",
                    )
                )

        # 字符串引用
        if config.i_configuration and config.i_configuration not in strings:
            errors.append(
                _issue(
                    "string.missing.configuration",
                    f"{path}.iConfiguration",
                    f"配置字符串索引 {config.i_configuration} 未定义",
                )
            )
        for interface in config.interfaces:
            if interface.i_interface and interface.i_interface not in strings:
                errors.append(
                    _issue(
                        "string.missing.interface",
                        f"{path}.interfaces[{interface.number}].iInterface",
                        f"接口字符串索引 {interface.i_interface} 未定义",
                    )
                )

        if config.max_power * 2 > 500:
            warnings.append(
                _issue(
                    "config.maxPower",
                    f"{path}.maxPower",
                    f"bMaxPower={config.max_power * 2} mA，超过 500 mA 需要自供电",
                )
            )

    if bos is not None:
        if device is not None and device.bcd_usb < 0x0200:
            warnings.append(
                _issue(
                    "bos.bcdUSB",
                    "bos",
                    "BOS 需要 bcdUSB >= 0x0200，当前设备声明更低版本",
                )
            )
    elif device is not None and device.bcd_usb >= 0x0201:
        warnings.append(
            _issue("bos.missing", "bos", "bcdUSB >= 0x0201 但未提供 BOS 描述符")
        )

    return {
        "ok": not errors,
        "errors": [item.to_dict() for item in errors],
        "warnings": [item.to_dict() for item in warnings],
    }


# ── 模板 ──────────────────────────────────────────────────

def _winusb_template() -> dict[str, Any]:
    return {
        "version": DOCUMENT_VERSION,
        "device": {
            "bcdUSB": "0x0210",
            "class": 0,
            "maxPacketSize0": 64,
            "vid": "0xC251",
            "pid": "0x0002",
            "bcdDevice": "0x0100",
            "strings": {"manufacturer": 1, "product": 2, "serial": 3},
        },
        "strings": {
            "0x0409": {"1": "Geehy", "2": "USBD WinUSB Echo", "3": "0123456789AB"}
        },
        "configurations": [
            {
                "value": 1,
                "attributes": "0x80",
                "maxPower": 100,
                "interfaces": [
                    {
                        "number": 0,
                        "alternates": [
                            {
                                "alternate": 0,
                                "class": "0xFF",
                                "subClass": 0,
                                "protocol": 0,
                                "endpoints": [
                                    {
                                        "address": "0x01",
                                        "attributes": "0x02",
                                        "maxPacketSize": 64,
                                        "interval": 0,
                                    },
                                    {
                                        "address": "0x81",
                                        "attributes": "0x02",
                                        "maxPacketSize": 64,
                                        "interval": 0,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _cdc_acm_template() -> dict[str, Any]:
    return {
        "version": DOCUMENT_VERSION,
        "device": {
            "bcdUSB": "0x0200",
            "class": "0xEF",
            "subClass": 0x02,
            "protocol": 0x01,
            "maxPacketSize0": 64,
            "vid": "0xC251",
            "pid": "0x0001",
            "bcdDevice": "0x0100",
            "strings": {"manufacturer": 1, "product": 2, "serial": 3},
        },
        "strings": {
            "0x0409": {"1": "Geehy", "2": "USBD CDC ACM", "3": "0123456789AB"}
        },
        "configurations": [
            {
                "value": 1,
                "attributes": "0x80",
                "maxPower": 100,
                "classDescriptors": [
                    {
                        "type": "0x0B",
                        "firstInterface": 0,
                        "interfaceCount": 2,
                        "class": "0x02",
                        "subClass": 0x02,
                        "protocol": 0x01,
                        "iFunction": 0,
                    }
                ],
                "interfaces": [
                    {
                        "number": 0,
                        "alternates": [
                            {
                                "alternate": 0,
                                "class": "0x02",
                                "subClass": 0x02,
                                "protocol": 0x01,
                                "classDescriptors": [
                                    {"type": "0x24", "subtype": 0, "hex": "05 24 00 10 01"},
                                    {"type": "0x24", "subtype": 1, "hex": "05 24 01 00 01"},
                                    {"type": "0x24", "subtype": 2, "hex": "04 24 02 06"},
                                    {"type": "0x24", "subtype": 6, "hex": "05 24 06 00 01"},
                                ],
                                "endpoints": [
                                    {
                                        "address": "0x82",
                                        "attributes": "0x03",
                                        "maxPacketSize": 8,
                                        "interval": 16,
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "number": 1,
                        "alternates": [
                            {
                                "alternate": 0,
                                "class": "0x0A",
                                "subClass": 0,
                                "protocol": 0,
                                "endpoints": [
                                    {
                                        "address": "0x01",
                                        "attributes": "0x02",
                                        "maxPacketSize": 64,
                                        "interval": 0,
                                    },
                                    {
                                        "address": "0x81",
                                        "attributes": "0x02",
                                        "maxPacketSize": 64,
                                        "interval": 0,
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }


def _device_only_template() -> dict[str, Any]:
    return {
        "version": DOCUMENT_VERSION,
        "device": {
            "bcdUSB": "0x0200",
            "class": 0,
            "maxPacketSize0": 64,
            "vid": "0x0000",
            "pid": "0x0000",
            "bcdDevice": "0x0100",
        },
        "configurations": [],
    }


_TEMPLATES = {
    "device": _device_only_template,
    "winusb": _winusb_template,
    "cdc-acm": _cdc_acm_template,
}

_TEMPLATE_INFO = {
    "device": "空白设备骨架（无配置，仅用于从零开始）",
    "winusb": "单接口 WinUSB 回环（0xC251:0x0002，2 个批量端点）",
    "cdc-acm": "CDC ACM 复合设备（IAD + 控制/数据两接口）",
}


def template_names() -> list[str]:
    return sorted(_TEMPLATES)


def templates() -> list[dict[str, str]]:
    """模板清单（名称 + 用途），供 CLI / API 展示。"""
    return [
        {"name": name, "description": _TEMPLATE_INFO.get(name, "")}
        for name in template_names()
    ]


def get_template(name: str) -> dict[str, Any]:
    try:
        return _TEMPLATES[name]()
    except KeyError:
        raise DescriptorError(
            f"未知模板 {name}；可用：{', '.join(template_names())}"
        ) from None


# ── 自动识别 ──────────────────────────────────────────────

def looks_like_ms_os_20_set(data: bytes) -> bool:
    """MS OS 2.0 描述符集：首个子描述符是 Set Header（wLength=10，type=0）"""
    if len(data) < 10:
        return False
    w_length, w_type = struct.unpack_from("<HH", data, 0)
    if w_length != 10 or w_type != 0:
        return False
    total = struct.unpack_from("<H", data, 8)[0]
    return total == len(data)


def _msos_ascii(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def _msos_utf16(raw: bytes) -> str:
    return raw.decode("utf-16-le", "replace").split("\x00", 1)[0]


def parse_ms_os_20_set(data: bytes) -> dict[str, Any]:
    """解析 MS OS 2.0 描述符集（Set/Config/Function/Compatible ID/Registry）"""
    _w_length, _w_type, win_version, total = struct.unpack_from("<HHIH", data, 0)
    result: dict[str, Any] = {
        "windowsVersion": f"0x{win_version:08X}",
        "totalLength": total,
        "configSubsets": [],
        "functionSubsets": [],
        "compatibleIds": [],
        "registryProperties": [],
        "unknown": [],
    }
    pos = 10
    while pos + 4 <= len(data):
        sub_len, sub_type = struct.unpack_from("<HH", data, pos)
        if sub_len < 4 or pos + sub_len > len(data):
            break
        body = data[pos : pos + sub_len]
        if sub_type == 0x01 and sub_len >= 8:
            result["configSubsets"].append(
                {
                    "bConfigurationValue": body[4],
                    "totalLength": struct.unpack_from("<H", body, 6)[0],
                }
            )
        elif sub_type == 0x02 and sub_len >= 8:
            result["functionSubsets"].append(
                {
                    "bFirstInterface": body[4],
                    "subsetLength": struct.unpack_from("<H", body, 6)[0],
                }
            )
        elif sub_type == 0x03 and sub_len >= 20:
            result["compatibleIds"].append(
                {
                    "compatibleId": _msos_ascii(body[4:12]),
                    "subCompatibleId": _msos_ascii(body[12:20]),
                }
            )
        elif sub_type == 0x04 and sub_len >= 10:
            prop_type, name_len = struct.unpack_from("<HH", body, 4)
            name_end = 8 + name_len
            name = _msos_utf16(body[8:name_end]) if name_end <= sub_len else ""
            data_len = (
                struct.unpack_from("<H", body, name_end)[0]
                if name_end + 2 <= sub_len
                else 0
            )
            payload = body[name_end + 2 : name_end + 2 + data_len]
            if prop_type in (0x01, 0x02, 0x07):
                value: Any = _msos_utf16(payload)
            elif prop_type == 0x04 and len(payload) >= 4:
                value = struct.unpack_from("<I", payload, 0)[0]
            else:
                value = hex_string(payload)
            result["registryProperties"].append(
                {"dataType": prop_type, "name": name, "value": value}
            )
        else:
            result["unknown"].append({"type": sub_type, "hex": hex_string(body)})
        pos += sub_len
    return result


def detect(data: bytes) -> dict[str, Any]:
    """识别一段原始字节是什么，并返回解析结果。

    返回 ``{kind, length, document?, extra?}``，其中 kind 为：

    - ``device`` / ``configuration`` / ``string`` / ``qualifier`` / ``bos``
    - ``setup``：8 字节 Setup 事务包（解析交给 ``usb_decode.decode_setup``）
    - ``raw``：无法识别，原样返回
    """
    if not data:
        raise DescriptorError("输入为空")
    length = len(data)
    dtype = data[1] if length >= 2 else None

    if dtype == DT_DEVICE and length >= 18:
        device = parse_device_descriptor(data)
        return {
            "kind": "device",
            "length": length,
            "document": to_document(device),
        }
    if dtype == DT_CONFIGURATION and length >= 9:
        config = parse_config_descriptor(data)
        return {
            "kind": "configuration",
            "length": length,
            "document": to_document(configurations=[config]),
        }
    if dtype == DT_STRING and 2 <= length <= 255:
        return {
            "kind": "string",
            "length": length,
            "text": parse_string_descriptor(data),
        }
    if dtype == DT_DEVICE_QUALIFIER and length >= 10:
        return {"kind": "qualifier", "length": length, "fields": parse_device_qualifier(data)}
    if dtype == DT_BOS:
        bos = parse_bos_descriptor(data)
        return {
            "kind": "bos",
            "length": length,
            "document": to_document(bos=bos),
        }
    if looks_like_ms_os_20_set(data):
        return {"kind": "msos20", "length": length, "msos20": parse_ms_os_20_set(data)}
    if length == 8 and (data[0] & 0x60) != 0x60:
        return {"kind": "setup", "length": length}
    return {"kind": "raw", "length": length, "hex": hex_string(data)}


def normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    """把文档过一遍模型再输出：补齐默认值、统一 ``0x`` 写法，可安全回写。"""
    device, configurations, strings, bos = document_to_objects(document)
    return to_document(device, configurations, strings, bos)
