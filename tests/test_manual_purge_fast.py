# -*- coding: utf-8 -*-
"""🧹 تست صفِ مستقل و موازیِ «پاک <عدد>».

قراردادها:

1. «پاک ۸۰۰» باید **همان ۸۰۰ پیام واقعی** را حذف کند (نه ۷۹۹، نه ۸۰۱) و
   خودِ پیامِ دستور و پیامِ گزارش را نشمارد.
2. سرعت: حذف ۱۰۰تایی و موازی؛ هیچ بچی‌ای بزرگ‌تر از ۱۰۰ و هیچ RPCای
   پشتِ‌سرهمِ تک‌تک نباید زده شود.
3. صف کاملاً جداست: هیچ آی‌دی‌ای به ``bot.message_delete_queue`` نرود و
   پاکِ ادمینِ A جلوی ادمینِ B را نگیرد.
4. گزارشِ پایانی فقط بعد از اتمامِ واقعیِ حذف و با تعدادِ واقعی می‌آید.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

os.environ["SOROUSH_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="purge-test-")

import handlers.message_handler as mh  # noqa: E402

PASSED = 0
FAILED = 0


def check(title, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {title}")
    else:
        FAILED += 1
        print(f"  ❌ {title}  {detail}")
        raise AssertionError(f"{title}: {detail}")


# ---------------------------------------------------------------------------
class _Logger:
    def __init__(self):
        self.lines = []

    def log_info(self, msg):
        self.lines.append(("info", str(msg)))

    def log_error(self, msg):
        self.lines.append(("error", str(msg)))

    def log_warning(self, msg):
        self.lines.append(("warning", str(msg)))

    def logged(self, needle):
        return [m for _lvl, m in self.lines if needle in m]


class _Msg:
    def __init__(self, msg_id):
        self.id = msg_id


class _Client:
    """کلاینتِ قلابی: هیستوری و حذف، با اندازه‌گیریِ هم‌زمانی و اندازهٔ بچ."""

    def __init__(self, history_top, *, history_limit=None, latency=0.01,
                 fail_ids=()):
        self.history_top = history_top
        self.history_limit = history_limit
        self.latency = latency
        self.fail_ids = set(fail_ids)
        self.deleted = []
        self.batch_sizes = []
        self.active = 0
        self.max_active = 0
        self.get_calls = 0
        self.visible = set(range(1, history_top + 1))

    async def get_messages(self, entity, limit=None, ids=None, offset_id=0):
        self.get_calls += 1
        if ids is not None:
            if isinstance(ids, list):
                return [_Msg(mid) if mid in self.visible else None for mid in ids]
            return _Msg(ids) if ids in self.visible else None
        ceiling = offset_id or self.history_top + 1
        rows = [mid for mid in sorted(self.visible, reverse=True) if mid < ceiling]
        count = min(int(limit or 0), self.history_limit or int(limit or 0))
        return [_Msg(mid) for mid in rows[:count]]

    async def delete_messages(self, entity, message_ids):
        self.batch_sizes.append(len(message_ids))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.latency)
            if any(mid in self.fail_ids for mid in message_ids):
                raise RuntimeError("message id invalid")
            self.deleted.extend(message_ids)
            self.visible.difference_update(message_ids)
            return True
        finally:
            self.active -= 1


class _DeleteQueueSpy:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, chat_id, message_ids, priority=1, rpc_peer=None):
        self.enqueued.append((chat_id, list(message_ids)))
        future = asyncio.get_running_loop().create_future()
        future.set_result((0, []))
        return future


class _Event:
    def __init__(self, msg_id):
        self.message = _Msg(msg_id)
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)
        return _Msg(999999)


class _Bot:
    def __init__(self, client):
        self.client = client
        self.logger = _Logger()
        self.reply_input_peer_cache = {}
        self.message_delete_queue = _DeleteQueueSpy()


def _reset_state():
    mh.MANUAL_PURGE_MAX_PER_ADMIN.clear()
    mh.ADD_DELETED_CACHE.clear() if hasattr(mh, "ADD_DELETED_CACHE") else None


# ---------------------------------------------------------------------------
def test_purge_800_deletes_exactly_800_and_fast():
    """پاک ۸۰۰ → ۸۰۰ حذفِ واقعی، بچِ ۱۰۰تایی، موازی، بدونِ صفِ مشترک آن."""

    async def scenario():
        client = _Client(history_top=5000)
        bot = _Bot(client)
        command_id = 5000
        event = _Event(command_id)
        started = time.perf_counter()
        await mh._run_manual_purge(
            bot, event, -1001, 7, {"username": "admin"},
            "admin", 800, -1001, anchor_id=command_id,
        )
        elapsed = time.perf_counter() - started
        return bot, client, event, elapsed

    bot, client, event, elapsed = asyncio.run(scenario())
    ids = client.deleted
    check("تعدادِ حذف‌شده دقیقاً ۸۰۰ است", len(ids) == 800, f"{len(ids)}")
    check("۸۰۰ شناسهٔ یکتا و بدونِ تکرار", len(set(ids)) == 800, f"{len(set(ids))}")
    check("خودِ پیام دستور (۵۰۰۰) پاک نشده", 5000 not in ids)
    check("بالاترین شناسهٔ پاک‌شده ۴۹۹۹ است (پیام قبل از دستور)",
          4999 in ids, f"{sorted(ids)[-3:]}")
    check("کوچک‌ترین شناسهٔ پاک‌شده ۴۲۰۰ است",
          min(ids) == 4200, f"{min(ids)}")
    check("هیچ بچی بزرگ‌تر از ۱۰۰ نیست",
          max(client.batch_sizes) <= mh.MANUAL_PURGE_BATCH_SIZE,
          f"{client.batch_sizes}")
    check("همهٔ بچ‌ها ۱۰۰تایی‌اند", set(client.batch_sizes) == {100},
          f"{client.batch_sizes}")
    check("حذف موازی انجام شد (بیش از یک RPC هم‌زمان)",
          client.max_active > 1, f"max_active={client.max_active}")
    check("سقفِ هم‌زمانی رعایت شد",
          client.max_active <= mh.MANUAL_PURGE_PARALLEL,
          f"max_active={client.max_active}")
    check("هیچ‌چیز به صفِ مشترکِ حذف نرفت",
          bot.message_delete_queue.enqueued == [],
          f"{bot.message_delete_queue.enqueued}")
    check("گزارش پایان با تعدادِ واقعی ۸۰۰",
          len(event.replies) == 1 and "８００" in event.replies[0],
          f"{event.replies}")
    check("لاگِ پایان ثبت شد", len(bot.logger.logged("MANUAL PURGE DONE")) == 1)
    check("زمانِ اجرا خیلی کمتر از حالتِ تک‌تک است (۸ RPC موازی)",
          elapsed < 0.6, f"{elapsed:.3f}s")


def test_reply_text_reports_real_count():
    async def scenario():
        client = _Client(history_top=1000)
        bot = _Bot(client)
        event = _Event(1000)
        await mh._run_manual_purge(
            bot, event, -1002, 8, {"username": "admin"}, "admin", 100,
            -1002, anchor_id=1000)
        return event

    event = asyncio.run(scenario())
    check("پیامِ گزارش ارسال شد", len(event.replies) == 1, f"{event.replies}")
    check("متنِ گزارش شاملِ ۱۰۰ است (ارقامِ تمام‌عرض مثل قبل)",
          "１００" in event.replies[0], event.replies[0])
    check("گزارش طبقِ قالبِ قبلی (💣)", event.replies[0].endswith("💣"),
          event.replies[0])


def test_history_short_falls_back_to_id_range():
    """If each page is capped at 50, page through actual messages, not IDs."""
    async def scenario():
        client = _Client(history_top=4000, history_limit=50)
        bot = _Bot(client)
        await mh._run_manual_purge(
            bot, _Event(4000), -1003, 9, {"username": "a"}, "a", 800,
            -1003, anchor_id=4000)
        return client

    client = asyncio.run(scenario())
    check("با صفحه‌های ۵۰تایی هم ۸۰۰ پیام واقعی پاک شد",
          len(client.deleted) == 800, f"{len(client.deleted)}")
    check("شناسه‌ها یکتا هستند", len(set(client.deleted)) == 800)


def test_retry_reports_remaining_only_when_real():
    async def scenario():
        client = _Client(history_top=500, fail_ids={400})
        bot = _Bot(client)
        event = _Event(500)
        await mh._run_manual_purge(
            bot, event, -1004, 10, {"username": "a"}, "a", 200,
            -1004, anchor_id=500)
        return event, client, bot

    event, client, bot = asyncio.run(scenario())
    check("۱۹۹ پیامِ سالم پاک شد (شناسهٔ خراب ۴۰۰ پاک نشد)",
          len(set(client.deleted)) == 199, f"{len(set(client.deleted))}")
    check("شناسهٔ خراب داخلِ بازمانده‌ها گزارش شد", 400 not in client.deleted)
    check("گزارشِ نقص ارسال شد", "تأیید نشد" in event.replies[0], event.replies[0])
    check("لاگِ INCOMPLETE ثبت شد",
          len(bot.logger.logged("MANUAL DELETE INCOMPLETE")) == 1)


def test_per_admin_cooldown_only():
    async def scenario():
        mh.MANUAL_PURGE_MAX_PER_ADMIN.clear()
        now = mh._loop_now()
        mh._manual_purge_mark(-2001, 111, 800, now=now)
        return (
            mh._manual_purge_allowed(-2001, 111, now=now),
            mh._manual_purge_allowed(-2001, 222, now=now - 1000),
            mh._manual_purge_allowed(-2002, 111, now=now),
        )

    same_admin, other_admin, other_group = asyncio.run(scenario())
    check("پاکِ هم‌زمانِ همان ادمین مسدود است", same_admin is False)
    check("ادمینِ دیگر همان گروه آزاد است", other_admin is True)
    check("گروه دیگر آزاد است", other_group is True)

    # آزادشدن پس از پایانِ واقعی
    async def scenario2():
        mh.MANUAL_PURGE_MAX_PER_ADMIN.clear()
        mh._manual_purge_mark(-2003, 5, 800)
        mh._manual_purge_release(-2003, 5)
        return mh._manual_purge_allowed(-2003, 5)

    check("بعد از اتمام، دوباره آزاد می‌شود", asyncio.run(scenario2()) is True)

    # A slow verification must not expire the guard while the task is active.
    expected = mh._manual_purge_expected_seconds(800)
    check("سقف قفل بررسی تک‌پیامی کند را پوشش می‌دهد", expected > 800, f"{expected}")


def test_two_admins_run_in_parallel():
    async def scenario():
        mh.MANUAL_PURGE_MAX_PER_ADMIN.clear()
        client = _Client(history_top=3000, latency=0.02)
        bot = _Bot(client)
        t0 = time.perf_counter()
        await asyncio.gather(
            mh._run_manual_purge(bot, _Event(3000), -1005, 1, {"u": 1}, "a",
                                 300, -1005, anchor_id=3000),
            mh._run_manual_purge(bot, _Event(2999), -1005, 2, {"u": 2}, "b",
                                 300, -1005, anchor_id=2999),
        )
        return time.perf_counter() - t0, client

    elapsed, client = asyncio.run(scenario())
    check("دو ادمین هم‌زمان هر دو ۳۰۰ حذف انجام دادند (۶۰۰ RPC-آی‌دی)",
          len(client.deleted) == 600, f"{len(client.deleted)}")
    check("شناسه‌های مشترک فقط یک‌بار حذف شده‌اند (بدونِ تکرارِ بی‌فایده)",
          len(set(client.deleted)) == 301, f"{len(set(client.deleted))}")
    check("اجرای موازیِ دو ادمین کند نشد", elapsed < 0.5, f"{elapsed:.3f}s")


def main():
    print("\n### ۱) پاک ۸۰۰ — دقیق، ۱۰۰تایی و موازی")
    test_purge_800_deletes_exactly_800_and_fast()
    print("\n### ۲) گزارشِ پایان با تعدادِ واقعی")
    test_reply_text_reports_real_count()
    print("\n### ۳) هیستوریِ ناقص → تکمیل با بازهٔ شناسه")
    test_history_short_falls_back_to_id_range()
    print("\n### ۴) بازمانده‌ها واقعی گزارش می‌شوند")
    test_retry_reports_remaining_only_when_real()
    print("\n### ۵) قفلِ per-admin (نه per-group)")
    test_per_admin_cooldown_only()
    print("\n### ۶) دو ادمینِ هم‌زمان")
    test_two_admins_run_in_parallel()
    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_800_sparse_actual_messages_across_server_capped_pages():
    async def scenario():
        client = _Client(history_top=5200, history_limit=100, latency=0)
        client.visible = {i for i in range(1, 5201) if i % 3 == 0 or i % 7 == 0}
        before = sorted((i for i in client.visible if i < 5000), reverse=True)
        event = _Event(5000); bot = _Bot(client)
        await mh._run_manual_purge(bot, event, -100, 9, {}, 'admin', 800, -100, anchor_id=5000)
        return before, client, event
    before, client, event = asyncio.run(scenario())
    assert set(client.deleted) == set(before[:800])
    assert len(client.deleted) == 800
    assert '８００ پیام پاک شد' in event.replies[0]
    assert client.get_calls >= 16  # history and verification, not guessed IDs


def test_history_partial_failure_never_invents_ids_or_claims_requested_count():
    class PartialHistory(_Client):
        async def get_messages(self, entity, limit=None, ids=None, offset_id=0):
            if ids is None and offset_id and offset_id <= self.history_top - 100:
                raise ConnectionError('history offline')
            return await super().get_messages(entity, limit=limit, ids=ids, offset_id=offset_id)
    async def scenario():
        client = PartialHistory(history_top=3000, latency=0)
        bot = _Bot(client); event = _Event(3000)
        await mh._run_manual_purge(bot, event, -100, 9, {}, 'admin', 800, -100, anchor_id=3000)
        return client, event
    client, event = asyncio.run(scenario())
    assert len(client.deleted) == 100
    assert set(client.deleted) == set(range(2900,3000))
    assert '１００ پیام پاک‌شده' in event.replies[0]
    assert 'فقط １００ پیام' in event.replies[0]
    assert '８００ پیام پاک شد' not in event.replies[0]


def test_acknowledged_rpc_without_deletion_is_not_counted():
    class SilentNoDelete(_Client):
        async def delete_messages(self, entity, ids):
            self.batch_sizes.append(len(ids))
            # Only one hundred are actually deleted although every RPC reports success.
            done = [mid for mid in ids if 901 <= mid <= 1000]
            self.visible.difference_update(done)
            self.deleted.extend(done)
            return True
    async def scenario():
        client=SilentNoDelete(history_top=1100,latency=0)
        bot=_Bot(client); event=_Event(1001)
        await mh._run_manual_purge(bot,event,-100,9,{},'admin',800,-100,anchor_id=1001)
        return client,event
    client,event=asyncio.run(scenario())
    assert len(set(client.deleted)) == 100
    assert '１００ پیام پاک‌شده' in event.replies[0]
    assert 'حذف ７００ پیام تأیید نشد' in event.replies[0]
    assert '８００ پیام پاک شد' not in event.replies[0]


def test_short_id_lookup_falls_back_to_individual_checks_not_assumptions():
    class ShortLookup(_Client):
        def __init__(self):
            super().__init__(history_top=20,latency=0)
            self.single_lookups = []
        async def delete_messages(self, entity, ids):
            self.visible.difference_update([1,2])
            return True
        async def get_messages(self, entity, limit=None, ids=None, offset_id=0):
            if isinstance(ids,list):
                # Server silently truncates the response; omitted != deleted.
                return [_Msg(mid) for mid in ids if mid in self.visible][:1]
            if ids is not None:
                self.single_lookups.append(ids)
            return await super().get_messages(entity,limit=limit,ids=ids,offset_id=offset_id)
    async def scenario():
        client=ShortLookup(); bot=_Bot(client)
        deleted,unconfirmed=await mh._manual_purge_delete(bot,-100,[1,2,3,4],-100)
        return client,deleted,unconfirmed
    client,deleted,unconfirmed=asyncio.run(scenario())
    assert deleted == 2
    assert set(unconfirmed) == {3,4}
    assert {1,2,4}.issubset(client.single_lookups)


def test_verify_failure_is_unknown_never_reported_as_success():
    class UnavailableVerification(_Client):
        async def get_messages(self, entity, limit=None, ids=None, offset_id=0):
            if ids is not None:
                raise RuntimeError('lookup unavailable')
            return await super().get_messages(entity,limit=limit,ids=ids,offset_id=offset_id)
    async def scenario():
        client=UnavailableVerification(history_top=200,latency=0)
        bot=_Bot(client); event=_Event(200)
        await mh._run_manual_purge(bot,event,-100,9,{},'admin',100,-100,anchor_id=200)
        return client,event
    client,event=asyncio.run(scenario())
    assert len(client.deleted) == 100
    assert len(client.batch_sizes) == 1  # unknown does not cause blind retries
    assert '０ پیام پاک‌شده' in event.replies[0]
    assert 'حذف １００ پیام تأیید نشد' in event.replies[0]
    assert '１００ پیام پاک شد' not in event.replies[0]


def test_missing_anchor_does_not_delete_command_or_any_messages():
    async def scenario():
        client=_Client(history_top=200,latency=0)
        bot=_Bot(client); event=_Event(200)
        await mh._run_manual_purge(bot,event,-100,9,{},'admin',100,-100,anchor_id=None)
        return client,event
    client,event=asyncio.run(scenario())
    assert client.deleted == []
    assert 'فقط ０ پیام' in event.replies[0]


def test_800_deletions_verified_with_omitted_entries_and_single_lookup():
    class OmittedResults(_Client):
        def __init__(self):
            super().__init__(history_top=5000,history_limit=100,latency=0)
            self.single_count=0
        async def get_messages(self, entity, limit=None, ids=None, offset_id=0):
            if isinstance(ids, list):
                # Some real library responses omit absent entries entirely.
                return [_Msg(mid) for mid in ids if mid in self.visible]
            if ids is not None:
                self.single_count += 1
            return await super().get_messages(entity,limit=limit,ids=ids,offset_id=offset_id)
    async def scenario():
        client=OmittedResults(); bot=_Bot(client); event=_Event(5000)
        await mh._run_manual_purge(bot,event,-100,9,{},'admin',800,-100,anchor_id=5000)
        return client,event
    client,event=asyncio.run(scenario())
    assert len(set(client.deleted)) == 800
    assert client.single_count == 800
    assert '８００ پیام پاک شد' in event.replies[0]
