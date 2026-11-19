"""USB 设备 → root hub / USBPcapN 的拓扑映射（仅 Windows）。

- USBPcap 每挂一个 root hub 生成一个 ``\\\\.\\USBPcapN``；实例顺序记录在
  ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\USBPcap\\Enum``，第 N 个 root hub 即
  ``USBPcapN``（与 USBPcapCMD ``--extcap-interfaces`` 的输出顺序一致）。
- 设备归属用 CfgMgr32 的 ``CM_Get_Parent`` 逐级上溯到 ``USB\\ROOT_HUB`` 为止。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

__all__ = ["usbpcap_root_hubs", "root_hub_for", "annotate_devices"]

_MAX_DEVICE_ID = 512
_CM_DRP_DEVICEDESC = 0x00000000
_CM_DRP_HARDWARE_ID = 0x00000001
_CM_DRP_FRIENDLYNAME = 0x0000000C
_USBPCAP_ENUM_KEY = r"SYSTEM\CurrentControlSet\Services\USBPcap\Enum"

if sys.platform == "win32":
    _cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
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
else:  # pragma: no cover - 非 Windows 不提供该能力
    _cfgmgr32 = None


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


def _device_desc(dev: int) -> str | None:
    """取设备的可读名：优先 FriendlyName，其次 DeviceDesc。"""
    if _cfgmgr32 is None:
        return None
    for prop in (_CM_DRP_FRIENDLYNAME, _CM_DRP_DEVICEDESC, _CM_DRP_HARDWARE_ID):
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.ULONG(ctypes.sizeof(buf))
        reg_type = wintypes.ULONG()
        code = _cfgmgr32.CM_Get_DevNode_Registry_PropertyW(
            dev, prop, ctypes.byref(reg_type), buf, ctypes.byref(size), 0
        )
        if code == 0 and buf.value:
            return buf.value
    return None


def _pci_id(instance_id: str | None) -> str | None:
    """``PCI\VEN_8086&DEV_A71E&...`` → ``8086:A71E``。"""
    if not instance_id:
        return None
    import re

    match = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", instance_id.upper())
    return f"{match.group(1)}:{match.group(2)}" if match else None


def usbpcap_root_hubs() -> list[dict[str, Any]]:
    """按 USBPcap 实例顺序返回 root hub：第 N 个对应 ``\\\\.\\USBPcapN``。"""
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
        if not instance_id.upper().startswith("USB\\ROOT_HUB"):
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
                "device": f"\\\\.\\USBPcap{index}",
                "label": f"USBPcap{index}",
                "instanceId": instance_id,
                "controller": controller,
                "controllerId": _pci_id(parent_id),
                "order": order,
            }
        )
    return hubs


def root_hub_for(instance_id: str) -> str | None:
    """设备实例沿父链上溯到 ``USB\\ROOT_HUB…``。"""
    dev = _locate(instance_id)
    for _ in range(16):
        if dev is None:
            return None
        current = _device_id(dev)
        if not current:
            return None
        if current.upper().startswith("USB\\ROOT_HUB"):
            return current
        dev = _parent(dev)
    return None


def annotate_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给 `all-devices` 的每一项补 ``hub`` / ``hubLabel`` / ``hubController``。"""
    hubs = usbpcap_root_hubs()
    by_instance = {hub["instanceId"].lower(): hub for hub in hubs}
    for item in devices:
        root = root_hub_for(item.get("instanceId") or "")
        hub = by_instance.get((root or "").lower())
        item["rootHub"] = root
        item["hubIndex"] = hub["index"] if hub else None
        item["hubLabel"] = hub["label"] if hub else None
        item["hubDevice"] = hub["device"] if hub else None
        item["hubController"] = hub["controller"] if hub else None
        item["hubControllerId"] = hub.get("controllerId") if hub else None
    return devices
