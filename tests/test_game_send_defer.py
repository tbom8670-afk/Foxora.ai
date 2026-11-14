"""Under pressure, game sends step aside for moderation/admin RPCs."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import group_dispatch, outgoing_sender
from modules.outgoing_sender import (
    GAME_CONTEXT_VAR, OutgoingSender, _defer_game_send, _with_game_context,
)
from modules.rpc_governor import P1_DELETE, RpcAdmission, RpcGovernor

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


class Event:
    def __init__(self, chat_id=1, sender_id=2):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_private = False


def delete_admission():
    return RpcAdmission(priority=P1_DELETE, bucket="delete",
                        request_name="DeleteMessagesRequest", chat_key="1")


async def main():
    # ---- which messages count as games
    is_game = group_dispatch.is_game_message
    for word in ("معما", "حدس پرچم", "شرکت", "جرئت", "خون‌آشام", "جک"):
        check(f"game word {word}", is_game(word, Event()) is True)
    for word in ("بن", "سکوت", "پاک 10", "راهنما", "پاکسازی کامل", "گزارش",
                 "پیام زماندار", "آمارم", "ثبت ادمین", "", "سلام خوبی"):
        check(f"not a game: {word!r}", is_game(word, Event()) is False)
    for word in group_dispatch._GAME_EXACT:
        priority, kind = group_dispatch.classify_priority(word, Event())
        if kind == group_dispatch.LANE_ADMIN:
            check(f"game list has no admin word {word}", False)
    check("game list never overlaps admin words", True)

    saved = group_dispatch._is_active_game_answer
    group_dispatch._is_active_game_answer = lambda event: True
    try:
        check("answer inside running game", is_game("تهران", Event()) is True)
        check("admin word inside running game", is_game("بن", Event()) is False)
        check("help inside running game", is_game("راهنما", Event()) is False)
    finally:
        group_dispatch._is_active_game_answer = saved

    # ---- defer helper against the real governor
    governor = RpcGovernor(total_limit=1, enabled=True)
    check("idle governor: no wait", await _defer_game_send(governor) == 0.0)
    check("no governor: no wait", await _defer_game_send(None) == 0.0)

    order = []

    async def queued_delete():
        permit = await governor.acquire(delete_admission())
        order.append(("delete", 0))
        return permit

    first = await governor.acquire(delete_admission())       # slot busy
    waiter = asyncio.create_task(queued_delete())
    await asyncio.sleep(0.01)
    check("a delete is queued", governor._urgent_waiting() is True)

    async def game_send():
        waited = await _defer_game_send(governor)
        order.append(("game", waited, time.perf_counter()))

    game_task = asyncio.create_task(game_send())
    await asyncio.sleep(0.23)
    check("game send holds back while delete waits", not game_task.done())
    released_at = time.perf_counter()
    first.release()
    second = await waiter
    second.release()
    await asyncio.wait_for(game_task, 2)
    game_row = next(row for row in order if row[0] == "game")
    check("delete was admitted", any(row[0] == "delete" for row in order), order)
    check("game continued only after the slot was handed to the delete",
          game_row[2] >= released_at, order)
    check("game waited, was not dropped", game_row[1] >= 150, order)

    # bounded: never longer than the limit
    os.environ["BOT_GAME_SEND_MAX_DEFER_SECONDS"] = "0.3"
    first = await governor.acquire(delete_admission())
    waiter = asyncio.create_task(governor.acquire(delete_admission()))
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    waited = await _defer_game_send(governor)
    elapsed = time.perf_counter() - started
    check("defer is bounded", 0.25 <= elapsed <= 0.6, elapsed)
    os.environ["BOT_GAME_SEND_MAX_DEFER_SECONDS"] = "0"
    check("0 switches it off", await _defer_game_send(governor) == 0.0)
    os.environ.pop("BOT_GAME_SEND_MAX_DEFER_SECONDS")
    first.release()
    (await waiter).release()

    disabled = RpcGovernor(total_limit=1, enabled=False)
    check("disabled governor: no wait", await _defer_game_send(disabled) == 0.0)

    # ---- queue workers carry the flag of the task that enqueued
    seen = []

    async def job():
        seen.append(GAME_CONTEXT_VAR.get())
        return "sent"

    token = GAME_CONTEXT_VAR.set(True)
    try:
        wrapped_game = _with_game_context(job, GAME_CONTEXT_VAR.get())
    finally:
        GAME_CONTEXT_VAR.reset(token)
    wrapped_plain = _with_game_context(job, False)
    check("result passes through", await wrapped_game() == "sent")
    token = GAME_CONTEXT_VAR.set(True)      # worker born in a game context
    try:
        await wrapped_plain()
        check("flag restored after job", GAME_CONTEXT_VAR.get() is True)
    finally:
        GAME_CONTEXT_VAR.reset(token)
    check("game job sees flag, plain job does not", seen == [True, False], seen)
    check("sync factory result passes through",
          await _with_game_context(lambda: 5, False)() == 5)

    # ---- real OutgoingSender: admin notice enqueued from a game task is not a game
    sender = OutgoingSender(client=None, logger=None)
    seen.clear()
    done = asyncio.Event()

    async def marked(label):
        seen.append((label, GAME_CONTEXT_VAR.get()))
        if len(seen) == 3:
            done.set()
        return None

    token = GAME_CONTEXT_VAR.set(True)
    try:
        check("enqueue game reply", sender.enqueue(5, lambda: marked("game"), priority=2))
        check("enqueue notice", sender.enqueue_notice(5, lambda: marked("notice"), priority=1))
    finally:
        GAME_CONTEXT_VAR.reset(token)
    check("enqueue plain reply", sender.enqueue(5, lambda: marked("plain"), priority=1))
    await asyncio.wait_for(done.wait(), 5)
    flags = dict(seen)
    check("queued game reply flagged", flags.get("game") is True, seen)
    check("notice never flagged", flags.get("notice") is False, seen)
    check("plain reply not flagged", flags.get("plain") is False, seen)
    try:
        await sender.close()
    except Exception:
        pass

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
