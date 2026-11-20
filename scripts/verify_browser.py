"""Run the actual invite, playback and download flow in the selected Playwright CLI session."""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = os.environ.get("PLAYWRIGHT_CLI")
PLAYWRIGHT = [RUNNER] if RUNNER else ["npx", "--yes", "--package", "@playwright/cli", "playwright-cli"]

BROWSER_CHECK = """
async (page) => {
  await page.context().clearCookies();
  await page.evaluate(() => sessionStorage.removeItem("voice-last-job"));
  await page.reload();
  await page.setViewportSize({ width: 1280, height: 800 });
  const inviteBox = page.getByLabel("邀请码", { exact: true });
  const enter = page.getByRole("button", { name: "进入工作台" });
  await inviteBox.waitFor();
  await inviteBox.fill("invalid");
  await enter.click();
  await page.getByRole("alert").filter({ hasText: "邀请码无效" }).waitFor();
  const fixture = await fetch(__FIXTURE__, { method: "POST" });
  if (!fixture.ok) throw new Error("Local invite fixture unavailable");
  await inviteBox.fill(await fixture.text());
  await enter.click();
  await page.getByRole("heading", { name: "文字配音。" }).waitFor();
  const voice = page.getByRole("combobox", { name: "选择音色" });
  if (await voice.inputValue() !== "serena" || await voice.locator('optgroup[label="内置音色"] option').count() !== 9) {
    throw new Error("Expected nine voices with Serena selected by default");
  }
  const input = page.getByLabel("配音文稿");
  const generate = page.getByRole("button", { name: "生成配音" });
  const preview = page.locator("#voice-preview-player");
  const previewButton = page.getByRole("button", { name: "试听", exact: true });
  const studio = await (await page.request.get(new URL("/web/config", page.url()).href)).json();
  const retainedText = "试听时保留编辑中的文稿。";
  let synthesisRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/web/jobs")) synthesisRequests++;
  });
  await input.fill(retainedText);
  await previewButton.click();
  await page.waitForFunction(() => document.querySelector("#voice-preview-player").currentTime > 0.4);
  if (!((await preview.getAttribute("src") ?? "").includes("/serena/preview"))) {
    throw new Error("Serena preview was not selected");
  }
  await voice.selectOption("vivian");
  if (!await preview.evaluate((audio) => audio.paused) || await input.inputValue() !== retainedText) {
    throw new Error("Switching voice stopped editing or did not stop the previous preview");
  }
  await previewButton.click();
  await page.waitForFunction(() => document.querySelector("#voice-preview-player").currentTime > 0.4);
  if (!((await preview.getAttribute("src") ?? "").includes("/vivian/preview")) || synthesisRequests !== 0) {
    throw new Error("Vivian preview was not selected, or preview submitted synthesis");
  }
  await voice.selectOption("serena");
  await input.fill("再".repeat(studio.max_characters + 1));
  if (!await generate.isDisabled() || await input.getAttribute("aria-invalid") !== "true") {
    throw new Error("Browser length check did not reject one character over the limit");
  }
  await input.fill("你好，我是 Serena。今天来试听一段由配音工作台生成的声音。");
  const audioResponse = page.waitForResponse((response) => /\\/web\\/jobs\\/[a-f0-9]+\\/audio$/.test(response.url()), { timeout: 120000 });
  await generate.click();
  const response = await audioResponse;
  if (!response.ok()) throw new Error("The authorized MP3 request failed");
  const expected = await page.evaluate(async (url) => {
    const audio = await fetch(url);
    const bytes = await audio.arrayBuffer();
    if (!audio.ok || bytes.byteLength < 1000) throw new Error("Invalid authorized audio response");
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }, response.url());
  const player = page.locator("#player");
  await page.getByText("配音已生成", { exact: true }).waitFor();
  await player.evaluate((audio) => audio.play());
  await page.waitForFunction(() => document.querySelector("#player").currentTime > 0.5);
  const played = await player.evaluate((audio) => {
    const position = audio.currentTime;
    audio.pause();
    return { position, decoded: audio.readyState >= 2, duration: audio.duration };
  });
  if (!played.decoded || !Number.isFinite(played.duration)) throw new Error("Audio did not decode");
  const pending = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 MP3" }).click();
  const download = await pending;
  await download.saveAs(__DOWNLOAD__);
  await page.screenshot({ path: __DESKTOP__ });
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
    throw new Error("Mobile page overflows the viewport");
  }
  await page.screenshot({ path: __MOBILE__, fullPage: true });
  await page.reload();
  await page.getByRole("link", { name: "下载 MP3" }).waitFor();
  await page.getByRole("button", { name: "退出" }).click();
  await inviteBox.waitFor();
  if (await page.locator("#player").getAttribute("src")) throw new Error("Audio still available on the page after logout");
  const unauthenticated = await page.request.get(response.url());
  if (unauthenticated.status() !== 401) throw new Error("The original audio URL still accepted after logout");
  return { previews: ["serena", "vivian"], played, downloaded: download.suggestedFilename(), sha256: expected };
}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".runtime/access.json")
    args = parser.parse_args()
    invite = json.loads(args.credentials.read_text())["web"]["owner"].encode()
    path = "/" + secrets.token_urlsafe(24)
    used = False
    mic = (ROOT / ".runtime/private-voice-lab/fake-mic.wav").read_bytes() if args.private else b""

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal used
            if mic and self.path == path + "/microphone":
                body = mic
            elif not used and self.path == path:
                used = True
                body = invite
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    clip_label = "-browser-private.mp3" if args.private else "-browser-serena.mp3"
    download = ROOT / "output/verification" / (args.label + clip_label)
    download.parent.mkdir(exist_ok=True, parents=True)
    (ROOT / "output/playwright").mkdir(exist_ok=True, parents=True)
    desktop = ROOT / "output/playwright" / (args.label + "-desktop.png")
    mobile = ROOT / "output/playwright" / (args.label + "-mobile.png")
    with HTTPServer(("127.0.0.1", 0), FixtureHandler) as fixture:
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        code = (ROOT / "scripts/verify_private_browser.js").read_text() if args.private else BROWSER_CHECK
        for marker, value in {
            "__FIXTURE__": f"http://127.0.0.1:{fixture.server_port}{path}",
            "__DOWNLOAD__": str(download),
            "__DESKTOP__": str(desktop),
            "__MOBILE__": str(mobile),
            "__VOICE_PREFIX__": args.label,
        }.items():
            code = code.replace(marker, json.dumps(value, ensure_ascii=False))
        try:
            result = subprocess.run(
                [*PLAYWRIGHT, "--session", args.session, "run-code", code],
                cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        finally:
            fixture.shutdown()
            thread.join()

    # The CLI repeats the source snippet. Keep the result and discard the one-use fixture address.
    if result.returncode:
        diagnostic = result.stdout.split("### Ran Playwright code", 1)[0].replace(path, "/<one-use-fixture>")
        raise RuntimeError(diagnostic.strip() or "Playwright browser check failed")
    summary = result.stdout.split("### Result\n", 1)
    if len(summary) != 2:
        raise RuntimeError("Browser validation did not return a result: " + result.stdout.split("### Ran Playwright code", 1)[0])
    evidence = summary[1].split("###", 1)[0].strip()
    sha = hashlib.sha256(download.read_bytes()).hexdigest()
    assert sha in evidence, (sha, evidence)
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(download), "-f", "null", "-"], check=True)
    (ROOT / "output/verification" / (args.label + "-browser-report.txt")).write_text(evidence + "\n")
    print(evidence)


if __name__ == "__main__":
    main()
