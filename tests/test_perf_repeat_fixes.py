"""🧪 تست‌های تسکِ «کارایی و اجرای تکراری» (۵ مشکل).

پوشش (همان سناریوهای الزامیِ پرامپت):

۱. کش «عضو» ۶۰ ثانیه: پیام دوم هیچ RPC‌ای نمی‌زند.
۲. کش «غیرعضو» ۴ ثانیه: بعد از انقضا دوباره بررسی می‌شود.
۳. تازه‌جوین: رویدادِ جوین کش را باطل می‌کند → تأیید بدون اسکن.
۴. لفت: رویدادِ خروج باطل می‌کند → غیرعضو شناخته و پیام حذف می‌شود.
۵. پیامِ تکراری (همان chat_id+message_id+متن) فقط یک‌بار پردازش می‌شود؛
   همان شناسه با متنِ متفاوت = پیامِ دیگر → پردازش می‌شود.
۶. مسیرِ «دستورِ تأییدشدهٔ دو مرحله‌ای» از dedup رد نمی‌شود.
۷. FloodWaitError: مثل قبل fail-open، بدون حذف، و بدون درخواستِ بعدی.
۸. فلگ‌های خاموش: رفتار دقیقاً مثل قبل (TTLهای ۳۰۰/۲۰ و بدونِ dedup).
۹. single-flight مسیرِ اسکن: دو بررسیِ هم‌زمانِ همان کاربر، یک پیمایش.

از کارگاه تست موجود (`tests/test_force_join.py`) بازاستفاده می‌شود؛ فقط
لایهٔ شبکه جعلی است.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("splusthon")

from handlers import message_handler  # noqa: E402
from handlers.message_handler import handle_new_message  # noqa: E402
from modules import force_join_service as service, force_join_storage as storage  # noqa: E402
from modules import message_dedup  # noqa: E402

from test_force_join import (  # noqa: E402
    CHANNEL_ID,
    CHAT_ID,
    FakeClient,
    FakeEvent,
    _make_bot,
    _register_channel,
    _scan_only_routes,
    _setup_group,
    _user,
    _user_with_hash,
)

PARTICIPANT_ONLY = {service.ROUTE_PARTICIPANT: True}


def _fresh(tmp_path, monkeypatch, routes, **client_kwargs):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    service.save_channel_routes(CHAT_ID, routes)
    client = FakeClient(**client_kwargs)
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    message_dedup.reset()
    return bot, client, deleted


# ---------------------------------------------------------------------------
# ۱) کشِ «عضو» و ۲) کشِ «غیرعضو»
# ---------------------------------------------------------------------------

def test_member_answer_is_cached_for_60s(tmp_path, monkeypatch):
    bot, client, deleted = _fresh(
        tmp_path, monkeypatch, PARTICIPANT_ONLY,
        members={555}, participants=[],
    )
    user = _user(555)

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "سلام", msg_id=1))
        await asyncio.sleep(0.02)
        first = client.get_participant_calls
        # ۲۰ پیامِ بعدیِ همان کاربر: مسیرِ گرم، صفر RPC
        for index in range(20):
            await handle_new_message(
                bot, FakeEvent(CHAT_ID, user, f"پیام {index}", msg_id=10 + index))
        await asyncio.sleep(0.02)
        return first, client.get_participant_calls

    first, total = asyncio.run(scenario())
    assert first == 1, first
    assert total == first, "کشِ «عضو» باید جلوی درخواستِ تکراری را بگیرد"
    assert deleted == []
    record = storage.get_group(CHAT_ID)
    assert service._ttl_for(storage.STATUS_MEMBER, record) == 60
    assert service._ttl_for(storage.STATUS_NOT_MEMBER, record) == 4


def test_not_member_answer_expires_after_4s(tmp_path, monkeypatch):
    """«عضو نیست» فقط کوتاه کش می‌شود؛ با گذشتِ ۴ ثانیه دوباره پرسیده می‌شود."""
    _bot_, _client_, _deleted_ = _fresh(
        tmp_path, monkeypatch, PARTICIPANT_ONLY, members=set(),
    )
    record = storage.get_group(CHAT_ID)
    key = service.channel_key(record["channel_id"])
    now = time.time()
    service._full_scan_cache[key] = (now - 2, set())          # ۲ ثانیه پیش
    assert service._completed_scan_status(record, 777, now) == \
        storage.STATUS_NOT_MEMBER
    service._full_scan_cache[key] = (now - 6, set())          # ۶ ثانیه پیش
    assert service._completed_scan_status(record, 777, now) is None

    # با فلگِ خاموش، همان پنجرهٔ قبلی (۲۰ ثانیه) برمی‌گردد.
    monkeypatch.setenv("FORCE_JOIN_CACHE_ENABLED", "0")
    service._full_scan_cache[key] = (now - 6, set())
    assert service._completed_scan_status(record, 777, now) == \
        storage.STATUS_NOT_MEMBER


# ---------------------------------------------------------------------------
# ۳) تازه‌جوین و ۴) لفت
# ---------------------------------------------------------------------------

def test_join_event_invalidates_cache_without_scan(tmp_path, monkeypatch):
    bot, client, deleted = _fresh(
        tmp_path, monkeypatch, PARTICIPANT_ONLY, members={901},
    )

    async def scenario():
        # کاربر در کشِ حافظه «غیرعضو» است
        service._remember(CHANNEL_ID, 901, storage.STATUS_NOT_MEMBER,
                          time.time())
        # رویدادِ جوین می‌رسد → باید فوراً باطل شود
        service.forget_user(CHANNEL_ID, 901)
        assert service._member_cache.get((CHANNEL_ID, 901)) is None
        await handle_new_message(bot, FakeEvent(CHAT_ID, _user(901), "سلام",
                                                msg_id=51))
        await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert deleted == []
    assert client.get_participants_calls == 0


def test_leave_event_invalidates_cache_and_blocks(tmp_path, monkeypatch):
    bot, client, deleted = _fresh(
        tmp_path, monkeypatch, PARTICIPANT_ONLY, members=set(),
    )

    async def scenario():
        service._remember(CHANNEL_ID, 902, storage.STATUS_MEMBER,
                          time.time())
        # خروجِ کاربر → باطل‌سازیِ همان کلید
        service.forget_user(CHANNEL_ID, 902)
        await handle_new_message(bot, FakeEvent(CHAT_ID, _user(902), "سلام",
                                                msg_id=52))
        await asyncio.sleep(0.03)

    asyncio.run(scenario())
    assert deleted != [], "پس از لفت باید غیرعضو شناخته شود"
    assert client.get_participant_calls == 1


# ---------------------------------------------------------------------------
# ۵) پیامِ تکراری  ۶) مسیرِ دو مرحله‌ای
# ---------------------------------------------------------------------------

def test_duplicate_message_is_processed_once(tmp_path, monkeypatch):
    bot, client, deleted = _fresh(
        tmp_path, monkeypatch, _scan_only_routes(),
        participants=[_user_with_hash(6001)], mode="not_supported",
    )
    event = FakeEvent(CHAT_ID, _user(6002), "سلام", msg_id=61)

    async def scenario():
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        after_first = len(deleted)
        calls_after_first = client.get_participants_calls
        # همان پیام دقیقاً دوباره (update تکراری بعد از reconnect)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        return after_first, calls_after_first, len(deleted), client.get_participants_calls

    after_first, calls_after_first, after_second, calls_after_second = (
        asyncio.run(scenario()))
    assert after_first >= 1
    assert after_second == after_first, "پیامِ تکراری نباید دوباره پردازش شود"
    assert calls_after_second == calls_after_first, "هیچ RPCِ تازه‌ای نزن"


def test_same_id_with_different_text_is_not_deduped(tmp_path, monkeypatch):
    bot, _client, deleted = _fresh(
        tmp_path, monkeypatch, _scan_only_routes(),
        participants=[_user_with_hash(6001)], mode="not_supported",
    )

    async def scenario():
        await handle_new_message(
            bot, FakeEvent(CHAT_ID, _user(6003), "سلام", msg_id=62))
        await asyncio.sleep(0.05)
        first = len(deleted)
        await handle_new_message(
            bot, FakeEvent(CHAT_ID, _user(6003), "یک پیامِ کاملاً دیگر", msg_id=62))
        await asyncio.sleep(0.05)
        return first, len(deleted)

    first, total = asyncio.run(scenario())
    assert total > first, "دو پیامِ متفاوت نباید dedup شوند"


def test_two_factor_confirmed_path_is_never_deduped(tmp_path, monkeypatch):
    bot, _client, _deleted = _fresh(
        tmp_path, monkeypatch, _scan_only_routes(),
        participants=[_user_with_hash(6001)], mode="not_supported",
    )
    event = FakeEvent(CHAT_ID, _user(6004), "بن", msg_id=63)
    seen = []

    real = message_handler.two_factor_confirmed_active
    monkeypatch.setattr(message_handler, "two_factor_confirmed_active",
                        lambda: True)
    try:
        async def scenario():
            for _ in range(3):
                await handle_new_message(bot, event)
                await asyncio.sleep(0.01)
        asyncio.run(scenario())
    finally:
        monkeypatch.setattr(message_handler, "two_factor_confirmed_active", real)
    assert message_dedup.COUNTERS["duplicates"] == 0
    assert seen == []


def test_dedup_lru_and_ttl_are_bounded():
    message_dedup.reset()
    for index in range(1500):
        message_dedup.is_duplicate(-100, index, f"متن {index}")
    assert message_dedup.size() <= message_dedup.max_entries() == 1000
    message_dedup.reset()
    assert message_dedup.is_duplicate(-100, 1, "الف") is False
    assert message_dedup.is_duplicate(-100, 1, "الف") is True
    assert message_dedup.is_duplicate(-100, 1, "ب") is False


# ---------------------------------------------------------------------------
# ۷) FloodWaitError
# ---------------------------------------------------------------------------

def test_flood_wait_is_fail_open_and_stops_further_calls(tmp_path, monkeypatch):
    bot, client, deleted = _fresh(
        tmp_path, monkeypatch, PARTICIPANT_ONLY, mode="flood",
    )

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, _user(8100), "سلام",
                                                msg_id=71))
        await asyncio.sleep(0.05)
        after_first = client.get_participant_calls
        await handle_new_message(bot, FakeEvent(CHAT_ID, _user(8101), "سلام",
                                                msg_id=72))
        await asyncio.sleep(0.05)
        return after_first, client.get_participant_calls

    first, total = asyncio.run(scenario())
    assert deleted == [], "FloodWait یعنی مدرکی نیست → هیچ حذفی"
    assert total == first, "در بازهٔ flood نباید درخواستِ تازه بزند"


# ---------------------------------------------------------------------------
# ۸) فلگ‌های خاموش → رفتارِ قبلی
# ---------------------------------------------------------------------------

def test_flags_off_restore_previous_behaviour(tmp_path, monkeypatch):
    monkeypatch.setenv("FORCE_JOIN_CACHE_ENABLED", "0")
    monkeypatch.setenv("HANDLER_MESSAGE_DEDUP_ENABLED", "0")
    bot, client, deleted = _fresh(
        tmp_path, monkeypatch, PARTICIPANT_ONLY, members={555},
    )
    record = storage.get_group(CHAT_ID)
    assert service._ttl_for(storage.STATUS_MEMBER, record) == service.member_ttl()
    assert service._ttl_for(storage.STATUS_NOT_MEMBER, record) == \
        service.not_member_ttl()
    assert service.member_ttl() == 300 and service.not_member_ttl() == 20

    event = FakeEvent(CHAT_ID, _user(555), "قفل", msg_id=81)

    async def scenario():
        await handle_new_message(bot, event)
        await asyncio.sleep(0.02)
        await handle_new_message(bot, event)   # با فلگِ خاموش: دوباره اجرا می‌شود
        await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert message_dedup.COUNTERS["duplicates"] == 0
    assert deleted == []


# ---------------------------------------------------------------------------
# ۹) single-flight مسیرِ اسکن
# ---------------------------------------------------------------------------

def test_concurrent_checks_for_same_user_run_one_scan(tmp_path, monkeypatch):
    bot, _client, _deleted = _fresh(
        tmp_path, monkeypatch, _scan_only_routes(),
        participants=[_user_with_hash(6001)], mode="not_supported",
    )
    record = storage.get_group(CHAT_ID)
    calls = {"n": 0}

    async def slow_find(*_args, **_kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"state": "absent", "complete": True, "scanned": 1, "pages": 1,
                "user": None, "member_ids": [], "member_ids_complete": True}

    monkeypatch.setattr(service.api, "find_member", slow_find)

    async def scenario():
        return await asyncio.gather(*[
            service.resolve_status(bot, record, 7000, force=True)
            for _ in range(4)
        ])

    results = asyncio.run(scenario())
    assert calls["n"] == 1, calls["n"]
    assert all(status == storage.STATUS_NOT_MEMBER for status, *_ in results), results
