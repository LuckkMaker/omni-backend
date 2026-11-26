"""USB 协议解码与描述符收集

本模块只做纯逻辑（不依赖 FastAPI / 文件系统），便于单元测试：
  - SETUP 包、标准/类请求、URB 状态名
  - 描述符解析**委托** ``core/usb_desc.py``（唯一实现），本节只负责把模型
    转成抓包页沿用的 dict 形状
  - CDC ACM、HID、MSC、DFU 的基础类解码
  - ``DescriptorCollector``: 从控制传输流中重建设备描述符树
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from core import usb_desc

# ── 常量表 ────────────────────────────────────────────────

TRANSFER_TYPE_LABELS = {
    0: "isochronous",
    1: "interrupt",
    2: "control",
    3: "bulk",
    0xFE: "urb",
}

STANDARD_REQUESTS = {
    0: "GET_STATUS",
    1: "CLEAR_FEATURE",
    3: "SET_FEATURE",
    5: "SET_ADDRESS",
    6: "GET_DESCRIPTOR",
    7: "SET_DESCRIPTOR",
    8: "GET_CONFIGURATION",
    9: "SET_CONFIGURATION",
    10: "GET_INTERFACE",
    11: "SET_INTERFACE",
    12: "SYNCH_FRAME",
}

DESCRIPTOR_TYPES = {
    1: "DEVICE",
    2: "CONFIGURATION",
    3: "STRING",
    4: "INTERFACE",
    5: "ENDPOINT",
    6: "DEVICE_QUALIFIER",
    7: "OTHER_SPEED_CONFIGURATION",
    8: "INTERFACE_POWER",
    9: "OTG",
    0x0B: "INTERFACE_ASSOCIATION",
    0x0F: "BOS",
    0x10: "DEVICE_CAPABILITY",
    0x21: "HID",
    0x22: "HID_REPORT",
    0x24: "CS_INTERFACE",
    0x25: "CS_ENDPOINT",
}

DEVICE_CLASSES = {
    0x00: "Per-Interface",
    0x01: "Audio",
    0x02: "CDC",
    0x03: "HID",
    0x05: "Physical",
    0x06: "Image",
    0x07: "Printer",
    0x08: "Mass Storage",
    0x09: "Hub",
    0x0A: "CDC-Data",
    0x0B: "Smart Card",
    0x0D: "Content Security",
    0x0E: "Video",
    0x0F: "Personal Healthcare",
    0x10: "Audio/Video",
    0x11: "Billboard",
    0x12: "USB Type-C Bridge",
    0xDC: "Diagnostic",
    0xE0: "Wireless",
    0xEF: "Miscellaneous",
    0xFE: "Application Specific",
    0xFF: "Vendor Specific",
}

#: Hub class SetPortFeature/ClearPortFeature 选择子（USB 2.0 hub 规范）
HUB_FEATURE_NAMES = {
    0: "PORT_CONNECTION",
    1: "PORT_ENABLE",
    2: "PORT_SUSPEND",
    3: "PORT_OVER_CURRENT",
    4: "PORT_RESET",
    8: "PORT_POWER",
    9: "PORT_LOW_SPEED",
    16: "C_PORT_CONNECTION",
    17: "C_PORT_ENABLE",
    18: "C_PORT_SUSPEND",
    19: "C_PORT_OVER_CURRENT",
    20: "C_PORT_RESET",
    21: "C_PORT_POWER",
}

#: Hub class 请求码
HUB_REQUESTS = {
    0: "GET_STATUS",
    1: "CLEAR_FEATURE",
    3: "SET_FEATURE",
    6: "GET_DESCRIPTOR",
    7: "SET_DESCRIPTOR",
}

CDC_REQUESTS = {
    0x00: "SEND_ENCAPSULATED_COMMAND",
    0x01: "GET_ENCAPSULATED_RESPONSE",
    0x20: "SET_LINE_CODING",
    0x21: "GET_LINE_CODING",
    0x22: "SET_CONTROL_LINE_STATE",
    0x23: "SEND_BREAK",
}

#: CDC NCM（subclass 0x0D）的类请求：NTB 参数协商 + 以太网过滤
NCM_REQUESTS = {
    0x40: "SET_ETHERNET_MULTICAST_FILTERS",
    0x41: "SET_ETHERNET_PM_PATTERN_FILTER",
    0x42: "GET_ETHERNET_PM_PATTERN_FILTER",
    0x43: "SET_ETHERNET_PACKET_FILTER",
    0x44: "GET_ETHERNET_STATISTIC",
    0x80: "GET_NTB_PARAMETERS",
    0x81: "GET_NET_ADDRESS",
    0x82: "SET_NET_ADDRESS",
    0x83: "GET_NTB_FORMAT",
    0x84: "SET_NTB_FORMAT",
    0x85: "GET_NTB_INPUT_SIZE",
    0x86: "SET_NTB_INPUT_SIZE",
    0x87: "GET_MAX_DATAGRAM_SIZE",
    0x88: "SET_MAX_DATAGRAM_SIZE",
    0x89: "GET_CRC_MODE",
    0x8A: "SET_CRC_MODE",
}

#: RNDIS（interface class 0xEF/0x04）的控制请求，码值与 CDC 一致
RNDIS_REQUESTS = {
    0x00: "SEND_ENCAPSULATED_COMMAND",
    0x01: "GET_ENCAPSULATED_RESPONSE",
}

HID_REQUESTS = {
    0x01: "GET_REPORT",
    0x02: "GET_IDLE",
    0x03: "GET_PROTOCOL",
    0x09: "SET_REPORT",
    0x0A: "SET_IDLE",
    0x0B: "SET_PROTOCOL",
}

DFU_REQUESTS = {
    0x00: "DETACH",
    0x01: "DNLOAD",
    0x02: "UPLOAD",
    0x03: "GETSTATUS",
    0x04: "CLRSTATUS",
    0x05: "GETSTATE",
    0x06: "ABORT",
}

HID_REPORT_TYPES = {1: "Input", 2: "Output", 3: "Feature"}

MSC_OPCODES = {
    0x00: "TEST_UNIT_READY",
    0x03: "REQUEST_SENSE",
    0x12: "INQUIRY",
    0x1A: "MODE_SENSE_6",
    0x1B: "START_STOP_UNIT",
    0x1E: "PREVENT_ALLOW_MEDIUM_REMOVAL",
    0x23: "READ_FORMAT_CAPACITIES",
    0x25: "READ_CAPACITY_10",
    0x28: "READ_10",
    0x2A: "WRITE_10",
    0x2E: "WRITE_AND_VERIFY_10",
    0x2F: "VERIFY_10",
    0x35: "SYNCHRONIZE_CACHE",
    0x36: "SYNCHRONIZE_CACHE_16",
    0x88: "READ_16",
    0x8A: "WRITE_16",
    0x9E: "SERVICE_ACTION_IN",
    0xA0: "REPORT_LUNS",
    0xA1: "BLANK",
}

USBD_STATUS_NAMES = {
    0x00000000: "SUCCESS",
    0x40000000: "PENDING",
    # 传输/协议错误
    0xC0000001: "CRC",
    0xC0000002: "BTSTUFF",
    0xC0000003: "DATA_TOGGLE_MISMATCH",
    0xC0000004: "STALL_PID",
    0xC0000005: "DEV_NOT_RESPONDING",
    0xC0000006: "PID_CHECK_FAILURE",
    0xC0000007: "UNEXPECTED_PID",
    0xC0000008: "DATA_OVERRUN",
    0xC0000009: "DATA_UNDERRUN",
    0xC000000C: "BUFFER_OVERRUN",
    0xC000000D: "BUFFER_UNDERRUN",
    0xC000000F: "NOT_ACCESSED",
    0xC0000010: "FIFO",
    0xC0000011: "XACT_ERROR",
    0xC0000012: "BABBLE_DETECTED",
    0xC0000013: "DATA_BUFFER_ERROR",
    0xC0000030: "ENDPOINT_HALTED",
    # 栈/参数错误
    0x80000200: "INVALID_URB_FUNCTION",
    0x80000300: "INVALID_PARAMETER",
    0x80000400: "ERROR_BUSY",
    0x80000600: "INVALID_PIPE_HANDLE",
    0x80000700: "NO_BANDWIDTH",
    0x80000800: "INTERNAL_HC_ERROR",
    0x80000900: "ERROR_SHORT_TRANSFER",
    # 等时/帧
    0xC0000A00: "BAD_START_FRAME",
    0xC0000B00: "ISOCH_REQUEST_FAILED",
    0xC0000C00: "FRAME_CONTROL_OWNED",
    0xC0000D00: "FRAME_CONTROL_NOT_OWNED",
    0xC0000E00: "NOT_SUPPORTED",
    0xC0000F00: "INVALID_CONFIGURATION_DESCRIPTOR",
    # 设备移除 / 取消
    0xC0007000: "DEVICE_GONE",
    0xC0010000: "CANCELED",
}

#: Windows usbdi.h 的 URB_FUNCTION_*（只列常见项，其余回落成 URB_0xNNNN）
_URB_TRANSFER_FUNCTIONS = {
    0x0000: "SELECT_CONFIGURATION",
    0x0001: "SELECT_INTERFACE",
    0x0002: "ABORT_PIPE",
    0x0003: "TAKE_FRAME_LENGTH_CONTROL",
    0x0004: "RELEASE_FRAME_LENGTH_CONTROL",
    0x0005: "GET_FRAME_LENGTH",
    0x0006: "SET_FRAME_LENGTH",
    0x0007: "GET_CURRENT_FRAME_NUMBER",
    0x0008: "CONTROL_TRANSFER",
    0x0009: "BULK_OR_INTERRUPT_TRANSFER",
    0x000A: "ISOCH_TRANSFER",
    0x000B: "GET_DESCRIPTOR_FROM_DEVICE",
    0x000C: "SET_DESCRIPTOR_TO_DEVICE",
    0x000D: "SET_FEATURE_TO_DEVICE",
    0x000E: "SET_FEATURE_TO_INTERFACE",
    0x000F: "SET_FEATURE_TO_ENDPOINT",
    0x0010: "CLEAR_FEATURE_TO_DEVICE",
    0x0011: "CLEAR_FEATURE_TO_INTERFACE",
    0x0012: "CLEAR_FEATURE_TO_ENDPOINT",
    0x0013: "GET_STATUS_FROM_DEVICE",
    0x0014: "GET_STATUS_FROM_INTERFACE",
    0x0015: "GET_STATUS_FROM_ENDPOINT",
    0x0017: "VENDOR_DEVICE",
    0x0018: "VENDOR_INTERFACE",
    0x0019: "VENDOR_ENDPOINT",
    0x001A: "CLASS_DEVICE",
    0x001B: "CLASS_INTERFACE",
    0x001C: "CLASS_ENDPOINT",
    0x001E: "SYNC_RESET_PIPE_AND_CLEAR_STALL",
    0x001F: "CLASS_OTHER",
    0x0020: "VENDOR_OTHER",
    0x0021: "GET_STATUS_FROM_OTHER",
    0x0022: "CLEAR_FEATURE_TO_OTHER",
    0x0023: "SET_FEATURE_TO_OTHER",
    0x0024: "GET_DESCRIPTOR_FROM_ENDPOINT",
    0x0025: "SET_DESCRIPTOR_TO_ENDPOINT",
    0x0026: "GET_CONFIGURATION",
    0x0027: "GET_INTERFACE",
    0x0028: "GET_DESCRIPTOR_FROM_INTERFACE",
    0x0029: "SET_DESCRIPTOR_TO_INTERFACE",
    0x002A: "GET_MS_FEATURE_DESCRIPTOR",
    0x0030: "SYNC_RESET_PIPE",
    0x0031: "SYNC_CLEAR_STALL",
    0x0032: "CONTROL_TRANSFER_EX",
    0x0038: "RESET_PIPE",
    0x0039: "OPEN_STATIC_STREAMS",
    0x003A: "CLOSE_STATIC_STREAMS",
}


# ── 通用辅助 ──────────────────────────────────────────────

def status_name(status: int) -> str:
    name = USBD_STATUS_NAMES.get(status & 0xFFFFFFFF)
    if name:
        return name
    return f"USBD_0x{status & 0xFFFFFFFF:08X}"


def urb_function_name(function: int) -> str:
    return _URB_TRANSFER_FUNCTIONS.get(function, f"URB_0x{function:04X}")


def transfer_type_label(transfer_type: int) -> str:
    return TRANSFER_TYPE_LABELS.get(transfer_type, f"unknown({transfer_type})")


def class_name(cls: int, subcls: int = 0, protocol: int = 0) -> str:
    if cls == 0xFE and subcls == 0x01:
        return "DFU"
    return DEVICE_CLASSES.get(cls, f"Class 0x{cls:02X}")


def _normalize_hex_id(value: str) -> str | None:
    """``0xC251`` / ``C251`` / ``c251`` → ``0xC251``；非法返回 None。"""
    text = str(value).strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        return f"0x{int(text, 16):04X}"
    except ValueError:
        return None


def class_hint_for(cls: int, subcls: int = 0) -> str | None:
    """把 bDeviceClass/bInterfaceClass 转成前端用的短标签"""
    if cls == 0xEF and subcls == 0x04:
        return "rndis"
    if cls == 0x02 and subcls == 0x0D:
        return "ncm"
    if cls == 0x02:
        return "cdc"
    if cls == 0x03:
        return "hid"
    if cls == 0x08:
        return "msc"
    if cls == 0x09:
        return "hub"
    if cls == 0xFE and subcls == 0x01:
        return "dfu"
    return None


def hex_bytes(data: bytes, limit: int = 256) -> str:
    view = data[:limit]
    text = " ".join(f"{b:02X}" for b in view)
    if len(data) > limit:
        text += " …"
    return text


def ascii_preview(data: bytes, limit: int = 64) -> str:
    out = []
    for b in data[:limit]:
        out.append(chr(b) if 0x20 <= b < 0x7F else ".")
    text = "".join(out)
    if len(data) > limit:
        text += "…"
    return text


# ── SETUP 解码 ────────────────────────────────────────────

def decode_setup(setup: bytes, class_hint: str | None = None) -> dict:
    """解码 8 字节 SETUP，返回 camelCase 字段（REST 直接使用）"""
    if len(setup) < 8:
        return {
            "bmRequestType": int.from_bytes(setup, "little") if setup else 0,
            "request": 0,
            "wValue": 0,
            "wIndex": 0,
            "wLength": 0,
            "direction": "out",
            "type": "reserved",
            "recipient": "other",
            "requestName": "MALFORMED_SETUP",
            "decoded": {},
            "summary": "SETUP 数据不足 8 字节",
        }

    bm, request, w_value, w_index, w_length = struct.unpack_from("<BBHHH", setup, 0)
    direction = "in" if bm & 0x80 else "out"
    req_type_bits = (bm >> 5) & 0x03
    req_type = {0: "standard", 1: "class", 2: "vendor", 3: "reserved"}[req_type_bits]
    recipient_bits = bm & 0x1F
    recipient = {
        0: "device",
        1: "interface",
        2: "endpoint",
        3: "other",
    }.get(recipient_bits, f"other({recipient_bits})")

    name = _request_name(req_type, request, class_hint)
    decoded: dict[str, str | int] = {}

    if req_type == "standard" and request == 6:  # GET_DESCRIPTOR
        desc_type = (w_value >> 8) & 0xFF
        desc_index = w_value & 0xFF
        decoded["descriptorType"] = DESCRIPTOR_TYPES.get(desc_type, f"0x{desc_type:02X}")
        decoded["descriptorIndex"] = desc_index
        if desc_type == 3:
            decoded["languageId"] = f"0x{w_index:04X}"
        decoded["wLength"] = w_length
    elif req_type == "standard" and request == 7:  # SET_DESCRIPTOR
        desc_type = (w_value >> 8) & 0xFF
        decoded["descriptorType"] = DESCRIPTOR_TYPES.get(desc_type, f"0x{desc_type:02X}")
        decoded["descriptorIndex"] = w_value & 0xFF
    elif req_type == "standard" and request == 9:
        decoded["configuration"] = w_value & 0xFF
    elif req_type == "standard" and request == 11:
        decoded["interface"] = w_index & 0xFF
        decoded["alternateSetting"] = w_value & 0xFF
    elif req_type == "standard" and request == 5:
        decoded["deviceAddress"] = w_value & 0x7F
    elif req_type == "standard" and request in (1, 3):
        decoded["featureSelector"] = w_value
        decoded["target"] = w_index
    elif req_type == "class" and class_hint == "cdc":
        _decode_cdc_setup_fields(request, w_value, w_index, w_length, decoded)
    elif req_type == "class" and class_hint == "ncm":
        decoded["interface"] = w_index
        if request in (0x84, 0x8A):
            decoded["value"] = w_value
        elif request in (0x86, 0x88):
            decoded["dataLength"] = w_length
    elif req_type == "class" and class_hint == "hid":
        _decode_hid_setup_fields(request, w_value, w_index, w_length, decoded)
    elif req_type == "class" and class_hint == "msc":
        decoded["mscRequest"] = request
        decoded["interface"] = w_index
    elif req_type == "class" and class_hint == "hub":
        if request in (0, 1, 3):  # GET_STATUS / CLEAR_FEATURE / SET_FEATURE
            decoded["hubFeature"] = HUB_FEATURE_NAMES.get(
                w_value, f"0x{w_value:04X}"
            )
            decoded["port"] = w_index + 1
    elif req_type == "class" and class_hint == "dfu":
        decoded["interface"] = w_index
    elif req_type == "vendor":
        decoded["vendorRequest"] = request
        decoded["wValue"] = w_value
        decoded["wIndex"] = w_index

    summary = _setup_summary(name, req_type, direction, decoded, w_length)
    return {
        "bmRequestType": bm,
        "request": request,
        "wValue": w_value,
        "wIndex": w_index,
        "wLength": w_length,
        "direction": direction,
        "type": req_type,
        "recipient": recipient,
        "requestName": name,
        "decoded": decoded,
        "summary": summary,
    }


def _request_name(req_type: str, request: int, class_hint: str | None) -> str:
    if req_type == "standard":
        return STANDARD_REQUESTS.get(request, f"STD_0x{request:02X}")
    if req_type == "class":
        table = {
            "cdc": CDC_REQUESTS,
            "hid": HID_REQUESTS,
            "dfu": DFU_REQUESTS,
            "hub": HUB_REQUESTS,
            "rndis": RNDIS_REQUESTS,
            "ncm": NCM_REQUESTS,
        }.get(class_hint or "", {})
        return table.get(request, f"CLASS_0x{request:02X}")
    if req_type == "vendor":
        return f"VENDOR_0x{request:02X}"
    return f"REQ_0x{request:02X}"


def _decode_cdc_setup_fields(
    request: int, w_value: int, w_index: int, w_length: int, decoded: dict
) -> None:
    decoded["interface"] = w_index
    if request == 0x22:
        decoded["dtr"] = bool(w_value & 0x01)
        decoded["rts"] = bool(w_value & 0x02)
    elif request == 0x23:
        decoded["breakMs"] = w_value
    elif request in (0x20, 0x21):
        decoded["dataLength"] = w_length


def _decode_hid_setup_fields(
    request: int, w_value: int, w_index: int, w_length: int, decoded: dict
) -> None:
    decoded["interface"] = w_index
    if request in (0x01, 0x09):  # GET_REPORT / SET_REPORT
        decoded["reportType"] = HID_REPORT_TYPES.get((w_value >> 8) & 0xFF, "Reserved")
        decoded["reportId"] = w_value & 0xFF
        decoded["dataLength"] = w_length
    elif request in (0x02, 0x0A):  # GET_IDLE / SET_IDLE
        decoded["duration"] = (w_value >> 8) & 0xFF
        decoded["reportId"] = w_value & 0xFF
    elif request in (0x03, 0x0B):
        decoded["protocol"] = "Report" if w_value == 1 else "Boot"


def _setup_summary(
    name: str, req_type: str, direction: str, decoded: dict, w_length: int
) -> str:
    parts: list[str] = []
    if name == "GET_DESCRIPTOR":
        desc = decoded.get("descriptorType", "?")
        idx = decoded.get("descriptorIndex", 0)
        parts.append(f"GET_DESCRIPTOR({desc}" + (f", idx={idx}" if idx else "") + ")")
        if w_length:
            parts.append(f"wLength={w_length}")
    elif name == "SET_CONFIGURATION":
        parts.append(f"SET_CONFIGURATION({decoded.get('configuration', '?')})")
    elif name == "SET_INTERFACE":
        parts.append(
            f"SET_INTERFACE(if={decoded.get('interface', '?')}, alt={decoded.get('alternateSetting', '?')})"
        )
    elif name == "SET_ADDRESS":
        parts.append(f"SET_ADDRESS({decoded.get('deviceAddress', '?')})")
    elif decoded.get("hubFeature"):
        parts.append(
            f"{name}({decoded['hubFeature']}, port {decoded.get('port', '?')})"
        )
    elif name in ("SET_FEATURE", "CLEAR_FEATURE"):
        parts.append(f"{name}(sel=0x{decoded.get('featureSelector', 0):04X})")
    elif name == "SET_LINE_CODING":
        parts.append("CDC SET_LINE_CODING")
    elif name == "GET_LINE_CODING":
        parts.append("CDC GET_LINE_CODING")
    elif name == "SET_CONTROL_LINE_STATE":
        flags = []
        if decoded.get("dtr"):
            flags.append("DTR")
        if decoded.get("rts"):
            flags.append("RTS")
        parts.append("CDC SET_CONTROL_LINE_STATE" + (f"({'|'.join(flags)})" if flags else ""))
    elif name == "SEND_BREAK":
        parts.append(f"CDC SEND_BREAK({decoded.get('breakMs', 0)}ms)")
    elif name in ("GET_REPORT", "SET_REPORT"):
        parts.append(
            f"HID {name}({decoded.get('reportType', '?')}, id={decoded.get('reportId', 0)})"
        )
    elif name in ("GET_IDLE", "SET_IDLE"):
        parts.append(f"HID {name}(id={decoded.get('reportId', 0)})")
    elif req_type == "vendor":
        parts.append(f"VENDOR 0x{decoded.get('vendorRequest', 0):02X}")
    else:
        parts.append(name)
    if direction == "in" and name in ("GET_STATUS", "GET_CONFIGURATION", "GET_DESCRIPTOR"):
        parts.append(f"({w_length}B)")
    return " ".join(parts)


# ── 描述符解析（委托 usb_desc；保持旧 dict 形状供 GUI/API 使用） ──
#
# 唯一的解析实现是 ``core/usb_desc.py``。本节的函数只做「模型 → 旧 dict」的
# 形状转换，字段名与顺序保持与既有抓包页一致，避免前端契约变化。


def parse_device_descriptor(data: bytes) -> dict:
    try:
        dev = usb_desc.parse_device_descriptor(data)
    except usb_desc.DescriptorError as exc:
        return {"error": str(exc)}
    return {
        "bcdUSB": usb_desc.bcd_text(dev.bcd_usb),
        "deviceClass": dev.klass,
        "deviceClassName": class_name(dev.klass, dev.subclass, dev.protocol),
        "deviceSubClass": dev.subclass,
        "deviceProtocol": dev.protocol,
        "maxPacketSize0": dev.max_packet_size0,
        "vid": f"0x{dev.vid:04X}",
        "pid": f"0x{dev.pid:04X}",
        "bcdDevice": usb_desc.bcd_text(dev.bcd_device),
        "iManufacturer": dev.i_manufacturer,
        "iProduct": dev.i_product,
        "iSerialNumber": dev.i_serial,
        "numConfigurations": dev.num_configurations,
    }


def _class_descriptor_dict(item: usb_desc.ClassDescriptor) -> dict | None:
    """把类描述符模型转成旧契约的 dict；未知类型返回 None（旧实现直接忽略）。"""
    raw = item.raw
    if item.kind == "IAD":
        first = usb_desc.as_int(item.fields.get("firstInterface"))
        count = usb_desc.as_int(item.fields.get("interfaceCount"))
        klass = usb_desc.as_int(item.fields.get("class"))
        subclass = usb_desc.as_int(item.fields.get("subClass"))
        protocol = usb_desc.as_int(item.fields.get("protocol"))
        return {
            "type": "IAD",
            "firstInterface": first,
            "interfaceCount": count,
            "class": klass,
            "className": class_name(klass, subclass, protocol),
            "subclass": subclass,
            "protocol": protocol,
            "iFunction": usb_desc.as_int(item.fields.get("iFunction")),
        }
    if item.kind == "CS_INTERFACE":
        return {
            "type": "CS_INTERFACE",
            "subtype": item.subtype,
            "hex": usb_desc.hex_string(raw),
        }
    if item.kind == "HID":
        entry: dict = {"type": "HID", "hex": usb_desc.hex_string(raw)}
        if len(raw) >= 9:
            bcd_hid = struct.unpack_from("<H", raw, 2)[0]
            entry.update(
                {
                    "bcdHID": f"{bcd_hid >> 8}.{bcd_hid & 0xFF:02X}",
                    "countryCode": raw[4],
                    "numDescriptors": raw[5],
                    "reportDescriptorType": raw[6],
                    "reportDescriptorLength": struct.unpack_from("<H", raw, 7)[0],
                }
            )
        return entry
    if item.kind == "HID_REPORT":
        return {
            "type": "HID_REPORT",
            "length": len(raw),
            "hex": hex_bytes(raw, 128),
        }
    return None


def parse_config_descriptor(data: bytes) -> dict:
    """解析整段配置描述符（含接口/端点/类描述符）"""
    try:
        config = usb_desc.parse_config_descriptor(data)
    except usb_desc.DescriptorError as exc:
        return {"error": str(exc), "interfaces": []}

    interfaces: list[dict] = []
    endpoint_classes: list[dict] = []
    for interface in config.interfaces:
        endpoints = []
        for endpoint in interface.endpoints:
            endpoints.append(
                {
                    "address": endpoint.address,
                    "number": endpoint.number,
                    "direction": endpoint.direction,
                    "type": endpoint.transfer_type,
                    "maxPacketSize": endpoint.max_packet_size,
                    "interval": endpoint.interval,
                }
            )
            endpoint_classes.append(
                {
                    "address": endpoint.address,
                    "class": interface.klass,
                    "subclass": interface.subclass,
                    "protocol": interface.protocol,
                }
            )
        class_descriptors = []
        for item in interface.class_descriptors:
            converted = _class_descriptor_dict(item)
            if converted is not None:
                class_descriptors.append(converted)
        interfaces.append(
            {
                "number": interface.number,
                "alternate": interface.alternate,
                "numEndpoints": interface.declared_endpoints,
                "class": interface.klass,
                "className": class_name(
                    interface.klass, interface.subclass, interface.protocol
                ),
                "subclass": interface.subclass,
                "protocol": interface.protocol,
                "iInterface": interface.i_interface,
                "endpoints": endpoints,
                "classDescriptors": class_descriptors,
            }
        )

    config_classes = []
    for item in config.class_descriptors:
        converted = _class_descriptor_dict(item)
        if converted is not None:
            config_classes.append(converted)

    return {
        "totalLength": config.total_length,
        "numInterfaces": config.declared_num_interfaces or config.num_interfaces,
        "configurationValue": config.value,
        "iConfiguration": config.i_configuration,
        "attributes": config.attributes,
        "selfPowered": bool(config.attributes & 0x40),
        "remoteWakeup": bool(config.attributes & 0x20),
        "maxPowerMa": config.max_power * 2,
        "interfaces": interfaces,
        "endpointClasses": endpoint_classes,
        # 配置级类描述符（IAD 常出现在接口之前）；旧契约没有此键，新增不破坏消费方
        "classDescriptors": config_classes,
    }


def parse_string_descriptor(data: bytes) -> str:
    try:
        return usb_desc.parse_string_descriptor(data)
    except usb_desc.DescriptorError:
        return ""


def parse_device_qualifier(data: bytes) -> dict:
    try:
        parsed = usb_desc.parse_device_qualifier(data)
    except usb_desc.DescriptorError as exc:
        return {"error": str(exc)}
    return {
        "bcdUSB": f"{parsed['bcdUSB'] >> 8}.{parsed['bcdUSB'] & 0xFF:02X}",
        "deviceClass": parsed["class"],
        "deviceClassName": class_name(
            parsed["class"], parsed["subClass"], parsed["protocol"]
        ),
        "maxPacketSize0": parsed["maxPacketSize0"],
        "numConfigurations": parsed["numConfigurations"],
    }


def parse_bos_descriptor(data: bytes) -> dict:
    try:
        bos = usb_desc.parse_bos_descriptor(data)
    except usb_desc.DescriptorError as exc:
        return {"error": str(exc)}
    capabilities = []
    for item in bos.capabilities:
        capabilities.append(
            {
                "type": DESCRIPTOR_TYPES.get(item.dtype, f"0x{item.dtype:02X}"),
                "length": len(item.raw),
                "hex": hex_bytes(item.raw, 64),
            }
        )
    return {
        "totalLength": bos.total_length,
        "numDeviceCaps": len(bos.capabilities),
        "capabilities": capabilities,
    }


# ── 类解码 ────────────────────────────────────────────────

def decode_line_coding(data: bytes) -> dict | None:
    if len(data) < 7:
        return None
    rate, char_format, parity, data_bits = struct.unpack_from("<IBBB", data, 0)
    return {
        "baudRate": rate,
        "stopBits": {0: 1, 1: 1.5, 2: 2}.get(char_format, char_format),
        "parity": {0: "None", 1: "Odd", 2: "Even", 3: "Mark", 4: "Space"}.get(
            parity, str(parity)
        ),
        "dataBits": data_bits,
        "text": f"{rate} {data_bits}{'N' if parity == 0 else parity}",
    }


def decode_msc(data: bytes) -> dict | None:
    """识别 Bulk-Only Transport 的 CBW / CSW"""
    if len(data) >= 31 and data[:4] == b"USBC":  # 0x43425355 LE
        (
            _sig,
            tag,
            transfer_length,
            flags,
            lun,
            cb_length,
            cdb,
        ) = struct.unpack_from("<IIIBBB16s", data, 0)
        opcode = cdb[0] if cdb else 0
        return {
            "kind": "CBW",
            "tag": tag,
            "transferLength": transfer_length,
            "direction": "in" if flags & 0x80 else "out",
            "lun": lun,
            "cbLength": cb_length,
            "opcode": opcode,
            "opcodeName": MSC_OPCODES.get(opcode, f"0x{opcode:02X}"),
        }
    if len(data) >= 13 and data[:4] == b"USBS":  # 0x53425355 LE
        _sig, tag, residue, status = struct.unpack_from("<IIIB", data, 0)
        return {
            "kind": "CSW",
            "tag": tag,
            "residue": residue,
            "status": status,
            "statusName": {0: "Passed", 1: "Failed", 2: "Phase Error"}.get(
                status, str(status)
            ),
        }
    return None


def decode_dfu_status(data: bytes) -> dict | None:
    if len(data) < 6:
        return None
    status, poll_timeout, state, i_string = struct.unpack_from("<B3sBB", data, 0)
    return {
        "status": status,
        "pollTimeoutMs": int.from_bytes(poll_timeout, "little"),
        "state": state,
        "iString": i_string,
    }


def decode_cdc_payload(request: int, direction: str, data: bytes) -> dict | None:
    if request in (0x20, 0x21) and direction == "in" or (
        request == 0x21 and direction == "in"
    ):
        return decode_line_coding(data)
    if request == 0x20 and len(data) >= 7:
        return decode_line_coding(data)
    return None


# ── 传输摘要 ──────────────────────────────────────────────

def summarize_transfer(
    transfer_type: int,
    direction: str,
    setup_info: dict | None,
    data: bytes,
    class_hint: str | None,
    iso_count: int = 0,
) -> str:
    if transfer_type == 2 and setup_info:
        text = setup_info.get("summary") or setup_info.get("requestName", "CONTROL")
        req = setup_info.get("requestName", "")
        if class_hint == "cdc" and req in ("SET_LINE_CODING", "GET_LINE_CODING"):
            coding = decode_line_coding(data)
            if coding:
                text += f" [{coding['text']}]"
        return text

    msc = decode_msc(data)
    if transfer_type == 3 and msc:
        if msc["kind"] == "CBW":
            return (
                f"MSC CBW {msc['opcodeName']} "
                f"{msc['direction'].upper()} {msc['transferLength']}B"
            )
        return (
            f"MSC CSW {msc['statusName']} residue={msc['residue']}B"
        )

    type_label = TRANSFER_TYPE_LABELS.get(transfer_type, "unknown")
    if transfer_type == 0:
        return f"{type_label.upper()} {direction.upper()} {iso_count} pkts {len(data)}B"
    return f"{type_label.upper()} {direction.upper()} {len(data)}B"


# ── 描述符收集器 ──────────────────────────────────────────

@dataclass(slots=True)
class _DeviceState:
    device_desc: bytes | None = None
    device: dict | None = None
    configs: dict[int, bytes] = field(default_factory=dict)
    parsed_configs: dict[int, dict] = field(default_factory=dict)
    strings: dict[int, dict[int, str]] = field(default_factory=dict)
    endpoint_classes: dict[int, dict] = field(default_factory=dict)


class DescriptorCollector:
    """按 (bus, dev) 累积 GET_DESCRIPTOR 响应，重建描述符树"""

    def __init__(self) -> None:
        self._devices: dict[tuple[int, int], _DeviceState] = {}
        self._buffers: dict[tuple[int, int, int, int, int], bytes] = {}

    def reset(self) -> None:
        self._devices.clear()
        self._buffers.clear()

    # ── 喂入一条控制传输 ──────────────────────────────────
    def feed(self, transfer) -> bool:
        """返回描述符树是否发生变化（transfer 为 ``UsbTransfer``）"""
        if transfer.transfer_type != 2 or not transfer.setup or len(transfer.setup) < 8:
            return False
        bm = transfer.setup[0]
        request = transfer.setup[1]
        if (bm & 0x60) != 0 or request != 6 or not (bm & 0x80):
            return False

        w_value = int.from_bytes(transfer.setup[2:4], "little")
        w_index = int.from_bytes(transfer.setup[4:6], "little")
        desc_type = (w_value >> 8) & 0xFF
        desc_index = w_value & 0xFF
        lang_id = w_index if desc_type == 3 else 0

        if not transfer.data:
            return False

        key = (transfer.bus, transfer.device, desc_type, desc_index, lang_id)
        previous = self._buffers.get(key, b"")
        data = transfer.data
        # 只保留最长的一份，避免重复请求导致拼接错位
        if len(data) >= len(previous):
            self._buffers[key] = data
        chosen = self._buffers[key]
        changed = chosen != previous

        if desc_type == 1 and len(chosen) >= 18:
            if previous != chosen:
                self._on_device_descriptor(transfer.bus, transfer.device, chosen[:18])
                changed = True
        elif desc_type == 2:
            expected = _config_total_length(chosen)
            if expected is None or len(chosen) >= expected:
                self._on_config_descriptor(
                    transfer.bus, transfer.device, desc_index, chosen, expected
                )
                changed = True
        elif desc_type == 3:
            text = parse_string_descriptor(chosen)
            if text:
                dev = self._devices.setdefault(
                    (transfer.bus, transfer.device), _DeviceState()
                )
                dev.strings.setdefault(desc_index, {})[lang_id] = text
                changed = True
        return changed

    def _on_device_descriptor(self, bus: int, device: int, raw: bytes) -> None:
        key = (bus, device)
        state = self._devices.setdefault(key, _DeviceState())
        parsed = parse_device_descriptor(raw)
        # 地址复用/重新枚举：VID/PID 变化时丢弃旧描述符
        if state.device and (
            state.device.get("vid") != parsed.get("vid")
            or state.device.get("pid") != parsed.get("pid")
        ):
            state.configs.clear()
            state.parsed_configs.clear()
            state.strings.clear()
            state.endpoint_classes.clear()
        state.device_desc = raw
        state.device = parsed

    def _on_config_descriptor(
        self, bus: int, device: int, index: int, raw: bytes, expected: int | None
    ) -> None:
        state = self._devices.setdefault((bus, device), _DeviceState())
        state.configs[index] = raw
        parsed = parse_config_descriptor(raw)
        state.parsed_configs[index] = parsed
        for item in parsed.get("endpointClasses", []):
            state.endpoint_classes[item["address"] & 0x0F] = item

    # ── 查询 ──────────────────────────────────────────────
    def device_addresses(
        self, *, vid: str | None = None, pid: str | None = None
    ) -> set[tuple[int, int]]:
        """按 VID/PID 找抓包期间枚举到的设备地址（``0xC251`` / ``C251`` 均可）。"""
        want_vid = _normalize_hex_id(vid) if vid else None
        want_pid = _normalize_hex_id(pid) if pid else None
        out: set[tuple[int, int]] = set()
        for key, state in self._devices.items():
            device = state.device or {}
            if want_vid and device.get("vid") != want_vid:
                continue
            if want_pid and device.get("pid") != want_pid:
                continue
            out.add(key)
        return out

    def class_hint(self, bus: int, device: int, endpoint: int) -> str | None:
        state = self._devices.get((bus, device))
        if state is None:
            return None
        if state.device:
            cls = state.device.get("deviceClass", 0)
            sub = state.device.get("deviceSubClass", 0)
            if cls not in (0, 0xEF):
                hint = class_hint_for(cls, sub)
                if hint:
                    return hint
            if cls == 0xEF and (endpoint & 0x7F) == 0:
                # 复合设备（0xEF/0x02）的控制请求：按接口类推断（RNDIS/CDC/HID…）
                for config in state.parsed_configs.values():
                    for interface in config.get("interfaces") or []:
                        hint = class_hint_for(
                            interface.get("class", 0), interface.get("subclass", 0)
                        )
                        if hint:
                            return hint
        item = state.endpoint_classes.get(endpoint & 0x0F)
        if item:
            return class_hint_for(item.get("class", 0), item.get("subclass", 0))
        return None

    def device_ids(self) -> list[tuple[int, int]]:
        return sorted(self._devices.keys())

    def documents(self) -> list[dict]:
        """把已收集的原始描述符整理成 ``usb_desc`` JSON 文档（每个设备一份）。

        供 ``/api/usb/desc/from-capture`` 使用：抓包 → 文档 → 改 → 生成 hex。
        描述符不完整（只有半个配置）时跳过该配置，而不是整份丢弃。
        """
        out: list[dict] = []
        for (bus, device), state in sorted(self._devices.items()):
            device_obj = None
            if state.device_desc:
                try:
                    device_obj = usb_desc.parse_device_descriptor(state.device_desc)
                except usb_desc.DescriptorError:
                    device_obj = None
            configs = []
            for index in sorted(state.configs):
                try:
                    configs.append(usb_desc.parse_config_descriptor(state.configs[index]))
                except usb_desc.DescriptorError:
                    continue
            if device_obj is None and not configs:
                continue
            out.append(
                {
                    "bus": bus,
                    "device": device,
                    "document": usb_desc.to_document(
                        device_obj, configs, state.strings
                    ),
                }
            )
        return out

    def tree(self) -> list[dict]:
        nodes: list[dict] = []
        for (bus, device), state in sorted(self._devices.items()):
            if state.device is None and not state.parsed_configs:
                continue
            fields = [
                {"name": "地址", "value": f"{bus}.{device}"},
            ]
            title = f"设备 {device:03d}"
            if state.device:
                dev = state.device
                product = self._string(state, dev.get("iProduct", 0), 0x0409)
                manufacturer = self._string(state, dev.get("iManufacturer", 0), 0x0409)
                serial = self._string(state, dev.get("iSerialNumber", 0), 0x0409)
                if product:
                    title = f"{product} ({dev['vid']}:{dev['pid']})"
                elif "vid" in dev:
                    title = f"{dev['vid']}:{dev['pid']} 设备 {device:03d}"
                fields += [
                    {"name": "VID:PID", "value": f"{dev.get('vid')}:{dev.get('pid')}"},
                    {"name": "bcdUSB", "value": str(dev.get("bcdUSB", ""))},
                    {"name": "设备类", "value": str(dev.get("deviceClassName", ""))},
                    {"name": "配置数", "value": str(dev.get("numConfigurations", ""))},
                ]
                if manufacturer:
                    fields.append({"name": "厂商", "value": manufacturer})
                if product:
                    fields.append({"name": "产品", "value": product})
                if serial:
                    fields.append({"name": "序列号", "value": serial})

            children: list[dict] = []
            for cfg_index in sorted(state.parsed_configs.keys()):
                cfg = state.parsed_configs[cfg_index]
                if "error" in cfg:
                    continue
                cfg_fields = [
                    {"name": "配置值", "value": str(cfg.get("configurationValue", ""))},
                    {"name": "接口数", "value": str(cfg.get("numInterfaces", ""))},
                    {"name": "总长度", "value": f"{cfg.get('totalLength', 0)} B"},
                    {"name": "供电", "value": f"{cfg.get('maxPowerMa', 0)} mA"},
                    {
                        "name": "特性",
                        "value": ", ".join(
                            filter(
                                None,
                                [
                                    "Self-powered" if cfg.get("selfPowered") else "",
                                    "Remote wakeup" if cfg.get("remoteWakeup") else "",
                                ],
                            )
                        )
                        or "-",
                    },
                ]
                config_name = self._string(state, cfg.get("iConfiguration") or 0, 0x0409)
                if config_name:
                    cfg_fields.append({"name": "配置字符串", "value": config_name})
                iad_nodes = self._iad_nodes(state, cfg)
                iface_nodes = []
                for iface in cfg.get("interfaces", []):
                    iface_fields = [
                        {"name": "接口号", "value": str(iface.get("number", ""))},
                        {"name": "Alternate", "value": str(iface.get("alternate", ""))},
                        {"name": "类", "value": str(iface.get("className", ""))},
                        {
                            "name": "SubClass/Protocol",
                            "value": f"{iface.get('subclass', 0)}/{iface.get('protocol', 0)}",
                        },
                        {"name": "端点数", "value": str(iface.get("numEndpoints", ""))},
                    ]
                    interface_name = self._string(state, iface.get("iInterface") or 0, 0x0409)
                    if interface_name:
                        iface_fields.append({"name": "接口名", "value": interface_name})
                    ep_nodes = []
                    for ep in iface.get("endpoints", []):
                        ep_nodes.append(
                            {
                                "kind": "endpoint",
                                "title": f"EP 0x{ep['address']:02X} {ep['direction'].upper()} {ep['type']}",
                                "fields": [
                                    {"name": "最大包长", "value": str(ep["maxPacketSize"])},
                                    {"name": "间隔", "value": str(ep["interval"])},
                                ],
                                "children": [],
                            }
                        )
                    iface_nodes.append(
                        {
                            "kind": "interface",
                            "title": f"接口 {iface.get('number')} alt {iface.get('alternate')} · {iface.get('className')}",
                            "fields": iface_fields,
                            "children": ep_nodes,
                        }
                    )
                children.append(
                    {
                        "kind": "configuration",
                        "title": f"配置 {cfg.get('configurationValue')}",
                        "fields": cfg_fields,
                        "children": iad_nodes + iface_nodes,
                    }
                )

            nodes.append(
                {
                    "kind": "device",
                    "title": title,
                    "fields": fields,
                    "children": children,
                }
            )
        return nodes

    def _iad_nodes(self, state: "_DeviceState", cfg: dict) -> list[dict]:
        """把配置里的 IAD 渲染成独立节点（接口关联 + 功能名）。"""
        nodes: list[dict] = []
        for item in cfg.get("classDescriptors") or []:
            if item.get("type") != "IAD":
                continue
            first = item.get("firstInterface") or 0
            count = item.get("interfaceCount") or 0
            scope = f"{first}..{first + count - 1}" if count > 1 else str(first)
            fields = [
                {"name": "接口范围", "value": scope},
                {"name": "功能类", "value": str(item.get("className") or "")},
                {
                    "name": "SubClass/Protocol",
                    "value": f"{item.get('subclass', 0)}/{item.get('protocol', 0)}",
                },
            ]
            function_name = self._string(state, item.get("iFunction") or 0, 0x0409)
            if function_name:
                fields.append({"name": "功能名", "value": function_name})
            nodes.append(
                {
                    "kind": "interface-association",
                    "title": "IAD 接口关联",
                    "fields": fields,
                    "children": [],
                }
            )
        return nodes

    @staticmethod
    def _string(state: _DeviceState, index: int, lang_id: int) -> str | None:
        table = state.strings.get(index)
        if not table:
            return None
        return table.get(lang_id) or next(iter(table.values()), None)


def _config_total_length(data: bytes) -> int | None:
    return usb_desc.config_total_length(data)


def describe_transfer(
    transfer,
    setup_info: dict | None,
    class_hint: str | None,
) -> dict:
    """给管理器/详情页生成解码树（不包含 hex，hex 由调用方附加）"""
    nodes = [
        {
            "label": "USB",
            "value": (
                f"{transfer.bus}.{transfer.device} EP0x{transfer.endpoint:02X} "
                f"{transfer.direction.upper()} · {transfer_type_label(transfer.transfer_type)}"
            ),
            "children": [
                {
                    "label": "状态",
                    "value": f"{status_name(transfer.status)} (0x{transfer.status & 0xFFFFFFFF:08X})",
                },
                {
                    "label": "URB",
                    "value": urb_function_name(transfer.function),
                },
                {
                    "label": "长度 / 耗时",
                    "value": (
                        f"{len(transfer.data)} B"
                        + (
                            f" · {transfer.duration_us} µs"
                            if transfer.duration_us is not None
                            else ""
                        )
                    ),
                },
            ],
        }
    ]
    if setup_info:
        setup_children = [
            {
                "label": "bmRequestType",
                "value": (
                    f"0x{setup_info['bmRequestType']:02X} "
                    f"({setup_info['direction']}, {setup_info['type']}, {setup_info['recipient']})"
                ),
            },
            {"label": "bRequest", "value": setup_info["requestName"]},
            {"label": "wValue", "value": f"0x{setup_info['wValue']:04X}"},
            {"label": "wIndex", "value": f"0x{setup_info['wIndex']:04X}"},
            {"label": "wLength", "value": str(setup_info["wLength"])},
        ]
        for key, value in (setup_info.get("decoded") or {}).items():
            setup_children.append({"label": key, "value": str(value)})
        nodes.append({"label": "SETUP", "children": setup_children})

    class_fields = decode_class_fields(
        transfer.transfer_type,
        transfer.direction,
        setup_info,
        transfer.data,
        class_hint,
    )
    if class_fields:
        nodes.append(
            {
                "label": "解码",
                "children": [
                    {"label": k, "value": str(v)} for k, v in class_fields.items()
                ],
            }
        )
    return nodes


def decode_class_fields(
    transfer_type: int,
    direction: str,
    setup_info: dict | None,
    data: bytes,
    class_hint: str | None,
) -> dict:
    fields: dict[str, object] = {}
    if transfer_type == 2 and setup_info:
        request = setup_info.get("request", 0)
        if setup_info.get("type") == "class" and class_hint == "cdc":
            coding = decode_line_coding(data)
            if coding:
                fields.update(coding)
        elif setup_info.get("type") == "class" and class_hint == "dfu":
            status = decode_dfu_status(data)
            if status:
                fields.update(status)
    msc = decode_msc(data)
    if msc:
        fields.update(msc)
    return fields
