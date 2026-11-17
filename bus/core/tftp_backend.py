"""TFTP 后端服务（RFC 1350，octet 模式，零第三方依赖）

TFTP 使用 UDP，块大小 512 字节，逐块确认：
  - 服务器收到 RRQ（读请求）→ 发送 DATA 分块，等待每块 ACK；
  - 服务器收到 WRQ（写请求）→ 回 ACK(0)，接收 DATA 分块，逐块确认；
  - 客户端主动发起 download（RRQ）/ upload（WRQ）。

事件推送（独立命名空间 tftp.*，避免与其他网络事件混淆）：
  - tftp.status   (JSON): {running, host?, port?, root?}
  - tftp.transfer (JSON): {id, role:'server'|'client', dir:'up'|'down',
                           peer, filename, total, transferred, progress,
                           status:'active'|'done'|'error'|'cancelled', error?}
  - tftp.log      (JSON): {level, message}
"""

import asyncio
import logging
import os
import socket
import struct
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5

MODE = b"octet"
BLKSIZE = 512
MAX_RETRIES = 6          # 单块最大重传次数
RETX_TIMEOUT = 1.0       # 单块确认等待超时（秒）
# 进度事件节流：同一会话两次推送的最小间隔（避免逐块高频事件打满前端）
PROGRESS_THROTTLE = 0.1

# TFTP 错误码（RFC 1350）
ERR_NOT_DEFINED = 0
ERR_FILE_NOT_FOUND = 1
ERR_ACCESS_VIOLATION = 2
ERR_DISK_FULL = 3
ERR_ILLEGAL_OP = 4
ERR_UNKNOWN_TID = 5
ERR_FILE_EXISTS = 6
ERR_NO_SUCH_USER = 7


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# ── 报文编解码 ──────────────────────────────────────────────


def pack_rq(opcode: int, filename: bytes, mode: bytes = MODE) -> bytes:
    """拼装 RRQ/WRQ 报文：op(2B) + filename\\x00 + mode\\x00"""
    return struct.pack("!H", opcode) + filename + b"\x00" + mode + b"\x00"


def pack_data(block: int, chunk: bytes) -> bytes:
    return struct.pack("!HH", OP_DATA, block) + chunk


def pack_ack(block: int) -> bytes:
    return struct.pack("!HH", OP_ACK, block)


def pack_error(code: int, msg: str) -> bytes:
    return struct.pack("!HH", OP_ERROR, code) + msg.encode("utf-8", "replace") + b"\x00"


def parse_request(data: bytes):
    """解析 RRQ/WRQ。返回 (opcode, filename_bytes, options)"""
    if len(data) < 4:
        return None
    opcode = struct.unpack("!H", data[:2])[0]
    if opcode not in (OP_RRQ, OP_WRQ):
        return None
    rest = data[2:]
    parts = rest.split(b"\x00")
    if len(parts) < 2 or not parts[0]:
        return None
    return opcode, parts[0], parts[1]


def parse_rq_filename(data: bytes) -> bytes | None:
    parsed = parse_request(data)
    return parsed[1] if parsed is not None else None


# ── 路径安全加固：确保 filename 不逃出根目录 ────────────────


def safe_join(root: str, filename: bytes) -> str:
    root_abs = os.path.abspath(root)
    name = filename.decode("utf-8", "replace")
    name = name.replace("\\", "/").lstrip("/")
    # 拒绝路径穿越片段
    for seg in name.split("/"):
        if seg in ("", ".", ".."):
            return None
    path = os.path.abspath(os.path.join(root_abs, name))
    if path != root_abs and not path.startswith(root_abs + os.sep):
        return None
    return path


# ── 通用会话收包（带超时与来源过滤，粘在独立传输 socket 上）──


class _SessionProto(asyncio.DatagramProtocol):
    """单会话 UDP 协议：入队收到的数据报（合并错误回调）"""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def datagram_received(self, data: bytes, addr):
        self._queue.put_nowait((data, addr))

    def error_received(self, exc):
        self._queue.put_nowait((None, exc))


async def _recv_from(q: asyncio.Queue, peer_ip: str, timeout: float) -> tuple[bytes | None, tuple | None]:
    """从会话队列取来源为 peer_ip 的数据报；超时返回 (None, None)（用于重传控制）"""

    async def _get():
        while True:
            item = await q.get()
            data, addr = item
            if data is None:
                return None, None
            if addr[0] == peer_ip:
                return data, addr

    try:
        return await asyncio.wait_for(_get(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None, None


# ── TFTP 服务器（供嵌入式设备拉取/上报文件） ───────────────


class TftpServerBackend:
    def __init__(self, event_manager):
        self._em = event_manager
        self._transport = None
        self._host = None
        self._port = None
        self._root = None
        self._sessions: dict[str, dict] = {}
        self._seq = 0

    @property
    def running(self) -> bool:
        return self._transport is not None

    async def start(self, root: str, host: str = "0.0.0.0", port: int = 69) -> dict:
        if self.running:
            return {"success": False, "error": "TFTP Server 已在运行"}

        root_abs = os.path.abspath(root)
        if not os.path.isdir(root_abs):
            return {"success": False, "error": f"根目录不存在或不是文件夹: {root}"}

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ServerRequestProto(self),
                local_addr=(host, port),
            )
        except OSError as e:
            self._em.log("error", f"Network TFTP Server 绑定失败 [{host}:{port}]: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._em.log("error", f"Network TFTP Server 绑定失败: {e}")
            return {"success": False, "error": str(e)}

        sockname = transport.get_extra_info("sockname")
        real_port = sockname[1] if isinstance(sockname, tuple) else port
        self._transport = transport
        self._host = host
        self._port = real_port
        self._root = root_abs
        self._em.log("info", f"Network TFTP Server 已启动 [{host}:{real_port}] 根目录 {root_abs}")
        self._emit_status()
        return {"success": True, "host": host, "port": real_port, "root": root_abs}

    async def stop(self) -> dict:
        transport = self._transport
        self._transport = None
        sessions = list(self._sessions.values())
        for s in sessions:
            await self._finish_session(s, "cancelled", None)
        if transport is not None:
            transport.close()
        if self._host is not None:
            self._em.log("info", f"Network TFTP Server 已停止 [{self._host}:{self._port}]")
        else:
            self._em.log("info", "Network TFTP Server 已停止")
        self._host = self._port = self._root = None
        self._emit_status()
        return {"success": True}

    def on_request(self, data: bytes, addr):
        """主端口收到 RRQ/WRQ：为每个会话分配独立 UDP socket（RFC1350 要求）"""
        parsed = parse_request(data)
        if parsed is None:
            return
        opcode, filename, _mode = parsed
        path = safe_join(self._root or "", filename)
        if path is None:
            # 路径非法：回 ERROR 文件不存在（不泄露目录结构）
            self._reply_main(addr, pack_error(ERR_FILE_NOT_FOUND, "非法文件名"))
            return
        # 同步回调内不能 await，故把建连与会话调度放入异步任务
        asyncio.create_task(self._spawn_session(opcode, filename, path, addr),
                            name="tftp-spawn")

    async def _spawn_session(self, opcode: int, filename: bytes, path: str, addr):
        if not self.running:
            return
        self._seq += 1
        sid = str(self._seq) + ":" + uuid.uuid4().hex[:6]
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _SessionProto(queue), local_addr=("0.0.0.0", 0),
            )
        except Exception as e:
            self._em.log("error", f"Network TFTP Server 会话创建失败: {e}")
            return

        d = "up" if opcode == OP_WRQ else "down"
        session = {
            "id": sid, "role": "server", "dir": d, "peer": addr,
            "filename": filename.decode("utf-8", "replace"),
            "path": path, "transport": transport, "queue": queue,
            "total": 0, "transferred": 0, "status": "active",
            "last_progress": 0.0, "error": None,
        }
        self._sessions[sid] = session
        self._emit_transfer(session, force=True)
        self._log("info", f"TFTP Server 收到{'写' if d == 'up' else '读'}请求 {session['filename']} 来自 {addr[0]}:{addr[1]}")

        task = asyncio.create_task(
            self._session_loop(session, opcode), name=f"tftp-server-{sid}")
        session["task"] = task

    def _reply_main(self, addr, packet: bytes):
        if self._transport is None:
            return
        try:
            self._transport.sendto(packet, addr)
        except Exception:
            pass

    async def _session_loop(self, session: dict, opcode: int):
        try:
            if opcode == OP_RRQ:
                result = await self._serve_download(session)
            else:
                result = await self._serve_upload(session)
        except asyncio.CancelledError:
            result = ("cancelled", None)
        except Exception as e:
            logger.debug("tftp server session error: %s", e)
            result = ("error", str(e))
        finally:
            await self._finish_session(session, result[0], result[1])

    async def _serve_download(self, session: dict) -> tuple[str, str | None]:
        """把根目录下文件分块发给对端（对端=嵌入式设备）"""
        path = session["path"]
        transport = session["transport"]
        q = session["queue"]
        peer_ip, peer_port = session["peer"]

        try:
            with open(path, "rb") as f:
                total = os.path.getsize(path)
                session["total"] = total
                block = 0
                while True:
                    chunk = f.read(BLKSIZE)
                    if chunk:
                        block += 1
                        ok = await self._send_data_wait_ack(session, transport, q, peer_ip, peer_port, block, chunk)
                        session["transferred"] = min(total, block * BLKSIZE)
                        self._emit_transfer(session)
                        if not ok:
                            return "error", "传输中止（对方无响应或返回错误）"
                        if len(chunk) < BLKSIZE:
                            break  # 短块即结束标志
                    else:
                        # EOF 且此前恰好传满整块（或空文件）：补发一个 0 长度 DATA 表示结束
                        block += 1
                        ok = await self._send_data_wait_ack(session, transport, q, peer_ip, peer_port, block, b"")
                        session["transferred"] = total
                        self._emit_transfer(session)
                        if not ok:
                            return "error", "传输中止（对方无响应或返回错误）"
                        break
        except OSError as e:
            self._reply_error(session, ERR_FILE_NOT_FOUND, "文件读取失败")
            return "error", str(e)
        session["transferred"] = total
        return "done", None

    async def _send_data_wait_ack(self, session, transport, q, peer_ip, peer_port, block, chunk) -> bool:
        packet = pack_data(block, chunk)
        if session["status"] == "active":
            peer_ip, peer_port = session["peer"]
        for _ in range(MAX_RETRIES):
            if session["status"] != "active":
                return False
            try:
                transport.sendto(packet, (peer_ip, peer_port))
            except Exception:
                return False
            resp, _ = await _recv_from(q, peer_ip, RETX_TIMEOUT)
            if resp is None:
                continue  # 超时重传本块
            if len(resp) >= 4 and struct.unpack("!H", resp[:2])[0] == OP_ACK:
                ack_block = struct.unpack("!HH", resp[:4])[1]
                if ack_block == block:
                    return True
                continue  # 期望更早块的重复 ACK，忽略
            if len(resp) >= 2 and struct.unpack("!H", resp[:2])[0] == OP_ERROR:
                err_msg = resp[4:].split(b"\x00")[0].decode(errors="replace") if len(resp) > 4 else ""
                self._em.log("warning", "Network TFTP 对端错误: " + (err_msg or "未知错误"))
                return False
        return False

    async def _serve_upload(self, session: dict) -> tuple[str, str | None]:
        """接收对端上传，先写临时文件，成功后原子改名，避免半截覆盖已有文件"""
        transport = session["transport"]
        q = session["queue"]
        peer_ip, _ = session["peer"]
        path = session["path"]

        tmp_path = path + f".tftp_tmp_{uuid.uuid4().hex[:6]}"
        try:
            transport.sendto(pack_ack(0), session["peer"])
        except Exception:
            return "error", "发送 ACK 失败"

        expected = 1
        total = 0
        try:
            # 清空临时文件内容
            with open(tmp_path, "wb") as f:
                while True:
                    resp, _ = await _recv_from(q, peer_ip, RETX_TIMEOUT)
                    if resp is None:
                        return "error", "接收数据超时"
                    if len(resp) < 4:
                        continue
                    op, block = struct.unpack("!HH", resp[:4])
                    if op == OP_ERROR:
                        return "error", "对端终止上传"
                    if op != OP_DATA:
                        continue
                    if block == expected:
                        chunk = resp[4:]
                        f.write(chunk)
                        total += len(chunk)
                        session["transferred"] = total
                        session["total"] = total
                        self._emit_transfer(session)
                        try:
                            transport.sendto(pack_ack(block), session["peer"])
                        except Exception:
                            return "error", "发送 ACK 失败"
                        expected += 1
                        if len(chunk) < BLKSIZE:
                            break
            # 成功：原子改名到位
            try:
                if os.path.exists(path):
                    os.remove(path)
                os.replace(tmp_path, path)
            except OSError as e:
                self._cleanup(tmp_path)
                return "error", f"落盘失败: {e}"
            session["total"] = total
            session["transferred"] = total
            return "done", None
        except OSError as e:
            self._cleanup(tmp_path)
            self._reply_error(session, ERR_DISK_FULL, "磁盘写入失败")
            return "error", str(e)
        except asyncio.CancelledError:
            self._cleanup(tmp_path)
            raise

    def _reply_error(self, session, code, msg):
        transport = session.get("transport")
        if transport is None:
            return
        try:
            transport.sendto(pack_error(code, msg), session["peer"])
        except Exception:
            pass

    def _cleanup(self, tmp_path: str):
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

    async def _finish_session(self, session: dict, status: str, error: str | None):
        task = session.pop("task", None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        transport = session.get("transport")
        if transport is not None:
            transport.close()
        if status == "done":
            act = "下传" if session["dir"] == "down" else "上传"
            self._log("info",
                      f"TFTP Server {act}完成 {session['filename']}"
                      f"（{session['transferred'] if session['transferred'] > 0 else 0} 字节）")
        elif status == "error":
            self._log("error",
                      f"TFTP Server 传输失败 {session['filename']}: {error or '未知错误'}")
        else:
            self._log("warning", f"TFTP Server 传输已取消 {session['filename']}")
        if session["id"] in self._sessions:
            del self._sessions[session["id"]]
        session["status"] = status
        session["error"] = error
        self._emit_transfer(session, force=True)

    def list_files(self) -> list[dict]:
        if not self._root:
            return []
        out = []
        try:
            for name in sorted(os.listdir(self._root)):
                p = os.path.join(self._root, name)
                if os.path.isfile(p):
                    out.append({"name": name, "size": os.path.getsize(p)})
        except OSError:
            pass
        return out

    def status(self) -> dict:
        return {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "root": self._root,
            "activeTransfers": len(self._sessions),
        }

    def _emit_status(self):
        self._em.emit("tftp.status", {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "root": self._root,
        })

    def _emit_transfer(self, session: dict, force: bool = False):
        now = _now()
        st = session
        # 节流：活跃中未完成时最多每 PROGRESS_THROTTLE 推送一次
        if not force and st["status"] == "active":
            if now and hasattr(self, "_last_pushed"):
                pass
            key = st["id"]
            if not force:
                last = getattr(self, "_last_pushed_ts", {}).get(key, 0)
                if float(datetime.now().timestamp()) - last < PROGRESS_THROTTLE:
                    return
        self._em.emit("tftp.transfer", {
            "id": st["id"], "role": st["role"], "dir": st["dir"],
            "peer": f"{st['peer'][0]}:{st['peer'][1]}" if isinstance(st["peer"], tuple) else str(st["peer"]),
            "filename": st["filename"], "total": st["total"],
            "transferred": st["transferred"],
            "status": st["status"], "error": st["error"],
        })
        if st["status"] == "active":
            ts = getattr(self, "_last_pushed_ts", {})
            ts[st["id"]] = datetime.now().timestamp()
            object.__setattr__(self, "_last_pushed_ts", ts)

    def _log(self, level: str, message: str):
        self._em.log(level, message)
        self._em.emit("tftp.log", {"level": level, "message": message})


class _ServerRequestProto(asyncio.DatagramProtocol):
    """主端口：收到请求回调 backend.on_request"""

    def __init__(self, backend: "TftpServerBackend"):
        self._backend = backend

    def datagram_received(self, data: bytes, addr):
        self._backend.on_request(data, addr)

    def error_received(self, exc):
        self._backend._em.log("warning", f"Network TFTP Server 收包异常: {exc}")


def _resolve_ip(host: str) -> str | None:
    """将主机名/域名解析为 IPv4 地址（供 UDP 来源过滤与建连）"""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


# ── TFTP 客户端（主动向远端服务器上传/下载） ────────────────


class TftpClientBackend:
    """单次活跃传输的 TFTP 客户端（download / upload）"""

    def __init__(self, event_manager):
        self._em = event_manager
        self._task: asyncio.Task | None = None
        self._session: dict | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def download(self, host: str, port: int, remote: str, local: str) -> dict:
        if not remote or not local:
            return {"success": False, "error": "远程/本地文件名不能为空"}
        if self.busy:
            return {"success": False, "error": "已有传输在进行，请先取消或等待完成"}
        await self._cancel_task()
        if not host:
            return {"success": False, "error": "未指定服务器地址"}
        ip_host = _resolve_ip(host)
        if ip_host is None:
            return {"success": False, "error": f"无法解析服务器地址: {host}"}

        session = {
            "id": "client:" + uuid.uuid4().hex[:8], "role": "client", "dir": "down",
            "remote": remote, "local": local, "host": host, "ip": ip_host,
            "peer_port": port, "total": 0, "transferred": 0,
            "status": "pending", "error": None, "cancel": False,
        }
        self._session = session
        self._emit_progress(session, force=True)
        self._log("info", f"TFTP Client 开始下载 {remote} ← {host}:{port}")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            transport = None
            try:
                _transport, _ = await loop.create_datagram_endpoint(
                    lambda: _SessionProto(queue), local_addr=("0.0.0.0", 0))
                transport = _transport
                session["transport"] = transport
                await self._do_download(session, transport, queue)
            finally:
                if transport is not None:
                    transport.close()
                await self._emit_done(session)

        self._task = asyncio.create_task(_run(), name="tftp-client")
        session["task"] = self._task
        return {"success": True}

    async def upload(self, host: str, port: int, local: str, remote: str) -> dict:
        if not remote or not local:
            return {"success": False, "error": "远程/本地文件名不能为空"}
        if not os.path.isfile(local):
            return {"success": False, "error": f"本地文件不存在: {local}"}
        if self.busy:
            return {"success": False, "error": "已有传输在进行，请先取消或等待完成"}
        await self._cancel_task()
        if not host:
            return {"success": False, "error": "未指定服务器地址"}
        ip_host = _resolve_ip(host)
        if ip_host is None:
            return {"success": False, "error": f"无法解析服务器地址: {host}"}

        try:
            total_size = os.path.getsize(local)
        except OSError as e:
            return {"success": False, "error": str(e)}

        session = {
            "id": "client:" + uuid.uuid4().hex[:8], "role": "client", "dir": "up",
            "remote": remote, "local": local, "host": host, "ip": ip_host,
            "peer_port": port, "total": total_size, "transferred": 0,
            "status": "pending", "error": None, "cancel": False,
        }
        self._session = session
        self._emit_progress(session, force=True)
        self._log("info", f"TFTP Client 开始上传 {local} → {host}:{port} 为 {remote}")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def _run():
            transport = None
            try:
                _transport, _ = await loop.create_datagram_endpoint(
                    lambda: _SessionProto(queue), local_addr=("0.0.0.0", 0))
                transport = _transport
                session["transport"] = transport
                await self._do_upload(session, transport, queue)
            finally:
                if transport is not None:
                    transport.close()
                await self._emit_done(session)

        self._task = asyncio.create_task(_run(), name="tftp-client")
        session["task"] = self._task
        return {"success": True}

    async def cancel(self) -> dict:
        was = self._session
        if was is not None:
            was["cancel"] = True
            was["status"] = "cancelled"
        await self._emit_done(was)
        return {"success": True}

    async def _cancel_task(self):
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def peer_addr(self, session: dict) -> tuple[str, int]:
        return (session["host"], session["peer_port"])

    async def _do_download(self, session, transport, q):
        """客户端收数据：发 RRQ → 收 DATA → ACK → ... → 完成"""
        host = session["host"]
        ip_host = session["ip"]
        peer = (ip_host, session["peer_port"])
        filename = session["remote"]
        local = session["local"]
        tmp = local + f".tftp_tmp_{uuid.uuid4().hex[:6]}"

        # 发 RRQ
        transport.sendto(pack_rq(OP_RRQ, filename.encode("utf-8")), peer)

        # 服务器会从临时端口回包，因此以其源地址作为会话对端（ACK 发回该地址）
        learned: tuple | None = None
        expected = 1
        total = 0
        try:
            with open(tmp, "wb") as f:
                while True:
                    if session["cancel"] or session["status"] in ("cancelled", "error"):
                        session["status"] = "cancelled"
                        return
                    resp, src = await _recv_from(q, ip_host, RETX_TIMEOUT)
                    if resp is None:
                        # 超时：未学到服务器端口则重发 RRQ，否则重发最后 ACK 催重传
                        transport.sendto(
                            pack_rq(OP_RRQ, filename.encode("utf-8")) if learned is None
                            else pack_ack(expected - 1),
                            peer if learned is None else learned,
                        )
                        continue
                    if len(resp) < 2:
                        continue
                    op = struct.unpack("!H", resp[:2])[0]
                    if op == OP_ERROR:
                        err_msg = resp[4:].split(b"\x00")[0].decode(errors="replace") if len(resp) > 4 else ""
                        raise RuntimeError(f"服务器返回错误: {err_msg or '未知'}")
                    if op != OP_DATA:
                        continue
                    if len(resp) < 4:
                        continue
                    # 记录服务器传输端口（DATA 包来源 = 服务器临时 socket）
                    if src is not None:
                        learned = (src[0], src[1])
                    block = struct.unpack("!HH", resp[:4])[1]
                    chunk = resp[4:]
                    if block == expected:
                        f.write(chunk)
                        total += len(chunk)
                        session["total"] = total
                        session["transferred"] = total
                        session["status"] = "active"
                        self._emit_progress(session)
                        transport.sendto(pack_ack(block), learned or peer)
                        expected += 1
                        if len(chunk) < BLKSIZE:
                            break
                    else:
                        # 重复块：重发该块的 ACK 催重传
                        transport.sendto(pack_ack(block), learned or peer)
        except RuntimeError as e:
            self._cleanup(tmp)
            raise e
        except OSError as e:
            self._cleanup(tmp)
            raise e

        # 成功：原子落地
        session["transferred"] = total
        try:
            if os.path.exists(local):
                os.remove(local)
            os.replace(tmp, local)
        except OSError as e:
            await self._abort_session(session, f"本地落盘失败: {e}")
            return
        session["total"] = total
        session["status"] = "done"
        self._log("info", f"TFTP Client 下载完成 {filename}（{total} 字节）→ {local}")

    async def _do_upload(self, session, transport, q):
        """客户端发数据：发 WRQ → 收 ACK(0) → 发 DATA 分块 → ..."""
        host = session["host"]
        ip_host = session["ip"]
        peer = (ip_host, session["peer_port"])
        filename = session["remote"]
        local = session["local"]

        transport.sendto(pack_rq(OP_WRQ, filename.encode("utf-8")), peer)
        send_to = peer

        # 等待 ACK(0)，并从来源学习服务器传输端口
        ack0 = False
        for _ in range(MAX_RETRIES):
            if session["cancel"]:
                session["status"] = "cancelled"
                return
            resp, src = await _recv_from(q, ip_host, RETX_TIMEOUT)
            if resp is None:
                transport.sendto(pack_rq(OP_WRQ, filename.encode("utf-8")), peer)
                continue
            if len(resp) < 4:
                continue
            op = struct.unpack("!H", resp[:2])[0]
            if op == OP_ACK and struct.unpack("!HH", resp[:4])[1] == 0:
                if src is not None:
                    send_to = (src[0], src[1])
                ack0 = True
                break
            if op == OP_ERROR:
                err_msg = resp[4:].split(b"\x00")[0].decode(errors="replace") if len(resp) > 4 else ""
                session["status"] = "error"; session["error"] = f"服务器返回错误: {err_msg or '未知'}"
                return
        if not ack0:
            session["status"] = "error"; session["error"] = "服务器无响应（未收到 ACK0）"
            return

        block = 0
        try:
            with open(local, "rb") as f:
                while True:
                    chunk = f.read(BLKSIZE)
                    if chunk:
                        block += 1
                        sent_total = min(session["total"], block * BLKSIZE)
                        ok = await self._send_data_wait_ack(session, transport, q, ip_host, send_to, block, chunk)
                        session["transferred"] = sent_total
                        self._emit_progress(session)
                        if not ok:
                            session["status"] = "error"; session["error"] = session.get("error") or "对端无响应"
                            return
                        if len(chunk) < BLKSIZE:
                            break
                    else:
                        # EOF 边界（整块倍数/空文件）：发 0 长度 DATA 结尾
                        block += 1
                        ok = await self._send_data_wait_ack(session, transport, q, ip_host, send_to, block, b"")
                        session["transferred"] = session["total"]
                        self._emit_progress(session)
                        if not ok:
                            session["status"] = "error"; session["error"] = session.get("error") or "对端无响应"
                            return
                        break
        except OSError as e:
            session["status"] = "error"; session["error"] = f"本地文件读取失败: {e}"
            return
        session["status"] = "done"
        session["transferred"] = session["total"]
        self._log("info", f"TFTP Client 上传完成 {local} → {filename}（{session['total']} 字节）")

    async def _send_data_wait_ack(self, session, transport, q, peer_ip, send_to, block, chunk) -> bool:
        packet = pack_data(block, chunk)
        for _ in range(MAX_RETRIES):
            if session.get("cancel") or session["status"] == "cancelled":
                session["status"] = "cancelled"
                return False
            try:
                transport.sendto(packet, send_to)
            except Exception:
                return False
            resp, _ = await _recv_from(q, peer_ip, RETX_TIMEOUT)
            if resp is None:
                continue
            if len(resp) < 4:
                continue
            op = struct.unpack("!H", resp[:2])[0]
            if op == OP_ACK:
                ack_block = struct.unpack("!HH", resp[:4])[1]
                if ack_block == block:
                    return True
                continue
            if op == OP_ERROR:
                err_msg = resp[4:].split(b"\x00")[0].decode(errors="replace") if len(resp) > 4 else ""
                session["error"] = f"服务器返回错误: {err_msg or '未知'}"
                return False
        session["error"] = "对端无响应（重传耗尽）"
        return False

    async def _abort_session(self, session, error: str):
        session["status"] = "error"
        session["error"] = error
        self._log("error", f"TFTP Client 传输失败: {error}")

    def _cleanup(self, tmp_path: str):
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

    async def _emit_done(self, session: dict | None):
        if session is None:
            return
        task = session.get("task")
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if session["status"] == "error":
            self._log("error", f"TFTP Client 传输失败: {session.get('error') or '未知错误'}")
        elif session["status"] == "cancelled":
            self._log("warning", "TFTP Client 传输已取消")
        self._emit_progress(session, force=True)

    def _emit_progress(self, session: dict, force: bool = False):
        if not force and session["status"] == "active":
            last = getattr(self, "_last_pushed_ts", 0)
            if datetime.now().timestamp() - last < PROGRESS_THROTTLE:
                return
        self._em.emit("tftp.transfer", {
            "id": session["id"], "role": session["role"], "dir": session["dir"],
            "peer": f"{session['host']}:{session['peer_port']}",
            "filename": session.get("remote") or session.get("filename") or "",
            "total": session["total"], "transferred": session["transferred"],
            "status": session["status"], "error": session.get("error"),
        })
        object.__setattr__(self, "_last_pushed_ts", datetime.now().timestamp())

    def _log(self, level: str, message: str):
        self._em.log(level, message)
        self._em.emit("tftp.log", {"level": level, "message": message})

    def status(self) -> dict:
        s = self._session
        if s is None:
            return {"busy": self.busy, "active": None}
        return {
            "busy": self.busy,
            "active": {
                "id": s["id"], "dir": s["dir"],
                "peer": f"{s['host']}:{s['peer_port']}",
                "filename": s.get("remote") or s.get("filename") or "",
                "total": s["total"], "transferred": s["transferred"],
                "status": s["status"], "error": s.get("error"),
            },
        }