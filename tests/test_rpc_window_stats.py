"""🧪 تستِ پنجرهٔ ۶۰ ثانیه‌ایِ RPC (مشکل ۴ و ۵: فقط شمارش، بدونِ تغییرِ رفتار).

پوشش:
* میانگینِ ``rpc_await_ms`` و سقفِ آن در یک پنجره.
* شمارشِ جداگانهٔ ``GetDifferenceRequest`` و ``GetChannelDifferenceRequest``.
* یک خطِ لاگ در پایانِ هر پنجره، و صفرشدنِ شمارنده‌ها.
* فلگِ ``RPC_WINDOW_STATS_ENABLED=0`` → صفر شمارش، صفر لاگ (رفتارِ قبلی).
* اتصالِ واقعی به مسیرِ خروجی: ``instrument_client`` روی کلاینتِ جعلی.
* هیچ تسک/تایمرِ پس‌زمینه‌ای ساخته نمی‌شود (شمارش تنبل است).
"""

from __future__ import annotations

import asyncio

import pytest

from modules import rpc_window_stats as window
from modules.outgoing_profiler import instrument_client

from test_connection_trace import (  # noqa: E402
    Client,
    GetChannelDifferenceRequest,
    GetDifferenceRequest,
    Logger,
    SendMessageRequest,
)


class _WindowLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, message):
        self.lines.append(str(message))

    def log_error(self, message):
        self.lines.append("ERROR " + str(message))


def _close_window_after_past(state_seconds=61.0):
    """پنجره را در گذشته شروع کن تا اولین RPC بعدی آن را ببندد."""
    window._STATE["start"] -= state_seconds


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RPC_WINDOW_STATS_ENABLED", raising=False)
    window.reset()
    yield
    window.reset()


def test_average_and_counts_are_reported_once_per_window():
    logger = _WindowLogger()
    for value in (60.0, 80.0, 100.0):
        window.record("SendMessageRequest", value, logger)
    window.record("GetDifferenceRequest", 400.0, logger)
    assert logger.lines == [], "خطِ لاگ فقط در پایانِ پنجره نوشته می‌شود"

    _close_window_after_past()
    window.record("GetChannelDifferenceRequest", 20.0, logger)

    assert len(logger.lines) == 1
    line = logger.lines[0]
    assert line.startswith("RPC WINDOW ")
    assert "requests=5 " in line
    assert "avg_rpc_await_ms=132.0 " in line     # (60+80+100+400+20)/5
    assert "max_rpc_await_ms=400.0 " in line
    assert "get_difference=1 " in line
    assert "get_channel_difference=1" in line

    # پنجره بعدی از صفر شروع می‌شود
    assert window.snapshot()["requests"] == 0


def test_disabled_flag_does_nothing():
    import os

    os.environ["RPC_WINDOW_STATS_ENABLED"] = "0"
    try:
        logger = _WindowLogger()
        for value in (50.0, 50.0):
            window.record("GetDifferenceRequest", value, logger)
        _close_window_after_past()
        window.record("GetDifferenceRequest", 50.0, logger)
        assert logger.lines == []
        assert window.snapshot()["requests"] == 0
        assert window.snapshot()["get_difference"] == 0
    finally:
        os.environ.pop("RPC_WINDOW_STATS_ENABLED", None)


def test_no_background_task_is_created():
    async def scenario():
        before = {task for task in asyncio.all_tasks()}
        for value in (10.0, 20.0, 30.0):
            window.record("PingRequest", value, None)
        await asyncio.sleep(0)
        return before == {task for task in asyncio.all_tasks()}

    assert asyncio.run(scenario()) is True


def test_instrumented_client_feeds_the_window():
    """مسیرِ واقعی: هر RPCِ خروجی خودکار ثبت می‌شود (بدونِ تغییرِ رفتار)."""
    logger = Logger()

    async def scenario():
        client = Client(rpc_ms=5)
        instrument_client(client, logger)
        await client._call(client._sender, SendMessageRequest())
        await client._call(client._sender, GetDifferenceRequest())
        return client

    asyncio.run(scenario())
    state = window.snapshot()
    assert state["requests"] >= 2
    assert state["get_difference"] >= 1
    assert state["avg_rpc_await_ms"] >= 0.0
