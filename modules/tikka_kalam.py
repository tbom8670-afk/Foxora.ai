# -*- coding: utf-8 -*-
"""🎭 «تیکه کلام» — جمله‌های طنز/کنایه، با توزیع تصادفیِ بدون تکرار.

قرارداد کارفرما:

* با نوشتن «تیکه کلام» یک جملهٔ تصادفی از این فهرست فرستاده می‌شود.
* **برای یک کاربر تکرار نمی‌شود:** هر کاربر یک «دستهٔ بُر» (deck) از همهٔ
  جمله‌ها دارد؛ تا وقتی همهٔ جمله‌ها را نگرفته، هیچ جمله‌ای برایش تکرار
  نمی‌شود. وقتی دسته خالی شد، دوباره به‌طور تصادفی بُر می‌خورد.
* **هم‌زمان برای دو نفر تکرار نمی‌شود:** جمله‌ای که در «پنجرهٔ تازگی»
  (پیش‌فرض ۱۵ دقیقه) به کسی داده شده، به کاربر دیگری داده نمی‌شود؛ اگر
  همهٔ گزینه‌های باقی‌ماندهٔ یک کاربر تازه باشند، قدیمی‌ترین‌شان انتخاب
  می‌شود تا همچنان «بدون تکرار برای همان کاربر» حفظ شود.
* تصادفی‌بودن واقعی است: ترتیب هر دسته با `random.SystemRandom` بُر می‌خورد.
* وضعیت روی دیسک ذخیره می‌شود (فایل JSON در پوشهٔ دادهٔ همین نمونهٔ ربات)،
  پس بعد از ری‌استارت هم حافظهٔ «چه کسی چه چیزی گرفته» از دست نمی‌رود.
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

from modules.atomic_write import write_json

TIKKAS = (
    "برات چهار جلسه نوبت چشم پزشکی گرفتم",
    "ببینم چی تو خودت دیدی که ما ندیدیم",
    "گیریم که اینستا و تلگرامت وصله",
    "با ذات روبیکاییت می‌خای چیکار کنی",
    "برات صندلی گذاشتن تو جلسه ای که هیچکس دعوتت نکرده",
    "روی جلدت نوشته بودن باز نکنید اما اشتباهی بازت کردم",
    "واست اینه آوردم ولی می‌ترسم بهت بدمش اینه هم شرمنده بشه",
    "تا دیروز تو شهر فرش بودی حالا تاجر فرش",
    "اونقدر عکس فیک گذاشتی از خودت وقتی اینترنت قطع بشه هویت خودتو گم می‌کنی",
    "بیوت نوشتی خودت باش ولی کل پیجت شده یکی دیگه",
    "سگی که زیاد کتک خورده باشه رو وقتی واسه اولین بار ناز کنی گازت می‌گیره",
    "کد تایید اکانتت رو گفتی اما هنوز پیش ما تایید خودت رو نه",
    "تو کال میکروفن از شدت ادعات می‌سوزه",
    "تو همونی که تو کپشن می‌نویسی ساده باش ولی ۴۰ تا فیلتر رو عکساته",
    "با سیم کارت اعتباری دنبال اعتبار تو مجازی هستی ؟",
    "سلامتی فروشنده ها اشتباه نشه\nرفیقامو می‌گم",
    "تو توی وان حمومتون ناخدایی",
    "تو دور خودتو سیم خاردار بکش بوست نکنن خوشگله 😌",
    "مارو دیدی زیارتت قبول\nاگرم ندیدی لیاقتت نبود",
    "فکر کن خیلی بالایی !!\nطوفان فقط اشغال هارو بلند می‌کنه",
    "بهت محل ندادم رفتی به یه محل دادی",
    "به جدم نیستی در حدم بده شیش 😀",
    "همینکه حالم خوشه\nبرای خیلیا فوشه",
    "اشکال نداره اگه ازم خوشت نمیاد همه که خوش سلیقه نیستن",
    "اول ببین قیافشو داری که قیافه بگیری ؟",
    "جواب دندون شکن زیاد دارم اما می‌ترسم به جای دندونات دلت بشکنه",
    "آخر دورم زد کسی که می‌گفت دورت بگردم",
    "جنس ارزون مشتری زیاد داره با ارزش بودی مال همه نبودی",
    "یه جوری سوختی که هنوز داری دود پس میدی",
    "دامپزشکی نخوندم که بفهمم چی میگی",
    "هرکی بود تو راهت بغل کردی بچه تو بگو بازم غلط کردی",
    "من با کسی دشمنی نمی‌کنم اما دشمنامو یکی یکی می‌کنم",
    "فاز لاتی برمی‌داری توی که صدات برامون لالایه",
    "زندگی یه پانتومیم هست حرف دلتو به زبون بیاری باختی",
    "اگه مهم بودی زیرت خط می‌کشیدم نه دورت",
    "میری کافه لاته سفارش میدی خوب لات می‌زارن؟",
    "قیافه ریالی ادعات دلاری ؟",
    "گفت بی من چی می‌کشی گفتم نفس راحت",
    "بده شیصدو شصتو شیش بشین روش برو کیش",
    "از هرجام بیوفتی می‌گیرمت جز چشمام",
    "کسی بهت گفت دوست دارم برو تحقیق کن ببین یه وقت تیکه کلامش نباشه",
    "درد داری ؟ حق داری",
    "گوزی بود راهی داشت آیا با تو کاری داشت ؟",
    "قیافتو توی مزایده با تخفیف ۱۰۰٪ هم نمی‌خرن",
    "هزارپا با هزارتا پا به کسی پا نداد\nاما تو با دوتا پا به هزار نفر دادی",
    "هالویین؟ نه ممنون ذاتت کافی بود",
    "تفریح همه شده خیانت",
    "توپ فوتبال هم اینقدر پاس کاری نشد که تو شدی",
    "زیر ولنجک کنسله مال اونایه که تو پراید مادر شدن",
    "نه تو شاخی نه دوستات\nبرگرد برو به روستات",
    "اعتماد حرامست به گفته امامست",
    "واسه پول بجنگید بقیه رو با پول می‌شه خرید",
    "ادعای شعور ولی اولین حرکتن بیشعوری",
    "از بس تو خیانت هاش گفت ای کاش کمتر گذاشتن لاش",
    "منتظر سقوط منی ؟ بشین تو فرودگاه تا کشتی بیاد",
    "من براش موسی بودم اما اون نوح دوست داشت",
    "صد تو حتی به صفر من هم نمی‌رسه",
    "حسابداری نخوندم که حسابت کنم",
    "میکانیک نیستم اما می‌دونم داخلت چقدر خرابه",
    "چوپان هم دورش شلوغه ولی همه گوسفندن",
    "خاستی از زندگیم بری وایسا اسنپ بگیرم زشته پیاده بری",
    "از چشام بیوفتی آسمون هم باشی سرمو بلند نمی‌کنم واسه نگاه کردنت",
    "از کی دو ریالی فکر می‌کنه تراوله ؟",
    "با یه اشتباه پیشم ۱۹ نمی‌شی صفر می‌شی",
    "هیچ وقت گرسنه نمی‌شی چون همش حرص منو می‌خوری",
    "نوازنده زندگیت باش نه رقصنده زندگی بقیه",
    "ما غمشو خوردیم خوشگلا لبشو",
    "به من می‌گفت بی تو قلبم می‌گیره\nبه اون می‌گفت تف بزن دردم نگیره",
    "اینقدر کلاس می‌زاری زنگ تفریح هم داری ؟",
    "ادما رو با گردنش زمان بشناس نه گردش زبان",
    "خدا سرت رو آفرید تا روی گردنت باشه نه توی زندگی دیگران",
    "فقط به درد ماهیگیری می‌خوری از بس کرم داری",
    "ریال به دلار نمی‌رسه توام به من",
    "سنگ های که انداختی جلوی پای ما بزار توی جیبت یکم سنگین باشی",
    "اینقدر شکستنم که وقتی تیکه هامو وصل کردم یه آدم دیگه شدم",
    "از چشم من افتادی و نقش زمینی\nپا می‌خوری؛ الحق که سزاوار همینی",
    "به درد رفاقت نمی‌خوری چه برسه رقابت",
    "مثل پاکت چیپس نصفت باد هوا بود",
)

TOTAL = len(TIKKAS)

# جمله‌ای که در این مدت (ثانیه) به کسی داده شده، به کاربر دیگری داده نمی‌شود.
FRESH_SECONDS = 15 * 60
_MAX_RECENT = 400

_LOCK = threading.RLock()
_STATE = {}          # در حافظه بالا آمده از دیسک
_LOADED = [False]
_OVERRIDE_FILE = [None]   # فقط برای تست/استقرار صریح
_RNG = random.SystemRandom()

_FILE_ENV = "TIKKA_KALAM_FILE"
_FILE_NAME = "tikka_kalam.json"


# ---------------------------------------------------------------------------
# مسیر فایل وضعیت
# ---------------------------------------------------------------------------
def state_path() -> Path:
    """فایل وضعیت توزیع تیکه‌ها (داخل پوشهٔ دادهٔ همین نمونهٔ ربات)."""
    if _OVERRIDE_FILE[0] is not None:
        return Path(_OVERRIDE_FILE[0])
    import os
    raw = (os.environ.get(_FILE_ENV, "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from modules.runtime_paths import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:                                   # pragma: no cover
        base = Path(__file__).resolve().parents[1] / "data"
    return base / _FILE_NAME


def set_state_path(path=None):
    """تعیین مسیر فایل وضعیت (تست). ``None`` یعنی مسیر پیش‌فرض نمونه."""
    with _LOCK:
        _OVERRIDE_FILE[0] = None if path is None else Path(path).expanduser()
        _STATE.clear()
        _LOADED[0] = False


def _ensure_loaded():
    if _LOADED[0]:
        return
    _LOADED[0] = True
    path = state_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            users = data.get("users")
            if isinstance(users, dict):
                _STATE["users"] = {str(key): value for key, value in users.items()
                                   if isinstance(value, dict)}
            recent = data.get("recent")
            if isinstance(recent, list):
                _STATE["recent"] = [row for row in recent
                                    if isinstance(row, list) and len(row) == 2]
    except FileNotFoundError:
        pass
    except Exception:
        # فایل خراب هرگز بازی را نمی‌خواباند؛ از نو شروع می‌کنیم.
        pass
    _STATE.setdefault("users", {})
    _STATE.setdefault("recent", [])


def _persist():
    data = {
        "version": 1,
        "total": TOTAL,
        "saved_at": time.time(),
        "users": _STATE.get("users", {}),
        "recent": _STATE.get("recent", [])[-_MAX_RECENT:],
    }
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# توزیع
# ---------------------------------------------------------------------------
def _user_key(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"


def _new_deck(exclude=()) -> list:
    deck = list(range(TOTAL))
    _RNG.shuffle(deck)
    excluded = set(exclude)
    if excluded and len(excluded) < TOTAL:
        # جمله‌ای که همین حالا به کاربر دیگری داده شده، ته دسته می‌رود.
        deck = [index for index in deck if index not in excluded] + \
               [index for index in deck if index in excluded]
    return deck


def _fresh_indices(now) -> set:
    fresh = set()
    cutoff = now - FRESH_SECONDS
    recent = []
    for row in _STATE.get("recent", []):
        try:
            index, stamp = int(row[0]), float(row[1])
        except Exception:
            continue
        if stamp >= cutoff:
            recent.append([index, stamp])
            fresh.add(index)
    _STATE["recent"] = recent[-_MAX_RECENT:]
    return fresh


def next_tikka(chat_id, user_id, now=None) -> str:
    """جملهٔ بعدی برای این کاربر (بدون تکرار برای او، بدون تکرار هم‌زمان)."""
    with _LOCK:
        _ensure_loaded()
        now = time.time() if now is None else float(now)
        users = _STATE["users"]
        key = _user_key(chat_id, user_id)
        record = users.get(key)
        if not isinstance(record, dict):
            record = {"deck": _new_deck(), "draws": 0}
            users[key] = record
        deck = record.get("deck")
        if not isinstance(deck, list) or not deck:
            deck = _new_deck()
        deck = [int(index) for index in deck
                if isinstance(index, int) and 0 <= index < TOTAL]
        if not deck:
            deck = _new_deck()

        fresh = _fresh_indices(now)
        choice_at = next((position for position, index in enumerate(deck)
                          if index not in fresh), None)
        if choice_at is None:
            # همهٔ باقی‌ماندهٔ دسته تازه‌اند → قدیمی‌ترینِ آن‌ها (بدون تکرار
            # برای همین کاربر، حتی اگر برای دیگری تازه باشد).
            stamps = {index: stamp for index, stamp in _STATE.get("recent", [])}
            oldest = min(range(len(deck)), key=lambda pos: stamps.get(deck[pos], 0))
            choice_at = oldest

        index = deck.pop(choice_at)
        record["deck"] = deck
        record["draws"] = int(record.get("draws", 0)) + 1
        record["last"] = index
        record["last_at"] = now

        _STATE.setdefault("recent", []).append([index, now])
        _STATE["recent"] = _STATE["recent"][-_MAX_RECENT:]
        _persist()
        return TIKKAS[index]


def stats(chat_id, user_id) -> dict:
    """وضعیت توزیع برای این کاربر (برای تست/گزارش)."""
    with _LOCK:
        _ensure_loaded()
        record = _STATE["users"].get(_user_key(chat_id, user_id)) or {}
        return {
            "total": TOTAL,
            "draws": int(record.get("draws", 0)),
            "remaining": len(record.get("deck") or []),
            "recent": len(_STATE.get("recent") or []),
        }


def reset_all():
    """پاک‌کردن وضعیت (فقط تست/مدیریت)."""
    with _LOCK:
        _STATE.clear()
        _STATE["users"] = {}
        _STATE["recent"] = []
        _LOADED[0] = True
        try:
            state_path().unlink()
        except Exception:
            pass


def help_line() -> str:
    return "🎭 تیکه کلام:"
