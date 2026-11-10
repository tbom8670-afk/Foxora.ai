# -*- coding: utf-8 -*-
"""🔐 «تأیید دو مرحله‌ای دستورات خطرناک» — هستهٔ منطق و ذخیره‌سازی.

خلاصهٔ رفتار (طبق مشخصاتِ کارفرما):

* رمز **داخل همان گروه** نوشته می‌شود و دفاعِ اصلی «هویتِ فرستنده» است:
  فقط ``access.is_owner(chat_id, user_id)`` (مالکِ ثبت‌شدهٔ گروه، یا مالکِ
  سراسری وقتی گروه مالکِ ثبت‌شده ندارد) می‌تواند فرایند را شروع کند و رمز
  را بپذیرد. هر کسِ دیگری — حتی اگر رمز را دیده باشد — هیچ اثری ندارد.
* دستورهای محافظت‌شده، وقتی قفل فعال است، **فقط** برای مالک و **فقط** با
  رمز اجرا می‌شوند؛ ادمین‌های ثبت‌شده در این حالت رد می‌شوند.
* تنظیم و لغوِ رمز فقط با فرمانِ ``دو مرحله ای`` / ``لغو دو مرحله ای`` از مالک
  انجام می‌شود (در دو گام: رمز، بعد تکرارش).

نکاتِ پیاده‌سازیِ مهم:

* رمز هیچ‌جا **نگه داشته یا لاگ نمی‌شود**؛ فقط ``hash`` + ``salt`` ذخیره
  می‌شود. برای هر گروه salt یکتای تصادفی و KDFِ ``scrypt`` (یا اگر در دسترس
  نبود ``pbkdf2_hmac``) با هزینهٔ بالا. مقایسه با ``hmac.compare_digest``.
  یک «فلفل» سروری هم اختیاری از ``FOXORA_TWO_FACTOR_PEPPER`` خوانده می‌شود
  (اگر تنظیم نشده باشد، فقط warning در لاگِ خودِ ماژول نوشته می‌شود، نه رمز).
* ذخیره‌سازی: ``config/two_factor.json`` با همان الگوی ``storage.py``
  (اتمیک + ``.bak`` + کش mtime) و همهٔ تغییرها با ``update``.
* درخواست‌های در انتظار و حالت‌های «منتظر رمز» کاملاً در حافظه و با
  ``time.monotonic`` هستند (ری‌استارت آن‌ها را پاک می‌کند؛ عمدی است).
* شمارندهٔ شکست‌ها **ماندگار** است تا با ری‌استارت صفر نشود.

این ماژول هیچ RPC شبکه‌ای نمی‌زند (به‌جز حذفِ best-effort پیامِ رمز که از
مسیرِ ``peers`` انجام می‌شود) — هیچ ``coroutine`` آماده‌ای هم ذخیره نمی‌کند.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import unicodedata

from modules.manage_features import access, storage
from modules.manage_features.queue import PerGroupQueue
from modules.manage_features.rpc import call, log_info

#: صف مشترکِ «همان گروه» برای تغییرهای این قابلیت (طبق بند ۶ مشخصات).
QUEUE = PerGroupQueue("two_factor", max_pending_per_key=16,
                      max_pending_total=256, task_timeout=30.0)

PASSWORD_LENGTH = 8
#: TTL حالت‌های انتظارِ رمز (ثانیه، ساعت یکنواخت).
PENDING_TTL = 120.0
#: TTL «درخواست در انتظار» برای اجرای دستور.
REQUEST_TTL = 120.0
#: حداکثر رمزِ غلط برای یک درخواست (بعدش درخواست باطل می‌شود).
MAX_WRONG_PER_REQUEST = 3
#: آستانهٔ قفلِ موقت: ``MAX_WRONG_IN_WINDOW`` خطا در ``FAILURE_WINDOW`` ثانیه.
MAX_WRONG_IN_WINDOW = 5
FAILURE_WINDOW = 15 * 60
#: قفل موقتِ اولیه و سقفِ افزایشِ تدریجی.
LOCKOUT_BASE = 15 * 60
LOCKOUT_MAX = 4 * 60 * 60

#: رمزهای بدیهی که همیشه رد می‌شوند (بدون توجه به salt).
OBVIOUS_PASSWORDS = frozenset({
    "00000000", "11111111", "22222222", "33333333", "44444444",
    "55555555", "66666666", "77777777", "88888888", "99999999",
    "12345678", "87654321", "12341234", "11223344", "12121212",
    "12345679", "01234567", "76543210", "13579246", "24681357",
})

#: نگاشتِ ارقام فارسی/عربی به لاتین (رمز همیشه با همین شکل مقایسه می‌شود).
_DIGITS = {ord("۰") + i: str(i) for i in range(10)}
_DIGITS.update({ord("٠") + i: str(i) for i in range(10)})
_DIGITS.update({ord("۰") + i: str(i) for i in range(10)})

#: دستورهای خطرناکِ محافظت‌شده (یک ثابتِ واحد — بعد از نرمال‌سازی مقایسه
#: می‌شود، پس «پاکسازی کامل» با فاصلهٔ اضافه یا نویسهٔ عربی هم می‌گیرد).
PROTECTED_COMMANDS = (
    "پاکسازی کامل",
    "پاک لیست سکوت",
    "پاک لیست اخراج",
    "ریست آمار",
    "تغییر مجازات",
    "تغییر اخطار",
    "پاکسازی خودکار",
    "سرگرمی فعال",
    "لغو کلمات ممنوعه",
)

#: نام‌های هم‌معنا/شکل‌های دیگرِ همان دستور (طبق گفتهٔ کارفرما: «همهٔ شکل‌ها»).
#: هر کلید به یک دستورِ فهرستِ بالا نگاشت می‌شود، پس کارت هنوز همان ۹ خط را
#: نشان می‌دهد ولی هر دو شکل گیت می‌شوند.
PROTECTED_ALIASES = {
    "سرگرمی خاموش": "سرگرمی فعال",
    "پاکسازی خودکار پیام ها": "پاکسازی خودکار",
    "پاکسازی خودکار پیامها": "پاکسازی خودکار",
    "پاک لیست سکوت ها": "پاک لیست سکوت",
    "پاک لیست اخراج ها": "پاک لیست اخراج",
    "ریست آمار گروه": "ریست آمار",
    "لغو کلمات ممنوعه گروه": "لغو کلمات ممنوعه",
}

#: فرمان‌های خودِ قابلیت (کلمه‌به‌کلمه بولد و داخل نقل‌قول؛ یک جا نگه داشته
#: می‌شوند تا متنِ «لیست ادمینی» و روتر از یک منبع بخوانند).
COMMAND_ENABLE = "دو مرحله ای"
COMMAND_DISABLE = "لغو دو مرحله ای"

#: نامِ فایل ذخیره‌سازی (الگوی ``storage.Storage``).
STORAGE_FILE = "two_factor.json"

_TWO_FACTOR_STORAGE = None

#: حالت‌های انتظار: ``(group_key, owner_key)`` → dict
_AWAIT = {}
#: درخواست‌های در انتظار: ``group_key`` → dict (هر گروه/مالک فقط یکی)
_REQUESTS = {}
#: محافظت از دسترسی هم‌زمان (بدون asyncio؛ sync و ارزان).
import threading as _threading

_LOCK = _threading.RLock()


# ---------------------------------------------------------------------------
#  ذخیره‌سازی
# ---------------------------------------------------------------------------
def _storage_instance():
    """نمونهٔ ``Storage`` برای ``two_factor.json`` (تنبل ساخته می‌شود)."""
    global _TWO_FACTOR_STORAGE
    if _TWO_FACTOR_STORAGE is None:
        _TWO_FACTOR_STORAGE = storage.Storage(
            STORAGE_FILE, {"version": 1, "groups": {}})
    return _TWO_FACTOR_STORAGE


#: کشِ «کدام گروه قفل دارد» — مسیر گرم (هر پیام گروه) فقط یک عضویتِ set است.
_ENABLED_CACHE = {"stamp": -1.0, "groups": frozenset()}
#: عمرِ کش (ثانیه، ساعت یکنواخت). تغییر از پروسهٔ دیگر حداکثر این‌قدر دیر دیده
#: می‌شود؛ تغییرهای همین پروسه فوراً بی‌اعتبار می‌کنند.
ENABLED_CACHE_TTL = 3.0


def _invalidate_enabled_cache():
    _ENABLED_CACHE["stamp"] = -1.0


def enabled_groups() -> frozenset:
    """کلیدِ گروه‌های قفل‌دار (کش‌شده؛ فقط هر چند ثانیه از دیسک)."""
    stamp = _ENABLED_CACHE["stamp"]
    now = time.monotonic()
    if stamp >= 0 and now - stamp <= ENABLED_CACHE_TTL:
        return _ENABLED_CACHE["groups"]
    try:
        data = load()
        groups = frozenset(
            key for key, entry in (data.get("groups") or {}).items()
            if isinstance(entry, dict) and entry.get("enabled"))
    except Exception:                                 # noqa: BLE001
        groups = frozenset()
    _ENABLED_CACHE["groups"] = groups
    _ENABLED_CACHE["stamp"] = now
    return groups


def load():
    return _storage_instance().load()


def save(data):
    return _storage_instance().save(data)


def update(mutate):
    return _storage_instance().update(mutate)


def reset_all():
    """پاک‌سازی کامل داده و حالت‌های حافظه (تست/ری‌ست دستی)."""
    _storage_instance().reset()
    _invalidate_enabled_cache()
    with _LOCK:
        _AWAIT.clear()
        _REQUESTS.clear()


# ---------------------------------------------------------------------------
#  نرمال‌سازی و اعتبارسنجی
# ---------------------------------------------------------------------------
def normalize(text) -> str:
    """نرمال‌سازی پایه برای مقایسهٔ دستور (هم‌رفتار با روترِ ربات)."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def match_command(text):
    """اگر متن فرمانِ خودِ قابلیت بود، ``"enable"``/``"disable"`` وگرنه ``None``.

    «لغو دو مرحله ای» اول بررسی می‌شود چون «دو مرحله ای» زیررشتهٔ آن است.
    """
    value = normalize(text)
    if value == normalize(COMMAND_DISABLE):
        return "disable"
    if value == normalize(COMMAND_ENABLE):
        return "enable"
    return None


def to_latin_digits(text) -> str:
    """ارقام فارسی/عربی → لاتین (بدون تغییرِ نویسهٔ دیگر)."""
    return str(text or "").translate(_DIGITS)


def is_obvious(password) -> bool:
    """رمزهای بدیهی: همه یکسان یا دنبالهٔ صعودی/نزولیِ کامل."""
    value = to_latin_digits(password)
    if len(set(value)) <= 1:
        return True
    if value in OBVIOUS_PASSWORDS:
        return True
    # دنبالهٔ متوالیِ رو به بالا/پایین (۱۲۳۴۵۶۷۸ یا ۸۷۶۵۴۳۲۱ و مشابه)
    steps = [int(b) - int(a) for a, b in zip(value, value[1:])]
    return bool(steps) and (all(step == 1 for step in steps)
                            or all(step == -1 for step in steps))


def validate_password(raw):
    """``(password, reason)`` — ``password=None`` اگر نامعتبر بود.

    ``reason`` ∈ ``{"length", "not_digits", "obvious"}`` یا ``None``.
    تابع خالص است؛ هیچ‌جا چیزی ذخیره/لاگ نمی‌کند.
    """
    value = to_latin_digits(raw).strip()
    if len(value) != PASSWORD_LENGTH:
        return None, "length"
    if not value.isdigit():
        return None, "not_digits"
    if is_obvious(value):
        return None, "obvious"
    return value, None


# ---------------------------------------------------------------------------
#  KDF
# ---------------------------------------------------------------------------
def _pepper() -> bytes:
    """«فلفل» سروریِ اختیاری (هرگز لاگ نمی‌شود)."""
    return (os.environ.get("FOXORA_TWO_FACTOR_PEPPER") or "").encode("utf-8")


def _kdf_params() -> dict:
    """پارامترهای KDF. ``scrypt`` اگر در دسترس باشد وگرنه PBKDF2."""
    try:
        hashlib.scrypt(b"probe", salt=b"probe", n=2 ** 14, r=8, p=1,
                       dklen=32)
        return {"algo": "scrypt", "n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
    except (ValueError, MemoryError, AttributeError):     # pragma: no cover
        return {"algo": "pbkdf2_sha256", "iterations": 260000, "dklen": 32}


def _derive(password: str, salt: bytes, params: dict) -> bytes:
    secret = password.encode("utf-8")
    salt_bytes = salt
    pepper = _pepper()
    if pepper:
        # HMAC پیش از KDF: تغییرِ salt/افزودنِ فلفل هر دو اثر دارند.
        import hashlib as _hashlib
        secret = _hashlib.new("sha256", secret + pepper).digest()
        salt_bytes = salt + pepper[:16]
    if params.get("algo") == "scrypt":
        return hashlib.scrypt(secret, salt=salt_bytes, n=int(params["n"]),
                              r=int(params["r"]), p=int(params["p"]),
                              dklen=int(params["dklen"]))
    return hashlib.pbkdf2_hmac("sha256", secret, salt_bytes,
                               int(params.get("iterations", 260000)),
                               dklen=int(params.get("dklen", 32)))


def _hash_password(password: str, salt: bytes, params: dict) -> str:
    return _derive(password, salt, params).hex()


# ---------------------------------------------------------------------------
#  وضعیت گروه
# ---------------------------------------------------------------------------
def _group(data, chat_id, create=False):
    groups = data.setdefault("groups", {})
    key = storage.group_key(chat_id)
    if create:
        return groups.setdefault(key, {
            "enabled": False, "set_by": None, "salt": None, "hash": None,
            "kdf": None, "created_at": None, "updated_at": None,
            "failures": [], "lockout_until": 0.0,
        })
    return groups.get(key)


def status(chat_id):
    """``(enabled, entry)`` — خواندنِ ارزان از ``two_factor.json``."""
    entry = _group(load(), chat_id)
    return bool(entry and entry.get("enabled")), entry


def is_enabled(chat_id) -> bool:
    """آیا این گروه قفلِ دومرحله‌ای دارد؟ (ارزان و کش‌شده — مسیر گرم)"""
    try:
        return storage.group_key(chat_id) in enabled_groups()
    except Exception:                                 # noqa: BLE001
        # خرابیِ فایل هرگز نباید دستورهای گروه را قفل کند.
        return False


def set_password(chat_id, password, *, set_by=None):
    """ذخیرهٔ رمزِ جدید (هش + salt تازه). رمز خام ذخیره نمی‌شود."""
    salt = secrets.token_bytes(16)
    params = _kdf_params()
    digest = _hash_password(password, salt, params)

    def mutate(data):
        entry = _group(data, chat_id, create=True)
        entry.update({
            "enabled": True,
            "set_by": storage.user_key(set_by) if set_by else None,
            "salt": salt.hex(),
            "hash": digest,
            "kdf": params,
            "created_at": storage.now(),
            "updated_at": storage.now(),
            "failures": [],
            "lockout_until": 0.0,
        })
        return True

    result = update(mutate)
    _invalidate_enabled_cache()
    return result


def clear_group(chat_id):
    """حذفِ کاملِ رکورد گروه (بازنشانی/لغو)."""

    def mutate(data):
        return data.setdefault("groups", {}).pop(
            storage.group_key(chat_id), None) is not None

    with _LOCK:
        _AWAIT.pop(storage.group_key(chat_id), None)
        _REQUESTS.pop(storage.group_key(chat_id), None)
    result = update(mutate)
    _invalidate_enabled_cache()
    return result


def verify_password(chat_id, password) -> bool:
    """مقایسهٔ امنِ رمز با هشِ ذخیره‌شده (بدون نگه‌داشتنِ رمز)."""
    entry = _group(load(), chat_id)
    if not entry or not entry.get("enabled"):
        return False
    salt_hex = entry.get("salt") or ""
    digest = entry.get("hash") or ""
    params = entry.get("kdf") or {}
    if not salt_hex or not digest or not params:
        return False
    value = to_latin_digits(password)
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    try:
        candidate = _hash_password(value, salt, params)
    except Exception:                                 # noqa: BLE001
        return False
    return hmac.compare_digest(candidate, digest)


# ---------------------------------------------------------------------------
#  ضدِ حدس‌زدن (شمارندهٔ ماندگار + قفلِ موقتِ تدریجی)
# ---------------------------------------------------------------------------
def lockout_remaining(chat_id) -> float:
    """ثانیه‌های باقی‌ماندهٔ قفلِ موقت (۰ یعنی آزاد)."""
    entry = _group(load(), chat_id)
    if not entry:
        return 0.0
    return max(0.0, float(entry.get("lockout_until") or 0.0) - storage.now())


def _recent_failures(entry, now=None):
    now = storage.now() if now is None else now
    cutoff = now - FAILURE_WINDOW
    return [float(ts) for ts in (entry.get("failures") or [])
            if float(ts) >= cutoff]


def register_failure(chat_id):
    """ثبتِ یک رمزِ غلط. خروجی: ``(recent_count, lockout_seconds, level)``.

    * ۵ خطا در ۱۵ دقیقه ⇒ قفلِ موقت ۱۵ دقیقه‌ای؛ دفعهٔ بعد دو برابر می‌شود
      (سقفِ ۴ ساعت) و با ورودِ موفق کاملاً صفر می‌شود.
    """
    def mutate(data):
        entry = _group(data, chat_id, create=True)
        now = storage.now()
        failures = _recent_failures(entry, now) + [now]
        entry["failures"] = failures[-32:]
        entry["updated_at"] = now
        recent = len(failures)
        level = int(entry.get("lockout_level") or 0)
        lockout = 0.0
        if recent >= MAX_WRONG_IN_WINDOW:
            seconds = min(LOCKOUT_MAX, LOCKOUT_BASE * (2 ** level))
            level += 1
            entry["lockout_level"] = level
            entry["lockout_until"] = now + seconds
            entry["failures"] = []        # پنجرهٔ تازه بعد از قفل
            lockout = float(seconds)
        return recent, lockout, level

    return update(mutate)


def register_success(chat_id):
    """ورودِ موفق: شمارنده و قفلِ موقت صفر می‌شوند."""
    def mutate(data):
        entry = _group(data, chat_id, create=True)
        entry["failures"] = []
        entry["lockout_until"] = 0.0
        entry["lockout_level"] = 0
        entry["updated_at"] = storage.now()
        return True

    return update(mutate)


def failure_count(chat_id) -> int:
    entry = _group(load(), chat_id)
    if not entry:
        return 0
    return len(_recent_failures(entry))


# ---------------------------------------------------------------------------
#  «فقط مالک» — یک نقطهٔ حقیقت برای همهٔ گیت‌ها
# ---------------------------------------------------------------------------
def is_owner(chat_id, user_id) -> bool:
    """مالکِ ثبت‌شدهٔ گروه، یا مالکِ سراسری اگر گروه مالکِ ثبت‌شده ندارد."""
    try:
        return bool(access.is_owner(chat_id, user_id))
    except Exception:                                 # noqa: BLE001
        return False


def gate(chat_id, user_id, text, *, confirmed=False, username=None):
    """گیتِ مرکزی: آیا این دستورِ خطرناک باید اجرا شود؟

    خروجی یکی از این سه است:

    * ``"ok"``        — اجرا آزاد است (قفل خاموش، یا تأییدشده، یا دستورِ
      محافظت‌شده نیست).
    * ``"owner"``     — قفل روشن است و فرستنده مالک است ⇒ باید رمز پرسیده شود.
    * ``"denied"``    — قفل روشن است و فرستنده مالک نیست ⇒ فقط پیام
      «تأیید دو مرحله‌ای دارد … فقط مالک ثبت شده»؛ هیچ درخواستی ساخته
      نمی‌شود.

    ``confirmed=True`` یعنی همان اجرای تأییدشده (تا گیت دوباره فعال نشود).
    """
    if confirmed:
        return "ok"
    if text is None:
        return "ok"
    command = protected_command(text)
    if command is None:
        return "ok"
    try:
        if not is_enabled(chat_id):
            return "ok"
    except Exception:                                 # noqa: BLE001
        return "ok"
    if is_owner(chat_id, user_id):
        return "owner"
    return "denied"


#: پیشوندهای مجازِ دستور در روتر ربات (همان چیزی که هندلرها strip می‌کنند).
COMMAND_PREFIXES = ("!", "/", ".", "！", "／")


def strip_prefixes(text) -> str:
    value = normalize(text)
    while value[:1] in COMMAND_PREFIXES:
        value = value[1:].strip()
    return value


def protected_command(text):
    """کلیدِ دستورِ محافظت‌شده (مثلاً ``"پاکسازی کامل"``) یا ``None``.

    پیشوندهای ``!``/``/``/``.`` (مثل ``!پاک لیست اخراج``) هم پذیرفته می‌شوند،
    چون هندلرهای واقعی ربات همین شکل را اجرا می‌کنند.
    """
    value = strip_prefixes(text)
    if not value:
        return None
    for command in PROTECTED_COMMANDS:
        normalized = normalize(command)
        if value == normalized:
            return command
        # دستورهای دارای آرگومان (مثل «پاکسازی خودکار ۳۰») هم می‌گیرند.
        if value.startswith(normalized + " "):
            return command
    for alias, command in PROTECTED_ALIASES.items():
        normalized = normalize(alias)
        if value == normalized or value.startswith(normalized + " "):
            return command
    return None


def is_protected(text) -> bool:
    return protected_command(text) is not None


# ---------------------------------------------------------------------------
#  حالت‌های انتظارِ رمز (تنظیم / لغو / تأییدِ دستور)
# ---------------------------------------------------------------------------
def _await_key(chat_id, owner_id):
    return (storage.group_key(chat_id), storage.user_key(owner_id))


def _prune_await(now=None):
    now = time.monotonic() if now is None else now
    for key in [k for k, v in _AWAIT.items()
                if now - float(v.get("created_at", 0)) > PENDING_TTL]:
        _AWAIT.pop(key, None)


def begin_cancel(chat_id, owner_id):
    """حالت انتظار برای «رمز فعلی» هنگام لغوِ دو مرحله‌ای."""
    _prune_await()
    with _LOCK:
        _AWAIT[_await_key(chat_id, owner_id)] = {
            "stage": "cancel", "created_at": time.monotonic(), "first": None,
        }
    return True


def begin_setup(chat_id, owner_id):
    """شروعِ «رمز جدید» — گام ۱ از ۲."""
    _prune_await()
    with _LOCK:
        _AWAIT[_await_key(chat_id, owner_id)] = {
            "stage": "new", "created_at": time.monotonic(), "first": None,
        }
    return True


def begin_repeat(chat_id, owner_id, first_password):
    """گام ۲: رمزِ اول را (فقط در حافظه، حداکثر ۲ دقیقه) نگه می‌دارد."""
    _prune_await()
    with _LOCK:
        _AWAIT[_await_key(chat_id, owner_id)] = {
            "stage": "repeat", "created_at": time.monotonic(),
            "first": str(first_password),
        }
    return True


def awaiting_stage(chat_id, owner_id):
    """اگر همین مالک در حالِ تنظیم/لغوِ رمز است، آن حالت را برمی‌گرداند."""
    _prune_await()
    with _LOCK:
        entry = _AWAIT.get(_await_key(chat_id, owner_id))
        return dict(entry) if entry else None


def cancel_await(chat_id, owner_id):
    _prune_await()
    with _LOCK:
        return _AWAIT.pop(_await_key(chat_id, owner_id), None) is not None


# ---------------------------------------------------------------------------
#  درخواستِ در انتظار برای اجرای دستور
# ---------------------------------------------------------------------------
def create_request(chat_id, owner_id, command, text, *, callback_kind=None,
                   in_group=True):
    """درخواستِ تأییدِ یک دستورِ خطرناک (حداکثر یکی برای هر گروه/مالک)."""
    key = storage.group_key(chat_id)
    request = {
        "nonce": secrets.token_hex(8),
        "chat_id": chat_id,
        "owner_id": owner_id,
        "command": command,
        "text": text,
        "callback_kind": callback_kind,
        "in_group": bool(in_group),
        "created_at": time.monotonic(),
        "tries": 0,
    }
    with _LOCK:
        previous = _REQUESTS.get(key)
        if previous is not None and str(previous.get("owner_id")) == str(
                owner_id):
            # ⚠️ ضدِ حدس‌زدن: نوشتنِ دوبارهٔ همان دستور، شمارندهٔ تلاش‌ها را
            # صفر نمی‌کند (وگرنه می‌شد با تکرارِ دستور، تلاشِ نامحدود گرفت).
            request["tries"] = int(previous.get("tries", 0))
        _REQUESTS[key] = request        # درخواستِ جدید جایگزینِ قبلی
    return dict(request)


def pending_request(chat_id):
    """درخواستِ تازه (اگر منقضی نشده باشد) یا ``None``."""
    key = storage.group_key(chat_id)
    with _LOCK:
        request = _REQUESTS.get(key)
        if request is None:
            return None
        if time.monotonic() - float(request["created_at"]) > REQUEST_TTL:
            _REQUESTS.pop(key, None)
            return None
        return dict(request)


def has_pending_state(chat_id) -> bool:
    """آیا برای این گروه درخواست یا حالتِ انتظارِ رمز وجود دارد؟"""
    _prune_await()
    key = storage.group_key(chat_id)
    with _LOCK:
        if key in _REQUESTS:
            return True
        return any(entry_key[0] == key for entry_key in _AWAIT)


def drop_request(chat_id, nonce=None):
    key = storage.group_key(chat_id)
    with _LOCK:
        request = _REQUESTS.get(key)
        if request is None:
            return None
        if nonce is not None and request.get("nonce") != nonce:
            return None
        return _REQUESTS.pop(key, None)


def register_request_failure(chat_id, nonce=None):
    """یک رمزِ غلط برای درخواستِ جاری. خروجی: ``(tries, dropped)``."""
    key = storage.group_key(chat_id)
    with _LOCK:
        request = _REQUESTS.get(key)
        if request is None or (nonce is not None
                               and request.get("nonce") != nonce):
            return None, False
        request["tries"] = int(request.get("tries", 0)) + 1
        if request["tries"] >= MAX_WRONG_PER_REQUEST:
            _REQUESTS.pop(key, None)
            return request["tries"], True
        return request["tries"], False


# ---------------------------------------------------------------------------
#  حذفِ بی‌خطرِ پیامِ رمز (best-effort؛ شکست فقط لاگ می‌شود)
# ---------------------------------------------------------------------------
async def wipe_password_message(bot, chat_id, message_id, logger=None):
    """پیامِ حاویِ رمز بلافاصله پاک شود. هرگز استثنا بالا نمی‌دهد."""
    if not message_id:
        return False
    try:
        from modules.manage_features import peers
        peer = peers.group_peer(bot, chat_id)
        await call(
            (lambda: bot.client.delete_messages(peer, [int(message_id)])),
            logger=logger, timeout=5.0, max_retry=1,
            what=f"TWO FACTOR WIPE chat={chat_id}",
        )
        return True
    except Exception as error:                        # noqa: BLE001
        log_info(logger, "TWO FACTOR WIPE FAILED "
                         f"chat_id={chat_id} error={error!r}")
        return False


def log(logger, message):
    """لاگِ امن: فقط رخداد، هرگز رمز/هش/طولِ رمز."""
    if logger is None:
        return
    try:
        logger.log_info(message)
    except Exception:                                 # noqa: BLE001
        pass


def log_error(logger, message):
    if logger is None:
        return
    try:
        logger.log_error(message)
    except Exception:                                 # noqa: BLE001
        pass
