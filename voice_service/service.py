from datetime import datetime, timezone
from functools import partial

from fastapi import HTTPException

from .engine import DEFAULT_VOICE, MAX_CHARACTERS, VOICE_NAMES
from .jobs import CONCURRENCY, QUEUE_LIMIT, RESULT_TTL, RETRY_AFTER
from .voices import DESCRIPTION_LIMIT, MAX_PROFILES, MAX_RECORD_SECONDS, NAME_LIMIT, RECORD_SCRIPT

API_SPEECH_PATH = "/v1/audio/speech"
MP3_TYPE = "audio/mpeg"


def fail(status, code, message, headers=None):
    raise HTTPException(status, {"code": code, "message": message}, headers)



def require_api_owner(access, authorization):
    owner = access.api_owner(authorization)
    if not owner:
        fail(401, "invalid_api_key", "请提供有效的 API Key。", {"WWW-Authenticate": "Bearer"})
    return owner


def configuration(store, owner):
    voices = [{"id": key, "name": name, "description": desc, "kind": "preset"}
              for key, (name, desc) in VOICE_NAMES.items()]
    options = None
    if store:
        voices.extend(store.list(owner.voices))
        options = {
            "record_script": RECORD_SCRIPT, "max_record_seconds": MAX_RECORD_SECONDS,
            "description_limit": DESCRIPTION_LIMIT, "name_limit": NAME_LIMIT, "profile_limit": MAX_PROFILES,
        }
    return {
        "voices": voices, "private_voices": options, "default_voice": DEFAULT_VOICE,
        "max_characters": MAX_CHARACTERS, "concurrency": CONCURRENCY,
        "queue_limit": QUEUE_LIMIT, "result_ttl_seconds": RESULT_TTL,
    }



def describe(jobs, job, base):
    status_url = f"{base}/{job.id}"
    result = {"id": job.id, "status": job.status, "voice": job.voice, "status_url": status_url}
    if job.status == "queued":
        result["queue_position"] = jobs.position(job.id)
    if job.status == "succeeded":
        result.update(audio_url=f"{status_url}/audio", duration_seconds=job.duration)
        if job.candidate:
            result["candidate"] = {"source": job.candidate.source, "description": job.candidate.description}
    if job.status == "failed":
        result["error"] = {"code": "generation_failed", "message": job.error}
    if job.expires_at:
        result["expires_at"] = datetime.fromtimestamp(job.expires_at, timezone.utc).isoformat()
    return result



def enqueue_speech(jobs, store, owner, text, voice):
    text = text.strip()
    voice = voice.strip().lower()
    if len(text) > MAX_CHARACTERS:
        fail(422, "input_too_long", f"单次最多 {MAX_CHARACTERS} 个字符。")
    if not any(char.isalnum() for char in text):
        fail(422, "empty_input", "请输入可以朗读的文字。")
    operation = None
    if voice not in VOICE_NAMES:
        reference = store.reference(owner.voices, voice) if store else None
        if not reference:
            fail(422, "unsupported_voice", "请选择可用音色。")
        operation = partial(jobs.engine.synthesize_reference, text, reference)
    return enqueue(jobs, owner, text, voice, operation)


def enqueue(jobs, owner, text, voice, operation=None):
    job = jobs.submit(owner.jobs, text, voice, operation)
    if not job:
        fail(429, "queue_full", "队列已满，请稍后再提交。", {"Retry-After": str(RETRY_AFTER)})
    return job


def find_job(jobs, owner, job_id):
    job = jobs.find(job_id, owner.jobs)
    if not job:
        fail(404, "job_not_found", "找不到任务，或结果已过期。")
    return job


def completed_audio(job):
    if job.status != "succeeded":
        fail(409, "audio_unavailable", "任务暂无可下载音频。")
    return job.audio


def audio_filename(job):
    return f"{job.voice}-{job.id[:8]}.mp3"
