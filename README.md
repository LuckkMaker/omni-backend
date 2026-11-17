# omni-backend

OMNI 平台的**后端仓库**：一个仓库装两个独立的 FastAPI 后端进程，供 GUI
（omni-link / omni-bus）与 CLI（omni-cli）共用。

```
omni-backend/
├── common/omni_service.py   共享：状态目录、登记表、health 载荷、优雅关停
├── link/                    探针 / 烧录 / RTT / 调试（内置 pyOCD 源码）
├── bus/                     串口 / 网络 / Modbus / USB
├── scripts/build.py         按服务调度 PyInstaller 构建
├── scripts/selfcheck.py     契约自检：起服务 → health → 登记表 → 关停
├── tests/                   两个服务共用的契约测试
└── docs/protocol.md         协议唯一来源（服务发现 / 版本 / 流式游标 / 错误）
```

两个服务**永远是两个进程**，互不 import，只共享 `common/`。合并成一个进程会让
探针操作卡住时连串口也一起僵死。

## 快速开始（源码态）

```bash
# 1) 装依赖（各自独立；link 需要 pyOCD 的一堆依赖，bus 只需串口/Modbus）
python -m venv .venv
.venv/Scripts/pip install -r link/requirements.txt   # Windows
.venv/Scripts/pip install -r bus/requirements.txt

# 2) 起服务（任意 cwd 都可以；--port 0 = 自动分配端口）
python link/server.py --port 0
python bus/server.py  --port 0
```

后端就绪时向 **stdout** 打印一行 `{"port": 57123}`，日志全部走 stderr。

开发态端口：`link/backend.config.json`（9001）、`bus/backend.config.json`（9004）。
不传 `--port` 时才读它；`OMNI_DEV_CONFIG` 可指向别的配置文件覆盖。

## 自检与测试

```bash
# 契约自检（起真进程，检查 health / 登记表 / 优雅关停 / 清理）
python scripts/selfcheck.py --service link
python scripts/selfcheck.py --service bus

# 共享契约单测（快，不启动进程）
python -m unittest discover -s tests -v

# 服务自己的回归测试（无需硬件）
python link/tests/test_rtt_scan.py
python bus/tests/test_usb_decode.py
```

`scripts/selfcheck.py` 会把状态/数据目录指到临时目录，不会污染
`%LOCALAPPDATA%` 或源码目录，失败时打印后端 stderr 末尾便于定位。

## 打包

```bash
python scripts/build.py --service link        # dist/link/omni-backend/omni-backend.exe
python scripts/build.py --service bus         # dist/bus/omni-bus-backend/omni-bus-backend.exe
python scripts/build.py --service all --clean # 两个都打
```

每个产物构建后会跑一次 `<exe> --help` 自检（只验证能加载依赖，不启动服务）。
GUI 仓库的 `build.ps1` 调用这里的脚本，并把产物复制到安装包的 `resources/python/`。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `OMNI_STATE_DIR` | link 的状态目录（默认 `%LOCALAPPDATA%\omni-link`） |
| `OMNI_BUS_STATE_DIR` | bus 的状态目录（默认 `%LOCALAPPDATA%\omni-bus`） |
| `OMNI_DATA_DIR` | 用户数据目录（设备库 `devices.db`、已装 CMSIS-Pack、自定义 FLM） |
| `OMNI_DEV_CONFIG` | 开发态端口配置文件的显式路径 |
| `OMNI_BACKEND_PORT` | GUI/CLI 侧的端口覆盖（后端自身不读） |
| `PYTHONIOENCODING` | 显式设置时后端不再强制 UTF-8 stdio |

## 版本

- 每个服务独立发版：tag `link-vX.Y.Z` / `bus-vX.Y.Z`。
- 协议版本 `protocol` 见 [docs/protocol.md](docs/protocol.md)，只增不改语义。
- 迁入本仓库的过程与取舍见 [docs/migration.md](docs/migration.md)。

## 许可证

MIT
