# -*- coding: utf-8 -*-
"""🛡️ سپر محافظت از ادمینی خودِ اکانت ربات (Self-Admin Demote Shield).

ریشهٔ مشکل در سروش‌پلاس / MTProto:
وقتی اکانتِ خودِ ربات در یک سوپرگروه ادمین است، ارسال هرگونه درخواستِ تغییر
وضعیت مشارکت‌کننده به مقصدِ خودِ اکانت ربات — از جمله:
  • ``channels.EditBannedRequest`` (حتی برای ``mute_user``، ``unmute_user`` یا
    ``unban_user`` با ``view_messages=False``!)
  • ``channels.EditAdminRequest`` (ارتقای محدود ``اختصاصی`` یا عزل با فلگ‌های خالی)
  • ``messages.EditChatAdminRequest`` / ``messages.DeleteChatUserRequest``
توسط سرور سروش‌پلاس پذیرفته شده و رکورد ``ChannelParticipantAdmin`` ربات را به
عضو عادی/محدود تبدیل می‌کند (یعنی ربات بدون خروج از گروه، خودبه‌خود از ادمینی
در می‌آید!).

این ماژول در ۴ سطح جلوی این اتفاق را می‌گیرد:
1. ذخیرهٔ پایدار ``bot_account_id`` در ``bot_identity.json`` تا حتی در ری‌استارت
   ناگهانی که ``get_me()`` تایم‌اوت بخورد، شناسهٔ خود ربات از ثانیهٔ صفر معلوم باشد.
2. تشخیص جامع هدفِ «خودِ ربات» (``InputPeerSelf``، ``InputUserSelf``، ``is_self``،
   ``_self_id`` نشست SPlusthon و ``bot_account_id``).
3. قلاب سطحِ سیم روی ``client._call`` و متدهای سطح بالای ``edit_permissions`` /
   ``edit_admin`` / ``kick_participant`` که هر درخواست خودزنی را پیش از ارسال به
   سوکت متوقف و لاگ می‌کند.
4. پاک‌سازی خودکار رکوردهای مسموم احتمالی از فایل‌های وضعیت در استارتاپ
   (``exclusive_access.json``، ``manual_mutes.json``، ``banned_users.json``).
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Optional

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

IDENTITY_FILE = runtime_config_file("bot_identity.json")


def _identity_file():
    return IDENTITY_FILE


_SELF_GUARDED_REQUESTS = frozenset({
    "EditBannedRequest",
    "EditAdminRequest",
    "EditChatAdminRequest",
    "DeleteChatUserRequest",
})


class SelfAdminProtectedError(RuntimeError):
    """درخواست تغییر دسترسی/محدودیت به مقصدِ خودِ اکانت ربات مسدود شد."""


def _safe_pos_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        ival = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return ival if ival > 0 else None


def load_persisted_bot_id() -> Optional[int]:
    """خواندن شناسهٔ ذخیره‌شدهٔ اکانت ربات از دیسک (مقاوم در برابر ری‌استارت ناگهانی)."""
    try:
        id_path = _identity_file()
        if not id_path.is_file():
            return None
        data = json.loads(id_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _safe_pos_int(data.get("bot_account_id"))
    except Exception:
        return None
    return None


def save_persisted_bot_id(bot_account_id: Any, *, username: Optional[str] = None) -> bool:
    """ذخیرهٔ اتمیک شناسهٔ اکانت ربات پس از دریافت موفق از ``get_me()``."""
    uid = _safe_pos_int(bot_account_id)
    if uid is None:
        return False
    try:
        id_path = _identity_file()
        current = load_persisted_bot_id()
        if current == uid and username is None:
            return True
        payload: dict[str, Any] = {"bot_account_id": uid}
        if username:
            payload["username"] = str(username)
        write_json(id_path, payload, indent=2)
        return True
    except Exception:
        return False


def known_bot_id(
    *,
    bot: Any = None,
    client: Any = None,
    bot_account_id: Any = None,
) -> Optional[int]:
    """استخراج شناسهٔ اکانت ربات از تمام منابع حافظه‌ای موجود."""
    candidates = [
        bot_account_id,
        getattr(bot, "bot_account_id", None) if bot is not None else None,
        (
            getattr(getattr(bot, "admin_actions", None), "bot_account_id", None)
            if bot is not None
            else None
        ),
        getattr(client, "_self_id", None) if client is not None else None,
        getattr(client, "_shield_bot_account_id", None) if client is not None else None,
    ]
    if bot is not None and client is None:
        bot_client = getattr(bot, "client", None)
        if bot_client is not None:
            candidates.append(getattr(bot_client, "_self_id", None))
            candidates.append(getattr(bot_client, "_shield_bot_account_id", None))
    for item in candidates:
        uid = _safe_pos_int(item)
        if uid is not None:
            return uid
    return None


def extract_target_user_id(target: Any) -> Optional[int]:
    """استخراج ``user_id`` عددی از انواع موجودیت/InputPeer/Peer."""
    if target is None or isinstance(target, bool):
        return None
    if isinstance(target, int):
        return target if target > 0 else None
    if isinstance(target, str):
        s = target.strip()
        return int(s) if s.isdigit() and int(s) > 0 else None
    cls_name = type(target).__name__
    if "Channel" in cls_name or "Chat" in cls_name:
        return None
    for attr in ("user_id", "id"):
        val = getattr(target, attr, None)
        uid = _safe_pos_int(val)
        if uid is not None:
            return uid
    peer = getattr(target, "peer", None)
    if peer is not None and peer is not target:
        return extract_target_user_id(peer)
    return None


def is_self_target(
    target: Any,
    *,
    bot: Any = None,
    client: Any = None,
    bot_account_id: Any = None,
) -> bool:
    """آیا ``target`` به خودِ اکانت ربات اشاره می‌کند؟"""
    if target is None:
        return False
    if isinstance(target, str) and target.strip().lower() in {"me", "self"}:
        return True
    cls_name = type(target).__name__
    if cls_name in {"InputPeerSelf", "InputUserSelf"}:
        return True
    if getattr(target, "is_self", False) is True:
        return True

    own_id = known_bot_id(bot=bot, client=client, bot_account_id=bot_account_id)
    if own_id is None:
        return False
    target_uid = extract_target_user_id(target)
    return target_uid is not None and target_uid == own_id


def _request_target_and_chat(request: Any) -> tuple[Any, Any]:
    req_name = type(request).__name__
    if req_name == "EditBannedRequest":
        return getattr(request, "participant", None), getattr(request, "channel", None)
    if req_name == "EditAdminRequest":
        return getattr(request, "user_id", None), getattr(request, "channel", None)
    if req_name in {"EditChatAdminRequest", "DeleteChatUserRequest"}:
        return getattr(request, "user_id", None), getattr(request, "chat_id", None)
    return None, None


async def _lazy_resolve_bot_id(client: Any, bot: Any = None, logger: Any = None) -> Optional[int]:
    """اگر هنگام ارسال درخواست حساس هنوز ``bot_account_id`` معلوم نیست، یک بار ``get_me`` بزن."""
    existing = known_bot_id(bot=bot, client=client)
    if existing is not None:
        return existing
    get_me = getattr(client, "get_me", None)
    if not callable(get_me) or getattr(client, "_shield_resolving_me", False):
        return None
    try:
        client._shield_resolving_me = True
        me = get_me()
        if inspect.isawaitable(me):
            me = await me
        uid = _safe_pos_int(getattr(me, "id", None))
        if uid is not None:
            try:
                client._shield_bot_account_id = uid
            except Exception:
                pass
            if bot is not None:
                try:
                    bot.bot_account_id = uid
                    admin_actions = getattr(bot, "admin_actions", None)
                    if admin_actions is not None:
                        admin_actions.bot_account_id = uid
                except Exception:
                    pass
            save_persisted_bot_id(uid)
            return uid
    except Exception as exc:
        if logger is not None and hasattr(logger, "log_error"):
            try:
                logger.log_error(f"SELF ADMIN SHIELD lazy get_me failed: {exc!r}")
            except Exception:
                pass
    finally:
        try:
            client._shield_resolving_me = False
        except Exception:
            pass
    return None


def _log_blocked(logger: Any, what: str, chat: Any, own_id: Any) -> None:
    msg = (
        "SELF ADMIN SHIELD BLOCKED — prevented self-demote/self-restriction "
        f"action={what} chat={chat!r} bot_account_id={own_id!r}"
    )
    if logger is not None and hasattr(logger, "log_error"):
        try:
            logger.log_error(msg)
            return
        except Exception:
            pass


async def _guard_tl_request(request: Any, *, client: Any, bot: Any = None, logger: Any = None) -> None:
    req_name = type(request).__name__
    if req_name not in _SELF_GUARDED_REQUESTS:
        return
    target, chat_ref = _request_target_and_chat(request)
    if target is None:
        return
    own_id = known_bot_id(bot=bot, client=client)
    if own_id is None:
        own_id = await _lazy_resolve_bot_id(client, bot=bot, logger=logger)
    if is_self_target(target, bot=bot, client=client, bot_account_id=own_id):
        _log_blocked(logger, req_name, chat_ref, own_id)
        raise SelfAdminProtectedError(
            f"Blocked {req_name} targeting the bot's own account ({own_id})"
        )


def install_self_admin_shield(client: Any, *, bot: Any = None, logger: Any = None) -> bool:
    """نصب سپر ضد عزلِ خودکار روی ``client`` (سطح سیم ``_call`` + متدهای کمکی)."""
    if client is None:
        return False
    try:
        client._shield_bot_ref = bot
        if bot is not None and getattr(bot, "bot_account_id", None):
            client._shield_bot_account_id = _safe_pos_int(bot.bot_account_id)
    except Exception:
        pass

    if getattr(client, "_self_admin_shield_installed", False):
        return True

    # ۱) قلاب سطح سیم روی client._call (مسیر اصلی تمام درخواست‌های SPlusthon)
    orig_call = getattr(client, "_call", None)
    if callable(orig_call):
        async def _shielded_call(sender, request, ordered=False, *args, **kwargs):
            bound_bot = getattr(client, "_shield_bot_ref", bot)
            if isinstance(request, (list, tuple)):
                for req in request:
                    await _guard_tl_request(req, client=client, bot=bound_bot, logger=logger)
            else:
                await _guard_tl_request(request, client=client, bot=bound_bot, logger=logger)
            return await orig_call(sender, request, ordered=ordered, *args, **kwargs)

        try:
            client._call = _shielded_call
        except (AttributeError, TypeError):
            pass

    # ۲) قلاب روی متدهای سطح بالا (برای پوشش کامل حتی در کلاینت‌های سفارشی/تست)
    for method_name in ("edit_permissions", "edit_admin", "kick_participant"):
        orig_method = getattr(client, method_name, None)
        if not callable(orig_method):
            continue

        def _make_wrapper(fn, mname):
            async def _wrapped(entity, user=None, *args, **kwargs):
                bound_bot = getattr(client, "_shield_bot_ref", bot)
                if user is not None:
                    own_id = known_bot_id(bot=bound_bot, client=client)
                    if own_id is None:
                        own_id = await _lazy_resolve_bot_id(client, bot=bound_bot, logger=logger)
                    if is_self_target(user, bot=bound_bot, client=client, bot_account_id=own_id):
                        _log_blocked(logger, mname, entity, own_id)
                        raise SelfAdminProtectedError(
                            f"Blocked {mname} targeting the bot's own account ({own_id})"
                        )
                res = fn(entity, user, *args, **kwargs)
                return await res if inspect.isawaitable(res) else res
            return _wrapped

        try:
            setattr(client, method_name, _make_wrapper(orig_method, method_name))
        except (AttributeError, TypeError):
            pass

    try:
        client._self_admin_shield_installed = True
    except (AttributeError, TypeError):
        pass
    return True


def sanitize_self_records(bot_account_id: Any, *, logger: Any = None) -> dict[str, int]:
    """پاک‌سازی رکوردهای مسموم احتمالی مربوط به خود اکانت ربات در فایل‌های پایدار.

    هیچ RPC شبکه‌ای ارسال نمی‌کند؛ فقط اگر در گذشته شناسهٔ خود ربات به اشتباه وارد
    ``exclusive_access.json``، ``manual_mutes.json`` یا ``banned_users.json`` شده
    باشد، در استارتاپ پاک می‌شود تا هرگز باعث عزل یا محدودیت خود ربات نگردد.
    """
    uid = _safe_pos_int(bot_account_id)
    cleaned = {"exclusive": 0, "manual_mute": 0, "banned": 0}
    if uid is None:
        return cleaned
    uid_str = str(uid)

    # الف) پاک‌سازی exclusive_access.json
    try:
        from modules.manage_features import storage as mf_storage

        def _clean_exclusive(data):
            groups = data.get("groups")
            if not isinstance(groups, dict):
                return
            for g_rec in groups.values():
                if not isinstance(g_rec, dict):
                    continue
                users = g_rec.get("users")
                if isinstance(users, dict) and uid_str in users:
                    users.pop(uid_str, None)
                    cleaned["exclusive"] += 1

        raw_exc = mf_storage.EXCLUSIVE_STORAGE.load()
        has_self_exc = any(
            isinstance(g, dict) and uid_str in (g.get("users") or {})
            for g in (raw_exc.get("groups") or {}).values()
        )
        if has_self_exc:
            mf_storage.EXCLUSIVE_STORAGE.update(_clean_exclusive)
    except Exception:
        pass

    # ب) پاک‌سازی manual_mutes.json
    try:
        from modules import manual_mute_storage

        mm_data = manual_mute_storage._load()
        if isinstance(mm_data, dict):
            for gid in list(mm_data.keys()):
                while manual_mute_storage.remove_manual_mute(gid, user_id=uid):
                    cleaned["manual_mute"] += 1
    except Exception:
        pass

    # ج) پاک‌سازی banned_users.json
    try:
        from modules import banned_storage

        removed_count, _, _ = banned_storage.remove_banned_everywhere(uid, None, None)
        if removed_count:
            cleaned["banned"] += int(removed_count)
    except Exception:
        pass

    if any(cleaned.values()) and logger is not None and hasattr(logger, "log_info"):
        try:
            logger.log_info(
                f"SELF ADMIN SHIELD SANITIZED bot_account_id={uid} cleaned={cleaned}"
            )
        except Exception:
            pass
    return cleaned
