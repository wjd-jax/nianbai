import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from .engine import ROOT

RECORD_SCRIPT = "你好，这是我的声音样本。我会用平时说话的语速，把这段话自然地读完。每一句之间稍微停一下，像在和朋友聊天一样。"
PREVIEW_SCRIPT = "你好，很高兴见到你。听听这个声音，读一段自然、有停顿的话。"
MAX_RECORD_SECONDS = 24
MAX_RECORD_BYTES = 3 * 1024 * 1024
MAX_PROFILES = 20
DESCRIPTION_LIMIT = 180
NAME_LIMIT = 24
MODEL_ALIASES = {"record": "录音克隆", "design": "声音定制", "random": "随机设计"}


@dataclass(frozen=True)
class Reference:
    pcm: bytes
    text: str


@dataclass(frozen=True)
class Candidate:
    source: str
    reference: Reference
    description: str = ""


@dataclass(frozen=True)
class VoiceResult:
    audio: bytes
    duration: float
    candidate: Candidate | None = None


class CapacityError(Exception):
    pass


def random_description():
    register = secrets.choice((
        "干净明亮的普通话女声", "轻柔细腻的普通话女声", "低沉醇厚的普通话男声",
        "清亮自然的普通话男声", "中性、清晰的普通话声音", "有一点沙哑的普通话声音",
    ))
    cadence = secrets.choice(("语速略慢，停顿清楚", "语速轻快，咬字清晰", "语速平稳，节奏自然"))
    style = secrets.choice(("像在和朋友聊天", "适合安静的故事旁白", "带一点微笑，像在做节目开场"))
    return f"{register}，{cadence}，{style}。"


class Voices:
    def __init__(self):
        path = os.environ.get("VOICE_PROFILE_DATABASE")
        self.path = ROOT / ".runtime/private-voices.sqlite3" if not path else ROOT / path
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
            os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise RuntimeError("Private voice database permissions must be 600")
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS voices (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    name TEXT NOT NULL, source TEXT NOT NULL,
                    reference BLOB NOT NULL, transcript TEXT NOT NULL,
                    preview BLOB NOT NULL, created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );
                CREATE INDEX IF NOT EXISTS voices_owner ON voices(owner, created_at);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list(self, owner):
        with self.connect() as connection:
            fields = connection.execute(
                "SELECT id, name, source FROM voices WHERE owner = ? ORDER BY created_at DESC, id",
                (owner,),
            ).fetchall()
        return [
            {"id": row["id"], "name": row["name"], "source": row["source"], "kind": "private",
             "description": MODEL_ALIASES[row["source"]]}
            for row in fields
        ]

    def reference(self, owner, voice_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT reference, transcript FROM voices WHERE owner = ? AND id = ?", (owner, voice_id)
            ).fetchone()
        return Reference(row["reference"], row["transcript"]) if row else None

    def preview(self, owner, voice_id):
        with self.connect() as connection:
            row = connection.execute("SELECT preview FROM voices WHERE owner = ? AND id = ?", (owner, voice_id)).fetchone()
        return row["preview"] if row else None

    def details(self, owner, voice_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, name, source FROM voices WHERE owner = ? AND id = ?", (owner, voice_id)
            ).fetchone()
        return dict(row) if row else None

    def save(self, owner, voice_id, name, candidate, audio):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, name, source FROM voices WHERE owner = ? AND id = ?", (owner, voice_id)
            ).fetchone()
            if existing:
                return dict(existing)
            count = connection.execute("SELECT COUNT(*) FROM voices WHERE owner = ?", (owner,)).fetchone()[0]
            if count >= MAX_PROFILES:
                raise CapacityError
            connection.execute(
                "INSERT INTO voices(id, owner, name, source, reference, transcript, preview) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (voice_id, owner, name, candidate.source, candidate.reference.pcm, candidate.reference.text, audio),
            )
        return {"id": voice_id, "name": name, "source": candidate.source}

    def delete(self, owner, voice_id):
        with self.connect() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            return connection.execute("DELETE FROM voices WHERE owner = ? AND id = ?", (owner, voice_id)).rowcount > 0
