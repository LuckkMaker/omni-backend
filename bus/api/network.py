"""网络调试 REST API

路由前缀: /api/network
提供本机地址枚举、TCP Server 启停/收发/踢出、Ping 启停与查询。
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from core.events import event_manager
from core import network_backend
from core.tftp_backend import TftpServerBackend, TftpClientBackend
from core.net_capture import CAPTURE_MAX_READ

logger = logging.getLogger(__name__)
router = APIRouter()

tcp_server = network_backend.TcpServerBackend(event_manager)
tcp_client = network_backend.TcpClientBackend(event_manager)
udp = network_backend.UdpBackend(event_manager)
ping = network_backend.PingBackend(event_manager)
tftp_server = TftpServerBackend(event_manager)
tftp_client = TftpClientBackend(event_manager)

#: 有接收环形缓存的后端（kind 与 read_capture 返回的 kind 一致）
CAPTURE_BACKENDS = {
    "tcp-server": tcp_server,
    "tcp-client": tcp_client,
    "udp": udp,
}


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


class AutoSendRequest(BaseModel):
    data: str = Field(..., description="hex 字符串（定时发送载荷）")
    interval_ms: int = Field(1000, ge=10, le=60000, description="发送间隔 (ms)")
    targets: list[str] | None = Field(None, description="TCP Server 客户端 id 或 UDP 'host:port' 目标列表")


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


@router.post("/tcp-server/auto-send/start")
async def tcp_server_auto_send_start(req: AutoSendRequest):
    """启动 TCP Server 定时发送（后端 asyncio 任务）"""
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await tcp_server.start_auto_send(data, req.interval_ms, req.targets)


@router.post("/tcp-server/auto-send/stop")
async def tcp_server_auto_send_stop():
    """停止 TCP Server 定时发送"""
    return await tcp_server.stop_auto_send()


@router.post("/tcp-server/auto-send/update")
async def tcp_server_auto_send_update(req: AutoSendRequest):
    """运行中更新 TCP Server 定时发送载荷/间隔/目标（不重启任务）"""
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await tcp_server.update_auto_send(data, req.interval_ms, req.targets)


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


@router.post("/tcp-client/auto-send/start")
async def tcp_client_auto_send_start(req: AutoSendRequest):
    """启动 TCP Client 定时发送（后端 asyncio 任务）"""
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await tcp_client.start_auto_send(data, req.interval_ms)


@router.post("/tcp-client/auto-send/stop")
async def tcp_client_auto_send_stop():
    """停止 TCP Client 定时发送"""
    return await tcp_client.stop_auto_send()


@router.post("/tcp-client/auto-send/update")
async def tcp_client_auto_send_update(req: AutoSendRequest):
    """运行中更新 TCP Client 定时发送载荷/间隔（不重启任务）"""
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await tcp_client.update_auto_send(data, req.interval_ms)


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


@router.post("/udp/auto-send/start")
async def udp_auto_send_start(req: AutoSendRequest):
    """启动 UDP 定时发送（后端 asyncio 任务，targets 每项为 'host:port'）"""
    if not req.targets:
        return {"success": False, "error": "未指定目标"}
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await udp.start_auto_send(data, req.interval_ms, req.targets)


@router.post("/udp/auto-send/stop")
async def udp_auto_send_stop():
    """停止 UDP 定时发送"""
    return await udp.stop_auto_send()


@router.post("/udp/auto-send/update")
async def udp_auto_send_update(req: AutoSendRequest):
    """运行中更新 UDP 定时发送载荷/间隔/目标（不重启任务）"""
    if not req.targets:
        return {"success": False, "error": "未指定目标"}
    try:
        data = network_backend.parse_hex(req.data)
    except ValueError as e:
        return {"success": False, "error": f"无效的 hex 字符串: {e}"}
    return await udp.update_auto_send(data, req.interval_ms, req.targets)


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


# ── TFTP ────────────────────────────────────────────────────


class TftpServerStartRequest(BaseModel):
    root: str = Field(..., description="根目录（供设备下载/上传文件的文件夹）")
    host: str = Field("0.0.0.0", description="监听地址（默认 0.0.0.0）")
    port: int = Field(69, description="监听端口 (1-65535)")


class TftpClientTransferRequest(BaseModel):
    host: str = Field(..., description="服务器地址")
    port: int = Field(69, description="服务器端口 (1-65535)")
    remote: str = Field(..., description="远端文件名")
    local: str = Field(..., description="本地文件路径")


@router.post("/tftp/server/start")
async def tftp_server_start(req: TftpServerStartRequest):
    """启动 TFTP Server（绑定 UDP 端口，供设备拉取/上报文件）"""
    if not req.root.strip():
        return {"success": False, "error": "未指定根目录"}
    if not (req.host.strip() or req.host == "0.0.0.0"):
        return {"success": False, "error": "未指定监听地址"}
    if not _validate_port(req.port):
        return {"success": False, "error": f"无效端口: {req.port}（应为 1-65535）"}
    return await tftp_server.start(req.root.strip(), req.host.strip(), req.port)


@router.post("/tftp/server/stop")
async def tftp_server_stop():
    """停止 TFTP Server"""
    return await tftp_server.stop()


@router.get("/tftp/server/status")
async def tftp_server_status():
    """TFTP Server 状态"""
    return tftp_server.status()


@router.get("/tftp/server/files")
async def tftp_server_files():
    """列出根目录下可被下载的文件"""
    return {"files": tftp_server.list_files()}


@router.post("/tftp/client/download")
async def tftp_client_download(req: TftpClientTransferRequest):
    """TFTP 客户端下载：从远端服务器拉取文件到本地"""
    if not req.host.strip():
        return {"success": False, "error": "未指定服务器地址"}
    if not _validate_port(req.port):
        return {"success": False, "error": f"无效端口: {req.port}（应为 1-65535）"}
    return await tftp_client.download(req.host.strip(), req.port, req.remote, req.local)


@router.post("/tftp/client/upload")
async def tftp_client_upload(req: TftpClientTransferRequest):
    """TFTP 客户端上传：将本地文件写入远端服务器"""
    if not req.host.strip():
        return {"success": False, "error": "未指定服务器地址"}
    if not _validate_port(req.port):
        return {"success": False, "error": f"无效端口: {req.port}（应为 1-65535）"}
    return await tftp_client.upload(req.host.strip(), req.port, req.local, req.remote)


@router.post("/tftp/client/cancel")
async def tftp_client_cancel():
    """取消当前 TFTP 客户端传输"""
    return await tftp_client.cancel()


@router.get("/tftp/client/status")
async def tftp_client_status():
    """TFTP 客户端状态与当前传输进度"""
    return tftp_client.status()


# ── 接收缓冲的游标读取（CLI `omni net read` 的数据源） ──────────────

class NetReadRequest(BaseModel):
    """同步等读请求：等到有数据或超时，一次返回"""
    kind: str = Field("tcp-client", description="tcp-server / tcp-client / udp")
    since: int = Field(0, ge=0, description="起始 seq（0 = 从最旧可用数据开始）")
    max_bytes: int = Field(65536, ge=1, description="单次返回的字节预算")
    timeout_ms: int = Field(500, ge=0, le=30000, description="最长等待毫秒数")
    peer: str | None = Field(None, description="只取该对端的数据（host:port，可选）")


def _capture_backend(kind: str):
    backend = CAPTURE_BACKENDS.get(kind)
    if backend is None:
        return None, {"success": False,
                      "error": f"未知的网络类型 {kind!r}；可选: {', '.join(CAPTURE_BACKENDS)}"}
    return backend, None


@router.get("/data")
async def read_data(
    kind: str = Query(default="tcp-client", description="tcp-server / tcp-client / udp"),
    since: int = Query(default=0, ge=0, description="起始 seq（0 = 从最旧可用数据开始）"),
    max_bytes: int = Query(default=65536, ge=1, description="单次返回的字节预算"),
    peer: str | None = Query(default=None, description="只取该对端的数据（host:port）"),
):
    """按 seq 游标增量读取网络接收缓冲。

    返回契约与 link 的 RTT / bus 的串口一致（含 ``cursor``/``lost``/``session_reset``），
    每批数据额外携带 ``peer``（对端 ``host:port``）。TCP Server 多客户端时可用
    ``peer`` 只看其中一个连接。
    """
    backend, err = _capture_backend(kind)
    if err:
        return err
    return await asyncio.to_thread(backend.read_capture, since,
                                   min(max_bytes, CAPTURE_MAX_READ), peer)


@router.post("/read")
async def read_sync(req: NetReadRequest):
    """同步等读：在 timeout_ms 内等待接收缓冲出现数据，首批到达即返回。

    用于「发一条、收一条」的交互；超时返回空 chunks（不算错误）。
    """
    backend, err = _capture_backend(req.kind)
    if err:
        return err
    deadline = time.monotonic() + req.timeout_ms / 1000.0

    def _drain() -> dict:
        while True:
            result = backend.read_capture(req.since, min(req.max_bytes, CAPTURE_MAX_READ),
                                          req.peer)
            if not result.get("success") or result.get("chunks"):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            time.sleep(min(0.01, remaining))

    return await asyncio.to_thread(_drain)
