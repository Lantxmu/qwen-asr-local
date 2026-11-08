# ASR 架构与 ChatGPT 接入方案

核查日期：2026-10-09。目标：在 ChatGPT 网页／桌面端基于正在产生的转写进行问答、总结。

## 1. 当前架构（已通读源码）

```text
main.py
  ├─ GUI → gui.ASRWorker（QThread）
  └─ console
       ↓
asr.session.run_realtime_asr
  ├─ audio.capture：16 kHz、单声道、int16、100 ms 一帧
  ├─ asr.client：鉴权、run-task、双向 WebSocket、finish-task
  ├─ 接收线程 → queue.Queue → 会话循环消费事件
  └─ result-generated → TranscriptState.update
       ├─ PARTIAL → 替换实时字幕
       └─ FINAL → 显示；GUI 追加写入 transcripts/*.md
```

| 文件 | 职责与接入含义 |
| --- | --- |
| `asr/__init__.py` | 包说明，无运行逻辑。 |
| `asr/client.py` | 从项目 `.env` 取 DashScope key，使用 `qwen-audio-3.1-asr-flash-streaming`；创建任务、收 JSON 事件、结束任务与连接测试。 |
| `asr/segment.py` | 保存当前临时句、句子 ID；按 `sentence_end` 判定 FINAL；保留最近 256 个已提交 ID 去重。 |
| `asr/session.py` | 协调录音发送、接收线程、回调、错误及退出；停止时等待最后事件，最长约 10 秒，随后清理连接和线程。 |
| `audio/capture.py` | 阻塞读取音频；暂停时继续读取麦克风，但发送同长度零字节，保持连接。 |
| `gui.py` | Worker 自己消费原始事件并维护 `TranscriptState`；通过 Qt Signal 更新界面，FINAL 写归档；历史记录分页读取。 |
| `output/transcript.py` | 控制台消费原始事件和显示 PARTIAL/FINAL。 |
| `output/archive.py` | FINAL 逐段追加 Markdown，首次收到非空 FINAL 才创建文件；另有从尾部翻页的本地阅读器。 |
| `diagnostics.py` | 轮转日志、Python/Qt 错误及原生崩溃诊断。 |

值得明确的现状：

- “Local”是本地客户端；语音识别通过网络在 Qwen 服务执行。
- GUI 与控制台各自处理原始事件；`on_event` 回调替换默认控制台处理，并非额外订阅。
- 控制台当前没有调用 `TranscriptArchive`，因此本次文件桥接适用于 GUI 产生的归档。
- FINAL 去重依赖有效句子 ID，且只保留 256 个；无 ID 的重复消息不会去重。
- 接收队列未设上限；事件消费与音频发送在同一循环。应避免把外部网络请求放进回调。
- Markdown 没有逐句时间戳、明确会话 ID、暂停／结束状态，也不保存 PARTIAL。文件没有增长不能判定录音结束。
- `main.py` 顶层导入 GUI，现有“控制台不依赖界面”的说明并不完全符合实际导入行为。

这些是架构观察；本次未顺带重构采集、GUI 或连接生命周期。

## 2. Basic approach

先复用现有 FINAL 归档，增加独立只读 MCP 进程：

```text
GUI / Qwen ASR → FINAL Markdown（不断追加）
                         ↑ 按游标读取
                  chatgpt_bridge.py
                         ↑ MCP 工具调用
             Secure MCP Tunnel / 已鉴权 HTTPS 入口
                         ↑
               ChatGPT → 问答、总结
```

这样 ASR 无需等待 ChatGPT，桥接停止也不会影响字幕。只传文本，无需重新上传音频到 OpenAI 语音接口。转写通过工具返回后会进入 ChatGPT 上下文。

本次实现 `read_transcript(cursor="", max_bytes=16384)`：

- 操作者启动时指定一份 `transcripts/` 下的 Markdown；模型不能选择路径或枚举其他文件。
- 空游标从开头读取；后续使用 `next_cursor`，已有文本不会重复返回。
- 单次最多 64 KiB，只返回有完整空行分隔符的段落，保留中文 UTF-8 边界。
- `has_more` 表示还可能有后续段落；空结果表示暂时没有完整新增内容，不代表结束。
- 文件名与 `session_id` 可用于标识来源；`recording_status` 明确返回 `unknown`。
- 服务重启后游标失效；文件要求只追加，禁止运行中手工改写。检测替换或游标超出截断后的长度时会报错；不保证检测所有原地改写。
- 超过 64 KiB 的单个段落暂不支持，不会静默截断或跳过。
- 默认仅监听 `127.0.0.1:8765/mcp`；也支持 stdio。未实现 OAuth，不应把这个无鉴权原型直接暴露到公网。

## 3. 接入路径与边界

### A. 按需问答／总结：本次原型

ChatGPT 可以把自定义 MCP 服务作为插件调用，支持 SSE 和 Streamable HTTP。先在网页配置并验证，桌面客户端再按实际可见的插件入口测试，不假设所有客户端入口和权限一致。参见 [Add custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server)。

本机服务优先考虑 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)：它允许 ChatGPT 调用私网中的 MCP，无需公开本机监听端口。需要 Platform tunnel、运行凭证、相关权限，以及目标 ChatGPT 工作区关联。没有这些条件时，可部署带 OAuth 的 HTTPS 网关；那是额外部署工作。

普通工具调用只提供“提问时读到最新已落盘内容”。保持 MCP 连接不会自动把每句字幕加入对话，也不会自动发起模型回答。

### B. 自动跟进：后续 MCP Events 阶段

截至核查日期，官方 [MCP Events](https://developers.openai.com/plugins/build/mcp-events) 支持用户订阅更新并指定后续动作；适用 Work 网页聊天、桌面 Cloud Work 聊天及 dots，要求 MCP 2.0（`2026-07-28`）、持久订阅和 HTTPS webhook。该集成不支持事件的 polling/streaming delivery。

因此，“每隔一段转写自动总结”有官方探索路径，但不能仅凭当前普通 MCP 工具实现。本次依赖限定 MCP 1.x，**未实现 Events**。下一阶段应先核实账户场景，再设计 `transcript.updated`，将多句 FINAL 合并后通知；这是设计建议，不是已验证的自动总结功能。

### C. 备用方式

手动上传／粘贴归档适合验证总结效果，但只是一次快照。浏览器自动粘贴和自动点击发送不作为基础架构。OpenAI API 可用于另做应用内持续分析，但与你指定的 ChatGPT 客户端目标不同，未在本次接入。

## 4. 本地启动与实际连接

项目虚拟环境中安装可选依赖（基础 ASR requirements 不变）：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-chatgpt.txt
```

启动原来的 GUI，开始识别并说完一句话。用“打开存档目录”找到本次新建的文件，然后在另一个终端执行，将示例文件名换成实际文件名：

```powershell
.venv/Scripts/python.exe chatgpt_bridge.py --transcript "transcripts/2026-10-09_14-30-00.md"
```

按 Secure MCP Tunnel 官方说明安装及配置 tunnel-client，将上游 HTTP 地址设为 `http://127.0.0.1:8765/mcp`。保持两个进程运行，在 ChatGPT 网页的 Plugins → Add custom MCP server 中选择 Tunnel 并关联相应隧道。账户入口、权限和认证配置以实际页面为准；不要将 `localhost` 当成 ChatGPT 云端能直接访问的地址。

选用 stdio 的本地 MCP 客户端／隧道可以运行：

```powershell
.venv/Scripts/python.exe chatgpt_bridge.py --transport stdio --transcript "transcripts/2026-10-09_14-30-00.md"
```

连接后可以问：“读取当前共享的转写，总结已经讲到的重点。”随后继续讲话，再问：“读取上次游标之后的新内容，补充总结。”切换录音时重启桥接并指定新文件；不会自动共享下一次录音。

本次没有创建远程隧道、配置 ChatGPT 账户或上传真实转写。完成本地验证不等于已完成 ChatGPT 端到端连接。

## 5. 后续结构化改造

如果进入持续事件阶段，建议在 `asr/session.py` 统一产生 `TranscriptEvent(session_id, sequence, sentence_id, kind, text, received_at)`，分别交给 GUI、控制台、归档和事件存储。使用 SQLite 保存已确认句子及会话状态；PARTIAL 仅保留最新值。网络发送在独立工作队列执行，增加重试、批处理和幂等 ID。已有原始事件回调应保留兼容。

这样才能可靠支持跨进程增量读取、会话切换、时间范围查询、暂停／结束状态与事件重放。不要从 Markdown 文件修改时间推断这些信息。

验证顺序：本地中文／追加／游标测试 → MCP 发现与工具调用 → ChatGPT 手动读取 → 账户支持情况确认 → Events 订阅、断线重放和自动总结验收。
