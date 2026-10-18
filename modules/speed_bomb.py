# -*- coding: utf-8 -*-
"""💣 بازی «بمب سرعت روباه» (Speed Bomb)

ویژگی‌ها:
- اجرا با دستور «بمب سرعت» یا «بمب ساعتی» (و «بمب سرعت روباه») در گروه.
- دقیقاً ۳ مرحله (دور اول، دور دوم، دور سوم)؛ هر مرحله ۱۵ ثانیه فرصت دارد.
- در هر مرحله یک موضوع و یک حرف از الفبای فارسی که فهرست معتبر کافی دارد
  به‌صورت تصادفی انتخاب می‌شود.
- اعتبارسنجی دقیق پاسخ‌ها از روی بانک واژگان معتبر و قابل توسعه (میوه، کشور،
  شهر، حیوان، غذا، اسم، وسیله) با نرمال‌سازی کامل حروف فارسی/عربی و فاصله‌ها.
- اولین پاسخ معتبرِ دریافت‌شده پس از شروع مرحله برندهٔ آن مرحله می‌شود و
  دقیقاً ۳ سکه برنز از طریق سیستم اقتصاد فعلی ربات (`economy.award_game`)
  دریافت می‌کند (حداکثر یک برنده و یک پرداخت در هر مرحله).
- اگر در ۱۵ ثانیه پاسخ معتبری ثبت نشود، پیام
  «جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود» ارسال شده و مرحلهٔ بعد آغاز
  می‌گردد.
- پس از پایان مرحلهٔ سوم، مجموع برندگان و سکه‌های کسب‌شدهٔ هر کاربر نمایش
  داده شده و وضعیت موقت بازی پاک می‌شود.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import random
import re
import time

import economy
from economy import rewards as economy_rewards
from modules.group_id import normalize_group_id

try:
    from splusthon.tl.types import MessageEntityBold
except ImportError:  # pragma: no cover
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

GAME = "speed_bomb"
COMMAND = "بمب سرعت"
COMMANDS = frozenset({"بمب سرعت", "بمب ساعتی", "بمب سرعت روباه"})

TOTAL_STAGES = 3
STAGE_SECONDS = 15
REWARD_BRONZE = 3
MIN_WORDS_PER_PAIR = 2

STAGE_ORDINALS = {
    1: "دور اول",
    2: "دور دوم",
    3: "دور سوم",
}

NO_WINNER_STAGE_MESSAGE = "جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود"
ALREADY_ACTIVE_MESSAGE = (
    "⏳ بازی بمب سرعت همین حالا در این گروه در جریان است؛ لطفاً به مرحلهٔ فعلی پاسخ دهید."
)

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_AR_CHAR_MAP = {
    "ي": "ی",
    "ئ": "ی",
    "ى": "ی",
    "ك": "ک",
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ؤ": "و",
    "ة": "ه",
    "ۀ": "ه",
    "ٔ": "",
    "ـ": "",  # کشیده (tatweel)
}
_DIACRITICS_RE = re.compile(r"[\u064b-\u065f\u0670\u200b\u200d\u200e\u200f\ufeff]")
_STRIP_PUNCT_CHARS = " \t\r\n!?.،؛:«»\"'()[]{}-_+=*/\\|~`"
_VALID_PERSIAN_RE = re.compile(r"^[آ-ی\s]+$")

_RANDOM = random.SystemRandom()
_FALLBACK_SEQ = 0

# وضعیت فعال هر گروه: chat_key -> session dict
_ACTIVE: dict[str, dict] = {}
# تسک‌های تایمر هر گروه: chat_key -> asyncio.Task
_STAGE_TASKS: dict[str, asyncio.Task] = {}


def to_persian_digits(value) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def u16_length(text: str) -> int:
    return len(str(text or "").encode("utf-16-le")) // 2


def bold_entities_for(text: str) -> list:
    length = u16_length(text)
    if length <= 0:
        return []
    return [MessageEntityBold(offset=0, length=length)]


def normalize_word(value) -> str:
    """نرمال‌سازی استاندارد واژهٔ فارسی برای مقایسهٔ دقیق."""
    if value is None:
        return ""
    text = str(value).strip(_STRIP_PUNCT_CHARS).lower()
    if not text:
        return ""
    text = _DIACRITICS_RE.sub("", text)
    for src, dst in _AR_CHAR_MAP.items():
        text = text.replace(src, dst)
    text = text.replace("\u200c", " ").replace("\u00a0", " ")
    text = text.strip(_STRIP_PUNCT_CHARS)
    return " ".join(text.split())


def _compact_word(normalized: str) -> str:
    """شکل بدون فاصله برای تطبیق املاهای سرهم/نیم‌فاصله (مثلاً توت‌فرنگی/توتفرنگی)."""
    return normalized.replace(" ", "")


def normalize_letter(letter: str) -> str:
    norm = normalize_word(letter)
    return norm[0] if norm else ""


def is_speed_bomb_command(text: str) -> bool:
    norm = normalize_word(text)
    return norm in {normalize_word(c) for c in COMMANDS}


# ---------------------------------------------------------------------------
# بانک جامع و قابل توسعهٔ واژگان معتبر به تفکیک موضوع
# ---------------------------------------------------------------------------
CATEGORY_PROMPTS = {
    "میوه": ("🍎", "یک میوه با حرف «{letter}» بگو!"),
    "کشور": ("🌍", "نام یک کشور با حرف «{letter}» بگو!"),
    "شهر": ("🏙️", "نام یک شهر با حرف «{letter}» بگو!"),
    "حیوان": ("🐾", "یک حیوان با حرف «{letter}» بگو!"),
    "غذا": ("🍲", "نام یک غذا با حرف «{letter}» بگو!"),
    "اسم": ("👤", "یک اسم با حرف «{letter}» بگو!"),
    "شیء": ("🧰", "نام یک وسیله با حرف «{letter}» بگو!"),
}

BASE_WORD_BANK: dict[str, list[str]] = {
    "میوه": [
        # الف / آ
        "انار", "انگور", "آناناس", "آلبالو", "انجیر", "انبه", "ازگیل", "آلو",
        "آلوچه", "آووکادو",
        # ب
        "به", "بلوبری", "بالنگ", "بنه", "برگ زردآلو",
        # پ
        "پرتقال", "پاپایا", "پوملو", "پسته",
        # ت
        "توت", "توت فرنگی", "تمشک", "تمر هندی", "ترنج",
        # چ
        "چاقاله", "چاقاله بادام",
        # خ
        "خرما", "خرمالو", "خربزه", "خیار", "خیارچنبر",
        # د
        "دارابی", "دوریان", "دراگون فروت",
        # ر
        "رطب", "ریواس", "رزبری",
        # ز
        "زیتون", "زردآلو", "زغال اخته", "زالزالک", "زرشک", "زنجبیل",
        # س
        "سیب", "سنجد", "سیب ترش", "سیب گلاب",
        # ش
        "شلیل", "شاه توت", "شفتالو", "شمام",
        # ط
        "طالبی", "طارم",
        # ع
        "عناب", "عسلی",
        # غ
        "غوره", "غنچه",
        # ف
        "فندق", "فیسالیس", "فرنگی",
        # ق
        "قیسی", "قره قاط",
        # ک
        "کیوی", "کشمش", "کامکوات", "نارگیل", "کرنبری", "کدو حلوایی",
        # گ
        "گیلاس", "گلابی", "گریپ فروت", "گرمک", "گوجه سبز",
        # ل
        "لیمو", "لیمو ترش", "لیمو شیرین", "لیچی", "لبو",
        # م
        "موز", "ملون", "مویز", "مدنی",
        # ن
        "نارنگی", "نارگیل", "نارنج", "نبق",
        # ه
        "هندوانه", "هلو", "هلو انجیری",
    ],
    "کشور": [
        # الف / آ
        "ایران", "آلمان", "آمریکا", "اسپانیا", "ایتالیا", "اتریش", "امارات",
        "افغانستان", "ارمنستان", "آذربایجان", "استرالیا", "آرژانتین", "الجزایر",
        "اندونزی", "اکوادور", "اتیوپی", "اردن", "اروگوئه", "ازبکستان", "اوکراین",
        "ایرلند", "ایسلند", "انگلیس", "انگلستان", "اسلوونی", "اسلواکی", "آنگولا",
        "آلبانی",
        # ب
        "برزیل", "بلژیک", "بلغارستان", "بحرین", "بنگلادش", "بلاروس", "بولیوی",
        "بوسنی", "بوتسوانا", "برونئی",
        # پ
        "پرتغال", "پاکستان", "پرو", "پاناما", "پاراگوئه",
        # ت
        "ترکیه", "تایلند", "تونس", "تاجیکستان", "ترکمنستان", "تایوان", "تانزانیا",
        "توگو",
        # ج
        "جامائیکا", "جیبوتی", "جمهوری چک", "جمهوری آذربایجان",
        # چ
        "چین", "چک", "چاد",
        # د
        "دانمارک", "دومینیکن", "دومینیکا",
        # ر
        "روسیه", "رومانی", "رواندا",
        # ز
        "زامبیا", "زیمبابوه", "زلاندنو",
        # ژ
        "ژاپن",
        # س
        "سوئد", "سوئیس", "سوریه", "سنگاپور", "سنگال", "سومالی", "سودان",
        "سریلانکا", "ساحل عاج", "سیرالئون",
        # ش
        "شیلی", "شام",
        # ص
        "صربستان", "صحرای غربی",
        # ع
        "عراق", "عمان", "عربستان",
        # غ
        "غنا", "گینه",
        # ف
        "فرانسه", "فنلاند", "فیلیپین", "فلسطین", "فیجی",
        # ق
        "قطر", "قزاقستان", "قرقیزستان", "قبرس",
        # ک
        "کانادا", "کره جنوبی", "کره شمالی", "کلمبیا", "کوبا", "کرواسی", "کویت",
        "کنیا", "کامرون", "کاستاریکا", "کامبوج", "کنگو",
        # گ
        "گرجستان", "گابن", "گواتمالا", "گینه", "گویان",
        # ل
        "لبنان", "لهستان", "لیبی", "لیتوانی", "لتونی", "لوکزامبورگ", "لیبریا",
        # م
        "مصر", "مکزیک", "مالزی", "مراکش", "مجارستان", "مغولستان", "ماداگاسکار",
        "مالی", "مالت", "مالدیو", "موریتانی", "موزامبیک",
        # ن
        "نروژ", "نیجریه", "نپال", "نیوزیلند", "نیکاراگوئه", "نیجر", "نامیبیا",
        # و
        "ونزوئلا", "ویتنام", "واتیکان",
        # ه
        "هند", "هندوستان", "هلند", "هندوراس", "هائیتی",
        # ی
        "یونان", "یمن",
    ],
    "شهر": [
        # الف / آ
        "اصفهان", "اهواز", "اراک", "ارومیه", "اردبیل", "آبادان", "آمل", "ایلام",
        "اسلامشهر", "ابهر", "اقلید", "اندیمشک", "اسدآباد", "الیگودرز", "استانبول",
        "آنکارا", "آتن", "آمستردام", "اسلو",
        # ب
        "بوشهر", "بجنورد", "بم", "بابل", "بابلسر", "بندرعباس", "بندر انزلی",
        "بیرجند", "بروجرد", "بانه", "بهبهان", "برلین", "بغداد", "بیروت", "بروکسل",
        # پ
        "پاریس", "پکن", "پاوه", "پیرانشهر", "پاکدشت", "پراگ",
        # ت
        "تهران", "تبریز", "توکیو", "تنکابن", "تربت حیدریه", "تفرش", "تاکستان",
        "تورنتو", "تفلیس",
        # ج
        "جهرم", "جلفا", "جیرفت", "جوانرود", "جده",
        # چ
        "چابهار", "چالوس", "چادگان", "چناران",
        # خ
        "خرمشهر", "خرم آباد", "خوی", "خلخال", "خمینی شهر", "خوانسار", "خواف",
        # د
        "دزفول", "دامغان", "دماوند", "درود", "دورود", "دهدشت", "دبی", "دمشق",
        "دوحه", "دوبلین", "دهلی",
        # ر
        "رشت", "رامسر", "رفسنجان", "ری", "رامهرمز", "رودسر", "رودبار", "رم", "ریاض",
        # ز
        "زاهدان", "زنجان", "زابل", "زرند", "زرقان", "زوریخ", "زاگرب",
        # س
        "ساری", "سمنان", "سنندج", "سبزوار", "ساوه", "سیرجان", "سمیرم", "سقز",
        "سلماس", "سئول", "سیدنی", "سمرقند",
        # ش
        "شیراز", "شهرکرد", "شاهرود", "شوشتر", "شوش", "شهریار", "شیروان", "شانگهای",
        # ص
        "صحنه", "صومعه سرا", "صفاشهر", "صوفیه",
        # ط
        "طبس", "طالقان", "طوس", "طرابلس",
        # ع
        "عسلویه", "عباس آباد", "عدن", "عمان",
        # غ
        "غزه", "غرناطه",
        # ف
        "فردوس", "فسا", "فومن", "فیروزآباد", "فیروزکوه", "فلاورجان", "فرانکفورت",
        # ق
        "قم", "قزوین", "قوچان", "قشم", "قائمشهر", "قروه", "قاهره", "قونیه",
        # ک
        "کرمان", "کرج", "کاشان", "کرمانشاه", "کازرون", "کیش", "لاهیجان", "کابل",
        "کربلا", "کوالالامپور",
        # گ
        "گرگان", "گنبد کاووس", "گچساران", "گلپایگان", "گناوه", "گیلانغرب",
        # ل
        "لاهیجان", "لار", "لنگرود", "لردگان", "لندن", "لیسبون", "لس آنجلس",
        # م
        "مشهد", "مراغه", "ملایر", "مرند", "مریوان", "محلات", "مسجدسلیمان", "مهاباد",
        "مادرید", "مسکو", "مونیخ", "میلان", "مکه", "مدینه",
        # ن
        "نیشابور", "نهاوند", "نوشهر", "نطنز", "نجف آباد", "نور", "نجف", "نیویورک",
        # و
        "ورامین", "ونیز", "وین", "ورشو", "واشنگتن",
        # ه
        "همدان", "هرات", "هشتگرد", "هندیجان", "هامبورگ", "هلسینکی",
        # ی
        "یزد", "یاسوج", "ایروان",
    ],
    "حیوان": [
        # الف / آ
        "اسب", "اردک", "الاغ", "آهو", "اختاپوس", "افعی", "استر", "ایگوانا",
        # ب
        "ببر", "بز", "بوفالو", "بلبل", "بوقلمون", "بزغاله", "بزمجه",
        # پ
        "پلنگ", "پروانه", "پنگوئن", "پاندا", "پشه", "پوما",
        # ت
        "تمساح", "توله سگ", "تورنگ", "تیهو",
        # ج
        "جغد", "جوجه تیغی", "جیرجیرک", "جبیر",
        # چ
        "چیتا", "چکاوک", "چلچله",
        # خ
        "خرس", "خرگوش", "خروس", "خفاش", "خرچنگ", "خوک", "خارپشت",
        # د
        "دلفین", "دایناسور", "دارکوب", "دم جنبانک",
        # ر
        "روباه", "رتیل", "سمور", "راکون",
        # ز
        "زرافه", "زنبور", "زالو", "زاغ", "زنجره",
        # ژ
        "ژرمن شپرد", "ژیان",
        # س
        "سگ", "سنجاب", "سمور", "سوسمار", "سوسک", "ستاره دریایی", "سار",
        # ش
        "شیر", "شتر", "شترمرغ", "شغال", "اهو", "شاهین", "شب پره",
        # ص
        "صدف", "صعوه",
        # ط
        "طوطی", "طاووس", "طرلان",
        # ع
        "عقاب", "عنکبوت", "عقرب", "عروس دریایی",
        # غ
        "غاز", "غزال", "غوک",
        # ف
        "فیل", "فلامینگو", "فک", "فنچ",
        # ق
        "قناری", "قورباغه", "قو", "قوچ", "قرقاول", "قمری",
        # ک
        "کبوتر", "کلاغ", "کوسه", "کرگدن", "کانگورو", "کفتار", "کرم", "کبک",
        "کوآلا", "کندور",
        # گ
        "گرگ", "گاو", "گوسفند", "گربه", "گوزن", "گورخر", "گراز", "گنجشک", "گوریل",
        # ل
        "لاک پشت", "لک لک", "لاما", "لور",
        # م
        "مار", "موش", "میمون", "ماهی", "مرغ", "مورچه", "مگس", "ملخ", "مارمولک",
        "مرغ دریایی",
        # ن
        "نهنگ", "نهنگ قاتل", "نسناس",
        # و
        "وروره", "وال", "ومبت",
        # ه
        "همستر", "هدهد", "هزارپا", "هشت پا",
        # ی
        "یوزپلنگ", "یاکریم", "یابو",
    ],
    "غذا": [
        # الف / آ
        "آبگوشت", "آش رشته", "املت", "الویه", "آلبالو پلو", "اشکنه", "آش دوغ",
        "آش شله قلمکار", "استیک", "اسپاگتی",
        # ب
        "باقالی پلو", "بریانی", "برگر", "بادمجان شکم پر", "بورانی", "باقلا قاتق",
        "بیف استراگانف",
        # پ
        "پیتزا", "پاستا", "پلو", "پلو مرغ", "پتاژ", "پوره سیب زمینی",
        # ت
        "ته چین", "تاس کباب", "ترشی تره", "تاکو", "تن ماهی",
        # ج
        "جوجه کباب", "جگر", "جغور بغور",
        # چ
        "چلوکباب", "چلو گوشت", "چلو مرغ", "چیکن استراگانف", "چیزبرگر",
        # ح
        "حلیم", "حلیم بادمجان", "حمص",
        # خ
        "خورش قیمه", "خورش کرفس", "خورش بامیه", "خورش بادمجان", "خوراک لوبیا",
        "خاگینه", "خوراک مرغ",
        # د
        "دلمه", "دیزی", "دمپختک", "دنده کباب", "دونر کباب",
        # ر
        "رشته پلو", "راتاتویی", "راویولی", "رولت گوشت",
        # ز
        "زرشک پلو", "زبان", "زرشک پلو با مرغ", "زیتون پرورده",
        # ژ
        "ژیگو", "ژامبون",
        # س
        "ساندویچ", "سوپ", "سوشی", "سمبوسه", "سبزی پلو", "سلطانی", "سیب زمینی سرخ کرده",
        # ش
        "شله زرد", "شویدپلو", "شامی", "شیشلیک", "شنیتسل", "شاورما", "شیربرنج",
        # ط
        "طاس کباب", "طباخی",
        # ع
        "عدس پلو", "عدسی", "عسل و سرشیر",
        # ف
        "فسنجان", "فلافل", "فرنی", "فاهیتا", "فیله مرغ",
        # ق
        "قورمه سبزی", "قیمه", "قیمه نثار", "قلیه ماهی", "قلیه میگو", "قاورمه",
        # ک
        "کباب", "کباب کوبیده", "کباب برگ", "کوکو سبزی", "کوکو سیب زمینی", "کتلت",
        "کله پاچه", "کلم پلو", "کشک بادمجان", "کوفته تبریزی",
        # گ
        "گردن", "گوجه بادمجان", "گراتن", "گولاش",
        # ل
        "لازانیا", "لوبیا پلو", "لبو", "لقمه کباب",
        # م
        "ماکارونی", "میرزاقاسمی", "مرصع پلو", "ماهی شکم پر", "مسما بادمجان",
        "مرغ ترش", "مرغ سوخاری",
        # ن
        "نرگسی", "ناگت", "نودل", "نان و پنیر",
        # ه
        "همبرگر", "هویج پلو", "هات داگ", "حلیم",
        # ی
        "یتیمچه", "یخنی",
    ],
    "اسم": [
        # الف / آ
        "امیر", "احمد", "احسان", "آرمان", "آرش", "آرتین", "اشکان", "امین",
        "اکبر", "الهام", "الناز", "آزاده", "آرزو", "آناهیتا", "ایمان",
        # ب
        "بهرام", "بهزاد", "بهنام", "بابک", "بهمن", "بهاره", "بیتا", "بنفشه",
        # پ
        "پارسا", "پرهام", "پژمان", "پویا", "پدرام", "پروانه", "پریسا", "پریناز",
        # ت
        "تینا", "ترانه", "تهمینه", "توحید", "تورج", "تمنا",
        # ج
        "جواد", "جلال", "جمشید", "جعفر", "جهانگیر",
        # چ
        "چکامه", "چنگیز",
        # ح
        "حسن", "حسین", "حمید", "حامد", "حدیث", "حنا", "حسام",
        # خ
        "خسرو", "خشایار", "خلیل", "خاطره", "خورشید",
        # د
        "دانیال", "داریوش", "داوود", "دنیا", "دلارام", "دریا",
        # ر
        "رضا", "رامین", "رحیم", "رسول", "رویا", "ریحانه", "رزیتا", "رستم",
        # ز
        "زهرا", "زینب", "زهره", "زلیخا", "زانیار", "زرتشت",
        # ژ
        "ژاله", "ژینا", "ژوبین",
        # س
        "سعید", "سامان", "سروش", "سینا", "سهراب", "سارا", "سمیرا", "سحر",
        "ستاره", "سپیده", "سجاد", "سیاوش",
        # ش
        "شهاب", "شروین", "شاهین", "شایان", "شیما", "شیرین", "شقایق", "شهرزاد",
        # ص
        "صادق", "صابر", "صبا", "صدف", "صوفیا",
        # ط
        "طاها", "طاهر", "طناز", "طاهره",
        # ع
        "علی", "عباس", "عرفان", "عماد", "عسل", "عاطفه", "عطیه", "علیرضا",
        # غ
        "غزل", "غزاله", "غلامرضا",
        # ف
        "فرهاد", "فرزاد", "فرشید", "فربد", "فاطمه", "فرزانه", "فرشته", "فریبا",
        # ق
        "قاسم", "قدرت", "قباد",
        # ک
        "کامران", "کاوه", "کیوان", "کیان", "کیمیا", "کتایون", "کامیار",
        # گ
        "گلاب", "گلنار", "گلاره", "گیتا", "گیسو", "گرشاسب",
        # ل
        "لیلا", "لادن", "لعیا", "لقمان",
        # م
        "محمد", "مهدی", "محسن", "مسعود", "مجید", "میلاد", "مریم", "مهسا",
        "مینا", "میترا", "مرجان", "فاطمه", "منصور",
        # ن
        "نادر", "ناصر", "نیما", "نوید", "نگار", "نازنین", "نرگس", "نسترن", "نیلوفر",
        # و
        "وحید", "ویدا", "ونوس", "والا",
        # ه
        "هادی", "هومن", "همایون", "هدی", "هانیه", "هلیا", "نگام", "هستی",
        # ی
        "یاسر", "یاسین", "یوسف", "یلدا", "یاسمن", "یگانه",
    ],
    "شیء": [
        # الف / آ
        "آینه", "اتو", "آچار", "استکان", "ابزار", "ارگ", "اره", "اجاق گاز",
        # ب
        "بشقاب", "بالش", "باتری", "بطری", "بیل", "برس", "بادبزن",
        # پ
        "پتو", "پنکه", "پرده", "پریز", "پوتین", "پیچ گوشتی", "پاک کن",
        # ت
        "تلویزیون", "تلفن", "تبلت", "تبر", "تخت", "ترازو", "تسبیح",
        # ج
        "جارو", "جاروبرقی", "جالباسی", "جعبه", "جاکلیدی",
        # چ
        "چاقو", "چراغ", "چتر", "چمدان", "چنگال", "چکش", "چرخ خیاطی",
        # خ
        "خودکار", "خط کش", "خلال دندان", "خمیر دندان",
        # د
        "دفتر", "دوربین", "دمپایی", "دیگ", "دریل", "دستکش", "دکمه",
        # ر
        "رادیو", "رنده", "روفرشی", "رومیزی", "ربات",
        # ز
        "زنجیر", "زنگوله", "زیرسیگاری", "زیرانداز", "زره",
        # س
        "ساعت", "سماور", "سشوار", "سبد", "سوزن", "سینی", "سفره", "سوهان",
        # ش
        "شانه", "شمع", "شیشه", "شلنگ", "شمشیر", "شارژر",
        # ص
        "صندلی", "صابون", "صندوق", "صافی",
        # ظ
        "ظرف", "ظرفشویی",
        # ع
        "عینک", "عصا", "عطر",
        # ف
        "فرش", "فنجان", "فندک", "فلاسک", "فرغون",
        # ق
        "قاشق", "قوری", "قابلمه", "قیچی", "قفل", "قلم", "قالی",
        # ک
        "کتاب", "کیف", "کفش", "لباس", "کامپیوتر", "کلید", "کمد", "کتری", "کاسه",
        "کلاه", "کولر",
        # گ
        "گلدان", "گوشی", "گیتار", "گاوصندوق", "گیره", "فرش",
        # ل
        "لیوان", "لپ تاپ", "لامپ", "لباسشویی", "لوستر", "لوله",
        # م
        "مداد", "میز", "موبایل", "مبل", "ملاقه", "مسواک", "میخ", "ماشین حساب",
        # ن
        "نردبان", "نخ", "نعلبکی", "نیمکت", "ناخن گیر",
        # و
        "واکس", "ولوم", "ویولن",
        # ه
        "هاون", "هدفون", "هارد", "هواکش", "هیتر",
        # ی
        "یخچال", "یقه",
    ],
}

_NAME_FAMILY_FILE = Path(__file__).resolve().parent.parent / "data" / "name_family_words.json"
_NF_CATEGORY_MAP = {
    "نام": "اسم",
    "شهر": "شهر",
    "میوه": "میوه",
    "وسیله": "شیء",
    "حیوان": "حیوان",
}

# ساختارهای نرمال‌شدهٔ داخلی برای جستجوی O(1)
_NORMALIZED_BANK: dict[str, set[str]] = {cat: set() for cat in CATEGORY_PROMPTS}
_COMPACT_BANK: dict[str, set[str]] = {cat: set() for cat in CATEGORY_PROMPTS}
_PAIR_WORDS: dict[tuple[str, str], set[str]] = {}
PLAYABLE_PAIRS: list[tuple[str, str]] = []


def _rebuild_playable_pairs() -> None:
    global PLAYABLE_PAIRS
    pairs = []
    for (cat, letter), words in sorted(_PAIR_WORDS.items()):
        if cat in CATEGORY_PROMPTS and len(words) >= MIN_WORDS_PER_PAIR:
            pairs.append((cat, letter))
    PLAYABLE_PAIRS = pairs


def register_words(category: str, words: list[str] | tuple[str, ...] | set[str]) -> int:
    """افزودن واژگان معتبر به یک موضوع (قابل توسعه در زمان اجرا و تست)."""
    if category not in CATEGORY_PROMPTS:
        raise ValueError(f"Unknown category: {category!r}")
    added = 0
    norm_set = _NORMALIZED_BANK.setdefault(category, set())
    compact_set = _COMPACT_BANK.setdefault(category, set())
    for raw in words:
        norm = normalize_word(raw)
        if not norm or len(norm) < 2:
            continue
        if not _VALID_PERSIAN_RE.match(norm):
            continue
        first_letter = norm[0]
        if norm not in norm_set:
            norm_set.add(norm)
            added += 1
        compact = _compact_word(norm)
        if compact:
            compact_set.add(compact)
        _PAIR_WORDS.setdefault((category, first_letter), set()).add(norm)
    _rebuild_playable_pairs()
    return added


def _init_word_bank() -> None:
    for cat, words in BASE_WORD_BANK.items():
        register_words(cat, words)
    # غنی‌سازی از دیتابیس نسخهٔ اسم‌فامیل در صورت وجود
    try:
        if _NAME_FAMILY_FILE.exists():
            nf_data = json.loads(_NAME_FAMILY_FILE.read_text(encoding="utf-8"))
            if isinstance(nf_data, dict):
                for nf_cat, target_cat in _NF_CATEGORY_MAP.items():
                    items = nf_data.get(nf_cat)
                    if isinstance(items, list):
                        register_words(target_cat, items)
    except Exception:
        pass


_init_word_bank()


def get_valid_words(category: str, letter: str | None = None) -> frozenset[str]:
    """برگرداندن مجموعهٔ واژگان نرمال‌شدهٔ معتبر برای موضوع (و حرف اختیاری)."""
    if category not in _NORMALIZED_BANK:
        return frozenset()
    if letter is None:
        return frozenset(_NORMALIZED_BANK[category])
    norm_l = normalize_letter(letter)
    if not norm_l:
        return frozenset()
    return frozenset(_PAIR_WORDS.get((category, norm_l), set()))


def validate_answer(
    category: str,
    letter: str,
    answer_text: str,
    *,
    used_answers: set[str] | frozenset[str] | None = None,
) -> tuple[bool, str]:
    """اعتبارسنجی دقیق پاسخ برای موضوع و حرف تعیین‌شده.

    خروجی: ``(is_valid, normalized_answer)``
    """
    if category not in _NORMALIZED_BANK:
        return False, ""
    raw = str(answer_text or "").strip()
    if not raw or "\n" in raw or len(raw) > 40:
        return False, ""
    if raw.startswith(("/", "!", ".")):
        return False, ""
    if is_speed_bomb_command(raw):
        return False, ""

    norm = normalize_word(raw)
    if not norm or len(norm) < 2:
        return False, ""
    # جمله‌های معمولی گفتگو (بیش از ۳ کلمه) یا متن غیرفارسی پذیرفته نمی‌شوند
    if len(norm.split()) > 3 or not _VALID_PERSIAN_RE.match(norm):
        return False, ""

    target_letter = normalize_letter(letter)
    if not target_letter or norm[0] != target_letter:
        return False, ""

    compact = _compact_word(norm)
    in_bank = (
        norm in _NORMALIZED_BANK.get(category, set())
        or (compact and compact in _COMPACT_BANK.get(category, set()))
    )
    if not in_bank:
        return False, ""

    if used_answers and (norm in used_answers or compact in used_answers):
        return False, ""

    return True, norm


# ---------------------------------------------------------------------------
# قالب‌های پیام
# ---------------------------------------------------------------------------
def format_category_prompt(category: str, letter: str) -> str:
    emoji, template = CATEGORY_PROMPTS.get(
        category, ("🎯", f"یک {category} با حرف «{{letter}}» بگو!")
    )
    return f"{emoji} {template.format(letter=letter)}"


def format_stage_message(stage_state: dict) -> str:
    stage_num = int(stage_state.get("stage", 1))
    stage_title = STAGE_ORDINALS.get(stage_num, f"دور {to_persian_digits(stage_num)}")
    category = stage_state["category"]
    letter = stage_state["letter"]
    prompt_line = format_category_prompt(category, letter)
    return (
        "🦊 روباه | بمب سرعت\n\n"
        f"💣 {stage_title} شروع شد!\n\n"
        f"{prompt_line}\n\n"
        f"⏳ فرصت: {to_persian_digits(STAGE_SECONDS)} ثانیه\n\n"
        "هرکس زودتر جواب صحیح بده، برنده است!"
    )


def format_winner_message(winner_name: str) -> str:
    clean_name = str(winner_name or "").strip() or "کاربر"
    return (
        f"🦊 روباه: {clean_name} برنده شد! 🎉\n\n"
        "چون جواب صحیحش زودتر از بقیه ثبت شد"
    )


def format_final_summary(summary: dict) -> str:
    totals = summary.get("totals") or []
    if not totals:
        return (
            "🏁 بازی «بمب سرعت روباه» تمام شد!\n\n"
            "در این ۳ مرحله هیچ پاسخ صحیحی ثبت نشد و سکه‌ای پرداخت نگردید."
        )
    lines = [
        "🏁 بازی «بمب سرعت روباه» تمام شد!\n",
        "🏆 مجموع برندگان و سکه‌های کسب‌شده:",
    ]
    for idx, row in enumerate(totals, 1):
        name = row["name"]
        coins = int(row["coins"])
        wins = int(row["wins"])
        lines.append(
            f"{to_persian_digits(idx)}. {name} — "
            f"{to_persian_digits(coins)} سکه برنز 🥉 ({to_persian_digits(wins)} برد)"
        )
    return "\n".join(lines)


def resolve_player_name(sender=None, fallback_name: str | None = None, user_id=None) -> str:
    """استخراج نام نمایشی یا یوزرنیم کاربر برای درج در پیام برنده."""
    if sender is not None:
        first = str(getattr(sender, "first_name", "") or "").strip()
        last = str(getattr(sender, "last_name", "") or "").strip()
        full = " ".join(p for p in (first, last) if p).strip()
        if full:
            return full
        username = str(getattr(sender, "username", "") or "").strip().lstrip("@")
        if username:
            return f"@{username}"
    if fallback_name and str(fallback_name).strip():
        return str(fallback_name).strip()
    if user_id is not None:
        return f"کاربر {user_id}"
    return "کاربر"


# ---------------------------------------------------------------------------
# مدیریت وضعیت ۳ مرحله‌ای به تفکیک گروه
# ---------------------------------------------------------------------------
def _chat_key(chat_id) -> str:
    return str(normalize_group_id(chat_id))


def _next_session_id() -> int:
    global _FALLBACK_SEQ
    try:
        return int(economy_rewards.round_id())
    except Exception:
        _FALLBACK_SEQ += 1
        return int(time.time() * 1000) + _FALLBACK_SEQ


def _pick_pair(used_pairs: set[tuple[str, str]]) -> tuple[str, str]:
    available = [pair for pair in PLAYABLE_PAIRS if pair not in used_pairs]
    if not available:
        available = list(PLAYABLE_PAIRS)
    if not available:
        return ("میوه", "ز")
    return _RANDOM.choice(available)


def _make_stage(
    chat_key: str,
    session_id: int,
    stage_num: int,
    used_pairs: set[tuple[str, str]],
    *,
    category: str | None = None,
    letter: str | None = None,
    stage_seconds: float | None = None,
) -> dict:
    if category and letter:
        chosen_cat = category
        chosen_letter = normalize_letter(letter) or letter
    else:
        chosen_cat, chosen_letter = _pick_pair(used_pairs)
    used_pairs.add((chosen_cat, chosen_letter))
    duration = float(STAGE_SECONDS if stage_seconds is None else stage_seconds)
    now_mono = time.monotonic()
    now_wall = time.time()
    return {
        "stage": int(stage_num),
        "token": f"{session_id}:{stage_num}",
        "category": chosen_cat,
        "letter": chosen_letter,
        "started_at": now_mono,
        "started_at_wall": now_wall,
        "duration": duration,
        "deadline": now_mono + duration,
        "resolved": False,
        "winner_id": None,
        "winner_name": None,
        "winning_answer": None,
        "reward_paid": False,
        "reward_amount": 0,
        "reward_reference": f"speed_bomb:{chat_key}:{session_id}:stage:{stage_num}",
    }


def is_active(chat_id) -> bool:
    return _chat_key(chat_id) in _ACTIVE


def current_stage(chat_id) -> dict | None:
    session = _ACTIVE.get(_chat_key(chat_id))
    if not session:
        return None
    stage = session.get("current_stage")
    return dict(stage) if isinstance(stage, dict) else None


def get_session(chat_id) -> dict | None:
    session = _ACTIVE.get(_chat_key(chat_id))
    if not session:
        return None
    snap = dict(session)
    if isinstance(session.get("current_stage"), dict):
        snap["current_stage"] = dict(session["current_stage"])
    snap["stages_history"] = [dict(s) for s in session.get("stages_history", [])]
    return snap


def _cancel_stage_task(chat_id) -> None:
    key = _chat_key(chat_id)
    task = _STAGE_TASKS.pop(key, None)
    if task is not None and not task.done():
        current = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()


def start_game(
    chat_id,
    *,
    stage_pairs: list[tuple[str, str]] | None = None,
    stage_seconds: float | None = None,
) -> dict | None:
    """شروع یک بازی ۳ مرحله‌ای جدید در گروه.

    اگر بازی همین حالا در این گروه فعال باشد، ``None`` برمی‌گرداند و بازی
    جدیدی شروع نمی‌کند.
    """
    key = _chat_key(chat_id)
    if key in _ACTIVE:
        return None

    session_id = _next_session_id()
    used_pairs: set[tuple[str, str]] = set()
    planned_pairs = list(stage_pairs) if stage_pairs else []
    first_cat, first_letter = planned_pairs[0] if len(planned_pairs) >= 1 else (None, None)

    stage1 = _make_stage(
        key,
        session_id,
        1,
        used_pairs,
        category=first_cat,
        letter=first_letter,
        stage_seconds=stage_seconds,
    )
    session = {
        "chat_id": chat_id,
        "chat_key": key,
        "session_id": session_id,
        "stage_seconds": float(STAGE_SECONDS if stage_seconds is None else stage_seconds),
        "planned_pairs": planned_pairs,
        "used_pairs": used_pairs,
        "used_answers": set(),
        "processed_message_ids": set(),
        "seen_stage_attempts": set(),
        "current_stage": stage1,
        "stages_history": [],
        "scores": {},  # user_key -> {"user_id": ..., "name": ..., "wins": int, "coins": int}
    }
    _ACTIVE[key] = session
    return dict(stage1)


def refresh_stage_clock(chat_id, stage_token: str | None = None) -> None:
    """به‌روزرسانی لحظهٔ شروع مرحله بلافاصله پس از ارسال پیام مرحله."""
    session = _ACTIVE.get(_chat_key(chat_id))
    if not session:
        return
    stage = session.get("current_stage")
    if not stage or stage.get("resolved"):
        return
    if stage_token is not None and stage.get("token") != stage_token:
        return
    now_mono = time.monotonic()
    now_wall = time.time()
    stage["started_at"] = now_mono
    stage["started_at_wall"] = now_wall
    stage["deadline"] = now_mono + float(stage.get("duration") or STAGE_SECONDS)


def _build_summary(session: dict) -> dict:
    totals = list(session.get("scores", {}).values())
    totals.sort(key=lambda r: (-int(r["coins"]), int(r.get("first_stage", 99))))
    return {
        "session_id": session["session_id"],
        "chat_id": session["chat_id"],
        "stages": [dict(s) for s in session.get("stages_history", [])],
        "totals": [dict(r) for r in totals],
    }


def _advance_or_finish(session: dict) -> tuple[dict | None, bool, dict | None]:
    """حرکت به مرحلهٔ بعدی (۲ یا ۳) یا خاتمهٔ کامل بازی پس از مرحلهٔ ۳."""
    current = session["current_stage"]
    stage_num = int(current["stage"])
    if stage_num >= TOTAL_STAGES:
        key = session["chat_key"]
        _ACTIVE.pop(key, None)
        summary = _build_summary(session)
        return None, True, summary

    next_num = stage_num + 1
    planned = session.get("planned_pairs") or []
    next_cat, next_letter = (
        planned[next_num - 1] if len(planned) >= next_num else (None, None)
    )
    next_stage = _make_stage(
        session["chat_key"],
        session["session_id"],
        next_num,
        session["used_pairs"],
        category=next_cat,
        letter=next_letter,
        stage_seconds=session["stage_seconds"],
    )
    session["current_stage"] = next_stage
    session["seen_stage_attempts"] = set()
    return dict(next_stage), False, None


def _pay_stage_reward_once(chat_id, user_id, winner_name: str, stage: dict, bot=None) -> int:
    """پرداخت دقیقاً ۳ سکه برنز از طریق سیستم اقتصاد فعلی، حداکثر یک‌بار در هر مرحله."""
    if stage.get("reward_paid"):
        return 0
    stage["reward_paid"] = True
    ref = stage["reward_reference"]
    override = getattr(bot, "award_coins", None) if bot is not None else None
    try:
        if callable(override):
            override(chat_id, user_id, winner_name, REWARD_BRONZE)
        else:
            economy.award_game(
                chat_id,
                user_id,
                GAME,
                reference=ref,
                name=winner_name,
                amount=REWARD_BRONZE,
                win=True,
            )
        stage["reward_amount"] = REWARD_BRONZE
        return REWARD_BRONZE
    except Exception as exc:
        logger = getattr(bot, "logger", None) if bot is not None else None
        if logger is not None and hasattr(logger, "log_error"):
            logger.log_error(f"SPEED BOMB REWARD FAILED: {exc!r}")
        return 0


def _is_message_older_than_stage(
    stage: dict,
    *,
    received_at: float | None = None,
    message_date=None,
) -> bool:
    if received_at is not None:
        try:
            ts = float(received_at)
            # تشخیص خودکار زمان monotonic در برابر epoch
            if ts > 1_000_000_000:
                if ts < float(stage["started_at_wall"]):
                    return True
            else:
                if ts < float(stage["started_at"]):
                    return True
        except (TypeError, ValueError):
            pass

    if message_date is not None:
        try:
            if isinstance(message_date, datetime):
                msg_ts = message_date.timestamp()
            else:
                msg_ts = float(message_date)
            if msg_ts < float(stage["started_at_wall"]):
                return True
        except (TypeError, ValueError, OSError):
            pass

    return False


def submit_answer(
    chat_id,
    user_id,
    text: str,
    *,
    sender=None,
    name: str | None = None,
    message_id=None,
    received_at: float | None = None,
    message_date=None,
    bot=None,
) -> dict | None:
    """بررسی پیام کاربر در مرحلهٔ فعال.

    اگر پاسخ معتبر و در مهلت ۱۵ ثانیه باشد:
    - مرحله فوراً بسته می‌شود (فقط اولین پاسخ معتبر برنده است)،
    - دقیقاً ۳ سکه برنز از طریق سیستم اقتصاد فعلی پرداخت می‌گردد،
    - مرحلهٔ بعدی (۲ یا ۳) آماده می‌شود یا در پایان مرحلهٔ ۳ بازی خاتمه می‌یابد.
    در غیر این صورت ``None`` برمی‌گرداند و هیچ سکه‌ای پرداخت نمی‌شود.
    """
    if user_id is None:
        return None
    key = _chat_key(chat_id)
    session = _ACTIVE.get(key)
    if not session:
        return None

    stage = session.get("current_stage")
    if not stage or stage.get("resolved"):
        return None

    # جلوگیری از پردازش مجدد یک پیام (replay / MessageEdited تکراری)
    if message_id is not None:
        if message_id in session["processed_message_ids"]:
            return None

    # بررسی انقضای مهلت ۱۵ ثانیه‌ای مرحله
    now_mono = time.monotonic()
    if received_at is not None and float(received_at) <= 1_000_000_000:
        check_mono = float(received_at)
    else:
        check_mono = now_mono
    if check_mono > float(stage["deadline"]):
        return None

    # پیام‌های قدیمی‌تر از زمان شروع مرحله پذیرفته نمی‌شوند
    if _is_message_older_than_stage(
        stage, received_at=received_at, message_date=message_date
    ):
        return None

    norm_attempt = normalize_word(text)
    if not norm_attempt:
        return None

    # جلوگیری از بررسی مکرر یک پیام تکراری در همان مرحله
    attempt_key = (str(user_id), norm_attempt)
    if attempt_key in session["seen_stage_attempts"]:
        return None
    session["seen_stage_attempts"].add(attempt_key)

    valid, normalized_answer = validate_answer(
        stage["category"],
        stage["letter"],
        text,
        used_answers=session["used_answers"],
    )
    if not valid:
        return None

    if message_id is not None:
        session["processed_message_ids"].add(message_id)

    # بستن اتمیک مرحله برای جلوگیری از برد یا پرداخت تکراری
    stage["resolved"] = True
    winner_name = resolve_player_name(sender=sender, fallback_name=name, user_id=user_id)
    stage["winner_id"] = user_id
    stage["winner_name"] = winner_name
    stage["winning_answer"] = normalized_answer
    session["used_answers"].add(normalized_answer)
    session["used_answers"].add(_compact_word(normalized_answer))

    _cancel_stage_task(chat_id)

    paid = _pay_stage_reward_once(chat_id, user_id, winner_name, stage, bot=bot)
    u_key = str(user_id)
    score_row = session["scores"].get(u_key)
    if score_row is None:
        score_row = {
            "user_id": user_id,
            "name": winner_name,
            "wins": 0,
            "coins": 0,
            "first_stage": int(stage["stage"]),
        }
        session["scores"][u_key] = score_row
    score_row["name"] = winner_name
    score_row["wins"] = int(score_row["wins"]) + 1
    score_row["coins"] = int(score_row["coins"]) + int(paid)

    won_snapshot = dict(stage)
    session["stages_history"].append(won_snapshot)

    next_stage, is_finished, summary = _advance_or_finish(session)
    return {
        "won_stage": won_snapshot,
        "winner_id": user_id,
        "winner_name": winner_name,
        "answer": normalized_answer,
        "reward": paid,
        "winner_message": format_winner_message(winner_name),
        "next_stage": next_stage,
        "next_stage_message": format_stage_message(next_stage) if next_stage else None,
        "finished": is_finished,
        "summary": summary,
        "summary_message": format_final_summary(summary) if summary else None,
    }


def timeout_stage(chat_id, stage_token: str | None = None) -> dict | None:
    """پایان مهلت ۱۵ ثانیه‌ای مرحله بدون برنده و انتقال به مرحلهٔ بعد یا پایان بازی."""
    key = _chat_key(chat_id)
    session = _ACTIVE.get(key)
    if not session:
        return None
    stage = session.get("current_stage")
    if not stage or stage.get("resolved"):
        return None
    if stage_token is not None and stage.get("token") != stage_token:
        return None

    stage["resolved"] = True
    timed_out_snapshot = dict(stage)
    session["stages_history"].append(timed_out_snapshot)

    next_stage, is_finished, summary = _advance_or_finish(session)
    return {
        "timed_out_stage": timed_out_snapshot,
        "timeout_message": NO_WINNER_STAGE_MESSAGE,
        "next_stage": next_stage,
        "next_stage_message": format_stage_message(next_stage) if next_stage else None,
        "finished": is_finished,
        "summary": summary,
        "summary_message": format_final_summary(summary) if summary else None,
    }


def cancel(chat_id) -> None:
    key = _chat_key(chat_id)
    _cancel_stage_task(chat_id)
    _ACTIVE.pop(key, None)


def reset_all(chat_id=None) -> None:
    if chat_id is None:
        for k in list(_STAGE_TASKS):
            task = _STAGE_TASKS.pop(k, None)
            if task is not None and not task.done():
                task.cancel()
        _ACTIVE.clear()
    else:
        cancel(chat_id)


# ---------------------------------------------------------------------------
# اتصال غیرمسدودکننده به رویدادهای گروه و تایمر ۱۵ ثانیه‌ای
# ---------------------------------------------------------------------------
async def _reply_bold(event, text: str):
    entities = bold_entities_for(text)
    if entities:
        try:
            return await event.reply(text, formatting_entities=entities)
        except TypeError:
            return await event.reply(text)
    return await event.reply(text)


def _schedule_stage_timer(bot, event, chat_id, stage_state: dict, track_timer=None) -> asyncio.Task:
    key = _chat_key(chat_id)
    _cancel_stage_task(chat_id)
    token = stage_state["token"]
    delay = float(stage_state.get("duration") or STAGE_SECONDS)

    async def _timer_runner():
        try:
            await asyncio.sleep(delay)
            res = timeout_stage(chat_id, token)
            if not res:
                return
            await _reply_bold(event, res["timeout_message"])
            if res["next_stage"] is not None:
                await _reply_bold(event, res["next_stage_message"])
                refresh_stage_clock(chat_id, res["next_stage"]["token"])
                _schedule_stage_timer(bot, event, chat_id, res["next_stage"], track_timer=track_timer)
            elif res["finished"] and res["summary_message"]:
                await _reply_bold(event, res["summary_message"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger = getattr(bot, "logger", None) if bot is not None else None
            if logger is not None and hasattr(logger, "log_error"):
                logger.log_error(f"SPEED BOMB TIMER ERROR chat_id={chat_id}: {exc!r}")

    task = asyncio.create_task(_timer_runner(), name=f"speed_bomb:{key}:{token}")
    _STAGE_TASKS[key] = task
    if callable(track_timer):
        try:
            track_timer(bot, chat_id, task)
        except Exception:
            pass
    return task


async def handle_command(
    bot,
    event,
    chat_id,
    *,
    chat_busy_check=None,
    busy_message: str | None = None,
    track_timer=None,
) -> bool:
    """شروع بازی با دستور «بمب سرعت» / «بمب ساعتی»."""
    if is_active(chat_id):
        await _reply_bold(event, ALREADY_ACTIVE_MESSAGE)
        return True
    if callable(chat_busy_check) and chat_busy_check(chat_id):
        await event.reply(
            busy_message or "⏳ یک بازی دیگر در این گروه در جریان است؛ لطفاً تا پایان آن صبر کنید."
        )
        return True

    stage1 = start_game(chat_id)
    if not stage1:
        await _reply_bold(event, ALREADY_ACTIVE_MESSAGE)
        return True

    msg = format_stage_message(stage1)
    await _reply_bold(event, msg)
    refresh_stage_clock(chat_id, stage1["token"])
    _schedule_stage_timer(bot, event, chat_id, stage1, track_timer=track_timer)
    return True


async def handle_answer(
    bot,
    event,
    chat_id,
    user_id,
    sender,
    text: str,
    *,
    track_timer=None,
) -> bool:
    """بررسی پاسخ کاربر در مرحلهٔ فعال «بمب سرعت روباه»."""
    if not is_active(chat_id):
        return False
    if is_speed_bomb_command(text):
        await _reply_bold(event, ALREADY_ACTIVE_MESSAGE)
        return True

    msg_obj = getattr(event, "message", None)
    msg_id = getattr(msg_obj, "id", None) if msg_obj is not None else None
    msg_date = getattr(msg_obj, "date", None) if msg_obj is not None else None

    result = submit_answer(
        chat_id,
        user_id,
        text,
        sender=sender,
        message_id=msg_id,
        message_date=msg_date,
        bot=bot,
    )
    if result is None:
        return False

    await _reply_bold(event, result["winner_message"])
    if result["next_stage"] is not None:
        await _reply_bold(event, result["next_stage_message"])
        refresh_stage_clock(chat_id, result["next_stage"]["token"])
        _schedule_stage_timer(bot, event, chat_id, result["next_stage"], track_timer=track_timer)
    elif result["finished"] and result["summary_message"]:
        await _reply_bold(event, result["summary_message"])
    return True
