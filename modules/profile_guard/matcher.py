"""🎯 تطبیق «نام کانال» و «جملهٔ بیوگرافی» با فهرست‌های فیلتر — منطق خالص.

این ماژول هیچ I/O و هیچ RPC ندارد؛ فقط تصمیم می‌گیرد یک متن با یکی از
عبارت‌های فیلتر مطابقت دارد یا نه. بنابراین کاملاً قابل تست است.

هدفِ اصلی: **دور زدن ممکن نباشد**، ولی هیچ حرفی هم از کلمات حذف نشود
(تا تطبیقِ اشتباه رخ ندهد). بنابراین:

* همهٔ نویسه‌های «تزئینی» بین حروف نادیده گرفته می‌شوند: فاصله، نیم‌فاصله،
  نقطه، ویرگول، خط تیره، اعراب، کشیده، نویسه‌های بی‌عرض، ایموجی و… .
  پس «ف ی ل م» ، «ف.ی.ل.م» ، «فـیـلـم» و «فیلم» همه یکی دیده می‌شوند.
* حروف عربی/فارسی یکدست می‌شوند: ``ي→ی`` ، ``ك→ک`` ، ``ة→ه`` ،
  ``أإآ→ا`` ، ``ؤ→و`` ، ``ئ→ی`` و ارقام فارسی/عربی → لاتین.
* **هیچ حرفی حذف نمی‌شود**؛ فقط جداکننده‌ها نادیده گرفته می‌شوند.

قاعدهٔ ضدِّ تطبیقِ اشتباه (مهم):

* عبارت‌های **یک‌کلمه‌ای** باید از دو طرف مرز کلمه داشته باشند؛ پس فیلترِ
  «فیلم» روی «فیلمبرداری» یا «فیلم‌ها» اثر نمی‌گذارد.
* عبارت‌های **چندکلمه‌ای** (جمله) از سمت راست مرز لازم ندارند تا نوشتنِ
  چسبیده («بیاتوکانالمفیلمهست») هم گرفته شود.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, NamedTuple, Optional, Sequence

# ---------------------------------------------------------------------------
# یکدست‌سازی نویسه‌ها
# ---------------------------------------------------------------------------

_FOLD_TABLE = {
    # حروف عربی → فارسی
    ord("ي"): "ی", ord("ى"): "ی", ord("ئ"): "ی", ord("ۍ"): "ی",
    ord("ك"): "ک",
    ord("ة"): "ه", ord("ۀ"): "ه", ord("ﻩ"): "ه",
    ord("أ"): "ا", ord("إ"): "ا", ord("آ"): "ا", ord("ٱ"): "ا",
    ord("ؤ"): "و", ord("ﻭ"): "و",
    # ارقام فارسی/عربی → لاتین
    **{ord("۰") + i: str(i) for i in range(10)},
    **{ord("٠") + i: str(i) for i in range(10)},
}

#: اعراب، تنوین، تشدید و علامت‌های قرآنی + کشیده (tatweel).
_DIACRITICS_RE = re.compile(
    "[\u064b-\u065f\u0670\u06d6-\u06ed\u0640\ufeff]"
)

#: نویسه‌های بی‌عرض/کنترلِ جهت که در متن دیده نمی‌شوند.
_INVISIBLE_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u2060-\u2064\u202a-\u202e\u205f\u00ad]"
)


def normalize(text) -> str:
    """متن را برای مقایسه یکدست می‌کند (حروف حذف نمی‌شوند)."""
    if text is None:
        return ""
    value = str(text)
    try:
        value = unicodedata.normalize("NFKC", value)
    except (TypeError, ValueError):  # pragma: no cover — ورودیِ ناسالم
        pass
    value = _INVISIBLE_RE.sub("", value)
    value = _DIACRITICS_RE.sub("", value)
    value = value.translate(_FOLD_TABLE)
    return value.lower()


def _letters_only(value: str) -> str:
    """فقط حروف/ارقام را برمی‌گرداند (برای ساخت الگوی تطبیق)."""
    return "".join(ch for ch in value if ch.isalnum())


#: هر چیزی که «حرف یا رقم» نیست، یک جداکنندهٔ قابل‌نادیده‌گیری است.
_SEP = r"[^\w]*"
_BOUNDARY_LEFT = r"(?<!\w)"
_BOUNDARY_RIGHT = r"(?!\w)"


class Phrase(NamedTuple):
    """یک عبارت فیلتر آمادهٔ تطبیق."""

    raw: str
    normalized: str
    pattern: "re.Pattern[str]"
    is_multiword: bool


def _compile(raw: str) -> Optional[Phrase]:
    normalized = normalize(raw)
    letters = _letters_only(normalized)
    # عبارت‌های بی‌حرف یا خیلی کوتاه (۱ حرف) کنار گذاشته می‌شوند تا تطبیقِ
    # تصادفی رخ ندهد.
    if len(letters) < 2:
        return None
    body = _SEP.join(re.escape(ch) for ch in letters)
    is_multiword = len(normalized.split()) > 1
    right = "" if is_multiword else _BOUNDARY_RIGHT
    try:
        pattern = re.compile(_BOUNDARY_LEFT + body + right)
    except re.error:  # pragma: no cover — با escape هرگز رخ نمی‌دهد
        return None
    return Phrase(raw=str(raw).strip(), normalized=normalized,
                  pattern=pattern, is_multiword=is_multiword)


_cache: dict = {}


def compile_phrases(phrases: Iterable[str]) -> List[Phrase]:
    """فهرست خام → فهرست عبارت‌های کامپایل‌شده (با کش)."""
    items = tuple(p for p in (str(x).strip() for x in phrases) if p)
    cached = _cache.get(items)
    if cached is not None:
        return cached
    compiled: List[Phrase] = []
    for raw in items:
        phrase = _compile(raw)
        if phrase is not None:
            compiled.append(phrase)
    _cache[items] = compiled
    return compiled


def find_match(text, phrases: Sequence[Phrase]) -> Optional[Phrase]:
    """اولین عبارتِ مطابق با متن را برمی‌گرداند (یا ``None``)."""
    if not phrases:
        return None
    haystack = normalize(text)
    if not haystack:
        return None
    for phrase in phrases:
        try:
            if phrase.pattern.search(haystack):
                return phrase
        except Exception:  # pragma: no cover — یک عبارت نباید کل مسیر را بشکند
            continue
    return None


def matches_any(text, phrases: Sequence[Phrase]) -> bool:
    return find_match(text, phrases) is not None


def build(phrases: Iterable[str]) -> List[Phrase]:
    """میان‌بر خوانا برای ساخت فهرست آمادهٔ تطبیق."""
    return compile_phrases(phrases)
