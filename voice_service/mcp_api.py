import base64
import json
import os
from typing import Annotated

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import AudioContent, CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .engine import DEFAULT_VOICE
from .service import (API_SPEECH_PATH, MP3_TYPE, audio_filename, completed_audio, configuration, describe,
                      enqueue_speech, find_job, require_api_owner)

MCP_PATH = "/mcp"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MCP_BODY_LIMIT = 16 * 1024


class MCPRoute:
    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        try:
            owner = require_api_owner(request.app.state.access, request.headers.get("authorization", ""))
        except HTTPException as exc:
            response = JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            await response(scope, receive, send)
            return

        request.state.voice_mcp_owner = owner
        await request.app.state.voice_mcp.session_manager.handle_request(scope, receive, send)


def request_state(ctx):
    request = ctx.request_context.request
    return request, request.app.state, request.state.voice_mcp_owner


def job_summary(request, jobs, job):
    base = str(request.base_url).rstrip("/") + API_SPEECH_PATH
    return describe(jobs, job, base)


def result(data, *, audio=None, is_error=False):
    content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]
    if audio is not None:
        content.append(AudioContent(type="audio", mimeType=MP3_TYPE, data=base64.b64encode(audio).decode("ascii")))
    return CallToolResult(content=content, structuredContent=data, isError=is_error)


def tool_error(exc):
    data = {"error": dict(exc.detail)}
    if exc.headers and "Retry-After" in exc.headers:
        data["error"]["retry_after_seconds"] = int(exc.headers["Retry-After"])
    return result(data, is_error=True)


def create_server():
    public_host = os.environ.get("VOICE_PUBLIC_HOST", "")
    allowed_hosts = [*LOOPBACK_HOSTS, *(host + ":*" for host in LOOPBACK_HOSTS)]
    allowed_origins = [*("http://" + host for host in LOOPBACK_HOSTS),
                       *("http://" + host + ":*" for host in LOOPBACK_HOSTS)]
    if public_host:
        allowed_hosts.extend((public_host, public_host + ":443"))
        allowed_origins.extend(("https://" + public_host, "https://" + public_host + ":443"))
    server = MCPServer(
        "念白",
        instructions=(
            "先用 list_voices 查询当前密钥可用的音色。generate_speech 提交异步任务，"
            "返回任务 ID；间隔约 2 秒调用 get_speech_status。成功后可用 get_speech_audio 获取 MP3，"
            "或凭同一 API Key 下载状态中提供的 audio_url。"
        ),
    )

    @server.tool(title="查询可用音色", description="查询内置音色、当前 API Key 可用的私人音色和服务限制。",
                 annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def list_voices(ctx: Context) -> CallToolResult:
        _, state, owner = request_state(ctx)
        data = configuration(state.voices, owner)
        data.pop("private_voices")
        return result(data)

    @server.tool(title="提交配音", description="提交配音文稿并立即返回任务 ID。默认音色为 Serena。"
                 "结果为异步任务，请用 get_speech_status 查询。该调用使用生成队列。",
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                             idempotentHint=False, openWorldHint=False))
    async def generate_speech(
        input: Annotated[str, Field(description="要朗读的文稿。字符上限见 list_voices。")],
        ctx: Context,
        voice: Annotated[str, Field(description="list_voices 返回的音色 ID。")] = DEFAULT_VOICE,
    ) -> CallToolResult:
        request, state, owner = request_state(ctx)
        try:
            job = enqueue_speech(state.jobs, state.voices, owner, input, voice)
        except HTTPException as exc:
            return tool_error(exc)
        return result(job_summary(request, state.jobs, job))

    @server.tool(title="查询配音任务", description="按任务 ID 查询排队、运行、完成或失败状态。"
                 "完成时附带时长和需要同一 API Key 的 HTTPS MP3 地址。",
                 annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def get_speech_status(
        job_id: Annotated[str, Field(description="generate_speech 返回的任务 ID。")], ctx: Context
    ) -> CallToolResult:
        request, state, owner = request_state(ctx)
        try:
            job = find_job(state.jobs, owner, job_id)
        except HTTPException as exc:
            return tool_error(exc)
        return result(job_summary(request, state.jobs, job))

    @server.tool(title="获取 MP3", description="读取已完成任务的 MP3 音频内容和下载地址。"
                 "同一个任务只需读取一次。只需要地址时，请使用 get_speech_status。",
                 annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def get_speech_audio(
        job_id: Annotated[str, Field(description="已成功完成的配音任务 ID。")], ctx: Context
    ) -> CallToolResult:
        request, state, owner = request_state(ctx)
        try:
            job = find_job(state.jobs, owner, job_id)
            audio = completed_audio(job)
        except HTTPException as exc:
            return tool_error(exc)
        data = job_summary(request, state.jobs, job)
        data.update(filename=audio_filename(job), mime_type=MP3_TYPE, size_bytes=len(audio))
        return result(data, audio=audio)

    server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MCP_BODY_LIMIT,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
        ),
    )
    return server


def route():
    return Route(MCP_PATH, endpoint=MCPRoute(), methods=["GET", "POST", "DELETE"])
