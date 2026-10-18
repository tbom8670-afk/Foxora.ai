"""🧟‍♂ زامبی — بازی گروهی ۴ نفره با نقش‌های مخفی و ادامهٔ مرحلهٔ «گروه آخرالزمان»."""
import asyncio
import random
import time

try:
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:
    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

from modules.fox_games.session_core import (
    SessionStore,
    display_name,
    log,
    log_error,
    normalize_text,
    to_persian_digits,
)

GAME_NAME = "zombie"
COMMAND = "زامبی"
JOIN_WORD = "شرکت"

REQUIRED_PLAYERS = 4
MIN_PLAYERS = 4
MAX_PLAYERS = 4
JOIN_SECONDS = 50
BITE_SECONDS = 17

ZOMBIE_WIN_SILVER = 5
BAIT_INFECTED_SILVER = 5
HUNTER_WIN_BRONZE = 7
SNIPER_WIN_BRONZE = 7

DM_TIMEOUT = 12
DM_RETRIES = 3

ROLE_ZOMBIE = "zombie"
ROLE_HUNTER = "hunter"
ROLE_SNIPER = "sniper"
ROLE_BAIT = "bait"

ALL_ROLES = (
    ROLE_ZOMBIE,
    ROLE_HUNTER,
    ROLE_SNIPER,
    ROLE_BAIT,
)

ROLE_LABELS = {
    ROLE_ZOMBIE: "زامبی",
    ROLE_HUNTER: "شکارچی",
    ROLE_SNIPER: "تک‌تیرانداز",
    ROLE_BAIT: "طعمه (انسان)",
}

_STORE = SessionStore(GAME_NAME)
_RANDOM = random.SystemRandom()

# وضعیت مرحلهٔ ادامه («گروه آخرالزمان») به تفکیک هر گروه (chat_id واقعی).
# بدون ساخت شناسهٔ جعلی یا ادعای ساخت گروه واقعی خارج از امکانات موجود.
_APOCALYPSE_BY_CHAT = {}

ALREADY_RUNNING = "🧟‍♂ بازی زامبی همین حالا در این گروه در جریان است."
NOT_ENOUGH = "🧟‍♂ تعداد شرکت‌کننده به ۴ نفر نرسید؛ بازی زامبی لغو شد."
DM_FAILED_MESSAGE = (
    "🧟‍♂ ارسال پیام خصوصی نقش‌ها انجام نشد و بازی زامبی لغو گردید."
)
TIMEOUT_MESSAGE = (
    "⏰ مهلت ۱۷ ثانیه انتخاب زامبی به پایان رسید و بازی بدون جایزه تمام شد."
)


def _u16(text):
    """طول رشته برحسب UTF-16 code units برای MessageEntityهای سروش پلاس."""
    return len(str(text or "").encode("utf-16-le")) // 2


def _clean_username(user):
    if user is None:
        return ""
    username = getattr(user, "username", None)
    if isinstance(user, dict):
        username = user.get("username")
    username = str(username or "").strip().lstrip("@").strip()
    if username and not username.isdigit():
        return username
    return ""


def _raw_full_name(user):
    if user is None:
        return ""
    if isinstance(user, dict):
        first, last = user.get("first_name"), user.get("last_name")
    else:
        first, last = getattr(user, "first_name", None), getattr(user, "last_name", None)
    parts = [
        str(p).replace("\n", " ").replace("\r", " ").strip()
        for p in (first, last)
        if p and str(p).strip()
    ]
    return " ".join(" ".join(parts).split())


def _clean_first_name(user):
    if user is None:
        return ""
    if isinstance(user, dict):
        first = user.get("first_name")
    else:
        first = getattr(user, "first_name", None)
    return " ".join(str(first or "").replace("\n", " ").replace("\r", " ").split())


def make_player_entry(user_id, user):
    """ساخت رکورد استاندارد بازیکن هنگام ثبت‌نام با «شرکت»."""
    shown = display_name(user)
    uname = _clean_username(user)
    raw_name = _raw_full_name(user)
    first_name = _clean_first_name(user)
    return {
        "user_id": user_id,
        "name": shown,
        "username": uname,
        "raw_name": raw_name or shown.lstrip("@"),
        "first_name": first_name,
        "peer": user,
    }


def is_active(chat_id):
    return _STORE.is_active(chat_id)


def phase(chat_id):
    session = _STORE.get(chat_id)
    return session.get("phase") if session else None


def start(chat_id, logger=None, group_event=None):
    """شروع مرحلهٔ ثبت‌نام ۴ نفره با مهلت ۵۰ ثانیه."""
    session = _STORE.create(chat_id, {
        "phase": "joining",
        "players": [],
        "roles": {},
        "by_role": {},
        "targets": [],
        "chosen_target": None,
        "deadline": time.monotonic() + JOIN_SECONDS,
        "bite_deadline": None,
        "full_event": asyncio.Event() if _has_running_loop() else None,
        "group_event": group_event,
        "reward_paid": False,
    })
    if session is None:
        return None
    log(logger, f"FOX ZOMBIE START chat_id={chat_id} session_id={session['session_id']}")
    return session


def _has_running_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def join(chat_id, user_id, user, logger=None):
    """ثبت‌نام بازیکن با «شرکت»؛ هر کاربر فقط یک بار در هر دور."""
    session = _STORE.get(chat_id)
    if not session or session.get("phase") != "joining":
        return "closed", None
    if user_id is None:
        return "closed", None
    if any(p["user_id"] == user_id for p in session["players"]):
        return "duplicate", list(session["players"])
    if len(session["players"]) >= REQUIRED_PLAYERS:
        return "full", list(session["players"])
    entry = make_player_entry(user_id, user)
    session["players"].append(entry)
    log(logger, f"FOX ZOMBIE JOIN chat_id={chat_id} user_id={user_id} "
                f"count={len(session['players'])}/{REQUIRED_PLAYERS}")
    if len(session["players"]) >= REQUIRED_PLAYERS:
        ev = session.get("full_event")
        if ev is not None:
            ev.set()
    return "joined", list(session["players"])


def is_full(chat_id):
    session = _STORE.get(chat_id)
    return bool(session and len(session["players"]) >= REQUIRED_PLAYERS)


def assign_roles(chat_id, logger=None):
    """تخصیص تصادفی ۴ نقش بین ۴ بازیکن ثبت‌نام‌شده."""
    session = _STORE.get(chat_id)
    if not session or len(session["players"]) != REQUIRED_PLAYERS:
        return None
    session["phase"] = "assigning"
    players = [dict(p) for p in session["players"]]
    roles = list(ALL_ROLES)
    _RANDOM.shuffle(roles)

    role_map = {}
    by_role = {}
    for player, role in zip(players, roles):
        p_copy = dict(player)
        p_copy["role"] = role
        p_copy["role_label"] = ROLE_LABELS[role]
        role_map[p_copy["user_id"]] = role
        by_role[role] = p_copy

    # سه بازیکن غیر زامبی به ترتیب تصادفی برای نمایش به زامبی
    targets = [
        dict(by_role[ROLE_HUNTER]),
        dict(by_role[ROLE_SNIPER]),
        dict(by_role[ROLE_BAIT]),
    ]
    _RANDOM.shuffle(targets)

    session["roles"] = role_map
    session["by_role"] = by_role
    session["targets"] = targets

    log(
        logger,
        f"FOX ZOMBIE ROLES ASSIGNED chat_id={chat_id} "
        f"zombie={by_role[ROLE_ZOMBIE]['user_id']} "
        f"hunter={by_role[ROLE_HUNTER]['user_id']} "
        f"sniper={by_role[ROLE_SNIPER]['user_id']} "
        f"bait={by_role[ROLE_BAIT]['user_id']}",
    )
    return {
        "session_id": session["session_id"],
        "players": players,
        "roles": dict(role_map),
        "by_role": {k: dict(v) for k, v in by_role.items()},
        "targets": [dict(t) for t in targets],
    }


def build_role_dm(role):
    """متن و entity پیام خصوصی نقش برای بازیکنان غیر زامبی."""
    label = ROLE_LABELS.get(role, "")
    if role == ROLE_HUNTER:
        text = (
            f"🏹 نقش شما در بازی زامبی: {label}\n"
            "اگر زامبی به اشتباه شما یا تک‌تیرانداز را گاز بزند، زامبی کشته می‌شود و ۷ سکه برنز می‌برید."
        )
    elif role == ROLE_SNIPER:
        text = (
            f"🎯 نقش شما در بازی زامبی: {label}\n"
            "اگر زامبی به اشتباه شما یا شکارچی را گاز بزند، زامبی کشته می‌شود و ۷ سکه برنز می‌برید."
        )
    elif role == ROLE_BAIT:
        text = (
            f"🧑 نقش شما در بازی زامبی: {label}\n"
            "اگر زامبی شما را گاز بزند، شما هم زامبی می‌شوید و ۵ سکه نقره دریافت می‌کنید."
        )
    else:
        text = f"🧟‍♂ نقش شما در بازی زامبی: {label}"
    entities = [MessageEntityBold(offset=0, length=_u16(text))]
    return text, entities


def build_zombie_prompt(zombie_player, targets):
    """ساخت پیام خصوصی انتخاب هدف برای زامبی با کوت واقعی سروش پلاس و Bold."""
    zombie_identity = zombie_player.get("name") or display_name(zombie_player.get("peer"))
    header = f"🧟‍♂ بازیکن زامبی انتخاب شد : {zombie_identity}"

    t1 = targets[0].get("name") or display_name(targets[0].get("peer"))
    t2 = targets[1].get("name") or display_name(targets[1].get("peer"))
    t3 = targets[2].get("name") or display_name(targets[2].get("peer"))

    body = (
        "دقت کن از سه کاربر زیر فقط می‌توانی یک نفر را با گاز زدن تبدیل به زامبی کنی "
        "و برنده شوی. دو نفر دیگر کشنده تو هستند و با گاز اشتباه، بازنده می‌شوی و آن دو نفر برنده می‌شوند.\n\n"
        f"کاربر یک: {t1}\n"
        f"کاربر دو: {t2}\n"
        f"کاربر سه: {t3}\n\n"
        "یک نفر انتخاب کن؛ مهلت ۱۷ ثانیه"
    )
    full_text = f"{header}\n\n{body}"
    entities = [
        MessageEntityBlockquote(offset=0, length=_u16(header)),
        MessageEntityBold(offset=_u16(header + "\n\n"), length=_u16(body)),
    ]
    return full_text, entities


def build_public_loss_message(hunter_player, sniper_player):
    """ساخت پیام عمومی باخت زامبی (فقط در گروه بازی، کل متن Bold)."""
    hunter_name = (hunter_player or {}).get("name") or "—"
    sniper_name = (sniper_player or {}).get("name") or "—"
    text = (
        "اشتباه حدس زدی و کشته شدی 🧌\n"
        "به دو بازیکن شکارچی و تک‌تیرانداز ۷ سکه برنز اضافه شد\n"
        f"🏹 شکارچی: {hunter_name}\n"
        f"🎯 تک‌تیرانداز: {sniper_name}"
    )
    entities = [MessageEntityBold(offset=0, length=_u16(text))]
    return text, entities


def build_public_win_message(bait_player):
    """ساخت پیام عمومی برد زامبی (فقط در گروه بازی؛ دو خط اول Bold، خط سوم کوت واقعی)."""
    bait_name = (bait_player or {}).get("name") or "—"
    bold_part = (
        "🧙‍♂ برنده شدی و درست گاز زدی\n"
        f"طعمه ﴿ {bait_name} ﴾ زامبی شد"
    )
    quote_part = "۵ سکه نقره دریافت کردی ادامه بده و در گروه آخرالزمان به وجود بیار"
    full_text = f"{bold_part}\n{quote_part}"
    entities = [
        MessageEntityBold(offset=0, length=_u16(bold_part)),
        MessageEntityBlockquote(
            offset=_u16(bold_part + "\n"),
            length=_u16(quote_part),
        ),
    ]
    return full_text, entities


def _is_transient_dm_error(error):
    if error is None:
        return False
    name = error.__class__.__name__.lower()
    text = str(error).lower()
    transient_names = (
        "timeout", "timedouterror", "rpc error", "flood", "servererror",
        "network", "connectionerror", "ioerror", "oserror",
    )
    transient_keywords = (
        "timeout", "timed out", "flood", "network", "connection",
        "cannot connect", "connection reset", "timedout",
    )
    if any(k in name for k in transient_names):
        return True
    return any(k in text for k in transient_keywords)


async def send_private_dm(client, player, text, entities=None, logger=None, chat_id=None):
    """ارسال پیام خصوصی به بازیکن با peer ذخیره‌شده یا user_id و پشتیبانی از formatting_entities."""
    user_id = player.get("user_id")
    targets = []
    peer = player.get("peer")
    if peer is not None:
        targets.append(("peer_object", peer))
    if user_id is not None:
        targets.append(("user_id", user_id))

    if not targets:
        log_error(logger, f"FOX ZOMBIE DM FAILED chat_id={chat_id} user_id={user_id} reason=no_target")
        return False, "no_target", False

    last_error = None
    for attempt in range(DM_RETRIES):
        last_error = None
        for label, target in targets:
            try:
                log(logger, f"FOX ZOMBIE DM TRY chat_id={chat_id} user_id={user_id} "
                            f"via={label} attempt={attempt + 1}")
                if entities:
                    try:
                        await asyncio.wait_for(
                            client.send_message(target, text, formatting_entities=entities),
                            timeout=DM_TIMEOUT,
                        )
                    except TypeError:
                        await asyncio.wait_for(
                            client.send_message(target, text),
                            timeout=DM_TIMEOUT,
                        )
                else:
                    await asyncio.wait_for(
                        client.send_message(target, text),
                        timeout=DM_TIMEOUT,
                    )
                log(logger, f"FOX ZOMBIE DM SENT chat_id={chat_id} user_id={user_id} via={label}")
                return True, None, False
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"send_message timed out after {DM_TIMEOUT}s")
                log_error(logger, f"FOX ZOMBIE DM TIMEOUT chat_id={chat_id} "
                                  f"user_id={user_id} via={label} attempt={attempt + 1}")
            except Exception as error:
                last_error = error
                log_error(logger, f"FOX ZOMBIE DM ATTEMPT FAILED chat_id={chat_id} "
                                  f"user_id={user_id} via={label} error={error!r} "
                                  f"attempt={attempt + 1}")

        transient = _is_transient_dm_error(last_error)
        if not transient:
            break
        if attempt < DM_RETRIES - 1:
            await asyncio.sleep(0.5)

    transient = _is_transient_dm_error(last_error)
    log_error(logger, f"FOX ZOMBIE DM FAILED chat_id={chat_id} user_id={user_id} "
                      f"error={last_error!r} transient={transient}")
    return False, last_error, transient


async def deliver_roles_and_prompt(client, chat_id, assignment, logger=None):
    """ارسال نقش هر ۴ بازیکن به پیوی خصوصی و ارسال پیام انتخاب هدف به پیوی زامبی."""
    if not assignment:
        return False
    by_role = assignment["by_role"]
    targets = assignment["targets"]

    # ۱) ارسال نقش سه بازیکن غیر زامبی فقط به پیوی خصوصی خودشان
    for role in (ROLE_HUNTER, ROLE_SNIPER, ROLE_BAIT):
        player = by_role[role]
        role_text, role_entities = build_role_dm(role)
        ok, _err, _transient = await send_private_dm(
            client, player, role_text, entities=role_entities,
            logger=logger, chat_id=chat_id,
        )
        if not ok:
            return False

    # ۲) ارسال نقش و لیست سه هدف به پیوی زامبی
    zombie_player = by_role[ROLE_ZOMBIE]
    prompt_text, prompt_entities = build_zombie_prompt(zombie_player, targets)
    ok, _err, _transient = await send_private_dm(
        client, zombie_player, prompt_text, entities=prompt_entities,
        logger=logger, chat_id=chat_id,
    )
    return bool(ok)


def open_choosing(chat_id, session_id=None, logger=None, bite_seconds=None):
    """باز کردن پنجرهٔ ۱۷ ثانیه‌ای انتخاب هدف پس از ارسال موفق پیام پیوی زامبی."""
    session = _STORE.get(chat_id)
    if not session:
        return False
    if session_id is not None and session["session_id"] != session_id:
        return False
    wait_s = BITE_SECONDS if bite_seconds is None else bite_seconds
    session["phase"] = "choosing"
    session["bite_deadline"] = time.monotonic() + wait_s
    log(logger, f"FOX ZOMBIE CHOOSING OPEN chat_id={chat_id} wait_s={wait_s}")
    return True


def _strip_target_prefix(raw_text):
    text = str(raw_text or "").strip()
    prefixes = (
        "کاربر یک:", "کاربر دو:", "کاربر سه:",
        "کاربر ۱:", "کاربر ۲:", "کاربر ۳:",
        "کاربر 1:", "کاربر 2:", "کاربر 3:",
        "کاربر یک", "کاربر دو", "کاربر سه",
    )
    norm = normalize_text(text)
    for prefix in prefixes:
        norm_prefix = normalize_text(prefix)
        if norm.startswith(norm_prefix):
            # برش پیشوند از متن اصلی در صورت وجود «:»
            if ":" in text:
                text = text.split(":", 1)[1].strip()
            else:
                text = norm[len(norm_prefix):].strip()
            break
    return text.strip()


def match_target_candidate(targets, raw_text):
    """تطبیق متن ارسالی زامبی با یکی از سه گزینهٔ هدف (بر اساس نام نمایشی یا یوزرنیم)."""
    cleaned = _strip_target_prefix(raw_text)
    if not cleaned:
        return None

    cand_username = cleaned.lstrip("@").strip().lower()
    cand_norm = normalize_text(cleaned)
    cand_norm_no_at = normalize_text(cleaned.lstrip("@"))

    # ۱) تطبیق دقیق یوزرنیم یا نام نمایشی کامل
    for target in targets:
        t_uname = str(target.get("username") or "").lstrip("@").strip().lower()
        t_name = str(target.get("name") or "").strip()
        t_raw = str(target.get("raw_name") or "").strip()

        if t_uname and cand_username == t_uname:
            return dict(target)
        if t_name and (cand_norm == normalize_text(t_name)
                       or cand_norm_no_at == normalize_text(t_name.lstrip("@"))):
            return dict(target)
        if t_raw and cand_norm == normalize_text(t_raw):
            return dict(target)

    # ۲) تطبیق با نام کوچک (first_name) در صورتی که فقط به یک نفر از سه هدف بخورد
    first_matches = [
        target for target in targets
        if target.get("first_name")
        and normalize_text(target["first_name"]) == cand_norm
    ]
    if len(first_matches) == 1:
        return dict(first_matches[0])

    return None


def find_active_zombie_chat_for_user(user_id):
    """اگر این کاربر هم‌اکنون زامبیِ در حال انتخاب در یک گروه است، chat_id آن گروه را برمی‌گرداند."""
    if user_id is None:
        return None
    for chat_id, session in list(_STORE._sessions.items()):
        if not session or session.get("phase") != "choosing":
            continue
        by_role = session.get("by_role") or {}
        zombie_player = by_role.get(ROLE_ZOMBIE)
        if zombie_player and zombie_player.get("user_id") == user_id:
            return chat_id
    return None


def select_target(chat_id, user_id, text, logger=None):
    """ثبت انتخاب هدف توسط زامبی در مهلت ۱۷ ثانیه.

    خروجی ``(status, info)``:
      - ``("closed", None)``: بازی فعال نیست یا در مرحلهٔ انتخاب نیست.
      - ``("not_zombie", None)``: فرستنده زامبی نیست.
      - ``("timeout", None)``: مهلت ۱۷ ثانیه تمام شده است.
      - ``("invalid_target", None)``: متن ارسالی یکی از ۳ گزینهٔ معتبر نیست.
      - ``("zombie_win", info)``: زامبی طعمه را گاز زد و برنده شد.
      - ``("zombie_loss", info)``: زامبی شکارچی یا تک‌تیرانداز را گاز زد و کشته شد.
    """
    session = _STORE.get(chat_id)
    if not session or session.get("phase") != "choosing":
        return "closed", None

    by_role = session.get("by_role") or {}
    zombie_player = by_role.get(ROLE_ZOMBIE)
    if not zombie_player or user_id != zombie_player.get("user_id"):
        return "not_zombie", None

    deadline = session.get("bite_deadline")
    if deadline is not None and time.monotonic() > deadline:
        return "timeout", None

    targets = session.get("targets") or []
    chosen = match_target_candidate(targets, text)
    if chosen is None:
        return "invalid_target", None

    # اولین انتخاب معتبر بلافاصله قفل می‌شود و session بسته می‌شود تا امکان تغییر هدف نباشد.
    session["chosen_target"] = dict(chosen)
    closed = _STORE.close(chat_id, session["session_id"])
    _STORE.cancel_task(chat_id)
    if closed is None:
        return "closed", None

    info = {
        "session_id": closed["session_id"],
        "chat_id": chat_id,
        "zombie": dict(by_role[ROLE_ZOMBIE]),
        "hunter": dict(by_role[ROLE_HUNTER]),
        "sniper": dict(by_role[ROLE_SNIPER]),
        "bait": dict(by_role[ROLE_BAIT]),
        "chosen": dict(chosen),
        "group_event": closed.get("group_event"),
    }

    if chosen.get("role") == ROLE_BAIT:
        apocalypse = record_apocalypse_win(
            chat_id,
            closed["session_id"],
            by_role[ROLE_ZOMBIE],
            by_role[ROLE_BAIT],
            logger=logger,
        )
        info["apocalypse"] = apocalypse
        log(
            logger,
            f"FOX ZOMBIE WIN chat_id={chat_id} zombie={user_id} "
            f"bait={chosen.get('user_id')} stage={apocalypse['stage']}",
        )
        return "zombie_win", info

    log(
        logger,
        f"FOX ZOMBIE LOSS chat_id={chat_id} zombie={user_id} "
        f"chosen={chosen.get('user_id')} chosen_role={chosen.get('role')}",
    )
    return "zombie_loss", info


def timeout_close(chat_id, session_id=None, logger=None):
    """پایان بازی بدون جایزه در صورت انتخاب نکردن زامبی ظرف ۱۷ ثانیه."""
    session = _STORE.get(chat_id)
    if not session:
        return None
    if session_id is not None and session["session_id"] != session_id:
        return None
    closed = _STORE.close(chat_id, session["session_id"])
    _STORE.cancel_task(chat_id)
    if closed is None:
        return None
    log(logger, f"FOX ZOMBIE BITE TIMEOUT chat_id={chat_id} session_id={closed['session_id']}")
    return {
        "session_id": closed["session_id"],
        "chat_id": chat_id,
        "by_role": dict(closed.get("by_role") or {}),
        "group_event": closed.get("group_event"),
    }


def abandon(chat_id, session_id=None, logger=None):
    """لغو و پاک‌سازی کامل وضعیت موقت و تایمرهای بازی زامبی در گروه."""
    closed = _STORE.close(chat_id, session_id)
    _STORE.cancel_task(chat_id)
    if closed is not None:
        log(logger, f"FOX ZOMBIE ABANDON chat_id={chat_id} session_id={closed['session_id']}")
    return bool(closed)


# ---------------------------------------------------------------------------
# ادامهٔ مرحلهٔ «گروه آخرالزمان»
# ---------------------------------------------------------------------------
def record_apocalypse_win(chat_id, session_id, zombie_player, bait_player, logger=None):
    """ثبت و حفظ وضعیت زامبی برنده و طعمهٔ تبدیل‌شده برای مرحلهٔ «گروه آخرالزمان»."""
    prev = _APOCALYPSE_BY_CHAT.get(chat_id)
    stage = (prev["stage"] + 1) if prev else 1

    z_summary = {
        "user_id": zombie_player.get("user_id"),
        "name": zombie_player.get("name"),
        "username": zombie_player.get("username", ""),
        "role": ROLE_ZOMBIE,
    }
    b_summary = {
        "user_id": bait_player.get("user_id"),
        "name": bait_player.get("name"),
        "username": bait_player.get("username", ""),
        "role": "infected_zombie",
    }

    team_by_id = {}
    if prev:
        for member in prev.get("zombie_team", ()):
            uid = member.get("user_id")
            if uid is not None:
                team_by_id[uid] = dict(member)
    if z_summary["user_id"] is not None:
        team_by_id[z_summary["user_id"]] = z_summary
    if b_summary["user_id"] is not None:
        team_by_id[b_summary["user_id"]] = b_summary

    history = list(prev.get("history", ())) if prev else []
    history.append({
        "session_id": session_id,
        "stage": stage,
        "zombie": dict(z_summary),
        "bitten_bait": dict(b_summary),
        "timestamp": time.time(),
    })

    state = {
        "chat_id": chat_id,
        "active": True,
        "stage": stage,
        "last_session_id": session_id,
        "alpha_zombie": dict(z_summary),
        "bitten_bait": dict(b_summary),
        "zombie_team": list(team_by_id.values()),
        "history": history,
        "updated_at": time.time(),
    }
    _APOCALYPSE_BY_CHAT[chat_id] = state
    log(
        logger,
        f"FOX ZOMBIE APOCALYPSE UPDATED chat_id={chat_id} stage={stage} "
        f"team_size={len(state['zombie_team'])}",
    )
    return dict(state)


def get_apocalypse_state(chat_id):
    """دریافت وضعیت فعلی مرحلهٔ «گروه آخرالزمان» برای گروه."""
    state = _APOCALYPSE_BY_CHAT.get(chat_id)
    if not state:
        return None
    return {
        "chat_id": state["chat_id"],
        "active": state["active"],
        "stage": state["stage"],
        "last_session_id": state["last_session_id"],
        "alpha_zombie": dict(state["alpha_zombie"]),
        "bitten_bait": dict(state["bitten_bait"]),
        "zombie_team": [dict(m) for m in state["zombie_team"]],
        "history": [dict(h) for h in state["history"]],
        "updated_at": state["updated_at"],
    }


def format_apocalypse_status(chat_id):
    """گزارش متنی وضعیت مرحلهٔ «گروه آخرالزمان» در همان گروه."""
    state = get_apocalypse_state(chat_id)
    if not state:
        return (
            "🧟‍♂ هنوز مرحلهٔ «گروه آخرالزمان» در این گروه شکل نگرفته است.\n"
            "برای شروع، بازی «زامبی» را اجرا کنید و طعمه را گاز بگیرید!"
        )
    members = "، ".join(m.get("name", "—") for m in state["zombie_team"])
    return (
        f"🧟‍♂ وضعیت گروه آخرالزمان (مرحله {to_persian_digits(state['stage'])})\n"
        f"🦠 زامبی اصلی: {state['alpha_zombie'].get('name', '—')}\n"
        f"🧟 طعمه تبدیل‌شده: {state['bitten_bait'].get('name', '—')}\n"
        f"👥 لشکر زامبی‌ها: {members}"
    )


async def run_game(chat_id, session_id, callbacks, logger=None,
                   join_seconds=None, bite_seconds=None):
    """چرخهٔ غیرمسدودکنندهٔ بازی زامبی: ۵۰ ثانیه ثبت‌نام و ۱۷ ثانیه انتخاب هدف."""
    join_wait = JOIN_SECONDS if join_seconds is None else join_seconds
    bite_wait = BITE_SECONDS if bite_seconds is None else bite_seconds
    finished = False
    try:
        session = _STORE.get(chat_id)
        if not session or session["session_id"] != session_id:
            return

        full_ev = session.get("full_event")
        if full_ev is not None and not is_full(chat_id):
            try:
                await asyncio.wait_for(full_ev.wait(), timeout=join_wait)
            except asyncio.TimeoutError:
                pass
        else:
            waited = 0.0
            step = 0.05 if join_wait <= 2 else 0.25
            while waited < join_wait and not is_full(chat_id):
                await asyncio.sleep(step)
                waited += step

        session = _STORE.get(chat_id)
        if not session or session["session_id"] != session_id:
            return
        if len(session["players"]) < REQUIRED_PLAYERS:
            abandon(chat_id, session_id, logger)
            on_abort = callbacks.get("on_abort")
            if on_abort is not None:
                await on_abort()
            finished = True
            return

        assignment = assign_roles(chat_id, logger=logger)
        if assignment is None:
            abandon(chat_id, session_id, logger)
            on_abort = callbacks.get("on_abort")
            if on_abort is not None:
                await on_abort()
            finished = True
            return

        delivered = await callbacks["on_roles"](assignment)
        if not delivered:
            log_error(logger, f"FOX ZOMBIE ABORT chat_id={chat_id} reason=dm_failed")
            abandon(chat_id, session_id, logger)
            on_dm_failed = callbacks.get("on_dm_failed")
            if on_dm_failed is not None:
                await on_dm_failed()
            else:
                on_abort = callbacks.get("on_abort")
                if on_abort is not None:
                    await on_abort()
            finished = True
            return

        if not open_choosing(chat_id, session_id, logger, bite_seconds=bite_wait):
            return

        on_started = callbacks.get("on_started")
        if on_started is not None:
            await on_started(assignment)

        # مهلت ۱۷ ثانیه از لحظهٔ تحویل موفق پیام پیوی زامبی آغاز می‌شود.
        await asyncio.sleep(bite_wait)
        session = _STORE.get(chat_id)
        if not session or session["session_id"] != session_id:
            return
        if session.get("phase") == "choosing":
            timed_out = timeout_close(chat_id, session_id, logger)
            if timed_out is not None:
                on_timeout = callbacks.get("on_timeout")
                if on_timeout is not None:
                    await on_timeout(timed_out)
        finished = True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log_error(logger, f"FOX ZOMBIE LOOP FAILED chat_id={chat_id} error={error!r}")
        if not finished:
            abandon(chat_id, session_id, logger)


def schedule(chat_id, session_id, callbacks, logger=None,
             join_seconds=None, bite_seconds=None):
    return _STORE.schedule(chat_id, lambda: run_game(
        chat_id, session_id, callbacks, logger, join_seconds, bite_seconds))


def reset_all(chat_id=None):
    _STORE.reset(chat_id)
    if chat_id is None:
        _APOCALYPSE_BY_CHAT.clear()
    else:
        _APOCALYPSE_BY_CHAT.pop(chat_id, None)
