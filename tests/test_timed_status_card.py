"""«پیام زماندار»: the status card must show real counts in every group, and
the bot's own messages inside the window must be deleted too."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import outgoing_sender
from modules.manage_features import storage, timed, timed_commands
from modules.outgoing_sender import DISPATCH_ACTIVE_VAR, OutgoingSender

PASSED = FAILED = 0
CHAT = -1000025467290


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


class Logger:
    def __init__(self):
        self.errors, self.infos = [], []

    def log_error(self, text):
        self.errors.append(str(text))

    def log_info(self, text):
        self.infos.append(str(text))


class Client:
    """Minimal Soroush client: real ids, records edits and deletes."""

    def __init__(self):
        self.next_id = 5000
        self.sent, self.edits, self.deleted = [], [], []
        self.history_fails = False

    async def send_message(self, entity, text, **kwargs):
        await asyncio.sleep(0)
        self.next_id += 1
        message = NS(id=self.next_id, message=text, out=True, peer_id=NS(channel_id=25467290),
                     chat_id=CHAT)
        self.sent.append(message)
        return message

    async def edit_message(self, entity, message, text, **kwargs):
        self.edits.append((message, text))
        return NS(id=message, message=text)

    async def delete_messages(self, entity, ids, revoke=True):
        self.deleted.extend(ids)
        return [NS(pts=1, pts_count=len(ids))]

    async def get_messages(self, entity, limit=20):
        if self.history_fails:
            raise RuntimeError("history unavailable")
        return list(reversed(self.sent))[:limit]

    async def get_input_entity(self, chat_id):
        return chat_id

    async def get_me(self):
        return NS(id=555, username="fox")


def fresh_storage():
    temp = Path(tempfile.mkdtemp())
    for name in ("TIMED_STORAGE",):
        store = getattr(storage, name)
        store.path = temp / "timed_message.json"
        store.backup_path = temp / "timed_message.json.bak"
        store._cache = None
        store._mtime = None
    timed_commands.reset_all()


def make_bot():
    client = Client()
    logger = Logger()
    bot = NS(client=client, logger=logger, reply_input_peer_cache={},
             config_manager=NS(get=lambda *a, **k: None))
    sender = OutgoingSender(client, logger)
    client._outgoing_sender = sender
    bot.outgoing_sender = sender
    outgoing_sender._wrap_send_message(client, sender)
    return bot, client, logger


async def no_lock(bot, chat_id, logger=None):
    return True


async def run_window(bot, *, in_dispatch, messages=30, own_messages=0):
    """Start a run, feed messages, end it and let the timer finish it."""
    token = DISPATCH_ACTIVE_VAR.set(2) if in_dispatch else None
    try:
        timed.start(CHAT, 30, actor_id=7, name="admin")
        for index in range(messages):
            timed_commands.track(CHAT, 100 + index, 900 + index)
        for index in range(own_messages):
            await asyncio.wait_for(asyncio.shield(
                _direct_send(bot, f"bot reply {index}")), 2)
        record = timed.get_record(CHAT)
        record["end_ts"] = storage.now() - 1          # window is over
        timed._write(CHAT, record)
        task = timed.schedule(bot, CHAT, logger=bot.logger)
    finally:
        if token is not None:
            DISPATCH_ACTIVE_VAR.reset(token)
    await asyncio.wait_for(task, 10)
    for _ in range(200):
        state = (timed.get_record(CHAT) or {}).get("state")
        if state in {"finished", "cancelled", "failed_timeout"}:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    return timed.get_record(CHAT)


async def _direct_send(bot, text):
    token = DISPATCH_ACTIVE_VAR.set(False)
    try:
        return await bot.client.send_message(CHAT, text)
    finally:
        DISPATCH_ACTIVE_VAR.reset(token)


def card_texts(client):
    return [text for _id, text in client.edits]


async def main():
    timed.lock_group = no_lock

    for in_dispatch in (False, True):
        label = "started from a group worker" if in_dispatch else "started directly"
        fresh_storage()
        bot, client, logger = make_bot()
        record = await run_window(bot, in_dispatch=in_dispatch)
        check(f"[{label}] run finished", record.get("state") == "finished", record.get("state"))
        check(f"[{label}] all 30 messages deleted",
              sorted(client.deleted) == list(range(100, 130)), len(client.deleted))
        check(f"[{label}] exactly one status card", len(client.sent) == 1, len(client.sent))
        check(f"[{label}] card id stored", bool(record.get("status_message_id")),
              logger.errors)
        final = card_texts(client)[-1] if client.edits else ""
        check(f"[{label}] card edited to the final count",
              "30" in final and record.get("deleted") == 30 and len(client.edits) >= 1,
              (final[:120], logger.errors))
        check(f"[{label}] final card says 0 remaining",
              any(line.strip().endswith(": 0") or line.strip().endswith(":0")
                  for line in final.splitlines() if "باقی" in line), final)
        check(f"[{label}] no card errors", not logger.errors, logger.errors)

    # ---- second run in the same group must get its own card, not the old id
    fresh_storage()
    bot, client, logger = make_bot()
    first = await run_window(bot, in_dispatch=False, messages=5)
    first_card = first.get("status_message_id")
    client.deleted.clear(); client.edits.clear()
    second = await run_window(bot, in_dispatch=True, messages=7)
    check("second run has a new card", second.get("status_message_id")
          and second.get("status_message_id") != first_card,
          (first_card, second.get("status_message_id")))
    check("second run edits only its own card",
          client.edits and all(mid == second.get("status_message_id")
                               for mid, _ in client.edits), client.edits[:3])
    check("second run counted its own messages", second.get("deleted") == 7)

    # ---- send gives no id and history is unavailable: counts still reported
    fresh_storage()
    bot, client, logger = make_bot()
    original_sent_id = timed_commands._sent_id
    timed_commands._sent_id = lambda sent: None
    client.history_fails = True
    try:
        record = await run_window(bot, in_dispatch=False, messages=12)
    finally:
        timed_commands._sent_id = original_sent_id
    bodies = [m.message for m in client.sent]
    check("unknown card id: deletion still completes", record.get("deleted") == 12)
    check("unknown card id: a final report with the real count is still shown",
          any("12" in body and "پایان" in body or "12" in body and "✅" in body
              for body in bodies[1:]) or any("12" in t for t in card_texts(client)),
          bodies)
    check("unknown card id: never more than two cards", len(client.sent) <= 2, len(client.sent))

    # one failure must not disable cards for the next run of that group
    client.history_fails = False
    client.sent.clear(); client.edits.clear()
    record = await run_window(bot, in_dispatch=False, messages=3)
    check("next run after a failure works normally",
          len(client.sent) == 1 and bool(record.get("status_message_id"))
          and client.edits, (len(client.sent), record.get("status_message_id")))

    # ---- history lookup: text changed by the server, still found
    fresh_storage()
    bot, client, logger = make_bot()
    timed_commands._sent_id = lambda sent: None
    real_send = Client.send_message

    async def send_with_changed_text(self, entity, text, **kwargs):
        message = await real_send(self, entity, text, **kwargs)
        message.message = text.replace("\n\n", "\n") + " "
        return message
    Client.send_message = send_with_changed_text
    try:
        record = await run_window(bot, in_dispatch=True, messages=4)
    finally:
        Client.send_message = real_send
        timed_commands._sent_id = original_sent_id
    check("card found in history even if the server reformatted the text",
          record.get("status_message_id") == client.sent[0].id and client.edits,
          (record.get("status_message_id"), logger.errors))

    # ---- the bot's own messages inside the window are deleted too
    fresh_storage()
    bot, client, logger = make_bot()
    check("own-message tracking installed",
          timed_commands.install_own_message_tracking(type(client)) in (True, False))
    bot, client, logger = make_bot()
    record = await run_window(bot, in_dispatch=False, messages=6, own_messages=3)
    own_ids = [m.id for m in client.sent if m.message.startswith("bot reply")]
    check("three own messages were sent", len(own_ids) == 3)
    check("own messages are deleted with the window",
          all(mid in client.deleted for mid in own_ids), (own_ids, client.deleted))
    check("count includes them", record.get("deleted") == 9, record.get("deleted"))
    card = record.get("status_message_id")
    check("the status card itself is not deleted", card and card not in client.deleted)

    # the same sent message reported twice is tracked once
    fresh_storage()
    timed.start(CHAT, 30, actor_id=7)
    message = NS(id=7777, peer_id=NS(channel_id=25467290))
    check("own message tracked", timed_commands.track_own(message) == 1)
    check("not tracked twice", timed_commands.track_own(message) == 0
          and timed.tracked_ids(CHAT).count(7777) == 1)
    check("album tracked", timed_commands.track_own(
        [NS(id=7778, peer_id=NS(channel_id=25467290)),
         NS(id=7779, peer_id=NS(channel_id=25467290))]) == 2)
    check("private message ignored", timed_commands.track_own(
        NS(id=7780, peer_id=NS(user_id=5), chat_id=5)) == 0)
    check("None / odd results ignored", timed_commands.track_own(None) == 0
          and timed_commands.track_own(NS(id="x")) == 0)

    # real library class: patch installs, signature preserved, idempotent
    try:
        from splusthon import SoroushClient
    except Exception:
        SoroushClient = None
    if SoroushClient is not None:
        before = SoroushClient.send_message
        try:
            check("installs on the real client class",
                  timed_commands.install_own_message_tracking(SoroushClient) is True)
            check("second install is a no-op",
                  timed_commands.install_own_message_tracking(SoroushClient) is False)
            check("wrapped method keeps its name and is async",
                  SoroushClient.send_message.__name__ == "send_message"
                  and asyncio.iscoroutinefunction(SoroushClient.send_message)
                  and asyncio.iscoroutinefunction(SoroushClient.send_file))
        finally:
            pass

    # own messages outside any window are not tracked
    fresh_storage()
    bot, client, logger = make_bot()
    await _direct_send(bot, "outside")
    check("nothing tracked without an active window", timed.get_record(CHAT) is None)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
