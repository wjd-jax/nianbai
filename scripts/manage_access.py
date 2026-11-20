"""Keep invitation codes and API keys on this host."""
import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice_service.access import ACCESS_FILE, read_access, write_access


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("add", "rotate", "revoke", "export"))
    parser.add_argument("kind", choices=("web", "api"))
    parser.add_argument("name")
    args = parser.parse_args()
    if not args.name.isascii() or not args.name.replace("-", "").isalnum() or len(args.name) > 40:
        parser.error("名称仅允许英文字母、数字、连字符，最多 40 个字符。")
    data = read_access()
    entries = data[args.kind]
    exists = args.name in entries
    if args.action == "add" and exists:
        parser.error("名称已存在。")
    if args.action != "add" and not exists:
        parser.error("找不到该名称。")
    if args.action == "export":
        path = ACCESS_FILE.parent / f"{args.kind}-{args.name}.txt"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            parser.error("该凭据已导出。请先移走旧的导出文件。")
        with os.fdopen(fd, "w") as file:
            file.write(entries[args.name] + "\n")
        print(path)
        return
    if args.action == "revoke":
        del entries[args.name]
    else:
        prefix, size = ("inv_", 24) if args.kind == "web" else ("tts_", 32)
        entries[args.name] = prefix + secrets.token_urlsafe(size)
    write_access(data)
    print("已保存。执行 .venv/bin/python scripts/launch.py restart 使凭据生效。")


if __name__ == "__main__":
    main()
