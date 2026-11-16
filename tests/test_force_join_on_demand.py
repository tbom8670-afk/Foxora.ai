#!/usr/bin/env python3
"""راندِ ۱۸ — اسکنِ عضویت «هنگامِ نیاز»، نه دائمی.

خواستهٔ مالک: اسکن وقتی شروع شود که عضوی پیام می‌دهد؛ تا اتمام (یا
عضویت در کانال) بدونِ توقف ادامه یابد؛ بعد از اتمام از اجرا دست بکشد و
همیشه روی صف‌ها نباشد. به‌علاوه دو ایدهٔ گزارشِ راندِ ۱۶:
(الف) تفکیکِ hit-rateِ کشِ اعضا به «خواندنِ واقعی» و «اسکن/تازه‌سازی»،
(ب) invalidateِ کشِ اعضا با رویدادهای عضویت/خروج.

اجرا: python3 tests/test_force_join_on_demand.py
"""
import asyncio
import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(label, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def test_watcher_idle_until_needed():
    """ناظرِ تأیید: صف خالی ⇒ هیچ پاسی اجرا نمی‌شود (خوابِ رویدادمحور)."""
    from modules import force_join_service as fjs

    saved_env = {k: os.environ.get(k) for k in
                 ("FORCE_JOIN_CONFIRM_JOIN", "FORCE_JOIN_CONFIRM_FIRST_DELAY",
                  "FORCE_JOIN_CONFIRM_TICK")}
    saved = {
        "pending": dict(fjs._awaiting_confirm),
        "wake": fjs._confirm_wake_event,
        "first_delay": fjs.confirm_first_delay,
        "tick": fjs.confirm_tick,
        "confirm_pending": fjs.confirm_pending,
    }
    calls = {"n": 0}

    async def fake_confirm_pending(bot, *, limit=0):
        calls["n"] += 1
        # شبیه‌سازیِ «تأیید شد»: همهٔ انتظارها پاک می‌شوند (پایانِ کار).
        fjs._awaiting_confirm.clear()
        return 0

    class Bot:
        pass

    async def run():
        fjs._awaiting_confirm.clear()
        fjs._confirm_wake_event = None
        fjs.confirm_first_delay = lambda: 0.05
        fjs.confirm_tick = lambda: 0.1
        fjs.confirm_pending = fake_confirm_pending
        task = asyncio.get_running_loop().create_task(fjs.confirm_watcher(Bot()))
        await asyncio.sleep(0.4)
        check("empty queue -> watcher runs ZERO passes", calls["n"] == 0)
        check("empty queue -> watcher still alive (sleeping)", not task.done())

        # ثبتِ انتظار (یعنی کاربر پیام داد و راهنما گرفت) ⇒ بیدارِ فوری
        fjs.await_join_confirmation({"group_id": 7, "channel_id": 99},
                                    -1007, 555, "کاربر")
        for _ in range(40):
            await asyncio.sleep(0.05)
            if calls["n"]:
                break
        check("entry registered -> pass runs within ~2s", calls["n"] >= 1)
        check("await_join_confirmation wakes the watcher",
              fjs._confirm_wake_event is not None
              and fjs._confirm_wake_event.is_set() or calls["n"] >= 1)

        # پاس، انتظارها را تمام کرد ⇒ ناظر دوباره کاملاً می‌خوابد
        await asyncio.sleep(0.3)
        n_after = calls["n"]
        await asyncio.sleep(0.6)
        check("after completion -> watcher stops polling (idle again)",
              calls["n"] == n_after)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run())
    finally:
        fjs._awaiting_confirm.clear()
        fjs._awaiting_confirm.update(saved["pending"])
        fjs._confirm_wake_event = saved["wake"]
        fjs.confirm_first_delay = saved["first_delay"]
        fjs.confirm_tick = saved["tick"]
        fjs.confirm_pending = saved["confirm_pending"]
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_first_delay_default():
    from modules import force_join_service as fjs
    saved = os.environ.get("FORCE_JOIN_CONFIRM_FIRST_DELAY")
    os.environ.pop("FORCE_JOIN_CONFIRM_FIRST_DELAY", None)
    try:
        check("first confirm attempt default = 1s (fast start)",
              abs(fjs.confirm_first_delay() - 1.0) < 1e-6)
    finally:
        if saved is None:
            os.environ.pop("FORCE_JOIN_CONFIRM_FIRST_DELAY", None)
        else:
            os.environ["FORCE_JOIN_CONFIRM_FIRST_DELAY"] = saved


def test_activity_gate_and_wake():
    """اسنپ‌شات فقط برای گروهِ فعال؛ اولین پیام ناظر را بیدار می‌کند."""
    from modules import force_join_service as fjs

    saved = {
        "activity": dict(fjs._group_activity),
        "wake": fjs._sync_wake_event,
    }
    try:
        fjs._group_activity.clear()
        fjs._sync_wake_event = None

        check("unknown group -> inactive", fjs.group_active(101) is False)
        fjs.touch_group_activity(101)
        check("after a message -> active", fjs.group_active(101) is True)
        check("first message wakes the snapshot watcher",
              fjs._sync_wake_event is not None
              and fjs._sync_wake_event.is_set())

        # گروهِ خاموش ⇒ فعال نیست (ناظرِ دوره‌ای ردش می‌کند)
        check("idle group -> group_active False", fjs.group_active(202) is False)

        # پنجرهٔ فعالیت از env خوانده می‌شود
        os.environ["FORCE_JOIN_SYNC_ACTIVE_WINDOW"] = "60"
        fjs._group_activity["303"] = time.time() - 120
        check("activity older than window -> inactive",
              fjs.group_active(303) is False)
        os.environ.pop("FORCE_JOIN_SYNC_ACTIVE_WINDOW", None)
        check("activity window default = 1h", fjs.sync_active_window() == 3600)
    finally:
        fjs._group_activity.clear()
        fjs._group_activity.update(saved["activity"])
        fjs._sync_wake_event = saved["wake"]


def test_watcher_source_gated():
    """ناظرِ دوره‌ایِ اسنپ‌شات: گروهِ خاموش رد + خوابِ رویدادمحور."""
    from modules import force_join_service as fjs
    src = inspect.getsource(fjs.watcher)
    check("periodic watcher skips inactive groups",
          "group_active(" in src)
    check("watcher sleep is wake-driven (not fixed polling)",
          "wake.wait()" in src and "_sync_wake()" in src)
    gate = inspect.getsource(fjs.gate_message)
    check("gate_message touches activity", "touch_group_activity(" in gate)
    check("message path spawns NO scan task (policies intact)",
          "_maybe_trigger_sync" not in gate and "create_task" not in gate)


def test_participant_cache_kind_stats():
    """تفکیکِ آمارِ کشِ اعضا به ازای kind + invalidateِ کامل."""
    from modules import participant_cache as pc

    pc.clear()
    fetches = {"members": 0, "page": 0}

    async def fetch_members():
        fetches["members"] += 1
        return [{"id": 1}, {"id": 2}]

    async def fetch_page():
        fetches["page"] += 1
        return [{"id": 9}]

    async def run():
        got = await pc.get_or_fetch(-555, "members", fetch_members,
                                    min_interval=0)
        check("members kind fetched once", got is not None
              and fetches["members"] == 1)
        again = await pc.get_or_fetch(-555, "members", fetch_members,
                                      min_interval=0)
        check("members kind second read = cache hit",
              again is not None and fetches["members"] == 1)
        page = await pc.get_or_fetch(24999, "participants_page", fetch_page,
                                     extra="ChannelParticipantsSearch:0:100",
                                     min_interval=0)
        check("page kind tracked separately", page is not None
              and fetches["page"] == 1)

        by_kind = pc.STATS_BY_KIND
        check("kind stats: members calls=1 hits=1",
              by_kind.get("members", {}).get("calls") == 1
              and by_kind.get("members", {}).get("hits") == 1)
        check("kind stats: participants_page calls=1 hits=0",
              by_kind.get("participants_page", {}).get("calls") == 1
              and by_kind.get("participants_page", {}).get("hits") == 0)
        check("global stats still consistent",
              pc.STATS["calls"] == 2 and pc.STATS["hits"] == 1)

        # invalidate با kind=None ⇒ همهٔ kindهای همان شناسه پاک می‌شوند
        pc.invalidate(-555)
        check("invalidate clears members kind too",
              pc.get_cached(-555, "members") is None)
        pc.invalidate(24999)
        check("invalidate clears page kind too",
              pc.get_cached(24999, "participants_page",
                            "ChannelParticipantsSearch:0:100") is None)

    try:
        asyncio.run(run())
    finally:
        pc.clear()


def test_perf_report_split_and_stamp():
    """گزارشِ تشخیص: خطِ تفکیکِ کش + مهرِ راندِ ۱۷ (شاملِ راندِ ۱۷ و ۱۸)."""
    from modules import participant_cache as pc
    from modules import perf_diagnostic as pd

    saved = dict(pc.STATS_BY_KIND)
    try:
        pc.STATS_BY_KIND.clear()
        pc.STATS_BY_KIND.update({
            "members": {"calls": 10, "hits": 8, "coalesced": 0,
                        "throttled": 0, "errors": 0},
            "participants_page": {"calls": 400, "hits": 20, "coalesced": 0,
                                  "throttled": 0, "errors": 0},
        })
        lines = pd._cache_section(None)
        text = "\n".join(lines)
        check("report splits real reads vs scan/refresh",
              ("خواندنِ واقعی" in text) and ("اسکن/تازه‌سازی" in text))
        check("split shows per-bucket hit-rates",
              "calls=10 hits=8" in text and "calls=400 hits=20" in text)
        check("BUILD_ROUND bumped for this deploy", pd.BUILD_ROUND == 17)
        src = inspect.getsource(pd.build_slow_path_report)
        check("stamp header still rendered", "راندِ استقرار" in src)
    finally:
        pc.STATS_BY_KIND.clear()
        pc.STATS_BY_KIND.update(saved)


def test_core_wiring_sources():
    """هندلرهای رویدادِ عضویت، کش را invalidate می‌کنند (بررسیِ منبع)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "core", "bot_working_split_ok.py"),
               encoding="utf-8").read()
    check("ChatAction join/leave invalidates participant cache",
          "participant_cache.invalidate(chat_id)" in src)
    check("UpdateChannelParticipant invalidates channel pages",
          "participant_cache.invalidate(_channel_id)" in src)


def main():
    print("== راندِ ۱۸: اسکنِ عضویتِ هنگامِ نیاز ==")
    test_first_delay_default()
    test_watcher_idle_until_needed()
    test_activity_gate_and_wake()
    test_watcher_source_gated()
    test_participant_cache_kind_stats()
    test_perf_report_split_and_stamp()
    test_core_wiring_sources()
    print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
