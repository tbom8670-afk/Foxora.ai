"""تست‌های جامع بازنویسی قالب راهنمای دستورات ادمین روباه (Admin Help Builder).

موارد تحت پوشش:
1. صحت ساختار خروجی `build_admin_help` (متن نهایی و موجودیت‌های SPlusthon).
2. عدم وجود تگ‌ها و نشانگرهای قالب‌بندی (b, n, n2, n3, *, **, __, `) در متن نهایی.
3. صحت تمام موجودیت‌های MessageEntityBold و MessageEntityBlockquote بر حسب آفست‌های UTF-16.
4. رعایت دقیق قواعد n (نقل‌قول یک‌خطی)، n2 (نقل‌قول دوخطی) و n3 (نقل‌قول سه‌خطی).
5. عدم امتداد موجودیت‌ها به خط بعدی (عدم bleed کاراکتر \\n در آفست‌ها).
6. حفظ عبارت پایانی: تماس با سازنده : @OSINE2.
7. همگام‌سازی و یکپارچگی با ماژول handlers.message_handler.
"""

import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.admin_help_builder import (
    _u16,
    build_admin_help,
    ADMIN_HELP_TEXT,
    ADMIN_HELP_ENTITIES,
)
from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote
from handlers.message_handler import ADMIN_HELP_TEXT as H_TEXT, ADMIN_HELP_ENTITIES as H_ENTITIES


def _slice_u16(text: str, offset: int, length: int) -> str:
    """برش یک بخش از متن بر اساس آفست و طول UTF-16."""
    u16_units = []
    for ch in text:
        encoded = ch.encode("utf-16-le")
        for i in range(0, len(encoded), 2):
            u16_units.append(encoded[i:i+2])
    target_units = u16_units[offset:offset + length]
    raw_bytes = b"".join(target_units)
    return raw_bytes.decode("utf-16-le")


def test_blocks_count_and_contact():
    """بررسی تعداد بلوک‌های ورودی و عبارت سازنده."""
    bquote_entities = [e for e in ADMIN_HELP_ENTITIES if isinstance(e, MessageEntityBlockquote)]
    # ۴۴ بلوک دستورات + 1 بلوک نقل‌قول تماس با سازنده = 45
    # (۴ بلوک «لینک/آیدی بیو» حذف شد — قابلیت خودکار است و نیاز به
    # فعال‌کردن ندارد — و ۱ بلوک «پاک کامل» اضافه شد.)
    assert len(bquote_entities) == 45
    contact_note = "تماس با سازنده : @OSINE2"
    assert ADMIN_HELP_TEXT.endswith(contact_note)


def test_bio_link_commands_removed_and_purge_block_present():
    """نوشته‌های لینک/آیدی بیو در «لیست ادمینی» نیستند؛ «پاک کامل» هست.

    قالبِ خواسته‌شده: جملهٔ «برای پاک کردن کل پیام های یک کاربر ریپلای
    کنید روش» به‌صورت Bold و خود دستور «پاک کامل» داخل نقل‌قول سروش پلاس.
    """
    for removed in (
        "فیلتر لینک بیو",
        "لغو فیلتر لینک بیو",
        "بررسی بیو",
        "وضعیت لینک بیو",
        "لیست لینک بیو",
        "پاک لیست لینک بیو",
        "فیلتر آیدی بیو",
        "لغو فیلتر آیدی بیو",
    ):
        assert removed not in ADMIN_HELP_TEXT

    title = "برای پاک کردن کل پیام های یک کاربر ریپلای کنید روش"
    bold_entities = [e for e in ADMIN_HELP_ENTITIES if isinstance(e, MessageEntityBold)]
    assert any(
        _slice_u16(ADMIN_HELP_TEXT, e.offset, e.length) == title
        for e in bold_entities
    )
    bquote_entities = [e for e in ADMIN_HELP_ENTITIES if isinstance(e, MessageEntityBlockquote)]
    assert any(
        _slice_u16(ADMIN_HELP_TEXT, e.offset, e.length) == "پاک کامل"
        for e in bquote_entities
    )


def test_no_raw_markers_in_text():
    """اطمینان از اینکه هیچ نشانگر قالبی (b, n, n2, n3 یا مارک‌داون) در متن نیست."""
    forbidden = ["**", "__", "`"]
    for sym in forbidden:
        assert sym not in ADMIN_HELP_TEXT

    lines = ADMIN_HELP_TEXT.split("\n")
    for line in lines:
        stripped = line.strip()
        assert stripped not in ("b", "n", "n2", "n3")
        assert not stripped.endswith(" b")
        assert not stripped.endswith(" n")
        assert not stripped.endswith(" n2")
        assert not stripped.endswith(" n3")


def test_utf16_entities_validity():
    """تمام آفست‌ها و طول‌ها باید درون محدودهٔ طول UTF-16 متن باشند."""
    total_u16 = _u16(ADMIN_HELP_TEXT)
    assert len(ADMIN_HELP_ENTITIES) > 0

    for ent in ADMIN_HELP_ENTITIES:
        assert ent.offset >= 0
        assert ent.length > 0
        assert ent.offset + ent.length <= total_u16

        # محتوای برش‌خورده نباید خالی باشد
        sliced = _slice_u16(ADMIN_HELP_TEXT, ent.offset, ent.length)
        assert len(sliced.strip()) > 0
        # برش نباید به کاراکتر انتهای خط bleed کند
        assert not sliced.endswith("\n")
        assert not sliced.startswith("\n")


def test_blockquotes_span_exact_lines():
    """بررسی اینکه نقل‌قول‌های n، n2 و n3 دقیقاً تعداد خطوط مربوطه را پوشش می‌دهند."""
    bquote_entities = [e for e in ADMIN_HELP_ENTITIES if isinstance(e, MessageEntityBlockquote)]
    assert len(bquote_entities) == 45

    for ent in bquote_entities:
        sliced = _slice_u16(ADMIN_HELP_TEXT, ent.offset, ent.length)
        lines = sliced.split("\n")
        assert len(lines) in (1, 2, 3), f"نقل‌قول با طول خط غیرمجاز: {sliced!r}"


def test_bold_entities_content():
    """بررسی اینکه تمام بخش‌های بولد (توضیحات و عناوین) به درستی مارک شده‌اند."""
    bold_entities = [e for e in ADMIN_HELP_ENTITIES if isinstance(e, MessageEntityBold)]
    # 44 عنوان بلوک + 1 هدر اولیه + 1 تماس با سازنده = 46
    assert len(bold_entities) == 46

    for ent in bold_entities:
        sliced = _slice_u16(ADMIN_HELP_TEXT, ent.offset, ent.length)
        assert len(sliced.strip()) > 0
        # نباید شامل شکست خط باشد (تمام خطوط بولد تک‌خطی هستند)
        assert "\n" not in sliced


def test_n2_and_n3_examples():
    """بررسی نمونه‌های n2 و n3 در متن خروجی."""
    # فیلتر کلمات (n3)
    assert "🪤فیلتر کردن کلمات گروه با دستورات" in ADMIN_HELP_TEXT
    assert "/فیلتر ← /فیلتر بیو\n/رفع ← / رفع بیو\n/فیلترها ← نمایش لیست فیلتر" in ADMIN_HELP_TEXT

    # پاکسازی چند پیام آخر (n2)
    assert "🔘 حذف گروهی چند پیام آخر با دستور" in ADMIN_HELP_TEXT
    assert "پاک + اعداد 1 تا 800\nپاک 200 یا پاک 600" in ADMIN_HELP_TEXT

    # فیلتر اسم (n3)
    assert "🖍 فیلتر کردن نام های کاربران در گروه با دستور" in ADMIN_HELP_TEXT
    assert "فیلتر اسم ← فیلتر اسم عسل\nلغو اسم ← لغو اسم عسل\nنمایش لیست ← لیست فیلتر اسم" in ADMIN_HELP_TEXT

    # اخراج یا سکوت دست جمعی (n2)
    assert "📑 برای اخراج یا سکوت دست جمعی نام ها از تمام لیست اعضای گروه" in ADMIN_HELP_TEXT
    assert "اخراج نام ← اخراج نام بیو چک\nسکوت نام ← سکوت نام بیو چک" in ADMIN_HELP_TEXT


def test_handler_admin_help_integration():
    """بررسی استفاده از ADMIN_HELP_TEXT و ADMIN_HELP_ENTITIES در message_handler."""
    assert H_TEXT == ADMIN_HELP_TEXT
    assert H_ENTITIES == ADMIN_HELP_ENTITIES
