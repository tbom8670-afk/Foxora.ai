"""Regression round-15: EVERY bot reply must be registered for cleanup.

Incident: user wrote «سلام», bot replied (raw ``await event.reply(...)``),
user edited the message into a bio-link violation; the bio-check deleted the
user message but the bot's reply bubble stayed in the group. Root cause:
registration only happened inside ``_schedule_reply``; raw ``event.reply``
call sites (INSULT_REPLY, simple replies, games, …) were invisible to
``bot_reply_registry``, so ``_cleanup_bot_reply_for_edited_message`` popped
nothing.

Fix: ``_wrap_event_reply_for_registry(event)`` wraps ``event.reply`` once at
the handler entry so replies from ANY code path are registered.

Run: python3 tests/test_reply_registry_wrap.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers.message_handler as mh  # noqa: E402
from modules import bot_reply_registry as reg  # noqa: E402

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


class FakeMessage:
    def __init__(self, mid):
        self.id = mid


class FakeSent:
    def __init__(self, mid):
        self.id = mid


class FakeEvent:
    def __init__(self, chat_id, msg_id, is_private=False):
        self.chat_id = chat_id
        self.message = FakeMessage(msg_id) if msg_id is not None else None
        self.is_private = is_private
        self._next = [900]

    async def reply(self, *args, **kwargs):
        self._next[0] += 1
        return FakeSent(self._next[0])


class FakeLogger:
    def log_info(self, *a, **k):
        pass

    def log_error(self, *a, **k):
        pass


class FakeConfig:
    def get(self, key, default=None):
        return True if key == "delete_spam" else default


class FakeBot:
    logger = FakeLogger()
    config_manager = FakeConfig()


async def _flow():
    reg.clear()
    queued = []
    original_queue = mh._queue_message_deletes
    mh._queue_message_deletes = lambda bot, chat_id, ids, *a, **k: queued.extend(ids)
    try:
        # 1) raw event.reply gets registered after the wrap
        ev = FakeEvent(-100123, 500)
        mh._wrap_event_reply_for_registry(ev)
        check("event.reply wrapped", getattr(ev, "_bot_reply_wrapped", False) is True)
        sent = await ev.reply("adab az keh…")
        ids = reg.peek_reply_ids(-100123, 500)
        check("raw reply registered", ids == [sent.id])

        # 2) second raw reply appends; same id never duplicated
        sent2 = await ev.reply("second")
        ids = reg.peek_reply_ids(-100123, 500)
        check("two replies registered", ids == [sent.id, sent2.id])
        reg.register(-100123, 500, sent2.id)
        check("register idempotent", reg.peek_reply_ids(-100123, 500) == ids)

        # 3) edited-message cleanup pops and queues the bot replies
        mh._cleanup_bot_reply_for_edited_message(FakeBot(), ev, -100123, True)
        check("cleanup queued both bot replies",
              sorted(queued) == sorted([sent.id, sent2.id]))
        check("registry entry consumed", reg.peek_reply_ids(-100123, 500) == [])

        # 4) non-edited message -> cleanup does nothing
        ev2 = FakeEvent(-100123, 600)
        mh._wrap_event_reply_for_registry(ev2)
        s3 = await ev2.reply("hi")
        queued.clear()
        mh._cleanup_bot_reply_for_edited_message(FakeBot(), ev2, -100123, False)
        check("non-edited -> no reply cleanup", queued == [])
        check("non-edited -> registry kept", reg.peek_reply_ids(-100123, 600) == [s3.id])

        # 5) private chats are not wrapped (registry stays empty)
        ev3 = FakeEvent(777, 700, is_private=True)
        mh._wrap_event_reply_for_registry(ev3)
        check("private not wrapped", getattr(ev3, "_bot_reply_wrapped", False) is False)
        await ev3.reply("private hello")
        check("private reply not registered", reg.peek_reply_ids(777, 700) == [])

        # 6) event without message id -> no wrap, no crash
        ev4 = FakeEvent(-100123, None)
        mh._wrap_event_reply_for_registry(ev4)
        check("no message id -> not wrapped",
              getattr(ev4, "_bot_reply_wrapped", False) is False)

        # 7) double wrap keeps a single layer (no exponential nesting)
        ev5 = FakeEvent(-100123, 800)
        mh._wrap_event_reply_for_registry(ev5)
        first = ev5.reply
        mh._wrap_event_reply_for_registry(ev5)
        check("double wrap = single layer", ev5.reply is first)

        # 8) handler entry wraps before anything can reply
        import inspect
        src = inspect.getsource(mh.handle_new_message)
        head = "\n".join(src.splitlines()[:9])
        check("handle_new_message wraps at entry",
              "_wrap_event_reply_for_registry(event)" in head)
    finally:
        mh._queue_message_deletes = original_queue
        reg.clear()


def main():
    print("== round-15 reply-registry wrap ==")
    asyncio.run(_flow())
    print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
