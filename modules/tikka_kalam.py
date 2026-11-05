# -*- coding: utf-8 -*-
"""🎭 «تیکه کلام» — جمله‌های طنز/کنایه، با توزیع تصادفیِ بدون تکرار.

قرارداد کارفرما:

* با نوشتن «تیکه کلام» یک جملهٔ تصادفی از این فهرست فرستاده می‌شود.
* **برای یک کاربر هرگز تکرار نمی‌شود:** برای هر (گروه، کاربر) فهرست
  «همهٔ متن‌هایی که تا حالا گرفته» روی دیسک نگه داشته می‌شود. تا وقتی حتی
  یک جمله از فهرست برای او باقی مانده، همان را می‌گیرد؛ **ری‌استارت ربات،
  ریست حافظه یا صدها بار پرسیدن هم این حافظه را پاک نمی‌کند.** فقط وقتی
  همهٔ جمله‌ها برای همان کاربر گفته شده باشد، دورهٔ بعدی (با بُر تازه)
  شروع می‌شود.
* **هم‌زمان برای دو نفر تکرار نمی‌شود:** جمله‌ای که در «پنجرهٔ تازگی»
  (پیش‌فرض ۱۵ دقیقه) به کسی داده شده، به کاربر دیگری داده نمی‌شود؛ اگر
  همهٔ گزینه‌های باقی‌ماندهٔ یک کاربر تازه باشند، قدیمی‌ترین‌شان انتخاب
  می‌شود تا همچنان «بدون تکرار برای همان کاربر» حفظ شود.
* تصادفی‌بودن واقعی است: ترتیب هر دسته با `random.SystemRandom` بُر می‌خورد.
* وضعیت روی دیسک ذخیره می‌شود (فایل JSON در پوشهٔ دادهٔ همین نمونهٔ ربات)
  و شامل تاریخچهٔ کامل «چه کسی چه جمله‌هایی گرفته» است؛ همان فایل، حافظهٔ
  دائمی ربات در طول پیام‌ها است.
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
    "تو تاکسیک نبودی ؛ تاکسی بودی عزیزم",
    "قولاتو گذاشتی زیر پات قدت بلند شده ؟",
    "مثل عکست شدی ؛ بزرگت کردم بی کیفیت شدی",
    "تنها کسی که باهام سرد بودنش روم تاثیر داشت کولر بود",
    "گریه نمی‌کنم برات شرمنده ریملم گرونه",
    "جایگزین کردن ما آسونه ولی یکی پیدا کردن مثل ما سخت",
    "توهم پرونده داری اما تو مدرسه",
    "قلبت بوی پا گرفته از بس توش رفتن اومدن",
    "همه ویژگی های یه سگ رو داری جز وفاداری",
    "چشم دیدن مارو نداری؟ آب هویج بخور",
    "اگه اذیتم کنین یه جوری به زن داییم میگم براتون دعا بنویسه که ابلیس هم نتونه باطلش کنه",
    "بی فایده ای مثل هشدار روی پاکت سیگار",
    "آرامشی که الان دارم مدیون رلیه که ندارم",
    "عین آسانسور شدی همه دکمتو می‌زنن",
    "تو که مارو فروختی اما خوشبحال خریدار",
    "مثل برنج همه خیست کردن 😝",
    "برو با اکسات تیم فوتبال بزن اینجا از وفاداری صحبت نکن",
    "دور سطل اشغال از ذاتت پاک تره",
    "به مار عسل دادن ولی باز زهر داد بیرون",
)

TOTAL = len(TIKKAS)

# جمله‌ای که در این مدت (ثانیه) به کسی داده شده، به کاربر دیگری داده نمی‌شود.
FRESH_SECONDS = 15 * 60
_MAX_RECENT = 400
_HISTORY_CAP = 50           # سقف تاریخچهٔ کوتاه هر کاربر (حجم فایل)
_MAX_USERS = 5000           # سقف کاربران ردیابی‌شده (پیرترین‌ها هرس می‌شوند)

_LOCK = threading.RLock()
_STATE = {"users": {}, "recent": []}
_LOADED = [False]
_OVERRIDE_FILE = [None]     # فقط برای تست/استقرار صریح
_RNG = random.SystemRandom()

_FILE_ENV = "TIKKA_KALAM_FILE"
_FILE_NAME = "tikka_kalam.json"
STATE_VERSION = 2


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
        _STATE.update({"users": {}, "recent": []})
        _LOADED[0] = False


def _clean_indices(values):
    out = []
    for value in values or []:
        try:
            index = int(value)
        except Exception:
            continue
        if 0 <= index < TOTAL:
            out.append(index)
    return out


def _ensure_loaded():
    if _LOADED[0]:
        return
    _LOADED[0] = True
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = None
    except Exception:
        # فایل خراب هرگز ربات را نمی‌خواباند؛ ولی پاک هم نمی‌شود.
        data = None
    if isinstance(data, dict):
        users = data.get("users")
        if isinstance(users, dict):
            for key, value in users.items():
                if isinstance(value, dict):
                    _STATE["users"][str(key)] = value
        recent = data.get("recent")
        if isinstance(recent, list):
            _STATE["recent"] = [row for row in recent
                                if isinstance(row, list) and len(row) == 2]
    _STATE.setdefault("users", {})
    _STATE.setdefault("recent", [])


def _prune_users():
    """هرس کاربران قدیمی تا فایل وضعیت برای همیشه سبک بماند."""
    users = _STATE.get("users") or {}
    if len(users) <= _MAX_USERS:
        return
    def _stamp(item):
        record = item[1] if isinstance(item[1], dict) else {}
        try:
            return float(record.get("last_at") or 0)
        except Exception:
            return 0.0
    keep = sorted(users.items(), key=_stamp, reverse=True)[:_MAX_USERS]
    _STATE["users"] = dict(keep)


def _persist():
    _prune_users()
    data = {
        "version": STATE_VERSION,
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
        # جمله‌های تازه‌داده‌شده به کاربر دیگری، ته دسته می‌روند.
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


def _record_for(chat_id, user_id) -> dict:
    users = _STATE["users"]
    key = _user_key(chat_id, user_id)
    record = users.get(key)
    if not isinstance(record, dict):
        record = {"cycle": 1, "used": [], "deck": _new_deck(),
                  "draws": 0, "history": []}
        users[key] = record
    # سازگاری با نسخهٔ قدیمی (که فقط deck/draws داشت)
    used = set(_clean_indices(record.get("used")))
    deck = _clean_indices(record.get("deck"))
    if not used and deck:
        used = {index for index in range(TOTAL) if index not in set(deck)}
    record["used"] = sorted(used)
    record["deck"] = [index for index in dict.fromkeys(deck) if index not in used]
    record.setdefault("cycle", 1)
    record.setdefault("draws", 0)
    if not isinstance(record.get("history"), list):
        record["history"] = []
    return record


def next_tikka(chat_id, user_id, now=None) -> str:
    """جملهٔ بعدی برای این (گروه، کاربر) — هرگز تکرار تا اتمام کل فهرست."""
    with _LOCK:
        _ensure_loaded()
        now = time.time() if now is None else float(now)
        record = _record_for(chat_id, user_id)
        used = set(record["used"])
        deck = list(record["deck"])

        if len(used) >= TOTAL:
            # همهٔ متن‌ها برای همین کاربر گفته شده → تنها حالت مجاز تکرار:
            # شروع دورهٔ تازه با بُر کامل و تصادفیِ جدید.
            record["cycle"] = int(record.get("cycle", 1)) + 1
            used = set()
            deck = _new_deck()
        if not deck:
            deck = _new_deck(exclude=used)

        fresh = _fresh_indices(now)
        choice_at = next(
            (position for position, index in enumerate(deck)
             if index not in used and index not in fresh), None)
        if choice_at is None:
            choice_at = next(
                (position for position, index in enumerate(deck)
                 if index not in used), None)
        if choice_at is None:
            deck = _new_deck(exclude=used)
            choice_at = 0

        index = deck.pop(choice_at)
        used.add(index)
        record["used"] = sorted(used)
        record["deck"] = deck
        record["draws"] = int(record.get("draws", 0)) + 1
        record["last"] = index
        record["last_at"] = now
        history = record["history"]
        history.append([index, now])
        del history[:-_HISTORY_CAP]

        _STATE.setdefault("recent", []).append([index, now])
        _STATE["recent"] = _STATE["recent"][-_MAX_RECENT:]
        _persist()
        return TIKKAS[index]


def history(chat_id, user_id, limit=None) -> list:
    """جمله‌هایی که ربات تا حالا به این کاربر در این گروه گفته است."""
    with _LOCK:
        _ensure_loaded()
        record = _STATE["users"].get(_user_key(chat_id, user_id)) or {}
        rows = []
        for entry in record.get("history") or []:
            try:
                index = int(entry[0])
            except Exception:
                continue
            if 0 <= index < TOTAL:
                rows.append(TIKKAS[index])
        return rows[-limit:] if limit else rows


def stats(chat_id, user_id) -> dict:
    """وضعیت توزیع برای این کاربر (برای تست/گزارش)."""
    with _LOCK:
        _ensure_loaded()
        record = _record_for(chat_id, user_id)
        used = set(record.get("used") or [])
        return {
            "total": TOTAL,
            "draws": int(record.get("draws", 0)),
            "used": len(used),
            "remaining": max(0, TOTAL - len(used)),
            "cycle": int(record.get("cycle", 1)),
            "recent": len(_STATE.get("recent") or []),
        }


def reset_all():
    """پاک‌کردن وضعیت — فقط برای تست. ربات واقعی هرگز این را صدا نمی‌زند."""
    with _LOCK:
        _STATE.clear()
        _STATE.update({"users": {}, "recent": []})
        _LOADED[0] = True
        try:
            state_path().unlink()
        except Exception:
            pass


def help_line() -> str:
    return "🎭 تیکه کلام:"
