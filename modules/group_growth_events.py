"""📊 ثبتِ رویدادهای عضویت/خروجِ گروه برای دستور «نمودار».

این ماژول فقط یک شنوندهٔ ``ChatAction`` نصب می‌کند و هر ورود/خروج را در
دفترِ رخدادِ :mod:`modules.group_growth` ثبت می‌کند. هیچ پیامی نمی‌فرستد،
هیچ RPC سنگینی انجام نمی‌دهد و به مسیرهای موجود (هرزنگاری، عضویت اجباری،
همگام‌سازی نام گروه) دست نمی‌زند.

اگر ربات هنگامِ رویداد خاموش بوده باشد، دستور «نمودار» خودش با گرفتنِ
فهرست اعضا شکاف را جبران می‌کند (``group_growth.sync_members``).
"""
from __future__ import annotations

from modules import group_growth

_registered = set()


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


async def handle_chat_action(bot, event) -> None:
    """ثبتِ ورود/خروجِ اعضای گروه در دفترِ رخداد."""
    try:
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        user_ids = _ids(event)
        if not user_ids:
            return
        joined = bool(getattr(event, "user_joined", False)
                      or getattr(event, "user_added", False))
        left = bool(getattr(event, "user_left", False)
                    or getattr(event, "user_kicked", False))
        if not joined and not left:
            return
        # دستهٔ اعضا (اضافه‌شدنِ چند نفر با هم) هم پشتیبانی می‌شود.
        for user_id in user_ids:
            if joined:
                group_growth.record_join(chat_id, user_id, source="event")
            else:
                group_growth.record_leave(chat_id, user_id, source="event")
        _log(bot, "GROWTH EVENT "
                  f"chat_id={chat_id} joined={joined} left={left} "
                  f"count={len(user_ids)}")
    except Exception as error:
        _log(bot, f"GROWTH EVENT FAILED error={error!r}", error=True)


def register(bot) -> bool:
    """نصبِ شنوندهٔ رویداد (idempotent). خروجی: آیا ثبت شد؟"""
    marker = id(bot)
    if marker in _registered:
        return False
    client = getattr(bot, "client", None)
    on = getattr(client, "on", None)
    if on is None:
        return False
    async def _on_chat_action(event):
        await handle_chat_action(bot, event)

    filter_object = None
    for module_name in ("splusthon", "telethon"):
        try:
            module = __import__(module_name, fromlist=["events"])
            filter_object = module.events.ChatAction()
            break
        except Exception:
            continue
    if filter_object is None:
        # بدونِ فیلترِ کتابخانه: هندلر خودش رویدادهای بی‌ربط را رد می‌کند.
        filter_object = _AnyEvent()

    try:
        on(filter_object)(_on_chat_action)
        _registered.add(marker)
        _log(bot, "GROWTH RECORDER REGISTERED")
        return True
    except Exception as error:
        _log(bot, f"GROWTH RECORDER REGISTER FAILED error={error!r}", error=True)
        return False


class _AnyEvent:
    """فیلترِ جایگزین وقتی کتابخانهٔ رویدادها در دسترس نیست.

    فقط برای محیطِ تست/بدونِ splusthon است؛ منطقِ ثبت در
    :func:`handle_chat_action` هر رویدادِ بی‌ربط را نادیده می‌گیرد.
    """

    def resolve(self, event):
        return event

    def __call__(self, event):
        return True
