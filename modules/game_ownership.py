# -*- coding: utf-8 -*-
"""🎯 مالکیت «شرکت» و «مرکز فرماندهی» بین بازی‌ها — یک قاعده، یک‌جا.

## مشکلی که این فایل حل می‌کند

چند بازیِ ربات با یک واژهٔ مشترک ثبت‌نام می‌کنند: «شرکت». اگر در یک گروه
هم‌زمان دو بازی در پنجرهٔ ثبت‌نام باز باشند (مثلاً «بقا» و «مرکز فرماندهی»)،
هر بازی که اول به پیام برسد آن را می‌بلعد و بازی دیگر هیچ‌وقت بازیکن نمی‌گیرد
یا برعکس، بازیکنان یک بازی به حساب بازی دیگر ثبت می‌شوند.

همچنین «مرکز فرماندهی» یک پیام اختصاصی دارد («ظرفیت این مسابقه پر است ⛔»)
که در دو حالتِ کاملاً متفاوت فرستاده می‌شد و کارفرما را گیج می‌کرد:

* مسابقه واقعاً دو فرمانده دارد ⇒ درست است.
* مسابقهٔ کهنه‌ای با صفر/یک فرمانده در حافظه مانده (مثلاً مهلت ثبت‌نامش
  گذشته یا ربات ری‌استارت شده) ⇒ **غلط**؛ باید همان مسابقه بازیابی/باز شود،
  نه اینکه به کاربر گفته شود «پر است». و اگر فرمانده‌ای از همان مسابقه
  دستور را بزند، دیگر پیام «ظرفیت پر است» نمی‌گیرد.

## قاعده‌ها (به ترتیب اولویت)

1. بازی‌ای که کاربر **از قبل عضو** آن است، «شرکت» او را می‌گیرد.
2. اگر فقط یک بازی در حال ثبت‌نام است، «شرکت» مال همان است. (رفتار قبلی)
3. اگر چند بازی در حال ثبت‌نام باشند، بازی‌ای که کاربر در آن **جا دارد**
   بیشترین شانس را دارد؛ در تساوی، بازی‌ای که در همان گروه **تازه‌تر**
   شروع شده (زمانِ شروع بزرگ‌تر) برنده است — یعنی رفتاری که کاربر انتظار
   دارد: «آخرین بازی‌ای که ساختم».
4. اگر بازی دیگری «شرکت» را برد، بازی بازنده پیام «قبلاً ثبت‌نام» یا
   «ظرفیت پر است» **نمی‌فرستد** (چون اصلاً ثبت‌نامی رخ نداده).

این فایل فقط «تصمیم» می‌گیرد؛ هیچ state یا تایمری نمی‌سازد و هیچ بازی‌ای
را تغییر نمی‌دهد. اگر روزی بازی تازه‌ای اضافه شود، فقط یک ``provider``
به ``OWNERSHIP`` اضافه می‌شود.
"""
from __future__ import annotations

import time

#: نام‌های پذیرفته‌شدهٔ «شرکت» (همه بعد از normalize مقایسه می‌شوند).
JOIN_WORDS = frozenset({"شرکت"})

#: دستور شروع بازی «مرکز فرماندهی» (و شکل دومش).
FOX_COMMAND_START_WORDS = frozenset({"مرکز فرماندهی", "مرکز فرماندهی روباه"})

_CLAIMED = {}      # (chat_id, "join") → {"game", "user_id", "at"}
_CLAIM_TTL = 30.0  # ثانیه؛ فقط برای اینکه دو روترِ پشت‌سرهم دوباره نگیرند


# ---------------------------------------------------------------------------
# ابزار
# ---------------------------------------------------------------------------
def _norm(text):
    """نرمال‌سازی مستقل (نیم‌فاصله/فاصلهٔ اضافه/حروف بزرگ) بدون import حلقوی."""
    if text is None:
        return ""
    value = str(text).replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\u064a", "ی").replace("\u0643", "ک")
    return " ".join(value.split()).strip()


def is_join_word(text) -> bool:
    return _norm(text) in {_norm(word) for word in JOIN_WORDS}


def is_fox_command_start(text) -> bool:
    return _norm(text) in {_norm(word) for word in FOX_COMMAND_START_WORDS}


# ---------------------------------------------------------------------------
# ارزیابی یک بازی
# ---------------------------------------------------------------------------
class GameStatus:
    """وضعیت یک بازی برای «شرکت» یک کاربر مشخص."""

    __slots__ = ("key", "joining", "has_room", "member", "age", "players")

    def __init__(self, key, joining, has_room, member, age, players):
        self.key = key
        self.joining = bool(joining)      # پنجرهٔ ثبت‌نام باز است
        self.has_room = bool(has_room)    # ظرفیت پر نیست
        self.member = bool(member)        # کاربر از قبل داخل این بازی است
        #: چند ثانیه از شروع مسابقه گذشته (کم‌تر = تازه‌تر). همهٔ بازی‌ها با
        #: همین واحد برمی‌گردانند تا مقایسه بین بازی‌ها معنی‌دار باشد.
        self.age = float(age or 0)
        self.players = int(players or 0)

    def __repr__(self):  # pragma: no cover - فقط برای دیباگ
        return (f"GameStatus({self.key!r}, joining={self.joining}, "
                f"room={self.has_room}, member={self.member}, "
                f"players={self.players}, age={self.age:.0f}s)")


def _safe(call, default=None):
    try:
        return call()
    except Exception:
        return default


def _status(key, chat_id, user_id, provider):
    """وضعیت یک بازی؛ provider از قبل به همین ``chat_id`` بسته شده است."""
    return GameStatus(
        key=key,
        joining=_safe(lambda: provider["joining"](), False),
        has_room=_safe(lambda: provider["has_room"](), False),
        member=_safe(lambda: provider["member"](chat_id, user_id), False),
        age=_safe(lambda: provider["age"](), 0),
        players=_safe(lambda: provider["players"](), 0),
    )


def _providers(chat_id):
    """providerهای همین گروه؛ هر بار تازه خوانده می‌شوند (بدون import حلقوی)."""
    from modules import game_ownership_registry as registry
    return registry.all_for(chat_id)


def evaluate(chat_id, user_id=None):
    """وضعیت همهٔ بازی‌هایی که «شرکت» را می‌شناسند (برای تست و دیباگ)."""
    return [_status(key, chat_id, user_id, provider)
            for key, provider in _providers(chat_id)]


# ---------------------------------------------------------------------------
# تصمیم‌گیری
# ---------------------------------------------------------------------------
def join_owner(chat_id, user_id=None):
    """کدام بازی «شرکت» این کاربر را می‌گیرد؟ ``None`` یا کلید بازی."""
    statuses = evaluate(chat_id, user_id)
    if not statuses:
        return None

    # ۱) عضویت قبلی — تکرار «شرکت» باید پاسخ خودِ همان بازی را بگیرد.
    members = [s for s in statuses if s.member]
    if members:
        return min(members, key=lambda s: (not s.joining, s.age)).key

    # ۲) بازی‌هایی که پنجرهٔ ثبت‌نامشان باز است و جا دارند.
    ready = [s for s in statuses if s.joining and s.has_room]
    if not ready:
        return None
    if len(ready) == 1:
        return ready[0].key

    # ۳) تساوی: تازه‌ترین مسابقه (بزرگ‌ترین زمانِ شروع) برنده است.
    ready.sort(key=lambda s: (s.age, -s.players))
    return ready[0].key


def owns_join(chat_id, user_id, game_key):
    """آیا «شرکت» در این لحظه مالِ این بازی است؟"""
    owner = join_owner(chat_id, user_id)
    return owner is None or owner == game_key


def blocked_by_other(chat_id, user_id, game_key):
    """آیا بازی دیگری «شرکت» را گرفته است؟ (برای پیام «ثبت‌نام نشد»)"""
    owner = join_owner(chat_id, user_id)
    return owner is not None and owner != game_key


def waiting_games(chat_id, user_id):
    """بازی‌هایی که این کاربر در آن‌ها ثبت‌نام کرده و منتظرِ بازی‌اند.

    برای پیامِ «چند بازی هم‌زمان باز است» استفاده می‌شود تا کاربر بداند
    «شرکت» به کدام بازی می‌رود.
    """
    return [s for s in evaluate(chat_id, user_id) if s.member and s.joining]


# ---------------------------------------------------------------------------
# ادعا (claim) — جلوگیری از مصرف دوبارهٔ همان «شرکت» در دو روتر
# ---------------------------------------------------------------------------
def claim_join(chat_id, user_id, game_key):
    """ثبت اینکه «شرکت» این پیام به این بازی رسید. True اگر ادعا مالِ ما شد."""
    now = time.monotonic()
    _prune(now)
    current = _CLAIMED.get((chat_id, "join"))
    if current and current["game"] != game_key and now - current["at"] < _CLAIM_TTL:
        return False
    _CLAIMED[(chat_id, "join")] = {"game": game_key, "user_id": user_id, "at": now}
    return True


def _prune(now=None):
    now = now or time.monotonic()
    for key in [k for k, v in _CLAIMED.items() if now - v["at"] > _CLAIM_TTL]:
        _CLAIMED.pop(key, None)


def reset_all(chat_id=None):
    """پاک‌سازی state درون‌حافظه (تست‌ها و ری‌ست سراسری)."""
    if chat_id is None:
        _CLAIMED.clear()
        return
    for key in [k for k in _CLAIMED if k[0] == chat_id]:
        _CLAIMED.pop(key, None)
