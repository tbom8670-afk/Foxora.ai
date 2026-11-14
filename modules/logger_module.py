"""Non-blocking, bounded logging for the Soroush bot.

All disk and console writes are handled by one background logging thread.
Runtime logs rotate automatically, so neither Android storage latency nor an
unbounded log file can stall the message event loop over time.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Optional

from modules.runtime_paths import runtime_log_file

_MAX_LOG_BYTES = int(os.environ.get("BOT_LOG_MAX_BYTES", 20 * 1024 * 1024))
_LOG_BACKUPS = int(os.environ.get("BOT_LOG_BACKUPS", 5))
_VERBOSE = os.environ.get("BOT_VERBOSE_LOGS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# These messages are useful while diagnosing a bug but produce one or more
# synchronous-looking lines per ordinary message/ping.  They are suppressed
# in production and can be restored with BOT_VERBOSE_LOGS=1.
_NOISY_PREFIXES = (
    "GROUP DISPATCH WORKER START",
    "OUTGOING SEND WORKER START",
    "KEEPALIVE PING SENT",
    "KEEPALIVE PONG RECEIVED",
    "BANNED WORD SKIP strict_off",
    "EXPIRY CHECK due_count=0",
    # Successful per-RPC traces generated several lines for every send/delete.
    # Keep failures, queue pressure and slow warnings; verbose mode can restore
    # these hot diagnostics when investigating a specific incident.
    "RPC TRACE",
    "RPC TIME",
    "OUTGOING SEND TIME",
    "DELETE TASK START",
    "BATCH DELETE START",
    "DELETE MESSAGE RPC TIME",
    "BATCH DELETE FINISHED",
    "NOTICE CLEANUP DUE",
    "NOTICE CLEANUP QUEUED",
    "NOTICE CLEANUP DELETED",
    "SPAM HISTORY SWEEP SKIPPED",
    "FILTER WORD DETECTED",
    "SPAM DETECTED",
    "SPAM DETECT TIME",
    "SPAM CLEANUP QUEUED",
    "🗑️ حذف شد",
    "NOTICE CLEANUP START",
    "DELETE QUEUE SIZE",
    "OUTGOING SEND QUEUE_WAIT",
    "OWNER PRIVATE PEER CAPTURED",
    "FAST MODERATION QUEUED",
    "HANDLER CALLED",
    "BANNED WORD HIT",
    "FORWARD DETECTED",
    "SPAM HISTORY SNAPSHOT",
    "PUNISHMENT TRIGGERED",
    "BIG SPAM BAN QUEUED",
    "BIG SPAM BAN CONFIRMED",
    "GROUP DISPATCH OVERFLOW",
    "DUPLICATE FLOOD DETECTED",
    "DUPLICATE GIF DETECTED",
    "GIF DELETE SCHEDULED",
    "SPAM LOCK SET",
    # --- Healthy-path diagnostics (one or more lines per RPC / message). ---
    # Failures, timeouts and real slowness are still logged; see
    # ``_is_noisy`` below for the conditional ones.
    "CONN TRACE AWAIT START",
    "CONN TRACE SOCKET SEND",
    "PERFORMANCE SNAPSHOT",
    "ACCUMULATION REPORT",
    "RPC WINDOW",
    "MEMORY GUARD OK",
    "PROFILE GUARD snapshot",
    "AUTO_NOTICE_CREATED",
    "AUTO_NOTICE_TIMER_STARTED",
    "AUTO_NOTICE_DELETE_TRIGGERED",
    "GOVERNOR ACQUIRE",
    "OUTGOING RPC NOT_SUPPORTED CACHED ",
    "MODERATION QUEUE ENQUEUED",
    "MODERATION WORKER START",
    "MODERATION RPC START",
    # These two also copied users' message text into the log.
    "EDITED MESSAGE INCOMING",
    "EDITED MESSAGE DETECTED",
)

# Lines that are noise only while everything is healthy: the line is dropped
# when the marker is present, and kept (failure / leftover) otherwise.
_NOISY_WHEN_OK = (
    ("CONN TRACE RESPONSE", " result=success"),
    ("BIO LINK CHECK", " status=ok"),
    ("AUTO_NOTICE_DELETE_RESULT", " remaining=0"),
    ("MODERATION RPC FINISHED", " result=success"),
)

# Timing lines that fire on every ordinary ~250 ms round trip.  They are kept
# only when the measured value shows a real stall.
_SLOW_LOG_MS = float(os.environ.get("BOT_SLOW_LOG_MS", "1000"))
_NOISY_BELOW_MS = (
    ("RPC GOVERNOR WAIT", " wait_ms="),
    ("QUEUE WAIT TIME", " queue_wait_ms="),
    ("OUTGOING RPC SLOW", " total_ms="),
)


def _is_noisy(text: str) -> bool:
    """True when ``text`` is routine diagnostics that production can skip."""
    if text.startswith(_NOISY_PREFIXES):
        return True
    for prefix, marker in _NOISY_WHEN_OK:
        if text.startswith(prefix):
            return marker in text
    for prefix, field in _NOISY_BELOW_MS:
        if text.startswith(prefix):
            start = text.find(field)
            if start < 0:
                return False
            raw = text[start + len(field):].split(" ", 1)[0]
            try:
                return float(raw) < _SLOW_LOG_MS
            except ValueError:
                return False
    return False


class _DroppingQueueHandler(QueueHandler):
    """Never block the bot when the logging queue is saturated."""

    dropped = 0

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            type(self).dropped += 1


class _LogRuntime:
    def __init__(self):
        self.queue = queue.Queue(maxsize=10_000)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        bot_path = runtime_log_file("bot.log")
        bot_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            bot_path,
            maxBytes=max(1024 * 1024, _MAX_LOG_BYTES),
            backupCount=max(1, _LOG_BACKUPS),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        self.listener = QueueListener(
            self.queue, console, file_handler, respect_handler_level=True
        )
        # Deletion audit JSON used to write synchronously from the incoming
        # message worker.  Keep the audit trail, but move its filesystem I/O
        # to a second bounded listener so spam bursts cannot block receipt.
        self.deleted_queue = queue.Queue(maxsize=10_000)
        deleted_handler = RotatingFileHandler(
            runtime_log_file("deleted_messages.log"),
            maxBytes=max(1024 * 1024, _MAX_LOG_BYTES),
            backupCount=max(1, _LOG_BACKUPS), encoding="utf-8",
        )
        deleted_handler.setFormatter(logging.Formatter("%(message)s"))
        self.deleted_listener = QueueListener(
            self.deleted_queue, deleted_handler, respect_handler_level=True
        )
        self.listener.start()
        self.deleted_listener.start()

    def stop(self):
        for listener in (getattr(self, "listener", None), getattr(self, "deleted_listener", None)):
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass


_RUNTIME = None
_RUNTIME_LOCK = Lock()


def _runtime():
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = _LogRuntime()
            atexit.register(_RUNTIME.stop)
        return _RUNTIME


def _build_json_logger(name: str, filename: str):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        path = runtime_log_file(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max(1024 * 1024, _MAX_LOG_BYTES),
            backupCount=max(1, _LOG_BACKUPS),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        # JSON event volume is low; using its own rotating handler avoids
        # cross-routing records while the main operational log stays queued.
        logger.addHandler(handler)
    return logger


class BotLogger:
    def __init__(self, log_file: str = "logs/deleted_messages.log",
                 console_log: bool = True):
        # ``log_file`` is retained for API compatibility.  Mutable logs always
        # live below the central runtime directory.
        self.log_file = str(runtime_log_file(Path(log_file).name))
        runtime = _runtime()
        self.logger = logging.getLogger("SoroushAntiSpam")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not any(isinstance(h, _DroppingQueueHandler)
                   for h in self.logger.handlers):
            self.logger.handlers.clear()
            self.logger.addHandler(_DroppingQueueHandler(runtime.queue))
        self._deleted_logger = logging.getLogger("SoroushAntiSpam.deleted")
        self._deleted_logger.setLevel(logging.INFO)
        self._deleted_logger.propagate = False
        if not any(isinstance(h, _DroppingQueueHandler) for h in self._deleted_logger.handlers):
            self._deleted_logger.handlers.clear()
            self._deleted_logger.addHandler(_DroppingQueueHandler(runtime.deleted_queue))
        self._actions_logger = _build_json_logger(
            "SoroushAntiSpam.actions", "actions.log"
        )

    def log_deleted_message(self, user_id: int, username: Optional[str],
                            group_id: int, group_title: Optional[str],
                            original_text: str, reason: str,
                            message_id: Optional[int] = None):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "username": username,
            "group_id": group_id,
            "group_title": group_title,
            "message_id": message_id,
            # Keep enough context for moderation without creating an unlimited
            # copy of users' messages.
            "original_text": str(original_text or "")[:500],
            "reason": reason,
        }
        self._deleted_logger.info(
            json.dumps(log_entry, ensure_ascii=False, separators=(",", ":"))
        )
        if _VERBOSE:
            self.logger.info(
                "🗑️ حذف شد | گروه: %s(%s) | کاربر: %s(%s) | دلیل: %s",
                group_title, group_id, username, user_id, reason,
            )

    def log_action(self, action: str, user_id: int, group_id: int,
                   details: str = ""):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "user_id": user_id,
            "group_id": group_id,
            "details": details,
        }
        self._actions_logger.info(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        )
        self.logger.warning(
            "⚙️ اقدام مدیریتی | %s | کاربر %s در گروه %s | %s",
            action, user_id, group_id, details,
        )

    def log_info(self, message: str):
        text = str(message)
        if not _VERBOSE and _is_noisy(text):
            return
        self.logger.info(text)

    def log_error(self, message: str):
        self.logger.error(str(message))

    @staticmethod
    def dropped_records() -> int:
        return int(_DroppingQueueHandler.dropped)
