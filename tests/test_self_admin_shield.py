# -*- coding: utf-8 -*-
"""Regression tests for multi-layer self-admin / self-demotion protection.

In Soroush Plus, sending ``channels.EditBannedRequest`` (including mute,
unmute, or unban with ``view_messages=False``) or ``channels.EditAdminRequest``
targeting the userbot's own account strips its ``ChannelParticipantAdmin``
role and turns the bot into a regular member inside the group.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from splusthon.tl import functions, types

from modules import banned_storage, manual_mute_storage
from modules.admin_actions import AdminActions
from modules.manage_features import exclusive, exclusive_commands, storage
from modules.manage_features.queue import reset_queue
from modules.self_admin_shield import (
    SelfAdminProtectedError,
    install_self_admin_shield,
    is_self_target,
    load_persisted_bot_id,
    sanitize_self_records,
    save_persisted_bot_id,
)


BOT_ID = 777001
OTHER_USER_ID = 888002
CHAT_ID = -1001234567890


class DummyLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, msg):
        self.infos.append(str(msg))

    def log_error(self, msg):
        self.errors.append(str(msg))


class FakeWireClient:
    """Minimal splusthon-like client with ``_call``, ``__call__``, and helpers."""

    def __init__(self, self_id=BOT_ID, *, lazy_get_me_id=None):
        self._self_id = self_id
        self._lazy_get_me_id = lazy_get_me_id
        self._sender = object()
        self.sent_requests = []
        self.permission_calls = []
        self.admin_calls = []
        self.kick_calls = []

    async def get_me(self):
        uid = self._self_id or self._lazy_get_me_id
        if uid is None:
            return None
        self._self_id = uid
        return SimpleNamespace(id=uid, is_self=True, username="foxora_self")

    async def get_entity(self, target):
        uid = getattr(target, "user_id", None) or getattr(target, "id", None) or int(target)
        return SimpleNamespace(id=int(uid), is_self=(int(uid) == (self._self_id or self._lazy_get_me_id)))

    async def get_input_entity(self, target):
        if isinstance(target, (types.InputPeerUser, types.InputPeerSelf, types.InputChannel)):
            return target
        uid = getattr(target, "id", None)
        if uid is not None:
            return types.InputPeerUser(user_id=int(uid), access_hash=111)
        val = int(target)
        if val < 0:
            return types.InputChannel(channel_id=abs(val), access_hash=222)
        return types.InputPeerUser(user_id=val, access_hash=111)

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        self.sent_requests.append(request)
        return SimpleNamespace(ok=True, request=request)

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        return await self._call(
            self._sender,
            request,
            ordered=ordered,
            flood_sleep_threshold=flood_sleep_threshold,
        )

    async def edit_permissions(self, entity, user=None, until_date=None, **kwargs):
        self.permission_calls.append((entity, user, kwargs))
        peer = await self.get_input_entity(user)
        chan = await self.get_input_entity(entity)
        return await self(
            functions.channels.EditBannedRequest(
                channel=chan,
                participant=peer,
                banned_rights=types.ChatBannedRights(
                    until_date=until_date,
                    view_messages=not kwargs.get("view_messages", True),
                    send_messages=not kwargs.get("send_messages", True),
                ),
            )
        )

    async def edit_admin(self, entity, user=None, **kwargs):
        self.admin_calls.append((entity, user, kwargs))
        peer = await self.get_input_entity(user)
        chan = await self.get_input_entity(entity)
        return await self(
            functions.channels.EditAdminRequest(
                channel=chan,
                user_id=peer,
                admin_rights=types.ChatAdminRights(),
                rank="",
            )
        )

    async def kick_participant(self, entity, user):
        self.kick_calls.append((entity, user))
        return True


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_wire_shield_blocks_edit_banned_and_edit_admin_on_self_only():
    client = FakeWireClient(self_id=BOT_ID)
    logger = DummyLogger()
    bot = SimpleNamespace(bot_account_id=BOT_ID, client=client, logger=logger)
    install_self_admin_shield(client, bot=bot, logger=logger)

    channel = types.InputChannel(channel_id=123456, access_hash=999)
    self_peer = types.InputPeerUser(user_id=BOT_ID, access_hash=111)
    other_peer = types.InputPeerUser(user_id=OTHER_USER_ID, access_hash=222)

    # 1) Unmute/unban EditBannedRequest (all False) on self MUST be blocked
    with pytest.raises(SelfAdminProtectedError):
        _run(
            client(
                functions.channels.EditBannedRequest(
                    channel=channel,
                    participant=self_peer,
                    banned_rights=types.ChatBannedRights(
                        until_date=None,
                        view_messages=False,
                        send_messages=False,
                    ),
                )
            )
        )

    # 2) InputPeerSelf MUST also be blocked
    with pytest.raises(SelfAdminProtectedError):
        _run(
            client(
                functions.channels.EditBannedRequest(
                    channel=channel,
                    participant=types.InputPeerSelf(),
                    banned_rights=types.ChatBannedRights(until_date=None, send_messages=True),
                )
            )
        )

    # 3) EditAdminRequest on self MUST be blocked
    with pytest.raises(SelfAdminProtectedError):
        _run(
            client(
                functions.channels.EditAdminRequest(
                    channel=channel,
                    user_id=self_peer,
                    admin_rights=types.ChatAdminRights(),
                    rank="",
                )
            )
        )

    # 4) High-level edit_permissions / edit_admin / kick_participant on self MUST be blocked
    with pytest.raises(SelfAdminProtectedError):
        _run(client.edit_permissions(CHAT_ID, BOT_ID, view_messages=True, send_messages=True))
    with pytest.raises(SelfAdminProtectedError):
        _run(client.edit_admin(CHAT_ID, BOT_ID, is_admin=False))
    with pytest.raises(SelfAdminProtectedError):
        _run(client.kick_participant(CHAT_ID, BOT_ID))

    assert client.sent_requests == []
    assert client.permission_calls == []
    assert client.admin_calls == []
    assert client.kick_calls == []

    # 5) Normal moderation on OTHER_USER_ID MUST succeed untouched
    res = _run(
        client(
            functions.channels.EditBannedRequest(
                channel=channel,
                participant=other_peer,
                banned_rights=types.ChatBannedRights(until_date=None, send_messages=True),
            )
        )
    )
    assert res.ok is True
    assert len(client.sent_requests) == 1


def test_wire_shield_lazy_resolves_bot_id_when_startup_get_me_initially_missed(tmp_path, monkeypatch):
    id_file = tmp_path / "bot_identity.json"
    monkeypatch.setattr("modules.self_admin_shield.IDENTITY_FILE", id_file)

    client = FakeWireClient(self_id=None, lazy_get_me_id=BOT_ID)
    logger = DummyLogger()
    bot = SimpleNamespace(bot_account_id=None, client=client, logger=logger)
    install_self_admin_shield(client, bot=bot, logger=logger)

    channel = types.InputChannel(channel_id=123456, access_hash=999)
    self_peer = types.InputPeerUser(user_id=BOT_ID, access_hash=111)

    with pytest.raises(SelfAdminProtectedError):
        _run(
            client(
                functions.channels.EditBannedRequest(
                    channel=channel,
                    participant=self_peer,
                    banned_rights=types.ChatBannedRights(until_date=None, send_messages=True),
                )
            )
        )

    assert bot.bot_account_id == BOT_ID
    assert load_persisted_bot_id() == BOT_ID
    assert client.sent_requests == []


def test_persisted_bot_id_and_sanitize_self_records(tmp_path, monkeypatch):
    id_file = tmp_path / "bot_identity.json"
    excl_file = tmp_path / "exclusive_access.json"
    mm_file = tmp_path / "manual_mutes.json"
    banned_file = tmp_path / "banned_users.json"

    monkeypatch.setattr("modules.self_admin_shield.IDENTITY_FILE", id_file)
    monkeypatch.setattr(storage.EXCLUSIVE_STORAGE, "path", excl_file)
    monkeypatch.setattr(storage.EXCLUSIVE_STORAGE, "backup_path", tmp_path / "exclusive_access.json.bak")
    storage.EXCLUSIVE_STORAGE._invalidate()
    monkeypatch.setattr(manual_mute_storage, "FILE", mm_file)
    monkeypatch.setattr(manual_mute_storage, "_cache", None)
    monkeypatch.setattr(banned_storage, "FILE", banned_file)
    monkeypatch.setattr(banned_storage, "_cache", None)

    save_persisted_bot_id(BOT_ID, username="foxora_bot")
    assert load_persisted_bot_id() == BOT_ID

    gkey = storage.group_key(CHAT_ID)
    storage.EXCLUSIVE_STORAGE.save({
        "groups": {
            gkey: {
                "users": {
                    str(BOT_ID): {"user_id": BOT_ID, "name": "Bot"},
                    str(OTHER_USER_ID): {"user_id": OTHER_USER_ID, "name": "Member"},
                }
            }
        }
    })
    manual_mute_storage.add_manual_mute(CHAT_ID, BOT_ID, display_name="Bot")
    manual_mute_storage.add_manual_mute(CHAT_ID, OTHER_USER_ID, display_name="Member")
    banned_storage.add_banned(CHAT_ID, user_id=BOT_ID, username="foxora_bot")
    banned_storage.add_banned(CHAT_ID, user_id=OTHER_USER_ID, username="other_u")

    removed = sanitize_self_records(BOT_ID, logger=DummyLogger())
    assert removed["exclusive"] == 1
    assert removed["manual_mute"] == 1
    assert removed["banned"] >= 1

    excl_after = storage.EXCLUSIVE_STORAGE.load()
    users_after = excl_after.get("groups", {}).get(gkey, {}).get("users", {})
    assert str(BOT_ID) not in users_after
    assert str(OTHER_USER_ID) in users_after

    muted_ids = {int(m["user_id"]) for m in manual_mute_storage.get_muted_users(CHAT_ID)}
    assert BOT_ID not in muted_ids
    assert OTHER_USER_ID in muted_ids

    assert not banned_storage.is_banned(CHAT_ID, user_id=BOT_ID)
    assert banned_storage.is_banned(CHAT_ID, user_id=OTHER_USER_ID)


def test_admin_actions_refuses_all_self_moderation_modes(monkeypatch):
    client = FakeWireClient(self_id=BOT_ID)
    logger = DummyLogger()
    actions = AdminActions(client, logger, {"mute_duration_minutes": 10})
    actions.bind_runtime_context(peer_cache={}, bot_account_id=BOT_ID)

    # Even when punishment_mode is set to mute instead of kick, ban_user MUST block self
    monkeypatch.setattr("modules.punishment_mode.is_mute", lambda _cid: True)

    assert _run(actions.mute_user(CHAT_ID, BOT_ID, 60)) is False
    assert _run(actions.unmute_user(CHAT_ID, BOT_ID)) is False
    assert _run(actions.ban_user(CHAT_ID, BOT_ID)) is False
    assert _run(actions.unban_user(CHAT_ID, BOT_ID)) is False
    assert _run(actions.punish_user(CHAT_ID, BOT_ID, announce=False)) is False
    assert client.sent_requests == []


def test_exclusive_grant_revoke_and_watch_protect_bot_self(tmp_path, monkeypatch):
    excl_file = tmp_path / "exclusive_access.json"
    monkeypatch.setattr(storage.EXCLUSIVE_STORAGE, "path", excl_file)
    monkeypatch.setattr(storage.EXCLUSIVE_STORAGE, "backup_path", tmp_path / "exclusive_access.json.bak")
    storage.EXCLUSIVE_STORAGE._invalidate()
    reset_queue(exclusive_commands.QUEUE)

    client = FakeWireClient(self_id=BOT_ID)
    logger = DummyLogger()
    bot = SimpleNamespace(
        bot_account_id=BOT_ID,
        client=client,
        logger=logger,
        peer_cache={CHAT_ID: types.InputChannel(channel_id=abs(CHAT_ID), access_hash=222)},
    )

    with pytest.raises(exclusive.GrantRejected):
        _run(exclusive.grant(bot, CHAT_ID, BOT_ID, actor_id=999))

    with pytest.raises(exclusive.NotExclusive):
        _run(exclusive.revoke(bot, CHAT_ID, BOT_ID))

    assert client.sent_requests == []

    # Verify watch() handles dict context={"bot": bot} and awaits QUEUE.submit cleanly
    gkey = storage.group_key(CHAT_ID)
    storage.EXCLUSIVE_STORAGE.save({
        "groups": {
            gkey: {
                "users": {
                    str(OTHER_USER_ID): {
                        "user_id": OTHER_USER_ID,
                        "name": "Member",
                        "state": "active",
                    },
                }
            }
        }
    })
    seen_bots = []

    async def fake_refresh(passed_bot, cid, _log):
        seen_bots.append((passed_bot, cid))
        return True

    monkeypatch.setattr(exclusive_commands, "lock_state_cached", lambda _cid: None)
    monkeypatch.setattr(exclusive_commands, "refresh_lock_state", fake_refresh)

    async def _exercise_watch():
        await exclusive_commands.watch(CHAT_ID, {"bot": bot}, logger=logger)
        await asyncio.sleep(0.05)

    _run(_exercise_watch())
    assert seen_bots == [(bot, CHAT_ID)]
    assert is_self_target(BOT_ID, bot=bot) is True
    assert is_self_target(OTHER_USER_ID, bot=bot) is False


def test_reply_to_bot_message_is_blocked_in_exclusive_commands_and_fast_mute(monkeypatch):
    from handlers.message_handler import handle_fast_moderation_command

    client = FakeWireClient(self_id=BOT_ID)
    logger = DummyLogger()
    actions = AdminActions(client, logger, {"mute_duration_minutes": 10})
    actions.bind_runtime_context(peer_cache={}, bot_account_id=BOT_ID)

    captured_jobs = []

    class ImmediateQueue:
        def enqueue(self, chat_id, action, *, user_id=None, timeout_seconds=15, operation=None, on_success=None, on_failure=None):
            captured_jobs.append((operation, on_success, on_failure))
            return True

    bot = SimpleNamespace(
        bot_account_id=BOT_ID,
        client=client,
        logger=logger,
        admin_actions=actions,
        moderation_queue=ImmediateQueue(),
        peer_cache={},
        reply_input_peer_cache={},
    )

    bot_sender = SimpleNamespace(id=BOT_ID, is_self=True, first_name="Foxora", username="foxora_self")

    class FakeReplyMsg:
        id = 555

        async def get_sender(self):
            return bot_sender

    async def fake_get_messages(_chat_id, ids=None):
        return FakeReplyMsg()

    client.get_messages = fake_get_messages
    monkeypatch.setattr("handlers.message_handler._has_group_management_permission", lambda *_a, **_kw: True)

    event_replies = []

    async def fake_event_reply(msg):
        event_replies.append(msg)

    fake_event = SimpleNamespace(
        chat_id=CHAT_ID,
        is_private=False,
        reply_to=SimpleNamespace(reply_to_msg_id=555),
        reply=fake_event_reply,
    )

    async def _exercise_fast_mute():
        handled = await handle_fast_moderation_command(
            bot, fake_event, text="سکوت", sender=SimpleNamespace(id=999000, username="owner")
        )
        assert handled is True
        assert len(captured_jobs) == 1
        op, on_ok, _on_fail = captured_jobs[0]
        res = await op()
        await on_ok(res)
        return res

    result = _run(_exercise_fast_mute())
    assert result["protected_admin"] is True
    assert result["muted"] is False
    assert any("ادمین" in r for r in event_replies)
    assert client.sent_requests == []

    # Also test exclusive_commands.handle("لغو اختصاصی") replying to bot's own message
    replies = []
    monkeypatch.setattr("modules.manage_features.access.is_registered_admin", lambda *_a, **_kw: True)

    async def fake_reply(_bot, _ev, payload, _log):
        replies.append(payload)

    async def fake_resolve(_bot, _ev, _cid, _tok):
        return (bot_sender, BOT_ID, "Foxora", "foxora_self")

    monkeypatch.setattr(exclusive_commands, "_reply", fake_reply)
    monkeypatch.setattr(exclusive_commands, "_resolve_target", fake_resolve)

    consumed = _run(
        exclusive_commands.handle(
            bot, fake_event, CHAT_ID, 999000, SimpleNamespace(username="owner"), "لغو اختصاصی", logger=logger
        )
    )
    assert consumed is True
    assert len(replies) == 1
    assert "خود ربات" in replies[0][0]
    assert client.sent_requests == []
