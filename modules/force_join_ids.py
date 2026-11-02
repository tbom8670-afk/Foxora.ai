"""🎯 قرارداد کانونیک شناسه‌ها برای قابلیت «عضویت اجباری کانال».

این تنها جایی است که تصمیم می‌گیرد یک شناسهٔ گروه/کانال به کدام ردیف
دیتابیس می‌رسد. همهٔ مسیرهای کد (رویداد، فرمان، مسیر پیام، ناظر و تست)
باید از همین توابع رد شوند؛ در غیر این صورت یک گروه می‌تواند دو ردیف
جدا بگیرد (باگ واقعی و رایجی که در پرامپت هم به آن اشاره شده است).

قرارداد:

* **گروه** (کلید اصلی جدول) → عدد **علامت‌دار** به شکل peer سروش:
  سوپرگروه ``-100xxxxxxxxxx`` و گروه کوچک به شکل اصلی خودش.
  اگر کسی شناسهٔ بدون علامت (``xxxxxxxxxx``) بدهد، به همان شکل علامت‌دار
  تبدیل می‌شود تا هر دو مسیر به یک ردیف برسند.
* **کانال** → عدد **بدون علامت** (همان ``channel_id`` خام).

>>> canonical_group_key(-1001234567890) == canonical_group_key(1234567890)
True
>>> channel_key(-1001234567890) == 1234567890
True
"""

from __future__ import annotations

CHANNEL_ID_OFFSET = 1_000_000_000_000

__all__ = [
    "CHANNEL_ID_OFFSET",
    "canonical_group_key",
    "canonical_group_key_str",
    "channel_key",
    "chat_id_for_rpc",
    "group_key_variants",
]


def _to_int(value):
    """تبدیل ایمن به int؛ ``None`` در صورت شکست."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def canonical_group_key(value) -> int:
    """شناسهٔ کانونیک و علامت‌دار گروه.

    ورودی می‌تواند ``-100…``، شناسهٔ بدون علامت، رشته یا هر آبجکت دارای
    ``id``/``channel_id``/``chat_id`` باشد. خروجی همیشه یک عدد علامت‌دار
    پایدار است. اگر تبدیل ممکن نباشد ``0`` برمی‌گردد (هرگز استثنا نمی‌دهد،
    چون این تابع در مسیر داغ پیام صدا زده می‌شود).
    """
    number = _to_int(value)
    if number is None:
        for attribute in ("channel_id", "chat_id", "user_id", "id"):
            number = _to_int(getattr(value, attribute, None))
            if number is not None:
                break
    if number is None:
        return 0
    if number <= -CHANNEL_ID_OFFSET:
        # از قبل علامت‌دار است (peer سوپرگروه).
        return number
    if number > 0:
        # شناسهٔ بدون علامت کانال/سوپرگروه → همان شکل علامت‌دار.
        return -(CHANNEL_ID_OFFSET + number)
    return number


def canonical_group_key_str(value) -> str:
    """همان کلید کانونیک، به شکل رشته برای کلیدهای دیکشنری/کش."""
    return str(canonical_group_key(value))


def channel_key(value) -> int:
    """شناسهٔ **بدون علامت** کانال.

    ``-1001234567890`` → ``1234567890`` و ``1234567890`` → ``1234567890``.
    """
    number = _to_int(value)
    if number is None:
        for attribute in ("channel_id", "chat_id", "id"):
            number = _to_int(getattr(value, attribute, None))
            if number is not None:
                break
    if number is None:
        return 0
    if number <= -CHANNEL_ID_OFFSET:
        return abs(number) - CHANNEL_ID_OFFSET
    return abs(number)


def chat_id_for_rpc(value) -> int:
    """شکل علامت‌دار ``chat_id`` برای RPC (حذف پیام، ارسال پیام)."""
    return canonical_group_key(value)


def group_key_variants(value) -> tuple:
    """همهٔ شکل‌های ممکن یک گروه، برای resolve و پاک‌سازی رکوردهای قدیمی."""
    signed = canonical_group_key(value)
    unsigned = channel_key(value)
    return (signed, unsigned, -unsigned if unsigned else 0)
