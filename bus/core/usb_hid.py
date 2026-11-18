"""USB HID 设备访问（Windows ``hid.dll`` + SetupAPI）

与串口/ WinUSB 并列的第三种"请求"通道：HID 设备由系统 HID 驱动接管，用户态
通过 HID API 收发**报告**（Input/Output/Feature），发不了任意 SETUP。

能力：
  - 枚举：按 HID 接口 GUID 列出设备（VID/PID/产品名/序列号/报告长度）；
  - 打开：``CreateFile``（overlapped），读/写报告带超时；
  - Feature 报告：``HidD_GetFeature`` / ``HidD_SetFeature``。

约束：
  - 需要系统 HID 驱动（免安装）；设备被独占时打开失败；
  - 写报告的第一个字节是 Report ID（设备没有 Report ID 时为 0）；
  - 只在本机 Windows 可用，其它平台调用会抛 :class:`HidUnavailable`。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from typing import Any

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
ERROR_NO_MORE_ITEMS = 259
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
HIDP_STATUS_SUCCESS = 0x00110000
HID_MAX_REPORT = 8192


class HidUnavailable(RuntimeError):
    """当前平台不支持或 HID API 不可用。"""


class HidError(RuntimeError):
    """HID 调用失败。"""

    def __init__(self, message: str, code: int = 0) -> None:
        self.code = code
        super().__init__(message)


# ── 结构体 ────────────────────────────────────────────────

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("InterfaceClassGuid", GUID),
        ("Flags", ctypes.c_ulong),
        ("Reserved", ctypes.c_size_t),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_ulong),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_ulong),
        ("OffsetHigh", ctypes.c_ulong),
        ("hEvent", ctypes.c_void_p),
    ]


# ── 设备信息 ──────────────────────────────────────────────

@dataclass
class HidDevice:
    path: str
    vid: int
    pid: int
    version: int
    product: str
    manufacturer: str
    serial: str
    usage_page: int = 0
    usage: int = 0
    input_report_length: int = 0
    output_report_length: int = 0
    feature_report_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "vid": f"0x{self.vid:04X}",
            "pid": f"0x{self.pid:04X}",
            "version": f"0x{self.version:04X}",
            "product": self.product,
            "manufacturer": self.manufacturer,
            "serial": self.serial,
            "usagePage": f"0x{self.usage_page:04X}",
            "usage": f"0x{self.usage:04X}",
            "inputReportLength": self.input_report_length,
            "outputReportLength": self.output_report_length,
            "featureReportLength": self.feature_report_length,
        }


def build_report_buffer(report_id: int, data: bytes, length: int) -> bytes:
    """把载荷补齐到报告长度，首字节写 Report ID。

    ``length`` 取 ``OutputReportByteLength``（包含 Report ID 字节）。设备不需要
    Report ID 时调用方传 0，首字节仍保留为 0。
    """
    if length <= 0:
        length = len(data) + 1
    buffer = bytearray(length)
    buffer[0] = report_id & 0xFF
    payload = data[: max(0, length - 1)]
    buffer[1 : 1 + len(payload)] = payload
    return bytes(buffer)


# ── Windows API ───────────────────────────────────────────

class _WinApi:
    """延迟加载 DLL，避免非 Windows 导入即失败。"""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise HidUnavailable("HID 访问仅支持 Windows")
        self.hid = ctypes.WinDLL("hid", use_last_error=True)
        self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        self.hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
        self.hid.HidD_GetHidGuid.restype = None

        self.hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
        self.hid.HidD_GetAttributes.restype = ctypes.c_int

        for name in ("HidD_GetProductString", "HidD_GetManufacturerString", "HidD_GetSerialNumberString"):
            func = getattr(self.hid, name)
            func.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            func.restype = ctypes.c_int

        self.hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.hid.HidD_GetPreparsedData.restype = ctypes.c_int
        self.hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        self.hid.HidD_FreePreparsedData.restype = ctypes.c_int
        self.hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
        self.hid.HidP_GetCaps.restype = ctypes.c_long

        self.hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        self.hid.HidD_GetFeature.restype = ctypes.c_int
        self.hid.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        self.hid.HidD_SetFeature.restype = ctypes.c_int

        self.setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong
        ]
        self.setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        self.setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
            ctypes.c_ulong, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        self.setupapi.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        self.setupapi.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
        self.setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        self.setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int

        self.kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ]
        self.kernel32.CreateFileW.restype = ctypes.c_void_p
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_int
        self.kernel32.ReadFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(OVERLAPPED),
        ]
        self.kernel32.ReadFile.restype = ctypes.c_int
        self.kernel32.WriteFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(OVERLAPPED),
        ]
        self.kernel32.WriteFile.restype = ctypes.c_int
        self.kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p
        ]
        self.kernel32.CreateEventW.restype = ctypes.c_void_p
        self.kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        self.kernel32.GetOverlappedResult.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(ctypes.c_ulong), ctypes.c_int
        ]
        self.kernel32.GetOverlappedResult.restype = ctypes.c_int
        self.kernel32.CancelIo.argtypes = [ctypes.c_void_p]
        self.kernel32.CancelIo.restype = ctypes.c_int


_API: _WinApi | None = None


def _api() -> _WinApi:
    global _API
    if _API is None:
        _API = _WinApi()
    return _API


# ── 枚举 ──────────────────────────────────────────────────

def _wide(buf: ctypes.Array) -> str:
    return ctypes.wstring_at(ctypes.addressof(buf)).rstrip("\x00")


def _open_device(path: str, write: bool = True) -> int:
    api = _api()
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    handle = api.kernel32.CreateFileW(
        path,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OVERLAPPED,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        if write:
            # 只读设备：降级为 GENERIC_READ 再试一次
            return _open_device(path, write=False)
        raise HidError(f"打开 HID 设备失败（Win32 {code}）", code)
    return handle


def _read_strings(handle: int, device: HidDevice) -> None:
    api = _api()
    buffer = ctypes.create_unicode_buffer(256)
    if api.hid.HidD_GetProductString(handle, buffer, ctypes.sizeof(buffer)):
        device.product = buffer.value
    if api.hid.HidD_GetManufacturerString(handle, buffer, ctypes.sizeof(buffer)):
        device.manufacturer = buffer.value
    if api.hid.HidD_GetSerialNumberString(handle, buffer, ctypes.sizeof(buffer)):
        device.serial = buffer.value


def _read_caps(handle: int, device: HidDevice) -> None:
    api = _api()
    preparsed = ctypes.c_void_p()
    if not api.hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
        return
    try:
        caps = HIDP_CAPS()
        status = api.hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
        if status == HIDP_STATUS_SUCCESS:
            device.usage = caps.Usage
            device.usage_page = caps.UsagePage
            device.input_report_length = caps.InputReportByteLength
            device.output_report_length = caps.OutputReportByteLength
            device.feature_report_length = caps.FeatureReportByteLength
    finally:
        api.hid.HidD_FreePreparsedData(preparsed)


def enumerate_devices() -> list[HidDevice]:
    """枚举当前系统上的 HID 设备（含产品名/报告长度）。"""
    api = _api()
    guid = GUID()
    api.hid.HidD_GetHidGuid(ctypes.byref(guid))
    handle = api.setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise HidError("SetupDiGetClassDevs 失败", ctypes.get_last_error())

    devices: list[HidDevice] = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not api.setupapi.SetupDiEnumDeviceInterfaces(
                handle, None, ctypes.byref(guid), index, ctypes.byref(interface)
            ):
                code = ctypes.get_last_error()
                if code != ERROR_NO_MORE_ITEMS:
                    raise HidError("枚举 HID 接口失败", code)
                break
            index += 1
            required = ctypes.c_ulong(0)
            api.setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(interface), None, 0, ctypes.byref(required), None
            )
            if not required.value:
                continue
            buffer = ctypes.create_string_buffer(required.value)
            # x64 下结构体按 8 字节对齐，cbSize=8；x86 为 6
            cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ulong))[0] = cb_size
            if not api.setupapi.SetupDiGetDeviceInterfaceDetailW(
                handle, ctypes.byref(interface), buffer, required.value,
                ctypes.byref(required), None,
            ):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
            try:
                device = _describe(path)
            except HidError:
                continue
            if device is not None:
                devices.append(device)
    finally:
        api.setupapi.SetupDiDestroyDeviceInfoList(handle)
    return devices


def _describe(path: str) -> HidDevice | None:
    api = _api()
    handle = _open_device(path)
    try:
        attributes = HIDD_ATTRIBUTES()
        attributes.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if not api.hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
            return None
        device = HidDevice(
            path=path,
            vid=int(attributes.VendorID),
            pid=int(attributes.ProductID),
            version=int(attributes.VersionNumber),
            product="",
            manufacturer="",
            serial="",
        )
        _read_strings(handle, device)
        _read_caps(handle, device)
        return device
    finally:
        api.kernel32.CloseHandle(handle)


# ── 会话 ──────────────────────────────────────────────────

@dataclass
class HidSession:
    device: HidDevice
    handle: int = 0
    timeout_ms: int = 1000
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if self.handle and not self._closed:
            _api().kernel32.CloseHandle(self.handle)
            self._closed = True
            self.handle = 0

    # ── 报告收发 ──────────────────────────────────────────
    def read(self, length: int | None = None, timeout_ms: int | None = None) -> bytes:
        api = _api()
        size = int(length or self.device.input_report_length or 64)
        buffer = (ctypes.c_ubyte * size)()
        overlapped, event = _overlapped(api)
        read = ctypes.c_ulong(0)
        try:
            ok = api.kernel32.ReadFile(
                self.handle, buffer, size, ctypes.byref(read), ctypes.byref(overlapped)
            )
            _await(api, self.handle, overlapped, event, ok, read, timeout_ms or self.timeout_ms, "读")
            return bytes(buffer[: read.value])
        finally:
            api.kernel32.CloseHandle(event)

    def write(
        self,
        data: bytes,
        report_id: int = 0,
        timeout_ms: int | None = None,
    ) -> int:
        api = _api()
        length = int(self.device.output_report_length or len(data) + 1)
        payload = build_report_buffer(report_id, data, length)
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        overlapped, event = _overlapped(api)
        written = ctypes.c_ulong(0)
        try:
            ok = api.kernel32.WriteFile(
                self.handle, buffer, len(payload), ctypes.byref(written), ctypes.byref(overlapped)
            )
            _await(api, self.handle, overlapped, event, ok, written, timeout_ms or self.timeout_ms, "写")
            return int(written.value or len(payload))
        finally:
            api.kernel32.CloseHandle(event)

    def get_feature(self, report_id: int = 0) -> bytes:
        api = _api()
        length = int(self.device.feature_report_length or 65)
        buffer = (ctypes.c_ubyte * length)()
        buffer[0] = report_id & 0xFF
        if not api.hid.HidD_GetFeature(self.handle, buffer, length):
            raise HidError("HidD_GetFeature 失败", ctypes.get_last_error())
        return bytes(buffer)

    def set_feature(self, data: bytes, report_id: int = 0) -> None:
        api = _api()
        length = int(self.device.feature_report_length or len(data) + 1)
        payload = build_report_buffer(report_id, data, length)
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        if not api.hid.HidD_SetFeature(self.handle, buffer, len(payload)):
            raise HidError("HidD_SetFeature 失败", ctypes.get_last_error())


def _overlapped(api: _WinApi) -> tuple[OVERLAPPED, int]:
    event = api.kernel32.CreateEventW(None, True, False, None)
    if not event:
        raise HidError("CreateEvent 失败", ctypes.get_last_error())
    overlapped = OVERLAPPED()
    overlapped.hEvent = event
    return overlapped, event


def _await(
    api: _WinApi,
    handle: int,
    overlapped: OVERLAPPED,
    event: int,
    ok: int,
    counter: ctypes.Array,
    timeout_ms: int,
    action: str,
) -> None:
    if ok:
        return
    code = ctypes.get_last_error()
    if code != ERROR_IO_PENDING:
        raise HidError(f"HID {action}失败（Win32 {code}）", code)
    wait = api.kernel32.WaitForSingleObject(event, max(1, int(timeout_ms)))
    if wait != WAIT_OBJECT_0:
        api.kernel32.CancelIo(handle)
        raise HidError(f"HID {action}超时（{timeout_ms} ms）", 1460)
    if not api.kernel32.GetOverlappedResult(
        handle, ctypes.byref(overlapped), ctypes.byref(counter), False
    ):
        raise HidError(f"HID {action}失败", ctypes.get_last_error())


class HidManager:
    """按 ``id`` 管理多个 HID 会话（与串口/网络会话模型一致）。"""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._sessions: dict[str, HidSession] = {}

    def devices(self) -> list[dict]:
        return [item.to_dict() for item in enumerate_devices()]

    def open(
        self,
        session_id: str,
        *,
        vid: int,
        pid: int,
        serial: str | None = None,
        path: str | None = None,
        timeout_ms: int = 1000,
    ) -> dict:
        try:
            devices = enumerate_devices()
        except HidUnavailable as exc:
            return {"success": False, "code": "hid_unavailable", "error": str(exc)}
        target = None
        for item in devices:
            if path and item.path != path:
                continue
            if item.vid != int(vid) or item.pid != int(pid):
                continue
            if serial and item.serial != serial:
                continue
            target = item
            break
        if target is None:
            return {
                "success": False,
                "code": "hid_not_found",
                "error": f"未找到 HID 设备 {vid:04X}:{pid:04X}"
                + (f" serial={serial}" if serial else ""),
            }
        try:
            handle = _open_device(target.path)
        except HidError as exc:
            return {"success": False, "code": "hid_busy", "error": str(exc)}
        with self._lock:
            old = self._sessions.pop(session_id, None)
            if old is not None:
                old.close()
            self._sessions[session_id] = HidSession(
                device=target, handle=handle, timeout_ms=max(1, int(timeout_ms))
            )
        return {"success": True, "device": target.to_dict(), "id": session_id}

    def _get(self, session_id: str) -> HidSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def close(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()
        return {"success": True, "id": session_id}

    def status(self, session_id: str) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": True, "connected": False, "id": session_id}
        return {
            "success": True,
            "connected": True,
            "id": session_id,
            "device": session.device.to_dict(),
            "timeoutMs": session.timeout_ms,
        }

    def read(
        self,
        session_id: str,
        *,
        length: int | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"HID 会话 {session_id!r} 未打开"}
        try:
            data = session.read(length, timeout_ms)
        except HidError as exc:
            return {"success": False, "code": "hid_read_failed", "error": str(exc)}
        return {
            "success": True,
            "dataHex": data.hex(" ").upper(),
            "length": len(data),
        }

    def write(
        self,
        session_id: str,
        data: bytes,
        *,
        report_id: int = 0,
        timeout_ms: int | None = None,
    ) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"HID 会话 {session_id!r} 未打开"}
        try:
            written = session.write(data, report_id, timeout_ms)
        except HidError as exc:
            return {"success": False, "code": "hid_write_failed", "error": str(exc)}
        return {"success": True, "bytesWritten": written}

    def get_feature(self, session_id: str, report_id: int = 0) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"HID 会话 {session_id!r} 未打开"}
        try:
            data = session.get_feature(report_id)
        except HidError as exc:
            return {"success": False, "code": "hid_feature_failed", "error": str(exc)}
        return {"success": True, "dataHex": data.hex(" ").upper(), "length": len(data)}

    def set_feature(self, session_id: str, data: bytes, report_id: int = 0) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"HID 会话 {session_id!r} 未打开"}
        try:
            session.set_feature(data, report_id)
        except HidError as exc:
            return {"success": False, "code": "hid_feature_failed", "error": str(exc)}
        return {"success": True}


def helper_sizeof() -> dict[str, int]:
    return {
        "GUID": ctypes.sizeof(GUID),
        "HIDP_CAPS": ctypes.sizeof(HIDP_CAPS),
        "SP_DEVICE_INTERFACE_DATA": ctypes.sizeof(SP_DEVICE_INTERFACE_DATA),
        "OVERLAPPED": ctypes.sizeof(OVERLAPPED),
    }


if sys.platform != "win32":  # pragma: no cover
    pass
