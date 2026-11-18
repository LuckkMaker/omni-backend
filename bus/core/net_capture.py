"""网络接收环形缓存（TCP Server / TCP Client / UDP 各一个）。

与串口的 ``serial_capture.py``、link 服务的 RTT 缓存保持同一套读取契约
（``oldest_seq / next_seq / cursor / lost / truncated / session_reset /
dropped_*``）——CLI 侧的流式命令（rtt / serial / net）共用一份读取实现。

差异点：每批数据携带 ``peer``（TCP 对端/UDP 来源的 ``host:port``），
支持按 peer 过滤——TCP Server 多客户端并发时只看其中一个连接的数据。
"""

from __future__ import annotations

import base64
import threading
from collections import deque

#: 环形缓存容量：1 MiB
CAPTURE_MAX_BYTES = 1 * 1024 * 1024
#: 单次读取的字节预算上限
CAPTURE_MAX_READ = 4 * 1024 * 1024


class NetCapture:
    """网络接收数据的环形缓存（线程安全：asyncio 收包 append，API 线程 read）。"""

    def __init__(self, max_bytes: int = CAPTURE_MAX_BYTES, start_seq: int = 1):
        self._max_bytes = max(1, int(max_bytes))
        self._chunks: deque = deque()
        self._bytes = 0
        self._next_seq = max(1, int(start_seq))
        self._dropped_bytes = 0
        self._dropped_chunks = 0
        self._lock = threading.Lock()

    # ── 写端（网络收包路径） ───────────────────────────────────
    def append(self, data: bytes, peer: str = "") -> int:
        """追加一批接收数据，返回分配到的 seq（空数据返回 0）。"""
        if not data:
            return 0
        data = bytes(data)
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            # 单批超过整个缓存容量：只保留最新的一部分
            if len(data) > self._max_bytes:
                dropped = len(data) - self._max_bytes
                self._dropped_bytes += dropped
                data = data[-self._max_bytes:]
            while self._chunks and self._bytes + len(data) > self._max_bytes:
                old = self._chunks.popleft()
                self._bytes -= len(old["data"])
                self._dropped_bytes += len(old["data"])
                self._dropped_chunks += 1
            self._chunks.append({"seq": seq, "peer": str(peer), "data": data})
            self._bytes += len(data)
            return seq

    # ── 读端（REST / CLI） ────────────────────────────────────
    def stats(self) -> dict:
        """缓存概览（供 status 与 CLI 诊断）。"""
        with self._lock:
            oldest = self._chunks[0]["seq"] if self._chunks else self._next_seq
            return {
                "oldest_seq": oldest,
                "next_seq": self._next_seq,
                "buffered_bytes": self._bytes,
                "dropped_bytes": self._dropped_bytes,
                "dropped_chunks": self._dropped_chunks,
            }

    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    def read(
        self,
        since: int = 0,
        max_bytes: int = CAPTURE_MAX_READ,
        peer: str | None = None,
    ) -> dict:
        """按 seq 增量读取。

        Args:
            since: 起始 seq（0 = 从当前最旧可用批开始）
            max_bytes: 单次返回的字节预算（整批返回，首批总是完整返回）
            peer: 仅返回该对端的数据（None = 全部）。被过滤掉的批照常占预算
                跳过（cursor 推进不受过滤影响，客户端游标可安全混用）。

        Returns:
            与 serial/rtt 相同的游标契约，chunks 每批额外携带 ``peer``。
        """
        limit = max(1, min(int(max_bytes), CAPTURE_MAX_READ))
        with self._lock:
            chunks = list(self._chunks)
            next_seq = self._next_seq
            dropped_bytes = self._dropped_bytes
            dropped_chunks = self._dropped_chunks

        oldest_seq = chunks[0]["seq"] if chunks else next_seq
        # since 大于 next_seq 只可能因为后端重启（seq 从 1 重新开始）
        session_reset = bool(since and since > 0 and int(since) > next_seq)
        if session_reset:
            start = oldest_seq
            lost = False
        else:
            start = int(since) if since and since > 0 else oldest_seq
            lost = bool(since and since > 0 and start < oldest_seq)

        selected = []
        used = 0
        truncated = False
        for chunk in chunks:
            if chunk["seq"] < start:
                continue
            if peer is not None and chunk["peer"] != peer:
                continue
            size = len(chunk["data"])
            if selected and used + size > limit:
                truncated = True
                break
            selected.append(chunk)
            used += size

        last_seq = selected[-1]["seq"] if selected else max(start - 1, 0)
        # 预算截断时从下一批续读；否则推进到 next_seq（被 peer 过滤掉的批无需回看）
        cursor = last_seq + 1 if truncated else next_seq
        return {
            "oldest_seq": oldest_seq,
            "next_seq": next_seq,
            "last_seq": last_seq,
            "cursor": cursor,
            "truncated": truncated,
            "lost": lost,
            "session_reset": session_reset,
            "dropped_bytes": dropped_bytes,
            "dropped_chunks": dropped_chunks,
            "chunks": [
                {
                    "seq": c["seq"],
                    "peer": c["peer"],
                    "size": len(c["data"]),
                    "data": base64.b64encode(c["data"]).decode("ascii"),
                }
                for c in selected
            ],
        }
