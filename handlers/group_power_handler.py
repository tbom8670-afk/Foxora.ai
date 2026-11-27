"""🔌 کنترل از راه دور ربات در گروه‌ها — فقط مالک سراسری.

دستورها (در پیوی ربات یا داخل هر گروه):

* «گروها» — فهرست گروه‌هایی که ربات همین حالا در آن‌ها فعال است
  (نام گروه + شناسه، برای استفاده در دستورهای خاموش/روشن).
* «ربات خاموش <شناسه>» — ربات دلیل را می‌پرسد؛ متن بعدی مالک (با همان
  قالب‌بندی‌اش، مثل اطلاع‌رسانی) به آن گروه فرستاده می‌شود و بلافاصله
  ربات در آن گروه خاموش می‌شود: دقیقاً وضعیت پایان انقضا
  (``active=False`` در groups.json و گیت اشتراک همهٔ پیام‌ها را
  می‌بندد). با «لغو» در مرحلهٔ دلیل، عملیات لغو می‌شود.
* «ربات روشن <شناسه>» — فعال‌سازی دوبارهٔ همان گروه از راه دور (داخل
  خود گروه هم مثل قبل «فعال» کار می‌کند). اگر مهلت گروه هم تمام شده
  باشد، فقط روشن می‌شود و برای کار کردن باید با دستورهای مهلت تمدید
  شود؛ این موضوع در پاسخ به مالک گفته می‌شود.

شناسه با هر شکل رایج پذیرفته می‌شود (مثبتِ نرمال‌شده، ‎-100…‎ یا شکل
کوتاه) و با همان کلید نرمالِ groups.json تطبیق داده می‌شود.
"""
import time

from handlers.broadcast_handler import _extract_message_entities
from modules import group_expiry, group_storage
from modules.broadcast_state import normalize_command_text
from modules.group_id import CHANNEL_ID_OFFSET, normalize_group_id
from modules.owner_check import is_global_owner

_LIST_WORDS = frozenset({"گروها", "گروه ها"})
_OFF_PREFIX = "ربات خاموش"
_ON_PREFIX = "ربات روشن"
_CANCEL_WORDS = frozenset({"لغو", "❌ لغو"})
_SESSION_TTL_SECONDS = 600.0

# sessionهای «در انتظار دلیل» مالک‌ها: owner_id -> dict. کوتاه‌عمرند و
# عمداً فقط در حافظه نگه داشته می‌شوند؛ با ری‌استارت از بین می‌روند و
# مالک کافی است دستور را دوباره بفرستد.
_sessions = {}


def match_list_command(text):
    """«گروها» با هر املای رایج (نیم‌فاصله/چسبیده) یا None."""
    normalized = normalize_command_text(text)
    if normalized in _LIST_WORDS:
        return True
    return "".join(normalized.split()) == "گروهها"


def parse_power_command(text):
    """خروجی: ``("off"|"on", شناسهٔ خام یا None)`` یا ``None``."""
    normalized = normalize_command_text(text)
    for prefix, action in ((_OFF_PREFIX, "off"), (_ON_PREFIX, "on")):
        if normalized == prefix:
            return action, None
        if normalized.startswith(prefix + " "):
            return action, normalized[len(prefix):].strip() or None
    return None


def _resolve_group(raw_id):
    """گروه را با هر شکل شناسه پیدا می‌کند: ``(key, record)`` یا None."""
    if raw_id is None:
        return None
    try:
        numeric = int(str(raw_id).strip().replace(" ", ""))
    except (TypeError, ValueError):
        return None
    key = normalize_group_id(numeric)
    data = group_storage.load_groups()
    if not isinstance(data, dict):
        return None
    for raw_key, record in data.items():
        if isinstance(record, dict) and normalize_group_id(raw_key) == key:
            return key, record
    return None


def _canonical_groups():
    """نمای نرمال‌شدهٔ groups.json: هر گروه یک بار، با جدیدترین دادهٔ مفید."""
    data = group_storage.load_groups()
    canonical = {}
    if not isinstance(data, dict):
        return canonical
    for raw_key, record in data.items():
        if not isinstance(record, dict):
            continue
        key = normalize_group_id(raw_key)
        current = canonical.get(key)
        if current is None:
            canonical[key] = dict(record)
            continue
        if record.get("active") and not current.get("active"):
            current["active"] = True
        if record.get("title") and not current.get("title"):
            current["title"] = record["title"]
    return canonical


def build_active_groups_text():
    """متن «گروها»: فقط گروه‌های فعال، نام + شناسهٔ قابل کپی."""
    rows = [
        (key, record)
        for key, record in _canonical_groups().items()
        if record.get("active")
    ]
    rows.sort(key=lambda item: str(item[1].get("title") or ""))
    if not rows:
        return "🦊 هیچ گروه فعالی ثبت نشده است."
    lines = [f"🦊 گروه‌های فعال ربات ({len(rows)} گروه):", ""]
    for index, (key, record) in enumerate(rows, 1):
        title = str(record.get("title") or "").strip() or "بدون نام"
        lines.append(f"{index}. {title}")
        lines.append(f"🆔 {key}")
        lines.append("")
    return "\n".join(lines).strip()


def is_power_command(text):
    """آیا متن یکی از دستورهای کنترل گروه است (بدون session)؟"""
    return bool(match_list_command(text) or parse_power_command(text))


def has_power_session(owner_id):
    """آیا مالک session «در انتظار دلیل» فعالی دارد؟"""
    return _live_session(owner_id) is not None


def _send_targets(key):
    """شکل‌های ممکن peer گروه برای ارسال پیام (الگوی واچر انقضا)."""
    try:
        numeric = int(key)
    except (TypeError, ValueError):
        return [key]
    bare = abs(numeric)
    if bare > CHANNEL_ID_OFFSET:
        bare -= CHANNEL_ID_OFFSET
    targets = []
    for value in (numeric, -(CHANNEL_ID_OFFSET + bare), -bare, bare):
        if value not in targets:
            targets.append(value)
    return targets


def _live_session(owner_id):
    session = _sessions.get(str(owner_id))
    if session is None:
        return None
    if session["expires"] <= time.monotonic():
        _sessions.pop(str(owner_id), None)
        return None
    return session


def _cancel_group_runtime(bot, key):
    """تایمرها/دورهای بازی گروه خاموش‌شده را مثل «غیر فعال» متوقف می‌کند."""
    try:
        numeric = int(key)
        chat_ids = {numeric, -numeric, -(CHANNEL_ID_OFFSET + numeric)}
    except (TypeError, ValueError):
        chat_ids = {key}
    timers = getattr(bot, "group_timer_tasks", None)
    if isinstance(timers, dict):
        for chat_id in chat_ids:
            for task in timers.pop(chat_id, set()) or set():
                try:
                    task.cancel()
                except Exception:
                    pass
    try:
        from modules.name_family import (
            cancel_round as cancel_name_family_round,
        )
        for chat_id in chat_ids:
            try:
                cancel_name_family_round(chat_id)
            except Exception:
                pass
    except Exception:
        pass


async def _reply(bot, event, text):
    return await event.reply(text)


async def handle_group_power(bot, event, owner_id, text):
    """نقطهٔ ورود workflow؛ True یعنی پیام مصرف شد.

    فقط مالک سراسری. session دلیل به چتی بسته است که دستور از آن آمده
    (پیوی یا همان گروه) تا متن کاربر دیگری دلیل حساب نشود.
    """
    if not is_global_owner(owner_id):
        return False
    raw_text = getattr(getattr(event, "message", None), "message", None) or text or ""
    normalized = normalize_command_text(text if text is not None else raw_text)
    chat_key = str(normalize_group_id(getattr(event, "chat_id", None)))
    session = _live_session(owner_id)

    # دستور تازه (لیست/خاموش/روشن) session قبلی را کنار می‌زند.
    is_new_command = bool(
        match_list_command(normalized) or parse_power_command(normalized)
    )
    # This command family is consumed by the core before the normal message
    # handler; only the initial command reply (never the follow-up reason)
    # should carry its owner-configured image.
    if is_new_command and not getattr(event, "is_private", False) and not (
            getattr(event, "is_edit", False)
            or getattr(event, "_is_edited_message", False)
            or getattr(getattr(event, "message", None), "edit_date", None)):
        from handlers.command_photo_handler import install_on_event
        install_on_event(bot, event, normalized)
    if session is not None and not is_new_command:
        if str(session["chat_key"]) != chat_key:
            return False
        if normalized in _CANCEL_WORDS:
            _sessions.pop(str(owner_id), None)
            await _reply(bot, event, "❌ خاموش کردن ربات لغو شد.")
            return True

        # متن دلیل: با همان قالب‌بندی مالک (مثل اطلاع‌رسانی) به گروه
        # فرستاده می‌شود و بعد ربات دقیقاً مثل پایان انقضا خاموش می‌شود.
        entities = _extract_message_entities(event)
        title = session["title"]
        delivered = False
        for target in _send_targets(session["key"]):
            try:
                await bot.client.send_message(
                    target, raw_text,
                    formatting_entities=entities or None,
                )
                delivered = True
                break
            except Exception as error:
                bot.logger.log_error(
                    "GROUP POWER OFF REASON FAILED "
                    f"group_id={target} error={error!r}"
                )
        try:
            group_storage.deactivate_group(session["key"], title)
        except Exception as error:
            bot.logger.log_error(
                "GROUP POWER OFF DEACTIVATE FAILED "
                f"group_id={session['key']} error={error!r}"
            )
        _cancel_group_runtime(bot, session["key"])
        _sessions.pop(str(owner_id), None)
        bot.logger.log_info(
            "GROUP POWER OFF "
            f"group_id={session['key']} title={title!r} "
            f"reason_delivered={delivered}"
        )
        if delivered:
            await _reply(
                bot, event,
                f"✅ دلیل به گروه «{title}» ارسال و ربات در آن خاموش شد.",
            )
        else:
            await _reply(
                bot, event,
                f"⚠️ دلیل به گروه «{title}» نرسید، ولی ربات در آن خاموش شد.",
            )
        return True

    if match_list_command(normalized):
        await _reply(bot, event, build_active_groups_text())
        return True

    parsed = parse_power_command(normalized)
    if parsed is None:
        return False
    action, raw_id = parsed
    if raw_id is None:
        await _reply(
            bot, event,
            "❌ شناسهٔ گروه را جلوی دستور بنویسید؛ مثلاً:\n"
            f"{_OFF_PREFIX} 1234567890",
        )
        return True
    resolved = _resolve_group(raw_id)
    if resolved is None:
        await _reply(
            bot, event, f"❌ گروهی با شناسهٔ «{raw_id}» پیدا نشد."
        )
        return True
    key, record = resolved
    title = str(record.get("title") or "").strip() or "بدون نام"

    if action == "on":
        if record.get("active") and not group_expiry.is_expired(key):
            await _reply(
                bot, event, f"ℹ️ ربات در گروه «{title}» از قبل فعال است."
            )
            return True
        try:
            group_storage.activate_group(key, title)
        except Exception as error:
            bot.logger.log_error(
                f"GROUP POWER ON FAILED group_id={key} error={error!r}"
            )
            await _reply(bot, event, "❌ روشن کردن ربات در این گروه ناموفق بود.")
            return True
        bot.logger.log_info(f"GROUP POWER ON group_id={key} title={title!r}")
        if group_expiry.is_expired(key):
            await _reply(
                bot, event,
                f"✅ ربات در گروه «{title}» روشن شد، اما مهلتش تمام شده "
                "است؛ با دستورهای مهلت (مثل «۵ روز») تمدیدش کنید تا "
                "دوباره کار کند.",
            )
        else:
            await _reply(
                bot, event, f"✅ ربات در گروه «{title}» روشن شد."
            )
        return True

    # action == "off"
    if not record.get("active"):
        await _reply(
            bot, event, f"ℹ️ ربات در گروه «{title}» از قبل خاموش است."
        )
        return True
    _sessions[str(owner_id)] = {
        "key": key,
        "title": title,
        "chat_key": chat_key,
        "expires": time.monotonic() + _SESSION_TTL_SECONDS,
    }
    bot.logger.log_info(
        f"GROUP POWER OFF ASK REASON group_id={key} title={title!r}"
    )
    await _reply(
        bot, event,
        f"📝 دلیل خاموش شدن ربات در گروه «{title}» را ارسال کنید.\n"
        "(متن شما عیناً به گروه فرستاده می‌شود و بعد ربات خاموش می‌شود؛ "
        "برای لغو «لغو» را بفرستید.)",
    )
    return True
