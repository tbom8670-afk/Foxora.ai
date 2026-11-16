"""Conservative Linux /proc-based control of this checkout's Foxora processes.

PID files are hints, not proof of identity. Never signal if identity cannot be
verified. No lock or PID file is removed by this controller.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import select
import signal
import sys
import time

from modules import runtime_paths
from modules.runtime_paths import CONFIG_DIR, DATA_DIR, PROJECT_ROOT


def read_record(path: Path, watchdog: bool = False):
    try:
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if watchdog else {"pid": int(raw)}
        if isinstance(data, int):
            data = {"pid": data}
        pid = int(data["pid"])
        if pid <= 1:
            return None, None
        return pid, data.get("start_ticks")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None, None


def process_info(pid: int):
    try:
        proc = Path(f"/proc/{pid}")
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        args = (proc / "cmdline").read_bytes().split(b"\0")
        env = (proc / "environ").read_bytes().split(b"\0")
        cwd = (proc / "cwd").resolve(strict=True)
        return stat[19], args, env, cwd
    except FileNotFoundError as error:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        raise RuntimeError(f"cannot verify live pid={pid}: /proc information unavailable") from error
    except (OSError, IndexError, ValueError) as error:
        raise RuntimeError(f"cannot verify pid={pid}: {error}") from error


def verified(pid: int, kind: str, expected_start=None):
    info = process_info(pid)
    if info is None:
        return False
    start, args, env, cwd = info
    if expected_start and str(expected_start) != start:
        return False
    if cwd != PROJECT_ROOT.resolve():
        return False
    instances = [v.split(b"=", 1)[1] for v in env if v.startswith(b"BOT_INSTANCE=")]
    if instances and instances != [b"foxora"]:
        return False
    # Direct Python entrypoints can obtain foxora from the checkout marker
    # after exec, so it may not appear in /proc's original environment.
    if not instances:
        try:
            if (PROJECT_ROOT / ".bot_instance").read_text().strip() != "foxora":
                return False
        except OSError:
            return False
    # Recompute the same runtime path from the process's original environment
    # and this checkout's dotenv. This prevents a stale copied PID record from
    # controlling a Foxora process using a different data directory.
    original_env = os.environ.copy()
    try:
        process_env = {}
        for entry in env:
            if b"=" in entry:
                key, value = entry.split(b"=", 1)
                process_env[os.fsdecode(key)] = os.fsdecode(value)
        os.environ.clear()
        os.environ.update(process_env)
        os.environ["BOT_INSTANCE"] = "foxora"
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        if runtime_paths._chosen_data_dir() != DATA_DIR:
            return False
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    # Inspect only the Python script argument, never arbitrary later arguments.
    try:
        executable = (Path(f"/proc/{pid}") / "exe").resolve(strict=True).name
    except OSError:
        return False
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", executable):
        return False
    decoded = [os.fsdecode(x) for x in args if x]
    if len(decoded) < 2:
        return False
    index = 1
    while index < len(decoded) and decoded[index] in ("-u", "-B", "-E", "-s"):
        index += 1
    if index >= len(decoded):
        return False
    script = Path(decoded[index])
    if not script.is_absolute():
        script = cwd / script
    allowed = {"watchdog.py"} if kind == "watchdog" else {"main.py", "foxora.py"}
    return any(script.resolve() == (PROJECT_ROOT / name).resolve() for name in allowed)


def lock_busy(path: Path):
    # Only used to fail closed if a flock is held while its PID record is empty.
    if not path.exists():
        return False
    try:
        import fcntl
        with path.open("r") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
    except (ImportError, OSError) as error:
        raise RuntimeError(f"cannot inspect lock {path}: {error}") from error


def stop_one(path: Path, kind: str, timeout: float):
    pid, start = read_record(path, kind == "watchdog")
    if pid is None:
        if lock_busy(path):
            raise RuntimeError(f"{kind} lock is held but its PID is unavailable")
        return False
    if process_info(pid) is None:
        if lock_busy(path):
            raise RuntimeError(f"{kind} lock is held but its PID is stale")
        return False
    # Linux pidfds prevent PID reuse between verification and signal delivery.
    # If unavailable, fail closed rather than risk signaling another process.
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("pidfd is unavailable; refusing unsafe PID signaling")
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        if not verified(pid, kind, start):
            if process_info(pid) is None:
                return False
            raise RuntimeError(f"pid={pid} is not verified as this Foxora {kind}; no signal sent")
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + timeout
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if process_info(pid) is None or poller.poll(0):
                    break
                raise RuntimeError(f"timeout waiting for {kind} pid={pid}; lock files unchanged")
            if poller.poll(max(1, int(remaining * 1000))):
                break
        print(f"Stopped Foxora {kind} (pid={pid})")
        return True
    finally:
        os.close(fd)


def main():
    command = sys.argv[1]
    watchdog_lock = CONFIG_DIR / "watchdog.lock"
    bot_lock = CONFIG_DIR / "bot.pid"
    try:
        if command == "status":
            print(f"FOXORA_DIR  = {PROJECT_ROOT}")
            print(f"DATA_DIR    = {DATA_DIR}")
            print(f"PID file    = {bot_lock}")
            pid, start = read_record(bot_lock)
            if pid and verified(pid, "bot", start):
                print(f"STATUS      = RUNNING (pid={pid})")
            elif lock_busy(bot_lock):
                print("STATUS      = UNKNOWN (bot lock held; owner not verified)")
                return 1
            else:
                print("STATUS      = STOPPED")
            return 0
        timeout = float(os.environ.get("FOXORA_STOP_TIMEOUT", "20"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("FOXORA_STOP_TIMEOUT must be positive and finite")
        stopped = stop_one(watchdog_lock, "watchdog", timeout)
        stopped = stop_one(bot_lock, "bot", timeout) or stopped
        if not stopped:
            print("No running Foxora process found")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Foxora {command} error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
