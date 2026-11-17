"""OMNI 后端共享基础设施。

只放两个后端（``link`` / ``bus``）真正共享的最小集：

- 状态目录与登记表（``backend.json``）的读写；
- ``/api/health`` 的公共字段；
- ``/api/system/shutdown`` 优雅关停（含看门狗）。

**两个服务互不 import**，只允许各自 import 本模块；本模块不 import ``link``/``bus``
的任何代码，也不引入 Electron/Node 相关依赖。
"""

# 注意：本模块**不能**启用 ``from __future__ import annotations``。
# install_shutdown() 注册的 FastAPI 端点用局部导入的 ``Request`` 作参数注解，
# FastAPI 在注册时按模块全局命名空间解析注解；注解一旦被延迟成字符串就解析不到
# ``Request``，端点会退化成"查询参数校验"并恒返回 422（关停因此失效）。
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("omni.service")

#: 登记表文件名（与 omni-cli / Electron 主进程约定一致，不要改）
REGISTRY_FILENAME = "backend.json"

#: 服务名 → (状态目录环境变量, 默认目录名)
#: link 沿用历史变量 ``OMNI_STATE_DIR`` / ``omni-link``（CLI 与 Electron 已按此实现），
#: bus 用独立变量与目录，避免两个服务的登记表互相覆盖。
SERVICE_ENV: dict[str, tuple[str, str]] = {
    "link": ("OMNI_STATE_DIR", "omni-link"),
    "bus": ("OMNI_BUS_STATE_DIR", "omni-bus"),
}


# ---------------------------------------------------------------------------
# 状态目录与登记表
# ---------------------------------------------------------------------------
def _platform_state_base() -> str:
    """用户级状态根目录：Windows 用 %LOCALAPPDATA%，其他平台用 XDG。"""
    if os.name == "nt":
        return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )


def state_dir(env_var: str, default_name: str) -> str:
    """解析服务状态目录。

    ``env_var`` 非空时优先（自检/测试把它指到临时目录），否则落到
    ``<用户状态根>/<default_name>``。计算方式必须与 omni-cli 保持一致，
    否则 CLI 找不到后端。
    """
    env = os.environ.get(env_var)
    if env:
        return env
    return os.path.join(_platform_state_base(), default_name)


def _service_state_dir(service: str) -> str:
    try:
        env_var, default_name = SERVICE_ENV[service]
    except KeyError:
        raise ValueError(
            f"未知服务 {service!r}；只支持 {sorted(SERVICE_ENV)}"
        ) from None
    return state_dir(env_var, default_name)


def registry_path(service: str) -> str:
    """服务登记表的完整路径：``<状态目录>/backend.json``。"""
    return os.path.join(_service_state_dir(service), REGISTRY_FILENAME)


def publish_runtime(
    service: str, version: str, protocol: int, host: str, port: int
) -> None:
    """写入登记表，供 CLI / GUI 发现并复用**同一个**后端进程。

    原子写（临时文件 + ``os.replace``）：调用方可能在任意时刻读到该文件，
    不能让它看到半截 JSON。写失败只记日志，不影响服务启动。
    """
    path = registry_path(service)
    data = {
        "service": service,
        "pid": os.getpid(),
        "host": host,
        "port": int(port),
        "version": version,
        "protocol": int(protocol),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:  # pragma: no cover - 权限/磁盘异常
        logger.warning("写服务登记表失败（%s）: %s", path, e)


def unpublish_runtime(service: str) -> None:
    """删除登记表；仅当记录仍指向本进程时才删。

    否则会误删“本进程之后启动的新后端”的登记（例如关停竞态）。
    """
    path = registry_path(service)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("pid") or 0) == os.getpid():
            os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def health_payload(service: str, version: str, protocol: int, **extra) -> dict:
    """``/api/health`` 的公共载荷。

    调用方用 ``**extra`` 追加服务专属字段（如 link 的 ``ws_connections``），
    但不得覆盖 ``status`` / ``pid`` / ``service`` / ``version`` / ``protocol``。
    """
    payload = {
        "status": "ok",
        "pid": os.getpid(),
        "service": service,
        "version": version,
        "protocol": int(protocol),
    }
    for key in payload:
        extra.pop(key, None)
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# 优雅关停
# ---------------------------------------------------------------------------
def install_shutdown(app, service: str, grace_s: float = 10.0) -> None:
    """注册 ``POST /api/system/shutdown``：先回响应，再让 uvicorn 退出。

    ``omni backend stop`` 在“没有运行时记录”的后端（手动 ``server.py``、
    Electron 托管）上只能靠这个端点优雅退出：走 uvicorn 正常退出路径，
    触发 lifespan 清理（断开探针、释放 J-Link 句柄）。

    清理可能被底层 SWD/DLL 操作卡死，故带看门狗：超过 ``grace_s`` 直接
    ``os._exit(0)``，保证进程真的消失、句柄被系统回收。
    """
    from fastapi import HTTPException, Request  # 局部导入：common 在无 web 依赖时仍可 import

    @app.post("/api/system/shutdown")
    async def system_shutdown(request: Request):
        server = getattr(request.app.state, "_server", None)
        if server is None:
            # 非 uvicorn.Server 启动（如测试客户端），无法优雅退出——由调用方回退强杀
            raise HTTPException(status_code=503, detail="当前后端不支持优雅关停")

        def _request_exit() -> None:
            # uvicorn 各版本 should_exit 类型不同：旧版 bool，新版 asyncio.Event
            should_exit = getattr(server, "should_exit", None)
            if hasattr(should_exit, "set"):
                should_exit.set()
            else:
                server.should_exit = True

        def _watchdog() -> None:
            time.sleep(grace_s)
            logger.warning(
                "优雅关停超时（清理可能被硬件操作阻塞），强制退出以释放设备句柄"
            )
            os._exit(0)

        threading.Thread(target=_watchdog, name=f"{service}-shutdown-watchdog", daemon=True).start()

        # 让当前请求先返回响应，再触发退出，避免连接被掐断
        import asyncio

        asyncio.get_running_loop().call_later(0.2, _request_exit)
        logger.info("收到关停请求，开始优雅退出（服务=%s）", service)
        return {"shutdown": True, "pid": os.getpid()}

    # 供测试/诊断确认关停宽限期
    app.state.shutdown_grace_s = grace_s
