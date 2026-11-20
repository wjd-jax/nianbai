"use strict";

const byId = (id) => document.getElementById(id);
const accessView = byId("access-view");
const studio = byId("studio");
const input = byId("script-input");
const player = byId("player");
const voiceSelect = byId("voice-select");
const previewPlayer = byId("voice-preview-player");
const previewButton = byId("voice-preview-button");
const generate = byId("generate");
const statusLine = byId("job-status");
const lastJob = "voice-last-job";
const idleMessage = "生成后可在这里试听或下载。";
let config;
let busy = false;
let resultUrl;
let cycle = 0;
let previewCycle = 0;

async function request(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.error?.message ?? "连接暂时不可用，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return response;
}

function jsonPost(path, data) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

function showError(id, message) {
  const element = byId(id);
  element.textContent = message;
  element.hidden = !message;
}

function refreshInput() {
  const value = input.value.trim();
  const length = Array.from(value).length;
  const over = config && length > config.max_characters;
  const count = byId("count");
  count.textContent = config ? `${length} / ${config.max_characters}` : "";
  count.classList.toggle("over", Boolean(over));
  input.setAttribute("aria-invalid", String(Boolean(over)));
  generate.disabled = !config || busy || !length || over;
}

function clearAudio() {
  player.pause();
  player.removeAttribute("src");
  player.load();
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  resultUrl = undefined;
  byId("audio-result").hidden = true;
  byId("duration").textContent = "";
}

function setPreviewPlaying(playing) {
  previewButton.setAttribute("aria-pressed", String(playing));
  byId("voice-preview-label").textContent = playing ? "停止" : "试听";
  byId("voice-preview-icon").textContent = playing ? "■" : "▶";
}

function stopPreview() {
  previewCycle += 1;
  previewPlayer.pause();
  previewPlayer.removeAttribute("src");
  previewPlayer.load();
  setPreviewPlaying(false);
}

previewButton.addEventListener("click", async () => {
  if (previewButton.getAttribute("aria-pressed") === "true") {
    stopPreview();
    return;
  }
  const attempt = ++previewCycle;
  showError("voice-preview-error", "");
  player.pause();
  previewPlayer.src = "/web/voices/" + encodeURIComponent(voiceSelect.value) + "/preview";
  setPreviewPlaying(true);
  try {
    await previewPlayer.play();
  } catch (error) {
    if (attempt !== previewCycle) return;
    setPreviewPlaying(false);
    showError("voice-preview-error", "试听暂时无法播放，请重试。");
  }
});

voiceSelect.addEventListener("change", () => {
  stopPreview();
  showError("voice-preview-error", "");
  refreshVoiceActions();
});
previewPlayer.addEventListener("ended", () => setPreviewPlaying(false));
player.addEventListener("play", stopPreview);

function showAccess() {
  privateDialog.close();
  cycle += 1;
  busy = false;
  clearAudio();
  stopPreview();
  showError("voice-preview-error", "");
  studio.hidden = true;
  accessView.hidden = false;
  byId("sign-out").hidden = true;
  byId("sign-out").disabled = false;
}

function refreshVoiceActions() {
  const selected = config?.voices.find((voice) => voice.id === voiceSelect.value);
  byId("delete-voice").hidden = selected?.kind !== "private";
}

function renderVoiceOptions(data, selected) {
  config = data;
  const presets = document.createElement("optgroup");
  presets.label = "内置音色";
  const personal = document.createElement("optgroup");
  personal.label = "私人音色";
  for (const voice of data.voices) {
    const option = document.createElement("option");
    option.value = voice.id;
    option.textContent = `${voice.name} · ${voice.description}`;
    (voice.kind === "private" ? personal : presets).append(option);
  }
  voiceSelect.replaceChildren(presets);
  if (personal.children.length) voiceSelect.append(personal);
  voiceSelect.value = data.voices.some((voice) => voice.id === selected) ? selected : data.default_voice;
  privateDialog.configure(data.private_voices);
  refreshVoiceActions();
}

async function reloadVoices(selected) {
  stopPreview();
  const data = await (await request("/web/config")).json();
  renderVoiceOptions(data, selected);
}

function showStudio(data) {
  renderVoiceOptions(data, data.default_voice);
  accessView.hidden = true;
  studio.hidden = false;
  byId("sign-out").hidden = false;
  refreshInput();
  const saved = sessionStorage.getItem(lastJob);
  if (saved && /^[a-f0-9]{32}$/.test(saved)) track(saved);
}

async function boot() {
  try {
    showStudio(await (await request("/web/config")).json());
  } catch (error) {
    if (error.status === 401) showAccess();
    else showError("boot-error", `${error.message} 请刷新页面。`);
  }
}

byId("invite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("invite-error", "");
  const button = byId("invite-submit");
  button.disabled = true;
  try {
    await jsonPost("/web/session", { invite: byId("invite").value });
    byId("invite").value = "";
    await boot();
    if (!studio.hidden) input.focus();
  } catch (error) {
    showError("invite-error", error.message);
  } finally {
    button.disabled = false;
  }
});

byId("sign-out").addEventListener("click", async () => {
  byId("sign-out").disabled = true;
  try {
    await request("/web/session", { method: "DELETE" });
    sessionStorage.removeItem(lastJob);
    input.value = "";
    showError("speech-error", "");
    statusLine.textContent = idleMessage;
    showAccess();
    byId("invite").focus();
  } catch (error) {
    showError("speech-error", error.message);
    byId("sign-out").disabled = false;
  }
});

function setWorking(value) {
  busy = value;
  refreshInput();
  byId("speech-form").setAttribute("aria-busy", String(value));
}

async function track(id) {
  const attempt = ++cycle;
  setWorking(true);
  const path = "/web/jobs/" + id;
  try {
    while (attempt === cycle) {
      const job = await (await request(path)).json();
      if (attempt !== cycle) return;
      if (job.status === "succeeded") {
        const response = await request(job.audio_url);
        const blob = await response.blob();
        if (attempt !== cycle) return;
        resultUrl = URL.createObjectURL(blob);
        player.src = resultUrl;
        const download = byId("download");
        download.href = resultUrl;
        download.download = `${job.voice}-${job.id.slice(0, 8)}.mp3`;
        byId("duration").textContent = `${job.duration_seconds.toFixed(1)} 秒`;
        statusLine.textContent = "配音已生成";
        byId("audio-result").hidden = false;
        return;
      }
      if (job.status === "failed") throw new Error(job.error.message);
      statusLine.textContent = job.status === "running" ? "正在合成语音…" : `正在排队，等待位置 ${job.queue_position}。`;
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  } catch (error) {
    if (attempt !== cycle) return;
    if (error.status === 401) {
      sessionStorage.removeItem(lastJob);
      showAccess();
      showError("invite-error", "会话已过期，请重新输入邀请码。");
      return;
    }
    if (error.status === 404) sessionStorage.removeItem(lastJob);
    statusLine.textContent = idleMessage;
    showError("speech-error", error.message);
  } finally {
    if (attempt === cycle) setWorking(false);
  }
}

input.addEventListener("input", () => {
  refreshInput();
  showError("speech-error", "");
});
byId("speech-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (generate.disabled) return;
  const attempt = ++cycle;
  setWorking(true);
  showError("speech-error", "");
  try {
    const voice = voiceSelect.value;
    const job = await (await jsonPost("/web/jobs", { input: input.value.trim(), voice })).json();
    if (attempt !== cycle) return;
    clearAudio();
    statusLine.textContent = "已提交配音任务…";
    sessionStorage.setItem(lastJob, job.id);
    await track(job.id);
  } catch (error) {
    if (attempt !== cycle) return;
    if (error.status === 401) {
      sessionStorage.removeItem(lastJob);
      showAccess();
      showError("invite-error", "会话已过期，请重新输入邀请码。");
    } else {
      showError("speech-error", error.message);
    }
  } finally {
    if (attempt === cycle) setWorking(false);
  }
});

byId("delete-voice").addEventListener("click", async () => {
  const voice = config.voices.find((item) => item.id === voiceSelect.value);
  if (voice?.kind !== "private" || !window.confirm(`删除「${voice.name}」及其录音和样音？`)) return;
  byId("delete-voice").disabled = true;
  showError("voice-preview-error", "");
  try {
    await request("/web/voices/" + encodeURIComponent(voice.id), { method: "DELETE" });
    await reloadVoices();
    voiceSelect.focus();
  } catch (error) {
    showError("voice-preview-error", error.message);
  } finally {
    byId("delete-voice").disabled = false;
  }
});

const privateDialog = window.setupPrivateVoiceDialog({
  request,
  post: jsonPost,
  refreshVoices: reloadVoices,
  beforeOpen() {
    stopPreview();
    player.pause();
  },
});

boot();
