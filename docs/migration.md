# 后端独立化迁移记录

> 迁移日期：2026-11-17
> 来源仓库：`omni-link/python`（link 后端）、`omni-bus/python`（bus 后端）
> 目标仓库：本仓库（`omni-backend`）

本文件归档迁移过程、与原始计划的偏差、验证结果与后续工作。原始计划（含阶段 0–4、
硬性约束、回滚方案）由 `omni-backend-plan.md` 描述，本文件只记录**实际执行**。

## 1. 目标

1. 两个后端（`link` / `bus`）从 GUI 仓库搬到独立仓库，GUI 退化为"外壳 + 打包脚本"。
2. 两个后端仍是**两个独立进程**，只共享少量基础设施（登记表 / health / 优雅关停）。
3. 协议不变：`/api/*` 路径、字段语义、`protocol = 1` 全部保持。
4. 安装包路径不变：link 产物仍是 `omni-backend.exe`，落在 `resources/python/`。

## 2. 实际步骤

### 2.1 阶段 0（在 omni-link 现仓完成）

- 新增 `python/scripts/selfcheck.py`：任意 cwd 起服务 → 握手 → `/api/health` → 登记表 →
  `POST /api/system/shutdown` → 清理校验。
- `load_config_port()` 改为按 `__file__` 定位：`OMNI_DEV_CONFIG` → `<后端目录>/backend.config.json`
  → 上级目录/backend.config.json（旧布局）→ `None`（自动分配）。
- 清理未跟踪产物（`build/`、`dist/`、`*.spec`、`*.egg-info`、`_srv.log`、`__pycache__`），
  `.gitignore` 增加 `python/*.spec`。
- 基线验证：8 个后端测试全绿、`npm run typecheck` 通过、selfcheck PASS。

### 2.2 阶段 1（本仓库）

```bash
# 1) 用 filter-repo 保留 link 后端历史：python/ → link/
git clone D:/workspace/embedded/omni-link /tmp/omni-split/link-clone
git filter-repo --force --path python/ --path-rename python/:link/

# 2) 用 filter-repo 保留 bus 后端历史：python/ → bus/
git clone D:/workspace/embedded/omni-bus /tmp/omni-split/bus-clone
git filter-repo --force --path python/ --path-rename python/:bus/

# 3) 并入同一仓库
git remote add bus ../bus-clone && git fetch bus dev
git merge --allow-unrelated-histories bus/dev
```

- link 侧保留 192 个提交；bus 侧保留 16 个（纯前端提交因不触及 `python/` 被 filter-repo 剪掉）。
- `omni-bus` 工作区里**未提交**的 USB 分析仪实现（`api/usb.py`、`core/usb_*.py`、`tests/`）
  一并迁入（否则会丢功能）；这些文件与 `server.py`/`build.py`/`core/events.py` 的改动
  在迁移提交里一并落地。拷入时把 CRLF 归一化为 LF（与原仓库 git 存储一致）。
- 结构清理：删除 `bus/_out.txt`、`bus/omni-backend.spec`、`link/__init__.py`（无人 import）、
  构建产物；8 个 link 测试移到 `link/tests/`、bus 测试留在 `bus/tests/`，`sys.path` 改为
  指向服务目录；`selftest_tftp.py` 移入 `bus/tests/`。
- 新增 `common/omni_service.py`（状态目录 / 登记表 / health 载荷 / 优雅关停），
  两个 `server.py` 改为 import `common`，删除 `link/api/system.py` 里重复的 shutdown 端点。
- `link/build.py` 增加 `--out`，输出/spec/work 全部落在输出根下；`bus/build.py` 以 link 为
  模板重写（`--name omni-bus-backend`、pymodbus collect-all、无 data 目录）。
- 新增 `scripts/build.py`（按服务调度 + `<exe> --help` 产物自检）、
  `scripts/selfcheck.py`（两个服务复用）、`tests/test_service_contract.py`（共享契约测试）。
- 新增 `docs/protocol.md`（协议唯一来源）、`README.md`、`.github/workflows/ci.yml`、`.gitignore`。

### 2.3 版本

| 服务 | 迁移前 | 迁移后 | 说明 |
| --- | --- | --- | --- |
| link | 0.3.1 | 0.3.2 | 独立发版起点，登记表/health 由 common 提供 |
| bus | 0.1.0 | 0.1.1 | 新增登记表、`service`/`protocol`、`/api/system/shutdown` |

协议版本保持 `1`（只增不改语义）。

## 3. 与原始计划的偏差

1. **`omni-bus` 的 exe 名**：计划 §5.4.2 要求 `--name omni-bus-backend`，与 §3.2「安装包路径
   不变（`resources/python/omni-backend.exe`）」冲突。本仓库按 §5.4.2 执行（两个安装包里
   同名 exe 会互相覆盖），**阶段 3 接 omni-bus GUI 时必须同步调整** `build.ps1` 的
   `extraResources`（`python/dist/omni-bus-backend`）与 `electron/python-bridge.ts` 里的
   `omni-backend.exe` 字面量。
2. **bus 未提交工作一并迁入**：计划只提到"未跟踪的构建产物不会带过来"，实际 bus 工作区
   有未提交的 USB 分析仪功能，属于必须保留的代码。
3. **`link/core/toolchain.py`**：源码态的"安装根"由"仓库根"改为"服务目录（`link/`）"，
   因为独立成仓后 `<安装目录>/tools` 对应的就是 `link/tools`。
4. **`common/omni_service.py` 不能启用 `from __future__ import annotations`**：启用后
   FastAPI 无法解析端点里局部导入的 `Request` 注解，`/api/system/shutdown` 会退化成查询
   参数校验并恒返回 422（关停失效）。文件顶部有注释说明。
5. **`python/` 目录名**：本仓库内统一叫 `link/`、`bus/`；GUI 仓库侧的打包输出目录仍叫
   `python/dist/`（安装包契约，不改）。

## 4. 验证结果

在 `omni-link/.venv`（link 依赖）与 `omni-bus/.venv`（bus 依赖）两个环境分别执行：

| 检查 | 结果 |
| --- | --- |
| `python scripts/selfcheck.py --service link`（任意 cwd） | PASS（握手 / health / 登记表 / 关停 / 清理 6/6） |
| `python scripts/selfcheck.py --service bus`（任意 cwd） | PASS（6/6） |
| `python -m unittest discover -s tests`（link venv） | 8 tests OK（bus 用例因缺 pymodbus 跳过） |
| `python -m unittest discover -s tests`（bus venv） | 8 tests OK（link 用例因缺 pyOCD 依赖跳过） |
| `link/tests/test_*.py`（8 个） | 全绿 |
| `bus/tests/test_usb_{decode,manager,pcap}.py` | 全绿 |
| `python link/build.py --check-sources` | 通过（UTF-8，无 U+FFFD） |
| `scripts/build.py --service link`（PyInstaller 全量） | 成功：`dist/link/omni-backend/omni-backend.exe` 11.9 MB，整包 57.4 MB，`--help` 自检通过 |
| `scripts/build.py --service bus`（PyInstaller 全量） | 成功：`dist/bus/omni-bus-backend/omni-bus-backend.exe` 5.4 MB，整包 21.8 MB，`--help` 自检通过 |
| 打包产物实跑（两个 exe） | 握手 → `/api/health`（`service`/`protocol`/`version` 正确）→ `POST /api/system/shutdown` → 进程退出、登记表清理 |

CI 的 `build` job（tag 触发）复跑同一套打包流程。

## 5. 后续阶段的落地情况

### 5.1 阶段 2（omni-link）——已完成

- 新增子模块 `vendor/omni-backend`（本地 clone + 手写 `.gitmodules` + `absorbgitdirs`，
  绕开 Git 对 `file://` 子模块的限制），指针固定到迁移提交。
- 删除仓库内 `python/` 源码（434 个文件）；`python/` 只作为打包输出目录
  （`build.ps1` 会重建 `python/dist/omni-backend` 与 `python/dist/omni.exe`）。
- `electron/python-bridge.ts` 开发态候选：`OMNI_BACKEND_DIR` →
  `<repo>/vendor/omni-backend/link/server.py` → 旧 `<repo>/python/server.py`（过渡兜底）；
  生产态仍是 `resources/python/omni-backend.exe`。
- `package.json`：`python:dev` / `python:install` 指向 `vendor/omni-backend/link`。
- `build.ps1`：新增后端子模块守卫；Step 1 改为
  `link/build.py --out "$ProjectRoot\python\dist"`（产物路径与 `extraResources` 不变）。
- 开发态数据（`devices.db`、已装 CMSIS-Pack）从 `python/data/` 迁到
  `vendor/omni-backend/link/data/`，未丢失。
- 验证：`npm run typecheck` 通过；`npm run python:dev` 起后端（端口 9001、
  `service=link`、`version=0.3.2`）并优雅关停；子模块工作区干净。

### 5.2 阶段 4（omni-cli）——已完成

- `settings.py`：`ServiceInfo` / `services()`，两个服务各自的状态目录、登记表、
  运行时记录、后端目录解析（环境变量 → 持久化配置 → 仓库布局 → 已安装 GUI）。
- 新增 `autoinstall.py`：探测 `%LOCALAPPDATA%/Programs/omni-{link,bus}/resources/python`
  与 `OMNI_LINK_HOME` / `OMNI_BUS_HOME`。
- `runtime.py`：命令组 → 服务映射（`serial/net/modbus/usb` → bus，其余 → link）、
  `--service link|bus` 覆盖、`* status` 拒绝 `--managed`。
- `commands/backend.py`：`start|stop|status [link|bus]`（默认 link，向后兼容）+ `list`；
  `status` 不带服务名时输出两个服务，`--json` 带 `services` 数组。
- `docs/backend-protocol.md` 改为指向本仓库 `docs/protocol.md`（单一来源）。
- 验证：11 个 CLI 测试全绿；`omni backend start/status/list/stop`（link + bus）、
  `probe list`、GUI 已启动后端的复用、`--service` 覆盖、启动失败时的可执行提示均实测通过。

### 5.3 阶段 3（omni-bus GUI）——未做

- `git rm -r python`、加子模块、`python-bridge.ts` 候选改为 `vendor/omni-backend/bus/server.py`、
  `build.ps1` 调 `bus/build.py --out python/dist`。
- **必须同时处理**：bus 产物 exe 名为 `omni-bus-backend.exe`，因此 `extraResources`
  （`python/dist/omni-bus-backend` → `resources/python`）与 `python-bridge.ts` 里的
  `omni-backend.exe` 字面量都要改；否则安装包会缺后端。

## 6. 回滚

- 迁移前分支：`omni-link` 的 `backup/pre-backend-split`（如需要可从 GUI 仓库历史恢复
  `python/`）；本仓库按服务打 tag（`link-v0.3.2`、`bus-v0.1.1`）便于定点回退。
- GUI 侧回退只需把 `vendor/omni-backend` 子模块指针退回上一个 commit。
