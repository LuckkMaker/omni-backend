# OMNI Bus
# Copyright (c) 2026 LuckkMaker
# SPDX-License-Identifier: MIT
"""
OMNI Bus - Python 后端入口

启动 FastAPI 服务器，通过 stdout 输出端口信息供 Electron 主进程读取。
"""

import sys
import os
import json
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
from core.events import event_manager

# 后端版本号（与前端 package.json 保持一致）
BACKEND_VERSION = "0.1.0"

logger = logging.getLogger("omni-bus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    loop = asyncio.get_event_loop()
    event_manager.set_loop(loop)

    logger.info("OMNI Bus backend started")

    yield

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


# WebSocket 端点
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await event_manager.handle_websocket(websocket)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ws_connections": event_manager.connection_count,
    }


def load_config_port():
    """从项目根目录的 backend.config.json 读取后端端口，未配置则返回 None。"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend.config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        port = cfg.get("backendPort")
        return int(port) if port else None
    except Exception:
        return None


def main():
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

    # 输出端口信息（Electron 主进程读取此行）
    print(json.dumps({"port": actual_port}), flush=True)

    # 使用 uvicorn 运行
    uvicorn.run(
        app,
        host=args.host,
        port=actual_port,
        log_level="warning",
        access_log=False,
        ws="wsproto",
    )


if __name__ == "__main__":
    main()
