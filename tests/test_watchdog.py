"""Offline crash/owner-isolation tests for the permanent watchdog.

Run directly without SPlusthon network access:

    python3 tests/test_watchdog.py
"""
import asyncio
import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import watchdog
from modules import owner_private as private_owner
from modules import watchdog_reporting as reporting


class FakePeerUser:
    def __init__(self, user_id):
        self.user_id = user_id


class FakePeerChannel:
    def __init__(self, channel_id):
        self.channel_id = channel_id


class FakeClient:
    def __init__(
        self,
        owner_id,
        *,
        resolve_to_group=False,
        self_owner=True,
    ):
        self.owner_id = owner_id
        self.resolve_to_group = resolve_to_group
        self.self_owner = self_owner
        self.get_me_calls = []
        self.resolve_calls = []
        self.sent = []

    async def get_me(self, input_peer=False):
        self.get_me_calls.append(input_peer)
        if self.self_owner:
            return FakePeerUser(self.owner_id)
        return FakePeerUser(self.owner_id + 1)

    async def get_input_entity(self, entity):
        entity_id = getattr(entity, "user_id", entity)
        self.resolve_calls.append(entity_id)
        if self.resolve_to_group:
            return FakePeerChannel(entity_id)
        return FakePeerUser(entity_id)

    async def send_message(self, target, text):
        self.sent.append((target, text))
        return {"id": len(self.sent)}


class WatchdogTests(unittest.TestCase):
    def test_stale_lock_file_never_blocks_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "watchdog.lock"
            lock_file.write_text(
                json.dumps({
                    "version": 2,
                    "kind": "soroush-watchdog",
                    "pid": 999999999,
                    "start_ticks": "stale",
                    "token": "old",
                }),
                encoding="utf-8",
            )
            instance = watchdog.SingleInstance(lock_file)
            instance.acquire()
            try:
                self.assertIn(instance.mode, {"flock", "pidfile"})
                current = json.loads(lock_file.read_text(encoding="utf-8"))
                self.assertEqual(current["pid"], os.getpid())
                self.assertEqual(current["token"], instance.token)
            finally:
                instance.close()

    def test_second_instance_is_rejected_only_while_first_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "watchdog.lock"
            # The test runner itself is not named watchdog.py; emulate the
            # command-line identity a real watchdog process has.
            with mock.patch.object(
                watchdog.SingleInstance, "_watchdog_cmdline", return_value=True
            ):
                first = watchdog.SingleInstance(lock_file)
                first.acquire()
                try:
                    second = watchdog.SingleInstance(lock_file)
                    with self.assertRaises(watchdog.WatchdogAlreadyRunning) as raised:
                        second.acquire()
                    self.assertEqual(raised.exception.pid, os.getpid())
                finally:
                    first.close()

            # The on-disk record may remain after flock close.  It must not
            # block a new instance because no kernel lock is alive anymore.
            third = watchdog.SingleInstance(lock_file)
            third.acquire()
            third.close()

    def test_live_non_watchdog_pid_is_stale_even_with_structured_record(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "watchdog.lock"
            lock_file.write_text(json.dumps({
                "kind": "soroush-watchdog", "pid": os.getpid(),
                "start_ticks": watchdog.SingleInstance._process_start_ticks(os.getpid()),
            }), encoding="utf-8")
            with mock.patch.object(
                watchdog.SingleInstance, "_watchdog_cmdline", return_value=False
            ):
                status = watchdog.SingleInstance.status(lock_file)
            self.assertEqual(status["state"], "stale")
            self.assertIsNone(status["active_pid"])

    def test_replace_wait_returns_when_previous_pid_is_already_dead(self):
        with mock.patch.object(
            watchdog.SingleInstance, "_pid_alive", return_value=False
        ):
            self.assertTrue(watchdog._stop_active_watchdog(999999999, 0.01))

    def test_clean_flock_close_keeps_inode_and_unlocks(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "watchdog.lock"
            first = watchdog.SingleInstance(lock_file)
            first.acquire()
            if first.mode != "flock":
                self.skipTest("flock unavailable")
            inode = lock_file.stat().st_ino
            first.close()
            self.assertTrue(lock_file.exists())
            self.assertEqual(lock_file.stat().st_ino, inode)
            with lock_file.open("r") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
            second = watchdog.SingleInstance(lock_file)
            second.acquire()
            self.assertEqual(second.mode, "flock")
            self.assertEqual(lock_file.stat().st_ino, inode)
            second.close()

    def test_redacts_instance_suffixed_secrets(self):
        secret = "synthetic-session-secret"
        for key in ("SOROUSH_SESSION_STRING_FOXORA", "BOT_TOKEN_FOXORA", "API_HASH_FOXORA"):
            redacted = watchdog._redact(f'{key}="{secret} more-secret"')
            self.assertNotIn(secret, redacted)
            self.assertNotIn("more-secret", redacted)
            self.assertIn(key, redacted)
            self.assertIn("<redacted>", redacted)

    def test_android_unsupported_flock_uses_stale_aware_pid_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "watchdog.lock"
            lock_file.write_text("999999999\n", encoding="utf-8")
            unsupported = OSError(errno.EOPNOTSUPP, "flock unsupported")
            with mock.patch("fcntl.flock", side_effect=unsupported), mock.patch.object(
                watchdog.SingleInstance, "_watchdog_cmdline", return_value=True
            ):
                instance = watchdog.SingleInstance(lock_file)
                instance.acquire()
                self.assertEqual(instance.mode, "pidfile")
                record = json.loads(lock_file.read_text(encoding="utf-8"))
                self.assertEqual(record["pid"], os.getpid())

                second = watchdog.SingleInstance(lock_file)
                with self.assertRaises(watchdog.WatchdogAlreadyRunning):
                    second.acquire()
                instance.close()
            self.assertFalse(lock_file.exists())

    def test_android_false_busy_without_live_pid_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "watchdog.lock"
            lock_file.write_text("999999999\n", encoding="utf-8")
            false_busy = OSError(errno.EACCES, "filesystem reports busy")
            with mock.patch("fcntl.flock", side_effect=false_busy):
                instance = watchdog.SingleInstance(lock_file)
                instance.acquire()
                self.assertEqual(instance.mode, "pidfile")
                instance.close()
            self.assertFalse(lock_file.exists())

    def test_experimental_process_crash_is_fully_extracted(self):
        code = (
            "def explode():\n"
            "    raise ValueError('خطای آزمایشی Watchdog')\n"
            "\n"
            "explode()\n"
        )
        result = watchdog.run_child(
            [sys.executable, "-u", "-c", code],
            threading.Event(),
        )
        incident = watchdog.analyze_child_result(result)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(incident["error_type"], "ValueError")
        self.assertEqual(incident["file"], "<string>")
        self.assertEqual(incident["line"], 2)
        self.assertIn("خطای آزمایشی Watchdog", incident["summary"])
        self.assertIn("Traceback (most recent call last):", incident["traceback"])
        self.assertIn("raise ValueError", incident["traceback"])
        self.assertIn("ValueError: خطای آزمایشی Watchdog", incident["traceback"])

    def test_experimental_crash_reaches_only_owner_without_main(self):
        synthetic_owner_id = 987654324
        result = watchdog.run_child(
            [
                sys.executable,
                "-u",
                "-c",
                "raise RuntimeError('owner crash delivery test')",
            ],
            threading.Event(),
        )
        incident = watchdog.analyze_child_result(result)
        incident["crash_log"] = "logs/watchdog-crash-owner-test.log"
        original_get_owner = private_owner.get_owner
        try:
            private_owner.get_owner = lambda: {
                "user_id": synthetic_owner_id,
                "username": None,
            }
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "watchdog_pending.json"
                self.assertTrue(reporting.queue_incident(
                    incident,
                    state_path=state_file,
                ))
                client = FakeClient(synthetic_owner_id)
                delivered = asyncio.run(reporting.deliver_pending_reports(
                    client,
                    state_path=state_file,
                    message_limit=20000,
                    status="نیاز به بررسی دارد",
                ))
                self.assertEqual(delivered, 1)
                self.assertEqual(len(client.sent), 1)
                target, report = client.sent[0]
                self.assertIsInstance(target, FakePeerUser)
                self.assertEqual(target.user_id, synthetic_owner_id)
                self.assertIn("🚨 خطای ربات", report)
                self.assertIn("نوع خطا:\nRuntimeError", report)
                self.assertIn("owner crash delivery test", report)
                self.assertIn("فایل:\n<string>", report)
                self.assertNotIn("main.py", incident.get("command", []))
        finally:
            private_owner.get_owner = original_get_owner

    def test_supervisor_restarts_after_experimental_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / "runs.txt"
            child = root / "crash_then_stop_watchdog.py"
            child.write_text(
                "import os, signal, sys, time\n"
                "counter = sys.argv[1]\n"
                "try:\n"
                "    run = int(open(counter, encoding='utf-8').read()) + 1\n"
                "except Exception:\n"
                "    run = 1\n"
                "open(counter, 'w', encoding='utf-8').write(str(run))\n"
                "if run == 1:\n"
                "    raise RuntimeError('restart integration crash')\n"
                "os.kill(os.getppid(), signal.SIGTERM)\n"
                "time.sleep(5)\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update({
                "SOROUSH_BOT_DATA_DIR": str(root / "runtime"),
                "WATCHDOG_RESTART_DELAY": "0.05",
                "WATCHDOG_MAX_RESTART_DELAY": "0.1",
                "WATCHDOG_STABLE_SECONDS": "5",
            })
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "watchdog.py"),
                    "--no-owner-report",
                    "--",
                    sys.executable,
                    str(child),
                    str(counter),
                ],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(counter.read_text(encoding="utf-8"), "2")
            crash_logs = list((root / "runtime" / "logs").glob(
                "watchdog-crash-*.log"
            ))
            self.assertEqual(len(crash_logs), 1)
            crash_text = crash_logs[0].read_text(encoding="utf-8")
            self.assertIn("RuntimeError", crash_text)
            self.assertIn("restart integration crash", crash_text)

    def test_pending_crash_report_is_sent_only_to_configured_owner(self):
        synthetic_owner_id = 987654321
        incident = {
            "time_local": "2026-08-21 20:00:00 +0330",
            "error_type": "RuntimeError",
            "file": "main.py",
            "line": 12,
            "summary": "خطای آزمایشی مالک",
            "traceback": (
                'Traceback (most recent call last):\n'
                '  File "main.py", line 12, in <module>\n'
                '    raise RuntimeError("خطای آزمایشی مالک")\n'
                'RuntimeError: خطای آزمایشی مالک'
            ),
            "crash_log": "logs/watchdog-crash-test.log",
            "created_at_epoch": 1,
        }
        original_get_owner = private_owner.get_owner
        try:
            private_owner.get_owner = lambda: {
                "user_id": synthetic_owner_id,
                "username": None,
            }
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "watchdog_pending.json"
                self.assertTrue(reporting.queue_incident(
                    incident,
                    state_path=state_file,
                    now=1,
                ))
                client = FakeClient(synthetic_owner_id)
                delivered = asyncio.run(reporting.deliver_pending_reports(
                    client,
                    state_path=state_file,
                    message_limit=20000,
                    status="ربات دوباره راه‌اندازی شد",
                ))

                self.assertEqual(delivered, 1)
                self.assertEqual(client.get_me_calls, [True])
                self.assertEqual(client.resolve_calls, [])
                self.assertEqual(len(client.sent), 1)
                target, message = client.sent[0]
                self.assertIsInstance(target, FakePeerUser)
                self.assertEqual(target.user_id, synthetic_owner_id)
                self.assertIn("🚨 خطای ربات", message)
                self.assertIn("نوع خطا:\nRuntimeError", message)
                self.assertIn("فایل:\nmain.py", message)
                self.assertIn("خط:\n12", message)
                self.assertIn("Traceback کامل", message)
                self.assertIn("وضعیت:\nربات دوباره راه‌اندازی شد", message)
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["pending"], [])
                self.assertEqual(len(saved["sent"]), 1)
        finally:
            private_owner.get_owner = original_get_owner

    def test_identical_crash_is_deduplicated_during_cooldown(self):
        synthetic_owner_id = 987654323
        incident = {
            "time_local": "2026-08-21 20:00:00 +0330",
            "error_type": "ValueError",
            "file": "worker.py",
            "line": 7,
            "summary": "same crash",
            "traceback": "ValueError: same crash",
        }
        original_get_owner = private_owner.get_owner
        try:
            private_owner.get_owner = lambda: {
                "user_id": synthetic_owner_id,
                "username": None,
            }
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "watchdog_pending.json"
                now = time.time()
                self.assertTrue(reporting.queue_incident(
                    incident,
                    state_path=state_file,
                    now=now,
                    cooldown_seconds=900,
                ))
                self.assertFalse(reporting.queue_incident(
                    incident,
                    state_path=state_file,
                    now=now + 1,
                    cooldown_seconds=900,
                ))
                client = FakeClient(synthetic_owner_id)
                asyncio.run(reporting.deliver_pending_reports(
                    client,
                    state_path=state_file,
                    message_limit=20000,
                ))
                self.assertEqual(len(client.sent), 1)
                self.assertIn("تعداد تکرار پیش از بازیابی: 2", client.sent[0][1])

                # The same fingerprint after successful delivery is suppressed
                # during cooldown and therefore cannot spam the owner.
                self.assertFalse(reporting.queue_incident(
                    incident,
                    state_path=state_file,
                    now=time.time() + 2,
                    cooldown_seconds=900,
                ))
                self.assertEqual(reporting.pending_count(state_file), 0)
        finally:
            private_owner.get_owner = original_get_owner

    def test_group_peer_is_rejected_before_any_send(self):
        synthetic_owner_id = 987654322
        incident = {
            "time_local": "2026-08-21 20:00:00 +0330",
            "error_type": "RuntimeError",
            "file": "main.py",
            "line": 99,
            "summary": "group isolation test",
            "traceback": "RuntimeError: group isolation test",
            "created_at_epoch": 2,
        }
        original_get_owner = private_owner.get_owner
        try:
            private_owner.get_owner = lambda: {
                "user_id": synthetic_owner_id,
                "username": None,
            }
            with tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "watchdog_pending.json"
                reporting.queue_incident(
                    incident,
                    state_path=state_file,
                    now=2,
                )
                client = FakeClient(
                    synthetic_owner_id,
                    resolve_to_group=True,
                    self_owner=False,
                )
                with self.assertRaises(reporting.WatchdogDeliveryError):
                    asyncio.run(reporting.deliver_pending_reports(
                        client,
                        state_path=state_file,
                    ))
                self.assertEqual(client.sent, [])
        finally:
            private_owner.get_owner = original_get_owner


if __name__ == "__main__":
    unittest.main(verbosity=2)
