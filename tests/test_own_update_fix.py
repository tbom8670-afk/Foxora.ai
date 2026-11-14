"""Own channel deletes must not trigger GetDifference/GetChannelDifference."""
import asyncio
import datetime
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from splusthon._updates.messagebox import ENTRY_ACCOUNT, GapError, MessageBox
from splusthon.extensions import BinaryReader
from splusthon.network.mtprotosender import MTProtoSender
from splusthon.tl import functions, types

from modules import own_update_fix

EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
CH = 18494970
PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


class FakeSender:
    """Only what ``_store_own_updates`` touches."""
    def __init__(self):
        self._updates_queue = asyncio.Queue()


def store(sender, obj):
    MTProtoSender._store_own_updates(sender, obj)


def delete_result(pts, count, channel=True):
    """Go through the real ``read_result`` of the real request class."""
    if channel:
        request = functions.channels.DeleteMessagesRequest(
            types.InputChannel(CH, 123), [1, 2])
    else:
        request = functions.messages.DeleteMessagesRequest([1, 2], revoke=True)
    data = bytes(types.messages.AffectedMessages(pts, count))
    with BinaryReader(data) as reader:
        return request.read_result(reader)


def incoming(pts):
    return types.UpdateShort(
        types.UpdateDeleteChannelMessages(CH, [7], pts, 1),
        datetime.datetime.now(datetime.timezone.utc))


async def scenario(account_pts, fixed, echo=False):
    """Five rounds of: a message arrives in the group, the bot deletes it."""
    box = MessageBox(logging.getLogger("test"))
    box.set_state(types.updates.State(
        pts=account_pts, qts=0, date=EPOCH, seq=1, unread_count=0))
    box.try_set_channel_state(CH, 5000)
    sender = FakeSender()
    server = 5000
    calls = []
    for _ in range(5):
        server += 1
        box.process_updates(incoming(server), {}, [])
        server += 1
        if fixed:
            store(sender, delete_result(server, 1))
        else:
            # what the unpatched library queues
            upd = types.UpdateShort(
                types.UpdateDeleteMessages([], server, 1), EPOCH)
            upd._self_outgoing = True
            sender._updates_queue.put_nowait(upd)
        dispatched = []
        while not sender._updates_queue.empty():
            try:
                box.process_updates(
                    sender._updates_queue.get_nowait(), {}, dispatched)
            except GapError:
                pass
        check("own update not dispatched", dispatched == [], dispatched) \
            if fixed else None
        if echo:
            # server also sends the same delete on the update stream
            box.process_updates(types.UpdateShort(
                types.UpdateDeleteChannelMessages(CH, [7], server, 1),
                datetime.datetime.now(datetime.timezone.utc)), {}, [])
        await asyncio.sleep(0.12)      # > POSSIBLE_GAP_TIMEOUT
        box.check_deadlines()
        for entry in list(box.getting_diff_for):
            calls.append("account" if entry is ENTRY_ACCOUNT else "channel")
            box.end_get_diff(entry)
            if entry is not ENTRY_ACCOUNT:
                box.map[entry].pts = server
    return calls, box.map[ENTRY_ACCOUNT].pts, box.map[CH].pts, server


async def main():
    # Reproduce the bug first, without the patch.
    for account_pts in (300, 9000):
        calls, _a, _c, _s = await scenario(account_pts, fixed=False)
        check(f"library behaviour needs difference (account_pts={account_pts})",
              len(calls) >= 2, calls)

    original_store = MTProtoSender._store_own_updates
    check("install", own_update_fix.install() is True)
    check("install twice", own_update_fix.install() is True)
    check("store patched once",
          MTProtoSender._store_own_updates is not original_store)

    for account_pts in (300, 9000):
        for echo in (False, True):
            calls, acct, chan, server = await scenario(
                account_pts, fixed=True, echo=echo)
            tag = f"account_pts={account_pts} echo={echo}"
            check(f"no difference calls ({tag})", calls == [], calls)
            check(f"account pts untouched ({tag})", acct == account_pts, acct)
            check(f"channel pts follows server ({tag})", chan == server,
                  (chan, server))

    # Non-channel delete keeps the library's own handling.
    sender = FakeSender()
    result = delete_result(777, 2, channel=False)
    check("basic chat result not tagged",
          getattr(result, "_fx_channel_id", None) is None)
    store(sender, result)
    upd = sender._updates_queue.get_nowait()
    check("basic chat uses account update",
          isinstance(upd.update, types.UpdateDeleteMessages)
          and upd.update.pts == 777 and upd.update.pts_count == 2)

    # Channel delete: right update type, right channel, flagged own.
    sender = FakeSender()
    store(sender, delete_result(5100, 2))
    upd = sender._updates_queue.get_nowait()
    check("channel delete update type",
          isinstance(upd.update, types.UpdateDeleteChannelMessages))
    check("channel delete fields",
          upd.update.channel_id == CH and upd.update.pts == 5100
          and upd.update.pts_count == 2 and upd.update.messages == [])
    check("flagged self outgoing", upd._self_outgoing is True)
    check("exactly one update queued", sender._updates_queue.empty())

    # Other results still go through the original code.
    sender = FakeSender()
    short = types.UpdateShort(types.UpdateDeleteMessages([], 5, 1), EPOCH)
    store(sender, short)
    check("ordinary updates pass through",
          sender._updates_queue.get_nowait() is short
          and short._self_outgoing is True)
    store(sender, types.BoolTrue() if hasattr(types, "BoolTrue") else True)
    check("non-update results ignored", sender._updates_queue.empty())

    # Kill switch.
    os.environ["OWN_DELETE_PTS_FIX"] = "0"
    sender = FakeSender()
    store(sender, delete_result(5200, 1))
    upd = sender._updates_queue.get_nowait()
    check("kill switch restores library behaviour",
          isinstance(upd.update, types.UpdateDeleteMessages))
    os.environ.pop("OWN_DELETE_PTS_FIX")

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
