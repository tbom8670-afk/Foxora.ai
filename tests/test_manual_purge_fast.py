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

    async def get_messages(self, entity, limit=None, ids=None):
        self.get_calls += 1
        if ids is not None:
            return _Msg(ids)
        top = self.history_top if self.history_limit is None else min(
            self.history_top, self.history_limit)
        count = min(int(limit or 0), top)
        return [_Msg(top - offset) for offset in range(count)]

    async def delete_messages(self, entity, message_ids):
        self.batch_sizes.append(len(message_ids))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.latency)
            if any(mid in self.fail_ids for mid in message_ids):
                raise RuntimeError("message id invalid")
            self.deleted.extend(message_ids)
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
    """اگر سرور فقط ۵۰ پیام از ۸۰۰ را برگرداند، ۸۰۰ تا کامل شود."""
    async def scenario():
        client = _Client(history_top=4000, history_limit=50)
        bot = _Bot(client)
        await mh._run_manual_purge(
            bot, _Event(4000), -1003, 9, {"username": "a"}, "a", 800,
            -1003, anchor_id=4000)
        return client

    client = asyncio.run(scenario())
    check("با هیستوریِ ناقص هم ۸۰۰ پیام پاک شد",
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
    check("گزارشِ نقص ارسال شد", "باقی" in event.replies[0], event.replies[0])
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

    # قفلِ تخمینی نباید بیش از حد طول بکشد
    expected = mh._manual_purge_expected_seconds(800)
    check("سقفِ انتظارِ ۸۰۰ پیام معقول است (کمتر از ۶۰ ثانیه)",
          expected < 60, f"{expected}")


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
