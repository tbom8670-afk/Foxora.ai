# -*- coding: utf-8 -*-
"""Native group-admin workflow against the real SPlusthon 1.1.4 TL classes.

No account credentials/server are present: the fake transports actual
EditAdminRequest/GetParticipantRequest/GetParticipantsRequest TL objects and
models their group-scoped results.  These are not live Soroush server tests.
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
        self.list_reads = []
        self.edits = []
        self.lookups = []
        self.ignore_rank = False
        self.fail_write = False
        self.no_read_after_write = False
        self.always_fail_read = False
        self.direct_unsupported = False
        self.list_override = None
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

    def _list_page(self, request):
        group = request.channel.channel_id
        rows = list(self.roles[group, user_id] for gid, user_id in self.roles
                    if gid == group)
        if isinstance(request.filter, types.ChannelParticipantsAdmins):
            rows = [row for row in rows if isinstance(
                row, (types.ChannelParticipantAdmin,
                      types.ChannelParticipantCreator))]
        page = rows[request.offset:request.offset + request.limit]
        users = [self.members.get(native._role_id(row), User(native._role_id(row)))
                 for row in page]
        return types.channels.ChannelParticipants(
            count=len(rows), participants=page, chats=[], users=users)

    async def __call__(self, request):
        if isinstance(request, functions.channels.GetParticipantRequest):
            group = request.channel.channel_id
            user_id = request.participant.user_id
            self.reads.append((group, user_id))
            if self.direct_unsupported:
                raise RuntimeError("RPCError 422: NOT_SUPPORTED")
            if self.always_fail_read or self.no_read_after_write and self.edits:
                raise RuntimeError("GetParticipant unavailable")
            return SimpleNamespace(participant=self.roles[group, user_id])
        if isinstance(request, functions.channels.GetParticipantsRequest):
            self.list_reads.append(request)
            if self.always_fail_read or self.no_read_after_write and self.edits:
                raise RuntimeError("GetParticipants unavailable")
            if self.list_override is not None:
                return self.list_override(request)
            return self._list_page(request)
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


def test_reply_title_and_one_numbers_message_executes_without_confirmation(setup):
    bot, _owners, bot_admins = setup
    target = User(101, "کاربر 🦊", username="Cowe")
    bot.client.add_user(1234, target)
    prompt = send(bot, "ادمین گروه", reply_id=4)
    assert "لقب مدیر" in prompt.sent[-1][0]
    assert bot.client.edits == []
    board = send(bot, "مدیر آزادِ نمونه 🦊")
    assert "Cowe" in board.sent[-1][0]
    assert "6_ افزودن مدیر جدید" in board.sent[-1][0]
    assert "تایید ادمین گروه" not in board.sent[-1][0]
    assert "روشن 6" not in board.sent[-1][0]
    assert bot.client.edits == []
    done = send(bot, "۱ و ۲")
    assert len(done.sent) == 1  # no intermediate changed/board messages
    assert "ادمین گروه شد" in done.sent[-1][0]
    assert len(bot.client.edits) == 1
    request = bot.client.edits[-1]
    assert request.rank == "مدیر آزادِ نمونه 🦊"
    expected = {1: False, 2: False, 3: True, 4: True, 5: True, 6: False}
    assert all(getattr(request.admin_rights, flag) is expected[n]
               for n, _label, flag in native.PERMISSIONS)
    assert bot_admins.read_text(encoding="utf-8") == \
        '{"1234": [{"user_id": "101"}]}'
    assert not handler.is_awaiting(1234, 111)
    late = Event(handler.CONFIRM, 111)
    assert run(handler.handle(bot, late, 1234, 111, late.sender,
                              late.message.message)) is False
    assert len(bot.client.edits) == 1


def test_none_and_selection_of_already_off_six_execute_defaults(setup):
    bot, _owners, _ = setup
    for answer, target_id in (("هیچکدام", 101), ("۶", 202)):
        target = User(target_id)
        bot.client.add_user(1234, target, message_id=target_id)
        send(bot, "ادمین گروه", reply_id=target_id)
        send(bot, "لقب")
        done = send(bot, answer)
        assert len(done.sent) == 1 and "ادمین گروه شد" in done.sent[0][0]
        assert [bool(getattr(bot.client.edits[-1].admin_rights, flag))
                for _n, _label, flag in native.PERMISSIONS] == \
            [True, True, True, True, True, False]
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
    event = send(bot, "هیچکدام")
    assert "فقط مالک ثبت‌شدهٔ همین گروه" in event.sent[-1][0]
    assert not bot.client.edits
    assert not handler.is_awaiting(1234, 111)


def test_missing_rank_reports_observed_admin_without_claiming_title(setup):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    bot.client.ignore_rank = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب دلخواه")
    result = send(bot, "هیچکدام")
    assert result.sent[-1][0] == (
        "⚙ کاربر : سارا\nادمین گروه شد مدیریت گروه را بررسی کنید")
    assert "لقب دلخواه" not in result.sent[-1][0]
    assert len(bot.client.edits) == 1
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipantAdmin)
    assert bot.client.roles[1234, 101].rank is None


def test_api_failure_and_unconfirmed_reply_are_not_success(setup):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    bot.client.fail_write = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    failed = send(bot, "هیچکدام")
    assert "درخواست ادمین‌کردن را نپذیرفت" in failed.sent[-1][0]
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipant)
    bot.client.fail_write = False
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب ۲")
    bot.client.edits.clear()
    bot.client.no_read_after_write = True
    unknown = send(bot, "هیچکدام")
    assert "موفقیت اعلام نشد" in unknown.sent[-1][0]
    assert "ادمین گروه شد" not in unknown.sent[-1][0]


def test_existing_admin_creator_self_and_missing_bot_rights_fail_closed(setup):
    bot, owners, _ = setup
    bot.client.seed_admin(1234, User(101))
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب تازه")
    already = send(bot, "۱")
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
    no_right = send(bot, "هیچکدام")
    assert "دسترسی «افزودن مدیر جدید» ندارد" in no_right.sent[-1][0]
    assert not bot.client.edits
    owners[1234] = 101
    no_owner_target = send(bot, "ادمین گروه", actor=101, reply_id=4)
    send(bot, "لقب", actor=101)
    denied = send(bot, "هیچکدام", actor=101)
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


def test_one_choice_is_not_applied_twice_when_messages_overlap(setup, monkeypatch):
    bot, _owners, _ = setup
    bot.client.add_user(1234, User(101))
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    calls = []

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_promote(*args):
            calls.append(args)
            started.set()
            await release.wait()
            return 101

        monkeypatch.setattr(native, "promote", slow_promote)
        first = Event("۱", 111)
        task = asyncio.create_task(handler.handle(
            bot, first, 1234, 111, first.sender, first.message.message))
        await started.wait()
        second = Event("هیچکدام", 111)
        assert await handler.handle(bot, second, 1234, 111, second.sender,
                                    second.message.message) is True
        assert "در حال بررسی" in second.sent[-1][0]
        assert len(calls) == 1
        release.set()
        await task
        assert "ادمین گروه شد" in first.sent[-1][0]

    run(scenario())
    assert not handler.is_awaiting(1234, 111)


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
            ("۱ و ۲", False, 1)):
        event = PipelineEvent(text, reply)
        run(main_handler.handle_new_message(bot, event))
        assert event.replies, (text, bot.logger.errors)
        assert len(bot.client.edits) == expected_edits
    assert bot.client.edits[0].rank == "مدیر آزاد"
    assert bot.client.edits[0].admin_rights.change_info is False
    assert bot.client.edits[0].admin_rights.delete_messages is False
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipantAdmin)

    # The actual group message route sends precisely the two original help
    # messages: the new block and contact are in the SECOND, not a third.
    help_event = PipelineEvent("لیست ادمینی")
    run(main_handler.handle_new_message(bot, help_event))
    assert len(help_event.replies) == 2, (help_event.replies, bot.logger.errors)
    assert "🌡 برای روشن کردن حالت بحرانی" in help_event.replies[1]
    assert "🌐 برای اضافه کردن یک کاربر به ادمین های گروه" in help_event.replies[1]
    assert help_event.replies[1].endswith("تماس با سازنده : @OSINE2")


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
    result = send(bot, "هیچکدام")
    assert "لغو اختصاصی" in result.sent[-1][0]
    assert not bot.client.edits
    assert bot_admins.read_text(encoding="utf-8") == \
        '{"1234": [{"user_id": "101"}]}'


@pytest.mark.parametrize("exclusive_state", ("active", "pending_grant",
                                             "pending_revoke"))
def test_exclusive_active_and_pending_records_block_native_writes(
        setup, monkeypatch, exclusive_state):
    bot, _owners, _ = setup
    from modules.manage_features import exclusive
    bot.client.direct_unsupported = True
    bot.client.add_user(1234, User(101))
    monkeypatch.setattr(exclusive, "list_entries", lambda _chat: [
        {"user_id": "101", "state": exclusive_state}])
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    refused = send(bot, "هیچکدام")
    assert "لغو اختصاصی" in refused.sent[-1][0]
    assert not bot.client.edits and not bot.client.list_reads


def test_exclusive_starting_during_role_checks_blocks_late_write(
        setup, monkeypatch):
    bot, _owners, _ = setup
    from modules.manage_features import exclusive
    bot.client.add_user(1234, User(101))
    calls = []

    def changing_entries(_chat):
        calls.append(1)
        return [] if len(calls) == 1 else [
            {"user_id": "101", "state": "pending_grant"}]

    monkeypatch.setattr(exclusive, "list_entries", changing_entries)
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    refused = send(bot, "هیچکدام")
    assert len(calls) == 2 and "لغو اختصاصی" in refused.sent[-1][0]
    assert not bot.client.edits


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
    for invalid_text in ("روشن ۷", "روشن ۶", handler.CONFIRM, "۷", "1, 7"):
        invalid = send(bot, invalid_text)
        assert "شماره‌های ۱ تا ۶" in invalid.sent[-1][0]
        assert not bot.client.edits
    rejected = send(bot, "۱ و ۲ و ۳ و ۴ و ۵ و ۶")
    assert "حداقل یکی" in rejected.sent[-1][0]
    assert not bot.client.edits
    assert handler.is_awaiting(1234, 111)
    success = send(bot, "خاموش ۱ و ۳")
    assert "ادمین گروه شد" in success.sent[-1][0]
    assert [bool(getattr(bot.client.edits[-1].admin_rights, flag))
            for _n, _label, flag in native.PERMISSIONS] == \
        [False, True, False, True, True, False]


def test_422_not_supported_uses_fresh_tl_lists_for_promote_and_demote(setup):
    bot, _owners, _ = setup
    target = User(101)
    bot.client.add_user(1234, target)
    bot.client.direct_unsupported = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب نیو")
    promoted = send(bot, "۱ و ۲")
    assert len(promoted.sent) == 1 and "ادمین گروه شد" in promoted.sent[0][0]
    assert bot.client.roles[1234, 101].rank == "لقب نیو"
    assert len(bot.client.edits) == 1
    assert {type(req.filter) for req in bot.client.list_reads} == {
        types.ChannelParticipantsAdmins, types.ChannelParticipantsRecent}
    assert all(req.hash == 0 and req.limit == native.PARTICIPANT_PAGE_SIZE
               and req.channel.channel_id == 1234
               for req in bot.client.list_reads)
    assert (1234, 101) in bot.client.reads and (1234, 999) in bot.client.reads
    demoted = send(bot, "لغو ادمین گروه", reply_id=4)
    assert len(demoted.sent) == 1 and "از ادمینی گروه لغو شد" in demoted.sent[0][0]
    assert isinstance(bot.client.roles[1234, 101], types.ChannelParticipant)
    assert len(bot.client.edits) == 2
    # The screenshot's «نیو» + «هیچکدام» path also works when direct read
    # responds NOT_SUPPORTED for target, bot, and post-write verification.
    bot.client.add_user(1234, User(202), message_id=5)
    send(bot, "ادمین گروه", reply_id=5)
    send(bot, "نیو")
    default = send(bot, "هیچکدام")
    assert len(default.sent) == 1 and "ادمین گروه شد" in default.sent[0][0]
    assert bot.client.edits[-1].rank == "نیو"
    assert [bool(getattr(bot.client.edits[-1].admin_rights, flag))
            for _n, _label, flag in native.PERMISSIONS] == \
        [True, True, True, True, True, False]


def test_fallback_identifies_admin_by_role_id_not_user_array_order(setup):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.seed_admin(1234, User(101), rank="لقب قبلی")
    client.seed_admin(1234, User(202), rank="ادمین دیگر", message_id=5)

    def reordered(req):
        page = client._list_page(req)
        return types.channels.ChannelParticipants(
            count=page.count, participants=page.participants,
            chats=[], users=list(reversed(page.users)))

    client.list_override = reordered
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب تازه")
    already = send(bot, "هیچکدام")
    assert "از قبل ادمین" in already.sent[-1][0]
    assert not client.edits
    assert client.roles[1234, 101].rank == "لقب قبلی"
    done = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "از ادمینی گروه لغو شد" in done.sent[-1][0]
    assert client.edits[-1].user_id.user_id == 101
    assert client.roles[1234, 202].rank == "ادمین دیگر"


def test_truncated_admin_page_never_overwrites_or_demotes_existing_admin(setup):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    for user_id in range(1000, 1210):
        client.seed_admin(1234, User(user_id), message_id=user_id)
    client.seed_admin(1234, User(101), rank="لقب قبلی")  # beyond page 0

    def truncated(req):
        if isinstance(req.filter, types.ChannelParticipantsAdmins) \
                and req.offset >= native.PARTICIPANT_PAGE_SIZE:
            raise RuntimeError("second page unavailable")
        return client._list_page(req)

    client.list_override = truncated
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب تازه")
    refused = send(bot, "هیچکدام")
    assert "وضعیت فعلی کاربر" in refused.sent[-1][0]
    refused = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "وضعیت فعلی کاربر" in refused.sent[-1][0]
    assert not client.edits
    assert client.roles[1234, 101].rank == "لقب قبلی"
    assert any(req.offset == 200 for req in client.list_reads)
    client.list_override = None
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب تازه")
    already = send(bot, "هیچکدام")
    assert "از قبل ادمین" in already.sent[-1][0]
    assert not client.edits
    done = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "از ادمینی گروه لغو شد" in done.sent[-1][0]
    assert client.edits[-1].user_id.user_id == 101


@pytest.mark.parametrize("recent", ("users_only", "empty", "capped"))
def test_replied_user_can_be_promoted_when_recent_omits_them(
        setup, monkeypatch, recent):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.add_user(1234, User(101))
    monkeypatch.setattr(native, "MAX_MEMBER_PAGES", 1)

    def incomplete(req):
        if isinstance(req.filter, types.ChannelParticipantsAdmins):
            return client._list_page(req)
        if recent == "empty":
            return types.channels.ChannelParticipants(
                count=0, participants=[], chats=[], users=[])
        if recent == "users_only":
            return types.channels.ChannelParticipants(
                count=1, participants=[], chats=[], users=[client.members[101]])
        return types.channels.ChannelParticipants(
            count=201, participants=[types.ChannelParticipant(
                user_id=2000 + i, date=None) for i in range(200)],
            chats=[], users=[User(2000 + i) for i in range(200)])

    client.list_override = incomplete
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    promoted = send(bot, "هیچکدام")
    assert promoted.sent[-1][0] == (
        "⚙ کاربر : سارا\nادمین گروه شد مدیریت گروه را بررسی کنید")
    assert client.lookups == [(1234, 4)]  # resolved from this group's reply
    assert len(client.edits) == 1
    assert client.edits[0].user_id.user_id == 101
    assert isinstance(client.roles[1234, 101], types.ChannelParticipantAdmin)


def test_recent_absence_does_not_mask_server_rejection(setup):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.fail_write = True
    client.add_user(1234, User(101))

    def no_recent(req):
        if isinstance(req.filter, types.ChannelParticipantsRecent):
            return types.channels.ChannelParticipants(
                count=0, participants=[], chats=[], users=[])
        return client._list_page(req)

    client.list_override = no_recent
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    result = send(bot, "هیچکدام")
    assert "درخواست ادمین‌کردن را نپذیرفت" in result.sent[-1][0]
    assert "ادمین گروه شد" not in result.sent[-1][0]
    assert len(client.edits) == 1  # sent to the server, but not accepted
    assert isinstance(client.roles[1234, 101], types.ChannelParticipant)


def test_recent_absence_and_stale_admin_list_are_not_false_success(setup):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.add_user(1234, User(101))

    def stale(req):
        if isinstance(req.filter, types.ChannelParticipantsAdmins):
            return types.channels.ChannelParticipants(
                count=1, participants=[client.roles[1234, 999]],
                chats=[], users=[User(999)])
        return types.channels.ChannelParticipants(
            count=0, participants=[], chats=[], users=[])

    client.list_override = stale
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    result = send(bot, "هیچکدام")
    assert "ادمینی و لقب در سرور تأیید نشد" in result.sent[-1][0]
    assert "ادمین گروه شد" not in result.sent[-1][0]
    assert len(client.edits) == 1  # server fake applied it, but read-back hid it
    assert isinstance(client.roles[1234, 101], types.ChannelParticipantAdmin)


@pytest.mark.parametrize("left_or_banned", ("left", "banned"))
def test_explicit_nonmember_role_never_qualifies_for_promotion(
        setup, left_or_banned):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.add_user(1234, User(101))
    peer = types.PeerUser(user_id=101)
    client.roles[1234, 101] = (
        types.ChannelParticipantLeft(peer=peer) if left_or_banned == "left"
        else types.ChannelParticipantBanned(
            peer=peer, kicked_by=999, date=None,
            banned_rights=types.ChatBannedRights(until_date=None)))
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    refused = send(bot, "هیچکدام")
    assert "عضو عادی گروه نیست" in refused.sent[-1][0]
    assert not client.edits


@pytest.mark.parametrize("admin_page", ("missing_roles", "not_modified", "bad_count"))
def test_malformed_admin_page_fails_closed_for_both_commands(
        setup, admin_page):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.seed_admin(1234, User(101), rank="قبلی")

    def broken(req):
        if isinstance(req.filter, types.ChannelParticipantsAdmins):
            if admin_page == "not_modified":
                return types.channels.ChannelParticipantsNotModified()
            if admin_page == "bad_count":
                return SimpleNamespace(count=None, participants=[
                    client.roles[1234, 999]])
            return types.channels.ChannelParticipants(
                count=1, participants=[], chats=[], users=[User(999)])
        return client._list_page(req)

    client.list_override = broken
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب تازه")
    refused = send(bot, "هیچکدام")
    assert "وضعیت فعلی کاربر" in refused.sent[-1][0]
    refused = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "وضعیت فعلی کاربر" in refused.sent[-1][0]
    assert not client.edits
    assert client.roles[1234, 101].rank == "قبلی"


def test_fallback_checks_bot_add_admins_right_and_partial_rank(setup):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.add_user(1234, User(101))
    client.roles[1234, 999].admin_rights = types.ChatAdminRights(add_admins=False)
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    refused = send(bot, "هیچکدام")
    assert "دسترسی «افزودن مدیر جدید» ندارد" in refused.sent[-1][0]
    assert not client.edits
    client.roles[1234, 999].admin_rights = types.ChatAdminRights(add_admins=True)
    client.ignore_rank = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    role_seen = send(bot, "هیچکدام")
    assert "ادمین گروه شد مدیریت گروه را بررسی کنید" in role_seen.sent[-1][0]
    assert "لقب :" not in role_seen.sent[-1][0]
    assert len(client.edits) == 1
    assert isinstance(client.roles[1234, 101], types.ChannelParticipantAdmin)


def test_bot_rights_use_get_me_entity_not_uncached_numeric_id(setup, monkeypatch):
    bot, _owners, _ = setup
    bot.client.direct_unsupported = True
    bot.client.add_user(1234, User(101))
    original = Client.get_input_entity

    async def resolve(self, entity):
        if isinstance(entity, int) and entity == 999:
            raise ValueError("bot's numeric entity is not cached")
        return await original(self, entity)

    monkeypatch.setattr(Client, "get_input_entity", resolve)
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    result = send(bot, "هیچکدام")
    assert "ادمین گروه شد" in result.sent[-1][0]
    assert len(bot.client.edits) == 1


def test_unsupported_read_after_write_outage_is_never_reported_as_success(setup):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.add_user(1234, User(101))
    client.no_read_after_write = True
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    unknown = send(bot, "هیچکدام")
    assert "موفقیت اعلام نشد" in unknown.sent[-1][0]
    assert "ادمین گروه شد" not in unknown.sent[-1][0]
    assert len(client.edits) == 1
    assert isinstance(client.roles[1234, 101], types.ChannelParticipantAdmin)
    client.edits.clear()  # simulate a fresh command, then a later read outage
    demoted = send(bot, "لغو ادمین گروه", reply_id=4)
    assert "نتیجهٔ لغو ادمینی از سرور قابل بررسی نیست" in demoted.sent[-1][0]
    assert "لغو شد" not in demoted.sent[-1][0]
    assert len(client.edits) == 1


def test_ambiguous_write_with_changed_admin_must_not_auto_demote(
        setup, monkeypatch):
    bot, _owners, _ = setup
    client = bot.client
    client.direct_unsupported = True
    client.add_user(1234, User(101))
    original = Client.__call__

    async def ambiguous(self, request):
        if isinstance(request, functions.channels.EditAdminRequest):
            self.edits.append(request)
            self.roles[1234, 101] = types.ChannelParticipantAdmin(
                user_id=101, promoted_by=999, date=None,
                admin_rights=request.admin_rights, rank="شخص دیگر")
            raise TimeoutError("unknown edit outcome")
        return await original(self, request)

    monkeypatch.setattr(Client, "__call__", ambiguous)
    send(bot, "ادمین گروه", reply_id=4)
    send(bot, "لقب")
    role_seen = send(bot, "هیچکدام")
    assert "ادمین گروه شد مدیریت گروه را بررسی کنید" in role_seen.sent[-1][0]
    assert "شخص دیگر" not in role_seen.sent[-1][0]
    assert len(client.edits) == 1  # no second, destructive EditAdminRequest
    assert client.roles[1234, 101].rank == "شخص دیگر"


def _slice_entity(text, entity):
    raw = text.encode("utf-16-le")
    return raw[2 * entity.offset:2 * (entity.offset + entity.length)] \
        .decode("utf-16-le")


def test_interactive_board_exact_rows_spacing_and_real_bolds(setup):
    from modules import group_admin_texts as texts

    prompt, prompt_entities = texts.title_prompt()
    assert [_slice_entity(prompt, item) for item in prompt_entities
            if isinstance(item, types.MessageEntityBold)] == [prompt]
    rows = [
        "1_ تغییر اطلاعات گروه" + " " * 18 + "روشن ◨",
        "2_ حذف پیام ها" + " " * 28 + "روشن ◨",
        "3_ محروم کردن کاربران" + " " * 16 + "روشن ◨",
        "4_ افزودن اعضا" + " " * 28 + "روشن ◨",
        "5_ سنجاق کردن پیام ها" + " " * 17 + "روشن ◨",
        "6_ افزودن مدیر جدید" + " " * 21 + "خاموش ◧",
    ]
    first_line = "👨‍🌾 کاربر برای ادمین گروه  : @Cowe 🦊"
    question = "کدام یک از قابلیت های زیر برای کاربر غیر فعال شود !؟"
    hints = ["⚙ برای انتخاب مثلا بنویسید 1 و 2",
             "⚙ اگر انتخابی ندارید بنویسید هیچکدام"]
    board, entities = texts.rights_board("@Cowe 🦊", native.DEFAULT_CHOICES)
    assert board == (first_line + "\n\n" + question + "\n\n\n"
                     + "\n".join(rows) + "\n\n\n" + "\n\n".join(hints))
    assert [_slice_entity(board, item) for item in entities
            if isinstance(item, types.MessageEntityBlockquote)] == [first_line]
    assert [_slice_entity(board, item) for item in entities
            if isinstance(item, types.MessageEntityBold)] == \
        [first_line, question, *rows, *hints]
    assert "تایید ادمین گروه" not in board and "روشن 6" not in board
    assert all(item.length > 0 and item.offset >= 0
               and item.offset + item.length <= len(board.encode("utf-16-le")) // 2
               for item in entities)
    choices = dict(native.DEFAULT_CHOICES)
    choices[1], choices[6] = False, True
    changed, _ = texts.rights_board("@Cowe", choices)
    assert "1_ تغییر اطلاعات گروه" + " " * 18 + "خاموش ◧" in changed
    assert "6_ افزودن مدیر جدید" + " " * 21 + "روشن ◨" in changed


def test_success_message_exact_text_and_bold_entities(setup):
    from modules import group_admin_texts as texts

    text, entities = texts.promoted("@oert")
    assert text == "⚙ کاربر : @oert\nادمین گروه شد مدیریت گروه را بررسی کنید"
    assert [_slice_entity(text, item) for item in entities
            if isinstance(item, types.MessageEntityBold)] == text.split("\n")
    assert not any(isinstance(item, types.MessageEntityBlockquote)
                   for item in entities)
    assert "⚠️" not in text and "لقب" not in text


def test_exact_admin_help_one_two_line_quote_and_three_real_bolds(setup):
    title = "🌐 برای اضافه کردن یک کاربر به ادمین های گروه"
    expected = f"{title}\n\nادمین گروه\nلغو ادمین گروه"
    assert ADMIN_HELP_TEXT.count(expected) == 1
    assert '"ادمین گروه"' not in ADMIN_HELP_TEXT
    fragment = [chunk for chunk in ADMIN_HELP_MESSAGES if expected in chunk[0]]
    assert len(ADMIN_HELP_MESSAGES) == 2
    assert len(fragment) == 1  # in the existing second help message
    text, entities = fragment[0]
    assert text == ADMIN_HELP_MESSAGES[-1][0]
    assert text.index("🌡 برای روشن کردن حالت بحرانی") \
        < text.index(expected) < text.index("تماس با سازنده : @OSINE2")
    quotes = [_slice_entity(text, entity) for entity in entities
              if isinstance(entity, types.MessageEntityBlockquote)]
    bolds = [_slice_entity(text, entity) for entity in entities
             if isinstance(entity, types.MessageEntityBold)]
    assert quotes[-2:] == ["ادمین گروه\nلغو ادمین گروه", "تماس با سازنده : @OSINE2"]
    assert "ادمین گروه" not in quotes and "لغو ادمین گروه" not in quotes
    assert title in bolds and "ادمین گروه" in bolds and "لغو ادمین گروه" in bolds
    assert all(sum(isinstance(ent, types.MessageEntityBlockquote) for ent in entities)
               <= MAX_QUOTES_PER_MESSAGE for _, entities in ADMIN_HELP_MESSAGES)
    # Valid UTF-16 spans even when the content contains emoji (🌐).
    assert all(ent.length > 0 and ent.offset >= 0
               and ent.offset + ent.length <= len(text.encode("utf-16-le")) // 2
               for ent in entities)
