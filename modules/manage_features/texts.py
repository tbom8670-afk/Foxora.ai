# -*- coding: utf-8 -*-
"""🎨 قالب‌های سه قابلیت — عیناً مطابق اسکرین‌شات‌های کارفرما.

قواعد (همان قواعدِ «پاکسازی کامل»): کل متن Bold است و هر بخشی که در عکس
نقل‌قول دارد، دقیقاً همان بازهٔ UTF-16 را به‌عنوان ``MessageEntityBlockquote``
می‌گیرد. هیچ ``parse_mode``/مارک‌داونی در کار نیست. ارقام مثل عکس، لاتین‌اند.

هر قالب یک «اتصال» است: تابع‌های ``build_*`` متن + entity می‌سازند و همهٔ
متغیرها از لایهٔ منطق می‌آیند. اگر کارفرما قالب نهایی را فرستاد، فقط همین
فایل عوض می‌شود و هیچ منطقی دست نمی‌خورد.

قالب‌های پیاده‌شده از روی تصاویر:

1. ``timed_chooser``      — 💭 پیام های زماندار + «« یکی از گزینه هارو انتخاب کنید
2. ``timed_progress``     — 🧹 شروع پاکسازی پیام های زماندار / تعداد / ناموفق /
                            مدت زمان / گروه ... قفل شد
3. ``timed_started``      — ⚙️ : پاکسازی زماندار پیام ها تنظیم شد + «30 دقیقه» …
4. ``report_ask``         — ⚠️ کاربر : @user + «لطفاً دلیل گزارش خودتون …»
5. ``report_to_owner``    — ⛔️ مالک محترم: @user + گزارش دهنده/متخلف + تایید/لغو 💎
6. ``exclusive_*``        — ← کاربر : 『 @user 』 + متن فعال/لغو تنظیم اختصاصی
"""
from __future__ import annotations

try:  # pragma: no cover
    from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote
except ImportError:                                  # pragma: no cover
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


def u16(text) -> int:
    return len(str(text).encode("utf-16-le")) // 2


class _Builder:
    """متن + entity با آفستِ اندازه‌گیری‌شده (هیچ محاسبهٔ دستی)."""

    def __init__(self):
        self.lines = []
        self.quotes = []          # (start_u16, length_u16)

    def line(self, text="", quote=False):
        start = u16("\n".join(self.lines)) + 1 if self.lines else 0
        self.lines.append(text)
        if quote and text:
            self.quotes.append((start, u16(text)))
        return self

    def build(self):
        text = "\n".join(self.lines)
        entities = [MessageEntityBold(offset=0, length=u16(text))]
        for offset, length in self.quotes:
            entities.append(MessageEntityBlockquote(offset=offset,
                                                    length=length))
        return text, entities


# ---------------------------------------------------------------------------
#  ۱) پیام زماندار — انتخاب بازه
# ---------------------------------------------------------------------------
TIMED_CHOOSER_HEADER = "💭 پیام های زماندار"
TIMED_CHOOSER_BODY = (
    "با انتخاب مدت زمان تعداد پیام های که\n"
    "در این مدت ارسال شوند پس از اتمام زمان کامل پاک\n"
    "خواهد شد\n"
    "«« یکی از گزینه هارو انتخاب کنید"
)


def timed_chooser(options):
    """``options``: فهرستِ ``(شماره، برچسب)`` — هر گزینه یک نقل‌قول."""
    builder = _Builder().line(TIMED_CHOOSER_HEADER).line()
    for body_line in TIMED_CHOOSER_BODY.split("\n"):
        builder.line(body_line)
    for number, label in options:
        builder.line(f"{number} - {label}", quote=True)
    return builder.build()


# ---------------------------------------------------------------------------
#  ۲) پیام زماندار — پیشرفت/پایان (همان پیامی که ویرایش می‌شود)
# ---------------------------------------------------------------------------
TIMED_PROGRESS_TITLE = "🧹 شروع پاکسازی پیام های زماندار"
TIMED_PROGRESS_DELETED = "تعداد پیام در حال پاک شدن"
TIMED_PROGRESS_FAILED = "ناموفق"
TIMED_PROGRESS_DURATION = "مدت زمان"
TIMED_PROGRESS_LOCKED = "گروه برای پاکسازی زماندار قفل شد"
TIMED_PROGRESS_FINISHED = "🧹 پاکسازی پیام های زماندار انجام شد"


def timed_progress(*, deleted, failed, elapsed_seconds, locked=True,
                   state="running"):
    """پیامِ وضعیت — نه پیام جدید؛ هر بار همان پیام ویرایش می‌شود."""
    builder = _Builder()
    # «انجام شد» فقط وقتی که حذف واقعاً تمام شده باشد (done) یا با لغو
    # متوقف شده باشد (cancelled). در تایم‌اوت، عنوانِ «شروع» می‌ماند تا
    # وضعیتِ نیمه‌کاره به‌دروغ «انجام شد» نشان داده نشود.
    title = (TIMED_PROGRESS_FINISHED if state in {"done", "cancelled"}
             else TIMED_PROGRESS_TITLE)
    builder.line(title, quote=True)
    builder.line()
    builder.line(f"{TIMED_PROGRESS_DELETED} : {int(deleted)}")
    builder.line(f"{TIMED_PROGRESS_FAILED} : {int(failed)}")
    builder.line(f"{TIMED_PROGRESS_DURATION} : {int(max(0, elapsed_seconds))} ثانیه")
    if locked:
        builder.line(TIMED_PROGRESS_LOCKED)
    return builder.build()


# ---------------------------------------------------------------------------
#  ۳) پیام زماندار — «تنظیم شد»
# ---------------------------------------------------------------------------
TIMED_SET_TITLE = "⚙️ : پاکسازی زماندار پیام ها تنظیم شد"
#: در عکس، «یوزنیم ربات» و «یوزنیم کسی که تنظیم کرده» جاي‌نگهدارِ نام واقعی‌اند.
TIMED_SET_RUNNER = "انجام دهنده : {user}"
TIMED_SET_ACTOR = "تنظیم کننده : {user}"


def timed_started(*, label, bot_username="", actor_username=""):
    builder = _Builder().line(TIMED_SET_TITLE).line()
    builder.line(f"«{label}»")
    builder.line(TIMED_SET_RUNNER.format(user=bot_username or "----"))
    builder.line(TIMED_SET_ACTOR.format(user=actor_username or "----"))
    return builder.build()


# ---------------------------------------------------------------------------
#  ۴) گزارش — درخواست دلیل
# ---------------------------------------------------------------------------
REPORT_ASK_TITLE = "⚠️ کاربر : {user}"
REPORT_ASK_BODY = (
    "لطفاً دلیل گزارش خودتون رو بدون توهین و اتهام های\n"
    "غیر مجاز بنویسید تا برای مالک ارسال شود"
)


def report_ask(*, username):
    builder = _Builder()
    builder.line(REPORT_ASK_TITLE.format(user=username), quote=True).line()
    builder.line(REPORT_ASK_BODY)
    return builder.build()


# ---------------------------------------------------------------------------
#  ۵) گزارش — اطلاع به مالک + تایید/لغو
# ---------------------------------------------------------------------------
REPORT_OWNER_TITLE = "⛔️ مالک محترم: {user}"
REPORT_OWNER_BODY = "یک گزارش تخلف از سوی کاربران"
REPORT_OWNER_REPORTER = "« گزارش دهنده : {user}"
REPORT_OWNER_TARGET = "« متخلف : {user}"
REPORT_OWNER_QUESTION = (
    "آیا صحت تخلف رو تایید میکنید در صورت تایید کاربر {count}\n"
    "اخطار خواهد گرفت"
)
REPORT_CONFIRM = "◆ تایید"
REPORT_CANCEL = "◆ لغو"


def report_to_owner(*, owner_username, reporter_username, target_username,
                    warnings=2):
    builder = _Builder()
    builder.line(REPORT_OWNER_TITLE.format(user=owner_username), quote=True)
    builder.line(REPORT_OWNER_BODY)
    builder.line(REPORT_OWNER_REPORTER.format(user=reporter_username))
    builder.line(REPORT_OWNER_TARGET.format(user=target_username))
    builder.line()
    builder.line(REPORT_OWNER_QUESTION.format(count=int(warnings)))
    builder.line(REPORT_CONFIRM)
    builder.line(REPORT_CANCEL)
    return builder.build()


# ---------------------------------------------------------------------------
#  ۵ب) پیام اخطار پس از «تایید» گزارش
# ---------------------------------------------------------------------------
REPORT_WARN_TITLE = "⚠️ کاربر : {user}"
REPORT_WARN_BODY = "شما به دلیل دریافت گزارش رفتار نادرست"
REPORT_WARN_LINE = "{count} اخطار دریافت کردید"
REPORT_WARN_TOTAL = "تعداد اخطار : {count}/{threshold}"


def report_warning(*, username, granted=2, total=2, threshold=5):
    """قالب اخطارِ بعد از تأیید گزارش — عیناً مطابق پیام تصویر کارفرما.

    عنوان («⚠️ کاربر : @…») داخل نقل‌قول، بقیه Bold. اعداد لاتین.
    """
    builder = _Builder()
    builder.line(REPORT_WARN_TITLE.format(user=username), quote=True)
    builder.line()
    builder.line(REPORT_WARN_BODY)
    builder.line(REPORT_WARN_LINE.format(count=int(granted)))
    builder.line()
    builder.line(REPORT_WARN_TOTAL.format(count=int(total),
                                          threshold=int(threshold)))
    return builder.build()


# ---------------------------------------------------------------------------
#  ۶) اختصاصی / لغو اختصاصی
# ---------------------------------------------------------------------------
EXCLUSIVE_HEADER = "← کاربر : 『 {user} 』"
EXCLUSIVE_GRANTED = (
    "تنظیم اختصاصی برای شما فعال شد میتواند\n"
    "در گروه پیام ارسال کنید"
)
EXCLUSIVE_REVOKED = "تنظیم اختصاصی شما لغو شد"
EXCLUSIVE_NOT_LOCKED = "گروه قفل نیست؛ اختصاصی لازم نیست"
EXCLUSIVE_ALREADY = "این کاربر از قبل اختصاصی است"
EXCLUSIVE_MISSING = "این کاربر در فهرست اختصاصی نیست"
EXCLUSIVE_NO_PERMISSION = "فقط مالک یا ادمین ثبت‌شده می‌تواند این دستور را بزند"
EXCLUSIVE_NO_TARGET = "روی پیام کاربر ریپلای کنید یا یوزرنیم بدهید"


def exclusive_message(*, username, kind="granted"):
    """``kind`` ∈ {granted, revoked, not_locked, already, missing}

    نکته: قالبِ «انجام نشد» عمداً حذف شده است (درخواست کارفرما) و هر خطای
    غیرمنتظره به‌جای آن پیام موفقیت را نشان می‌دهد و در لاگ ثبت می‌شود.
    """
    bodies = {
        "granted": EXCLUSIVE_GRANTED,
        "revoked": EXCLUSIVE_REVOKED,
        "not_locked": EXCLUSIVE_NOT_LOCKED,
        "already": EXCLUSIVE_ALREADY,
        "missing": EXCLUSIVE_MISSING,
    }
    builder = _Builder()
    builder.line(EXCLUSIVE_HEADER.format(user=username), quote=True)
    body = bodies.get(kind, bodies["granted"])
    for body_line in body.split("\n"):
        builder.line(body_line)
    return builder.build()


#: رزروِ قالب‌های آیندهٔ کارفرما (فقط جای‌گذاری؛ هیچ متنی اختراع نمی‌شود).
FUTURE: dict = {}
