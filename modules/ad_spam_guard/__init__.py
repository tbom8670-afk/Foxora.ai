"""ضدِ ربات‌های تبلیغاتی — قابلیت مستقل با صف حذفِ اختصاصی.

این بسته عمداً از مسیرهای ضد-اسپم موجود ربات (``modules.big_spam``،
``SpamDetector``، ``modules.bot_detector``) جدا است:

* تشخیص بر پایهٔ **هویتِ فرستنده** (نام/یوزرنیم/سری ایموجی) **به‌همراه**
  محتوای پیام، تا پیامِ کاربرِ عادی هرگز اشتباهی حذف نشود؛
* صف حذفِ اختصاصی (``queue.AdSpamDeleteQueue``) که با صف‌های اصلی تداخل ندارد؛
* روشن/خاموشِ per-group با دستورهای «تبلیغ گر روشن» / «تبلیغ گر خاموش».

⚠️ نکتهٔ مهم (باگِ واقعیِ کارفرما): همهٔ نام‌هایی که «بیرون» از این بسته
استفاده می‌شوند باید در ``__all__`` و در ایمپورت‌های زیر باشند. قبلاً
``handle_confirm`` / ``is_confirm_word`` / ``is_cancel_word`` ایمپورت نشده
بودند؛ نتیجه این بود که در مسیرِ «تایید» یک ``AttributeError`` رخ می‌داد،
خطا در ``message_handler`` بلعیده می‌شد و همان «تایید» به مسیرِ «گزارش»
می‌افتاد ⇒ پیامِ «گزارش‌دهنده نمی‌تواند تصمیم بگیرد».
"""

from . import behavior, patterns, queue, state, storage, texts  # noqa: F401
from .detector import (  # noqa: F401
    bind_queue,
    evaluate,
    guard_message,
    handle_confirm,
    handle_toggle,
    has_pending_confirm,
    is_cancel_word,
    is_confirm_word,
)
from .queue import AdSpamDeleteQueue  # noqa: F401

__all__ = [
    "AdSpamDeleteQueue",
    "bind_queue",
    "evaluate",
    "guard_message",
    "handle_confirm",
    "handle_toggle",
    "has_pending_confirm",
    "is_cancel_word",
    "is_confirm_word",
    "behavior",
    "patterns",
    "queue",
    "state",
    "storage",
    "texts",
]
