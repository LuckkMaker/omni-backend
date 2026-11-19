"""USB 捕获管理

职责：
  - 探测 USBPcap 安装/权限/可捕获的 root hub
  - 以子进程运行 ``USBPcapCMD.exe``，读取 stdout 的 pcap 流
  - 解析、IRP 重组、描述符收集、查询/详情/导出/离线打开
  - 通过 WebSocket 批量推送摘要行（详情按需 REST 拉取）

设计要点见 ``docs/usb-analyzer.md``：抓包是单向监听，不继承
``BackendInterface``（没有 connect/send 语义），而是使用 Manager 模式。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.usb_decode import (
    DescriptorCollector,
    decode_setup,
    describe_transfer,
    status_name,
    summarize_transfer,
    transfer_type_label,
    urb_function_name,
)
from core.usb_pcap import (
    IrpAssembler,
    PcapError,
    PcapStreamReader,
    RecordRef,
    UsbTransfer,
    parse_record,
)

DEFAULT_RING_SIZE = 50_000
DEFAULT_SNAPLEN = 65535
#: 列表接口随行返回的 payload 预览字节数（详情仍按需回读 spool）
PREVIEW_BYTES = 64
SPOOL_KEEP_FILES = 20
ENV_CACHE_SECONDS = 5.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: USBPcapCMD ``--devices`` 的参数形状：逗号分隔的设备地址
_DEVICES_RE = re.compile(r"\d{1,3}(,\d{1,3})*")


# ── 小工具 ────────────────────────────────────────────────

def is_windows() -> bool:
    return os.name == "nt"


def is_elevated() -> bool:
    if not is_windows():
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _registry_candidates() -> list[Path]:
    """从卸载注册表项推导 USBPcap/Wireshark 的安装目录（支持非 C: 盘安装）"""
    if not is_windows():
        return []
    try:
        import winreg
    except ImportError:
        return []

    results: list[Path] = []
    bases = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, base in bases:
        try:
            root = winreg.OpenKey(hive, base)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        try:
                            display, _ = winreg.QueryValueEx(sub, "DisplayName")
                        except OSError:
                            continue
                        label = str(display).lower()
                        is_usbpcap = "usbpcap" in label
                        is_wireshark = "wireshark" in label
                        if not (is_usbpcap or is_wireshark):
                            continue
                        for value_name in ("InstallLocation", "UninstallString", "DisplayIcon"):
                            try:
                                raw, _ = winreg.QueryValueEx(sub, value_name)
                            except OSError:
                                continue
                            if not raw:
                                continue
                            path = str(raw).strip()
                            if path.startswith('"'):
                                path = path[1:].split('"')[0]
                            else:
                                path = path.split(",")[0].split(" ")[0]
                            candidate = Path(path)
                            if candidate.suffix.lower() == ".exe":
                                candidate = candidate.parent
                            if is_wireshark:
                                candidate = candidate / "extcap"
                            results.append(candidate / "USBPcapCMD.exe")
                except OSError:
                    continue
    return results


def find_usbpcap(explicit: str | None = None) -> str | None:
    """按优先级探测 USBPcapCMD.exe，找不到返回 None"""
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.is_file() else None

    candidates: list[Path] = []
    candidates.extend(_registry_candidates())
    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        candidates.append(Path(base) / "USBPcap" / "USBPcapCMD.exe")
        candidates.append(Path(base) / "Wireshark" / "extcap" / "USBPcapCMD.exe")
    which = shutil.which("USBPcapCMD.exe") or shutil.which("USBPcapCMD")
    if which:
        candidates.append(Path(which))

    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _data_dir() -> Path:
    base = os.environ.get("OMNI_BUS_DATA_DIR")
    if not base:
        base = str(Path(tempfile.gettempdir()) / "omni-bus")
    return Path(base)


def _captures_dir() -> Path:
    path = _data_dir() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_captures(directory: Path, keep: int = SPOOL_KEEP_FILES) -> None:
    try:
        files = sorted(
            directory.glob("usb-*.pcap"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _device_id_from_value(value: str) -> str:
    value = value.strip()
    if value.startswith("\\\\.\\"):
        return value
    if value.lower().startswith("usbpcap"):
        suffix = value[len("USBPcap") :] if value.lower().startswith("usbpcap") else ""
        return "\\\\.\\USBPcap" + suffix
    return value


_EXTCAP_INTERFACE_RE = re.compile(
    r"interface\s*\{\s*value=([^}]+)\}\s*\{\s*display=([^}]+)\}", re.IGNORECASE
)
_USBPCAP_ID_RE = re.compile(r"USBPcap(\d+)", re.IGNORECASE)


@dataclass(slots=True)
class TransferRecord:
    """ring 中保存的紧凑传输记录（payload 只留前 PREVIEW_BYTES 字节预览）"""

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
    length: int
    summary: str
    class_hint: str | None
    partial: bool
    setup: bytes | None
    records: tuple[RecordRef, ...]
    preview: bytes = b""

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "durationUs": self.duration_us,
            "bus": self.bus,
            "device": self.device,
            "endpoint": self.endpoint,
            "direction": self.direction,
            "type": self.transfer_type,
            "typeLabel": transfer_type_label(self.transfer_type),
            "status": self.status,
            "statusName": status_name(self.status),
            "ok": self.status == 0,
            "length": self.length,
            "function": self.function,
            "functionName": urb_function_name(self.function),
            "classHint": self.class_hint,
            "summary": self.summary,
            "partial": self.partial,
            "dataHex": self.preview.hex(" ").upper(),
            "dataTruncated": self.length > len(self.preview),
            "setupHex": self.setup.hex(" ").upper() if self.setup else "",
        }


class UsbCaptureManager:
    def __init__(
        self,
        event_manager,
        *,
        ring_size: int = DEFAULT_RING_SIZE,
        captures_dir: Path | None = None,
    ):
        self._em = event_manager
        self._ring_size = max(1000, int(ring_size))
        self._rows: deque[TransferRecord] = deque(maxlen=self._ring_size)
        self._by_id: dict[int, TransferRecord] = {}
        self._lock = threading.RLock()
        self._spool_lock = threading.Lock()

        self._collector = DescriptorCollector()
        self._next_transfer_id = 1
        self._captures_dir = captures_dir or _captures_dir()

        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._status_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._spool = None
        self._capture_path: str | None = None
        self._pid_path = self._captures_dir / "usb-capture.pid"
        self._stderr_tail = b""
        self._last_devices_emit = 0.0

        self._env_cache: tuple[float, dict] | None = None
        self._flag_cache: dict[tuple[str, str], bool] = {}
        self._session_id = 0
        self._totals = self._empty_totals()
        self._status: dict[str, Any] = {
            "running": False,
            "loading": False,
            "mode": None,
            "deviceId": None,
            "startedAt": None,
            "capturePath": None,
            "lastError": None,
            # 离线解析进度 {processed, total, percent}；实时捕获时为 None
            "progress": None,
        }

    # ── 基础状态 ──────────────────────────────────────────
    @staticmethod
    def _empty_totals() -> dict[str, int]:
        return {
            "transfers": 0,
            "bytes": 0,
            "control": 0,
            "bulk": 0,
            "interrupt": 0,
            "isochronous": 0,
            "other": 0,
            "errors": 0,
            "unmatched": 0,
            "dropped": 0,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._status)
            payload["totals"] = dict(self._totals)
            payload["ringSize"] = self._ring_size
            payload["buffered"] = len(self._rows)
            return payload

    def _emit_status(self) -> None:
        self._safe_emit("usb.status", self.status())

    def _start_status_loop(self) -> None:
        """独立于数据流的状态心跳：低流量/空闲时也能刷新统计"""
        session = self._session_id
        thread = threading.Thread(
            target=self._status_loop, args=(session,), name="usb-status", daemon=True
        )
        self._status_thread = thread
        thread.start()

    def _status_loop(self, session: int) -> None:
        while not self._stop_event.wait(1.0):
            if session != self._session_id:
                break
            if not (self._status.get("running") or self._status.get("loading")):
                break
            self._emit_status()

    def _safe_emit(self, event: str, data: dict) -> None:
        try:
            self._em.emit(event, data)
        except Exception:
            pass

    def _log(self, level: str, message: str) -> None:
        self._safe_emit(
            "log",
            {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "level": level,
                "message": message,
                "source": "usb",
            },
        )

    # ── 环境探测 ──────────────────────────────────────────
    def environment(self, usbpcap_path: str | None = None, *, use_cache: bool = True) -> dict:
        now = time.monotonic()
        if (
            use_cache
            and not usbpcap_path
            and self._env_cache
            and now - self._env_cache[0] < ENV_CACHE_SECONDS
        ):
            return self._env_cache[1]

        supported = is_windows()
        path = find_usbpcap(usbpcap_path) if supported else None
        devices: list[dict] = []
        warnings: list[str] = []

        if not supported:
            warnings.append("USB 抓包依赖 USBPcap，仅支持 Windows。")
        elif not path:
            warnings.append(
                "未找到 USBPcapCMD.exe。请安装 USBPcap（安装后需重启）或手动指定路径。"
            )
        else:
            devices, device_warnings = self._probe_devices(path)
            warnings.extend(device_warnings)
            if not devices:
                warnings.append(
                    "未枚举到 USBPcap 根集线器。请确认已安装 USBPcap 并重启系统。"
                )

        result = {
            "supported": supported,
            "platform": sys.platform,
            "installed": bool(path),
            "path": path,
            "version": None,
            "elevated": is_elevated(),
            "devices": devices,
            "warnings": warnings,
        }
        self._env_cache = (now, result)
        return result

    @staticmethod
    def _probe_devices(path: str) -> tuple[list[dict], list[str]]:
        """优先用 extcap 接口列表；失败时尝试交互式列表，全部失败返回空"""
        warnings: list[str] = []
        devices: list[dict] = []

        try:
            proc = subprocess.run(
                [path, "--extcap-interfaces"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            seen: set[str] = set()
            for match in _EXTCAP_INTERFACE_RE.finditer(output):
                value, display = match.group(1), match.group(2)
                device_id = _device_id_from_value(value)
                if device_id in seen:
                    continue
                seen.add(device_id)
                devices.append(
                    {
                        "id": device_id,
                        "label": display or value,
                        "rootHub": value,
                        "attached": [],
                    }
                )
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"extcap 枚举失败：{exc}")

        if devices:
            return devices, warnings

        # 交互式列表：输入 q 退出，超时后强杀；输出里只有 USBPcapN 编号可
        # 可靠提取，具体挂载设备由 Phase 0 实机验证后再增强。
        try:
            proc = subprocess.Popen(
                [path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                output, _ = proc.communicate(input="q\n", timeout=2.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
            seen: set[str] = set()
            for match in _USBPCAP_ID_RE.finditer(output or ""):
                device_id = f"\\\\.\\USBPcap{match.group(1)}"
                if device_id in seen:
                    continue
                seen.add(device_id)
                devices.append(
                    {
                        "id": device_id,
                        "label": f"USBPcap{match.group(1)}",
                        "rootHub": f"USBPcap{match.group(1)}",
                        "attached": [],
                    }
                )
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"USBPcap 设备列表读取失败：{exc}")

        return devices, warnings

    # ── 实时捕获 ──────────────────────────────────────────
    def start(self, device_id: str, options: dict | None = None) -> dict:
        options = options or {}
        if not is_windows():
            return {"success": False, "code": "unsupported_platform", "error": "USB 抓包仅支持 Windows"}
        if not device_id:
            return {"success": False, "code": "capture_start_failed", "error": "未选择 USB 设备"}

        # 参数先校验：这类错误与权限/安装状态无关，先报参数问题更容易定位。
        bufferlen = options.get("bufferlen")
        if bufferlen is not None:
            try:
                bufferlen = int(bufferlen)
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "code": "invalid_bufferlen",
                    "error": f"bufferlen 必须是整数：{options.get('bufferlen')!r}",
                }
            if not (4096 <= bufferlen <= 134217728):
                return {
                    "success": False,
                    "code": "invalid_bufferlen",
                    "error": f"bufferlen 超出范围（4096..134217728）：{bufferlen}",
                }

        devices = (options.get("devices") or "").strip()
        if devices and not _DEVICES_RE.fullmatch(devices):
            return {
                "success": False,
                "code": "invalid_devices",
                "error": f"--devices 需要逗号分隔的设备地址（如 1,2,3）：{devices}",
            }

        with self._lock:
            if (self._thread and self._thread.is_alive()) or (
                self._proc and self._proc.poll() is None
            ):
                return {
                    "success": False,
                    "code": "capture_already_running",
                    "error": "已有 USB 捕获或解析任务在运行",
                }

        path = find_usbpcap(options.get("usbpcapPath"))
        if not path:
            return {
                "success": False,
                "code": "usbpcap_not_found",
                "error": "未找到 USBPcapCMD.exe，请先安装 USBPcap",
            }

        if not is_elevated():
            return {
                "success": False,
                "code": "not_elevated",
                "error": (
                    "USB 抓包需要管理员权限。请以管理员身份重新打开终端后重试；"
                    "GUI 下可右键 OMNI Bus 选择“以管理员身份运行”。"
                ),
            }

        stale_pid = self._clear_stale_capture()
        if stale_pid is not None:
            return {
                "success": False,
                "code": "device_busy",
                "error": (
                    f"检测到残留的 USBPcapCMD（PID {stale_pid}），可能仍占用 root hub。"
                    "请在任务管理器中以管理员身份结束该进程或重启系统。"
                ),
            }

        snaplen = options.get("snaplen") or DEFAULT_SNAPLEN

        # -A: 抓取 root hub 上所有已连接设备；--inject-descriptors: 把已连接
        # 设备的描述符注入抓包。旧版本不支持时自动降级。
        cmd = [path, "-d", device_id, "-o", "-"]
        if self._has_option(path, "--capture-from-all-devices"):
            cmd.append("-A")
        if self._has_option(path, "--inject-descriptors"):
            cmd.append("--inject-descriptors")
        if snaplen:
            cmd += ["-s", str(int(snaplen))]
        if bufferlen is not None and self._has_option(path, "--bufferlen"):
            cmd += ["-b", str(bufferlen)]
        if devices and self._has_option(path, "--devices"):
            cmd += ["--devices", devices]

        spool_path = self._new_spool_path()
        try:
            _cleanup_captures(self._captures_dir)
            spool = open(spool_path, "wb")
        except OSError as exc:
            return {
                "success": False,
                "code": "file_write_failed",
                "error": f"无法创建抓包文件：{exc}",
            }

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            spool.close()
            try:
                os.unlink(spool_path)
            except OSError:
                pass
            return {
                "success": False,
                "code": "capture_start_failed",
                "error": f"启动 USBPcapCMD 失败：{exc}",
            }

        try:
            self._pid_path.write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass

        self._reset_data()
        self._stop_event.clear()
        self._session_id += 1
        self._stderr_tail = b""
        with self._lock:
            self._proc = proc
            self._spool = spool
            self._capture_path = spool_path
            self._status.update(
                {
                    "running": True,
                    "loading": False,
                    "mode": "live",
                    "deviceId": device_id,
                    "startedAt": datetime.now().isoformat(timespec="seconds"),
                    "capturePath": spool_path,
                    "lastError": None,
                    "progress": None,
                }
            )

        self._thread = threading.Thread(
            target=self._run_capture, args=(proc, spool), name="usb-capture", daemon=True
        )
        self._thread.start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(proc,), name="usb-capture-err", daemon=True
        )
        self._stderr_thread.start()

        self._log("info", f"USB: 开始捕获 {device_id}")
        self._emit_status()
        self._start_status_loop()
        return {"success": True, "capturePath": spool_path}

    @staticmethod
    def _supports_option(path: str, option: str) -> bool:
        try:
            proc = subprocess.run(
                [path, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return option in ((proc.stdout or "") + (proc.stderr or ""))

    def _has_option(self, path: str, option: str) -> bool:
        key = (path.lower(), option)
        if key not in self._flag_cache:
            self._flag_cache[key] = self._supports_option(path, option)
        return self._flag_cache[key]

    def _new_spool_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._captures_dir / f"usb-{stamp}.pcap"
        counter = 1
        while path.exists():
            path = self._captures_dir / f"usb-{stamp}-{counter}.pcap"
            counter += 1
        return str(path)

    def _process_alive(self, pid: int) -> bool:
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "USBPcapCMD.exe" in output

    def _clear_stale_capture(self) -> int | None:
        """清理上次异常退出遗留的 USBPcapCMD（可能占着 root hub）。

        返回仍然存活的 PID（清理失败）或 None。
        """
        try:
            pid = int(self._pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        if not self._process_alive(pid):
            try:
                self._pid_path.unlink()
            except OSError:
                pass
            return None

        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.2)
        if self._process_alive(pid):
            return pid
        try:
            self._pid_path.unlink()
        except OSError:
            pass
        return None

    def _clear_pid_file(self, pid: int) -> None:
        try:
            if self._pid_path.read_text(encoding="utf-8").strip() == str(pid):
                self._pid_path.unlink()
        except (OSError, ValueError):
            pass

    def _reset_data(self) -> None:
        with self._lock:
            self._rows.clear()
            self._by_id.clear()
            self._collector.reset()
            self._totals = self._empty_totals()
        self._last_devices_emit = 0.0

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stderr is not None
            for line in iter(proc.stderr.readline, b""):
                if not line:
                    break
                self._stderr_tail = (self._stderr_tail + line)[-4096:]
        except (OSError, ValueError):
            pass

    def _run_capture(self, proc: subprocess.Popen, spool) -> None:
        reader = PcapStreamReader()
        assembler = IrpAssembler()
        batch: list[TransferRecord] = []
        devices_changed = False
        last_emit = 0.0
        last_status = 0.0
        error: str | None = None

        try:
            assert proc.stdout is not None
            while not self._stop_event.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                with self._spool_lock:
                    try:
                        spool.write(chunk)
                        spool.flush()
                    except (OSError, ValueError):
                        pass

                transfers, changed = self._process_chunk(reader, assembler, chunk)
                batch.extend(transfers)
                devices_changed = devices_changed or changed

                now = time.monotonic()
                if batch and (
                    len(batch) >= 200 or now - last_emit >= 0.1 or len(batch) <= 20
                ):
                    self._emit_batch(batch)
                    batch = []
                    last_emit = now
                    if devices_changed:
                        self._emit_devices()
                        devices_changed = False
                if now - last_status >= 1.0:
                    last_status = now
                    self._emit_status()

            if batch:
                self._emit_batch(batch)
            if devices_changed:
                self._emit_devices()

            if not self._stop_event.is_set() and not error:
                code = proc.poll()
                if code == 0 and self._totals["transfers"] == 0:
                    tail = self._stderr_tail.decode("utf-8", errors="replace").strip()
                    error = "USBPcapCMD 提前退出且未捕获到数据"
                    if tail:
                        error += f"：{tail[-300:]}"
                    error += "（可能 root hub 被其他抓包程序占用，或提权被拒绝）"
                elif code not in (None, 0):
                    tail = self._stderr_tail.decode("utf-8", errors="replace").strip()
                    error = f"USBPcapCMD 退出（code {code}）"
                    if tail:
                        error += f"：{tail[-300:]}"
        except PcapError as exc:
            error = f"pcap 解析失败：{exc}"
        except Exception as exc:
            if not self._stop_event.is_set():
                error = f"USB 捕获异常：{exc}"
        finally:
            unmatched = assembler.flush()
            with self._lock:
                self._totals["unmatched"] += unmatched
                if error:
                    self._status["lastError"] = error
                self._status["running"] = False
            # 解析异常退出时确保不遗留 USBPcapCMD 子进程
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            with self._spool_lock:
                try:
                    spool.flush()
                    spool.close()
                except (OSError, ValueError):
                    pass
            with self._lock:
                self._spool = None
                self._proc = None
            self._clear_pid_file(proc.pid)

            if error:
                hint = self._error_hint(error)
                self._safe_emit(
                    "usb.capture.error",
                    {"code": "capture_process_exited", "message": error, "hint": hint},
                )
                self._log("error", f"USB: {error}")
            else:
                self._log("info", "USB: 捕获已停止")
            self._emit_status()

    def _process_chunk(
        self, reader: PcapStreamReader, assembler: IrpAssembler, chunk: bytes
    ) -> tuple[list[TransferRecord], bool]:
        packets = reader.feed(chunk)
        transfers: list[UsbTransfer] = []
        for packet in packets:
            transfers.extend(assembler.feed(packet))

        rows: list[TransferRecord] = []
        devices_changed = False
        for transfer in transfers:
            record, changed = self._append_transfer(transfer)
            rows.append(record)
            devices_changed = devices_changed or changed
        with self._lock:
            self._totals["unmatched"] = assembler.unmatched
        return rows, devices_changed

    def _append_transfer(self, transfer: UsbTransfer) -> tuple[TransferRecord, bool]:
        hint = self._collector.class_hint(transfer.bus, transfer.device, transfer.endpoint)
        setup_info = (
            decode_setup(transfer.setup, hint) if transfer.setup else None
        )
        summary = summarize_transfer(
            transfer.transfer_type,
            transfer.direction,
            setup_info,
            transfer.data,
            hint,
            iso_count=len(transfer.iso_packets),
        )
        if transfer.transfer_type not in (0, 1, 2, 3):
            # 0xFE：非数据传输 URB（管道 abort/reset 等）
            summary = f"{urb_function_name(transfer.function)}"

        with self._lock:
            transfer_id = self._next_transfer_id
            self._next_transfer_id += 1
            record = TransferRecord(
                id=transfer_id,
                ts=transfer.ts,
                duration_us=transfer.duration_us,
                bus=transfer.bus,
                device=transfer.device,
                endpoint=transfer.endpoint,
                direction=transfer.direction,
                transfer_type=transfer.transfer_type,
                status=transfer.status,
                function=transfer.function,
                length=len(transfer.data),
                summary=summary,
                class_hint=hint,
                partial=transfer.partial,
                setup=transfer.setup,
                records=transfer.records,
                preview=transfer.data[:PREVIEW_BYTES],
            )
            if len(self._rows) == self._rows.maxlen:
                evicted = self._rows[0]
                self._by_id.pop(evicted.id, None)
                self._totals["dropped"] += 1
            self._rows.append(record)
            self._by_id[record.id] = record

            self._totals["transfers"] += 1
            self._totals["bytes"] += len(transfer.data)
            key = {
                0: "isochronous",
                1: "interrupt",
                2: "control",
                3: "bulk",
            }.get(transfer.transfer_type, "other")
            if key:
                self._totals[key] += 1
            if transfer.status != 0:
                self._totals["errors"] += 1

        changed = self._collector.feed(transfer)
        return record, changed

    def _emit_batch(self, rows: list[TransferRecord]) -> None:
        if not rows:
            return
        for start in range(0, len(rows), 500):
            part = rows[start : start + 500]
            self._safe_emit(
                "usb.transfers",
                {
                    "items": [row.to_row() for row in part],
                    "firstId": part[0].id,
                    "lastId": part[-1].id,
                    "dropped": 0,
                },
            )

    def _emit_devices(self) -> None:
        now = time.monotonic()
        if now - self._last_devices_emit < 0.5:
            return
        self._last_devices_emit = now
        try:
            tree = self._collector.tree()
        except Exception:
            return
        self._safe_emit("usb.devices", {"tree": tree})

    @staticmethod
    def _error_hint(message: str) -> str:
        low = message.lower()
        if (
            "access" in low
            or "denied" in low
            or "permission" in low
            or "elevat" in low
            or "管理员" in message
        ):
            return "请允许 USBPcap 提权，或以管理员身份运行 OMNI Bus 后重试。"
        if "usb" in low and "3" in message:
            return "USB 3.x 根集线器支持依版本/主控而异，可尝试换到 USB 2.0 端口。"
        return "可停止后重试；若持续失败，请用 Wireshark + USBPcap 交叉验证。"

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
            thread = self._thread
            running = bool(self._status.get("running") or self._status.get("loading"))

        if not running and proc is None and (thread is None or not thread.is_alive()):
            return {
                "success": True,
                "running": False,
                "capturePath": self._capture_path,
                "totals": dict(self._totals),
            }

        self._stop_event.set()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

        with self._lock:
            self._status["running"] = False
            self._status["loading"] = False
        self._emit_status()
        return {
            "success": True,
            "running": False,
            "capturePath": self._capture_path,
            "totals": dict(self._totals),
        }

    def shutdown(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    # ── 离线打开 ──────────────────────────────────────────
    def open_file(self, path: str) -> dict:
        source = Path(path).expanduser()
        if not source.is_file():
            return {
                "success": False,
                "code": "pcap_open_failed",
                "error": f"文件不存在：{path}",
            }
        try:
            total = source.stat().st_size
        except OSError as exc:
            return {"success": False, "code": "pcap_open_failed", "error": str(exc)}
        if total < 24:
            return {"success": False, "code": "pcap_open_failed", "error": "文件为空或不是 pcap"}

        self.stop()
        self._reset_data()
        self._stop_event.clear()
        self._session_id += 1
        with self._lock:
            self._capture_path = str(source)
            self._status.update(
                {
                    "running": False,
                    "loading": True,
                    "mode": "offline",
                    "deviceId": None,
                    "startedAt": datetime.now().isoformat(timespec="seconds"),
                    "capturePath": str(source),
                    "lastError": None,
                    "progress": {"processed": 0, "total": total, "percent": 0.0},
                }
            )
        self._emit_status()

        self._thread = threading.Thread(
            target=self._run_open, args=(source, total), name="usb-open", daemon=True
        )
        self._thread.start()
        self._start_status_loop()
        return {"success": True, "path": str(source), "total": total}

    def _run_open(self, source: Path, total: int) -> None:
        reader = PcapStreamReader()
        assembler = IrpAssembler()
        batch: list[TransferRecord] = []
        devices_changed = False
        last_progress = 0.0
        last_emit = 0.0
        last_status = 0.0
        error: str | None = None

        try:
            with open(source, "rb") as handle:
                while not self._stop_event.is_set():
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    transfers, changed = self._process_chunk(reader, assembler, chunk)
                    batch.extend(transfers)
                    devices_changed = devices_changed or changed

                    now = time.monotonic()
                    if batch and (
                        len(batch) >= 200 or now - last_emit >= 0.1 or len(batch) <= 20
                    ):
                        self._emit_batch(batch)
                        batch = []
                        last_emit = now
                    if now - last_status >= 1.0:
                        last_status = now
                        self._emit_status()
                    if now - last_progress >= 0.2:
                        last_progress = now
                        processed = reader.total_bytes
                        percent = round(min(100.0, processed * 100.0 / total), 1)
                        with self._lock:
                            self._status["progress"] = {
                                "processed": processed,
                                "total": total,
                                "percent": percent,
                            }
                        self._safe_emit(
                            "usb.open.progress",
                            {
                                "processed": processed,
                                "total": total,
                                "percent": percent,
                            },
                        )
            if batch:
                self._emit_batch(batch)
            if devices_changed:
                self._emit_devices()
        except PcapError as exc:
            error = f"pcap 解析失败：{exc}"
        except Exception as exc:
            error = f"离线解析异常：{exc}"
        finally:
            unmatched = assembler.flush()
            with self._lock:
                self._totals["unmatched"] += unmatched
                self._status["loading"] = False
                self._status["progress"] = None
                if error:
                    self._status["lastError"] = error
            if error:
                self._safe_emit(
                    "usb.capture.error",
                    {
                        "code": "pcap_parse_error",
                        "message": error,
                        "hint": "请确认文件是 USBPcap 生成的 classic pcap（LINKTYPE 249）。",
                    },
                )
                self._log("error", f"USB: {error}")
            else:
                self._log("info", f"USB: 已加载 {len(self._rows)} 条传输")
            self._emit_status()

    # ── 查询/详情 ─────────────────────────────────────────
    def query(
        self,
        *,
        limit: int = 200,
        before_id: int | None = None,
        after_id: int | None = None,
        filters: dict | None = None,
    ) -> dict:
        limit = max(1, min(2000, int(limit)))
        filters = filters or {}
        with self._lock:
            rows = list(self._rows)
            next_id = self._next_transfer_id
            vid = filters.get("vid")
            pid = filters.get("pid")
            if vid or pid:
                allowed = self._collector.device_addresses(vid=vid, pid=pid)
                rows = [
                    row for row in rows if (row.bus, row.device) in allowed
                ]

        matched = [row for row in rows if self._matches(row, filters)]
        total = len(matched)

        if before_id is not None:
            candidates = [row for row in matched if row.id < before_id]
            page = candidates[-limit:]
        elif after_id is not None:
            page = [row for row in matched if row.id > after_id][:limit]
        else:
            page = matched[-limit:]

        return {
            "total": total,
            "items": [row.to_row() for row in page],
            # 游标：CLI 的 --follow 用 nextId 作为下一次的 afterId；oldestId
            # 用于提示「请求的游标已被环形缓存覆盖」。
            "oldestId": rows[0].id if rows else None,
            "nextId": next_id,
            "buffered": len(rows),
        }

    @staticmethod
    def _matches(row: TransferRecord, filters: dict) -> bool:
        types = filters.get("type") or []
        if types and transfer_type_label(row.transfer_type) not in types:
            return False
        direction = filters.get("dir")
        if direction and row.direction != direction:
            return False
        if filters.get("bus") is not None and row.bus != int(filters["bus"]):
            return False
        if filters.get("device") is not None and row.device != int(filters["device"]):
            return False
        if filters.get("endpoint") is not None and row.endpoint != int(filters["endpoint"]):
            return False
        status_filter = filters.get("status")
        if status_filter == "ok" and row.status != 0:
            return False
        if status_filter == "error" and row.status == 0:
            return False
        min_length = filters.get("minLength")
        if min_length is not None and row.length < int(min_length):
            return False
        needle = (filters.get("q") or "").strip().lower()
        if needle:
            haystack = " ".join(
                [row.summary, row.class_hint or "", str(row.bus), str(row.device)]
            ).lower()
            if needle not in haystack:
                return False
        return True

    def detail(self, transfer_id: int) -> dict:
        with self._lock:
            record = self._by_id.get(int(transfer_id))
            path = self._capture_path
        if record is None:
            return {
                "success": False,
                "code": "transfer_not_found",
                "error": f"传输 {transfer_id} 不存在或已被淘汰",
            }

        packets = []
        try:
            if not path:
                raise OSError("抓包文件不可用")
            with open(path, "rb") as handle:
                for ref in record.records:
                    handle.seek(ref.offset)
                    body = handle.read(ref.length)
                    if len(body) != ref.length:
                        raise OSError("抓包文件已被截断")
                    packets.append(parse_record(body, ts=ref.ts, offset=ref.offset))
        except (OSError, PcapError) as exc:
            return {
                "success": False,
                "code": "detail_unavailable",
                "error": f"详情不可用：{exc}",
            }

        data = self._reassemble_data(record, packets)
        hint = record.class_hint
        setup_info = decode_setup(record.setup, hint) if record.setup else None
        summary = summarize_transfer(
            record.transfer_type,
            record.direction,
            setup_info,
            data,
            hint,
            iso_count=len(self._iso_packets(packets)),
        )
        pseudo = SimpleNamespace(
            bus=record.bus,
            device=record.device,
            endpoint=record.endpoint,
            direction=record.direction,
            transfer_type=record.transfer_type,
            status=record.status,
            function=record.function,
            data=data,
            duration_us=record.duration_us,
        )
        iso = self._iso_packets(packets)
        preview = data[:4096]
        return {
            "success": True,
            **record.to_row(),
            "summary": summary,
            "setup": setup_info,
            "dataHex": preview.hex(" ").upper(),
            "dataLength": len(data),
            "dataTruncated": len(data) > len(preview),
            "setupHex": (record.setup or b"").hex(" ").upper(),
            "ascii": "".join(
                chr(b) if 0x20 <= b < 0x7F else "." for b in preview[:512]
            ),
            "isoPackets": [
                {"offset": p.offset, "length": p.length, "status": p.status}
                for p in iso
            ],
            "decodeTree": describe_transfer(pseudo, setup_info, hint),
        }

    @staticmethod
    def _reassemble_data(record: TransferRecord, packets) -> bytes:
        if record.transfer_type == 2:
            if record.direction == "in":
                return b"".join(
                    p.payload
                    for p in packets
                    if p.stage in (1, 3, None) and p.payload
                )
            parts = []
            for packet in packets:
                if packet.stage == 0 and len(packet.payload) > 8:
                    parts.append(packet.payload[8:])
                elif packet.stage == 1:
                    parts.append(packet.payload)
            return b"".join(parts)
        if record.direction == "in":
            return b"".join(p.payload for p in packets if p.is_completion)
        return b"".join(p.payload for p in packets if not p.is_completion)

    @staticmethod
    def _iso_packets(packets):
        for packet in packets:
            if packet.iso_packets:
                return packet.iso_packets
        return ()

    def devices(self) -> dict:
        return {"tree": self._collector.tree()}

    def documents(self) -> list[dict]:
        """当前捕获/离线解析出的描述符文档（供 ``usb_test`` 与 from-capture 复用）。"""
        return self._collector.documents()

    def clear(self) -> dict:
        with self._lock:
            self._rows.clear()
            self._by_id.clear()
            self._collector.reset()
            self._totals = self._empty_totals()
            self._env_cache = None
        self._safe_emit("usb.devices", {"tree": []})
        self._emit_status()
        return {"success": True}

    # ── 导出 ──────────────────────────────────────────────
    def save(self, target: str) -> dict:
        with self._lock:
            source = self._capture_path
        if not source or not Path(source).is_file():
            return {
                "success": False,
                "code": "detail_unavailable",
                "error": "当前没有可导出的抓包文件",
            }
        target_path = Path(target).expanduser()
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with self._spool_lock:
                if self._spool is not None:
                    try:
                        self._spool.flush()
                    except (OSError, ValueError):
                        pass
                snapshot_size = Path(source).stat().st_size
            # 不长时间持有 spool 锁：只复制快照时刻的字节，避免阻塞捕获线程
            remaining = snapshot_size
            with open(source, "rb") as src, open(target_path, "wb") as dst:
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)
            size = target_path.stat().st_size
        except OSError as exc:
            return {
                "success": False,
                "code": "file_write_failed",
                "error": f"导出失败：{exc}",
            }
        self._log("info", f"USB: 已导出抓包到 {target_path}")
        return {"success": True, "path": str(target_path), "bytes": size}
