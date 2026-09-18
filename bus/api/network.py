"""网络调试 REST API

路由前缀: /api/network
提供本机地址枚举、TCP Server 启停/收发/踢出、Ping 启停与查询。
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.events import event_manager
from core import network_backend

logger = logging.getLogger(__name__)
router = APIRouter()

tcp_server = network_backend.TcpServerBackend(event_manager)
ping = network_backend.PingBackend(event_manager)


# ── 请求模型 ──────────────────────────────────────────────

class TcpServerStartRequest(BaseModel):
    host: str = Field(..., description="绑定地址（本机 IPv4 或 0.0.0.0）")
    port: int = Field(..., description="监听端口 (1-65535)")


class SendRequest(BaseModel):
    data: str = Field(..., description="hex 字符串")
    targets: list[str] | None = Field(None, description="客户端 id 列表；None 或 ['all'] 表示广播")


class KickRequest(BaseModel):
    client_id: str = Field(..., description="客户端 id")


class PingStartRequest(BaseModel):
    host: str = Field(..., description="目标 IP 或域名")
    count: int | None = Field(None, ge=1, le=1000, description="发送次数（空=系统默认）")
    interval_ms: int = Field(1000, ge=100, le=60000, description="发送间隔 (ms)")
    timeout_ms: int = Field(1000, ge=100, le=60000, description="超时 (ms)")
    size: int = Field(32, ge=0, le=65500, description="包长 (字节)")


# ── 验证辅助 ──────────────────────────────────────────────

def _validate_port(port: int):
    return 0 < port <= 65535


# ── 路由 ──────────────────────────────────────────────────

@router.get("/interfaces")
async def list_interfaces():
    """枚举本机 IPv4 地址"""
    return {"ips": network_backend.list_local_ips(),
            "hostname": __import__("socket").gethostname()}


@router.post("/tcp-server/start")
async def tcp_server_start(req: TcpServerStartRequest):
    """启动 TCP Server"""
    if not req.host:
        return {"success": False, "error": "未指定绑定地址"}
    if not _validate_port(req.port):
        return {"success": False, "error": f"无效端口: {req.port}（应为 1-65535）"}
    return await tcp_server.start(req.host, req.port)


@router.post("/tcp-server/stop")
async def tcp_server_stop():
    """停止 TCP Server"""
    return await tcp_server.stop()


@router.post("/tcp-server/send")
async def tcp_server_send(req: SendRequest):
    """发送数据（hex）给指定客户端或全部"""
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await tcp_server.send(data, req.targets)


@router.post("/tcp-server/kick")
async def tcp_server_kick(req: KickRequest):
    """踢出指定客户端"""
    return await tcp_server.kick(req.client_id)


class EchoRequest(BaseModel):
    enabled: bool = Field(..., description="是否开启回声")


@router.post("/tcp-server/echo")
async def tcp_server_echo(req: EchoRequest):
    """开/关 TCP Server 回声（原样回发收到的数据）"""
    return await tcp_server.set_echo(req.enabled)


@router.get("/tcp-server/status")
async def tcp_server_status():
    """TCP Server 状态"""
    return tcp_server.status()


@router.get("/tcp-server/clients")
async def tcp_server_clients():
    """已连接客户端列表"""
    return {"clients": tcp_server.clients()}


@router.post("/ping/start")
async def ping_start(req: PingStartRequest):
    """启动 Ping"""
    if not req.host.strip():
        return {"success": False, "error": "未指定 Ping 目标"}
    return await ping.start(
        req.host.strip(), req.count, req.interval_ms, req.timeout_ms, req.size,
    )


@router.post("/ping/stop")
async def ping_stop():
    """停止 Ping"""
    return await ping.stop()


@router.get("/ping/status")
async def ping_status():
    """Ping 状态"""
    return ping.status()