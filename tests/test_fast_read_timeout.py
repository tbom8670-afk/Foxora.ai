"""مهلتِ کوتاه برای خواندن‌های سبکی که در باکتِ critical می‌نشینند.

قاعده: هرچه در مسیرِ بن/سکوت/حذف نوشته می‌شود باید مهلتِ ۲۰ ثانیه‌ایِ خودش را
نگه دارد؛ فقط سبک‌های خواندنی (GetState/GetConfig/Ping) به ۵ ثانیه می‌آیند تا
یک اسلاتِ critical پشتِ اتصالِ مرده ۲۰ ثانیه بلاک نماند.
"""
from __future__ import annotations

from splusthon.tl.functions.updates import GetStateRequest
from splusthon.tl.functions.help import GetConfigRequest
from splusthon.tl.functions import PingRequest
from splusthon.tl.functions.channels import GetParticipantRequest
from splusthon.tl.functions.messages import (
    DeleteMessagesRequest,
    DeleteHistoryRequest,
)
from splusthon.tl.functions.channels import EditBannedRequest
from splusthon.tl.functions.updates import GetDifferenceRequest

from modules.rpc_policy import (
    FAST_READ_TIMEOUT_REQUESTS,
    SHORT_TIMEOUT_REQUESTS,
    RpcPolicy,
)


def test_fast_read_requests_default_to_five_seconds():
    policy = RpcPolicy.from_environment()
    for request in (GetStateRequest(), GetConfigRequest(), PingRequest(ping_id=1)):
        assert policy.timeout_for(request) == 5.0, type(request).__name__
        assert policy.timeout_for(request) < policy.moderation_timeout_s


def test_moderation_writes_keep_the_twenty_second_deadline():
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(EditBannedRequest(channel=None, participant=None,
                                                banned_rights=None)) == \
        policy.moderation_timeout_s
    assert policy.moderation_timeout_s == 20.0


def test_deletes_keep_the_bucket_deadline():
    """حذف عمداً کوتاه نشد: مهلتِ کمتر = شکستِ بیشتر در همان موردی که D
    دربارهٔ آن بود."""
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(DeleteMessagesRequest(id=[1])) == \
        policy.moderation_timeout_s
    assert policy.timeout_for(DeleteHistoryRequest(peer=None, max_id=0)) == \
        policy.moderation_timeout_s


def test_difference_requests_are_never_shortened():
    """کنسلِ زودهنگامِ difference وسطِ catch-up = زنده‌قفل."""
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(GetDifferenceRequest(pts=0, date=0, qts=0)) == 20


def test_get_participant_keeps_its_own_knob():
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(GetParticipantRequest(channel=None,
                                                    participant=None)) == 5.0


def test_short_read_deadline_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("BOT_RPC_FAST_READ_TIMEOUT_S", "0")
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(GetStateRequest()) == policy.moderation_timeout_s
    assert policy.timeout_for(PingRequest(ping_id=1)) == policy.moderation_timeout_s


def test_short_read_deadline_follows_the_environment(monkeypatch):
    monkeypatch.setenv("BOT_RPC_FAST_READ_TIMEOUT_S", "3")
    policy = RpcPolicy.from_environment()
    assert policy.timeout_for(GetStateRequest()) == 3.0


def test_sets_stay_disjoint_and_minimal():
    assert FAST_READ_TIMEOUT_REQUESTS.isdisjoint(SHORT_TIMEOUT_REQUESTS)
    assert "GetDifferenceRequest" not in FAST_READ_TIMEOUT_REQUESTS
    assert "ExportAuthorizationRequest" not in FAST_READ_TIMEOUT_REQUESTS
