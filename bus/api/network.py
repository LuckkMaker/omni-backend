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
tcp_client = network_backend.TcpClientBackend(event_manager)
udp = network_backend.UdpBackend(event_manager)
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


class TcpClientConnectRequest(BaseModel):
    host: str = Field(..., description="目标服务器地址")
    port: int = Field(..., description="目标服务器端口")


@router.post("/tcp-client/connect")
async def tcp_client_connect(req: TcpClientConnectRequest):
    """连接远端 TCP 服务器"""
    if not req.host.strip():
        return {"success": False, "error": "未指定服务器地址"}
    if not _validate_port(req.port):
        return {"success": False, "error": f"无效端口: {req.port}（应为 1-65535）"}
    return await tcp_client.connect(req.host.strip(), req.port)


@router.post("/tcp-client/disconnect")
async def tcp_client_disconnect():
    """断开 TCP 连接"""
    return await tcp_client.disconnect()


@router.post("/tcp-client/send")
async def tcp_client_send(req: SendRequest):
    """发送数据（hex）到远端服务器"""
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await tcp_client.send(data)


@router.get("/tcp-client/status")
async def tcp_client_status():
    """TCP Client 状态"""
    return tcp_client.status()


class UdpStartRequest(BaseModel):
    local_host: str = Field(..., description="本地绑定地址（本机 IPv4 或 0.0.0.0）")
    local_port: int = Field(..., description="本地绑定端口 (1-65535)")


class UdpSendRequest(BaseModel):
    data: str = Field(..., description="hex 字符串")
    target_host: str = Field(..., description="目标地址（支持广播）")
    target_port: int = Field(..., description="目标端口 (1-65535)")


@router.post("/udp/start")
async def udp_start(req: UdpStartRequest):
    """启动 UDP（绑定本地地址/端口开始接收）"""
    if not req.local_host.strip():
        return {"success": False, "error": "未指定本地绑定地址"}
    if not _validate_port(req.local_port):
        return {"success": False, "error": f"无效本地端口: {req.local_port}（应为 1-65535）"}
    return await udp.start(req.local_host.strip(), req.local_port)


@router.post("/udp/stop")
async def udp_stop():
    """停止 UDP"""
    return await udp.stop()


class UdpEchoRequest(BaseModel):
    enabled: bool = Field(..., description="是否开启回声")


@router.post("/udp/echo")
async def udp_echo(req: UdpEchoRequest):
    """开/关 UDP 回声（将收到的数据原样返回给来源）"""
    return await udp.set_echo(req.enabled)


@router.post("/udp/send")
async def udp_send(req: UdpSendRequest):
    """发送 UDP 数据报到目标地址"""
    if not req.target_host.strip():
        return {"success": False, "error": "未指定目标地址"}
    if not _validate_port(req.target_port):
        return {"success": False, "error": f"无效目标端口: {req.target_port}（应为 1-65535）"}
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await udp.send(data, req.target_host.strip(), req.target_port)


@router.get("/udp/status")
async def udp_status():
    """UDP 状态"""
    return udp.status()


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