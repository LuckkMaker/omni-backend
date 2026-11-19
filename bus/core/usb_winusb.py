"""WinUSB 主机端访问（Windows ``winusb.dll`` + SetupAPI）

第三种"请求"通道，也是唯一能发**任意控制传输**、且带宽最高的通道：
设备接口必须绑定 WinUSB（固件带 MS OS 2.0 描述符，或用 INF 绑定）。

能力与 ``omni-emb/utilities/winusb_test`` 对齐：
  - 按设备接口 GUID 枚举（默认用本仓示例的 GUID，可传多个）；
  - 打开接口、查询速度/接口/端点，控制传输、批量/中断读写；
  - 管道 abort/flush/reset、超时策略、回环吞吐（echo，含 pipeline 模式）。

限制：
  - WinUSB 不支持等时传输；
  - 接口是独占的：被 Keil/厂商工具打开时这里会失败；
  - 仅 Windows。
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ── 常量 ──────────────────────────────────────────────────

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
ERROR_NO_MORE_ITEMS = 259
ERROR_IO_PENDING = 997
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: WinUsb_Initialize 要求接口句柄以 overlapped 方式打开（否则返回 ERROR_INVALID_HANDLE）
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_OVERLAPPED = 0x40000000

DEVICE_SPEED = 0x01
PIPE_TRANSFER_TIMEOUT = 0x03
USB_DEVICE_DESCRIPTOR_TYPE = 0x01

PIPE_TYPES = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}
SPEED_NAMES = {1: "full", 3: "high"}

#: 本仓示例（usbd_winusb）与 Keil WinUSB_Echo 的接口 GUID
DEFAULT_GUIDS = [
    "{12345678-1234-5678-1234-567812345678}",
    "{7D9ADCFC-E570-4B38-BF4E-8F81F68964E0}",
]

MAX_TRANSFER = 4 * 1024 * 1024


class WinUsbUnavailable(RuntimeError):
    """非 Windows 或 winusb.dll 不可用。"""


class WinUsbError(RuntimeError):
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


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", GUID),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.c_size_t),
    ]


#: SetupDiGetDeviceRegistryProperty 的属性编号
SPDRP_DEVICEDESC = 0x00
SPDRP_SERVICE = 0x04
SPDRP_FRIENDLYNAME = 0x0C
DIGCF_ALLCLASSES = 0x04


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bInterfaceNumber", ctypes.c_ubyte),
        ("bAlternateSetting", ctypes.c_ubyte),
        ("bNumEndpoints", ctypes.c_ubyte),
        ("bInterfaceClass", ctypes.c_ubyte),
        ("bInterfaceSubClass", ctypes.c_ubyte),
        ("bInterfaceProtocol", ctypes.c_ubyte),
        ("iInterface", ctypes.c_ubyte),
    ]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PipeType", ctypes.c_ulong),
        ("PipeId", ctypes.c_ubyte),
        ("MaximumPacketSize", ctypes.c_ushort),
        ("Interval", ctypes.c_ubyte),
    ]


class WINUSB_SETUP_PACKET(ctypes.Structure):
    _fields_ = [
        ("RequestType", ctypes.c_ubyte),
        ("Request", ctypes.c_ubyte),
        ("Value", ctypes.c_ushort),
        ("Index", ctypes.c_ushort),
        ("Length", ctypes.c_ushort),
    ]


def guid_from_string(text: str) -> GUID:
    try:
        parsed = uuid.UUID(str(text).strip("{}"))
    except ValueError:
        raise WinUsbError(f"无效的 GUID：{text}") from None
    return GUID.from_buffer_copy(bytes(parsed.bytes_le))


# ── 数据模型 ──────────────────────────────────────────────

@dataclass
class WinUsbEndpoint:
    address: int
    transfer_type: str
    max_packet_size: int
    interval: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": f"0x{self.address:02X}",
            "direction": "in" if self.address & 0x80 else "out",
            "type": self.transfer_type,
            "maxPacketSize": self.max_packet_size,
            "interval": self.interval,
        }


@dataclass
class WinUsbDevice:
    path: str
    guid: str
    vid: int
    pid: int
    bcd_usb: int
    bcd_device: int
    speed: str
    interface_number: int
    interface_class: int
    interface_subclass: int
    interface_protocol: int
    endpoints: list[WinUsbEndpoint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "guid": self.guid,
            "vid": f"0x{self.vid:04X}",
            "pid": f"0x{self.pid:04X}",
            "bcdUSB": f"0x{self.bcd_usb:04X}",
            "bcdDevice": f"0x{self.bcd_device:04X}",
            "speed": self.speed,
            "interfaceNumber": self.interface_number,
            "class": f"0x{self.interface_class:02X}",
            "subClass": self.interface_subclass,
            "protocol": self.interface_protocol,
            "endpoints": [item.to_dict() for item in self.endpoints],
        }

    def find_pipe(self, address: int) -> WinUsbEndpoint | None:
        for endpoint in self.endpoints:
            if endpoint.address == address:
                return endpoint
        return None


# ── DLL 绑定 ──────────────────────────────────────────────

class WinUsbLibrary:
    """``winusb.dll`` / ``setupapi`` / ``kernel32`` 的薄封装（可注入假对象测试）。"""

    def __init__(self, *, library: Any = None) -> None:
        if library is not None:
            self.winusb = library
            self.setupapi = None
            self.kernel32 = None
            return
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise WinUsbUnavailable("WinUSB 访问仅支持 Windows")
        self.winusb = ctypes.WinDLL("winusb", use_last_error=True)
        self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        w = self.winusb
        w.WinUsb_Initialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        w.WinUsb_Initialize.restype = ctypes.c_int
        w.WinUsb_Free.argtypes = [ctypes.c_void_p]
        w.WinUsb_Free.restype = ctypes.c_int
        w.WinUsb_QueryInterfaceSettings.argtypes = [
            ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)
        ]
        w.WinUsb_QueryInterfaceSettings.restype = ctypes.c_int
        w.WinUsb_QueryPipe.argtypes = [
            ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
            ctypes.POINTER(WINUSB_PIPE_INFORMATION),
        ]
        w.WinUsb_QueryPipe.restype = ctypes.c_int
        w.WinUsb_ControlTransfer.argtypes = [
            ctypes.c_void_p, WINUSB_SETUP_PACKET, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        w.WinUsb_ControlTransfer.restype = ctypes.c_int
        for name in ("WinUsb_ReadPipe", "WinUsb_WritePipe"):
            func = getattr(w, name)
            func.argtypes = [
                ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
            ]
            func.restype = ctypes.c_int
        for name in ("WinUsb_AbortPipe", "WinUsb_FlushPipe", "WinUsb_ResetPipe"):
            func = getattr(w, name)
            func.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
            func.restype = ctypes.c_int
        w.WinUsb_SetPipePolicy.argtypes = [
            ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p
        ]
        w.WinUsb_SetPipePolicy.restype = ctypes.c_int
        w.WinUsb_GetDescriptor.argtypes = [
            ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ushort,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ]
        w.WinUsb_GetDescriptor.restype = ctypes.c_int
        w.WinUsb_QueryDeviceInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p
        ]
        w.WinUsb_QueryDeviceInformation.restype = ctypes.c_int

        s = self.setupapi
        s.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong
        ]
        s.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        s.SetupDiEnumDeviceInterfaces.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
            ctypes.c_ulong, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        s.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
        s.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        s.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
        s.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        s.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int
        s.SetupDiEnumDeviceInfo.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(SP_DEVINFO_DATA)
        ]
        s.SetupDiEnumDeviceInfo.restype = ctypes.c_int
        s.SetupDiGetDeviceInstanceIdW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.c_wchar_p,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ]
        s.SetupDiGetDeviceInstanceIdW.restype = ctypes.c_int
        s.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        s.SetupDiGetDeviceRegistryPropertyW.restype = ctypes.c_int

        k = self.kernel32
        k.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ]
        k.CreateFileW.restype = ctypes.c_void_p
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = ctypes.c_int

    # ── 句柄 ──────────────────────────────────────────────
    def open_file(self, path: str) -> int:
        handle = self.kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            raise WinUsbError(_explain_open(code), code)
        return handle

    def initialize(self, file_handle: int) -> int:
        winusb = ctypes.c_void_p()
        if not self.winusb.WinUsb_Initialize(file_handle, ctypes.byref(winusb)):
            raise WinUsbError("WinUsb_Initialize 失败（设备未绑定 WinUSB 驱动？）", ctypes.get_last_error())
        return winusb.value

    def close(self, file_handle: int, winusb_handle: int | None) -> None:
        if winusb_handle:
            self.winusb.WinUsb_Free(winusb_handle)
        if file_handle:
            self.kernel32.CloseHandle(file_handle)


def _explain_open(code: int) -> str:
    if code == 5:
        return "打开设备接口被拒绝（需要管理员/驱动未绑定）"
    if code in (2, 3):
        return "设备接口不存在（设备已拔出或驱动未绑定）"
    return f"打开设备接口失败（Win32 {code}）"


# ── 枚举 ──────────────────────────────────────────────────

def _path_from_detail(buffer: ctypes.Array) -> str:
    return ctypes.wstring_at(ctypes.addressof(buffer) + 4)


def parse_instance_id(text: str) -> dict[str, Any]:
    """``USB\\VID_C251&PID_0002\\serial`` → ``{vid, pid, serial}``。

    复合设备的 ``&MI_00`` 会被忽略；解析失败时对应字段为 None。
    """
    import re

    match = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", text or "")
    vid = int(match.group(1), 16) if match else None
    pid = int(match.group(2), 16) if match else None
    parts = (text or "").split("\\")
    serial = parts[-1] if len(parts) >= 3 and parts[-1] else None
    return {"vid": vid, "pid": pid, "serial": serial}


def _registry_string(setupapi, hdev: int, info: SP_DEVINFO_DATA, prop: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    required = ctypes.c_ulong(0)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev,
        ctypes.byref(info),
        prop,
        None,
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.sizeof(buffer),
        ctypes.byref(required),
    )
    return buffer.value if ok else ""


def _instance_id(setupapi, hdev: int, info: SP_DEVINFO_DATA) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    required = ctypes.c_ulong(0)
    ok = setupapi.SetupDiGetDeviceInstanceIdW(
        hdev, ctypes.byref(info), buffer, ctypes.sizeof(buffer), ctypes.byref(required)
    )
    return buffer.value if ok else ""


def list_all_usb_devices(
    library: WinUsbLibrary | None = None, service: str | None = None
) -> dict[str, Any]:
    """列出总线上所有 USB 设备及其驱动服务（``WINUSB``/``usbser``/``HidUsb``…）。

    这是 ``winusb_test.py list --all`` 的等价能力：用于判断"设备没枚举"还是
    "枚举了但驱动没绑定"。
    """
    lib = library or WinUsbLibrary()
    if lib.setupapi is None:
        raise WinUsbUnavailable("注入模式下不支持枚举")
    hdev = lib.setupapi.SetupDiGetClassDevsW(
        None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES
    )
    if not hdev or hdev == INVALID_HANDLE_VALUE:
        raise WinUsbError("SetupDiGetClassDevs 失败", ctypes.get_last_error())

    devices: list[dict[str, Any]] = []
    try:
        index = 0
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not lib.setupapi.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(info)):
                break
            index += 1
            instance = _instance_id(lib.setupapi, hdev, info)
            if not instance:
                continue
            name = _registry_string(lib.setupapi, hdev, info, SPDRP_SERVICE)
            if service and name.lower() != service.lower():
                continue
            description = _registry_string(
                lib.setupapi, hdev, info, SPDRP_FRIENDLYNAME
            ) or _registry_string(lib.setupapi, hdev, info, SPDRP_DEVICEDESC)
            parsed = parse_instance_id(instance)
            devices.append(
                {
                    "instanceId": instance,
                    "description": description,
                    "service": name or "-",
                    "vid": f"0x{parsed['vid']:04X}" if parsed["vid"] is not None else None,
                    "pid": f"0x{parsed['pid']:04X}" if parsed["pid"] is not None else None,
                    "serial": parsed["serial"],
                }
            )
    finally:
        lib.setupapi.SetupDiDestroyDeviceInfoList(hdev)
    try:
        from core.usb_topology import annotate_devices

        annotate_devices(devices)
    except Exception:
        pass
    return {"success": True, "devices": devices}


def _describe(lib: WinUsbLibrary, path: str, guid: str) -> WinUsbDevice:
    file_handle = lib.open_file(path)
    winusb_handle = None
    try:
        winusb_handle = lib.initialize(file_handle)
        descriptor = bytearray(18)
        transferred = ctypes.c_ulong(0)
        buffer = (ctypes.c_ubyte * 18).from_buffer(descriptor)
        lib.winusb.WinUsb_GetDescriptor(
            winusb_handle, USB_DEVICE_DESCRIPTOR_TYPE, 0, 0x0409, buffer, 18,
            ctypes.byref(transferred),
        )
        if len(descriptor) >= 18 and descriptor[1] == 1:
            (
                _length, _type, bcd_usb, _cls, _sub, _proto, _mps,
                vid, pid, bcd_device, _im, _ip, _isn, _n,
            ) = struct.unpack("<BBHBBBBHHHBBBB", bytes(descriptor))
        else:
            bcd_usb = vid = pid = bcd_device = 0

        speed_length = ctypes.c_ulong(1)
        speed_byte = ctypes.c_ubyte(0)
        speed = "unknown"
        if lib.winusb.WinUsb_QueryDeviceInformation(
            winusb_handle, DEVICE_SPEED, ctypes.byref(speed_length), ctypes.byref(speed_byte)
        ):
            speed = SPEED_NAMES.get(int(speed_byte.value), f"unknown({speed_byte.value})")

        interface = USB_INTERFACE_DESCRIPTOR()
        if not lib.winusb.WinUsb_QueryInterfaceSettings(
            winusb_handle, 0, ctypes.byref(interface)
        ):
            raise WinUsbError("WinUsb_QueryInterfaceSettings 失败")

        endpoints: list[WinUsbEndpoint] = []
        for index in range(int(interface.bNumEndpoints)):
            pipe = WINUSB_PIPE_INFORMATION()
            if not lib.winusb.WinUsb_QueryPipe(
                winusb_handle, 0, index, ctypes.byref(pipe)
            ):
                break
            endpoints.append(
                WinUsbEndpoint(
                    address=int(pipe.PipeId),
                    transfer_type=PIPE_TYPES.get(int(pipe.PipeType), "unknown"),
                    max_packet_size=int(pipe.MaximumPacketSize),
                    interval=int(pipe.Interval),
                )
            )

        return WinUsbDevice(
            path=path,
            guid=guid,
            vid=vid,
            pid=pid,
            bcd_usb=bcd_usb,
            bcd_device=bcd_device,
            speed=speed,
            interface_number=int(interface.bInterfaceNumber),
            interface_class=int(interface.bInterfaceClass),
            interface_subclass=int(interface.bInterfaceSubClass),
            interface_protocol=int(interface.bInterfaceProtocol),
            endpoints=endpoints,
        )
    finally:
        lib.close(file_handle, winusb_handle)


def enumerate_devices(
    lib: WinUsbLibrary | None = None,
    guids: list[str] | None = None,
) -> list[WinUsbDevice]:
    """按接口 GUID 枚举 WinUSB 设备（每个接口算一条）。"""
    library = lib or WinUsbLibrary()
    if library.setupapi is None:
        raise WinUsbUnavailable("注入模式下不支持枚举")

    devices: list[WinUsbDevice] = []
    for text in guids or DEFAULT_GUIDS:
        guid = guid_from_string(text)
        handle = library.setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            continue
        try:
            index = 0
            while True:
                interface = SP_DEVICE_INTERFACE_DATA()
                interface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
                if not library.setupapi.SetupDiEnumDeviceInterfaces(
                    handle, None, ctypes.byref(guid), index, ctypes.byref(interface)
                ):
                    break
                index += 1
                required = ctypes.c_ulong(0)
                library.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    handle, ctypes.byref(interface), None, 0, ctypes.byref(required), None
                )
                if not required.value:
                    continue
                buffer = ctypes.create_string_buffer(required.value)
                cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ulong))[0] = cb_size
                if not library.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    handle, ctypes.byref(interface), buffer, required.value,
                    ctypes.byref(required), None,
                ):
                    continue
                path = _path_from_detail(buffer)
                try:
                    devices.append(_describe(library, path, text))
                except WinUsbError:
                    continue
        finally:
            library.setupapi.SetupDiDestroyDeviceInfoList(handle)
    return devices


# ── 会话 ──────────────────────────────────────────────────

class WinUsbSession:
    def __init__(
        self,
        device: WinUsbDevice,
        file_handle: int,
        winusb_handle: int,
        library: WinUsbLibrary,
        timeout_ms: int = 1000,
    ) -> None:
        self.device = device
        self._file = file_handle
        self._winusb = winusb_handle
        self._lib = library
        self.timeout_ms = max(1, int(timeout_ms))
        self._timeouts: dict[int, int] = {}

    def close(self) -> None:
        if self._winusb or self._file:
            self._lib.close(self._file, self._winusb)
            self._file = 0
            self._winusb = 0

    # ── 传输 ──────────────────────────────────────────────
    def _apply_timeout(self, pipe: int) -> None:
        if self._timeouts.get(pipe) == self.timeout_ms:
            return
        value = ctypes.c_ulong(self.timeout_ms)
        self._lib.winusb.WinUsb_SetPipePolicy(
            self._winusb, pipe, PIPE_TRANSFER_TIMEOUT, ctypes.sizeof(value), ctypes.byref(value)
        )
        self._timeouts[pipe] = self.timeout_ms

    def control(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        length: int,
        data: bytes = b"",
    ) -> bytes:
        self._apply_timeout(0)
        setup = WINUSB_SETUP_PACKET(
            request_type, request, value & 0xFFFF, index & 0xFFFF, length & 0xFFFF
        )
        direction_in = bool(request_type & 0x80)
        if direction_in:
            buffer = (ctypes.c_ubyte * max(1, length))()
            transfer_length = length
            buffer_arg = ctypes.cast(buffer, ctypes.c_void_p) if length else None
        else:
            payload = data[:length] if length else data
            buffer = (ctypes.c_ubyte * max(1, len(payload))).from_buffer_copy(payload or b"\x00")
            transfer_length = len(payload)
            buffer_arg = ctypes.cast(buffer, ctypes.c_void_p) if transfer_length else None
        transferred = ctypes.c_ulong(0)
        if not self._lib.winusb.WinUsb_ControlTransfer(
            self._winusb, setup, buffer_arg, transfer_length,
            ctypes.byref(transferred), None,
        ):
            raise WinUsbError("WinUsb_ControlTransfer 失败", ctypes.get_last_error())
        if direction_in:
            return bytes(buffer[: transferred.value])
        return b""

    def read(self, pipe: int, length: int) -> bytes:
        if length <= 0 or length > MAX_TRANSFER:
            raise WinUsbError(f"读取长度非法：{length}")
        self._apply_timeout(pipe)
        buffer = (ctypes.c_ubyte * length)()
        transferred = ctypes.c_ulong(0)
        if not self._lib.winusb.WinUsb_ReadPipe(
            self._winusb, pipe, buffer, length, ctypes.byref(transferred), None
        ):
            raise WinUsbError("WinUsb_ReadPipe 失败（超时或管道 STALL）", ctypes.get_last_error())
        return bytes(buffer[: transferred.value])

    def write(self, pipe: int, data: bytes) -> int:
        if len(data) > MAX_TRANSFER:
            raise WinUsbError(f"写入长度超过上限：{len(data)}")
        self._apply_timeout(pipe)
        buffer = (ctypes.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\x00")
        transferred = ctypes.c_ulong(0)
        if not self._lib.winusb.WinUsb_WritePipe(
            self._winusb, pipe, buffer, len(data), ctypes.byref(transferred), None
        ):
            raise WinUsbError("WinUsb_WritePipe 失败（超时或管道 STALL）", ctypes.get_last_error())
        return int(transferred.value)

    def pipe(self, pipe: int, action: str) -> bool:
        function = {
            "abort": self._lib.winusb.WinUsb_AbortPipe,
            "flush": self._lib.winusb.WinUsb_FlushPipe,
            "reset": self._lib.winusb.WinUsb_ResetPipe,
        }[action]
        return bool(function(self._winusb, pipe))

    # ── 回环吞吐 ──────────────────────────────────────────
    def echo(
        self,
        out_pipe: int,
        in_pipe: int,
        *,
        size: int,
        count: int,
        pipeline: int = 1,
        pattern: str = "increment",
        seed: int = 0,
    ) -> dict[str, Any]:
        payload = build_pattern(pattern, size, seed)
        pipeline = max(1, int(pipeline))
        chunk = max(1, int(self.device.find_pipe(out_pipe).max_packet_size if self.device.find_pipe(out_pipe) else size))
        started = time.perf_counter()
        iterations = 0
        mismatches = 0
        bytes_each_way = 0
        first_error = None
        while count == 0 or iterations < count:
            iterations += 1
            expected = payload
            for _ in range(pipeline):
                for offset in range(0, size, chunk):
                    self.write(out_pipe, expected[offset : offset + chunk])
            received = bytearray()
            for _ in range(pipeline):
                need = size
                while need > 0:
                    part = self.read(in_pipe, min(need, chunk if chunk else need))
                    if not part:
                        raise WinUsbError("设备停止了回显（读到 0 字节）")
                    received.extend(part)
                    need -= len(part)
            bytes_each_way += size * pipeline
            if bytes(received) != expected * pipeline:
                mismatches += 1
                if first_error is None:
                    first_error = f"第 {iterations} 次回环数据不一致"
        elapsed = time.perf_counter() - started
        return {
            "iterations": iterations,
            "size": size,
            "pipeline": pipeline,
            "bytesEachWay": bytes_each_way,
            "durationMs": round(elapsed * 1000, 1),
            "throughputBps": round(bytes_each_way / elapsed, 1) if elapsed > 0 else 0,
            "throughputKiBs": round(bytes_each_way / elapsed / 1024, 2) if elapsed > 0 else 0,
            "mismatches": mismatches,
            "firstError": first_error,
        }


def build_pattern(kind: str, size: int, seed: int = 0) -> bytes:
    size = max(1, int(size))
    if kind == "increment":
        return bytes(index & 0xFF for index in range(size))
    if kind == "zeros":
        return b"\x00" * size
    if kind == "aa":
        return b"\xAA" * size
    if kind == "ff":
        return b"\xFF" * size
    if kind == "random":
        import random

        rng = random.Random(seed)
        return bytes(rng.randrange(256) for _ in range(size))
    raise WinUsbError(f"未知图样：{kind}")


# ── 管理器 ────────────────────────────────────────────────

class WinUsbManager:
    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._sessions: dict[str, WinUsbSession] = {}
        self._lib: WinUsbLibrary | None = None

    def _library(self) -> WinUsbLibrary:
        if self._lib is None:
            self._lib = WinUsbLibrary()
        return self._lib

    def devices(self, guids: list[str] | None = None) -> dict:
        try:
            found = enumerate_devices(self._library(), guids)
        except WinUsbUnavailable as exc:
            return {"success": False, "code": "winusb_unavailable", "error": str(exc)}
        return {"success": True, "devices": [item.to_dict() for item in found]}

    def all_devices(self, service: str | None = None) -> dict:
        """所有 USB 设备 + 驱动服务名（分诊用）"""
        try:
            return list_all_usb_devices(self._library(), service)
        except WinUsbUnavailable as exc:
            return {"success": False, "code": "winusb_unavailable", "error": str(exc)}
        except WinUsbError as exc:
            return {"success": False, "code": "winusb_enum_failed", "error": str(exc)}

    def open(
        self,
        session_id: str,
        *,
        path: str | None = None,
        vid: int | None = None,
        pid: int | None = None,
        interface: int = 0,
        timeout_ms: int = 1000,
        guids: list[str] | None = None,
    ) -> dict:
        try:
            library = self._library()
            candidates = enumerate_devices(library, guids)
        except WinUsbUnavailable as exc:
            return {"success": False, "code": "winusb_unavailable", "error": str(exc)}
        target = None
        for item in candidates:
            if path and item.path != path:
                continue
            if vid is not None and item.vid != int(vid):
                continue
            if pid is not None and item.pid != int(pid):
                continue
            if item.interface_number != int(interface):
                continue
            target = item
            break
        if target is None:
            if path:
                target = WinUsbDevice(
                    path=path, guid="", vid=0, pid=0, bcd_usb=0, bcd_device=0,
                    speed="unknown", interface_number=int(interface), interface_class=0,
                    interface_subclass=0, interface_protocol=0,
                )
            else:
                return {
                    "success": False,
                    "code": "winusb_not_found",
                    "error": "未找到匹配的 WinUSB 接口：确认接口 GUID 与固件一致、驱动已绑定",
                }
        try:
            file_handle = library.open_file(target.path)
            winusb_handle = library.initialize(file_handle)
        except (WinUsbError, OSError) as exc:
            return {"success": False, "code": "winusb_open_failed", "error": str(exc)}
        session = WinUsbSession(target, file_handle, winusb_handle, library, timeout_ms)
        with self._lock:
            old = self._sessions.pop(session_id, None)
            if old is not None:
                old.close()
            self._sessions[session_id] = session
        return {"success": True, "id": session_id, "device": target.to_dict()}

    def _get(self, session_id: str) -> WinUsbSession | None:
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

    def control(self, session_id: str, **kwargs) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"WinUSB 会话 {session_id!r} 未打开"}
        try:
            data = session.control(
                kwargs["request_type"], kwargs["request"], kwargs["value"],
                kwargs["index"], kwargs["length"], kwargs.get("data", b""),
            )
        except WinUsbError as exc:
            return {"success": False, "code": "winusb_transfer_failed", "error": str(exc)}
        return {"success": True, "dataHex": data.hex(" ").upper(), "length": len(data)}

    def read(self, session_id: str, pipe: int, length: int) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"WinUSB 会话 {session_id!r} 未打开"}
        try:
            data = session.read(pipe, length)
        except WinUsbError as exc:
            return {"success": False, "code": "winusb_transfer_failed", "error": str(exc)}
        return {"success": True, "dataHex": data.hex(" ").upper(), "length": len(data)}

    def write(self, session_id: str, pipe: int, data: bytes) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"WinUSB 会话 {session_id!r} 未打开"}
        try:
            written = session.write(pipe, data)
        except WinUsbError as exc:
            return {"success": False, "code": "winusb_transfer_failed", "error": str(exc)}
        return {"success": True, "bytesWritten": written}

    def pipe(self, session_id: str, pipe: int, action: str) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"WinUSB 会话 {session_id!r} 未打开"}
        ok = session.pipe(pipe, action)
        return {"success": bool(ok), "action": action, "pipe": f"0x{pipe:02X}"}

    def echo(self, session_id: str, **kwargs) -> dict:
        session = self._get(session_id)
        if session is None:
            return {"success": False, "error": f"WinUSB 会话 {session_id!r} 未打开"}
        try:
            report = session.echo(
                kwargs["out_pipe"], kwargs["in_pipe"],
                size=kwargs["size"], count=kwargs["count"],
                pipeline=kwargs.get("pipeline", 1),
                pattern=kwargs.get("pattern", "increment"),
                seed=kwargs.get("seed", 0),
            )
        except WinUsbError as exc:
            return {"success": False, "code": "winusb_transfer_failed", "error": str(exc)}
        return {"success": True, **report}


def helper_sizeof() -> dict[str, int]:
    return {
        "WINUSB_SETUP_PACKET": ctypes.sizeof(WINUSB_SETUP_PACKET),
        "USB_INTERFACE_DESCRIPTOR": ctypes.sizeof(USB_INTERFACE_DESCRIPTOR),
        "WINUSB_PIPE_INFORMATION": ctypes.sizeof(WINUSB_PIPE_INFORMATION),
        "GUID": ctypes.sizeof(GUID),
    }
