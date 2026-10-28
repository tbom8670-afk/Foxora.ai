import asyncio
from unittest.mock import AsyncMock, MagicMock

from modules.admin_actions import AdminActions
from modules.cache_manager import PermissionCircuitBreaker, is_permission_error


class MockLogger:
    def log_action(self, *args, **kwargs):
        pass

    def log_error(self, *args, **kwargs):
        pass

    def log_info(self, *args, **kwargs):
        pass


class MockInputPeerChannel:
    def __init__(self, channel_id, access_hash=123):
        self.channel_id = channel_id
        self.access_hash = access_hash


class MockInputPeerChat:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class MockInputPeerUser:
    def __init__(self, user_id, access_hash=456):
        self.user_id = user_id
        self.access_hash = access_hash


def test_mute_channel_rpc_call():
    """Verify channel entity calls EditBannedRequest."""
    async def scenario():
        client = MagicMock()
        client.get_me = AsyncMock(return_value=MagicMock(id=999999))
        client.get_input_entity = AsyncMock(side_effect=lambda x: MockInputPeerChannel(x))
        client.edit_permissions = AsyncMock()

        # Mock client call for EditBannedRequest
        rpc_calls = []
        async def mock_call(req):
            rpc_calls.append(req)
            return True
        client.side_effect = mock_call

        cb = PermissionCircuitBreaker(default_cooldown=60.0)
        admin = AdminActions(client, MockLogger(), {}, circuit_breaker=cb)

        channel_peer = MockInputPeerChannel(1001)
        user_peer = MockInputPeerUser(5001)

        result = await admin.mute_user(1001, 5001, duration_seconds=3600, user=user_peer, chat=channel_peer)
        assert result is True
        assert cb.can_execute(1001, "mute") is True
        assert len(rpc_calls) == 1
        assert "EditBannedRequest" in type(rpc_calls[0]).__name__
        assert rpc_calls[0].channel == channel_peer
        assert rpc_calls[0].participant == user_peer

    asyncio.run(scenario())


def test_mute_basic_chat_rpc_call():
    """Verify basic chat entity falls back to edit_permissions with send_messages=False."""
    async def scenario():
        client = MagicMock()
        client.get_me = AsyncMock(return_value=MagicMock(id=999999))
        client.get_input_entity = AsyncMock(side_effect=lambda x: MockInputPeerChat(x))
        client.edit_permissions = AsyncMock(return_value=True)

        cb = PermissionCircuitBreaker(default_cooldown=60.0)
        admin = AdminActions(client, MockLogger(), {}, circuit_breaker=cb)

        chat_peer = MockInputPeerChat(2002)
        user_peer = MockInputPeerUser(5002)

        result = await admin.mute_user(2002, 5002, duration_seconds=1800, user=user_peer, chat=chat_peer)
        assert result is True
        assert cb.can_execute(2002, "mute") is True
        client.edit_permissions.assert_called_once()
        call_args, call_kwargs = client.edit_permissions.call_args
        assert call_args[0] == chat_peer
        assert call_args[1] == user_peer
        assert call_kwargs.get("send_messages") is False

    asyncio.run(scenario())


def test_input_entity_resolution_and_caching():
    """Test resolution flow, peer_cache priority, and TtlCache lookup."""
    async def scenario():
        client = MagicMock()
        resolved_entities = {
            100: MockInputPeerChannel(100),
            -1000000000200: MockInputPeerChannel(200),
            300: MockInputPeerUser(300),
        }

        async def mock_get_input_entity(val):
            if val in resolved_entities:
                return resolved_entities[val]
            if isinstance(val, int) and val == 200:
                raise ValueError("Channel format required")
            raise KeyError("Entity not found")

        client.get_input_entity = AsyncMock(side_effect=mock_get_input_entity)

        peer_cache = {500: MockInputPeerChannel(500)}
        cb = PermissionCircuitBreaker(default_cooldown=60.0)
        admin = AdminActions(client, MockLogger(), {}, peer_cache=peer_cache, circuit_breaker=cb)

        # 1. Peer Cache hit
        p500 = await admin._input_entity(500)
        assert p500 == peer_cache[500]
        assert client.get_input_entity.call_count == 0

        # 2. Already an InputPeer
        direct_user = MockInputPeerUser(999)
        p_direct = await admin._input_entity(direct_user)
        assert p_direct == direct_user
        assert client.get_input_entity.call_count == 0

        # 3. Direct client resolution
        p100 = await admin._input_entity(100)
        assert p100 == resolved_entities[100]
        assert client.get_input_entity.call_count == 1

        # 4. TtlCache hit on repeated call
        p100_again = await admin._input_entity(100)
        assert p100_again == resolved_entities[100]
        assert client.get_input_entity.call_count == 1  # No extra RPC

        # 5. Positive ID falling back to channel negative format
        p200 = await admin._input_entity(200)
        assert p200 == resolved_entities[-1000000000200]

    asyncio.run(scenario())


def test_circuit_breaker_resilience_to_non_permission_errors():
    """Verify non-permission errors (entity resolution, ValueError, ChannelInvalid) do not trip the circuit breaker."""
    async def scenario():
        client = MagicMock()
        client.get_me = AsyncMock(return_value=MagicMock(id=999999))
        client.get_input_entity = AsyncMock(side_effect=ValueError("Could not find the input entity for PeerUser"))
        client.edit_permissions = AsyncMock(side_effect=RuntimeError("Generic network hiccup"))

        cb = PermissionCircuitBreaker(default_cooldown=60.0)
        admin = AdminActions(client, MockLogger(), {}, circuit_breaker=cb)

        # Execute failures due to entity resolution / network errors
        for _ in range(5):
            res = await admin.mute_user(1001, 5001)
            assert res is False

        # Circuit breaker should NOT be open because none were permission errors!
        assert cb.can_execute(1001, "mute") is True

        # Now simulate actual ChatAdminRequiredError
        from modules.admin_actions import ChatAdminRequiredError
        client.get_input_entity = AsyncMock(return_value=MockInputPeerChannel(1001))
        async def mock_fail_admin(*args, **kwargs):
            raise ChatAdminRequiredError("Admin rights required")
        client.side_effect = mock_fail_admin

        # ChatAdminRequired is logged/cancelled for this mute only; unrelated
        # moderation/commands for the same chat remain eligible.
        res1 = await admin.mute_user(1001, 5001)
        assert res1 is False
        assert cb.can_execute(1001, "mute") is True

    asyncio.run(scenario())


def test_concurrent_multi_group_mute_isolation():
    """Verify mute operations across multiple groups run concurrently and failures in one group don't affect others."""
    async def scenario():
        client = MagicMock()
        client.get_me = AsyncMock(return_value=MagicMock(id=999999))

        async def mock_client_call(req):
            if getattr(req, "channel", None) and getattr(req.channel, "channel_id", None) == 999:
                from modules.admin_actions import ChatAdminRequiredError
                raise ChatAdminRequiredError("Admin rights missing in group 999")
            await asyncio.sleep(0.01)
            return True

        client.side_effect = mock_client_call
        client.edit_permissions = AsyncMock(return_value=True)

        cb = PermissionCircuitBreaker(default_cooldown=60.0)
        admin = AdminActions(client, MockLogger(), {}, circuit_breaker=cb)

        # Mute in group 1 (channel), group 2 (basic chat), group 999 (failing channel)
        task1 = admin.mute_user(1, 101, chat=MockInputPeerChannel(1), user=MockInputPeerUser(101))
        task2 = admin.mute_user(2, 102, chat=MockInputPeerChat(2), user=MockInputPeerUser(102))
        task3 = admin.mute_user(999, 103, chat=MockInputPeerChannel(999), user=MockInputPeerUser(103))

        results = await asyncio.gather(task1, task2, task3)

        assert results[0] is True   # Group 1 succeeded
        assert results[1] is True   # Group 2 succeeded
        assert results[2] is False  # Group 999 failed

        # All chats remain eligible; the ChatAdminRequired failure is scoped
        # to the failed operation and is not recorded in the breaker.
        assert cb.can_execute(1, "mute") is True
        assert cb.can_execute(2, "mute") is True
        assert cb.can_execute(999, "mute") is True

    asyncio.run(scenario())
