"""🦊 ضد کانال و بیوگرافی — شناسایی کانالِ متصل به پروفایل و جمله‌های بیو.

این بسته یک قابلیت مستقل است و با ضد-اسپم عادی (``SpamDetector``)،
``modules/big_spam`` و ``modules/bio_link_service`` تداخل ندارد:

* **کانال**: ``users.getFullUser`` → ``full_user.personal_channel_id`` →
  ``Channel.title`` (از وکتور ``chats`` همان پاسخ) ⇒ تطبیق با فهرست
  ``config/profile_channel_names.txt`` ⇒ حذف پیام + سکوت دائمی.
* **بیوگرافی**: ``full_user.about`` ⇒ تطبیق با فهرست
  ``config/profile_bio_sentences.txt`` ⇒ حذف پیام + سکوت ۴ ساعته.

دستورها: «ضد کانال» (روشن) و «ضد کانال خاموش» (خاموش).

⚠️ قاعدهٔ ثابت: نام‌هایی که بیرون از این بسته استفاده می‌شوند باید در
``__all__`` باشند (درسِ باگِ ``ad_spam_guard``).
"""

from . import (cleanup, filters, guard, importer, matcher,  # noqa: F401
               penalties, service, storage, texts)
from .guard import (guard_message, handle_file_command,  # noqa: F401
                    handle_toggle, parse_file_command, recover)
from .storage import toggle_mode  # noqa: F401

__all__ = [
    "cleanup",
    "filters",
    "guard",
    "importer",
    "matcher",
    "penalties",
    "service",
    "storage",
    "texts",
    "guard_message",
    "handle_file_command",
    "handle_toggle",
    "parse_file_command",
    "recover",
    "toggle_mode",
]
