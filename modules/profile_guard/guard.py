"""🛡️ گیت «ضد کانال و بیوگرافی» — نقطهٔ ورودِ قابلیت از message_handler.

جریان کار (کم‌هزینه و بدون شکستنِ مسیر پیام):

    پیام گروهی
        → روشن است؟ (storage، فقط خواندن فایل با کش)
        → فیلترها خالی‌اند؟ (بدون RPC، خروج)
        → اسنپ‌شات پروفایل از کش (بدون RPC در پیام‌های بعدی)
        → تطبیق نام کانال → سکوت دائمی
        → تطبیق جملهٔ بیو   → سکوت ۴ ساعته
        → حذف پیام + ارسال قالب

قواعد ایمنی (خواست کارفرما: «تشخیص اشتباه نکن»):

* معافیت: ادمین/مالک ثبت‌شده، VIP، خودِ ربات.
* مجازات فقط با **شاهد تازه**: اگر اسنپ‌شات قدیمی‌تر از
  ``PROFILE_GUARD_FRESH`` (پیش‌فرض ۱۲۰ ثانیه) باشد، پیش از مجازات یک‌بار
  تازه‌سازی می‌شود؛ اگر تازه‌سازی نشد ⇒ هیچ مجازاتی انجام نمی‌شود.
* اگر نام کانال قابل استخراج نبود (نبودِ ``access_hash``) هیچ تطبیقی
  انجام نمی‌شود (نبودِ اطلاعات ≠ تخلف).
* هر خطا در لاگ می‌ماند و مسیر پیام را نمی‌شکند.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from modules.profile_guard import (filters, importer, matcher, penalties,
                                  service, storage, texts)

#: مدت سکوت برای تخلفِ «جملهٔ بیوگرافی» (پیش‌فرض ۴ ساعت — طبق اسکرین‌شات).
BIO_MUTE_SECONDS = 4 * 3600

_notified: dict = {}

#: زمانِ آخرین تلاش برای «اعمالِ دوبارهٔ سکوتِ دائمی» (ضدِ اسپمِ RPC).
_healed: dict = {}


def _env_int(name: str, default: int, minimum: int = 0,
             maximum: int = 10_000_000) -> int:
    try:
        return max(minimum, min(maximum, int(str(os.getenv(name, default)).strip())))
    except (TypeError, ValueError):
        return default


def bio_mute_seconds() -> int:
    return _env_int("PROFILE_GUARD_BIO_MUTE", BIO_MUTE_SECONDS, 60,
                    365 * 24 * 3600)


def freshness_seconds() -> int:
    """حداکثر عمرِ مجازِ شاهد برای مجازات."""
    return _env_int("PROFILE_GUARD_FRESH", 120, 15, 86400)


def notice_cooldown() -> int:
    """فاصلهٔ حداقلی بین دو پیامِ اخطارِ **همان تخلفِ** همین کاربر."""
    return _env_int("PROFILE_GUARD_NOTICE_COOLDOWN", 3600, 0, 86400)


def reheal_interval() -> int:
    """فاصلهٔ حداقلی بین دو تلاشِ «سکوتِ دوباره» برای یک کاربرِ دائم‌ساکت."""
    return _env_int("PROFILE_GUARD_REHEAL_INTERVAL", 60, 5, 3600)


# ---------------------------------------------------------------------------
# ابزارهای کوچک
# ---------------------------------------------------------------------------
def _log(bot, message: str, *, error: bool = False) -> None:
    logger = getattr(bot, "logger", None)
    try:
        (logger.log_error if error else logger.log_info)(
            f"PROFILE GUARD {message}"
        )
    except Exception:
        pass


def _display_name(user) -> str:
    """نام نمایشی برای خطِ «⛔ کاربر : …» (بدون RPC)."""
    if user is None:
        return texts.UNKNOWN_VALUE
    username = getattr(user, "username", None)
    if username:
        return "@" + str(username).lstrip("@")
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = (first + " " + last).strip()
    return name or str(getattr(user, "id", "")) or texts.UNKNOWN_VALUE


def _is_admin(chat_id, user_id, sender) -> bool:
    try:
        from modules.admin_tools import has_admin_permission
        username = getattr(sender, "username", None)
        return bool(has_admin_permission(chat_id, user_id, username))
    except Exception:
        return False


def _is_exempt(chat_id, user_id, sender) -> bool:
    """معافیت‌های محلی: ادمین ثبت‌شده/مالک/VIP (بدون RPC)."""
    try:
        from modules import bio_link_service as _bls
        if _bls.local_exemption(chat_id, user_id,
                                getattr(sender, "username", None)):
            return True
    except Exception:
        pass
    return _is_admin(chat_id, user_id, sender)


def _schedule_cleanup(bot, chat_id, sent) -> None:
    """ثبتِ پیامِ خودکار در صفِ «حذف پس از ۶۰ ثانیه»ی خودِ ربات.

    همان سرویسِ ``modules/notice_cleanup`` که بقیهٔ هشدارهای خودکار ربات
    استفاده می‌کنند؛ تنها پیام‌های هشدار اینجا می‌آیند (پاسخِ دستورها نه).
    """
    try:
        from modules.notice_cleanup import capture_sent
        if capture_sent(bot, chat_id, sent):
            _log(bot, f"notice scheduled for cleanup chat_id={chat_id}")
    except Exception as error:
        _log(bot, f"notice cleanup schedule failed "
                  f"error={type(error).__name__}", error=True)


async def _send(bot, event, chat_id, payload, *, notice: bool = False) -> None:
    """ارسال پیام مستقل (مثل بقیهٔ قابلیت‌های همین ربات).

    ``notice=True`` ⇒ پیامِ هشدار است و پس از TTLِ سرویسِ
    ``notice_cleanup`` (پیش‌فرض ۶۰ ثانیه) از گروه پاک می‌شود.
    """
    text, entities = payload if isinstance(payload, tuple) else (payload, None)
    kwargs = {"formatting_entities": entities} if entities else {}
    target = None
    try:
        from modules.manage_features import peers
        target = peers.group_peer(bot, chat_id)
    except Exception:
        target = None
    for candidate in (target, chat_id):
        if candidate is None:
            continue
        try:
            sent = await bot.client.send_message(candidate, text, **kwargs)
        except Exception as error:
            _log(bot, f"send failed target={type(candidate).__name__} "
                      f"error={type(error).__name__}", error=True)
            continue
        if notice:
            _schedule_cleanup(bot, chat_id, sent)
        return
    try:
        sent = await event.reply(text, **kwargs)
    except Exception as error:
        _log(bot, f"reply failed error={type(error).__name__}", error=True)
        return
    if notice:
        _schedule_cleanup(bot, chat_id, sent)


# ---------------------------------------------------------------------------
# دستور روشن/خاموش
# ---------------------------------------------------------------------------
async def handle_toggle(bot, event, chat_id, user_id, sender, mode: str) -> bool:
    """«ضد کانال» / «ضد کانال خاموش» را اجرا می‌کند؛ ``True`` = مصرف شد."""
    if not _is_admin(chat_id, user_id, sender):
        try:
            from modules.admin_help_builder import ADMIN_PERMISSION_DENIED_TEXT
            await event.reply(ADMIN_PERMISSION_DENIED_TEXT)
        except Exception:
            pass
        return True

    if mode == "on":
        storage.enable(chat_id)
        try:
            filters.reload()
        except Exception:
            pass
        await _send(bot, event, chat_id, texts.enabled(filters.filter_count()))
        _log(bot, f"enabled chat_id={chat_id} user_id={user_id} "
                  f"filters={filters.filter_count()}")
        return True

    storage.disable(chat_id)
    await _send(bot, event, chat_id, texts.disabled())
    _log(bot, f"disabled chat_id={chat_id} user_id={user_id}")
    return True


# ---------------------------------------------------------------------------
# دستور «ضد کانال فایل <مسیر>» — ورودِ جمله‌ها از فایلِ Termux
# ---------------------------------------------------------------------------
FILE_COMMAND = "ضد کانال فایل"

_SECTION_WORDS = {
    "کانال": importer.SECTION_CHANNEL,
    "channel": importer.SECTION_CHANNEL,
    "channels": importer.SECTION_CHANNEL,
    "بیو": importer.SECTION_BIO,
    "بیوگرافی": importer.SECTION_BIO,
    "bio": importer.SECTION_BIO,
}


def _squash(text) -> str:
    folded = matcher.normalize(text)
    return "".join(ch for ch in folded if ch.isalnum())


def parse_file_command(text: str) -> Optional[dict]:
    """اگر متن دستورِ فایل باشد، ``{"section":…, "path":…}`` می‌دهد."""
    raw = str(text or "").strip()
    tokens = raw.split()
    if len(tokens) < 4:
        return None
    if _squash(tokens[0]) != _squash("ضد"):
        return None
    if not _squash(tokens[1]).startswith(_squash("کانال")):
        return None
    if _squash(tokens[2]) != _squash("فایل"):
        return None
    rest = tokens[3:]
    section = None
    if rest and _squash(rest[0]) in _SECTION_WORDS:
        section = _SECTION_WORDS[_squash(rest[0])]
        rest = rest[1:]
    path = " ".join(rest).strip().strip("«»\"'")
    if not path:
        return None
    return {"section": section, "path": path}


def _is_owner(user_id, sender) -> bool:
    """فقط مالکِ ربات می‌تواند فایلِ مسیر دلخواه را به ربات بدهد."""
    try:
        from modules.owner_check import is_global_owner
        if is_global_owner(sender if sender is not None else user_id):
            return True
    except Exception:
        pass
    try:
        from modules.owner_check import get_owner
        owner = get_owner() or {}
        return str(owner.get("user_id")) == str(user_id)
    except Exception:
        return False


async def handle_file_command(bot, event, chat_id, user_id, sender,
                              text: str) -> bool:
    """«ضد کانال فایل [کانال|بیو] <مسیر>» را اجرا می‌کند؛ ``True`` = مصرف شد."""
    parsed = parse_file_command(text)
    if parsed is None:
        return False
    if not _is_owner(user_id, sender):
        try:
            await event.reply("⛔️ این دستور فقط برای مالکِ ربات است.")
        except Exception:
            pass
        return True

    report = importer.import_file(parsed["path"],
                                  default_section=parsed["section"])
    message = importer.summary(report)
    if report.get("ok"):
        # بعدِ تغییرِ فهرست‌ها، آمارِ تازه در پیامِ روشن‌شدن درست بماند.
        try:
            filters.reload()
        except Exception:
            pass
    await _send(bot, event, chat_id, message)
    _log(bot, "file import path=%r section=%r ok=%r added=%r"
              % (parsed["path"], parsed["section"], report.get("ok"),
                 report.get("added")))
    return True


# ---------------------------------------------------------------------------
# بررسی پیام
# ---------------------------------------------------------------------------
def _evaluate(snapshot: dict, channel_phrases, bio_phrases):
    """نتیجهٔ بررسی یک اسنپ‌شات: ``(kind, matched_value)`` یا ``None``."""
    channel_id = snapshot.get("channel_id")
    if channel_id:
        title = snapshot.get("channel_title")
        username = snapshot.get("channel_username")
        for candidate in (title, ("@" + str(username).lstrip("@")) if username else None):
            hit = matcher.find_match(candidate, channel_phrases)
            if hit is not None:
                return "channel", hit.raw
    about = snapshot.get("about")
    if about:
        hit = matcher.find_match(about, bio_phrases)
        if hit is not None:
            return "bio", hit.raw
    return None


async def _punish(bot, event, chat_id, user_id, sender, message_id,
                  kind: str, matched: str, snapshot: dict) -> bool:
    """حذف پیام + سکوت + ارسال قالب. ``True`` = پیام مصرف شد."""
    duration = None if kind == "channel" else bio_mute_seconds()

    deleted = False
    try:
        await bot.client.delete_messages(int(chat_id), [int(message_id)])
        deleted = True
    except Exception as error:
        _log(bot, f"delete failed chat_id={chat_id} user_id={user_id} "
                  f"error={type(error).__name__}", error=True)
    storage_ok = False
    try:
        if getattr(bot, "admin_actions", None) is not None:
            storage_ok = bool(await bot.admin_actions.mute_user(
                chat_id, user_id, duration,
                user=sender, chat=None,
            ))
    except Exception as error:
        _log(bot, f"mute failed chat_id={chat_id} user_id={user_id} "
                  f"error={type(error).__name__}", error=True)

    # 🗂 ثبتِ پایدار: سکوتِ دائمی باید از ری‌استارت جان سالم بردارد و کاربرِ
    # متخلف «زیرِ نظر» بماند تا بیوگرافی‌اش هر بار تازه چک شود.
    try:
        penalties.record(
            chat_id, user_id, kind,
            None if duration is None else time.time() + int(duration),
        )
    except Exception as error:
        _log(bot, f"penalty record failed error={type(error).__name__}",
             error=True)
    # کشِ پروفایل دور ریخته می‌شود تا پیامِ بعدی حتماً تازه خوانده شود.
    try:
        service.invalidate(user_id)
    except Exception:
        pass

    # پیامِ اخطار: کلیدِ cooldown شاملِ نوع و مقدارِ تخلف است تا تخلفِ
    # **جدید** همیشه هشدار بگیرد و فقط تکرارِ عینِ همان تخلف ساکت بماند.
    key = f"{chat_id}:{user_id}:{kind}:{matched}"
    now = time.time()
    if now - _notified.get(key, 0) >= notice_cooldown():
        members = snapshot.get("participants")
        if kind == "channel" and members is None and snapshot.get("channel_id"):
            # فقط در همین یک مورد (کانال شناسایی‌شده) یک RPC اضافه برای
            # «تعداد اعضا» می‌زنیم؛ در بقیهٔ حالت‌ها هیچ درخواستی نیست.
            try:
                details = await service.channel_details(
                    bot, snapshot.get("channel_id"),
                    snapshot.get("access_hash"),
                )
                members = details.get("participants")
            except Exception:
                members = None
        payload = texts.notice_for(
            kind, _display_name(sender),
            value=matched if kind == "bio" else (snapshot.get("channel_title")
                                                 or matched),
            members=members, duration_seconds=duration,
        )
        await _send(bot, event, chat_id, payload, notice=True)
        _notified[key] = now

    _log(bot, f"punished kind={kind} chat_id={chat_id} user_id={user_id} "
              f"deleted={deleted} muted={storage_ok} "
              f"duration={'permanent' if duration is None else duration}")
    return deleted or storage_ok


async def _enforce_permanent(bot, event, chat_id, user_id, sender,
                             message_id) -> bool:
    """کاربرِ «دائم‌ساکت» دوباره پیام داده ⇒ سکوتش برداشته شده؛ فوراً برگردان.

    بدونِ RPCِ پروفایل و بدونِ پیامِ اخطار (هشدار قبلاً داده شده) — فقط
    حذفِ پیام + سکوتِ دوباره، با فاصلهٔ حداقلی تا RPC تکرار نشود.
    """
    try:
        await bot.client.delete_messages(int(chat_id), [int(message_id)])
    except Exception as error:
        _log(bot, f"reheal delete failed chat_id={chat_id} "
                  f"error={type(error).__name__}", error=True)

    key = f"{chat_id}:{user_id}"
    now = time.time()
    if now - _healed.get(key, 0) < reheal_interval():
        return True
    _healed[key] = now

    ok = False
    try:
        if getattr(bot, "admin_actions", None) is not None:
            ok = bool(await bot.admin_actions.mute_user(
                chat_id, user_id, None, user=sender, chat=None,
            ))
    except Exception as error:
        _log(bot, f"reheal mute failed chat_id={chat_id} user_id={user_id} "
                  f"error={type(error).__name__}", error=True)
    _log(bot, f"rehealed permanent chat_id={chat_id} user_id={user_id} "
              f"muted={ok}")
    return True


async def _reapply_permanent(bot, logger=None) -> int:
    """پس از ری‌استارت، سکوتِ دائمیِ ثبت‌شده را دوباره اعمال می‌کند."""
    rows = [row for row in penalties.permanent_entries()
            if storage.is_enabled(row.get("chat_id"))]
    applied = 0
    for row in rows:
        try:
            chat_id = int(row.get("chat_id"))
            user_id = int(row.get("user_id"))
        except (TypeError, ValueError):
            continue
        if getattr(bot, "admin_actions", None) is None:
            break
        try:
            ok = bool(await bot.admin_actions.mute_user(chat_id, user_id, None,
                                                       user=None, chat=None))
        except Exception as error:
            _log(bot, f"reapply failed chat_id={chat_id} user_id={user_id} "
                      f"error={type(error).__name__}", error=True)
            continue
        applied += 1 if ok else 0
        if reheal_interval():
            await asyncio.sleep(min(reheal_interval(), 5))
    _log(bot, f"permanent mutes reapplied count={applied} total={len(rows)}")
    return applied


def recover(bot, logger=None) -> int:
    """قلابِ استارتاپ: تعدادِ سکوت‌های دائمیِ ثبت‌شده را (بدون بلاک‌کردن) برمی‌گرداند."""
    try:
        total = len([row for row in penalties.permanent_entries()
                     if storage.is_enabled(row.get("chat_id"))])
        penalties.prune()
    except Exception:
        return 0
    if not total:
        return 0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return total
    try:
        loop.create_task(_reapply_permanent(bot, logger))
    except Exception:
        pass
    return total


async def guard_message(bot, event, chat_id, user_id, sender,
                        message_id) -> bool:
    """پیامِ گروهی را بررسی می‌کند؛ ``True`` یعنی رسیدگی شد (پاک شد)."""
    if not chat_id or not user_id or message_id is None:
        return False
    if getattr(event, "is_private", False):
        return False
    if not storage.is_enabled(chat_id):
        return False

    own_id = getattr(bot, "bot_account_id", None)
    if own_id is not None and str(user_id) == str(own_id):
        return False

    if filters.is_empty():
        _log(bot, f"no filters configured chat_id={chat_id}", error=True)
        return False

    if _is_exempt(chat_id, user_id, sender):
        return False

    # 🩹 خودترمیمی: این کاربر قبلاً «سکوت دائمی» گرفته ولی دارد پیام می‌دهد
    # ⇒ سکوتش برداشته شده (ری‌استارت/باگ سرویس)؛ بدون معطلی برمی‌گردانیم.
    try:
        if penalties.is_permanent(chat_id, user_id):
            return await _enforce_permanent(bot, event, chat_id, user_id,
                                            sender, message_id)
    except Exception as error:
        _log(bot, f"reheal check failed error={type(error).__name__}",
             error=True)

    channel_phrases = matcher.build(filters.channel_names())
    bio_phrases = matcher.build(filters.bio_sentences())

    # 🎯 کاربرِ سابقه‌دار (سکوتِ دائمی/موقتِ فعال یا تازه‌تمام‌شده) همیشه با
    # پروفایلِ تازه بررسی می‌شود تا تغییرِ بیوگرافی از دست نرود.
    try:
        watch = penalties.is_watched(chat_id, user_id)
    except Exception:
        watch = False

    snapshot = await service.snapshot(bot, sender, force=watch)
    if not snapshot:
        return False

    verdict = _evaluate(snapshot, channel_phrases, bio_phrases)
    if verdict is None:
        return False

    # 🔒 مجازات فقط با شاهد تازه
    age = time.time() - float(snapshot.get("ts") or 0)
    if age > freshness_seconds():
        fresh = await service.snapshot(bot, sender, force=True)
        if not fresh:
            _log(bot, f"no fresh evidence chat_id={chat_id} "
                      f"user_id={user_id}", error=True)
            return False
        snapshot = fresh
        verdict = _evaluate(snapshot, channel_phrases, bio_phrases)
        if verdict is None:
            return False

    kind, matched = verdict
    return await _punish(bot, event, chat_id, user_id, sender, message_id,
                         kind, matched, snapshot)
