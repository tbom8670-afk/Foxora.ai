# -*- coding: utf-8 -*-
"""🧪 تستِ مسیرِ ۶۰ ثانیه‌ایِ حذفِ کارت‌های هشدار (تبلیغ گر + ضد کانال).

اجرا:

    PYTHONPATH=. python3 tests/test_notice_cleanup_ttl.py

این فایل همان سرویسِ مشترک (``modules/profile_guard/cleanup.py``) را
می‌آزماید که هم کارتِ «تبلیغ گر» و هم کارتِ «ضد کانال و بیو» از آن استفاده
می‌کنند. تمرکز روی چیزهایی است که در تولید خراب می‌شدند:

۱. پنجرهٔ تاریخچه: کارتی که از ۲۰ پیامِ آخر بیرون افتاده (گروه شلوغ) باز هم
   پیدا و پاک می‌شود (پنجره‌ها ۲۰ ⇒ ۵۰ ⇒ ۱۰۰).
۲. مقصدِ حذف: اگر ``delete_messages`` با peerِ ساخته‌شده خطا داد، با شناسهٔ
   خام (و شکلِ ``-100…``) دوباره تلاش می‌شود.
۳. جاروی دوره‌ای: ردیفی که تایمرش خطا داده، بعد از موعد + مهلت پاک می‌شود.
۴. استارتاپ: ``ready ttl=60s`` و بالا آمدنِ جارو.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="notice_ttl_test_")
os.environ.setdefault("BOT_HOME", _TMP)
os.environ.setdefault("BOT_INSTANCE", "notice_ttl_test")

from modules.profile_guard import cleanup  # noqa: E402

cleanup.FILE = Path(_TMP) / "notice_ttl_case.json"

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ✅ %s" % label)
    else:
        FAIL += 1
        print("  ❌ %s %s" % (label, detail))


def run(coro):
    return asyncio.run(coro)


def reset_file():
    try:
        cleanup.FILE.unlink()
    except OSError:
        pass
    cleanup._resolved.clear()


class Msg:
    def __init__(self, mid, body):
        self.id = mid
        self.message = body


class Peer:
    """peerِ ساختگی — حذف با آن خطا می‌دهد (مثلِ حالتِ واقعیِ بعضی نسخه‌ها)."""


class Client:
    def __init__(self, history, *, peer_delete_fails=True, small_window=20):
        self.history = history
        self.deleted = []
        self.history_calls = []
        self.peer_delete_fails = peer_delete_fails
        self.small_window = small_window

    async def get_messages(self, target, limit=20):
        self.history_calls.append((target, limit))
        if limit <= self.small_window:
            return []
        return list(self.history)

    async def delete_messages(self, target, ids):
        if isinstance(target, Peer) and self.peer_delete_fails:
            raise RuntimeError("PEER_ID_INVALID")
        self.deleted.append((target, list(ids)))


class Bot:
    def __init__(self, client):
        self.client = client
        self.logger = self

    def log_info(self, message):
        pass

    def log_error(self, message):
        pass


class Peers:
    @staticmethod
    def group_peer(bot, chat_id):
        return Peer()


# ---------------------------------------------------------------------------
print("\n=== ۱) کارت از پنجرهٔ ۲۰ پیام بیرون افتاده (گروه شلوغ) ===")
reset_file()
card_text = "⛔ کاربر  @x\nپیام شما پاک شد | دلیل : هرزنامه تبلیغات"
client = Client([Msg(8001, "پیامِ عادی ۱"), Msg(7000, card_text)])
bot = Bot(client)


async def _busy_group():
    ok = await cleanup._delete_now(bot, -1001234567890, None, card_text)
    return ok


check("کارتِ بیرون‌افتاده از پنجرهٔ ۲۰ پیام پیدا و پاک شد",
      run(_busy_group()) is True)
check("اول پنجرهٔ کوچک، بعد پنجرهٔ بزرگ‌تر امتحان شد",
      [limit for _t, limit in client.history_calls] == [20, 20, 50],
      client.history_calls)
check("شناسهٔ درست حذف شد",
      client.deleted and client.deleted[-1][1] == [7000], client.deleted)

# ---------------------------------------------------------------------------
print("\n=== ۲) خطای حذف با peer ⇒ تلاش با شناسهٔ خام ===")
reset_file()
client2 = Client([Msg(9111, card_text)], peer_delete_fails=True)
bot2 = Bot(client2)


async def _peer_fail():
    return await cleanup._delete_now(bot2, -1001234567890, 9111, card_text)


check("حذف با وجودِ خطای peer انجام شد", run(_peer_fail()) is True)
check("با شناسهٔ خام حذف شد",
      any(target == -1001234567890 for target, _ids in client2.deleted),
      client2.deleted)

# ---------------------------------------------------------------------------
print("\n=== ۳) جاروی دوره‌ای: ردیفِ جامانده پاک می‌شود ===")
reset_file()
client3 = Client([])
bot3 = Bot(client3)
cleanup._save([{"chat_id": -1001234567890, "message_id": 9500,
                "text": card_text, "due": time.time() - 60}])
check("ردیفِ سررسیده جارو می‌شود", run(cleanup.sweep(bot3)) == 1)
check("پیامِ همان ردیف حذف شد",
      client3.deleted and client3.deleted[-1][1] == [9500], client3.deleted)
check("ردیف از فایل پاک شد", cleanup._load() == [], cleanup._load())

# ردیفی که تازه سررسیده (داخلِ مهلت) دست‌نخورده می‌ماند تا با تایمرِ اصلی
# دوبار حذف نشود.
reset_file()
client4 = Client([])
bot4 = Bot(client4)
cleanup._save([{"chat_id": -1001234567890, "message_id": 9600,
                "text": card_text, "due": time.time()}])
check("ردیفِ تازه‌سررسیده را جارو نمی‌زند (دوبار حذف نشود)",
      run(cleanup.sweep(bot4)) == 0 and client4.deleted == [])

# ---------------------------------------------------------------------------
print("\n=== ۴) زمان‌بندی: ثبت و شناسهٔ زودهنگام ===")
reset_file()
client5 = Client([Msg(9700, card_text)], small_window=0)
bot5 = Bot(client5)


class Sent:
    id = None          # این نسخهٔ SPlusthon اغلب شناسه برنمی‌گرداند


async def _schedule_case():
    task = cleanup.schedule(bot5, -1001234567890, Sent(), ttl=0.05,
                            text=card_text)
    if task is not None:
        await task
    return list(cleanup._load())


rows = run(_schedule_case())
check("ردیف پس از حذف از فایل پاک می‌شود", rows == [], rows)
check("بدونِ شناسه هم پیام حذف شد",
      client5.deleted and client5.deleted[-1][1] == [9700], client5.deleted)
check("مهلتِ پیش‌فرض ۶۰ ثانیه است", cleanup.ttl_seconds() == 60)

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("نتیجه: %d پاس / %d شکست" % (PASS, FAIL))
print("=" * 60)
sys.exit(1 if FAIL else 0)
