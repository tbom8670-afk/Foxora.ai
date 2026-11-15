# -*- coding: utf-8 -*-
"""🧭 فهرستِ «دستورهای مدیریتیِ واقعی» ربات — استخراجِ خودکار از خودِ کد.

پروژه یک Registryِ واحد برای دستورها ندارد (دستورها شاخه‌های ``if`` در
هندلرها هستند). برای اینکه فهرست **دستی و ثابت** نباشد، این ماژول آن را در
زمانِ اجرا از منابعِ واقعیِ پروژه می‌سازد:

1. **کدِ هندلرها (AST)** — هر شاخه‌ای که متنِ پیام را با یک رشته مقایسه
   می‌کند (``clean_text == "اخراج"``، ``in {…}``، ``startswith("پاک ")``) و در
   بدنه‌اش بررسیِ مجوزِ ادمین/مالک دارد ⇒ دستورِ مدیریتی، با شمارهٔ خطِ هندلر.
2. **جدولِ مسیریابیِ خطِ ادمین** — ``group_dispatch._ADMIN_EXACT`` و پیشوندهای
   مدیریتی.
3. **دستورهای محافظت‌شدهٔ «دو مرحله ای»** و نام‌های جایگزینِ ثبت‌شده‌شان.
4. **RESERVED_COMMANDS** — (دستور، هندلر)؛ دستورهای با هندلرِ یکسان هم‌نام‌اند.
5. **«لیست ادمینی»** (``admin_help_builder``) — فقط تکه‌هایی که در کدِ واقعی
   هم به‌عنوان دستور وجود دارند (ثابت‌های ``*COMMAND*`` ماژول‌ها/هندلرها).

دستورِ تازه‌ای که به هر کدام از این ساختارها اضافه شود، بدون تغییرِ این فایل
شناخته می‌شود. بازی‌ها و دستورهای عمومی (جدول‌های خودِ پروژه) کنار می‌روند.
نتیجه با امضای فایل‌ها روی دیسک کش می‌شود تا استارتاپ کند نشود.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_VERSION = 6

#: هندلرهایی که دستورِ مدیریتی ندارند (روترِ بازی/اقتصاد/پیوی).
_NON_ADMIN_HANDLER_FILES = frozenset({
    "fox_games_router.py", "court_game_router.py", "fox_command_router.py",
    "economy_handler.py", "private_handler.py", "photo_download_handler.py",
})
_TEXT_VARS = frozenset({"clean_text", "message_text", "text", "command",
                        "early_command", "normalized", "raw", "cmd", "t",
                        "value", "norm"})
#: واژه‌های «پاسخِ مرحله‌ای» (تأیید/انصرافِ فرایندها) دستور نیستند.
_FLOW_REPLY_WORDS = frozenset({"تایید", "لغو", "انصراف", "بله", "خیر"})
_PERMISSION_MARKERS = (
    "has_admin_permission", "has_zero_permission", "is_global_owner",
    "_has_group_management_permission", "ADMIN_PERMISSION_DENIED_TEXT",
    "is_registered_admin", "is_group_owner", "_is_native_group_admin",
    "is_owner", "can_manage",
)
#: واژه‌هایی که دو دستورِ هم‌شاخه را «متضاد» می‌کنند (نه هم‌نام).
_OPPOSITE_TOKENS = frozenset({
    "لغو", "باز", "خاموش", "غیر", "رفع", "لیست", "حذف", "روشن", "فعال",
    "پاک", "ثبت", "برکناری", "آزاد",
})
_PREFIX_CHARS = "!/."
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_LOCK = threading.RLock()
_REGISTRY = None
#: دستورهای خودِ «محدودیت دستورات» (ویژهٔ مالک؛ قابل محدودکردن نیستند).
_EXCLUDED = set()


def exclude(*names):
    for name in names:
        _EXCLUDED.add(normalize(name))


def normalize(text) -> str:
    value = str(text or "").translate(_DIGITS)
    value = value.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    for ch in ("\u200f", "\u200e", "\ufeff", "\u0640"):
        value = value.replace(ch, "")
    value = " ".join(value.split())
    while value[:1] and value[0] in _PREFIX_CHARS:
        value = value[1:].lstrip()
    return value.lower()


def command_id(name) -> str:
    return "c" + hashlib.sha1(normalize(name).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
#  منبع ۱: AST هندلرها
# ---------------------------------------------------------------------------
def _names(node):
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            yield item.id
        elif isinstance(item, ast.Attribute):
            yield item.attr


def _string_items(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return [item.value for item in node.elts
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)]
    return []


def _commands_in_test(test, lenient=False):
    """``[(kind, [names…])]`` — kind ∈ {exact, prefix}."""
    found = []
    for node in ast.walk(test):
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Eq, ast.In)) for op in node.ops):
            if not lenient and not (set(_names(node)) & _TEXT_VARS):
                continue
            group = []
            for part in [node.left] + list(node.comparators):
                group.extend(_string_items(part))
            if group:
                found.append(("exact", group))
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == "startswith"
              and set(_names(node.func.value)) & _TEXT_VARS):
            group = []
            for argument in node.args:
                group.extend(_string_items(argument))
            if group:
                found.append(("prefix", group))
    return found


def _scan_handler_source(path, lenient=False):
    """``(admin_groups, prefixes, all_exact)`` از یک فایلِ هندلر."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return [], set(), set()
    admin_groups, prefixes, every = [], set(), set()
    all_prefixes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "startswith":
            for argument in node.args:
                for item in _string_items(argument):
                    all_prefixes.add(item)
                    every.add(item.rstrip(" @:"))
        if not isinstance(node, ast.If):
            continue
        commands = _commands_in_test(node.test, lenient)
        if not commands:
            continue
        seen = set(_names(ast.Module(body=node.body, type_ignores=[])))
        seen |= set(_names(node.test))
        gated = any(marker in seen for marker in _PERMISSION_MARKERS)
        for kind, group in commands:
            if kind == "exact":
                every.update(group)
                if gated:
                    admin_groups.append((path.name, node.lineno, group))
            else:
                for item in group:
                    every.add(item.rstrip(" @:"))
                    if gated:
                        prefixes.add(item)
    return admin_groups, prefixes | all_prefixes, every


def _command_constants(path):
    """رشته‌های ثابت‌هایی که نامشان ``COMMAND`` دارد (بدون import)."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    if "COMMAND" not in source:
        return set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and "COMMAND" in t.id
                   for t in targets):
            continue
        value = getattr(node, "value", None)
        if value is None:
            continue
        for item in ast.walk(value):
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                found.add(item.value)
    return found


# ---------------------------------------------------------------------------
#  منبع ۵: «لیست ادمینی»
# ---------------------------------------------------------------------------
def _help_candidates():
    try:
        from modules import admin_help_builder
        rows = admin_help_builder._admin_rows()
    except Exception:                                # noqa: BLE001
        return set()
    pieces = set()
    for text, entities in rows:
        raw = str(text).encode("utf-16-le")
        for entity in entities:
            if not type(entity).__name__.endswith("Blockquote"):
                continue
            quote = raw[entity.offset * 2:
                        (entity.offset + entity.length) * 2].decode(
                            "utf-16-le", "ignore")
            for line in quote.split("\n"):
                for part in re.split(r"[|←›]", line):
                    part = part.split(":")[-1]
                    part = part.split("@")[0]
                    part = re.sub(r"[«»]", " ", part)
                    part = normalize(part)
                    part = re.sub(r"(\s+\d+)+$", "", part).strip()
                    if part:
                        pieces.add(part)
    return pieces


# ---------------------------------------------------------------------------
#  ساخت
# ---------------------------------------------------------------------------
def _signature(files):
    digest = hashlib.sha1(
        f"{CACHE_VERSION}:{sorted(_EXCLUDED)}".encode())
    for path in sorted(files):
        try:
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
                          .encode())
        except OSError:
            digest.update(f"{path.name}:missing".encode())
    return digest.hexdigest()


def _source_files():
    handlers = [path for path in sorted((ROOT / "handlers").glob("*.py"))
                if path.name not in _NON_ADMIN_HANDLER_FILES]
    modules = sorted((ROOT / "modules").rglob("*.py"))
    return handlers, modules


class Registry:
    def __init__(self, commands, names, arg_capable):
        #: id → {"id", "name", "aliases", "handler", "args"}
        self.commands = commands
        #: نامِ نرمال‌شده → id
        self.names = names
        self.arg_capable = arg_capable
        self._by_length = sorted(names, key=len, reverse=True)

    def __len__(self):
        return len(self.commands)

    def resolve_name(self, text):
        """فقط تطبیقِ **دقیق** با نام/نامِ جایگزین (بدون آرگومان، بدون حدس)."""
        return self.names.get(normalize(text))

    def get(self, identifier):
        return self.commands.get(identifier)

    def match_message(self, text):
        """شناسهٔ دستوری که این پیام اجرا می‌کند (با آرگومان/پیشوند) یا None.

        طولانی‌ترین نام برنده است تا «پاک کامل» با «پاک» اشتباه نشود.
        """
        value = normalize(text)
        if not value:
            return None
        found = self.names.get(value)
        if found is not None:
            return found
        for name in self._by_length:
            if len(name) >= len(value) or not value.startswith(name):
                continue
            separator = value[len(name)]
            if separator not in " @:":
                continue
            rest = value[len(name):].lstrip(" :")
            identifier = self.names[name]
            if identifier in self.arg_capable:
                if len(rest.split()) <= 6:
                    return identifier
                return None
            if _looks_like_arguments(rest):
                return identifier
            return None
        return None


_TIME_WORDS = frozenset({"دقیقه", "ساعت", "روز", "ثانیه", "هفته", "ماه",
                         "دائم", "همیشه"})


def _looks_like_arguments(rest):
    tokens = rest.split()
    if not tokens or len(tokens) > 4:
        return False
    for token in tokens:
        if token.startswith("@") or token.isdigit() or token in _TIME_WORDS:
            continue
        return False
    return True


def _build():
    handler_files, module_files = _source_files()
    admin_groups, prefixes, real = [], set(), set()
    for path in handler_files:
        groups, found_prefixes, every = _scan_handler_source(path)
        admin_groups.extend(groups)
        prefixes |= found_prefixes
        real |= every
    for path in module_files + handler_files:
        real |= _command_constants(path)
    # ماژول‌های «…commands» هم دستور را با startswith/in تشخیص می‌دهند.
    manage_literals = set()
    for path in module_files:
        if "command" in path.name:
            managed = path.parent.name == "manage_features"
            _groups, found_prefixes, every = _scan_handler_source(
                path, lenient=managed)
            real |= every
            prefixes |= found_prefixes
            if managed:
                # پکیجِ «قابلیت‌های مدیریتی»: هر چه اینجا دستور است، مدیریتی است.
                manage_literals |= {normalize(name) for name in every}

    admin = {}            # نامِ نرمال‌شده → handler

    def add(name, handler):
        key = normalize(name)
        if key and len(key) <= 40:
            admin.setdefault(key, handler)

    alias_pairs = []
    for filename, line, group in admin_groups:
        for name in group:
            add(name, f"{filename}:{line}")
        cleaned = [normalize(name) for name in group if normalize(name)]
        plain = [name for name in cleaned
                 if not (set(name.split()) & _OPPOSITE_TOKENS)]
        for other in plain[1:]:
            alias_pairs.append((plain[0], other))

    manage_prefixes = ()
    public, games = set(), set()
    try:
        from modules import group_dispatch
        for name in group_dispatch._ADMIN_EXACT:
            add(name, "group_dispatch:admin_lane")
        manage_prefixes = tuple(group_dispatch._COMMAND_PREFIXES_MANAGE)
        # پیشوندهای خطِ ادمین فقط «آرگومان‌پذیر بودن» را نشان می‌دهند
        # (این جدول نامِ بازی‌ها را هم دارد، پس منبعِ «مدیریتی بودن» نیست).
        prefixes |= set(getattr(group_dispatch, "_ADMIN_PREFIXES", ()))
        public |= {normalize(name) for name in
                   getattr(group_dispatch, "_COMMAND_EXACT", ())}
        real |= set(group_dispatch._ADMIN_EXACT)
    except Exception:                                # noqa: BLE001
        pass
    for prefix in manage_prefixes:
        add(prefix, "group_dispatch:manage_prefix")
        prefixes.add(prefix)

    try:
        from modules.manage_features import two_factor
        for name in two_factor.PROTECTED_COMMANDS:
            add(name, "two_factor:protected")
            prefixes.add(name + " ")
        for alias, target in two_factor.PROTECTED_ALIASES.items():
            add(alias, "two_factor:protected")
            prefixes.add(alias + " ")
            alias_pairs.append((normalize(target), normalize(alias)))
    except Exception:                                # noqa: BLE001
        pass

    try:
        from handlers import message_handler
        by_handler = {}
        for name, handler in message_handler.RESERVED_COMMANDS:
            key = normalize(name)
            if key in admin:
                admin[key] = f"reserved:{handler}"
                prefixes.add(name + " ")
                by_handler.setdefault(handler, []).append(key)
        for names in by_handler.values():
            for other in names[1:]:
                alias_pairs.append((names[0], other))
        real |= set(message_handler._INTERNAL_EXACT_COMMANDS)
        games |= {normalize(n) for n in message_handler.FOX_GAME_COMMANDS}
    except Exception:                                # noqa: BLE001
        pass

    real_normalized = {normalize(name) for name in real}
    help_names = set()
    for piece in _help_candidates():
        if piece in real_normalized or piece in admin:
            help_names.add(piece)
            add(piece, "admin_help_list")

    try:
        from modules import entertainment_control
        games |= {normalize(n) for n in entertainment_control.GAME_COMMANDS}
    except Exception:                                # noqa: BLE001
        pass
    for name in list(admin):
        first = name[:1]
        weak = (admin[name] == "admin_help_list"
                and name not in manage_literals)
        if (name in games or (name in public and weak)
                or name in _FLOW_REPLY_WORDS
                or name in _EXCLUDED or not (first.isalnum())):
            del admin[name]
    # «غیر مجاز» و «غیرمجاز» یک دستورند.
    squashed = {}
    for name in admin:
        key = name.replace(" ", "")
        if key in squashed:
            alias_pairs.append((squashed[key], name))
        else:
            squashed[key] = name

    # --- هم‌نام‌ها (union-find) ---
    parent = {name: name for name in admin}

    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    for left, right in alias_pairs:
        if left in parent and right in parent:
            parent[find(right)] = find(left)

    groups = {}
    for name in admin:
        groups.setdefault(find(name), []).append(name)

    prefix_names = {normalize(prefix.rstrip(" @:")) for prefix in prefixes}
    commands, names, arg_capable = {}, {}, set()
    for members in groups.values():
        members.sort(key=lambda n: (n not in help_names, len(n), n))
        canonical = members[0]
        identifier = command_id(canonical)
        commands[identifier] = {
            "id": identifier, "name": canonical,
            "aliases": members[1:], "handler": admin[canonical],
            "args": any(member in prefix_names for member in members),
        }
        if commands[identifier]["args"]:
            arg_capable.add(identifier)
        for member in members:
            names[member] = identifier
    return commands, names, sorted(arg_capable)


def _cache_path():
    from modules.runtime_paths import runtime_config_file
    return Path(runtime_config_file("command_registry_cache.json"))


def load(force=False):
    """Registry (کش‌شده در حافظه و روی دیسک با امضای فایل‌ها)."""
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is not None and not force:
            return _REGISTRY
        handler_files, module_files = _source_files()
        signature = _signature(handler_files + module_files)
        cached = None
        if not force:
            try:
                cached = json.loads(_cache_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        if isinstance(cached, dict) and cached.get("signature") == signature:
            commands, names = cached["commands"], cached["names"]
            arg_capable = cached["arg_capable"]
        else:
            commands, names, arg_capable = _build()
            try:
                path = _cache_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + ".tmp")
                temp.write_text(json.dumps(
                    {"signature": signature, "commands": commands,
                     "names": names, "arg_capable": arg_capable},
                    ensure_ascii=False), encoding="utf-8")
                os.replace(temp, path)
            except OSError:
                pass
        _REGISTRY = Registry(commands, names, set(arg_capable))
        return _REGISTRY


def reset():
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
