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
    ADMIN_HELP_MESSAGES,
    MAX_QUOTES_PER_MESSAGE,
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
    # ۴۵ بلوک دستورات (شامل بلوک تازهٔ عضویت اجباری) + ۱ بلوک نقل‌قول
    # «تماس با سازنده» = ۴۶
    # (۴ بلوک «لینک/آیدی بیو» حذف شد — قابلیت خودکار است — و ۱ بلوک
    # «پاک کامل» و ۱ بلوک «اضافه کردن کانال من برای عضویت اجباری» اضافه شد.
    # + ۲ بلوک «اختصاصی» و «پیام زماندار» + ۱ بلوک «تأیید دو مرحله‌ای» = ۵۱
    # + ۱ بلوک «متخلف/لغو متخلف» (پایینِ لیست) = ۵۲)
    assert len(bquote_entities) == 52
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
    # ۵۲ = ۴۸ ردیفِ قبلی + ۲ نقل‌قولِ «اختصاصی»/«پیام زماندار»
    # + ۱ نقل‌قولِ «تأیید دو مرحله‌ای دستورات خطرناک»
    # + ۱ نقل‌قولِ «متخلف/لغو متخلف».
    assert len(bquote_entities) == 52

    for ent in bquote_entities:
        sliced = _slice_u16(ADMIN_HELP_TEXT, ent.offset, ent.length)
        lines = sliced.split("\n")
        assert len(lines) in (1, 2, 3), f"نقل‌قول با طول خط غیرمجاز: {sliced!r}"


def test_bold_entities_content():
    """بررسی اینکه تمام بخش‌های بولد (توضیحات و عناوین) به درستی مارک شده‌اند."""
    bold_entities = [e for e in ADMIN_HELP_ENTITIES if isinstance(e, MessageEntityBold)]
    # 45 عنوان بلوک + 1 هدر اولیه + 1 تماس با سازنده = 47
    # + 2 دستور Bold داخل بلوک عضویت اجباری («راهنمای کانال»، «راهنمای اجرا») = 51
    # + 2 بلوکِ «اختصاصی»/«پیام زماندار» که کلِ متن‌شان Bold است
    # + 1 بلوکِ «تأیید دو مرحله‌ای دستورات خطرناک» = 54
    # + 1 بلوکِ «متخلف/لغو متخلف» (کلِ متن Bold) = 55
    assert len(bold_entities) == 55

    # بلوک‌هایی که به‌درخواستِ کارفرما کلِ متن‌شان (چندخطی) Bold است.
    for ent in bold_entities:
        sliced = _slice_u16(ADMIN_HELP_TEXT, ent.offset, ent.length)
        assert len(sliced.strip()) > 0
        # تمام خطوط بولد تک‌خطی‌اند، جز بلوک‌های «آماده» مثل «نمودار»،
        # «پاکسازی کامل»، «اختصاصی» و «پیام زماندار».
        if "\n" in sliced:
            # «تأیید دو مرحله‌ای دستورات خطرناک» و «متخلف» هم بلوکِ آمادهٔ
            # چندخطی‌اند (کلِ بلوک Bold، دستورها داخل یک نقل‌قول).
            assert sliced.startswith(("🕹️", "🔐", "🧬", "📊", "⏰", "⛔️")), sliced


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


# ---------------------------------------------------------------------------
#  تکه‌تکه ارسال (محدودیت نقل‌قول سروش‌پلاس)
# ---------------------------------------------------------------------------
def test_messages_are_chunked_to_avoid_lost_quotes():
    """سروش‌پلاس نقل‌قول‌های اضافیِ یک پیام بلند را بی‌صدا حذف می‌کند.

    برای همین «لیست ادمینی» تکه‌تکه فرستاده می‌شود؛ هر تکه باید:
    * حداکثر MAX_QUOTES_PER_MESSAGE نقل‌قول داشته باشد،
    * هیچ ردیفی بین دو تکه نصف نشود باشد (هر تکه با «\\n\\n» کامل جدا شود).
    """
    chunks = ADMIN_HELP_MESSAGES
    assert len(chunks) >= 2, "لیست باید حداقل دو تکه شود"

    total_quotes = 0
    for text, entities in chunks:
        quotes = [e for e in entities
                  if type(e).__name__ == "MessageEntityBlockquote"]
        total_quotes += len(quotes)
        assert len(quotes) <= MAX_QUOTES_PER_MESSAGE
        # هیچ تکه‌ای نباید با جداکنندهٔ ردیف تمام شود (یعنی ردیف نصف شده)
        assert not text.endswith("\n")

    # هیچ نقتیول از دست نرفته باشد
    full_quotes = [e for e in ADMIN_HELP_ENTITIES
                   if type(e).__name__ == "MessageEntityBlockquote"]
    assert total_quotes == len(full_quotes)

    # چسباندن تکه‌ها باید همهٔ ردیف‌ها را بدهد (فقط جداکنندهٔ بین‌تکه‌ای کم است)
    joined = "\n\n".join(chunk[0] for chunk in chunks)
    assert joined == ADMIN_HELP_TEXT

    # آخرین تکه باید خط «تماس با سازنده» را داشته باشد
    assert "تماس با سازنده" in chunks[-1][0]
    # اولین تکه باید عنوان لیست را داشته باشد
    assert chunks[0][0].startswith("👑 دستورات ادمین‌ها:")


def test_every_row_keeps_its_quote_in_chunks():
    """هر ردیفی که در پیام تک‌تکه نقل‌قول داشت، در تکه‌ها هم دارد."""
    def quotes_of(text, entities):
        raw = text.encode("utf-16-le")
        return [raw[e.offset * 2:(e.offset + e.length) * 2]
                .decode("utf-16-le", "replace")
                for e in entities
                if type(e).__name__ == "MessageEntityBlockquote"]

    wanted = set(quotes_of(ADMIN_HELP_TEXT, ADMIN_HELP_ENTITIES))
    got = set()
    for text, entities in ADMIN_HELP_MESSAGES:
        got.update(quotes_of(text, entities))
    assert wanted == got, "بعضی نقل‌قول‌ها در تکه‌بندی گم شده‌اند"
