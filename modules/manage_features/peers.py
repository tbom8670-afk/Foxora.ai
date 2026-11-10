# -*- coding: utf-8 -*-
"""🧭 resolve کردن «گروه» و «کاربر» برای RPCها — یک‌جا و درست.

مشکلِ واقعی: شناسهٔ گروه در این پروژه به شکلِ **مثبت** ذخیره می‌شود
(``normalize_group_id``). اگر همان عدد به متدهای SPlusthon داده شود، کتابخانه
آن را «کاربر» فرض می‌کند و برای گرفتنِ access_hash سراغ
``users.GetUsersRequest`` می‌رود؛ نتیجه: یا ``Could not find the input
entity``/خطای شبکه، یا عملیاتِ بی‌اثر (حذف/ویرایش انجام نمی‌شود و خطایی هم
برنمی‌گردد). این ماژول فقط چند قاعده دارد:

1. اگر InputPeer گروه در ``bot.reply_input_peer_cache`` بود ⇒ همان.
2. وگرنه شناسهٔ منفیِ کانال: ``-(1000000000000 + short_id)``.

در این نسخه، **کشِ وضعیت قفل** هم به همین ماژول منتقل شده تا دو کشِ موازی
(``exclusive._LOCKED_CACHE`` و ``exclusive_commands._LOCK_CACHE``) وجود نداشته
باشد؛ همهٔ مصرف‌کننده‌ها از همین توابع استفاده می‌کنند.

هیچ RPC‌ای این‌جا زده نمی‌شود (به‌جز ``resolve_user`` که صریحاً صدا زده
می‌شود و فقط در مسیر دستور است).
"""
from __future__ import annotations

from modules.group_id import normalize_group_id
from modules.manage_features import storage

CHANNEL_ID_OFFSET = 1_000_000_000_000

#: مدت اعتبارِ نتیجهٔ «گروه قفل است». کوتاه است تا پس از باز شدنِ گروه، ثبتِ
#: جدید اشتباهاً مجاز نشود؛ ولی همان یک بار هم کافی است که مسیر گرم (بررسی هر
#: پیام) هیچ RPC‌ای نزند. (قبلاً داک‌استرینگ «۲ دقیقه» می‌گفت و کد ۲۰ ثانیه
#: بود؛ حالا یک عدد واحد است.)
LOCK_CACHE_TTL = 20.0

#: ``chat_key`` → ``(ts_monotonic, locked)``
_LOCK_STATE: dict = {}
#: ``chat_key`` → ``InputPeer`` کانال (پرهیز از resolve تکراری)
_CHANNEL_PEERS: dict = {}
_CHANNEL_PEERS_MAX = 256


# ---------------------------------------------------------------------------
#  گروه
# ---------------------------------------------------------------------------
def group_peer(bot, chat_id):
    """InputPeer (یا شناسهٔ منفیِ امنِ) گروه برای RPCهای SPlusthon."""
    cache = getattr(bot, "reply_input_peer_cache", None) or {}
    try:
        peer = cache.get(chat_id)
    except Exception:                                # noqa: BLE001
        peer = None
    if peer is not None:
        return peer

    wanted = storage.group_key(chat_id)
    try:
        for cached_id, cached_peer in list(cache.items()):
            if cached_peer is not None and storage.group_key(cached_id) == wanted:
                return cached_peer
    except Exception:                                # noqa: BLE001
        pass

    try:
        raw = int(chat_id)
        short = int(normalize_group_id(chat_id))
    except (TypeError, ValueError):
        return chat_id
    if raw < 0:
        return raw
    return -(CHANNEL_ID_OFFSET + short)


def channel_peer(bot, chat_id):
    """InputPeerِ **کانال** (با ``channel_id``/``access_hash``) یا ``None``.

    این تابع هرگز RPC نمی‌زند: فقط از کشِ این ماژول و کشِ خودِ ربات می‌خواند.
    """
    key = storage.group_key(chat_id)
    peer = _CHANNEL_PEERS.get(key)
    if peer is not None and hasattr(peer, "channel_id"):
        return peer
    candidate = group_peer(bot, chat_id)
    if candidate is not None and hasattr(candidate, "channel_id"):
        _remember_channel_peer(bot, chat_id, candidate)
        return candidate
    return None


def _remember_channel_peer(bot, chat_id, peer):
    if peer is None or not hasattr(peer, "channel_id"):
        return peer
    _CHANNEL_PEERS[storage.group_key(chat_id)] = peer
    while len(_CHANNEL_PEERS) > _CHANNEL_PEERS_MAX:
        _CHANNEL_PEERS.pop(next(iter(_CHANNEL_PEERS)), None)
    return peer


def remember_channel_peer(bot, chat_id, peer):
    """ثبتِ عمومیِ InputPeer کانال (برای استفادهٔ لایهٔ دستور)."""
    return _remember_channel_peer(bot, chat_id, peer)


def is_meaningful(value) -> bool:
    """آیا peer واقعاً به گروه اشاره می‌کند؟ (برای مسیرهای حساس)"""
    return value is not None


# ---------------------------------------------------------------------------
#  کشِ وضعیت قفل (تنها منبعِ حقیقت — به‌جای دو کشِ قبلی)
# ---------------------------------------------------------------------------
def lock_state_cached(chat_id, *, ttl=LOCK_CACHE_TTL):
    """``True/False`` اگر نتیجهٔ تازه (< ``ttl``) موجود باشد، وگرنه ``None``."""
    entry = _LOCK_STATE.get(storage.group_key(chat_id))
    if not entry:
        return None
    stamp, locked = entry
    if storage.monotonic() - stamp > float(ttl):
        _LOCK_STATE.pop(storage.group_key(chat_id), None)
        return None
    return bool(locked)


def remember_lock_state(chat_id, locked):
    _LOCK_STATE[storage.group_key(chat_id)] = (storage.monotonic(),
                                               bool(locked))
    return bool(locked)


def forget_lock_state(chat_id):
    _LOCK_STATE.pop(storage.group_key(chat_id), None)


def clear_caches():
    """پاک‌سازیِ کامل (تست/ری‌ست)."""
    _LOCK_STATE.clear()
    _CHANNEL_PEERS.clear()


# ---------------------------------------------------------------------------
#  کاربر
# ---------------------------------------------------------------------------
def user_identifier(user):
    """شناسهٔ عددیِ پایدار از هر شکلِ شیء (entity/InputPeer/عدد/رشته)."""
    if user is None:
        return None
    if isinstance(user, bool):                       # bool زیرِ int است
        return None
    if isinstance(user, int):
        return user
    if isinstance(user, str):
        stripped = user.strip().lstrip("@")
        return int(stripped) if stripped.lstrip("-").isdigit() else None
    for attr in ("id", "user_id"):
        value = getattr(user, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
    peer = getattr(user, "peer", None)
    if peer is not None:
        return user_identifier(peer)
    return None


def user_key(value):
    """کلیدِ رشته‌ای برای مقایسه (هم‌شکل با ``storage.user_key``)."""
    identifier = user_identifier(value)
    if identifier is None:
        return str(value)
    return storage.user_key(identifier)


def as_input_user(value):
    """اگر ``value`` خودش یک InputUser/entity باشد همان را برمی‌گرداند.

    ``int``/``str`` ⇒ ``None`` (یعنی «resolve لازم است»).
    """
    if value is None or isinstance(value, (int, str)):
        return None
    # InputPeerUser / User / InputUser همه ``access_hash`` یا ``id`` دارند.
    if hasattr(value, "access_hash") and (
            hasattr(value, "user_id") or hasattr(value, "id")):
        return value
    if hasattr(value, "id") and not hasattr(value, "channel_id"):
        return value
    return None


async def resolve_user(bot, value, *, chat_id=None):
    """``(input_entity, user_id)`` برای یک کاربر — یک RPC در بدترین حالت.

    * اگر ``value`` خودش entity/InputPeer باشد ⇒ بدون RPC.
    * اگر شناسهٔ عددی باشد ⇒ ``get_entity(int)`` (کشِ SPlusthon اول تلاش
      می‌شود و در بیشتر موارد RPC نمی‌زند).
    * اگر یوزرنیم باشد ⇒ ``get_entity(username)``.

    خطا بالا می‌رود (``ValueError``/خطای شبکه) تا لایهٔ بالاتر پیامِ فارسیِ
    درست بدهد؛ این‌جا هیچ خطایی خفه نمی‌شود.
    """
    entity = as_input_user(value)
    if entity is not None:
        return entity, user_identifier(entity)

    # رشتهٔ عددی («۶۱۲» یا " 612 ") ⇒ مثل شناسهٔ عددی رفتار کن، نه یوزرنیم.
    numeric = user_identifier(value) if isinstance(value, str) else None
    if numeric is not None and user_identifier(value) != 0:
        value = numeric

    identifier = user_identifier(value)
    if identifier is not None:
        try:
            resolved = await bot.client.get_entity(int(identifier))
        except Exception:                            # noqa: BLE001
            resolved = None
        if resolved is not None:
            return resolved, user_identifier(resolved) or identifier
        # شناسهٔ عددی خام: SPlusthon خودش resolve می‌کند (ممکن است شکست بخورد).
        return int(identifier), identifier

    text = str(value or "").strip().lstrip("@")
    if not text:
        return None, None
    resolved = await bot.client.get_entity(text)
    return resolved, user_identifier(resolved)
