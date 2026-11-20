import asyncio
import base64
import binascii
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from functools import partial
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from .access import COOKIE, SESSION_SECONDS, Access, Principal
from .engine import DEFAULT_VOICE, ROOT, VOICE_NAMES, create_engine
from .jobs import CONCURRENCY, Jobs
from .mcp_api import create_server, route as mcp_route
from .service import (API_SPEECH_PATH, MP3_TYPE, audio_filename, completed_audio, configuration, describe,
                      enqueue, enqueue_speech, fail, find_job, require_api_owner)
from .voices import (DESCRIPTION_LIMIT, MAX_PROFILES, MAX_RECORD_BYTES, NAME_LIMIT,
                     PREVIEW_SCRIPT, CapacityError, Voices, random_description)

WEB = ROOT / "voice_service/web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 8192
LOGIN_WINDOW = 60
LOGIN_LIMIT = 12
RECORD_TYPES = {"audio/webm": "webm", "audio/mp4": "mov", "audio/ogg": "ogg"}
RECORD_BODY_LIMIT = 4 * MAX_RECORD_BYTES // 3 + MAX_BODY
logging.getLogger("voice.jobs").setLevel(logging.INFO)
logging.getLogger("voice.jobs").addHandler(logging.StreamHandler())


@asynccontextmanager
async def lifespan(app):
    app.state.access = Access()
    app.state.login_attempts = OrderedDict()
    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="qwen") as executor:
        engine = await asyncio.get_running_loop().run_in_executor(executor, create_engine)
        app.state.jobs = Jobs(engine)
        app.state.voices = Voices() if getattr(engine, "private_voices", False) else None
        app.state.voice_mcp = create_server()
        worker = asyncio.create_task(app.state.jobs.work(executor))
        cleaner = asyncio.create_task(app.state.jobs.cleanup())
        try:
            async with app.state.voice_mcp.session_manager.run():
                yield
        finally:
            for task in (worker, cleaner):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    if hasattr(engine, "close"):
        engine.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def response_policy(request, call_next):
    # The app binds to loopback. Public scheme/peer come from the local tunnel only.
    if request.url.hostname not in LOCAL_HOSTS and request.url.scheme != "https":
        response = JSONResponse({"error": {"code": "https_required", "message": "请使用 HTTPS。"}}, 400)
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = "microphone=(self)"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=86400"
    return response


@app.exception_handler(HTTPException)
async def error_response(_request, exc):
    error = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": exc.detail}
    return JSONResponse({"error": error}, exc.status_code, headers=exc.headers)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Body):
    invite: str


class Speech(Body):
    input: str
    voice: str = DEFAULT_VOICE


class PrivateCandidate(Body):
    mode: Literal["record", "design", "random"]
    recording: str | None = None
    media_type: str | None = None
    description: str | None = None


class SaveVoice(Body):
    name: str


async def read_body(request, schema, limit=MAX_BODY):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        fail(415, "invalid_content_type", "请求需要 application/json。")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            fail(413, "body_too_large", "请求体过大。")
        body.extend(chunk)
    try:
        return schema.model_validate_json(bytes(body))
    except ValidationError:
        fail(400, "invalid_json", "请检查 JSON 格式、字段名和字段类型。")


def require_same_origin(request):
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if (origin is not None and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
        fail(403, "invalid_origin", "请从配音工作台操作。")


def web_owner(request: Request):
    owner = request.app.state.access.session(request.cookies.get(COOKIE, ""))
    if not owner:
        fail(401, "invite_required", "请输入有效的邀请码。")
    return owner


def api_owner(request: Request):
    return require_api_owner(request.app.state.access, request.headers.get("authorization", ""))


def login_rate_limit(request):
    records = request.app.state.login_attempts
    now = time.monotonic()
    while records and next(iter(records.values()))[0] + LOGIN_WINDOW <= now:
        records.popitem(last=False)
    peer = request.client.host if request.client else "unknown"
    since, count = records.get(peer, (now, 0))
    if count >= LOGIN_LIMIT or (peer not in records and len(records) >= 4096):
        fail(429, "login_rate_limit", "尝试次数过多，请稍后再试。", {"Retry-After": str(LOGIN_WINDOW)})
    records[peer] = (since, count + 1)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/studio.css")
async def stylesheet():
    return FileResponse(WEB / "studio.css", media_type="text/css")


@app.get("/studio.js")
async def javascript():
    return FileResponse(WEB / "studio.js", media_type="text/javascript")


@app.get("/private-voices.js")
async def private_javascript():
    return FileResponse(WEB / "private-voices.js", media_type="text/javascript")


@app.get("/healthz")
async def health():
    return {"status": "ready"}


@app.post("/web/session", status_code=204)
async def login(request: Request):
    require_same_origin(request)
    login_rate_limit(request)
    payload = await read_body(request, Login)
    token = request.app.state.access.login(payload.invite.strip())
    if not token:
        fail(401, "invalid_invite", "邀请码无效，请检查后重试。")
    response = Response(status_code=204)
    response.set_cookie(
        COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
        samesite="strict", secure=request.url.scheme == "https", path="/web"
    )
    return response


@app.delete("/web/session", status_code=204)
async def logout(request: Request):
    require_same_origin(request)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/web", secure=request.url.scheme == "https",
                           httponly=True, samesite="strict")
    return response


def private_store(request):
    store = request.app.state.voices
    if store is None:
        fail(503, "private_voices_unavailable", "私人音色暂不可用。")
    return store


@app.get("/web/voices/{voice}/preview")
async def voice_preview(request: Request, voice: str, owner: Principal = Depends(web_owner)):
    if voice in VOICE_NAMES:
        path = WEB / "previews" / f"{voice}.mp3"
        if path.is_file():
            return FileResponse(path, media_type="audio/mpeg")
    elif request.app.state.voices:
        preview = request.app.state.voices.preview(owner.voices, voice)
        if preview:
            return Response(preview, media_type="audio/mpeg")
    fail(404, "preview_not_found", "该音色暂无试听。")


@app.get("/web/config")
async def web_config(request: Request, owner: Principal = Depends(web_owner)):
    return configuration(request.app.state.voices, owner)


@app.get("/v1/audio/voices")
async def api_voices(request: Request, owner: Principal = Depends(api_owner)):
    return configuration(request.app.state.voices, owner)


def candidate_operation(engine, payload):
    if payload.mode == "record":
        if payload.description is not None:
            fail(422, "invalid_candidate", "录音模式需要麦克风录音。")
        container = RECORD_TYPES.get((payload.media_type or "").split(";", 1)[0].lower())
        if not container or not payload.recording:
            fail(422, "invalid_recording", "请使用工作台录音。")
        if len(payload.recording) > 4 * MAX_RECORD_BYTES // 3:
            fail(413, "recording_too_large", "录音文件过大，请重新录制。")
        try:
            raw = base64.b64decode(payload.recording, validate=True)
        except (ValueError, binascii.Error):
            fail(422, "invalid_recording", "录音数据有误，请重新录制。")
        if not raw or len(raw) > MAX_RECORD_BYTES:
            fail(422, "invalid_recording", "录音数据有误，请重新录制。")
        return partial(engine.preview_recording, raw, container)

    if payload.recording is not None or payload.media_type is not None:
        fail(422, "invalid_candidate", "声音设计需要文字描述。")
    if payload.mode == "random":
        if payload.description is not None:
            fail(422, "invalid_candidate", "随机设计由工作台选择描述。")
        description = random_description()
    else:
        description = (payload.description or "").strip()
        if not 1 <= len(description) <= DESCRIPTION_LIMIT or not any(char.isalnum() for char in description):
            fail(422, "invalid_description", f"请输入想要的声音，最多 {DESCRIPTION_LIMIT} 字。")
    return partial(engine.preview_design, description, payload.mode)


@app.post("/web/voice-candidates", status_code=202)
async def create_voice_candidate(request: Request, owner: Principal = Depends(web_owner)):
    require_same_origin(request)
    private_store(request)
    payload = await read_body(request, PrivateCandidate, RECORD_BODY_LIMIT)
    jobs = request.app.state.jobs
    operation = candidate_operation(jobs.engine, payload)
    job = enqueue(jobs, owner, PREVIEW_SCRIPT, payload.mode, operation)
    return accepted_job(jobs, job, "/web/jobs")


@app.post("/web/voice-candidates/{job_id}/save")
async def save_voice_candidate(request: Request, job_id: str, owner: Principal = Depends(web_owner)):
    require_same_origin(request)
    store = private_store(request)
    _, job = get_job(request, owner, job_id)
    voice_id = "private_" + job.id
    existing = store.details(owner.voices, voice_id)
    if existing:
        return existing
    if job.status != "succeeded" or job.candidate is None:
        fail(409, "candidate_unavailable", "候选音色尚未生成。")
    payload = await read_body(request, SaveVoice)
    name = payload.name.strip()
    if not 1 <= len(name) <= NAME_LIMIT or not any(char.isalnum() for char in name):
        fail(422, "invalid_voice_name", f"音色名称需包含文字，最多 {NAME_LIMIT} 字。")
    try:
        saved = store.save(owner.voices, voice_id, name, job.candidate, job.audio)
    except CapacityError:
        fail(409, "private_voice_limit", f"最多保存 {MAX_PROFILES} 个私人音色，请先删除不用的音色。")
    job.candidate = None
    return saved


@app.delete("/web/voices/{voice}", status_code=204)
async def delete_voice(request: Request, voice: str, owner: Principal = Depends(web_owner)):
    require_same_origin(request)
    store = private_store(request)
    if not store.delete(owner.voices, voice):
        fail(404, "private_voice_not_found", "该私人音色不存在。")
    request.app.state.jobs.finished.pop(voice.removeprefix("private_"), None)
    return Response(status_code=204)


async def submit(request, owner):
    payload = await read_body(request, Speech)
    jobs = request.app.state.jobs
    job = enqueue_speech(jobs, request.app.state.voices, owner, payload.input, payload.voice)
    return accepted_job(jobs, job, request.url.path)


def accepted_job(jobs, job, base):
    data = describe(jobs, job, base)
    return JSONResponse(data, 202, headers={"Location": data["status_url"]})


def get_job(request, owner, job_id):
    jobs = request.app.state.jobs
    return jobs, find_job(jobs, owner, job_id)


def status(request, owner, job_id):
    jobs, job = get_job(request, owner, job_id)
    return describe(jobs, job, request.url.path.rsplit("/", 1)[0])


def audio(request, owner, job_id):
    _, job = get_job(request, owner, job_id)
    return Response(completed_audio(job), media_type=MP3_TYPE,
                    headers={"Content-Disposition": f'attachment; filename="{audio_filename(job)}"'})


@app.post("/web/jobs", status_code=202)
async def submit_web(request: Request, owner: Principal = Depends(web_owner)):
    require_same_origin(request)
    return await submit(request, owner)


@app.get("/web/jobs/{job_id}")
async def status_web(request: Request, job_id: str, owner: Principal = Depends(web_owner)):
    return status(request, owner, job_id)


@app.get("/web/jobs/{job_id}/audio")
async def audio_web(request: Request, job_id: str, owner: Principal = Depends(web_owner)):
    return audio(request, owner, job_id)


@app.post(API_SPEECH_PATH, status_code=202)
async def submit_api(request: Request, owner: Principal = Depends(api_owner)):
    return await submit(request, owner)


@app.get(API_SPEECH_PATH + "/{job_id}")
async def status_api(request: Request, job_id: str, owner: Principal = Depends(api_owner)):
    return status(request, owner, job_id)


@app.get(API_SPEECH_PATH + "/{job_id}/audio")
async def audio_api(request: Request, job_id: str, owner: Principal = Depends(api_owner)):
    return audio(request, owner, job_id)


app.router.routes.append(mcp_route())
