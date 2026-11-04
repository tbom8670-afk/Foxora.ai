# -*- coding: utf-8 -*-
"""⚖️ ذخیره‌سازی مستقل «دادگاه روباه» — همه‌چیز داخل خودِ پوشهٔ پروژه.

قواعدی که این ماژول تضمین می‌کند:

* **مسیر قطعی و پروژه‌محور.** ریشه از روی محل واقعی همین فایل محاسبه
  می‌شود (``Path(__file__).resolve()``)، پس اجرای ربات از هر دایرکتوری
  (Termux، home، cron، systemd) هرگز محل ذخیره را عوض نمی‌کند::

      <project>/data/court_game/
          cases/      یک فایل JSON برای هر پرونده (با شناسهٔ یکتا)
          sessions/   وضعیت بازی‌های فعال هر گروه (برای بازیابی بعد از ری‌استارت)
          ai_logs/    گزارش تحلیل‌های هوش مصنوعی (JSONL به‌ازای هر پرونده)
          results/    آرشیو نتیجهٔ نهایی هر دادگاه
          config/     تنظیمات اختصاصی بازی

* **هیچ داده‌ای بیرون از پروژه نوشته نمی‌شود** — نه در ``$HOME``، نه در
  ``/tmp`` و نه در مسیرهای عمومی Termux. تنها استثنا یک override صریح
  با متغیر محیطی ``COURT_GAME_DATA_DIR`` است (برای تست/استقرار دلخواه).
* **نوشتن اتمیک.** همهٔ فایل‌های JSON با ``modules.atomic_write`` نوشته
  می‌شوند (فایل موقت در همان پوشه + ``os.replace`` + fsync)، پس قطع
  برق یا kill شدن پروسه فایل نیمه‌کاره باقی نمی‌گذارد.
* **هر پرونده یک فایل.** دو بازی هم‌زمان در دو گروه هرگز روی یک فایل
  نمی‌نویسند؛ کلید فایل، شناسهٔ یکتای پرونده است.
* **شناسهٔ یکتا.** ``new_case_id()`` زمان + شمارنده + تصادف امن است و
  وجود فایل را هم بررسی می‌کند تا هیچ پرونده‌ای روی پروندهٔ قبلی نیفتد.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from modules.atomic_write import write_json

# ---------------------------------------------------------------------------
# مسیرها
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = PROJECT_ROOT / "data" / "court_game"

_OVERRIDE_ENV = "COURT_GAME_DATA_DIR"
_override_base = None  # فقط برای تست/استقرار صریح

WRITE_LOCK = threading.RLock()

CASE_STATUS_OPEN = "open"
CASE_STATUS_JUDGING = "judging"
CASE_STATUS_CLOSED = "closed"
CASE_STATUS_CANCELLED = "cancelled"
CASE_STATUS_INTERRUPTED = "interrupted"


def base_dir() -> Path:
    """ریشهٔ داده‌های بازی (همیشه داخل پروژه، مگر override صریح)."""
    if _override_base is not None:
        return Path(_override_base)
    raw = (os.environ.get(_OVERRIDE_ENV, "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_BASE


def set_base_dir(path=None):
    """تعیین ریشهٔ داده (تست). ``None`` یعنی برگشت به مسیر پیش‌فرض پروژه."""
    global _override_base
    _override_base = None if path is None else Path(path).expanduser().resolve()
    if _override_base is not None:
        ensure_dirs()


def cases_dir() -> Path:
    return base_dir() / "cases"


def sessions_dir() -> Path:
    return base_dir() / "sessions"


def ai_logs_dir() -> Path:
    return base_dir() / "ai_logs"


def results_dir() -> Path:
    return base_dir() / "results"


def config_dir() -> Path:
    return base_dir() / "config"


def ensure_dirs():
    """ساخت پوشه‌های لازم؛ خطای دیسک هرگز بازی را از کار نمی‌اندازد."""
    for directory in (cases_dir(), sessions_dir(), ai_logs_dir(),
                      results_dir(), config_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def describe() -> dict:
    return {
        "base": str(base_dir()),
        "cases": str(cases_dir()),
        "sessions": str(sessions_dir()),
        "ai_logs": str(ai_logs_dir()),
        "results": str(results_dir()),
        "config": str(config_dir()),
        "inside_project": _is_inside_project(base_dir()),
    }


def _is_inside_project(path) -> bool:
    try:
        Path(path).resolve().relative_to(PROJECT_ROOT)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "join_seconds": 60,
    "step_seconds": 120,
    "punishment_seconds": 60,
    "ai_timeout_seconds": 75,
    "ai_retries": 1,
}


def settings() -> dict:
    """تنظیمات پیش‌فرض + مقادیر ``config/settings.json``.

    فایل خراب یا غایب ⇒ پیش‌فرض‌ها (بازی هرگز به‌خاطر تنظیمات نمی‌خوابد).
    """
    values = dict(DEFAULT_SETTINGS)
    path = config_dir() / "settings.json"
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            for key, value in data.items():
                if key not in values:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if value > 0:
                    values[key] = value
    except (OSError, ValueError):
        pass
    return values


def write_default_settings(force=False):
    path = config_dir() / "settings.json"
    if path.exists() and not force:
        return path
    return write_json(path, DEFAULT_SETTINGS, indent=2)


# ---------------------------------------------------------------------------
# شناسه‌های یکتا
# ---------------------------------------------------------------------------
_SEQUENCE = [0]
_SEQUENCE_LOCK = threading.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def is_safe_case_id(case_id) -> bool:
    return bool(case_id) and bool(_SAFE_ID.match(str(case_id)))


def new_case_id(now=None) -> str:
    """شناسهٔ یکتای پرونده؛ تضمین عدم برخورد با فایل‌های موجود."""
    stamp = datetime.fromtimestamp(
        time.time() if now is None else now, tz=timezone.utc
    ).strftime("%Y%m%d-%H%M%S")
    for _ in range(50):
        with _SEQUENCE_LOCK:
            _SEQUENCE[0] = (_SEQUENCE[0] + 1) % 100000
            sequence = _SEQUENCE[0]
        candidate = "COURT-%s-%03d-%s" % (
            stamp, sequence, secrets.token_hex(2).upper(),
        )
        if not (cases_dir() / ("%s.json" % candidate)).exists():
            return candidate
    return "COURT-%s-%s" % (stamp, secrets.token_hex(4).upper())


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# پرونده‌ها
# ---------------------------------------------------------------------------
def case_path(case_id) -> Path:
    if not is_safe_case_id(case_id):
        raise ValueError("unsafe case id: %r" % (case_id,))
    return cases_dir() / ("%s.json" % case_id)


def write_case(case) -> Path:
    """نوشتن اتمیک پرونده؛ فقط داخل پوشهٔ cases."""
    ensure_dirs()
    case_id = (case or {}).get("case_id")
    with WRITE_LOCK:
        return write_json(case_path(case_id), case, indent=2)


def read_case(case_id):
    try:
        with open(case_path(case_id), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_cases():
    ensure_dirs()
    found = []
    try:
        for path in sorted(cases_dir().glob("*.json")):
            found.append(path.stem)
    except OSError:
        return []
    return found


# ---------------------------------------------------------------------------
# session‌های فعال (بازیابی بعد از ری‌استارت)
# ---------------------------------------------------------------------------
def chat_key_for(chat_id) -> str:
    text = str(chat_id).strip().replace("-", "m").replace("/", "_")
    return re.sub(r"[^A-Za-z0-9_]", "", text)[:40] or "chat"


def session_path(chat_key) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(chat_key))[:60] or "chat"
    return sessions_dir() / ("session_%s.json" % safe)


def write_session(chat_key, data) -> None:
    ensure_dirs()
    with WRITE_LOCK:
        write_json(session_path(chat_key), data, indent=2)


def delete_session(chat_key) -> None:
    try:
        session_path(chat_key).unlink()
    except OSError:
        pass


def read_session(chat_key):
    """خواندن session ذخیره‌شدهٔ یک گروه (برای بازیابی ثبت‌نام)."""
    try:
        with open(session_path(chat_key), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_sessions():
    ensure_dirs()
    rows = []
    try:
        paths = sorted(sessions_dir().glob("session_*.json"))
    except OSError:
        return rows
    for path in paths:
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("_file", path.name)
            rows.append(data)
    return rows


# ---------------------------------------------------------------------------
# گزارش هوش مصنوعی و نتایج
# ---------------------------------------------------------------------------
_AI_LOG_LOCK = threading.RLock()


def append_ai_log(case_id, record) -> None:
    """افزودن یک رکورد JSON به گزارش تحلیل (فقط append، هر پرونده یک فایل)."""
    if not is_safe_case_id(case_id):
        return
    ensure_dirs()
    line = json.dumps(record, ensure_ascii=False)
    with _AI_LOG_LOCK:
        try:
            with open(ai_logs_dir() / ("%s.jsonl" % case_id),
                      "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass


def read_ai_logs(case_id):
    if not is_safe_case_id(case_id):
        return []
    rows = []
    try:
        with open(ai_logs_dir() / ("%s.jsonl" % case_id),
                  encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def write_result(case_id, data) -> None:
    if not is_safe_case_id(case_id):
        return
    ensure_dirs()
    with WRITE_LOCK:
        write_json(results_dir() / ("%s.json" % case_id), data, indent=2)


def read_result(case_id):
    if not is_safe_case_id(case_id):
        return None
    try:
        with open(results_dir() / ("%s.json" % case_id),
                  encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_results():
    ensure_dirs()
    try:
        return sorted(path.stem for path in results_dir().glob("*.json"))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# بازیابی پس از قطع اجرا / ری‌استارت
# ---------------------------------------------------------------------------
def recover(now=None):
    """وضعیت پرونده‌های نیمه‌کاره را قابل‌بازیابی و بی‌خطر می‌کند.

    * session مربوط به **ثبت‌نام** (پرونده هنوز تشکیل نشده) حذف نمی‌شود؛
      دست‌نخورده می‌ماند تا «شرکت» بعدی همان پرونده را ادامه دهد و
      شرکت‌کننده‌های قبلی گم نشوند. (باگ قبلی: همین پاک‌سازی باعث می‌شد
      بعد از هر ری‌استارت، شمارش ثبت‌نام از «۱ از ۵» شروع شود.)
    * session مربوط به پروندهٔ **تشکیل‌شده** «interrupted» علامت می‌خورد،
      در ``results/`` آرشیو می‌شود و فایل session پاک می‌شود تا بازی بعدی
      تداخل نکند. طبق سیاست بازی، پروندهٔ نیمه‌کاره **هیچ جایزه‌ای** ندارد.

    خروجی: خلاصهٔ کارهای انجام‌شده (برای لاگ و تست).
    """
    summary = {"sessions": 0, "cases": 0, "punishments": 0,
               "resumable": 0, "details": []}
    for row in list_sessions():
        chat_key = row.get("chat_key")
        case_id = row.get("case_id")
        detail = {
            "chat_key": chat_key,
            "case_id": case_id,
            "kind": row.get("kind", "case"),
            "phase": row.get("phase"),
        }
        summary["sessions"] += 1
        if (row.get("kind") != "punishment"
                and not case_id
                and row.get("phase") == "joining"
                and row.get("players")):
            # پروندهٔ هنوز تشکیل‌نشده: ثبت‌نام ادامه‌پذیر می‌ماند.
            summary["resumable"] += 1
            detail["action"] = "kept_resumable"
            summary["details"].append(detail)
            continue
        if row.get("kind") == "punishment":
            summary["punishments"] += 1
        else:
            summary["cases"] += 1
            if case_id:
                case = read_case(case_id) or {}
                case["status"] = CASE_STATUS_INTERRUPTED
                case["interrupted_at"] = now_iso()
                case["payment"] = {
                    "status": "skipped",
                    "reason": "interrupted",
                }
                try:
                    write_case(case)
                except Exception:
                    pass
                try:
                    write_result(case_id, {
                        "case_id": case_id,
                        "status": CASE_STATUS_INTERRUPTED,
                        "verdict": None,
                        "payment": {"status": "skipped",
                                    "reason": "interrupted"},
                        "recovered_at": now_iso(),
                    })
                except Exception:
                    pass
        detail["action"] = "marked_interrupted"
        summary["details"].append(detail)
        if chat_key:
            delete_session(chat_key)
    return summary
