#!/usr/bin/env python3
"""📊 بنچمارک «عضویت اجباری کانال» — اعداد واقعی، بدون ادعا.

این ابزار سه چیز را می‌سنجد:

1. **مسیر گرم ما**: تصمیم هر پیام با کش حافظه/دیتابیس (بدون RPC).
2. **روش ممنوع**: گرفتن کل اعضای کانال در هر پیام (``channels.getParticipants``).
3. **هزینهٔ ری‌استارت** و نرخ کش.

⚠️ صداقت در اندازه‌گیری: هیچ سروری برای اتصال به سروش پلاس در دسترس
نیست، پس لایهٔ شبکه با یک کلاینت جعلی که **تأخیر ثابت** دارد شبیه‌سازی
می‌شود (پیش‌فرض ۱۲۰ میلی‌ثانیه برای هر RPC، شبیه RTT دیده‌شده در همین
پروژه). بنابراین اعداد «زمان تصمیم منطق» و «تعداد فراخوانی» هستند، نه
تأخیر واقعی سرور سروش.

اجرا:
    python3 tools/bench_force_join.py
    python3 tools/bench_force_join.py --rpc-latency 0.2 --messages 2000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import force_join_api as api          # noqa: E402
from modules import force_join_service as service  # noqa: E402
from modules import force_join_storage as storage  # noqa: E402
from modules.force_join_ids import channel_key     # noqa: E402

CHAT_ID = -1009910999
CHANNEL_ID = 1234599999
CHANNEL_HASH = 424242


class BenchClient:
    """کلاینت با تأخیر شبکهٔ مصنوعی و شمارندهٔ فراخوانی."""

    def __init__(self, members, latency=0.12):
        self.members = set(members)
        self.latency = latency
        self.get_participant_calls = 0
        self.get_participants_calls = 0

    async def _sleep(self):
        if self.latency:
            await asyncio.sleep(self.latency)

    async def get_entity(self, entity):
        await self._sleep()
        from splusthon import types
        return types.Channel(id=CHANNEL_ID, title="کانال بنچمارک", photo=None,
                             date=None, access_hash=CHANNEL_HASH,
                             username="benchchannel")

    async def get_input_entity(self, entity):
        from splusthon import types
        value = int(getattr(entity, "id", entity))
        return types.InputPeerUser(value, 1)

    async def get_me(self, input_peer=False):
        from splusthon import types
        return types.InputPeerUser(1, 1) if input_peer else \
            types.User(id=1, user_type=None)

    def __call__(self, request):
        from splusthon import functions, types
        from splusthon.tl.types import channels as tl_channels

        async def _run():
            if isinstance(request, functions.channels.GetFullChannelRequest):
                await self._sleep()
                from splusthon.tl.types import messages as tl_messages
                return tl_messages.ChatFull(
                    full_chat=types.ChannelFull(
                        id=CHANNEL_ID, about="", read_inbox_max_id=0,
                        read_outbox_max_id=0, unread_count=0, chat_photo=None,
                        notify_settings=None, bot_info=[], pts=0,
                        can_view_participants=True, participants_hidden=False,
                        participants_count=len(self.members),
                    ),
                    chats=[], users=[],
                )
            if isinstance(request, functions.channels.GetParticipantRequest):
                self.get_participant_calls += 1
                await self._sleep()
                user_id = getattr(request.participant, "user_id", None)
                if user_id in self.members:
                    return type("R", (), {"participant": types.ChannelParticipant(
                        user_id=user_id, date=None)})()
                from splusthon import errors
                raise errors.UserNotParticipantError(request=request)
            if isinstance(request, functions.channels.GetParticipantsRequest):
                self.get_participants_calls += 1
                await self._sleep()
                users = [types.User(id=uid, user_type=None, access_hash=1)
                         for uid in sorted(self.members)]
                page = users[request.offset: request.offset + request.limit]
                return tl_channels.ChannelParticipants(
                    count=len(users),
                    participants=[types.ChannelParticipant(user_id=u.id, date=None)
                                  for u in page],
                    chats=[], users=page,
                )
            raise AssertionError(type(request).__name__)

        return _run()


class BenchBot:
    def __init__(self, client):
        self.client = client
        self.bot_account_id = 1
        self.reply_input_peer_cache = {}
        self.message_delete_queue = None
        self.outgoing_sender = None
        self.notice_cleanup = None
        self.logger = type("L", (), {
            "log_info": staticmethod(lambda *a, **k: None),
            "log_error": staticmethod(lambda *a, **k: None),
        })()


class BenchEvent:
    def __init__(self, chat_id, user_id, message_id, text="پیام"):
        self.chat_id = chat_id
        self.is_private = False
        self._sender = type("U", (), {"id": user_id, "access_hash": 1,
                                      "username": None, "bot": False})()
        self.message = type("M", (), {"id": message_id, "message": text})()
        self.replies = []

    async def get_sender(self):
        return self._sender

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return type("S", (), {"id": 1})()


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


async def measure_ours(bot, record, users, messages, warm=True):
    latencies = []
    deleted = 0
    for index in range(messages):
        user_id = users[index % len(users)]
        event = BenchEvent(CHAT_ID, user_id, index + 1)
        started = time.perf_counter()
        blocked = await service.gate_message(bot, event, CHAT_ID, user_id,
                                             event._sender)
        latencies.append((time.perf_counter() - started) * 1000)
        deleted += 1 if blocked else 0
    return latencies, deleted


async def measure_forbidden(bot, users, messages, member_count):
    """روش ممنوع: در هر پیام کل فهرست اعضا گرفته می‌شود."""
    latencies = []
    calls = 0
    from splusthon import functions
    channel_input = api.input_channel(CHANNEL_ID, CHANNEL_HASH)
    for index in range(messages):
        user_id = users[index % len(users)]
        started = time.perf_counter()
        page = await bot.client(functions.channels.GetParticipantsRequest(
            channel=channel_input,
            filter=api.ChannelParticipantsRecent(),
            offset=0, limit=200, hash=0,
        ))
        pages = 1
        while len(page.users) and len(page.users) < member_count:
            page = await bot.client(functions.channels.GetParticipantsRequest(
                channel=channel_input,
                filter=api.ChannelParticipantsRecent(),
                offset=pages * 200, limit=200, hash=0,
            ))
            pages += 1
            if pages > 60:
                break
        calls += pages
        latencies.append((time.perf_counter() - started) * 1000)
        _ = user_id  # فقط برای شمارش هزینه؛ تصمیم واقعی هم روی همین لیست است
    return latencies, calls


def summarize(latencies, api_calls, messages, extra=None):
    payload = {
        "messages": messages,
        "api_calls": api_calls,
        "api_calls_per_message": round(api_calls / messages, 4) if messages else 0,
        "latency_ms_mean": round(statistics.fmean(latencies), 3) if latencies else 0,
        "latency_ms_median": round(statistics.median(latencies), 3) if latencies else 0,
        "latency_ms_p95": round(percentile(latencies, 0.95), 3),
        "latency_ms_max": round(max(latencies), 3) if latencies else 0,
    }
    if extra:
        payload.update(extra)
    return payload


async def run(args):
    workdir = Path(tempfile.mkdtemp(prefix="force-join-bench-"))
    storage.set_db_path(workdir / "bench.sqlite3")
    storage.reset_caches()
    service.reset_runtime_state()

    member_users = [10_000 + i for i in range(5)]
    outsider_users = [20_000 + i for i in range(5)]
    all_members = set(member_users) | {30_000 + i for i in range(args.member_count)}

    client = BenchClient(all_members, latency=args.rpc_latency)
    bot = BenchBot(client)

    storage.save_group(
        CHAT_ID, channel_id=CHANNEL_ID, channel_access_hash=CHANNEL_HASH,
        channel_link="https://splus.ir/benchchannel", channel_title="کانال بنچمارک",
        enabled=True, sync_mode=storage.SYNC_MODE_EVENT,
    )
    service.refresh_group_cache(CHAT_ID)
    record = storage.get_group(CHAT_ID)

    # --- گرم‌کردن: نخستین تصمیم هر کاربر (سرد) ---
    cold_latencies, cold_blocked = await measure_ours(
        bot, record, member_users + outsider_users, len(member_users) + len(outsider_users)
    )
    cold_api = client.get_participant_calls
    service.STATS["api_checks"] = 0

    # --- مسیر گرم: پیام‌های تکراری از همان کاربران ---
    client.get_participant_calls = 0
    hot_latencies, hot_blocked = await measure_ours(
        bot, record, member_users + outsider_users, args.messages
    )
    hot_api = client.get_participant_calls

    # --- ری‌استارت: کش حافظه پاک، دیتابیس می‌ماند ---
    service.reset_runtime_state()
    service.refresh_group_cache()
    client.get_participant_calls = 0
    client.get_participants_calls = 0
    restart_latencies, restart_blocked = await measure_ours(
        bot, record, member_users + outsider_users, 20
    )
    restart_api = client.get_participant_calls

    # --- روش ممنوع ---
    forbidden_latencies, forbidden_calls = await measure_forbidden(
        bot, all_members and list(all_members)[:5], args.forbidden_messages,
        len(all_members),
    )

    result = {
        "config": {
            "rpc_latency_seconds": args.rpc_latency,
            "members_in_channel": len(all_members),
            "hot_messages": args.messages,
            "forbidden_messages": args.forbidden_messages,
            "note": "لایهٔ شبکه شبیه‌سازی‌شده با تأخیر ثابت؛ اعداد تصمیم‌گیری منطق است",
        },
        "cold_start": summarize(cold_latencies, cold_api,
                               len(member_users) + len(outsider_users),
                               {"blocked": cold_blocked}),
        "hot_path": summarize(hot_latencies, hot_api, args.messages,
                              {"blocked": hot_blocked,
                               "cache_hit_rate": round(
                                   (service.STATS["cache_hits"] + service.STATS["db_hits"]) /
                                   max(1, service.STATS["decisions"]), 4)}),
        "after_restart": summarize(restart_latencies, restart_api, 20,
                                   {"blocked": restart_blocked}),
        "forbidden_baseline": summarize(forbidden_latencies, forbidden_calls,
                                        args.forbidden_messages,
                                        {"note": "دریافت کل اعضا در هر پیام"}),
    }
    result["ratio"] = {
        "api_calls_hot_vs_forbidden": (
            round(result["forbidden_baseline"]["api_calls_per_message"] /
                  max(1e-9, result["hot_path"]["api_calls_per_message"]), 1)
            if result["hot_path"]["api_calls_per_message"] else "inf"
        ),
        "latency_hot_vs_forbidden": round(
            result["forbidden_baseline"]["latency_ms_mean"] /
            max(1e-9, result["hot_path"]["latency_ms_mean"]), 1),
    }
    return result


def to_markdown(result):
    lines = []
    lines.append("| سنجه | مسیر گرم (ما) | روش ممنوع (کل اعضا در هر پیام) |")
    lines.append("|---|---|---|")
    hot = result["hot_path"]
    forb = result["forbidden_baseline"]
    lines.append(f"| فراخوانی API به‌ازای هر پیام | {hot['api_calls_per_message']} | "
                 f"{forb['api_calls_per_message']} |")
    lines.append(f"| میانگین تأخیر تصمیم (ms) | {hot['latency_ms_mean']} | "
                 f"{forb['latency_ms_mean']} |")
    lines.append(f"| میانه (ms) | {hot['latency_ms_median']} | {forb['latency_ms_median']} |")
    lines.append(f"| p95 (ms) | {hot['latency_ms_p95']} | {forb['latency_ms_p95']} |")
    lines.append(f"| بیشترین (ms) | {hot['latency_ms_max']} | {forb['latency_ms_max']} |")
    lines.append(f"| تعداد پیام اندازه‌گیری‌شده | {hot['messages']} | {forb['messages']} |")
    lines.append("")
    lines.append(f"- نرخ کش مسیر گرم: **{hot['cache_hit_rate'] * 100:.1f}%**")
    lines.append(f"- فراخوانی پس از ری‌استارت (۲۰ پیام): "
                 f"**{result['after_restart']['api_calls']}**")
    lines.append(f"- نخستین تصمیم هر کاربر (سرد): "
                 f"میانگین {result['cold_start']['latency_ms_mean']} ms، "
                 f"{result['cold_start']['api_calls']} فراخوانی برای "
                 f"{result['cold_start']['messages']} کاربر")
    ratio = result["ratio"]["api_calls_hot_vs_forbidden"]
    if ratio == "inf":
        lines.append(
            "- نسبت فراخوانی: مسیر گرم **صفر** فراخوانی دارد، روش ممنوع "
            f"**{forb['api_calls_per_message']}** فراخوانی در هر پیام."
        )
    else:
        lines.append(f"- نسبت فراخوانی ما به روش ممنوع: **۱ به {ratio}**")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc-latency", type=float, default=0.12)
    parser.add_argument("--messages", type=int, default=1000)
    parser.add_argument("--forbidden-messages", type=int, default=20)
    parser.add_argument("--member-count", type=int, default=2000)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    markdown = to_markdown(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print(markdown)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
