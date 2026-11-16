"""Load-shedding: defer low-priority work when the connection/queues are heavy.

Covers the new ``modules.load_shedding`` sheds-state machine plus its two
integrations:

* ``bio_link_service.watcher`` must skip (defer) a periodic member-scan pass
  while the shedder is shedding, and
* ``daily_admin_report``'s 23:00 watcher must defer the report round while
  shedding (retried on later ticks inside the same hour).

Also a regression for the AI advertiser guard: the public punishment
announcement («⛔️ کاربر … به دلیل اسپم مکرر از گروه حذف شد») must NOT be
sent for ad-spam-guard punishments (``announce=False``).

Run: python3 tests/test_load_shedding.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Deterministic config: shed quickly after one overloaded sample, recover
# quickly after one clean sample, no minimum state age.
os.environ.setdefault("BOT_LOAD_SHED", "1")
os.environ.setdefault("BOT_LOAD_SHED_SAMPLE_SECONDS", "0.2")
os.environ.setdefault("BOT_LOAD_SHED_ENTER_SAMPLES", "1")
os.environ.setdefault("BOT_LOAD_SHED_EXIT_SAMPLES", "1")
os.environ.setdefault("BOT_LOAD_SHED_MIN_STATE_SECONDS", "0")
os.environ.setdefault("BOT_LOAD_SHED_GOVERNOR_WAIT_MS", "100")
os.environ.setdefault("BOT_LOAD_SHED_GOVERNOR_WAITING", "3")
os.environ.setdefault("BOT_LOAD_SHED_RPC_PENDING", "4")
os.environ.setdefault("BOT_LOAD_SHED_SENDER_OLDEST_MS", "1000")
os.environ.setdefault("BOT_LOAD_SHED_DISPATCH_PENDING", "5")

from modules import load_shedding  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class FakeLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, msg):
        self.lines.append(str(msg))

    def log_error(self, msg, *a, **k):
        self.lines.append(str(msg))


class FakeGovernor:
    def __init__(self, queues=None, latency_ema=None, latency_samples=0):
        self._queues = queues or {}
        self._latency_ema = latency_ema
        self._latency_samples = latency_samples
        self.adaptive_healthy_ms = 500.0


class _FakeFuture:
    def done(self):
        return False


class _FakeWaiter:
    def __init__(self, age_seconds):
        self.future = _FakeFuture()
        self.enqueued_at = time.perf_counter() - age_seconds


class FakeBot:
    """Just enough surface for runtime_snapshot.collect_sync + the shedder."""

    def __init__(self, governor=None):
        self.started_at = time.time()
        self.logger = FakeLogger()
        self.rpc_governor = governor
        # Everything snapshot touches must getattr-miss cleanly:
        self.moderation_queue = None
        self.message_delete_queue = None
        self.client = None
        self.outgoing_sender = None
        self.group_dispatcher = None
        self.performance_monitor = None


def make_shedder(governor=None):
    bot = FakeBot(governor=governor)
    return load_shedding.LoadShedder(bot), bot


def overloaded_governor():
    waiter = _FakeWaiter(age_seconds=5.0)  # 5000ms >= BOT_LOAD_SHED_GOVERNOR_WAIT_MS
    return FakeGovernor(queues={0: {"-100": [waiter, waiter, waiter, waiter]}})


_LOOP = None


def run(coro):
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


print("### load_shedding module")

shedder, bot = make_shedder()
check("starts in normal state", shedder.shedding is False)

status = shedder.sample(force=True)
check("sample returns status dict", isinstance(status, dict) and "shedding" in status)
check("idle bot is not shedding", status["shedding"] is False)
check("clean sample has zero score", status["score"] == 0.0)

shedder_ov, bot_ov = make_shedder(governor=overloaded_governor())
status = shedder_ov.sample(force=True)
check("overloaded governor scores >= enter threshold",
      status["score"] >= shedder_ov.enter_score)
check("overloaded bot enters shedding", status["shedding"] is True)
check("shedding reports reasons", len(status["reasons"]) >= 1)
check("transition logged", any("LOAD SHED ENTER" in line for line in bot_ov.logger.lines))

check("should_defer is True while shedding",
      shedder_ov.should_defer("some.feature") is True)
check("deferred counted in stats",
      shedder_ov.status()["stats"]["deferred"].get("some.feature", 0) == 1)

base = 30.0
stretched = shedder_ov.stretch("some.feature", base)
check("stretch multiplies interval while shedding",
      stretched == base * shedder_ov.stretch_factor and stretched > base)

# Recovery: swap in a quiet governor → one clean sample exits shedding.
bot_ov.rpc_governor = FakeGovernor()
status = shedder_ov.sample(force=True)
check("recovered bot leaves shedding", status["shedding"] is False)
check("exit transition logged", any("LOAD SHED EXIT" in line for line in bot_ov.logger.lines))
check("stretch is identity when normal", shedder_ov.stretch("some.feature", base) == base)

# Normal state + min_interval spacing: second immediate call defers.
shedder2, _bot2 = make_shedder()
check("first call passes", shedder2.should_defer("spaced", min_interval=60.0) is False)
check("immediate re-run defers via min_interval",
      shedder2.should_defer("spaced", min_interval=60.0) is True)
check("without min_interval the same feature passes again",
      shedder2.should_defer("unspaced") is False)

# Disabled master switch: never sheds even when overloaded.
os.environ["BOT_LOAD_SHED"] = "0"
shedder_off, _bot_off = make_shedder(governor=overloaded_governor())
shedder_off.sample(force=True)
check("BOT_LOAD_SHED=0 never sheds", shedder_off.shedding is False)
check("BOT_LOAD_SHED=0 never defers", shedder_off.should_defer("x") is False)
os.environ["BOT_LOAD_SHED"] = "1"

# Slow connection signal: high RPC latency EMA alone contributes to the score.
shedder_slow, _ = make_shedder(
    governor=FakeGovernor(latency_ema=2500.0, latency_samples=20)
)
status = shedder_slow.sample(force=True)
check("slow connection (latency EMA) raises the score", status["score"] > 0.0)
check("latency shows up in reasons",
      any(str(r).startswith("rpc_latency_ema_ms") for r in status["reasons"]))

# get_shedder caches one instance per bot.
bot_cache = FakeBot()
s1 = load_shedding.get_shedder(bot_cache)
s2 = load_shedding.get_shedder(bot_cache)
check("get_shedder caches per bot", s1 is s2 and getattr(bot_cache, "_load_shedder") is s1)


# ----------------------------------------------------------------------
print("### bio_link_service.watcher defers its pass while shedding")

from modules import bio_link_service  # noqa: E402


def run_watcher_round(*, shedding):
    """Run the watcher loop until the first post-scan sleep; record actions."""
    bot = FakeBot(governor=overloaded_governor() if shedding else FakeGovernor())
    calls = {"scan": 0}

    async def fake_scan(bot_, chat_id_, logger=None):
        calls["scan"] += 1

    orig_scan = bio_link_service.scan_group
    orig_groups = bio_link_service.active_groups
    orig_auto = bio_link_service.auto_scan_members
    orig_interval = bio_link_service.scan_interval
    orig_start = bio_link_service.start_delay
    orig_enabled = bio_link_service.bio_link_storage.is_enabled
    bio_link_service.scan_group = fake_scan
    bio_link_service.active_groups = lambda: [-1001]
    bio_link_service.auto_scan_members = lambda: True
    bio_link_service.scan_interval = lambda: 0.05
    bio_link_service.start_delay = lambda: 0
    bio_link_service.bio_link_storage.is_enabled = lambda chat_id: True

    async def main():
        task = asyncio.ensure_future(bio_link_service.watcher(bot))
        # shedding: 0.05 sleep * stretch(3.0) = 0.15s per deferred round;
        # normal: scan_interval 0.05s per round. Give it a few rounds.
        await asyncio.sleep(0.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        run(main())
    finally:
        bio_link_service.scan_group = orig_scan
        bio_link_service.active_groups = orig_groups
        bio_link_service.auto_scan_members = orig_auto
        bio_link_service.scan_interval = orig_interval
        bio_link_service.start_delay = orig_start
        bio_link_service.bio_link_storage.is_enabled = orig_enabled
    return calls["scan"]


scan_normal = run_watcher_round(shedding=False)
check("normal load: periodic member scan runs", scan_normal >= 1)

scan_shed = run_watcher_round(shedding=True)
check("heavy load: periodic member scan deferred", scan_shed == 0)


# ----------------------------------------------------------------------
print("### daily_admin_report watcher defers the round while shedding")

from modules import daily_admin_report  # noqa: E402


class _FakeNow:
    hour = 23


def run_report_round(*, shedding):
    bot = FakeBot(governor=overloaded_governor() if shedding else FakeGovernor())
    calls = {"send": 0}

    async def fake_check(bot_, now=None, force=False):
        calls["send"] += 1

    orig_check = daily_admin_report.check_and_send_all_groups
    orig_now = daily_admin_report.now_local
    daily_admin_report.check_and_send_all_groups = fake_check
    daily_admin_report.now_local = lambda: _FakeNow()

    async def main():
        task = asyncio.ensure_future(
            daily_admin_report.run_daily_admin_report_watcher(bot, interval=0.05)
        )
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        run(main())
    finally:
        daily_admin_report.check_and_send_all_groups = orig_check
        daily_admin_report.now_local = orig_now
    return calls["send"]


sent_normal = run_report_round(shedding=False)
check("normal load: daily report round runs", sent_normal >= 1)

sent_shed = run_report_round(shedding=True)
check("heavy load: daily report round deferred", sent_shed == 0)


# ----------------------------------------------------------------------
print("### ad-spam-guard punishment stays silent (announce=False)")

from modules.ad_spam_guard import detector  # noqa: E402


class FakeAdminActions:
    def __init__(self):
        self.calls = []

    def punish_user(self, chat_id, user_id, username=None, announce=True,
                    *, user=None, chat=None):
        self.calls.append({"announce": announce})
        return True


class FakeModQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, chat_id, kind, user_id=None, timeout_seconds=None,
                operation=None, on_success=None, on_failure=None):
        self.jobs.append(operation)


class FakeSender:
    username = "spammer"
    first_name = "Spam"
    id = 999


ad_bot = FakeBot()
ad_bot.admin_actions = FakeAdminActions()
ad_bot.moderation_queue = FakeModQueue()
ad_bot.tracker = None
ad_bot.config_manager = None

enqueued = detector._punish_at_threshold(ad_bot, -100, 999, FakeSender(), 3, 3)
check("punish enqueued at threshold", enqueued is True and len(ad_bot.moderation_queue.jobs) == 1)
if ad_bot.moderation_queue.jobs:
    ad_bot.moderation_queue.jobs[0]()
check("punish ran with announce=False (no public removal message)",
      ad_bot.admin_actions.calls == [{"announce": False}])


print()
if FAIL:
    print(f"LOAD SHEDDING TESTS FAILED: {FAIL} failed / {PASS} passed")
    sys.exit(1)
print(f"LOAD SHEDDING TESTS OK: {PASS} checks passed")
