import base64
import json
import math
import os
import selectors
import subprocess
import time
from array import array
from uuid import uuid4

from .engine import MAX_AUDIO_TOKENS, ROOT, STYLE, VOICE_NAMES, GenerationError, segments

SAMPLE_RATE = 24000
STARTUP_TIMEOUT = 60
JOB_TIMEOUT = 240
MAX_SEGMENT_SECONDS = MAX_AUDIO_TOKENS / 12.5
MAX_SEGMENT_BYTES = math.ceil(MAX_SEGMENT_SECONDS * SAMPLE_RATE) * 2
MAX_LINE_BYTES = 128 * 1024
GGUF = "Qwen3-TTS-12Hz-1.7B-CustomVoice-Q6_K.gguf"


class SpeechCppEngine:
    def __init__(self):
        import lameenc

        self.encoder_factory = lameenc.Encoder
        self.executable = ROOT / "bin/speech"
        self.model = ROOT / "assets" / GGUF
        if not self.executable.is_file() or not self.model.is_file():
            raise RuntimeError("Install the pinned speech.cpp executable and GGUF model")
        threads = os.environ.get("VOICE_CPU_THREADS", "3")
        self.command = [
            str(self.executable), "worker", str(self.model), "--device", "cpu",
            "--threads", threads, "--no-warmup",
        ]
        self.process = None
        self.selector = None
        self.buffer = bytearray()
        self.start()

    def start(self):
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.buffer.clear()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            ready = self.receive(time.monotonic() + STARTUP_TIMEOUT)
            if ready.get("type") != "ready" or ready.get("protocol") != 4:
                raise RuntimeError("speech.cpp did not start protocol 4")
            model = ready["model"]
            if model["architecture"] != "qwen3-tts" or model["sample_rate"] != SAMPLE_RATE:
                raise RuntimeError("Unexpected Qwen3-TTS model")
            if not VOICE_NAMES.keys() <= {item["name"].lower() for item in model["voices"]}:
                raise RuntimeError("Voice presets do not match the installed model")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()
        self.process = None

    def receive(self, deadline):
        while True:
            end = self.buffer.find(b"\n")
            if end != -1:
                line = self.buffer[:end]
                del self.buffer[:end + 1]
                return json.loads(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Timed out waiting for speech.cpp")
            chunk = os.read(self.process.stdout.fileno(), 8192)
            if not chunk:
                raise ConnectionError("speech.cpp exited")
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_LINE_BYTES:
                raise RuntimeError("Oversized speech.cpp protocol frame")

    def request(self, text, voice, deadline):
        request_id = uuid4().hex
        payload = {
            "type": "synthesize", "id": request_id, "text": text, "voice": voice,
            "instructions": STYLE, "temperature": 0.75, "top_p": 0.89,
            "max_seconds": MAX_SEGMENT_SECONDS,
        }
        self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        samples = bytearray()
        next_sequence = 0
        while True:
            message = self.receive(deadline)
            if message.get("id") != request_id:
                raise RuntimeError("Mismatched speech.cpp reply")
            kind = message["type"]
            if kind == "chunk":
                if message["seq"] != next_sequence:
                    raise RuntimeError("Missing speech.cpp frame")
                next_sequence += 1
                samples.extend(base64.b64decode(message["pcm"], validate=True))
                if len(samples) > MAX_SEGMENT_BYTES:
                    raise RuntimeError("Oversized audio from speech.cpp")
            elif kind == "end":
                if message["stop"] != "complete":
                    raise GenerationError("有一段文字未能完整读完。请缩短句子或增加标点后重试。")
                if not samples or len(samples) != message["samples"] * 2:
                    raise RuntimeError("Incomplete audio from speech.cpp")
                return samples
            elif kind in {"error", "cancelled"}:
                raise GenerationError("这一段文字未生成配音。请调整文稿后重试。")
            elif kind != "progress":
                raise RuntimeError("Unexpected speech.cpp reply")

    def synthesize(self, text, voice):
        deadline = time.monotonic() + JOB_TIMEOUT
        if self.process is None:
            self.start()
        chunks = []
        try:
            for segment in segments(text):
                if any(char.isalnum() for char in segment):
                    chunks.append(trim(self.request(segment, voice, deadline)))
        except TimeoutError as exc:
            self.close()
            raise GenerationError("本次合成超时，请缩短文稿后重试。") from exc
        except GenerationError:
            raise
        except Exception:
            self.close()
            raise

        if not chunks:
            raise GenerationError("文稿需要包含可以朗读的文字。")
        pcm = bytes(round(SAMPLE_RATE * 0.12) * 2).join(chunks)
        encoder = self.encoder_factory()
        encoder.set_in_sample_rate(SAMPLE_RATE)
        encoder.set_channels(1)
        encoder.set_bit_rate(96)
        encoder.set_quality(2)
        audio = bytes(encoder.encode(pcm)) + bytes(encoder.flush())
        if not audio:
            raise GenerationError("MP3 编码失败，请重试。")
        return audio, round(len(pcm) / (2 * SAMPLE_RATE), 2)


def trim(pcm):
    samples = array("h")
    samples.frombytes(pcm)
    # The target Intel runtime and worker PCM are both little-endian.
    size = SAMPLE_RATE // 100
    energies = [
        sum(value * value for value in samples[start:start + size])
        for start in range(0, len(samples) - size + 1, size)
    ]
    threshold = max(max(energies, default=0) * 0.02**2, size * (32768 * 0.0022)**2)
    audible = [index for index, energy in enumerate(energies) if energy > threshold]
    if not audible:
        raise GenerationError("这段文字未生成可听语音，请调整文稿后重试。")
    return pcm[max(0, audible[0] - 5) * size * 2:(audible[-1] + 12) * size * 2]
