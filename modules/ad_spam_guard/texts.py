"""قالب پیام هشدار.

⚠️ این قالب را کارفرما خودش می‌دهد. تا وقتی ``WARN_TEMPLATE`` پر نشده باشد
هیچ پیام هشداری ارسال نمی‌شود (فقط حذف انجام می‌گیرد) — بنابراین هیچ
قالبِ موقتِ تکراری به گروه فرستاده نمی‌شود.

متغیرهای موجود برای جایگذاری:
    {user}   — نمایش کاربر (مثل ``@username`` یا نام نمایشی)
    {chat}   — شناسهٔ گروه
"""

# None = هنوز قالبی تعیین نشده؛ پیام هشدار ارسال نمی‌شود.
WARN_TEMPLATE: str | None = None


def render_warn(user: str, chat: str) -> str | None:
    """قالب هشدار را می‌سازد؛ اگر تعیین نشده باشد None."""
    if not WARN_TEMPLATE:
        return None
    try:
        return WARN_TEMPLATE.format(user=user, chat=chat)
    except Exception:
        # قالبِ خراب نباید کل قابلیت را از کار بیندازد.
        return WARN_TEMPLATE
