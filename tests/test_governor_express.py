"""Violation lookups get their own lane; an extra moderation slot opens only
while the server is fast.  Base limits never shrink."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import rpc_governor as rg
from modules.rpc_governor import (
    P0_CRITICAL, P1_DELETE, P2_SEND, P3_HEAVY, RpcAdmission, RpcGovernor,
)

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


def adm(kind, chat="1"):
    table = {
        "ban": (P0_CRITICAL, "critical", "EditBannedRequest"),
        "delete": (P1_DELETE, "delete", "DeleteMessagesRequest"),
        "send": (P2_SEND, "send", "SendMessageRequest"),
        "guard": (P3_HEAVY, "heavy", "GetFullUserRequest"),
        "read": (P3_HEAVY, "heavy", "GetHistoryRequest"),
        "poll": (P3_HEAVY, "poll", "GetParticipantsRequest"),
    }
    priority, bucket, name = table[kind]
    return RpcAdmission(priority=priority, bucket=bucket,
                        request_name=name, chat_key=chat)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def queued(governor, kind, log, chat="1"):
    permit = await governor.acquire(adm(kind, chat))
    log.append(kind)
    return permit


def make_healthy(governor, ms=120.0):
    for _ in range(10):
        governor._note_latency(ms)


async def main():
    class Req:
        pass
    GetFullUserRequest = type("GetFullUserRequest", (Req,), {})
    full = rg.classify_request(GetFullUserRequest())
    check("violation lookup keeps bucket=heavy / P3 (timeouts unchanged)",
          full.bucket == "heavy" and full.priority == P3_HEAVY
          and rg.is_guard_request(full.request_name))

    # ---- 1. own lane: unrelated read in flight does not block the lookup
    g = RpcGovernor(total_limit=2)
    read = await g.acquire(adm("read"))
    log = []
    task = asyncio.create_task(queued(g, "guard", log))
    await settle()
    check("lookup runs beside an unrelated read", log == ["guard"], log)
    second_guard = asyncio.create_task(queued(g, "guard", log, chat="2"))
    second_read = asyncio.create_task(queued(g, "read", log, chat="3"))
    await settle()
    check("still one lookup at a time, one read at a time, total 2",
          log == ["guard"] and g.active == 2, (log, g.active))
    (await task).release()
    await settle()
    check("next lookup follows", log == ["guard", "guard"], log)
    read.release()
    await settle()
    check("next read follows", log == ["guard", "guard", "read"], log)
    (await second_guard).release(); (await second_read).release()
    check("everything released", g.active == 0 and g.waiting == 0)

    # ---- 2. order when a slot frees: delete, then lookup, then reply, then read
    g = RpcGovernor(total_limit=1)
    hold = await g.acquire(adm("send"))
    log = []
    tasks = [asyncio.create_task(queued(g, kind, log, chat=str(i)))
             for i, kind in enumerate(("read", "send", "guard", "delete", "ban"))]
    await settle()
    check("all wait behind the busy slot", log == [] and g.waiting == 5)
    hold.release()
    for index in range(5):
        await settle()
        permit = None
        for task in tasks:
            if task.done() and not getattr(task, "_freed", False):
                task._freed = True
                permit = task.result()
        if permit is not None:
            permit.release()
    await settle()
    check("order: ban, delete, violation lookup, reply, other read",
          log == ["ban", "delete", "guard", "send", "read"], log)

    # lookup does not wait for queued replies, but does wait for a queued delete
    g = RpcGovernor(total_limit=2)
    a = await g.acquire(adm("send"))
    b = await g.acquire(adm("read"))
    log = []
    t_send = asyncio.create_task(queued(g, "send", log, chat="5"))
    await settle()
    check("lookup not held back by a queued reply",
          g._eligible(P3_HEAVY, "heavy", "GetFullUserRequest") is False
          and g._urgent_waiting(include_p2=False) is False)
    b.release()
    await settle()
    check("with a free slot the lookup may start although a reply is queued",
          g._eligible(P3_HEAVY, "heavy", "GetFullUserRequest") is True)
    check("an ordinary read still yields to the queued reply",
          g._eligible(P3_HEAVY, "heavy", "GetHistoryRequest") is False)
    a.release(); (await t_send).release()

    # ---- 3. express lane closed by default / when slow
    base = RpcGovernor(total_limit=2)
    make_healthy(base)
    x = await base.acquire(adm("send")); y = await base.acquire(adm("read"))
    check("constructor default: no extra slot",
          base._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is False)
    x.release(); y.release()

    g = RpcGovernor(total_limit=2, adaptive_extra=1, adaptive_healthy_ms=500)
    x = await g.acquire(adm("send")); y = await g.acquire(adm("read"))
    check("no samples yet: base limits",
          g._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is False)
    make_healthy(g, 2000.0)
    check("slow server: base limits",
          g._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is False)
    check("slow server: ban also at base", g._eligible(P0_CRITICAL, "critical") is False)

    # ---- 4. express lane open when fast: moderation only
    for _ in range(40):
        g._note_latency(100.0)
    check("fast server: delete uses the extra slot",
          g._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is True)
    check("fast server: ban uses the extra slot",
          g._eligible(P0_CRITICAL, "critical", "EditBannedRequest") is True)
    check("fast server: violation lookup uses the extra slot",
          g._eligible(P3_HEAVY, "heavy", "GetFullUserRequest") is True)
    check("replies never use it", g._eligible(P2_SEND, "send", "SendMessageRequest") is False)
    check("notices never use it", g._eligible(P2_SEND, "notice", "SendMessageRequest") is False)
    check("member polling never uses it",
          g._eligible(P3_HEAVY, "poll", "GetParticipantsRequest") is False)
    z = await g.acquire(adm("delete"))
    check("three in flight, never more", g.active == 3
          and g._eligible(P0_CRITICAL, "critical", "EditBannedRequest") is False
          and g._eligible(P3_HEAVY, "heavy", "GetFullUserRequest") is False)
    check("class cap still holds (one delete)", g._active_by_bucket["delete"] == 1)
    z.release()

    # ---- 5. FloodWait closes it at once and keeps it closed
    g.note_flood_wait("delete", seconds=3)
    check("flood wait closes the extra slot",
          g._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is False)
    for _ in range(40):
        g._note_latency(80.0)
    check("stays closed during the cooldown even if fast again",
          g._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is False)
    g._express_block_until = 0.0
    check("opens again after the cooldown",
          g._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is True)

    # hysteresis and slowdown
    for _ in range(3):
        g._note_latency(900.0)
    check("a few slow answers do not flap it shut",
          g._latency_ema < 750 and g._express_open() is True, g._latency_ema)
    for _ in range(30):
        g._note_latency(1500.0)
    check("sustained slowness closes it", g._express_open() is False)
    x.release(); y.release()
    check("clean", g.active == 0)

    # ---- 6. real latency measurement through acquire/release
    g = RpcGovernor(total_limit=2, adaptive_extra=1, adaptive_healthy_ms=500)
    for _ in range(9):
        permit = await g.acquire(adm("send"))
        await asyncio.sleep(0.01)
        permit.release()
    check("latency measured from real holds",
          g._latency_samples == 9 and 5 < g._latency_ema < 200, g._latency_ema)

    # ---- 7. environment defaults and kill switch
    env = RpcGovernor.from_environment()
    check("runtime default: extra slot available", env.adaptive_extra == 1)
    os.environ["BOT_RPC_ADAPTIVE_EXTRA"] = "0"
    off = RpcGovernor.from_environment()
    os.environ.pop("BOT_RPC_ADAPTIVE_EXTRA")
    check("base limits untouched",
          env.total_limit == off.total_limit
          and env.class_limits == off.class_limits
          and env.noncritical_limit == off.noncritical_limit
          and env.critical_reserve == off.critical_reserve
          and env.class_limits["send"] == 1 and env.class_limits["heavy"] == 1)
    make_healthy(off)
    make_healthy(env)
    for governor in (off, env):
        governor._held = [
            await governor.acquire(adm(kind, chat=str(i)))
            for i, kind in enumerate(
                ["send", "read", "poll"][:governor.total_limit])
        ]
    check("BOT_RPC_ADAPTIVE_EXTRA=0 switches it off",
          off.adaptive_extra == 0 and off.active == off.total_limit
          and off._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is False)
    check("runtime default: delete passes a full road when the server is fast",
          env.active == env.total_limit
          and env._eligible(P1_DELETE, "delete", "DeleteMessagesRequest") is True)
    extra = await env.acquire(adm("delete", chat="9"))
    peak = env.total_limit + max(env.critical_reserve, env.adaptive_extra)
    check("peak in flight = total + max(reserve, extra), never stacked",
          env.active == peak == env.total_limit + 1
          and env._eligible(P0_CRITICAL, "critical", "EditBannedRequest") is False,
          (env.active, peak))
    extra.release()
    for governor in (off, env):
        for permit in governor._held:
            permit.release()
    check("all clean", env.active == 0 and off.active == 0)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
