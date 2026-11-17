# OMNI 后端独立化计划（omni-backend 仓库）

> 状态：已执行（阶段 0 / 1 / 2 / 4 完成；阶段 3（omni-bus GUI 接入）待做）
> 执行记录与偏差：见 `docs/migration.md`
> 编写日期：2026-11-17
> 适用仓库：`omni-link`、`omni-bus`、`omni-cli`，以及新建的 `omni-backend`
> 执行方式：按「阶段 0 → 1 → 2 → 3 → 4」顺序推进，**阶段 0 未完成不要开始阶段 1**

---

## 0. 背景与目标

当前状态：每个桌面应用各自带一份 Python 后端（FastAPI + pyOCD/串口等），CLI 是纯 HTTP 客户端。
问题：两套后端重复实现「登记表 / 健康检查 / 优雅关停」，且后端与 GUI 仓库绑死，换 UI 架构（Tauri/Web/其他）时要连后端一起搬。

目标：

1. 新建独立仓库 `omni-backend`，里面装**两个后端服务**：`link`（探针/烧录/调试）与 `bus`（串口/网络/Modbus/USB）。
2. 两个 GUI 仓库（omni-link / omni-bus）退化为「Electron 外壳 + 打包脚本」，通过 git submodule 引用后端仓库。
3. `omni-cli` 保持薄客户端，通过 HTTP + 服务登记表连接任意后端。
4. 两个后端共享少量基础设施（登记表、health、shutdown、协议常量），但**仍是两个独立进程**，互不拖累。

非目标（本次不做）：

- 不合并两个后端为一个进程。
- 不改任何 REST/WS 字段语义（`protocol` 保持 1）。
- 不在搬家期间加新功能（`omni serial` / `omni net` 放到阶段 3 之后）。
- 不把 GUI 交互逻辑下沉到后端。
- 不删除内置 pyOCD 源码（`pyocd/` 随 link 后端整体搬走）。

预期最终拓扑：

```
omni-backend/        平台仓：link + bus 两个后端 + 共享 common + 协议文档
omni-link/           Electron GUI + 打包脚本（submodule → vendor/omni-backend）
omni-bus/            Electron GUI + 打包脚本（submodule → vendor/omni-backend）
omni-cli/            纯 HTTP 客户端（已独立，2026-11 完成拆分）
```

---

## 1. 现状（已核实事实，执行前请复核）

### 1.1 仓库与版本

| 仓库 | 路径 | 版本 | 说明 |
|---|---|---|---|
| omni-link | `D:\workspace\embedded\omni-link` | app `0.5.8` / backend `0.3.1` | GUI + link 后端；已含 `vendor/omni-cli` 子模块（commit `4f03c37`） |
| omni-bus | `D:\workspace\embedded\omni-bus` | app `0.2.4` / backend `0.1.0` | GUI + bus 后端 |
| omni-cli | `D:\workspace\embedded\omni-cli` | `0.24.0` | 独立仓库，分支 `dev` |

omni-link 现状（本次改动前）：

- `python/` 是后端全部内容：`server.py`、`api/`、`core/`、`omni_tools/`、`pyocd/`（内置源码 32 MB）、`data/`（设备库 seed XML）、`flm/`（烧录算法）、`tools/`（FLM 转换开发工具）、`build.py`、`requirements.txt`、`pyproject.toml`（仅用于打 `omni-pyocd` 发行版）、8 个 `test_*.py`。
- `python/dist/omni-backend/`（约 120 MB）经 `package.json` 的 `extraResources` 复制为安装包的 `resources/python/`。
- `electron/python-bridge.ts`：开发态找 `<repo>/python/server.py`，生产态用 `resources/python/omni-backend.exe`。
- `server.py` 已有：`BACKEND_VERSION = "0.3.1"`、`PROTOCOL_VERSION = 1`、`SERVICE_NAME = "link"`、`publish_runtime()` / `unpublish_runtime()`（写 `%LOCALAPPDATA%\omni-link\backend.json`）、`/api/system/shutdown`、`/api/health`。
- 8 个后端测试：`test_breakpoint_manager.py`、`test_dwarf_line_tables.py`、`test_jlink_timeouts.py`、`test_pipelined_read.py`、`test_read_groups.py`、`test_rtt_scan.py`、`test_session_integration.py`、`test_session_recorder.py`。

omni-bus 现状：

- `python/server.py` + `api/{serial,network,modbus,usb,system}.py` + `core/{serial,network,modbus,tftp,usb}_backend.py` + `core/{events,usb_decode,usb_pcap}.py`。
- **没有** `backend.json` 登记、`/api/health` 的 `service/protocol`、`/api/system/shutdown`。
- `requirements.txt`：fastapi/uvicorn/starlette/websockets/wsproto + pyserial + pymodbus。
- 测试：`tests/test_usb_*.py` + `usb_fixtures.py` + `manual_ui_smoke.py`。
- `electron/python-bridge.ts` 与 link 同构，开发态找 `<repo>/python/server.py`。

### 1.2 已知耦合点（搬家时要处理的路径）

| 位置 | 现状 | 处理方式 |
|---|---|---|
| `python/server.py` | `load_config_port()` 读 `<repo>/backend.config.json`（`../backend.config.json`） | 改为读 `<service_dir>/backend.config.json`，并支持 `OMNI_DEV_CONFIG` 覆盖 |
| `core/database.py` | 已支持 `OMNI_DATA_DIR`，开发态 seed 取 `core/../data` | 保持相对结构（`link/core` → `link/data`），只验证 |
| `core/pack_manager.py:48` | 用 `__file__` 定位 src_dir | 随目录整体移动，验证即可 |
| `core/custom_target.py:299` | `_get_data_dir()` + `flm` | 随目录整体移动，验证即可 |
| `build.py` | `BACKEND_DIR = Path(__file__).parent`，输出 `dist/omni-backend` | 移入 `link/` 后不变；增加 `--out` 参数 |
| `python/pyproject.toml` | 仅用于 `omni-pyocd` 发行版 | 移到 `link/pyproject.toml` |
| `python/tools/` | FLM 转换开发工具（不打包） | 移到 `link/tools/` |
| `python/__init__.py` | 疑似历史遗留 | 确认无 `import python` 后删除 |
| `python/{build,dist}/`、`omni-backend.spec`、`*.egg-info`、`_srv.log` | 未跟踪产物 | 搬家前清理，不要带进新仓库 |

### 1.3 CLI 侧现状（不用改，仅对齐）

- `omni-cli/omni_cli/settings.py` 的后端发现顺序：`OMNI_BACKEND_SERVER` → `OMNI_BACKEND_DIR` → 用户级 `cli.config.json` → 仓库布局探测（子模块 `vendor/omni-cli` 自动识别）。
- 协议文档：`omni-cli/docs/backend-protocol.md`（阶段 1 后改为链接到新仓库，避免两份）。
- 状态目录：`%LOCALAPPDATA%\omni-link\`（`OMNI_STATE_DIR` 覆盖）。

---

## 2. 目标结构

### 2.1 新仓库 `omni-backend`

```
omni-backend/
├── common/
│   ├── __init__.py
│   └── omni_service.py        # 共享：状态目录、registry 读写、health 载荷、shutdown 安装
├── link/
│   ├── server.py              # 入口（--port/--host/--log-level）
│   ├── api/ core/ omni_tools/ pyocd/ data/ flm/ tools/
│   ├── build.py               # PyInstaller 打包（输出 dist/link/omni-backend/）
│   ├── backend.config.json    # 开发端口（可选）
│   ├── requirements.txt
│   ├── pyproject.toml         # omni-pyocd 发行版（原 python/pyproject.toml）
│   └── tests/                 # 原 8 个后端测试
├── bus/
│   ├── server.py
│   ├── api/ core/
│   ├── build.py               # 输出 dist/bus/omni-bus-backend/
│   ├── backend.config.json
│   ├── requirements.txt
│   └── tests/                 # 原 bus 测试
├── scripts/
│   ├── build.py               # --service link|bus|all 调度，产物统一到 dist/
│   └── selfcheck.py           # 契约自检：起服务 → health → registry → shutdown
├── tests/
│   └── test_service_contract.py   # 两个服务共用的契约测试
├── docs/
│   ├── protocol.md            # 协议唯一来源（服务发现/版本/流式游标/错误）
│   └── migration.md           # 本计划归档
├── .github/workflows/ci.yml
├── pyproject.toml             # 可选：仅放开发依赖/工具；服务本身不 pip 安装
└── README.md
```

### 2.2 `common/omni_service.py` 接口（最小集）

```python
def state_dir(env_var: str, default_name: str) -> str
    # link: state_dir("OMNI_STATE_DIR", "omni-link")
    # bus : state_dir("OMNI_BUS_STATE_DIR", "omni-bus")

def publish_runtime(service: str, version: str, protocol: int, host: str, port: int) -> None
def unpublish_runtime(service: str) -> None          # 仅当 pid 匹配才删
def registry_path(service: str) -> str
def health_payload(service: str, version: str, protocol: int, **extra) -> dict
def install_shutdown(app, service: str, grace_s: float = 10.0) -> None
    # 注册 POST /api/system/shutdown：先响应，再触发 uvicorn 退出；带看门狗 os._exit
```

服务命名固定为 `link` / `bus`，登记文件名 `backend.json`，字段：

```json
{"service":"link","pid":1234,"host":"127.0.0.1","port":51234,
 "version":"0.3.1","protocol":1,"started_at":"2026-11-17T07:19:48+00:00"}
```

### 2.3 运行拓扑

```
omni-cli ─┐
          ├─ HTTP ─▶ link 后端（127.0.0.1:随机端口）──▶ 探针 ──▶ 目标芯片
omni-link ┘            registry: %LOCALAPPDATA%\omni-link\backend.json

omni-cli ─┐
          ├─ HTTP ─▶ bus  后端（127.0.0.1:随机端口）──▶ 串口/网络/USB
omni-bus ┘            registry: %LOCALAPPDATA%\omni-bus\backend.json
```

- 两个进程各自独立，禁止合并。
- 端口只从 `--port 0` 自动分配；`backend.config.json` 仅开发态可覆盖。
- GUI 启动时「先发现、找不到才启动」；CLI 同样规则，绝不重复起第二个。

### 2.4 版本与发布

- 每个服务独立打 tag：`link-v0.3.2`、`bus-v0.1.0`；同一仓库一套 CI。
- `protocol` 只增不改语义；破坏性变更才 +1。CLI 侧声明「至少支持 N-1」。
- GUI 安装包按 submodule commit 固定后端版本，与 CLI 版本解耦。

---

## 3. 硬性约束（执行时逐条遵守）

1. **搬家阶段不改协议**：只搬代码，`/api/*` 路径、字段、`protocol` 全部保持。
2. **安装包路径不变**：产物仍是 `python/dist/omni-backend/`（link）与 `python/dist/omni-backend/`（bus），安装后仍是 `resources/python/omni-backend.exe`，NSIS 的 PATH/签名逻辑不动。
3. **保留 pyOCD 内置源码**：`link/pyocd/` 原样搬，不做 pip 安装改造。
4. **不写仓库相对路径以外的状态**：后端只写 `%LOCALAPPDATA%`（或 `OMNI_STATE_DIR` / `OMNI_DATA_DIR`）和显式传入的目录。
5. **两个服务互不 import**：`link` 不 import `bus`，反之亦然；只允许都 import `common`。
6. **`common` 只放真正共享的**（≤5 个函数），不要做“万能工具包”。
7. **每个阶段独立可回滚**：每阶段结束打一个 commit/tag，确认通过再进入下一阶段。
8. **GUI 不解析 CLI 输出**：GUI 继续直连 HTTP/WS，禁止 shell 出去调 `omni`。
9. **不引入 Electron 依赖到后端**：后端不得 import electron/node 相关任何东西。
10. **测试必须能在没有 GUI 的机器上跑**。

---

## 4. 阶段 0：让 link 后端“能独立站着”（在 omni-link 现仓做）

目的：搬家前把路径问题修掉，否则搬完会花大量时间修 import/路径。**大约 1–2 天。**

### 4.1 任务

1. 新增 `python/scripts/selfcheck.py`（新目录），内容要求：
   - `chdir` 到临时目录（模拟“不在仓库根”）；
   - 设置 `OMNI_DATA_DIR=<临时目录>`、`OMNI_STATE_DIR=<临时目录>`；
   - 用当前解释器 spawn `python/server.py --port 0`；
   - 读 stdout 首行 `{"port": N}`；
   - `GET /api/health` 断言 `status=ok`、`service=link`、`protocol=1`；
   - 断言 `%TEMP%/omni-link/backend.json` 存在且 `pid/port` 正确；
   - `POST /api/system/shutdown`，等待进程退出（≤15s）；
   - 任一失败退出码 1，并打印可定位的原因。
2. 修 `server.py::load_config_port()`：
   - 先看 `OMNI_DEV_CONFIG` 环境变量；
   - 否则 `<server.py 所在目录>/backend.config.json`；
   - 再否则上一级目录的（兼容旧布局）；
   - 都没有返回 `None`（自动分配端口）。
3. 复核并记录（不必大改）：
   - `core/database.py` 在 `OMNI_DATA_DIR` 下的读写；
   - `core/custom_target.py` 的 `flm` 目录来源；
   - `core/pack_manager.py:48` 的 `src_dir`；
   - `build.py` 的 `DATA_DIRS` 是否都基于 `BACKEND_DIR`。
4. 清理未跟踪产物：`python/build/`、`python/dist/`、`python/omni-backend.spec`、`python/omni_pyocd.egg-info/`、`python/_srv.log`；确认 `.gitignore` 覆盖。
5. 记录基线（写进执行记录）：
   - `python/test_*.py` 8 个全绿；
   - `npm run typecheck` 通过；
   - `omni backend start/status/stop` 正常（显示 `协议=1`）。

### 4.2 验收

- 在任意目录（例如 `%TEMP%`）执行 `python D:\workspace\embedded\omni-link\python\scripts\selfcheck.py` 通过。
- 后端不再依赖“当前目录必须是仓库根”。
- 现有 GUI（`npm run dev`）连接、烧录、RTT 均正常。

---

## 5. 阶段 1：新建 `omni-backend` 仓库

### 5.1 用 filter-repo 保留历史（link 部分）

```bash
# 前置：pip install git-filter-repo（或 uv tool install）
git clone D:/workspace/embedded/omni-link /tmp/omni-backend-split
cd /tmp/omni-backend-split
PATH="$(dirname "$(command -v git-filter-repo)"):$PATH" git filter-repo --force \
  --path python/ \
  --path-rename python/:link/
```

完成后 `link/` 下就是后端全部内容，`python/` 之外的历史被裁掉。

### 5.2 并入 bus 后端历史

```bash
git clone D:/workspace/embedded/omni-bus /tmp/omni-bus-split
cd /tmp/omni-bus-split
PATH="$(dirname "$(command -v git-filter-repo)"):$PATH" git filter-repo --force \
  --path python/ \
  --path-rename python/:bus/
cd /tmp/omni-backend-split
git remote add bus /tmp/omni-bus-split
git fetch bus
git merge --allow-unrelated-histories bus/<bus 默认分支>
git remote remove bus
```

> 注意：`omni-bus/python` 里的 `build/`、`dist/`、`omni-backend.spec`、`__pycache__` 若未跟踪不会被带过来；若历史里有，先在新仓库删除并加 `.gitignore`。

### 5.3 结构调整

1. 删除 `link/` 与 `bus/` 里的构建产物、spec、egg-info、日志、`__pycache__`。
2. `link/pyproject.toml`（原 `python/pyproject.toml`）保留；`link/tools/` 保留（开发工具）。
3. 确认 `link/__init__.py`（原 `python/__init__.py`）无人 import 后删除。
4. 把两个 `backend.config.json` 分别放到 `link/` 与 `bus/`（内容各保留原端口，或删除改用自动分配——推荐后者的长期做法，但本次先保留以降低变量）。
5. 新增 `common/omni_service.py`（接口见 §2.2），两个 `server.py` 改为：
   ```python
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
   from common import omni_service
   ```
   用它替换各自的 `_state_dir/publish_runtime/unpublish_runtime/health/shutdown` 实现。
6. `bus/server.py` 补：
   - `PROTOCOL_VERSION = 1`、`SERVICE_NAME = "bus"`、`BACKEND_VERSION` 沿用 `0.1.0`（或升 `0.1.1`）；
   - `publish_runtime` / `unpublish_runtime`（atexit + lifespan）；
   - `/api/health` 增加 `service/protocol`；
   - `/api/system/shutdown`（用 `common.omni_service.install_shutdown`）。
7. `link/server.py` 保持 `SERVICE_NAME="link"`、`PROTOCOL_VERSION=1`，改用 common。

### 5.4 构建与入口

1. `link/build.py`：保持现有逻辑，增加 `--out` 参数（默认 `<repo>/dist/link`），并把 `--specpath` 指到 `dist/link/spec`（避免污染源码目录）。
2. `bus/build.py`：以 link 的 `build.py` 为模板改写：
   - 入口 `bus/server.py`；
   - `--name omni-bus-backend`；
   - `--paths bus`；
   - hiddenimports 至少包含：`api.*`、`core.*`、`serial`、`serial.tools.list_ports`、`pymodbus.*`、`uvicorn.*`；
   - datas 无（bus 没有 data/flm）。
3. `scripts/build.py`：
   - `--service link|bus|all`；
   - `all` 顺序执行，产物统一在 `<repo>/dist/<service>/`；
   - 每个产物自检：运行 exe `--help`（不启动服务），失败即非 0。
4. CI（`.github/workflows/ci.yml`）：
   - `tests` job：Windows + Ubuntu，Python 3.11，分别 `pip install -r link/requirements.txt` / `bus/requirements.txt`，跑 `link/tests`、`bus/tests`、`tests/test_service_contract.py`（契约测试在 Ubuntu 上跳过 Windows-only 的 USB 用例）；
   - `build` job：Windows，tag 触发，`python scripts/build.py --service all`，上传两个产物。

### 5.5 测试迁移

- 8 个 link 测试移到 `link/tests/`，把文件头部的 `sys.path.insert(0, PY_DIR)` 改为指向 `link/`（新仓库里测试在 `link/tests`，根是 `link/`）。
- bus 测试移到 `bus/tests/`，同样调整 `sys.path`（指向 `bus/`），`usb_fixtures.py` 一起搬。
- 新增 `tests/test_service_contract.py`（两个服务跑同一套断言）：
  1. 任意 cwd 启动成功；
  2. stdout 握手得到端口；
  3. `/api/health` 有 `status/service/protocol/pid`；
  4. registry 文件存在、字段齐全、`pid` 与实际进程一致；
  5. `POST /api/system/shutdown` 后进程在 15s 内退出；
  6. 进程退出后 registry 被清理。

### 5.6 文档

- `README.md`：如何起 link/bus、如何跑测试、如何打包、常见环境变量。
- `docs/protocol.md`：从 `omni-cli/docs/backend-protocol.md` 迁入，补 bus 小节；CLI 仓库那份改为一行链接（避免两份漂移）。
- `docs/migration.md`：归档本计划与执行记录。

### 5.7 验收

- `python link/server.py --port 0`、`python bus/server.py --port 0` 在任意 cwd 可启动，health 正确。
- `scripts/build.py --service all` 产出两个目录，且 exe 能运行 `--help`。
- 契约测试在两个服务上都绿。
- 新仓库默认分支 `dev`，首个 commit 说明“从 omni-link / omni-bus 迁入”。

---

## 6. 阶段 2：omni-link 接入 omni-backend

### 6.1 加子模块

Git 默认禁止 `file://` 子模块克隆，用「本地 clone + 手写 .gitmodules + absorbgitdirs」：

```bash
cd D:/workspace/embedded/omni-link
git clone D:/workspace/embedded/omni-backend vendor/omni-backend
printf '[submodule "vendor/omni-backend"]\n\tpath = vendor/omni-backend\n\turl = ../omni-backend.git\n' > .gitmodules
git add .gitmodules vendor/omni-backend
git config submodule.vendor/omni-backend.url ../omni-backend.git
git submodule init
git submodule absorbgitdirs vendor/omni-backend
git add vendor/omni-backend
```

> 推送远端后，把子模块的 remote 改成 GitHub 地址并 `git submodule sync`。

### 6.2 删除旧后端

```bash
git rm -r python
```

- 删除仓库根的 `backend.config.json`：迁移后没有任何代码读它（后端读 `<service>/backend.config.json`，CLI 读后端目录上一级）。若想保留开发端口，把配置放到 `vendor/omni-backend/link/backend.config.json`。
- 不要留 `python/` 空壳；`build.ps1` 会在打包时重建 `python/dist/`。
- 确认 `package.json` 的 `extraResources` 仍指向 `python/dist/omni-backend`（脚本会重建该目录）。

### 6.3 改 Electron 后端发现逻辑

`electron/python-bridge.ts`（两处，约 168–215 行）开发态候选路径改为：

1. `process.env.OMNI_BACKEND_DIR` 指向的 `link/server.py`；
2. `<repo>/vendor/omni-backend/link/server.py`；
3. 兼容旧路径 `<repo>/python/server.py`（保留一个版本，方便过渡）。

生产态路径不动（`resources/python/omni-backend.exe`）。

### 6.4 改 npm scripts（`package.json`）

```json
"python:dev": "cd vendor/omni-backend/link && ..\\..\\..\\.venv\\Scripts\\python.exe server.py",
"python:install": "python -m venv .venv && .venv\\Scripts\\pip.exe install -r vendor/omni-backend/link/requirements.txt"
```

### 6.5 改 `build.ps1`

- 子模块守卫（参照 CLI 的做法）：缺 `vendor/omni-backend/link/build.py` 时报错并提示 `git submodule update --init --recursive`。
- Step 1 改为：
  ```powershell
  & $pyExe vendor/omni-backend/link/build.py --out "$ProjectRoot\python\dist"
  ```
  保证产物落在 `python/dist/omni-backend/`，`extraResources` 与 NSIS 不变。
- 删除对 `python/build.py`、`python/requirements.txt` 的引用。
- `-CliOnly` 分支保持现状（委托 CLI 仓库）。

### 6.6 测试与文档

- 删除 `python/test_*.py`（已迁入 omni-backend）；如需保留集成测试，改为 `scripts/smoke-backend.ps1` 调 `vendor/omni-backend/scripts/selfcheck.py`。
- 更新 `AGENTS.md`（结构：`vendor/omni-backend` 取代 `python/`；`npm run python:dev` 路径）、`docs/tech/architecture.md`、`README.md`。
- 保留 `python/` 目录仅作为打包输出目录；`.gitignore` 增加 `python/dist/`（已有）。

### 6.7 验收

- `npm run python:dev` 能起后端；`npm run dev` 能连接并列出探针。
- `npm run package` 成功，安装包里有 `resources/python/omni-backend.exe`，安装后 `omni probe list` 可用。
- `npm run typecheck` 通过。
- `git status` 中子模块干净。

---

## 7. 阶段 3：omni-bus 接入 + bus 后端补能力

### 7.1 后端侧（在 omni-backend 仓库）

- 按 §5.3 第 6 条补齐 bus 的 registry/health/shutdown（若阶段 1 未做完）。
- 补 `bus/tests/test_service_contract.py` 覆盖（或复用共享契约测试）。

### 7.2 omni-bus 仓库

- `git rm -r python`。
- 加子模块 `vendor/omni-backend`（命令同 §6.1）。
- `electron/python-bridge.ts` 开发态候选路径改为 `vendor/omni-backend/bus/server.py`（保留旧路径兜底）。
- `package.json`：`python:dev` / `python:install` 指向 `vendor/omni-backend/bus/`。
- `build.ps1`：Step 1 调 `vendor/omni-backend/bus/build.py --out python/dist`。
- 更新 README/AGENTS 与 `.gitmodules` 的 URL。

### 7.3 验收

- omni-bus 开发态可启动、串口/网络/USB 页面功能回归。
- `omni backend start bus`（阶段 4 之后）能发现并复用 GUI 已启动的 bus 后端。
- 打包产物含 `resources/python/omni-backend.exe`，启动正常。

---

## 8. 阶段 4：CLI 多服务支持

### 8.1 改动

- `omni_cli/settings.py`：
  - `services()` 返回 `{"link": ServiceInfo(registry, state_env="OMNI_STATE_DIR", ...), "bus": ServiceInfo(registry, state_env="OMNI_BUS_STATE_DIR", ...)}`；
  - 端口发现按服务解析。
- `omni_cli/runtime.py`：按命令组 → 服务映射表选择服务；`--service link|bus` 可覆盖；状态型命令拒绝 `--managed`。
- `omni_cli/commands/backend.py`：
  - `start|stop|status [link|bus]`（位置参数，默认 `link`，保持向后兼容）；
  - `list`（列出登记表里所有服务）；
  - `status` 输出两服务汇总（JSON 带 `services` 数组）。
- 新增 `omni_cli/autoinstall.py`（或 `settings` 内函数）：自动探测已安装 GUI 的后端目录（`%LOCALAPPDATA%\Programs\omni-link\resources\python`、`...\omni-bus\resources\python`、`OMNI_LINK_HOME`），用于 `backend start` 与 `--managed`。
- 命令组 → 服务映射：`serial/net/modbus/usb` → `bus`；其余 → `link`。
- `docs/backend-protocol.md` 增加 bus 小节（或链接到 omni-backend 的 `docs/protocol.md`）。

### 8.2 验收

- 只装 CLI：`omni backend start bus` 给出“未找到 bus 后端，请安装 OMNI Bus 或设 `OMNI_BUS_STATE_DIR`”的可执行提示。
- 装了 GUI：`omni backend start` 复用 GUI 已启动的后端，不重复起进程。
- 两个服务同时运行，`omni probe list` 与 `omni serial ...`（阶段 3 之后新增）互不干扰。

---

## 9. 全链路验收清单

- [ ] `omni-backend`：两服务任意 cwd 可启动；health/registry/shutdown 契约测试通过。
- [ ] `omni-backend`：两个 exe 产物可运行，体积合理（link ≈120 MB，bus 明显更小）。
- [ ] omni-link：`npm run dev`、`npm run package`、安装后 `omni probe list`/烧录/RTT 正常。
- [ ] omni-bus：`npm run dev`、`npm run package`、串口/网络/USB 页面回归。
- [ ] omni-cli：`omni backend list/status/start/stop` 两个服务；`--service` 生效。
- [ ] 三个 GUI/CLI 同时开，不出现“重复后端/抢设备”。
- [ ] 文档：`AGENTS.md`、`architecture.md`、`README.md`、`protocol.md` 全部指向新结构。

---

## 10. 回滚方案

- 每阶段一个 commit；子模块指针固定。
- 阶段 2/3 出问题时：把 `vendor/omni-backend` 指针回退一个 commit，或临时把旧 `python/` 目录从备份分支恢复（迁移前先打 `backup/pre-backend-split` 分支）。
- 安装包出问题：`build.ps1` 的 `extraResources` 路径未变，可单独回退后端产物。

---

## 11. 已知环境坑（Windows）

1. **PowerShell 5.1 读无 BOM 的 UTF-8 中文脚本会乱码/解析失败**。
   - 验证脚本请用 `pwsh`；
   - 如必须兼容 5.1，写 `.ps1` 时保存为 **UTF-8 with BOM**。
2. **`git submodule add <本地路径>` 会报 `transport 'file' not allowed`**：用 §6.1 的「clone + .gitmodules + absorbgitdirs」流程；`.gitmodules` 先写，再 `absorbgitdirs`。
3. **MSYS 路径传给原生 Python**：环境变量用 `D:/path` 形式，不要用 `/d/path`（会被解析成 `D:\d\path`）。
4. **单文件 exe 在工作区内可能报 `Could not create temporary directory!`**：这是沙箱对 onefile 解包的限制，同字节 exe 在工作区外可运行；不要据此判断构建失败。
5. `git filter-repo` 需在干净克隆上运行；对已有远程的仓库会移除 origin，属正常行为。

---

## 12. 开放问题（执行前与用户确认）

1. 新仓库的远端地址与可见性（建议 `git@github.com:LuckkMaker/omni-backend.git`，与 link/cli 同组织）。
2. `omni-pyocd` 的 `pyproject.toml` 是否随 link 迁入新仓库（建议是）。
3. `link/tools/`（FLM 转 FLASH_ALGO 的开发工具）是否留在后端仓库（建议是，不打包）。
4. 两个服务的 dev 端口是否保留（9001/9004）还是统一 `--port 0`（建议：保留文件、日常用自动分配）。
5. 是否保留「旧 `python/` 路径兜底」一个版本（建议保留，减少 GUI 与脚本的硬切）。

---

## 13. 后续（本计划之后）

阶段 3/4 完成后，再开始 `omni serial` / `omni net`：

- 后端（omni-backend/bus）：串口接收环形缓存 + `GET /api/serial/data?since=`、`POST /api/serial/read`；TCP/UDP 接收缓存 + 游标；ping 记录；serial 同步调用改 `asyncio.to_thread`。
- CLI：`omni serial`、`omni net` 命令组；`--follow --interval` 游标续读（对齐 `omni rtt read --follow`）。
- 明确不做：modbus/usb/tftp 的 CLI 命令（本期）。

---

## 附录 A：建议提交信息

```
feat(backend): 新增 omni_service 共享模块（registry/health/shutdown）
refactor(backend): link/bus 后端迁入独立仓库
build(backend): 按服务拆分 PyInstaller 产物
test(backend): 新增两服务契约测试
refactor(omni link): 后端改为 vendor/omni-backend 子模块
build(omni link): 打包改为调用后端仓库构建脚本
refactor(omni bus): 后端改为 vendor/omni-backend 子模块
feat(omni cli): backend 支持 link/bus 多服务
feat(omni cli): 自动探测已安装 GUI 的后端
```

## 附录 B：常用命令

```bash
# 后端开发
python link/server.py --port 0
python bus/server.py --port 0
python scripts/selfcheck.py --service link
python scripts/build.py --service all

# 契约测试
python tests/test_service_contract.py

# 子模块初始化（克隆安装包仓库时）
git submodule update --init --recursive

# 冒烟
.venv/Scripts/omni.exe backend start
.venv/Scripts/omni.exe backend status
.venv/Scripts/omni.exe probe list
.venv/Scripts/omni.exe backend stop
```
