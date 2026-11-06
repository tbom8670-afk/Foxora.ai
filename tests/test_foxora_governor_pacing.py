"""Preserved Foxora pacing knob on the live B2 RPC governor.

Foxora's SerialRpcQueue spaced outbound RPC starts with
``RPC_MIN_INTERVAL_SECONDS``. That capability now also lives on the
production path: ``RpcGovernor`` honors the same knob. Default must stay
0.0 so B2 behavior is unchanged unless the operator opts in.
"""
from __future__ import annotations

import asyncio
import time

from modules.rpc_governor import (
    P2_SEND,
    RpcAdmission,
    RpcGovernor,
)


def _admission(name="SendMessageRequest"):
    return RpcAdmission(priority=P2_SEND, bucket="send", request_name=name,
                        chat_key="g1")


def test_pacing_is_disabled_by_default() -> None:
    async def scenario() -> None:
        governor = RpcGovernor(total_limit=8, noncritical_limit=8,
                               send_limit=8, heavy_limit=8)
        assert governor.min_interval_seconds == 0.0
        starts = []
        for _ in range(3):
            permit = await governor.acquire(_admission())
            starts.append(time.monotonic())
            permit.release()
        assert starts[-1] - starts[0] < 0.05

    asyncio.run(scenario())


def test_pacing_spaces_rpc_starts() -> None:
    async def scenario() -> None:
        governor = RpcGovernor(total_limit=8, noncritical_limit=8,
                               send_limit=8, heavy_limit=8,
                               min_interval_seconds=0.05)
        starts = []
        for _ in range(3):
            permit = await governor.acquire(_admission())
            starts.append(time.monotonic())
            permit.release()
        gap1 = starts[1] - starts[0]
        gap2 = starts[2] - starts[1]
        assert gap1 >= 0.04, f"first gap too small: {gap1}"
        assert gap2 >= 0.04, f"second gap too small: {gap2}"

    asyncio.run(scenario())


def test_from_environment_reads_foxora_pacing_knob(monkeypatch) -> None:
    monkeypatch.setenv("RPC_MIN_INTERVAL_SECONDS", "0.25")
    governor = RpcGovernor.from_environment()
    assert abs(governor.min_interval_seconds - 0.25) < 1e-9


def test_from_environment_defaults_pacing_off(monkeypatch) -> None:
    monkeypatch.delenv("RPC_MIN_INTERVAL_SECONDS", raising=False)
    governor = RpcGovernor.from_environment()
    assert governor.min_interval_seconds == 0.0


def test_pacing_knob_is_clamped_to_documented_bounds() -> None:
    assert RpcGovernor(min_interval_seconds=-1).min_interval_seconds == 0.0
    assert RpcGovernor(min_interval_seconds=999).min_interval_seconds == 60.0


def test_concurrent_pacing_reserves_distinct_start_slots() -> None:
    async def scenario() -> None:
        governor = RpcGovernor(total_limit=8, noncritical_limit=8,
                               send_limit=8, heavy_limit=8,
                               min_interval_seconds=0.04)
        starts = []
        permits = []

        async def begin():
            permit = await governor.acquire(_admission())
            starts.append(time.monotonic())
            permits.append(permit)

        await asyncio.gather(*(begin() for _ in range(4)))
        gaps = [right - left for left, right in zip(starts, starts[1:])]
        assert len(gaps) == 3
        assert min(gaps) >= 0.03, f"concurrent starts bunched: {gaps}"
        for permit in permits:
            permit.release()

    asyncio.run(scenario())
