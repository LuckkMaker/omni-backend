# USB 能力实施计划（backend + CLI，Windows）

> 状态：草案 v1（M0–M2 完成；M4a 离线用例完成；M3 请求拆分为 CDC/HID/WinUSB 三条通道，CDC 与 HID 已完成，WinUSB 待做）  
> 范围：`omni-backend/bus` 后端 + `omni-cli` 命令组  
> 不在范围：GUI、Linux/macOS、主机侧等时传输、USB-IF 合规测试、固件代码生成

## 0. 目标与非目标

**目标**：把 USB 做成后端的一个完整能力域，CLI 可脚本化、可进 CI：

1. **描述符**：解析、校验、生成、模板、导出（只输出 hex / JSON）；
2. **请求**：SetupAPI 枚举、WinUSB 会话、控制/批量/中断收发、管道控制、回环吞吐；
3. **抓包**：USBPcap 启停、传输查询/详情/描述符树/导出/离线打开、`--follow` 跟踪；
4. **测试**：声明式用例（JSON），断言可落在响应上，输出 JSON 报告与退出码。

**非目标**（明确不做，避免承诺）：

- 不做虚拟 USB 设备（Windows 无通用驱动无法伪造枚举）；
- 不做等时传输主机侧收发（WinUSB 不支持；需要 libusbK 驱动，代价远大于收益）；
- 不做 USB-IF Chapter 9 / USBCV 合规认证，只做**开发回归**；
- 不生成固件 C 代码（本轮只到 hex/JSON；与 omni-emb `usbd` 的对齐留到下一轮）；
- 不接管 GUI（`src/pages/usb` 仍只用抓包接口，新增接口对前端无破坏性）。

## 1. 关键结论：USBPcap 不内置

**结论：不内置，也不静默下载。** 理由：

| 维度 | 事实 |
| --- | --- |
| 组成 | USBPcap 不是单个 exe：`USBPcapCMD.exe` 只是用户态客户端，真正的抓包能力来自内核过滤驱动 `USBPcap.sys` + `USBPcap.inf` + 签名目录 `usbpcapamd64.cat` |
| 安装 | 必须走安装器写入驱动、创建 `\\.\USBPcapN` 控制设备，需要管理员权限，安装后通常要重启 |
| 只带 exe | 只分发 `USBPcapCMD.exe` 在未装驱动的机器上**完全不可用**，会误导用户 |
| 许可证 | BSD-2-Clause 允许再分发，但要求保留版权与许可声明；内核驱动与 OS/内核版本强耦合，分发方要承担兼容、升级、卸载与蓝屏类问题 |
| 生态 | Wireshark 安装包即可选装，用户机器上往往已有；官方安装器本身就有版本升级通道 |
| 体积/签名 | 安装包增重、杀软误报、驱动签名与 OMNI 自身签名流程不同源 |

**替代做法（v1 就按这个实现）**：

1. **自动发现**：沿用现有 `find_usbpcap()`（注册表卸载项 → 常见安装目录 → PATH → Wireshark extcap 目录）；
2. **诊断命令**：`omni usb doctor` 输出「是否安装 / 路径 / 版本 / 是否管理员 / 可抓 root hub / 建议」；
3. **手动指定**：`--usbpcap-path`（已有后端参数，CLI 透传）；
4. **只给指引**：`omni usb capture doctor` 在未安装时打印官方地址 <https://desowin.org/usbpcap/> 与「安装后重启」；
5. **不做**：静默下载、捆绑安装器、自动提权。

> 关键点：**描述符与请求功能不依赖 USBPcap**。没装 USBPcap 的机器上，`omni usb desc *`、`omni usb control/read/write/echo` 全部可用；只有 `omni usb capture *` 要求安装 + 管理员。

## 2. 架构

### 2.1 后端（`bus`）

```
bus/
├── core/
│   ├── usb_desc.py      【新】描述符模型：parse / build / validate / emit / templates
│   ├── usb_host.py      【新】SetupAPI 枚举 + WinUSB 会话池 + 传输
│   ├── usb_test.py      【新】用例执行器（步骤 / 断言 / 报告）
│   ├── usb_decode.py    【改】抓包解码；描述符解析改为复用 usb_desc
│   ├── usb_pcap.py      【不改】pcap 流读取 / IRP 重组
│   └── usb_backend.py   【改】捕获管理；补 bufferlen、devices 过滤、afterId 游标、replay
├── api/
│   ├── usb.py           【改】抓包路由（现有 10 条）
│   ├── usb_host.py      【新】SetupAPI 枚举 / 会话 / 传输路由
│   ├── usb_desc.py      【新】描述符 parse / validate / build / 模板路由
│   └── usb_test.py      【新】用例执行路由
├── server.py            【改】注册新增路由
└── tests/
    ├── usb_fixtures.py       【改】补描述符样本、BOS/MSOS、畸形样本
    ├── test_usb_desc.py      【新】round-trip / 校验规则 / 快照
    ├── test_usb_host.py      【新】ctypes 打桩：会话、超时、错误映射、独占
    └── test_usb_test_runner.py【新】步骤执行、断言、报告、退出码
```

原则：

- **零新增第三方依赖**。枚举/传输继续用 `ctypes` 直调 `setupapi.dll` / `winusb.dll`（`winusb_test.py` 已在真机验证），打包脚本 `bus/build.py` 的 `REQUIRED_PACKAGES` 不变。
- 阻塞调用不能占住事件循环：所有 WinUSB 传输放进**独立工作线程 + 每次调用超时**，API 用 `run_in_threadpool` 等待；退出时 `WinUsb_AbortPipe` 兜底。
- **一份描述符解析**：`usb_decode.py` 里现有的 `parse_device_descriptor` / `parse_config_descriptor` 收敛进 `usb_desc`，抓包解码与 CLI 共用，禁止第三份实现。omni-emb 的 `utilities/scripts/usb_parse` 不再演进，脚本里的解析规则（CDC functional descriptor 等）一次性搬过来后即可停用。
- 抓包与请求**同进程共存**：USBPcap 是 root hub 过滤驱动，不独占设备，WinUSB 打开同一设备时仍能抓到 URB——这是「发一条请求，立刻在抓包里看到」的前提，M3 首次联调时实测确认。

### 2.2 CLI（`omni-cli`）

```
omni_cli/
├── commands/usb.py     【新】omni usb 命令组（parser + handler）
├── commands/__init__.py【改】注册 usb 模块
├── cli.py              【改】COMMAND_CATEGORIES 的 bus 分组加入 usb
└── runtime.py          【不改】COMMAND_SERVICE 已含 "usb": bus
```

- 会话型命令（`open/close/control/read/...`）与 `serial` 一样用 `--id`（默认 `cli`）在**后端**持有 Winsock 句柄，CLI 进程退出不影响会话；`omni backend stop` 一并清理。
- 无会话的命令（`list/info/desc/capture`）不要求先 `open`。
- 所有命令支持 `--json`；状态类命令沿用 `reject_managed` 规则（`status` 不允许 `--managed`）。
- `--follow` 用 `after_id` 轮询（复用 `docs/protocol.md` 的游标约定），不做 WebSocket。

## 3. 命令面（CLI 契约）

### 3.1 设备与请求

> **覆盖范围（重要）**：枚举、描述符、抓包面向总线上的**所有**设备；但"主动发请求"
> 必须有一个**把接口暴露给用户态的函数驱动**。Windows 没有 Linux `usbfs` 那样的
> 通用裸设备通道，所以覆盖面是：
>
> | 设备绑定 | 能做什么 | 不能做什么 |
> | --- | --- | --- |
> | WinUSB（MS OS 2.0 / INF） | 任意控制传输 + 批量/中断收发 | 等时传输 |
> | HID（系统 HID 驱动） | Input/Output/Feature 报告、Get/Set_Report | 任意 SETUP（厂商请求需走 Feature Report） |
> | CDC ACM（`usbser` → COM） | 数据收发、line coding、DTR/RTS | 任意 SETUP |
> | usbstor / 厂商私有驱动 | 只能看（抓包/描述符） | 用户态无法直接收发 |
>
> 想对"别的驱动占用的设备"发请求，只能用 UsbDk / libusbK 之类**替换驱动**，
> 代价是设备在系统里不再按原功能工作；v1 不做，失败时明确提示原因。

| 命令 | 说明 |
| --- | --- |
| `omni usb doctor [--json]` | USBPcap / 管理员 / WinUSB 环境诊断，给出下一步建议 |
| `omni usb list [--all] [--vid] [--pid] [--guid ...] [--json]` | 枚举；默认按内置 GUID 找 WinUSB 设备，`--all` 列全部 USB 设备（含驱动服务名，用于判断「没枚举」还是「驱动没绑定」） |
| `omni usb info [--index N] [--vid/--pid] [--hex] [--json]` | 设备/配置/接口/端点详情，`--hex` 附原始描述符 |
| `omni usb open --id cli [--index N\|--vid/--pid] [--interface N] [--timeout-ms]` | 打开接口（独占），失败给出可执行提示（驱动未绑定 / 被占用 / 无权限） |
| `omni usb close --id cli` | 关闭并释放句柄（幂等） |
| `omni usb status --id cli [--json]` | 会话列表与占用状态 |
| `omni usb control --id cli --request-type 0x80 --request 0x06 --value 0x0100 --index 0 --length 18 [--data HEX\|--file F] [--decode]` | 控制传输；`--decode` 按 wValue 自动解析（GET_DESCRIPTOR → 描述符树，GET_LINE_CODING → line coding） |
| `omni usb read --id cli --pipe 0x81 --length 64 [--count N] [--out F] [--timeout-ms] [--hex]` | IN 传输；`--count 0` 持续收 |
| `omni usb write --id cli --pipe 0x01 (--data HEX\|--file F) [--chunk 64] [--repeat N]` | OUT 传输；`--chunk` 按包拆分 |
| `omni usb pipe --id cli --pipe 0x81 (--abort\|--flush\|--reset)` | 管道复位 |
| `omni usb echo --id cli [--out-pipe 0x01] [--in-pipe 0x81] [--size N] [--count N] [--pipeline N] [--pattern increment\|random\|zeros\|aa\|ff] [--json]` | 回环自检，含 `--pipeline` 连续写（暴露单缓冲/RX 未重装类缺陷），输出吞吐与不一致计数 |

### 3.2 描述符

| 命令 | 说明 |
| --- | --- |
| `omni usb desc parse (--hex "12 01 ..." \| --file F \| -) [--json]` | 自动识别 setup 包 / 设备 / 配置集合 / 字符串 / BOS；`--json` 给结构化结果 |
| `omni usb desc validate --file doc.json [--json]` | 规则校验，返回 `{ok, errors[], warnings[]}` |
| `omni usb desc build --file doc.json [--out F]` | 由 JSON 文档生成 hex；默认打印，`--out` 落盘 |
| `omni usb desc emit --file doc.json --format hex\|json [--out F]` | 统一导出（本轮只这两种格式） |
| `omni usb desc template (list \| show NAME) [--json]` | 模板：`device`、`cdc-acm`、`cdc-hid`、`winusb`、`msc`、`dfu`、`hid-keyboard`、`hid-custom` |
| `omni usb desc from-capture --pcap F [--bus N --device N] [--json]` | 从离线 pcap 反推描述符文档（抓包 → 改 → 生成，闭环起点） |

**描述符文档（JSON，唯一中间格式）**：

```json
{
  "version": 1,
  "device": { "bcdUSB": "0x0210", "vid": "0xC251", "pid": "0x0002",
              "bcdDevice": "0x0100", "class": 0, "maxPacketSize0": 64,
              "strings": { "manufacturer": 1, "product": 2, "serial": 3 } },
  "strings": { "0x0409": { "1": "Geehy", "2": "USBD WinUSB Echo" } },
  "configurations": [{
    "value": 1, "attributes": "0x80", "maxPower": 100,
    "interfaces": [{
      "number": 0, "alternates": [{
        "class": "0xFF", "subClass": 0, "protocol": 0,
        "endpoints": [
          { "address": "0x01", "attributes": "0x02", "maxPacketSize": 64, "interval": 0 },
          { "address": "0x81", "attributes": "0x02", "maxPacketSize": 64, "interval": 0 }
        ]
      }]
    }]
  }],
  "bos": null,
  "msos": null
}
```

`emit --format hex` 输出分节结果（不是一坨拼接），便于直接贴进代码或对比：

```json
{ "device": "12 01 00 02 ...", "configurations": [{"value":1,"hex":"09 02 20 00 ..."}],
  "strings": [{"index":1,"langId":"0x0409","hex":"..."}], "bos": null }
```

**校验规则（v1 必须实现）**：

- `wTotalLength` 与实际长度一致；`bNumInterfaces` 与接口数一致；
- 接口号从 0 连续、`bAlternateSetting` 连续；
- 端点地址唯一、方向与 `bmAttributes` 匹配；`bEndpointAddress` 在 1..15；
- `wMaxPacketSize` 与速度匹配（FS：控制/批量/中断 ≤64；HS：批量 ≤512、中断 ≤1024）；等时仅提示不支持主机侧测试；
- `bMaxPower` 与 `bmAttributes` 自供电位一致性；全配置总功耗提示（100 mA 单位）；
- 字符串索引存在（含 langId 0 缺失告警）；IAD 的 `bFirstInterface/bInterfaceCount` 覆盖的接口确实存在且连续；
- `bcdUSB ≥ 0x0200` 才能用 BOS/MS OS 2.0（低版本给 warning）。

### 3.3 抓包

| 命令 | 说明 |
| --- | --- |
| `omni usb capture doctor [--json]` | 是否安装、是否管理员、可抓 root hub；未满足时给安装/提权指引 |
| `omni usb capture start [--device \\.\USBPcapN] [--snaplen] [--bufferlen] [--devices 1,2] [--usbpcap-path PATH] [--json]` | 启动；默认自动选唯一 root hub，多 hub 时必须指定 |
| `omni usb capture stop [--json]` | 停止（幂等），返回统计 |
| `omni usb capture status [--json]` | 运行态、模式、统计、抓包文件 |
| `omni usb capture transfers [--since ID\|--follow] [--limit] [--type] [--dir] [--bus] [--device] [--endpoint] [--status] [--q] [--json]` | 增量拉取传输；`--follow` 轮询 |
| `omni usb capture detail ID [--json]` | 单条详情（setup、hex、解码树、等时包） |
| `omni usb capture devices [--json]` | 从描述符重建的设备树 |
| `omni usb capture open --pcap F [--json]` | 离线解析（不需要 USBPcap / 管理员） |
| `omni usb capture save --out F` | 导出当前抓包文件 |
| `omni usb capture replay ID --id cli [--json]` | 把抓包里的一条 SETUP/传输在真实设备上重放（M3 起可用） |

### 3.4 测试

| 命令 | 说明 |
| --- | --- |
| `omni usb test run --file case.json [--json] [--junit F] [--keep-going]` | 执行用例，退出码 0 通过 / 1 失败 / 2 用法错误 |
| `omni usb test list` | 内置用例模板（枚举冒烟、控制向量、echo 吞吐、HID 报表、CDC 吞吐） |

用例格式（步骤 + 断言，不引入 YAML 依赖）：

```json
{
  "name": "winusb-echo",
  "device": { "vid": "0xC251", "pid": "0x0002", "guid": "{...}" },
  "steps": [
    { "open":    { "interface": 0, "id": "t" } },
    { "control": { "requestType": "0x80", "request": "0x06", "value": "0x0100", "length": 18 },
      "expect":  { "ok": true, "length": 18, "field": "idVendor", "equals": "0xC251" } },
    { "write":   { "pipe": "0x01", "pattern": "increment", "size": 64 } },
    { "read":    { "pipe": "0x81", "length": 64 },
      "expect":  { "data": "matches:increment" } },
    { "close":   {} }
  ]
}
```

断言种类（v1）：`ok`、`length`、`equals/contains`（hex/字符串）、`matches`（回环图样）、`field`（用描述符模型取字段）、`maxDurationMs`。  
报告：`{name, ok, durationMs, steps:[{index, action, ok, durationMs, detail}], summary}`；`--junit` 生成 JUnit XML 供 CI 直接消费。

## 4. REST 契约

现有抓包路由不变（对 GUI 兼容）；新增只在 `/api/usb/host/*`、`/api/usb/desc/*`、`/api/usb/test/*`，避免与现有 `/api/usb/devices`（抓包重建的设备树）重名。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/usb/environment` | 已有；doctor 复用 |
| GET | `/api/usb/host/devices` | SetupAPI 枚举（`all/vid/pid/guid`） |
| GET | `/api/usb/host/sessions` | 会话列表与占用 |
| POST | `/api/usb/host/open` | `{id, key, interface, guid?, timeoutMs}` |
| POST | `/api/usb/host/close` | `{id}`，幂等 |
| POST | `/api/usb/host/control` | `{id, requestType, request, value, index, length, dataB64?, timeoutMs?, decode?}` |
| POST | `/api/usb/host/transfer` | `{id, pipe, direction, length?, dataB64?, chunk?, repeat?, timeoutMs?}` |
| POST | `/api/usb/host/pipe` | `{id, pipe, action: abort\|flush\|reset}` |
| POST | `/api/usb/host/echo` | `{id, outPipe, inPipe, size, count, pipeline, pattern, seed}` |
| GET | `/api/usb/desc/templates` | 模板列表 |
| GET | `/api/usb/desc/templates/{name}` | 模板文档 |
| POST | `/api/usb/desc/parse` | `{dataB64}` 或 `{hex}` → 结构化 |
| POST | `/api/usb/desc/validate` | `{document}` → errors/warnings |
| POST | `/api/usb/desc/build` | `{document}` → `{device, configurations[], strings[], bos, hex}` |
| POST | `/api/usb/desc/from-capture` | `{path, bus?, device?}` → document |
| GET | `/api/usb/transfers` | 已有；响应补 `oldestId/nextId`，支持 `afterId` 增量 |
| POST | `/api/usb/capture/replay` | `{transferId, id}` |
| GET | `/api/usb/test/templates` | 内置用例 |
| POST | `/api/usb/test/run` | `{document\|name}` → 报告 |

约定：变更类接口统一 `{success, code?, error?}`（沿用 `api/usb.py` 现有风格）；大块二进制用 base64，单次上限 4 MiB；所有路径参数用 camelCase。

**协议版本**：只增不改，`protocol` 保持 1；`BACKEND_VERSION` 从 `0.1.4` 升到 `0.2.0`，CLI 同步发版。

## 5. 里程碑

| 里程碑 | 内容 | 交付物 | 验收 | 预估 |
| --- | --- | --- | --- | --- |
| **M0 地基** | `usb_desc` 模型骨架；把 `usb_decode` 的解析收敛进来；`usb.py` 命令组注册（仅 `--help`，handler 抛未实现） | `core/usb_desc.py`、`omni usb --help`、`test_usb_desc.py` | `python -m unittest discover -s bus/tests` 全绿；后端只有一份描述符解析实现 | 3–4 人日 |
| **M1 抓包接线** | `omni usb doctor`；CLI 封装现有抓包接口；`transfers --follow`；`--bufferlen/--devices`；`open/save/detail/devices` | `commands/usb.py` 抓包部分 | 真机：先 start 再插拔，能 `--follow` 看到枚举；离线 pcap 在无 USBPcap 机器上可解析 | 2–3 人日 |
| **M2 描述符** | parse/validate/build/emit/template/from-capture；导出 hex/JSON | `usb_desc` 全量 + `api/usb_desc.py` | 8 个模板 round-trip；CDC 复合样本 `parse(build(x)) == x`；校验规则各有正反例 | 6–8 人日 |
| **M3a CDC** | 枚举 USB CDC、open/send/read/status/close、BREAK | 复用 `serial_backend` + `/api/serial/break` | `omni usb cdc list` 只列 USB 串口；收发与 `omni serial` 同源 | ✅ 已完成 |
| **M3b HID** | HID 枚举、open、Input/Output/Feature 报告读写 | `core/usb_hid.py` + `api/usb_hid.py` + 命令 | `usb hid list/open/read/write/feature` 能对 `usbd_hid_custom` 收发报告 | ✅ 已完成 |
| **M3c WinUSB** | SetupAPI 枚举、控制/批量/中断、pipe 控制、echo | `core/usb_winusb.py` + `api/usb_winusb.py` + 命令 | 接 `usbd_winusb` 跑通 list/info/control/read/write/echo --pipeline | ✅ 已实现（链路/结构体测试通过，真机传输待接 `usbd_winusb` 验证） |
| **M4a 测试（离线 pcap）** | 用例执行器、`expect` 断言、JSON/JUnit 报告、退出码 | `core/usb_test.py` + `api/usb_tests.py` + 命令 | 录制的 pcap 可断言传输/设备/描述符；失败停在首个断言；JUnit 可被 CI 消费 | ✅ 已完成 |
| **M4b 测试（动作步骤）** | `control/write/read/echo` 步骤 + 真机用例 | 依赖 M3 的宿主能力 | 三个通道各有一条真机用例通过；失败时停在首个断言并输出 JUnit | ✅ 已完成 |

合计约 **5–6 周**（单人、含联调）。M1 与 M2 无依赖，可并行；M3 依赖 M0；M4 依赖 M2+M3。

**建议顺序：M0 → M1 → M2 → M3 → M4**。M1 先落地是为了尽早验证 CLI 的会话/游标约定，且后端已具备、风险最低；M2 是 M3/M4 的公共依赖，紧随其后。

### 5.1 真机验证记录（2026-11-18，STM32F407IG + J-Link）

| 例程 | 命令 | 结果 |
| --- | --- | --- |
| `usbd_winusb` | `usb winusb list/info/open` | 枚举到 `C251:0002`，full speed，2 个 bulk 端点（0x01 OUT / 0x81 IN） |
| | `usb winusb control --request 0x06 --value 0x0100 --length 18` | 返回设备描述符 `12 01 10 02 ...` |
| | `usb winusb write/read` | 写入 `01 02 03 04` 后原样读回 |
| | `usb winusb echo --size 64 --count 100` | 0 不一致，约 100 KiB/s |
| | `usb winusb echo --size 512 --count 5 --pipeline 32` | **按预期失败**（`usbd_winusb` 单缓冲，RX 只在 IN 完成回调里重装） |
| `usbd_hid_custom` | `usb hid list/open/write/read` | 枚举 `C251:0001`，IN/OUT 64 字节；写 report id 1 × 64B 成功；读到设备周期上报的 64B 报告 |
| `usbd_cdc_acm` | `usb cdc list/open/send/read/break` | 枚举为 `COM73`；发送 `hello omni` 后回显；BREAK 100 ms 成功 |

踩到的两个坑（已修）：

1. `WinUsb_Initialize` 要求接口句柄以 `FILE_FLAG_OVERLAPPED` 打开，否则失败且 `GetLastError` 为 0；
2. `WinUsb_QueryDeviceInformation(DEVICE_SPEED)` 的 length 必须是 1，返回 1=full / 3=high（不是 0..3 的速度枚举）。

补充能力（已实现）：

- `omni usb winusb list --all`：列出总线上全部 USB 设备与驱动服务
  （实测可见 `WINUSB` / `usbser` / `HidUsb` / `USBSTOR` / `usbvideo` 等），
  用于判断"没枚举"还是"驱动没绑定"；
- `winusb control/echo --capture`：请求前后自动启停 USBPcap，并按端点核对
  本次产生的传输（离线单测覆盖）；**真机端到端受管理员权限限制**：
  非管理员直接跑 USBPcapCMD 会留下提权 worker 并锁住 pcap，
  因此该路径必须用管理员终端验证。

用例化验证（M4b，`omni usb test run --file ...`）：

| 用例文件 | 结果 |
| --- | --- |
| `docs/examples/usb/winusb-echo-smoke.json` | PASS 6/6（open/control/write/read/echo/close，echo ≈235 KiB/s） |
| `docs/examples/usb/hid-smoke.json` | PASS 4/4（写 64B 报告 + 读周期报告） |
| `docs/examples/usb/cdc-echo.json` | PASS 4/4（COM73 发一行、回显校验） |
| 负例（read 期望 `FF FF`） | FAIL 且退出码 1、`--junit` failures=1、后续步骤停止、close 仍执行 |

## 6. 关键设计决策与风险

| 决策/风险 | 处理 |
| --- | --- |
| WinUSB 独占 | 后端起会话池，同一接口路径只允许一个会话；打开失败映射 `usb_busy` / `usb_driver_unbound` / `usb_not_found` 三种可读错误 |
| 阻塞调用拖死后端 | 传输在工作线程执行，单次调用有超时；服务关停时 `AbortPipe` + 关闭句柄；API 超时不等于设备无响应，错误信息区分 |
| 设备身份映射 | 抓包用 `bus/device`，请求用 SetupAPI 路径；用抓到的 device descriptor（VID/PID + 序列号字符串）建映射，失败时要求用户显式选设备，不做猜测 |
| 64 位指针 | 沿用 `winusb_test.py` 已验证的 ctypes 结构体与 `WinUsb_*` 声明；`test_usb_host.py` 对结构体大小与打包做断言 |
| 管理员权限 | 后端不自动提权；`capture start` 在非管理员时返回 `not_elevated`，CLI 打印「以管理员重开终端」的命令 |
| USBPcap 静默丢包 | `capture status` 暴露 `dropped` 统计（基于 URB 缺口启发式）；文档写明不能当无损抓包用 |
| 大 payload | JSON + base64，单次上限 4 MiB；`read --out` 流式落盘，不经过 JSON |
| 安全问题 | 后端只监听 127.0.0.1（既有约定）；`host` 能力可对任意已绑定设备写入，属于本机用户自担风险，文档声明 |
| 打包 | 无新依赖；若后续加 HID（`hid.dll` ctypes）仍零依赖；引入 hidapi 需改 `bus/build.py` 与安装包 |

## 7. 测试策略

- **离线单测（CI 必跑）**：描述符 round-trip 与快照、校验规则正反例、pcap fixtures、`afterId` 游标、用例执行器（用假会话注入结果）、错误码映射。
- **ctypes 打桩**：`test_usb_host.py` 用 `unittest.mock` 替换 `winusb.dll`/`setupapi.dll` 符号，覆盖成功、超时、STALL、句柄泄漏、关闭时传输中。
- **硬件在环（不进 CI）**：`OMNI_USB_HW=1` 才跑，文档给接线与命令；至少覆盖 `usbd_winusb`、`usbd_winusb_cdc`、`usbd_cdc_acm`、`usbd_hid_custom` 四个 omni-emb 示例。
- **CLI 契约**：`omni-cli/tests/test_usb_cli.py` 用假后端 HTTP 断言请求体/输出/退出码；复用现有 `tests/support.py` 的后端定位逻辑。
- **回归基线**：M3 起把 `winusb_test.py` 的 `echo --pipeline 32`（`usbd_winusb` 应失败、`usbd_winusb_cdc` 应通过）固化成用例，作为「测试能力真的能发现缺陷」的证据。

## 8. 完成定义（DoD）

- [ ] `omni usb` 出现在 `omni --help` 的 bus 命令分组（串口/网络/USB），`--help` 全中文、有示例；
- [ ] 描述符：8 个模板可 build/validate/emit；CDC 复合样本 round-trip 通过；
- [ ] 请求：真机跑通 list/info/open/control/read/write/echo/pipe，错误场景有可执行提示；
- [ ] 抓包：真机 start→插拔→follow→detail→save 全链路；离线 pcap 在无 USBPcap 机器可解析；
- [ ] 测试：5 个内置用例 + JSON/JUnit 报告 + 退出码；
- [ ] 文档：`docs/protocol.md` 补路由与错误码、`omni-cli/docs/cli-coverage.md` 补覆盖表、`bus/README` 或本文档补使用示例；
- [ ] 版本：bus `0.2.0`、CLI 同步；`python scripts/selfcheck.py --service bus` 通过。

## 9. 后续（不在本轮）

- HID Report Descriptor 字段级解码（键盘/鼠标/手柄语义化显示）；
- 描述符生成 omni-emb `usbd` 的 C 配置代码；
- 抓包侧断言（用例可断言「总线上出现了某个 SETUP」）；
- pcapng、Linux `usbmon`、传输时序/吞吐图（GUI 阶段）。
