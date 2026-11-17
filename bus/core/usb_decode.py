"""USB 协议解码与描述符收集

本模块只做纯逻辑（不依赖 FastAPI / 文件系统），便于单元测试：
  - SETUP 包、标准/类请求、URB 状态名
  - 设备/配置/接口/端点/字符串/BOS/IAD 描述符解析
  - CDC ACM、HID、MSC、DFU 的基础类解码
  - ``DescriptorCollector``: 从控制传输流中重建设备描述符树
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

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

CDC_REQUESTS = {
    0x00: "SEND_ENCAPSULATED_COMMAND",
    0x01: "GET_ENCAPSULATED_RESPONSE",
    0x20: "SET_LINE_CODING",
    0x21: "GET_LINE_CODING",
    0x22: "SET_CONTROL_LINE_STATE",
    0x23: "SEND_BREAK",
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
    0xC0000004: "STALL_PID",
    0xC0000005: "DEV_NOT_RESPONDING",
    0xC0000008: "DATA_OVERRUN",
    0xC0000009: "DATA_UNDERRUN",
    0xC000000C: "BUFFER_OVERRUN",
    0xC000000D: "BUFFER_UNDERRUN",
    0xC0010000: "CANCELED",
}

_URB_TRANSFER_FUNCTIONS = {
    0x0008: "CONTROL_TRANSFER",
    0x0009: "BULK_OR_INTERRUPT_TRANSFER",
    0x000A: "ISOCH_TRANSFER",
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


def class_hint_for(cls: int, subcls: int = 0) -> str | None:
    """把 bDeviceClass/bInterfaceClass 转成前端用的短标签"""
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
    elif req_type == "class" and class_hint == "hid":
        _decode_hid_setup_fields(request, w_value, w_index, w_length, decoded)
    elif req_type == "class" and class_hint == "msc":
        decoded["mscRequest"] = request
        decoded["interface"] = w_index
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


# ── 描述符解析 ────────────────────────────────────────────

def _parse_descriptor_chain(data: bytes) -> list[tuple[int, int, bytes]]:
    """把配置描述符切成 (type, index, raw) 列表，防御 bLength=0"""
    out: list[tuple[int, int, bytes]] = []
    pos = 0
    while pos + 2 <= len(data):
        length = data[pos]
        dtype = data[pos + 1]
        if length < 2 or pos + length > len(data):
            break
        out.append((dtype, pos, data[pos : pos + length]))
        pos += length
    return out


def parse_device_descriptor(data: bytes) -> dict:
    if len(data) < 18:
        return {"error": f"设备描述符长度不足（{len(data)} < 18）"}
    (
        _blen,
        _btype,
        bcd_usb,
        dev_class,
        dev_subclass,
        dev_protocol,
        max_packet0,
        vid,
        pid,
        bcd_device,
        i_manufacturer,
        i_product,
        i_serial,
        num_configs,
    ) = struct.unpack_from("<BBHBBBBHHHBBBB", data, 0)
    return {
        "bcdUSB": f"{bcd_usb >> 8}.{(bcd_usb >> 4) & 0x0F}{bcd_usb & 0x0F}",
        "deviceClass": dev_class,
        "deviceClassName": class_name(dev_class, dev_subclass, dev_protocol),
        "deviceSubClass": dev_subclass,
        "deviceProtocol": dev_protocol,
        "maxPacketSize0": max_packet0,
        "vid": f"0x{vid:04X}",
        "pid": f"0x{pid:04X}",
        "bcdDevice": f"{bcd_device >> 8}.{(bcd_device >> 4) & 0x0F}{bcd_device & 0x0F}",
        "iManufacturer": i_manufacturer,
        "iProduct": i_product,
        "iSerialNumber": i_serial,
        "numConfigurations": num_configs,
    }


def parse_config_descriptor(data: bytes) -> dict:
    """解析整段配置描述符（含接口/端点/类描述符）"""
    if len(data) < 9:
        return {"error": f"配置描述符长度不足（{len(data)} < 9）", "interfaces": []}

    (
        _blen,
        _btype,
        total_length,
        num_interfaces,
        config_value,
        i_config,
        attributes,
        max_power,
    ) = struct.unpack_from("<BBHBBBBB", data, 0)

    interfaces: list[dict] = []
    current: dict | None = None
    endpoint_classes: list[dict] = []

    for dtype, _pos, raw in _parse_descriptor_chain(data[9:]):
        if dtype == 4 and len(raw) >= 9:  # INTERFACE
            (
                _l,
                _t,
                iface_num,
                alt,
                num_ep,
                iface_class,
                iface_sub,
                iface_proto,
                i_iface,
            ) = struct.unpack_from("<BBBBBBBBB", raw, 0)
            current = {
                "number": iface_num,
                "alternate": alt,
                "numEndpoints": num_ep,
                "class": iface_class,
                "className": class_name(iface_class, iface_sub, iface_proto),
                "subclass": iface_sub,
                "protocol": iface_proto,
                "iInterface": i_iface,
                "endpoints": [],
                "classDescriptors": [],
            }
            interfaces.append(current)
        elif dtype == 5 and len(raw) >= 7:  # ENDPOINT
            (
                _l,
                _t,
                ep_addr,
                ep_attr,
                max_packet,
                interval,
            ) = struct.unpack_from("<BBBBHB", raw, 0)
            ep = {
                "address": ep_addr,
                "number": ep_addr & 0x0F,
                "direction": "in" if ep_addr & 0x80 else "out",
                "type": {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}[
                    ep_attr & 0x03
                ],
                "maxPacketSize": max_packet & 0x7FF,
                "interval": interval,
            }
            if current is not None:
                current["endpoints"].append(ep)
                endpoint_classes.append(
                    {
                        "address": ep_addr,
                        "class": current["class"],
                        "subclass": current["subclass"],
                        "protocol": current["protocol"],
                    }
                )
        elif dtype == 0x0B and len(raw) >= 8:  # IAD
            (
                _l,
                _t,
                first_if,
                if_count,
                func_class,
                func_sub,
                func_proto,
                i_func,
            ) = struct.unpack_from("<BBBBBBBB", raw, 0)
            entry = {
                "type": "IAD",
                "firstInterface": first_if,
                "interfaceCount": if_count,
                "class": func_class,
                "className": class_name(func_class, func_sub, func_proto),
                "subclass": func_sub,
                "protocol": func_proto,
                "iFunction": i_func,
            }
            if current is not None:
                current["classDescriptors"].append(entry)
        elif dtype == 0x24 and current is not None:  # class-specific interface
            current["classDescriptors"].append(
                {
                    "type": "CS_INTERFACE",
                    "subtype": raw[2] if len(raw) > 2 else None,
                    "hex": raw.hex(" ").upper(),
                }
            )
        elif dtype == 0x21 and current is not None:  # HID
            entry: dict = {"type": "HID", "hex": raw.hex(" ").upper()}
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
            current["classDescriptors"].append(entry)
        elif dtype == 0x22 and current is not None:  # HID report descriptor
            current["classDescriptors"].append(
                {
                    "type": "HID_REPORT",
                    "length": len(raw),
                    "hex": hex_bytes(raw, 128),
                }
            )

    return {
        "totalLength": total_length,
        "numInterfaces": num_interfaces,
        "configurationValue": config_value,
        "iConfiguration": i_config,
        "attributes": attributes,
        "selfPowered": bool(attributes & 0x40),
        "remoteWakeup": bool(attributes & 0x20),
        "maxPowerMa": max_power * 2,
        "interfaces": interfaces,
        "endpointClasses": endpoint_classes,
    }


def parse_string_descriptor(data: bytes) -> str:
    if len(data) < 2:
        return ""
    length = min(data[0], len(data))
    body = data[2:length]
    if len(body) % 2:
        body = body[:-1]
    try:
        return body.decode("utf-16-le", errors="replace").rstrip("\x00")
    except Exception:
        return ""


def parse_device_qualifier(data: bytes) -> dict:
    if len(data) < 10:
        return {"error": "设备限定描述符长度不足"}
    (
        _l,
        _t,
        bcd_usb,
        dev_class,
        dev_sub,
        dev_proto,
        max_packet0,
        num_configs,
        _reserved,
    ) = struct.unpack_from("<BBHBBBBBB", data, 0)
    return {
        "bcdUSB": f"{bcd_usb >> 8}.{bcd_usb & 0xFF:02X}",
        "deviceClass": dev_class,
        "deviceClassName": class_name(dev_class, dev_sub, dev_proto),
        "maxPacketSize0": max_packet0,
        "numConfigurations": num_configs,
    }


def parse_bos_descriptor(data: bytes) -> dict:
    if len(data) < 5:
        return {"error": "BOS 描述符长度不足"}
    _l, _t, total_length, num_caps = struct.unpack_from("<BBHB", data, 0)
    caps = []
    pos = 5
    while pos + 3 <= len(data):
        length = data[pos]
        dtype = data[pos + 1]
        if length < 3 or pos + length > len(data):
            break
        caps.append(
            {
                "type": DESCRIPTOR_TYPES.get(dtype, f"0x{dtype:02X}"),
                "length": length,
                "hex": hex_bytes(data[pos : pos + length], 64),
            }
        )
        pos += length
    return {"totalLength": total_length, "numDeviceCaps": num_caps, "capabilities": caps}


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
        item = state.endpoint_classes.get(endpoint & 0x0F)
        if item:
            return class_hint_for(item.get("class", 0), item.get("subclass", 0))
        return None

    def device_ids(self) -> list[tuple[int, int]]:
        return sorted(self._devices.keys())

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
                        "children": iface_nodes,
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

    @staticmethod
    def _string(state: _DeviceState, index: int, lang_id: int) -> str | None:
        table = state.strings.get(index)
        if not table:
            return None
        return table.get(lang_id) or next(iter(table.values()), None)


def _config_total_length(data: bytes) -> int | None:
    if len(data) < 4:
        return None
    return struct.unpack_from("<H", data, 2)[0]


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
