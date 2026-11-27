"""Per-group, registered-owner-only command aliases (Plus500).

Only the command token is translated; arguments and reply context are preserved.
The original handlers remain the sole authority for execution/permissions.
Persisted aliases are independent from group configuration and are deleted alone.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
import re
import time

from modules import runtime_db
from modules.group_id import normalize_group_id
from modules.group_storage import get_group_owner

try:
    from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote
except ImportError:  # unit tests without the bot SDK
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset, self.length = offset, length

    class MessageEntityBlockquote(MessageEntityBold):
        pass


NAMESPACE = "plus500_command_aliases_v1"
START = "plus500"
RESET = "لغو plus500"
SESSION_SECONDS = 900
_SESSIONS = {}  # group+registered owner -> (phase, canonical, monotonic time)
_FORBIDDEN = {START, RESET, "لغو", "انصراف", "تایید", "بله", "نه", "شرکت",
              "ثبت", "اسم", "اسم جدید", "نام جدید", "مالک", "رمز"}
# These are conversational answers or security-sensitive commands; the original
# authorization/confirmation flow must never be bypassed with renamed input.
_PROTECTED = {"پاکسازی کامل", "دو مرحله ای", "لغو دو مرحله ای", "ثبت مالک",
              "لغو مالک", "برکناری مالک", "تایید", "حذف گروه"}
_NORMALIZE = str.maketrans({"ي": "ی", "ك": "ک", "\u200c": " ", "\u200e": "", "\u200f": "", "\ufeff": ""})


def normalize(text):
    return " ".join(str(text or "").translate(_NORMALIZE).split())


def _u16(text):
    return len(text.encode("utf-16-le")) // 2


def _group(chat_id):
    return str(normalize_group_id(chat_id))


def is_owner(chat_id, user_id):
    if chat_id is None or user_id is None:
        return False
    registered = get_group_owner(chat_id)
    return registered is not None and str(registered) == str(user_id)


def is_awaiting(chat_id, user_id):
    """Prevent the core fast lane consuming the owner’s command-name answer."""
    return (chat_id is not None and user_id is not None
            and _session(chat_id, user_id) is not None
            and is_owner(chat_id, user_id))


def _session(chat_id, user_id):
    key = (_group(chat_id), str(user_id))
    item = _SESSIONS.get(key)
    if item and time.monotonic() - item[2] > SESSION_SECONDS:
        _SESSIONS.pop(key, None)
        return None
    return item


def _literal_strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [x.value for x in node.elts
                if isinstance(x, ast.Constant) and isinstance(x.value, str)]
    return []


@lru_cache(maxsize=1)
def supported_commands():
    """Discover canonical commands from the real dispatcher, not a second router.

    Deliberately only literal comparisons/startswith on dispatch variables are
    accepted: arbitrary user text, replies, and bot output are not commands.
    New literal commands in the dispatcher become eligible automatically.
    """
    path = Path(__file__).resolve().parents[1] / "handlers" / "message_handler.py"
    root = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    dispatch_vars = {"clean_text", "early_command", "norm_cmd_text", "normalized_cmd"}
    for node in ast.walk(root):
        if isinstance(node, ast.Compare):
            if isinstance(node.left, ast.Name) and node.left.id in dispatch_vars:
                for op, right in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.In)):
                        found.update(_literal_strings(right))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "startswith" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in dispatch_vars and node.args):
                found.update(_literal_strings(node.args[0]))
    # Additional command sources already exported by the existing router.
    from handlers.message_handler import RESERVED_COMMANDS, FOX_GAME_COMMANDS, FAST_MODERATION_COMMANDS
    found.update(name for name, _ in RESERVED_COMMANDS)
    found.update(FOX_GAME_COMMANDS)
    found.update(FAST_MODERATION_COMMANDS)
    return frozenset(normalize(name) for name in found
                     if 1 < len(normalize(name)) <= 48
                     and normalize(name) not in _FORBIDDEN | _PROTECTED
                     and not normalize(name).isdigit()
                     and not any(ch in normalize(name) for ch in "\n\r|:→›")
                     and not normalize(name).startswith(("!", "/", ".")))


def _aliases(chat_id):
    raw = runtime_db.kv_get(NAMESPACE, _group(chat_id), {})
    if not isinstance(raw, dict):
        return {}
    saved = raw.get("aliases", {})
    if not isinstance(saved, dict):
        return {}
    return {normalize(k): normalize(v) for k, v in saved.items()
            if isinstance(k, str) and isinstance(v, str)}


def resolve(chat_id, text):
    """Resolve at most one alias; original commands always have precedence."""
    value = normalize(text)
    if not value or value in {START, RESET}:
        return text
    aliases = _aliases(chat_id)
    if not aliases:
        return text
    originals = supported_commands()
    # A canonical command (with arguments) must never be shadowed.
    if any(value == name or value.startswith(name + " ") for name in originals):
        return text
    for canonical, alias in sorted(aliases.items(), key=lambda item: len(item[1]), reverse=True):
        if value == alias or value.startswith(alias + " "):
            return canonical + value[len(alias):]
    return text


def _valid_alias(chat_id, canonical, alias):
    if len(alias) < 2 or len(alias) > 48 or len(alias.split()) > 4:
        return "نام دستور باید بین ۲ تا ۴۸ نویسه و حداکثر چهار کلمه باشد."
    if not re.fullmatch(r"[\w\u0600-\u06ff]+(?: [\w\u0600-\u06ff]+){0,3}", alias, re.UNICODE):
        return "نام دستور فقط می‌تواند شامل حرف، عدد، زیرخط و فاصله باشد."
    if alias in _FORBIDDEN or alias in _PROTECTED or alias == canonical:
        return "این نام رزرو شده یا با دستور اصلی یکسان است."
    commands = supported_commands()
    if any(alias == cmd or alias.startswith(cmd + " ") or cmd.startswith(alias + " ")
           for cmd in commands):
        return "این نام با یکی از دستورهای اصلی ربات تداخل دارد."
    aliases = _aliases(chat_id)
    if any(alias == existing or alias.startswith(existing + " ")
           or existing.startswith(alias + " ") for key, existing in aliases.items()
           if key != canonical):
        return "این نام قبلاً برای دستور دیگری ثبت شده است."
    return None


def _entities(text, *, bold_spans=(), quotes=()):
    entities = []
    for needle in bold_spans:
        at = text.find(needle)
        if at >= 0:
            entities.append(MessageEntityBold(offset=_u16(text[:at]), length=_u16(needle)))
    for needle in quotes:
        at = text.find(needle)
        if at >= 0:
            entities.append(MessageEntityBlockquote(offset=_u16(text[:at]), length=_u16(needle)))
    return text, entities


def _prompt():
    heading = "🔺 : اسم کدام دستور را میخواهید تغییر دهید"
    body = "لطفا نام دستور مدیریتی یا عمومی رو بنویسید اگر دستورات رو نمیدانید راهنما را ارسال کنید"
    text = "≡ Plus 500 → online ❞\n\n" + heading + "\n" + body
    return _entities(text, bold_spans=(heading,), quotes=("≡ Plus 500 → online ❞",))


def _request_new(canonical):
    heading = "🔺 نام دستور تغییر کرد"
    previous = "نام قبلی : " + canonical
    new = "نام جدید :"
    text = heading + "\n" + previous + "\n" + new + "\nلطفا نام جدید ارسال کنید"
    return _entities(text, bold_spans=(heading,), quotes=(previous, new))


def _success(canonical, alias):
    heading = "🔺 نام دستور تغییر کرد"
    previous, new = "نام قبلی : " + canonical, "نام جدید : " + alias
    text = heading + "\n" + previous + "\n" + new + "\nاز این پس برای دستور «" + canonical + "» بنویسید " + alias
    return _entities(text, bold_spans=(heading,), quotes=(previous, new))


def _reset():
    text = "✅ : تمام دستورات توسط Plus 500 به حالت قبل بازگشتند"
    return _entities(text, bold_spans=(text,))


async def _reply(event, payload):
    text, entities = payload
    await event.reply(text, formatting_entities=entities)


async def handle(event, chat_id, user_id, text):
    """Claim only Plus500 and the registered owner's active workflow."""
    if getattr(event, "is_private", False):
        return False
    value = normalize(text)
    session = _session(chat_id, user_id) if user_id is not None else None
    if value not in (START, RESET) and not session:
        return False
    if not is_owner(chat_id, user_id):
        if session:
            _SESSIONS.pop((_group(chat_id), str(user_id)), None)
        # Do not reveal owner-only workflow to anyone else.
        return value in (START, RESET)
    if (getattr(event, "_is_edited_message", False)
            or getattr(event, "is_edit", False)
            or getattr(getattr(event, "message", None), "edit_date", None)):
        return True
    key = (_group(chat_id), str(user_id))
    if value == RESET:
        runtime_db.kv_delete(NAMESPACE, key[0])
        _SESSIONS.pop(key, None)
        await _reply(event, _reset())
        return True
    if value == START:
        _SESSIONS[key] = ("old", None, time.monotonic())
        await _reply(event, _prompt())
        return True
    phase, canonical, _ = session
    if phase == "old":
        if value not in supported_commands():
            await event.reply("❌ این دستور در فهرست دستورهای قابل تغییر نیست. نام اصلی دستور را بنویسید.")
            return True
        _SESSIONS[key] = ("new", value, time.monotonic())
        await _reply(event, _request_new(value))
        return True
    error = _valid_alias(chat_id, canonical, value)
    if error:
        await event.reply("❌ " + error)
        return True
    aliases = _aliases(chat_id)
    aliases[canonical] = value
    runtime_db.kv_set(NAMESPACE, key[0], {"aliases": aliases})
    _SESSIONS[key] = ("old", None, time.monotonic())
    await _reply(event, _success(canonical, value))
    return True
