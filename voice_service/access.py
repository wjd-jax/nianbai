import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCESS_FILE = ROOT / os.environ.get("VOICE_ACCESS_FILE", ".runtime/access.json")
COOKIE = "voice_session"
SESSION_SECONDS = 8 * 60 * 60
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,100}")


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def write_access(data):
    ACCESS_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    # Replace atomically so reads and permission bits remain consistent.
    with tempfile.NamedTemporaryFile(mode="w", dir=ACCESS_FILE.parent, delete=False) as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write("\n")
        temp_path = out.name
    os.replace(temp_path, ACCESS_FILE)


def read_access():
    if ACCESS_FILE.stat().st_mode & 0o077:
        raise RuntimeError("Set access.json permissions to 600")
    return json.loads(ACCESS_FILE.read_text())


def bootstrap():
    if ACCESS_FILE.exists():
        return
    write_access({
        "session_secret": secrets.token_hex(32),
        "web": {"owner": "inv_" + secrets.token_urlsafe(24)},
        "api": {"owner": "tts_" + secrets.token_urlsafe(32)},
    })


@dataclass(frozen=True)
class Principal:
    jobs: str
    voices: str


class Access:
    def __init__(self):
        data = read_access()
        self.secret = bytes.fromhex(data["session_secret"])
        self.invites = {digest(token): name for name, token in data["web"].items()}
        self.keys = {digest(token): name for name, token in data["api"].items()}

    @staticmethod
    def match(token, allowed):
        if not TOKEN_PATTERN.fullmatch(token):
            return None
        fingerprint = digest(token)
        matched = False
        for expected in allowed:
            matched |= hmac.compare_digest(fingerprint, expected)
        return fingerprint if matched else None

    def sign(self, payload):
        return hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def login(self, invite):
        fingerprint = self.match(invite, self.invites)
        if not fingerprint:
            return None
        payload = f"{fingerprint}.{secrets.token_hex(16)}.{int(time.time()) + SESSION_SECONDS}"
        return f"{payload}.{self.sign(payload)}"

    def session(self, token):
        if not re.fullmatch(r"[a-f0-9]{64}\.[a-f0-9]{32}\.[0-9]{1,12}\.[a-f0-9]{64}", token):
            return None
        fingerprint, nonce, expires, signature = token.split(".")
        if int(expires) <= time.time() or fingerprint not in self.invites:
            return None
        if not hmac.compare_digest(signature, self.sign(f"{fingerprint}.{nonce}.{expires}")):
            return None
        return Principal("web:" + nonce, self.invites[fingerprint])

    def api_owner(self, authorization):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        fingerprint = self.match(token, self.keys)
        return Principal("api:" + fingerprint, self.keys[fingerprint]) if fingerprint else None
