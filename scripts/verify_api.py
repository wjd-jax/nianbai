import argparse
import concurrent.futures
import hashlib
import http.cookiejar
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output/verification"
API = "/v1/audio/speech"
LONG_TEXT = (
    "今天我们用一段文稿测试配音工作台。开始前，请先打开需要演示的页面，"
    "把操作步骤按照顺序整理好。介绍功能时，可以先说明使用场景，再演示如何完成任务。"
    "录制屏幕时，鼠标可以略微停留，让观众看清当前选择的按钮。"
    "如果文稿中间出现缩写或英文名称，可以先念一遍，确认语速和停顿。"
    "生成以后请完整试听，特别留意术语、数字以及不同段落之间的衔接。"
    "需要修改的地方，可以调整文稿和标点后再生成。导出音频之后，记得确认配音能正常播放，"
    "并把音量调整到适合视频的水平。最后请记下今天的口令，青色的风铃。"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8796")
    parser.add_argument("--label", default="local")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--voices", action="store_true")
    args = parser.parse_args()
    access = json.loads((ROOT / ".runtime/access.json").read_text())
    invite = access["web"]["owner"]
    auth = {"Authorization": "Bearer " + access["api"]["owner"]}
    default = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    report = []

    def record(message):
        print(message, flush=True)
        report.append(message)

    def request(path, status, payload=None, headers=None, opener=default, method=None):
        headers = {"User-Agent": "AI-Voice-Verification/1.0", **(headers or {})}
        if isinstance(payload, dict):
            payload = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(args.base + path, payload, headers, method=method)
        try:
            response = opener.open(req, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            assert response.status == status, (path, response.status, status, body[:300])
            if response.headers.get_content_type() == "application/json":
                body = json.loads(body)
            return body, response.headers

    def save_audio(path, label, headers=None, opener=default):
        audio, meta = request(path, 200, headers=headers, opener=opener)
        assert meta.get_content_type() == "audio/mpeg" and "attachment" in meta["Content-Disposition"]
        assert "no-store" in meta["Cache-Control"] and len(audio) > 1000
        target = args.output_dir / f"{label}.mp3"
        target.write_bytes(audio)
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
            "-of", "json", str(target),
        ], text=True)
        parsed = json.loads(probe)
        assert parsed["streams"] == [{"codec_name": "mp3", "sample_rate": "24000", "channels": 1}]
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"], check=True)
        record(f"{target.name} {float(parsed['format']['duration']):.2f}s, {len(audio)} bytes, sha256={hashlib.sha256(audio).hexdigest()}")
        return audio

    def wait(job, headers=None, opener=default):
        started = time.monotonic()
        states = []
        while time.monotonic() - started < 360:
            result, _ = request(job["status_url"], 200, headers=headers, opener=opener)
            if not states or states[-1] != result["status"]:
                states.append(result["status"])
            if result["status"] == "succeeded":
                record(f"{job['voice']}: {' → '.join(states)}, observed after {time.monotonic() - started:.1f}s polling")
                return result
            assert result["status"] != "failed", result.get("error")
            time.sleep(1.25)
        raise TimeoutError("Synthesis still pending after 360 s")

    args.output_dir.mkdir(exist_ok=True, parents=True)
    assert request("/healthz", 200)[0] == {"status": "ready"}
    _, index_headers = request("/", 200)
    if args.base.startswith("https"):
        assert index_headers.get("Strict-Transport-Security")
    request("/web/config", 401)
    request("/web/voices/serena/preview", 401)
    request("/web/session", 401, {"invite": "invalid"})
    request("/web/session", 401, {"invite": access["api"]["owner"]})
    request("/web/session", 403, {"invite": invite}, {"Origin": "https://unrelated.invalid"})
    _, login_headers = request("/web/session", 204, {"invite": invite}, {"Origin": args.base}, opener=browser)
    cookie = login_headers["Set-Cookie"].lower()
    assert all(attr in cookie for attr in ("httponly", "samesite=strict", "path=/web"))
    if args.base.startswith("https"):
        assert "secure" in cookie
    request(API, 401, {"input": "测试"})
    request(API, 401, {"input": "测试"}, {"Authorization": "Bearer " + invite})
    request(API, 401, {"input": "测试"}, {"Authorization": "Bearer tts_invalid"})
    request(API, 401, {"input": "测试"}, opener=browser)
    request("/web/jobs", 401, {"input": "测试"}, auth)
    config, _ = request("/v1/audio/voices", 200, headers=auth)
    assert config["default_voice"] == "serena" and request("/web/config", 200, opener=browser)[0] == config
    for preset in config["voices"]:
        media, meta = request(f"/web/voices/{preset['id']}/preview", 200, opener=browser)
        assert len(media) > 1000 and meta.get_content_type() == "audio/mpeg"
    request("/web/voices/unknown/preview", 404, opener=browser)
    record("Auth: anonymous, wrong credentials, cross-use, cross-origin denied. Preset and private previews require a web session.")

    if args.full:
        maximum = config["max_characters"]
        request(API, 422, {"input": "再" * (maximum + 1)}, auth)
        request(API, 422, {"input": "… "}, auth)
        request(API, 422, {"input": "测试", "voice": "unknown"}, auth)
        request(API, 400, {"input": "测试", "voice": 1}, auth)
        request(API, 413, {"input": "a" * 8300}, auth)
        request(API, 415, b"{}", auth)
        text = LONG_TEXT
        if len(text) > maximum:
            ending = "请记下口令，青色的风铃。"
            text = text[:maximum - len(ending)] + ending
        if len(text) < maximum:
            text = ("请" + "先" * (maximum - len(text) - 1)) + text
        assert len(text) == maximum
        first, submit_headers = request(API, 202, {"input": text}, auth)
        assert submit_headers["Location"] == first["status_url"]
        request(first["status_url"] + "/audio", 409, headers=auth)
        request(first["status_url"], 401)
        request(first["status_url"].replace(API, "/web/jobs"), 404, opener=browser)
        other_browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request("/web/session", 204, {"invite": invite}, opener=other_browser)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            web_future = pool.submit(request, "/web/jobs", 202, {"input": "这是一段由同一台设备生成的普通话配音。"}, None, browser)
            api_future = pool.submit(request, API, 202, {"input": "你好，欢迎试用另一种音色。", "voice": "Dylan"}, auth)
            web_job, _ = web_future.result()
            dylan_job, _ = api_future.result()
        _, overload_headers = request(API, 429, {"input": "队列测试"}, auth)
        assert int(overload_headers["Retry-After"]) > 0
        request(web_job["status_url"], 404, opener=other_browser)
        initial, _ = request(first["status_url"], 200, headers=auth)
        queued_api, _ = request(dylan_job["status_url"], 200, headers=auth)
        queued_web, _ = request(web_job["status_url"], 200, opener=browser)
        assert initial["status"] == "running"
        assert {queued_api["queue_position"], queued_web["queue_position"]} == {1, 2}
        record(f"Validated {maximum}/{maximum + 1} characters, invalid inputs, private jobs, one running plus two queued, and 429 with Retry-After.")
        long_result = wait(first, headers=auth)
        save_audio(long_result["audio_url"], args.label + "-serena-max", headers=auth)
        web_result = wait(web_job, opener=browser)
        save_audio(web_result["audio_url"], args.label + "-web-queue", opener=browser)
        dylan_result = wait(dylan_job, headers=auth)
        save_audio(dylan_result["audio_url"], args.label + "-dylan", headers=auth)
        (args.output_dir / "long-text.txt").write_text(text + "\n")
    elif args.voices:
        samples = {
            "serena": "你好，欢迎使用配音工作台。",
            "vivian": "你好，欢迎使用配音工作台。",
            "uncle_fu": "你好，欢迎使用配音工作台。",
            "dylan": "您好，今儿咱们试试这个配音工具。",
            "eric": "你好噻，今儿我们来试一下配音。",
            "ryan": "Hello, this is a short narration sample.",
            "aiden": "Hello, this is a short narration sample.",
            "ono_anna": "こんにちは。これは音声合成のサンプルです。",
            "sohee": "안녕하세요. 음성 합성 테스트입니다.",
        }
        assert {voice["id"] for voice in config["voices"] if voice.get("kind", "preset") == "preset"} == samples.keys()
        for voice, text in samples.items():
            job, _ = request(API, 202, {"input": text, "voice": voice}, auth)
            result = wait(job, headers=auth)
            assert result["voice"] == voice
            save_audio(result["audio_url"], "voice-" + voice, headers=auth)
    else:
        first, _ = request(API, 202, {"input": "你好，我是 Serena。欢迎试用 AI 配音工作台。"}, auth)
        result = wait(first, headers=auth)
        save_audio(result["audio_url"], args.label + "-api-serena", headers=auth)
        request(result["audio_url"], 401)
    request("/web/session", 204, opener=browser, method="DELETE")
    request("/web/config", 401, opener=browser)
    (args.output_dir / (args.label + "-api-report.txt")).write_text("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
