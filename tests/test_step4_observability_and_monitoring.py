import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.observability import MetricsCollector, PeriodicHealthMonitor
from modules.cache_manager import PermissionCircuitBreaker, STATE_OPEN, STATE_CLOSED


class MockLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, msg):
        self.infos.append(msg)

    def log_error(self, msg):
        self.errors.append(msg)


def test_metrics_collector_rpc_recording():
    """Verify RPC metrics aggregation, latency calculations, and error tracking."""
    collector = MetricsCollector()
    collector.rpc_stats.clear()

    collector.record_rpc("delete", 25.0)
    collector.record_rpc("delete", 15.0)
    collector.record_rpc("delete", 50.0, is_error=True)
    collector.record_rpc("send", 10.0)

    summary = collector.get_rpc_summary()

    assert "delete" in summary
    assert summary["delete"]["count"] == 3
    assert summary["delete"]["avg_ms"] == 30.0
    assert summary["delete"]["max_ms"] == 50.0
    assert summary["delete"]["errors"] == 1

    assert "send" in summary
    assert summary["send"]["count"] == 1
    assert summary["send"]["avg_ms"] == 10.0
    assert summary["send"]["errors"] == 0


def test_metrics_collector_queue_wait_recording():
    """Verify queue wait sampling and statistical calculation."""
    collector = MetricsCollector()
    collector.queue_waits.clear()

    collector.record_queue_wait("dispatcher_normal", 5.0)
    collector.record_queue_wait("dispatcher_normal", 15.0)
    collector.record_queue_wait("delete_queue", 40.0)

    stats = collector.get_queue_stats()

    assert "dispatcher_normal" in stats
    assert stats["dispatcher_normal"]["avg_ms"] == 10.0
    assert stats["dispatcher_normal"]["max_ms"] == 15.0
    assert stats["dispatcher_normal"]["samples"] == 2

    assert "delete_queue" in stats
    assert stats["delete_queue"]["avg_ms"] == 40.0
    assert stats["delete_queue"]["max_ms"] == 40.0


def test_metrics_collector_overflow_tracking():
    """Verify overflow tracking per source and per chat ID."""
    collector = MetricsCollector()
    collector.overflow_counts.clear()
    collector.overflow_groups.clear()

    collector.record_overflow("group_dispatcher", 1001)
    collector.record_overflow("group_dispatcher", 1001)
    collector.record_overflow("group_dispatcher", 1002)
    collector.record_overflow("outgoing_sender", 1001)

    assert collector.overflow_counts["group_dispatcher"] == 3
    assert collector.overflow_counts["outgoing_sender"] == 1
    assert collector.overflow_groups["1001"] == 3
    assert collector.overflow_groups["1002"] == 1


def test_system_snapshot_and_diagnostic_formatting():
    """Verify full system diagnostic report formatting."""
    logger = MockLogger()
    collector = MetricsCollector(logger)
    collector.rpc_stats.clear()
    collector.queue_waits.clear()
    collector.overflow_counts.clear()

    # Mock bot with subcomponents
    bot = SimpleNamespace()
    bot.group_dispatcher = SimpleNamespace(
        worker_count=lambda: 6,
        stats={"processed": 500, "failed": 2, "dropped": 0},
    )
    bot.message_delete_queue = SimpleNamespace(
        _workers={"100": MagicMock(done=lambda: False)},
        _queues={"100": MagicMock(qsize=lambda: 5)},
    )
    bot.moderation_queue = SimpleNamespace(
        _workers={"100": [MagicMock(done=lambda: False)]},
        _pending_keys={("100", 1, "mute")},
    )
    bot.outgoing_sender = SimpleNamespace(
        _workers={("100", "normal"): [MagicMock(done=lambda: False)]},
        stats={"enqueued": 50, "sent": 48, "failed": 0, "dropped": 0},
    )
    bot.rpc_governor = SimpleNamespace(
        snapshot=lambda: {"enabled": True, "shadow": False, "active": 3, "total_limit": 10, "waiting": 0}
    )

    collector.record_rpc("delete", 20.0)
    collector.record_queue_wait("dispatcher_normal", 4.5)

    # Circuit breaker
    cb = PermissionCircuitBreaker.get_default()
    cb.reset_all_for_tests() if hasattr(cb, "reset_all_for_tests") else getattr(cb, "_breakers", {}).clear()
    class UserAdminInvalidError(Exception):
        pass

    cb.record_failure(999, UserAdminInvalidError("USER_ADMIN_INVALID"))

    snap = collector.get_system_snapshot(bot)
    assert snap["rss_mb"] >= 0.0
    assert snap["workers"]["dispatcher"] == 6
    assert snap["circuit_breaker"]["open"] == 1
    assert "999" in snap["circuit_breaker"]["open_groups"]

    report = collector.format_diagnostic_report(bot)
    assert "گزارش جامع سلامت و عملکرد ربات" in report
    assert "مدارشکن" in report
    assert "999" in report
    assert "بودجه RPC" in report


def test_periodic_health_monitor_lifecycle():
    """Verify start, periodic report generation, and clean shutdown of health monitor."""
    async def scenario():
        logger = MockLogger()
        bot = SimpleNamespace(
            group_dispatcher=SimpleNamespace(worker_count=lambda: 2, stats={}),
            message_delete_queue=SimpleNamespace(_workers={}, _queues={}),
            moderation_queue=SimpleNamespace(_workers={}, _pending_keys=set()),
            outgoing_sender=SimpleNamespace(_workers={}, stats={}),
            rpc_governor=None,
        )

        monitor = PeriodicHealthMonitor(bot, logger, interval_seconds=0.05)
        monitor.start()

        # Let the monitor fire at least once
        await asyncio.sleep(0.12)
        await monitor.stop()

        assert len(logger.infos) >= 1
        assert any("HEALTH_HEARTBEAT_REPORT" in msg for msg in logger.infos)
        assert monitor._task is None or monitor._task.done()

    asyncio.run(scenario())
