#!/usr/bin/env python3
"""🧪 پروبِ «کدام روش بررسی عضویت روی سروش پلاس جواب می‌دهد؟»

این ابزار **فقط می‌خواند** (هیچ پیامی حذف/ارسال نمی‌کند، هیچ عضویتی را
تغییر نمی‌دهد) و روی حساب واقعیِ همین باکس، یک‌به‌یک می‌سنجد:

| مسیر | متد |
|---|---|
| A | ``channels.getParticipant`` (تک‌کاربره) |
| B | ``channels.getParticipants`` + ``ChannelParticipantsRecent`` |
| C | ``channels.getParticipants`` + ``ChannelParticipantsSearch`` |
| D | ``channels.getParticipants`` + ``ChannelParticipantsAdmins`` |
| E | Bot API ``getChatMember`` (روش ربات‌های تلگرامی) |
| F | ``channels.getFullChannel`` (can_view_participants / participants_hidden) |

و در پایان می‌گوید: با این حساب و این کانال، کدام مسیرها قابل استفاده‌اند.

⚠️ **قبل از اجرا ربات را ببندید:**

    ./foxora stop
    python3 -m tools.soroush_membership_probe            # کانال‌های ثبت‌شده
    python3 -m tools.soroush_membership_probe -c @mychannel
    ./foxora

دو پروس با یک session string هم‌زمان کار نمی‌کنند (سرور آپدیت‌ها را فقط به
یکی می‌دهد)؛ اجرای پروب روی رباتِ در حال اجرا، ربات را «بی‌تحرک» می‌کند.

مثال‌ها:
    python3 -m tools.soroush_membership_probe
    python3 -m tools.soroush_membership_probe --channel @mychannel
    python3 -m tools.soroush_membership_probe -c 24844874 --user 32441922
    python3 -m tools.soroush_membership_probe -c https://splus.ir/x --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:  # pragma: no cover - dotenv همیشه در requirements هست
    pass

from modules import force_join_api as api          # noqa: E402
from modules import force_join_service as service  # noqa: E402
from modules import force_join_storage as storage  # noqa: E402

_OK = "✅ OK"
_NO = "⛔️ NOT_SUPPORTED"
_DENIED = "🚫 NO_ACCESS"
_ERR = "⚠️ ERROR"

#: کلاینتِ باز برای بستن تمیز در ``Ctrl+C``
_CLIENT = None


class _BotShell:
    """آداپتور کوچک: ماژول سرویس فقط به ``bot.client`` نیاز دارد."""

    def __init__(self, client):
        self.client = client
        self.bot_account_id = None
        self.reply_input_peer_cache = {}
        self.logger = None
        self.message_delete_queue = None
        self.outgoing_sender = None
        self.notice_cleanup = None

    def _noop(self, *_args, **_kwargs):
        return None

    log_info = _noop
    log_error = _noop


def _label(state: str) -> str:
    return {
        api.RESULT_MEMBER: _OK + " (عضو)",
        api.RESULT_NOT_MEMBER: _OK + " (غیرعضو)",
        api.RESULT_NOT_SUPPORTED: _NO,
        api.RESULT_NO_ACCESS: _DENIED,
        api.RESULT_FLOOD: "⏳ FLOOD_WAIT",
        api.RESULT_ERROR: _ERR,
        "ok": _OK,
        "empty": _OK + " (خالی)",
    }.get(state, state)


def registered_channels() -> list:
    """کانال‌های ثبت‌شده در همین دیتابیس (برای وقتی ``--channel`` نمی‌دهیم)."""
    try:
        rows = storage.list_groups(enabled_only=True)
        if not rows:
            rows = storage.list_groups()
    except Exception:
        return []
    seen, out = set(), []
    for row in rows or ():
        channel_id = row.get("channel_id")
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        out.append({
            "channel_id": int(channel_id),
            "access_hash": row.get("channel_access_hash"),
            "title": row.get("channel_title") or row.get("title"),
            "group_id": row.get("group_id"),
            "enabled": bool(row.get("force_join_enabled")),
        })
    return out


async def _probe(channel_ref, user_id=None, *, limit: int = 20,
                 known: dict | None = None) -> dict:
    """همهٔ مسیرها را روی کلاینت واقعی امتحان می‌کند و گزارش می‌دهد."""
    from splusthon import SoroushClient  # noqa: WPS433 (همان کلاینت پروژه)
    from splusthon.sessions import StringSession

    session_string = (os.getenv("SOROUSH_SESSION_STRING_FOXORA")
                      or os.getenv("SOROUSH_SESSION_STRING") or "").strip()
    api_id = os.getenv("API_ID") or os.getenv("SOROUSH_API_ID")
    api_hash = os.getenv("API_HASH") or os.getenv("SOROUSH_API_HASH")
    if not session_string or not api_id or not api_hash:
        raise SystemExit(
            "❌ SOROUSH_SESSION_STRING_FOXORA / API_ID / API_HASH در .env نیست.\n"
            "   این ابزار باید در همان مسیر ربات (کنار .env) اجرا شود."
        )

    global _CLIENT
    client = SoroushClient(StringSession(session_string), int(api_id), api_hash)
    _CLIENT = client
    report = {
        "at": int(time.time()),
        "channel_ref": str(channel_ref),
        "channel_id": None,
        "channel_title": None,
        "target_user": user_id,
        "self_user": None,
        "is_admin": None,
        "can_view_participants": None,
        "participants_hidden": None,
        "participants_count": None,
        "routes": {},
        "notes": [],
    }
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("❌ نشست (session) معتبر نیست؛ دوباره لاگین کنید.")

        # اگر کانال از دیتابیس آمده، resolve لازم نیست (access_hash داریم).
        resolved = {"ok": True, **known} if known else await api.resolve_channel(
            client, channel_ref, timeout=20)
        if not resolved.get("ok"):
            report["notes"].append(
                f"کانال حل نشد: {resolved.get('reason')} — لینک/آیدی را بررسی کنید.")
            return report
        channel_id = int(resolved["channel_id"])
        access_hash = resolved.get("access_hash")
        report["channel_id"] = channel_id
        report["channel_title"] = resolved.get("title") or (
            (known or {}).get("title"))
        channel_input = api.input_channel(channel_id, access_hash)
        if channel_input is None:
            report["notes"].append("access_hash کانال خوانده نشد.")
            return report

        me = await service.self_input_peer(_BotShell(client))
        my_id = getattr(me, "user_id", None) if me is not None else None
        report["self_user"] = my_id

        # ---- F) getFullChannel ----
        full = await api.get_full_channel(client, channel_input, timeout=20)
        if full.get("ok"):
            report["can_view_participants"] = full.get("can_view_participants")
            report["participants_hidden"] = full.get("participants_hidden")
            report["participants_count"] = full.get("participants_count")
            report["routes"]["getFullChannel"] = "ok"
        else:
            report["routes"]["getFullChannel"] = full.get("reason")

        # ---- A) getParticipant روی خودمان و روی کاربر هدف ----
        if me is not None:
            mine = await api.get_participant(client, channel_input, me, timeout=15)
            report["routes"]["getParticipant(self)"] = mine.get("state")
        if user_id:
            target_input = None
            try:
                entity = await asyncio.wait_for(
                    client.get_entity(int(user_id)), timeout=15)
                target_input = api.input_user(entity)
                report["notes"].append(
                    "InputPeer کاربر هدف از get_entity ساخته شد.")
            except Exception as error:
                report["notes"].append(
                    f"get_entity کاربر {user_id} نشد: {type(error).__name__} "
                    "— برای تست، کاربر باید حداقل یک‌بار برای این حساب دیده "
                    "شده باشد.")
            if target_input is not None:
                target = await api.get_participant(client, channel_input,
                                                   target_input, timeout=15)
                report["routes"]["getParticipant(target)"] = target.get("state")
            else:
                report["routes"]["getParticipant(target)"] = "unresolved"

        # ---- B) getParticipants (Recent) ----
        listing = await api.list_probe(client, channel_input, timeout=20)
        report["routes"]["getParticipants(recent,limit=1)"] = listing.get("state")

        # ---- C) getParticipants (Search) ----
        search = await api.search_participants(
            client, channel_input,
            (str(user_id) if user_id else "a"), user_id, limit=limit, timeout=20,
        )
        state = search.get("state")
        report["routes"]["getParticipants(search,limit=%d)" % limit] = state
        if state == "absent":
            report["notes"].append(
                "جست‌وجو «پیدا نشد» داد؛ این به‌تنهایی یعنی «عضو نیست» نیست "
                "(نتیجه ممکن است در صفحهٔ اول نباشد).")

        # ---- D) getParticipants (Admins) ----
        admins = await api.list_admins(client, channel_input, timeout=20)
        report["routes"]["getParticipants(admins)"] = admins.get("state")
        if admins.get("state") == "ok" and my_id:
            report["is_admin"] = int(my_id) in admins.get("user_ids", [])
        report["admins_found"] = len(admins.get("user_ids") or [])

        await asyncio.sleep(0.5)  # ادب نسبت به سرور

        # ---- E) Bot API getChatMember ----
        token = service.bot_api_token()
        if not token:
            report["routes"]["botApi.getChatMember"] = "no_token"
            report["notes"].append(
                "BOT_TOKEN تنظیم نشده؛ مسیر رسمی ربات‌ها (getChatMember) "
                "آزمایش نشد. این دقیق‌ترین مسیر است.")
        else:
            target = int(user_id or my_id or 0)
            if target:
                member = await api.bot_api_get_chat_member(
                    token, channel_id, target, timeout=15)
                report["routes"]["botApi.getChatMember"] = member.get("state")
                if member.get("state") == "no_access":
                    report["notes"].append(
                        "رباتِ BOT_TOKEN ادمین این کانال نیست؛ getChatMember "
                        "بدون ادمین بودن جواب نمی‌دهد.")
    finally:
        await _disconnect()
    return report


async def _disconnect() -> None:
    """بستن تمیز اتصال (و جلوگیری از هشدار Unclosed client session)."""
    global _CLIENT
    client = _CLIENT
    _CLIENT = None
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        pass
    try:  # اگر کتابخانه سشن aiohttp خودش را نبست، خودمان ببندیم
        breaker = getattr(client, "_sender", None)
        session = getattr(breaker, "_connection", None) or getattr(
            breaker, "session", None)
        session = getattr(session, "session", session)
        close = getattr(session, "close", None)
        if callable(close):
            await close()
    except Exception:
        pass


def _verdict(report: dict) -> list:
    routes = report.get("routes") or {}
    lines = []
    participant = routes.get("getParticipant(self)")
    listing = routes.get("getParticipants(recent,limit=1)")
    bot_api = routes.get("botApi.getChatMember")
    usable = []
    if participant in (api.RESULT_MEMBER, api.RESULT_NOT_MEMBER):
        usable.append("channels.getParticipant (تک‌کاربره، ارزان‌ترین)")
    if listing in ("ok", "empty"):
        usable.append("channels.getParticipants (اسنپ‌شات/diff دوره‌ای)")
        usable.append("ChannelParticipantsSearch (تأیید هدفمند عضو بودن)")
    if bot_api in ("member", "not_member"):
        usable.append("Bot API getChatMember (دقیق و لحظه‌ای، نیازمند ربات ادمین)")
    if usable:
        lines.append("مسیرهای قابل استفاده با این حساب:")
        lines.extend(f"  • {item}" for item in usable)
    else:
        lines.append("⛔️ هیچ مسیری جواب نداد؛ عضویت اجباری نمی‌تواند فعال شود.")
    if participant == api.RESULT_NOT_SUPPORTED:
        lines.append(
            "یادداشت: این سرور «channels.getParticipant» را پیاده نکرده است "
            "(422 NOT_SUPPORTED) — این طبیعی است و باگ حساب شما نیست.")
    return lines


def _print_report(report: dict) -> None:
    print("🧪 پروب بررسی عضویت — سروش پلاس")
    print(f"کانال: {report.get('channel_title') or '—'} "
          f"(id={report.get('channel_id')})")
    print(f"حساب: {report.get('self_user')} | ادمین کانال: "
          f"{'بله' if report.get('is_admin') else 'نه/نامشخص'}")
    print(f"can_view_participants={report.get('can_view_participants')} "
          f"participants_hidden={report.get('participants_hidden')} "
          f"count={report.get('participants_count')}")
    print("")
    print(f"{'مسیر':<42} نتیجه")
    print("-" * 64)
    for name, state in (report.get("routes") or {}).items():
        print(f"{name:<42} {_label(str(state))}")
    print("-" * 64)
    for line in _verdict(report):
        print(line)
    if report.get("notes"):
        print("")
        print("یادداشت‌ها:")
        for note in report["notes"]:
            print(f"  • {note}")
    print("")


async def _run_all(args, channels: list) -> list:
    reports = []
    for index, channel in enumerate(channels):
        if index:
            await asyncio.sleep(1.0)
        print(f"\n=== کانال {index + 1} از {len(channels)}: "
              f"{channel.get('title') or channel['channel_id']} ===")
        try:
            reports.append(await _probe(None, args.user, limit=args.limit,
                                        known=channel))
        except SystemExit:
            raise
        except Exception as error:
            print(f"⚠️ خطا در سنجش این کانال: {type(error).__name__}: {error}")
    return reports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="سنجش روش‌های بررسی عضویت کانال روی سرور سروش پلاس",
        epilog="بدون --channel، کانال‌های ثبت‌شده در دیتابیس ربات سنجیده "
               "می‌شوند. قبل از اجرا: ./foxora stop",
    )
    parser.add_argument("--channel", "-c", default=None,
                        help="@username یا لینک یا آیدی عددی کانال "
                             "(پیش‌فرض: کانال‌های ثبت‌شده)")
    parser.add_argument("--user", "-u", type=int, default=None,
                        help="آیدی عددی کاربر هدف (اختیاری)")
    parser.add_argument("--limit", type=int, default=20,
                        help="سقف صفحهٔ جست‌وجو (پیش‌فرض ۲۰، حداکثر ۵۰)")
    parser.add_argument("--json", action="store_true", help="خروجی JSON")
    args = parser.parse_args(argv)

    known_channels = []
    if not args.channel:
        known_channels = registered_channels()
        if not known_channels:
            print("⚠️ هیچ کانالی در دیتابیس این ربات ثبت نشده است.")
            print("  یا با «کانال من <لینک>» در گروه ثبت کنید، یا:")
            print("  python3 -m tools.soroush_membership_probe -c @mychannel")
            return 2
        print("ℹ️ --channel داده نشد؛ کانال‌های ثبت‌شده سنجیده می‌شوند: "
              + "، ".join(str(c["channel_id"]) for c in known_channels))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if known_channels:
            reports = loop.run_until_complete(_run_all(args, known_channels))
        else:
            reports = [loop.run_until_complete(
                _probe(args.channel, args.user, limit=args.limit))]
    except KeyboardInterrupt:
        print("\n⏹ متوقف شد؛ بستن اتصال…")
        try:
            loop.run_until_complete(_disconnect())
        except Exception:
            pass
        return 130
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    reports = [r for r in reports if r]
    if args.json:
        print(json.dumps(reports if known_channels else reports[0],
                         ensure_ascii=False, indent=2))
        return 0
    for report in reports:
        _print_report(report)
    print("پس از پایان: ./foxora")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
