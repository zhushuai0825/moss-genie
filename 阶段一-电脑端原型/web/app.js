const state = {
  commands: [],
  recognition: null,
  listening: false,
  speechUnavailable: false,
  ttsConfigured: false,
};

const $ = (selector) => document.querySelector(selector);

function showToast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function loadStatus() {
  const data = await api("/api/status");
  const model = data.model;
  const tts = data.tts;
  const card = $("#statusCard");
  card.querySelector(".status-dot").classList.add("ok");
  $("#statusTitle").textContent = "本地服务在线";
  $("#statusDetail").textContent = model.minimax_configured
    ? `MiniMax 文本模型：${model.minimax_model}`
    : "未配置云端模型，当前会使用 Ollama 或本地 fallback";
  $("#modelBadge").textContent = model.minimax_configured
    ? `MiniMax · ${model.minimax_model}`
    : model.ollama_available
      ? `Ollama · ${model.ollama_model}`
      : `本地 fallback · ${model.ollama_model}`;
  state.ttsConfigured = Boolean(tts.configured);
  setSelectValue($("#ttsModel"), tts.model || "speech-2.8-hd");
  setSelectValue($("#ttsVoice"), tts.voice_id || "Chinese (Mandarin)_Cute_Spirit");
}

function setSelectValue(select, value) {
  if (![...select.options].some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  }
  select.value = value;
}

function addMessage(role, text, meta = "") {
  const box = document.createElement("div");
  box.className = `message ${role}`;
  box.innerHTML = `<p>${escapeHtml(text)}</p>${meta ? `<small>${escapeHtml(meta)}</small>` : ""}`;
  $("#chatLog").appendChild(box);
  $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
}

function renderMemoryHits(items) {
  const list = $("#memoryHitList");
  list.innerHTML = items.length ? "" : "<p class='empty-text'>这轮没有命中旧记忆。</p>";
  for (const item of items) {
    const div = document.createElement("div");
    div.className = "item hit-item";
    const type = memoryType(item);
    div.innerHTML = `
      <strong>${escapeHtml(type)} · #${item.id}</strong>
      <p>${escapeHtml(memoryDisplayText(item))}</p>
      <small>${escapeHtml(item.tags || "无标签")}</small>
    `;
    list.appendChild(div);
  }
}

async function sendChatText(message) {
  const clean = message.trim();
  if (!clean) return;
  $("#chatInput").value = "";
  $("#transcriptText").textContent = clean;
  $("#lastUserText").textContent = clean;
  $("#lastAssistantText").textContent = "Moss 正在想...";
  addMessage("user", clean);
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: clean, api_key: $("#sessionApiKey").value.trim() }),
    });
    const meta = `来源：${data.source} · 命中记忆：${data.memory_hits.length}`;
    $("#lastAssistantText").textContent = data.reply;
    $("#ttsText").value = data.reply;
    addMessage("assistant", data.reply, meta);
    speakReply(data.reply);
    renderMemoryHits(data.memory_hits);
    if (data.saved_memory) showToast("已自动保存一条记忆");
    if (data.wish_created) showToast("已从聊天中种下一颗心愿种子");
    await Promise.all([loadMemories(), loadConversations()]);
  } catch (error) {
    $("#lastAssistantText").textContent = `出错了：${error.message}`;
    addMessage("assistant", `出错了：${error.message}`);
  }
}

async function sendChat(event) {
  event.preventDefault();
  await sendChatText($("#chatInput").value);
}

async function speakReply(text) {
  if (!$("#autoSpeak").checked) return;
  const audio = $("#replyAudio");
  $("#speakStatus").textContent = "正在生成 MiniMax 人声...";
  audio.classList.remove("ready");
  try {
    const data = await api("/api/tts/speak", {
      method: "POST",
      body: JSON.stringify({
        text,
        api_key: $("#sessionApiKey").value.trim(),
        model: $("#ttsModel").value,
        voice_id: $("#ttsVoice").value,
        speed: Number($("#ttsSpeed").value),
        volume: Number($("#ttsVolume").value),
        pitch: Number($("#ttsPitch").value),
      }),
    });
    audio.src = `${data.audio_url}?t=${Date.now()}`;
    audio.classList.add("ready");
    $("#speakStatus").textContent = `MiniMax 人声已生成：${data.voice_id}`;
    await audio.play().catch(() => {});
  } catch (error) {
    $("#speakStatus").textContent = "MiniMax 人声生成失败，请展开下方语音播放面板查看或换一个音色。";
    showToast(`MiniMax 人声朗读失败：${error.message}`);
  }
}

function setupSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  checkMicrophonePermission();
  if (!SpeechRecognition) {
    $("#speechSupport").textContent = "这个浏览器没有开放 Web Speech 语音识别。你可以先打字测试，或用 Chrome 打开。";
    $("#micButton").disabled = true;
    $("#micHint").textContent = "当前不可用";
    state.speechUnavailable = true;
    return;
  }
  const recognition = new SpeechRecognition();
  recognition.lang = "zh-CN";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onstart = () => {
    state.listening = true;
    $("#micButton").classList.add("listening");
    $("#micButton strong").textContent = "正在听";
    $("#micHint").textContent = "说完后会自动发送";
  };
  recognition.onresult = (event) => {
    let transcript = "";
    let finalText = "";
    for (const result of event.results) {
      transcript += result[0].transcript;
      if (result.isFinal) finalText += result[0].transcript;
    }
    $("#transcriptText").textContent = transcript || "正在听...";
    $("#chatInput").value = transcript;
    if (finalText.trim()) sendChatText(finalText);
  };
  recognition.onerror = (event) => {
    const message = speechErrorMessage(event.error);
    showToast(message);
    $("#speechSupport").textContent = message;
    $("#transcriptText").textContent = "语音识别没有成功。可以点击“重新测试语音”，或点“系统听写输入”后用键盘听写。";
    $("#micHint").textContent = "语音服务暂不可用";
    if (event.error === "network" || event.error === "service-not-allowed") {
      state.speechUnavailable = true;
      $("#micButton").classList.add("speech-warning");
    }
  };
  recognition.onend = () => {
    state.listening = false;
    $("#micButton").classList.remove("listening");
    $("#micButton strong").textContent = "点击说话";
    $("#micHint").textContent = "浏览器会请求麦克风权限";
  };
  state.recognition = recognition;
  $("#speechSupport").textContent = "点击中间按钮即可语音输入，浏览器会把你说的话转成文字。";
}

function toggleMic() {
  if (!state.recognition) {
    $("#chatInput").focus();
    return;
  }
  if (state.speechUnavailable) {
    $("#chatInput").focus();
    showToast("浏览器语音识别服务暂不可用，已切到文字/系统听写输入。");
    return;
  }
  if (state.listening) {
    state.recognition.stop();
  } else {
    $("#transcriptText").textContent = "正在听...";
    try {
      state.recognition.start();
    } catch (error) {
      $("#speechSupport").textContent = "语音识别启动失败，可以先用文字输入。";
      $("#chatInput").focus();
    }
  }
}

async function checkMicrophonePermission() {
  if (!navigator.permissions?.query) {
    $("#micStatus").textContent = "麦克风权限：浏览器未提供查询";
    return;
  }
  try {
    const status = await navigator.permissions.query({ name: "microphone" });
    renderMicPermission(status.state);
    status.onchange = () => renderMicPermission(status.state);
  } catch {
    $("#micStatus").textContent = "麦克风权限：无法查询";
  }
}

function renderMicPermission(permissionState) {
  const text = {
    granted: "麦克风权限：已允许",
    denied: "麦克风权限：已拒绝，请在地址栏图标里打开",
    prompt: "麦克风权限：点击说话时请求",
  }[permissionState] || `麦克风权限：${permissionState}`;
  $("#micStatus").textContent = text;
}

function speechErrorMessage(error) {
  const messages = {
    network: "语音输入失败：浏览器语音识别服务连接不上。麦克风可能没问题，但 Web Speech 云服务不可达。",
    "not-allowed": "语音输入失败：麦克风权限被拒绝。请点地址栏麦克风图标允许后重试。",
    "service-not-allowed": "语音输入失败：当前浏览器不允许使用语音识别服务。",
    "no-speech": "没有听到声音，请靠近麦克风再试一次。",
    "audio-capture": "没有检测到可用麦克风，请检查系统输入设备。",
    aborted: "语音输入已停止。",
  };
  return messages[error] || `语音输入失败：${error}`;
}

function retrySpeech() {
  state.speechUnavailable = false;
  $("#micButton").classList.remove("speech-warning");
  $("#speechSupport").textContent = "点击中间按钮即可语音输入，浏览器会把你说的话转成文字。";
  $("#micHint").textContent = "浏览器会请求麦克风权限";
  toggleMic();
}

function focusDictation() {
  $("#chatInput").focus();
  $("#speechSupport").textContent = "已聚焦文字框。Mac 可按 Fn 两下或使用系统听写，把听写结果输入到这里再发送。";
  showToast("已切到系统听写/文字输入。");
}

async function loadMemories() {
  const data = await api("/api/memories");
  const list = $("#memoryList");
  list.innerHTML = data.items.length ? "" : "<p class='empty-text'>还没有记忆。试试说：记住我喜欢青绿色。</p>";
  for (const item of data.items) {
    const div = document.createElement("div");
    div.className = "item";
    const type = memoryType(item);
    div.innerHTML = `
      <div class="item-head">
        <div>
          <strong>${escapeHtml(type)} · #${item.id}</strong>
          <p>${escapeHtml(memoryDisplayText(item))}</p>
          <small>${escapeHtml(item.tags || "无标签")} · ${escapeHtml(item.created_at)}</small>
        </div>
        <button class="link-button" data-delete-memory="${item.id}">删除</button>
      </div>
    `;
    list.appendChild(div);
  }
}

function memoryType(item) {
  const tags = item.tags || "";
  const content = item.content || "";
  if (tags.includes("name")) return "称呼";
  if (content.startsWith("称呼：")) return "称呼";
  if (tags.includes("preference")) return "偏好";
  if (content.startsWith("偏好：")) return "偏好";
  if (item.kind === "wish_progress") return "心愿进展";
  if (item.kind === "note") return "手动记忆";
  return "自动记忆";
}

function memoryDisplayText(item) {
  const content = item.content || "";
  if (content.startsWith("我喜欢")) return `偏好：喜欢${content.slice(3)}`;
  if (content.startsWith("我不喜欢")) return `偏好：不喜欢${content.slice(4)}`;
  return content;
}

async function loadConversations() {
  const data = await api("/api/conversations");
  const list = $("#conversationList");
  list.innerHTML = data.items.length ? "" : "<p class='empty-text'>还没有会话记录。</p>";
  for (const item of data.items.slice(0, 8)) {
    const div = document.createElement("div");
    div.className = "item conversation-item";
    div.innerHTML = `
      <strong>${escapeHtml(item.source)} · #${item.id}</strong>
      <p>你：${escapeHtml(item.user_text)}</p>
      <p>Moss：${escapeHtml(item.assistant_text)}</p>
      <small>命中记忆：${escapeHtml(item.memory_ids || "无")} · ${escapeHtml(item.created_at)}</small>
    `;
    list.appendChild(div);
  }
}

async function saveMemory(event) {
  event.preventDefault();
  const content = $("#memoryContent").value.trim();
  if (!content) return;
  await api("/api/memories", {
    method: "POST",
    body: JSON.stringify({ content, tags: "manual", kind: "note" }),
  });
  $("#memoryContent").value = "";
  showToast("记忆已保存到本地");
  await loadMemories();
}

async function deleteMemory(id) {
  await fetch(`/api/memories/${id}`, { method: "DELETE" });
  showToast("记忆已删除");
  await loadMemories();
}

async function loadCommands() {
  const data = await api("/api/commands");
  state.commands = data.items;
  const select = $("#commandSelect");
  select.innerHTML = "";
  for (const command of state.commands) {
    const option = document.createElement("option");
    option.value = command.id;
    option.textContent = `${command.name} · ${command.level}`;
    select.appendChild(option);
  }
  renderCommandFields();
}

function renderCommandFields() {
  const selected = state.commands.find((item) => item.id === $("#commandSelect").value);
  const box = $("#commandFields");
  box.innerHTML = "";
  for (const field of selected?.fields || []) {
    const label = document.createElement("label");
    label.textContent = field.label;
    const input = document.createElement("input");
    input.name = field.name;
    input.placeholder = field.placeholder || "";
    label.appendChild(input);
    box.appendChild(label);
  }
  $("#commandConfirm").checked = false;
  $("#commandResult").textContent = selected ? selected.warning : "等待指令...";
}

function commandPayload() {
  const action = $("#commandSelect").value;
  const args = {};
  for (const input of $("#commandFields").querySelectorAll("input")) {
    args[input.name] = input.value;
  }
  return { action, args };
}

async function previewCommand() {
  const data = await api("/api/commands/preview", {
    method: "POST",
    body: JSON.stringify(commandPayload()),
  });
  $("#commandResult").textContent = JSON.stringify(data, null, 2);
}

async function runCommand(event) {
  event.preventDefault();
  const payload = commandPayload();
  payload.confirm = $("#commandConfirm").checked;
  try {
    const data = await api("/api/commands/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    $("#commandResult").textContent = JSON.stringify(data, null, 2);
    showToast(data.message || "指令已执行");
    $("#commandConfirm").checked = false;
  } catch (error) {
    $("#commandResult").textContent = `错误：${error.message}`;
  }
}

function syncTtsRangeValues() {
  $("#ttsSpeedValue").textContent = Number($("#ttsSpeed").value).toFixed(1);
  $("#ttsVolumeValue").textContent = Number($("#ttsVolume").value).toFixed(1);
  $("#ttsPitchValue").textContent = $("#ttsPitch").value;
}

async function synthesizeTts(event) {
  event.preventDefault();
  const text = $("#ttsText").value.trim();
  if (!text) {
    showToast("先输入要朗读的文本");
    return;
  }
  const button = $("#ttsButton");
  const apiKey = $("#ttsApiKey").value.trim() || $("#sessionApiKey").value.trim();
  if (!state.ttsConfigured && !apiKey) {
    $("#ttsResult").textContent = "请先填写页面上方或这里的临时 MiniMax Key，或在本机 .env 里配置 MINIMAX_API_KEY。";
    showToast("需要 MiniMax Key 才能生成语音");
    return;
  }
  const audio = $("#ttsAudio");
  button.disabled = true;
  button.textContent = "生成中...";
  $("#ttsResult").textContent = "正在请求 MiniMax 语音合成...";
  audio.classList.remove("ready");
  try {
    const data = await api("/api/tts/synthesize", {
      method: "POST",
      body: JSON.stringify({
        text,
        api_key: apiKey,
        model: $("#ttsModel").value,
        voice_id: $("#ttsVoice").value,
        speed: Number($("#ttsSpeed").value),
        volume: Number($("#ttsVolume").value),
        pitch: Number($("#ttsPitch").value),
      }),
    });
    audio.src = `${data.audio_url}?t=${Date.now()}`;
    audio.classList.add("ready");
    await audio.play().catch(() => {});
    $("#ttsResult").textContent = JSON.stringify(
      {
        ok: data.ok,
        provider: data.provider,
        model: data.model,
        voice_id: data.voice_id,
        audio_url: data.audio_url,
      },
      null,
      2,
    );
    $("#ttsApiKey").value = "";
    $("#speakStatus").textContent = `MiniMax 人声已生成：${data.voice_id}`;
    showToast("语音已生成");
  } catch (error) {
    $("#ttsResult").textContent = `错误：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "朗读回复";
  }
}

async function exportData() {
  const data = await api("/api/export");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `moss-export-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

function clearChat() {
  $("#chatLog").innerHTML = "";
  $("#lastUserText").textContent = "等待你说话。";
  $("#lastAssistantText").textContent = "连接后会显示回复来源和内容。";
  $("#transcriptText").textContent = "还没有语音输入。也可以在下面直接打字测试。";
  renderMemoryHits([]);
}

document.addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("[data-delete-memory]");
  if (deleteButton) {
    await deleteMemory(deleteButton.dataset.deleteMemory);
  }
});

$("#chatForm").addEventListener("submit", sendChat);
$("#memoryForm").addEventListener("submit", saveMemory);
$("#commandSelect").addEventListener("change", renderCommandFields);
$("#previewCommand").addEventListener("click", previewCommand);
$("#commandForm").addEventListener("submit", runCommand);
$("#exportData").addEventListener("click", exportData);
$("#ttsForm").addEventListener("submit", synthesizeTts);
$("#micButton").addEventListener("click", toggleMic);
$("#clearChat").addEventListener("click", clearChat);
$("#retrySpeech").addEventListener("click", retrySpeech);
$("#focusDictation").addEventListener("click", focusDictation);
for (const id of ["#ttsSpeed", "#ttsVolume", "#ttsPitch"]) {
  $(id).addEventListener("input", syncTtsRangeValues);
}

async function init() {
  addMessage("assistant", "你好，我是 Moss。你可以点击说话，或先打字测试：记住我喜欢青绿色。", "local");
  renderMemoryHits([]);
  setupSpeechRecognition();
  syncTtsRangeValues();
  await loadStatus();
  await Promise.all([loadCommands(), loadMemories(), loadConversations()]);
}

init().catch((error) => {
  $("#statusTitle").textContent = "连接失败";
  $("#statusDetail").textContent = error.message;
});
