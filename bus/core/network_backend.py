"""网络调试后端服务

提供网络调试助手所需的能力：
  - 本机 IPv4 地址枚举
  - TCP Server（基于 asyncio.start_server），支持多客户端接入、
    单发/群发/广播/踢出
  - Ping（调用系统 ping 子进程，流式解析回包）

事件推送:
  - network.status (JSON): {running, host, port, clientCount, error?}
  - network.client (JSON): {clientId, addr, action: 'connect'|'disconnect'}
  - network.data (JSON):   {clientId, addr, direction, data(hex), timestamp}
  - network.ping.reply (JSON): {seq, host, status, timeMs?, ttl?, timestamp}
  - network.ping.done (JSON): {host, sent, received, lost, lossPct, min, max, avg, stopped}
  - log (JSON):             日志事件
"""

import asyncio
import logging
import platform
import re
import socket
from datetime import datetime

logger = logging.getLogger(__name__)

# ── 通用工具 ────────────────────────────────────────────────


def parse_hex(hex_str: str) -> bytes:
    """将 hex 字符串解析为 bytes（支持空格/换行分隔）"""
    cleaned = hex_str.replace(" ", "").replace("\n", "") \
                      .replace("\r", "").replace("\t", "")
    return bytes.fromhex(cleaned)


def list_local_ips() -> list[str]:
    """枚举本机 IPv4 地址（无第三方依赖）

    结果至少包含 127.0.0.1；优先通过主机名解析 + UDP 默认路由技巧
    获取局域网 IP。
    """
    ips: set[str] = {"127.0.0.1"}

    # 主机名对应的所有 IPv4 地址
    try:
        _, aliases, addrs = socket.gethostbyname_ex(socket.gethostname())
        for a in addrs:
            ips.add(a)
        for a in aliases:
            try:
                ips.add(socket.gethostbyname(a))
            except Exception:
                pass
    except Exception:
        pass

    # 默认路由出口 IP（UDP connect 不用真正发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass

    return sorted(ips)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# ── TCP Server 后端 ──────────────────────────────────────────


class TcpServerBackend:
    """基于 asyncio 的 TCP Server

    单个服务器实例，支持多个客户端同时接入；客户端以自增号标识。
    """

    def __init__(self, event_manager):
        self._event_manager = event_manager
        self._server: asyncio.AbstractServer | None = None
        self._host: str | None = None
        self._port: int | None = None
        self._clients: dict[str, dict] = {}
        self._client_seq = 0
        self._echo = False

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self, host: str, port: int) -> dict:
        """启动 TCP Server（绑定失败返回明确错误）"""
        if self.running:
            return {"success": False, "error": "TCP Server 已在运行"}

        try:
            self._server = await asyncio.start_server(
                self._client_handler, host, port, reuse_address=True,
            )
        except OSError as e:
            self._event_manager.log(
                "error", f"Network TCP Server 启动失败 [{host}:{port}]: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._event_manager.log(
                "error", f"Network TCP Server 启动失败: {e}")
            return {"success": False, "error": str(e)}

        self._host = host
        self._port = port
        self._event_manager.log(
            "info", f"Network TCP Server 已启动: {host}:{port}")
        self._emit_status()
        return {"success": True, "host": host, "port": port}

    async def stop(self) -> dict:
        """停止服务器并断开所有客户端"""
        server = self._server
        self._server = None
        clients = list(self._clients.values())
        for c in clients:
            await self._close_writer(c.get("writer"))
        self._clients.clear()

        if server is not None:
            try:
                server.close()
                await server.wait_closed()
            except Exception as e:
                logger.warning("close server: %s", e)

        if self._host is not None:
            self._event_manager.log(
                "info", f"Network TCP Server 已停止: {self._host}:{self._port}")
        else:
            self._event_manager.log("info", "Network TCP Server 已停止")
        self._host = None
        self._port = None
        self._emit_status()
        return {"success": True}

    async def _close_writer(self, writer):
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _client_handler(self, reader, writer):
        """单个客户端接入后的读写循环（在事件循环内运行）"""
        self._client_seq += 1
        client_id = f"c{self._client_seq}"
        addr = writer.get_extra_info("peername")
        addr_str = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr)

        self._clients[client_id] = {
            "client_id": client_id,
            "addr": addr_str,
            "writer": writer,
            "connected_at": _now(),
            "rx": 0,
            "tx": 0,
        }
        self._event_manager.emit("network.client", {
            "clientId": client_id,
            "addr": addr_str,
            "action": "connect",
        })
        self._event_manager.log(
            "info", f"Network 客户端接入 [{client_id}] {addr_str}")
        self._emit_status()

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self._emit_rx(client_id, addr_str, data)
                if self._echo:
                    # 回声：将收到的数据原样返回给该客户端
                    writer = self._clients[client_id].get("writer")
                    try:
                        writer.write(data)
                        await writer.drain()
                        self._clients[client_id]["tx"] += len(data)
                        self._event_manager.emit("network.data", {
                            "clientId": client_id,
                            "addr": addr_str,
                            "direction": "tx",
                            "data": data.hex().upper(),
                            "timestamp": _now(),
                        })
                    except ConnectionError:
                        break
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        except Exception as e:
            logger.debug("client read error: %s", e)
        finally:
            await self._remove_client(client_id)

    async def _remove_client(self, client_id: str):
        client = self._clients.pop(client_id, None)
        if client is None:
            return
        await self._close_writer(client.get("writer"))
        self._event_manager.emit("network.client", {
            "clientId": client_id,
            "addr": client["addr"],
            "action": "disconnect",
        })
        self._event_manager.log(
            "info", f"Network 客户端断开 [{client_id}] {client['addr']}")
        self._emit_status()

    def _emit_rx(self, client_id: str, addr: str, data: bytes):
        client = self._clients.get(client_id)
        if client is not None:
            client["rx"] += len(data)
        self._event_manager.emit("network.data", {
            "clientId": client_id,
            "addr": addr,
            "direction": "rx",
            "data": data.hex().upper(),
            "timestamp": _now(),
        })

    async def send(self, data: bytes, targets: list[str] | None = None) -> dict:
        """发送数据给指定客户端或全部

        targets: 客户端 id 列表；None 或 ["all"] 表示广播。
        """
        if not self.running:
            return {"success": False, "error": "TCP Server 未运行"}
        to_all = not targets or "all" in targets
        sent_clients = []
        failed = 0
        for client_id, client in list(self._clients.items()):
            if not to_all and client_id not in targets:
                continue
            writer = client.get("writer")
            try:
                writer.write(data)
                await writer.drain()
                client["tx"] += len(data)
                sent_clients.append(client_id)
                # 发送数据回显（含来源客户端）
                self._event_manager.emit("network.data", {
                    "clientId": client_id,
                    "addr": client["addr"],
                    "direction": "tx",
                    "data": data.hex().upper(),
                    "timestamp": _now(),
                })
            except Exception:
                failed += 1
        if not sent_clients:
            if not to_all:
                return {"success": False, "error": "没有匹配的客户端可发送"}
            return {"success": False, "error": "当前没有已连接的客户端"}
        return {"success": True, "clients": sent_clients, "failed": failed}

    async def set_echo(self, enabled: bool) -> dict:
        """开/关回声（运行时生效，无需重启服务器）"""
        self._echo = bool(enabled)
        state = "开启" if self._echo else "关闭"
        self._event_manager.log("info", f"Network TCP Echo {state}")
        return {"success": True, "echo": self._echo}

    async def kick(self, client_id: str) -> dict:
        """踢出指定客户端"""
        client = self._clients.get(client_id)
        if client is None:
            return {"success": False, "error": f"客户端 {client_id} 不存在"}
        await self._close_writer(client.get("writer"))
        return {"success": True}

    def status(self) -> dict:
        return {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "clientCount": len(self._clients),
            "clients": self.clients(),
        }

    def clients(self) -> list[dict]:
        return [
            {
                "clientId": c["client_id"],
                "addr": c["addr"],
                "connectedAt": c["connected_at"],
                "rx": c["rx"],
                "tx": c["tx"],
            }
            for c in self._clients.values()
        ]

    def _emit_status(self):
        self._event_manager.emit("network.status", {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "clientCount": len(self._clients),
        })


# ── Ping 后端 ────────────────────────────────────────────────

# 平台 ping 命令差异
_IS_WINDOWS = platform.system() == "Windows"

_RE_TIME = re.compile(r"(?:time|时间)\s*[=<]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_TTL = re.compile(r"[Tt]{2}[Ll]\s*=\s*(\d+)")


class PingBackend:
    """系统 ping 子进程封装（零第三方依赖）

    启动系统 ping，流式读取按行解析回包，实时推送 network.ping.reply；
    进程结束（跑完 count 或手动停止）推送 network.ping.done。
    """

    def __init__(self, event_manager):
        self._event_manager = event_manager
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._host: str | None = None
        self._seq = 0
        self._sent = 0
        self._received = 0
        self._times: list[float] = []
        self._stopped = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(
        self,
        host: str,
        count: int | None = None,
        interval_ms: int = 1000,
        timeout_ms: int = 1000,
        size: int = 32,
    ) -> dict:
        """启动 ping（count 为空时按系统默认）"""
        if self.running:
            return {"success": False, "error": "Ping 已在运行"}

        args = ["ping", host]
        if _IS_WINDOWS:
            if count:
                args += ["-n", str(count)]
            args += ["-w", str(max(100, timeout_ms))]
            if size:
                args += ["-l", str(size)]
        else:
            if count:
                args += ["-c", str(count)]
            args += ["-i", str(max(0.2, interval_ms / 1000))]
            args += ["-W", str(max(1, int(timeout_ms / 1000)))]
            if size:
                args += ["-s", str(size)]

        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except Exception as e:
            self._process = None
            self._event_manager.log("error", f"Network Ping 启动失败: {e}")
            return {"success": False, "error": str(e)}

        self._host = host
        self._seq = 0
        self._sent = 0
        self._received = 0
        self._times = []
        self._stopped = False
        self._task = asyncio.create_task(self._read_loop())

        self._event_manager.log("info", f"Network Ping 开始 [{host}]")
        return {"success": True, "host": host}

    async def stop(self) -> dict:
        """手动停止 ping，并推送部分统计"""
        self._stopped = True
        await self._terminate()
        return {"success": True}

    async def _terminate(self):
        proc = self._process
        self._process = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _read_loop(self):
        """读取 ping 输出并逐行解析回包"""
        proc = self._process
        if proc is None or proc.stdout is None:
            self._emit_done()
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._parse_line(line.decode(errors="ignore").strip())
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("ping read error: %s", e)
        finally:
            self._process = None
            self._emit_done()

    def _parse_line(self, line: str):
        if not line:
            return
        low = line.lower()
        status = "reply"
        time_ms = None
        ttl = None

        # 仅处理真正的探测结果行（回包/超时/不可达），跳过头部/汇总等说明行
        is_timeout = (
            "timed out" in low or "timeout" in low or "超时" in line
            or "timeout for" in low or "request timed out" in low
        )
        is_unreachable = "unreachable" in low or "无法访问" in line
        is_result = (
            is_timeout or is_unreachable
            or _RE_TIME.search(line) is not None
            or re.search(r"(?:bytes|时间)\s*=|字节\s*=", low) is not None
            or _RE_TTL.search(line) is not None
        )
        if not is_result:
            return

        self._sent += 1
        if is_timeout:
            status = "timeout"
        elif is_unreachable:
            status = "unreachable"
        else:
            m_time = _RE_TIME.search(line)
            if m_time:
                time_ms = float(m_time.group(1))
                self._times.append(time_ms)
                self._received += 1
            else:
                # 有回包特征但无 time 字段（如 bytes+TTL），按应答计
                self._received += 1
            m_ttl = _RE_TTL.search(line)
            if m_ttl:
                ttl = int(m_ttl.group(1))

        self._seq += 1
        self._event_manager.emit("network.ping.reply", {
            "seq": self._seq,
            "host": self._host,
            "status": status,
            "timeMs": time_ms,
            "ttl": ttl,
            "timestamp": _now(),
        })

    def _emit_done(self):
        sent = self._sent
        received = self._received
        lost = sent - received
        loss_pct = round(lost / sent * 100, 1) if sent else 0.0
        times = self._times
        done = {
            "host": self._host,
            "sent": sent,
            "received": received,
            "lost": lost,
            "lossPct": loss_pct,
            "min": min(times) if times else None,
            "max": max(times) if times else None,
            "avg": round(sum(times) / len(times), 2) if times else None,
            "stopped": self._stopped,
            "timestamp": _now(),
        }
        # host 若已重置则不推送
        self._event_manager.emit("network.ping.done", done)
        self._event_manager.log("info", "Network Ping 结束")

    def status(self) -> dict:
        return {"running": self.running, "host": self._host}