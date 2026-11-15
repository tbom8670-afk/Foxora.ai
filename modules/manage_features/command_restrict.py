# -*- coding: utf-8 -*-
"""⚙️ «محدودیت دستورات» — محدودکردنِ یک دستورِ مدیریتی برای یک ادمینِ مشخص.

* ``محدودیت دستورات`` → لیستِ ادمین‌های ثبت‌شده → عدد → نامِ دستور
* ``لغو دستور``        → ادمین‌های محدودشده → عدد → دستورهای او → عدد

فقط مالکِ ثبت‌شدهٔ گروه و مالکِ اصلیِ ربات. همه‌چیز با **شناسهٔ عددی** و
**شناسهٔ دستور** (نه متنِ نمایشی) ذخیره می‌شود.

اجرای واقعی: ``intercept`` در ابتدای مسیرِ پیام (پیش از همهٔ هندلرها،
مسیرهای سریع و «دو مرحله ای») صدا زده می‌شود؛ اگر فرستنده برای آن دستور
محدود باشد، پیام همان‌جا تمام می‌شود و هیچ هندلری اجرا نمی‌شود.
"""
from __future__ import annotations

import asyncio
import threading
import time

from modules import admin_tools
from modules.manage_features import access, command_registry, storage, texts
from modules.user_display import format_user

COMMAND_RESTRICT = "محدودیت دستورات"
COMMAND_UNRESTRICT = "لغو دستور"
#: مهلتِ هر مرحلهٔ انتخاب (مثل «پیام زماندار»).
FLOW_TTL = 180.0
ENTITY_TIMEOUT = 6.0

# این دو دستور ویژهٔ مالک‌اند و خودشان قابل محدودکردن نیستند.
command_registry.exclude(COMMAND_RESTRICT, COMMAND_UNRESTRICT)

STORAGE = storage.Storage("command_restrictions.json", {"groups": {}})

_LOCK = threading.RLock()
#: group_key → {admin_id_str → {"username", "label", "commands": {id: rec}}}
_INDEX = None
#: (group_key, owner_id_str) → state
_FLOWS = {}


def _log(logger, message, error=False):
    try:
        if logger is None:
            return
        (logger.log_error if error else logger.log_info)(message)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  ذخیره‌سازی
# ---------------------------------------------------------------------------
def _index():
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            data = STORAGE.load()
            groups = data.get("groups") if isinstance(data, dict) else None
            _INDEX = groups if isinstance(groups, dict) else {}
        return _INDEX


def reset_memory():
    """فقط حافظه (شبیه‌سازِ ری‌استارت در تست‌ها)."""
    global _INDEX
    with _LOCK:
        _INDEX = None
        _FLOWS.clear()


def _persist():
    snapshot = _index()

    def mutate(data):
        data.clear()
        data["groups"] = snapshot
    STORAGE.update(mutate)


def add(chat_id, admin_id, identifier, *, name, owner_id, username="",
        label="") -> bool:
    """``True`` اگر تازه اضافه شد؛ تکراری ⇒ ``False`` (بدون رکوردِ دوم)."""
    group, admin = storage.group_key(chat_id), str(int(admin_id))
    with _LOCK:
        entry = _index().setdefault(group, {}).setdefault(
            admin, {"username": "", "label": "", "commands": {}})
        if username:
            entry["username"] = username
        if label:
            entry["label"] = label
        created = identifier not in entry["commands"]
        if created:
            entry["commands"][identifier] = {
                "name": name, "by": int(owner_id), "ts": int(time.time())}
        _persist()
        return created


def remove(chat_id, admin_id, identifier) -> bool:
    group, admin = storage.group_key(chat_id), str(int(admin_id))
    with _LOCK:
        groups = _index()
        entry = groups.get(group, {}).get(admin)
        if not entry or identifier not in entry.get("commands", {}):
            return False
        del entry["commands"][identifier]
        if not entry["commands"]:
            del groups[group][admin]
            if not groups[group]:
                del groups[group]
        _persist()
        return True


def restrictions(chat_id, admin_id) -> dict:
    with _LOCK:
        entry = _index().get(storage.group_key(chat_id), {}).get(
            str(admin_id))
        return dict(entry["commands"]) if entry else {}


def restricted_admins(chat_id) -> dict:
    with _LOCK:
        return {admin: dict(entry) for admin, entry in
                _index().get(storage.group_key(chat_id), {}).items()
                if entry.get("commands")}


# ---------------------------------------------------------------------------
#  مجوز / تشخیص
# ---------------------------------------------------------------------------
def is_any_owner(chat_id, user_id) -> bool:
    if user_id is None:
        return False
    try:
        return bool(admin_tools.is_global_owner(user_id)
                    or access.is_group_owner(chat_id, user_id))
    except Exception:                                # noqa: BLE001
        return False


def _own_command(text):
    raw = str(text or "")
    if "دستور" not in raw[:32]:
        return None
    value = command_registry.normalize(raw)
    if value == COMMAND_RESTRICT:
        return "restrict"
    if value == COMMAND_UNRESTRICT:
        return "unrestrict"
    return None


def is_command(text) -> bool:
    return _own_command(text) is not None


def _flow(chat_id, user_id):
    if not _FLOWS:
        return None
    key = (storage.group_key(chat_id), str(user_id))
    with _LOCK:
        state = _FLOWS.get(key)
        if state is None:
            return None
        if state["expires"] < time.monotonic():
            del _FLOWS[key]
            return None
        return state


def _set_flow(chat_id, user_id, **state):
    state["expires"] = time.monotonic() + FLOW_TTL
    with _LOCK:
        _FLOWS[(storage.group_key(chat_id), str(user_id))] = state


def _clear_flow(chat_id, user_id):
    with _LOCK:
        _FLOWS.pop((storage.group_key(chat_id), str(user_id)), None)


def blocked_command(chat_id, user_id, text):
    """شناسهٔ دستوری که این پیام اجرا می‌کند و برای این ادمین محدود است."""
    groups = _INDEX if _INDEX is not None else _index()
    if not groups or user_id is None:
        return None
    group = groups.get(storage.group_key(chat_id))
    if not group:
        return None
    entry = group.get(str(user_id))
    if not entry:
        return None
    identifier = command_registry.load().match_message(text)
    if identifier is None or identifier not in entry["commands"]:
        return None
    if is_any_owner(chat_id, user_id):
        return None
    return identifier


def interest(chat_id, user_id, text) -> bool:
    """بررسیِ ارزان (بدون RPC): آیا این پیام به این قابلیت مربوط است؟"""
    try:
        if chat_id is None or user_id is None:
            return False
        if _FLOWS and _flow(chat_id, user_id) is not None:
            return True
        if _own_command(text) is not None:
            return True
        return blocked_command(chat_id, user_id, text) is not None
    except Exception:                                # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
#  نمایش
# ---------------------------------------------------------------------------
async def _reply(event, payload, logger=None):
    text, entities = payload
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:                       # noqa: BLE001
        _log(logger, f"COMMAND RESTRICT REPLY FAILED error={error!r}", True)
        return None


async def _entity(bot, target):
    try:
        return await asyncio.wait_for(bot.client.get_entity(target),
                                      ENTITY_TIMEOUT)
    except Exception:                                # noqa: BLE001
        return None


async def _admin_rows(bot, chat_id):
    """ادمین‌های ثبت‌شدهٔ گروه: ``[{id, username, label}]`` (مالک‌ها نه)."""
    from modules import admin_display
    from modules.admin_storage import load_admins
    from modules.group_id import normalize_group_id
    try:
        entries = load_admins().get(normalize_group_id(chat_id), [])
    except Exception:                                # noqa: BLE001
        entries = []
    rows, seen = [], set()
    for entry in list(entries):
        user_id, username, _name = admin_display.entry_fields(entry)
        if user_id is not None and is_any_owner(chat_id, user_id):
            continue
        key = user_id if user_id is not None else username.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        label = await admin_display.admin_label(bot, chat_id, entry)
        rows.append({"id": user_id, "username": username, "label": label})
    return rows


def _number(text):
    value = command_registry.normalize(text)
    return int(value) if value.isdigit() and len(value) <= 3 else None


# ---------------------------------------------------------------------------
#  نقطهٔ ورودِ واحد
# ---------------------------------------------------------------------------
async def intercept(bot, event, chat_id, user_id, sender, text, logger=None,
                    edited=False) -> bool:
    """``True`` ⇒ پیام مصرف شد و **هیچ** هندلرِ دیگری نباید اجرا شود."""
    if chat_id is None or user_id is None:
        return False
    kind = _own_command(text)
    state = _flow(chat_id, user_id) if _FLOWS else None
    if kind is None and state is None:
        identifier = blocked_command(chat_id, user_id, text)
        if identifier is None:
            return False
        _log(logger, "COMMAND RESTRICT BLOCKED "
             f"chat_id={chat_id} user_id={user_id} command={identifier}")
        await _reply(event, texts.restrict_blocked(), logger)
        return True
    if not is_any_owner(chat_id, user_id):
        _clear_flow(chat_id, user_id)
        # کاربر/ادمینِ عادی: نه وارد فرایند می‌شود، نه پیامی می‌گیرد.
        return kind is not None
    if edited:
        return kind is not None
    if kind == "restrict":
        return await _start_restrict(bot, event, chat_id, user_id, logger)
    if kind == "unrestrict":
        return await _start_unrestrict(event, chat_id, user_id, logger)
    step = state["step"]
    if step == "command":
        return await _take_command(bot, event, chat_id, user_id, sender,
                                   text, state, logger)
    number = _number(text)
    if number is None:
        return False
    if step == "admin":
        return await _pick_admin(bot, event, chat_id, user_id, number,
                                 state, logger)
    if step == "cancel_admin":
        return await _pick_cancel_admin(event, chat_id, user_id, number,
                                        state, logger)
    if step == "cancel_command":
        return await _pick_cancel_command(event, chat_id, user_id, number,
                                          state, logger)
    return False


async def _start_restrict(bot, event, chat_id, user_id, logger):
    _clear_flow(chat_id, user_id)
    rows = await _admin_rows(bot, chat_id)
    if not rows:
        return True
    _set_flow(chat_id, user_id, step="admin", rows=rows)
    await _reply(event, texts.restrict_start([row["label"] for row in rows]),
                 logger)
    return True


async def _pick_admin(bot, event, chat_id, user_id, number, state, logger):
    rows = state["rows"]
    if not 1 <= number <= len(rows):
        return False
    row = dict(rows[number - 1])
    if row["id"] is None:
        # ثبتِ قدیمی فقط با یوزرنیم: شناسهٔ عددی لازم است.
        entity = await _entity(bot, row["username"])
        try:
            row["id"] = int(getattr(entity, "id"))
        except (TypeError, ValueError):
            _clear_flow(chat_id, user_id)
            _log(logger, "COMMAND RESTRICT ADMIN UNRESOLVED "
                 f"chat_id={chat_id} username={row['username']!r}", True)
            return True
    if is_any_owner(chat_id, row["id"]):
        _clear_flow(chat_id, user_id)
        return True
    _set_flow(chat_id, user_id, step="command", admin=row)
    await _reply(event, texts.restrict_ask_command(row["label"]), logger)
    return True


async def _take_command(bot, event, chat_id, user_id, sender, text, state,
                        logger):
    registry = command_registry.load()
    identifier = registry.resolve_name(text)
    if identifier is None:
        # نامِ نادرست/غیرمدیریتی: چیزی ثبت و چیزی اجرا نمی‌شود؛ مالک
        # می‌تواند همان‌جا نامِ درست را بفرستد.
        state["expires"] = time.monotonic() + FLOW_TTL
        await _reply(event, texts.restrict_unknown_command(), logger)
        return True
    admin = state["admin"]
    name = registry.get(identifier)["name"]
    _clear_flow(chat_id, user_id)
    created = add(chat_id, admin["id"], identifier, name=name,
                  owner_id=user_id, username=admin["username"],
                  label=admin["label"])
    _log(logger, "COMMAND RESTRICT ADDED "
         f"chat_id={chat_id} admin={admin['id']} command={identifier} "
         f"new={created} by={user_id}")
    actor = format_user(sender, fallback="")
    if not actor:
        actor = format_user(await _entity(bot, user_id))
    await _reply(event, texts.restrict_done(
        user=admin["label"], command=name, actor=actor), logger)
    return True


async def _start_unrestrict(event, chat_id, user_id, logger):
    _clear_flow(chat_id, user_id)
    admins = restricted_admins(chat_id)
    if not admins:
        return True
    rows = [{"id": int(admin),
             "label": entry.get("label") or ("@" + entry["username"]
                                             if entry.get("username") else "")}
            for admin, entry in admins.items()]
    _set_flow(chat_id, user_id, step="cancel_admin", rows=rows)
    await _reply(event, texts.unrestrict_start(
        [row["label"] for row in rows]), logger)
    return True


async def _pick_cancel_admin(event, chat_id, user_id, number, state, logger):
    rows = state["rows"]
    if not 1 <= number <= len(rows):
        return False
    row = rows[number - 1]
    commands = restrictions(chat_id, row["id"])
    if not commands:
        _clear_flow(chat_id, user_id)
        return True
    items = [(identifier, record["name"])
             for identifier, record in commands.items()]
    _set_flow(chat_id, user_id, step="cancel_command", admin=row,
              items=items)
    await _reply(event, texts.unrestrict_pick(
        row["label"], [name for _id, name in items]), logger)
    return True


async def _pick_cancel_command(event, chat_id, user_id, number, state,
                               logger):
    items = state["items"]
    if not 1 <= number <= len(items):
        return False
    identifier = items[number - 1][0]
    admin = state["admin"]
    _clear_flow(chat_id, user_id)
    if remove(chat_id, admin["id"], identifier):
        _log(logger, "COMMAND RESTRICT REMOVED "
             f"chat_id={chat_id} admin={admin['id']} command={identifier} "
             f"by={user_id}")
        await _reply(event, texts.unrestrict_done(), logger)
    return True
