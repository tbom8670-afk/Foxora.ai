"""📥 قابلیت «دانلود عکس» — جدا و مستقل برای SPlusthon.

این ماژول با معماری فعلی ربات هماهنگ است:
  - از ``economy.get_balance`` / ``economy.spend`` برای سکه استفاده می‌کند.
  - با ``bot.client.send_file`` تصویر را مستقیم داخل چت می‌فرستد.
  - دستورِ «دانلود عکس» یک جریان تأییدی (confirm) دارد و فقط بعد از تأیید و
    آماده‌بودنِ تصاویر، هزینه کم می‌شود.

هزینه (ثابت و دقیق):
  - هر عکس = ۱۰ سکه برنز.
  - در هر درخواست حداکثر ۲ عکس ارسال می‌شود.
  - اگر ۲ عکس با موفقیت ارسال شود: ۲۰ برنز.
  - اگر فقط ۱ عکس با موفقیت ارسال شود: ۱۰ برنز.
  - اگر هیچ عکسی ارسال نشود: ۰ برنز.

رفتار:
  ۱) «دانلود عکس» → درخواستِ عبارت از کاربر + پیام تأیید هزینه.
  ۲) کاربر عبارت/تأیید یا لغو را می‌فرستد.
  ۳) قبل از تأیید هیچ سکه‌ای کم نمی‌شود.
  ۴) فقط بعد از تأیید: فیلترِ محتوای ممنوع → جستجو → دانلود تصاویر →
     ارسال در چت → سپس کسرِ سکه فقط بابت تصاویری که واقعاً با موفقیت
     دانلود و ارسال شده‌اند.
  ۵) قفلِ گروه + صفِ هر کاربر تا درخواست‌های هم‌زمان اجرا نشوند.

پایداری:
  - جستجو/دانلود داخل ``asyncio.to_thread`` تا Event Loop قفل نشود.
  - همهٔ عملیات شبکه timeout دارند و خطاها مدیریت می‌شوند.
  - هر خطا/لغو، وضعیتِ قفل و صف را آزاد می‌کند تا درخواست بعدی گیر نکند.
"""
import asyncio
import io
import logging
import re
import time
import traceback
from urllib.parse import quote, urlparse

import requests

# سشنِ مشترکِ HTTP برایِ reuseِ کانکشن (سرعت + جلوگیری ازِ نشتی)
_HTTP = requests.Session()

import economy

COST_PER_IMAGE = 10   # هزینهٔ دقیقِ هر عکس (برنز)
IMAGE_COUNT = 2       # حداکثر ۲ عکس در هر درخواست
SEARCH_CANDIDATES = 5  # تعدادِ نامزدِ جستجو (کمتر برای سرعت)
DL_CONCURRENCY = 3    # دانلودِ هم‌زمانِ لینک‌ها (موازی)
SEND_RETRIES = 2      # تلاش مجدد برای ارسالِ هر عکس در صورت خطای گذرا
NETWORK_TIMEOUT = (5, 8)
# حداکثرِ زمانِ کلِ یک لینک (دانلود + ارسال). اگر لینکی بیشتر از این طول کشید،
# رد می‌شود و سراغِ لینکِ بعدی می‌رویم تا کل عملیات مدت‌ها درگیر یک لینک نشود.
LINK_TIMEOUT = 8
# سقفِ زمانیِ کلِ عملیاتِ یک درخواست (جستجو + تلاش روی چند لینک + ارسال).
# بعد از این زمان، دیگر لینکِ جدیدی تست نمی‌شود تا عملیات چند دقیقه معطل نماند.
PROCESS_TIMEOUT = 25

# پسوندِ فایلِ تصویر — برای تشخیصِ آدرسِ مستقیمِ فایل
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|bmp)([?#;].*)?$", re.I)

# میزبان‌هایِ پایدارِ CDN که دانلودِ مستقیمِ تصویر می‌دهند (نه لینکِ صفحه).
# این منابع معمولاً فایلِ تصویر را مستقیم و بدونِ صفحه/HTML برمی‌گردانند.
_RELIABLE_HOSTS = (
    "pinimg.com",             # Pinterest — i.pinimg.com (مستقیم و پایدار)
    "unsplash.com",           # images.unsplash.com
    "pexels.com",             # images.pexels.com
    "wikimedia.org",          # upload.wikimedia.org (ویکی‌انبار)
    "flickr.com",             # live.staticflickr.com / farm*.staticflickr.com
    "pixabay.com",            # cdn.pixabay.com
    "istockphoto.com",        # media.istockphoto.com
    "gettyimages.com",        # media.gettyimages.com
    "googleusercontent.com",  # lh3.googleusercontent.com
    "gstatic.com",            # www.gstatic.com
    "ytimg.com",              # i.ytimg.com
    "wixstatic.com",          # static.wixstatic.com
    "amazonaws.com",          # *.s3.amazonaws.com
    "cloudfront.net",         # *.cloudfront.net
    "githubusercontent.com",
    "imgur.com",              # i.imgur.com
    "tenor.com",              # media.tenor.com (gif)
    "giphy.com",              # media.giphy.com
    "twimg.com",              # pbs.twimg.com
    "wp.com",
    "wordpress.com",
)

# زمان انقضای جریانِ تأیید (ثانیه). بعد از آن، کاربر باید دوباره شروع کند.
CONFIRM_TIMEOUT = 120

COMMAND = "دانلود عکس"

CONFIRM_TEXT = (
    "📥 دانلود تصویر\n\n"
    "برای هر عکس ۱۰ سکه برنز نیاز است.\n"
    "برای دریافت حداکثر ۲ تصویر، در صورت ارسال هر دو عکس ۲۰ سکه برنز از موجودی شما کم می‌شود.\n\n"
    "آیا تأیید می‌کنید؟\n\n"
    "بله / تایید / تأیید   → انجام\n"
    "خیر / لغو             → انصراف"
)
INSUFFICIENT = ("❌ موجودی شما کمتر از ۱۰ سکه برنز است؛ این درخواست انجام نمی‌شود.")
BLOCKED_CONTENT = "🚫 این درخواست ممنوع است و قابل انجام نیست."
CANCELLED = "❌ درخواست لغو شد؛ هیچ سکه‌ای کم نشد."
BUSY_GROUP = "⏳ یک دانلود عکس در همین گروه در حال پردازش است؛ لطفاً صبر کنید."
BUSY_USER = "⏳ شما درخواستِ دانلود عکسِ در حال پردازش دارید؛ لطفاً صبر کنید."
ASK_QUERY = "🖼️ چه تصویری می‌خواهید؟ عبارت موردنظر خود را بنویسید."
ERROR_MSG = "❌ مشکلی در جستجو/دانلود پیش آمد؛ دوباره تلاش کنید. هیچ سکه‌ای کم نشد."
NO_RESULTS = "❌ تصویر مرتبطی پیدا نشد، دوباره تلاش کنید. هیچ سکه‌ای کم نشد."

# ---------------------------------------------------------------------------
#  کلمات/عبارت‌های ممنوع — فارسی و انگلیسی
# ---------------------------------------------------------------------------
_BANNED_WORDS_FA = {
    "سکس", "سکسی", "پورن", "پورنو", "برهنه", "برهنه‌ها", "لخت", "لختی",
    "عریان", "عریانی", "جنسى", "جنسی", "رابطه جنسی", "مست", "مست‌کننده",
    "اسپرم", "کیر", "کص", "کون", "جنده", "فحش", "فحاشی", "دیوث", "هرزه",
    "هرزگی", "سکسی", "بیکینی", "بی‌کینی", "پستان", "سینه‌برهنه",
    "نود", "نودی", "عکس مست", "امراض جنسی", "مقاربت", "آمیزش",
    "اروتیک", "اروتیسم",
    # موضوعاتِ حساسِ سیاسی/تاریخیِ درخواستیِ مالک
    "پهلوی", "خاندان پهلوی", "رضا شاه", "رضاشاه", "محمدرضا پهلوی",
    "شاه پهلوی", "دودمان پهلوی",
}
_BANNED_WORDS_EN = {
    "sex", "porn", "porno", "nude", "naked", "nsfw", "erotic", "xxx",
    "fuck", "pussy", "dick", "cock", "hentai", "boobs", "anal", "blowjob",
    "bikini", "lingerie", "strip", "stripper", "escort", "orgy", "masturb",
    "thong", "nude photography", "18+", "adult content", "pornstar",
}
# برای تشخیصِ صریحِ «عبارت جنسی» حتی اگر دقیقاً کلمهٔ ممنوع نباشد.
_BANNED_PATTERNS = [
    r"برهنه", r"لخت", r"عکس\s*مست", r"سکس", r"پورن", r"جنسی",
    r"nude", r"naked", r"porn", r"sex", r"nsfw", r"erotic", r"xxx",
    r"پهلوی", r"رضا\s*شاه", r"رضاشاه", r"شاه\s*پهلوی",
]

# ---------------------------------------------------------------------------
#  وضعیت — به تفکیک (chat, user)
# ---------------------------------------------------------------------------
_SESSIONS = {}       # (chat_id, user_id) -> {"query": str, "ts": float}
_GROUP_LOCKS = {}    # chat_id -> asyncio.Lock
_BUSY_GROUPS = set()  # chat_id هایی که در حال پردازش‌اند
_BUSY_USERS = set()   # (chat_id, user_id) هایی که در حال پردازش‌اند
_PROCESSING = set()   # processing state set before the async task is created

# کشِ جستجو: normalized_query -> (timestamp, [urls]) برایِ پاسخِ سریع به
# عبارت‌هایِ پرتکرار. بعد از _CACHE_TTL ثانیه منقضی می‌شود.
_SEARCH_CACHE = {}
_CACHE_TTL = 3600  # ۱ ساعت
_CACHE_MAX = 500
_SESSION_MAX = 2000


def reset_all():
    """پاک‌سازی کامل — برای تست/ری‌استارت."""
    _SESSIONS.clear()
    _GROUP_LOCKS.clear()
    _BUSY_GROUPS.clear()
    _BUSY_USERS.clear()
    _PROCESSING.clear()
    _SEARCH_CACHE.clear()


def _group_lock(chat_id):
    lock = _GROUP_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _GROUP_LOCKS[chat_id] = lock
    return lock


def is_busy(chat_id, user_id):
    key = (chat_id, user_id)
    return chat_id in _BUSY_GROUPS or key in _BUSY_USERS or key in _PROCESSING


def begin_processing(chat_id, user_id):
    """Atomically claim the confirmed request before creating its task."""
    key = (chat_id, user_id)
    if key in _PROCESSING or key in _BUSY_USERS or chat_id in _BUSY_GROUPS:
        return False
    _PROCESSING.add(key)
    return True


def is_processing(chat_id, user_id):
    return (chat_id, user_id) in _PROCESSING


def clear_processing(chat_id, user_id):
    _PROCESSING.discard((chat_id, user_id))


# ---------------------------------------------------------------------------
#  فیلتر محتوای ممنوع
# ---------------------------------------------------------------------------
def _norm(value):
    text = (value or "").strip().lower()
    # یکسان‌سازی حروف فارسی
    for a, b in (("ي", "ی"), ("ك", "ک"), ("\u200c", " "), ("\u200f", "")):
        text = text.replace(a, b)
    return " ".join(text.split())


def is_blocked(query):
    """اگر درخواست، صریحاً برای محتوای جنسی/مستهجن باشد → True."""
    norm = _norm(query)
    tokens = set(norm.split())
    for w in _BANNED_WORDS_FA:
        if _norm(w) in norm:
            return True
    for w in _BANNED_WORDS_EN:
        if w in norm:
            return True
    # عبارت‌های ترکیبی
    for pat in _BANNED_PATTERNS:
        if re.search(pat, norm):
            return True
    # تشخیصِ «به شکل واضح برای پیدا کردن محتوای جنسی» حتی بدون کلمهٔ ممنوع
    if any(t in tokens for t in ("عکس", "تصویر", "تصاویر", "photo", "pic", "image")):
        if any(t in tokens for t in ("مست", "برهنه", "لخت", "جنسی", "سکس",
                                     "nude", "naked", "sex", "nsfw", "porn")):
            return True
    return False


# ---------------------------------------------------------------------------
#  جستجو و دانلود تصویر (خارج از Event Loop، با timeout)
# ---------------------------------------------------------------------------
# دامنه‌های اسپم/تبلیغاتی/کازینو/نامرتبط که نباید عکس از آن‌ها گرفته شود.
_SPAM_KEYWORDS = (
    "casino", "bet", "betting", "slot", "poker", "gambling", "bonus",
    "offer", "deal", "discount", "coupon", "promo", "advert", "click",
    "track", "redirect", "shortener", "bit.ly", "t.ly", "cutt.ly",
)
_SPAM_TLDS = (
    ".click", ".loan", ".cash", ".buzz", ".xyz", ".top", ".icu", ".gq",
    ".ml", ".ga", ".cf", ".tk", ".men", ".win", ".bid", ".trade", ".review",
    ".cam", ".rest", ".mom", ".lol",
)


def _is_spam_url(url):
    """آیا این URL یک دامنهٔ اسپم/تبلیغاتی/کازینو یا نامرتبط است؟"""
    if not url:
        return True
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return True
    # دامنهٔ ناقص/محلی
    if host.startswith(("localhost", "127.", "0.")):
        return True
    # TLD اسپم
    for tld in _SPAM_TLDS:
        if host.endswith(tld):
            return True
    # کلمهٔ اسپم در دامنه یا مسیر
    full = (host + " " + urlparse(url).path).lower()
    for kw in _SPAM_KEYWORDS:
        if kw in full:
            return True
    return False


# نقشهٔ سادهٔ نویسهٔ فارسی → لاتین برای جستجویِ بهتر (مثل «رونالدو» → ronaldo).
_FA_TO_LATIN = {
    "ا": "a", "آ": "a", "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j",
    "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z",
    "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z",
    "ع": "", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "گ": "g", "ل": "l",
    "م": "m", "ن": "n", "و": "v", "ه": "h", "ی": "y", "ئ": "e", "ء": "",
    "،": ",", "؟": "?", " ": " ",
}


def _transliterate_fa(text):
    """تبدیلِ سادهٔ متنِ فارسی به نویسهٔ لاتین (برای جستجو)."""
    out = []
    for ch in text:
        out.append(_FA_TO_LATIN.get(ch, ch))
    return "".join(out).strip()


def _is_direct_image_url(url):
    """آیا این URL یک آدرسِ مستقیمِ قابلِ دانلودِ تصویر است؟

    - از یک میزبانِ CDNِ شناخته‌شده باشد (که فایلِ تصویر را مستقیم می‌دهد)،
      یا
    - پسوندِ فایلِ تصویر داشته باشد (.jpg/.png/...).
    لینکِ صفحه/مقاله (که فقط داخل مرورگر باز می‌شود و دانلودِ مستقیم نمی‌دهد)
    و دامنه‌هایِ اسپم/تبلیغاتی/کازینو/فروشگاهی رد می‌شوند تا ربات سراغِ
    لینک‌هایِ ناکارآمد یا نامرتبط نرود.
    """
    if not url:
        return False
    if _is_spam_url(url):
        return False
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    if any(host == h or host.endswith("." + h) for h in _RELIABLE_HOSTS):
        return True
    return bool(_IMAGE_EXT_RE.search(url))


# کلماتِ پرتکرار/غیر معنادار که برایِ سنجشِ ارتباط نباید شمرده شوند.
_STOPWORDS = {"عکس", "تصویر", "تصاویر", "های", "و", "با", "به", "از", "یک",
              "برای", "تصویر", "عکسی", "لاکپشت", "photo", "image", "pic",
              "the", "a", "an", "of", "for", "and"}

# نگاشتِ کوچکِ فارسی → انگلیسی برایِ کلماتِ پرکاربرد، تا جستجویِ انگلیسیِ
# مرتبط هم انجام شود (مثل «لاکپشت» → turtle / «نینجا» → ninja).
_FA_EN_HINTS = {
    "لاکپشت": "turtle", "لاک پشت": "turtle", "لاکپشتی": "tortoise",
    "نینجا": "ninja", "سگ": "dog", "گربه": "cat", "گل": "flower",
    "لاکپشت های نینجا": "ninja turtle", "لاک پشت های نینجا": "ninja turtle",
    "نینجا لاکپشت": "ninja turtle", "کریستیانو": "cristiano ronaldo",
    "رونالدو": "ronaldo",
    "گل رز": "rose", "کوه": "mountain", "منظره": "landscape",
    "ماشین": "car", "ماشین مسابقه": "race car", "طبیعت": "nature",
    "ماشین ۴۰۵": "peugeot 405", "پژو ۴۰۵": "peugeot 405",
    "۴۰۵": "peugeot 405", "ماشین پژو": "peugeot",
    "دوج": "dodge", "ماشین دوج": "dodge car", "خودرو دوج": "dodge car",
    "دوج چلنجر": "dodge challenger", "دوج چارجر": "dodge charger",
    "دوج رام": "dodge ram", "بی‌ام‌و": "bmw", "مرسدس": "mercedes",
    "پورشه": "porsche", "فراری": "ferrari", "لامبورگینی": "lamborghini",
    "تویوتا": "toyota", "هیوندای": "hyundai", "کره‌ای": "kia",
    "شورولت": "chevrolet", "فورد": "ford", "مازراتی": "maserati",
    "آئودی": "audi", "فولکس": "volkswagen", "جیپ": "jeep",
    "اتوبوس": "bus", "کامیون": "truck", "وانت": "pickup truck",
    "موتور": "motorcycle", "موتورسیکلت": "motorcycle", "موتور هوندا": "honda motorcycle",
    "هوندا": "honda", "موتور سیکلت": "motorcycle", "دوچرخه": "bicycle",
    "قطار": "train", "هواپیما": "airplane", "کشتی": "ship", "قایق": "boat",
    "شهر": "city", "ساحل": "beach", "صحرا": "desert", "جزیره": "island",
    "برج ایفل": "eiffel tower", "ایفل": "eiffel tower",
    "کوه دماوند": "damavand", "دماوند": "damavand",
    "برج میلاد": "milad tower", "پل طبیعت": "nature bridge tehran",
    "کعبه": "kaaba", "مسجد": "mosque", "کلیسا": "church",
    "اهرام": "pyramid", "اهرام مصر": "pyramids of giza", "تاج محل": "taj mahal",
    "نیویورک": "new york", "پاریس": "paris", "لندن": "london",
    "تهران": "tehran", "دبی": "dubai", "توکیو": "tokyo", "رم": "rome",
    "آزادی": "azadi tower", "تخت جمشید": "persepolis",
    "کتاب": "book", "خانه": "house", "ساختمان": "building", "برج": "tower",
    "لباس": "clothing", "لباس ارتشی": "military uniform", "ارتشی": "military",
    "لباس نظامی": "military uniform", "یونیفرم": "uniform",
    "لباس مجلسی": "evening dress", "لباس عروس": "wedding dress",
    "پیراهن": "shirt", "شلوار": "pants", "کت": "jacket", "کلاه": "hat",
    "کفش": "shoes", "چکمه": "boots", "روسری": "scarf", "شال": "shawl",
    "دستکش": "gloves", "جوراب": "socks", "لباس ورزشی": "sportswear",
    "ساعت": "watch", "عینک": "glasses", "کیف": "bag", "کوله‌پشتی": "backpack",
    "عینک آفتابی": "sunglasses", "جواهرات": "jewelry", "انگشتر": "ring",
    "پروفایل": "profile", "پرفایل": "profile", "عکس پروفایل": "profile picture",
    "دختر": "girl", "دخترونه": "girl", "دختران": "girls", "دختر زیبا": "beautiful girl",
    "پسر": "boy", "پسرونه": "boy", "زن": "woman", "مرد": "man",
    "پرفایل دخترونه": "girl portrait", "پروفایل دخترانه": "girl portrait",
    "پروفایل دخترونه": "girl portrait", "پرفایل پسرونه": "boy portrait",
    "غذا": "food", "میوه": "fruit", "سیب": "apple", "موز": "banana",
    "پرتقال": "orange", "گوجه": "tomato", "هویج": "carrot", "گلابی": "pear",
    "انگور": "grape", "هندوانه": "watermelon", "توت": "berry",
    "فوتبال": "football", "بسکتبال": "basketball", "والیبال": "volleyball",
    "تنیس": "tennis", "گلف": "golf", "شنا": "swimming", "دویدن": "running",
    "ورزش": "sport", "کوهستان": "mountain range", "قلعه": "castle",
    "پل": "bridge", "جاده": "road", "تپه": "hill", "آبشار": "waterfall",
    "پلنگ": "leopard", "پلنگ صورتی": "pink panther", "پلنگ برفی": "snow leopard",
    "یوزپلنگ": "cheetah", "ببر": "tiger", "شیر": "lion",
    "رودخانه": "river", "اقیانوس": "ocean", "لاکپشت": "turtle", "کوسه": "shark",
    "دلفین": "dolphin", "نهنگ": "whale", "خرچنگ": "crab", "پروانه": "butterfly",
    "زنبور": "bee", "مورچه": "ant", "عنکبوت": "spider", "مار": "snake",
    "سوسمار": "lizard", "تمساح": "crocodile", "گوزن": "deer", "گورخر": "zebra",
    "شترمرغ": "ostrich", "طاووس": "peacock", "جغد": "owl", "عقاب": "eagle",
    "کلاغ": "crow", "قناری": "canary", "کبوتر": "pigeon", "مرغ": "chicken",
    "خروس": "rooster", "بز": "goat", "گوسفند": "sheep", "گاو": "cow",
    "ماهی قرمز": "goldfish", "اختاپوس": "octopus", "ستاره دریایی": "starfish",
    "لاکپشت دریایی": "sea turtle", "شاهین": "falcon", "فلامینگو": "flamingo",
    "آسمان": "sky", "دریا": "sea", "جنگل": "forest", "پرنده": "bird",
    "ماهی": "fish", "اسب": "horse", "ببر": "tiger", "شیر": "lion",
    "خرس": "bear", "گرگ": "wolf", "روباه": "fox", "پاندا": "panda",
    "سنجاب": "squirrel", "خرگوش": "rabbit", "فیل": "elephant",
    "زرافه": "giraffe", "کانگورو": "kangaroo", "پنگوئن": "penguin",
    "خورشید": "sun", "ماه": "moon", "ستاره": "star", "باران": "rain",
    "برف": "snow", "غروب": "sunset", "طلوع": "sunrise",
}

# نگاشتِ افراد/شخصیت‌هایِ مشهور فارسی → انگلیسی (برایِ جستجویِ دقیقِ تصویر).
# این‌ها «حساس» هستند: باید عبارتِ انگلیسیِ کاملِ شخص را به‌عنوانِ anchor
# بیاورند تا تصاویرِ واقعیِ همان شخص (نه فیلم/مکان/شباهت) انتخاب شوند.
_FA_PEOPLE_HINTS = {
    "بروسلی": "bruce lee", "بروس لی": "bruce lee", "بروسلی": "bruce lee",
    "رونالدو": "cristiano ronaldo", "کریستیانو رونالدو": "cristiano ronaldo",
    "تتلو": "amir tataloo", "امیر تتلو": "amir tataloo",
    "امیر تاتالو": "amir tataloo", "تاتالو": "amir tataloo",
    "مسی": "lionel messi", "لیونل مسی": "lionel messi",
    "نیمار": "neymar", "پله": "pele", "مارادونا": "maradona",
    "زیدان": "zidane", "رونالدینیو": "ronaldinho",
    "مایکل جکسون": "michael jackson", "مایکل جردن": "michael jordan",
    "لئوناردو دیکاپریو": "leonardo dicaprio", "برد پیت": "brad pitt",
    "آلبرت انیشتین": "albert einstein", "نیوتن": "isaac newton",
    "ناپلئون": "napoleon", "جنگجو": "warrior",
    "گاندی": "gandhi", "چگوارا": "che guevara", "مونالیزا": "mona lisa",
    "سوپرمن": "superman", "بتمن": "batman", "اسپایدرمن": "spiderman",
    "هالک": "hulk", "ثور": "thor", "آیرونمن": "iron man",
    "دزدان دریایی": "pirate", "دزد دریایی": "pirate", "دزدان دریایی کارائیب": "pirates of caribbean",
    "جنگ ستارگان": "star wars", "ددپول": "deadpool", "ولورین": "wolverine",
    "سامورایی": "samurai", "نینجا": "ninja", "کونگ‌فو": "kung fu",
    "رزمی": "martial arts", "کاراته": "karate", "جودو": "judo", "تکواندو": "taekwondo",
    "مشت‌زن": "boxer", "بوکس": "boxing", "کشتی‌گیر": "wrestler",
}


def _normalize_fa_text(value):
    """نرمال‌سازیِ متنِ فارسی: حذفِ نیم‌فاصله (ZWNJ)، یکسان‌سازیِ حروفِ عربی/فارسی.

    «لاک‌پشت» → «لاکپشت»، «ك»→«ک»، «ي»→«ی»، «ة»→«ه». این کار معنی را
    تغییر نمی‌دهد و باعث می‌شود شکل‌هایِ مختلفِ یک کلمه (با/بدونِ نیم‌فاصله)
    یکسان شناخته شوند.
    """
    if not value:
        return ""
    text = (value or "").replace("\u200c", "").replace("\u200f", "")
    for a, b in (("ي", "ی"), ("ك", "ک"), ("ة", "ه"), ("ۀ", "ه"), ("ؤ", "و")):
        text = text.replace(a, b)
    return text


def _fa_key_in_query(key, q):
    """آیا «کلید» به‌عنوانِ یک کلمهٔ کامل در عبارت هست؟ (نه داخلِ کلمهٔ دیگر)

    مانعِ تطابقِ اشتباه مثل «پل» داخلِ «پلنگ» می‌شود.
    """
    if not key:
        return False
    # کلیدِ چندکلمه‌ای: باید دقیقاً به‌صورتِ زیرعبارت (با مرزِ کلمه) باشد
    if " " in key.strip():
        return (" " + key.strip() + " ") in (" " + q + " ")
    # کلیدِ تک‌کلمه: در بینِ توکن‌هایِ عبارت باشد
    return key.strip() in set(q.split())


def _search_keywords(query):
    """کلیدواژه‌هایِ جستجو را می‌سازد (عبارتِ نرمال‌شده + نگاشتِ انگلیسیِ مشخص).

    برایِ سنجشِ ارتباط استفاده می‌شود. ابتدا متن نرمال می‌شود (حذفِ نیم‌فاصله
    و یکسان‌سازیِ حروف) تا «لاک‌پشت» و «لاکپشت» یکی شوند. سپس:
      - کلماتِ معنادارِ خودِ عبارت (فارسیِ نرمال‌شده)، و
      - عبارتِ انگلیسیِ مشخص از نگاشتِ کوچک استفاده می‌شود.
    **نویسه‌گردانیِ سادهٔ فارسی→لاتین استفاده نمی‌شود** چون خروجیِ بی‌معنا
    می‌دهد که باعثِ تطابقِ اتفاقی با تصاویرِ نامرتبط می‌شود.
    خروجی: (keywords, search_queries, english_hint) — english_hint در صورتِ
    وجود برایِ سنجشِ «قویِ» ارتباط استفاده می‌شود.
    """
    kws = set()
    q = _normalize_fa_text(query).strip()
    if not q:
        return kws, [], None

    # کلماتِ معنادارِ خودِ عبارت (فارسیِ نرمال‌شده) — بدونِ کلماتِ توقف
    for tok in q.split():
        tok = tok.lower()
        if tok and tok not in _STOPWORDS:
            kws.add(tok)

    # عبارتِ انگلیسیِ مشخص — از هر دو نگاشت (افرادِ مشهور + عمومی)،
    # «طولانی‌ترین» و مشخص‌ترین تطابق انتخاب می‌شود.
    # (مثلاً «لاکپشت های نینجا» → ninja turtle، نه فقط ninja)
    search_queries = [q]
    english_hint = None
    # طولانی‌ترین «کلیدِ فارسیِ» منطبق (با مرزِ کلمه) انتخاب می‌شود تا
    # «کوه دماوند» به‌جایِ «کوه»→mountain به «دماوند»→damavand برسد،
    # و «پل» داخلِ «پلنگ» تطبیق نخورد.
    matched = [fa for fa in _FA_PEOPLE_HINTS if _fa_key_in_query(fa, q)]
    matched += [fa for fa in _FA_EN_HINTS if _fa_key_in_query(fa, q)]
    if matched:
        best_fa = max(matched, key=len)
        english_hint = (_FA_PEOPLE_HINTS.get(best_fa)
                        or _FA_EN_HINTS.get(best_fa))
        search_queries.append(english_hint)
        for tok in english_hint.split():
            kws.add(tok.lower())

    kws.discard("")
    return kws, search_queries, english_hint


def _relevance_score(title, tags, url, keywords):
    """امتیازِ ارتباطِ یک تصویر با کلیدواژه‌هایِ جستجو."""
    if not keywords:
        return 0
    text = (" ".join([title or "", " ".join(tags or []), url or ""])).lower()
    score = 0
    for kw in keywords:
        if kw and kw in text:
            score += 1
    return score


def _strongly_relevant(title, tags, url, keywords, english_hint):
    """آیا تصویر «به‌شدت» مرتبط است؟

    اگر عبارتِ انگلیسیِ مشخص (english_hint) وجود داشته باشد، باید **همهٔ**
    کلماتِ آن در عنوان/برچسب/URLِ تصویر حاضر باشند (مثل ninja + turtle برایِ
    «لاکپشت های نینجا»). این باعث می‌شود تصویرِ اتفاقیِ «ninja» (بدونِ turtle)
    یا «hay» رد شود. اگر hint نباشد، همان امتیازِ ≥۱ کافی است.
    """
    text = (" ".join([title or "", " ".join(tags or []), url or ""])).lower()
    if english_hint:
        hint_tokens = [t for t in english_hint.lower().split() if t]
        if not hint_tokens:
            return True
        return all(t in text for t in hint_tokens)
    if not keywords:
        return False
    return any(kw in text for kw in keywords)


def _search_openverse(query, limit):
    """جستجو در Openverse؛ برمی‌گرداند لیستِ (url, title, tags)."""
    from urllib.parse import quote
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
    }
    url = ("https://api.openverse.org/v1/images/?q="
           + quote(query) + "&page_size=20")
    resp = _HTTP.get(url, headers=headers, timeout=NETWORK_TIMEOUT)
    if resp.status_code != 200:
        return []
    data = resp.json()
    out = []
    seen = set()
    for item in data.get("results", []):
        u = (item.get("url") or "").strip()
        if not (u and "http" in u and u not in seen):
            continue
        if not _is_direct_image_url(u):
            continue
        seen.add(u)
        title = item.get("title") or ""
        tags = [t.get("name") for t in (item.get("tags") or []) if t.get("name")]
        out.append((u, title, tags))
        if len(out) >= limit * 3:  # جمع‌آوریِ بیشتر برایِ انتخابِ مرتبط‌ترین
            break
    return out


def _search_wikimedia(query, limit):
    """جستجو در Wikimedia Commons — محتوای فارسیِ خوبی دارد.

    برایِ عبارت‌هایِ فارسی که Openverse نتیجه نمی‌دهد، منبعِ دوم است.
    برمی‌گرداند لیستِ (url, title, []).
    """
    from urllib.parse import quote
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
    }
    try:
        url = ("https://commons.wikimedia.org/w/api.php?action=query"
               "&generator=search&gsrsearch=" + quote(query) +
               "&gsrnamespace=6&gsrlimit=" + str(limit * 3) +
               "&prop=imageinfo&iiprop=url&iiurlwidth=800&format=json")
        data = _HTTP.get(url, headers=headers, timeout=NETWORK_TIMEOUT).json()
    except Exception:
        return []
    out = []
    seen = set()
    for p in (data.get("query", {}).get("pages", {}) or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        u = (ii.get("thumburl") or ii.get("url") or "").strip()
        if not (u and "http" in u and u not in seen):
            continue
        if not _is_direct_image_url(u):
            continue
        seen.add(u)
        out.append((u, p.get("title") or "", []))
        if len(out) >= limit * 3:
            break
    return out


def _search_wikipedia_topic(query, limit=None):
    """جستجویِ ویکی‌پدیا (فارسی): تشخیصِ موضوع + گرفتنِ تصویرِ اصلیِ صفحه.

    برمی‌گرداند لیستِ (url, title, []). اگر صفحه‌ای با تصویرِ اصلی پیدا
    نشد (یا ویکی در دسترس نبود/rate-limit شد)، لیستِ خالی برمی‌گردد تا
    fallback به منابعِ دیگر انجام شود.
    """
    from urllib.parse import quote
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
    }
    # اول ویکیِ فارسی، بعد (اگر نتیجه نبود) ویکیِ انگلیسی (پوششِ بهتر برایِ
    # برند/مدل مثل «dodge»).
    for lang in ("fa", "en"):
        try:
            # ۱) جستجویِ صفحه در ویکی‌پدیا
            search = (f"https://{lang}.wikipedia.org/w/api.php?action=query"
                      "&list=search&srsearch=" + quote(query) +
                      "&srlimit=1&format=json")
            data = _HTTP.get(search, headers=headers, timeout=NETWORK_TIMEOUT)
            if data.status_code != 200:
                continue
            hits = data.json().get("query", {}).get("search", [])
            if not hits:
                continue
            title = hits[0].get("title") or ""
            if not title:
                continue

            # ۲) گرفتنِ تصویرِ اصلیِ همان صفحه (pageimages)
            page = (f"https://{lang}.wikipedia.org/w/api.php?action=query"
                    "&titles=" + quote(title) +
                    "&prop=pageimages&piprop=thumbnail|original"
                    "&pithumbsize=800&format=json")
            data2 = _HTTP.get(page, headers=headers, timeout=NETWORK_TIMEOUT)
            if data2.status_code != 200:
                continue
            pages = data2.json().get("query", {}).get("pages", {}) or {}
            for p in pages.values():
                pi = p.get("thumbnail") or {}
                u = (pi.get("source") or "").strip()
                if u and "http" in u and _is_direct_image_url(u):
                    return [(u, title, [])]
        except Exception:
            pass
    return []


_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _norm_for_match(value):
    """متن را برایِ مقایسهٔ ارتباط نرمال می‌کند: فارسی+حروف عربی+ارقام.

    «۴۰۵» و «405» یکی می‌شوند؛ «پژو ۴۰۵» و «Peugeot 405» با ترجمهٔ سادهٔ
    اعداد قابلِ مقایسه‌اند.
    """
    text = _normalize_fa_text(value).lower()
    text = text.translate(_PERSIAN_DIGITS)
    return text


def _title_relevant(title, tags, url, exact, keywords, english_hint):
    """آیا عنوان/برچسب/URLِ یک نتیجه با عبارتِ جستجو مرتبط است؟

    - اگر hint انگلیسی داریم، باید همهٔ کلماتِ آن در متنِ نتیجه باشد (فیلترِ
      قوی؛ مثلاً pink + panther).
    - اگر hint نداریم، با «کلیدواژه‌هایِ خودِ عبارت» (پس از نرمال‌سازیِ ارقام)
      مقایسه می‌شود؛ هر تطابقی کافی است.
    برمی‌گرداند True اگر مرتبط باشد.
    """
    text = _norm_for_match(" ".join([title or "", " ".join(tags or []), url or ""]))

    # فیلترِ قوی با hint انگلیسی
    if english_hint:
        hint_tokens = [t for t in english_hint.lower().split() if t]
        if hint_tokens:
            if all(t in text for t in hint_tokens):
                return True
            # اگر hint کامل نشد، به تطابقِ کلیدواژه‌هایِ خودِ عبارت برگرد
        # (در صورتِ نبودِ تطابقِ کاملِ hint، به کلیدواژه‌هایِ عبارت وابسته است)

    # تطابق با کلیدواژه‌هایِ خودِ عبارت (نرمال‌شده، با ارقام)
    for kw in keywords:
        nkw = _norm_for_match(kw)
        if nkw and len(nkw) >= 2 and nkw in text:
            return True

    # تطابقِ دقیقِ کلِ عبارت (نرمال‌شده) در عنوان
    n_exact = _norm_for_match(exact)
    if n_exact and len(n_exact) >= 2 and n_exact in text:
        return True

    return False


def _search_image_urls(query, limit=IMAGE_COUNT):
    """جستجویِ تصویر: «عبارتِ دقیقِ کاربر» به‌صورتِ همان‌که نوشته جستجو می‌شود.

    اصول (مطابقِ خواستهٔ کاربر):
      ۱) عبارتِ دقیقِ کاربر بدونِ هیچ تغییر/ترجمه/حدسی جستجو می‌شود.
      ۲) منابعِ معتبر: ویکی‌پدیا (تصویرِ اصلیِ صفحهٔ موضوع) و Wikimedia Commons
         (که برایِ فارسی خوب کار می‌کنند) و Openverse.
      ۳) نتایجِ «عبارتِ دقیق» از این منابع معتبر همان هستند که موتور برایِ
         همان عبارتِ دقیق پیدا کرده و «اعتماد» می‌شوند (مرتبط‌اند).
      ۴) فقط برایِ «افرادِ مشهورِ شناخته‌شده» (مثل بروسلی) که عبارتِ فارسی‌شان
         در منبع بی‌ربط است، عبارتِ انگلیسیِ دقیقِ همان شخص هم به‌عنوانِ مکمل
         جستجو می‌شود و نتایجِ آن با فیلترِ سختِ ارتباط بررسی می‌شوند.
      ۵) اگر هیچ نتیجه‌ای (عبارتِ دقیق) نبود → خالی (پیامِ «تصویر مرتبطی
         پیدا نشد»).
    """
    keywords, _search_queries, english_hint = _search_keywords(query)
    exact = _normalize_fa_text(query).strip()
    if not exact:
        return []

    cache_key = exact.lower()
    now = time.monotonic()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1][:limit]
    if cached:
        _SEARCH_CACHE.pop(cache_key, None)

    exact_candidates = []  # از عبارتِ دقیق (اولویتِ اول)
    hint_candidates = []   # از عبارتِ انگلیسیِ مکمل (fallback)

    # ۱) عبارتِ دقیقِ کاربر (اولویتِ اصلی)
    for src in (_search_wikipedia_topic, _search_wikimedia, _search_openverse):
        try:
            exact_candidates += src(exact, limit)
        except Exception:
            pass

    # ۲) عبارتِ انگلیسیِ مکمل (اگر hint داریم) — برایِ مواردی که عبارتِ فارسی
    #    در منبع بی‌ربط است (مثل «پلنگ صورتی»→pink panther یا «ماشین ۴۰۵»→
    #    peugeot 405 یا افرادِ مشهور). این فقط fallback است.
    if english_hint:
        for src in (_search_wikipedia_topic, _search_wikimedia, _search_openverse):
            try:
                hint_candidates += src(english_hint, limit)
            except Exception:
                pass

    # ترکیب: اول نتایجِ عبارتِ دقیق (مرتبط، اعتمادشده)، بعد مکملِ افرادِ مشهور
    # با فیلترِ ارتباطِ قوی (همهٔ کلماتِ hint)
    combined = []
    seen = set()

    for url, title, tags in exact_candidates:
        if url in seen:
            continue
        seen.add(url)
        # عبارتِ دقیق از موتورِ معتبر آمده؛ اما هنوز باید «مرتبط» باشد تا
        # نتایجِ بی‌ربطِ موتور (مثلِ ماشینِ عمومی برایِ «ماشین ۴۰۵» یا صفحهٔ
        # بی‌ربط برایِ «پلنگ صورتی») حذف شوند. با tolerance به اختلافِ زبان
        # و ارقامِ فارسی/انگلیسی.
        if not _title_relevant(title, tags, url, exact, keywords, english_hint):
            continue
        combined.append((5, url))  # وزنِ بالا برایِ عبارتِ دقیق

    for url, title, tags in hint_candidates:
        if url in seen:
            continue
        seen.add(url)
        # برایِ مکملِ افرادِ مشهور: فیلترِ سختِ ارتباط (همهٔ کلماتِ hint)
        if english_hint and not _strongly_relevant(title, tags, url, keywords, english_hint):
            continue
        combined.append((2, url))

    if not combined:
        return []

    # مرتب‌سازیِ پایدار: عبارتِ دقیق اول
    combined.sort(key=lambda x: x[0], reverse=True)
    result = [url for _w, url in combined[:limit]]

    _SEARCH_CACHE[cache_key] = (now, result)
    # TTL alone did not remove one-off queries; enforce a hard memory cap.
    for key, (created, _urls) in list(_SEARCH_CACHE.items()):
        if now - created >= _CACHE_TTL:
            _SEARCH_CACHE.pop(key, None)
    while len(_SEARCH_CACHE) > _CACHE_MAX:
        _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)), None)
    return result


def _is_valid_image_bytes(content):
    """بررسیِ magic bytes؛ صرفاً بر اساسِ محتوا، نه content-type.

    اگر سایت content-type اشتباه بدهد ولی bytes واقعاً تصویرِ معتبر باشد،
    اینجا رد نمی‌شود.
    """
    if not content:
        return False
    # JPEG — خانوادهٔ \xff\xd8\xff (اکثر عکس‌ها)
    if content[:3] == b"\xff\xd8\xff":
        return True
    # PNG
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # GIF
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return True
    # BMP
    if content[:2] == b"BM":
        return True
    # WebP — RIFF....WEBP
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    # AVIF — 'ftypavif'/'ftypavis' در ابتدای فایل
    if b"ftypavif" in content[:32] or b"ftypavis" in content[:32]:
        return True
    return False


def _fetch_image_bytes(url, timeout=NETWORK_TIMEOUT, log_func=None):
    """تصویر را دانلود و به bytes تبدیل می‌کند (همگام، داخل thread).

    هدرِ مرورگر + Accept برای تصویر فرستاده می‌شود تا سایت‌ها به‌جای صفحهٔ
    HTML خودِ عکس را بدهند. فقط محتوایی برمی‌گردد که magic bytes معتبرِ
    تصویر داشته باشد؛ دربارهٔ content-type تصمیم نمی‌گیرد.
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Accept": ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
        "Referer": "https://www.bing.com/",
    }
    if log_func:
        log_func(f"DOWNLOAD REQUEST url={url}")
    try:
        resp = _HTTP.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        if log_func:
            log_func(f"DOWNLOAD ERROR url={url} reason={type(e).__name__}: {e}\n"
                     f"{traceback.format_exc()}")
        return None
    content = resp.content
    magic = content[:20]
    if log_func:
        log_func(
            f"DOWNLOAD url={url} status={resp.status_code} "
            f"type={resp.headers.get('Content-Type')} bytes={len(content)} "
            f"magic={magic.hex()}")
    if resp.status_code != 200:
        if log_func:
            log_func(f"DOWNLOAD reject reason=status_{resp.status_code}")
        return None
    if not content or len(content) < 100:
        if log_func:
            log_func("DOWNLOAD reject reason=size_too_small")
        return None
    if not _is_valid_image_bytes(content):
        if log_func:
            log_func("DOWNLOAD reject reason=bad_magic")
        return None
    if log_func:
        log_func("DOWNLOAD accept")
    return content


def _make_image_stream(data, index=0):
    """bytes تصویر را به یک استریمِ قابلِ ارسال با نام و پسوند معتبر تبدیل می‌کند.

    نکتهٔ مهم: وقتی فایل بدونِ ``name``/پسوند به ``send_file`` داده می‌شود،
    کتابخانه آن را به‌عنوان یک «سند» بدونِ پسوند (application/octet-stream)
    آپلود می‌کند و سرورِ سروش نمی‌تواند آن را به‌درستی به‌عنوان عکس بشناسد.
    با گذاشتنِ نامِ ``photo_<n>.jpg`` و موقعیتِ ابتدای جریان، تصویر به‌عنوان
    یک عکسِ واقعی (photo) با پسوند و mime درست آپلود و ارسال می‌شود.
    """
    stream = io.BytesIO(data)
    stream.name = f"photo_{int(index) + 1}.jpg"
    stream.seek(0)
    return stream


def _log(bot, message):
    """لاگِ امن روی logger ربات (و logger استانداردِ پایتون).

    اگر logger ربات در دسترس نبود، به loggerِ پایتون می‌رود تا چیزی
    بی‌صدا از دست نرود.
    """
    logging.getLogger("photo_download").info(message)
    try:
        bot.logger.log_info(message)
    except Exception:
        pass


def _log_exception(bot, context):
    """لاگِ کاملِ استثناء با traceback — تا معلوم شود مشکل از کجاست.

    از ``logger.exception`` استفاده می‌کند که خودش tracebackِ جاری را
    می‌گیرد. اگر logger ربات در دسترس نبود، به loggerِ پایتون و
    ``bot.logger.log_error`` برمی‌گردد.
    """
    tb = traceback.format_exc()
    msg = f"{context}\n{tb}"
    try:
        underlying = getattr(bot.logger, "logger", None)
        if underlying is not None and hasattr(underlying, "exception"):
            underlying.exception(context)
        else:
            bot.logger.log_error(msg)
    except Exception:
        logging.getLogger("photo_download").exception(context)
    # همیشه یک کپی در فایلِ لاگ ربات هم بنویس (اگر روش بالا جواب نداد)
    try:
        bot.logger.log_error(msg)
    except Exception:
        pass


async def _send_by_url(bot, chat_id, url):
    """ارسالِ عکس با URL (InputMediaPhotoExternal).

    در این روش سرورِ سروش خودش تصویر را از ``url`` می‌گیرد و آن را به‌صورت
    یک عکسِ واقعی داخل چت می‌فرستد. **هیچ آپلودی رخ نمی‌دهد** و درخواستِ
    ``upload.saveFilePart`` ساخته نمی‌شود؛ بنابراین خطای
    ``FILE_REQUEST_RECEIVED_ON_CONNECTION_SERVER`` پیش نمی‌آید.
    """
    from splusthon.tl import types, functions
    entity = await bot.client.get_input_entity(chat_id)
    media = types.InputMediaPhotoExternal(url=url)
    request = functions.messages.SendMediaRequest(entity, media, message="")
    await bot.client(request)
    return True


async def _download_valid(bot, url):
    """دانلود و اعتبارسنجیِ یک عکس؛ اگر واقعاً تصویرِ معتبر باشد bytes برمی‌گرداند.

    در غیرِ این صورت None برمی‌گرداند (عکسِ خراب/نامعتبر رد می‌شود تا لینکِ
    شکسته ارسال نشود).
    """
    _log(bot, f"PHOTO DOWNLOAD START url={url}")
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(
                _fetch_image_bytes, url,
                log_func=lambda m: _log(bot, m)),
            timeout=LINK_TIMEOUT)
    except asyncio.TimeoutError:
        _log(bot, f"PHOTO DOWNLOAD TIMEOUT url={url} timeout={LINK_TIMEOUT}s")
        return None
    except Exception:
        _log_exception(bot, f"PHOTO DOWNLOAD ERROR url={url}")
        return None
    if not data:
        _log(bot, f"PHOTO DOWNLOAD FAILED url={url} reason=download_invalid")
        return None
    _log(bot, f"PHOTO DOWNLOAD OK url={url} bytes={len(data)}")
    return data


async def _download_valid_parallel(bot, urls, limit, deadline):
    """دانلودِ هم‌زمانِ لینک‌ها تا جمع‌آوریِ limit عکسِ معتبر.

    لینک‌ها با حداکثرِ ``DL_CONCURRENCY`` هم‌زمان بررسی می‌شوند؛ اولین
    عکس‌هایِ معتبر سریع برمی‌گردند تا پاسخ در چند ثانیه باشد نه چند دقیقه.
    """
    results = []

    async def _try(url):
        if len(results) >= limit or time.monotonic() >= deadline:
            return None
        data = await _download_valid(bot, url)
        if data:
            results.append((url, data))
        return data

    # دانلودِ هم‌زمانِ همهٔ نامزدها (محدود به DL_CONCURRENCY هم‌زمان)
    sem = asyncio.Semaphore(DL_CONCURRENCY)

    async def _worker(url):
        async with sem:
            if len(results) >= limit or time.monotonic() >= deadline:
                return
            data = await _download_valid(bot, url)
            if data:
                results.append((url, data))

    tasks = [_worker(u) for u in urls]
    await asyncio.gather(*tasks, return_exceptions=True)
    return results[:limit]


async def _upload_album(bot, chat_id, items, force_reconnect=False):
    """ارسالِ همهٔ عکس‌هایِ معتبر با هم به‌صورتِ یک آلبوم (نه جدا جدا).

    items = [(url, bytes), ...]. اگر media sender نبود/قطع بود و
    force_reconnect=True، قبل از آپلود دوباره ساخته می‌شود.
    """
    if not items:
        return False
    streams = []
    for idx, (_url, data) in enumerate(items):
        streams.append(_make_image_stream(data, idx))
    if force_reconnect and hasattr(bot.client, "_call"):
        try:
            from modules.splusthon_upload_fix import _get_media_sender
            await _get_media_sender(bot.client, force_reconnect=True)
        except Exception:
            pass
    try:
        await asyncio.wait_for(
            bot.client.send_file(chat_id, streams), timeout=LINK_TIMEOUT * len(items))
        _log(bot, f"PHOTO SEND OK method=album count={len(items)}")
        return True
    except asyncio.TimeoutError:
        _log(bot, f"PHOTO SEND TIMEOUT method=album count={len(items)}")
    except Exception:
        _log_exception(bot, f"PHOTO SEND ERROR method=album count={len(items)}")
    return False


def _u16_len(value):
    """طول به واحدِ UTF-16 (که MessageEntity از آن استفاده می‌کند)."""
    return len((value or "").encode("utf-16-le")) // 2


def _build_links_message(items):
    """متنِ شماره‌دارِ لینک‌ها + entityهای blockquote برای هر لینک.

    خروجی: (text, entities) — هر لینک جدا، شماره‌خورده و داخلِ نقلِ‌قولِ
    شیشه‌ای (MessageEntityBlockquote) قرار می‌گیرد.
    """
    from splusthon.tl.types import MessageEntityBlockquote

    header = "🖼️ تصویر (لینک‌هایِ مستقیم):\n\n"
    blocks = []
    entities = []
    offset = _u16_len(header)
    for i, (url, _data) in enumerate(items, 1):
        block = f"لینک {i}:\n{url}"
        blocks.append(block)
        entities.append(MessageEntityBlockquote(
            offset=offset, length=_u16_len(block)))
        offset += _u16_len(block) + _u16_len("\n\n")
    text = header + "\n\n".join(blocks)
    return text, entities


async def _send_links_together(bot, chat_id, items):
    """fallback: همهٔ لینک‌هایِ معتبر را در یک پیامِ متن می‌فرستد.

    هر لینک جدا، شماره‌خورده و داخلِ نقلِ‌قولِ شیشه‌ای (blockquote) است.
    فقط لینکِ عکس‌هایی که واقعاً معتبر دانلود شده‌اند فرستاده می‌شود؛ هرگز
    لینکِ شکسته/نامعتبر.
    """
    text, entities = _build_links_message(items)
    try:
        await bot.client.send_message(
            chat_id, text, formatting_entities=entities)
    except Exception:
        # اگر سرور entity را نپذیرد، همان متنِ ساده ارسال شود (بدون blockquote)
        _log_exception(bot, "PHOTO SEND direct_links entities rejected; retry plain")
        await bot.client.send_message(chat_id, text)
    return True


# ---------------------------------------------------------------------------
#  API عمومی برای هندلر
# ---------------------------------------------------------------------------
def start_session(chat_id, user_id):
    """شروع جریانِ «دانلود عکس»؛ درخواستِ عبارت."""
    now = time.monotonic()
    for stale_key, state in list(_SESSIONS.items()):
        if now - float(state.get("ts", 0)) > CONFIRM_TIMEOUT:
            _SESSIONS.pop(stale_key, None)
    while len(_SESSIONS) >= _SESSION_MAX:
        _SESSIONS.pop(next(iter(_SESSIONS)), None)
    key = (chat_id, user_id)
    _SESSIONS[key] = {"query": None, "ts": now}


def session(chat_id, user_id):
    """دادهٔ جریانِ تأییدِ این کاربر، یا None."""
    key = (chat_id, user_id)
    s = _SESSIONS.get(key)
    if s is None:
        return None
    if time.monotonic() - s["ts"] > CONFIRM_TIMEOUT:
        _SESSIONS.pop(key, None)
        return None
    return s


def close_session(chat_id, user_id):
    _SESSIONS.pop((chat_id, user_id), None)


def _is_main_owner(user_id):
    """فقط مالکِ اصلیِ ربات (osine2) برایِ تست، بدونِ سکه استفاده می‌کند."""
    try:
        from modules.owner_check import is_global_owner
        return is_global_owner(user_id)
    except Exception:
        return False


def handle_query(chat_id, user_id, query):
    """بعد از دریافت عبارت، عبارت را ذخیره و پیام تأیید برمی‌گرداند.

    خروجی: ("ask_confirm", confirm_text) یا ("blocked", msg) یا
            ("insufficient", msg) یا ("start_processing", None).
    """
    key = (chat_id, user_id)
    s = _SESSIONS.get(key)
    if s is None:
        return "no_session", None

    if is_blocked(query):
        close_session(chat_id, user_id)
        return "blocked", BLOCKED_CONTENT

    # فقط مالکِ اصلی (osine2) برایِ تست، بدونِ نیاز به سکه است؛
    # سایرِ مالکان/ادمین‌ها همچنان باید سکه داشته باشند.
    if not _is_main_owner(user_id):
        balance = economy.get_balance(chat_id, user_id)
        if balance.get(economy.BRONZE, 0) < COST_PER_IMAGE:
            close_session(chat_id, user_id)
            return "insufficient", INSUFFICIENT

    s["query"] = query
    s["ts"] = time.monotonic()
    return "ask_confirm", CONFIRM_TEXT


def handle_confirm(chat_id, user_id, text):
    """بررسی پاسخِ تأیید/لغو.

    خروجی: ("cancel", msg) یا ("start", None) یا ("no_session", None) یا
            ("invalid", msg).
    """
    key = (chat_id, user_id)
    s = _SESSIONS.get(key)
    if s is None or s.get("query") is None:
        return "no_session", None
    norm = _norm(text)
    if norm in {"لغو", "خیر", "نه", "انصراف", "بی‌خیال", "cancel", "no"}:
        close_session(chat_id, user_id)
        return "cancel", CANCELLED
    if norm in {"بله", "تایید", "تأیید", "آره", "بفرست", "ارسال", "ok", "yes", "بله بفرست"}:
        return "start", None
    return "invalid", CONFIRM_TEXT


async def process(chat_id, user_id, bot):
    """اجرای کاملِ جستجو/دانلود/ارسال/کسرِ سکه، با قفلِ گروه و صفِ کاربر.

    فقط وقتی فراخوانی می‌شود که تأیید شده و هنوز هیچ‌چیز کم نشده است.
    در پایان (موفق یا خطا) قفل/صف آزاد می‌شوند.
    """
    key = (chat_id, user_id)
    query = None
    s = _SESSIONS.get(key)
    if s is not None:
        query = s.get("query")
    if not query:
        close_session(chat_id, user_id)
        _release_busy(chat_id, user_id)
        return "no_session", None

    lock = _group_lock(chat_id)
    if not lock.locked():
        # فقط یک دانلود هم‌زمان در هر گروه
        await lock.acquire()
    _BUSY_GROUPS.add(chat_id)
    _BUSY_USERS.add(key)
    try:
        # ۱) جستجو (خارج از حلقه)
        _log(bot, f"PHOTO SEARCH START chat_id={chat_id} user_id={user_id} query={query!r}")
        try:
            urls = await asyncio.to_thread(_search_image_urls, query, SEARCH_CANDIDATES)
        except Exception:
            _log_exception(bot, f"PHOTO SEARCH EXCEPTION query={query!r}")
            urls = []
        _log(bot, f"PHOTO URL FOUND count={len(urls)} query={query!r}")
        if not urls:
            close_session(chat_id, user_id)
            return "no_results", NO_RESULTS

        # ۲) جستجو نتیجه نداد
        if not urls:
            close_session(chat_id, user_id)
            return "no_results", NO_RESULTS

        # ۳) جمع‌آوری عکس‌هایِ معتبر (حداکثر IMAGE_COUNT) از بین نامزدها،
        #    با دانلودِ هم‌زمان (موازی) تا سریع تمام شود.
        #    اگر یک URL خراب/نامعتبر باشد، کل درخواست شکست نمی‌خورد؛ رد می‌شود
        #    و لینکِ بعدی (موازی) بررسی می‌شود. فقط عکسِ واقعاً معتبر (bytes
        #    درست) نگه داشته می‌شود تا لینکِ شکسته ارسال نشود.
        deadline = time.monotonic() + PROCESS_TIMEOUT
        items = await _download_valid_parallel(bot, urls, IMAGE_COUNT, deadline)

        if not items:
            _log(bot, f"PHOTO DOWNLOAD FAILED chat_id={chat_id} user_id={user_id} "
                      f"query={query!r} no valid image (all links failed)")
            close_session(chat_id, user_id)
            return "error", ERROR_MSG

        # ۴) ارسالِ همهٔ عکس‌هایِ معتبر با هم (آلبوم) — نه جدا جدا.
        #    ترتیب: آپلودِ آلبوم → تلاشِ مجدد با reconnect → لینکِ مستقیمِ
        #    فقطِ عکس‌هایِ معتبر (در یک پیام). اگر هیچ‌کدام نشد → خطا.
        delivered = 0
        if await _upload_album(bot, chat_id, items):
            delivered = len(items)
        elif await _upload_album(bot, chat_id, items, force_reconnect=True):
            delivered = len(items)
        else:
            # fallback: لینکِ عکس‌هایِ معتبر در یک پیام (هرگز لینکِ شکسته)
            _log(bot, f"PHOTO FALLBACK to direct links count={len(items)}")
            try:
                await asyncio.wait_for(
                    _send_links_together(bot, chat_id, items), timeout=LINK_TIMEOUT)
                _log(bot, "PHOTO SEND OK method=direct_links")
                delivered = len(items)
            except asyncio.TimeoutError:
                _log(bot, f"PHOTO SEND TIMEOUT method=direct_links "
                          f"timeout={LINK_TIMEOUT}s")
            except Exception:
                _log_exception(bot, "PHOTO SEND ERROR method=direct_links")

        if delivered == 0:
            close_session(chat_id, user_id)
            return "error", ERROR_MSG

        # ۵) کسرِ سکه — فقط بعد از موفقیتِ واقعیِ ارسال.
        #    هزینه = ۱۰ برنز به‌ازای هر عکسِ ارسال‌شده (۱ عکس = ۱۰، ۲ عکس = ۲۰).
        #    مالکِ اصلی (osine2) برایِ تست بدونِ کسرِ سکه استفاده می‌کند.
        cost = 0
        if _is_main_owner(user_id):
            _log(bot, f"PHOTO OWNER TEST chat_id={chat_id} user_id={user_id} "
                      f"delivered={delivered} (no charge)")
        else:
            cost = COST_PER_IMAGE * delivered
            try:
                economy.spend(
                    chat_id, user_id, cost, economy.BRONZE,
                    reference=f"photo_download:{chat_id}:{user_id}:{int(time.time())}",
                    note=f"دانلود عکس ({delivered} تصویر)",
                )
            except Exception:
                _log_exception(bot, f"PHOTO DOWNLOAD FAILED (spend) chat_id={chat_id} "
                                    f"user_id={user_id} cost={cost}")
                close_session(chat_id, user_id)
                return "error", ERROR_MSG

        close_session(chat_id, user_id)
        return "done", f"✅ {delivered} تصویر ارسال شد. {cost} سکه برنز کسر شد."
    except Exception:
        # لایهٔ نهایی: هر خطایِ پیش‌بینی‌نشده را با traceback ثبت کن و به‌جایِ
        # پیامِ عمومیِ خام، علت را در لاگ نگه دار (بدون این لاگ نمی‌شود فهمید
        # مشکل از جستجو/لینک/دانلود/ارسال است).
        _log_exception(bot, f"PHOTO DOWNLOAD FAILED (unexpected) "
                            f"chat_id={chat_id} user_id={user_id} query={query!r}")
        close_session(chat_id, user_id)
        return "error", ERROR_MSG
    finally:
        clear_processing(chat_id, user_id)
        _release_busy(chat_id, user_id)


def _release_busy(chat_id, user_id):
    """آزادسازی قفل/صف — همیشه در finally صدا زده می‌شود."""
    _BUSY_GROUPS.discard(chat_id)
    _BUSY_USERS.discard((chat_id, user_id))
    lock = _GROUP_LOCKS.get(chat_id)
    if lock is not None and lock.locked():
        try:
            lock.release()
        except Exception:
            pass
