"""🧪 تست‌های حادثهٔ بعد از دیپلوی (A/B/C/D).

A — «Fatal error handling updates»: تایم‌اوت/کنسلِ GetDifference باید به خطایی
    تبدیل شود که حلقهٔ updateِ کتابخانه تحمل می‌کند (TimedOutError) و کلاینت
    disconnect نشود؛ بقیهٔ درخواست‌ها دست‌نخورده بمانند.
B — کشِ منفیِ سراسریِ NOT_SUPPORTED برای هر کانال با cooldown یک‌ساعته و
    «هیچ درخواستی به شبکه نرود» (و در نتیجه هیچ resendی بعد از reconnect).
C — timeout کوتاه‌تر برای GetParticipantRequest و باکتِ poll (پیش‌فرض: خاموش).
D — retry حذف پیام وقتی مهلت تمام می‌شود (شاملِ RpcTimeout که TimeoutError نیست).
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("splusthon")

from splusthon import errors  # noqa: E402

from modules import message_delete_queue as mdq  # noqa: E402
from modules import outgoing_sender as sender_mod  # noqa: E402
from modules import rpc_unsupported_cache as unsupported  # noqa: E402
from modules import update_loop_compat as compat  # noqa: E402
from modules.connection_guard import RpcTimeout  # noqa: E402
from modules.rpc_governor import classify_request  # noqa: E402
from modules.rpc_policy import RpcPolicy  # noqa: E402


class _Logger:
    def __init__(self):
        self.infos, self.errors = [], []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))


# ---------------------------------------------------------------------------
# A) ترجمهٔ خطا برای حلقهٔ update
# ---------------------------------------------------------------------------

# همان تاپلی که ``splusthon/client/updates.py::_update_loop`` برای
# GetDifference تحمل می‌کند.
_TOLERATED_BY_UPDATE_LOOP = (
    errors.ServerError,
    errors.TimedOutError,
    errors.FloodWaitError,
    ValueError,
)


def test_rpc_timeout_becomes_tolerated_for_get_difference():
    translated = compat.translate("GetDifferenceRequest", RpcTimeout("slow"))
    assert isinstance(translated, errors.TimedOutError)
    assert isinstance(translated, _TOLERATED_BY_UPDATE_LOOP)


def test_channel_difference_timeout_is_translated_too():
    translated = compat.translate(
        "GetChannelDifferenceRequest", asyncio.TimeoutError())
    assert isinstance(translated, errors.TimedOutError)


def test_other_requests_are_not_touched():
    original = RpcTimeout("slow")
    assert compat.translate("SendMessageRequest", original) is original
    assert compat.translate("GetParticipantRequest", original) is original


def test_real_cancellation_is_never_swallowed():
    cancel = asyncio.CancelledError()
    assert compat.translate("GetDifferenceRequest", cancel) is cancel


def test_rpc_errors_and_oserrors_pass_through_unchanged():
    rpc_error = errors.RPCError(None, "FLOOD", 420)
    assert compat.translate("GetDifferenceRequest", rpc_error) is rpc_error
    os_error = ConnectionError("down")
    assert compat.translate("GetDifferenceRequest", os_error) is os_error


def test_flag_off_disables_translation(monkeypatch):
    monkeypatch.setenv("UPDATE_LOOP_COMPAT_ENABLED", "0")
    original = RpcTimeout("slow")
    assert compat.translate("GetDifferenceRequest", original) is original


def test_wrapper_translates_inside_the_real_outgoing_path():
    """مسیرِ واقعی: کلاینتی که مهلتش تمام می‌شود، به کتابخانه TimedOutError می‌دهد."""
    logger = _Logger()

    class Client:
        def __init__(self):
            self.calls = 0

        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            self.calls += 1
            raise RpcTimeout("GetDifferenceRequest did not answer within 20s")

    class Sender:
        pass

    class GetDifferenceRequest:
        pass

    client = Client()
    assert sender_mod._wrap_call_with_gate(client, logger) is True

    async def scenario():
        return await client._call(Sender(), GetDifferenceRequest())

    with pytest.raises(errors.TimedOutError):
        asyncio.run(scenario())
    assert client.calls == 1
    assert any("UPDATE LOOP TRANSIENT" in line for line in logger.errors)


def test_wrapper_keeps_normal_errors_unchanged():
    class Client:
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            raise RpcTimeout("boom")

    class SendMessageRequest:
        pass

    client = Client()
    sender_mod._wrap_call_with_gate(client, _Logger())

    async def scenario():
        return await client._call(object(), SendMessageRequest())

    with pytest.raises(RpcTimeout):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# B) کشِ منفیِ سراسری
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    for name in ("RPC_NOT_SUPPORTED_CACHE_ENABLED",
                 "RPC_NOT_SUPPORTED_COOLDOWN",
                 "BOT_RPC_GET_PARTICIPANT_TIMEOUT_S",
                 "BOT_RPC_GET_PARTICIPANT_BUCKET",
                 "UPDATE_LOOP_COMPAT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    unsupported.reset()
    yield
    unsupported.reset()


def test_not_supported_is_cached_per_channel_and_blocks_the_rpc():
    class Client:
        def __init__(self):
            self.calls = 0

        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            self.calls += 1
            raise errors.RPCError(request, "NOT_SUPPORTED", 422)

    class GetParticipantRequest:
        def __init__(self, channel):
            self.channel = type("Ch", (), {"channel_id": channel})()

    logger = _Logger()
    client = Client()
    sender_mod._wrap_call_with_gate(client, logger)

    async def call(channel):
        return await client._call(object(), GetParticipantRequest(channel))

    with pytest.raises(errors.RPCError):
        asyncio.run(call(555))
    assert client.calls == 1

    # بار دوم: هیچ درخواستی به شبکه نمی‌رود
    with pytest.raises(unsupported.NotSupportedCached):
        asyncio.run(call(555))
    assert client.calls == 1

    # کانالِ دیگر و درخواستِ دیگر دست‌نخورده
    with pytest.raises(errors.RPCError):
        asyncio.run(call(556))
    assert client.calls == 2


def test_cached_error_looks_exactly_like_the_server_error():
    from modules import force_join_api as api

    error = unsupported.NotSupportedCached("GetParticipantRequest", "555")
    assert api.is_not_supported(error) is True
    assert api.classify_error(error) == api.RESULT_NOT_SUPPORTED


def test_cooldown_expires(monkeypatch):
    unsupported.note("GetParticipantRequest", "900")
    assert unsupported.is_cached("GetParticipantRequest", "900") is True
    key = ("GetParticipantRequest", "900")
    unsupported._UNTIL[key] = time.monotonic() - 1     # پنجره را گذشته کن
    assert unsupported.is_cached("GetParticipantRequest", "900") is False


def test_cooldown_is_one_hour_by_default():
    assert unsupported.cooldown_seconds() == 3600.0
    unsupported.note("GetParticipantRequest", "901")
    remaining = unsupported.snapshot()["entries"]["GetParticipantRequest|901"]
    assert 3590 < remaining <= 3600


def test_cache_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("RPC_NOT_SUPPORTED_CACHE_ENABLED", "0")
    assert unsupported.note("GetParticipantRequest", "902") is False
    assert unsupported.is_cached("GetParticipantRequest", "902") is False


def test_requests_outside_the_allowlist_are_not_cached():
    assert unsupported.note("SendMessageRequest", "903") is False
    assert unsupported.is_cached("SendMessageRequest", "903") is False


# ---------------------------------------------------------------------------
# C) timeout کوتاه‌تر و باکت
# ---------------------------------------------------------------------------

class GetParticipantRequest:
    def __init__(self, channel="x"):
        self.channel = channel


def test_get_participant_gets_a_short_deadline():
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(GetParticipantRequest()) == 5.0
    assert policy.timeout_for(GetParticipantRequest()) < policy.moderation_timeout_s


def test_short_deadline_can_be_restored(monkeypatch):
    monkeypatch.setenv("BOT_RPC_GET_PARTICIPANT_TIMEOUT_S", "0")
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(GetParticipantRequest()) == policy.moderation_timeout_s


def test_bucket_stays_critical_unless_opted_in(monkeypatch):
    assert classify_request(GetParticipantRequest()).bucket == "critical"
    monkeypatch.setenv("BOT_RPC_GET_PARTICIPANT_BUCKET", "poll")
    assert classify_request(GetParticipantRequest()).bucket == "poll"


# ---------------------------------------------------------------------------
# D) retry حذف پیام
# ---------------------------------------------------------------------------

class _DeleteClient:
    def __init__(self, errors_sequence):
        self.errors_sequence = list(errors_sequence)
        self.calls = 0

    async def delete_messages(self, chat_id, ids):
        self.calls += 1
        if self.errors_sequence:
            raise self.errors_sequence.pop(0)
        return True


def _queue(client):
    return mdq.MessageDeleteQueue(client, _Logger(), batch_size=10)


def test_delete_retries_after_rpc_timeout():
    client = _DeleteClient([RpcTimeout("timeout")])
    deleted, remaining = asyncio.run(_queue(client)._delete_ids(-1001, [1, 2]))
    assert client.calls == 2
    assert deleted == 2


def test_delete_gives_up_after_two_attempts():
    client = _DeleteClient([RpcTimeout("t"), RpcTimeout("t"), RpcTimeout("t")])
    deleted, _remaining = asyncio.run(_queue(client)._delete_ids(-1002, [1]))
    assert client.calls == 2
    assert deleted == 0


def test_delete_retries_plain_timeout_too():
    client = _DeleteClient([asyncio.TimeoutError()])
    deleted, _remaining = asyncio.run(_queue(client)._delete_ids(-1003, [7]))
    assert client.calls == 2
    assert deleted == 1


# ---------------------------------------------------------------------------
# A-۲) کنسلی که watchdog می‌زند «تایم‌اوت» است، نه مرگِ حلقهٔ update
# ---------------------------------------------------------------------------

def test_watchdog_cancel_is_reported_as_timeout_for_get_difference(monkeypatch):
    from modules import connection_guard

    logger = _Logger()

    class Client:
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            await asyncio.sleep(30)          # هرگز جواب نمی‌دهد

    class GetDifferenceRequest:
        pass

    client = Client()
    assert connection_guard.install_rpc_timeout(
        client, timeout=25, logger=logger) is True

    # ناظرِ واقعی تسکِ درونی را کنسل می‌کند؛ همان کار را شبیه‌سازی می‌کنیم.
    inner = {}

    import modules.outgoing_profiler as profiler

    real_attach = profiler.attach_cancel_target
    monkeypatch.setattr(
        profiler, "attach_cancel_target",
        lambda request_id, task: (inner.setdefault("task", task),
                                  real_attach(request_id, task))[1],
    )

    async def scenario():
        task = asyncio.ensure_future(client._call(object(), GetDifferenceRequest()))
        for _ in range(50):
            await asyncio.sleep(0)
            if inner.get("task") is not None:
                break
        inner["task"].cancel()
        return await task

    with pytest.raises(RpcTimeout):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# B-۲) همهٔ callerها: مسیرِ bio_check روی گروه اصلی
# ---------------------------------------------------------------------------

def test_bio_check_caller_stops_calling_after_not_supported():
    from types import SimpleNamespace

    from modules import bio_link_service

    class FakeClient:
        """کلاینتی که فقط لایهٔ شبکه را دارد و ``__call__`` واقعی است."""

        def __init__(self):
            self.calls = 0

        async def get_input_entity(self, entity):
            value = getattr(entity, "id", 1)
            return SimpleNamespace(channel_id=abs(int(value)) or 1,
                                   user_id=int(value) or 1, id=int(value) or 1)

        async def __call__(self, request, **kwargs):
            return await self._call(None, request, **kwargs)

        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            self.calls += 1
            raise errors.RPCError(request, "NOT_SUPPORTED", 422)

    client = FakeClient()
    sender_mod._wrap_call_with_gate(client, _Logger())
    bot = SimpleNamespace(client=client, reply_input_peer_cache={})
    user = SimpleNamespace(id=4242, participant=None, first_name="u")

    async def scenario():
        first = await bio_link_service.fetch_native_role(bot, -100777, user)
        after_first = client.calls
        second = await bio_link_service.fetch_native_role(bot, -100777, user)
        return first, after_first, second, client.calls

    first, after_first, second, total = asyncio.run(scenario())
    assert first is None and second is None      # «نامعلوم» مثل قبل
    assert after_first == 1
    assert total == after_first, "بعد از NOT_SUPPORTED نباید درخواستِ تازه برود"
