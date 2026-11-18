# OMNI 后端协议（v1）

本文件是 **协议唯一来源**。GUI（omni-link / omni-bus）与 CLI（omni-cli）都按这里
的约定与后端通信；`omni-cli/docs/backend-protocol.md` 只是指向本文件的一行链接，
避免两份漂移。

后端只是"能力的宿主"：调试/烧录/总线类命令的状态都在后端进程里。后端与客户端
可以独立发版（后端随 GUI 安装包分发、CLI 独立安装或作为 submodule），因此需要
显式契约来避免"命令写到一半发现后端不认"。

## 1. 服务发现

后端启动后把自己的 `{service, pid, host, port, version, protocol}` 写入用户级
注册表文件，客户端读取它来发现并复用**同一个**后端进程（不要另起一个去抢设备）。

| 服务 | 注册表文件 | 环境变量覆盖 |
| --- | --- | --- |
| `link`（探针/烧录/RTT/调试） | `%LOCALAPPDATA%\omni-link\backend.json` | `OMNI_STATE_DIR` |
| `bus`（串口/网络/Modbus/USB） | `%LOCALAPPDATA%\omni-bus\backend.json` | `OMNI_BUS_STATE_DIR` |

- 非 Windows：`$XDG_STATE_HOME/<service>`（默认 `~/.local/state/<service>`）。
- 文件名固定 `backend.json`；写入是原子的（临时文件 + `os.replace`）。
- 进程退出时删除登记表，且**仅当 `pid` 与自身一致才删**（避免误删后启动的新后端）。

注册表示例：

```json
{
  "service": "link",
  "pid": 17904,
  "host": "127.0.0.1",
  "port": 57123,
  "version": "0.3.2",
  "protocol": 1,
  "started_at": "2026-11-17T07:19:48+00:00"
}
```

客户端侧的端口优先级：`--backend-port` → 注册表 → 客户端自己的常驻后端记录
（`.backend_runtime.json`）→ `backend.config.json` 的开发端口 → `OMNI_BACKEND_PORT`。

### 1.1 启动握手

后端只从 `--port 0` 自动分配端口（`backend.config.json` 仅开发态可覆盖），
并在 uvicorn 绑定前向 **stdout** 打印一行：

```json
{"port": 57123}
```

日志一律走 **stderr**，保证 stdout 只有这一行握手 JSON。

## 2. 协议版本与兼容

- 后端在 `GET /api/health` 返回 `{status, pid, service, version, protocol, ws_connections}`。
- `protocol` 是整数，当前为 `1`；只增不改语义，新增字段/接口不算破坏性变更。
- CLI 声明 `BACKEND_PROTOCOL = 1`：
  - 响应缺 `protocol`（老后端）→ 按兼容处理；
  - `protocol > BACKEND_PROTOCOL` → stderr 提示"建议升级 OMNI CLI"，不阻断；
  - 兼容窗口：CLI 至少支持最近 N 个后端协议版本，超出窗口后新命令直接报错并给出升级指引。
- 破坏性改动（删除/改变既有字段或路由语义）必须递增 `protocol`。

## 3. 生命周期接口

- `GET /api/health`：健康探测（客户端用它区分"没进程"与"进程僵死"）。
- `POST /api/system/shutdown`：优雅关停；后端先回响应，再触发 uvicorn 退出，
  并带看门狗（宽限期 10s）兜底强退——探针/串口清理卡死时进程也必须消失。
  两个服务由 `common/omni_service.install_shutdown` 统一注册，行为一致。
- CLI `omni backend stop` 在拿不到优雅关停时按端口定位进程树强杀。

## 4. 流式数据

CLI 不订阅 WebSocket（零第三方依赖），所有流式数据都用"后端环形缓存 + seq 游标 + 轮询"：

- 后端把接收数据存进有上限的环形缓存，并维护 `oldest_seq` / `next_seq` / `dropped_chunks`。
- CLI 用 `--since <seq>` 拉取，返回 `{chunks, oldest_seq, next_seq, cursor, lost, truncated, session_reset}`；
  `lost=true` 表示请求的游标已被覆盖，`session_reset=true` 表示后端重启后 seq 重新开始。
- CLI 的 `--follow` 只是把上一次的 `cursor` 当成下一次的 `since`，间隔默认 0.2s。
- 现有实现：`omni rtt read --follow`（`link/core/rtt_backend.py::read_capture`）与
  `omni serial read --follow`（`bus/core/serial_capture.py`，另有 `POST /api/serial/read`
  同步等读：`timeout_ms` 内等首批数据，供 AT 命令式交互）；`net` 沿用同一契约。

## 5. 错误约定

- HTTP 4xx/5xx 的 `detail` 直接展示给用户，命令退出码 1。
- 端口可连但 HTTP 超时 → "后端僵死/被硬件操作阻塞"，引导 `omni backend stop`。
- 端口无监听 → 引导 `omni backend start` 或 `--managed`（仅一次性命令可用）。

## 6. 版本与发布

- 每个服务独立打 tag：`link-v0.3.2`、`bus-v0.1.1`；同一仓库一套 CI。
- GUI 安装包按 submodule commit 固定后端版本，与 CLI 版本解耦。

## 7. 两个服务的边界

| 能力 | link | bus |
| --- | --- | --- |
| 探针枚举 / 烧录 / 擦除 / 校验 | ✅ | ❌ |
| 目标芯片 / CMSIS-Pack / 自定义 FLM | ✅ | ❌ |
| Commander（内存读写/断点/执行） | ✅ | ❌ |
| RTT / 变量监视 / ELF 解析 | ✅ | ❌ |
| 串口 / 网络（TCP/UDP/TFTP） / Modbus | ❌ | ✅ |
| USB 抓包分析 | ❌ | ✅ |
| USB 描述符解析 / 校验 / 生成 / 模板 | ❌ | ✅ |
| `/api/system/info`、`/api/system/shutdown`、`/api/health` | ✅ | ✅ |

两个服务**互不 import**，只共享 `common/`（登记表、health 载荷、优雅关停）。

## 8. USB 扩展路由（protocol 1 内新增）

只增不改，不触及既有抓包路由；CLI 的 `omni usb` 与后续 GUI 共用。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/usb/environment` | USBPcap 安装 / 权限 / root hub |
| GET | `/api/usb/capture/status` | 捕获或离线解析状态（含 `progress`） |
| POST | `/api/usb/capture/start` | `{device_id, usbpcap_path?, snaplen?, bufferlen?, devices?}` |
| POST | `/api/usb/capture/stop`、`/save`、`/open` | 停止 / 导出 / 离线解析 |
| GET | `/api/usb/transfers` | 传输列表；响应含 `oldestId` / `nextId` / `buffered`，支持 `after_id` 增量 |
| GET | `/api/usb/transfers/{id}` | 单条详情（SETUP / hex / 解码树） |
| GET | `/api/usb/devices` | 抓包重建的设备描述符树 |
| GET | `/api/usb/desc/templates`、`/templates/{name}` | 描述符模板 |
| POST | `/api/usb/desc/parse` | `{hex}` 或 `{dataB64}` → 自动识别（Setup/设备/配置/字符串/BOS/限定符） |
| POST | `/api/usb/desc/validate` | `{document}` → `{ok, errors[], warnings[]}` |
| POST | `/api/usb/desc/build` | `{document}` → `{device, configurations[], strings[], bos, validation}` |
| POST | `/api/usb/desc/from-capture` | `{path, bus?, device?}` → 每设备一份文档 |
| GET | `/api/usb/test/schema` | 用例格式示例 |
| POST | `/api/usb/test/run` | `{case|path, junit?, stopOnFailure?}` → 步骤级报告 |

用例支持两类步骤：离线 pcap 断言（`transfers/transfer/device/errors/descriptor`）
与真机动作（`open/control/write/read/echo/close`，`device.channel=winusb|hid|cdc`）。
样例见 `docs/examples/usb/*.json`，格式定义见 `bus/core/usb_test.py::case_schema`。
| POST | `/api/serial/break` | `{connection_id, duration_ms}` → 发送 BREAK（CDC `SEND_BREAK` 等价） |
| GET | `/api/usb/hid/devices` | HID 设备枚举（VID/PID/产品名/报告长度） |
| POST | `/api/usb/hid/open` | `{id, vid, pid, serial?, path?, timeout_ms?}` |
| GET | `/api/usb/hid/status` | 会话状态 |
| POST | `/api/usb/hid/close` | `{id}` |
| POST | `/api/usb/hid/read` | `{id, length?, timeout_ms?}` → 一条 Input 报告 |
| POST | `/api/usb/hid/write` | `{id, hex|dataB64, report_id?, timeout_ms?}` |
| POST | `/api/usb/hid/feature/get`、`/feature/set` | Feature 报告读写 |
| GET | `/api/usb/winusb/devices` | 枚举绑定 WinUSB 的接口（可选 `guid=` 覆盖） |
| POST | `/api/usb/winusb/open` | `{id, path?|vid?/pid?, interface?, timeout_ms?, guids?}` |
| GET | `/api/usb/winusb/status`、POST `/close` | 会话状态 / 关闭 |
| POST | `/api/usb/winusb/control` | `{id, request_type, request, value, index, length, hex?}` |
| POST | `/api/usb/winusb/transfer` | `{id, pipe, direction: in|out, length?, hex?}` |
| POST | `/api/usb/winusb/pipe` | `{id, pipe, action: abort|flush|reset}` |
| POST | `/api/usb/winusb/echo` | `{id, out_pipe, in_pipe, size, count, pipeline, pattern, seed}` |

USB 相关错误码（响应 `code` 字段，`success=false`）：

`usbpcap_not_found`、`not_elevated`、`capture_already_running`、`device_busy`、
`invalid_bufferlen`、`invalid_devices`、`desc_parse_failed`、`document_invalid`、
`template_not_found`、`input_too_large`、`pcap_not_found`、`pcap_open_failed`、
`pcap_parse_error`、`case_missing`、`case_invalid`、`case_not_found`、
`case_too_large`、`case_read_failed`、`hid_unavailable`、`hid_not_found`、
`hid_busy`、`hid_read_failed`、`hid_write_failed`、`hid_feature_failed`、
`hid_invalid`、`winusb_unavailable`、`winusb_not_found`、`winusb_open_failed`、
`winusb_transfer_failed`、`winusb_invalid`。
