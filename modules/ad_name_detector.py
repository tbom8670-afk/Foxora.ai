"""تشخیص مستقل نام‌های تبلیغاتی؛ جدا از فیلتر متن گروه."""
import re
import unicodedata

from modules.user_display import format_user
from modules.global_profile_terms import GLOBAL_PROFILE_TERMS
from modules.profile_guard import matcher as profile_matcher

# فیلتر سراسریِ نام خود ربات (مثل «خاله» و «بیوچک»)، نه فیلتر اسمِ
# قابل‌ویرایش هر گروه. صورت‌های سانسورشده و حروف تکراری هم بررسی می‌شوند.
_NEW_NAME_TERMS = (
    "عسل خانوم", "خانوم", "پی", "استوری", "استوریم", "استوررررری",
    "پیوی", "چنلم", "فیلم", "فلم", "فی لم",
)
_NAME_EMOJIS = ("🤤", "😈", "💦", "🔥")

_TERMS = (
    r"بیو\s*چک", r"چک\s*بیو", r"بیوگرافی\s*چک", r"بیومو\s*(?:چک|ببینید|ببین)",
    r"بیو.*(?:فیلم|لینک|چک|ببین)",
    r"فیلم",
    r"حال\s*پی",
    r"تمام\s*سانسور",
    r"حال\s*می(?:د|ذ)م",
    r"فیلم\s*پی",
    r"🔞",
    r"پی\s*وی",
    r"پیوی",
    r"\bpv\b",
    r"خاله",
    r"صیغه",
    r"رایگان",
    r"سکس",
    r"سکسی",
    r"پورن",
    r"نود",
    r"فیلتر\s*شکن",
    r"فیلترشکن",
    r"\bvpn\b",
    r"شارژ\s*رایگان",
    r"کانال",
    r"پکیج",
    r"ارز\s*دیجیتال",
    r"تتر",
    r"پهلوی",
    r"شاهزاده",
    r"شاه\s*زاده",
    r"پرچم\s*آمریکا",
    r"دلباخته\s*پهلوی",
    r"رضا\s*شاه",
    r"رضاشاه",
    r"محمدرضا\s*شاه",
    r"جان\s*فدای\s*میهن",
    r"جانفدای\s*میهن",
    r"فرزند\s*ایران",
)
_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in _TERMS)


def _norm(value):
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).lower()
    value = value.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    # اعراب و کشیده خودِ حرف نیستند؛ بقیهٔ نمادها (از جمله ایموجیِ
    # سانسورکنندهٔ وسط واژه) جداکننده‌اند. ایموجی‌های ممنوعه جدا بررسی می‌شوند.
    value = re.sub(r"[\u0640\u064b-\u065f\u0670\u06d6-\u06ed\u200b-\u200f\ufeff\ufe00-\ufe0f]", "", value)
    value = re.sub(r"[\W_]+", " ", value)
    return " ".join(value.split())


def _collapse(value):
    """جمع کردن حروف تکراری: «بیوچکک» و «بییییو چک» → «بیوچک» و «بیو چک»."""
    return re.sub(r"(.)\1+", r"\1", value)


# متنِ یکدست‌شده بدون جداکننده: «فی لم» و «ف.ی.لـلـم» هر دو «فیلم».
# «پی» فقط مستقل (یا حروفِ سانسورشدهٔ مستقل) مجاز است، نه در «پیام».
_ROBUST_TERMS = tuple(
    (term, _collapse(_norm(term).replace(" ", "")))
    for term in _NEW_NAME_TERMS if term != "پی"
)
_SHORT_PI = re.compile(r"(?<!\w)پ(?:\s*پ)*\s*ی(?:\s*ی)*(?!\w)")


_GLOBAL_NAME_PHRASES = profile_matcher.build(GLOBAL_PROFILE_TERMS)


def display_name(user):
    return format_user(user)


def reason(user):
    username_raw = getattr(user, "username", None) or ""
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    for raw in (username_raw, f"{first} {last}".strip()):
        hit = profile_matcher.find_match(raw, _GLOBAL_NAME_PHRASES)
        if hit:
            return hit.raw
    for raw in (username_raw, first, last):
        for emoji in _NAME_EMOJIS:
            if emoji in str(raw):
                return emoji

    username = _norm(username_raw)
    name = _norm(f"{first} {last}".strip())
    for value in (username, name):
        if not value:
            continue
        compact = _collapse(value.replace(" ", ""))
        for term, key in _ROBUST_TERMS:
            if key in compact:
                return term
        if _SHORT_PI.search(value):
            return "پی"
        # الگوهای قدیمی را نیز حفظ می‌کنیم تا مجازات‌های پیشین عوض نشوند.
        for candidate in (value, _collapse(value)):
            for pattern in _PATTERNS:
                if pattern.search(candidate):
                    return pattern.pattern
    return None
