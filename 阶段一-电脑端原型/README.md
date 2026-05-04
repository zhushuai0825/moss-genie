# 心愿 Moss 精灵 · 阶一电脑端语音原型

阶段一目标：

- 跑通一个本地电脑端语音对话台，先模拟娃娃交互。
- 支持浏览器麦克风语音输入，页面展示“我听到了什么”。
- 展示用户输入、大模型回复、本轮命中的旧记忆和已保存记忆。
- 实现 SQLite 本地记忆库。
- 实现 5 个安全电脑控制指令。
- 提供可替换的模型适配层，优先尝试 MiniMax，其次 Ollama，不可用时使用本地规则回复。
- 接入 MiniMax TTS 语音合成预览，生成音频保存在本地。

## 启动

```bash
cd /Users/zhushuai/Downloads/心愿MOSS精灵/阶段一-电脑端原型
/Users/zhushuai/.local/bin/python app/server.py
```

打开：

```text
http://127.0.0.1:8787
```

## 可选环境变量

复制 `.env.example` 为 `.env` 后手动填写。不要把真实密钥提交到代码里。

```bash
export OLLAMA_MODEL=qwen2.5:7b
export MINIMAX_API_KEY=你的密钥
export MINIMAX_TEXT_MODEL=MiniMax-M1
export MINIMAX_TTS_MODEL=speech-2.8-hd
export MINIMAX_TTS_VOICE='Chinese (Mandarin)_Cute_Spirit'
```

当前版本不会把 MiniMax 密钥写入数据库或日志。页面里的临时密钥输入只在本页会话里用于聊天和朗读，不会保存到文件。

## 阶段一已包含

- 语音输入：浏览器 Web Speech API，识别后自动发送到 Moss
- 本地朗读：浏览器 `speechSynthesis`，不需要 MiniMax Key
- 聊天接口：`POST /api/chat`，返回回复来源、命中记忆、自动保存的新记忆和会话记录
- 记忆接口：增删查导出
- 会话接口：`GET /api/conversations`
- 心愿接口：创建、推进成长值、归档
- 安全指令：打开网页、搜索网页、打开应用、调音量、保存截图
- 审计日志：所有电脑控制指令都会记录到本地 SQLite
- 语音合成：`GET /api/tts/status`、`POST /api/tts/synthesize`、`GET /api/audio/<文件名>`

## MiniMax TTS

页面右侧的“语音合成”面板支持模型、音色、速度、音量和音调调节。默认模型是 `speech-2.8-hd`，默认音色是 `Chinese (Mandarin)_Cute_Spirit`。

生成后的 mp3 文件会保存在：

```text
/Users/zhushuai/Downloads/心愿MOSS精灵/阶段一-电脑端原型/data/audio
```
