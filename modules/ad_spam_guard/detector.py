"""موتور تشخیص ربات‌های تبلیغاتی و اجرای اکشن.

قاعدهٔ تصمیم (ایمن به‌صورت پیش‌فرض):

    سیگنال هویت  = مارکر در نام  |  مارکر در یوزرنیم  |  سری ایموجی در نام
    سیگنال محتوا = مارکر در متن |  سری ایموجی در متن |  تکرار حروف مشکوک

    اسپم = سیگنال هویت  AND  سیگنال محتوا

یعنی پیامِ یک کاربرِ عادی که اتفاقاً کلمهٔ تبلیغاتی دارد هرگز حذف نمی‌شود،
و رباتی که نامش را عوض می‌کند ولی متنش را نگه می‌دارد هم گرفته می‌شود.
این همان چیزی است که کارفرما خواست: «یک وقت باعث خطا نشه که پیام عادی
کاربر‌ها را اشتباه بگیره».
"""
from __future__ import annotations

import logging

from . import patterns as P
from . import state
from . import storage
from . import texts

logger = logging.getLogger(__name__)

# صف از بیرون تزریق می‌شود (تا ماژول در تست‌ها بدون ربات واقعی کار کند).
_delete_queue = None


def bind_queue(queue) -> None:
    """صف اختصاصی حذف را به موتور متصل می‌کند."""
    global _delete_queue
    _delete_queue = queue


# ---------------------------------------------------------------------------
# سیگنال‌ها
# ---------------------------------------------------------------------------

def _has_repeated_run(text: str) -> bool:
    """آیا متن حرفِ ۳+ بار تکرارشده دارد؟ (مثل «استرررررر»)"""
    if not text:
        return False
    # روی متنِ خام چک می‌کنیم، چون نرمال‌سازی تکرارها را جمع می‌کند.
    import re

    return bool(re.search(r"(.)\1{2,}", text, re.UNICODE))


def evaluate(sender, message_text: str) -> dict:
    """همهٔ سیگنال‌ها را می‌سنجد و نتیجه را با جزئیات برمی‌گرداند."""
    name_raw = P.sender_name(sender)
    username_raw = P.sender_username(sender)

    name = P.normalize(name_raw)
    username = P.normalize(username_raw)
    text = P.normalize(message_text)

    name_emoji = P.emoji_signature(name_raw)
    text_emoji = P.emoji_signature(message_text)

    signals: dict[str, bool] = {
        "name_marker": any(m in name for m in P.NAME_MARKERS),
        "username_marker": any(m in username for m in P.NAME_MARKERS),
        "name_emoji_series": any(s in name_emoji for s in P.EMOJI_SERIES),
        "text_marker": any(m in text for m in P.TEXT_MARKERS),
        "text_emoji_series": any(s in text_emoji for s in P.EMOJI_SERIES),
        "repeated_run": _has_repeated_run(message_text or ""),
        "spam_phrase": any(p in text for p in P.SPAM_PHRASES),
    }

    score = sum(P.WEIGHTS.get(k, 0) for k, on in signals.items() if on)

    identity_hit = (
        signals["name_marker"]
        or signals["username_marker"]
        or signals["name_emoji_series"]
    )
    content_hit = (
        signals["text_marker"]
        or signals["text_emoji_series"]
        or signals["repeated_run"]
    )

    # سیگنالِ «قویِ هویت»: رشتهٔ مارکر در نام یا یوزرنیم. این سیگنال به
    # تنهایی کافی است، چون هیچ کاربرِ عادی چنین رشته‌ای در نام ندارد.
    identity_strong = signals["name_marker"] or signals["username_marker"]
    # سیگنالِ «محتوای قوی»: مارکرِ متنی به‌همراه تکرار حروف یا سری ایموجی.
    content_strong = signals["text_marker"] and (
        signals["repeated_run"] or signals["text_emoji_series"]
    )

    if P.REQUIRE_IDENTITY_SIGNAL:
        # هویت قوی به تنهایی کافی است؛ در غیر این صورت هویتِ ضعیف (سری
        # ایموجیِ نام) یا محتوای قوی باید تأیید شود.
        is_spam = (
            identity_strong
            or (signals["name_emoji_series"] and content_hit)
            or content_strong
        )
    else:
        is_spam = score >= P.CONTENT_ONLY_THRESHOLD

    return {
        "is_spam": bool(is_spam),
        "score": score,
        "signals": signals,
        "identity_hit": identity_hit,
        "content_hit": content_hit,
        "identity_strong": identity_strong,
        "content_strong": content_strong,
    }


# ---------------------------------------------------------------------------
# ورود اصلی
# ---------------------------------------------------------------------------

async def guard_message(bot, event, chat_id, user_id, sender, message_id) -> bool:
    """پیام را بررسی و در صورت اسپم حذف می‌کند.

    خروجی: ``True`` یعنی این پیام توسط این قابلیت رسیدگی (حذف) شد و
    فراخوان نباید مسیر عادی را ادامه دهد.
    """
    if not chat_id or not user_id or message_id is None:
        return False
    if getattr(event, "is_private", False):
        return False

    # قابلیت فقط وقتی روشن باشد کار می‌کند.
    if not storage.is_enabled(chat_id):
        return False

    # خودِ ربات هرگز هدف نمی‌شود.
    own_id = getattr(bot, "bot_account_id", None)
    if own_id is not None and str(user_id) == str(own_id):
        return False

    message_text = (
        getattr(getattr(event, "message", None), "message", "")
        or getattr(getattr(event, "message", None), "caption", "")
        or ""
    )
    if not str(message_text).strip():
        # پیام بدون متنِ قابلِ بررسی؛ فقط رسانه را دست نمی‌زنیم.
        return False

    result = evaluate(sender, message_text)

    # ادمین/مالک معاف‌اند، **مگر** اینکه هویتِ فرستنده با مارکر مطابقت
    # داشته باشد. این عمدی است: این ربات‌ها گاهی نشانِ ادمین هم دارند و
    # اگر معافیتِ ادمین بی‌قید اجرا شود، آن‌ها عبور می‌کنند. در مقابل،
    # ادمینِ واقعی هرگز مارکر را در نام/یوزرنیم ندارد، پس پیامِ او دست
    # نمی‌خورد.
    if not result["identity_strong"]:
        try:
            from modules.admin_tools import has_admin_permission

            if has_admin_permission(chat_id, user_id, P.sender_username(sender)):
                return False
        except Exception as exc:  # noqa: BLE001 — قابلیت نکند نه گروه را
            logger.warning("ad-spam guard admin check failed: %r", exc)
            return False

    if not result["is_spam"]:
        return False

    # --- حذف از طریق صف اختصاصی ---
    queued = False
    if _delete_queue is not None:
        queued = _delete_queue.submit(chat_id, message_id)
    else:
        queued = await _direct_delete(bot, chat_id, message_id)

    try:
        bot.logger.log_info(
            "AD SPAM GUARD HIT chat_id=%s user_id=%s message_id=%s score=%s "
            "signals=%s queued=%s"
            % (
                chat_id,
                user_id,
                message_id,
                result["score"],
                ",".join(k for k, v in result["signals"].items() if v),
                queued,
            )
        )
    except Exception:
        pass

    # --- شمارش اخطار + پیام هشدار ---
    try:
        count = state.add_warn(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ad-spam warn counter failed: %r", exc)
        count = 0

    # پیامِ هشدار به‌صورتِ مستقل فرستاده می‌شود: پیامِ متخلف همین حالا حذف
    # شده و ریپلای روی آن یا خطا می‌دهد یا نقل‌قولِ شکسته می‌سازد.
    try:
        await _send(bot, event, chat_id, texts.render_warn(_display(sender), count))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ad-spam warn send failed: %r", exc)

    return True


def _display(sender) -> str:
    try:
        from modules.user_display import format_user

        return format_user(sender)
    except Exception:
        return P.sender_name(sender) or "کاربر"


async def _direct_delete(bot, chat_id, message_id) -> bool:
    """حذف مستقیم وقتی صف متصل نیست (مسیر کم‌هزینه/تست)."""
    try:
        await bot.client.delete_messages(int(chat_id), [int(message_id)])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ad-spam direct delete failed: %r", exc)
        return False


# ---------------------------------------------------------------------------
# دستورهای روشن/خاموش
# ---------------------------------------------------------------------------
def _is_admin(chat_id, user_id, sender) -> bool:
    try:
        from modules.admin_tools import has_admin_permission

        return bool(
            has_admin_permission(chat_id, user_id, P.sender_username(sender))
        )
    except Exception:
        return False


async def _denied(bot, event) -> bool:
    try:
        from modules.admin_help_builder import ADMIN_PERMISSION_DENIED_TEXT

        await event.reply(ADMIN_PERMISSION_DENIED_TEXT)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# نرمال‌سازیِ کلمهٔ «تایید/لغو»
# ---------------------------------------------------------------------------
#: نشانه‌ها/قاب‌هایی که در قالبِ کارت می‌آیند یا کاربر ممکن است بنویسد.
_WORD_NOISE = (
    "▪▫◾◽◼◻■□●○◆◇◈★☆✅☑✔❌✖✗⚠💥🔹🔸•·«»‹›\"'`.,!:;،؛_*#-–—/+"
)


def _clean_word(text: str) -> str:
    """کلمه را برای مقایسه یکدست می‌کند (نشانه/نیم‌فاصله/اعراب/تکرار).

    «▪️تایید»، «تأیید»، «تایيد»، «✅ تایید» و «تــایید» همه به یک چیز
    می‌رسند؛ وگرنه «تایید»ِ کاربر به مسیرِ دیگری (گزارش) می‌افتاد.
    """
    value = str(text or "")
    for ch in ("\u200c", "\u200f", "\u200e", "\ufeff", "\u0640",
               "\u064b", "\u064c", "\u064d", "\u064e", "\u064f",
               "\u0650", "\u0651", "\u0652", "\u0653", "\u0654", "\u0655"):
        value = value.replace(ch, "")
    for src, dst in (("ي", "ی"), ("ك", "ک"), ("أ", "ا"), ("إ", "ا"),
                     ("آ", "ا"), ("ٱ", "ا"), ("ة", "ه"), ("ۀ", "ه")):
        value = value.replace(src, dst)
    for ch in _WORD_NOISE:
        value = value.replace(ch, " ")
    value = value.replace("\ufe0f", "")
    return " ".join(value.split())


_CONFIRM_ROOTS = frozenset({"تایید", "تصویب", "قبول", "اوکی", "ok"})
_CANCEL_ROOTS = frozenset({"لغو", "انصراف", "نه", "کنسل", "cancel"})


def is_confirm_word(text: str) -> bool:
    """آیا پیام یعنی «تایید» (با هر نشانه/اعراب/شکلِ نوشتاری)؟"""
    return _clean_word(text) in _CONFIRM_ROOTS


def is_cancel_word(text: str) -> bool:
    """آیا پیام یعنی «لغو»؟"""
    return _clean_word(text) in _CANCEL_ROOTS


def has_pending_confirm(chat_id) -> bool:
    """آیا کارتِ تأییدیه در این گروه منتظرِ تصمیم است؟ (بدون RPC)"""
    try:
        return bool(state.has_pending(chat_id))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# ارسال
# ---------------------------------------------------------------------------
async def _send(bot, event, chat_id, text, entities=None):
    """پیام را **مستقل** (بدونِ ریپلای روی دستور) می‌فرستد.

    در اسکرین‌شاتِ درست، کارتِ تأییدیه هیچ نقل‌قولی از پیامِ «تبلیغ گر روشن»
    ندارد؛ پس ``event.reply`` فقط «آخرین راهِ چاره» است.
    """
    payload = {"formatting_entities": entities} if entities else {}
    target = None
    try:
        from modules.manage_features import peers

        target = peers.group_peer(bot, chat_id)
    except Exception:  # noqa: BLE001 — بدونِ helpers هم باید کار کند
        target = None
    for candidate in (target, chat_id):
        if candidate is None:
            continue
        try:
            return await bot.client.send_message(candidate, text, **payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ad-spam send via %r failed: %r", candidate, exc)
    try:
        return await event.reply(text, **payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ad-spam card reply failed: %r", exc)
        return None


async def handle_toggle(bot, event, chat_id, user_id, sender, mode: str) -> bool:
    """«تبلیغ گر روشن/خاموش» را اجرا می‌کند. خروجی: رسیدگی شد؟

    «روشن» مستقیم فعال نمی‌کند: اول **کارتِ تأییدیه** (عیناً مطابق اسکرین‌شات)
    به‌صورتِ پیامِ مستقل فرستاده می‌شود و منتظرِ «تایید/لغو» می‌ماند.
    «خاموش» مستقیم است.
    """
    if not _is_admin(chat_id, user_id, sender):
        return await _denied(bot, event)

    if mode == "on":
        state.request_confirm(chat_id, user_id)
        try:
            bot.logger.log_info(
                "AD SPAM GUARD CONFIRM REQUESTED chat_id=%s user_id=%s"
                % (chat_id, user_id)
            )
        except Exception:
            pass
        text, entities = texts.confirm_enable()
        sent = await _send(bot, event, chat_id, text, entities)
        state.set_confirm_card(chat_id, getattr(sent, "id", None))
        return True

    storage.disable(chat_id)
    state.clear_confirm(chat_id)
    try:
        bot.logger.log_info(
            "AD SPAM GUARD DISABLED chat_id=%s user_id=%s" % (chat_id, user_id)
        )
    except Exception:
        pass
    await _send(bot, event, chat_id, texts.DISABLED_MESSAGE)
    return True


def _reply_to_message_id(event):
    """شناسهٔ پیامی که این پیام روی آن ریپلای شده (اگر باشد)."""
    reply_to = getattr(getattr(event, "message", None), "reply_to", None)
    if reply_to is None:
        reply_to = getattr(event, "reply_to", None)
    if reply_to is None:
        return None
    return (getattr(reply_to, "reply_to_msg_id", None)
            or getattr(reply_to, "reply_to_id", None))


async def handle_confirm(
    bot, event, chat_id, user_id, sender, *, accept: bool
) -> bool:
    """پاسخِ «تایید/لغو» کارت را پردازش می‌کند.

    قواعدِ ثابتِ این مسیر (رفعِ باگِ کارفرما):

    * تا وقتی کارتی در این گروه منتظرِ تصمیم است، کلمهٔ «تایید/لغو» **همیشه**
      همین‌جا مصرف می‌شود و هرگز به مسیرِ «گزارش» نمی‌افتد
      («گزارش‌دهنده نمی‌تواند تصمیم بگیرد»).
    * هر ادمین/مالکِ همان گروه می‌تواند تأیید کند، نه فقط کسی که دستور را
      زده؛ در عوض کاربرِ بی‌مجوز پیامِ روشنِ «دسترسی ندارید» می‌گیرد و کارت
      باز می‌ماند.
    * اگر وضعیت پس از ری‌استارت از دست رفته باشد، «تایید» با **ریپلای روی
      خودِ کارت** هم پذیرفته می‌شود.
    """
    pending_user = state.pending_user(chat_id)
    if pending_user is None:
        # راهِ دوم: ریپلای روی همان کارتِ تأییدیه.
        if state.is_pending_card(chat_id, _reply_to_message_id(event)):
            pass
        else:
            return False

    if not _is_admin(chat_id, user_id, sender):
        # کلمه مصرف می‌شود تا به مسیرِ گزارش نرود، ولی کارت دست‌نخورده
        # می‌ماند تا ادمینِ واقعی تصمیم بگیرد.
        return await _denied(bot, event)

    state.clear_confirm(chat_id)

    if not accept:
        try:
            bot.logger.log_info(
                "AD SPAM GUARD CANCELLED chat_id=%s user_id=%s"
                % (chat_id, user_id)
            )
        except Exception:
            pass
        await _send(bot, event, chat_id, texts.CANCELLED_MESSAGE)
        return True

    storage.enable(chat_id)
    try:
        bot.logger.log_info(
            "AD SPAM GUARD ENABLED chat_id=%s user_id=%s" % (chat_id, user_id)
        )
    except Exception:
        pass
    await _send(bot, event, chat_id, texts.ENABLED_MESSAGE)
    return True
