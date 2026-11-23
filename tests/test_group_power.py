"""تست‌های کنترل از راه دور گروه‌ها («گروها» / «ربات خاموش|روشن»).

فقط با fakeهای سبک: بدون تلگرام و بدون شبکه. ذخیرهٔ گروه‌ها و انقضا
با monkeypatch روی همان ماژول‌ها کنترل می‌شود و is_global_owner هم
روی مالک تست قفل می‌شود.

اجرا:  python3 -m pytest tests/test_group_power.py -q
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers import group_power_handler as power
from modules import group_expiry, group_storage


class _Logger:
    def __init__(self):
        self.errors = []
        self.infos = []

    def log_error(self, msg):
        self.errors.append(str(msg))

    def log_info(self, msg):
        self.infos.append(str(msg))


class _Client:
    def __init__(self, fail_targets=()):
        self.sent = []
        self.fail_targets = set(fail_targets)

    async def send_message(self, target, text, formatting_entities=None):
        if target in self.fail_targets:
            raise RuntimeError("send failed")
        self.sent.append((target, text, formatting_entities))
        return types.SimpleNamespace(id=99)


class _Message:
    def __init__(self, text, entities=None):
        self.message = text
        self.text = text
        self.entities = entities


class _Event:
    def __init__(self, chat_id, sender_id, text, entities=None):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = _Message(text, entities)
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return types.SimpleNamespace(id=1)


class _Bot:
    def __init__(self, fail_targets=()):
        self.client = _Client(fail_targets)
        self.logger = _Logger()
        self.group_timer_tasks = {}


OWNER = 424242
GID_POS = 1169900590
GID_KEY = str(GID_POS)
OWNER_CHAT = 777001
GROUPS = {
    "1169900590": {"title": "تست", "active": True},
    "2222": {"title": "دوم", "active": False},
    "3333": {"title": "سوم", "active": True},
}


def _setup(monkeypatch, groups=None, expired=()):
    store = {k: dict(v) for k, v in (groups or GROUPS).items()}
    monkeypatch.setattr(group_storage, "load_groups", lambda: store)
    monkeypatch.setattr(
        group_storage, "deactivate_group",
        lambda gid, title: store[str(gid)].update(active=False, title=title),
    )
    activated = []
    monkeypatch.setattr(
        group_storage, "activate_group",
        lambda gid, title: activated.append((gid, title)),
    )
    monkeypatch.setattr(group_expiry, "is_expired", lambda gid: str(gid) in expired)
    monkeypatch.setattr(power, "is_global_owner", lambda uid: uid == OWNER)
    power._sessions.clear()
    return store, activated


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_list_only_active_groups(monkeypatch):
    _setup(monkeypatch)
    bot = _Bot()
    ev = _Event(OWNER_CHAT, OWNER, "گروها")
    handled = _run(power.handle_group_power(bot, ev, OWNER, "گروها"))
    assert handled is True
    reply = ev.replies[-1]
    assert "تست" in reply and GID_KEY in reply
    assert "سوم" in reply and "3333" in reply
    assert "دوم" not in reply  # خاموش‌ها در فهرست نیستند


def test_non_owner_ignored(monkeypatch):
    _setup(monkeypatch)
    ev = _Event(OWNER_CHAT, 999, "گروها")
    assert _run(power.handle_group_power(_Bot(), ev, 999, "گروها")) is False
    assert ev.replies == []


def test_power_command_parsing():
    assert power.parse_power_command("ربات خاموش 123") == ("off", "123")
    assert power.parse_power_command("ربات روشن -1001169900590") == (
        "on", "-1001169900590")
    assert power.parse_power_command("ربات خاموش") == ("off", None)
    assert power.parse_power_command("سلام") is None
    assert power.match_list_command("گروه ها")
    assert power.match_list_command("گروها")


def test_off_flow_sends_reason_then_deactivates(monkeypatch):
    store, _ = _setup(monkeypatch)
    bot = _Bot()
    ev = _Event(OWNER_CHAT, OWNER, f"ربات خاموش {GID_POS}")
    assert _run(power.handle_group_power(
        bot, ev, OWNER, f"ربات خاموش {GID_POS}")) is True
    assert "دلیل" in ev.replies[-1]
    assert store[GID_KEY]["active"] is True  # هنوز خاموش نشده

    reason = _Event(OWNER_CHAT, OWNER, "چون قوانین رعایت نشد")
    assert _run(power.handle_group_power(
        bot, reason, OWNER, "چون قوانین رعایت نشد")) is True
    assert bot.client.sent and bot.client.sent[0][1] == "چون قوانین رعایت نشد"
    assert store[GID_KEY]["active"] is False  # حالا خاموش شد
    assert "خاموش شد" in reason.replies[-1]


def test_off_flow_cancel_keeps_group_active(monkeypatch):
    store, _ = _setup(monkeypatch)
    bot = _Bot()
    _run(power.handle_group_power(
        bot, _Event(OWNER_CHAT, OWNER, f"ربات خاموش {GID_POS}"),
        OWNER, f"ربات خاموش {GID_POS}"))
    ev = _Event(OWNER_CHAT, OWNER, "لغو")
    assert _run(power.handle_group_power(bot, ev, OWNER, "لغو")) is True
    assert store[GID_KEY]["active"] is True
    assert bot.client.sent == []


def test_off_unknown_group_and_missing_id(monkeypatch):
    _setup(monkeypatch)
    bot = _Bot()
    ev = _Event(OWNER_CHAT, OWNER, "ربات خاموش 555")
    assert _run(power.handle_group_power(bot, ev, OWNER, "ربات خاموش 555"))
    assert "پیدا نشد" in ev.replies[-1]
    ev2 = _Event(OWNER_CHAT, OWNER, "ربات خاموش")
    assert _run(power.handle_group_power(bot, ev2, OWNER, "ربات خاموش"))
    assert "شناسه" in ev2.replies[-1]


def test_on_flow_activates_and_expired_note(monkeypatch):
    _, activated = _setup(monkeypatch)
    bot = _Bot()
    ev = _Event(OWNER_CHAT, OWNER, "ربات روشن 2222")
    assert _run(power.handle_group_power(bot, ev, OWNER, "ربات روشن 2222"))
    assert activated and activated[0][0] == "2222"
    assert "روشن شد" in ev.replies[-1]

    _, activated2 = _setup(monkeypatch, expired={"2222"})
    ev2 = _Event(OWNER_CHAT, OWNER, "ربات روشن 2222")
    _run(power.handle_group_power(bot, ev2, OWNER, "ربات روشن 2222"))
    assert activated2
    assert "مهلتش تمام شده" in ev2.replies[-1]


def test_reason_send_failure_still_deactivates(monkeypatch):
    store, _ = _setup(monkeypatch)
    bot = _Bot(fail_targets={GID_POS, -(1000000000000 + GID_POS), -GID_POS})
    _run(power.handle_group_power(
        bot, _Event(OWNER_CHAT, OWNER, f"ربات خاموش {GID_POS}"),
        OWNER, f"ربات خاموش {GID_POS}"))
    ev = _Event(OWNER_CHAT, OWNER, "دلیل تست")
    assert _run(power.handle_group_power(bot, ev, OWNER, "دلیل تست")) is True
    assert store[GID_KEY]["active"] is False  # خاموشی حتمی است
    assert "نرسید" in ev.replies[-1]
