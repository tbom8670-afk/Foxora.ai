# -*- coding: utf-8 -*-
"""🦊 هستهٔ اجرایی «مرکز فرماندهی روباه» — state، تایمرها، پرداخت جایزه.

این ماژول تنهای جای state بازی است. قواعد:

* **همهٔ محاسبات از ``engine``** می‌آید؛ اینجا هیچ عدد تعادل بازی نیست.
* **پایداری کامل:** هر تغییر با ``storage.save_match`` روی دیسک می‌نشیند،
  پس ری‌استارت ربات هیچ مسابقه‌ای را از بین نمی‌برد (``recover``).
* **اتمیک بودن:** هر عملیات داخل قفل همان چت اجرا می‌شود؛ حمله و دفاع
  هم‌زمان نمی‌توانند یک خسارت را دو بار ثبت کنند (``attack["settled"]``).
* **پرداخت واقعی جایزه:** فقط از راه ``economy.award`` با مرجع یکتا
  ``fox_command:<match_id>``؛ دفتر تراکنش اقتصاد مرجع تکراری را رد می‌کند،
  پس جایزه هرگز دو بار پرداخت نمی‌شود.
* **بدون تداخل با کیف پول:** برنز بازی داخل همین فایل مسابقه نگه داشته
  می‌شود و هیچ‌جا از موجودی واقعی کاربر کم نمی‌شود.
"""
from __future__ import annotations

import asyncio
import time

from modules.fox_command import (
    COMMAND,
    COMMAND_ALT,
    COMMAND_EN,
    DEFENSE_SECONDS,
    JOIN_SECONDS,
    JOIN_WORD,
    PLAYERS,
    START_BRONZE,
    WINNER_SILVER,
    engine,
    storage,
    texts,
)
from modules.fox_command import texts as T
from modules.fox_command.engine import (
    ST_ALREADY_DEFENDED,
    ST_ALREADY_FINISHED,
    ST_BAD_CHOICE,
    ST_COUNTRY_TAKEN,
    ST_DUPLICATE,
    ST_FULL,
    ST_NUKE_LIMIT,
    ST_NUKE_ROUND,
    ST_NOT_DEFENDER,
    ST_NOT_PLAYER,
    ST_NOT_READY,
    ST_NO_ATTACK,
    ST_NO_BUDGET,
    ST_OK,
    ST_SOLD_OUT,
    ST_TOO_MANY,
)

try:  # مسیر عادی ربات؛ در تست‌های سبک ممکن است در دسترس نباشد
    from modules.fox_games.session_core import log, log_error, normalize_text
except Exception:  # pragma: no cover
    def log(_logger, _message):
        return None

    def log_error(_logger, _message):
        return None

    def normalize_text(value):
        return " ".join(str(value or "").strip().lower().split())


GAME_KEY = "fox_command"
#: دستورهای *شروع* بازی. «شرکت» عمداً اینجا نیست: «شرکت» فقط وقتی به این
#: بازی می‌رسد که مسابقه‌ای در همان گروه باز باشد، وگرنه دست‌نخورده به
#: بازی‌های دیگر (که همین کلمه را استفاده می‌کنند) می‌رسد.
START_COMMANDS = frozenset({
    normalize_text(COMMAND), normalize_text(COMMAND_ALT), COMMAND_EN,
})
GAME_COMMANDS = frozenset({
    normalize_text("پایگاه"), normalize_text("فروشگاه"), normalize_text("حمله"),
    normalize_text("دفاع"), normalize_text("وضعیت پایگاه"),
    normalize_text("گزارش جنگ"), normalize_text("تعمیر پایگاه"),
    normalize_text("تسلیم"), normalize_text("انصراف"), normalize_text("بازگشت"),
})

# ---------------------------------------------------------------------------
# state درون‌حافظه‌ای (منبع حقیقت = دیسک)
# ---------------------------------------------------------------------------
_MATCHES = {}       # chat_id → match dict
_LOCKS = {}         # chat_id → asyncio.Lock
_PENDING = {}       # chat_id → {"user_id", "kind", "deadline", "data"}
_TASKS = {}         # key → asyncio.Task
_RECOVERED = [False]

IDLE_HINT_SETUP = 150      # یادآوری آماده‌سازی
_PENDING_TIMEOUT = 90      # مهلت پاسخ منوها


# ---------------------------------------------------------------------------
# ابزار
# ---------------------------------------------------------------------------
def _lock(chat_id):
    lock = _LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[chat_id] = lock
    return lock


def _cancel(key):
    task = _TASKS.pop(key, None)
    if task is not None and not task.done():
        try:
            task.cancel()
        except Exception:
            pass


def _schedule(key, coro):
    _cancel(key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(coro, name=f"fox-command-{key}")
    _TASKS[key] = task
    return task


def _save(match):
    try:
        storage.save_match(match)
    except Exception as error:  # pragma: no cover
        log_error(None, f"FOX COMMAND SAVE FAILED match={match.get('match_id')} "
                        f"error={error!r}")


def match_of(chat_id):
    return _MATCHES.get(chat_id)


def is_active(chat_id):
    match = _MATCHES.get(chat_id)
    return bool(match) and match.get("phase") != engine.PHASE_FINISHED


def active_count(chat_id=None):
    if chat_id is not None:
        return 1 if is_active(chat_id) else 0
    return sum(1 for cid in list(_MATCHES) if is_active(cid))


def phase(chat_id):
    match = _MATCHES.get(chat_id)
    return (match or {}).get("phase") or "none"


def reset_all(chat_id=None):
    """بستن state درون‌حافظه (مثل بقیهٔ بازی‌ها) — دیسک دست‌نخورده می‌ماند."""
    targets = [chat_id] if chat_id is not None else list(_MATCHES)
    for cid in targets:
        _MATCHES.pop(cid, None)
        _pending_pop(cid)
        for key in [k for k in _TASKS if k[0] == cid]:
            _cancel(key)
    if chat_id is None:
        _LOCKS.clear()


# ---------------------------------------------------------------------------
# ارسال پیام (با fallbackهای امن)
# ---------------------------------------------------------------------------
async def _send(bot, chat_id, built, *, event=None, logger=None):
    text, entities = built
    if not text:
        return None
    if event is not None:
        try:
            return await event.reply(text, formatting_entities=entities)
        except Exception as error:
            log_error(logger, f"FOX COMMAND REPLY FAILED chat_id={chat_id} "
                              f"error={error!r}")
    client = getattr(bot, "client", None)
    sender = getattr(client, "send_message", None)
    if callable(sender):
        try:
            return await sender(chat_id, text, formatting_entities=entities)
        except Exception as error:
            log_error(logger, f"FOX COMMAND SEND FAILED chat_id={chat_id} "
                              f"error={error!r}")
            try:
                return await sender(chat_id, text)
            except Exception as second:  # pragma: no cover
                log_error(logger, f"FOX COMMAND SEND PLAIN FAILED "
                                  f"chat_id={chat_id} error={second!r}")
    return None


def _display(user, fallback="فرمانده"):
    try:
        from modules.user_display import format_user
        return format_user(user, fallback)
    except Exception:
        name = getattr(user, "username", None) or getattr(user, "first_name", None)
        return str(name or fallback)


# ---------------------------------------------------------------------------
# شروع مسابقه
# ---------------------------------------------------------------------------
async def start(bot, event, chat_id, user_id, sender=None, logger=None):
    """دستور شروع. ``True`` یعنی پیام مصرف شد."""
    existing = _MATCHES.get(chat_id)
    if existing and existing.get("phase") != engine.PHASE_FINISHED:
        await _send(bot, chat_id, T.join_full(), event=event, logger=logger)
        return True
    try:
        from modules import entertainment_control
        if await entertainment_control.guard(event, chat_id, COMMAND):
            return True
    except Exception:
        pass

    match_id = storage.new_match_id(chat_id)
    match = engine.new_match(chat_id, match_id)
    match["join_deadline"] = time.time() + JOIN_SECONDS
    _MATCHES[chat_id] = match
    _save(match)
    log(logger, f"FOX COMMAND START chat_id={chat_id} match_id={match_id}")

    await _send(bot, chat_id, T.start_prompt(), event=event, logger=logger)
    status, player = engine.join(match, user_id, _display(sender, f"کاربر {user_id}"))
    if status == ST_OK:
        _save(match)
        log(logger, f"FOX COMMAND JOIN chat_id={chat_id} user_id={user_id} "
                    f"status={status} players=1")
    _schedule((chat_id, "join"), _join_timeout(bot, chat_id, match_id, logger))
    return True


async def _join(bot, event, chat_id, user_id, sender, logger=None):
    """الحاق فرماندهٔ دوم با «شرکت» (تکرار ثبت‌نام و نفر سوم رد می‌شوند)."""
    match = _MATCHES.get(chat_id)
    if not match:
        return False
    async with _lock(chat_id):
        status, player = engine.join(
            match, user_id, _display(sender, f"کاربر {user_id}"))
        if status == ST_OK:
            _save(match)
    log(logger, f"FOX COMMAND JOIN chat_id={chat_id} user_id={user_id} "
                f"status={status} players={len(match.get('players', []))}")
    if status == ST_OK:
        await _send(bot, chat_id,
                    T.joined(len(match.get("players", [])), player.get("name")),
                    event=event, logger=logger)
        if len(match.get("players", [])) == PLAYERS:
            _cancel((chat_id, "join"))
            await _send(bot, chat_id, T.both_joined(match), event=event,
                        logger=logger)
            await _after_join(bot, chat_id, logger)
        return True
    if status == ST_DUPLICATE:
        await _send(bot, chat_id, T.join_duplicate(), event=event, logger=logger)
        return True
    if status == ST_FULL:
        await _send(bot, chat_id, T.join_full(), event=event, logger=logger)
        return True
    await _send(bot, chat_id, T.bad_choice_notice(), event=event, logger=logger)
    return True


async def _after_join(bot, chat_id, logger=None):
    """پس از کامل شدن ثبت‌نام: منوی انتخاب کشور برای فرماندهٔ اول."""
    match = _MATCHES.get(chat_id)
    if not match:
        return
    waiting = engine.missing_country(match)
    if waiting is None:
        return
    _set_pending(chat_id, waiting["user_id"], "country")
    await _send(bot, chat_id, T.country_menu(match, waiting), logger=logger)
    log(logger, f"FOX COMMAND COUNTRY PROMPT chat_id={chat_id} "
                f"user_id={waiting['user_id']}")
    _schedule((chat_id, "country-hint"), _country_hint(bot, chat_id, logger))


async def _country_hint(bot, chat_id, logger=None):
    try:
        await asyncio.sleep(IDLE_HINT_SETUP)
    except asyncio.CancelledError:
        return
    match = _MATCHES.get(chat_id)
    if not match or match.get("phase") not in (engine.PHASE_COUNTRY,
                                               engine.PHASE_JOINING):
        return
    waiting = engine.missing_country(match)
    if waiting is None:
        return
    msg = T.Message()
    msg.title("در انتظار انتخاب کشور ⏳")
    msg.line(f"فرمانده: {waiting.get('name')}")
    msg.quote("شمارهٔ کشور را از منوی انتخاب کشور بفرستید.")
    await _send(bot, chat_id, msg.build(), logger=logger)


async def _join_timeout(bot, chat_id, match_id, logger=None):
    try:
        await asyncio.sleep(JOIN_SECONDS + 1)
    except asyncio.CancelledError:
        return
    async with _lock(chat_id):
        match = _MATCHES.get(chat_id)
        if not match or match.get("match_id") != match_id:
            return
        if match.get("phase") != engine.PHASE_JOINING:
            return
        players = match.get("players", [])
        match["phase"] = engine.PHASE_FINISHED
        match["end_reason"] = "no_opponent"
        match["finished_at"] = time.time()
        _save(match)
        try:
            storage.archive_match(match)
        except Exception:
            pass
        _MATCHES.pop(chat_id, None)
    msg = T.Message()
    msg.title("مهلت ثبت‌نام تمام شد ⏰")
    msg.quote("فرماندهٔ دومی نیامد و مسابقه بسته شد. برای شروع دوباره دستور بازی را بفرستید.")
    await _send(bot, chat_id, msg.build(), logger=logger)
    log(logger, f"FOX COMMAND JOIN TIMEOUT chat_id={chat_id} "
                f"players={len(players)}")


# ---------------------------------------------------------------------------
# پردازش پیام‌ها
# ---------------------------------------------------------------------------
def claims(chat_id, text) -> bool:
    """آیا این پیام مربوط به بازی «مرکز فرماندهی» است؟

    فقط وقتی True است که مسابقه‌ای در این گروه باز باشد یا متن، دستور
    شروع بازی باشد. بنابراین هیچ دستور دیگری از ربات را نمی‌بلعد.
    """
    normalized = normalize_text(text)
    if normalized in START_COMMANDS:
        return True
    if not is_active(chat_id):
        return False
    if normalized == normalize_text(JOIN_WORD):
        return True
    return normalized in GAME_COMMANDS or _is_number(normalized)


def _is_number(text) -> bool:
    return normalize_text(text).isdigit() or _digits(normalize_text(text)) is not None


def _digits(text):
    """ارقام فارسی/عربی → لاتین؛ ``None`` اگر عدد نباشد."""
    if not text:
        return None
    table = {ord("۰") + i: str(i) for i in range(10)}
    table.update({ord("٠") + i: str(i) for i in range(10)})
    converted = str(text).translate(table)
    converted = converted.strip()
    if converted.isdigit():
        return int(converted)
    return None


async def handle_message(bot, event, chat_id, user_id, sender, text,
                         logger=None):
    """پیام درون‌بازی. ``True`` یعنی مصرف شد."""
    normalized = normalize_text(text)
    if not is_active(chat_id):
        if normalized in START_COMMANDS:
            return await start(bot, event, chat_id, user_id, sender, logger)
        return False

    match = _MATCHES[chat_id]
    if normalized in START_COMMANDS:
        await _send(bot, chat_id, T.join_full(), event=event, logger=logger)
        return True

    # «شرکت» = الحاق فرماندهٔ دوم، فقط در مرحلهٔ ثبت‌نام و فقط یک بار.
    if normalized == normalize_text(JOIN_WORD):
        if match.get("phase") == engine.PHASE_JOINING:
            return await _join(bot, event, chat_id, user_id, sender, logger)
        await _send(bot, chat_id, T.join_full(), event=event, logger=logger)
        return True

    player = engine.player_of(match, user_id)
    if player is None:
        # پیام‌های غیرفرمانده‌ها به بازی کاری ندارند.
        return False

    # جواب منوی باز (عدد) — فقط از همان کاربر و پیش از مهلت
    if _is_number(normalized):
        if await _handle_pending_number(bot, chat_id, user_id, normalized,
                                        logger, event=event):
            return True

    if normalized == normalize_text("پایگاه"):
        await _send(bot, chat_id, T.base_card(match, player, full=False),
                    event=event, logger=logger)
        return True
    if normalized == normalize_text("وضعیت پایگاه"):
        opponent = engine.opponent_of(match, user_id)
        await _send(bot, chat_id,
                    T.base_card(match, player, full=True, opponent=opponent),
                    event=event, logger=logger)
        return True
    if normalized == normalize_text("فروشگاه"):
        if not await _require_country(bot, chat_id, player, event, logger):
            return True
        if match.get("phase") == engine.PHASE_FINISHED:
            await _send(bot, chat_id, T.closed_notice(), event=event, logger=logger)
            return True
        await _open_shop(bot, chat_id, user_id, event, logger)
        return True
    if normalized == normalize_text("حمله"):
        await _open_attack(bot, chat_id, user_id, event, logger)
        return True
    if normalized == normalize_text("دفاع"):
        await _open_defense(bot, chat_id, user_id, event, logger)
        return True
    if normalized == normalize_text("تعمیر پایگاه"):
        await _open_repair(bot, chat_id, user_id, event, logger)
        return True
    if normalized == normalize_text("گزارش جنگ"):
        await _send(bot, chat_id, T.war_report(match), event=event, logger=logger)
        return True
    if normalized == normalize_text("تسلیم"):
        await _surrender(bot, chat_id, user_id, event, logger)
        return True
    if normalized in (normalize_text("انصراف"), normalize_text("بازگشت")):
        _pending_pop(chat_id)
        await _send(bot, chat_id, T.base_card(match, player, full=False),
                    event=event, logger=logger)
        return True
    # متن دیگر: بلعیده نمی‌شود تا پیام عادی گروه کار خودش را بکند.
    return False


async def _require_country(bot, chat_id, player, event, logger):
    if player.get("country"):
        return True
    await _send(bot, chat_id, T.need_country_notice(), event=event, logger=logger)
    return False


# ---------------------------------------------------------------------------
# منوها و پاسخ عددی
# ---------------------------------------------------------------------------
def _set_pending(chat_id, user_id, kind, data=None, timeout=_PENDING_TIMEOUT):
    """منوی باز هر فرمانده جداست تا دو بازیکن هم‌زمان همدیگر را خراب نکنند."""
    _PENDING[(chat_id, int(user_id))] = {
        "user_id": int(user_id),
        "kind": kind,
        "data": data or {},
        "deadline": time.time() + timeout,
    }


def _pending_get(chat_id, user_id):
    return _PENDING.get((chat_id, int(user_id)))


def _pending_pop(chat_id, user_id=None):
    if user_id is None:
        for key in [k for k in _PENDING if k[0] == chat_id]:
            _PENDING.pop(key, None)
    else:
        _PENDING.pop((chat_id, int(user_id)), None)


async def _open_shop(bot, chat_id, user_id, event, logger):
    match = _MATCHES[chat_id]
    player = engine.player_of(match, user_id)
    _set_pending(chat_id, user_id, "shop_category")
    await _send(bot, chat_id, T.shop_home(player), event=event, logger=logger)


async def _open_attack(bot, chat_id, user_id, event, logger):
    match = _MATCHES[chat_id]
    player = engine.player_of(match, user_id)
    if match.get("phase") != engine.PHASE_WAR:
        await _send(bot, chat_id, T.not_ready_notice(), event=event, logger=logger)
        return
    if not engine.can_declare(player):
        await _send(bot, chat_id, T.attack_menu(player), event=event, logger=logger)
        return
    _set_pending(chat_id, user_id, "attack_item")
    await _send(bot, chat_id, T.attack_menu(player), event=event, logger=logger)


async def _open_defense(bot, chat_id, user_id, event, logger):
    match = _MATCHES[chat_id]
    player = engine.player_of(match, user_id)
    pending_attacks = [a for a in engine.attacks_against(match, user_id)
                       if not a.get("defended") and not a.get("settled")]
    if not pending_attacks:
        await _send(bot, chat_id, T.no_attack_notice(), event=event, logger=logger)
        return
    attack = pending_attacks[0]
    _set_pending(chat_id, user_id, "defense_system",
                 {"attack_id": attack["attack_id"]},
                 timeout=max(1, attack["defend_deadline"] - time.time()))
    await _send(bot, chat_id, T.defense_menu(attack, player),
                event=event, logger=logger)


async def _open_repair(bot, chat_id, user_id, event, logger):
    match = _MATCHES[chat_id]
    player = engine.player_of(match, user_id)
    _set_pending(chat_id, user_id, "repair")
    await _send(bot, chat_id, T.repair_prompt(player), event=event, logger=logger)


async def _handle_pending_number(bot, chat_id, user_id, normalized, logger,
                                 event=None):
    pending = _pending_get(chat_id, user_id)
    if not pending:
        return False
    if time.time() > float(pending.get("deadline") or 0):
        _pending_pop(chat_id, user_id)
        return False
    number = _digits(normalized)
    if number is None:
        return False
    match = _MATCHES.get(chat_id)
    if not match:
        return False
    player = engine.player_of(match, user_id)
    if player is None:
        return False

    kind = pending.get("kind")
    if kind == "country":
        await _apply_country(bot, chat_id, user_id, number, logger, event=event)
        return True
    if kind == "shop_category":
        category = T.category_of_index(number)
        if not category:
            await _send(bot, chat_id, T.bad_choice_notice(), logger=logger)
            return True
        _set_pending(chat_id, user_id, "shop_item", {"category": category})
        await _send(bot, chat_id, T.shop_category(player, category), logger=logger)
        return True
    if kind == "shop_item":
        category = (pending.get("data") or {}).get("category")
        order = SHOP_LIST(category)
        if not order or not (1 <= number <= len(order)):
            await _send(bot, chat_id, T.bad_choice_notice(), logger=logger)
            return True
        await _apply_purchase(bot, chat_id, user_id, order[number - 1], logger, event=event)
        return True
    if kind == "attack_item":
        mapping = T.attack_menu_index(player)
        item_id = mapping.get(number)
        if item_id is None:
            await _send(bot, chat_id, T.bad_choice_notice(), logger=logger)
            return True
        await _apply_launch(bot, chat_id, user_id, item_id, logger, event=event)
        return True
    if kind == "defense_system":
        mapping = T.defense_menu_index(player)
        item_id = mapping.get(number)
        if item_id is None:
            await _send(bot, chat_id, T.bad_choice_notice(), logger=logger)
            return True
        attack_id = (pending.get("data") or {}).get("attack_id")
        await _apply_defense(bot, chat_id, user_id, item_id, attack_id, logger, event=event)
        return True
    if kind == "repair":
        await _apply_repair(bot, chat_id, user_id, number, logger, event=event)
        return True
    return False


def SHOP_LIST(category):  # noqa: N802 - سازگاری ساده با استفادهٔ داخلی
    from modules.fox_command import SHOP
    return SHOP.get(category) or ()


# ---------------------------------------------------------------------------
# عملیات بازی
# ---------------------------------------------------------------------------
async def _apply_country(bot, chat_id, user_id, number, logger, event=None):
    from modules.fox_command import COUNTRY_ORDER
    match = _MATCHES[chat_id]
    if 1 <= number <= len(COUNTRY_ORDER):
        key = COUNTRY_ORDER[number - 1]
    else:
        await _send(bot, chat_id, T.bad_choice_notice(), logger=logger, event=event)
        return
    async with _lock(chat_id):
        status, player = engine.choose_country(match, user_id, key)
        if status == ST_OK:
            _save(match)
            _pending_pop(chat_id)
        elif status == ST_COUNTRY_TAKEN:
            taken_msg = T.Message()
            taken_msg.title("این کشور انتخاب شده ⛔")
            taken_msg.quote("هر کشور در یک مسابقه فقط یک بار انتخاب می‌شود. کشور دیگری انتخاب کنید.")
            await _send(bot, chat_id, taken_msg.build(), logger=logger, event=event)
            _set_pending(chat_id, user_id, "country")
            return
        elif status == ST_DUPLICATE:
            _pending_pop(chat_id)
        else:
            await _send(bot, chat_id, T.bad_choice_notice(), logger=logger, event=event)
            return
    log(logger, f"FOX COMMAND COUNTRY chat_id={chat_id} user_id={user_id} "
                f"country={key} status={status}")
    await _send(bot, chat_id, T.country_saved(match, player), logger=logger, event=event)
    await _after_country(bot, chat_id, logger)


async def _after_country(bot, chat_id, logger):
    match = _MATCHES.get(chat_id)
    if not match:
        return
    waiting = engine.missing_country(match)
    if waiting is not None:
        _set_pending(chat_id, waiting["user_id"], "country")
        menu = T.country_menu(match, waiting)
        await _send(bot, chat_id, menu, logger=logger)
        log(logger, f"FOX COMMAND COUNTRY PROMPT chat_id={chat_id} "
                    f"user_id={waiting['user_id']}")
        return
    msg = T.Message()
    msg.title("همه کشور انتخاب کردند ✅")
    msg.quote("حالا با دستور «فروشگاه» تجهیزات بخرید. جنگ وقتی شروع می‌شود که هر دو فرمانده آماده باشند.")
    await _send(bot, chat_id, msg.build(), logger=logger)
    _schedule((chat_id, "setup-hint"), _setup_hint(bot, chat_id, logger))


async def _war_watch(bot, chat_id, logger=None):
    """نگهبان جنگ: بی‌تحرکی طولانی یا بی‌توانیِ یک طرف را پایان می‌دهد.

    بدون این نگهبان، اگر هر دو فرمانده موشکشان تمام شود و کسی خرید نکند،
    مسابقه تا ابد «فعال» می‌ماند و گروه قفل می‌شود.
    """
    from modules.fox_command import WAR_IDLE_SECONDS
    while True:
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            return
        match = _MATCHES.get(chat_id)
        if not match or match.get("phase") != engine.PHASE_WAR:
            return
        winner_id, loser_id, reason = engine.check_end(match)
        if winner_id and reason in (engine.END_NO_AMMO, engine.END_STALEMATE):
            await _finish_match(bot, match, winner_id, loser_id, reason, logger)
            return
        last = max([float(a.get("settled_at") or a.get("launched_at") or 0)
                    for a in match.get("attacks", [])] or
                   [float(match.get("war_started_at") or 0)])
        if time.time() - last >= WAR_IDLE_SECONDS:
            log(logger, f"FOX COMMAND WAR IDLE chat_id={chat_id} "
                        f"idle_s={int(time.time() - last)}")
            await _finish_match(bot, match, winner_id, loser_id,
                                engine.END_STALEMATE if winner_id else
                                engine.END_STALEMATE, logger)
            return


async def _setup_hint(bot, chat_id, logger):
    try:
        await asyncio.sleep(IDLE_HINT_SETUP)
    except asyncio.CancelledError:
        return
    match = _MATCHES.get(chat_id)
    if not match or match.get("phase") != engine.PHASE_SETUP:
        return
    msg = T.Message()
    msg.title("یادآوری آماده‌سازی ⏳")
    for player in match.get("players", []):
        msg.line(f"{engine.country_title(player)} — {player.get('name')}")
        msg.line(f"موشک: {T.fa(engine.count_offensive(player))} | دفاع: "
                 f"{T.fa(engine.count_defense_systems(player))} | "
                 f"رادار: {T.fa(engine.count_radars(player))} | برنز: "
                 f"{T.fa(player.get('bronze'))}")
        msg.blank()
    msg.quote("با دستور «فروشگاه» خرید را کامل کنید تا جنگ شروع شود.")
    await _send(bot, chat_id, msg.build(), logger=logger)


async def _apply_purchase(bot, chat_id, user_id, item_id, logger, event=None):
    match = _MATCHES[chat_id]
    async with _lock(chat_id):
        status, info = engine.purchase(match, user_id, item_id)
        if status == ST_OK:
            _save(match)
        player = engine.player_of(match, user_id)
    log(logger, f"FOX COMMAND BUY chat_id={chat_id} user_id={user_id} "
                f"item={item_id} status={status}")
    if status == ST_OK:
        await _send(bot, chat_id, T.bought(player, info["item"]), logger=logger, event=event)
        await _maybe_start_war(bot, chat_id, logger)
    else:
        await _send(bot, chat_id, T.buy_failed(status, info), logger=logger, event=event)


async def _maybe_start_war(bot, chat_id, logger):
    match = _MATCHES.get(chat_id)
    if not match or match.get("phase") != engine.PHASE_SETUP:
        return
    async with _lock(chat_id):
        if not engine.start_war(match):
            return
        _save(match)
    _cancel((chat_id, "setup-hint"))
    _schedule((chat_id, "war-watch"), _war_watch(bot, chat_id, logger))
    log(logger, f"FOX COMMAND WAR START chat_id={chat_id} "
                f"match_id={match.get('match_id')}")
    await _send(bot, chat_id, T.war_started(match), logger=logger)


async def _apply_launch(bot, chat_id, user_id, item_id, logger, event=None):
    match = _MATCHES[chat_id]
    attack_id = f"{match.get('match_id')}-{int(time.time() * 1000)}-{user_id}"
    async with _lock(chat_id):
        status, attack = engine.launch(match, user_id, item_id, attack_id)
        if status == ST_OK:
            _save(match)
        player = engine.player_of(match, user_id)
    log(logger, f"FOX COMMAND LAUNCH chat_id={chat_id} user_id={user_id} "
                f"item={item_id} status={status} attack={getattr(attack, 'get', lambda *_: None)('attack_id') if attack else None}")
    if status != ST_OK:
        if status in (ST_NUKE_LIMIT, ST_NUKE_ROUND):
            await _send(bot, chat_id, T.nuke_blocked(status), logger=logger, event=event)
        elif status == ST_TOO_MANY:
            await _send(bot, chat_id, T.too_many_attacks(), logger=logger, event=event)
        elif status == ST_SOLD_OUT:
            msg = T.Message()
            msg.title("این موشک در انبار نیست ⛔")
            msg.quote("موجودی انبار خود را با «حمله» ببینید.")
            await _send(bot, chat_id, msg.build(), logger=logger, event=event)
        else:
            await _send(bot, chat_id, T.bad_choice_notice(), logger=logger, event=event)
        return

    defender = engine.player_of(match, attack["defender_id"])
    await _send(bot, chat_id,
                T.attack_sent(attack, (defender or {}).get("name") or "فرمانده",
                              attack.get("detected")),
                logger=logger, event=event)
    remaining = max(0, int(attack["defend_deadline"] - time.time()))
    await _send(bot, chat_id,
                T.incoming_warning(attack, engine.country_title(player), remaining),
                logger=logger, event=event)
    _schedule((chat_id, f"defense-{attack['attack_id']}"),
              _defense_timer(bot, chat_id, attack["attack_id"], logger))


async def _defense_timer(bot, chat_id, attack_id, logger):
    try:
        match = _MATCHES.get(chat_id)
        attack = engine.attack_by_id(match or {}, attack_id)
        if attack is None:
            return
        wait = max(0.0, float(attack["defend_deadline"]) - time.time()) + 0.5
        await asyncio.sleep(wait)
    except asyncio.CancelledError:
        return
    await _settle_attack(bot, chat_id, attack_id, "timeout", logger)


async def _apply_defense(bot, chat_id, user_id, system_id, attack_id, logger,
                         event=None):
    match = _MATCHES[chat_id]
    async with _lock(chat_id):
        status, attack, item = engine.defend(match, user_id, system_id, attack_id)
        if status == ST_OK:
            _save(match)
    log(logger, f"FOX COMMAND DEFEND chat_id={chat_id} user_id={user_id} "
                f"system={system_id} status={status}")
    if status == ST_OK:
        _pending_pop(chat_id)
        _cancel((chat_id, f"defense-{attack_id}"))
        await _send(bot, chat_id, T.defense_registered(attack, item), logger=logger, event=event)
        await _settle_attack(bot, chat_id, attack_id, "defended", logger)
        return
    if status == ST_NOT_DEFENDER:
        await _send(bot, chat_id, T.not_defender_notice(), logger=logger, event=event)
    elif status in (ST_NO_ATTACK, ST_ALREADY_DEFENDED):
        await _send(bot, chat_id, T.attack_expired_notice(), logger=logger, event=event)
    elif status == ST_NOT_PLAYER:
        await _send(bot, chat_id, T.not_player_notice(), logger=logger, event=event)
    else:
        await _send(bot, chat_id, T.bad_choice_notice(), logger=logger, event=event)


async def _settle_attack(bot, chat_id, attack_id, reason, logger):
    """تعیین تکلیف حمله + گزارش + بررسی پایان مسابقه (فقط یک بار)."""
    report = None
    match = _MATCHES.get(chat_id)
    if not match:
        return
    async with _lock(chat_id):
        attack, report = engine.settle(match, attack_id, reason)
        if report is not None:
            _save(match)
    if report is None:
        return
    log(logger, f"FOX COMMAND SETTLE chat_id={chat_id} attack={attack_id} "
                f"result={attack.get('result')} damage={attack.get('damage')} "
                f"reason={reason}")
    await _send(bot, chat_id, T.attack_report(report), logger=logger)
    winner_id, loser_id, end_reason = engine.check_end(match)
    if winner_id:
        await _finish_match(bot, match, winner_id, loser_id, end_reason, logger)


async def _apply_repair(bot, chat_id, user_id, percent, logger, event=None):
    match = _MATCHES[chat_id]
    async with _lock(chat_id):
        status, info = engine.repair(match, user_id, percent)
        if status == ST_OK:
            _save(match)
        player = engine.player_of(match, user_id)
    log(logger, f"FOX COMMAND REPAIR chat_id={chat_id} user_id={user_id} "
                f"percent={percent} status={status}")
    if status == ST_OK:
        _pending_pop(chat_id)
        await _send(bot, chat_id, T.repair_done(player, info), logger=logger, event=event)
    else:
        await _send(bot, chat_id, T.repair_failed(status, info), logger=logger, event=event)


async def _surrender(bot, chat_id, user_id, event, logger):
    match = _MATCHES[chat_id]
    async with _lock(chat_id):
        status, opponent_id = engine.surrender(match, user_id)
        if status == ST_OK:
            _save(match)
    log(logger, f"FOX COMMAND SURRENDER chat_id={chat_id} user_id={user_id} "
                f"status={status}")
    if status == ST_NOT_READY:
        msg = T.Message()
        msg.title("هنوز حریفی ندارید ⛔")
        msg.quote("تسلیم وقتی معنا دارد که هر دو فرمانده در مسابقه باشند.")
        await _send(bot, chat_id, msg.build(), event=event, logger=logger)
        return
    if status == ST_ALREADY_FINISHED:
        await _send(bot, chat_id, T.already_finished(), event=event, logger=logger)
        return
    opponent = engine.player_of(match, opponent_id)
    await _send(bot, chat_id, T.surrender_done(match, opponent),
                event=event, logger=logger)
    await _finish_match(bot, match, opponent_id, user_id, engine.END_SURRENDER,
                        logger)


# ---------------------------------------------------------------------------
# پایان مسابقه و پرداخت جایزه
# ---------------------------------------------------------------------------
def _pay_reward(bot, match, winner_id, logger):
    """۱۰ سکه نقره به کیف پول واقعی برنده — با مرجع یکتا (فقط یک بار)."""
    chat_id = match.get("chat_id")
    reference = f"{GAME_KEY}:{match.get('match_id')}"
    if match.get("reward_paid"):
        log(logger, f"FOX COMMAND REWARD SKIP match={match.get('match_id')} "
                    f"reason=already_paid")
        return False
    winner = engine.player_of(match, winner_id)
    override = getattr(bot, "award_silver", None)
    try:
        if callable(override):
            override(chat_id, winner_id, WINNER_SILVER)
        else:
            from economy import award
            from economy import settings as economy_settings
            award(chat_id, winner_id, WINNER_SILVER, economy_settings.SILVER,
                  reference=reference, note="جایزه مرکز فرماندهی روباه",
                  name=(winner or {}).get("name"), win=True)
        match["reward_paid"] = True
        match["reward_reference"] = reference
        if winner is not None:
            winner["reward_paid"] = True
        log(logger, f"FOX COMMAND REWARD PAID chat_id={chat_id} "
                    f"user_id={winner_id} silver={WINNER_SILVER} "
                    f"reference={reference}")
        return True
    except Exception as error:
        log_error(logger, f"FOX COMMAND REWARD FAILED chat_id={chat_id} "
                          f"user_id={winner_id} error={error!r}")
        return False


async def _finish_match(bot, match, winner_id, loser_id, reason, logger):
    """بستن مسابقه، ارسال پیام‌ها و پرداخت جایزه — فقط یک بار."""
    if match.get("phase") == engine.PHASE_FINISHED:
        return
    chat_id = match.get("chat_id")
    for key in [k for k in _TASKS if k[0] == chat_id]:
        _cancel(key)
    engine.finish(match, winner_id, loser_id, reason)
    paid = _pay_reward(bot, match, winner_id, logger)
    _save(match)
    try:
        storage.archive_match(match)
    except Exception as error:  # pragma: no cover
        log_error(logger, f"FOX COMMAND ARCHIVE FAILED "
                          f"match={match.get('match_id')} error={error!r}")
    winner = engine.player_of(match, winner_id)
    loser = engine.player_of(match, loser_id)
    if winner is not None:
        await _send(bot, chat_id, T.winner_message(match, winner, paid),
                    logger=logger)
    if loser is not None:
        await _send(bot, chat_id, T.loser_message(match, loser, reason),
                    logger=logger)
    _MATCHES.pop(chat_id, None)
    _pending_pop(chat_id)
    log(logger, f"FOX COMMAND FINISH chat_id={chat_id} "
                f"match={match.get('match_id')} winner={winner_id} "
                f"loser={loser_id} reason={reason} reward_paid={paid}")


# ---------------------------------------------------------------------------
# بازیابی پس از ری‌استارت
# ---------------------------------------------------------------------------
async def recover(bot, logger=None):
    """مسابقه‌های باز را از دیسک برمی‌گرداند و تایمرها را دوباره می‌چیند."""
    if _RECOVERED[0]:
        return 0
    _RECOVERED[0] = True
    try:
        storage.ensure_dirs()
        matches = storage.all_open_matches()
    except Exception as error:  # pragma: no cover
        log_error(logger, f"FOX COMMAND RECOVER FAILED error={error!r}")
        return 0
    restored = 0
    for match in matches:
        chat_id = match.get("chat_id")
        if chat_id is None:
            continue
        _MATCHES[chat_id] = match
        restored += 1
        # حمله‌های در جریان که مهلتشان گذشته → همین حالا تعیین تکلیف
        for attack in engine.active_attacks(match):
            if time.time() > float(attack.get("defend_deadline") or 0):
                _schedule((chat_id, f"catchup-{attack['attack_id']}"),
                          _settle_attack(_BotProxy(bot), chat_id,
                                         attack["attack_id"], "recovered", logger))
            else:
                _schedule((chat_id, f"defense-{attack['attack_id']}"),
                          _defense_timer(_BotProxy(bot), chat_id,
                                         attack["attack_id"], logger))
        if match.get("phase") == engine.PHASE_SETUP:
            _schedule((chat_id, "setup-hint"),
                      _setup_hint(_BotProxy(bot), chat_id, logger))
    log(logger, f"FOX COMMAND RECOVERED matches={restored}")
    return restored


class _BotProxy:
    """پوشش نازک برای ارسال پیام در تایمرها (بدون event)."""

    def __init__(self, bot):
        self._bot = bot

    def __getattr__(self, item):
        return getattr(self._bot, item)

    @property
    def client(self):
        return getattr(self._bot, "client", None)
