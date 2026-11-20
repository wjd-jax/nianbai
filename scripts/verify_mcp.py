"""Exercise both MCP protocol revisions using the SDK and the live TTS queue."""
import argparse
import asyncio
import base64
import hashlib
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output/verification"
TOOLS = {"list_voices", "generate_speech", "get_speech_status", "get_speech_audio"}
USER_AGENT = "AI-Voice-MCP-Verification/1.0"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "voice-verification", "version": "1.0"},
}}


@asynccontextmanager
async def connect(base, http, mode="auto"):
    transport = streamable_http_client(f"{base}/mcp", http_client=http)
    async with Client(transport, mode=mode, read_timeout_seconds=15) as client:
        yield client


async def tool(client, name, arguments=None, error=None):
    value = await client.call_tool(name, arguments)
    if error is None:
        assert not value.is_error, value.content
    else:
        assert value.is_error, value.content
        assert value.structured_content["error"]["code"] == error, value.structured_content
    return value


async def wait_mcp(client, job_id):
    for _ in range(100):
        state = (await tool(client, "get_speech_status", {"job_id": job_id})).structured_content
        if state["status"] == "succeeded":
            return state
        assert state["status"] in {"queued", "running"}, state
        await asyncio.sleep(0.8)
    raise TimeoutError(job_id)


async def wait_rest(http, path):
    for _ in range(100):
        response = await http.get(path)
        response.raise_for_status()
        state = response.json()
        if state["status"] == "succeeded":
            return state
        assert state["status"] in {"queued", "running"}, state
        await asyncio.sleep(0.8)
    raise TimeoutError(path)


async def prepare_private(base, invite):
    http = httpx2.AsyncClient(base_url=base, timeout=12, headers={"User-Agent": USER_AGENT})
    try:
        response = await http.post("/web/session", json={"invite": invite})
        assert response.status_code == 204, response.status_code
        draft = await http.post("/web/voice-candidates", json={"mode": "design",
                               "description": "沉稳温暖的普通话女声，像在做一段晚间广播。"})
        assert draft.status_code == 202, draft.status_code
        job = await wait_rest(http, draft.json()["status_url"])
        save = await http.post(f"/web/voice-candidates/{job['id']}/save", json={"name": "MCP 权限验证"})
        save.raise_for_status()
        return http, save.json()["id"]
    except BaseException:
        await http.aclose()
        raise


async def run(args):
    OUTPUT.mkdir(exist_ok=True, parents=True)
    credentials = json.loads(args.credentials.read_text())
    headers = {"Authorization": "Bearer " + credentials["api"]["owner"], "User-Agent": USER_AGENT}
    private_voice = None
    web = None
    report = []

    try:
        if args.full:
            web, private_voice = await prepare_private(args.base, credentials["web"]["owner"])
        async with httpx2.AsyncClient(base_url=args.base, timeout=12, headers=headers) as http:
            baseline = await http.get("/v1/audio/voices")
            assert baseline.status_code == 200, baseline.status_code
            async with httpx2.AsyncClient(base_url=args.base, timeout=12, headers={"User-Agent": USER_AGENT}) as anonymous:
                unauthorized = await anonymous.post("/mcp", json=INITIALIZE)
                assert unauthorized.status_code == 401, unauthorized.status_code
                assert unauthorized.headers["www-authenticate"] == "Bearer"
                wrong_key = await anonymous.post("/mcp", json=INITIALIZE,
                                                 headers={"Authorization": "Bearer " + credentials["web"]["owner"]})
                assert wrong_key.status_code == 401
            accept = {"Accept": "application/json, text/event-stream"}
            blocked_origin = await http.post("/mcp", json=INITIALIZE,
                                             headers={**accept, "Origin": "https://invalid.example"})
            assert blocked_origin.status_code == 403, blocked_origin.status_code
            oversized = await http.post("/mcp", json={**INITIALIZE, "padding": "a" * 16384}, headers=accept)
            assert oversized.status_code == 413, oversized.status_code
            report.append("HTTP auth, credential type, Origin and 16 KiB request limit verified.")

            async with connect(args.base, http) as client:
                report.append(f"SDK auto protocol {client.protocol_version}.")
                discovery = await client.list_tools()
                assert {item.name for item in discovery.tools} == TOOLS
                config = (await tool(client, "list_voices")).structured_content
                assert config["voices"] == baseline.json()["voices"]
                assert config["default_voice"] == "serena"
                await tool(client, "generate_speech", {"input": "张" * (config["max_characters"] + 1)}, "input_too_long")
                await tool(client, "generate_speech", {"input": "。"}, "empty_input")
                await tool(client, "generate_speech", {"input": "配音", "voice": "missing"}, "unsupported_voice")
                text = "这是一次 MCP 配音测试。我们把文字变成语音。"
                if args.full:
                    text = "这是一次跨入口的配音队列测试。请用自然节奏读完整段文字。" * 3
                first = (await tool(client, "generate_speech", {"input": text})).structured_content
                job_id = first["id"]
                assert first["voice"] == "serena"
                assert first["status_url"] == f"{args.base}/v1/audio/speech/{job_id}"
                await tool(client, "get_speech_audio", {"job_id": job_id}, "audio_unavailable")

                if args.full:
                    second = (await tool(client, "generate_speech", {"input": "这是私人音色测试。", "voice": private_voice})).structured_content
                    third = await http.post("/v1/audio/speech", json={"input": "这是 REST 的任务。"})
                    assert third.status_code == 202, third.status_code
                    queued = await tool(client, "generate_speech", {"input": "队列满"}, "queue_full")
                    assert queued.structured_content["error"]["retry_after_seconds"] > 0
                    outsider_headers = {"Authorization": "Bearer " + credentials["api"]["outsider"], "User-Agent": USER_AGENT}
                    async with httpx2.AsyncClient(headers=outsider_headers, timeout=12) as outsider:
                        async with connect(args.base, outsider) as guest:
                            other_voices = (await tool(guest, "list_voices")).structured_content["voices"]
                            assert private_voice not in {voice["id"] for voice in other_voices}
                            await tool(guest, "generate_speech", {"input": "隐藏音色", "voice": private_voice}, "unsupported_voice")
                            await tool(guest, "get_speech_status", {"job_id": job_id}, "job_not_found")
                            await tool(guest, "get_speech_audio", {"job_id": job_id}, "job_not_found")
                    report.append("Mixed MCP/REST queue and private voice/job isolation verified.")
                completed = await wait_mcp(client, job_id)
                audio_result = await tool(client, "get_speech_audio", {"job_id": job_id})
                audio_parts = [part for part in audio_result.content if part.type == "audio"]
                assert len(audio_parts) == 1 and audio_parts[0].mime_type == "audio/mpeg"
                mp3 = base64.b64decode(audio_parts[0].data, validate=True)
                assert len(mp3) == audio_result.structured_content["size_bytes"] > 1000
                assert completed["audio_url"] == f"{args.base}/v1/audio/speech/{job_id}/audio"
                download = await http.get(completed["audio_url"])
                assert download.status_code == 200 and download.content == mp3
                file = OUTPUT / f"{args.label}-serena.mp3"
                file.write_bytes(mp3)
                subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(file), "-f", "null", "-"], check=True)
                report.append(f"MP3 {completed['duration_seconds']:.2f}s; matches Bearer-protected REST download; SHA-256 {hashlib.sha256(mp3).hexdigest()}.")

                if args.full:
                    _, cross_status = await asyncio.gather(wait_mcp(client, second["id"]),
                                                         wait_rest(http, third.json()["status_url"]))
                    private_audio = await tool(client, "get_speech_audio", {"job_id": second["id"]})
                    assert private_audio.structured_content["voice"] == private_voice
                    assert cross_status["voice"] == "serena"
                    report.append("Real inference completed for both the private voice and the REST request in the shared queue.")

            async with connect(args.base, http, mode="legacy") as legacy:
                assert {item.name for item in (await legacy.list_tools()).tools} == TOOLS
                state = (await tool(legacy, "get_speech_status", {"job_id": job_id})).structured_content
                assert state["status"] == "succeeded"
                report.append(f"SDK initialize handshake {legacy.protocol_version} can discover tools and read the same task.")
    finally:
        if web is not None:
            try:
                response = await web.delete(f"/web/voices/{private_voice}")
                assert response.status_code == 204, response.status_code
            finally:
                await web.aclose()

    (OUTPUT / f"{args.label}-report.txt").write_text("\n".join(report) + "\n")
    for line in report:
        print(line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--credentials", type=Path, default=ROOT / ".runtime/access.json")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    args.base = args.base.rstrip("/")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
