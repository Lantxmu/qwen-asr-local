# Qwen ASR Local

本项目是一个本地实时语音识别与字幕显示工具，能够从麦克风采集音频，并将音频流发送到 Qwen 的 ASR streaming 接口进行识别。识别结果可以在桌面悬浮字幕中实时展示，也可以在终端中查看，并将最终文本保存到 transcript 归档文件中。

## 功能概览

- 实时采集麦克风音频
- 通过 WebSocket 连接 Qwen ASR 服务
- 支持桌面端悬浮字幕显示
- GUI 支持暂停、继续识别，并可单独结束当前识别任务
- 支持终端实时输出识别结果
- 支持自动保存最终识别文本到 Markdown 文件
- 支持测试 Qwen 连接状态与麦克风状态

## 项目结构

```text
qwen-asr-local/
├── main.py                  # 程序入口
├── gui.py                   # PySide6 图形界面与字幕窗口
├── requirements.txt         # Python 依赖
├── .env.example             # API Key 配置模板
├── asr/
│   ├── __init__.py
│   ├── client.py            # Qwen API Key 与 WebSocket 连接管理
│   ├── segment.py           # 句子状态管理（PARTIAL/FINAL）
│   └── session.py           # 实时 ASR 运行循环
├── audio/
│   ├── __init__.py
│   └── capture.py           # 麦克风采集逻辑
├── output/
│   ├── __init__.py
│   ├── archive.py           # 转写记录保存与读取
│   └── transcript.py        # 控制台输出格式化
├── transcripts/             # 本地生成的转写记录（不会提交到 Git）
├── .env                     # 本地 API Key（不会提交到 Git）
└── README.md
```

## 运行环境

- Python 3.10+
- 麦克风设备
- 可访问 Qwen / DashScope API 的网络环境

## 安装依赖

在项目根目录执行：

```bash
pip install -r requirements.txt
```

## 配置 API Key

在`env`中填入自己的 API Key：

```env
DASHSCOPE_API_KEY=your_api_key_here
```

程序会在运行时自动加载该环境变量；GUI 中也可以录入并保存 API Key。

## 启动方式

### 1. 启动 GUI

```bash
python main.py
```

默认情况下，若未指定 `--console` 且没有 `--duration-seconds`，程序会启动桌面 GUI。

### 2. 终端实时识别

```bash
python main.py --console
```

也可以限制识别时长：

```bash
python main.py --console --duration-seconds 30
```

### 3. 测试 Qwen 连接

```bash
python main.py --test-qwen-connection
```

### 4. 测试麦克风

```bash
python main.py --test-microphone
```

## 工作原理

1. 通过 [audio/capture.py](audio/capture.py) 读取麦克风 PCM 音频。
2. 在 [asr/session.py](asr/session.py) 中启动 Qwen ASR 的 streaming 任务。
3. 将音频帧持续发送到 WebSocket 连接。
4. 服务端返回识别事件；程序通过 [asr/segment.py](asr/segment.py) 维护 PARTIAL 与 FINAL 状态。
5. 识别结果输出到 GUI 或命令行，同时最终文本写入 [output/archive.py](output/archive.py) 生成的 Markdown 文件。

## 生成的转写文件

程序会将最终识别文本保存到 `transcripts/` 目录，文件名类似：

```text
transcripts/2026-09-26_18-34-43.md
```

文件内容以标题和逐句文本的形式追加保存。

## 注意事项

- 需要确保电脑麦克风正常工作。
- 需要正确配置 `DASHSCOPE_API_KEY`。
- Windows 下可能需要 PortAudio 相关音频支持；若出现麦克风初始化失败，请检查录音设备和驱动。
- GUI 功能依赖 PySide6；终端模式不依赖界面。

## 依赖列表

- `dashscope`
- `websocket-client`
- `sounddevice`
- `numpy`
- `python-dotenv`
- `PySide6`
