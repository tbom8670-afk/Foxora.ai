"""📣 ارسال تبلیغات به پیوی کاربران — فقط مالک سراسری ربات.

مثل «اطلاع رسانی» است (پرامپت ← متن با قالب‌بندی ← پیش‌نمایش ←
تایید/لغو ← ارسال ← گزارش)، با یک تفاوت: مقصد گروه‌ها نیستند؛ همهٔ
کاربرانی‌اند که با حساب ربات **گفت‌وگوی خصوصی** دارند — یعنی قبلاً به
ربات پیام داده‌اند یا ربات قبلاً به آن‌ها پیام داده است. این مخاطبان از
دایالوگ‌های خصوصی حساب خوانده می‌شوند (``iter_dialogs``)؛ دیتابیس
جداگانه‌ای ساخته نمی‌شود.

قراردادها:
* فقط مالک سراسری (``is_global_owner``)؛ برای بقیه کاملاً بی‌صداست.
* متن با همان entityهای پیام مالک ارسال می‌شود تا بولد/نقل‌قول و…
  دقیقاً مثل اطلاع‌رسانی حفظ شود.
* ارسال تک‌تک و با مکث انجام می‌شود (حساب کاربری است، نه Bot API)؛
  FloodWait رعایت و هر کاربر حداکثر یک بار دوباره امتحان می‌شود.
  کاربران مسدود/حذف‌شده فقط در شمارندهٔ «ناموفق» می‌آیند.
* هم‌زمان فقط یک ارسال همگانی (اطلاع‌رسانی یا تبلیغات) در جریان است
  (همان قفل سراسری اطلاع‌رسانی).

دستور: «ارسال تبلیغات» (نیم‌فاصله و چسبیده هم پذیرفته می‌شود).
"""
import asyncio
import os

from handlers.broadcast_handler import (
    _extract_message_entities,
    _shift_entities,
    _u16_len,
)
from modules.broadcast_state import (
    acquire_broadcast_slot,
    begin,
    clear,
    consume_confirmation,
    get,
    normalize_broadcast_trigger,
    normalize_command_text,
    release_broadcast_slot,
    set_message,
)
from modules.group_id import normalize_group_id
from modules.owner_check import is_global_owner


PROMPT = "📣 متن تبلیغ را ارسال کنید."

PREVIEW_HEADER = (
    "━━━━━━━━━━━━━━\n\n"
    "📣 پیش‌نمایش تبلیغ\n\n"
)
PREVIEW_FOOTER = (
    "\n\n"
    "━━━━━━━━━━━━━━\n\n"
    "ارسال شود؟\n\n"
    "✅ تایید\n"
    "❌ لغو"
)

#: دستورهای شروع ارسال تبلیغات (پس از نرمال‌سازی نیم‌فاصله/حروف عربی).
ADS_TRIGGERS = frozenset({"ارسال تبلیغات", "ارسال تبلیغ"})
_CONFIRM_WORDS = frozenset({"تایید", "✅ تایید", "تأیید"})
_CANCEL_WORDS = frozenset({"لغو", "❌ لغو"})


def _state_key(owner_id):
    """کلید session تبلیغات؛ جدا از session اطلاع‌رسانی همان مالک."""
    return f"ads:{owner_id}"


def match_ads_trigger(text):
    """«ارسال تبلیغات» با هر املای رایج (نیم‌فاصله/چسبیده) یا None."""
    normalized = normalize_broadcast_trigger(text)
    if normalized in ADS_TRIGGERS:
        return normalized
    compact = "".join(normalized.split())
    if compact in {"ارسالتبلیغات", "ارسالتبلیغ"}:
        return "ارسال تبلیغات"
    return None


def has_ads_session(owner_id):
    return get(_state_key(owner_id)) is not None


def _send_interval_seconds():
    """مکث بین دو ارسال پیوی؛ قابل تنظیم با BOT_ADS_SEND_INTERVAL."""
    try:
        value = float(os.environ.get("BOT_ADS_SEND_INTERVAL", "1.0"))
    except (TypeError, ValueError):
        value = 1.0
    return min(10.0, max(0.0, value))


async def _ads_reply(bot, event, text, entities=None):
    message = await event.reply(text, formatting_entities=entities or None)
    if message is not None:
        message_id = getattr(message, "id", None)
        if message_id is not None:
            if not hasattr(bot, "private_ads_bot_message_ids"):
                bot.private_ads_bot_message_ids = set()
            bot.private_ads_bot_message_ids.add(message_id)
    return message


async def _collect_private_audience(bot):
    """مخاطبان پیوی از دایالوگ‌های خصوصی حساب ربات.

    هر دایالوگ کاربری یعنی یا کاربر قبلاً به ربات پیام داده یا ربات به
    او پیام داده است. حذف می‌شوند: خود حساب ربات، مالک(ها)ی سراسری،
    بات‌ها و حساب‌های حذف‌شده. خروجی: فهرست ``(user_id, target)`` بدون
    تکرار، به ترتیب دایالوگ‌ها.
    """
    audience = {}
    try:
        async for dialog in bot.client.iter_dialogs():
            if not getattr(dialog, "is_user", False):
                continue
            entity = getattr(dialog, "entity", None)
            user_id = getattr(dialog, "id", None)
            if user_id is None:
                user_id = getattr(entity, "id", None)
            if user_id is None:
                continue
            try:
                user_id = int(user_id)
            except (TypeError, ValueError):
                continue
            key = str(user_id)
            if key in audience:
                continue
            if getattr(entity, "bot", False):
                continue
            if getattr(entity, "deleted", False):
                continue
            if user_id == int(getattr(bot, "bot_account_id", 0) or 0):
                continue
            if is_global_owner(user_id):
                continue
            audience[key] = (user_id, entity if entity is not None else user_id)
    except Exception as error:
        bot.logger.log_error(f"AD BROADCAST AUDIENCE FAILED: {error!r}")
    return list(audience.values())


async def _send_ads(bot, text, entities=None):
    """ارسال تبلیغ به همهٔ مخاطبان پیوی؛ (جمع، موفق، ناموفق)."""
    entities = list(entities) if entities else []
    targets = await _collect_private_audience(bot)
    total = len(targets)
    successful = 0
    failed = 0
    interval = _send_interval_seconds()
    bot.logger.log_info(
        f"AD BROADCAST SEND START targets={total} text_len={len(text)}"
    )
    for index, (user_id, target) in enumerate(targets):
        delivered = False
        for attempt in (1, 2):
            try:
                await bot.client.send_message(
                    target,
                    text,
                    formatting_entities=entities or None,
                )
                delivered = True
                break
            except Exception as error:
                wait_seconds = getattr(error, "seconds", None)
                if (attempt == 1
                        and isinstance(wait_seconds, (int, float))
                        and wait_seconds > 0):
                    # FloodWait سروش‌پلاس: صبر می‌کنیم و همان کاربر را
                    # یک بار دیگر امتحان می‌کنیم، نه اینکه جا بیفتد.
                    bot.logger.log_info(
                        "AD BROADCAST FLOODWAIT "
                        f"user_id={user_id} seconds={wait_seconds}"
                    )
                    await asyncio.sleep(min(float(wait_seconds) + 1, 120))
                    continue
                failed += 1
                bot.logger.log_error(
                    f"AD BROADCAST USER FAILED user_id={user_id}: {error!r}"
                )
                break
        if delivered:
            successful += 1
        if (index + 1) % 25 == 0:
            bot.logger.log_info(
                "AD BROADCAST PROGRESS "
                f"done={index + 1}/{total} successful={successful} "
                f"failed={failed}"
            )
        if interval and index + 1 < total:
            await asyncio.sleep(interval)
    bot.logger.log_info(
        "AD BROADCAST SEND RESULT "
        f"total={total} successful={successful} failed={failed}"
    )
    return total, successful, failed


def _preview(text, audience_count, entities=None):
    """پیش‌نمایش تبلیغ با شمارش مخاطبان؛ شیفت entityها مثل اطلاع‌رسانی."""
    prefix = PREVIEW_HEADER
    if audience_count is not None:
        prefix += f"👥 مخاطبان پیوی: {audience_count}\n\n"
    preview_text = f"{prefix}{text}{PREVIEW_FOOTER}"
    preview_entities = _shift_entities(entities, _u16_len(prefix))
    return preview_text, preview_entities


async def handle_private_ads(bot, event, owner_id, text, origin_key=None):
    """workflow تبلیغات؛ True یعنی پیام متعلق به این مسیر بود.

    ``origin_key``: کلید گروه وقتی دستور از داخل گروه شروع شده؛ ``None``
    یعنی مسیر پیوی — دقیقاً قرارداد مسیر گروهی اطلاع‌رسانی.
    """
    if not is_global_owner(owner_id):
        return False
    raw_text = getattr(getattr(event, "message", None), "message", None) or text or ""
    text = normalize_command_text(text if text is not None else raw_text)
    entities = _extract_message_entities(event)
    key = _state_key(owner_id)
    state = get(key)

    if match_ads_trigger(text) is not None:
        begin(key, origin=origin_key)
        bot.logger.log_info(f"AD BROADCAST START owner_id={owner_id}")
        await _ads_reply(bot, event, PROMPT)
        return True

    if not state:
        return False

    state_origin = state.get("origin")
    if state_origin is not None:
        event_chat_key = str(normalize_group_id(getattr(event, "chat_id", None)))
        if event_chat_key != str(state_origin):
            return False

    if state["phase"] == "awaiting_confirmation":
        if text in _CANCEL_WORDS:
            clear(key)
            await _ads_reply(bot, event, "❌ ارسال تبلیغات لغو شد.")
            return True

        if text in _CONFIRM_WORDS:
            ad_text, ad_entities = consume_confirmation(key)
            if ad_text is None:
                return False
            # همان قفل سراسری اطلاع‌رسانی: دو ارسال همگانی هم‌زمان نه.
            if not acquire_broadcast_slot(owner_id):
                await _ads_reply(
                    bot, event, "⏳ یک ارسال همگانی در حال انجام است."
                )
                return True
            try:
                total, successful, failed = await _send_ads(
                    bot, ad_text, ad_entities
                )
                await _ads_reply(
                    bot,
                    event,
                    "✅ ارسال تبلیغات پایان یافت.\n\n"
                    f"مخاطبان پیوی: {total}\n"
                    f"ارسال موفق: {successful}\n"
                    f"ارسال ناموفق: {failed}"
                )
                bot.logger.log_info(
                    f"AD BROADCAST FINISHED owner_id={owner_id}"
                )
            finally:
                release_broadcast_slot(owner_id)
                clear(key)
            return True

        await _ads_reply(
            bot, event, "برای ارسال «✅ تایید» یا برای لغو «❌ لغو» را ارسال کنید."
        )
        return True

    if state["phase"] == "awaiting_message":
        if text in _CONFIRM_WORDS or text in _CANCEL_WORDS:
            await _ads_reply(bot, event, "📣 ابتدا متن تبلیغ را ارسال کنید.")
            return True

        # بدنهٔ خام ذخیره می‌شود تا offsetهای entity جابه‌جا نشوند.
        set_message(key, raw_text, entities)
        audience = await _collect_private_audience(bot)
        preview_text, preview_entities = _preview(
            raw_text, len(audience), entities
        )
        bot.logger.log_info(
            "AD BROADCAST PREVIEW "
            f"owner_id={owner_id} audience={len(audience)}"
        )
        await _ads_reply(bot, event, preview_text, preview_entities)
        return True

    return True


async def handle_group_ads(bot, event, owner_id, text):
    """مسیر گروهی «ارسال تبلیغات» — همان workflow پیوی، ورود از گروه.

    فقط برای مالک سراسری صدا زده می‌شود؛ بدنه و تایید فقط از همان گروه
    پذیرفته می‌شوند و مقصد نهایی همچنان پیوی کاربران است.
    """
    message_id = getattr(getattr(event, "message", None), "id", None)
    if (
        getattr(event, "out", False)
        and message_id in getattr(bot, "private_ads_bot_message_ids", set())
    ):
        return False

    chat_key = str(normalize_group_id(getattr(event, "chat_id", None)))
    normalized = normalize_command_text(text)
    if match_ads_trigger(normalized) is not None:
        return await handle_private_ads(
            bot, event, owner_id, text, origin_key=chat_key
        )
    state = get(_state_key(owner_id))
    if state and str(state.get("origin") or "") == chat_key:
        return await handle_private_ads(bot, event, owner_id, text)
    return False
