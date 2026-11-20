"""Download pinned Qwen3-TTS checkpoints and verify each asset against its manifest."""

import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
REVISION = {
    "model": ("CustomVoice", "1c6c0ff58c43afa8df571facde2efa077efd85e2"),
    "base": ("Base", "34ff5318365b59cba9c03ff729f2eee0814caf72"),
    "design": ("VoiceDesign", "ffc6545dc9cb086950aa46c6cd3db490e6ece3e1"),
}
TOKENIZER = Path("speech_tokenizer/model.safetensors")


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def entries(manifest, relative_to):
    for line in manifest.read_text().splitlines():
        expected, filename = line.split("  ", 1)
        yield relative_to / filename, expected


def prepare(private=False):
    files = list(entries(ASSETS / "model.sha256", Path("model")))
    if private:
        files.extend(entries(ASSETS / "private-models.sha256", Path(".")))

    verified = {}
    for relative, expected in files:
        folder, *parts = relative.parts
        filename = Path(*parts)
        path = ASSETS / relative
        if path.is_file():
            metadata = path.stat()
            key = (metadata.st_dev, metadata.st_ino)
            if key not in verified:
                verified[key] = sha256(path)
            if verified[key] == expected:
                continue
            raise SystemExit(f"SHA-256 校验失败，请移走损坏的文件后重新安装：{relative}")

        path.parent.mkdir(parents=True, exist_ok=True)
        if folder != "model" and filename == TOKENIZER:
            source = ASSETS / "model" / TOKENIZER
            try:
                os.link(source, path)
            except OSError:
                shutil.copyfile(source, path)
            continue

        variant, revision = REVISION[folder]
        url = f"https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-{variant}-6bit/resolve/{revision}/{filename.as_posix()}"
        partial = path.with_name(path.name + ".partial")
        print(f"下载 {relative}", flush=True)
        subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "15",
                        "--max-time", "1800", "--retry", "2", url, "-o", str(partial)], check=True)
        if sha256(partial) != expected:
            raise SystemExit(f"SHA-256 校验失败：{relative}")
        partial.replace(path)
    print(f"模型文件已通过校验，共 {len(files)} 项。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-private-voices", action="store_true", help="同时安装 Base 和 VoiceDesign")
    args = parser.parse_args()
    prepare(args.with_private_voices)


if __name__ == "__main__":
    main()
