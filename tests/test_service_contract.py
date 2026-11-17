#!/usr/bin/env python3
# OMNI Backend
# Copyright (c) 2026 LuckkMaker
# SPDX-License-Identifier: MIT
"""两个服务共用的契约测试。

覆盖 docs/protocol.md 里“任何后端都必须满足”的部分：

1. 任意 cwd 启动成功（由 scripts/selfcheck.py 覆盖）；
2. stdout 握手得到端口；
3. ``/api/health`` 有 ``status/service/protocol/pid``；
4. 登记表文件存在、字段齐全、``pid`` 与实际进程一致；
5. ``POST /api/system/shutdown`` 后进程在宽限期内退出；
6. 进程退出后登记表被清理。

1/2/5/6 需要真正起进程，交给 ``scripts/selfcheck.py``（同一套断言，两个服务复用）；
3/4 的纯函数部分（``common/omni_service.py``）在本文件里直接单测，跑得快。

运行::

    python -m unittest discover -s tests -v
    python -m pytest tests -v          # 装了 pytest 时同样可用
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
SERVICES = ("link", "bus")

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common import omni_service  # noqa: E402


def _state_env(service: str) -> str:
    return omni_service.SERVICE_ENV[service][0]


class OmniServiceUnitTest(unittest.TestCase):
    """common/omni_service.py 的纯函数契约（不启动进程）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="omni-contract-")
        self.tmp = Path(self._tmp.name)
        self._saved_env = {svc: os.environ.get(_state_env(svc)) for svc in SERVICES}

    def tearDown(self):
        for svc, value in self._saved_env.items():
            key = _state_env(svc)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_state_dir_uses_env_override(self):
        """OMNI_STATE_DIR / OMNI_BUS_STATE_DIR 必须能覆盖默认用户目录。"""
        for service in SERVICES:
            with self.subTest(service=service):
                env_var, default_name = omni_service.SERVICE_ENV[service]
                os.environ.pop(env_var, None)
                fallback = omni_service.state_dir(env_var, default_name)
                self.assertTrue(fallback.endswith(default_name))
                os.environ[env_var] = str(self.tmp)
                self.assertEqual(omni_service.state_dir(env_var, default_name), str(self.tmp))

    def test_registry_path_per_service(self):
        """两个服务的登记表必须落在各自目录，文件名固定 backend.json。"""
        paths = {}
        for service in SERVICES:
            os.environ[_state_env(service)] = str(self.tmp / service)
            path = Path(omni_service.registry_path(service))
            self.assertEqual(path.name, "backend.json")
            self.assertEqual(path.parent, self.tmp / service)
            paths[service] = path
        self.assertNotEqual(paths["link"], paths["bus"])

    def test_unknown_service_rejected(self):
        with self.assertRaises(ValueError):
            omni_service.registry_path("nope")

    def test_health_payload_has_contract_fields(self):
        payload = omni_service.health_payload("link", "9.9.9", 7, ws_connections=3)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "link")
        self.assertEqual(payload["version"], "9.9.9")
        self.assertEqual(payload["protocol"], 7)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["ws_connections"], 3)

    def test_health_payload_cannot_override_reserved_fields(self):
        """服务专属字段不能覆盖 status/pid 等公共字段（protocol 是位置参数，传不进 extra）。"""
        payload = omni_service.health_payload(
            "bus", "1.0.0", 1, status="bad", pid=-1
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["protocol"], 1)
        self.assertEqual(payload["pid"], os.getpid())

    def test_publish_then_unpublish(self):
        os.environ[_state_env("link")] = str(self.tmp)
        omni_service.publish_runtime("link", "0.0.1", 1, "127.0.0.1", 43210)
        path = Path(omni_service.registry_path("link"))
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(data),
            {"service", "pid", "host", "port", "version", "protocol", "started_at"},
        )
        self.assertEqual(data["service"], "link")
        self.assertEqual(data["port"], 43210)
        self.assertEqual(data["pid"], os.getpid())
        omni_service.unpublish_runtime("link")
        self.assertFalse(path.exists())

    def test_unpublish_keeps_other_process_record(self):
        """登记表指向别的 pid（后启动的后端）时不能删，否则会误删新后端。"""
        os.environ[_state_env("link")] = str(self.tmp)
        path = Path(omni_service.registry_path("link"))
        path.write_text(json.dumps({"pid": os.getpid() + 12345}), encoding="utf-8")
        omni_service.unpublish_runtime("link")
        self.assertTrue(path.is_file())


def _deps_available(service: str) -> tuple[bool, str]:
    """检查当前解释器能否 import 该服务的运行时依赖。

    link 的 pyOCD 是内置源码（``link/pyocd``），所以 ``find_spec("pyocd")`` 恒为真，
    必须同时检查 pyOCD 的第三方依赖，否则会在缺依赖的环境里"假通过"。
    """
    if service == "link":
        required = [
            "fastapi", "uvicorn", "pyocd",
            "capstone", "cmsis_pack_manager", "intelhex", "jinja2", "yaml",
        ]
    else:
        required = ["fastapi", "uvicorn", "serial", "pymodbus"]
    saved = list(sys.path)
    sys.path.insert(0, str(REPO_ROOT / service))
    try:
        missing = [name for name in required if importlib.util.find_spec(name) is None]
    finally:
        sys.path[:] = saved
    return (not missing, ", ".join(missing))


class ServiceStartupContractTest(unittest.TestCase):
    """真正的端到端契约：起进程 → health → 登记表 → shutdown → 清理。"""

    def test_services_start_and_shutdown(self):
        for service in SERVICES:
            with self.subTest(service=service):
                ok, missing = _deps_available(service)
                if not ok:
                    self.skipTest(f"当前解释器缺少 {service} 依赖: {missing}")
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / "selfcheck.py"), "--service", service],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    cwd=str(REPO_ROOT),
                )
                if proc.returncode != 0:
                    self.fail(
                        f"{service} 契约自检失败（exit={proc.returncode}）\n"
                        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
