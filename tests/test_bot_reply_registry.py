"""تستِ «پاکسازیِ پاسخِ ربات برای پیامِ ویرایش‌شده».

سناریو: کاربر «سلام» می‌فرستد، ربات پاسخ می‌دهد، سپس کاربر همان پیام را به
متنِ فیلترشده ویرایش می‌کند. پیامِ ویرایش‌شده حذف می‌شود و پاسخِ ربات هم باید
هم‌زمان پاک شود.

بخشِ نخست، ماژولِ ثبت (modules/bot_reply_registry) را مستقیم و بدونِ وابستگی به
splusthon می‌سنجد. بخشِ دوم، سیم‌کشیِ هندلر را در سطحِ منبع بررسی می‌کند
(چون وارد کردنِ handlers.message_handler بدونِ splusthon ممکن نیست).
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import bot_reply_registry as reg


def setup_function(_func):
    reg.clear()


def test_register_then_pop_returns_reply_id():
    chat_id = -1001234567890
    user_msg_id = 555
    bot_reply_id = 556

    reg.register(chat_id, user_msg_id, bot_reply_id)
    assert reg.peek_reply_ids(chat_id, user_msg_id) == [bot_reply_id]

    popped = reg.pop_reply_ids(chat_id, user_msg_id)
    assert popped == [bot_reply_id]
    # pop باید ورودی را پاک کند تا ویرایشِ بعدی حذفِ تکراری نسازد.
    assert reg.pop_reply_ids(chat_id, user_msg_id) == []


def test_chat_id_form_is_normalized_across_sites():
    """event.chat_id (علامت‌دار) و event_chat.id (کوتاه) باید یک کلید باشند."""
    user_msg_id = 777
    bot_reply_id = 778

    # در _schedule_reply کلید از event.chat_id ساخته می‌شود (شکلِ -100...).
    reg.register(-1009876543210, user_msg_id, bot_reply_id)
    # در بلوکِ حذف، chat_id معمولاً شکلِ کوتاهِ event_chat.id است.
    assert reg.pop_reply_ids(9876543210, user_msg_id) == [bot_reply_id]


def test_multiple_replies_to_one_message_all_tracked():
    chat_id = -10042
    user_msg_id = 10
    reg.register(chat_id, user_msg_id, 11)
    reg.register(chat_id, user_msg_id, 12)
    # ویرایشِ «سلام» به «خوبی» و سپس به متنِ فیلترشده: هر دو پاسخ باید پاک شوند.
    assert set(reg.pop_reply_ids(chat_id, user_msg_id)) == {11, 12}


def test_duplicate_reply_id_not_stored_twice():
    chat_id = -10042
    reg.register(chat_id, 1, 2)
    reg.register(chat_id, 1, 2)
    assert reg.pop_reply_ids(chat_id, 1) == [2]


def test_invalid_inputs_are_ignored():
    assert reg.pop_reply_ids(None, None) == []
    assert reg.pop_reply_ids(-1001, 0) == []
    assert reg.pop_reply_ids(-1001, -5) == []
    reg.register(-1001, 5, 0)        # شناسهٔ نامعتبرِ پاسخ
    reg.register(-1001, 5, None)
    assert reg.pop_reply_ids(-1001, 5) == []


def test_expired_entry_returns_empty(monkeypatch):
    chat_id = -1001
    reg.register(chat_id, 1, 2)
    # زمان را جلوتر از TTL ببر.
    real_monotonic = time.monotonic
    monkeypatch.setattr(reg.time, "monotonic", lambda: real_monotonic() + reg._TTL_SECONDS + 1)
    assert reg.pop_reply_ids(chat_id, 1) == []


def test_size_cap_evicts_oldest():
    chat_id = -1001
    overflow = reg._MAX_ENTRIES + 50
    for message_id in range(1, overflow + 1):
        reg.register(chat_id, message_id, message_id + 1)
    # اندازه باید زیرِ سقف بماند و قدیمی‌ترین‌ها بیرون رفته باشند.
    assert len(reg._store) <= reg._MAX_ENTRIES
    assert reg.pop_reply_ids(chat_id, 1) == []                 # قدیمی‌ترین: رفته
    assert reg.pop_reply_ids(chat_id, overflow) == [overflow + 1]  # تازه‌ترین: مانده


def test_handler_registers_reply_in_schedule_reply():
    src = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    assert "def _schedule_reply(" in src
    # ثبتِ پاسخ در هلپرِ پاسخ‌دهی.
    assert "from modules.bot_reply_registry import register" in src
    assert "extract_sent_id(sent)" in src


def test_handler_cleans_reply_on_edited_spam():
    src = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    # هلپرِ مشترکِ حذفِ پاسخِ ربات برای پیامِ ویرایش‌شده.
    assert "def _cleanup_bot_reply_for_edited_message(" in src
    assert "EDITED MESSAGE BOT REPLY CLEANUP" in src
    assert "pop_reply_ids" in src
    # فقط برای پیامِ ویرایش‌شده و فقط وقتی حذفِ هرزنامه فعال است.
    assert "if not is_edited_message:" in src
    assert 'config.get("delete_spam", True)' in src
    # از صفِ حذفِ موجود استفاده می‌کند (بدونِ RPC اضافه در مسیرِ گرم).
    assert "_queue_message_deletes(bot, chat_id, list(bot_reply_ids))" in src
    # هلپر باید در همهٔ مسیرهای حذفِ پیامِ ویرایش‌شده صدا زده شود:
    # گیتِ متخلف، گیتِ ضدِرباتِ تبلیغاتی، گیتِ ضدِکانال/بیو، اسپمِ بزرگ،
    # پیامِ معوقِ اسپمِ بزرگ، قفلِ اسپم، بلوکِ اصلیِ اسپم/فیلتر، و ویرایشِ خالی.
    # ۱ تعریف + ۸ فراخوانی.
    assert src.count("_cleanup_bot_reply_for_edited_message(") >= 9


def test_handler_cleans_reply_on_empty_edit():
    src = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    # تشخیصِ «متنِ خالی» حتی با نویسه‌های نامرئی.
    assert "def _is_blank_text(" in src
    assert "_BLANK_STRIP_CHARS" in src
    # بلوکِ پاکسازیِ پاسخِ ربات برای ویرایش به متنِ خالی، مستقل از مسیرِ اسپم.
    assert "_is_blank_text(message_text)" in src
    # ZWSP/BOM باید (به‌شکلِ دنبالهٔ فرار) در مجموعهٔ نویسه‌های «خالی» باشند.
    assert "\\u200b" in src and "\\ufeff" in src
