"""手动 UI 冒烟测试：启动静态 renderer + Electron + Python 后端，通过 CDP 截图。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe python/tests/manual_ui_smoke.py

不依赖 USBPcap；页面会显示已安装状态（若本机已安装）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "out" / "renderer"
ELECTRON = ROOT / "node_modules" / "electron" / "dist" / "electron.exe"
CDP_PORT = 9223
HTTP_PORT = 5173
BACKEND_PORT = 8792


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # noqa: D401
        pass


def start_static() -> ThreadingHTTPServer:
    handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
        *args, directory=str(RENDERER), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_cdp(timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("CDP 未就绪")


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 1
        self.events: list[dict] = []

    async def call(self, method: str, params: dict | None = None):
        msg_id = self.next_id
        self.next_id += 1
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} failed: {data['error']}")
                return data.get("result", {})
            self.events.append(data)

    async def evaluate(self, expression: str):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")

    async def screenshot(self, path: Path):
        result = await self.call("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(result["data"]))

    def console_errors(self) -> list[str]:
        errors = []
        for event in self.events:
            method = event.get("method")
            params = event.get("params", {})
            if method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                args = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
                errors.append("console: " + " ".join(args))
            elif method == "Log.entryAdded" and params.get("entry", {}).get("level") == "error":
                errors.append("log: " + str(params["entry"].get("text")))
        return errors


async def run() -> None:
    pages = wait_cdp()
    page = pages[0]
    print("page:", page.get("title"), page.get("url"))
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.evaluate("location.hash = '#/usb'")
        await asyncio.sleep(1.0)
        print("hash:", await cdp.evaluate("location.href"))
        clicked = await cdp.evaluate(
            "(() => { const a = [...document.querySelectorAll('a')].find(x => x.textContent.trim() === 'USB'); if (!a) return 'no-link'; a.click(); return a.getAttribute('href'); })()"
        )
        print("clicked:", clicked)
        await asyncio.sleep(2.0)
        text = await cdp.evaluate("document.body.innerText")
        print("----- BODY TEXT -----")
        print(text[:4000])
        print("---------------------")
        await cdp.screenshot(ROOT / "out" / "usb-page.png")
        errors = cdp.console_errors()
        print("console errors:", errors[:10])

        if os.environ.get("USB_OFFLINE") == "1":
            pcap = os.environ.get("USB_PCAP") or find_capture()
            print("offline pcap:", pcap)
            payload = json.dumps({"path": pcap}).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{BACKEND_PORT}/api/usb/capture/open",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            print("open:", urllib.request.urlopen(request, timeout=10).read()[:200])
            await asyncio.sleep(2.0)
            offline_text = await cdp.evaluate("document.body.innerText")
            print("----- OFFLINE TEXT (head) -----")
            print(offline_text[:1800])
            print("-------------------------------")
            await cdp.screenshot(ROOT / "out" / "usb-offline.png")
            clicked_row = await cdp.evaluate(
                "(() => { const r = [...document.querySelectorAll('tbody tr')].find(x => x.offsetParent !== null && /BULK|GET_|CONT/.test(x.textContent)); if (!r) return 'no-row'; r.click(); return r.textContent.slice(0, 60); })()"
            )
            print("row click:", clicked_row)
            await asyncio.sleep(1.5)
            detail_text = await cdp.evaluate("document.body.innerText")
            print("----- DETAIL TEXT (tail) -----")
            print(detail_text[-1600:])
            print("------------------------------")
            await cdp.screenshot(ROOT / "out" / "usb-detail.png")
            print("offline console errors:", cdp.console_errors()[-5:])

        if os.environ.get("USB_LIVE") == "1":
            candidates = await cdp.evaluate(
                "[...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => t.includes('开始') || t.includes('停止'))"
            )
            print("button candidates:", candidates)
            clicked_start = await cdp.evaluate(
                "(() => { const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === '开始' && x.offsetParent !== null); if (!b) return 'no-start'; b.click(); return 'ok'; })()"
            )
            print("start:", clicked_start)
            await asyncio.sleep(1.0)
            print(
                "backend status:",
                await cdp.evaluate(
                    "fetch('http://127.0.0.1:8792/api/usb/capture/status').then(r => r.text())"
                ),
            )
            await asyncio.sleep(3.0)
            live_text = await cdp.evaluate("document.body.innerText")
            print("----- LIVE TEXT (head) -----")
            print(live_text[:2500])
            print("----------------------------")
            await cdp.screenshot(ROOT / "out" / "usb-live.png")
            print("live console errors:", cdp.console_errors()[-5:])
            clicked_stop = await cdp.evaluate(
                "(() => { const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === '停止' && x.offsetParent !== null); if (!b) return 'no-stop'; b.click(); return 'ok'; })()"
            )
            print("stop:", clicked_stop)
            await asyncio.sleep(1.5)
            await cdp.screenshot(ROOT / "out" / "usb-live-stopped.png")


def find_capture() -> str:
    roots = [
        Path(os.environ.get("APPDATA", "")) / "omni-bus" / "captures",
        Path(os.environ.get("TEMP", "")) / "omni-bus" / "captures",
        Path(os.environ.get("TEMP", "")),
    ]
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.glob("usb-*.pcap"))
    candidates = [p for p in candidates if p.is_file() and p.stat().st_size > 1000]
    if not candidates:
        raise SystemExit("找不到可用的 pcap，请先抓一段或设置 USB_PCAP")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0])


def main() -> None:
    if not RENDERER.exists():
        raise SystemExit("out/renderer 不存在，请先 npm run build")
    server = start_static()
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["ELECTRON_RENDERER_URL"] = f"http://127.0.0.1:{HTTP_PORT}"
    env["OMNI_BACKEND_PORT"] = str(BACKEND_PORT)
    proc = subprocess.Popen(
        [
            str(ELECTRON),
            ".",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-sandbox",
            "--disable-gpu",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    lines: list[str] = []

    def drain():
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            lines.append(line)
            print("[electron]", line)

    threading.Thread(target=drain, daemon=True).start()
    try:
        asyncio.run(run())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()
        print("screenshot:", ROOT / "out" / "usb-page.png")
        backend_errors = [line for line in lines if "Traceback" in line or "Error" in line]
        if backend_errors:
            print("electron errors:", backend_errors[:10])


if __name__ == "__main__":
    main()
