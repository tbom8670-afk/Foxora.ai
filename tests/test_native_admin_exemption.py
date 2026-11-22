"""🛡️ معافیت سراسری ادمین بومی گروه — مثل VIP، بدون هیچ مجوز دستوری.

پوشش:
  * واحد: کلید روشن/خاموش، تشخیص participant، کش نقش و RPC کش‌شده.
  * یکپارچه با هندلر واقعی: اخطار خودکار برای ادمین بومی ثبت نمی‌شود
    (کاربر عادی همچنان اخطار می‌گیرد)، دورهٔ حذف پنج‌دقیقه‌ای شروع
    نمی‌شود، اخطار/اخراج دستی روی او رد می‌شود و دستور مدیریتیِ خودش
    هم تا وقتی ادمین ثبت‌شدهٔ ربات نشده اجرا نمی‌شود.

    python -m pytest tests/test_native_admin_exemption.py -q
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from handlers import message_handler as handler
from modules import native_admin_exemption as exempt_mod
from handlers.message_handler import ADMIN_PERMISSION_DENIED_TEXT
from modules.group_id import normalize_group_id
from modules import native_admin_warning_lock as warning_lock

from test_bio_link_guard import FakeEvent
from test_native_admin_warning_period import _setup_scenario, _user

NATIVE_ID = 440044


# ===========================================================================
# واحد — ماژول سیاست
# ===========================================================================
def test_enabled_default_on_and_env_switch(monkeypatch):
    monkeypatch.delenv("BOT_NATIVE_ADMIN_EXEMPT", raising=False)
    assert exempt_mod.enabled() is True
    for raw in ("0", "false", "no", "off"):
        monkeypatch.setenv("BOT_NATIVE_ADMIN_EXEMPT", raw)
        assert exempt_mod.enabled() is False
    monkeypatch.setenv("BOT_NATIVE_ADMIN_EXEMPT", "1")
    assert exempt_mod.enabled() is True


def test_participant_is_admin():
    admin = type("ChannelParticipantAdmin", (), {})()
    creator = type("ChatParticipantCreator", (), {})()
    member = type("ChannelParticipant", (), {})()
    assert exempt_mod.participant_is_admin(admin)
    assert exempt_mod.participant_is_admin(creator)
    assert not exempt_mod.participant_is_admin(member)
    assert not exempt_mod.participant_is_admin(None)


def test_cached_native_admin_reads_handler_caches():
    chat, uid = -100123, 55
    bot = SimpleNamespace(native_group_admin_cache={
        (normalize_group_id(chat), "55"): (True, time.monotonic() + 60),
    })
    assert exempt_mod.cached_native_admin(bot, chat, uid)
    bot.native_group_admin_cache[(normalize_group_id(chat), "55")] = (
        True, time.monotonic() - 1)
    assert not exempt_mod.cached_native_admin(bot, chat, uid)

    bot2 = SimpleNamespace(native_group_admin_ids_cache={
        normalize_group_id(chat): (frozenset({"55"}), time.monotonic() + 60),
    })
    assert exempt_mod.cached_native_admin(bot2, chat, uid)
    assert not exempt_mod.cached_native_admin(bot2, chat, 56)


def test_is_native_admin_uses_one_cached_rpc():
    calls = []

    async def get_permissions(chat, user):
        calls.append(getattr(user, "id", user))
        return SimpleNamespace(is_admin=getattr(user, "id", user) == 7)

    bot = SimpleNamespace(client=SimpleNamespace(get_permissions=get_permissions))
    user = SimpleNamespace(id=7)

    async def scenario():
        assert await exempt_mod.is_native_admin(bot, -1001, 7, user) is True
        assert await exempt_mod.is_native_admin(bot, -1001, 7, user) is True
        assert await exempt_mod.is_native_admin(bot, -1001, 8,
                                                SimpleNamespace(id=8)) is False
        assert await exempt_mod.is_native_admin(bot, -1001, 8,
                                                SimpleNamespace(id=8)) is False

    asyncio.run(scenario())
    assert calls == [7, 8]  # هر کاربر فقط یک RPC؛ بقیه از کش.


def test_exempt_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("BOT_NATIVE_ADMIN_EXEMPT", "0")
    bot = SimpleNamespace(native_group_admin_cache={
        (normalize_group_id(-1001), "7"): (True, time.monotonic() + 60),
    })
    assert asyncio.run(exempt_mod.exempt(bot, -1001, 7)) is False


# ===========================================================================
# یکپارچه — هندلر واقعی
# ===========================================================================
def _scenario(tmp_path, monkeypatch, chat, **kwargs):
    """سناریوی هارنس قدیمی، ولی با معافیت سراسریِ روشن (پیش‌فرض جدید)."""
    bot, deleted, tracker, actions = _setup_scenario(
        tmp_path, monkeypatch, chat, **kwargs)
    monkeypatch.setenv("BOT_NATIVE_ADMIN_EXEMPT", "1")
    return bot, deleted, tracker, actions


def test_automatic_warnings_never_touch_native_admin(tmp_path, monkeypatch):
    chat = -1_000_000_911_201
    bot, deleted, tracker, actions = _scenario(tmp_path, monkeypatch, chat)
    user = _user()

    async def scenario():
        for number in (1, 2, 3):  # سه تخلف = بالاتر از آستانهٔ ۳
            event = FakeEvent(chat, user, f"تخلف {number} متفاوت", 200 + number)
            await handler.handle_new_message(bot, event)
            await asyncio.sleep(0)
        assert tracker.get_count(chat, NATIVE_ID) == 0        # هیچ اخطاری
        assert actions.warnings == []                        # هیچ هشدار ارسالی
        assert actions.punishes == actions.bans == actions.mutes == []
        assert bot.sent_notices == []                        # هیچ اطلاعیه‌ای
        assert not warning_lock.is_active(bot, chat, NATIVE_ID)
        assert not any(mid in ids for _c, ids in deleted
                       for mid in (201, 202, 203))           # هیچ حذفی
        assert len(bot.permissions_checked) == 1             # فقط یک RPC نقش

        # کاربر عادی با همان متن همچنان مسیر عادی اخطار را می‌رود.
        stranger = SimpleNamespace(id=223344, username="other",
                                   first_name="غریبه")
        event = FakeEvent(chat, stranger, "تخلف غریبه", 210)
        await handler.handle_new_message(bot, event)
        await asyncio.sleep(0)
        assert tracker.get_count(chat, 223344) == 1
        assert len(actions.warnings) == 1

    asyncio.run(scenario())


def test_native_admin_commands_are_not_executed(tmp_path, monkeypatch):
    chat = -1_000_000_911_202
    bot, deleted, tracker, actions = _scenario(tmp_path, monkeypatch, chat)

    async def scenario():
        # ادمین بومی است ولی ادمین ثبت‌شدهٔ ربات نیست ⇒ مجوز دستور ندارد.
        assert handler._has_group_management_permission(
            bot, chat, NATIVE_ID, "oeuia") is False
        event = FakeEvent(chat, _user(), "اخطار", 300)
        event.reply_to = SimpleNamespace(reply_to_msg_id=290)
        await handler.handle_new_message(bot, event)
        await asyncio.sleep(0)
        assert any(ADMIN_PERMISSION_DENIED_TEXT in text
                   for text, _ in event.replies)
        assert tracker.get_count(chat, NATIVE_ID) == 0
        assert actions.punishes == actions.bans == actions.mutes == []

    asyncio.run(scenario())


def test_manual_warning_on_native_admin_is_refused(tmp_path, monkeypatch):
    chat = -1_000_000_911_203
    bot, deleted, tracker, actions = _scenario(tmp_path, monkeypatch, chat)
    owner = SimpleNamespace(id=1001, username="owner", first_name="مالک")

    async def scenario():
        event = FakeEvent(chat, owner, "اخطار", 300)
        event.reply_to = SimpleNamespace(reply_to_msg_id=290)
        await handler.handle_new_message(bot, event)
        await asyncio.sleep(0)
        assert any("ادمین این گروه است و نمی‌تواند اخطار بگیرد" in text
                   for text, _ in event.replies)
        assert tracker.get_count(chat, NATIVE_ID) == 0
        assert bot.sent_notices == []
        assert not warning_lock.is_active(bot, chat, NATIVE_ID)
        assert actions.punishes == actions.bans == actions.mutes == []

    asyncio.run(scenario())


def test_manual_kick_on_native_admin_is_refused(tmp_path, monkeypatch):
    chat = -1_000_000_911_204
    bot, deleted, tracker, actions = _scenario(tmp_path, monkeypatch, chat)
    owner = SimpleNamespace(id=1001, username="owner", first_name="مالک")

    async def scenario():
        event = FakeEvent(chat, owner, "اخراج", 300)
        event.reply_to = SimpleNamespace(reply_to_msg_id=290)
        await handler.handle_new_message(bot, event)
        await asyncio.sleep(0)
        assert any("ادمین این گروه است و نمی‌تواند اخراج شود" in text
                   for text, _ in event.replies)
        assert actions.punishes == actions.bans == actions.mutes == []

    asyncio.run(scenario())


def test_kill_switch_restores_warning_counter(tmp_path, monkeypatch):
    chat = -1_000_000_911_205
    # _setup_scenario خودش BOT_NATIVE_ADMIN_EXEMPT=0 می‌گذارد (رفتار قبلی).
    bot, deleted, tracker, actions = _setup_scenario(
        tmp_path, monkeypatch, chat)

    async def scenario():
        event = FakeEvent(chat, _user(), "تخلف اول متفاوت", 201)
        await handler.handle_new_message(bot, event)
        await asyncio.sleep(0)
        assert tracker.get_count(chat, NATIVE_ID) == 1
        assert len(actions.warnings) == 1

    asyncio.run(scenario())
