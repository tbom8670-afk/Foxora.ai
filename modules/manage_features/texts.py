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
TIMED_PROGRESS_REMAINING = "پیام باقی مانده"
TIMED_PROGRESS_FAILED = "ناموفق"
TIMED_PROGRESS_DURATION = "مدت زمان"
TIMED_PROGRESS_ENDED_AT = "زمان پایان"
TIMED_PROGRESS_LATE = "تاخیر"
TIMED_PROGRESS_LOCKED = "گروه برای پاکسازی زماندار قفل شد"
TIMED_PROGRESS_FINISHED = "🧹 پاکسازی پیام های زماندار انجام شد"


def _fa_number(value) -> str:
    """عدد لاتین؛ اگر عدد نبود همان رشته."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def timed_progress(*, deleted, failed, elapsed_seconds, locked=True,
                   state="running", total=None, ended_at=None, late_seconds=0):
    """پیامِ وضعیت — نه پیام جدید؛ هر بار همان پیام ویرایش می‌شود.

    به‌درخواست کارفرما تعداد **واقعی** پیام‌های پاک‌شده همیشه نوشته می‌شود
    (حتی وقتی صفر است) و خطِ «زمان پایان» (به وقت تهران) هم می‌آید تا معلوم
    باشد زمان‌بندی درست بوده است.
    """
    builder = _Builder()
    # «انجام شد» فقط وقتی که حذف واقعاً تمام شده باشد (done) یا با لغو
    # متوقف شده باشد (cancelled). در تایم‌اوت، عنوانِ «شروع» می‌ماند تا
    # وضعیتِ نیمه‌کاره به‌دروغ «انجام شد» نشان داده نشود.
    title = (TIMED_PROGRESS_FINISHED if state in {"done", "cancelled"}
             else TIMED_PROGRESS_TITLE)
    builder.line(title, quote=True)
    builder.line()
    builder.line(f"{TIMED_PROGRESS_DELETED} : {_fa_number(deleted)}")
    if total is not None:
        remaining = max(0, int(total) - int(deleted or 0))
        builder.line(f"{TIMED_PROGRESS_REMAINING} : {_fa_number(remaining)}")
    builder.line(f"{TIMED_PROGRESS_FAILED} : {_fa_number(failed)}")
    builder.line(f"{TIMED_PROGRESS_DURATION} : "
                 f"{_fa_number(max(0, elapsed_seconds))} ثانیه")
    if ended_at:
        builder.line(f"{TIMED_PROGRESS_ENDED_AT} : {ended_at}")
    if late_seconds:
        builder.line(f"{TIMED_PROGRESS_LATE} : {_fa_number(late_seconds)} ثانیه")
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
#: پیام‌های خطای دقیق (بازخورد کارفرما: هر خطا باید پیامِ روشنِ خودش را
#: داشته باشد و فقط وقتی «موفق» گفته شود که RPC واقعاً انجام شده باشد).
EXCLUSIVE_BANNED_USER = "این کاربر از طرف ربات اخراج/بن شده؛ ابتدا «آزاد» کنید"
EXCLUSIVE_BOT_NO_RIGHTS = (
    "ربات در این گروه حق «افزودن ادمین» ندارد؛ ابتدا این حق را به ربات بدهید"
)
EXCLUSIVE_NO_TARGET_ENTITY = (
    "کاربر پیدا نشد؛ روی پیام خودش ریپلای کنید یا یوزرنیم را درست بدهید"
)
EXCLUSIVE_FAILED = "ارتقا انجام نشد؛ دوباره تلاش می‌شود"
EXCLUSIVE_TIMEOUT = "سرور پاسخ نداد؛ دوباره تلاش می‌شود"


def exclusive_message(*, username, kind="granted"):
    """``kind`` ∈ {granted, revoked, not_locked, already, missing,
    banned_user, bot_no_rights, no_target_entity, failed, timeout}

    هر خطا پیامِ فارسیِ خودش را دارد و «موفق» فقط با RPCِ بی‌خطا اعلام
    می‌شود؛ هیچ خطایی با قالبِ موفقیت پنهان نمی‌شود.
    """
    bodies = {
        "granted": EXCLUSIVE_GRANTED,
        "revoked": EXCLUSIVE_REVOKED,
        "not_locked": EXCLUSIVE_NOT_LOCKED,
        "already": EXCLUSIVE_ALREADY,
        "missing": EXCLUSIVE_MISSING,
        "banned_user": EXCLUSIVE_BANNED_USER,
        "bot_no_rights": EXCLUSIVE_BOT_NO_RIGHTS,
        "no_target_entity": EXCLUSIVE_NO_TARGET_ENTITY,
        "failed": EXCLUSIVE_FAILED,
        "timeout": EXCLUSIVE_TIMEOUT,
    }
    builder = _Builder()
    builder.line(EXCLUSIVE_HEADER.format(user=username), quote=True)
    body = bodies.get(kind, bodies["granted"])
    for body_line in body.split("\n"):
        builder.line(body_line)
    return builder.build()


# ---------------------------------------------------------------------------
#  ۷) تأیید دو مرحله‌ای دستورات خطرناک
# ---------------------------------------------------------------------------
# متن‌ها **عیناً** از اسکرین‌شات کارفرما هستند (همان اعراب‌گذاری و همان
# نیم‌فاصله/کشیده‌ها). ارقام لاتین‌اند. هر بخشی که در عکس نقل‌قول دارد،
# دقیقاً همان بازه به‌عنوان ``MessageEntityBlockquote`` علامت می‌خورد و کلِ
# متن Bold است.

TWO_FACTOR_HEADER = "تایید دو مرحله ای برای دستورات زیر"
#: نُه دستورِ محافظت‌شده — «●» ابتدای هر خط، عیناً مثل تصویر.
TWO_FACTOR_PROTECTED_START = "●"
TWO_FACTOR_PROTECTED_LINES = (
    "پـاکسـازی خودکـار",
    "پـاکسـازی کـامل",
    "پـاک لـیست اخـراج",
    "پـاک لـیست سکـوت",
    "ریـست آمـار",
    "تغـییر مـجازات",
    "تغـییر اخطـار",
    "سـرگرمـی فعـال",
    "لـغو کلـمـات ممـنوعه",
)
TWO_FACTOR_ASK_NEW = (
    "⚙ : یک رمز 8 رقمی بنویسید و ارسال کنید\n"
    "این رمز رو به خاطر بسپارید"
)
TWO_FACTOR_CREATED_TITLE = "رمـز ساختـه شـد 📨"
TWO_FACTOR_CREATED_BODY = (
    "از این پس برای اجرای این دستورات باید فقط رمز رو\n"
    "وارد کنید اونم فقط توسط مالک ثبت شده نه ادمین ها"
)
TWO_FACTOR_ASK_DANGER = (
    "✺ : این دستور تایید دو مرحله ای دارد\n"
    "اگر مالـک ثبت شده هستید رمز آن را وارد کنید"
)
TWO_FACTOR_DISABLED_TITLE = "⚠️ : تـایید دو مـرحلـه ای لـغو شد"
TWO_FACTOR_DISABLED_BODY = (
    "حالا ادمین های ثبت شده هم میتوانند از سایر\n"
    "دستورات هم استفاده کنند"
)
TWO_FACTOR_WRONG_TITLE = "📌 رمـز رد شد اشتباه میـباشد"
TWO_FACTOR_NOT_OWNER = "▪️رمـز شما تـایید نشد شما مالـک ثبت شده نیستید"
#: پیام‌های کمکی (در تصویر نبودند؛ طبق فهرستِ کلیدهای موردنیازِ کارفرما).
TWO_FACTOR_NEW_REQUEST = TWO_FACTOR_ASK_NEW
TWO_FACTOR_REPEAT_REQUEST = "⚙ : همان رمز 8 رقمی را یک بار دیگر ارسال کنید"
TWO_FACTOR_INVALID = "⚙ : رمز باید دقیقاً 8 رقم باشد (فقط عدد)"
TWO_FACTOR_OBVIOUS = "⚙ : این رمز خیلی ساده است؛ یک رمز 8 رقمیِ دیگر بگذارید"
TWO_FACTOR_MISMATCH = "⚙ : تکرار رمز مطابقت ندارد؛ دوباره «دو مرحله ای» را بزنید"
TWO_FACTOR_ALREADY_ON = "⚙ : تایید دو مرحله ای از قبل فعال است"
TWO_FACTOR_NOT_ON = "⚙ : تایید دو مرحله ای فعال نیست"
TWO_FACTOR_EXPIRED = "⏳ : درخواست منقضی شد؛ دستور را دوباره بزنید"
TWO_FACTOR_TOO_MANY = "🚫 : سه رمز غلط ⇒ درخواست لغو شد؛ دستور را دوباره بزنید"
TWO_FACTOR_EXECUTED = "✅ : اجرا شد"
TWO_FACTOR_FAILED = "❌ : انجام نشد"
TWO_FACTOR_LOCKOUT_TITLE = "⏳ : قفل موقت فعال شد"
TWO_FACTOR_DISABLE_REQUEST = (
    "✺ : برای لغو تایید دو مرحله ای رمز فعلی را وارد کنید"
)


def two_factor_activate_request():
    """کارتِ گام ۱: عنوان + نُه دستور داخل **یک** نقل‌قول، بعد درخواست رمز.

    عیناً مثل تصویر: نقل‌قول فقط عنوان و فهرست را می‌گیرد؛ «⚙ : …» بیرونِ
    نقل‌قول است.
    """
    builder = _Builder()
    # در تصویر، فقط عنوان داخل نقل‌قول است؛ فهرست با «●» بیرونِ آن می‌آید.
    builder.line(TWO_FACTOR_HEADER, quote=True)
    for line in TWO_FACTOR_PROTECTED_LINES:
        builder.line(f"{TWO_FACTOR_PROTECTED_START}{line}")
    builder.line()
    for line in TWO_FACTOR_ASK_NEW.split("\n"):
        builder.line(line)
    return builder.build()


def two_factor_created():
    """«رمز ساخته شد» + توضیح — عنوان داخل نقل‌قول."""
    builder = _Builder()
    builder.line(TWO_FACTOR_CREATED_TITLE, quote=True)
    builder.line()
    for line in TWO_FACTOR_CREATED_BODY.split("\n"):
        builder.line(line)
    return builder.build()


def two_factor_ask_password():
    """«این دستور تایید دو مرحله ای دارد …» — بدون نقل‌قول (مثل تصویر)."""
    builder = _Builder()
    for line in TWO_FACTOR_ASK_DANGER.split("\n"):
        builder.line(line)
    return builder.build()


def two_factor_disable_request():
    builder = _Builder()
    builder.line(TWO_FACTOR_DISABLE_REQUEST, quote=True)
    return builder.build()


def two_factor_lockout(seconds):
    """قفل موقت پس از ۵ رمز غلط (متنِ مکمل؛ در تصویر نبود)."""
    builder = _Builder()
    builder.line(TWO_FACTOR_LOCKOUT_TITLE, quote=True)
    builder.line()
    builder.line(f"بعد از {_fa_number(max(1, int(seconds)))} ثانیه دوباره "
                 "تلاش کنید")
    return builder.build()


def two_factor_wrong_password(attempts_left):
    """«📌 رمـز رد شد اشتباه میـباشد» + تعداد تلاشِ باقی‌مانده."""
    builder = _Builder()
    # عیناً مثل تصویر: بدون نقل‌قول.
    builder.line(TWO_FACTOR_WRONG_TITLE)
    builder.line()
    builder.line(f"تلاش باقی مانده : {_fa_number(max(0, int(attempts_left)))}")
    return builder.build()


def two_factor_not_registered_owner():
    builder = _Builder()
    builder.line(TWO_FACTOR_NOT_OWNER)
    return builder.build()


TWO_FACTOR_NO_REGISTERED_OWNER = (
    "▪️این گروه مالک ثبت شده ندارد؛ اول با دستور «ثبت مالک» مالک را ثبت کنید"
)


def two_factor_owner_required(registered=True):
    """فرمانِ قابلیت از کسی که مالکِ ثبت‌شده نیست.

    اگر گروه **هیچ** مالکِ ثبت‌شده‌ای ندارد، به‌جای پیامِ «شما مالک نیستید»
    می‌گوید اول باید «ثبت مالک» انجام شود (وگرنه کاربر نمی‌فهمد چرا هیچ‌کس
    نمی‌تواند رمز بگذارد).
    """
    if not registered:
        builder = _Builder()
        builder.line(TWO_FACTOR_NO_REGISTERED_OWNER)
        return builder.build()
    return two_factor_not_registered_owner()


def two_factor_gate_owner_only():
    """دستورِ خطرناک از غیرمالک وقتی قفل روشن است (عیناً پیامِ تصویر)."""
    return two_factor_ask_password()


def two_factor_failed(detail=""):
    """«❌ : انجام نشد» + دلیلِ کوتاه (برای عیب‌یابیِ کارفرما)."""
    builder = _Builder()
    builder.line(TWO_FACTOR_FAILED)
    if detail:
        builder.line()
        builder.line(str(detail)[:180])
    return builder.build()


def two_factor_message(kind="executed"):
    """قالب‌های تک‌خطی/دوستانه با نقل‌قولِ عنوان (مثل بقیهٔ قالب‌ها).

    ``kind`` ∈ {new_request, repeat_request, invalid, obvious, mismatch,
    already_on, not_on, disabled, wrong, not_owner, expired, too_many,
    executed, failed}
    """
    titles = {
        "new_request": TWO_FACTOR_NEW_REQUEST,
        "repeat_request": TWO_FACTOR_REPEAT_REQUEST,
        "invalid": TWO_FACTOR_INVALID,
        "obvious": TWO_FACTOR_OBVIOUS,
        "mismatch": TWO_FACTOR_MISMATCH,
        "already_on": TWO_FACTOR_ALREADY_ON,
        "not_on": TWO_FACTOR_NOT_ON,
        "wrong": TWO_FACTOR_WRONG_TITLE,
        "not_owner": TWO_FACTOR_NOT_OWNER,
        "expired": TWO_FACTOR_EXPIRED,
        "too_many": TWO_FACTOR_TOO_MANY,
        "executed": TWO_FACTOR_EXECUTED,
        "failed": TWO_FACTOR_FAILED,
    }
    if kind == "disabled":
        return two_factor_disabled()
    body = titles.get(kind, TWO_FACTOR_EXECUTED)
    builder = _Builder()
    for line in body.split("\n"):
        builder.line(line)
    return builder.build()


def two_factor_disabled():
    """«⚠️ : تـایید دو مـرحلـه ای لـغو شد» + توضیح — عنوان در نقل‌قول."""
    builder = _Builder()
    builder.line(TWO_FACTOR_DISABLED_TITLE, quote=True)
    builder.line()
    for line in TWO_FACTOR_DISABLED_BODY.split("\n"):
        builder.line(line)
    return builder.build()


#: سطرهای «لیست ادمینی» — عیناً به همان شکلِ درخواستیِ کارفرما.
TWO_FACTOR_HELP_TITLE = (
    "⏰ : برای تایید دو مرحله ای دستورات خطرناک بنویسید"
)
TWO_FACTOR_HELP_COMMANDS = ("دو مرحله ای", "لغو دو مرحله ای")


#: رزروِ قالب‌های آیندهٔ کارفرما (فقط جای‌گذاری؛ هیچ متنی اختراع نمی‌شود).
FUTURE: dict = {}


# ---------------------------------------------------------------------------
#  ۹) «متخلف» / «لغو متخلف» — عیناً مطابق اسکرین‌شات کارفرما
# ---------------------------------------------------------------------------
# در تصویر کارفرما: خطِ عنوان («⛔️ کاربر : @…») داخل نقل‌قول سروش‌پلاس و
# کلِ متن Bold است (همان قاعدهٔ باقی قالب‌ها). ارقام لاتین.
VIOLATOR_HEADER = "⛔️ کـاربـر : {user}"
VIOLATOR_MARKED_BODY = (
    "در لیست متخلف قرار گرفت\n"
    "از این پس پیام های کاربر پاک خواهد شد"
)
VIOLATOR_UNMARKED_BODY = (
    "از لیست متخلف معاف شد\n"
    "از این پس کاربر میتواند پیام ارسال کند"
)
#: پیام‌های خطای دقیق — هر خطا پیامِ فارسیِ خودش را دارد و «موفق» فقط با
#: RPC/ثبتِ بی‌خطا اعلام می‌شود.
VIOLATOR_ALREADY = "این کاربر از قبل در لیست متخلف گروه است"
VIOLATOR_MISSING = "این کاربر در لیست متخلف گروه نیست"
VIOLATOR_PROTECTED = "این کاربر محافظت شده است و نمیتواند متخلف شود"
VIOLATOR_NO_PERMISSION = "فقط مالک یا ادمین ثبت شده میتواند این دستور را بزند"
VIOLATOR_NO_TARGET = "روی پیام کاربر ریپلای کنید"
#: «لغو متخلف @یوزرنیم» وقتی یوزرنیم به هیچ کاربری resolve نشود.
VIOLATOR_USERNAME_NOT_FOUND = "کاربری با این یوزرنیم پیدا نشد"
VIOLATOR_BOT_NO_RIGHTS = (
    "ربات در این گروه حق حذف پیام ندارد؛\n"
    "ابتدا این حق را به ربات بدهید"
)
VIOLATOR_CREATOR_UNKNOWN = "مالک گروه خوانده نشد؛ ثبت انجام نشد"
VIOLATOR_NO_TARGET_ENTITY = "کاربر پیدا نشد؛ روی پیام خودش ریپلای کنید"
VIOLATOR_FAILED = "ثبت انجام نشد؛ دوباره تلاش میشود"
VIOLATOR_TIMEOUT = "سرور پاسخ نداد؛ دوباره تلاش میشود"
#: اطلاعٍ ساعتی به مالک وقتی ربات حقِ حذف را از دست داده است.
VIOLATOR_RIGHTS_LOST_TITLE = "⛔️ ربات حق حذف پیام متخلفان را ندارد"
VIOLATOR_RIGHTS_LOST_BODY = (
    "حذف پیام های کاربران متخلف انجام نمیشود؛\n"
    "مالک محترم حق حذف پیام ها را به ربات بدهید"
)

_VIOLATOR_BODIES = {
    "marked": VIOLATOR_MARKED_BODY,
    "unmarked": VIOLATOR_UNMARKED_BODY,
    "already": VIOLATOR_ALREADY,
    "missing": VIOLATOR_MISSING,
    "protected": VIOLATOR_PROTECTED,
    "bot_no_rights": VIOLATOR_BOT_NO_RIGHTS,
    "creator_unknown": VIOLATOR_CREATOR_UNKNOWN,
    "no_target_entity": VIOLATOR_NO_TARGET_ENTITY,
    "username_not_found": VIOLATOR_USERNAME_NOT_FOUND,
    "failed": VIOLATOR_FAILED,
    "timeout": VIOLATOR_TIMEOUT,
}


def violator_message(*, username, kind="marked"):
    """قالبِ «متخلف/لغو متخلف» و خطاها — عیناً مثل تصویر کارفرما.

    ``kind`` ∈ {marked, unmarked, already, missing, protected,
    bot_no_rights, creator_unknown, no_target_entity, username_not_found,
    failed, timeout}

    عنوان داخل نقل‌قول، بقیهٔ متن Bold (کلِ پیام Bold است).
    """
    builder = _Builder()
    builder.line(VIOLATOR_HEADER.format(user=username), quote=True)
    body = _VIOLATOR_BODIES.get(kind, VIOLATOR_MARKED_BODY)
    for body_line in body.split("\n"):
        builder.line(body_line)
    return builder.build()


def violator_rights_lost():
    """اطلاع به مالک: ربات حقِ حذف پیام را ندارد (عنوان در نقل‌قول)."""
    builder = _Builder()
    builder.line(VIOLATOR_RIGHTS_LOST_TITLE, quote=True)
    for body_line in VIOLATOR_RIGHTS_LOST_BODY.split("\n"):
        builder.line(body_line)
    return builder.build()
