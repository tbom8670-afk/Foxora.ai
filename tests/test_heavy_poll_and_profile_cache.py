# -*- coding: utf-8 -*-
"""تست‌های راند ۱۲ — طوفانِ GetParticipants، باکتِ heavy، و کشِ پروفایل.

هر بخش دقیقاً یک خطِ لاگِ واقعی را بازتولید و بعد رفعش را بررسی می‌کند::

    python3 tests/test_heavy_poll_and_profile_cache.py     (PYTHONPATH=.)

بخش‌ها:
 ۱) ۱۳ فراخوانیِ پشتِ‌سرِهمِ GetParticipants برای یک chat → ۱ RPC + ۱۲ کش
 ۲) coalescing: ۱۰ درخواستِ هم‌زمان → ۱ RPC (لاگ «coalesced»)
 ۳) حداقلِ فاصلهٔ ۱٫۵ ثانیه بین دو RPC واقعی
 ۴) Governor: باکتِ poll مستقل از heavy + سقفِ مستقل
 ۵) Governor: read سنگین سهمِ notice/delete را نمی‌خورد (HOLD SLOW)
 ۶) Governor: heavy/poll وقتی delete/notice/send در صف است شروع نمی‌شود
 ۷) کشِ GetFullUser: ۵ گروه، یک کاربر → ۱ RPC؛ خطا کش نمی‌شود
 ۸) PERF: زیرمرحله‌ها (bio_check/banned_word) در خطِ هشدار می‌آیند
 ۹) پیمایشِ صفحه‌ای force_join: اسکنِ دوم در ۸ ثانیه صفر RPC
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from modules import participant_cache  # noqa: E402
from modules import user_profile_cache  # noqa: E402
from modules import rpc_governor  # noqa: E402
from modules import performance  # noqa: E402
from modules import force_join_api  # noqa: E402

CHAT = 22389465
PASSED = 0
FAILED = 0


def check(label, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {label} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class CaptureLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, message):
        self.lines.append(str(message))

    def log_error(self, message):
        self.lines.append(str(message))

    def text(self):
        return "\n".join(self.lines)

    def count(self, needle):
        return sum(1 for line in self.lines if needle in line)


class FakePage:
    def __init__(self, users, participants=None, count=None):
        self.users = users
        self.participants = users if participants is None else participants
        self.count = len(users) if count is None else count


# ---------------------------------------------------------------------------
# ۱) طوفانِ ۱۳ فراخوانی → ۱ RPC + ۱۲ کش
# ---------------------------------------------------------------------------
def test_thirteen_calls_become_one_rpc():
    print("\n### ۱ طوفانِ ۱۳ فراخوانیِ GetParticipants روی یک chat_id")
    participant_cache.clear()
    logger = CaptureLogger()
    calls = {"n": 0}

    async def scenario():
        results = []
        for _ in range(13):
            async def fetch():
                calls["n"] += 1
                return FakePage([1, 2, 3])

            value = await participant_cache.get_or_fetch(
                CHAT, "participants_page", fetch,
                extra="ChannelParticipantsRecent:0:100", logger=logger,
            )
            results.append(value)
        return results

    results = asyncio.run(scenario())
    check("۱۳ فراخوانی → فقط ۱ RPC واقعی", calls["n"] == 1, f"-> rpc={calls['n']}")
    check("هر ۱۳ نتیجه دادهٔ درست گرفتند",
          all(r is not None and len(r.users) == 3 for r in results))
    check("لاگِ cache hit نوشته شد",
          logger.count("GetParticipants cache hit chat=") >= 1,
          f"-> hits_log={logger.count('GetParticipants cache hit')}")
    check("آمار: ۱ call و ۱۲ hit",
          participant_cache.STATS["calls"] == 1 and participant_cache.STATS["hits"] == 12,
          f"-> calls={participant_cache.STATS['calls']} hits={participant_cache.STATS['hits']}")


# ---------------------------------------------------------------------------
# ۲) coalescing
# ---------------------------------------------------------------------------
def test_concurrent_calls_coalesce():
    print("\n### ۲ درخواست‌های هم‌زمان → یک RPC (coalescing)")
    participant_cache.clear()
    logger = CaptureLogger()
    calls = {"n": 0}

    async def scenario():
        async def fetch():
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return FakePage([7])

        return await asyncio.gather(*[
            participant_cache.get_or_fetch(
                CHAT, "participants_page", fetch,
                extra="ChannelParticipantsRecent:0:100", logger=logger,
            )
            for _ in range(10)
        ])

    results = asyncio.run(scenario())
    check("۱۰ درخواستِ هم‌زمان → ۱ RPC", calls["n"] == 1, f"-> rpc={calls['n']}")
    check("همه نتیجه گرفتند", all(r is not None for r in results))
    check("لاگِ coalesced نوشته شد",
          logger.count("GetParticipants coalesced chat=") >= 1,
          f"-> coalesced_log={logger.count('coalesced')}")


# ---------------------------------------------------------------------------
# ۳) حداقل فاصله
# ---------------------------------------------------------------------------
def test_min_interval_between_rpcs():
    print("\n### ۳ حداقل فاصلهٔ ۱٫۵ ثانیه بین دو RPC واقعی")
    participant_cache.clear()
    stamps = []

    async def scenario():
        for extra in ("a", "b"):
            async def fetch():
                stamps.append(time.monotonic())
                return FakePage([])

            await participant_cache.get_or_fetch(
                CHAT, "participants_page", fetch, extra=extra,
            )

    asyncio.run(scenario())
    gap = stamps[1] - stamps[0]
    check("فاصلهٔ دو RPC واقعی ≥ ۱٫۴s", gap >= 1.4, f"-> gap={gap:.2f}s")
    check("MIN_INTERVAL در بازهٔ ۱ تا ۲ ثانیه",
          1.0 <= participant_cache.MIN_INTERVAL <= 2.0,
          f"-> {participant_cache.MIN_INTERVAL}")
    check("TTL در بازهٔ ۵ تا ۱۰ ثانیه",
          5.0 <= participant_cache.DEFAULT_TTL <= 10.0,
          f"-> {participant_cache.DEFAULT_TTL}")


# ---------------------------------------------------------------------------
# ۴) باکتِ poll مستقل
# ---------------------------------------------------------------------------
def test_poll_bucket_is_separate():
    print("\n### ۴ Governor: باکتِ poll مستقل از heavy")
    governor = rpc_governor.RpcGovernor.from_environment()
    poll = rpc_governor.classify_request(_FakeRequest("GetParticipantsRequest"))
    heavy = rpc_governor.classify_request(_FakeRequest("GetHistoryRequest"))
    full_user = rpc_governor.classify_request(_FakeRequest("GetFullUserRequest"))
    send = rpc_governor.classify_request(_FakeRequest("SendMessageRequest"))

    check("GetParticipantsRequest → bucket=poll", poll.bucket == "poll",
          f"-> {poll.bucket}")
    check("GetHistoryRequest هنوز bucket=heavy", heavy.bucket == "heavy",
          f"-> {heavy.bucket}")
    check("GetFullUserRequest دیگر bucket=other/send نیست",
          full_user.bucket == "heavy", f"-> {full_user.bucket}")
    check("GetFullUserRequest با send قاطی نمی‌شود",
          full_user.bucket != send.bucket, f"-> {full_user.bucket} vs {send.bucket}")
    check("سقفِ مستقلِ poll = ۱", governor.class_limits.get("poll") == 1,
          f"-> {governor.class_limits.get('poll')}")
    check("پایین‌ترین اولویت (P3) برای poll",
          poll.priority == rpc_governor.P3_HEAVY, f"-> {poll.priority}")


# ---------------------------------------------------------------------------
# ۵) read سنگین سهمِ notice/delete را نمی‌خورد
# ---------------------------------------------------------------------------
def test_heavy_read_does_not_eat_notice_budget():
    print("\n### ۵ read سنگین سهمِ notice را نمی‌خورد (HOLD SLOW)")
    governor = rpc_governor.RpcGovernor(
        total_limit=3, noncritical_limit=2, delete_limit=1,
        send_limit=1, heavy_limit=1, notice_limit=1, poll_limit=1,
    )
    # یک GetParticipants در جریان + یک delete در جریان (همان وضعیتِ لاگ)
    governor._active_total = 2
    governor._active_noncritical = 2
    governor._active_by_bucket["poll"] = 1
    governor._active_by_bucket["delete"] = 1

    notice_ok = governor._eligible(rpc_governor.P2_SEND, "notice")
    check("notice با وجودِ read سنگین اجازهٔ شروع دارد", notice_ok,
          f"-> eligible={notice_ok}")

    # بدونِ read: دو کارِ غیرِ read + سقفِ ۲ ⇒ notice باید صبر کند
    governor._active_by_bucket["poll"] = 0
    governor._active_by_bucket["send"] = 1
    governor._active_noncritical = 2
    check("با پر بودنِ سهمِ غیرِ read، notice صبر می‌کند",
          governor._eligible(rpc_governor.P2_SEND, "notice") is False)


# ---------------------------------------------------------------------------
# ۶) heavy/poll با صفِ send/notice شروع نمی‌شود
# ---------------------------------------------------------------------------
def test_heavy_yields_to_queued_send():
    print("\n### ۶ heavy/poll وقتی delete/notice در صف است شروع نمی‌شود")
    governor = rpc_governor.RpcGovernor(
        total_limit=3, noncritical_limit=2, delete_limit=1,
        send_limit=1, heavy_limit=1, notice_limit=1, poll_limit=1,
    )

    class Waiter:
        def __init__(self):
            self.future = asyncio.get_event_loop().create_future() if False else _DoneFuture()

    class _Pending:
        def done(self):
            return False

    pending = type("W", (), {"future": _Pending()})()
    governor._queues[rpc_governor.P2_SEND] = {"9429374": [pending]}
    check("صفِ notice ⇒ poll شروع نمی‌شود",
          governor._eligible(rpc_governor.P3_HEAVY, "poll") is False)
    check("صفِ notice ⇒ heavy هم شروع نمی‌شود",
          governor._eligible(rpc_governor.P3_HEAVY, "heavy") is False)
    check("صفِ notice ⇒ delete منعی ندارد",
          governor._eligible(rpc_governor.P1_DELETE, "delete") is True)


class _DoneFuture:
    def done(self):
        return True


# ---------------------------------------------------------------------------
# ۷) کشِ GetFullUser
# ---------------------------------------------------------------------------
def test_profile_cache_per_user():
    print("\n### ۷ کشِ GetFullUser: یک کاربر در ۵ گروه → ۱ RPC")
    user_profile_cache.clear()
    logger = CaptureLogger()
    calls = {"n": 0}

    async def scenario():
        results = []
        for _group in range(5):
            async def fetch():
                calls["n"] += 1
                return ("ok", "بیو نمونه")
            results.append(await user_profile_cache.get_about(
                70874302, fetch, logger=logger))
        return results

    results = asyncio.run(scenario())
    check("۵ بررسی → ۱ RPC", calls["n"] == 1, f"-> rpc={calls['n']}")
    check("همه نتیجهٔ درست گرفتند",
          all(value == ("ok", "بیو نمونه") for value in results))
    check("لاگِ cache hit نوشته شد",
          logger.count("GETFULLUSER cache hit user=") >= 1)
    check("TTL در بازهٔ ۳۰ تا ۶۰ ثانیه",
          30.0 <= user_profile_cache.TTL <= 60.0,
          f"-> {user_profile_cache.TTL}")

    user_profile_cache.clear()
    errors = {"n": 0}

    async def failing():
        errors["n"] += 1
        return ("error", None)

    for _ in range(3):
        asyncio.run(user_profile_cache.get_about(999, failing))
    check("خطا کش نمی‌شود (۳ بار فراخوانی شد)", errors["n"] == 3,
          f"-> calls={errors['n']}")


# ---------------------------------------------------------------------------
# ۸) PERF با زیرمرحله‌ها
# ---------------------------------------------------------------------------
def test_perf_warning_has_breakdown():
    print("\n### ۸ PERF: زیرمرحله‌ها در خطِ هشدار")
    logger = CaptureLogger()
    profiler = performance.MessagePerformance()
    profiler.mark("RECEIVE")
    profiler.note("bio_check", 812.3)
    profiler.note("banned_word", 0.6)
    profiler.mark("ADMIN_CHECK")
    result = profiler.finish(logger, 24100530, threshold_ms=0.0001)

    text = logger.text()
    check("خطِ PERF SLOW نوشته شد", "PERF SLOW" in text)
    check("زیرمرحلهٔ bio_check در خط هست", "bio_check=812.3" in text)
    check("زیرمرحلهٔ banned_word در خط هست", "banned_word=0.6" in text)
    check("کندترین مرحله گزارش شد", result.get("slowest_stage") in
          performance.MessagePerformance.STAGES, f"-> {result.get('slowest_stage')}")
    check("نتیجه شاملِ notes است",
          result.get("notes", {}).get("bio_check") == 812.3)


# ---------------------------------------------------------------------------
# ۹) پیمایشِ صفحه‌ای force_join از کش
# ---------------------------------------------------------------------------
def test_force_join_scan_uses_page_cache():
    print("\n### ۹ اسکنِ دومِ force_join در ۸ ثانیه صفر RPC")
    participant_cache.clear()

    # در محیطِ تست کتابخانهٔ SPlusthon نصب نیست؛ فقط همین دو نام لازم است.
    class FakeRequest:
        def __init__(self, channel=None, filter=None, offset=0, limit=0, hash=0):
            self.channel = channel
            self.filter = filter
            self.offset = offset
            self.limit = limit
            self.hash = hash

    class FakeRecent:
        pass

    saved = (force_join_api.LIBRARY_AVAILABLE, force_join_api.GetParticipantsRequest,
             force_join_api.ChannelParticipantsRecent)
    force_join_api.LIBRARY_AVAILABLE = True
    force_join_api.GetParticipantsRequest = FakeRequest
    force_join_api.ChannelParticipantsRecent = FakeRecent
    pages = {"n": 0}
    total_members = 1300
    page_size = 100

    async def client(request):
        pages["n"] += 1
        offset = getattr(request, "offset", 0)
        users = [type("U", (), {"id": i, "access_hash": 1})()
                 for i in range(offset, min(offset + page_size, total_members))]
        return FakePage(users, count=total_members)

    class Input:
        channel_id = CHAT

    async def scenario():
        first = await force_join_api.find_member(
            client, Input(), 42, max_members=2000, budget_seconds=10.0,
            delay=0.0, page_size=page_size,
        )
        after_first = pages["n"]
        second = await force_join_api.find_member(
            client, Input(), 42, max_members=2000, budget_seconds=10.0,
            delay=0.0, page_size=page_size,
        )
        return first, second, after_first

    try:
        first, second, after_first = asyncio.run(scenario())
    finally:
        (force_join_api.LIBRARY_AVAILABLE, force_join_api.GetParticipantsRequest,
         force_join_api.ChannelParticipantsRecent) = saved
    check("اسکنِ اول واقعاً صفحه‌ها را خواند", after_first == 13,
          f"-> pages={after_first}")
    check("اسکنِ اول «عضو نیست» قطعی داد",
          first.get("state") == "absent" and first.get("complete") is True,
          f"-> {first.get('state')}/{first.get('complete')}")
    check("اسکنِ دوم صفر RPC اضافه زد", pages["n"] == after_first,
          f"-> pages={pages['n']}")
    check("اسکنِ دوم همان نتیجه را داد",
          second.get("state") == first.get("state"))


class _FakeRequest:
    """درخواست ساختگی با همان نامِ کلاسِ SPlusthon."""

    def __init__(self, name):
        self.__class__ = type(name, (_FakeRequest,), {})
        self.peer = type("P", (), {"channel_id": CHAT})()


def main():
    print("=" * 60)
    print("راند ۱۲ — GetParticipants cache / heavy bucket / profile cache")
    print("=" * 60)
    test_thirteen_calls_become_one_rpc()
    test_concurrent_calls_coalesce()
    test_min_interval_between_rpcs()
    test_poll_bucket_is_separate()
    test_heavy_read_does_not_eat_notice_budget()
    test_heavy_yields_to_queued_send()
    test_profile_cache_per_user()
    test_perf_warning_has_breakdown()
    test_force_join_scan_uses_page_cache()
    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
