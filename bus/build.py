#!/usr/bin/env python3
"""
Build script: package the Python backend into a standalone executable using PyInstaller.

Usage:
    python build.py                    # Build using current Python
    python build.py --clean            # Clean build artifacts before building
    python build.py --python PATH      # Use specific Python executable

Output:
    dist/omni-backend/omni-backend.exe   # Executable (Windows)

The output is placed in dist/ and is consumed by electron-builder
(build.ps1 -> extraResources) when building the Electron installer.
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
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
DIST_DIR = BACKEND_DIR / "dist"
BUILD_DIR = BACKEND_DIR / "build"

ENTRY_SCRIPT = BACKEND_DIR / "server.py"
APP_NAME = "omni-backend"

# Required runtime packages that must be importable by the building Python.
# pyserial 以 `serial` 为模块名导入；pymodbus 是大型库，打包时用 --collect-all 收集。
REQUIRED_PACKAGES = [
    "fastapi", "uvicorn", "pydantic",
    "serial", "pymodbus", "websockets", "wsproto",
]

# Hidden imports for FastAPI / uvicorn modules that PyInstaller can't auto-detect
HIDDEN_IMPORTS = [
    # uvicorn
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # App modules
    "api.system",
    "api.serial",
    "api.modbus",
    "core.events",
    "core.interface",
    "core.modbus_backend",
    "core.serial_backend",
]

# Data files to include (backends that ship non-.py resources go here)
DATA_DIRS: list[tuple[str, str]] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str):
    """Print a build log message."""
    print(f"[build] {msg}")


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
    """Remove previous build artifacts."""
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            log(f"Cleaning {d}")
            shutil.rmtree(d)
    spec_file = BACKEND_DIR / f"{APP_NAME}.spec"
    if spec_file.exists():
        spec_file.unlink()


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

    # Collect all pymodbus data/submodules (dynamic imports, transaction backends)
    collect_args = [
        "--collect-all", "pymodbus",
    ]

    # Assemble the PyInstaller command
    cmd = [
        python_exe, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--noconfirm",
        "--console",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={BACKEND_DIR}",
        *datas,
        *collect_args,
    ]

    for imp in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", imp])

    # Add the backend dir to the Python path for PyInstaller
    cmd.extend(["--paths", str(BACKEND_DIR)])
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
    parser = argparse.ArgumentParser(description="Build OMNI Bus backend")
    parser.add_argument(
        "--clean", action="store_true",
        help="Clean build artifacts before building",
    )
    parser.add_argument(
        "--python", default=None,
        help="Path to Python executable to use (default: current interpreter)",
    )
    args = parser.parse_args()

    if args.clean:
        clean()

    py = ensure_python(args.python)
    build(py)


if __name__ == "__main__":
    main()