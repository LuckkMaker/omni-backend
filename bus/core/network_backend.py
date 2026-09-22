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


def _parse_target(target: str) -> tuple[str | None, int | None]:
    """解析 'host:port' 目标（UDP 定时发送用）；非法返回 (None, None)"""
    idx = target.rfind(":")
    if idx <= 0:
        return None, None
    host = target[:idx]
    try:
        port = int(target[idx + 1:])
    except ValueError:
        return None, None
    if not host or not (0 < port <= 65535):
        return None, None
    return host, port


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
        self._auto_send_task: asyncio.Task | None = None
        self._auto_send_data: bytes = b""
        self._auto_send_targets: list[str] | None = None
        self._auto_send_interval: float = 1.0

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
        await self._cancel_auto_send()
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
        # 连接建立也作为报文字段呈现（kind=connect 伪报文，无载荷）
        self._event_manager.emit("network.data", {
            "clientId": client_id,
            "addr": addr_str,
            "direction": "rx",
            "data": "",
            "timestamp": _now(),
            "kind": "connect",
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
        # 连接断开也作为报文字段呈现（kind=disconnect 伪报文，无载荷）
        self._event_manager.emit("network.data", {
            "clientId": client_id,
            "addr": client["addr"],
            "direction": "tx",
            "data": "",
            "timestamp": _now(),
            "kind": "disconnect",
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

    async def start_auto_send(self, data: bytes, interval_ms: int, targets: list[str] | None = None) -> dict:
        """启动定时发送（后端 asyncio 任务，浏览器最小化不受节流影响）"""
        if not self.running:
            return {"success": False, "error": "TCP Server 未运行"}
        await self._cancel_auto_send()
        self._auto_send_data = data
        self._auto_send_targets = targets
        self._auto_send_interval = max(10, int(interval_ms)) / 1000.0
        self._auto_send_task = asyncio.create_task(self._auto_send_loop())
        self._event_manager.log("info", "Network TCP Server 定时发送已启动")
        return {"success": True}

    async def stop_auto_send(self) -> dict:
        """停止 TCP Server 定时发送"""
        return await self._cancel_auto_send()

    async def update_auto_send(self, data: bytes, interval_ms: int, targets: list[str] | None = None) -> dict:
        """运行中更新定时发送载荷/间隔/目标（不重启任务，下一拍即生效）"""
        self._auto_send_data = data
        if targets is not None:
            self._auto_send_targets = targets
        self._auto_send_interval = max(10, int(interval_ms)) / 1000.0
        return {"success": True}

    async def _auto_send_loop(self):
        try:
            while self.running:
                await asyncio.sleep(self._auto_send_interval)
                if not self.running:
                    break
                try:
                    await self.send(self._auto_send_data, self._auto_send_targets)
                except Exception as e:
                    logger.debug("tcp auto send error: %s", e)
        except asyncio.CancelledError:
            pass

    async def _cancel_auto_send(self) -> dict:
        task = self._auto_send_task
        if task is None:
            return {"success": True}
        self._auto_send_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._event_manager.log("info", "Network TCP Server 定时发送已停止")
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


class TcpClientBackend:
    """基于 asyncio 的 TCP Client（单连接，主动连接远端服务器）

    连接/收发事件沿独立命名空间推送（network.tcpclient.*），避免与
    TCP Server 的 network.* 事件混淆：
      - network.tcpclient.status (JSON): {running, host, port, rx, tx, connectedAt?}
      - network.tcpclient.data  (JSON): {direction, data(hex), timestamp, addr, kind?}
    """

    def __init__(self, event_manager):
        self._event_manager = event_manager
        self._reader = None
        self._writer = None
        self._host = None
        self._port = None
        self._connected = False
        self._connecting = False
        self._rx = 0
        self._tx = 0
        self._connected_at = None
        self._recv_task = None
        self._auto_send_task: asyncio.Task | None = None
        self._auto_send_data: bytes = b""
        self._auto_send_interval: float = 1.0

    @property
    def running(self) -> bool:
        return self._connected

    async def connect(self, host: str, port: int) -> dict:
        """连接远端 TCP 服务器"""
        if self._connected:
            return {"success": False, "error": "TCP Client 已连接"}
        if self._connecting:
            return {"success": False, "error": "正在连接中，请稍候"}
        self._connecting = True
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as e:
            self._event_manager.log(
                "error", f"Network TCP Client 连接失败 [{host}:{port}]: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._event_manager.log(
                "error", f"Network TCP Client 连接失败: {e}")
            return {"success": False, "error": str(e)}
        finally:
            self._connecting = False

        self._reader, self._writer = reader, writer
        self._host, self._port = host, port
        self._connected = True
        self._rx = 0
        self._tx = 0
        self._connected_at = _now()
        addr_str = f"{host}:{port}"
        self._event_manager.log(
            "info", f"Network TCP Client 已连接: {host}:{port}")
        self._event_manager.emit("network.tcpclient.data", {
            "direction": "rx", "data": "", "timestamp": _now(),
            "addr": addr_str, "kind": "connect",
        })
        self._emit_status()
        self._recv_task = asyncio.create_task(self._recv_loop())
        return {"success": True, "host": host, "port": port}

    async def disconnect(self) -> dict:
        """主动断开连接"""
        was_connected = self._connected
        host, port = self._host, self._port
        self._connected = False
        await self._cancel_auto_send()
        self._reader = None
        writer = self._writer
        self._writer = None
        task = self._recv_task
        self._recv_task = None
        if task is not None and not task.done():
            task.cancel()
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._host = self._port = None
        self._connected_at = None
        if was_connected:
            addr_str = f"{host}:{port}"
            self._event_manager.log(
                "info", f"Network TCP Client 已断开: {addr_str}")
            self._event_manager.emit("network.tcpclient.data", {
                "direction": "tx", "data": "", "timestamp": _now(),
                "addr": addr_str, "kind": "disconnect",
            })
        self._emit_status()
        return {"success": True}

    async def _recv_loop(self):
        reader = self._reader
        addr_str = f"{self._host}:{self._port}"
        try:
            while self._connected and reader is not None:
                data = await reader.read(4096)
                if not data:
                    break
                self._rx += len(data)
                self._event_manager.emit("network.tcpclient.data", {
                    "direction": "rx",
                    "data": data.hex().upper(),
                    "timestamp": _now(),
                    "addr": addr_str,
                })
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError, OSError):
            pass
        except Exception as e:
            logger.debug("tcp client read error: %s", e)
        finally:
            if self._connected:
                await self.disconnect()

    async def send(self, data: bytes) -> dict:
        """发送数据到远端服务器"""
        if not self._connected or self._writer is None:
            return {"success": False, "error": "TCP Client 未连接"}
        writer = self._writer
        try:
            writer.write(data)
            await writer.drain()
        except Exception as e:
            return {"success": False, "error": str(e)}
        self._tx += len(data)
        self._event_manager.emit("network.tcpclient.data", {
            "direction": "tx",
            "data": data.hex().upper(),
            "timestamp": _now(),
            "addr": f"{self._host}:{self._port}",
        })
        return {"success": True}

    async def start_auto_send(self, data: bytes, interval_ms: int) -> dict:
        """启动定时发送（后端 asyncio 任务，浏览器最小化不受节流影响）"""
        if not self._connected:
            return {"success": False, "error": "TCP Client 未连接"}
        await self._cancel_auto_send()
        self._auto_send_data = data
        self._auto_send_interval = max(10, int(interval_ms)) / 1000.0
        self._auto_send_task = asyncio.create_task(self._auto_send_loop())
        self._event_manager.log("info", "Network TCP Client 定时发送已启动")
        return {"success": True}

    async def stop_auto_send(self) -> dict:
        """停止 TCP Client 定时发送"""
        return await self._cancel_auto_send()

    async def update_auto_send(self, data: bytes, interval_ms: int) -> dict:
        """运行中更新定时发送载荷/间隔（不重启任务，下一拍即生效）"""
        self._auto_send_data = data
        self._auto_send_interval = max(10, int(interval_ms)) / 1000.0
        return {"success": True}

    async def _auto_send_loop(self):
        try:
            while self._connected:
                await asyncio.sleep(self._auto_send_interval)
                if not self._connected:
                    break
                try:
                    await self.send(self._auto_send_data)
                except Exception as e:
                    logger.debug("tcp client auto send error: %s", e)
        except asyncio.CancelledError:
            pass

    async def _cancel_auto_send(self) -> dict:
        task = self._auto_send_task
        if task is None:
            return {"success": True}
        self._auto_send_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._event_manager.log("info", "Network TCP Client 定时发送已停止")
        return {"success": True}

    def status(self) -> dict:
        return {
            "running": self._connected,
            "host": self._host,
            "port": self._port,
            "rx": self._rx,
            "tx": self._tx,
            "connectedAt": self._connected_at,
        }

    def _emit_status(self):
        self._event_manager.emit("network.tcpclient.status", {
            "running": self._connected,
            "host": self._host,
            "port": self._port,
            "rx": self._rx,
            "tx": self._tx,
            "connectedAt": self._connected_at,
        })


# ── UDP 后端 ────────────────────────────────────────────────


class _UdpProtocol(asyncio.DatagramProtocol):
    """UDP 数据报处理：收到远端数据报回调 backend"""

    def __init__(self, backend: "UdpBackend"):
        self._backend = backend

    def datagram_received(self, data: bytes, addr):
        self._backend._on_datagram(data, addr)

    def error_received(self, exc):
        self._backend._event_manager.log(
            "warning", f"Network UDP 收包异常: {exc}")


class UdpBackend:
    """基于 asyncio DatagramProtocol 的 UDP 调试端

    绑定本地 host:port 用于接收；发送时指定目标 host:port（支持广播地址）。
    独立命名空间推送：
      - network.udp.status (JSON): {running, localHost, localPort, rx, tx}
      - network.udp.data  (JSON): {direction, data(hex), timestamp, addr, kind?}
    """

    def __init__(self, event_manager):
        self._event_manager = event_manager
        self._transport = None
        self._local_host = None
        self._local_port = None
        self._rx = 0
        self._tx = 0
        self._echo = False
        self._auto_send_task: asyncio.Task | None = None
        self._auto_send_data: bytes = b""
        self._auto_send_targets: list[str] = []
        self._auto_send_interval: float = 1.0

    @property
    def running(self) -> bool:
        return self._transport is not None

    async def start(self, local_host: str, local_port: int) -> dict:
        """绑定本地地址/端口开始接收 UDP"""
        if self.running:
            return {"success": False, "error": "UDP 已在运行"}

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self),
                local_addr=(local_host, local_port),
            )
        except OSError as e:
            self._event_manager.log(
                "error", f"Network UDP 绑定失败 [{local_host}:{local_port}]: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._event_manager.log(
                "error", f"Network UDP 绑定失败: {e}")
            return {"success": False, "error": str(e)}

        self._transport = transport
        self._local_host = local_host
        self._local_port = local_port
        self._rx = 0
        self._tx = 0
        self._event_manager.log(
            "info", f"Network UDP 已启动: {local_host}:{local_port}")
        self._event_manager.emit("network.udp.data", {
            "direction": "rx", "data": "", "timestamp": _now(),
            "addr": f"{local_host}:{local_port}", "kind": "connect",
        })
        self._emit_status()
        return {"success": True, "host": local_host, "port": local_port}

    async def stop(self) -> dict:
        """停止接收并关闭 socket"""
        transport = self._transport
        self._transport = None
        await self._cancel_auto_send()
        host, port = self._local_host, self._local_port
        if host is not None:
            self._event_manager.log(
                "info", f"Network UDP 已停止: {host}:{port}")
            self._event_manager.emit("network.udp.data", {
                "direction": "tx", "data": "", "timestamp": _now(),
                "addr": f"{host}:{port}", "kind": "disconnect",
            })
        else:
            self._event_manager.log("info", "Network UDP 已停止")
        self._local_host = None
        self._local_port = None
        if transport is not None:
            transport.close()
        self._emit_status()
        return {"success": True}

    def _on_datagram(self, data: bytes, addr):
        """收到数据报（事件循环线程内回调）"""
        if self._transport is None:
            return
        self._rx += len(data)
        addr_str = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr)
        self._event_manager.emit("network.udp.data", {
            "direction": "rx",
            "data": data.hex().upper(),
            "timestamp": _now(),
            "addr": addr_str,
            "kind": "data",
        })
        if self._echo:
            # 回声：将收到的数据原样返回给来源地址；回显包同样作为 TX 报文展示并计入统计
            try:
                self._transport.sendto(data, addr)
            except Exception as e:
                self._event_manager.log(
                    "warning", f"Network UDP 回声发送失败: {e}")
                return
            self._tx += len(data)
            self._event_manager.emit("network.udp.data", {
                "direction": "tx",
                "data": data.hex().upper(),
                "timestamp": _now(),
                "addr": addr_str,
                "kind": "data",
            })

    async def set_echo(self, enabled: bool) -> dict:
        """开/关回声（运行时生效，无需重启）"""
        self._echo = bool(enabled)
        state = "开启" if self._echo else "关闭"
        self._event_manager.log("info", f"Network UDP Echo {state}")
        return {"success": True, "echo": self._echo}

    async def start_auto_send(self, data: bytes, interval_ms: int, targets: list[str]) -> dict:
        """启动定时发送（后端 asyncio 任务，浏览器最小化不受节流影响）"""
        if not self.running:
            return {"success": False, "error": "UDP 未启动"}
        if not targets:
            return {"success": False, "error": "未指定目标"}
        await self._cancel_auto_send()
        self._auto_send_data = data
        self._auto_send_targets = list(targets)
        self._auto_send_interval = max(10, int(interval_ms)) / 1000.0
        self._auto_send_task = asyncio.create_task(self._auto_send_loop())
        self._event_manager.log("info", "Network UDP 定时发送已启动")
        return {"success": True}

    async def stop_auto_send(self) -> dict:
        """停止 UDP 定时发送"""
        return await self._cancel_auto_send()

    async def update_auto_send(self, data: bytes, interval_ms: int, targets: list[str]) -> dict:
        """运行中更新定时发送载荷/间隔/目标（不重启任务，下一拍即生效）"""
        if not targets:
            return {"success": False, "error": "未指定目标"}
        self._auto_send_data = data
        self._auto_send_targets = list(targets)
        self._auto_send_interval = max(10, int(interval_ms)) / 1000.0
        return {"success": True}

    async def _auto_send_loop(self):
        try:
            while self.running:
                await asyncio.sleep(self._auto_send_interval)
                if not self.running:
                    break
                for target in self._auto_send_targets:
                    host, port = _parse_target(target)
                    if host is None or port is None:
                        continue
                    try:
                        await self.send(self._auto_send_data, host, port)
                    except Exception as e:
                        logger.debug("udp auto send error: %s", e)
        except asyncio.CancelledError:
            pass

    async def _cancel_auto_send(self) -> dict:
        task = self._auto_send_task
        if task is None:
            return {"success": True}
        self._auto_send_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._event_manager.log("info", "Network UDP 定时发送已停止")
        return {"success": True}

    async def send(self, data: bytes, target_host: str, target_port: int) -> dict:
        """发送 UDP 数据报到目标地址（支持广播目标）"""
        if self._transport is None:
            return {"success": False, "error": "UDP 未启动"}
        try:
            self._transport.sendto(data, (target_host, target_port))
        except Exception as e:
            return {"success": False, "error": str(e)}
        self._tx += len(data)
        self._event_manager.emit("network.udp.data", {
            "direction": "tx",
            "data": data.hex().upper(),
            "timestamp": _now(),
            "addr": f"{target_host}:{target_port}",
            "kind": "data",
        })
        return {"success": True}

    def status(self) -> dict:
        return {
            "running": self.running,
            "localHost": self._local_host,
            "localPort": self._local_port,
            "rx": self._rx,
            "tx": self._tx,
        }

    def _emit_status(self):
        self._event_manager.emit("network.udp.status", {
            "running": self.running,
            "localHost": self._local_host,
            "localPort": self._local_port,
            "rx": self._rx,
            "tx": self._tx,
        })