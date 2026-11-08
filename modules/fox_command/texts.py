# -*- coding: utf-8 -*-
"""🦊 متن‌ها و قالب‌بندی پیام‌های «مرکز فرماندهی روباه».

قالب‌بندی با همان ساختار موجود پروژه در سروش پلاس انجام می‌شود:
``MessageEntityBold`` و ``MessageEntityBlockquote`` از
``splusthon.tl.types`` — بدون هیچ Markdown یا تگ HTML.

قاعدهٔ قالب در این فایل:

* **عنوان و نام بخش‌ها** → Bold
* **راهنما، هشدار و توضیح** → Blockquote (شیشه‌ای سروش پلاس)
* فهرست‌ها یکدست و با فاصلهٔ درست؛ هیچ متن خامی/تگی به کاربر نمی‌رسد.
"""
from __future__ import annotations

from modules.fox_command import (
    BALANCE,
    CATEGORY_BALLISTIC,
    CATEGORY_CRUISE,
    CATEGORY_DEFENSE,
    CATEGORY_LABELS,
    CATEGORY_NUKE,
    CATEGORY_RADAR,
    COMMAND,
    COUNTRIES,
    COUNTRY_ORDER,
    DEFENSE_SECONDS,
    HELP_LINES,
    JOIN_WORD,
    PLAYERS,
    REPAIR_COST_TEXT,
    SHOP,
    START_BRONZE,
    START_HEALTH,
    START_POPULATION,
    WARNING_NUKE,
    WEAPONS,
    WINNER_SILVER,
    MessageEntityBlockquote,
    MessageEntityBold,
    u16,
)
from modules.fox_command import engine

BULLET = "●"


# ---------------------------------------------------------------------------
# سازندهٔ قالب: Bold + Blockquote با آفست UTF-16
# ---------------------------------------------------------------------------
class Message:
    """پیام با بخش‌های Bold و Blockquote.

    استفاده::

        msg = Message()
        msg.bold("عنوان")
        msg.line("متن عادی")
        msg.quote("راهنما")
        text, entities = msg.build()
    """

    def __init__(self):
        self._parts = []
        self._entities = []
        self._length = 0

    # --- داخلی ---
    def _append(self, text):
        start = self._length
        self._parts.append(text)
        self._length += u16(text)
        return start

    @property
    def text(self):
        return "".join(self._parts)

    def raw(self, text):
        self._append(str(text))
        return self

    def bold(self, text, *, quote=False):
        text = str(text)
        if not text:
            return self
        start = self._append(text)
        self._entities.append(MessageEntityBold(offset=start, length=u16(text)))
        if quote:
            self._entities.append(
                MessageEntityBlockquote(offset=start, length=u16(text)))
        return self

    def quote(self, text):
        text = str(text)
        if not text:
            return self
        start = self._append(text)
        self._entities.append(
            MessageEntityBlockquote(offset=start, length=u16(text)))
        return self

    def title(self, text):
        """عنوان پیام: Bold، و یک خط خالی بعدش."""
        self.bold(text)
        self._append("\n\n")
        return self

    def line(self, text=""):
        self._append(str(text) + "\n")
        return self

    def blank(self):
        self._append("\n")
        return self

    def bullet(self, text, *, bold=False):
        return self.bold(f"{BULLET} {text}\n") if bold else self.line(f"{BULLET} {text}")

    def numbered(self, index, text):
        return self.line(f"{fa(index)}. {text}")

    def build(self):
        text = self.text.rstrip("\n")
        entities = [e for e in self._entities if getattr(e, "length", 0) > 0
                    and getattr(e, "offset", 0) + getattr(e, "length", 0) <= u16(text)]
        return text, entities


def fa(value) -> str:
    """ارقام فارسی با جداکنندهٔ هزارگان."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    digits = "۰۱۲۳۴۵۶۷۸۹"
    grouped = f"{abs(number):,}"
    out = "".join(digits[int(ch)] if ch.isdigit() else ch for ch in grouped)
    return ("-" + out) if number < 0 else out


def fa_float(value, digits=1) -> str:
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return fa(int(text)) if "." not in text else "".join(
        "۰۱۲۳۴۵۶۷۸۹"[int(c)] if c.isdigit() else c for c in text)


def percent(value) -> str:
    text = fa_float(value, 1)
    return "۰٪" if text in ("0", "۰", "") else f"{text}٪"


# ---------------------------------------------------------------------------
# پیام‌های آغازین
# ---------------------------------------------------------------------------
def start_prompt() -> tuple:
    msg = Message()
    msg.title("مرکز فرماندهی روباه 🦊")
    msg.bold(f"🌟 برای شرکت در بازی بنویسید: {JOIN_WORD}\n")
    msg.bold(f"🌟 ظرفیت بازی: فقط {fa(PLAYERS)} فرمانده\n")
    msg.bold(f"💰 به هر فرمانده {fa(START_BRONZE)} سکه برنز مخصوص بازی داده می‌شود.\n")
    msg.quote(
        "این بودجه کل خرید تجهیزات شماست؛ بین همهٔ فروشگاه‌ها آزادید، "
        "اما مجموع خریدها از بودجه بیشتر نمی‌شود."
    )
    return msg.build()


def joined(players_count, name) -> tuple:
    msg = Message()
    msg.title("ثبت‌نام شد ✅")
    msg.line(f"فرمانده: {name}")
    msg.line(f"ظرفیت: {fa(players_count)} از {fa(PLAYERS)}")
    msg.quote("منتظر فرماندهٔ دوم باشید…")
    return msg.build()


def join_duplicate() -> tuple:
    msg = Message()
    msg.title("قبلاً ثبت‌نام کرده‌اید ⛔")
    msg.quote("هر فرمانده فقط یک بار می‌تواند در این مسابقه شرکت کند.")
    return msg.build()


def join_word_hint() -> tuple:
    """پیام وقتی مسابقه باز است ولی ظرفیت پر نشده و کسی با «مرکز فرماندهی»
    می‌خواهد وارد شود — ثبت‌نام فقط با «شرکت» است."""
    msg = Message()
    msg.title("این مسابقه باز است ⏳")
    msg.bold(f"🌟 برای ثبت‌نام بنویسید: {JOIN_WORD}\n")
    msg.quote("دستور «مرکز فرماندهی» فقط بازی را شروع می‌کند؛ ورود فرمانده‌ها با «شرکت» است.")
    return msg.build()


def join_full() -> tuple:
    msg = Message()
    msg.title("ظرفیت این مسابقه پر است ⛔")
    msg.quote("فقط دو فرمانده وارد هر مسابقه می‌شوند. بعد از پایان این جنگ دوباره تلاش کنید.")
    return msg.build()


def both_joined(match) -> tuple:
    names = "، ".join(p.get("name") or "فرمانده" for p in match.get("players", []))
    msg = Message()
    msg.title("دو فرمانده آماده‌اند 🎖")
    msg.line(f"فرماندهان: {names}")
    msg.quote("قدم بعدی: انتخاب کشور. هر کشور فقط یک بار انتخاب می‌شود.")
    return msg.build()


def country_menu(match, player) -> tuple:
    taken = engine.taken_countries(match)
    msg = Message()
    msg.title("انتخاب کشور")
    for index, key in enumerate(COUNTRY_ORDER, start=1):
        name, flag, _official = COUNTRIES[key]
        mark = " ⛔ انتخاب شده" if key in taken else ""
        msg.line(f"{fa(index)}. {name} {flag}{mark}")
    msg.blank()
    msg.quote("شمارهٔ کشور موردنظر را ارسال کنید. انتخاب کشور قابل تغییر نیست.")
    return msg.build()


def country_saved(match, player) -> tuple:
    msg = Message()
    msg.title(engine.country_title(player))
    msg.line(f"فرمانده: {player.get('name')}")
    msg.quote("حالا تجهیزات را از «فروشگاه» بخرید و پایگاه را آماده کنید.")
    return msg.build()


def need_country_notice() -> tuple:
    msg = Message()
    msg.title("اول کشور را انتخاب کنید ⛔")
    msg.quote("برای دسترسی به فروشگاه و حمله، ابتدا کشور خود را از منوی انتخاب کشور بفرستید.")
    return msg.build()


# ---------------------------------------------------------------------------
# پایگاه
# ---------------------------------------------------------------------------
def base_card(match, player, *, full=False, opponent=None) -> tuple:
    msg = Message()
    msg.title(engine.country_title(player))
    msg.line(f"سلامت پایگاه: {percent(player.get('health'))}")
    msg.line(f"موجودی: {fa(player.get('bronze'))} 💰 برنز")
    msg.line(f"جمعیت: {fa(player.get('population'))} 👥 نفر")
    if full:
        msg.blank()
        msg.bold("تجهیزات و سامانه‌ها\n")
        arsenal = engine.arsenal_list(player)
        if arsenal:
            for item_id, count in arsenal:
                item = WEAPONS[item_id]
                msg.line(f"{BULLET} {item['name']} × {fa(count)}")
        else:
            msg.line(f"{BULLET} هیچ موشکی در انبار نیست")
        systems = engine.defense_list(player)
        msg.blank()
        msg.bold("سامانه‌های دفاعی\n")
        if systems:
            for item_id, capacity in systems:
                item = WEAPONS[item_id]
                msg.line(f"{BULLET} {item['name']} — ظرفیت {fa(capacity)}")
        else:
            msg.line(f"{BULLET} سامانهٔ دفاعی ندارد")
        msg.blank()
        msg.bold("رادار\n")
        msg.line(engine.radar_label(player))
        msg.blank()
        msg.bold("آمار نبرد\n")
        msg.line(f"حملات انجام‌شده: {fa(player.get('attacks_sent'))}")
        msg.line(f"موشک‌های رهگیری‌شده: {fa(player.get('interceptions'))}")
        msg.line(f"حملات موفق دشمن: {fa(player.get('attacks_blocked_by_enemy'))}")
        msg.line(f"خسارت دریافت‌شده: {percent(player.get('damage_taken'))}")
        msg.line(f"خرید انجام‌شده: {fa(player.get('spent'))} برنز")
        if opponent is not None:
            msg.blank()
            msg.bold("وضعیت فرماندهٔ مقابل\n")
            msg.line(f"{engine.country_title(opponent)}")
            msg.line(f"سلامت پایگاه: {percent(opponent.get('health'))}")
            msg.line(f"جمعیت: {fa(opponent.get('population'))}")
    msg.blank()
    msg.bold("دستورات فرمانده\n")
    msg.raw(HELP_LINES)
    return msg.build()


def not_ready_notice() -> tuple:
    msg = Message()
    msg.title("جنگ هنوز شروع نشده ⛔")
    msg.quote("اول هر دو فرمانده باید کشور را انتخاب و تجهیزات را خریداری کنند.")
    return msg.build()


def closed_notice() -> tuple:
    msg = Message()
    msg.title("این مسابقه تمام شده ⛔")
    msg.quote(f"برای مسابقهٔ تازه بنویسید: {COMMAND}")
    return msg.build()


# ---------------------------------------------------------------------------
# فروشگاه
# ---------------------------------------------------------------------------
def shop_home(player) -> tuple:
    msg = Message()
    msg.title("فروشگاه تجهیزات 🛒")
    msg.line(f"موجودی: {fa(player.get('bronze'))} 💰 برنز")
    msg.line(f"موشک‌های خریداری‌شده: {fa(engine.count_offensive(player))} از {fa(3)}")
    msg.line(f"سامانهٔ دفاعی: {fa(engine.count_defense_systems(player))} از {fa(3)}")
    msg.blank()
    msg.bold("دسته‌ها\n")
    msg.line(f"{fa(1)}. موشک‌های بالستیک 🚀")
    msg.line(f"{fa(2)}. موشک‌های کروز 🚀")
    msg.line(f"{fa(3)}. سامانه‌های دفاعی 🛡")
    msg.line(f"{fa(4)}. رادارها 📡")
    msg.line(f"{fa(5)}. سلاح‌های هسته‌ای ☢️")
    msg.blank()
    msg.quote("شمارهٔ دسته را بفرستید. برای پایان خرید بنویسید: انصراف")
    return msg.build()


_CATEGORY_BY_INDEX = {
    1: CATEGORY_BALLISTIC,
    2: CATEGORY_CRUISE,
    3: CATEGORY_DEFENSE,
    4: CATEGORY_RADAR,
    5: CATEGORY_NUKE,
}


def category_of_index(index):
    try:
        return _CATEGORY_BY_INDEX.get(int(index))
    except (TypeError, ValueError):
        return None


def shop_category(player, category) -> tuple:
    msg = Message()
    msg.title(CATEGORY_LABELS[category])
    for index, item_id in enumerate(SHOP[category], start=1):
        item = WEAPONS[item_id]
        owned = int((player.get("arsenal") or {}).get(item_id, 0))
        owned += int((player.get("defense") or {}).get(item_id, 0))
        owned += int((player.get("radars") or {}).get(item_id, 0))
        tail = f" — موجود: {fa(owned)}" if owned else ""
        msg.line(f"{fa(index)}. {item['name']} — {fa(item['price'])} برنز{tail}")
    msg.blank()
    if category == CATEGORY_NUKE:
        msg.quote(WARNING_NUKE)
    elif category == CATEGORY_DEFENSE:
        msg.quote("ظرفیت هر سامانه یعنی چند رهگیری می‌تواند انجام دهد؛ هر رهگیری یک واحد مصرف می‌کند.")
    elif category == CATEGORY_RADAR:
        msg.quote("رادار شانس شناسایی حمله و دقت سامانه‌های دفاعی را بالا می‌برد.")
    else:
        msg.quote("خرید موشک با شلیک آن جدا است؛ موشک خریداری‌شده تا شلیک‌نشدن در انبار می‌ماند.")
    msg.line(f"موجودی شما: {fa(player.get('bronze'))} 💰 برنز")
    msg.line("برای بازگشت بنویسید: بازگشت")
    return msg.build()


def bought(player, item) -> tuple:
    msg = Message()
    msg.title("خرید انجام شد ✅")
    msg.line(f"تجهیز: {item['name']}")
    msg.line(f"هزینه: {fa(item['price'])} برنز")
    msg.line(f"موجودی: {fa(player.get('bronze'))} 💰 برنز")
    msg.quote("برای خرید بعدی همین عدد را بفرستید یا دستهٔ دیگری را انتخاب کنید.")
    return msg.build()


def buy_failed(status, info=None) -> tuple:
    info = info or {}
    msg = Message()
    if status == engine.ST_NO_BUDGET:
        msg.title("موجودی کافی نیست ⛔")
        msg.line(f"قیمت: {fa(info.get('price'))} برنز")
        msg.line(f"موجودی شما: {fa(info.get('bronze'))} برنز")
        msg.quote("این بودجه فقط برای تجهیزات همین مسابقه است و از کیف پول اصلی شما کم نمی‌شود.")
    elif status == engine.ST_CAP_REACHED:
        msg.title("ظرفیت این دسته پر است ⛔")
        msg.quote(f"سقف مجاز این دسته {fa(info.get('cap'))} عدد است. اول باید شلیک/مصرف کنید.")
    elif status == engine.ST_NUKE_LIMIT:
        msg.title("سلاح هسته‌ای برای شما تمام شد ☢️")
        msg.quote("هر فرمانده در هر مسابقه فقط یک سلاح هسته‌ای می‌تواند به کار ببرد.")
    else:
        msg.title("این خرید ممکن نیست ⛔")
        msg.quote("گزینهٔ معتبر را از همان منو انتخاب کنید.")
    return msg.build()


def shop_ready_notice(player) -> tuple:
    msg = Message()
    msg.title("پایگاه شما آماده است ✅")
    msg.line(f"موجودی باقی‌مانده: {fa(player.get('bronze'))} برنز")
    msg.line(f"موشک: {fa(engine.count_offensive(player))} — دفاع: {fa(engine.count_defense_systems(player))} — رادار: {fa(engine.count_radars(player))}")
    msg.quote("وقتی هر دو فرمانده تجهیزات را آماده کردند، جنگ خودکار شروع می‌شود.")
    return msg.build()


def war_started(match) -> tuple:
    msg = Message()
    msg.title(f"جنگ آغاز شد ⚔️")
    for player in match.get("players", []):
        msg.line(f"{engine.country_title(player)} — {player.get('name')}")
        msg.line(f"سلامت پایگاه: {percent(player.get('health'))} | جمعیت: {fa(player.get('population'))}")
    msg.blank()
    msg.bold("دستورات فرمانده\n")
    msg.raw(HELP_LINES)
    msg.quote("با «حمله» موشک شلیک کنید و با «دفاع» حملهٔ ورودی را رهگیری کنید.")
    return msg.build()


# ---------------------------------------------------------------------------
# حمله
# ---------------------------------------------------------------------------
def attack_menu(player) -> tuple:
    msg = Message()
    msg.title("حمله | انتخاب موشک 🚀")
    msg.quote("موشک‌های موجود را مشاهده کنید و شمارهٔ موشک موردنظر را ارسال کنید.")
    rows = engine.arsenal_list(player)
    if not rows:
        msg.blank()
        msg.line("انبار شما خالی است.")
        msg.quote("از فروشگاه تجهیزات، موشک تهیه کنید.")
        return msg.build()
    msg.blank()
    for index, (item_id, count) in enumerate(rows, start=1):
        item = WEAPONS[item_id]
        suffix = f" ☢️" if item["category"] == CATEGORY_NUKE else ""
        msg.line(f"{fa(index)} - {item['name']}{suffix} × {fa(count)}")
    msg.blank()
    msg.line("برای بازگشت بنویسید: بازگشت")
    return msg.build()


def attack_menu_index(player):
    """نگاشت شمارهٔ منو → item_id (همان ترتیبی که نمایش داده می‌شود)."""
    rows = engine.arsenal_list(player)
    return {index: item_id for index, (item_id, _count) in enumerate(rows, start=1)}


def attack_sent(attack, defender_name, detected) -> tuple:
    msg = Message()
    msg.title("🚀 شلیک انجام شد")
    msg.line(f"موشک: {attack.get('item_name')}")
    msg.line(f"هدف: {defender_name}")
    msg.line(f"قدرت بازی: {fa(attack.get('power'))}")
    if attack.get("nuke"):
        msg.line("نوع: ☢️ سلاح هسته‌ای")
    msg.quote("منتظر نتیجهٔ حمله بمانید؛ اگر هدف دفاع کند نتیجه در گزارش جنگ می‌آید.")
    return msg.build()


def incoming_warning(attack, attacker_label, remaining) -> tuple:
    msg = Message()
    msg.title("⚠️ هشدار فرمانده")
    msg.quote("یک موشک با مشخصات زیر در حال حمله به پایگاه شماست.")
    msg.blank()
    msg.line(f"اسم موشک: {attack.get('item_name')}")
    msg.line(f"قدرت بازی: {fa(attack.get('power'))}")
    msg.line(f"کشور مهاجم: {attacker_label}")
    msg.line(f"زمان باقی‌مانده: {fa(max(0, int(remaining)))} ثانیه")
    if attack.get("nuke"):
        msg.line("نوع: ☢️ هسته‌ای — خسارت بسیار سنگین")
    msg.blank()
    msg.bold("برای دفاع دستور دفاع را ارسال کنید.\n")
    msg.line(f"⏳ مهلت دفاع: {fa(DEFENSE_SECONDS)} ثانیه")
    return msg.build()


def defense_menu(attack, defender) -> tuple:
    msg = Message()
    msg.title("دفاع | انتخاب سامانه 🛡")
    msg.line(f"موشک مهاجم: {attack.get('item_name')}")
    msg.line(f"قدرت بازی: {fa(attack.get('power'))}")
    msg.blank()
    rows = engine.defense_list(defender)
    if not rows:
        msg.line("هیچ سامانهٔ دفاعی آماده ندارید.")
        msg.quote("این حمله بدون دفاع تعیین تکلیف می‌شود.")
        return msg.build()
    for index, (item_id, capacity) in enumerate(rows, start=1):
        item = WEAPONS[item_id]
        msg.line(f"{fa(index)}. {item['name']} — ظرفیت باقی‌مانده: {fa(capacity)}")
    msg.blank()
    msg.quote("شمارهٔ سامانه را بفرستید. هر حمله فقط یک فرصت دفاع دارد.")
    return msg.build()


def defense_menu_index(defender):
    rows = engine.defense_list(defender)
    return {index: item_id for index, (item_id, _c) in enumerate(rows, start=1)}


def defense_registered(attack, system_item) -> tuple:
    msg = Message()
    msg.title("رهگیری آغاز شد 🛡")
    msg.line(f"موشک مهاجم: {attack.get('item_name')}")
    msg.line(f"سامانه: {system_item['name']}")
    msg.quote("نتیجهٔ رهگیری در گزارش جنگ اعلام می‌شود.")
    return msg.build()


def attack_report(report) -> tuple:
    attack = report["attack"]
    msg = Message()
    if report.get("intercepted"):
        msg.title("🛡 گزارش دفاع")
        msg.line(f"موشک مهاجم: {attack.get('item_name')}")
        msg.line(f"سامانهٔ دفاعی: {report.get('defense_item')}")
        msg.line("وضعیت: رهگیری موفق")
        msg.blank()
        msg.line(f"سلامت قبلی پایگاه: {percent(attack.get('health_before'))}")
        msg.line(f"خسارت واردشده: {percent(attack.get('damage'))}")
        msg.line(f"سلامت فعلی پایگاه: {percent(attack.get('health_after'))}")
        msg.line(f"موشک رهگیری‌شده: {fa(1)} عدد")
        if int(attack.get("casualties") or 0):
            msg.line(f"تلفات جانبی: {fa(attack.get('casualties'))} نفر")
    else:
        outcome = "اصابت موفق"
        if attack.get("result") == "defense_failed":
            outcome = "دفاع ناموفق — اصابت"
        elif not attack.get("landed"):
            outcome = "اصابت با دقت پایین"
        msg.title("⚠️ گزارش حمله")
        msg.line(f"موشک مهاجم: {attack.get('item_name')}")
        msg.line(f"وضعیت: {outcome}")
        if report.get("defense_item"):
            msg.line(f"سامانهٔ دفاعی مدافع: {report.get('defense_item')}")
        msg.blank()
        msg.line(f"سلامت قبلی پایگاه: {percent(attack.get('health_before'))}")
        msg.line(f"خسارت واردشده: {percent(attack.get('damage'))}")
        msg.line(f"سلامت فعلی پایگاه: {percent(attack.get('health_after'))}")
        msg.blank()
        msg.line(f"جمعیت قبلی: {fa(attack.get('population_before'))} نفر")
        msg.line(f"تلفات: {fa(attack.get('casualties'))} نفر")
        msg.line(f"جمعیت باقی‌مانده: {fa(attack.get('population_after'))} نفر")
    blowback = report.get("blowback") or {}
    if blowback.get("self_damage"):
        msg.blank()
        msg.title("☢️ پیامد هسته‌ای برای مهاجم")
        msg.line(f"خسارت به پایگاه خودی: {percent(blowback.get('self_damage'))}")
        msg.line(f"تلفات خودی: {fa(blowback.get('self_casualties'))} نفر")
        msg.line(f"سلامت پایگاه مهاجم: {percent(blowback.get('health_after'))}")
        msg.quote("استفاده از سلاح هسته‌ای برای خود فرمانده هم هزینه دارد.")
    return msg.build()


def no_attack_notice() -> tuple:
    msg = Message()
    msg.title("حملهٔ در انتظاری وجود ندارد ⛔")
    msg.quote("فقط فرماندهٔ مدافع می‌تواند برای حملهٔ ورودی دفاع ثبت کند.")
    return msg.build()


def attack_expired_notice() -> tuple:
    msg = Message()
    msg.title("مهلت دفاع این حمله تمام شد ⏳")
    msg.quote("حمله تعیین تکلیف شده است؛ نتیجه را در «گزارش جنگ» ببینید.")
    return msg.build()


def not_defender_notice() -> tuple:
    msg = Message()
    msg.title("شما مدافع این حمله نیستید ⛔")
    msg.quote("مهاجم نمی‌تواند حملهٔ خودش را خنثی کند.")
    return msg.build()


# ---------------------------------------------------------------------------
# تعمیر
# ---------------------------------------------------------------------------
def repair_prompt(player) -> tuple:
    msg = Message()
    msg.title("تعمیر پایگاه 🔧")
    msg.line(f"سلامت فعلی: {percent(player.get('health'))}")
    msg.line(f"موجودی: {fa(player.get('bronze'))} برنز")
    msg.blank()
    msg.line(REPAIR_COST_TEXT)
    msg.quote(f"حداکثر {fa(20)}٪ در هر دستور و هر {fa(20)} ثانیه یک بار. عدد درصد را بفرستید.")
    return msg.build()


def repair_done(player, info) -> tuple:
    msg = Message()
    msg.title("پایگاه تعمیر شد ✅")
    msg.line(f"سلامت جدید: {percent(player.get('health'))}")
    msg.line(f"هزینه: {fa(info.get('cost'))} برنز")
    msg.line(f"موجودی: {fa(player.get('bronze'))} برنز")
    return msg.build()


def repair_failed(status, info=None) -> tuple:
    info = info or {}
    msg = Message()
    if status == engine.ST_REPAIR_FULL:
        msg.title("پایگاه شما سالم است ✅")
        msg.quote("سلامت پایگاه روی ۱۰۰٪ است و تعمیر لازم نیست.")
    elif status == engine.ST_REPAIR_COOLDOWN:
        msg.title("هنوز زود است ⏳")
        msg.quote(f"{fa(info.get('wait', 0))} ثانیهٔ دیگر می‌توانید تعمیر کنید.")
    elif status == engine.ST_REPAIR_NO_BUDGET:
        msg.title("برنز کافی برای تعمیر نیست ⛔")
        msg.line(f"هزینهٔ حداقلی: {fa(info.get('cost'))} برنز")
    else:
        msg.title("تعمیر ممکن نیست ⛔")
        msg.quote("عدد معتبر (درصد) را بفرستید.")
    return msg.build()


# ---------------------------------------------------------------------------
# گزارش جنگ و پایان
# ---------------------------------------------------------------------------
def war_report(match) -> tuple:
    msg = Message()
    msg.title("گزارش جنگ 📜")
    history = match.get("history") or []
    if not history:
        msg.quote("هنوز هیچ حمله‌ای ثبت نشده است.")
        return msg.build()
    for entry in history[-12:]:
        attacker = engine.player_of(match, entry.get("attacker_id"))
        defender = engine.player_of(match, entry.get("defender_id"))
        a_name = (attacker or {}).get("name") or "فرمانده"
        d_name = (defender or {}).get("name") or "فرمانده"
        if entry.get("result") == "intercepted":
            outcome = "رهگیری موفق"
        elif entry.get("result") == "blowback":
            outcome = "پیامد هسته‌ای"
        else:
            outcome = "اصابت"
        msg.line(f"{BULLET} {entry.get('item')} — {a_name} → {d_name}")
        msg.line(f"وضعیت: {outcome} | خسارت: {percent(entry.get('damage'))} | تلفات: {fa(entry.get('casualties'))}")
        msg.line(f"سلامت پایگاه مدافع: {percent(entry.get('health_after'))}")
        msg.blank()
    msg.quote("فقط ۱۲ رویداد آخر نمایش داده می‌شود؛ کل تاریخچه در آرشیو مسابقه ذخیره است.")
    return msg.build()


def winner_message(match, winner, prize_paid) -> tuple:
    msg = Message()
    msg.title("فرمانده: 🪖")
    msg.bold("شما برنده بازی شدید.\n")
    msg.blank()
    if prize_paid:
        msg.line(f"🏆 جایزه دریافتی: {fa(WINNER_SILVER)} سکه نقره")
    else:
        msg.line(f"🏆 جایزه: {fa(WINNER_SILVER)} سکه نقره (در حال پرداخت)")
    msg.blank()
    msg.line(f"سلامت پایگاه: {percent(winner.get('health'))}")
    msg.line(f"جمعیت باقی‌مانده: {fa(winner.get('population'))}")
    msg.line(f"امتیاز فرماندهی کل: {fa(winner.get('score'))}")
    msg.quote("از اینکه سرباز شما برای اجرای دستورات بودم خرسندم. شما راهبرد دفاعی و استراتژی حملهٔ خوبی داشتید.")
    return msg.build()


def loser_message(match, loser, reason) -> tuple:
    msg = Message()
    msg.title("فرمانده: 🪖")
    msg.bold("شما بازنده شدید.\n")
    msg.blank()
    msg.bold("گزارش عملکرد و خسارت ⛔\n")
    msg.line(f"سلامت پایگاه: {percent(loser.get('health'))}")
    msg.line(f"موشک‌های رهگیری‌شده: {fa(loser.get('interceptions'))}")
    msg.line(f"موشک‌های بدون رهگیری: {fa(loser.get('attacks_successful'))}")
    msg.line(f"جمعیت باقی‌مانده: {fa(loser.get('population'))}")
    msg.line(f"تعداد حملات موفق: {fa(loser.get('attacks_successful'))}")
    msg.line(f"امتیاز فرماندهی کل: {fa(loser.get('score'))}")
    if reason == engine.END_SURRENDER:
        msg.quote("با دستور «تسلیم» مسابقه را واگذار کردید.")
    elif reason == engine.END_NUKE_BLOWBACK:
        msg.quote("پیامد استفادهٔ هسته‌ای به پایگاه خودتان بازگشت.")
    else:
        msg.quote("پایگاه شما از کار افتاد. در بازی بعدی راهبرد حمله و دفاع خود را بهتر کنید.")
    msg.blank()
    msg.bold("🔔 شما شکست خوردید. در بازی بعدی راهبرد و سیاست حمله و دفاع خود را بهتر کنید.\n")
    return msg.build()


def already_finished() -> tuple:
    msg = Message()
    msg.title("این مسابقه بسته شده ⛔")
    msg.quote(f"برای مسابقهٔ تازه بنویسید: {COMMAND}")
    return msg.build()


def not_player_notice() -> tuple:
    msg = Message()
    msg.title("شما فرماندهٔ این مسابقه نیستید ⛔")
    msg.quote("دستورهای بازی فقط برای دو فرماندهٔ ثبت‌نام‌شده عمل می‌کنند.")
    return msg.build()


def bad_choice_notice() -> tuple:
    msg = Message()
    msg.title("انتخاب نامعتبر ⛔")
    msg.quote("شمارهٔ موجود در منو را ارسال کنید.")
    return msg.build()


def too_many_attacks() -> tuple:
    msg = Message()
    msg.title("هم‌زمانی حمله‌ها زیاد است ⏳")
    msg.quote("اول نتیجهٔ حمله‌های در جریان مشخص شود.")
    return msg.build()


def nuke_blocked(reason) -> tuple:
    msg = Message()
    if reason == engine.ST_NUKE_ROUND:
        msg.title("سلاح هسته‌ای در این دور استفاده شده ☢️")
        msg.quote("در هر دور فقط یک سلاح هسته‌ای شلیک می‌شود تا بازی به بن‌بست نرسد.")
    elif reason == engine.ST_NUKE_LIMIT:
        msg.title("سلاح هسته‌ای برای شما تمام شد ☢️")
        msg.quote("هر فرمانده در هر مسابقه فقط یک بار می‌تواند هسته‌ای شلیک کند.")
    else:
        msg.title("شلیک هسته‌ای ممکن نیست ☢️")
        msg.quote(WARNING_NUKE)
    return msg.build()


def surrender_done(match, opponent) -> tuple:
    msg = Message()
    msg.title("تسلیم ثبت شد 🏳")
    msg.line(f"فرمانده: {(opponent or {}).get('name')}")
    msg.quote("مسابقه بسته می‌شود و گزارش نهایی ارسال می‌گردد.")
    return msg.build()


def waiting_notice(match) -> tuple:
    msg = Message()
    msg.title("در انتظار فرماندهٔ دیگر ⏳")
    msg.quote("وقتی هر دو فرمانده آماده شدند جنگ شروع می‌شود.")
    return msg.build()
