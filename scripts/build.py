#!/usr/bin/env python3
# OMNI Backend
# Copyright (c) 2026 LuckkMaker
# SPDX-License-Identifier: MIT
"""按服务调度 PyInstaller 构建。

用法::

    python scripts/build.py                      # link + bus
    python scripts/build.py --service link
    python scripts/build.py --service bus --clean
    python scripts/build.py --service all --python <venv python>

产物统一落在 ``<repo>/dist/<service>/``：

- ``dist/link/omni-link-backend/omni-link-backend.exe``
- ``dist/bus/omni-bus-backend/omni-bus-backend.exe``

每个产物构建后都会执行一次 ``<exe> --help`` 自检（只验证能启动解释器并加载依赖，
不启动服务、不碰硬件），失败即整体非 0 退出。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SERVICES = ("link", "bus")

# 服务 → (构建脚本, 产物 exe 相对 dist/<service> 的路径)
PRODUCTS = {
    "link": ("link/build.py", Path("omni-link-backend") / "omni-link-backend"),
    "bus": ("bus/build.py", Path("omni-bus-backend") / "omni-bus-backend"),
}


def _log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def _configure_stdio() -> None:
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def exe_path(service: str, dist_root: Path) -> Path:
    """产物的 exe 路径（Windows 带 .exe 后缀）。"""
    rel = PRODUCTS[service][1]
    if sys.platform == "win32":
        rel = rel.with_suffix(".exe")
    return dist_root / service / rel


def build_service(service: str, python_exe: str, clean: bool) -> Path:
    """调用单个服务的 build.py，返回产物 exe 路径。"""
    script = REPO_ROOT / PRODUCTS[service][0]
    if not script.is_file():
        raise SystemExit(f"ERROR: 找不到构建脚本: {script}")

    cmd = [python_exe, str(script)]
    if clean:
        cmd.append("--clean")

    _log(f"--- {service}: {script.relative_to(REPO_ROOT)} ---")
    subprocess.check_call(cmd, cwd=str(REPO_ROOT))

    exe = exe_path(service, REPO_ROOT / "dist")
    if not exe.is_file():
        raise SystemExit(f"ERROR: {service} 产物不存在: {exe}")
    return exe


def selfcheck_exe(service: str, exe: Path, timeout: float = 180.0) -> None:
    """运行 ``<exe> --help``：验证产物能启动并加载全部依赖，不启动服务。"""
    _log(f"--- {service}: 产物自检 {exe.name} --help ---")
    try:
        proc = subprocess.run(
            [str(exe), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"ERROR: {service} 产物自检超时（{timeout:.0f}s）") from None
    if proc.returncode != 0:
        _log(proc.stdout[-2000:])
        _log(proc.stderr[-2000:])
        raise SystemExit(f"ERROR: {service} 产物自检失败（exit={proc.returncode}）")
    if "--port" not in (proc.stdout or ""):
        # --help 至少应打印出参数；否则说明入口被改坏
        _log(proc.stdout[-2000:])
        raise SystemExit(f"ERROR: {service} 产物 --help 输出异常")
    _log(f"{service}: 产物自检通过")


def main() -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="构建 OMNI 后端产物（link / bus）")
    parser.add_argument("--service", default="all", choices=(*SERVICES, "all"),
                        help="要构建的服务，默认 all")
    parser.add_argument("--python", default=None, help="构建用解释器，默认当前解释器")
    parser.add_argument("--clean", action="store_true", help="构建前清理旧产物")
    parser.add_argument("--skip-selfcheck", action="store_true",
                        help="跳过 <exe> --help 自检（不建议）")
    args = parser.parse_args()

    python_exe = args.python or sys.executable
    targets = SERVICES if args.service == "all" else (args.service,)

    built: list[tuple[str, Path]] = []
    for service in targets:
        built.append((service, build_service(service, python_exe, args.clean)))

    if not args.skip_selfcheck:
        for service, exe in built:
            selfcheck_exe(service, exe)

    _log("全部完成：")
    for service, exe in built:
        size_mb = exe.stat().st_size / (1024 * 1024)
        _log(f"  {service}: {exe} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
