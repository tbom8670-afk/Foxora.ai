"""تست‌های جامع ویژگی‌ها و شخصی‌سازی‌های جدید درخواستی کاربر.

موارد تحت پوشش:
1. افزایش سقف پاکسازی پیام‌ها به 1 تا 800.
2. قالب جدید خطای دسترسی مالک:
   ⛔️فقط مالک اصلی ربات : @osine2
   ⛔️ و مالک ثبت شده : {owner_label}
   اجازه استفاده از این قابلیت را دارند
3. متن جدید خطای دسترسی ادمین:
   🏕 فقط ادمین های ثبت شده مجاز هستند
4. اخطار سکوت مجدد:
   ⚠️ این کاربر قبلا سکوت شده
5. قالب جدید پیام فعال‌سازی گروه.
6. پاسخ‌های جدید «ربات» برای کاربران ثبت‌شده:
   🤗‌ جونم {name} و 🫀 بله {name}
7. پاسخ‌های جدید گفتگوی عادی (عشقم، چیکارا میکنی).
8. حذف «بی‌ادب» و «بی ادب» از کلمات توهین‌آمیز.
9. بررسی افزوده‌شدن تمام کلمات ممنوعهٔ جدید به دیتابیس کلمات ممنوعه.
"""

import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.simple_replies import SIMPLE_REPLIES, INSULTS
from modules.group_memory import friendly_reply
from handlers.message_handler import ADMIN_PERMISSION_DENIED_TEXT, _get_owner_display


def test_bulk_delete_limit_message():
    """بررسی اینکه پیام پاکسازی به 800 ارتقا یافته است."""
    from handlers.message_handler import handle_new_message
    import inspect
    source = inspect.getsource(handle_new_message)
    assert "requested_count > 800" in source
    assert "❌ تعداد پیام باید بین 1 تا 800 باشد" in source
    assert "requested_count > 700" not in source


def test_owner_permission_template():
    """بررسی قالب خطای دسترسی مختص مالک."""
    from handlers.message_handler import _owner_permission_denied_reply
    import inspect
    source = inspect.getsource(_owner_permission_denied_reply)
    assert "⛔️فقط مالک اصلی ربات : @osine2" in source
    assert "⛔️ و مالک ثبت شده :" in source
    assert "اجازه استفاده از این قابلیت را دارند" in source


def test_admin_permission_denied_text():
    """بررسی متن خطای عدم دسترسی ادمین."""
    assert ADMIN_PERMISSION_DENIED_TEXT == "🏕 فقط ادمین های ثبت شده مجاز هستند"


def test_duplicate_mute_warning_in_source():
    """بررسی اخطار سکوت کاربر از قبل سایلنت‌شده."""
    from handlers.message_handler import handle_new_message
    import inspect
    source = inspect.getsource(handle_new_message)
    assert "⚠️ این کاربر قبلا سکوت شده" in source


def test_activation_message_template():
    """بررسی قالب جدید پیام فعال‌سازی."""
    from handlers.message_handler import send_activation_message
    import inspect
    source = inspect.getsource(send_activation_message)
    assert "🧝‍♂ مالک گروه:" in source
    assert "🦹‍♂ ادمین های گروه:" in source
    assert "روباه در گروه «{title}» فعال سازی شد" in source
    assert "کانال راهنما را مطالعه کنید" in source


def test_group_memory_robot_replies():
    """بررسی پاسخ به کلمهٔ 'ربات' برای افراد با اسم ثبت‌شده."""
    reply = friendly_reply("عماد", "ربات")
    assert reply in ("🤗‌ جونم عماد", "🫀 بله عماد")


def test_simple_replies_updates():
    """بررسی پاسخ‌های جدید گفتگوی عمومی."""
    assert SIMPLE_REPLIES.get("عشقم") == "جانم عجقم 👶"
    assert SIMPLE_REPLIES.get("چیکارا میکنی") == "سلامتی در حال مدیریت گروه 👀"
    assert SIMPLE_REPLIES.get("چیکار میکنی") == "سلامتی در حال مدیریت گروه 👀"


def test_insults_removal():
    """اطمینان از اینکه 'بی‌ادب' و 'بی ادب' دیگر در لیست INSULTS نیستند."""
    assert "بی‌ادب" not in INSULTS
    assert "بی ادب" not in INSULTS


def test_new_banned_words_present():
    """بررسی وجود تک‌تک کلمات ممنوعهٔ جدید در فایل config/banned_words.txt."""
    banned_path = ROOT / "config" / "banned_words.txt"
    content = banned_path.read_text(encoding="utf-8").splitlines()
    words_set = set(line.strip() for line in content if line.strip())

    required = [
        "کوس", "خانم بیاد", "خانوم بیاد", "پیوی", "پی", "پی وی",
        "جاوید شاه", "عرزشی", "استوری", "استوریمو", "استوریامو",
        "کون", "کونی", "مدرسه ها شروع شد", "هوا سلده", "سکس",
        # «ک» عمداً حذف شده است: یک حرف تنها هرگز «کلمه» نیست و باعث حذف
        # پیام‌های سالم می‌شد (نگاه کنید به config/banned_words_retired.txt).
        "سک س", "بایو چک", "بیعو چ", "گروهم بیایین", "بایو",
        "ف ی ل م", "ف یلم", "ثبادل", "طبادل", "ث بادل", "ت با دل",
        "شاه", "بی"
    ]
    for w in required:
        assert w in words_set, f"کلمه ممنوعه '{w}' در فایل نیست!"
    # ⛔ حرف تنهای «ک» باید بازنشسته بماند (باگ حذف پیام سالم).
    assert "ک" not in words_set, "«ک» نباید در کلمات ممنوعه باشد"
    retired = (ROOT / "config" / "banned_words_retired.txt").read_text("utf-8")
    assert "ک" in [line.strip() for line in retired.splitlines()]
