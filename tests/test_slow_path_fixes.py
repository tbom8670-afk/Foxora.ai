"""Regression for round-14 slow-path fixes (owner slow reports).

The owner's «گزارش کندی» named two slow stages: ``spam_check`` and ``receive``.
Root causes fixed here (bounded inline waits instead of unbounded ones):

1. bio-link first check waited up to **8s** inline in the message path
   (stage=spam_check, observed 0.8-2.4s) -> cap is now 0.45s; slower RPCs are
   cut and moved to the background (``BIO_LINK_AWAIT_TIMEOUT``).
2. profile-guard waited for ``GetFullUser`` inline with **no cap**
   (stage=receive, observed 0.6-1.2s) -> ``snapshot_waited`` caps the wait at
   0.45s (``PROFILE_GUARD_AWAIT_TIMEOUT``), falls back to a background
   refresh and never punishes on stale/missing data; watched users keep the
   uncapped fresh inline fetch.
3. owner slow reports now carry the profiler sub-stage ``notes``
   (bio_check / sender_fetch / ad_guard / profile_guard / force_join)
   so the exact slow sub-path is visible WITHOUT runtime logs.

Run: python3 tests/test_slow_path_fixes.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ------------------------------------------------------- 1) bio bounded wait
def test_bio_bounded_wait():
    from modules import bio_link_service as bls

    saved = {k: os.environ.get(k) for k in
             ("BIO_LINK_AWAIT_FIRST_CHECK", "BIO_LINK_AWAIT_TIMEOUT")}
    os.environ.pop("BIO_LINK_AWAIT_FIRST_CHECK", None)
    os.environ.pop("BIO_LINK_AWAIT_TIMEOUT", None)
    try:
        check("bio first-check await still ON (first message protected)",
              bls.await_first_check() is True)
        check("bio await cap reduced to <=0.5s (was 8s)",
              0.0 < bls.await_timeout() <= 0.5)
        os.environ["BIO_LINK_AWAIT_TIMEOUT"] = "3"
        check("bio await cap env override works", bls.await_timeout() == 3.0)
        os.environ["BIO_LINK_AWAIT_FIRST_CHECK"] = "0"
        check("bio await can still be disabled fully",
              bls.await_first_check() is False)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ------------------------------------------- 2) profile guard bounded wait
class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


async def _snapshot_waited_flow():
    from modules.profile_guard import service as pgs

    saved = os.environ.get("PROFILE_GUARD_AWAIT_TIMEOUT")
    os.environ.pop("PROFILE_GUARD_AWAIT_TIMEOUT", None)
    pgs._cache.clear()
    pgs._retry_after.clear()
    pgs._bg_inflight.clear()
    fetched = {"n": 0}

    async def fast_fetch(bot, user, fresh=False):
        fetched["n"] += 1
        return {"ts": time.time(), "user_id": user.id, "about": "bio"}

    async def slow_fetch(bot, user, fresh=False):
        fetched["n"] += 1
        await asyncio.sleep(0.5)
        return {"ts": time.time(), "user_id": user.id, "about": "bio"}

    original = pgs._fetch
    try:
        # fast RPC -> first message IS moderated (contract preserved)
        pgs._fetch = fast_fetch
        got = await pgs.snapshot_waited(None, FakeUser(111))
        check("fast RPC -> first message gets snapshot inline",
              isinstance(got, dict))
        check("fast RPC fetched once", fetched["n"] == 1)
        got2 = await pgs.snapshot_waited(None, FakeUser(111))
        check("fresh cache -> no second RPC",
              isinstance(got2, dict) and fetched["n"] == 1)

        # slow RPC -> capped wait, background refresh, no stale punishment
        pgs._cache.clear()
        fetched["n"] = 0
        pgs._fetch = slow_fetch
        os.environ["PROFILE_GUARD_AWAIT_TIMEOUT"] = "0.1"
        started = time.perf_counter()
        none = await pgs.snapshot_waited(None, FakeUser(222))
        waited = time.perf_counter() - started
        check("slow RPC -> returns None (no stale punishment)", none is None)
        check("slow RPC wait capped (~0.1s, not 0.5s)", waited < 0.35)
        check("background refresh scheduled after cutoff",
              "222" in pgs._bg_inflight)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if pgs._cache.get("222"):
                break
        check("background refresh filled cache", bool(pgs._cache.get("222")))
        check("inflight cleared", "222" not in pgs._bg_inflight)
        os.environ.pop("PROFILE_GUARD_AWAIT_TIMEOUT", None)

        # force (watched user) -> uncapped fresh inline fetch
        pgs._cache.clear()
        fetched["n"] = 0
        pgs._fetch = slow_fetch
        forced = await pgs.snapshot_waited(None, FakeUser(333), force=True)
        check("watched user -> uncapped inline fresh snapshot",
              isinstance(forced, dict) and fetched["n"] == 1)

        # retry_after window -> no inline wait at all
        pgs._cache.clear()
        pgs._retry_after["444"] = time.time() + 600
        fetched["n"] = 0
        pgs._fetch = slow_fetch
        blocked = await pgs.snapshot_waited(None, FakeUser(444))
        check("retry_after window -> None without inline RPC",
              blocked is None and fetched["n"] == 0)
    finally:
        pgs._fetch = original
        pgs._cache.clear()
        pgs._retry_after.clear()
        pgs._bg_inflight.clear()
        if saved is None:
            os.environ.pop("PROFILE_GUARD_AWAIT_TIMEOUT", None)
        else:
            os.environ["PROFILE_GUARD_AWAIT_TIMEOUT"] = saved


def test_profile_guard_wiring():
    import inspect
    from modules.profile_guard import guard as pg

    src = inspect.getsource(pg.guard_message)
    check("guard common path uses bounded snapshot_waited",
          "await service.snapshot_waited(bot, sender)" in src)
    check("watched users keep uncapped fresh snapshot",
          "await service.snapshot(bot, sender, force=True)" in src)


# ------------------------------------------------------- 3) notes in reports
def test_monitor_notes():
    from modules.performance_monitor import SlowProcessMonitor as M

    event = {
        "total_ms": 1234.5,
        "chat_id": -1001,
        "message_id": 55,
        "handler": "process_incoming_message:spam_check",
        "timestamp": "now",
        "count": 4,
        "max_ms": 2444.1,
        "notes": {"bio_check": 812.3, "banned_word": 0.6,
                  "sender_fetch": 940.1, "junk": "x"},
    }
    alert = M.format_alert(event)
    batch = M.format_batch([event])
    check("alert shows sub-stage notes", "زیرمرحله‌ها" in alert)
    check("alert notes sorted desc (sender_fetch first)",
          alert.index("sender_fetch") < alert.index("bio_check"))
    check("garbage note values dropped", "junk" not in alert)
    check("batch shows sub-stage notes", "زیرمرحله‌ها" in batch)
    no_notes = M.format_alert({**event, "notes": {}})
    check("no notes -> no extra line", "زیرمرحله‌ها" not in no_notes)

    async def record_flow():
        mon = M(client=None, logger=None, alert_threshold_ms=10 ** 6)
        ok = mon.record(
            total_ms=900.0, chat_id=-1001, message_id=55,
            handler="process_incoming_message:receive",
            notes={"sender_fetch": 700.2, "force_join": 120.0, "zero": 0},
            now_epoch=time.time(),
        )
        key = M._key("process_incoming_message:receive", -1001)
        agg = mon._batch.get(key)
        check("record stored cleaned top notes",
              agg is not None and agg.get("notes", {}).get("sender_fetch") == 700.2
              and "zero" not in agg.get("notes", {}))
        check("severe alert not queued below alert threshold", ok is False)
        mon._closed = True
        return None

    asyncio.run(record_flow())


def test_handler_wiring():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "handlers", "message_handler.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    for note in ("sender_fetch", "ad_guard", "profile_guard", "force_join"):
        check(f"message_handler notes {note}", f'"{note}"' in src)
    with open(os.path.join(root, "core", "bot_working_split_ok.py"),
              encoding="utf-8") as fh:
        core = fh.read()
    check("core passes profiler notes to monitor.record",
          'notes=(' in core and 'stage_result.get("notes")' in core)


def main():
    print("== round-14 slow-path fixes ==")
    test_bio_bounded_wait()
    asyncio.run(_snapshot_waited_flow())
    test_profile_guard_wiring()
    test_monitor_notes()
    test_handler_wiring()
    print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
