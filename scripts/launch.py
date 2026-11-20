"""Manage the local worker and its HTTPS entrances."""
import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = ROOT / ".runtime/deploy.json"
CONFIG = json.loads(LOCAL_CONFIG.read_text()) if LOCAL_CONFIG.is_file() else {}
LEGACY_DEPLOY = Path.home() / "Library/Application Support/NianGao"
DEPLOY = Path(CONFIG.get("directory", str(
    LEGACY_DEPLOY if LEGACY_DEPLOY.is_dir() else Path.home() / "Library/Application Support/Nianbai"
))).expanduser()
RUNTIME = DEPLOY / ".runtime"
PROJECT_ACCESS = ROOT / ".runtime/access.json"
SERVICE = CONFIG.get("label", "io.github.niangao.tts" if DEPLOY == LEGACY_DEPLOY else "io.github.nianbai.tts")
PREVIEW = SERVICE + ".preview"
FIXED = SERVICE + ".tunnel"
DOMAIN = os.environ.get("VOICE_PUBLIC_HOST") or CONFIG.get("public_host", "")


def call(*args, check=True, **kwargs):
    return subprocess.run(["launchctl", *args], check=check, **kwargs)


def path_for(label):
    return RUNTIME / f"{label}.plist"


def target_for(label):
    return f"gui/{os.getuid()}/{label}"


def is_loaded(label):
    return call("print", target_for(label), check=False, capture_output=True).returncode == 0


def load(label):
    call("bootstrap", f"gui/{os.getuid()}", str(path_for(label)))


def unload(label):
    if is_loaded(label):
        call("bootout", target_for(label))


def make_plist(label, command, keep_alive, environment=None):
    config = {
        "Label": label,
        "ProgramArguments": command,
        "WorkingDirectory": str(DEPLOY),
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
        "ThrottleInterval": 15,
        "StandardOutPath": str(RUNTIME / f"{label}.log"),
        "StandardErrorPath": str(RUNTIME / f"{label}.log"),
        "Umask": 0o077,
    }
    if environment:
        config["EnvironmentVariables"] = environment
    path_for(label).write_bytes(plistlib.dumps(config))


def sync_access():
    if not PROJECT_ACCESS.is_file() or PROJECT_ACCESS.stat().st_mode & 0o077:
        raise SystemExit("请先运行 scripts/setup 并确认项目凭据文件权限为 600。")
    temporary = RUNTIME / "access.next.json"
    shutil.copy2(PROJECT_ACCESS, temporary)
    temporary.replace(RUNTIME / "access.json")


def install():
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    registration = directory / f"{SERVICE}.plist"
    expected = path_for(SERVICE)
    previous = ROOT / ".runtime" / f"{SERVICE}.plist"
    if registration.is_symlink() and registration.resolve() == previous:
        unload(SERVICE)
        registration.unlink()
    if registration.exists() or registration.is_symlink():
        if not registration.is_symlink() or registration.resolve() != expected:
            raise SystemExit(f"已有其他 LaunchAgent，请检查：{registration}")

    DEPLOY.mkdir(mode=0o700, exist_ok=True)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    unload(SERVICE)
    sync_access()
    subprocess.run(
        ["/usr/bin/rsync", "-aH", "--exclude=__pycache__", "--exclude=.cache", "--exclude=*.partial",
         str(ROOT / "voice_service"), str(ROOT / "assets"), str(ROOT / "requirements.lock"),
         str(ROOT / ".python-version"), str(DEPLOY)],
        check=True,
    )
    (DEPLOY / "scripts").mkdir(exist_ok=True)
    for name in ("setup", "start", "setup_models.py"):
        shutil.copy2(ROOT / "scripts" / name, DEPLOY / "scripts" / name)
    env = dict(os.environ)
    env["PATH"] = f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:" + env["PATH"]
    subprocess.run([str(DEPLOY / "scripts/setup")], cwd=DEPLOY, check=True, env=env)
    environment = {"VOICE_PUBLIC_HOST": DOMAIN} if DOMAIN else None
    make_plist(SERVICE, [str(DEPLOY / "scripts/start")], True, environment)
    if not registration.is_symlink():
        registration.symlink_to(expected)
    load(SERVICE)


def restart():
    sync_access()
    call("kickstart", "-k", target_for(SERVICE))


def cloudflared_path():
    path = shutil.which("cloudflared", path="/opt/homebrew/bin:/usr/local/bin:" + os.environ["PATH"])
    if not path:
        raise SystemExit("找不到 cloudflared。")
    return path


def fixed_install():
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", DOMAIN):
        raise SystemExit("请用 VOICE_PUBLIC_HOST 或 .runtime/deploy.json 配置公网域名。")
    if not is_loaded(SERVICE):
        raise SystemExit("请先启动本地服务。")
    source = ROOT / ".runtime/fixed-tunnel-credentials.json"
    if not source.is_file() or source.stat().st_mode & 0o077:
        raise SystemExit("缺少本机受控的 Cloudflare 隧道凭据。")
    tunnel_id = json.loads(source.read_text())["TunnelID"]
    if not re.fullmatch(r"[0-9a-f-]{36}", tunnel_id):
        raise RuntimeError("Unexpected tunnel ID")
    path = cloudflared_path()
    target = RUNTIME / "fixed-tunnel-credentials.json"
    pending = target.with_suffix(".next")
    shutil.copyfile(source, pending)
    pending.chmod(0o600)
    pending.replace(target)
    config = RUNTIME / "fixed-tunnel.yml"
    config.write_text(
        f"tunnel: {tunnel_id}\n"
        f"credentials-file: {json.dumps(str(target), ensure_ascii=False)}\n"
        f"ingress:\n  - hostname: {DOMAIN}\n    service: http://127.0.0.1:8796\n"
        "  - service: http_status:404\n"
    )
    config.chmod(0o600)
    subprocess.run([path, "tunnel", "--config", str(config), "ingress", "validate"], check=True)
    registration = Path.home() / "Library/LaunchAgents" / f"{FIXED}.plist"
    expected = path_for(FIXED)
    if registration.exists() or registration.is_symlink():
        if not registration.is_symlink() or registration.resolve() != expected:
            raise SystemExit(f"已有其他 LaunchAgent，请检查：{registration}")
    unload(FIXED)
    make_plist(FIXED, [path, "tunnel", "--config", str(config), "--no-autoupdate",
                       "--protocol", "http2", "run", tunnel_id], True)
    if not registration.is_symlink():
        registration.symlink_to(expected)
    call("enable", target_for(FIXED))
    load(FIXED)


def preview_start():
    if not is_loaded(SERVICE):
        raise SystemExit("请先启动本地服务。")
    path = cloudflared_path()
    if is_loaded(PREVIEW):
        raise SystemExit("预览已注册，请先检查 preview-status 或执行 preview-stop。")
    log = RUNTIME / f"{PREVIEW}.log"
    if log.exists():
        log.replace(RUNTIME / f"{PREVIEW}.previous.log")
    make_plist(PREVIEW, [path, "tunnel", "--config", "/dev/null", "--protocol", "http2",
                         "--url", "http://127.0.0.1:8796"], False)
    load(PREVIEW)


def preview_status():
    result = call("print", target_for(PREVIEW), capture_output=True, text=True, check=False)
    log_file = RUNTIME / f"{PREVIEW}.log"
    if result.returncode or not re.search(r"\bstate = running\b", result.stdout):
        raise SystemExit(f"预览未运行。日志：{log_file}")
    text = log_file.read_text() if log_file.exists() else ""
    urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
    if not urls:
        raise SystemExit(f"预览地址尚未生成。日志：{log_file}")
    print(urls[-1])


def main():
    parser = argparse.ArgumentParser()
    commands = {
        "install": install,
        "restart": restart,
        "status": lambda: call("print", target_for(SERVICE)),
        "stop": lambda: unload(SERVICE),
        "fixed-install": fixed_install,
        "fixed-status": lambda: call("print", target_for(FIXED)),
        "fixed-stop": lambda: unload(FIXED),
        "preview-start": preview_start,
        "preview-status": preview_status,
        "preview-stop": lambda: unload(PREVIEW),
    }
    parser.add_argument("command", choices=commands)
    args = parser.parse_args()
    commands[args.command]()


if __name__ == "__main__":
    main()
