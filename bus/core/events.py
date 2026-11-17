"""事件系统：WebSocket 推送管理

管理 WebSocket 连接，向前端推送实时事件（数据、状态、日志等）。
支持 JSON 通道（低频控制）和 Binary 通道（高频数据）。
"""

import asyncio
import json
import struct
from datetime import datetime
from typing import Any
from fastapi import WebSocket, WebSocketDisconnect


def _infer_log_source(message: str) -> str:
    """根据日志消息内容推断来源页面"""
    msg = message or ""
    if "Serial" in msg or "串口" in msg:
        return "serial"
    if "Network" in msg or "网络" in msg:
        return "network"
    if "Modbus" in msg:
        return "modbus"
    return "system"


class EventManager:
    """WebSocket 事件推送管理器"""

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._binary_seq = 0

    async def handle_websocket(self, websocket: WebSocket):
        """处理 WebSocket 连接"""
        await websocket.accept()
        self._connections.append(websocket)
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

        # 推送欢迎消息
        await websocket.send_text(json.dumps({
            "event": "ws.connected",
            "data": {"message": "WebSocket connected"}
        }))

        # 推送一条系统日志，保证全局日志区在连接建立后有可见内容
        await self.emit_async("log", {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "level": "info",
            "message": "系统: WebSocket 已连接 (OMNI Bus 后端)",
            "source": "system",
        })

        try:
            while True:
                # 接收客户端消息（心跳 / 命令）
                raw = await websocket.receive_text()
                msg = json.loads(raw) if raw.startswith("{") else {"action": raw}
                action = msg.get("action")

                if action == "ping":
                    await websocket.send_text(json.dumps({
                        "event": "pong",
                        "data": {"timestamp": datetime.now().isoformat()}
                    }))

        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if websocket in self._connections:
                self._connections.remove(websocket)

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """设置事件循环引用（供后台线程使用）"""
        self._loop = loop

    def emit(self, event: str, data: dict[str, Any]):
        """同步接口：向所有连接推送 JSON 事件（从非 async 上下文调用）"""
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": data})
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._broadcast(message), self._loop)

    async def emit_async(self, event: str, data: dict[str, Any]):
        """异步接口：向所有连接推送 JSON 事件（从 async 上下文调用）"""
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": data})
        await self._broadcast(message)

    def emit_binary(self, data: bytes, data_type: int = 0):
        """推送 Binary 数据（高频数据通道）

        帧格式: [1B type][4B seq][4B len][N B data]
        type: 0=serial_rx, 1=network_rx
        """
        if not self._connections:
            return
        self._binary_seq = (self._binary_seq + 1) & 0xFFFFFFFF
        header = struct.pack('<BII', data_type, self._binary_seq, len(data))
        frame = header + data
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._broadcast_binary(frame), self._loop)

    async def _broadcast(self, message: str):
        """异步广播 JSON 消息"""
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)

    async def _broadcast_binary(self, frame: bytes):
        """异步广播 Binary 消息"""
        dead = []
        for ws in self._connections:
            try:
                await ws.send_bytes(frame)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)

    def log(self, level: str, message: str):
        """推送日志事件"""
        self.emit("log", {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "level": level,
            "message": message,
            "source": _infer_log_source(message),
        })

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# 全局单例
event_manager = EventManager()
