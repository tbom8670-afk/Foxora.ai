"""🖇 تشخیص «لینک در بیوگرافی» — منطق خالص و بدون I/O.

این ماژول فقط تصمیم می‌گیرد متن بیوگرافی یک کاربر «لینک» دارد یا نه.
هیچ درخواست شبکه‌ای، ذخیره‌سازی یا مجازاتی اینجا انجام نمی‌شود؛ بنابراین
کاملاً قابل تست و بدون هزینهٔ CPU قابل توجه است.

پوشش تشخیص:
  * ``http://`` ، ``https://`` ، ``ftp://`` ، ``hxxp://`` (نوشتار مبدل)
  * ``www.`` و دامنهٔ برهنه با TLD رایج (``example.com`` ، ``shop.ir`` …)
  * دامنه‌های کوتاه‌کننده و دعوت: ``t.me`` ، ``telegram.me`` ، ``joinchat`` ،
    ``sapp.ir`` ، ``splus.ir`` ، ``soroush.ir`` ، ``soroush.plus``
  * لینک دعوت مستقیم سروش/تلگرام با فرم ``+HASH`` یا ``joinchat/HASH``
  * آیدی ``@username`` فقط در دو حالت: (الف) در کنار واژه‌های دعوت/تبلیغ
    (``جوین``، ``عضو``، ``کانال``، ``join`` …)؛ (ب) وقتی صاحب گروه با
    تنظیم ``detect_username`` آن را روشن کرده باشد.

مقاوم‌سازی در برابر شکل‌های نوشتاری:
  * فاصله/نیم‌فاصله/کاراکترهای بی‌عرض اطراف ``.`` و ``/`` و ``:`` و ``@``
  * نویسه‌های نقطهٔ غیر ASCII (``۔`` ، ``。`` ، ``․`` ، ``·`` ، ``•`` ، ``٫``)
  * اسلش و دونقطهٔ تمام‌عرض (``／`` ، ``：``)
  * نوشتارهای ``[.]`` ، ``(.)`` ، `` dot `` و ``نقطه`` به‌جای نقطه
  * ارقام فارسی/عربی و حروف بزرگ/کوچک

قاعدهٔ مهم: متنی که لینک ندارد هرگز نباید True بدهد. هر الگوی «مبهم»
(مثل ``@username`` تنها) به‌صورت پیش‌فرض نادیده گرفته می‌شود.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# نویسه‌های چندمعنا و آماده‌سازی متن
# ---------------------------------------------------------------------------

_DOT_CHARS = "\u06d4\u3002\u2024\u00b7\u2022\u066b\u2027\u2219\uff0e"
_DOT_TABLE = {ord(ch): "." for ch in _DOT_CHARS}
_SLASH_TABLE = {0xFF0F: "/", 0x2044: "/", 0x2215: "/", 0x29F8: "/"}
_COLON_TABLE = {0xFF1A: ":", 0xFE55: ":", 0x02D0: ":", 0x2236: ":"}

# کاراکترهایی که «چسبندگی» و فاصله‌گذاری در لینک را به‌هم می‌زنند.
_INVISIBLE_RE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u0640\ufeff]"
)
_SPACE = r"[\s\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"

# نقطه/اسلش/دونقطه/ات که با فاصله و نویسهٔ بی‌عرض «شکسته» شده‌اند.
_BROKEN_DOT_RE = re.compile(_SPACE + r"*(\[\.\]|\(\.\)|\.|dot|نقطه)" + _SPACE + r"*", re.I)
_BROKEN_SLASH_RE = re.compile(_SPACE + r"*(\/|\\|\|)" + _SPACE + r"*")
_BROKEN_COLON_RE = re.compile(_SPACE + r"*:" + _SPACE + r"*")
_BROKEN_AT_RE = re.compile(_SPACE + r"*@" + _SPACE + r"*")

# ارقام فارسی/عربی → لاتین (برای دامنه‌های عددی و هش‌های دعوت)
_DIGIT_TABLE = {
    ord("۰") + i: str(i) for i in range(10)
}
_DIGIT_TABLE.update({ord("٠") + i: str(i) for i in range(10)})
_DIGIT_TABLE.update({ord("۰") + i: str(i) for i in range(10)})


def normalize(text: str) -> str:
    """متن را برای تطبیق لینک یکدست می‌کند (بدون تغییر طول منطقی متن اصلی)."""
    if not text:
        return ""
    # NFKC: حروف تمام‌عرض/ریاضی (𝐬𝐢𝐭𝐞) و فرم‌های نمایشی عربی به شکل پایه برمی‌گردند.
    try:
        value = unicodedata.normalize("NFKC", str(text))
    except (TypeError, ValueError):
        value = str(text)
    value = value.translate(_DIGIT_TABLE)
    value = value.translate(_DOT_TABLE)
    value = value.translate(_SLASH_TABLE)
    value = value.translate(_COLON_TABLE)
    value = _INVISIBLE_RE.sub("", value)
    value = value.lower()
    return value


# «s i t e . c o m» — حروف تک‌تک با فاصله از هم جدا شده‌اند.
_SINGLE_TOKEN_RE = re.compile(r"^(?:[a-z0-9]|[a-z0-9]\.|\.[a-z0-9]|\.)$", re.I)


def _squash_letter_spacing(value: str) -> str:
    """قطعه‌های تک‌حرفیِ لاتین را که با فاصله از هم جدا شده‌اند به هم می‌چسباند."""
    if not value:
        return value
    tokens = value.split(" ")
    if len(tokens) < 4:
        return value
    out = []
    run = []

    def _flush():
        # نقطه جدا نوشته می‌شود ولی حرف حساب نمی‌شود.
        if sum(1 for token in run if token != ".") >= 4:
            out.append("".join(run))
        else:
            out.extend(run)
        run.clear()

    for token in tokens:
        if _SINGLE_TOKEN_RE.match(token):
            run.append(token)
        else:
            _flush()
            out.append(token)
    _flush()
    return " ".join(out)


def compact(text: str) -> str:
    """فاصله‌های اطراف جداکننده‌های لینک را حذف می‌کند تا «t . me / x» هم دیده شود."""
    value = _squash_letter_spacing(normalize(text))
    # چند بار تکرار می‌شود تا «t . me / join chat» کامل بچسبد.
    previous = None
    while previous != value:
        previous = value
        value = _BROKEN_DOT_RE.sub(".", value)
        value = _BROKEN_SLASH_RE.sub("/", value)
        value = _BROKEN_COLON_RE.sub(":", value)
        value = _BROKEN_AT_RE.sub("@", value)
    return _squash_letter_spacing(value)


# ---------------------------------------------------------------------------
# الگوهای تشخیص
# ---------------------------------------------------------------------------

_TLD = (
    "com|ir|net|org|info|me|co|io|app|xyz|dev|site|online|shop|store|club|link|pro|top|tv|"
    "ru|de|uk|us|ca|biz|live|fun|click|space|website|gov|edu|cc|ws|in|to|ly|gl|gd|tk|ml|"
    "ga|cf|gq|pw|icu|buzz|monster|rest|bar|bet|win|bid|loan|download|racing|review|stream|"
    "trade|date|faith|party|science|accountant|cricket|webcam|blog|page|news|email|social|"
    "agency|company|solutions|services|support|systems|network|digital|media|video|music|"
    "art|design|photo|pics|shop|market|money|finance|capital|exchange|academy|school|"
    "university|institute|foundation|charity|church|travel|holiday|hotel|estate|house|"
    "property|rent|care|clinic|doctor|health|fitness|gym|game|games|play|fun|zone|world|"
    "life|style|fashion|beauty|spa|flowers|gift|gifts|deal|deals|discount|sale|cheap|"
    "plus|today|now|fast|easy|best|one|vip|ink|pub|run|sh|ac|id|gg|ai"
)

_HOST = r"[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9-]{0,62})+"

# لینک کامل با پروتکل — باید میزبان واقعی (حداقل یک نقطه) داشته باشد.
_URL_SCHEME_RE = re.compile(
    r"(?:https?|ftps?|hxxps?)\s*:\s*/\s*/\s*" + _HOST, re.I
)
_WWW_RE = re.compile(r"(?:^|[^a-z0-9@])www\." + _HOST, re.I)
_LABEL_RE_PART = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_BARE_DOMAIN_RE = re.compile(
    r"(?:^|[^a-z0-9@._-])("
    + _LABEL_RE_PART
    + r"(?:\." + _LABEL_RE_PART + r")*\.(?:"
    + _TLD
    + r"))(?![a-z0-9-])",
    re.I,
)
_IP_LINK_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?(?:/\S*)?")

# دامنه‌های پیام‌رسان: بدون «مقصد» لینک حساب نمی‌شوند (t.me/ ← ناقص).
_MESSENGER_HOSTS = (
    "t.me", "telegram.me", "telegram.dog", "telegrama.me", "tl.me",
)
_SOROUSH_HOSTS = (
    "sapp.ir", "splus.ir", "soroush.ir", "soroush.plus", "soroushplus.ir",
)
_MESSENGER_PATH_RE = re.compile(
    r"(?:^|[^a-z0-9@._-])(?:"
    + "|".join(re.escape(h) for h in _MESSENGER_HOSTS)
    + r")\s*/\s*(?:joinchat\s*/\s*|s\s*/\s*|\+\s*)?[a-z0-9_\-+]{1,}",
    re.I,
)
_SOROUSH_PATH_RE = re.compile(
    r"(?:^|[^a-z0-9@._-])(?:"
    + "|".join(re.escape(h) for h in _SOROUSH_HOSTS)
    + r")\s*/\s*[a-z0-9_\-+/]{1,}",
    re.I,
)
_JOINCHAT_RE = re.compile(r"(?:^|[^a-z0-9_])joinchat\s*/\s*[a-z0-9_\-]{6,}", re.I)
_DIRECT_INVITE_RE = re.compile(
    r"(?:^|[^a-z0-9@._-])(?:"
    + "|".join(re.escape(h) for h in _MESSENGER_HOSTS)
    + r")\s*/\s*\+[a-z0-9_\-]{6,}",
    re.I,
)

_USERNAME_RE = re.compile(r"(?<![a-z0-9_@])@([a-z0-9_]{4,32})(?![a-z0-9_])", re.I)
_AD_WORDS_RE = re.compile(
    r"(?:جوین|جوین\s*شو|عضو\s*شو|عضویت|کانال|تبلیغ|دایرکت|پی\s*وی|پیوی|"
    r"join|subscribe|follow|advertis)",
    re.I,
)


def find_link(about: Optional[str], *, detect_username: bool = False) -> Optional[str]:
    """برچسب کوتاه نوع لینک، یا ``None`` اگر بیو لینک ندارد.

    ``detect_username`` فقط برای آیدی ``@username`` تنهاست؛ اگر همان آیدی در
    کنار واژه‌های دعوت/تبلیغ باشد، حتی با خاموش بودن این گزینه هم لینک
    محسوب می‌شود (چون «کانال رو جوین کن @x» واضحاً تبلیغ است).

    لینک «ناقص» (``http://`` تنها، ``t.me/`` بدون مقصد، ``www.`` تنها)
    لینک حساب نمی‌شود؛ هدف این قابلیت تبلیغ واقعی است، نه هر نقطه و اسلش.
    """
    if about is None:
        return None
    raw = str(about)
    if not raw.strip():
        return None

    # روی متن فشرده‌شده کار می‌کنیم تا هر جداییِ عمدیِ حروف از بین برود.
    value = compact(raw)

    if _URL_SCHEME_RE.search(value):
        return "لینک وب"
    if _DIRECT_INVITE_RE.search(value):
        return "لینک دعوت"
    if _MESSENGER_PATH_RE.search(value):
        return "لینک تلگرام"
    if _SOROUSH_PATH_RE.search(value):
        return "لینک سروش"
    if _JOINCHAT_RE.search(value):
        return "لینک دعوت"
    if _WWW_RE.search(value):
        return "لینک وب"
    if _IP_LINK_RE.search(value):
        return "لینک IP"

    domain = _BARE_DOMAIN_RE.search(value)
    if domain:
        host = domain.group(1).lower()
        # دامنهٔ برهنهٔ پیام‌رسان بدون مقصد، لینک نیست.
        if host not in _MESSENGER_HOSTS and host not in _SOROUSH_HOSTS:
            return "دامنه"

    # آیدی عمومی
    username_match = _USERNAME_RE.search(value)
    if username_match:
        handle = "@" + username_match.group(1)
        if detect_username:
            return f"آیدی {handle}"
        if _AD_WORDS_RE.search(value):
            return f"آیدی {handle}"
    return None


def has_link(about: Optional[str], *, detect_username: bool = False) -> bool:
    """``True`` اگر بیوگرافی حاوی لینک باشد."""
    return find_link(about, detect_username=detect_username) is not None


def analyze(about: Optional[str], *, detect_username: bool = False) -> Tuple[bool, Optional[str]]:
    """``(دارد؟، برچسب دلیل)`` — راحت‌ترین راه استفاده در ماژول‌های دیگر."""
    label = find_link(about, detect_username=detect_username)
    return (label is not None, label)


def reason_text(label: Optional[str]) -> str:
    """متن کوتاه فارسی برای لاگ/پیام اخطار."""
    if not label:
        return "لینک در بیوگرافی"
    return f"لینک در بیوگرافی ({label})"
