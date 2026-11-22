"""📣 «ارسال تبلیغات» مالک سراسری به پیوی کاربران — مثل اطلاع‌رسانی.

پوشش: تشخیص دستور (نیم‌فاصله/چسبیده)، workflow کامل (پرامپت ← متن ←
پیش‌نمایش با شمارش مخاطب ← تایید ← ارسال)، ساخت مخاطبان از دایالوگ‌های
خصوصی (حذف بات/مالک/خود ربات/حذف‌شده/تکراری)، شمارش موفق/ناموفق، و
این‌که کاربر عادی هیچ دسترسی‌ای ندارد.

    python -m pytest tests/test_private_ads.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from handlers import private_ads_handler as ads
from modules import broadcast_state

OWNER_ID = 68074059


class FakeLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, message):
        self.lines.append(("INFO", str(message)))

    def log_error(self, message):
        self.lines.append(("ERROR", str(message)))


class FakeClient:
    """دایالوگ‌ها و ارسال‌های پیوی جعلی."""

    def __init__(self, dialogs=(), fail_ids=()):
        self._dialogs = list(dialogs)
        self.fail_ids = set(fail_ids)
        self.sent = []  # (user_id, text, entities)

    async def iter_dialogs(self):
        for dialog in self._dialogs:
            yield dialog

    async def send_message(self, target, text, formatting_entities=None):
        user_id = getattr(target, "id", target)
        if user_id in self.fail_ids:
            raise RuntimeError("blocked by user")
        self.sent.append((user_id, text, formatting_entities))
        return SimpleNamespace(id=5000 + len(self.sent))


def _dialog(user_id, *, is_user=True, bot=False, deleted=False):
    entity = SimpleNamespace(id=user_id, bot=bot, deleted=deleted,
                             username=f"u{user_id}")
    return SimpleNamespace(is_user=is_user, is_group=not is_user,
                           id=user_id, entity=entity)


class FakeEvent:
    def __init__(self, text, user_id=OWNER_ID, chat_id=None):
        self.message = SimpleNamespace(
            message=text, entities=[], id=100)
        self.chat_id = OWNER_ID if chat_id is None else chat_id
        self.is_private = chat_id is None
        self.out = False
        self.replies = []

    async def reply(self, text, formatting_entities=None):
        self.replies.append((text, formatting_entities))
        return SimpleNamespace(id=900 + len(self.replies))


def _bot(dialogs=(), fail_ids=()):
    return SimpleNamespace(
        client=FakeClient(dialogs, fail_ids),
        logger=FakeLogger(),
        bot_account_id=999,
    )


@pytest.fixture(autouse=True)
def _owner_only(monkeypatch):
    monkeypatch.setattr(
        ads, "is_global_owner", lambda user_id: user_id == OWNER_ID)
    monkeypatch.setenv("BOT_ADS_SEND_INTERVAL", "0")
    yield
    broadcast_state.clear(ads._state_key(OWNER_ID))


def test_trigger_spellings():
    assert ads.match_ads_trigger("ارسال تبلیغات")
    assert ads.match_ads_trigger("ارسال‌تبلیغات")   # نیم‌فاصله
    assert ads.match_ads_trigger("ارسالتبلیغات")     # چسبیده
    assert ads.match_ads_trigger("ارسال تبلیغ")
    assert ads.match_ads_trigger("اطلاع رسانی") is None
    assert ads.match_ads_trigger("سلام") is None


def test_audience_from_dialogs_excludes_non_users():
    dialogs = [
        _dialog(11),                      # کاربر عادی پیوی ✔
        _dialog(22),                      # کاربر عادی پیوی ✔
        _dialog(-100123, is_user=False),  # گروه ✖
        _dialog(33, bot=True),            # بات ✖
        _dialog(OWNER_ID),                # خود مالک ✖
        _dialog(999),                     # حساب ربات ✖
        _dialog(44, deleted=True),        # حساب حذف‌شده ✖
        _dialog(11),                      # تکراری ✖
    ]
    bot = _bot(dialogs)
    audience = asyncio.run(ads._collect_private_audience(bot))
    assert [user_id for user_id, _ in audience] == [11, 22]


def test_non_owner_gets_nothing():
    bot = _bot([_dialog(11)])
    event = FakeEvent("ارسال تبلیغات", user_id=12345)
    assert asyncio.run(
        ads.handle_private_ads(bot, event, 12345, "ارسال تبلیغات")) is False
    assert event.replies == []
    assert bot.client.sent == []


def test_full_workflow_sends_to_private_users_only():
    dialogs = [_dialog(11), _dialog(22), _dialog(-100123, is_user=False)]
    bot = _bot(dialogs, fail_ids={22})
    entity = SimpleNamespace(type="Bold", offset=0, length=2)

    async def scenario():
        # ۱) دستور ← پرامپت
        event = FakeEvent("ارسال تبلیغات")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "ارسال تبلیغات") is True
        assert event.replies[-1][0] == ads.PROMPT

        # ۲) متن (با قالب‌بندی) ← پیش‌نمایش با شمارش مخاطبان
        event = FakeEvent("تخفیف ویژه امروز")
        event.message.entities = [entity]
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "تخفیف ویژه امروز") is True
        preview_text, preview_entities = event.replies[-1]
        assert "پیش‌نمایش تبلیغ" in preview_text
        assert "مخاطبان پیوی: 2" in preview_text
        assert "تخفیف ویژه امروز" in preview_text
        # entity بدنه باید بعد از سربرگ پیش‌نمایش قرار گرفته باشد.
        assert preview_entities and preview_entities[0].offset > 10

        # ۳) تایید ← ارسال به پیوی‌ها + گزارش (۲۲ مسدود کرده: ناموفق)
        event = FakeEvent("تایید")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "تایید") is True
        assert [user_id for user_id, _, _ in bot.client.sent] == [11]
        assert bot.client.sent[0][1] == "تخفیف ویژه امروز"
        assert bot.client.sent[0][2] == [entity]
        summary = event.replies[-1][0]
        assert "مخاطبان پیوی: 2" in summary
        assert "ارسال موفق: 1" in summary
        assert "ارسال ناموفق: 1" in summary
        assert not ads.has_ads_session(OWNER_ID)

    asyncio.run(scenario())


def test_cancel_and_text_before_trigger():
    bot = _bot([_dialog(11)])

    async def scenario():
        # بدون دستور، متن عادی مالک وارد workflow نمی‌شود.
        event = FakeEvent("سلام")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "سلام") is False

        event = FakeEvent("ارسال تبلیغات")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "ارسال تبلیغات") is True
        # تایید پیش از متن: راهنمایی می‌شود، چیزی ارسال نمی‌شود.
        event = FakeEvent("تایید")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "تایید") is True
        assert "ابتدا متن تبلیغ را ارسال کنید" in event.replies[-1][0]

        event = FakeEvent("متن تبلیغ")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "متن تبلیغ") is True
        event = FakeEvent("لغو")
        assert await ads.handle_private_ads(
            bot, event, OWNER_ID, "لغو") is True
        assert "لغو شد" in event.replies[-1][0]
        assert bot.client.sent == []
        assert not ads.has_ads_session(OWNER_ID)

    asyncio.run(scenario())


def test_group_start_uses_same_workflow():
    bot = _bot([_dialog(11)])
    group_chat = -100555

    async def scenario():
        event = FakeEvent("ارسال تبلیغات", chat_id=group_chat)
        assert await ads.handle_group_ads(
            bot, event, OWNER_ID, "ارسال تبلیغات") is True
        # بدنه از گروه دیگر پذیرفته نمی‌شود.
        other = FakeEvent("متن از گروه دیگر", chat_id=-100666)
        assert await ads.handle_private_ads(
            bot, other, OWNER_ID, "متن از گروه دیگر") is False
        event = FakeEvent("تبلیغ گروهی", chat_id=group_chat)
        assert await ads.handle_group_ads(
            bot, event, OWNER_ID, "تبلیغ گروهی") is True
        event = FakeEvent("تایید", chat_id=group_chat)
        assert await ads.handle_group_ads(
            bot, event, OWNER_ID, "تایید") is True
        assert [user_id for user_id, _, _ in bot.client.sent] == [11]

    asyncio.run(scenario())
