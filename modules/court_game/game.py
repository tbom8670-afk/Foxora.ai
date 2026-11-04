# -*- coding: utf-8 -*-
"""⚖️ هستهٔ بازی «دادگاه روباه» — state، نقش‌ها، مراحل و پرونده.

جریان رسمی دادگاه::

    ثبت‌نام (۶۰ ثانیه، دقیقاً ۵ نفر)
        ↓
    تخصیص تصادفی نقش‌ها (بدون تکرار): شاکی، متهم، شاهد شاکی، شاهد متهم، وکیل
        ↓
    انتخاب موضوع جرم (شاکی) → شرح شکایت (شاکی)
        ↓
    دفاع متهم → شهادت شاهد شاکی → شهادت شاهد متهم → دفاعیه نهایی وکیل
        ↓
    داوری داخلی بر پایهٔ شواهد → رأی → جایزه (+ مجازات ۶۰ ثانیه‌ای متهم در صورت محکومیت)

قواعد سخت این ماژول:

* در هر مرحله **فقط** بازیکن همان نقش اجازهٔ پیام دارد؛ پیام دیگران نادیده
  گرفته می‌شود و مرحله را جلو نمی‌برد.
* هر مرحله فقط **یک بار** ثبت می‌شود؛ پیام دوم همان نقش نادیده گرفته می‌شود.
* هر پیام با ``order``، ``message_id``، نقش، شناسه و یوزرنیم واقعی ثبت
  می‌شود؛ قاضی داخلی فقط از همین رکورد تغذیه می‌شود.
* همهٔ state هر گروه مستقل است (``SessionStore`` پروژه) و روی دیسک در
  ``data/court_game/sessions`` آینه می‌شود تا بعد از ری‌استارت قابل
  بازیابی/بستن باشد.
"""
from __future__ import annotations

import asyncio
import random
import time

from modules.fox_games.session_core import (
    SessionStore, display_name, log, log_error, normalize_text,
)
from modules.court_game import storage, texts

GAME_NAME = "court"
COMMANDS = ("دادگاه", "دادگاه روباه")

REQUIRED_PLAYERS = 5
JOIN_SECONDS = 60
# اگر مهلت ثبت‌نام یک پروندهٔ ذخیره‌شده بیش از این مقدار گذشته باشد،
# پرونده «کهنه» است و بازیابی نمی‌شود (فقط پاک می‌شود) تا بعد از چند روز
# پیام «پروندهٔ این گروه باز است» به کاربر نشان داده نشود.
JOIN_EXPIRED_GRACE = 300
JOIN_WORD = texts.JOIN_WORD

ROLE_ORDER = ("plaintiff", "defendant", "witness_p", "witness_d", "lawyer")

PHASE_JOINING = "joining"
PHASE_CRIME = "crime"
PHASE_COMPLAINT = "complaint"
PHASE_DEFENSE = "defense"
PHASE_WITNESS_P = "witness_p"
PHASE_WITNESS_D = "witness_d"
PHASE_LAWYER = "lawyer"
PHASE_JUDGING = "judging"
PHASE_CLOSED = "closed"

# نقشِ نوبت‌دار هر مرحله (به‌ترتیب رسمی دادگاه).
PHASE_ROLE = {
    PHASE_CRIME: "plaintiff",
    PHASE_COMPLAINT: "plaintiff",
    PHASE_DEFENSE: "defendant",
    PHASE_WITNESS_P: "witness_p",
    PHASE_WITNESS_D: "witness_d",
    PHASE_LAWYER: "lawyer",
}

NEXT_PHASE = {
    PHASE_CRIME: PHASE_COMPLAINT,
    PHASE_COMPLAINT: PHASE_DEFENSE,
    PHASE_DEFENSE: PHASE_WITNESS_P,
    PHASE_WITNESS_P: PHASE_WITNESS_D,
    PHASE_WITNESS_D: PHASE_LAWYER,
    PHASE_LAWYER: PHASE_JUDGING,
}

# کلید بخش‌های پرونده (متن‌های ثبت‌شده).
SECTION_BY_PHASE = {
    PHASE_COMPLAINT: "complaint",
    PHASE_DEFENSE: "defense",
    PHASE_WITNESS_P: "witness_p",
    PHASE_WITNESS_D: "witness_d",
    PHASE_LAWYER: "lawyer",
}

MIN_TEXT_LENGTH = 3
MAX_TEXT_LENGTH = 1500
MAX_INVALID_CHOICES = 3

_STORE = SessionStore(GAME_NAME)
_RANDOM = random.SystemRandom()

# دستورهایی که هرگز نباید به‌عنوان «متن پرونده» بلعیده شوند.
_COMMAND_PREFIXES = ("!", "/", ".", "#")


# ---------------------------------------------------------------------------
# کمکی‌ها
# ---------------------------------------------------------------------------
def is_active(chat_id) -> bool:
    return _STORE.is_active(chat_id)


def phase(chat_id):
    session = _STORE.get(chat_id)
    return session.get("phase") if session else None


def session_of(chat_id):
    session = _STORE.get(chat_id)
    return dict(session) if session else None


def case_of(chat_id):
    session = _STORE.get(chat_id)
    if not session:
        return None
    return session.get("case")


def player_display(session, role) -> str:
    """نام نمایشی بازیکنِ یک نقش (یوزرنیم، وگرنه نام نمایشی)."""
    players = (session or {}).get("role_players") or {}
    player = players.get(role) or {}
    if isinstance(player, dict):
        return player.get("display") or "—"
    return display_name(player) or "—"


def assign_roles(players, rng=None):
    """تخصیص تصادفی پنج نقش بدون تکرار روی پنج بازیکن."""
    rng = rng or _RANDOM
    pool = list(players)
    if len(pool) != len(ROLE_ORDER):
        raise ValueError("exactly %d players required" % len(ROLE_ORDER))
    rng.shuffle(pool)
    return {role: player for role, player in zip(ROLE_ORDER, pool)}


def _player_record(user, user_id) -> dict:
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    return {
        "user_id": int(user_id) if user_id is not None else None,
        "username": username or None,
        "display": display_name(user),
        "peer": user,
    }


def _stop_task(chat_id) -> None:
    """توقف تایمر/حلقهٔ این گروه — بدون کشتن تسکی که همین حالا در حال اجراست.

    ``cancel_task`` اگر روی تسک جاری صدا زده شود، CancelledError را در
    نخستین ``await`` بعدی تزریق می‌کند و پیام پایانی بازی ارسال نمی‌شود.
    پس تسک جاری فقط از انبار حذف می‌شود و خودش تمیز برمی‌گردد.
    """
    task = _STORE.task_for(chat_id)
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    if task is not None and task is current:
        _STORE._tasks.pop(chat_id, None)
        return
    _STORE.cancel_task(chat_id)


def _persist(chat_id, session) -> None:
    """آینه‌سازی session روی دیسک (برای بازیابی بعد از ری‌استارت).

    فهرست کامل ثبت‌نام‌شده‌ها (شناسه، یوزرنیم، نام نمایشی) و مهلت
    ثبت‌نام ذخیره می‌شود تا اگر ربات وسط ثبت‌نام خاموش/روشن شود،
    هیچ شرکت‌کننده‌ای گم نشود.
    """
    try:
        storage.write_session(storage.chat_key_for(chat_id), {
            "kind": "case",
            "chat_key": storage.chat_key_for(chat_id),
            "chat_id": chat_id,
            "case_id": (session.get("case") or {}).get("case_id"),
            "phase": session.get("phase"),
            "players": [
                {"user_id": p.get("user_id"),
                 "username": p.get("username"),
                 "display": p.get("display")}
                for p in session.get("players", [])
            ],
            "join_expires_at": session.get("join_expires_at"),
            "saved_at": storage.now_iso(),
        })
    except Exception as error:  # pragma: no cover - خطای دیسک نباید بازی را بکشد
        log_error(session.get("logger"), f"COURT SESSION PERSIST FAILED {error!r}")


def _persist_case(session) -> None:
    case = session.get("case")
    if not case:
        return
    try:
        storage.write_case(case)
    except Exception as error:
        log_error(session.get("logger"), f"COURT CASE PERSIST FAILED {error!r}")


# ---------------------------------------------------------------------------
# ثبت‌نام
# ---------------------------------------------------------------------------
def start(chat_id, logger=None, group_event=None):
    session = _STORE.create(chat_id, {
        "phase": PHASE_JOINING,
        "players": [],
        "ids": set(),
        "role_players": {},
        "case": None,
        "logger": logger,
        "group_event": group_event,
        "invalid_choices": 0,
        "phase_deadline": None,
        "pending_prompt": None,
        "judged": False,
        "join_expires_at": time.time() + storage.settings()["join_seconds"],
        "restored": False,
    })
    if session is None:
        log(logger, f"COURT START BLOCKED chat_id={chat_id} reason=already_active")
        return None
    log(logger, f"COURT START chat_id={chat_id} session_id={session['session_id']}")
    _persist(chat_id, session)
    return dict(session)


def join_deadline(chat_id):
    """زمان پایان ثبت‌نام (epoch) برای session جاری."""
    session = _STORE.get(chat_id)
    return (session or {}).get("join_expires_at")


def remaining_join_seconds(chat_id):
    deadline = join_deadline(chat_id)
    if deadline is None:
        return float(JOIN_SECONDS)
    return max(0.0, float(deadline) - time.time())


def restore(chat_id, logger=None):
    """بازیابی پروندهٔ در حال ثبت‌نام از روی دیسک.

    اگر ربات وسط ۶۰ ثانیهٔ ثبت‌نام ری‌استارت شود (یا session از حافظه
    برود)، این تابع فهرست ثبت‌نام‌شده‌ها را از فایل session برمی‌گرداند
    و بازی را در همان نقطه ادامه می‌دهد. بدون این کار، هر «شرکت» یک
    پروندهٔ تازه می‌ساخت و شمارش از «۱ از ۵» شروع می‌شد.

    فقط مرحلهٔ «ثبت‌نام» بازیابی می‌شود؛ پروندهٔ نیمه‌کارهٔ وسط دادگاه
    طبق سیاست بازی بسته می‌شود (بدون جایزه).
    """
    if _STORE.is_active(chat_id):
        return dict(_STORE.get(chat_id))
    try:
        data = storage.read_session(storage.chat_key_for(chat_id))
    except Exception:
        data = None
    if not data or data.get("kind") != "case":
        return None
    if data.get("phase") != PHASE_JOINING:
        return None
    try:
        deadline = data.get("join_expires_at")
        stale = (deadline is not None
                 and float(deadline) + JOIN_EXPIRED_GRACE < time.time())
    except (TypeError, ValueError):
        stale = False
    if stale:
        try:
            storage.delete_session(storage.chat_key_for(chat_id))
        except Exception:
            pass
        log(logger, f"COURT STALE JOIN DROPPED chat_id={chat_id}")
        return None
    players = []
    ids = set()
    for row in data.get("players") or []:
        try:
            user_id = int(row.get("user_id"))
        except (TypeError, ValueError):
            continue
        if user_id in ids:
            continue
        ids.add(user_id)
        players.append({
            "user_id": user_id,
            "username": row.get("username"),
            "display": row.get("display") or "@%s" % user_id,
            "peer": None,
        })
    if not players:
        return None
    session = _STORE.create(chat_id, {
        "phase": PHASE_JOINING,
        "players": players,
        "ids": ids,
        "role_players": {},
        "case": None,
        "logger": logger,
        "invalid_choices": 0,
        "phase_deadline": None,
        "pending_prompt": None,
        "judged": False,
        "join_expires_at": data.get("join_expires_at"),
        "restored": True,
    })
    if session is None:
        return dict(_STORE.get(chat_id))
    log(logger, f"COURT SESSION RESTORED chat_id={chat_id} "
                f"players={len(players)}")
    return dict(session)


def forget(chat_id):
    """فقط حافظهٔ همین گروه را پاک می‌کند (شبیه‌سازی ری‌استارت ربات).

    فایل session روی دیسک دست‌نخورده می‌ماند؛ برای تست بازیابی و برای
    ری‌استارت واقعی که state در حافظه از دست می‌رود.
    """
    _STORE.reset(chat_id)


def join(chat_id, user_id, user, logger=None):
    session = _STORE.get(chat_id)
    if not session or session.get("phase") != PHASE_JOINING:
        return "closed", []
    if user_id in session["ids"]:
        return "duplicate", list(session["players"])
    if len(session["players"]) >= REQUIRED_PLAYERS:
        return "full", list(session["players"])
    player = _player_record(user, user_id)
    session["ids"].add(user_id)
    session["players"].append(player)
    log(logger, f"COURT JOIN chat_id={chat_id} user_id={user_id} "
                f"count={len(session['players'])}")
    _persist(chat_id, session)
    return "joined", list(session["players"])


def player_count(chat_id) -> int:
    session = _STORE.get(chat_id)
    return len(session["players"]) if session else 0


def is_full(chat_id) -> bool:
    return player_count(chat_id) >= REQUIRED_PLAYERS


def roster_map(session) -> dict:
    return {role: player.get("display", "—")
            for role, player in (session.get("role_players") or {}).items()}


def open_trial(chat_id, session_id, bot_username=None, logger=None):
    """ساخت پروندهٔ رسمی + تخصیص نقش‌ها؛ خروجی: dict پرونده یا None."""
    session = _STORE.get(chat_id)
    if not session or session.get("session_id") != session_id:
        return None
    if len(session["players"]) != REQUIRED_PLAYERS:
        return None
    try:
        roles = assign_roles(session["players"])
    except ValueError:
        return None

    case_id = storage.new_case_id()
    case = {
        "case_id": case_id,
        "chat_id": chat_id,
        "status": storage.CASE_STATUS_OPEN,
        "created_at": storage.now_iso(),
        "game": "دادگاه روباه",
        "court": bot_username,
        "crime": "",
        "roles": list(ROLE_ORDER),
        "players": {
            role: {
                "user_id": player.get("user_id"),
                "username": player.get("username"),
                "display": player.get("display"),
            }
            for role, player in roles.items()
        },
        "entries": [],
        "sections": {
            "complaint": "",
            "defense": "",
            "witness_p": "",
            "witness_d": "",
            "lawyer": "",
        },
        "verdict": None,
        "payment": {"status": "pending"},
        "punishment": {"status": "none"},
    }

    session["role_players"] = roles
    session["case"] = case
    session["phase"] = PHASE_CRIME
    session["phase_deadline"] = time.monotonic() + storage.settings()["step_seconds"]
    session["pending_prompt"] = PHASE_CRIME
    _persist_case(session)
    _persist(chat_id, session)
    log(logger, f"COURT CASE OPEN chat_id={chat_id} case_id={case_id} "
                f"roles={ {r: p['user_id'] for r, p in roles.items()} }")
    return case


# ---------------------------------------------------------------------------
# ثبت پیام مراحل
# ---------------------------------------------------------------------------
def _add_entry(session, role, kind, text, message_id, user_id):
    case = session["case"]
    order = len(case["entries"]) + 1
    player = (case.get("players") or {}).get(role) or {}
    entry = {
        "order": order,
        "role": role,
        "role_label": texts.ROLE_LABELS.get(role, role),
        "kind": kind,
        "user_id": player.get("user_id"),
        "username": player.get("username"),
        "display": player.get("display"),
        "text": text,
        "message_id": message_id,
        "at": storage.now_iso(),
        "sent_by": user_id,
    }
    case["entries"].append(entry)
    return entry


def _clean_text(text) -> str:
    value = str(text or "").replace("\x00", " ")
    value = " ".join(value.split())
    return value[:MAX_TEXT_LENGTH]


def match_crime(text):
    """تشخیص جرم از متن شاکی؛ None اگر گزینه معتبر نباشد."""
    cleaned = normalize_text(text)
    if not cleaned:
        return None
    for prefix in ("جرم", "شکایت", "موضوع", "اتهام"):
        if cleaned.startswith(normalize_text(prefix)):
            cleaned = cleaned[len(normalize_text(prefix)):].strip(" :،-")
    compact = cleaned.replace("‌", " ")
    compact = "".join(
        ch for ch in compact
        if not ch.isdigit() and ch not in "٠١٢٣٤٥٦٧٨٩.-)(_•*"
    ).strip()
    compact = " ".join(compact.split())
    for crime in texts.CRIMES:
        if compact == normalize_text(crime):
            return crime
    matched = [crime for crime in texts.CRIMES
               if normalize_text(crime) in compact]
    if len(matched) == 1 and len(compact) <= len(normalize_text(matched[0])) + 8:
        return matched[0]
    return None


def current_role(chat_id):
    session = _STORE.get(chat_id)
    if not session:
        return None
    return PHASE_ROLE.get(session.get("phase"))


def submit(chat_id, user_id, text, message_id=None, logger=None):
    """ثبت پیام بازیکنِ نوبت‌دار. خروجی ``(status, info)``.

    statusها:
        ``not_active``   بازی در مرحلهٔ ورودی نیست
        ``wrong_user``   فرستنده نقشِ نوبت نیست (مرحله دست‌نخورده می‌ماند)
        ``empty``        متن خالی/خیلی کوتاه (مرحله دست‌نخورده می‌ماند)
        ``invalid``      گزینهٔ جرم نامعتبر (مرحله دست‌نخورده می‌ماند)
        ``recorded``     ثبت شد و مرحله جلو رفت
        ``limit``        تعداد پاسخ نامعتبر بیش از حد
    """
    session = _STORE.get(chat_id)
    if not session:
        return "not_active", None
    phase = session.get("phase")
    role = PHASE_ROLE.get(phase)
    if role is None:
        return "not_active", None

    role_players = session.get("role_players") or {}
    owner = role_players.get(role) or {}
    if int(owner.get("user_id") or 0) != int(user_id or 0):
        return "wrong_user", {"role": role,
                              "role_label": texts.ROLE_LABELS.get(role, role)}

    cleaned = _clean_text(text)
    if len(cleaned) < MIN_TEXT_LENGTH:
        return "empty", {"role": role}

    if phase == PHASE_CRIME:
        crime = match_crime(cleaned)
        if crime is None:
            session["invalid_choices"] = int(session.get("invalid_choices", 0)) + 1
            if session["invalid_choices"] >= MAX_INVALID_CHOICES:
                return "limit", {"role": role}
            return "invalid", {"role": role,
                               "remaining": MAX_INVALID_CHOICES
                               - session["invalid_choices"]}
        session["case"]["crime"] = crime
        _add_entry(session, role, "crime_choice", crime, message_id, user_id)
    else:
        section = SECTION_BY_PHASE.get(phase)
        if section:
            session["case"]["sections"][section] = cleaned
            _add_entry(session, role, section, cleaned, message_id, user_id)

    next_phase = NEXT_PHASE.get(phase, PHASE_CLOSED)
    session["phase"] = next_phase
    if next_phase == PHASE_JUDGING:
        session["phase_deadline"] = None
        session["case"]["status"] = storage.CASE_STATUS_JUDGING
    else:
        session["phase_deadline"] = (
            time.monotonic() + storage.settings()["step_seconds"]
        )
        session["pending_prompt"] = next_phase
    _persist_case(session)
    _persist(chat_id, session)
    log(logger, f"COURT ENTRY chat_id={chat_id} case_id="
                f"{session['case']['case_id']} phase={phase} role={role} "
                f"next={next_phase}")
    return "recorded", {
        "role": role,
        "next_phase": next_phase,
        "entry_order": len(session["case"]["entries"]),
        "case_id": session["case"]["case_id"],
        "case": session["case"],
    }


def prompt_text_for(session, phase, seconds):
    """متن پیامِ درخواست هر مرحله."""
    role = PHASE_ROLE.get(phase)
    name = player_display(session, role) if role else "—"
    if phase == PHASE_CRIME:
        return texts.crime_prompt(name, seconds)
    if phase == PHASE_COMPLAINT:
        return texts.complaint_prompt(name, seconds)
    if phase == PHASE_DEFENSE:
        return texts.defense_prompt(name, seconds)
    if phase == PHASE_WITNESS_P:
        return texts.witness_p_prompt(name, seconds)
    if phase == PHASE_WITNESS_D:
        return texts.witness_d_prompt(name, seconds)
    if phase == PHASE_LAWYER:
        return texts.lawyer_prompt(name, seconds)
    return ""


def cancel(chat_id, session_id=None, reason="timeout", logger=None):
    """بستن پرونده بدون جایزه (کمبود بازیکن، پایان مهلت، خطا)."""
    session = _STORE.get(chat_id)
    if not session:
        return None
    if session_id is not None and session["session_id"] != session_id:
        return None
    case = session.get("case") or {}
    info = {
        "case_id": case.get("case_id"),
        "reason": reason,
        "phase": session.get("phase"),
        "role": PHASE_ROLE.get(session.get("phase")),
    }
    if case.get("case_id"):
        case["status"] = storage.CASE_STATUS_CANCELLED
        case["cancel_reason"] = reason
        case["payment"] = {"status": "skipped", "reason": reason}
        session["case"] = case
        _persist_case(session)
        try:
            storage.write_result(case["case_id"], {
                "case_id": case["case_id"],
                "status": storage.CASE_STATUS_CANCELLED,
                "reason": reason,
                "verdict": None,
                "payment": {"status": "skipped", "reason": reason},
                "closed_at": storage.now_iso(),
            })
        except Exception:
            pass
    closed = _STORE.close(chat_id, session["session_id"])
    _stop_task(chat_id)
    try:
        storage.delete_session(storage.chat_key_for(chat_id))
    except Exception:
        pass
    log(logger, f"COURT CANCEL chat_id={chat_id} case_id={info['case_id']} "
                f"reason={reason}")
    return info


def finish(chat_id, session_id, result, logger=None, status=None):
    """بستن قطعی پرونده پس از رأی (فقط یک بار).

    ``status`` اجازه می‌دهد پرونده‌ای که داوری‌اش نامعتبر بود با وضعیت
    ``judge_error`` آرشیو شود؛ پیش‌فرض همان «closed» است.
    """
    session = _STORE.get(chat_id)
    if not session or session["session_id"] != session_id:
        return None
    case = session.get("case") or {}
    case["status"] = status or storage.CASE_STATUS_CLOSED
    case["closed_at"] = storage.now_iso()
    case["verdict"] = result
    session["case"] = case
    session["phase"] = PHASE_CLOSED
    _persist_case(session)
    try:
        storage.write_result(case.get("case_id"), {
            "case_id": case.get("case_id"),
            "status": case.get("status"),
            "crime": case.get("crime"),
            "verdict": result,
            "payment": case.get("payment"),
            "punishment": case.get("punishment"),
            "players": case.get("players"),
            "closed_at": case.get("closed_at"),
        })
    except Exception as error:
        log_error(logger, f"COURT RESULT WRITE FAILED {error!r}")
    closed = _STORE.close(chat_id, session["session_id"])
    _stop_task(chat_id)
    try:
        storage.delete_session(storage.chat_key_for(chat_id))
    except Exception:
        pass
    log(logger, f"COURT FINISH chat_id={chat_id} case_id={case.get('case_id')} "
                f"verdict={(result or {}).get('verdict')}")
    return closed


def set_payment(chat_id, payment, logger=None):
    session = _STORE.get(chat_id)
    if not session or not session.get("case"):
        return
    session["case"]["payment"] = payment
    _persist_case(session)


def set_punishment(chat_id, punishment, logger=None):
    session = _STORE.get(chat_id)
    if not session or not session.get("case"):
        return
    session["case"]["punishment"] = punishment
    _persist_case(session)


def mark_judge_error(chat_id, message="", logger=None):
    """ثبت خطای داوری روی پروندهٔ جاری (بدون جایزه و بدون مجازات)."""
    session = _STORE.get(chat_id)
    if not session or not session.get("case"):
        return
    session["case"]["judge_error"] = str(message)[:500]
    _persist_case(session)


def mark_judged(chat_id) -> bool:
    """قفل یک‌بارهٔ داوری: True اگر همین حالا مالک داوری شدیم."""
    session = _STORE.get(chat_id)
    if not session:
        return False
    if session.get("judged"):
        return False
    session["judged"] = True
    return True


# ---------------------------------------------------------------------------
# حلقهٔ بازی
# ---------------------------------------------------------------------------
async def run(chat_id, session_id, callbacks, logger=None, join_seconds=None,
              step_seconds=None):
    """چرخهٔ کامل دادگاه؛ همهٔ ارسال‌ها از همین task انجام می‌شود."""
    join_wait = JOIN_SECONDS if join_seconds is None else join_seconds
    session = _STORE.get(chat_id)
    if join_seconds is None and session and session.get("join_expires_at"):
        join_wait = max(0.0, float(session["join_expires_at"]) - time.time())
        join_wait = min(join_wait, float(JOIN_SECONDS))
    step_wait = (storage.settings()["step_seconds"]
                 if step_seconds is None else step_seconds)
    finished = False
    try:
        waited = 0.0
        poll = min(0.25, max(0.02, join_wait / 20.0)) if join_wait <= 5 else 0.4
        while waited < join_wait and not is_full(chat_id):
            await asyncio.sleep(poll)
            waited += poll

        session = _STORE.get(chat_id)
        if not session or session["session_id"] != session_id:
            return
        if not session.get("join_expires_at"):
            session["join_expires_at"] = time.time() + join_wait
            _persist(chat_id, session)
        if len(session["players"]) < REQUIRED_PLAYERS:
            count = len(session["players"])
            # پیام پایانی پیش از بستن session فرستاده می‌شود تا حتی اگر
            # ارسال کمی طول بکشد، وضعیت بازی نیمه‌کاره نماند و پیام گم نشود.
            await callbacks["on_cancel"]("not_enough_players", count)
            cancel(chat_id, session_id, "not_enough_players", logger)
            finished = True
            return

        bot_username = None
        if callbacks.get("bot_username") is not None:
            try:
                bot_username = await callbacks["bot_username"]()
            except Exception:
                bot_username = None

        session = _STORE.get(chat_id)
        if not session or session["session_id"] != session_id:
            return
        case = open_trial(chat_id, session_id, bot_username, logger)
        if case is None:
            await callbacks["on_cancel"]("roles_failed", 0)
            cancel(chat_id, session_id, "roles_failed", logger)
            finished = True
            return

        session = _STORE.get(chat_id)
        if session.get("phase_deadline"):
            session["phase_deadline"] = time.monotonic() + step_wait
        await callbacks["on_start"](case, roster_map(session))

        while True:
            session = _STORE.get(chat_id)
            if not session or session["session_id"] != session_id:
                return
            phase = session.get("phase")

            if phase == PHASE_JUDGING:
                if not mark_judged(chat_id):
                    return
                await callbacks["on_judging"](case_of(chat_id))
                finished = True
                return

            pending = session.get("pending_prompt")
            if pending:
                session["pending_prompt"] = None
                session["phase_deadline"] = time.monotonic() + step_wait
                text = prompt_text_for(session, pending, step_wait)
                if text:
                    await callbacks["on_prompt"](pending, text)

            deadline = session.get("phase_deadline")
            if deadline is not None and time.monotonic() >= deadline:
                role = PHASE_ROLE.get(phase)
                await callbacks["on_cancel"](
                    "step_timeout", 0,
                    texts.ROLE_LABELS.get(role, "بازیکن") if role else None,
                )
                cancel(chat_id, session_id, "step_timeout", logger)
                finished = True
                return

            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log_error(logger, f"COURT LOOP FAILED chat_id={chat_id} error={error!r}")
        if not finished:
            cancel(chat_id, session_id, "loop_error", logger)


def schedule(chat_id, session_id, callbacks, logger=None, join_seconds=None,
             step_seconds=None):
    return _STORE.schedule(chat_id, lambda: run(
        chat_id, session_id, callbacks, logger, join_seconds, step_seconds))


def reset_all(chat_id=None):
    if chat_id is None:
        _STORE.reset()
        return
    # فایل session همیشه پاک می‌شود، حتی اگر state در حافظه نباشد
    # (مثلاً بعد از ری‌استارت)، وگرنه پروندهٔ بسته‌شده دوباره بازیابی می‌شد.
    try:
        storage.delete_session(storage.chat_key_for(chat_id))
    except Exception:
        pass
    _STORE.reset(chat_id)
