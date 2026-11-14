# -*- coding: utf-8 -*-
"""🧪 تست قابلیت «ضد ربات تبلیغاتی» ( modules/ad_spam_guard ).

اجرا:

    PYTHONPATH=. python3 tests/test_ad_spam_guard.py

پوشش:
 ۱. نرمال‌سازی مقاوم (تکرار حروف، ی/ک عربی، کاراکتر نامرئی، تاتویل، جداکننده)
 ۲. تشخیص ربات از روی هویت (نام/یوزرنیم) + محتوا
 ۳. ایمنی: پیام کاربر عادی هرگز اسپم شمرده نمی‌شود
 ۴. ربات با نشان ادمین هم گرفته می‌شود (اگر مارکر در نام باشد)
 ۵. صف اختصاصی: سقف، تکراری‌نبودن، حذف موفق/ناموفق
 ۶. دستورهای روشن/خاموش per-group
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# محیط آزمون: فایل‌های تنظیمات در پوشهٔ موقت بنشیند تا state واقعی دست نخورد.
_TMP = tempfile.mkdtemp(prefix="adspam_test_")
os.environ["BOT_HOME"] = _TMP

from modules.ad_spam_guard import detector, patterns as P, storage  # noqa: E402
from modules.ad_spam_guard.queue import AdSpamDeleteQueue  # noqa: E402

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ✅ %s" % label)
    else:
        FAIL += 1
        print("  ❌ %s %s" % (label, detail))


class _Sender:
    def __init__(self, first=None, last=None, username=None):
        self.first_name = first
        self.last_name = last
        self.username = username


def ev(first, last, username, text):
    return detector.evaluate(_Sender(first, last, username), text)


deleted_log = []


async def _fake_delete(group_id, message_id):
    deleted_log.append((group_id, message_id))
    return True


async def queue_tests():
    """آزمون‌های صف اختصاصی."""
    q = AdSpamDeleteQueue(
        _fake_delete, max_pending=5, max_pending_per_group=3
    )
    q.start()
    try:
        check("ثبت پیام موفق است", q.submit("-100", "10"))
        check("پیام تکراری دوباره صف نمی‌شود", not q.submit("-100", "10"))
        check("پیام دیگرِ همان گروه قبول می‌شود", q.submit("-100", "11"))
        await asyncio.sleep(0.4)
        check("پیام‌ها حذف شدند", len(deleted_log) == 2, deleted_log)
        check("صف خالی شد", q.pending == 0)
        check("آمار درست است",
              q.stats["deleted"] == 2 and q.stats["failed"] == 0, q.stats)

        # سقف per-group
        for i in range(20, 40):
            q.submit("-200", str(i))
        check("سقف per-group رعایت می‌شود",
              q.stats["dropped"] > 0 and q.pending <= 3, q.stats)
        await asyncio.sleep(1.0)

        # حذف ناموفق
        async def _fail_delete(group_id, message_id):
            return False

        q2 = AdSpamDeleteQueue(_fail_delete, max_pending=5, max_attempts=2)
        q2.start()
        try:
            q2.submit("-300", "50")
            await asyncio.sleep(1.0)
            check("حذف ناموفق شمرده می‌شود", q2.stats["failed"] == 1, q2.stats)
        finally:
            await q2.stop(drain=False)

        # استثنا در delete ربات را نمی‌کند
        async def _boom_delete(group_id, message_id):
            raise RuntimeError("boom")

        q3 = AdSpamDeleteQueue(_boom_delete, max_pending=5, max_attempts=1)
        q3.start()
        try:
            q3.submit("-400", "60")
            await asyncio.sleep(0.5)
            check("استثنا گرفته و لاگ می‌شود",
                  q3.stats["failed"] == 1, q3.stats)
        finally:
            await q3.stop(drain=False)
    finally:
        await q.stop(drain=False)


def main() -> int:
    global PASS, FAIL
    PASS = 0
    FAIL = 0

    # ---------------------------------------------------------------- ۱
    print("\n=== ۱. نرمال‌سازی مقاوم ===")
    check("تکرار حروف جمع می‌شود",
          P.normalize("عسل استررررررومو ببین") == "عسل استرومو ببین")
    check("ی/ک عربی یکدست می‌شود",
          P.normalize("عسل استررررررومو ببين") == "عسل استرومو ببین")
    check("کاراکتر نامرئی حذف می‌شود",
          P.normalize("عسل اس\u200bترررررررومو ببین") == "عسل استرومو ببین")
    check("تاتویل حذف می‌شود",
          P.normalize("عسل اســـــــترررررررومو ببین") == "عسل استرومو ببین")
    check("جداکننده به فاصله تبدیل می‌شود",
          P.normalize("عسل_استررررررومو_ببین") == "عسل استرومو ببین")
    check("فاصلهٔ اضافه فشرده می‌شود",
          P.normalize("عسل    استررررررومو   ببین") == "عسل استرومو ببین")
    check("امضای ایموجی درست است",
          P.emoji_signature("سلام 😍🥵😍 سلام") == "😍🥵😍")

    # ---------------------------------------------------------------- ۲
    print("\n=== ۲. تشخیص ربات ===")
    r = ev("عسل", "استررررررومو ببین 😍🥵😍", None, "عسل استررررررومو ببین")
    check("ربات اسکرین‌شات گرفته می‌شود", r["is_spam"], r)
    r = ev("عسسسسل", "اسسسستتتترررررررومو ببییییین 🤗👋", None, "ببین")
    check("ربات با نام تحریف‌شده گرفته می‌شود", r["is_spam"], r)
    r = ev("asalam", None, "عسل_استرررررررومو_ببین", "سلام")
    check("ربات با یوزرنیمِ نشانه‌دار گرفته می‌شود", r["is_spam"], r)
    r = ev("ربات", None, "x", "عسل استررررررومو ببین 🥰😍🔥")
    check("ربات با متنِ نشانه‌دار گرفته می‌شود", r["is_spam"], r)
    r = ev("A", None, None,
           "عسسسسل اسسسستتتتتتتتتتتتتتررررررررررررررومو ببییییین")
    check("ربات با بیشینه تکرار گرفته می‌شود", r["is_spam"], r)

    # ---------------------------------------------------------------- ۳
    print("\n=== ۳. ایمنی کاربر عادی ===")
    normals = [
        ("رضا", "محمدی", "reza_m", "سلام دوستان، امشب بازی فوتبال تماشا می‌کنیم"),
        ("مهدی", None, "mehii", "سلام"),
        ("عسل", None, "asal", "سلام خوبی؟"),
        ("استر", None, "star_fan", "استر والیبال امشب played خوب بود"),
        ("Ali", None, "ali_dev", "ببین این کد رو می‌شه اصلاح کنی؟"),
        ("سارا", None, "sara", "🥰😍 عالی بود ممنون"),
        ("زهرا", None, "zahra", "کد تخفیف دارم برای همه 🎁"),
        ("محمد", None, "mohsen", "بیوگرافی ات رو خوندم، خیلی قشنگ بود"),
        ("نگار", None, "negar", "عسل مو خوبه، ببین چقوه شیرینه"),
        ("رضا", None, "reza2", "قیمت دلار امروز چنده؟"),
        ("Mehi", None, "mehi", "سلام 🤗👋"),
    ]
    for first, last, uname, text in normals:
        r = ev(first, last, uname, text)
        check("عادی: %s / %r" % (first, text[:20]), not r["is_spam"], r)

    # ---------------------------------------------------------------- ۴
    print("\n=== ۴. ربات با نشان ادمین ===")
    r = ev("عسل", "استررررررومو ببین 😍🥵😍", None, "عسل استررررررومو ببین")
    check("هویت قوی تشخیص داده می‌شود", r["identity_strong"], r)
    r = ev("Mehi", None, "mehi", "سلام 🤗👋")
    check("ادمینِ واقعی هویت قوی ندارد", not r["identity_strong"], r)

    # ---------------------------------------------------------------- ۵
    print("\n=== ۵. صف اختصاصی ===")
    asyncio.run(queue_tests())

    # ---------------------------------------------------------------- ۶
    print("\n=== ۶. دستورهای روشن/خاموش ===")
    check("دستور روشن تشخیص داده می‌شود",
          storage.is_toggle_command("تبلیغ گر روشن") == "on")
    check("دستور خاموش تشخیص داده می‌شود",
          storage.is_toggle_command("تبلیغ گر خاموش") == "off")
    check("متن عادی دستور نیست", storage.is_toggle_command("سلام") is None)
    check("فاصلهٔ اضافی نادیده گرفته می‌شود",
          storage.is_toggle_command(" تبلیغ گر روشن ") == "on")

    storage.enable("-555")
    check("روشن شدن ذخیره می‌شود", storage.is_enabled("-555"))
    storage.disable("-555")
    check("خاموش شدن ذخیره می‌شود", not storage.is_enabled("-555"))
    storage.enable("-555")
    check("گروه دیگر تأثیر نمی‌بیند", not storage.is_enabled("-999"))

    # ---------------------------------------------------------------- ۷
    print("\n=== ۷. قالب‌های پیام (مطابق اسکرین‌شات) ===")
    from modules.ad_spam_guard import texts, state  # noqa: E402

    check("قالب تأییدیه شامل فهرست قابلیت‌هاست",
          "شناسایی پیام های تبلیغاتی ربات ها" in texts.CONFIRM_ENABLE_TEMPLATE)
    check("قالب تأییدیه سؤالِ فعال‌سازی را دارد",
          "آیا این قابلیت فعال شود ؟" in texts.CONFIRM_ENABLE_TEMPLATE)
    check("قالب تأییدیه گزینهٔ تایید دارد", "▪️تایید" in texts.CONFIRM_ENABLE_TEMPLATE)
    check("قالب تأییدیه گزینهٔ لغو دارد", "▫️ لغو" in texts.CONFIRM_ENABLE_TEMPLATE)

    check("پیام فعال‌شدن درست است",
          texts.ENABLED_MESSAGE
          == "▫️ضد تبلیغ گر هوش مصنوعی روباه فعال شد")
    check("پیام خاموش‌شدن درست است",
          texts.DISABLED_MESSAGE
          == "▪️ضد تبلیغ گر هوش مصنوعی روباه خاموش شد")

    warn = texts.render_warn("@oskwma", 1, threshold=5, score=11)
    check("قالب هشدار شامل نام کاربر است", "⛔ کاربر  @oskwma" in warn)
    check("قالب هشدار شامل دلیل است", "دلیل : هرزنامه\u200c تبلیغات" in warn)
    check("قالب هشدار شامل تعداد اخطار است", "■ تعداد اخطار : 1/5" in warn)
    check("قالب هشدار شامل انجام‌دهنده است",
          "■ انجام دهنده : هوش مصنوعی" in warn)
    check("قالب هشدار شامل درصد تشخیص است", "■ درصد تشخیص : 85/11" in warn)
    check("کارت هیچ خطِ اضافه‌ای ندارد",
          len([ln for ln in warn.split("\n") if ln]) == 5, repr(warn))

    # ساختارِ entityها: نقل‌قول فقط روی سرصفحه، Bold فقط روی ارقام
    warn_text, warn_entities = texts.warn_payload("@oskwma", 1, threshold=5,
                                                 score=11)
    lines = warn_text.split("\n")
    utf16 = warn_text.encode("utf-16-le")

    def _slice(entity):
        start = entity.offset * 2
        stop = (entity.offset + entity.length) * 2
        return utf16[start:stop].decode("utf-16-le")

    blocks = [e for e in warn_entities
              if type(e).__name__ == "MessageEntityBlockquote"]
    bolds = [e for e in warn_entities
             if type(e).__name__ == "MessageEntityBold"]
    check("فقط سرصفحه نقل‌قول دارد", len(blocks) == 1
          and _slice(blocks[0]) == lines[0])
    check("دلیل ساده است (Bold ندارد)",
          all(_slice(b) != lines[1] for b in bolds))
    check("ارقامِ تعداد اخطار Bold است", any(_slice(b) == "1/5" for b in bolds))
    check("ارقامِ درصد تشخیص Bold است",
          any(_slice(b) == "85/11" for b in bolds))
    check("انجام‌دهنده Bold نیست",
          all(_slice(b) not in ("هوش مصنوعی", lines[4]) for b in bolds))
    check("خطِ آخر (درصد تشخیص) همان خطِ عکس است",
          lines[-1] == "■ درصد تشخیص : 85/11")

    # درصدِ تشخیص از امتیاز خام
    check("درصد تشخیص از امتیاز ۱۳ برابر ۱۰۰ می‌شود",
          texts.detect_percent(13) == 99 or texts.detect_percent(13) == 100)
    check("درصد تشخیص صفر برای امتیاز صفر", texts.detect_percent(0) == 0)
    check("درصد تشخیص با score=6 حدودِ ۴۶ است", texts.detect_percent(6) == 46)

    check("راهنمای ادمینی درست است",
          texts.HELP_HEADER
          == "💥برای فعال کردن ضد ربات های تبلیغ گر بنویسید")
    check("دستور روشن درست است", texts.HELP_ON == "تبلیغ گر روشن")
    check("دستور خاموش درست است", texts.HELP_OFF == "تبلیغ گر خاموش")

    check("کلمهٔ تایید تشخیص داده می‌شود", detector.is_confirm_word("تایید"))
    check("کلمهٔ لغو تشخیص داده می‌شود", detector.is_cancel_word("لغو"))
    check("متن عادی تایید نیست", not detector.is_confirm_word("سلام"))

    # ------------------------------------------------ ۷ب. حذفِ خودکارِ هشدار
    print("\n=== ۷ب. حذفِ خودکارِ کارتِ هشدار (۶۰ ثانیه / بدونِ شناسه) ===")
    deletes = []

    class _Msg:
        def __init__(self, mid, body):
            self.id = mid
            self.message = body

    class _Client:
        async def get_messages(self, peer, limit=20):
            # عمداً یک Messageِ تنها (نه لیست) — بعضی نسخه‌ها همین‌طورند.
            return _Msg(4242, warn_text)

        async def delete_messages(self, chat_id, ids):
            deletes.append((chat_id, list(ids)))

    class _Logger:
        def log_info(self, *_a, **_k):
            pass

    class _Bot:
        client = _Client()
        logger = _Logger()

    async def _ttl_case():
        import asyncio as _asyncio

        ok = detector.schedule_notice_cleanup(
            _Bot(), -100123, None, ttl=0.05, text=warn_text)
        await _asyncio.sleep(0.4)
        return ok

    scheduled = asyncio.run(_ttl_case())
    check("هشدار برای حذفِ خودکار ثبت می‌شود", bool(scheduled))
    check("هشدار بدونِ شناسه با متن پیدا و پاک می‌شود",
          deletes == [(-100123, [4242])])

    # ---------------------------------------------------------------- ۸
    print("\n=== ۸. جریان تأییدیه ===")
    state.clear_confirm("-777")
    state.request_confirm("-777", "555")
    check("کاربرِ منتظرِ تایید ثبت می‌شود", state.pending_user("-777") == "555")
    state.clear_confirm("-777")
    check("پس از لغو، کسی در انتظار نیست", state.pending_user("-777") is None)

    # شمارندهٔ اخطار
    before = state.warn_count("-888", "999")
    c1 = state.add_warn("-888", "999")
    c2 = state.add_warn("-888", "999")
    check("شمارندهٔ اخطار افزایش می‌یابد", c2 == c1 + 1, (c1, c2))
    state.reset_warns("-888", "999")
    check("صفر کردن اخطار کار می‌کند", state.warn_count("-888", "999") == 0)

    print("\n" + "=" * 60)
    print("نتیجه: %d پاس / %d شکست" % (PASS, FAIL))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
