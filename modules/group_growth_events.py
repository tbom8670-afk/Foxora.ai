"""📊 ثبتِ رویدادهای عضویت/خروجِ گروه برای دستور «نمودار».

این ماژول هیچ پیامی نمی‌فرستد و هیچ RPC سنگینی انجام نمی‌دهد. سه منبعِ
رخداد دارد، چون سروش پلاس بسته به نوعِ گروه یکی از آن‌ها را می‌فرستد:

1. **``ChatAction``** — رویدادِ رسمیِ ورود/خروج (بیشتر در کانال‌ها و
   سوپرگروه‌ها با فهرستِ اعضای مخفی، و در بعضی نسخه‌های کلاینت).
2. **پیامِ سرویس در خودِ گروه** — در گروه‌های عادی، «X به گروه پیوست» و
   «Y گروه را ترک کرد» به‌صورتِ پیامِ سرویس (``MessageService``) می‌آید
   و ``ChatAction`` ای وجود ندارد. دقیقاً همین حالت باعث می‌شد ترکِ عضو
   در یک گروهِ ۸ نفره هم دیده نشود.
3. **همگام‌سازی با فهرستِ اعضا** — وقتی دستور «نمودار» اجرا می‌شود،
   فهرستِ واقعی اعضا با دفتر مقایسه و اختلاف جبران می‌شود (برای گروهِ
   بزرگ هم کار می‌کند؛ صفحه‌به‌صفحه تا انتها).

شروعِ پایش اولین بار که ربات گروه را می‌بیند ثبت می‌شود (نه لحظهٔ اجرای
دستور)، تا ترک/ورودِ همان روزِ اول هم شمرده شود.
"""
from __future__ import annotations

from modules import group_growth

_registered = set()

#: نام‌های کلاسِ پیامِ سرویس که «ورود» یا «خروج» را نشان می‌دهند.
_JOIN_SERVICE_NAMES = {
    "MessageActionChatAddUser",
    "MessageActionChatJoinedByLink",
    "MessageActionChatJoinedByRequest",
    "MessageActionChatMigrateTo",
}
_LEAVE_SERVICE_NAMES = {
    "MessageActionChatDeleteUser",
    "MessageActionChatMigrateFrom",
}


def _log(bot, message, error=False):
    logger = getattr(bot, "logger", None)
    if logger is None:
        return
    try:
        if error:
            logger.log_error(message)
        else:
            logger.log_info(message)
    except Exception:
        pass


def _ids(event):
    ids = getattr(event, "user_ids", None)
    if ids:
        return [int(value) for value in ids if value]
    single = getattr(event, "user_id", None)
    return [int(single)] if single else []


def _wait_for_peer_ids(event, wanted=9, timeout=0.0):
    """``event`` کامل شود (برای دسترسی به ``chat_id``) — بدونِ معطلیِ طولانی."""
    import asyncio

    async def _complete():
        try:
            from splusthon import utils as _utils  # noqa: F401
        except Exception:
            pass
        try:
            await event._refetch_event()  # نوعِ مخفیِ splusthon/telethon
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if timeout > 0:
        try:
            loop.run_until_complete(asyncio.wait_for(_complete(), timeout))
        except Exception:
            pass
    else:
        loop.create_task(_complete())


async def handle_chat_action(bot, event) -> None:
    """ثبتِ ورود/خروج از رویدادِ رسمیِ ``ChatAction``."""
    try:
        if not getattr(event, "chat_id", None):
            # ترک/ورودِ اعضا در اولین فرصت شناسهٔ گروه را می‌دهد.
            _wait_for_peer_ids(event)
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        user_ids = _ids(event)
        joined = bool(getattr(event, "user_joined", False)
                      or getattr(event, "user_added", False))
        left = bool(getattr(event, "user_left", False)
                    or getattr(event, "user_kicked", False))
        if not joined and not left:
            return
        group_growth.start_tracking(chat_id)
        if not user_ids:
            # «خروجِ خودم»: کاربرِ رویداد همان کسی است که رفته است.
            me_id = getattr(getattr(bot, "client", None), "_self_id", None)
            sender_id = getattr(getattr(event, "sender_id", None), "user_id", None) \
                or getattr(event, "sender_id", None)
            if left and sender_id and (me_id is None or sender_id != me_id):
                user_ids = [int(sender_id)]
        record = group_growth.record_join if joined else group_growth.record_leave
        for user_id in user_ids:
            record(chat_id, user_id, source="event")
        _log(bot, "GROWTH EVENT "
                  f"chat_id={chat_id} joined={joined} left={left} "
                  f"count={len(user_ids)}")
    except Exception as error:
        _log(bot, f"GROWTH EVENT FAILED error={error!r}", error=True)


def _service_action_name(message):
    action = getattr(message, "action", None)
    if action is None:
        return None
    return type(action).__name__


def _as_int(value):
    """شناسهٔ عددی از هر شکلی: int، آبجکتِ کاربر، یا Peer."""
    if value is None:
        return None
    for attr in ("id", "user_id", "channel_id"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, int):
            return candidate
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _service_user_ids(message, action):
    """شناسهٔ کاربر(های) دخیل در پیامِ سرویس.

    ``MessageActionChatDeleteUser`` فیلدِ ``user_id`` را به شکلِ عدد یا
    ``PeerUser`` می‌دهد و ``MessageActionChatAddUser`` فهرستِ ``users``؛
    هر دو پوشش داده می‌شوند.
    """
    ids = []
    for attr in ("users", "user_ids"):
        for value in getattr(action, attr, None) or []:
            resolved = _as_int(value)
            if resolved:
                ids.append(resolved)
    single = _as_int(getattr(action, "user_id", None))
    if single:
        ids.append(single)
    if not ids:
        from_id = getattr(message, "from_id", None)
        sender = _as_int(from_id) or _as_int(
            getattr(message, "sender_id", None))
        if sender:
            ids.append(sender)
    # بدونِ تکرار، با حفظِ ترتیب
    seen, unique = set(), []
    for value in ids:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def is_service_membership_message(message):
    """آیا این پیامِ سرویس، «ورود» یا «خروجِ» یک عضو را نشان می‌دهد؟"""
    name = _service_action_name(message)
    if name is None:
        return False
    return name in _JOIN_SERVICE_NAMES or name in _LEAVE_SERVICE_NAMES


def handle_service_message(bot, chat_id, message) -> bool:
    """ثبتِ ورود/خروج از پیامِ سرویسِ داخلیِ گروه.

    خروجی: ``True`` اگر رخدادی ثبت شد. مخصوصِ گروه‌های عادی است که
    ``ChatAction`` نمی‌فرستند.
    """
    try:
        name = _service_action_name(message)
        if name is None or chat_id is None:
            return False
        if name not in _JOIN_SERVICE_NAMES and name not in _LEAVE_SERVICE_NAMES:
            return False
        action = message.action
        removed = set()
        for value in getattr(action, "removed_users", None) or []:
            resolved = _as_int(value)
            if resolved:
                removed.add(resolved)
        # «ChatDeleteUser» یعنی اخراج/خروج؛ خودِ فرستنده هم ممکن است رفته باشد.
        actor = getattr(getattr(message, "from_id", None), "user_id", None)
        user_ids = _service_user_ids(message, action)
        joined = name in _JOIN_SERVICE_NAMES
        if joined:
            # در «افزودن عضو»، حذف‌شدگان هم در فهرست‌اند و نباید بشمار آیند.
            user_ids = [value for value in user_ids if value not in removed]
        group_growth.start_tracking(chat_id)
        recorded = 0
        for user_id in user_ids:
            if joined:
                if group_growth.record_join(chat_id, user_id, source="service"):
                    recorded += 1
            elif group_growth.record_leave(chat_id, user_id, source="service"):
                recorded += 1
        if not joined and not user_ids and actor:
            if group_growth.record_leave(chat_id, actor, source="service"):
                recorded += 1
        if recorded:
            _log(bot, "GROWTH SERVICE EVENT "
                      f"chat_id={chat_id} action={name} count={recorded}")
        return bool(recorded)
    except Exception as error:
        _log(bot, f"GROWTH SERVICE EVENT FAILED error={error!r}", error=True)
        return False


#: چند پیامِ آخرِ گروه برای جست‌وجوی پیام‌های سرویسِ ورود/خروج.
RECENT_SCAN_LIMIT = int(__import__("os").environ.get(
    "GROWTH_RECENT_SCAN", "150"))


async def recent_service_scan(bot, chat_id) -> int:
    """یک‌بار پیام‌های اخیرِ گروه را می‌خواند و ورود/خروج‌های جاافتاده را
    از پیام‌های سرویس ثبت می‌کند.

    سقفِ تعداد از متغیرِ ``GROWTH_RECENT_SCAN`` خوانده می‌شود (پیش‌فرض ۱۵۰).
    این کار با دستور «نمودار» انجام می‌شود، نه در مسیرِ هر پیام؛ پس برای
    گروهِ ۴۰ هزار نفری هم هزینهٔ اضافی روی پیام‌های عادی ندارد.
    """
    if RECENT_SCAN_LIMIT <= 0:
        return 0
    try:
        messages = await bot.client.get_messages(chat_id, limit=RECENT_SCAN_LIMIT)
    except Exception as error:
        _log(bot, f"GROWTH RECENT SCAN FETCH FAILED chat_id={chat_id} "
                  f"error={error!r}", error=True)
        return 0
    recorded = 0
    for message in messages or []:
        try:
            if not is_service_membership_message(message):
                continue
            if handle_service_message(bot, chat_id, message):
                recorded += 1
        except Exception:
            continue
    if recorded:
        _log(bot, f"GROWTH RECENT SCAN chat_id={chat_id} recorded={recorded}")
    return recorded


async def register(bot) -> bool:
    """نصبِ شنونده‌ها: ``ChatAction`` + پیام‌های سرویسِ عضویت (idempotent)."""
    return _install_listeners(bot)


def register_sync(bot) -> bool:
    """نسخهٔ همگامِ :func:`register` برای فراخوانی از راه‌اندازیِ ربات.

    ثبتِ شنونده هیچ await ای ندارد، پس همیشه همگام انجام می‌شود؛ هیچ
    coroutine ای ساخته و رها نمی‌شود.
    """
    return _install_listeners(bot)


def _install_listeners(bot) -> bool:
    """نصبِ شنونده‌ها بدونِ نیاز به حلقهٔ asyncio."""
    marker = id(bot)
    if marker in _registered:
        return False
    client = getattr(bot, "client", None)
    on = getattr(client, "on", None)
    if on is None:
        return False

    async def _on_chat_action(event):
        await handle_chat_action(bot, event)

    async def _on_new_message(event):
        try:
            message = getattr(event, "message", None)
            if message is None or not is_service_membership_message(message):
                return
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                return
            handle_service_message(bot, chat_id, message)
        except Exception as error:
            _log(bot, f"GROWTH NEW-MESSAGE HOOK FAILED error={error!r}",
                 error=True)

    filters = []
    for module_name in ("splusthon", "telethon"):
        try:
            module = __import__(module_name, fromlist=["events"])
            filters = [module.events.ChatAction(), module.events.NewMessage()]
            break
        except Exception:
            continue
    if not filters:
        # محیطِ بدونِ splusthon (تست): هر دو شنونده با فیلترِ مستقل نصب می‌شوند.
        filters = [_AnyEvent(), _AnyEvent()]
    try:
        on(filters[0])(_on_chat_action)
        if len(filters) > 1:
            on(filters[1])(_on_new_message)
        _registered.add(marker)
        _log(bot, "GROWTH RECORDER REGISTERED (ChatAction + service messages)")
        return True
    except Exception as error:
        _log(bot, f"GROWTH RECORDER REGISTER FAILED error={error!r}", error=True)
        return False


class _AnyEvent:
    """فیلترِ جایگزین وقتی کتابخانهٔ رویدادها در دسترس نیست (محیطِ تست)."""

    def resolve(self, event):
        return event

    def __call__(self, event):
        return True
