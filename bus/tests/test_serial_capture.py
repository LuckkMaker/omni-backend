"""SerialCapture 环形缓存离线测试（无需硬件、无需 FastAPI）。

对齐 link 服务 RttCapture 的语义（omni-cli tests/test_cli_fixes.py 第 1 节），
serial 版没有 channel 维度，重点覆盖：

1. seq 单调递增、cursor 连续读取（多客户端互不影响）
2. 字节预算按整批截断（不拆批、不丢批内数据）
3. 容量上限丢弃最旧整批，lost 告知游标已被覆盖
4. 单批超过总容量只保留最新部分
5. 跨 close/open 的 seq 续号由 SerialManager 保证（实例不销毁）

运行::

    python bus/tests/test_serial_capture.py
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.serial_capture import CAPTURE_MAX_BYTES, CAPTURE_MAX_READ, SerialCapture  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(name)
    print(f"  {status}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def _join(result: dict) -> bytes:
    import base64
    return b"".join(base64.b64decode(c["data"]) for c in result["chunks"])


def test_seq_and_cursor():
    print("[1] seq 单调 + 游标连续读取")
    cap = SerialCapture()
    cap.append(b"AAA")   # seq 1
    cap.append(b"BBB")   # seq 2
    cap.append(b"CCC")   # seq 3

    first = cap.read()
    check("首次读取拿到全部 3 批", len(first["chunks"]) == 3, str(len(first["chunks"])))
    check("chunks 按序携带数据", _join(first) == b"AAABBBCCC", repr(_join(first)))
    check("chunks 携带 size", first["chunks"][0]["size"] == 3)
    check("无截断/无丢失", not first["truncated"] and not first["lost"])
    check("读毕游标 = next_seq", first["cursor"] == 4 == first["next_seq"])
    check("last_seq = 3", first["last_seq"] == 3)

    second = cap.read(since=first["cursor"])
    check("游标续读为空（不重复消费）", second["chunks"] == [], repr(second["chunks"]))

    # 多客户端：另一个游标从 2 开始，只看到 seq 2/3，互不影响
    other = cap.read(since=2)
    check("第二个客户端独立游标", _join(other) == b"BBBCCC", repr(_join(other)))


def test_budget_truncate():
    print("[2] 字节预算整批截断")
    cap = SerialCapture()
    cap.append(b"A" * 100)   # seq 1
    cap.append(b"B" * 100)   # seq 2
    cap.append(b"C" * 100)   # seq 3

    # 对齐 RTT 契约：首批总是整批返回，之后的批装不下即截断
    r = cap.read(since=1, max_bytes=150)
    check("预算 150 只装下第一批", len(r["chunks"]) == 1, str(len(r["chunks"])))
    check("truncated 标记置位", r["truncated"])
    check("截断时游标指向下一批", r["cursor"] == 2 and r["last_seq"] == 1,
          f"cursor={r['cursor']} last={r['last_seq']}")

    r2 = cap.read(since=2, max_bytes=250)
    check("预算 250 装下第二、三批", _join(r2) == b"B" * 100 + b"C" * 100, repr(_join(r2)[:20]))
    check("预算内读完则游标 = next_seq", r2["cursor"] == 4,
          f"cursor={r2['cursor']}")

    cont = cap.read(since=2, max_bytes=150)
    check("从截断游标续读不重复不跳批", len(cont["chunks"]) == 1 and cont["chunks"][0]["seq"] == 2,
          str([c["seq"] for c in cont["chunks"]]))


def test_eviction_lost():
    print("[3] 容量上限丢最旧整批 + lost")
    cap = SerialCapture(max_bytes=256)
    cap.append(b"X" * 200)   # seq 1
    cap.append(b"Y" * 100)   # seq 2：挤掉 seq 1
    stats = cap.stats()
    check("dropped_chunks = 1", stats["dropped_chunks"] == 1, str(stats))
    check("dropped_bytes 计入 200", stats["dropped_bytes"] == 200)
    check("oldest_seq 已推进到 2", stats["oldest_seq"] == 2)

    r = cap.read(since=1)
    check("过期游标 => lost 置位", r["lost"])
    check("lost 时从最旧可用数据重读", _join(r) == b"Y" * 100)

    r2 = cap.read(since=2)
    check("有效游标不受影响", not r2["lost"] and _join(r2) == b"Y" * 100)


def test_single_chunk_over_capacity():
    print("[4] 单批超过总容量只保留最新部分")
    cap = SerialCapture(max_bytes=64)
    cap.append(b"Z" * 200)   # seq 1：只留最新 64 字节
    r = cap.read()
    check("只返回 64 字节", len(_join(r)) == 64, str(len(_join(r))))
    check("dropped_bytes 计入 136", r["dropped_bytes"] == 136, str(r["dropped_bytes"]))
    check("游标照常推进到 next_seq", r["cursor"] == 2)


def test_session_reset():
    print("[5] 超前游标 => session_reset（后端重启语义）")
    cap = SerialCapture()
    cap.append(b"NEW")
    r = cap.read(since=999)   # 超出当前进程序号范围
    check("session_reset 置位", r["session_reset"])
    check("已从最旧数据重读", _join(r) == b"NEW")
    r2 = cap.read(since=1)
    check("进程内合法游标不会误报 reset", not r2["session_reset"])


def test_empty_and_stats():
    print("[6] 空缓存与统计")
    cap = SerialCapture()
    r = cap.read()
    check("空缓存返回空 chunks", r["chunks"] == [])
    check("空缓存游标 = next_seq", r["cursor"] == r["next_seq"] == 1)
    s = cap.stats()
    check("stats 字段齐全",
          set(s) == {"oldest_seq", "next_seq", "buffered_bytes", "dropped_bytes", "dropped_chunks"},
          str(sorted(s)))


def test_thread_safety_smoke():
    print("[7] 并发 append/read 冒烟（有界生产者，避免忙循环饿死读端）")
    cap = SerialCapture()
    batches = 2000
    produced = []

    def _producer():
        for _ in range(batches):
            cap.append(b"ab" * 32)
            produced.append(1)

    t = threading.Thread(target=_producer)
    t.start()
    total = 0
    for _ in range(50):
        result = cap.read(since=max(1, cap.stats()["oldest_seq"]), max_bytes=CAPTURE_MAX_READ)
        total += len(_join(result))
    t.join()
    check("并发读写不抛异常且能取到数据", total > 0, str(total))
    check("生产者全部批次已入缓存", sum(produced) == batches)
    final = cap.stats()
    check("并发后 stats 一致（dropped 计入淘汰）",
          final["buffered_bytes"] <= CAPTURE_MAX_BYTES
          and final["dropped_bytes"] + final["buffered_bytes"] == batches * 64,
          str(final))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass
    test_seq_and_cursor()
    test_budget_truncate()
    test_eviction_lost()
    test_single_chunk_over_capacity()
    test_session_reset()
    test_empty_and_stats()
    test_thread_safety_smoke()
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
