async (page) => {
  // Feed a synthetic reference through a real browser MediaStream and MediaRecorder.
  const microphone = await fetch(__FIXTURE__ + "/microphone", { method: "POST" });
  const wav = Buffer.from(await microphone.arrayBuffer());
  if (!microphone.ok || wav.length < 1000) throw new Error("Test microphone sample missing");
  await page.route("**/__voice-test-microphone.wav", (route) => route.fulfill({
    contentType: "audio/wav", body: wav,
  }));
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      await context.resume();
      const response = await fetch("/__voice-test-microphone.wav");
      const sample = await context.decodeAudioData(await response.arrayBuffer());
      const player = context.createBufferSource();
      player.buffer = sample;
      const capture = context.createMediaStreamDestination();
      player.connect(capture);
      player.start();
      for (const track of capture.stream.getAudioTracks()) {
        const stop = track.stop.bind(track);
        track.stop = () => { stop(); void context.close(); };
      }
      return capture.stream;
    };
  });
  await page.context().clearCookies();
  await page.evaluate(() => sessionStorage.removeItem("voice-last-job"));
  await page.reload();
  await page.setViewportSize({ width: 1280, height: 850 });
  const fixture = await fetch(__FIXTURE__, { method: "POST" });
  if (!fixture.ok) throw new Error("Local invite fixture unavailable");
  await page.getByLabel("邀请码", { exact: true }).fill(await fixture.text());
  await page.getByRole("button", { name: "进入工作台" }).click();
  const create = page.getByRole("button", { name: "创建音色" });
  const dialog = page.getByRole("dialog", { name: "创建私人音色" });
  const select = page.getByRole("combobox", { name: "选择音色" });
  const filledText = "创建音色时保留这段文稿。";
  await page.getByLabel("配音文稿").fill(filledText);
  const results = {};

  async function play(selector) {
    const media = page.locator(selector);
    await media.evaluate((audio) => audio.play());
    await page.waitForFunction((selector) => document.querySelector(selector).currentTime > .4, selector);
    return await media.evaluate((audio) => {
      const result = { time: audio.currentTime, duration: audio.duration, ready: audio.readyState >= 2 };
      audio.pause();
      return result;
    });
  }

  async function waitCandidate() {
    const error = dialog.locator("#candidate-error");
    await Promise.race([
      dialog.getByRole("heading", { name: "试听候选音色" }).waitFor({ timeout: 140000 }),
      error.waitFor({ timeout: 140000 }).then(async () => { throw new Error(await error.textContent()); }),
    ]);
  }

  async function save(kind) {
    const name = `${__VOICE_PREFIX__}-${kind}`;
    await dialog.getByLabel("音色名称").fill(name);
    const request = page.waitForResponse((response) => response.url().endsWith("/save"));
    await dialog.getByRole("button", { name: "保存音色" }).click();
    const reply = await request;
    if (!reply.ok()) throw new Error("Could not save private candidate");
    const data = await reply.json();
    await dialog.waitFor({ state: "hidden" });
    if (await select.inputValue() !== data.id) throw new Error("Saved voice was not selected");
    return data.id;
  }

  await create.click();
  await dialog.getByRole("button", { name: "声音定制" }).click();
  await dialog.getByLabel("你想要怎样的声音？").fill("一位沉稳、温暖的普通话女声，语速平稳，像在做晚间电台节目。");
  await dialog.getByRole("button", { name: "生成定制试听" }).click();
  await waitCandidate();
  results.design = { preview: await play("#candidate-playback") };
  await page.screenshot({ path: __DESKTOP__ });
  results.design.id = await save("design");

  await create.click();
  await dialog.getByRole("button", { name: "声音定制" }).click();
  await dialog.getByRole("button", { name: "随机设计", exact: true }).click();
  await waitCandidate();
  const description = await dialog.locator("#candidate-description").textContent();
  if (!description.trim()) throw new Error("No random description");
  results.random = { preview: await play("#candidate-playback"), description };
  results.random.id = await save("random");

  await create.click();
  await page.setViewportSize({ width: 390, height: 844 });
  if (await dialog.evaluate((element) => element.scrollWidth > element.clientWidth + 1)) {
    throw new Error("Voice recording dialog overflows mobile layout");
  }
  await dialog.getByRole("button", { name: "开始录音" }).click();
  const stop = dialog.getByRole("button", { name: "停止录音" });
  await stop.waitFor();
  await page.waitForTimeout(11900);
  await stop.click();
  await dialog.getByLabel("回听录音").waitFor();
  results.record = { recording: await play("#record-playback") };
  await page.screenshot({ path: __MOBILE__ });
  await dialog.getByRole("button", { name: "生成克隆试听" }).click();
  await waitCandidate();
  results.record.preview = await play("#candidate-playback");
  results.record.id = await save("record");

  if (await page.getByLabel("配音文稿").inputValue() !== filledText) throw new Error("Editing lost during voice creation");
  await page.reload();
  await select.waitFor();
  for (const item of Object.values(results)) {
    if (await select.locator(`option[value="${item.id}"]`).count() !== 1) {
      throw new Error("Saved voice was not retained");
    }
  }
  await select.selectOption(results.record.id);
  await page.getByRole("button", { name: "试听", exact: true }).click();
  results.savedPreview = await play("#voice-preview-player");
  await page.getByLabel("配音文稿").fill("这是新写的一句，用我保存的音色读出来。");
  const audioResponse = page.waitForResponse((response) => /\/web\/jobs\/[a-f0-9]+\/audio$/.test(response.url()), { timeout: 140000 });
  await page.getByRole("button", { name: "生成配音" }).click();
  const response = await audioResponse;
  if (!response.ok()) throw new Error("Private voice synthesis did not return audio");
  results.synthesis = await play("#player");
  const pending = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 MP3" }).click();
  await (await pending).saveAs(__DOWNLOAD__);
  results.sha256 = await page.evaluate(async (url) => {
    const response = await fetch(url);
    const bytes = await response.arrayBuffer();
    const result = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(result), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }, response.url());
  return results;
}
