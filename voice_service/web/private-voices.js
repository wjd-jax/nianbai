"use strict";

window.setupPrivateVoiceDialog = ({ request, post, refreshVoices, beforeOpen }) => {
  const get = (id) => document.getElementById(id);
  const dialog = get("private-voice-dialog");
  const record = get("record-toggle");
  const recordPlayback = get("record-playback");
  const candidatePlayback = get("candidate-playback");
  const name = get("private-voice-name");
  const context = { options: null, mode: "record", working: false, startingMic: false };
  let stream;
  let recorder;
  let recording;
  let recordingUrl;
  let candidateUrl;
  let candidateId;
  let started;
  let timer;
  let micCycle = 0;
  let taskCycle = 0;

  function status(message) {
    get("candidate-status").textContent = message;
    get("candidate-status").hidden = !message;
  }

  function showError(message) {
    get("candidate-error").textContent = message;
    get("candidate-error").hidden = !message;
  }

  function resetAudio(audio, url) {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    if (url) URL.revokeObjectURL(url);
  }

  function clearRecording() {
    resetAudio(recordPlayback, recordingUrl);
    recordingUrl = undefined;
    recording = undefined;
    recordPlayback.hidden = true;
  }

  function clearCandidate() {
    resetAudio(candidatePlayback, candidateUrl);
    candidateUrl = undefined;
    candidateId = undefined;
    get("candidate-result").hidden = true;
  }

  function refreshControls() {
    const active = recorder?.state === "recording";
    get("create-record-candidate").disabled = context.working || !recording || active;
    get("create-design-candidate").disabled = context.working;
    get("create-random-candidate").disabled = context.working;
    get("record-mode").disabled = context.working;
    get("design-mode").disabled = context.working;
    get("save-private-voice").disabled = context.working;
    record.disabled = context.working || context.startingMic;
    record.textContent = active ? "停止录音" : recording ? "重新录音" : "开始录音";
    record.setAttribute("aria-pressed", String(Boolean(active)));
  }

  function setMode(mode) {
    if (mode !== context.mode) {
      resetMic();
      clearRecording();
      get("record-time").textContent = "0:00";
    }
    context.mode = mode;
    get("record-pane").hidden = mode !== "record";
    get("design-pane").hidden = mode !== "design";
    get("record-mode").setAttribute("aria-pressed", String(mode === "record"));
    get("design-mode").setAttribute("aria-pressed", String(mode === "design"));
    clearCandidate();
    status("");
    showError("");
    refreshControls();
  }

  function stopTracks() {
    if (stream) for (const track of stream.getTracks()) track.stop();
    stream = undefined;
  }

  function resetMic() {
    micCycle += 1;
    context.startingMic = false;
    clearInterval(timer);
    if (recorder?.state === "recording") recorder.stop();
    recorder = undefined;
    stopTracks();
  }

  function updateTime() {
    const seconds = Math.min(context.options.max_record_seconds, Math.floor((performance.now() - started) / 1000));
    get("record-time").textContent = "0:" + String(seconds).padStart(2, "0");
    if (seconds >= context.options.max_record_seconds && recorder?.state === "recording") recorder.stop();
  }

  async function startMic() {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      showError("当前浏览器无法录音，请使用支持麦克风录制的浏览器。");
      return;
    }
    const mimeType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"]
      .find((mime) => MediaRecorder.isTypeSupported(mime));
    if (!mimeType) {
      showError("当前浏览器的录音格式不可用。");
      return;
    }
    const attempt = ++micCycle;
    context.startingMic = true;
    showError("");
    refreshControls();
    try {
      const audio = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (attempt !== micCycle) {
        for (const track of audio.getTracks()) track.stop();
        return;
      }
      stream = audio;
      clearCandidate();
      clearRecording();
      const chunks = [];
      recorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 64000 });
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => {
        if (attempt !== micCycle) return;
        clearInterval(timer);
        stopTracks();
        const blob = new Blob(chunks, { type: recorder.mimeType });
        if (performance.now() - started >= 3000 && blob.size) {
          recording = blob;
          recordingUrl = URL.createObjectURL(recording);
          recordPlayback.src = recordingUrl;
          recordPlayback.hidden = false;
        } else {
          showError("录音至少需要 3 秒，请完整读一遍台词。");
        }
        refreshControls();
      }, { once: true });
      recorder.addEventListener("error", () => {
        if (attempt !== micCycle) return;
        resetMic();
        showError("录音中断，请重试。");
        refreshControls();
      }, { once: true });
      started = performance.now();
      recorder.start();
      updateTime();
      timer = setInterval(updateTime, 200);
    } catch (error) {
      if (attempt === micCycle) {
        stopTracks();
        showError(error.name === "NotAllowedError" ? "请允许麦克风权限后再录音。" : "无法使用麦克风，请检查设备后重试。");
      }
    } finally {
      if (attempt === micCycle) {
        context.startingMic = false;
        refreshControls();
      }
    }
  }

  record.addEventListener("click", () => {
    if (recorder?.state === "recording") recorder.stop();
    else startMic();
  });

  function encodedRecording() {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("录音无法读取，请重试。"));
      reader.onload = () => resolve(reader.result.split(",", 2)[1]);
      reader.readAsDataURL(recording);
    });
  }

  async function generate(mode) {
    const attempt = ++taskCycle;
    context.working = true;
    refreshControls();
    recordPlayback.pause();
    clearCandidate();
    showError("");
    status("正在提交音色试听…");
    try {
      const payload = { mode };
      if (mode === "record") {
        payload.media_type = recording.type;
        payload.recording = await encodedRecording();
      } else if (mode === "design") {
        payload.description = get("voice-description").value.trim();
      }
      if (attempt !== taskCycle) return;
      const queued = await (await post("/web/voice-candidates", payload)).json();
      while (attempt === taskCycle) {
        const job = await (await request(queued.status_url)).json();
        if (attempt !== taskCycle) return;
        if (job.status === "failed") throw new Error(job.error.message);
        if (job.status === "succeeded") {
          const clip = await (await request(job.audio_url)).blob();
          if (attempt !== taskCycle) return;
          candidateId = job.id;
          candidateUrl = URL.createObjectURL(clip);
          candidatePlayback.src = candidateUrl;
          const description = get("candidate-description");
          description.textContent = job.candidate.description;
          description.hidden = job.candidate.source !== "random" || !job.candidate.description;
          name.value = { record: "我的声音", design: "定制音色", random: "随机音色" }[mode];
          get("candidate-result").hidden = false;
          status("");
          return;
        }
        status(job.status === "running" ? "正在生成音色试听…" : `正在排队，等待位置 ${job.queue_position}。`);
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    } catch (error) {
      if (attempt === taskCycle) {
        status("");
        showError(error.message);
      }
    } finally {
      if (attempt === taskCycle) {
        context.working = false;
        refreshControls();
      }
    }
  }

  get("record-mode").addEventListener("click", () => setMode("record"));
  get("design-mode").addEventListener("click", () => setMode("design"));
  get("create-record-candidate").addEventListener("click", () => generate("record"));
  get("create-design-candidate").addEventListener("click", () => generate("design"));
  get("create-random-candidate").addEventListener("click", () => generate("random"));
  get("close-private-voice").addEventListener("click", () => dialog.close());

  get("save-voice-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!candidateId || context.working) return;
    context.working = true;
    refreshControls();
    showError("");
    try {
      const saved = await (await post("/web/voice-candidates/" + candidateId + "/save", { name: name.value.trim() })).json();
      await refreshVoices(saved.id);
      dialog.close();
    } catch (error) {
      showError(error.message);
    } finally {
      context.working = false;
      refreshControls();
    }
  });

  dialog.addEventListener("close", () => {
    taskCycle += 1;
    context.working = false;
    resetMic();
    clearRecording();
    clearCandidate();
    get("record-time").textContent = "0:00";
    get("voice-description").value = "";
  });

  get("create-voice").addEventListener("click", () => {
    if (!context.options) return;
    beforeOpen();
    setMode("record");
    dialog.showModal();
  });

  return {
    configure(options) {
      context.options = options;
      get("create-voice").hidden = !options;
      if (!options) return;
      get("record-script").textContent = options.record_script;
      get("voice-description").maxLength = options.description_limit;
      name.maxLength = options.name_limit;
    },
    close() {
      if (dialog.open) dialog.close();
    },
  };
};
