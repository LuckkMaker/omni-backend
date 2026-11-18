"""UsbDk 用户态 API 绑定（spike）

UsbDk 通过 `UsbDkHelper.dll` 暴露一套 C API：枚举设备、把设备从 Windows 驱动栈
"接管"（StartRedirect）、直接收发控制/批量/中断/等时传输。它和 WinUSB 的区别是
**不需要设备固件支持**，代价是接管期间设备会从系统里消失。

本模块只做绑定与最小封装，不含 CLI/API 策略：

- 未安装 UsbDk 时所有入口抛 :class:`UsbDkUnavailable`，由上层转成可读提示；
- 结构体/常量来自 UsbDk 源码（`UsbDkHelper.h`、`UsbDkData.h`，Apache-2.0）；
- 控制传输按 libusb 的 UsbDk 后端实现：``TransferType=ControlTransferType``，
  缓冲区前 8 字节是 SETUP，IN 用 ReadPipe、OUT 用 WritePipe。

注意：本机未安装 UsbDk 驱动，**传输路径尚未在真机验证**；已用假 DLL 覆盖
设备解析与缓冲区构造。驱动安装后的验证清单见 ``docs/usb-raw-access.md``。
"""

from __future__ import annotations

import ctypes
import ctypes.util  # noqa: F401  （保持与其他后端一致的导入习惯）
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── 常量（UsbDkData.h） ───────────────────────────────────

MAX_DEVICE_ID_LEN = 200

CONTROL_TRANSFER = 0
BULK_TRANSFER = 1
INTERRUPT_TRANSFER = 2
ISOCHRONOUS_TRANSFER = 3

TRANSFER_FAILURE = 0
TRANSFER_SUCCESS = 1
TRANSFER_SUCCESS_ASYNC = 2

SPEED_NAMES = {
    0: "unknown",
    1: "low",
    2: "full",
    3: "high",
    4: "super",
}

#: 常见 USBD_STATUS（只用于把错误码翻译成人话）
USBD_STATUS_NAMES = {
    0x00000000: "SUCCESS",
    0xC0000004: "STALL_PID",
    0xC0000006: "TIMEOUT",
    0xC0000008: "BAD_REQUEST",
    0xC0000011: "CRC",
    0xC0000012: "BABBLE",
    0xC0000013: "DATA_OVERRUN",
    0xC0000014: "DATA_UNDERRUN",
    0xC0000015: "BUFFER_OVERRUN",
    0xC0000016: "BUFFER_UNDERRUN",
}


class UsbDkUnavailable(RuntimeError):
    """UsbDk 未安装 / DLL 不存在 / 驱动服务未运行。"""


class UsbDkError(RuntimeError):
    """UsbDk 调用失败。"""

    def __init__(self, message: str, code: int = 0) -> None:
        self.code = code
        super().__init__(message)


# ── 结构体（与 UsbDkData.h 对齐） ─────────────────────────

class USB_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bcdUSB", ctypes.c_ushort),
        ("bDeviceClass", ctypes.c_ubyte),
        ("bDeviceSubClass", ctypes.c_ubyte),
        ("bDeviceProtocol", ctypes.c_ubyte),
        ("bMaxPacketSize0", ctypes.c_ubyte),
        ("idVendor", ctypes.c_ushort),
        ("idProduct", ctypes.c_ushort),
        ("bcdDevice", ctypes.c_ushort),
        ("iManufacturer", ctypes.c_ubyte),
        ("iProduct", ctypes.c_ubyte),
        ("iSerialNumber", ctypes.c_ubyte),
        ("bNumConfigurations", ctypes.c_ubyte),
    ]


class USB_DK_DEVICE_ID(ctypes.Structure):
    _fields_ = [
        ("DeviceID", ctypes.c_wchar * MAX_DEVICE_ID_LEN),
        ("InstanceID", ctypes.c_wchar * MAX_DEVICE_ID_LEN),
    ]


class USB_DK_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("ID", USB_DK_DEVICE_ID),
        ("FilterID", ctypes.c_uint64),
        ("Port", ctypes.c_uint64),
        ("Speed", ctypes.c_uint64),
        ("DeviceDescriptor", USB_DEVICE_DESCRIPTOR),
    ]


class USB_DK_GEN_TRANSFER_RESULT(ctypes.Structure):
    _fields_ = [
        ("BytesTransferred", ctypes.c_uint64),
        ("UsbdStatus", ctypes.c_uint64),
    ]


class USB_DK_TRANSFER_RESULT(ctypes.Structure):
    _fields_ = [
        ("GenResult", USB_DK_GEN_TRANSFER_RESULT),
        ("IsochronousResultsArray", ctypes.c_void_p),
    ]


class USB_DK_TRANSFER_REQUEST(ctypes.Structure):
    _fields_ = [
        ("EndpointAddress", ctypes.c_uint64),
        ("Buffer", ctypes.c_void_p),
        ("BufferLength", ctypes.c_uint64),
        ("TransferType", ctypes.c_uint64),
        ("IsochronousPacketsArraySize", ctypes.c_uint64),
        ("IsochronousPacketsArray", ctypes.c_void_p),
        ("Result", USB_DK_TRANSFER_RESULT),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_ulong),
        ("OffsetHigh", ctypes.c_ulong),
        ("hEvent", ctypes.c_void_p),
    ]


# ── 设备发现 ──────────────────────────────────────────────

def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("OMNI_USBDK_HELPER")
    if explicit:
        candidates.append(Path(explicit))
    for env in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if not base:
            continue
        root = Path(base) / "UsbDk"
        candidates += [
            root / "x64" / "UsbDkHelper.dll",
            root / "x86" / "UsbDkHelper.dll",
            root / "UsbDkHelper.dll",
        ]
    # UsbDk 官方安装器会把 DLL 放到 System32（libusb 就是按名字 LoadLibrary）
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates.append(Path(system_root) / "System32" / "UsbDkHelper.dll")
    return candidates


def find_helper() -> str | None:
    """按优先级找 ``UsbDkHelper.dll``，找不到返回 None。"""
    seen: set[str] = set()
    for candidate in _candidate_paths():
        try:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    if os.name == "nt":
        found = ctypes.util.find_library("UsbDkHelper")
        if found:
            return found
    return None


def _service_installed() -> bool | None:
    """读注册表判断 UsbDk 驱动服务是否存在（None = 无法判断）。"""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\UsbDk"
        ):
            return True
    except OSError:
        return False


@dataclass
class UsbDkDevice:
    device_id: str
    instance_id: str
    filter_id: int
    port: int
    speed: str
    vid: int
    pid: int
    bcd_usb: int
    bcd_device: int
    klass: int
    subclass: int
    protocol: int
    num_configurations: int
    serial_index: int
    product_index: int
    manufacturer_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "instanceId": self.instance_id,
            "filterId": self.filter_id,
            "port": self.port,
            "speed": self.speed,
            "vid": f"0x{self.vid:04X}",
            "pid": f"0x{self.pid:04X}",
            "bcdUSB": f"0x{self.bcd_usb:04X}",
            "bcdDevice": f"0x{self.bcd_device:04X}",
            "class": self.klass,
            "subClass": self.subclass,
            "protocol": self.protocol,
            "numConfigurations": self.num_configurations,
        }


def device_from_raw(raw: USB_DK_DEVICE_INFO) -> UsbDkDevice:
    descriptor = raw.DeviceDescriptor
    return UsbDkDevice(
        device_id=raw.ID.DeviceID,
        instance_id=raw.ID.InstanceID,
        filter_id=int(raw.FilterID),
        port=int(raw.Port),
        speed=SPEED_NAMES.get(int(raw.Speed), f"unknown({raw.Speed})"),
        vid=int(descriptor.idVendor),
        pid=int(descriptor.idProduct),
        bcd_usb=int(descriptor.bcdUSB),
        bcd_device=int(descriptor.bcdDevice),
        klass=int(descriptor.bDeviceClass),
        subclass=int(descriptor.bDeviceSubClass),
        protocol=int(descriptor.bDeviceProtocol),
        num_configurations=int(descriptor.bNumConfigurations),
        serial_index=int(descriptor.iSerialNumber),
        product_index=int(descriptor.iProduct),
        manufacturer_index=int(descriptor.iManufacturer),
    )


# ── 控制传输缓冲区（纯函数，便于单测） ─────────────────────

def build_control_buffer(
    request_type: int,
    request: int,
    value: int,
    index: int,
    length: int,
    data: bytes = b"",
) -> tuple[bytearray, str, int]:
    """构造 UsbDk 控制传输缓冲区。

    返回 ``(buffer, direction, payload_length)``：缓冲区前 8 字节是 SETUP，
    IN 传输按 ``wLength`` 预留数据区，OUT 传输紧跟实际数据。
    """
    direction = "in" if request_type & 0x80 else "out"
    if direction == "out" and len(data) != length:
        length = len(data)
    setup = struct.pack("<BBHHH", request_type, request, value, index, length)
    buffer = bytearray(setup)
    if direction == "in":
        buffer.extend(b"\x00" * length)
    else:
        buffer.extend(data)
    return buffer, direction, length


def control_payload_length(transferred: int, requested: int, direction: str) -> int:
    """从 UsbDk 返回的字节数里析出数据段长度。

    UsbDk 的 ``BytesTransferred`` 是否包含 8 字节 SETUP 取决于驱动版本
    （libusb 直接透传），这里用保守规则并在真机验证时修正：

    - IN：``transferred == requested + 8`` → 不含 SETUP；否则按数据长度处理；
    - OUT：返回 0（调用方只关心是否成功）。
    """
    if direction != "in":
        return 0
    if requested == 0:
        return 0
    if transferred == requested + 8:
        return requested
    if transferred <= requested:
        return transferred
    return max(0, transferred - 8)


def usbd_status_name(status: int) -> str:
    return USBD_STATUS_NAMES.get(status & 0xFFFFFFFF, f"0x{status & 0xFFFFFFFF:08X}")


# ── DLL 绑定 ──────────────────────────────────────────────

class UsbdkLibrary:
    """``UsbDkHelper.dll`` 的薄封装。``library`` 参数用于测试注入假对象。"""

    def __init__(self, path: str | None = None, library: Any | None = None) -> None:
        if library is not None:
            self.path = path or "<injected>"
            self._lib = library
            self._bind_calls = {}
            return
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise UsbDkUnavailable("UsbDk 仅支持 Windows")
        self.path = path or find_helper()
        if not self.path:
            raise UsbDkUnavailable(
                "未找到 UsbDkHelper.dll。请安装 UsbDk 后重试，"
                "或设置 OMNI_USBDK_HELPER 指向 DLL。"
            )
        try:
            self._lib = ctypes.WinDLL(self.path, use_last_error=True)
        except OSError as exc:
            raise UsbDkUnavailable(f"加载 {self.path} 失败：{exc}") from None
        self._bind()

    # ── 绑定 ──────────────────────────────────────────────
    def _bind(self) -> None:
        lib = self._lib
        lib.UsbDk_GetDevicesList.argtypes = [
            ctypes.POINTER(ctypes.POINTER(USB_DK_DEVICE_INFO)),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        lib.UsbDk_GetDevicesList.restype = ctypes.c_int
        lib.UsbDk_ReleaseDevicesList.argtypes = [
            ctypes.POINTER(USB_DK_DEVICE_INFO)
        ]
        lib.UsbDk_ReleaseDevicesList.restype = None

        lib.UsbDk_StartRedirect.argtypes = [ctypes.POINTER(USB_DK_DEVICE_ID)]
        lib.UsbDk_StartRedirect.restype = ctypes.c_void_p
        lib.UsbDk_StopRedirect.argtypes = [ctypes.c_void_p]
        lib.UsbDk_StopRedirect.restype = ctypes.c_int

        for name in ("UsbDk_ReadPipe", "UsbDk_WritePipe"):
            func = getattr(lib, name)
            func.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(USB_DK_TRANSFER_REQUEST),
                ctypes.POINTER(OVERLAPPED),
            ]
            func.restype = ctypes.c_int

        for name in ("UsbDk_AbortPipe", "UsbDk_ResetPipe"):
            func = getattr(lib, name)
            func.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            func.restype = ctypes.c_int

        lib.UsbDk_SetAltsetting.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint64,
        ]
        lib.UsbDk_SetAltsetting.restype = ctypes.c_int
        lib.UsbDk_ResetDevice.argtypes = [ctypes.c_void_p]
        lib.UsbDk_ResetDevice.restype = ctypes.c_int

    # ── 设备 ──────────────────────────────────────────────
    def devices(self) -> list[UsbDkDevice]:
        array = ctypes.POINTER(USB_DK_DEVICE_INFO)()
        count = ctypes.c_ulong(0)
        if not self._lib.UsbDk_GetDevicesList(
            ctypes.byref(array), ctypes.byref(count)
        ):
            code = ctypes.get_last_error()
            raise UsbDkError(_explain(code), code)
        try:
            return [device_from_raw(array[index]) for index in range(count.value)]
        finally:
            self._lib.UsbDk_ReleaseDevicesList(array)

    def open(self, device: UsbDkDevice, timeout_ms: int = 1000) -> "UsbdkSession":
        raw_id = USB_DK_DEVICE_ID()
        raw_id.DeviceID = device.device_id
        raw_id.InstanceID = device.instance_id
        handle = self._lib.UsbDk_StartRedirect(ctypes.byref(raw_id))
        if not handle:
            code = ctypes.get_last_error()
            raise UsbDkError(_explain(code), code)
        return UsbdkSession(self, device, handle, timeout_ms)


def _explain(code: int) -> str:
    if code in (1060, 2):  # ERROR_SERVICE_DOES_NOT_EXIST / ERROR_FILE_NOT_FOUND
        return "UsbDk 驱动服务未安装（错误 1060）。请安装 UsbDk 后重启。"
    if code == 5:
        return "打开 UsbDk 控制设备需要管理员权限。"
    return f"UsbDk 调用失败（Win32 错误 {code}）"


class UsbdkSession:
    """接管一个 USB 设备之后的会话（独占）。"""

    def __init__(
        self,
        library: UsbdkLibrary,
        device: UsbDkDevice,
        handle: int,
        timeout_ms: int = 1000,
    ) -> None:
        self._library = library
        self.device = device
        self._handle = handle
        self.timeout_ms = max(1, int(timeout_ms))

    def __enter__(self) -> "UsbdkSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._handle:
            self._library._lib.UsbDk_StopRedirect(self._handle)
            self._handle = None

    # ── 传输 ──────────────────────────────────────────────
    def control(
        self,
        request_type: int,
        request: int,
        value: int = 0,
        index: int = 0,
        length: int = 0,
        data: bytes = b"",
        timeout_ms: int | None = None,
    ) -> bytes:
        buffer, direction, requested = build_control_buffer(
            request_type, request, value, index, length, data
        )
        transferred = self._transfer(
            endpoint=0,
            buffer=buffer,
            length=len(buffer),
            transfer_type=CONTROL_TRANSFER,
            direction=direction,
            timeout_ms=timeout_ms,
        )
        payload = control_payload_length(transferred, requested, direction)
        return bytes(buffer[8 : 8 + payload])

    def read(
        self,
        endpoint: int,
        length: int,
        transfer_type: int = BULK_TRANSFER,
        timeout_ms: int | None = None,
    ) -> bytes:
        buffer = (ctypes.c_ubyte * max(1, length))()
        transferred = self._transfer(
            endpoint=endpoint,
            buffer=buffer,
            length=length,
            transfer_type=transfer_type,
            direction="in",
            timeout_ms=timeout_ms,
        )
        return bytes(buffer[:transferred])

    def write(
        self,
        endpoint: int,
        data: bytes,
        transfer_type: int = BULK_TRANSFER,
        timeout_ms: int | None = None,
    ) -> int:
        buffer = (ctypes.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\x00")
        return self._transfer(
            endpoint=endpoint,
            buffer=buffer,
            length=len(data),
            transfer_type=transfer_type,
            direction="out",
            timeout_ms=timeout_ms,
        )

    def abort(self, endpoint: int) -> bool:
        return bool(self._library._lib.UsbDk_AbortPipe(self._handle, endpoint))

    def reset_pipe(self, endpoint: int) -> bool:
        return bool(self._library._lib.UsbDk_ResetPipe(self._handle, endpoint))

    def reset_device(self) -> bool:
        return bool(self._library._lib.UsbDk_ResetDevice(self._handle))

    def set_altsetting(self, interface: int, alternate: int) -> bool:
        return bool(
            self._library._lib.UsbDk_SetAltsetting(self._handle, interface, alternate)
        )

    # ── 同步传输核心 ──────────────────────────────────────
    def _transfer(
        self,
        *,
        endpoint: int,
        buffer: Any,
        length: int,
        transfer_type: int,
        direction: str,
        timeout_ms: int | None,
    ) -> int:
        if not self._handle:
            raise UsbDkError("设备已关闭")

        request = USB_DK_TRANSFER_REQUEST()
        request.EndpointAddress = endpoint
        request.Buffer = ctypes.cast(buffer, ctypes.c_void_p)
        request.BufferLength = length
        request.TransferType = transfer_type

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        event = kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise UsbDkError("CreateEvent 失败")
        overlapped = OVERLAPPED()
        overlapped.hEvent = event
        try:
            call = (
                self._library._lib.UsbDk_ReadPipe
                if direction == "in"
                else self._library._lib.UsbDk_WritePipe
            )
            result = call(self._handle, ctypes.byref(request), ctypes.byref(overlapped))
            if result == TRANSFER_FAILURE:
                code = ctypes.get_last_error()
                raise UsbDkError(f"传输失败（Win32 {code}）", code)
            if result == TRANSFER_SUCCESS_ASYNC:
                wait = kernel32.WaitForSingleObject(event, int(timeout_ms or self.timeout_ms))
                if wait != 0:  # WAIT_OBJECT_0
                    self.abort(endpoint)
                    raise UsbDkError("传输超时", 1460)

            status = int(request.Result.GenResult.UsbdStatus)
            if status & 0xFFFFFFFF not in (0,):
                raise UsbDkError(
                    f"USBD 状态 {usbd_status_name(status)}", status
                )
            return int(request.Result.GenResult.BytesTransferred)
        finally:
            kernel32.CloseHandle(event)


# ── 环境诊断 ──────────────────────────────────────────────

def doctor() -> dict[str, Any]:
    """检测 UsbDk 是否可用（CLI/API 的 doctor 共用）。"""
    supported = os.name == "nt"
    path = find_helper() if supported else None
    service = _service_installed()
    result: dict[str, Any] = {
        "supported": supported,
        "installed": bool(path) and service is not False,
        "path": path,
        "service": service,
        "available": False,
        "devices": [],
        "error": None,
        "warnings": [],
    }
    if not supported:
        result["warnings"].append("UsbDk 仅支持 Windows。")
        return result
    if not path:
        result["warnings"].append(
            "未找到 UsbDkHelper.dll：安装 UsbDk 后重启，或用 OMNI_USBDK_HELPER 指定。"
        )
        return result
    if service is False:
        result["warnings"].append(
            "检测到 DLL 但 UsbDk 驱动服务不存在（系统可能未安装驱动或安装未完成）。"
        )
        return result
    try:
        library = UsbdkLibrary(path)
        devices = library.devices()
        result["available"] = True
        result["devices"] = [item.to_dict() for item in devices]
    except (UsbDkUnavailable, UsbDkError) as exc:
        result["error"] = str(exc)
        result["warnings"].append(str(exc))
    return result


def helper_sizeof() -> dict[str, int]:
    """结构体尺寸（测试与排障用，确认与 64 位 C 布局一致）。"""
    return {
        "USB_DK_DEVICE_ID": ctypes.sizeof(USB_DK_DEVICE_ID),
        "USB_DK_DEVICE_INFO": ctypes.sizeof(USB_DK_DEVICE_INFO),
        "USB_DK_TRANSFER_REQUEST": ctypes.sizeof(USB_DK_TRANSFER_REQUEST),
        "OVERLAPPED": ctypes.sizeof(OVERLAPPED),
    }


if sys.platform != "win32":  # pragma: no cover - 仅用于提醒
    # 模块可被非 Windows 导入（CI），所有入口会在调用时报 UsbDkUnavailable
    pass
