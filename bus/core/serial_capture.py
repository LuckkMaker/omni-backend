"""串口接收环形缓存（每个连接一个）。

与 link 服务的 RTT 缓存（``link/core/rtt_backend.py::RttCapture``）保持同一套
读取契约——``oldest_seq / next_seq / cursor / lost / truncated / session_reset /
dropped_*``——这样 CLI 侧的流式命令（rtt / serial / 将来的 net）只需实现一份
读取与 ``--follow`` 逻辑。

- 读取线程每刷新一批接收数据就 ``append(data)``，每批分配一个单调 ``seq``；
  读端只需记住上次返回的 ``cursor`` 即可增量读取，无需服务端消费游标。
- ``seq`` 在同一后端进程内跨 串口重开 单调递增：``close`` 不销毁实例（见
  SerialManager.close），新 open 复用同一个 capture，旧游标仍安全。
  只有后端重启才从 1 开始（读端经 ``session_reset`` 得知）。
- 总字节数超过 ``max_bytes`` 时丢弃最旧的整批，并累计 ``dropped_bytes`` /
  ``dropped_chunks``；读端通过 ``lost`` 得知自己请求的 ``since`` 已被覆盖。
"""

from __future__ import annotations

import base64
import threading
from collections import deque

#: 环形缓存容量：1 MiB。高速率（如 921600 baud ≈ 92 kB/s）下约可回溯 10 秒。
CAPTURE_MAX_BYTES = 1 * 1024 * 1024
#: 单次读取的字节预算上限（防止 --max-bytes 传天文数字拖垮响应）
CAPTURE_MAX_READ = 4 * 1024 * 1024


class SerialCapture:
    """串口接收数据的环形缓存（线程安全：读取线程 append，API 线程 read）。"""

    def __init__(self, max_bytes: int = CAPTURE_MAX_BYTES, start_seq: int = 1):
        self._max_bytes = max(1, int(max_bytes))
        self._chunks: deque = deque()
        self._bytes = 0
        self._next_seq = max(1, int(start_seq))
        self._dropped_bytes = 0
        self._dropped_chunks = 0
        self._lock = threading.Lock()

    # ── 写端（串口读取线程） ───────────────────────────────────
    def append(self, data: bytes) -> int:
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
            self._chunks.append({"seq": seq, "data": data})
            self._bytes += len(data)
            return seq

    # ── 读端（REST / CLI） ────────────────────────────────────
    def stats(self) -> dict:
        """缓存概览（供 /api/serial/status 与 CLI 诊断）。"""
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
        """下一个将分配的 seq（供跨重开的续号）。"""
        with self._lock:
            return self._next_seq

    def read(
        self,
        since: int = 0,
        max_bytes: int = CAPTURE_MAX_READ,
    ) -> dict:
        """按 seq 增量读取。

        Args:
            since: 起始 seq（0 = 从当前最旧可用批开始）
            max_bytes: 单次返回的字节预算。按整批返回（不拆批），因此实际可能
                略超；只有在“已选到至少一批”之后才会因预算截断，绝不丢批内数据。

        Returns:
            {oldest_seq, next_seq, last_seq, cursor, truncated, lost, session_reset,
             dropped_bytes, dropped_chunks, chunks:[{seq, size, data(base64)}]}

            session_reset: since 超出当前后端进程的序号（后端重启后 seq 从 1 开始），
                本次已从当前最旧数据重读。
        """
        limit = max(1, min(int(max_bytes), CAPTURE_MAX_READ))
        with self._lock:
            chunks = list(self._chunks)
            next_seq = self._next_seq
            dropped_bytes = self._dropped_bytes
            dropped_chunks = self._dropped_chunks

        oldest_seq = chunks[0]["seq"] if chunks else next_seq
        # since 大于 next_seq 只可能因为后端重启（seq 从 1 重新开始），或调用方
        # 使用了明显超前的游标。此时从当前最旧数据重读，并通过 session_reset 告知，
        # 而不是静默返回 0 字节。
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
            size = len(chunk["data"])
            if selected and used + size > limit:
                truncated = True
                break
            selected.append(chunk)
            used += size

        last_seq = selected[-1]["seq"] if selected else max(start - 1, 0)
        # 预算截断时必须从下一批续读；否则说明已把截至 next_seq 的数据全部看过，
        # 直接推进到 next_seq。
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
                    "size": len(c["data"]),
                    "data": base64.b64encode(c["data"]).decode("ascii"),
                }
                for c in selected
            ],
        }
