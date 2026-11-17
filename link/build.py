#!/usr/bin/env python3
"""
Build script: package the Python backend into a standalone executable using PyInstaller.

Usage:
    python link/build.py                       # 输出 <repo>/dist/link/omni-backend/
    python link/build.py --clean               # 先清理旧产物
    python link/build.py --python PATH         # 指定解释器
    python link/build.py --out <dir>           # 指定输出根目录（默认 <repo>/dist/link）

Output:
    <out>/omni-backend/omni-backend.exe        # Backend executable (Windows)

产物目录名与 exe 名（``omni-backend``）保持不变：GUI 安装包的 ``extraResources``
（``python/dist/omni-backend`` → ``resources/python``）与 NSIS 逻辑因此无需改动。

CLI（omni.exe）不再由本脚本构建：它来自 omni-cli 仓库的
``scripts/build-exe.ps1``，由 GUI 仓库的 build.ps1 调用并拷到 dist/omni.exe。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent          # <repo>/link
REPO_ROOT = BACKEND_DIR.parent                          # <repo>
COMMON_DIR = REPO_ROOT / "common"

ENTRY_SCRIPT = BACKEND_DIR / "server.py"
APP_NAME = "omni-backend"

# 输出目录（默认 <repo>/dist/link），可被 --out 覆盖
DIST_DIR = REPO_ROOT / "dist" / "link"
BUILD_DIR = DIST_DIR / "work"
SPEC_DIR = DIST_DIR / "spec"


def set_output_dir(out: str | None) -> None:
    """解析并设置输出目录（--out 指向的目录就是 dist 根，产物在其下的 APP_NAME/）。"""
    global DIST_DIR, BUILD_DIR, SPEC_DIR
    DIST_DIR = Path(out).resolve() if out else REPO_ROOT / "dist" / "link"
    BUILD_DIR = DIST_DIR / "work"
    SPEC_DIR = DIST_DIR / "spec"

# Required runtime packages that must be importable by the building Python.
# 注意：pyocd 是内置源码（python/pyocd/），check_package 会把 BACKEND_DIR 加入
# sys.path 使其可导入；pyusb / hidapi 是 pyOCD 可选 USB 后端，项目实际用
# libusb_package，不强制检测。
REQUIRED_PACKAGES = [
    "fastapi", "uvicorn", "pyocd", "cmsis_pack_manager",
    "capstone", "intelhex", "libusb_package",
    "jinja2", "yaml",
]

# Hidden imports for FastAPI / uvicorn / pyOCD modules that PyInstaller can't auto-detect
HIDDEN_IMPORTS = [
    # uvicorn
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pyOCD - probe backends
    "pyocd.probe.cmsis_dap_probe",
    "pyocd.probe.stlink_probe",
    "pyocd.probe.jlink_probe",
    "pyocd.probe.picoprobe",
    "pyocd.probe.pydapaccess.dap_access_cmsis_dap",
    "pyocd.probe.pydapaccess.interface.pyusb_backend",
    "pyocd.probe.pydapaccess.interface.pyusb_v2_backend",
    "pyocd.probe.pydapaccess.interface.hidapi_backend",
    "pyocd.probe.pydapaccess.interface.pywinusb_backend",
    # pyOCD - USB backends
    "usb",
    "usb.backend.libusb1",
    "usb.backend.libusb0",
    "usb.backend.openusb",
    "libusb_package",
    "hid",
    # pyOCD - target families (builtin targets)
    "pyocd.target.builtin",
    "pyocd.target.family",
    # pyOCD - core
    "pyocd.core.helpers",
    "pyocd.core.session",
    "pyocd.core.soc_target",
    "pyocd.core.memory_map",
    "pyocd.flash.loader",
    "pyocd.flash.eraser",
    "pyocd.flash.file_programmer",
    # pyOCD - debug
    "pyocd.debug.elf.elf_reader",
    "pyocd.debug.svd.loader",
    # App modules
    "api.probes",
    "api.flash",
    "api.targets",
    "api.files",
    "api.devices",
    "api.commander",
    "api.rtt",
    "api.monitor",
    "api.tools",
    "core.pyocd_backend",
    "core.events",
    "core.probe_monitor",
    "core.interface",
    "core.rtt_backend",
    "core.commander_backend",
    "core.monitor_backend",
    "core.database",
    "core.map_parser",
    "core.command_examples",
    # OMNI Tool（多功能调试工具）后端模块
    "omni_tools",
    "omni_tools.router",
    "omni_tools.session",
    "omni_tools.device_finder",
    "omni_tools.protocol.serial_link",
    "omni_tools.protocol.modbus_rtu",
    "omni_tools.protocol.boot_iap",
    "omni_tools.protocol.app_cmd",
    # pyserial（OMNI Tool 串口底座，serialwin32 为运行时动态导入，需显式声明）
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.serialwin32",
    # ELF 解析依赖
    "elftools.elf.elffile",
    "elftools.elf.sections",
    "elftools.dwarf.dwarfinfo",
]

# Data files to include
DATA_DIRS = [
    ("data", "data"),       # device_info.json
    ("flm", "flm"),         # Flash algorithms
]

# ---------------------------------------------------------------------------
# Source encoding checks
# ---------------------------------------------------------------------------
# 目的：防止“中文源码在某个环节被按错误编码处理 → 字符串里留下 U+FFFD”的产物
# 再次发版（实机报告 P1-4）。替换字符一旦出现在源码中，运行期无论怎么设置
# stdout 编码都无法恢复原文，必须在构建前拦下。
#
# 排除项：pyocd/ 是内置的第三方源码；dist/、build/ 是打包产物。
ENCODING_CHECK_EXCLUDE_PREFIXES = (
    "pyocd/",
    "dist/",
    "build/",
)
ENCODING_CHECK_EXCLUDE_DIR_NAMES = {"__pycache__"}
REPLACEMENT_CHAR = "\ufffd"


def _iter_first_party_sources(root: Path):
    """遍历第一方 .py 文件（跳过第三方/构建产物目录）。"""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ENCODING_CHECK_EXCLUDE_DIR_NAMES and not d.endswith(".egg-info")
        )
        rel_dir = Path(dirpath).resolve().relative_to(root).as_posix()
        rel_prefix = (rel_dir + "/") if rel_dir != "." else ""
        if any(rel_prefix.startswith(p) for p in ENCODING_CHECK_EXCLUDE_PREFIXES):
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def check_source_encoding(root: Path = BACKEND_DIR) -> list[str]:
    """检查第一方 .py：必须是合法 UTF-8 且不含 U+FFFD。返回问题列表。"""
    root = root.resolve()
    problems: list[str] = []
    for path in _iter_first_party_sources(root):
        rel = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
        except OSError as e:  # noqa: PERF203
            problems.append(f"{rel}: 无法读取: {e}")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append(f"{rel}: 不是合法 UTF-8（{e}）")
            continue
        if REPLACEMENT_CHAR in text:
            line = text[: text.index(REPLACEMENT_CHAR)].count("\n") + 1
            problems.append(f"{rel}:{line}: 含 U+FFFD 替换字符")
    return problems


def report_source_encoding(roots: tuple[Path, ...] | None = None) -> bool:
    """执行源码编码检查并打印结果（通过返回 True）。

    默认检查本服务（link/）与共享包（common/）：两者都会进产物。
    """
    if roots is None:
        roots = (BACKEND_DIR, COMMON_DIR)
    problems: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        prefix = "" if root == BACKEND_DIR else f"{root.name}/"
        problems.extend(prefix + p for p in check_source_encoding(root))
    if problems:
        log(f"ERROR: 源码编码检查失败（{len(problems)} 处）")
        for item in problems[:20]:
            log(f"  {item}")
        if len(problems) > 20:
            log(f"  ... 其余 {len(problems) - 20} 处省略")
        return False
    log("源码编码检查通过（UTF-8，无 U+FFFD）")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str):
    """Print a build log message."""
    print(f"[build] {msg}")


def _configure_stdio() -> None:
    """构建日志也固定 UTF-8（管道下中文 Windows 默认 cp936，CI 里会变乱码）。"""
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def check_package(python_exe: str, package: str) -> bool:
    """Check if a package is importable in the given Python.

    把 BACKEND_DIR 加入 sys.path，让内置的 pyocd 源码包可被 import。
    """
    try:
        subprocess.run(
            [python_exe, "-c",
             f"import sys; sys.path.insert(0, r'{BACKEND_DIR}'); import {package}"],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def ensure_python(python_exe: str | None, require_packages: bool = True) -> str:
    """
    Resolve the Python executable to use.

    Priority:
      1. Explicit --python argument
      2. Current interpreter (sys.executable)
    Then verify required packages are available (backend build only).
    """
    py = python_exe or sys.executable

    # Verify the Python exists
    try:
        result = subprocess.run(
            [py, "--version"], capture_output=True, text=True, check=True
        )
        log(f"Using Python: {py} ({result.stdout.strip()})")
    except (FileNotFoundError, subprocess.CalledProcessError):
        log(f"ERROR: Python not found at '{py}'")
        sys.exit(1)

    # Check required packages
    if require_packages:
        missing = [pkg for pkg in REQUIRED_PACKAGES if not check_package(py, pkg)]
        if missing:
            log(f"ERROR: Missing packages in {py}: {', '.join(missing)}")
            log(f"  Install them with:  {py} -m pip install -r requirements.txt")
            sys.exit(1)

    return py


def ensure_pyinstaller(python_exe: str):
    """Install PyInstaller if it's not already available."""
    if check_package(python_exe, "PyInstaller"):
        return
    log("PyInstaller not found, installing...")
    subprocess.check_call(
        [python_exe, "-m", "pip", "install", "pyinstaller>=6.0"]
    )
    log("PyInstaller installed.")


def clean():
    """Remove previous build artifacts（只动输出目录，不碰源码）。"""
    for d in [BUILD_DIR, SPEC_DIR, DIST_DIR / APP_NAME]:
        if d.exists():
            log(f"Cleaning {d}")
            shutil.rmtree(d)
    for stale in list(SPEC_DIR.glob("*.spec")) if SPEC_DIR.exists() else []:
        stale.unlink()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(python_exe: str):
    """Run PyInstaller to create the standalone executable."""
    ensure_pyinstaller(python_exe)

    # Data files to include
    datas = []
    for src_dir, dst_dir in DATA_DIRS:
        src_path = BACKEND_DIR / src_dir
        if src_path.exists():
            datas.append(f"--add-data={src_path}{os.pathsep}{dst_dir}")

    # Collect all pyOCD data files (YAML sequences, SVD zip, etc.)
    collect_args = [
        "--collect-all", "pyocd",
        "--collect-all", "cmsis_pack_manager",
        "--collect-all", "libusb_package",
    ]

    # 输出/工作/spec 目录都落在输出根下，避免污染源码目录
    for d in (DIST_DIR, BUILD_DIR, SPEC_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # Assemble the PyInstaller command
    cmd = [
        python_exe, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--noconfirm",
        "--console",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={SPEC_DIR}",
        *datas,
        *collect_args,
    ]

    for imp in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", imp])

    # 让 PyInstaller 能 import 本服务（link/）与共享包（common/）
    cmd.extend(["--paths", str(BACKEND_DIR)])
    cmd.extend(["--paths", str(REPO_ROOT)])
    cmd.append(str(ENTRY_SCRIPT))

    log("Running PyInstaller...")
    log(f"Command: {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=str(BACKEND_DIR))

    # Verify output
    if sys.platform == "win32":
        exe_path = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    else:
        exe_path = DIST_DIR / APP_NAME / APP_NAME

    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        log(f"Build successful!")
        log(f"Executable: {exe_path}")
        log(f"Size: {size_mb:.1f} MB")

        # Check _internal directory size
        internal_dir = DIST_DIR / APP_NAME / "_internal"
        if internal_dir.exists():
            total_size = sum(f.stat().st_size for f in internal_dir.rglob("*") if f.is_file())
            log(f"Total package size: {total_size / (1024 * 1024):.1f} MB")
    else:
        log(f"ERROR: Expected output not found at {exe_path}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Build OMNI Link backend")
    parser.add_argument(
        "--clean", action="store_true",
        help="Clean build artifacts before building",
    )
    parser.add_argument(
        "--python", default=None,
        help="Path to Python executable to use (default: current interpreter)",
    )
    parser.add_argument(
        "--out", default=None,
        help=f"输出根目录（产物在 <out>/{APP_NAME}/），默认 <repo>/dist/link",
    )
    parser.add_argument(
        "--check-sources", action="store_true",
        help="Only run the source encoding check (UTF-8 / no U+FFFD); do not build",
    )
    args = parser.parse_args()

    set_output_dir(args.out)

    if args.clean:
        clean()

    # 编码检查先于任何打包：含 U+FFFD 的源码无法靠运行期设置修复
    if not report_source_encoding():
        sys.exit(1)
    if args.check_sources:
        return

    py = ensure_python(args.python)
    build(py)


if __name__ == "__main__":
    main()
