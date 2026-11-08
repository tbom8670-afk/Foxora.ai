# -*- coding: utf-8 -*-
"""🦊 موتور خالص بازی «مرکز فرماندهی روباه».

هیچ I/O، هیچ await و هیچ دسترسی به دیتابیس اینجا نیست؛ فقط توابع خالص که
وضعیت می‌گیرند و وضعیت/نتیجه برمی‌گردانند. همین ویژگی باعث می‌شود کل منطق
(خرید، شلیک، رهگیری، خسارت، تعمیر، پایان) دقیق و بدون شبکه تست شود.

قواعد کلیدی که این موتور تضمین می‌کند:

* خرید همیشه از موجودی **همان مسابقه** کم می‌شود، نه کیف پول واقعی.
* جمع سفارش‌های موشکی از ``MAX_OFFENSIVE`` و سامانه‌های دفاعی از
  ``MAX_DEFENSE_SYSTEMS`` بیشتر نمی‌شود (خرید و شلیک جدا هستند).
* شلیک یک عدد از انبار کم می‌کند و یک حملهٔ مستقل با شناسهٔ یکتا می‌سازد.
* هر حمله فقط یک بار تعیین تکلیف می‌شود (``settled``).
* سلامت پایگاه همیشه در بازهٔ ۰..۱۰۰ و جمعیت هرگز منفی نیست.
* هیچ دفاعی ۱۰۰٪ نیست و رهگیری موفق هم خسارت صفر نمی‌دهد.
* سلاح هسته‌ای پرخطر است: پیامد برای مهاجم + سقف مصرف.
"""
from __future__ import annotations

import random
import time

from modules.fox_command import (
    BALANCE,
    CATEGORY_BALLISTIC,
    CATEGORY_CRUISE,
    CATEGORY_DEFENSE,
    CATEGORY_NUKE,
    CATEGORY_RADAR,
    CHEAPEST_OFFENSIVE,
    COUNTRY_ORDER,
    COUNTRIES,
    MAX_ACTIVE_ATTACKS,
    MAX_DEFENSE_SYSTEMS,
    MAX_NUKES,
    MAX_OFFENSIVE,
    MAX_RADARS,
    NUKES_PER_GAME,
    NUKES_PER_ROUND,
    PLAYERS,
    REPAIR_COOLDOWN,
    REPAIR_MAX_PER_ORDER,
    REPAIR_PER_POINT,
    START_BRONZE,
    START_HEALTH,
    START_POPULATION,
    WEAPONS,
    is_defense,
    is_nuke,
    is_offensive,
    is_radar,
)

PHASE_JOINING = "joining"
PHASE_COUNTRY = "country"
PHASE_SETUP = "setup"
PHASE_WAR = "war"
PHASE_FINISHED = "finished"

# گزارش‌های تصمیم (برای پیام‌های بازی)
ST_OK = "ok"
ST_DUPLICATE = "duplicate"
ST_FULL = "full"
ST_NOT_PLAYER = "not_player"
ST_NOT_YOUR_TURN = "not_your_turn"
ST_WRONG_PHASE = "wrong_phase"
ST_BAD_CHOICE = "bad_choice"
ST_COUNTRY_TAKEN = "country_taken"
ST_NO_BUDGET = "no_budget"
ST_CAP_REACHED = "cap_reached"
ST_SOLD_OUT = "sold_out"
ST_EMPTY_ARSENAL = "empty_arsenal"
ST_NO_SYSTEM = "no_system"
ST_NO_ATTACK = "no_attack"
ST_ATTACK_EXPIRED = "attack_expired"
ST_NOT_DEFENDER = "not_defender"
ST_ALREADY_DEFENDED = "already_defended"
ST_TOO_MANY = "too_many"
ST_REPAIR_FULL = "repair_full"
ST_REPAIR_COOLDOWN = "repair_cooldown"
ST_REPAIR_NO_BUDGET = "repair_no_budget"
ST_NUKE_LIMIT = "nuke_limit"
ST_NUKE_ROUND = "nuke_round"
ST_ALREADY_FINISHED = "already_finished"
ST_NOT_READY = "not_ready"

# دلایل پایان مسابقه
END_DESTROYED = "destroyed"
END_SURRENDER = "surrender"
END_NUKE_BLOWBACK = "nuke_blowback"
END_NO_AMMO = "no_ammo"          # یک طرف هیچ موشک/بودجه‌ای برای جنگ ندارد
END_STALEMATE = "stalemate"      # هر دو طرف توان جنگ ندارند → امتیاز تعیین می‌کند

_RANDOM = random.SystemRandom()


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# ساخت و بازرسی وضعیت
# ---------------------------------------------------------------------------
def _empty_arsenal() -> dict:
    return {}


def new_match(chat_id, match_id) -> dict:
    """وضعیت تازهٔ یک مسابقه (بدون بازیکن)."""
    return {
        "match_id": str(match_id),
        "chat_id": int(chat_id),
        "phase": PHASE_JOINING,
        "created_at": now(),
        "join_deadline": now() + 60,
        "players": [],
        "damage_events": 0,
        "round": 0,
        "winner": None,
        "loser": None,
        "end_reason": None,
        "finished_at": None,
        "reward_paid": False,
        "reward_reference": None,
        "history": [],
    }


def new_player(user_id, name) -> dict:
    """وضعیت تازهٔ یک فرمانده داخل مسابقه (کیف پول مستقل)."""
    return {
        "user_id": int(user_id),
        "name": str(name or "")[:64],
        "country": None,
        "bronze": START_BRONZE,
        "spent": 0,
        "health": START_HEALTH,
        "population": START_POPULATION,
        "arsenal": _empty_arsenal(),          # item_id → count (شلیک‌نشده)
        "defense": _empty_arsenal(),          # system_id → remaining capacity
        "defense_total": _empty_arsenal(),    # system_id → capacity کل (نمایش)
        "radars": {},                        # radar_id → count
        "interceptions": 0,
        "attacks_sent": 0,
        "attacks_successful": 0,
        "attacks_blocked_by_enemy": 0,       # موشک‌های رهگیری‌شدهٔ من
        "damage_taken": 0.0,
        "nukes_used": 0,
        "nukes_round": {},                   # round → count
        "last_repair_at": 0.0,
        "surrendered": False,
        "reward_paid": False,
        "score": 0,
        "finished_at": None,
    }


def player_of(match, user_id):
    for player in match.get("players", []):
        if int(player.get("user_id") or 0) == int(user_id or 0):
            return player
    return None


def opponent_of(match, user_id):
    for player in match.get("players", []):
        if int(player.get("user_id") or 0) != int(user_id or 0):
            return player
    return None


def count_offensive(player) -> int:
    return sum(int(v) for v in (player.get("arsenal") or {}).values())


def count_defense_systems(player) -> int:
    return len(player.get("defense") or {})


def count_radars(player) -> int:
    return sum(int(v) for v in (player.get("radars") or {}).values())


def radar_bonus(player) -> float:
    """قدرت رادار: شناسایی + مقابله (سقف‌دار)."""
    bonus = 0.0
    for radar_id, count in (player.get("radars") or {}).items():
        item = WEAPONS.get(radar_id) or {}
        bonus += (float(item.get("counter") or 0.0)) * int(count)
    return min(bonus, float(BALANCE["radar_max_counter"]))


# ---------------------------------------------------------------------------
# ثبت‌نام و کشور
# ---------------------------------------------------------------------------
def join(match, user_id, name):
    """ثبت‌نام یک فرمانده. خروجی ``(status, player)``."""
    if match.get("phase") != PHASE_JOINING:
        return ST_WRONG_PHASE, None
    if player_of(match, user_id) is not None:
        return ST_DUPLICATE, None
    if len(match.get("players", [])) >= PLAYERS:
        return ST_FULL, None
    player = new_player(user_id, name)
    match.setdefault("players", []).append(player)
    return ST_OK, player


def missing_country(match):
    """کدام فرمانده هنوز کشور انتخاب نکرده است (اولی که نوبت است)."""
    for player in match.get("players", []):
        if not player.get("country"):
            return player
    return None


def taken_countries(match) -> set:
    return {p.get("country") for p in match.get("players", []) if p.get("country")}


def choose_country(match, user_id, country_key):
    """انتخاب کشور؛ هر کشور فقط یک بار در هر مسابقه."""
    if match.get("phase") not in (PHASE_COUNTRY, PHASE_JOINING):
        return ST_WRONG_PHASE, None
    player = player_of(match, user_id)
    if player is None:
        return ST_NOT_PLAYER, None
    key = str(country_key or "").strip().lower()
    if key.isdigit():
        index = int(key) - 1
        if 0 <= index < len(COUNTRY_ORDER):
            key = COUNTRY_ORDER[index]
    if key not in COUNTRIES:
        return ST_BAD_CHOICE, None
    if player.get("country") == key:
        return ST_DUPLICATE, player
    if key in taken_countries(match):
        return ST_COUNTRY_TAKEN, player
    player["country"] = key
    if len(match.get("players", [])) == PLAYERS and not missing_country(match):
        match["phase"] = PHASE_SETUP
    else:
        match["phase"] = PHASE_COUNTRY
    return ST_OK, player


def country_title(player) -> str:
    key = player.get("country")
    if key not in COUNTRIES:
        return "بدون کشور"
    name, flag, official = COUNTRIES[key]
    return f"{official} {flag}"


# ---------------------------------------------------------------------------
# فروشگاه و خرید
# ---------------------------------------------------------------------------
def purchase(match, user_id, item_id):
    """خرید یک عدد از یک تجهیز. خروجی ``(status, info)``.

    اتمیک است: یا پول کم می‌شود و تجهیز اضافه می‌گردد، یا هیچ‌کدام.
    """
    if match.get("phase") not in (PHASE_SETUP, PHASE_WAR):
        return ST_WRONG_PHASE, None
    player = player_of(match, user_id)
    if player is None:
        return ST_NOT_PLAYER, None
    item = WEAPONS.get(item_id)
    if not item:
        return ST_BAD_CHOICE, None

    price = int(item["price"])
    if int(player.get("bronze") or 0) < price:
        return ST_NO_BUDGET, {"price": price, "bronze": player.get("bronze", 0)}

    category = item["category"]
    if category in (CATEGORY_BALLISTIC, CATEGORY_CRUISE):
        if count_offensive(player) >= MAX_OFFENSIVE:
            return ST_CAP_REACHED, {"cap": MAX_OFFENSIVE}
        container = player["arsenal"]
    elif category == CATEGORY_DEFENSE:
        # سقف روی «تعداد سامانهٔ متفاوت» است؛ خرید پشتیبان از همان سامانه
        # (ظرفیت رهگیری بیشتر) مجاز است.
        if (item_id not in player["defense"]
                and count_defense_systems(player) >= MAX_DEFENSE_SYSTEMS):
            return ST_CAP_REACHED, {"cap": MAX_DEFENSE_SYSTEMS}
        container = player["defense"]
    elif category == CATEGORY_RADAR:
        if count_radars(player) >= MAX_RADARS:
            return ST_CAP_REACHED, {"cap": MAX_RADARS}
        container = player["radars"]
    elif category == CATEGORY_NUKE:
        owned_nukes = sum(int(c) for nid, c in player["arsenal"].items() if is_nuke(nid))
        if owned_nukes >= MAX_NUKES:
            return ST_CAP_REACHED, {"cap": MAX_NUKES}
        if player.get("nukes_used", 0) >= NUKES_PER_GAME:
            return ST_NUKE_LIMIT, None
        container = player["arsenal"]
    else:  # pragma: no cover - دستهٔ ناشناخته
        return ST_BAD_CHOICE, None

    # --- اعمال اتمیک ---
    player["bronze"] = int(player["bronze"]) - price
    player["spent"] = int(player.get("spent") or 0) + price
    container[item_id] = int(container.get(item_id, 0)) + 1
    if category == CATEGORY_DEFENSE:
        capacity = int(item.get("capacity") or 1)
        player["defense_total"][item_id] = int(player["defense_total"].get(item_id, 0)) + capacity
    return ST_OK, {"item": item, "price": price, "bronze": player["bronze"]}


def repair(match, user_id, percent):
    """تعمیر پایگاه با برنزِ همان مسابقه."""
    if match.get("phase") not in (PHASE_SETUP, PHASE_WAR):
        return ST_WRONG_PHASE, None
    player = player_of(match, user_id)
    if player is None:
        return ST_NOT_PLAYER, None
    health = float(player.get("health") or 0)
    if health >= START_HEALTH:
        return ST_REPAIR_FULL, None
    try:
        percent = int(percent)
    except (TypeError, ValueError):
        return ST_BAD_CHOICE, None
    if percent <= 0:
        return ST_BAD_CHOICE, None
    percent = min(percent, REPAIR_MAX_PER_ORDER, int(START_HEALTH - health))
    cost = percent * REPAIR_PER_POINT
    if int(player.get("bronze") or 0) < cost:
        affordable = int(player.get("bronze") or 0) // REPAIR_PER_POINT
        if affordable <= 0:
            return ST_REPAIR_NO_BUDGET, {"cost": cost}
        percent = affordable
        cost = percent * REPAIR_PER_POINT
    last = float(player.get("last_repair_at") or 0)
    if last and (now() - last) < REPAIR_COOLDOWN:
        return ST_REPAIR_COOLDOWN, {"wait": int(REPAIR_COOLDOWN - (now() - last)) + 1}
    player["bronze"] = int(player["bronze"]) - cost
    player["health"] = clamp_health(health + percent)
    player["last_repair_at"] = now()
    return ST_OK, {"percent": percent, "cost": cost,
                   "health": player["health"], "bronze": player["bronze"]}


# ---------------------------------------------------------------------------
# حمله
# ---------------------------------------------------------------------------
def launch(match, user_id, item_id, attack_id):
    """شلیک یک موشک از انبار. خروجی ``(status, attack)``."""
    if match.get("phase") != PHASE_WAR:
        return ST_WRONG_PHASE, None
    attacker = player_of(match, user_id)
    if attacker is None:
        return ST_NOT_PLAYER, None
    defender = opponent_of(match, user_id)
    if defender is None:
        return ST_WRONG_PHASE, None
    item = WEAPONS.get(item_id)
    if not item or not (is_offensive(item_id) or is_nuke(item_id)):
        return ST_BAD_CHOICE, None
    if int((attacker.get("arsenal") or {}).get(item_id, 0)) <= 0:
        return ST_SOLD_OUT, None
    if len(active_attacks(match)) >= MAX_ACTIVE_ATTACKS:
        return ST_TOO_MANY, {"cap": MAX_ACTIVE_ATTACKS}
    if is_nuke(item_id):
        if attacker.get("nukes_used", 0) >= NUKES_PER_GAME:
            return ST_NUKE_LIMIT, None
        current_round = int(match.get("round") or 0)
        used_round = int((attacker.get("nukes_round") or {}).get(str(current_round), 0))
        if used_round >= NUKES_PER_ROUND:
            return ST_NUKE_ROUND, None

    # --- مصرف اتمیک ---
    attacker["arsenal"][item_id] = int(attacker["arsenal"][item_id]) - 1
    if attacker["arsenal"][item_id] <= 0:
        attacker["arsenal"].pop(item_id, None)
    attacker["attacks_sent"] = int(attacker.get("attacks_sent") or 0) + 1
    if is_nuke(item_id):
        attacker["nukes_used"] = int(attacker.get("nukes_used") or 0) + 1
        current_round = int(match.get("round") or 0)
        rounds = attacker.setdefault("nukes_round", {})
        rounds[str(current_round)] = int(rounds.get(str(current_round), 0)) + 1

    launched_at = now()
    attack = {
        "attack_id": str(attack_id),
        "match_id": match.get("match_id"),
        "attacker_id": int(user_id),
        "defender_id": int(defender["user_id"]),
        "item_id": item_id,
        "item_name": item["name"],
        "power": item["power"],
        "nuke": is_nuke(item_id),
        "launched_at": launched_at,
        "defend_deadline": launched_at + 17,
        "detected": False,
        "settled": False,
        "defended": False,
        "status": "in_flight",
        "defense_item": None,
        "result": None,
        "damage": 0.0,
        "casualties": 0,
        "created_at": launched_at,
    }
    # شناسایی توسط رادار مدافع (اطلاع در متن هشدار)
    attack["detected"] = bool(
        _RANDOM.random() < _detection_chance(defender, item))
    match.setdefault("attacks", []).append(attack)
    if not match.get("attacks_started_at"):
        match["attacks_started_at"] = launched_at
    return ST_OK, attack


def _detection_chance(defender, item) -> float:
    """شانس شناسایی حملهٔ ورودی توسط رادارهای مدافع."""
    base = 0.55 + float(BALANCE["radar_defense_bonus"]) * 2
    bonus = 0.0
    for radar_id, count in (defender.get("radars") or {}).items():
        radar = WEAPONS.get(radar_id) or {}
        bonus += float(radar.get("detect") or 0) * int(count)
    chance = base + bonus - float(item.get("power") or 0) / 1000.0
    return max(0.15, min(0.95, chance))


def active_attacks(match) -> list:
    return [a for a in match.get("attacks", []) if not a.get("settled")]


def attacks_against(match, user_id, *, pending_only=True) -> list:
    result = []
    for attack in match.get("attacks", []):
        if int(attack.get("defender_id") or 0) != int(user_id or 0):
            continue
        if pending_only and attack.get("settled"):
            continue
        result.append(attack)
    return result


def attack_by_id(match, attack_id):
    for attack in match.get("attacks", []):
        if str(attack.get("attack_id")) == str(attack_id):
            return attack
    return None


def defend(match, user_id, system_id, attack_id):
    """ثبت انتخاب سامانهٔ دفاعی توسط مدافع. خروجی ``(status, attack, info)``."""
    attack = attack_by_id(match, attack_id)
    if attack is None:
        return ST_NO_ATTACK, None, None
    if int(attack.get("defender_id") or 0) != int(user_id or 0):
        return ST_NOT_DEFENDER, attack, None
    if attack.get("settled"):
        return ST_ATTACK_EXPIRED, attack, None
    if attack.get("defended"):
        return ST_ALREADY_DEFENDED, attack, None
    defender = player_of(match, user_id)
    if defender is None:
        return ST_NOT_PLAYER, attack, None
    item = WEAPONS.get(system_id)
    if not item or item["category"] != CATEGORY_DEFENSE:
        return ST_BAD_CHOICE, attack, None
    remaining = int((defender.get("defense") or {}).get(system_id, 0))
    if remaining <= 0:
        return ST_NO_SYSTEM, attack, None
    # مصرف ظرفیت سامانه (هر حمله فقط یک فرصت دفاع)
    defender["defense"][system_id] = remaining - 1
    if defender["defense"][system_id] <= 0:
        defender["defense"].pop(system_id, None)
    attack["defended"] = True
    attack["defense_item"] = system_id
    return ST_OK, attack, item


def _intercept_chance(defender, system_item, attack_item, *, nuke_fallout=0.0):
    """شانس رهگیری: سامانه + رادار، ضربدر «سختی رهگیری» موشک، منهای قدرت آن.

    ``intercept`` هر موشک یعنی «چقدر راحت رهگیری می‌شود»: کروزِ ارزان ~۰.۹۵
    (آسان) و بالستیک سنگین ~۰.۷۴ (سخت). بنابراین سیلِ موشک ارزان با دفاع
    درست مهار می‌شود و موشک سنگین ارزش خرید خودش را دارد.
    """
    base = float(system_item.get("intercept") or 0)
    bonus = radar_bonus(defender)
    ease = float(attack_item.get("intercept") or 0.9)
    power = float(attack_item.get("power") or 0)
    chance = (base + bonus) * ease - power / 900.0 - nuke_fallout
    # تصادفِ کوچک (±۴٪) برای عدم قطعیت طبیعی
    chance += _RANDOM.uniform(-0.04, 0.04)
    return max(float(BALANCE["intercept_floor"]),
               min(float(BALANCE["intercept_cap"]), chance))


def clamp_health(value) -> float:
    return max(0.0, min(float(START_HEALTH), round(float(value), 2)))


def clamp_population(value) -> int:
    return max(0, int(round(float(value))))


def settle(match, attack_id, reason="timeout"):
    """تعیین تکلیف یک حمله — **فقط یک بار**. خروجی ``(attack, report)``.

    ``report`` شامل خسارت واقعی، تلفات و وضعیت پس از حمله است تا لایهٔ
    پیام بدون هیچ محاسبهٔ دوباره فقط آن را منتشر کند.
    """
    attack = attack_by_id(match, attack_id)
    if attack is None or attack.get("settled"):
        return attack, None
    attacker = player_of(match, attack.get("attacker_id"))
    defender = player_of(match, attack.get("defender_id"))
    if attacker is None or defender is None:  # pragma: no cover - وضعیت ناسازگار
        attack["settled"] = True
        attack["status"] = "void"
        return attack, None

    attack_item = WEAPONS.get(attack.get("item_id")) or {}
    nuke = bool(attack.get("nuke"))
    system_item = WEAPONS.get(attack.get("defense_item")) if attack.get("defense_item") else None

    health_before = float(defender.get("health") or 0)
    population_before = int(defender.get("population") or 0)

    intercepted = False
    if system_item is not None:
        chance = _intercept_chance(defender, system_item, attack_item)
        intercepted = _RANDOM.random() < chance
        attack["intercept_chance"] = round(chance, 4)

    if intercepted:
        damage = float(attack_item.get("damage") or 0) * BALANCE["intercepted_damage_ratio"]
        casualties = int(population_before * float(attack_item.get("casualties") or 0)
                         * BALANCE["intercepted_casualty_ratio"])
        defender["interceptions"] = int(defender.get("interceptions") or 0) + 1
        attacker["attacks_blocked_by_enemy"] = int(attacker.get("attacks_blocked_by_enemy") or 0) + 1
        attack["status"] = "intercepted"
    else:
        if system_item is not None:
            # دفاع شد ولی شکست خورد → موشک کامل اصابت می‌کند.
            attacker["attacks_blocked_by_enemy"] = int(attacker.get("attacks_blocked_by_enemy") or 0)
            attack["status"] = "defense_failed"
        else:
            attack["status"] = "hit"
        # «دقت» موشک: با احتمال کمی موشک به هدف نمی‌خورد (نه رهگیری).
        landed = _RANDOM.random() < float(attack_item.get("hit") or 0.7)
        scale = BALANCE["failed_defense_damage_ratio"] if landed else 0.35
        health_penalty = 1.0 + BALANCE["damage_health_penalty"] * (1 - health_before / START_HEALTH)
        damage = float(attack_item.get("damage") or 0) * scale * health_penalty
        casualties = int(population_before * float(attack_item.get("casualties") or 0) * scale)
        if landed:
            attacker["attacks_successful"] = int(attacker.get("attacks_successful") or 0) + 1
        attack["landed"] = bool(landed)

    damage = max(0.0, float(damage))
    casualties = clamp_population(casualties)
    defender["health"] = clamp_health(health_before - damage)
    defender["population"] = clamp_population(population_before - casualties)
    defender["damage_taken"] = round(float(defender.get("damage_taken") or 0) + damage, 2)

    attack["settled"] = True
    attack["settled_at"] = now()
    attack["settle_reason"] = reason
    attack["damage"] = round(damage, 2)
    attack["casualties"] = casualties
    attack["health_before"] = round(health_before, 2)
    attack["health_after"] = defender["health"]
    attack["population_before"] = population_before
    attack["population_after"] = defender["population"]
    attack["result"] = "intercepted" if intercepted else attack["status"]

    match["damage_events"] = int(match.get("damage_events") or 0) + 1
    history_entry = {
        "kind": "attack",
        "attack_id": attack["attack_id"],
        "attacker_id": attack["attacker_id"],
        "defender_id": attack["defender_id"],
        "item": attack.get("item_name"),
        "nuke": nuke,
        "result": attack["result"],
        "damage": attack["damage"],
        "casualties": attack["casualties"],
        "health_after": defender["health"],
        "population_after": defender["population"],
        "at": attack["settled_at"],
    }
    match.setdefault("history", []).append(history_entry)

    # ☢️ پیامد هسته‌ای برای مهاجم (ریسک واقعی، نه تشریفاتی)
    blowback = None
    if nuke:
        blowback = _nuke_blowback(match, attacker, attack_item)
        if blowback.get("history"):
            match.setdefault("history", []).append(blowback["history"])

    report = {
        "attack": attack,
        "nuke": nuke,
        "intercepted": intercepted,
        "defense_item": (system_item or {}).get("name"),
        "attacker_health": attacker["health"],
        "defender_health": defender["health"],
        "defender_population": defender["population"],
        "blowback": blowback,
    }
    return attack, report


def _nuke_blowback(match, attacker, attack_item):
    """عواقب استفادهٔ هسته‌ای برای خود مهاجم — گاهی فاجعه‌بار."""
    result = {"self_damage": 0.0, "self_casualties": 0, "collapse": False,
              "history": None}
    chance = float(attack_item.get("blowback") or 0.05)
    if _RANDOM.random() >= chance:
        return result
    health = float(attacker.get("health") or 0)
    population = int(attacker.get("population") or 0)
    damage = max(float(BALANCE["nuke_min_self_damage"]),
                 health * float(BALANCE["nuke_self_damage_ratio"]))
    casualties = int(population * 0.05)
    attacker["health"] = clamp_health(health - damage)
    attacker["population"] = clamp_population(population - casualties)
    attacker["damage_taken"] = round(float(attacker.get("damage_taken") or 0) + damage, 2)
    result.update({
        "self_damage": round(damage, 2),
        "self_casualties": casualties,
        "health_after": attacker["health"],
        "population_after": attacker["population"],
        "collapse": attacker["health"] <= 0,
        "history": {
            "kind": "blowback",
            "attack_id": None,
            "attacker_id": attacker["user_id"],
            "defender_id": attacker["user_id"],
            "item": "پیامد هسته‌ای",
            "result": "blowback",
            "damage": round(damage, 2),
            "casualties": casualties,
            "health_after": attacker["health"],
            "population_after": attacker["population"],
            "at": now(),
        },
    })
    return result


# ---------------------------------------------------------------------------
# پایان مسابقه
# ---------------------------------------------------------------------------
def out_of_offense(player) -> bool:
    """آیا این فرمانده هیچ راهی برای حمله ندارد؟ (موشک ندارد و برنزِ خرید هم نه)"""
    if not player:
        return True
    if count_offensive(player) > 0:
        return False
    return int(player.get("bronze") or 0) < CHEAPEST_OFFENSIVE


def check_end(match):
    """اگر مسابقه باید تمام شود، ``(winner_id, loser_id, reason)`` برمی‌گرداند.

    پایان‌ها به ترتیب اولویت: نابودی پایگاه، تسلیم، و در آخر «بی‌توانی»
    (فرمانده‌ای که هیچ موشک و بودجهٔ خریدی ندارد نمی‌تواند بجنگد؛ بدون این
    قاعده جنگ با دو طرفِ بی‌موشک تا ابد ادامه می‌یافت).
    """
    if match.get("phase") == PHASE_FINISHED:
        return match.get("winner"), match.get("loser"), match.get("end_reason")
    players = match.get("players", [])
    if len(players) < PLAYERS:
        return None, None, None
    destroyed = [p for p in players if float(p.get("health") or 0) <= 0]
    if destroyed:
        loser = destroyed[0]
        winner = opponent_of(match, loser["user_id"])
        return (winner or {}).get("user_id"), loser["user_id"], END_DESTROYED
    surrendered = [p for p in players if p.get("surrendered")]
    if surrendered:
        loser = surrendered[0]
        winner = opponent_of(match, loser["user_id"])
        return (winner or {}).get("user_id"), loser["user_id"], END_SURRENDER
    if match.get("phase") == PHASE_WAR:
        stuck = [p for p in players if out_of_offense(p)]
        if len(stuck) == 1:
            loser = stuck[0]
            winner = opponent_of(match, loser["user_id"])
            return (winner or {}).get("user_id"), loser["user_id"], END_NO_AMMO
        if len(stuck) == PLAYERS:
            first, second = players
            score_a = commander_score(match, first)
            score_b = commander_score(match, second)
            if score_a == score_b:
                # تساوی کامل → سلامت بیشتر، بعد جمعیت بیشتر تعیین می‌کند.
                score_a += float(first.get("health") or 0) + int(first.get("population") or 0) / 1000
                score_b += float(second.get("health") or 0) + int(second.get("population") or 0) / 1000
            if score_a == score_b:
                return None, None, None
            winner, loser = (first, second) if score_a > score_b else (second, first)
            return winner["user_id"], loser["user_id"], END_STALEMATE
    return None, None, None


def finish(match, winner_id, loser_id, reason):
    """مسابقه را می‌بندد؛ فقط یک بار مؤثر است."""
    if match.get("phase") == PHASE_FINISHED:
        return False
    winner = player_of(match, winner_id)
    loser = player_of(match, loser_id)
    match["phase"] = PHASE_FINISHED
    match["winner"] = int(winner_id) if winner_id else None
    match["loser"] = int(loser_id) if loser_id else None
    match["end_reason"] = reason
    match["finished_at"] = now()
    if winner is not None:
        winner["score"] = commander_score(match, winner)
        winner["finished_at"] = match["finished_at"]
    if loser is not None:
        loser["score"] = commander_score(match, loser)
        loser["finished_at"] = match["finished_at"]
    return True


def surrender(match, user_id):
    if match.get("phase") == PHASE_FINISHED:
        return ST_ALREADY_FINISHED, None
    player = player_of(match, user_id)
    if player is None:
        return ST_NOT_PLAYER, None
    if len(match.get("players", [])) < PLAYERS:
        # هنوز حریف نیامده: مسابقه لغو می‌شود، نه باخت.
        return ST_NOT_READY, None
    player["surrendered"] = True
    opponent = opponent_of(match, user_id)
    return ST_OK, (opponent or {}).get("user_id")


def commander_score(match, player) -> int:
    """امتیاز فرماندهی — ترکیب سلامت، حمله و رهگیری."""
    hits = int(player.get("attacks_successful") or 0)
    intercepts = int(player.get("interceptions") or 0)
    health = float(player.get("health") or 0)
    population = int(player.get("population") or 0)
    score = (health * 3) + (hits * 40) + (intercepts * 20)
    score += int(population / START_POPULATION * 40)
    if player.get("surrendered"):
        score = int(score * 0.35)
    return max(0, int(round(score)))


# ---------------------------------------------------------------------------
# دسترسی و قفل نوبت
# ---------------------------------------------------------------------------
def both_ready(match) -> bool:
    """آیا هر دو فرمانده کشور و تجهیزات را آماده کرده‌اند؟"""
    players = match.get("players", [])
    if len(players) < PLAYERS:
        return False
    for player in players:
        if not player.get("country"):
            return False
        if count_offensive(player) + count_defense_systems(player) + count_radars(player) <= 0:
            return False
    return True


def start_war(match) -> bool:
    if match.get("phase") != PHASE_SETUP:
        return False
    if not both_ready(match):
        return False
    match["phase"] = PHASE_WAR
    match["round"] = int(match.get("round") or 0) + 1
    match["war_started_at"] = now()
    return True


def nuke_available(player) -> bool:
    if int(player.get("nukes_used") or 0) >= NUKES_PER_GAME:
        return False
    owned = sum(int(c) for nid, c in (player.get("arsenal") or {}).items() if is_nuke(nid))
    return owned > 0


def arsenal_list(player) -> list:
    """[(item_id, count)] برای نمایش انبار (بالستیک/کروز/هسته‌ای)."""
    rows = []
    for item_id, count in (player.get("arsenal") or {}).items():
        if int(count) <= 0:
            continue
        if is_offensive(item_id) or is_nuke(item_id):
            rows.append((item_id, int(count)))
    return rows


def defense_list(player) -> list:
    rows = []
    for item_id, capacity in (player.get("defense") or {}).items():
        if int(capacity) <= 0:
            continue
        rows.append((item_id, int(capacity)))
    return rows


def radar_label(player) -> str:
    rows = []
    for radar_id, count in (player.get("radars") or {}).items():
        item = WEAPONS.get(radar_id) or {}
        rows.append(f"{item.get('name')} × {count}")
    return "، ".join(rows) if rows else "ندارد"


def can_declare(player) -> bool:
    return count_offensive(player) > 0 or nuke_available(player)
