"""NetCapture 环形缓存离线测试（无需硬件、无需 FastAPI）。

与 serial 的 SerialCapture 同一套游标契约，差异点是每批带 ``peer``（对端地址）
并支持按 peer 过滤。覆盖：

1. seq 单调 + 游标连续读取（多客户端互不影响）
2. peer 过滤：只看某连接的数据，cursor 仍推进到 next_seq
3. 字节预算整批截断（首批总是完整返回）
4. 容量上限丢最旧整批 + lost
5. 单批超过总容量只保留最新部分
6. 超前游标 => session_reset
7. 并发 append/read 冒烟

运行::

    python bus/tests/test_net_capture.py
"""

import base64
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.net_capture import CAPTURE_MAX_BYTES, CAPTURE_MAX_READ, NetCapture  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(name)
    print(f"  {status}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def _join(result: dict) -> bytes:
    return b"".join(base64.b64decode(c["data"]) for c in result["chunks"])


def test_seq_and_cursor():
    print("[1] seq 单调 + 游标连续读取")
    cap = NetCapture()
    cap.append(b"AAA", peer="10.0.0.1:1000")   # seq 1
    cap.append(b"BBB", peer="10.0.0.2:2000")   # seq 2

    first = cap.read()
    check("首次读取拿到全部 2 批", len(first["chunks"]) == 2)
    check("数据按序", _join(first) == b"AAABBB", repr(_join(first)))
    check("chunks 携带 peer", [c["peer"] for c in first["chunks"]] ==
          ["10.0.0.1:1000", "10.0.0.2:2000"], str(first["chunks"]))
    check("读毕游标 = next_seq", first["cursor"] == 3 == first["next_seq"])
    check("续读为空", cap.read(since=first["cursor"])["chunks"] == [])


def test_peer_filter():
    print("[2] peer 过滤")
    cap = NetCapture()
    cap.append(b"A1", peer="10.0.0.1:1000")
    cap.append(b"B1", peer="10.0.0.2:2000")
    cap.append(b"A2", peer="10.0.0.1:1000")

    only_a = cap.read(peer="10.0.0.1:1000")
    check("只取该对端数据", _join(only_a) == b"A1A2", repr(_join(only_a)))
    check("过滤后 seq 保留原编号", [c["seq"] for c in only_a["chunks"]] == [1, 3],
          str([c["seq"] for c in only_a["chunks"]]))
    check("过滤后 cursor 仍推进到 next_seq", only_a["cursor"] == 4, str(only_a["cursor"]))

    only_b = cap.read(peer="10.0.0.2:2000")
    check("另一对端独立可读", _join(only_b) == b"B1", repr(_join(only_b)))
    check("未知对端返回空且游标推进", cap.read(peer="1.2.3.4:5")["chunks"] == [])


def test_budget_truncate():
    print("[3] 字节预算整批截断")
    cap = NetCapture()
    for i in range(3):
        cap.append(bytes([65 + i]) * 100, peer="p:1")

    r = cap.read(since=1, max_bytes=150)
    check("首批总是完整返回", len(r["chunks"]) == 1, str(len(r["chunks"])))
    check("truncated 置位", r["truncated"])
    check("截断游标指向下一批", r["cursor"] == 2 and r["last_seq"] == 1,
          f"cursor={r['cursor']} last={r['last_seq']}")

    cont = cap.read(since=r["cursor"], max_bytes=150)
    check("续读不重复不跳批", cont["chunks"][0]["seq"] == 2, str(cont["chunks"][0]["seq"]))


def test_eviction_lost():
    print("[4] 容量上限丢最旧整批 + lost")
    cap = NetCapture(max_bytes=256)
    cap.append(b"X" * 200, peer="p:1")   # seq 1
    cap.append(b"Y" * 100, peer="p:1")   # seq 2：挤掉 seq 1
    stats = cap.stats()
    check("dropped_chunks = 1", stats["dropped_chunks"] == 1, str(stats))
    check("dropped_bytes 计入 200", stats["dropped_bytes"] == 200)
    check("oldest_seq 推进到 2", stats["oldest_seq"] == 2)

    r = cap.read(since=1)
    check("过期游标 => lost", r["lost"])
    check("lost 时从最旧数据重读", _join(r) == b"Y" * 100)


def test_single_chunk_over_capacity():
    print("[5] 单批超过总容量只保留最新部分")
    cap = NetCapture(max_bytes=64)
    cap.append(b"Z" * 200, peer="p:1")
    r = cap.read()
    check("只返回 64 字节", len(_join(r)) == 64, str(len(_join(r))))
    check("dropped_bytes 计入 136", r["dropped_bytes"] == 136)


def test_session_reset():
    print("[6] 超前游标 => session_reset")
    cap = NetCapture()
    cap.append(b"NEW", peer="p:1")
    r = cap.read(since=999)
    check("session_reset 置位", r["session_reset"])
    check("已从最旧数据重读", _join(r) == b"NEW")
    check("合法游标不误报", not cap.read(since=1)["session_reset"])


def test_thread_safety_smoke():
    print("[7] 并发 append/read 冒烟")
    cap = NetCapture()
    batches = 2000
    done = []

    def _producer():
        for _ in range(batches):
            cap.append(b"ab" * 32, peer="p:1")
        done.append(True)

    t = threading.Thread(target=_producer)
    t.start()
    total = 0
    for _ in range(50):
        total += len(_join(cap.read(since=max(1, cap.stats()["oldest_seq"]),
                                    max_bytes=CAPTURE_MAX_READ)))
    t.join()
    check("并发读写不抛异常且能取到数据", total > 0, str(total))
    check("生产者完成", done == [True])
    final = cap.stats()
    check("并发后容量不超上限", final["buffered_bytes"] <= CAPTURE_MAX_BYTES, str(final))
    check("字节守恒（缓冲 + 丢弃 = 总产出）",
          final["buffered_bytes"] + final["dropped_bytes"] == batches * 64, str(final))


def test_ping_console_decoding():
    """回归：中文 Windows 的 ping 输出是 GBK，解码错会把「时间」吞掉，时延统计全空。"""
    print("[8] ping 控制台解码（GBK / UTF-8）")
    import importlib
    import core.network_backend as nb
    importlib.reload(nb)

    gbk_line = "来自 127.0.0.1 的回复: 字节=32 时间<1ms TTL=128".encode("gbk")
    text = nb._decode_console(gbk_line)
    check("GBK 输出可正确解码", "时间" in text, repr(text))
    m = nb._RE_TIME.search(text)
    check("解码后能解析出时延", m is not None and m.group(1) == "1", repr(text))

    en_line = b"Reply from 127.0.0.1: bytes=32 time=3ms TTL=128"
    text = nb._decode_console(en_line)
    m = nb._RE_TIME.search(text)
    check("英文输出照常解析", m is not None and m.group(1) == "3", repr(text))

    # 无法解码的字节不应抛异常
    junk = b"\xff\xfe time=7ms"
    check("非法字节不抛异常", isinstance(nb._decode_console(junk), str))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass
    test_seq_and_cursor()
    test_peer_filter()
    test_budget_truncate()
    test_eviction_lost()
    test_single_chunk_over_capacity()
    test_session_reset()
    test_thread_safety_smoke()
    test_ping_console_decoding()
    print()
    if failures:
        print(f"FAILED: {len(failures)} 项")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
