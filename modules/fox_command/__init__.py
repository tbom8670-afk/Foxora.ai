# -*- coding: utf-8 -*-
"""🦊 مرکز فرماندهی روباه (FOX COMMAND) — تعادل، تجهیزات و قالب‌بندی.

این فایل **تنها جای اعداد بازی** است: قیمت‌ها، قدرت اسلحه‌ها، درصد رهگیری،
خسارت‌ها و متن‌ها. هیچ محاسبه‌ای اینجا انجام نمی‌شود (آن کار ``engine``
است) تا منطق و اعداد از هم جدا بمانند و تنظیم تعادل یک‌جا قابل تغییر باشد.

قیمت‌ها همان قیمت‌های قطعی سفارش کارفرما هستند و بدون اجازهٔ صریح تغییر
نمی‌کنند (``test_fox_command.py`` همین را قفل می‌کند).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# قالب‌بندی سروش پلاس — همان ساختار موجود پروژه، بدون هیچ Markdown
# ---------------------------------------------------------------------------
try:  # pragma: no cover - بسته به نصب کلاینت
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:  # pragma: no cover
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


def u16(text) -> int:
    """طول رشته با واحد UTF-16 (همان واحدی که entityهای سروش پلاس می‌شناسند)."""
    return len(str(text or "").encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# پیکربندی بازی
# ---------------------------------------------------------------------------
GAME_NAME = "مرکز فرماندهی روباه"
COMMAND = "مرکز فرماندهی"
COMMAND_ALT = "مرکز فرماندهی روباه"
COMMAND_EN = "fox command"
JOIN_WORD = "شرکت"

PLAYERS = 2
START_BRONZE = 400          # بودجهٔ کل بازی (نه برای هر فروشگاه)
JOIN_SECONDS = 60
DEFENSE_SECONDS = 17        # مهلت واقعی دفاع
TURN_SECONDS = 30           # مهلت هر انتخاب
MAX_ACTIVE_ATTACKS = 4      # بیش از این، حملهٔ هم‌زمان پذیرفته نمی‌شود
WAR_IDLE_SECONDS = 180      # بی‌تحرکی طولانی در جنگ → پایان با امتیاز
WINNER_SILVER = 10
START_HEALTH = 100
START_POPULATION = 10_000

# تانک/ظرفیت تجهیزات
MAX_OFFENSIVE = 3           # بالستیک + کروز با هم
MAX_DEFENSE_SYSTEMS = 3     # سامانه‌های دفاعی
MAX_RADARS = 2
MAX_NUKES = 1               # فقط یک سلاح هسته‌ای در هر مسابقه
NUKES_PER_GAME = 1          # هر فرمانده فقط یک بار می‌تواند شلیک هسته‌ای کند
NUKES_PER_ROUND = 1         # و در هر دور فقط یک هسته‌ای

# ارزان‌ترین موشک تهاجمی — برای تشخیص «فرماندهٔ بدون توان جنگ»
CHEAPEST_OFFENSIVE = 20

# اقتصاد
REPAIR_PER_POINT = 3        # هر ۱٪ سلامت = ۳ برنز
REPAIR_MAX_PER_ORDER = 20   # سقف هر دستور تعمیر (٪)
REPAIR_COOLDOWN = 20        # ثانیه بین دو تعمیر همان فرمانده

COUNTRIES = {
    "iran": ("ایران", "🇮🇷", "جمهوری اسلامی ایران"),
    "russia": ("روسیه", "🇷🇺", "فدراسیون روسیه"),
    "usa": ("آمریکا", "🇺🇸", "ایالات متحده آمریکا"),
    "china": ("چین", "🇨🇳", "جمهوری خلق چین"),
}
COUNTRY_ORDER = ("iran", "russia", "usa", "china")

# ---------------------------------------------------------------------------
# تجهیزات — قیمت‌ها قطعی و دست‌نخوردنی
# ---------------------------------------------------------------------------
CATEGORY_BALLISTIC = "ballistic"
CATEGORY_CRUISE = "cruise"
CATEGORY_DEFENSE = "defense"
CATEGORY_RADAR = "radar"
CATEGORY_NUKE = "nuke"

CATEGORY_LABELS = {
    CATEGORY_BALLISTIC: "فروشگاه موشک‌های بالستیک 🚀",
    CATEGORY_CRUISE: "فروشگاه موشک‌های کروز 🚀",
    CATEGORY_DEFENSE: "فروشگاه سامانه‌های دفاعی 🛡",
    CATEGORY_RADAR: "فروشگاه رادارها 📡",
    CATEGORY_NUKE: "فروشگاه سلاح‌های هسته‌ای ☢️",
}

#: ``id → dict`` — تمام مشخصات مستقل هر تجهیز در همین‌جا زندگی می‌کند.
WEAPONS = {
    # ---------------- بالستیک (قیمت قطعی) ----------------
    "khorramshahr4": dict(
        name="خرمشهر ۴", category=CATEGORY_BALLISTIC, price=100,
        power=100, hit=0.80, damage=60.0, casualties=0.045, intercept=0.78),
    "sejjil": dict(
        name="سجیل", category=CATEGORY_BALLISTIC, price=70,
        power=78, hit=0.72, damage=39.0, casualties=0.035, intercept=0.85),
    "kheibar": dict(
        name="خیبرشکن", category=CATEGORY_BALLISTIC, price=60,
        power=70, hit=0.68, damage=33.0, casualties=0.030, intercept=0.88),
    "emad": dict(
        name="عماد", category=CATEGORY_BALLISTIC, price=55,
        power=66, hit=0.72, damage=30.0, casualties=0.050, intercept=0.88),
    "minuteman3": dict(
        name="مینوتمن ۳ آمریکا", category=CATEGORY_BALLISTIC, price=100,
        power=98, hit=0.80, damage=60.0, casualties=0.040, intercept=0.78),
    "pershing": dict(
        name="پرشینگ آمریکا", category=CATEGORY_BALLISTIC, price=80,
        power=76, hit=0.75, damage=46.0, casualties=0.032, intercept=0.84),
    "trident2": dict(
        name="ترایدنت ۲ آمریکا", category=CATEGORY_BALLISTIC, price=110,
        power=112, hit=0.82, damage=68.0, casualties=0.050, intercept=0.74),
    # ---------------- کروز (ارزان، دقیق، کم‌خسارت) ----------------
    "paveh": dict(
        name="پاوه", category=CATEGORY_CRUISE, price=20,
        power=30, hit=0.78, damage=9.0, casualties=0.010, intercept=0.95),
    "sommar": dict(
        name="سومار", category=CATEGORY_CRUISE, price=35,
        power=45, hit=0.80, damage=17.0, casualties=0.015, intercept=0.92),
    "hovyzeh": dict(
        name="هویزه", category=CATEGORY_CRUISE, price=25,
        power=38, hit=0.78, damage=12.0, casualties=0.012, intercept=0.94),
    "tomahawk": dict(
        name="تاماهاوک آمریکا", category=CATEGORY_CRUISE, price=40,
        power=52, hit=0.84, damage=20.0, casualties=0.016, intercept=0.90),
    "agm80": dict(
        name="AGM-80 آمریکا", category=CATEGORY_CRUISE, price=40,
        power=50, hit=0.80, damage=19.5, casualties=0.015, intercept=0.90),
    # ---------------- سامانه‌های دفاعی ----------------
    "mersad": dict(
        name="مرصاد", category=CATEGORY_DEFENSE, price=20,
        intercept=0.18, capacity=3, anti_nuke=0.0),
    "somkhordad": dict(
        name="سوم خرداد", category=CATEGORY_DEFENSE, price=30,
        intercept=0.26, capacity=3, anti_nuke=0.0),
    "panzdahkhordad": dict(
        name="پانزده خرداد", category=CATEGORY_DEFENSE, price=35,
        intercept=0.32, capacity=3, anti_nuke=0.0),
    "s300": dict(
        name="اس ۳۰۰", category=CATEGORY_DEFENSE, price=45,
        intercept=0.40, capacity=3, anti_nuke=0.05),
    "bavar373": dict(
        name="باور ۳۷۳", category=CATEGORY_DEFENSE, price=50,
        intercept=0.46, capacity=4, anti_nuke=0.08),
    "patriot": dict(
        name="پاتریوت PAC-3", category=CATEGORY_DEFENSE, price=55,
        intercept=0.50, capacity=4, anti_nuke=0.10),
    "thaad": dict(
        name="تاد THAAD", category=CATEGORY_DEFENSE, price=60,
        intercept=0.55, capacity=4, anti_nuke=0.12),
    "aegis": dict(
        name="ایجیس", category=CATEGORY_DEFENSE, price=55,
        intercept=0.48, capacity=4, anti_nuke=0.10),
    "gmd": dict(
        name="جی ام دی آمریکا", category=CATEGORY_DEFENSE, price=65,
        intercept=0.58, capacity=5, anti_nuke=0.15),
    # ---------------- رادارها ----------------
    "radar_basic": dict(
        name="رادار معمولی", category=CATEGORY_RADAR, price=10,
        detect=0.10, counter=0.02),
    "radar_advanced": dict(
        name="رادار پیشرفته", category=CATEGORY_RADAR, price=20,
        detect=0.20, counter=0.04),
    "radar_early": dict(
        name="رادار هشدار زودهنگام", category=CATEGORY_RADAR, price=30,
        detect=0.30, counter=0.06),
    "radar_super": dict(
        name="رادار فوق‌پیشرفته", category=CATEGORY_RADAR, price=40,
        detect=0.40, counter=0.08),
    # ---------------- سلاح هسته‌ای ----------------
    "nuke_tactical": dict(
        name="بمب هسته‌ای تاکتیکی", category=CATEGORY_NUKE, price=150,
        power=180, hit=0.90, damage=85.0, casualties=0.120,
        intercept=0.95, fallout=0.10, blowback=0.08),
    "nuke_powerful": dict(
        name="بمب هسته‌ای قدرتمند", category=CATEGORY_NUKE, price=180,
        power=220, hit=0.92, damage=105.0, casualties=0.160,
        intercept=0.96, fallout=0.15, blowback=0.10),
    "nuke_heavy": dict(
        name="کلاهک هسته‌ای سنگین", category=CATEGORY_NUKE, price=210,
        power=260, hit=0.93, damage=125.0, casualties=0.200,
        intercept=0.97, fallout=0.20, blowback=0.12),
    "nuke_super": dict(
        name="کلاهک هسته‌ای فوق‌سنگین", category=CATEGORY_NUKE, price=300,
        power=320, hit=0.95, damage=150.0, casualties=0.260,
        intercept=0.98, fallout=0.25, blowback=0.15),
}

#: فروشگاه‌ها با ترتیب نمایش (۱، ۲، ۳ …)
SHOP = {
    CATEGORY_BALLISTIC: ("khorramshahr4", "sejjil", "kheibar", "emad",
                         "minuteman3", "pershing", "trident2"),
    CATEGORY_CRUISE: ("paveh", "sommar", "hovyzeh", "tomahawk", "agm80"),
    CATEGORY_DEFENSE: ("mersad", "somkhordad", "panzdahkhordad", "s300",
                       "bavar373", "patriot", "thaad", "aegis", "gmd"),
    CATEGORY_RADAR: ("radar_basic", "radar_advanced", "radar_early",
                     "radar_super"),
    CATEGORY_NUKE: ("nuke_tactical", "nuke_powerful", "nuke_heavy",
                    "nuke_super"),
}

# ---------------------------------------------------------------------------
# تنظیمات تعادل — تنها جایی که اعداد قابل تغییرند
# ---------------------------------------------------------------------------
BALANCE = dict(
    # رادار: شانس شناسایی و کاهش دقت مهاجم
    radar_max_counter=0.18,
    # سقف جمعی رهگیری (هیچ دفاعی ۱۰۰٪ موفق نیست)
    intercept_cap=0.92,
    intercept_floor=0.05,
    # پاداش رادار به سامانهٔ دفاعی
    radar_defense_bonus=0.08,
    # رهگیری موفق: خسارت باقی‌مانده (نه صفر — خرده‌ترکش)
    intercepted_damage_ratio=0.15,
    intercepted_casualty_ratio=0.15,
    # شکست دفاع: خسارت کامل
    failed_defense_damage_ratio=1.0,
    # آسیب پایگاه: با افت سلامت، خسارت بیشتر می‌شود (۰ تا ۰.۲۵)
    damage_health_penalty=0.25,
    # جمعیت: تلفات با نسبت جمعیت فعلی مقیاس می‌گیرد
    population_floor_ratio=0.10,
    # عواقب هسته‌ای برای مهاجم
    nuke_min_self_damage=4.0,
    nuke_self_damage_ratio=0.18,
    # تعمیر
    repair_cost_ratio=1.0,
)

# ---------------------------------------------------------------------------
# متن‌ها
# ---------------------------------------------------------------------------
REPAIR_COST_TEXT = (
    f"هر {REPAIR_PER_POINT} برنز = ۱٪ سلامت پایگاه "
    f"(حداکثر {REPAIR_MAX_PER_ORDER}٪ در هر دستور)"
)

WARNING_NUKE = (
    "☢️ هشدار فروشگاه\n"
    "استفاده از سلاح هسته‌ای تصمیمی بسیار پرخطر است و می‌تواند عواقب سنگینی "
    "برای خود فرمانده داشته باشد؛ حتی استفادهٔ نادرست از آن می‌تواند باعث "
    "شکست خودتان شود. عجله نکنید."
)

HELP_LINES = (
    "● پایگاه\n"
    "● فروشگاه\n"
    "● حمله\n"
    "● دفاع\n"
    "● وضعیت پایگاه\n"
    "● گزارش جنگ\n"
    "● تعمیر پایگاه\n"
    "● تسلیم"
)

#: دستورهای درون‌بازی — برای مسیریابی و مستندسازی
COMMANDS = frozenset({
    "پایگاه", "فروشگاه", "حمله", "دفاع", "وضعیت پایگاه",
    "گزارش جنگ", "تعمیر پایگاه", "تسلیم", "انصراف", "بازگشت",
    "خرید", "رادار", "هسته ای", "هسته‌ای",
})


def weapon(item_id):
    """مشخصات یک تجهیز (یا ``None``)."""
    return WEAPONS.get(item_id)


def price_of(item_id) -> int:
    item = WEAPONS.get(item_id)
    return int(item["price"]) if item else 0


def is_nuke(item_id) -> bool:
    item = WEAPONS.get(item_id)
    return bool(item) and item["category"] == CATEGORY_NUKE


def is_offensive(item_id) -> bool:
    item = WEAPONS.get(item_id)
    return bool(item) and item["category"] in (CATEGORY_BALLISTIC, CATEGORY_CRUISE)


def is_defense(item_id) -> bool:
    item = WEAPONS.get(item_id)
    return bool(item) and item["category"] == CATEGORY_DEFENSE


def is_radar(item_id) -> bool:
    item = WEAPONS.get(item_id)
    return bool(item) and item["category"] == CATEGORY_RADAR


def category_of(item_id):
    item = WEAPONS.get(item_id)
    return item["category"] if item else None
