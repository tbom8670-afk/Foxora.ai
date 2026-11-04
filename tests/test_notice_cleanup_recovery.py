"""♻️ تست «بازیابی تایمر اعلان‌ها پس از ری‌استارت».

تکمیل اصلاحِ «اعلام مهلت گروه»: ماژول `group_expiry` شناسهٔ پیام‌های اعلام
را در رکورد مهلت نگه می‌دارد تا در لحظهٔ انقضا پاک شوند؛ ولی صف عمومی
`notice_cleanup` که تایمرِ همهٔ اعلان‌های خودکار (مهلت، هرزنامه، عضویت
اجباری، …) را می‌گرداند، فایل پایدارش را در هر استارت دور می‌ریخت. نتیجه:
هر ری‌استارت، همهٔ تایمرهای در انتظار را نابود می‌کرد و همان پیام‌ها تا
ابد می‌ماندند.

قرارداد تازه (بدون بازگشت طوفان حذف):
  • ردیف منقضی‌نشده  → همیشه بازیابی می‌شود.
  • ردیف تازه‌منقضی   → یک‌بار جبران می‌شود (تا سقف ۶ ساعت کهنگی).
  • ردیف کهنه/نامعتبر → دور ریخته می‌شود (سقف کل ۲۰۰۰ و ۲۰۰ در هر گروه).
"""
from __future__ import annotations

import asyncio
import json
import time

from modules.notice_cleanup import NoticeCleanup

CHAT_ID = -1009910888


def test_future_notice_survives_restart(tmp_path):
    """تایمر «۵ روز بعد» با ری‌استارت گم نمی‌شود."""
    path = str(tmp_path / "notice_cleanup.json")
    first = NoticeCleanup(path, ttl_seconds=60)
    first.schedule(CHAT_ID, 1001, ttl=5 * 86400)
    first._persist()

    second = NoticeCleanup(path, ttl_seconds=60)
    pending = [row["message_id"] for row in second.pending(CHAT_ID)]
    assert pending == [1001]


def test_recently_due_notice_is_recovered_once(tmp_path):
    """حذفی که در فاصلهٔ ری‌استارت جا مانده، یک‌بار جبران می‌شود."""
    path = str(tmp_path / "notice_cleanup.json")
    first = NoticeCleanup(path, ttl_seconds=60)
    first.schedule(CHAT_ID, 1002, ttl=1)
    first._persist()

    # ۲ دقیقه بعد ربات ری‌استارت می‌شود (ردیف منقضی است ولی تازه).
    payload = json.loads((tmp_path / "notice_cleanup.json").read_text())
    for rows in payload.values():
        for row in rows:
            row["expires_at"] = time.time() - 120
    (tmp_path / "notice_cleanup.json").write_text(json.dumps(payload))

    second = NoticeCleanup(path, ttl_seconds=60)
    assert [row["message_id"] for row in second.pending(CHAT_ID)] == [1002]


def test_ancient_rows_are_dropped_to_avoid_rpc_storm(tmp_path):
    """ردیف کهنه (شناسهٔ بی‌اعتبار) دوباره اجرا نمی‌شود."""
    path = str(tmp_path / "notice_cleanup.json")
    first = NoticeCleanup(path, ttl_seconds=60)
    first.schedule(CHAT_ID, 1003, ttl=-10 ** 7)
    first._persist()

    second = NoticeCleanup(path, ttl_seconds=60)
    assert second.pending(CHAT_ID) == []
    assert not (tmp_path / "notice_cleanup.json").exists()


def test_recovered_notice_is_really_deleted(tmp_path):
    """ردیف بازیابی‌شده واقعاً به صف حذف می‌رود."""
    path = str(tmp_path / "notice_cleanup.json")

    class _Queue:
        def __init__(self):
            self.calls = []

        def enqueue(self, chat_id, ids, *, priority=1, rpc_peer=None):
            self.calls.append((chat_id, list(ids)))
            future = asyncio.get_running_loop().create_future()
            future.set_result((len(ids), []))
            return future

    async def run():
        first = NoticeCleanup(path, ttl_seconds=0.05)
        first.schedule(CHAT_ID, 2001, ttl=0.05)
        first._persist()

        queue = _Queue()
        second = NoticeCleanup(path, ttl_seconds=0.05, delete_queue=queue)
        second.start()
        await asyncio.sleep(0.3)
        second.stop()
        return queue.calls

    calls = asyncio.run(run())
    assert any(2001 in ids for _chat, ids in calls), calls


def test_recovery_is_capped(tmp_path):
    """سقف هر گروه جلوی طوفان RPC را می‌گیرد."""
    path = str(tmp_path / "notice_cleanup.json")
    rows = [
        {"message_id": 5000 + index, "expires_at": time.time() + 600,
         "chat_id": CHAT_ID, "attempts": 0}
        for index in range(400)
    ]
    (tmp_path / "notice_cleanup.json").write_text(json.dumps({str(CHAT_ID): rows}))

    cleaner = NoticeCleanup(path, ttl_seconds=60)
    assert len(cleaner.pending(CHAT_ID)) == 200
