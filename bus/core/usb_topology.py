"""USB 设备 → root hub / USBPcapN / 动态总线地址的拓扑映射（仅 Windows）。

- USBPcap 每挂一个 root hub 生成一个 ``\\\\.\\USBPcapN``；实例顺序记录在
  ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\USBPcap\\Enum``，第 N 个 root hub 即
  ``USBPcapN``（与 USBPcapCMD ``--extcap-interfaces`` 的输出顺序一致）。
- 设备归属用 CfgMgr32 的 ``CM_Get_Parent`` 逐级上溯到 ``USB\\ROOT_HUB`` 为止。
- 动态地址通过 hub 的 ``IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX`` 读取，
  不需要启动 USBPcap；端口返回的 driver key 用来与 PnP 实例及其复合接口准确关联。
"""

from __future__ import annotations

import ctypes
import sys
from collections import defaultdict
from ctypes import wintypes
from typing import Any

__all__ = [
    "usbpcap_root_hubs",
    "root_hub_for",
    "usb_device_addresses",
    "annotate_devices",
]

_MAX_DEVICE_ID = 512
_MAX_HUB_BUFFER = 4096

# CM_Get_DevNode_Registry_PropertyW 使用 CM_DRP_*，编号与 SetupAPI 的 SPDRP_* 不同。
_CM_DRP_DEVICEDESC = 0x00000001
_CM_DRP_HARDWARE_ID = 0x00000002
_CM_DRP_DRIVER = 0x0000000A
_CM_DRP_FRIENDLYNAME = 0x0000000D
_USBPCAP_ENUM_KEY = r"SYSTEM\CurrentControlSet\Services\USBPcap\Enum"

_DIGCF_PRESENT = 0x00000002
_DIGCF_DEVICEINTERFACE = 0x00000010
_ERROR_NO_MORE_ITEMS = 259
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_FILE_DEVICE_USB = 0x00000022
_METHOD_BUFFERED = 0
_FILE_ANY_ACCESS = 0


def _ctl_code(device_type: int, function: int, method: int, access: int) -> int:
    return (device_type << 16) | (access << 14) | (function << 2) | method


_IOCTL_USB_GET_NODE_INFORMATION = _ctl_code(
    _FILE_DEVICE_USB, 258, _METHOD_BUFFERED, _FILE_ANY_ACCESS
)
_IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME = _ctl_code(
    _FILE_DEVICE_USB, 264, _METHOD_BUFFERED, _FILE_ANY_ACCESS
)
_IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = _ctl_code(
    _FILE_DEVICE_USB, 274, _METHOD_BUFFERED, _FILE_ANY_ACCESS
)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]


_GUID_DEVINTERFACE_USB_HUB = _GUID(
    0xF18A0E88,
    0xC30C,
    0x11D0,
    (ctypes.c_uint8 * 8)(0x88, 0x15, 0x00, 0xA0, 0xC9, 0x06, 0xBE, 0xD8),
)


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("InterfaceClassGuid", _GUID),
        ("Flags", ctypes.c_uint32),
        ("Reserved", ctypes.c_size_t),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("ClassGuid", _GUID),
        ("DevInst", ctypes.c_uint32),
        ("Reserved", ctypes.c_size_t),
    ]


class _USB_DEVICE_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class _USB_HUB_DESCRIPTOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bDescriptorLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bNumberOfPorts", ctypes.c_uint8),
        ("wHubCharacteristics", ctypes.c_uint16),
        ("bPowerOnToPowerGood", ctypes.c_uint8),
        ("bHubControlCurrent", ctypes.c_uint8),
        ("bRemoveAndPowerMask", ctypes.c_uint8 * 64),
    ]


class _USB_HUB_INFORMATION(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("HubDescriptor", _USB_HUB_DESCRIPTOR),
        ("HubIsBusPowered", ctypes.c_uint8),
    ]


class _USB_MI_PARENT_INFORMATION(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("NumberOfInterfaces", ctypes.c_uint32)]


class _USB_NODE_INFORMATION_UNION(ctypes.Union):
    _pack_ = 1
    _fields_ = [
        ("HubInformation", _USB_HUB_INFORMATION),
        ("MiParentInformation", _USB_MI_PARENT_INFORMATION),
    ]


class _USB_NODE_INFORMATION(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("NodeType", ctypes.c_uint32),
        ("u", _USB_NODE_INFORMATION_UNION),
    ]


class _USB_NODE_CONNECTION_INFORMATION_EX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ConnectionIndex", ctypes.c_uint32),
        ("DeviceDescriptor", _USB_DEVICE_DESCRIPTOR),
        ("CurrentConfigurationValue", ctypes.c_uint8),
        ("Speed", ctypes.c_uint8),
        ("DeviceIsHub", ctypes.c_uint8),
        ("DeviceAddress", ctypes.c_uint16),
        ("NumberOfOpenPipes", ctypes.c_uint32),
        ("ConnectionStatus", ctypes.c_uint32),
    ]


if sys.platform == "win32":
    _cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    _setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _cfgmgr32.CM_Locate_DevNodeW.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    _cfgmgr32.CM_Locate_DevNodeW.restype = wintypes.ULONG
    _cfgmgr32.CM_Get_Parent.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _cfgmgr32.CM_Get_Parent.restype = wintypes.ULONG
    _cfgmgr32.CM_Get_Device_IDW.argtypes = [
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    _cfgmgr32.CM_Get_Device_IDW.restype = wintypes.ULONG
    _cfgmgr32.CM_Get_DevNode_Registry_PropertyW.argtypes = [
        wintypes.DWORD,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG,
    ]
    _cfgmgr32.CM_Get_DevNode_Registry_PropertyW.restype = wintypes.ULONG

    _setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    _setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    _setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
    ]
    _setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    _setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(_SP_DEVINFO_DATA),
    ]
    _setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    _setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVINFO_DATA),
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    _setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _kernel32.CreateFileW.restype = ctypes.c_void_p
    _kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.DeviceIoControl.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - 非 Windows 不提供该能力
    _cfgmgr32 = None
    _setupapi = None
    _kernel32 = None


def _locate(instance_id: str) -> int | None:
    if _cfgmgr32 is None or not instance_id:
        return None
    dev = wintypes.DWORD()
    if _cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(dev), instance_id, 0) != 0:
        return None
    return int(dev.value)


def _parent(dev: int) -> int | None:
    if _cfgmgr32 is None:
        return None
    parent = wintypes.DWORD()
    if _cfgmgr32.CM_Get_Parent(ctypes.byref(parent), dev, 0) != 0:
        return None
    return int(parent.value)


def _device_id(dev: int) -> str | None:
    if _cfgmgr32 is None:
        return None
    buf = ctypes.create_unicode_buffer(_MAX_DEVICE_ID)
    if _cfgmgr32.CM_Get_Device_IDW(dev, buf, _MAX_DEVICE_ID, 0) != 0:
        return None
    return buf.value or None


def _device_property(dev: int, prop: int) -> str | None:
    if _cfgmgr32 is None:
        return None
    buf = ctypes.create_unicode_buffer(512)
    size = wintypes.ULONG(ctypes.sizeof(buf))
    reg_type = wintypes.ULONG()
    code = _cfgmgr32.CM_Get_DevNode_Registry_PropertyW(
        dev, prop, ctypes.byref(reg_type), buf, ctypes.byref(size), 0
    )
    return buf.value if code == 0 and buf.value else None


def _device_desc(dev: int) -> str | None:
    """取设备的可读名：优先 FriendlyName，其次 DeviceDesc/HardwareID。"""
    for prop in (_CM_DRP_FRIENDLYNAME, _CM_DRP_DEVICEDESC, _CM_DRP_HARDWARE_ID):
        value = _device_property(dev, prop)
        if value:
            return value
    return None


def _driver_keys_for(instance_id: str) -> list[str]:
    """返回设备及父链的 driver key；复合接口借此关联到物理 USB 设备。"""
    keys: list[str] = []
    dev = _locate(instance_id)
    for _ in range(16):
        if dev is None:
            break
        key = _device_property(dev, _CM_DRP_DRIVER)
        if key and key.lower() not in keys:
            keys.append(key.lower())
        dev = _parent(dev)
    return keys


def _pci_id(instance_id: str | None) -> str | None:
    r"""``PCI\VEN_8086&DEV_A71E&...`` → ``8086:A71E``。"""
    if not instance_id:
        return None
    import re

    match = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", instance_id.upper())
    return f"{match.group(1)}:{match.group(2)}" if match else None


def usbpcap_root_hubs() -> list[dict[str, Any]]:
    r"""按 USBPcap 实例顺序返回 root hub：第 N 个对应 ``\\.\USBPcapN``。"""
    if sys.platform != "win32":
        return []
    import winreg

    entries: dict[int, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _USBPCAP_ENUM_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if str(name).isdigit() and isinstance(value, str):
                    entries[int(name)] = value
    except OSError:
        return []

    hubs: list[dict[str, Any]] = []
    for order, instance_id in enumerate(
        [entries[key] for key in sorted(entries)], start=1
    ):
        if not instance_id.upper().startswith(r"USB\ROOT_HUB"):
            continue
        controller = None
        parent_id = None
        dev = _locate(instance_id)
        if dev is not None:
            parent = _parent(dev)
            if parent is not None:
                parent_id = _device_id(parent)
                controller = _device_desc(parent) or parent_id
        index = len(hubs) + 1
        hubs.append(
            {
                "index": index,
                "device": rf"\\.\USBPcap{index}",
                "label": f"USBPcap{index}",
                "instanceId": instance_id,
                "controller": controller,
                "controllerId": _pci_id(parent_id),
                "order": order,
            }
        )
    return hubs


def root_hub_for(instance_id: str) -> str | None:
    r"""设备实例沿父链上溯到 ``USB\ROOT_HUB…``。"""
    dev = _locate(instance_id)
    for _ in range(16):
        if dev is None:
            return None
        current = _device_id(dev)
        if not current:
            return None
        if current.upper().startswith(r"USB\ROOT_HUB"):
            return current
        dev = _parent(dev)
    return None


def _setup_instance_id(hdev: int, info: _SP_DEVINFO_DATA) -> str:
    if _setupapi is None:
        return ""
    buf = ctypes.create_unicode_buffer(_MAX_DEVICE_ID)
    required = wintypes.DWORD()
    ok = _setupapi.SetupDiGetDeviceInstanceIdW(
        hdev, ctypes.byref(info), buf, _MAX_DEVICE_ID, ctypes.byref(required)
    )
    return buf.value if ok else ""


def _hub_interfaces() -> list[tuple[str, str]]:
    """返回所有当前 hub 的 ``(接口路径, PnP 实例 ID)``。"""
    if _setupapi is None:
        return []
    hdev = _setupapi.SetupDiGetClassDevsW(
        ctypes.byref(_GUID_DEVINTERFACE_USB_HUB),
        None,
        None,
        _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE,
    )
    if not hdev or hdev == _INVALID_HANDLE_VALUE:
        return []

    found: list[tuple[str, str]] = []
    try:
        index = 0
        while True:
            interface = _SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not _setupapi.SetupDiEnumDeviceInterfaces(
                hdev,
                None,
                ctypes.byref(_GUID_DEVINTERFACE_USB_HUB),
                index,
                ctypes.byref(interface),
            ):
                if ctypes.get_last_error() == _ERROR_NO_MORE_ITEMS:
                    break
                break
            index += 1

            required = wintypes.DWORD()
            _setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(interface), None, 0, ctypes.byref(required), None
            )
            if required.value < 8:
                continue
            detail = ctypes.create_string_buffer(required.value)
            detail_cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            ctypes.cast(detail, ctypes.POINTER(ctypes.c_uint32))[0] = detail_cb_size
            info = _SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
            if not _setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev,
                ctypes.byref(interface),
                detail,
                required.value,
                ctypes.byref(required),
                ctypes.byref(info),
            ):
                continue
            path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
            instance_id = _setup_instance_id(hdev, info)
            if path and instance_id:
                found.append((path, instance_id))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return found


def _open_hub(path: str) -> int | None:
    if _kernel32 is None:
        return None
    # 三个查询 IOCTL 都是 FILE_ANY_ACCESS；用 desiredAccess=0，避免不必要的提权。
    handle = _kernel32.CreateFileW(
        path,
        0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    return int(handle)


def _ioctl(handle: int, code: int, buffer: Any, size: int | None = None) -> bool:
    if _kernel32 is None:
        return False
    length = int(size if size is not None else ctypes.sizeof(buffer))
    returned = wintypes.DWORD()
    return bool(
        _kernel32.DeviceIoControl(
            handle,
            code,
            ctypes.byref(buffer),
            length,
            ctypes.byref(buffer),
            length,
            ctypes.byref(returned),
            None,
        )
    )


def _hub_port_count(handle: int) -> int:
    info = _USB_NODE_INFORMATION()
    info.NodeType = 0  # UsbHub
    if not _ioctl(handle, _IOCTL_USB_GET_NODE_INFORMATION, info):
        return 0
    return int(info.u.HubInformation.HubDescriptor.bNumberOfPorts)


def _connection_info(handle: int, port: int) -> dict[str, Any] | None:
    raw = (ctypes.c_uint8 * _MAX_HUB_BUFFER)()
    info = ctypes.cast(
        raw, ctypes.POINTER(_USB_NODE_CONNECTION_INFORMATION_EX)
    ).contents
    info.ConnectionIndex = int(port)
    if not _ioctl(
        handle, _IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, raw, len(raw)
    ):
        return None
    if info.ConnectionStatus != 1 or not info.DeviceAddress:  # DeviceConnected
        return None
    descriptor = info.DeviceDescriptor
    return {
        "port": int(port),
        "deviceAddress": int(info.DeviceAddress),
        "vid": int(descriptor.idVendor),
        "pid": int(descriptor.idProduct),
        "deviceIsHub": bool(info.DeviceIsHub),
    }


def _connection_driver_key(handle: int, port: int) -> str | None:
    raw = (ctypes.c_uint8 * _MAX_HUB_BUFFER)()
    header = ctypes.cast(raw, ctypes.POINTER(ctypes.c_uint32))
    header[0] = int(port)
    if not _ioctl(
        handle, _IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME, raw, len(raw)
    ):
        return None
    actual_length = min(int(header[1]), len(raw))
    if actual_length <= 8:
        return None
    text = bytes(raw[8:actual_length]).decode("utf-16-le", errors="ignore")
    return text.split("\0", 1)[0] or None


def usb_device_addresses(
    hubs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """直接从 Windows hub 读取当前 USB 总线地址，不启动抓包。

    返回一条物理端口记录；``driverKey`` 用于把复合设备的多个接口归到同一地址。
    单个 hub/端口查询失败会被忽略，调用方仍可展示其余设备。
    """
    if sys.platform != "win32" or _kernel32 is None:
        return []
    roots = hubs if hubs is not None else usbpcap_root_hubs()
    by_root = {str(hub["instanceId"]).lower(): hub for hub in roots}
    records: list[dict[str, Any]] = []

    for path, hub_instance_id in _hub_interfaces():
        root = root_hub_for(hub_instance_id)
        root_info = by_root.get((root or "").lower())
        if root_info is None:
            continue
        handle = _open_hub(path)
        if handle is None:
            continue
        try:
            for port in range(1, _hub_port_count(handle) + 1):
                info = _connection_info(handle, port)
                if info is None:
                    continue
                driver_key = _connection_driver_key(handle, port)
                records.append(
                    {
                        **info,
                        "bus": int(root_info["index"]),
                        "address": f"{int(root_info['index'])}.{info['deviceAddress']}",
                        "driverKey": driver_key,
                        "hubInstanceId": hub_instance_id,
                        "rootHub": root,
                    }
                )
        finally:
            _kernel32.CloseHandle(handle)
    return records


def _usb_id(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except ValueError:
        return None


def _address_sort_key(value: str) -> tuple[int, int]:
    try:
        bus, device = str(value).split(".", 1)
        return int(bus), int(device)
    except (TypeError, ValueError):
        return sys.maxsize, sys.maxsize


def annotate_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给 ``all-devices`` 补 root hub 与无需抓包即可读取的动态地址。"""
    hubs = usbpcap_root_hubs()
    by_instance = {hub["instanceId"].lower(): hub for hub in hubs}
    address_records = usb_device_addresses(hubs)
    by_driver: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in address_records:
        if record.get("driverKey"):
            by_driver[str(record["driverKey"]).lower()].append(record)

    for item in devices:
        instance_id = item.get("instanceId") or ""
        root = root_hub_for(instance_id)
        hub = by_instance.get((root or "").lower())
        item["rootHub"] = root
        item["hubIndex"] = hub["index"] if hub else None
        item["hubLabel"] = hub["label"] if hub else None
        item["hubDevice"] = hub["device"] if hub else None
        item["hubController"] = hub["controller"] if hub else None
        item["hubControllerId"] = hub.get("controllerId") if hub else None

        matches: list[dict[str, Any]] = []
        for driver_key in _driver_keys_for(instance_id):
            candidates = by_driver.get(driver_key, [])
            if root:
                candidates = [
                    record
                    for record in candidates
                    if str(record.get("rootHub") or "").lower() == root.lower()
                ]
            if candidates:
                # 父链越靠前越接近设备本身。找到第一层就停止，不能再把上游 hub
                # 自己的动态地址合并进来。
                matches = list(candidates)
                break

        # driver key 在异常枚举状态下可能取不到。仅当 root hub + VID/PID 能唯一定位时回退，
        # 避免把两个相同设备强行认成同一个地址。
        if not matches and root:
            vid, pid = _usb_id(item.get("vid")), _usb_id(item.get("pid"))
            candidates = [
                record
                for record in address_records
                if str(record.get("rootHub") or "").lower() == root.lower()
                and record.get("vid") == vid
                and record.get("pid") == pid
            ]
            if len(candidates) == 1:
                matches = candidates

        unique = {record["address"]: record for record in matches}
        addresses = sorted(unique, key=_address_sort_key)
        item["captureAddresses"] = addresses
        item["addressSource"] = "hub_ioctl" if addresses else None
        if len(addresses) == 1:
            record = unique[addresses[0]]
            item["usbBus"] = int(record["bus"])
            item["usbDeviceAddress"] = int(record["deviceAddress"])
        else:
            item["usbBus"] = None
            item["usbDeviceAddress"] = None
    return devices
