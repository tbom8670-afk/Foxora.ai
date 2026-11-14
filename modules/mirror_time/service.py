# -*- coding: utf-8 -*-
"""🕰 قابلیت «تایم جفت» (Mirror Time) — سرویسِ مستقل.

رفتار:

1. **تشخیصِ ساعتِ جفت** — هر ساعتی که ``hour == minute`` باشد، به وقتِ
   تهران (``modules.time_utils.now_local``): ``0:0``، ``1:1`` … ``23:23``
   (یعنی همان ۱:۱ … ۱۲:۱۲ کارفرما، به‌علاوهٔ ساعت‌های جفتِ ۲۴ساعته).
2. **ارسال دقیقاً سرِ لحظه** — یک تسکِ پس‌زمینهٔ مستقل تا ثانیهٔ صفرِ همان
   دقیقه می‌خوابد و بعد پیامِ ``♡ ↻ تایم : 5:5 🦊🦋`` (کاملاً Bold) را
   برای گروه‌های فعال می‌فرستد. مسیرِ پیام‌های کاربران هیچ تماسی با این
   حلقه ندارد؛ نه صف و نه لوپِ پردازشِ پیام‌ها را کند می‌کند.
3. **فقط یک بار** — کلیدِ ``YYYY-MM-DD H:M`` برای هر گروه در فایلِ وضعیت
   ذخیره می‌شود؛ نه ارسالِ دوباره در همان دقیقه، نه بعد از ری‌استارت.
4. **پاسخِ «فدات»** — فقط وقتی کاربر روی **همان پیامِ تایم** ریپلای کند و
   متنش «فدات» (یا هر چیزی که «فدات» داشته باشد) باشد، پیامِ
   ``فدامون 🥲`` ارسال می‌شود. تطبیق بر اساسِ **شناسهٔ پیام** انجام می‌شود،
   نه وجودِ واژه؛ پس در گفت‌وگوی معمولی هیچ پاسخی داده نمی‌شود.
"""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from modules.time_utils import now_local

from . import state, texts

#: اگر حلقه دیر بیدار شود (خوابِ سرور/ری‌استارت)، تا این مدت هنوز همان
#: تایم را می‌فرستد؛ بیشتر از این، تایمِ گذشته بی‌معنی است و رد می‌شود.
MAX_LATE_SECONDS = 90

#: سقفِ تلاش برای یک گروه در همان تایم (اگر ارسال خطا داد).
MAX_SEND_ATTEMPTS = 3

#: فاصلهٔ چک‌های کوتاه (در حدِ ثانیه) — بارِ ناچیز.
TICK_SECONDS = 1.0

#: بزرگ‌ترین تکهٔ خواب تا لحظهٔ هدف (تغییرِ ساعت/کنسل شدن سریع دیده می‌شود).
SLEEP_CHUNK = 15.0


# ---------------------------------------------------------------------------
# محاسبهٔ تایم
# ---------------------------------------------------------------------------
def is_pair_time(moment) -> bool:
    """آیا این لحظه «تایم جفت» است؟ (ساعت == دقیقه)"""
    return int(moment.hour) == int(moment.minute)


def next_pair_time(moment=None):
    """اولین تایمِ جفتِ بعدی (شروعِ دقیقه) — یا ``None``."""
    current = moment if moment is not None else now_local()
    candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(24 * 60 + 2):
        if is_pair_time(candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def seconds_until(moment) -> float:
    return (moment - now_local()).total_seconds()


def _due_slot(moment):
    """اگر همین حالا داخلِ دقیقهٔ جفت هستیم و دیر نشده، همان دقیقه را بده."""
    minute = moment.replace(second=0, microsecond=0)
    if is_pair_time(minute) and int(moment.second) <= MAX_LATE_SECONDS:
        return minute
    return None


# ---------------------------------------------------------------------------
# ارسال
# ---------------------------------------------------------------------------
def _log(logger, message, *, error=False):
    try:
        if logger is None:
            return
        if error:
            logger.log_error(message)
        else:
            logger.log_info(message)
    except Exception:
        pass


def _message_id_of(sent):
    """شناسهٔ پیام از خروجیِ ارسال (این نسخه اغلب ``None`` می‌دهد)."""
    if sent is None:
        return 0
    try:
        from modules.notice_cleanup import extract_sent_id
        found = extract_sent_id(sent)
        if isinstance(found, int) and found > 0:
            return found
    except Exception:
        pass
    for attr in ("id", "message_id"):
        value = getattr(sent, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def active_chats():
    """گروه‌های فعال و منقضی‌نشده (همان منبعِ بقیهٔ حلقه‌های ربات)."""
    try:
        from modules import group_storage
    except Exception:
        return []
    try:
        groups = group_storage.load_groups() or {}
    except Exception:
        return []
    chats = []
    for raw in list(groups.keys()):
        try:
            chat_id = int(raw)
        except (TypeError, ValueError):
            chat_id = raw
        try:
            if not group_storage.is_active(chat_id):
                continue
        except Exception:
            continue
        try:
            from modules import group_expiry
            if group_expiry.is_expired(chat_id):
                continue
        except Exception:
            pass
        chats.append(chat_id)
    return chats


async def _deliver(bot, chat_id, text, entities, slot, logger=None):
    """ارسال از صفِ موجودِ پروژه (``outgoing_sender``) یا مستقیم."""
    def record(sent):
        found = _message_id_of(sent)
        try:
            if found:
                state.set_card_id(chat_id, found, text=text, slot=slot)
            _log(logger, "MIRROR TIME SENT chat_id=%s slot=%s id=%s"
                        % (chat_id, slot, found or "(unknown)"))
        except Exception:
            pass

    sender = getattr(bot, "outgoing_sender", None)
    payload = {"formatting_entities": list(entities)} if entities else {}
    if sender is not None:
        try:
            accepted = sender.enqueue_send(
                chat_id, text, priority=1, on_done=record, **payload
            )
            if accepted:
                return True
        except TypeError:
            try:
                if sender.enqueue_send(chat_id, text, priority=1,
                                       on_done=record):
                    return True
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            _log(logger, "MIRROR TIME ENQUEUE FAILED chat_id=%s error=%r"
                        % (chat_id, exc), error=True)
    try:
        sent = await bot.client.send_message(chat_id, text, **payload)
    except Exception as exc:  # noqa: BLE001
        _log(logger, "MIRROR TIME SEND FAILED chat_id=%s error=%r"
                    % (chat_id, exc), error=True)
        return False
    record(sent)
    return True


async def send_slot(bot, moment, logger=None) -> int:
    """ارسالِ پیامِ همان تایم برای همهٔ گروه‌های فعال — هر گروه فقط یک بار."""
    slot = state.slot_key(moment)
    text, entities = texts.payload(moment.hour, moment.minute)
    sent_count = 0
    for chat_id in active_chats():
        if state.already_sent(slot, chat_id):
            continue
        attempts = 0
        while attempts < MAX_SEND_ATTEMPTS:
            attempts += 1
            # قفلِ یک‌بار‌مصرف **پیش از** ارسال: حتی اگر ارسال خطا بدهد یا
            # پروسه همان‌جا بمیرد، این تایم برای این گروه دوباره فرستاده
            # نمی‌شود (خواستهٔ «به‌هیچ‌وجه پیام تکراری»).
            state.mark_sent(slot, chat_id, message_id=None, text=text)
            if await _deliver(bot, chat_id, text, entities, slot, logger):
                sent_count += 1
                break
        else:
            _log(logger, "MIRROR TIME GAVE UP chat_id=%s slot=%s attempts=%s"
                        % (chat_id, slot, attempts), error=True)
    return sent_count


async def send_now(bot, moment=None, logger=None) -> int:
    """ارسالِ دستی/تستیِ تایمِ داده‌شده (بدونِ انتظار)."""
    return await send_slot(bot, moment or now_local(), logger=logger)


# ---------------------------------------------------------------------------
# حلقهٔ مستقل
# ---------------------------------------------------------------------------
async def _sleep_until(moment) -> None:
    """خوابِ تکه‌تکه تا لحظهٔ هدف (با تغییرِ ساعت/کنسل شدن سازگار است)."""
    while True:
        remaining = seconds_until(moment)
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, SLEEP_CHUNK))


async def watcher(bot, logger=None) -> None:
    """حلقهٔ پس‌زمینهٔ «تایم جفت» — کاملاً مستقل از مسیرِ پیام‌ها."""
    _log(logger, "MIRROR TIME WATCHER STARTED")
    while True:
        try:
            moment = now_local()
            due = _due_slot(moment)
            if due is not None:
                slot = state.slot_key(due)
                pending = [c for c in active_chats()
                           if not state.already_sent(slot, c)]
                if pending:
                    count = await send_slot(bot, due, logger=logger)
                    _log(logger, "MIRROR TIME SLOT DONE slot=%s sent=%s"
                                % (slot, count))
                await asyncio.sleep(TICK_SECONDS)
                continue
            target = next_pair_time(moment)
            if target is None:                       # pragma: no cover
                await asyncio.sleep(30)
                continue
            _log(logger, "MIRROR TIME NEXT slot=%s in=%ds"
                        % (state.slot_key(target),
                           int(max(0.0, seconds_until(target)))))
            await _sleep_until(target)
        except asyncio.CancelledError:               # pragma: no cover
            raise
        except Exception as exc:  # noqa: BLE001
            _log(logger, "MIRROR TIME WATCHER ERROR error=%r" % (exc,),
                 error=True)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# پاسخِ اختصاصی «فدات»
# ---------------------------------------------------------------------------
_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_AR_MAP = {"ي": "ی", "ك": "ک", "ﻻ": "لا"}


def normalize_text(value) -> str:
    """نرمال‌سازیِ سبکِ فارسی برای تطبیقِ «فدات»."""
    text = str(value or "")
    text = _DIACRITICS.sub("", text)
    for src, dst in _AR_MAP.items():
        text = text.replace(src, dst)
    text = text.replace("\u200c", " ").replace("\u00a0", " ")
    return " ".join(text.split()).lower()


def is_fadat_text(value) -> bool:
    """آیا متن پیام شاملِ «فدات» است؟ (فدات، فداتم، فدات شم، فدات ربات …)"""
    if not value:
        return False
    return texts.FADAT_STEM in normalize_text(value)


def reply_target_id(event):
    """شناسهٔ پیامی که کاربر روی آن ریپلای زده (هر شکلِ رویداد)."""
    message = getattr(event, "message", None)
    reply = getattr(message, "reply_to", None)
    candidates = (
        getattr(message, "reply_to_msg_id", None),
        getattr(reply, "reply_to_msg_id", None),
        getattr(reply, "reply_to_top_id", None),
        getattr(event, "reply_to_msg_id", None),
        getattr(getattr(event, "reply_to", None), "reply_to_msg_id", None),
    )
    for value in candidates:
        if isinstance(value, int) and value > 0:
            return value
    return None


async def _locate_card(bot, chat_id, card):
    """وقتی ارسال شناسه برنگرداند: کارتِ تایم را با متن در تاریخچه پیدا کن."""
    text = (card or {}).get("text")
    client = getattr(bot, "client", None)
    if not text or client is None:
        return None
    try:
        history = await client.get_messages(chat_id, limit=20)
    except Exception:
        return None
    if history is None:
        return None
    if not isinstance(history, (list, tuple)):
        history = [history]
    wanted = str(text).strip()
    for message in history:
        body = (getattr(message, "message", None)
                or getattr(message, "text", None) or "")
        if str(body).strip() != wanted:
            continue
        found = getattr(message, "id", None)
        if isinstance(found, int) and found > 0:
            return found
    return None


async def handle_reply(bot, event, chat_id, text, logger=None) -> bool:
    """اگر این پیام «فدات» روی کارتِ تایم جفت باشد، «فدامون 🥲» را بفرست.

    خروجی ``True`` یعنی پیام مصرف شد (تا هیچ پاسخِ عمومیِ دیگری تداخل نکند).
    """
    if not is_fadat_text(text):
        return False
    card = state.card_for(chat_id)
    if not card:
        return False
    target = reply_target_id(event)
    if target is None:                    # فدات در گفت‌وگوی معمولی ⇒ هیچ
        return False
    known = state.known_ids(chat_id)
    if target not in known:
        located = await _locate_card(bot, chat_id, card)
        if located:
            try:
                state.set_card_id(chat_id, located,
                                  text=card.get("text"), slot=card.get("slot"))
            except Exception:
                pass
            known.add(located)
        if target not in known:
            return False
    try:
        await event.reply(texts.FADAT_REPLY)
    except Exception as exc:  # noqa: BLE001
        _log(logger, "MIRROR TIME FADAT REPLY FAILED chat_id=%s error=%r"
                    % (chat_id, exc), error=True)
        return True
    _log(logger, "MIRROR TIME FADAT reply chat_id=%s reply_to=%s"
                % (chat_id, target))
    return True


__all__ = [
    "MAX_LATE_SECONDS",
    "is_pair_time",
    "next_pair_time",
    "send_slot",
    "send_now",
    "watcher",
    "is_fadat_text",
    "normalize_text",
    "reply_target_id",
    "handle_reply",
    "active_chats",
]
