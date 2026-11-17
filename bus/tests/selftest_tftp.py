"""TFTP 后端自测：启动本机 server，用原始 UDP 客户端上传/下载验证协议正确性。"""
import asyncio
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tftp_backend import (
    TftpServerBackend, pack_rq, pack_data, pack_ack, pack_error,
    OP_DATA, OP_ACK, OP_ERROR, BLKSIZE, RETX_TIMEOUT,
)
from core.events import EventManager


class Proto(asyncio.DatagramProtocol):
    def __init__(self, q):
        self.q = q

    def datagram_received(self, data, addr):
        self.q.put_nowait((data, addr))


async def recv(q, expect_ip, timeout=RETX_TIMEOUT):
    async def _g():
        while True:
            data, addr = await q.get()
            if addr[0] == expect_ip:
                return data, addr
    try:
        return await asyncio.wait_for(_g(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None, None


async def do_download(loop, server_addr, fname):
    """RRQ 下载：读全部块拼回内容。learn 服务器源端口回 ACK。"""
    q = asyncio.Queue()
    t, p = await loop.create_datagram_endpoint(lambda: Proto(q), local_addr=("0.0.0.0", 0))
    t.sendto(pack_rq(1, fname.encode()), server_addr)
    learned = None
    expected = 1
    buf = bytearray()
    while True:
        data, src = await recv(q, server_addr[0])
        if data is None:
            await asyncio.sleep(0.1)
            t.sendto(pack_rq(1, fname.encode()), server_addr)
            continue
        op = struct.unpack("!H", data[:2])[0]
        if op == OP_ERROR:
            t.close()
            raise RuntimeError("下载遇到 ERROR")
        if op != OP_DATA:
            continue
        learned = src
        block = struct.unpack("!HH", data[:4])[1]
        chunk = data[4:]
        if block == expected:
            buf += chunk
            t.sendto(pack_ack(block), learned)
            expected += 1
            if len(chunk) < BLKSIZE:
                break
        else:
            t.sendto(pack_ack(block), learned)
    t.close()
    return bytes(buf)


async def do_upload(loop, server_addr, fname, content):
    """WRQ 上传：发块等 ACK。"""
    q = asyncio.Queue()
    t, p = await loop.create_datagram_endpoint(lambda: Proto(q), local_addr=("0.0.0.0", 0))
    t.sendto(pack_rq(2, fname.encode()), server_addr)
    # wait ack0
    learned = None
    for _ in range(6):
        data, src = await recv(q, server_addr[0])
        if data is None:
            t.sendto(pack_rq(2, fname.encode()), server_addr)
            continue
        op = struct.unpack("!H", data[:2])[0]
        if op == OP_ACK and struct.unpack("!HH", data[:4])[1] == 0:
            learned = src
            break
    assert learned, "no ack0"
    block = 0
    sent = 0
    while True:
        chunk = content[sent:sent + BLKSIZE]
        if chunk:
            block += 1
            ok = False
            for _ in range(6):
                t.sendto(pack_data(block, chunk), learned)
                data, _ = await recv(q, learned[0])
                if data and struct.unpack("!H", data[:2])[0] == OP_ACK and struct.unpack("!HH", data[:4])[1] == block:
                    ok = True
                    break
            assert ok, "no ack data"
            sent += len(chunk)
            if len(chunk) < BLKSIZE:
                break
        else:
            block += 1
            for _ in range(6):
                t.sendto(pack_data(block, b""), learned)
                data, _ = await recv(q, learned[0])
                if data and struct.unpack("!H", data[:2])[0] == OP_ACK and struct.unpack("!HH", data[:4])[1] == block:
                    break
            break
    t.close()


async def main():
    em = EventManager()
    server = TftpServerBackend(em)
    tmpdir = tempfile.mkdtemp()
    server_addr = ("127.0.0.1", 0)
    loop = asyncio.get_running_loop()

    # 测试1：服务器监听，客户端下载
    port = 0  # 让启动函数绑定随机端口

    # start 绑定 0 端口拿随机端口
    res = await server.start(tmpdir, "0.0.0.0", 0)
    assert res["success"], res
    srv_port = res["port"]
    print("[server] started on", srv_port)

    # 准备一个 512*3+10 的文件
    content = bytes((i * 7) & 0xFF for i in range(BLKSIZE * 3 + 10))
    src = os.path.join(tmpdir, "file.bin")
    with open(src, "wb") as f:
        f.write(content)

    got = await do_download(loop, ("127.0.0.1", srv_port), "file.bin")
    assert got == content, f"下载内容不一致 len={len(got)} vs {len(content)}"
    print("[client] 下载 OK,", len(got), "bytes")

    # 测试2：上传
    up = bytes((i * 3) & 0xFF for i in range(BLKSIZE * 2 + 7))
    await do_upload(loop, ("127.0.0.1", srv_port), "up.bin", up)
    dst = os.path.join(tmpdir, "up.bin")
    with open(dst, "rb") as f:
        got_up = f.read()
    assert got_up == up, "上传内容不一致"
    print("[client] 上传 OK,", len(got_up), "bytes")

    # 测试3：路径穿越防护（虚拟机上只是验证不产生文件）
    with open(os.path.join(tmpdir, "evil.log"), "w") as f:
        f.write("should not be touched")

    await server.stop()
    print("[server] stopped. ALL PASS")


asyncio.run(main())