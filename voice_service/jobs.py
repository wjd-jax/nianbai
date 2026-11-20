import asyncio
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

from .engine import GenerationError
from .voices import Candidate, VoiceResult

QUEUE_LIMIT = int(os.environ.get("VOICE_QUEUE_LIMIT", "2"))
CONCURRENCY = 1
RESULT_TTL = 30 * 60
RESULT_LIMIT = 64
RETRY_AFTER = 10

logger = logging.getLogger("voice.jobs")


@dataclass
class Job:
    id: str
    owner: str
    voice: str
    status: str = "queued"
    audio: bytes | None = None
    duration: float | None = None
    error: str | None = None
    expires_at: float | None = None
    candidate: Candidate | None = None


class Jobs:
    def __init__(self, engine):
        self.engine = engine
        self.queue = asyncio.Queue(maxsize=CONCURRENCY + QUEUE_LIMIT)
        self.active = {}
        self.finished = OrderedDict()
        self.running = 0

    def submit(self, owner, text, voice, operation=None):
        if len(self.active) >= CONCURRENCY + QUEUE_LIMIT:
            return None
        job = Job(uuid4().hex, owner, voice)
        self.active[job.id] = job
        if operation is None:
            operation = partial(self.engine.synthesize, text, voice)
        self.queue.put_nowait((job, len(text), operation))
        return job

    def find(self, job_id, owner):
        self.prune()
        job = self.active.get(job_id) or self.finished.get(job_id)
        return job if job and job.owner == owner else None

    def position(self, job_id):
        for position, job in enumerate(self.active.values()):
            if job.id == job_id:
                return position - self.running + 1
        return None

    def prune(self):
        now = time.time()
        while self.finished:
            oldest = next(iter(self.finished.values()))
            if oldest.expires_at > now and len(self.finished) <= RESULT_LIMIT:
                break
            self.finished.popitem(last=False)

    async def cleanup(self):
        while True:
            await asyncio.sleep(30)
            self.prune()

    async def work(self, executor):
        loop = asyncio.get_running_loop()
        while True:
            job, characters, operation = await self.queue.get()
            job.status = "running"
            self.running = 1
            started = time.monotonic()
            try:
                result = await loop.run_in_executor(executor, operation)
                if isinstance(result, VoiceResult):
                    job.audio, job.duration, job.candidate = result.audio, result.duration, result.candidate
                else:
                    job.audio, job.duration = result
                del result
                job.status = "succeeded"
            except GenerationError as exc:
                job.status, job.error = "failed", str(exc)
            except Exception as exc:
                logger.error("Synthesis %s failed (%s)", job.id, type(exc).__name__)
                job.status, job.error = "failed", "合成出现错误，请稍后重试。"
            finally:
                self.running = 0
                job.expires_at = time.time() + RESULT_TTL
                self.active.pop(job.id)
                self.finished[job.id] = job
                self.prune()
                self.queue.task_done()
                logger.info("Job %s voice=%s chars=%d state=%s elapsed=%.2fs audio=%ss",
                            job.id, job.voice, characters, job.status,
                            time.monotonic() - started, job.duration)
                del operation
