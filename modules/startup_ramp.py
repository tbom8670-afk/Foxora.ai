"""Soft start: feed the restart backlog to the bot gradually.

Right after a restart Soroush hands over, in one burst, the messages that
arrived while the bot was down.  Every cache is still cold, so each of those
messages needs its own server lookups; they all queue behind the same few RPC
slots, the bot looks frozen for a moment and the owner gets a "severe
slowness" report.

During the first seconds after start this gate spaces the *old* ordinary group
messages out (those sent more than ``BOT_STARTUP_BACKLOG_AGE_SECONDS`` before
they reached the bot; live messages are never held): the gap between two messages starts at ``BOT_STARTUP_WARMUP_INTERVAL_MS``
and shrinks to zero by the end of ``BOT_STARTUP_WARMUP_SECONDS``.  Nothing is
dropped and the order is kept.  Admin commands, user commands and private
chats never wait.  After the window the gate costs one comparison.

``BOT_STARTUP_WARMUP_SECONDS=0`` switches it off.
"""

import asyncio
import datetime
import os
import time


def _env_float(name, default, low, high):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(low, min(high, value))


def message_age_seconds(event, now=None):
    """Seconds between the message being sent and now; None when unknown."""
    try:
        date = getattr(getattr(event, "message", None), "date", None)
        if date is None:
            return None
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        current = now or datetime.datetime.now(datetime.timezone.utc)
        return (current - date).total_seconds()
    except Exception:
        return None


class StartupRamp:
    def __init__(self, *, seconds=None, interval_ms=None, clock=time.monotonic):
        self.seconds = (
            _env_float("BOT_STARTUP_WARMUP_SECONDS", 60.0, 0.0, 600.0)
            if seconds is None else max(0.0, float(seconds))
        )
        self.interval = (
            _env_float("BOT_STARTUP_WARMUP_INTERVAL_MS", 200.0, 0.0, 2000.0)
            if interval_ms is None else max(0.0, float(interval_ms))
        ) / 1000.0
        self.backlog_age = _env_float(
            "BOT_STARTUP_BACKLOG_AGE_SECONDS", 10.0, 1.0, 3600.0
        )
        self._clock = clock
        self._started = None
        self._next_at = 0.0
        self._done = self.seconds <= 0.0 or self.interval <= 0.0
        self.stats = {"paced": 0, "waited_ms": 0.0, "max_wait_ms": 0.0}

    @property
    def active(self):
        if self._done:
            return False
        if self._started is None:
            return True
        if self._clock() - self._started >= self.seconds:
            self._done = True
            return False
        return True

    def is_backlog(self, event, now=None):
        """True for a message that was sent while the bot was not listening."""
        age = message_age_seconds(event, now)
        return age is not None and age >= self.backlog_age

    def remaining(self):
        """Seconds of warm-up left (full length before the first message)."""
        if not self.active:
            return 0.0
        if self._started is None:
            return self.seconds
        return max(0.0, self.seconds - (self._clock() - self._started))

    def _delay(self):
        """Reserve the next slot; returns how long this message should wait."""
        now = self._clock()
        if self._started is None:
            self._started = now
            self._next_at = now
        end = self._started + self.seconds
        if now >= end:
            self._done = True
            return 0.0
        slot = max(now, self._next_at)
        if slot >= end:
            # Backlog longer than the window: let the rest go at the end.
            return max(0.0, end - now)
        gap = self.interval * (1.0 - (slot - self._started) / self.seconds)
        self._next_at = slot + max(0.0, gap)
        return slot - now

    async def pace(self):
        """Wait for this message's turn.  Returns milliseconds waited."""
        if self._done:
            return 0.0
        delay = self._delay()
        if delay <= 0.0:
            return 0.0
        self.stats["paced"] += 1
        started = time.perf_counter()
        await asyncio.sleep(delay)
        waited = (time.perf_counter() - started) * 1000.0
        self.stats["waited_ms"] += waited
        self.stats["max_wait_ms"] = max(self.stats["max_wait_ms"], waited)
        return waited
