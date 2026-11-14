"""Restart backlog is fed gradually; nothing dropped, order kept."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.performance_monitor import SlowProcessMonitor
from modules.startup_ramp import StartupRamp

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


async def main():
    # ---- schedule (fake clock): a burst of 50 messages at the same instant
    clock = Clock()
    ramp = StartupRamp(seconds=60, interval_ms=200, clock=clock)
    check("active before first message", ramp.active)
    delays = [ramp._delay() for _ in range(50)]
    check("first message does not wait", delays[0] == 0.0)
    check("order kept (delays never decrease)",
          all(b >= a for a, b in zip(delays, delays[1:])), delays[:6])
    gaps = [b - a for a, b in zip(delays, delays[1:])]
    check("starts at the configured gap", abs(gaps[0] - 0.2) < 1e-6, gaps[0])
    check("gap shrinks over time", gaps[-1] < gaps[0], (gaps[0], gaps[-1]))
    check("50 messages spread over several seconds", 5.0 < delays[-1] < 15.0,
          delays[-1])

    # huge backlog: nobody waits past the end of the window
    clock = Clock()
    ramp = StartupRamp(seconds=10, interval_ms=500, clock=clock)
    delays = [ramp._delay() for _ in range(5000)]
    check("wait never exceeds the window", max(delays) <= 10.0 + 1e-6, max(delays))
    clock.now += 10.0
    check("window over -> inactive", not ramp.active)
    check("after the window: no wait", ramp._delay() == 0.0)
    check("pace after the window returns at once", await ramp.pace() == 0.0)

    # quiet period: messages arriving later than their slot do not wait
    clock = Clock()
    ramp = StartupRamp(seconds=60, interval_ms=200, clock=clock)
    ramp._delay()
    clock.now += 5.0
    check("a lone later message does not wait", ramp._delay() == 0.0)
    check("remaining counts down", 54.0 < ramp.remaining() <= 55.0, ramp.remaining())

    # switched off
    for kwargs in ({"seconds": 0}, {"seconds": 60, "interval_ms": 0}):
        off = StartupRamp(**kwargs)
        check(f"off with {kwargs}", not off.active and await off.pace() == 0.0)
    os.environ["BOT_STARTUP_WARMUP_SECONDS"] = "0"
    check("env switch off", not StartupRamp().active)
    os.environ["BOT_STARTUP_WARMUP_SECONDS"] = "abc"
    check("bad env falls back to default", StartupRamp().seconds == 60.0)
    os.environ.pop("BOT_STARTUP_WARMUP_SECONDS")
    check("defaults", StartupRamp().seconds == 60.0 and abs(StartupRamp().interval - 0.2) < 1e-9)

    # ---- only old messages are backlog
    import datetime
    from types import SimpleNamespace as NS
    now = datetime.datetime(2026, 11, 15, 2, 0, 0, tzinfo=datetime.timezone.utc)
    ramp = StartupRamp(seconds=60, interval_ms=200)

    def ev(seconds_ago, naive=False):
        date = now - datetime.timedelta(seconds=seconds_ago)
        if naive:
            date = date.replace(tzinfo=None)
        return NS(message=NS(date=date))

    check("message from 5 minutes ago is backlog", ramp.is_backlog(ev(300), now))
    check("naive datetime handled", ramp.is_backlog(ev(300, naive=True), now))
    check("live message is not backlog", not ramp.is_backlog(ev(1), now))
    check("clock slightly ahead is not backlog", not ramp.is_backlog(ev(-3), now))
    check("no date -> not backlog", not ramp.is_backlog(NS(message=NS()), now))
    check("no message -> not backlog", not ramp.is_backlog(NS(), now))
    check("broken date -> not backlog",
          not ramp.is_backlog(NS(message=NS(date="x")), now))

    # ---- real time: 8 concurrent messages come out one after another
    ramp = StartupRamp(seconds=5, interval_ms=60)
    out = []

    async def message(index):
        await ramp.pace()
        out.append((index, time.perf_counter()))

    started = time.perf_counter()
    await asyncio.gather(*(message(i) for i in range(8)))
    check("all delivered", len(out) == 8)
    check("delivered in arrival order", [i for i, _ in out] == list(range(8)), out)
    spread = out[-1][1] - started
    check("spread out instead of one burst", 0.3 <= spread <= 1.5, spread)
    check("stats counted", ramp.stats["paced"] == 7 and ramp.stats["max_wait_ms"] > 250,
          ramp.stats)

    # ---- through the real message handler of the bot
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import modules.group_storage as group_storage
    from test_economy_routing import CHAT, Event, build_handler
    group_storage.activate_group(CHAT, "گروه تست")
    bot, handler = await build_handler()
    check("real handler built", handler is not None)
    if handler is not None:
        utc_now = datetime.datetime.now(datetime.timezone.utc)

        async def timed(event):
            begun = time.perf_counter()
            await handler(event)
            return time.perf_counter() - begun

        live = Event("سلام دوستان عزیز", 901)
        live.message.date = utc_now
        check("live message is not held", await timed(live) < 0.15)
        undated = Event("سلام بچه ها", 902)
        check("message without date is not held", await timed(undated) < 0.15)

        old = []
        for index in range(4):
            event = Event(f"پیام قدیمی شماره {index}", 910 + index)
            event.message.date = utc_now - datetime.timedelta(minutes=5)
            old.append(event)
        times = await asyncio.gather(*(timed(event) for event in old))
        check("first old message goes at once", times[0] < 0.15, times)
        check("later old messages are spaced out",
              times[1] >= 0.15 and times[3] >= 0.5 and times[3] > times[2] > times[1],
              times)

        command = Event("موجودی", 920)
        command.message.date = utc_now - datetime.timedelta(minutes=5)
        check("old command is not held", await timed(command) < 0.15)
        admin = Event("راهنما", 921)
        admin.message.date = utc_now - datetime.timedelta(minutes=5)
        check("old help command is not held", await timed(admin) < 0.15)
        private = Event("سلام", 922)
        private.is_private = True
        private.message.date = utc_now - datetime.timedelta(minutes=5)
        check("old private message is not held", await timed(private) < 0.15)

    # ---- owner report is silent during warm-up, back afterwards
    monitor = SlowProcessMonitor(client=None, logger=None)
    monitor.quiet_for(0.2)
    quiet = monitor.record(total_ms=2316.2, chat_id=-1, message_id=1,
                           handler="process_incoming_message:receive")
    check("no severe report during warm-up", quiet is False and not monitor._batch)
    await asyncio.sleep(0.25)
    loud = monitor.record(total_ms=2316.2, chat_id=-1, message_id=2,
                          handler="process_incoming_message:receive")
    check("reports work again after warm-up", loud is True or bool(monitor._batch),
          (loud, monitor._batch))
    try:
        await monitor.close()
    except Exception:
        pass

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
