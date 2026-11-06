"""Regression coverage for single-user force-join membership checks."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from modules import force_join_api as api
from modules import force_join_service as service
from modules import force_join_storage as storage


@pytest.fixture(autouse=True)
def _reset_membership_cache(monkeypatch):
    service._member_cache.clear()
    service._full_scan_cache.clear()
    service._awaiting_confirm.clear()
    service._scan_until.clear()
    monkeypatch.delenv("FORCE_JOIN_MEMBER_TTL", raising=False)
    monkeypatch.delenv("FORCE_JOIN_NOT_MEMBER_TTL", raising=False)


def test_membership_ttls_are_asymmetric():
    assert service.member_ttl() == 300
    assert service.not_member_ttl() == 20


def test_full_scan_result_is_shared_for_all_users_and_expires(monkeypatch):
    record = {"channel_id": 445566, "group_id": -100445566}
    now = time.time()
    service._full_scan_cache[service.channel_key(record["channel_id"])] = (
        now, {10, 20, 30}
    )

    assert service._completed_scan_status(record, 20, now) == storage.STATUS_MEMBER
    assert service._completed_scan_status(record, 21, now) == storage.STATUS_NOT_MEMBER
    # Positive scan evidence may live for the snapshot TTL; negative evidence
    # must expire with the shorter per-user negative TTL.
    assert service._completed_scan_status(record, 20, now + 21) == storage.STATUS_MEMBER
    assert service._completed_scan_status(record, 21, now + 21) is None
    assert service._completed_scan_status(record, 21, now + 46) is None


def test_full_scan_caches_all_members_for_next_unknown_user(monkeypatch):
    record = {"channel_id": 778899, "group_id": -100778899}
    bot = SimpleNamespace(client=object(), log_info=lambda _msg: None,
                          log_error=lambda _msg: None)

    async def _channel_input(_bot, _record):
        return object()

    async def _complete_scan(*_args, **_kwargs):
        return {"state": "absent", "complete": True, "pages": 3,
                "member_ids": [11, 22, 33], "scanned": 500}

    monkeypatch.setattr(service, "ensure_channel_input", _channel_input)
    monkeypatch.setattr(api, "find_member", _complete_scan)
    monkeypatch.setattr(service, "_rpc_budget_allows", lambda _n: True)
    monkeypatch.setattr(service, "scan_max_members", lambda: 5000)
    monkeypatch.setattr(service, "scan_delay", lambda: 0)
    monkeypatch.setattr(service, "scan_budget", lambda: 10)
    monkeypatch.setattr(service, "gate_await_timeout", lambda: 10)

    status, outcome, _details = asyncio.run(service._scan_route(
        bot, record, 99, inline=False
    ))
    assert (status, outcome) == (storage.STATUS_NOT_MEMBER, "absent")
    assert service._completed_scan_status(record, 22) == storage.STATUS_MEMBER
    assert service._completed_scan_status(record, 99) == storage.STATUS_NOT_MEMBER


def test_incomplete_scan_does_not_cache_negative_membership(monkeypatch):
    record = {"channel_id": 889900, "group_id": -100889900}
    bot = SimpleNamespace(client=object(), log_info=lambda _msg: None,
                          log_error=lambda _msg: None)

    async def _channel_input(_bot, _record):
        return object()

    async def _partial_scan(*_args, **_kwargs):
        return {"state": "partial", "complete": False, "pages": 1,
                "member_ids": [11, 22], "scanned": 200}

    monkeypatch.setattr(service, "ensure_channel_input", _channel_input)
    monkeypatch.setattr(api, "find_member", _partial_scan)
    monkeypatch.setattr(service, "_rpc_budget_allows", lambda _n: True)
    monkeypatch.setattr(service, "scan_max_members", lambda: 5000)
    monkeypatch.setattr(service, "scan_delay", lambda: 0)
    monkeypatch.setattr(service, "gate_scan_budget", lambda: 10)

    status, _outcome, _details = asyncio.run(service._scan_route(
        bot, record, 99, inline=True
    ))
    assert status == storage.STATUS_UNKNOWN
    assert service._completed_scan_status(record, 99) is None
    assert service._member_cache.get((service.channel_key(record["channel_id"]), 11), (None,))[0] == storage.STATUS_MEMBER


def test_progressive_scan_uses_soroush_safe_page_sizes_and_short_circuits(monkeypatch):
    requested = []

    class FakeUser:
        def __init__(self, user_id):
            self.id = user_id

    async def _page(_client, _channel, _filter, offset, limit, **_kwargs):
        requested.append((offset, limit))
        ids = list(range(offset + 1, min(offset + limit, 500) + 1))
        return SimpleNamespace(
            users=[FakeUser(uid) for uid in ids],
            participants=[],
            count=500,
        )

    monkeypatch.setattr(api, "LIBRARY_AVAILABLE", True)
    monkeypatch.setattr(api, "ChannelParticipantsRecent", lambda: object())
    monkeypatch.setattr(api, "participants_page", _page)

    result = asyncio.run(api.find_member(
        object(), object(), 500, max_members=500, budget_seconds=10,
        delay=0,
    ))
    assert result["state"] == "found"
    assert result["position"] == 500
    assert result["pages"] == 3
    assert requested == [(0, 100), (100, 200), (300, 200)]


def test_join_event_confirms_immediately_at_urgent_priority(monkeypatch):
    chat_id, channel_id, user_id = -100778811, 778811, 12345
    record = {"group_id": chat_id, "channel_id": channel_id,
              "force_join_enabled": True}
    key = (service.canonical_group_key_str(chat_id), user_id)
    service._awaiting_confirm[key] = {
        "user_id": user_id, "chat_id": chat_id, "group_id": chat_id,
        "channel_id": channel_id, "name": "Test User",
    }
    # A deliberately huge poll delay must not affect an already received join event.
    monkeypatch.setattr(service, "confirm_first_delay", lambda: 3600)
    monkeypatch.setattr(service.storage, "get_group", lambda _chat: record)
    monkeypatch.setattr(service.storage, "set_member", lambda *_a, **_k: None)

    class Sender:
        def __init__(self):
            self.calls = []

        def enqueue_send(self, chat, text, **kwargs):
            self.calls.append((chat, text, kwargs))
            return True

    sender = Sender()
    bot = SimpleNamespace(
        outgoing_sender=sender,
        log_info=lambda _msg: None,
        log_error=lambda _msg: None,
    )

    class JoinEvent:
        user_joined = True
        user_added = False
        user_left = False
        user_kicked = False

        async def get_user(self):
            return SimpleNamespace(id=user_id)

    async def run_event():
        await service._handle_channel_membership(
            bot, JoinEvent(), [record], [user_id]
        )
        # Let the fire-and-forget confirmation task run, with no polling sleep.
        await asyncio.sleep(0)

    asyncio.run(run_event())
    assert sender.calls
    chat, _text, kwargs = sender.calls[0]
    assert chat == chat_id
    assert kwargs["priority"] == 0
    assert kwargs["channel"] == "main"
    assert key not in service._awaiting_confirm
