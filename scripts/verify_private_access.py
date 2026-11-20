"""Verify saved private voice ownership and fresh synthesis from the API."""
import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output/verification"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--credentials", type=Path, default=ROOT / ".runtime/access.json")
    parser.add_argument("--check-outsider", action="store_true")
    parser.add_argument("--delete-fixtures", action="store_true")
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())
    browser_report = json.loads((OUTPUT / (args.label + "-browser-report.txt")).read_text())
    modes = {"record": "record", "design": "design", "random": "random"}
    saved = {key: browser_report[key]["id"] for key in modes}
    no_cookies = urllib.request.build_opener()

    def request(path, expected, data=None, *, opener=no_cookies, key=None, method=None):
        headers = {"User-Agent": "AI-Voice-Verification/1.0"}
        if key:
            headers["Authorization"] = "Bearer " + credentials["api"][key]
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(args.base + path, json.dumps(data).encode() if data is not None else None,
                                     headers, method=method)
        try:
            response = opener.open(req, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert response.status == expected, (path, response.status, expected)
            body = response.read()
            return json.loads(body) if response.headers.get_content_type() == "application/json" else body

    def login(key):
        cookies = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request("/web/session", 204, {"invite": credentials["web"][key]}, opener=cookies)
        return cookies

    owner = login("owner")
    web = {v["id"]: v for v in request("/web/config", 200, opener=owner)["voices"]}
    api = {v["id"]: v for v in request("/v1/audio/voices", 200, key="owner")["voices"]}
    assert web == api
    assert sum(v["kind"] == "preset" for v in web.values()) == 9
    if args.check_outsider:
        outsider = login("outsider")
        outsider_voices = request("/web/config", 200, opener=outsider)["voices"]
        outsider_api = request("/v1/audio/voices", 200, key="outsider")["voices"]
        assert web.keys() > {v["id"] for v in outsider_voices} == {v["id"] for v in outsider_api}
        request("/web/voice-candidates", 401, {"mode": "random"})
        request("/web/voice-candidates", 422, {"mode": "design", "description": "！"}, opener=owner)
        request("/web/voice-candidates", 422, {"mode": "record", "media_type": "audio/webm", "recording": "%%"}, opener=owner)
        for voice_id in saved.values():
            request(f"/web/voices/{voice_id}/preview", 404, opener=outsider)
            request(f"/web/voices/{voice_id}", 404, opener=outsider, method="DELETE")
            request("/v1/audio/speech", 422, {"input": "其他人不能使用这个音色。", "voice": voice_id}, key="outsider")

    lines = []
    for source, voice_id in saved.items():
        assert api[voice_id]["source"] == modes[source]
        sample = request(f"/web/voices/{voice_id}/preview", 200, opener=owner)
        assert len(sample) > 1000
        (OUTPUT / f"{args.label}-{source}-preview.mp3").write_bytes(sample)
        start = time.monotonic()
        job = request("/v1/audio/speech", 202, {"input": "这是一句新的文字，用保存的私人音色读出来。", "voice": voice_id}, key="owner")
        if args.check_outsider:
            request(job["status_url"], 404, key="outsider")
        while time.monotonic() - start < 120:
            job = request(job["status_url"], 200, key="owner")
            if job["status"] == "succeeded":
                audio = request(job["audio_url"], 200, key="owner")
                assert len(audio) > 1000
                output = OUTPUT / f"{args.label}-{source}-api.mp3"
                output.write_bytes(audio)
                lines.append(f"{source}: authorized, {job['duration_seconds']:.2f}s MP3, elapsed {time.monotonic() - start:.1f}s")
                break
            assert job["status"] != "failed", job.get("error")
            time.sleep(1)
        else:
            raise TimeoutError(job["id"])

    if args.delete_fixtures:
        for source, voice_id in saved.items():
            assert web[voice_id]["name"] == f"{args.label}-{source}"
            request(f"/web/voices/{voice_id}", 204, opener=owner, method="DELETE")
            request(f"/web/voices/{voice_id}/preview", 404, opener=owner)
        lines.append("Deleted all three synthetic verification voices; each preview now returns 404.")
    if args.check_outsider:
        lines.append("Second named invite and API key cannot see, play, delete or synthesize another identity's voices.")
    for line in lines:
        print(line)
    (OUTPUT / f"{args.label}-private-api-report.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
