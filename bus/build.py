#!/usr/bin/env python3
"""
Build script: package the OMNI Bus backend into a standalone executable (PyInstaller).

Usage:
    python bus/build.py                        # 输出 <repo>/dist/bus/omni-bus-backend/
    python bus/build.py --clean                # 先清理旧产物
    python bus/build.py --python PATH          # 指定解释器
    python bus/build.py --out <dir>            # 指定输出根目录（默认 <repo>/dist/bus）

Output:
    <out>/omni-bus-backend/omni-bus-backend.exe

exe 名与 link 服务（``omni-backend``）区分开，避免两个安装包里同名文件互相覆盖；
GUI 侧 ``extraResources`` / ``python-bridge`` 的对应调整见 docs/migration.md（阶段 3）。
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
BACKEND_DIR = Path(__file__).resolve().parent          # <repo>/bus
REPO_ROOT = BACKEND_DIR.parent                          # <repo>
COMMON_DIR = REPO_ROOT / "common"

ENTRY_SCRIPT = BACKEND_DIR / "server.py"
APP_NAME = "omni-bus-backend"

# 输出目录（默认 <repo>/dist/bus），可被 --out 覆盖
DIST_DIR = REPO_ROOT / "dist" / "bus"
BUILD_DIR = DIST_DIR / "work"
SPEC_DIR = DIST_DIR / "spec"


def set_output_dir(out: str | None) -> None:
    """解析并设置输出目录（--out 指向的目录就是 dist 根，产物在其下的 APP_NAME/）。"""
    global DIST_DIR, BUILD_DIR, SPEC_DIR
    DIST_DIR = Path(out).resolve() if out else REPO_ROOT / "dist" / "bus"
    BUILD_DIR = DIST_DIR / "work"
    SPEC_DIR = DIST_DIR / "spec"


# 打包机必须能 import 的运行时依赖。
# pyserial 以 `serial` 为模块名导入；pymodbus 是大型库，打包时用 --collect-all 收集。
REQUIRED_PACKAGES = [
    "fastapi", "uvicorn", "pydantic",
    "serial", "pymodbus", "websockets", "wsproto",
]

# PyInstaller 无法自动发现的动态导入
HIDDEN_IMPORTS = [
    # uvicorn
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 共享包
    "common.omni_service",
    # App modules
    "api.system",
    "api.serial",
    "api.modbus",
    "api.network",
    "api.usb",
    "core.events",
    "core.interface",
    "core.modbus_backend",
    "core.serial_backend",
    "core.network_backend",
    "core.tftp_backend",
    "core.usb_backend",
    "core.usb_pcap",
    "core.usb_decode",
    # pyserial（serialwin32 为运行时动态导入，需显式声明）
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.serialwin32",
]

# 本服务没有 data/flm 之类的随包资源
DATA_DIRS: list[tuple[str, str]] = []


# ---------------------------------------------------------------------------
# 源码编码检查（与 link 服务同一套规则：合法 UTF-8 且无 U+FFFD）
# ---------------------------------------------------------------------------
ENCODING_CHECK_EXCLUDE_PREFIXES = (
    "dist/",
    "build/",
)
ENCODING_CHECK_EXCLUDE_DIR_NAMES = {"__pycache__"}
REPLACEMENT_CHAR = "\ufffd"


def _iter_first_party_sources(root: Path):
    """遍历第一方 .py 文件（跳过构建产物目录）。"""
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
    """执行源码编码检查并打印结果（通过返回 True）。默认检查 bus/ 与 common/。"""
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
    """Check if a package is importable in the given Python."""
    try:
        subprocess.run(
            [python_exe, "-c", f"import {package}"],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def ensure_python(python_exe: str | None) -> str:
    """
    Resolve the Python executable to use.

    Priority:
      1. Explicit --python argument
      2. Current interpreter (sys.executable)
    Then verify required packages are available.
    """
    py = python_exe or sys.executable

    try:
        result = subprocess.run(
            [py, "--version"], capture_output=True, text=True, check=True
        )
        log(f"Using Python: {py} ({result.stdout.strip()})")
    except (FileNotFoundError, subprocess.CalledProcessError):
        log(f"ERROR: Python not found at '{py}'")
        sys.exit(1)

    missing = [pkg for pkg in REQUIRED_PACKAGES if not check_package(py, pkg)]
    if missing:
        log(f"ERROR: Missing packages in {py}: {', '.join(missing)}")
        log(f"  Install them with:  {py} -m pip install -r bus/requirements.txt")
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

    # 本服务无随包数据文件；保留循环以便后续加入
    datas = []
    for src_dir, dst_dir in DATA_DIRS:
        src_path = BACKEND_DIR / src_dir
        if src_path.exists():
            datas.append(f"--add-data={src_path}{os.pathsep}{dst_dir}")

    # pymodbus 有大量运行时动态导入（transaction / framer 后端）
    collect_args = [
        "--collect-all", "pymodbus",
    ]

    for d in (DIST_DIR, BUILD_DIR, SPEC_DIR):
        d.mkdir(parents=True, exist_ok=True)

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

    # 让 PyInstaller 能 import 本服务（bus/）与共享包（common/）
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
        log("Build successful!")
        log(f"Executable: {exe_path}")
        log(f"Size: {size_mb:.1f} MB")

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
    parser = argparse.ArgumentParser(description="Build OMNI Bus backend")
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
        help=f"输出根目录（产物在 <out>/{APP_NAME}/），默认 <repo>/dist/bus",
    )
    parser.add_argument(
        "--check-sources", action="store_true",
        help="Only run the source encoding check (UTF-8 / no U+FFFD); do not build",
    )
    args = parser.parse_args()

    set_output_dir(args.out)

    if args.clean:
        clean()

    if not report_source_encoding():
        sys.exit(1)
    if args.check_sources:
        return

    py = ensure_python(args.python)
    build(py)


if __name__ == "__main__":
    main()
