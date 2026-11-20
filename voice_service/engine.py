import gc
import os
import re
import shutil
import subprocess
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "assets/model"
VOICE_NAMES = {
    "serena": ("Serena", "温柔自然的普通话女声"),
    "vivian": ("Vivian", "清亮的普通话女声"),
    "uncle_fu": ("Uncle_Fu", "醇厚低沉的普通话男声"),
    "dylan": ("Dylan", "清晰自然的北京男声"),
    "eric": ("Eric", "活泼的四川话男声"),
    "ryan": ("Ryan", "节奏鲜明的英语男声"),
    "aiden": ("Aiden", "阳光的美式英语男声"),
    "ono_anna": ("Ono_Anna", "俏皮轻快的日语女声"),
    "sohee": ("Sohee", "温暖的韩语女声"),
}
DEFAULT_VOICE = "serena"
MAX_CHARACTERS = int(os.environ.get("VOICE_MAX_CHARACTERS", "240"))
SEGMENT_LIMIT = 72
MAX_AUDIO_TOKENS = 448
STYLE = "亲切轻松，像在和一位同事交谈。用日常讲话速度，停连自然，英文名称读清楚。"


class GenerationError(Exception):
    pass


def segments(text):
    pending = ""
    for part in re.split(r"(?<=[。！？!?；;\.])|(?=\n)", text):
        pending += part
        while len(pending) > SEGMENT_LIMIT:
            for boundary in ("。！？!?；;\n", "，、：,: ", "."):
                cut = max(pending.rfind(mark, 0, SEGMENT_LIMIT) for mark in boundary)
                if cut >= SEGMENT_LIMIT // 2:
                    break
            else:
                cut = SEGMENT_LIMIT - 1
            yield pending[:cut + 1].strip()
            pending = pending[cut + 1:].lstrip()
    if pending.strip():
        yield pending.strip()


def create_engine():
    mode = os.environ.get("VOICE_ENGINE", "mlx")
    if mode == "mlx":
        return SpeechEngine()
    if mode == "speech_cpp":
        from .speech_cpp import SpeechCppEngine

        return SpeechCppEngine()
    raise RuntimeError(f"Unsupported VOICE_ENGINE: {mode}")


class SpeechEngine:
    variants = {
        "custom_voice": MODEL,
        "base": ROOT / "assets/base",
        "voice_design": ROOT / "assets/design",
    }
    sample_rate = 24000

    def __init__(self):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        import mlx.core as mx
        import numpy as np
        from mlx_audio.tts.utils import load_model

        self.mx = mx
        self.np = np
        self.load_model = load_model
        self.ffmpeg = os.environ.get("VOICE_FFMPEG") or shutil.which("ffmpeg")
        if not self.ffmpeg or not Path(self.ffmpeg).is_file():
            raise RuntimeError("FFmpeg is required")
        self.model = None
        self.mode = None
        self.private_voices = all(
            (self.variants[variant] / filename).is_file()
            for variant in ("base", "voice_design")
            for filename in ("model.safetensors", "speech_tokenizer/model.safetensors")
        )
        model = self.model_for("custom_voice")
        if not VOICE_NAMES.keys() <= {speaker.lower() for speaker in model.supported_speakers}:
            raise RuntimeError("Voice presets do not match the installed model")

    def model_for(self, mode):
        if self.mode == mode:
            return self.model
        self.model = None
        self.mode = None
        gc.collect()
        self.mx.clear_cache()
        directory = self.variants[mode]
        for filename in ("model.safetensors", "speech_tokenizer/model.safetensors"):
            if not (directory / filename).is_file():
                raise RuntimeError(f"Model asset missing: {mode}/{filename}")
        model = self.load_model(str(directory))
        if model.config.tts_model_type != mode or model.sample_rate != self.sample_rate:
            raise RuntimeError(f"Unexpected Qwen3-TTS model configuration: {mode}")
        if mode == "base" and not model.speech_tokenizer.has_encoder:
            raise RuntimeError("Qwen3-TTS Base is missing its reference encoder")
        self.model = model
        self.mode = mode
        return model

    def synthesize(self, text, voice):
        model = self.model_for("custom_voice")
        generator = partial(
            model.generate_custom_voice, speaker=VOICE_NAMES[voice][0],
            language="auto", instruct=STYLE, temperature=0.75, top_p=0.89,
        )
        pcm = self.render(text, generator)
        return self.encode(pcm)

    def synthesize_reference(self, text, reference):
        model = self.model_for("base")
        waveform = self.np.frombuffer(reference.pcm, dtype="<i2").astype(self.np.float32) / 32768
        generator = partial(
            model.generate, ref_audio=self.mx.array(waveform),
            ref_text=reference.text, lang_code="auto", temperature=0.72, top_p=0.9,
        )
        try:
            return self.encode(self.render(text, generator))
        finally:
            model._icl_cache.clear()

    def preview_recording(self, raw, container):
        from .voices import Candidate, MAX_RECORD_SECONDS, PREVIEW_SCRIPT, RECORD_SCRIPT, Reference, VoiceResult

        try:
            decoded = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-protocol_whitelist", "pipe", "-f", container, "-i", "pipe:0",
                 "-vn", "-t", str(MAX_RECORD_SECONDS + 1), "-ac", "1",
                 "-ar", str(self.sample_rate), "-f", "s16le", "pipe:1"],
                input=raw, capture_output=True, check=True, timeout=10,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GenerationError("录音无法读取，请用浏览器重新录一段。") from exc
        duration = len(decoded) / (self.sample_rate * 2)
        if not 3 <= duration <= MAX_RECORD_SECONDS + 0.5:
            raise GenerationError(f"录音时长需在 3 到 {MAX_RECORD_SECONDS} 秒之间。")
        wave = self.np.frombuffer(decoded, dtype="<i2").astype(self.np.float32) / 32768
        audible = self.trim(wave)
        if len(audible) < self.sample_rate * 2:
            raise GenerationError("录到的说话声太短，请读完整段台词后重试。")
        reference = Reference(self.to_pcm(audible), RECORD_SCRIPT)
        audio, seconds = self.synthesize_reference(PREVIEW_SCRIPT, reference)
        return VoiceResult(audio, seconds, Candidate("record", reference))

    def preview_design(self, description, source):
        from .voices import Candidate, PREVIEW_SCRIPT, Reference, VoiceResult

        model = self.model_for("voice_design")
        generator = partial(
            model.generate_voice_design, instruct=description, language="Chinese",
            temperature=0.78, top_p=0.9,
        )
        pcm = self.render(PREVIEW_SCRIPT, generator)
        if len(pcm) < self.sample_rate * 4:
            raise GenerationError("样音太短，请换一个描述后再生成。")
        audio, seconds = self.encode(pcm)
        return VoiceResult(audio, seconds, Candidate(source, Reference(pcm, PREVIEW_SCRIPT), description))

    def trim(self, wave):
        np = self.np
        if wave.size < 240 or not np.all(np.isfinite(wave)):
            raise GenerationError("生成的音频有误，请重试。")
        frames = wave[:len(wave) // 240 * 240].reshape(-1, 240)
        activity = np.sqrt(np.mean(frames**2, axis=1))
        active = np.flatnonzero(activity > max(float(activity.max()) * 0.02, 0.0022))
        if not active.size:
            raise GenerationError("未检测到可听语音，请重试。")
        return wave[max(0, (int(active[0]) - 5) * 240):(int(active[-1]) + 12) * 240]

    def to_pcm(self, waveform):
        return (self.np.clip(waveform, -0.999, 0.999) * 32767).astype("<i2").tobytes()

    def render(self, text, generate):
        np = self.np
        waves = []
        for segment in segments(text):
            if not any(char.isalnum() for char in segment):
                continue
            parts = []
            for result in generate(text=segment, max_tokens=MAX_AUDIO_TOKENS):
                if result.token_count >= MAX_AUDIO_TOKENS:
                    raise GenerationError("有一段文字未能完整读完。请缩短句子或增加标点后重试。")
                parts.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
            if not parts:
                raise GenerationError("这段文字未生成语音，请调整文稿后重试。")
            if waves:
                waves.append(np.zeros(round(self.sample_rate * 0.12), dtype=np.float32))
            waves.append(self.trim(np.concatenate(parts)))
        if not waves:
            raise GenerationError("文稿需要包含可以朗读的文字。")
        return self.to_pcm(np.concatenate(waves))

    def encode(self, pcm):
        encoded = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "s16le", "-ar", str(self.sample_rate), "-ac", "1", "-i", "pipe:0",
             "-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1"],
            input=pcm, capture_output=True, check=True, timeout=15,
        )
        if not encoded.stdout:
            raise GenerationError("MP3 编码失败，请重试。")
        return encoded.stdout, round(len(pcm) / (2 * self.sample_rate), 2)
