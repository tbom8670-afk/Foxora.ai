# -*- coding: utf-8 -*-
"""🧪 تستِ رفعِ کندی/خطاهای لاگ: Auto Cleanup، GetParticipants، Governor، حافظه.

هر بخش یک سناریوی واقعیِ همان خطِ لاگ را بازسازی می‌کند:

1. ``IndexError('list index out of range')`` در snapshot پاک‌سازی
   (SPlusthon روی شناسهٔ خام ``channels[0]`` را می‌خواند).
2. ``GetUsersRequest → 404 NOT_FOUND`` برای کاربرِ حذف‌شده در batch.
3. ~۱۰ بار ``GetParticipantsRequest`` در ۱۴ ثانیه برای یک گروه.
4. ``GOVERNOR HOLD SLOW bucket=delete`` و حالتِ auto با نگهبانِ FloodWait.
5. نگهبان حافظه (بسته‌شدنِ خودکار ترموکس).
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import admin_tools, memory_guard, participant_cache  # noqa: E402
from modules import rpc_governor  # noqa: E402

PASSED = 0
FAILED = 0

CHAT = 24606755


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Logger:
    def __init__(self):
        self.lines = []
        self.errors = []

    def log_info(self, line):
        self.lines.append(line)

    def log_error(self, line):
        self.errors.append(line)

    def all(self):
        return self.lines + self.errors


class Bot:
    """ربات ساختگی: هیچ cacheای ندارد → مسیر «شناسهٔ خام» تست می‌شود."""

    def __init__(self, iter_factory=None, actions=None):
        self.logger = Logger()
        self.reply_input_peer_cache = {}
        self.client = SimpleNamespace(iter_messages=iter_factory)
        self.group_actions = actions
        self.message_delete_queue = None


# ---------------------------------------------------------------------------
# ۱ — snapshot پاک‌سازی: بدون IndexError، abort تمیز
# ---------------------------------------------------------------------------
def test_snapshot_never_passes_raw_int():
    print("\n### ۱ پاک‌سازی خودکار: شناسهٔ خام هرگز به کلاینت داده نمی‌شود")

    seen = []

    async def iter_messages(target, limit):
        seen.append(target)
        if isinstance(target, int):
            raise IndexError("list index out of range")   # همان خطای لاگ
        for message_id in (11, 12, 13):
            yield SimpleNamespace(id=message_id)

    class Peer:
        """InputPeer ساختگی (همان چیزی که get_input_entity برمی‌گرداند)."""

        def __init__(self, channel_id):
            self.channel_id = channel_id

    async def scenario():
        bot = Bot(iter_factory=iter_messages)
        peer = Peer(CHAT)

        async def get_input_entity(*_args, **_kwargs):
            return peer

        bot.client.get_input_entity = get_input_entity
        logs = []
        ids = await admin_tools._snapshot_cleanup_message_ids(
            bot, CHAT, 3000, logs.append)
        return ids, logs

    ids, logs = asyncio.run(scenario())
    check("اسنپ‌شات موفق شد (بدون IndexError)", ids == [11, 12, 13],
          f"-> {ids}")
    check("هیچ targetای از نوع int صدا زده نشد",
          all(not isinstance(target, int) for target in seen), f"-> {seen}")
    check("هیچ خطای IndexError در لاگ نیست",
          not any("IndexError" in line for line in logs), f"-> {logs}")


def test_snapshot_aborts_cleanly_when_nothing_resolves():
    print("\n### ۲ اگر هیچ Peerای resolve نشود، abort تمیز (بدون traceback)")

    async def scenario():
        class Actions:
            pass

        bot = Bot(iter_factory=None, actions=Actions())

        async def boom(*_args, **_kwargs):
            raise RuntimeError("cold entity cache")

        bot.client.get_input_entity = boom
        logs = []
        ids = await admin_tools._snapshot_cleanup_message_ids(
            bot, CHAT, 3000, logs.append)
        return ids, logs

    ids, logs = asyncio.run(scenario())
    failed = [line for line in logs if "SNAPSHOT FAILED" in line]
    check("اسنپ‌شات None برگرداند (شکست ≠ صفر پیام)", ids is None, f"-> {ids}")
    check("دلیلِ شکست در لاگ واضح است",
          failed and "reason=no_resolvable_peer" in failed[0], f"-> {failed}")
    check("هیچ traceback/Repr خامی در لاگ نیست",
          not any("Traceback" in line or "IndexError" in line for line in logs))


def test_execute_cleanup_reports_clear_abort():
    print("\n### ۳ execute_cleanup در شکستِ snapshot با پیام واضح abort می‌کند")

    async def scenario():
        locked = {"lock_calls": 0, "unlock_calls": 0}

        class Actions:
            async def lock_group(self, _chat_id):
                locked["lock_calls"] += 1

            async def unlock_group(self, _chat_id):
                locked["unlock_calls"] += 1

            async def _group_peer(self, _chat_id):
                raise RuntimeError("not resolved")

        bot = Bot(iter_factory=None, actions=Actions())
        bot.client.get_input_entity = None
        result = await admin_tools.execute_cleanup(
            bot, 9429374001, 700, logger=bot.logger)
        return result, locked

    result, locked = asyncio.run(scenario())
    check("پاک‌سازی اجرا نشد", result is False)
    check("گروه قفل نشد (abort پیش از هر تغییری)",
          locked == {"lock_calls": 0, "unlock_calls": 0}, f"-> {locked}")


# ---------------------------------------------------------------------------
# ۴ — GetUsers 404 در batch: یک کاربر حذف‌شده کل batch را نمی‌کشد
# ---------------------------------------------------------------------------
def test_safe_get_users_skips_deleted_user():
    print("\n### ۴ GetUsers: یک کاربر حذف‌شده (404) کل batch را نمی‌کشد")
    from modules.safe_rpc import get_users_safe

    class NotFoundError(Exception):
        pass

    calls = []

    class Client:
        async def get_users(self, ids):
            ids = list(ids)
            calls.append(list(ids))
            if len(ids) > 1:
                # رفتار سروش: یک شناسهٔ حذف‌شده کل درخواست را 404 می‌کند
                raise NotFoundError("404 NOT_FOUND")
            if int(ids[0]) == 999:
                raise NotFoundError("404 NOT_FOUND")
            return [SimpleNamespace(id=int(ids[0]))]

    async def scenario():
        client = Client()
        users = await get_users_safe(client, [101, 999, 102], chunk=3)
        return users

    users = asyncio.run(scenario())
    check("کاربران سالم برگشتند (بدون ۹۹۹ حذف‌شده)",
          sorted(user.id for user in users) == [101, 102], f"-> {users}")
    check("اول batch و بعد fallbackِ تک‌تک صدا زده شد",
          calls[0] == [101, 999, 102] and len(calls) > 1, f"-> {calls}")
    check("شناسه‌های نامعتبر فیلتر شدند",
          all(isinstance(value, int) and value > 0
              for call in calls for value in call))


# ---------------------------------------------------------------------------
# ۵ — کشِ GetParticipants
# ---------------------------------------------------------------------------
def test_participants_are_cached_and_coalesced():
    print("\n### ۵ GetParticipants: کش ۶۰ ثانیه + single-flight + حداقل فاصله")
    participant_cache.clear()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return "owner-label"

    async def scenario():
        # ۱۰ فراخوانی هم‌زمان → فقط یک RPC
        results = await asyncio.gather(*[
            participant_cache.get_or_fetch(CHAT, "owner", fetch)
            for _ in range(10)
        ])
        # فراخوانی بعدی داخل TTL → صفر RPC
        again = await participant_cache.get_or_fetch(CHAT, "owner", fetch)
        return results, again

    results, again = asyncio.run(scenario())
    check("همهٔ ۱۰ فراخوانی جواب گرفتند", results == ["owner-label"] * 10)
    check("فقط یک RPC واقعی زده شد", calls["n"] == 1, f"-> {calls['n']}")
    check("فراخوانی بعدی از کش آمد", again == "owner-label" and calls["n"] == 1)
    stats = participant_cache.describe()
    check("۹ درخواست coalesce شدند", stats["coalesced"] == 9, f"-> {stats}")
    check("TTL پیش‌فرض ۶۰ ثانیه است",
          participant_cache.DEFAULT_TTL == 60.0)
    check("حداقل فاصلهٔ RPC ۲ ثانیه است",
          participant_cache.MIN_INTERVAL == 2.0)


def test_participant_cache_throttles_failures():
    print("\n### ۶ در حالت خطا، polling به یک RPC در ۲ ثانیه محدود می‌شود")
    participant_cache.clear()
    calls = {"n": 0}

    async def failing():
        calls["n"] += 1
        raise RuntimeError("boom")

    async def scenario():
        first = await participant_cache.get_or_fetch(CHAT, "owner", failing)
        started = time.monotonic()
        second = await participant_cache.get_or_fetch(CHAT, "owner", failing)
        waited = time.monotonic() - started
        return first, second, waited

    first, second, waited = asyncio.run(scenario())
    check("خطا به کشِ دائمی تبدیل نشد", first is None and second is None)
    check("بین دو تلاشِ ناموفق حداقل ~۲ ثانیه فاصله بود",
          waited >= participant_cache.MIN_INTERVAL - 0.1, f"-> {waited:.2f}s")


# ---------------------------------------------------------------------------
# ۷ — Governor: حالتِ auto + نگهبانِ FloodWait
# ---------------------------------------------------------------------------
def test_governor_auto_delete_mode():
    print("\n### ۷ Governor: حالتِ auto برای delete با نگهبان FloodWait")
    os.environ["BOT_RPC_TOTAL_LIMIT"] = "3"
    os.environ["BOT_RPC_DELETE_LIMIT"] = "auto"
    try:
        governor = rpc_governor.RpcGovernor.from_environment(None)
    finally:
        os.environ.pop("BOT_RPC_TOTAL_LIMIT", None)
        os.environ.pop("BOT_RPC_DELETE_LIMIT", None)

    check("حالت auto فعال شد", governor.auto_delete is True)
    check("سقفِ delete در حالت سالم ۲ است",
          governor.class_limits["delete"] == 2 and governor._delete_cap() == 2,
          f"-> {governor.class_limits}")
    governor.note_flood_wait("delete", seconds=12)
    check("بعد از FloodWait سقف به ۱ برمی‌گردد",
          governor._delete_cap() == 1)
    governor._flood_until["delete"] = time.monotonic() - 1
    check("بعد از پایان cooldown به ۲ برمی‌گردد",
          governor._delete_cap() == 2)

    # پیش‌فرض پروژه محافظه‌کارانه می‌ماند
    default = rpc_governor.RpcGovernor.from_environment(None)
    check("پیش‌فرضِ بدون تنظیم، سقف ۱ است",
          default.auto_delete is False
          and default.class_limits["delete"] == 1, f"-> {default.class_limits}")

    # هوک FloodWait از لایهٔ بیرونی
    rpc_governor.register_active(governor)
    check("هوکِ نامِ درخواست، باکت delete را می‌شناسد",
          rpc_governor.bucket_for_request_name("DeleteMessages") == "delete")
    check("هوک FloodWait اجرا شد",
          rpc_governor.note_flood_wait("DeleteMessages", seconds=3) is True)
    rpc_governor.register_active(None)


# ---------------------------------------------------------------------------
# ۸ — نگهبان حافظه
# ---------------------------------------------------------------------------
def test_memory_guard_prunes_before_oom():
    print("\n### ۸ نگهبان حافظه: هرسِ کش‌ها پیش از OOM ترموکس")
    memory_guard._REGISTRY.clear()
    bucket = {"entries": [index for index in range(500)]}
    pruned_kinds = []

    def prune(kind):
        pruned_kinds.append(kind)
        before = len(bucket["entries"])
        limit = 50 if kind == "hard" else 200
        del bucket["entries"][limit:]
        return before - len(bucket["entries"])

    memory_guard.register("fake_cache", prune,
                          size=lambda: len(bucket["entries"]))
    os.environ["BOT_MEMORY_SOFT_MB"] = "1"
    os.environ["BOT_MEMORY_HARD_MB"] = "2"
    try:
        logger = Logger()
        report = memory_guard.check(logger)
    finally:
        os.environ.pop("BOT_MEMORY_SOFT_MB", None)
        os.environ.pop("BOT_MEMORY_HARD_MB", None)

    check("RSS خوانده شد", report["rss_mb"] > 0, f"-> {report}")
    check("در فشار حافظه، هرسِ اضطراری انجام شد",
          report["kind"] == "hard", f"-> {report}")
    check("کش تا سقفِ اضطراری کوچک شد", len(bucket["entries"]) == 50,
          f"-> {len(bucket['entries'])}")
    check("گزارش MEMORY GUARD در لاگ ثبت شد",
          any("MEMORY GUARD" in line for line in logger.lines),
          f"-> {logger.lines}")
    memory_guard.unregister("fake_cache")

    check("نگهبان در core راه‌اندازی می‌شود",
          "memory-guard" in (ROOT / "core" / "bot_working_split_ok.py")
          .read_text(encoding="utf-8"))
    check("مقدارهای پیش‌فرض محافظه‌کارانه‌اند",
          memory_guard.soft_limit_mb() == 220.0
          and memory_guard.hard_limit_mb() == 320.0)




def test_peer_fallback_never_returns_raw_int():
    print("\n### ۹ جایگزینِ Peer هرگز شناسهٔ خام برنمی‌گرداند (شبیه‌سازی splusthon)")
    import types as pytypes

    fake_tl = pytypes.ModuleType("splusthon.tl")
    fake_types = pytypes.ModuleType("splusthon.tl.types")

    class PeerChannel:
        def __init__(self, channel_id, access_hash=None):
            self.channel_id = channel_id
            self.access_hash = access_hash

    class InputPeerChannel:
        def __init__(self, channel_id, access_hash=None):
            self.channel_id = channel_id
            self.access_hash = access_hash

    fake_types.PeerChannel = PeerChannel
    fake_types.InputPeerChannel = InputPeerChannel
    fake_tl.types = fake_types
    fake_root = pytypes.ModuleType("splusthon")
    saved = {name: sys.modules.get(name) for name in
             ("splusthon", "splusthon.tl", "splusthon.tl.types")}
    sys.modules["splusthon"] = fake_root
    sys.modules["splusthon.tl"] = fake_tl
    sys.modules["splusthon.tl.types"] = fake_types
    try:
        variants = admin_tools._cleanup_peer_fallbacks(CHAT)
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    check("جایگزین ساخته شد", bool(variants), f"-> {variants}")
    check("هیچ شناسهٔ خامی در جایگزین‌ها نیست",
          all(not isinstance(item, int) for item in variants), f"-> {variants}")
    check("شناسهٔ نرمال‌شدهٔ کانال داخل Peer است",
          any(getattr(item, "channel_id", None) == abs(CHAT)
              for item in variants), f"-> {[getattr(v, 'channel_id', None) for v in variants]}")


def main():
    print("=" * 60)
    print("🧪 تست رفعِ کندی/خطا (Auto Cleanup, Participants, Governor, Memory)")
    print("=" * 60)
    test_snapshot_never_passes_raw_int()
    test_snapshot_aborts_cleanly_when_nothing_resolves()
    test_execute_cleanup_reports_clear_abort()
    test_safe_get_users_skips_deleted_user()
    test_participants_are_cached_and_coalesced()
    test_participant_cache_throttles_failures()
    test_governor_auto_delete_mode()
    test_memory_guard_prunes_before_oom()
    test_peer_fallback_never_returns_raw_int()
    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
