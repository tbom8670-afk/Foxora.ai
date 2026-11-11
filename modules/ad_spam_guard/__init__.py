"""ضدِ ربات‌های تبلیغاتی — قابلیت مستقل با صف حذفِ اختصاصی.

این بسته عمداً از مسیرهای ضد-اسپم موجود ربات (``modules.big_spam``،
``SpamDetector``، ``modules.bot_detector``) جدا است:

* تشخیص بر پایهٔ **هویتِ فرستنده** (نام/یوزرنیم/سری ایموجی) **به‌همراه**
  محتوای پیام، تا پیامِ کاربرِ عادی هرگز اشتباهی حذف نشود؛
* صف حذفِ اختصاصی (``queue.AdSpamDeleteQueue``) که با صف‌های اصلی تداخل ندارد؛
* روشن/خاموشِ per-group با دستورهای «تبلیغ گر روشن» / «تبلیغ گر خاموش».
"""

from . import patterns, queue, storage, texts  # noqa: F401
from .detector import (  # noqa: F401
    bind_queue,
    evaluate,
    guard_message,
    handle_toggle,
)
from .queue import AdSpamDeleteQueue  # noqa: F401

__all__ = [
    "AdSpamDeleteQueue",
    "bind_queue",
    "evaluate",
    "guard_message",
    "handle_toggle",
    "patterns",
    "queue",
    "storage",
    "texts",
]
