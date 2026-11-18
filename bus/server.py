# OMNI Bus
# Copyright (c) 2026 LuckkMaker
# SPDX-License-Identifier: MIT
"""
OMNI Bus - Python 后端入口（omni-backend 仓库的 bus 服务）

启动 FastAPI 服务器，通过 stdout 输出端口信息供 Electron 主进程 / omni-cli 读取。

本服务只依赖同仓库的 ``common``（登记表 / health / 优雅关停），
**不 import link**，两个后端始终是两个独立进程。
"""

import sys
from pathlib import Path

# 仓库根：<repo>/bus/server.py -> <repo>
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import os
import json
import atexit
import asyncio
import argparse
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from api import system
from api import serial
from api import modbus
from api import network
from api import usb
from api import usb_descriptors
from api import usb_hid
from api import usb_tests
from core.events import event_manager

from common import omni_service

# 后端版本号（bus 服务独立发版：bus-vX.Y.Z）
BACKEND_VERSION = "0.1.4"

# CLI ↔ 后端协议版本（只增不改语义；见 docs/protocol.md）
PROTOCOL_VERSION = 1
SERVICE_NAME = "bus"

logger = logging.getLogger("omni-bus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    loop = asyncio.get_event_loop()
    event_manager.set_loop(loop)

    logger.info("OMNI Bus backend started")

    yield

    # 退出前停止 USB 捕获，避免遗留 USBPcapCMD 子进程
    try:
        usb.usb_manager.shutdown()
    except Exception:
        logger.exception("stop usb capture failed")
    logger.info("OMNI Bus backend shutdown")


app = FastAPI(title="OMNI Bus Backend", version=BACKEND_VERSION, lifespan=lifespan)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(system.router, prefix="/api", tags=["system"])
app.include_router(serial.router, prefix="/api/serial", tags=["serial"])
app.include_router(modbus.router, prefix="/api/modbus", tags=["modbus"])
app.include_router(network.router, prefix="/api/network", tags=["network"])
app.include_router(usb.router, prefix="/api/usb", tags=["usb"])
app.include_router(usb_descriptors.router, prefix="/api/usb/desc", tags=["usb"])
app.include_router(usb_tests.router, prefix="/api/usb/test", tags=["usb"])
app.include_router(usb_hid.router, prefix="/api/usb/hid", tags=["usb"])

# POST /api/system/shutdown：由 common 统一实现（含看门狗）
omni_service.install_shutdown(app, SERVICE_NAME)


# WebSocket 端点
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await event_manager.handle_websocket(websocket)


@app.get("/api/health")
async def health():
    return omni_service.health_payload(
        SERVICE_NAME,
        BACKEND_VERSION,
        PROTOCOL_VERSION,
        ws_connections=event_manager.connection_count,
    )


def dev_config_candidates() -> list[str]:
    """开发态端口配置文件的候选路径（按优先级）。

    1. ``OMNI_DEV_CONFIG`` 显式指定的文件；
    2. 后端目录自身的 ``backend.config.json``（本仓库布局：``bus/backend.config.json``）；
    3. 上一级目录的 ``backend.config.json``（旧布局：后端在 ``<应用仓库>/python``，
       配置放在应用仓库根）。
    """
    explicit = os.environ.get("OMNI_DEV_CONFIG")
    if explicit:
        return [explicit]
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(backend_dir, "backend.config.json"),
        os.path.normpath(os.path.join(backend_dir, "..", "backend.config.json")),
    ]


def load_config_port():
    """读取开发态后端端口，未配置则返回 None（由调用方自动分配）。"""
    for config_path in dev_config_candidates():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            port = cfg.get("backendPort")
            return int(port) if port else None
        except Exception:
            continue
    return None


def _configure_stdio() -> None:
    """把后端 stdout/stderr 固定为 UTF-8（CLI/Electron 按 UTF-8 读日志）。"""
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def main():
    _configure_stdio()
    parser = argparse.ArgumentParser(description="OMNI Bus Backend Server")
    parser.add_argument("--port", type=int, default=None,
                        help="监听端口 (默认读取 backend.config.json，未配置则自动分配)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--log-level", type=str, default="info",
                        choices=["debug", "info", "warning", "error"],
                        help="日志级别")
    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # 日志输出到 stderr，不干扰 stdout 的端口 JSON
    )

    actual_port = args.port
    if actual_port is None:
        actual_port = load_config_port()

    # 如果端口未配置或为 0（自动分配），先绑定一个 socket 获取可用端口
    if actual_port is None or actual_port == 0:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((args.host, 0))
        actual_port = sock.getsockname()[1]
        sock.close()

    # 输出端口信息（Electron 主进程 / CLI 读取此行）
    print(json.dumps({"port": actual_port}), flush=True)

    # 共享注册表：让 CLI 等外部进程能发现并复用本后端（退出时自动清理）
    omni_service.publish_runtime(
        SERVICE_NAME, BACKEND_VERSION, PROTOCOL_VERSION, args.host, actual_port
    )
    atexit.register(omni_service.unpublish_runtime, SERVICE_NAME)

    # 使用 uvicorn.Server（而非 uvicorn.run）以便 /api/system/shutdown 触发优雅退出
    config = uvicorn.Config(
        app,
        host=args.host,
        port=actual_port,
        log_level="warning",
        access_log=False,
        ws="wsproto",
    )
    server = uvicorn.Server(config)
    app.state._server = server
    server.run()


if __name__ == "__main__":
    main()
