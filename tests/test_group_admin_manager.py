# -*- coding: utf-8 -*-
"""Native group-admin workflow against the real SPlusthon 1.1.4 TL classes.

No account credentials/server are present: the fake transports actual
EditAdminRequest/GetParticipantRequest objects and models their group-scoped
results.  These tests must not be described as live Soroush server tests.
"""
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from splusthon.tl import functions, types

from handlers import group_admin_handler as handler
from handlers.message_handler import match_reserved_command
from modules import admin_storage, group_admin_manager as native
from modules.admin_help_builder import (ADMIN_HELP_ENTITIES, ADMIN_HELP_MESSAGES,
                                        ADMIN_HELP_TEXT, MAX_QUOTES_PER_MESSAGE)
from modules.group_dispatch import LANE_ADMIN, classify_priority
from modules.group_id import normalize_group_id


def run(coro):
    return asyncio.run(coro)


class User:
    def __init__(self, user_id, name="سارا", username=None):
        self.id = user_id
        self.first_name = name
        self.last_name = ""
        self.username = username
        self.access_hash = user_id * 13


class Logger:
    def __init__(self):
        self.errors = []

    def log_info(self, *args):
        pass

    def log_error(self, message):
        self.errors.append(message)


class RepliedMessage:
    def __init__(self, sender):
        self.sender = sender

    async def get_sender(self):
        return self.sender


class Client:
    def __init__(self, bot_id=999):
        self.bot_id = bot_id
        self.group_ids = (1234, 5678)
        self.roles = {}
        self.replied = {}
        self.reads = []
        self.edits = []
        self.lookups = []
        self.ignore_rank = False
        self.fail_write = False
        self.no_read_after_write = False
        self.always_fail_read = False
        self.members = {}
        for group in self.group_ids:
            self.roles[group, bot_id] = types.ChannelParticipantAdmin(
                user_id=bot_id, promoted_by=bot_id, date=None,
                admin_rights=types.ChatAdminRights(add_admins=True), rank="bot")

    def add_user(self, group, user, message_id=4):
        self.members[user.id] = user
        self.roles[group, user.id] = types.ChannelParticipant(
            user_id=user.id, date=None)
        self.replied[group, message_id] = user

    def seed_admin(self, group, user, rank="پیشین", message_id=4):
        self.add_user(group, user, message_id=message_id)
        self.roles[group, user.id] = types.ChannelParticipantAdmin(
            user_id=user.id, promoted_by=self.bot_id, date=None,
            admin_rights=types.ChatAdminRights(change_info=True), rank=rank)

    async def get_messages(self, group, ids):
        # Never resolve a normalized positive short group ID as a user.
        assert isinstance(group, types.InputPeerChannel) or int(group) < 0
        gid = group.channel_id if hasattr(group, "channel_id") else \
            int(normalize_group_id(group))
        self.lookups.append((gid, ids))
        user = self.replied.get((gid, ids))
        return RepliedMessage(user) if user else None

    async def get_me(self):
        return self.members.get(self.bot_id, User(self.bot_id, "ربات"))

    async def get_input_entity(self, entity):
        if isinstance(entity, (types.InputPeerUser, types.InputPeerChannel)):
            return entity
        if isinstance(entity, (types.InputUser, types.User)):
            return types.InputPeerUser(entity.user_id, entity.access_hash)
        if isinstance(entity, User):
            return types.InputPeerUser(entity.id, entity.access_hash)
        if isinstance(entity, int):
            if entity < 0:
                return types.InputPeerChannel(int(normalize_group_id(entity)), 17)
            return types.InputPeerUser(entity, entity * 13)
        raise ValueError("uncached entity")

    async def __call__(self, request):
        if isinstance(request, functions.channels.GetParticipantRequest):
            group = request.channel.channel_id
            user_id = request.participant.user_id
            self.reads.append((group, user_id))
            if self.always_fail_read or self.no_read_after_write and self.edits:
                raise RuntimeError("GetParticipant unavailable")
            return SimpleNamespace(participant=self.roles[group, user_id])
        assert isinstance(request, functions.channels.EditAdminRequest)
        self.edits.append(request)
        if self.fail_write:
            raise RuntimeError("CHAT_ADMIN_REQUIRED")
        group = request.channel.channel_id
        user_id = request.user_id.user_id
        values = [getattr(request.admin_rights, name)
                  for name in inspect.signature(types.ChatAdminRights.__init__).parameters
                  if name != "self"]
        if any(bool(value) for value in values):
            self.roles[group, user_id] = types.ChannelParticipantAdmin(
                user_id=user_id, promoted_by=self.bot_id, date=None,
                admin_rights=request.admin_rights,
                rank=None if self.ignore_rank else request.rank)
        else:
            self.roles[group, user_id] = types.ChannelParticipant(
                user_id=user_id, date=None)
        return SimpleNamespace(ok=True)


class Event:
    def __init__(self, text, user_id, chat_id=1234, *, reply_id=None,
                 private=False, is_group=True, edit=False):
        self.chat_id = chat_id
        self.sender_id = user_id
        self.is_private = private
        self.is_group = is_group
        self.is_edit = edit
        self.sender = User(user_id, "صاحب")
        self.reply_to = SimpleNamespace(reply_to_msg_id=reply_id) if reply_id else None
        self.message = SimpleNamespace(message=text, reply_to=self.reply_to,
                                       edit_date=None)
        self.sent = []

    async def reply(self, text, **kwargs):
        self.sent.append((text, kwargs.get("formatting_entities", [])))
        return SimpleNamespace(id=98765)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    handler.reset_memory()
    monkeypatch.setattr(native, "VERIFY_DELAYS", (0.0,))
    owners = {1234: 111, 5678: 222}
    monkeypatch.setattr(native, "get_group_owner",
                        lambda chat: owners.get(int(normalize_group_id(chat))))
    client = Client()
    bot = SimpleNamespace(
        client=client, bot_account_id=999, logger=Logger(),
        reply_input_peer_cache={
            1234: types.InputPeerChannel(1234, 17),
            5678: types.InputPeerChannel(5678, 29),
        },
    )
    client._outgoing_sender_bot = bot
    # An existing bot-admin registration must remain byte-for-byte untouched.
    bot_admins = tmp_path / "admins.json"
    bot_admins.write_text('{"1234": [{"user_id": "101"}]}', encoding="utf-8")
    monkeypatch.setattr(admin_storage, "FILE", bot_admins)
    monkeypatch.setattr(admin_storage, "_cache", None)
    monkeypatch.setattr(admin_storage, "_cache_mtime", None)
    yield bot, owners, bot_admins
    handler.reset_memory()


def send(bot, text, actor=111, group=1234, reply_id=None, **kwargs):
    event = Event(text, actor, group, reply_id=reply_id, **kwargs)
    assert run(handler.handle(bot, event, group, actor, event.sender, text)) is True
    return event


def test_real_splusthon_schema_and_all_permission_combinations(setup):
    bot, owners, bot_admins = setup
    client = bot.client
    names = set(inspect.signature(types.ChatAdminRights.__init__).parameters) - {"self"}
    assert {flag for _number, _label, flag in native.PERMISSIONS} <= names
    assert len(native.PERMISSIONS) == 6
    assert tuple(native.DEFAULT_CHOICES.values()) == (True, True, True, True, True, False)
    assert native.functions.channels.EditAdminRequest is functions.channels.EditAdminRequest
    for mask in range(1, 64):
        user = User(10_000 + mask)
        client.add_user(1234, user)
        choices = {n: bool(mask & (1 << (n - 1))) for n in range(1, 7)}
        title = f"مدیر رویا {mask} 🦊"
        assert run(native.promote(bot, 1234, 111, user, title, choices)) == user.id
        req = client.edits[-1]
        assert isinstance(req, functions.channels.EditAdminRequest)
        assert req.channel.channel_id == 1234
        assert req.user_id.user_id == user.id
        assert req.rank == title
        for number, _label, flag in native.PERMISSIONS:
            assert getattr(req.admin_rights, flag) is choices[number]
        for flag in names - {row[2] for row in native.PERMISSIONS}:
            assert getattr(req.admin_rights, flag) is False
        assert client.roles[1234, user.id].rank == title
    with pytest.raises(native.GroupAdminError, match="حداقل یکی"):
        native.build_rights({n: False for n in range(1, 7)})
    assert bot_admins.read_text(encoding="utf-8") == \
        '{"1234": [{"user_id": "101"}]}'


def test_reply_title_six_rights_confirmation_and_no_early_write(setup):
    bot, _owners, bot_admins = setup
    target = User(101, "کاربر 🦊", username="Cowe")
    bot.client.add_user(1234, target)
    prompt = send(bot, "ادمین گروه", reply_id=4)
    assert "لقب مدیر" in prompt.sent[-1][0]
    assert bot.client.edits == []
    board = send(bot, "مدیر آزادِ نمونه 🦊")
    assert "Cowe" in board.sent[-1][0]
    assert "6_ افزودن مدیر جدید" in board.sent[-1][0]
    assert bot.client.edits == []
    changed = send(bot, "۱ و ۲")
    assert "1 - 2 خاموش" in changed.sent[0][0]
    assert len(bot.client.edits) == 0
    toggled = send(bot, "روشن ۶")
    assert "6 روشن" in toggled.sent[0][0]
    assert len(bot.client.edits) == 0
    done = send(bot, handler.CONFIRM)
    assert "ادمین گروه شد" in done.sent[-1][0]
    request = bot.client.edits[-1]
    assert request.rank == "مدیر آزادِ نمونه 🦊"
    expected = {1: False, 2: False, 3: True, 4: True, 5: True, 6: True}
    assert all(getattr(request.admin_rights, flag) is expected[n]
               for n, _label, flag in native.PERMISSIONS)
    assert bot_admins.read_text(encoding="utf-8") == \
        '{"1234": [{"user_id": "101"}]}'
    assert not handler.is_awaiting(1234, 111)


def test_demote_only_replied_user_in_this_group_and_preserve_bot_admins(setup):
    bot, owners, bot_admins = setup
    target, other = User(101), User(202)
    bot.client.seed_admin(1234, target)
    bot.client.seed_admin(5678, target)
    bot.client.seed_admin(1234, other, message_id=5)
    empty = send(bot, "لغو ادمین گروه")
    assert "ریپلای" in empty.sent[-1][0]
    assert bot.client.edits == []
    done = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "از ادمینی گروه لغو شد" in done.sent[-1][0]
    request = bot.client.edits[-1]
    assert request.channel.channel_id == 1234
    assert request.user_id.user_id == 101
    assert request.rank == ""
    assert all(getattr(request.admin_rights, flag) is None
               for flag in inspect.signature(types.ChatAdminRights.__init__).parameters
               if flag != "self")
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipant)
    assert isinstance(bot.client.roles[5678, 101], types.ChannelParticipantAdmin)
    assert isinstance(bot.client.roles[1234, 202], types.ChannelParticipantAdmin)
    assert bot_admins.read_text(encoding="utf-8") == \
        '{"1234": [{"user_id": "101"}]}'


def test_only_registered_owner_and_only_group_not_global_or_bot_admin(setup):
    bot, owners, _bot_admins = setup
    bot.client.add_user(1234, User(101))
    # Even a registered bot admin (101) or the global bot owner (333) is not
    # a substitute for owner_id=111 in groups.json.
    for actor in (101, 333, 222):
        for cmd in ("ادمین گروه", "لغو ادمین گروه"):
            denied = send(bot, cmd, actor=actor, reply_id=4)
            assert "فقط مالک ثبت‌شدهٔ همین گروه" in denied.sent[-1][0]
    assert bot.client.edits == []
    pv = send(bot, "ادمین گروه", private=True, is_group=False, reply_id=4)
    assert "فقط در گروه" in pv.sent[-1][0]
    broadcast = send(bot, "لغو ادمین گروه", private=False, is_group=False,
                     reply_id=4)
    assert "فقط در گروه" in broadcast.sent[-1][0]
    assert bot.client.edits == []
    assert not handler.is_awaiting(1234, 111)


def test_corrupt_or_missing_owner_fails_closed_without_rpc(setup, monkeypatch):
    bot, owners, _ = setup
    bot.client.add_user(1234, User(101))
    owners[1234] = 0
    assert not native.is_registered_group_owner(1234, 0)
    denied = send(bot, "ادمین گروه", actor=0, reply_id=4)
    assert "فقط مالک ثبت‌شدهٔ همین گروه" in denied.sent[-1][0]
    owners[1234] = None
    assert not native.is_registered_group_owner(1234, 111)
    monkeypatch.setattr(native, "get_group_owner",
                        lambda _chat: (_ for _ in ()).throw(ValueError("corrupt")))
    denied = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "فقط مالک ثبت‌شدهٔ همین گروه" in denied.sent[-1][0]
    assert not bot.client.edits


def test_owner_change_during_workflow_revokes_permission(setup):
    bot, owners, _ = setup
    bot.client.add_user(1234, User(101))
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "مدیر")
    owners[1234] = 333
    event = send(bot, handler.CONFIRM)
    assert "فقط مالک ثبت‌شدهٔ همین گروه" in event.sent[-1][0]
    assert not bot.client.edits
    assert not handler.is_awaiting(1234, 111)


def test_rank_missing_is_rolled_back_without_false_success(setup):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    bot.client.ignore_rank = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب دلخواه")
    result = send(bot, handler.CONFIRM)
    assert "ارتقا لغو شد" in result.sent[-1][0]
    assert "ادمین گروه شد" not in result.sent[-1][0]
    assert len(bot.client.edits) == 2
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipant)


def test_api_failure_and_unconfirmed_reply_are_not_success(setup):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    bot.client.fail_write = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    failed = send(bot, handler.CONFIRM)
    assert "درخواست ادمین‌کردن را نپذیرفت" in failed.sent[-1][0]
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipant)
    bot.client.fail_write = False
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب ۲")
    bot.client.edits.clear()
    bot.client.no_read_after_write = True
    unknown = send(bot, handler.CONFIRM)
    assert "موفقیت اعلام نشد" in unknown.sent[-1][0]
    assert "ادمین گروه شد" not in unknown.sent[-1][0]


def test_existing_admin_creator_self_and_missing_bot_rights_fail_closed(setup):
    bot, owners, _ = setup
    bot.client.seed_admin(1234, User(101))
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب تازه")
    already = send(bot, handler.CONFIRM)
    assert "از قبل ادمین" in already.sent[-1][0]
    assert not bot.client.edits
    bot.client.roles[1234, 101] = types.ChannelParticipantCreator(
        user_id=101, admin_rights=types.ChatAdminRights(add_admins=True), rank=None)
    creator = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "مالک اصلی گروه" in creator.sent[-1][0]
    assert not bot.client.edits
    bot.client.roles[1234, 101] = types.ChannelParticipant(user_id=101, date=None)
    bot.client.roles[1234, 999] = types.ChannelParticipantAdmin(
        user_id=999, promoted_by=999, date=None,
        admin_rights=types.ChatAdminRights(add_admins=False), rank="bot")
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    no_right = send(bot, handler.CONFIRM)
    assert "دسترسی «افزودن مدیر جدید» ندارد" in no_right.sent[-1][0]
    assert not bot.client.edits
    owners[1234] = 101
    no_owner_target = send(bot, "ادمین گروه", actor=101, reply_id=4)
    send(bot, "لقب", actor=101)
    denied = send(bot, handler.CONFIRM, actor=101)
    assert "مالک ثبت‌شدهٔ گروه" in denied.sent[-1][0]
    assert not bot.client.edits


def test_cancel_expiry_edit_and_old_command_do_not_mutate(setup, monkeypatch):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    start = send(bot, "ادمین گروه", reply_id=4)
    assert handler.is_awaiting(1234, 111)
    # The old bot-admin command must pass through the new module untouched.
    old = Event("لغو ادمین", 111, reply_id=4)
    assert run(handler.handle(bot, old, 1234, 111, old.sender,
                              old.message.message)) is False
    assert handler.is_awaiting(1234, 111)
    assert not bot.client.edits
    assert run(handler.handle(bot, Event("مدیر", 111, edit=True),
                              1234, 111, None, "مدیر")) is True
    assert handler._FLOWS[handler._key(1234, 111)].phase == "rank"
    monkeypatch.setattr(handler, "FLOW_TTL", -1)
    assert not handler.is_awaiting(1234, 111)
    monkeypatch.setattr(handler, "FLOW_TTL", 300.0)
    send(bot, "ادمین گروه", reply_id=4)
    cancelled = send(bot, handler.CANCEL)
    assert "تغییری اعمال نشد" in cancelled.sent[-1][0]
    assert not bot.client.edits
    assert handler.is_command("لغو  ادمين\u200cگروه")
    assert classify_priority("ادمین گروه")[1] == LANE_ADMIN
    assert classify_priority("لغو ادمین گروه")[1] == LANE_ADMIN
    # The pre-existing matcher accepts a prefix; the independent hook in
    # message_handler runs first so this never removes an admins.json entry.
    assert match_reserved_command("لغو ادمین گروه")[0] == "لغو ادمین"
    source = (Path(__file__).resolve().parents[1] / "handlers/message_handler.py") \
        .read_text(encoding="utf-8")
    assert source.index("native_group_admin.is_command(clean_text)") \
        < source.index('if _reserved_command in {"برکناری ادمین", "لغو ادمین"}')


def test_start_reply_failure_clears_only_its_own_flow(setup, monkeypatch):
    """An error while sending the prompt must not leave a phantom workflow."""
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    monkeypatch.setattr(handler, "MAX_FLOWS", 1)
    previous_key = handler._key(5678, 222)
    handler._FLOWS[previous_key] = handler.Flow(
        target=User(202), target_id=202, label="پیشین")
    event = Event("ادمین گروه", 111, reply_id=4)

    async def broken_reply(*_args, **_kwargs):
        raise RuntimeError("prompt unavailable")

    event.reply = broken_reply
    with pytest.raises(RuntimeError, match="prompt unavailable"):
        run(handler._start(bot, event, 1234, 111))
    assert not handler.is_awaiting(1234, 111)
    assert handler._FLOWS.get(previous_key) is None  # the oldest was evicted


def test_capacity_never_discards_an_inflight_role_change(setup, monkeypatch):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    monkeypatch.setattr(handler, "MAX_FLOWS", 1)
    old_key = handler._key(5678, 222)
    old = handler.Flow(target=User(202), target_id=202,
                       label="در حال اجرا", phase="busy")
    handler._FLOWS[old_key] = old
    attempt = send(bot, "ادمین گروه", reply_id=4)
    assert "در حال انجام" in attempt.sent[-1][0]
    assert handler._FLOWS[old_key] is old
    assert not handler.is_awaiting(1234, 111)


def test_real_private_route_rejects_commands_before_group_dispatch(setup):
    """Drive the core NewMessage entry point: private must get a reply."""
    import test_broadcast_routing as core_fixture

    bot, _owners, _ = setup
    core_fixture._captured.clear()
    private_bot = run(core_fixture._build())
    for command in ("ادمین گروه", "لغو ادمین گروه"):
        event = core_fixture.Event(command, out=False, is_private=True,
                                   sender_id=111, chat_id=111)
        run(core_fixture._captured[-1](event))
        assert event.replies == ["❌ این دستور فقط در گروه قابل استفاده است."]
    assert not handler._FLOWS
    assert not private_bot.client.sent


def test_real_message_pipeline_reaches_independent_handler(setup, monkeypatch):
    """Exercise handle_new_message, not just our direct feature entry point."""
    import test_admin_commands as core_fakes
    from handlers import message_handler as main_handler

    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101, username="Cowe"))
    bot.is_spam_locked = lambda _key: False
    bot.clear_spam_lock = lambda _key: None
    bot.detector = core_fakes.Detector()
    bot.tracker = core_fakes.Tracker()
    bot.config_manager = core_fakes.ConfigManager()
    bot.punished_users = set()
    bot.spam_burst_messages = {}
    bot.spammer_messages = {}
    bot.spam_burst_users = set()
    bot.moderation_queue = SimpleNamespace(enqueue=lambda *a, **kw: True)
    monkeypatch.setattr(main_handler.admin_tools, "get_group_owner",
                        lambda _chat_id: 111)
    monkeypatch.setattr(main_handler, "get_group_owner", lambda _chat_id: 111)

    class PipelineEvent(core_fakes.Event):
        def __init__(self, text, reply=False):
            super().__init__(text, 111, chat_id=1234,
                             reply_sender=core_fakes.User(101) if reply else None)
            self.sender_id = 111
            self.is_group = True
            self._bot_cached_chat = SimpleNamespace(id=1234, megagroup=True)
            self._bot_cached_sender = self._user
            if reply:
                self.reply_to = core_fakes.ReplyTo(4)
                self.message.reply_to = self.reply_to

    for text, reply, expected_edits in (
            ("ادمین گروه", True, 0), ("مدیر آزاد", False, 0),
            ("هیچکدام", False, 0), (handler.CONFIRM, False, 1)):
        event = PipelineEvent(text, reply)
        run(main_handler.handle_new_message(bot, event))
        assert event.replies, (text, bot.logger.errors)
        assert len(bot.client.edits) == expected_edits
    assert bot.client.edits[0].rank == "مدیر آزاد"
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipantAdmin)


def test_exclusive_temporary_admin_cannot_be_overwritten_or_auto_regranted(
        setup, monkeypatch):
    bot, _owners, bot_admins = setup
    from modules.manage_features import exclusive
    bot.client.seed_admin(1234, User(101))
    monkeypatch.setattr(exclusive, "list_entries",
                        lambda chat_id: [{"user_id": "101", "state": "active"}])
    result = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "لغو اختصاصی" in result.sent[-1][0]
    assert not bot.client.edits
    bot.client.roles[1234, 101] = types.ChannelParticipant(user_id=101, date=None)
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "مدیر")
    result = send(bot, handler.CONFIRM)
    assert "لغو اختصاصی" in result.sent[-1][0]
    assert not bot.client.edits
    assert bot_admins.read_text(encoding="utf-8") == \
        '{"1234": [{"user_id": "101"}]}'


def test_pending_input_admin_lane_and_invalid_choices_never_write(setup):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    send(bot, "ادمین گروه", reply_id=4)
    owner_event = Event("مدیر دلخواه", 111)
    other_event = Event("مدیر دلخواه", 333)
    assert classify_priority(owner_event.message.message, owner_event)[1] == LANE_ADMIN
    assert classify_priority(other_event.message.message, other_event)[1] != LANE_ADMIN
    empty = send(bot, "   ")
    assert "غیرخالی" in empty.sent[-1][0]
    send(bot, "مدیر دلخواه")
    invalid = send(bot, "روشن ۷")
    assert "شماره‌های ۱ تا ۶" in invalid.sent[-1][0]
    assert not bot.client.edits
    for n in range(1, 7):
        send(bot, f"خاموش {n}")
    rejected = send(bot, handler.CONFIRM)
    assert "حداقل یکی" in rejected.sent[-1][0]
    assert not bot.client.edits
    send(bot, "روشن ۶")
    success = send(bot, handler.CONFIRM)
    assert "ادمین گروه شد" in success.sent[-1][0]
    assert [bool(getattr(bot.client.edits[-1].admin_rights, flag))
            for _n, _label, flag in native.PERMISSIONS] == \
        [False, False, False, False, False, True]


def _slice_entity(text, entity):
    raw = text.encode("utf-16-le")
    return raw[2 * entity.offset:2 * (entity.offset + entity.length)] \
        .decode("utf-16-le")


def test_interactive_board_uses_real_entities_and_utf16_offsets(setup):
    from modules import group_admin_texts as texts

    prompt, prompt_entities = texts.title_prompt()
    assert [_slice_entity(prompt, item) for item in prompt_entities
            if isinstance(item, types.MessageEntityBold)] == [prompt]
    board, entities = texts.rights_board("@Cowe 🦊", native.DEFAULT_CHOICES)
    first_line = board.splitlines()[0]
    assert first_line.startswith("🧑‍🌾 کاربر برای ادمینی گروه انتخاب شد")
    assert [_slice_entity(board, item) for item in entities
            if isinstance(item, types.MessageEntityBlockquote)] == [first_line]
    assert [_slice_entity(board, item) for item in entities
            if isinstance(item, types.MessageEntityBold)] == [first_line]
    assert [f"{number}_ {label}" in board for number, label, _ in native.PERMISSIONS] \
        == [True] * 6
    assert "6_ افزودن مدیر جدید     ◐ خاموش" in board
    assert all(item.length > 0 and item.offset >= 0
               and item.offset + item.length <= len(board.encode("utf-16-le")) // 2
               for item in entities)


def test_exact_admin_help_two_real_quotes_and_three_real_bolds(setup):
    title = "🌐 برای اضافه کردن یک کاربر به ادمین های گروه"
    expected = f"{title}\n\nادمین گروه\n\nلغو ادمین گروه"
    assert ADMIN_HELP_TEXT.count(expected) == 1
    assert '"ادمین گروه"' not in ADMIN_HELP_TEXT
    fragment = [chunk for chunk in ADMIN_HELP_MESSAGES if expected in chunk[0]]
    assert len(fragment) == 1  # a block is not split between messages
    text, entities = fragment[0]
    quotes = [_slice_entity(text, entity) for entity in entities
              if isinstance(entity, types.MessageEntityBlockquote)]
    bolds = [_slice_entity(text, entity) for entity in entities
             if isinstance(entity, types.MessageEntityBold)]
    assert quotes == ["ادمین گروه", "لغو ادمین گروه", "تماس با سازنده : @OSINE2"]
    assert title in bolds and "ادمین گروه" in bolds and "لغو ادمین گروه" in bolds
    assert all(sum(isinstance(ent, types.MessageEntityBlockquote) for ent in entities)
               <= MAX_QUOTES_PER_MESSAGE for _, entities in ADMIN_HELP_MESSAGES)
    # Valid UTF-16 spans even when the content contains emoji (🌐).
    assert all(ent.length > 0 and ent.offset >= 0
               and ent.offset + ent.length <= len(text.encode("utf-16-le")) // 2
               for ent in entities)
