"""Offline concurrency and safe launcher tests; no service credentials needed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError("timed out waiting for test process")


def environment(root):
    env = os.environ.copy()
    for key in ("BOT_INSTANCE", "SOROUSH_BOT_DATA_DIR", "SOROUSH_BOT_DATA_DIR_FOXORA"):
        env.pop(key, None)
    env.update(HOME=str(root), BOT_INSTANCE="foxora", SOROUSH_BOT_DATA_DIR_FOXORA=str(root / "runtime"))
    return env


class MainLockTests(unittest.TestCase):
    def test_competing_processes_and_second_does_not_modify_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = environment(root)
            gate, finish = root / "go", root / "finish"
            code = '''import os, sys, time
from pathlib import Path
import main
root = Path(sys.argv[1]); n = sys.argv[2]
(root / ("ready" + n)).touch()
while not (root / "go").exists(): time.sleep(.01)
main._acquire_instance_lock()
(root / ("won" + n)).write_text(str(os.getpid()))
while not (root / "finish").exists(): time.sleep(.01)
'''
            procs = [subprocess.Popen([sys.executable, "-c", code, str(root), str(i)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(2)]
            try:
                wait_for(lambda: all((root / f"ready{i}").exists() for i in range(2)))
                gate.touch()
                wait_for(lambda: any((root / f"won{i}").exists() for i in range(2)))
                wait_for(lambda: any(p.poll() is not None for p in procs))
                self.assertEqual(sum((root / f"won{i}").exists() for i in range(2)), 1)
                loser = next(p for p in procs if p.poll() is not None)
                self.assertNotEqual(loser.returncode, 0)
                pidfile = root / "runtime/config/bot.pid"
                before, inode = pidfile.read_text(), pidfile.stat().st_ino
                self.assertEqual(int(before), next(p.pid for p in procs if p.poll() is None))
                second = subprocess.run([sys.executable, "-c", "import main; main._acquire_instance_lock()"], cwd=ROOT, env=env, capture_output=True, timeout=20)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn(b"ALREADY RUNNING", second.stderr)
                self.assertEqual(pidfile.read_text(), before)
                self.assertEqual(pidfile.stat().st_ino, inode)
                finish.touch()
                for p in procs:
                    p.wait(timeout=10)
                self.assertEqual(pidfile.read_text(), "")
                self.assertEqual(pidfile.stat().st_ino, inode)
            finally:
                finish.touch()
                for p in procs:
                    if p.poll() is None:
                        p.kill()
                    p.wait()

    def test_unsupported_flock_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            env = environment(Path(temp))
            code = "import errno,fcntl,main; from unittest.mock import patch; " \
                   "p=patch.object(fcntl, 'flock', side_effect=OSError(errno.ENOSYS, 'unsupported')); " \
                   "p.start(); main._acquire_instance_lock()"
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"LOCK ERROR", result.stderr)



class LauncherTests(unittest.TestCase):
    def test_watchdog_slow_child_wait_and_timeout_preserves_locks(self):
        for should_timeout in (False, True):
            with self.subTest(timeout=should_timeout), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                env = environment(root)
                child = root / "slow.py"
                child.write_text('''import fcntl, os, signal, time
from pathlib import Path
p = Path(os.environ["SOROUSH_BOT_DATA_DIR_FOXORA"]) / "config/bot.pid"
p.parent.mkdir(parents=True, exist_ok=True)
f = p.open("w+"); fcntl.flock(f, fcntl.LOCK_EX); f.write(str(os.getpid())); f.flush()
def stop(*args):
    time.sleep(1.5)
    f.seek(0); f.truncate(); f.flush(); f.close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
(p.parent / "child-ready").touch()
while True: time.sleep(.05)
''')
                w = subprocess.Popen([sys.executable, str(ROOT / 'watchdog.py'), '--no-owner-report', '--', sys.executable, str(child)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    config = root / 'runtime/config'
                    wait_for(lambda: (config / 'child-ready').exists())
                    watch, bot = config / 'watchdog.lock', config / 'bot.pid'
                    inodes = watch.stat().st_ino, bot.stat().st_ino
                    env['FOXORA_STOP_TIMEOUT'] = '.3' if should_timeout else '6'
                    started = time.monotonic()
                    result = subprocess.run([str(ROOT / 'foxora'), 'stop'], cwd=ROOT, env=env, capture_output=True, timeout=12)
                    elapsed = time.monotonic() - started
                    if should_timeout:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIsNone(w.poll())
                        self.assertTrue(bot.read_text().strip())
                        self.assertTrue(watch.read_text().strip())
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                        self.assertGreaterEqual(elapsed, 1.2)
                        w.wait(timeout=5)
                    self.assertEqual((watch.stat().st_ino, bot.stat().st_ino), inodes)
                finally:
                    if w.poll() is None:
                        w.terminate()
                    try:
                        w.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        w.kill(); w.wait()

    def test_unrelated_pid_is_never_signaled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = environment(root)
            proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=ROOT, env=env)
            try:
                config = root / 'runtime/config'; config.mkdir(parents=True)
                (config / 'watchdog.lock').write_text(json.dumps({'pid': proc.pid}))
                result = subprocess.run([str(ROOT / 'foxora'), 'stop'], cwd=ROOT, env=env, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(proc.poll())
                self.assertTrue((config / 'watchdog.lock').exists())
            finally:
                proc.terminate(); proc.wait()

    def test_dotenv_custom_path_with_live_bot_status_and_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); checkout = root / "checkout"
            (checkout / "modules").mkdir(parents=True)
            for file in ("foxora", ".bot_instance"):
                shutil.copy(ROOT / file, checkout / file)
            (checkout / "foxora").chmod(0o755)
            for file in ("runtime_paths.py", "foxora_control.py"):
                shutil.copy(ROOT / "modules" / file, checkout / "modules" / file)
            (checkout / "modules/__init__.py").write_text("")
            data = root / "custom runtime"
            (checkout / ".env").write_text(f'SOROUSH_BOT_DATA_DIR_FOXORA="{data}"\n')
            (checkout / "foxora.py").write_text("""import fcntl,os,time
from modules.runtime_paths import runtime_config_file
p=runtime_config_file('bot.pid', migrate=False)
f=p.open('w+'); fcntl.flock(f,fcntl.LOCK_EX); f.write(str(os.getpid())); f.flush()
while True: time.sleep(.1)
""")
            env = environment(root); env.pop("SOROUSH_BOT_DATA_DIR_FOXORA")
            proc = subprocess.Popen([sys.executable, str(checkout / "foxora.py")], cwd=checkout, env=env)
            try:
                pidfile = data / "config/bot.pid"
                wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
                status = subprocess.run([str(checkout / "foxora"), "status"], cwd=checkout, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn(f"DATA_DIR    = {data}", status.stdout)
                self.assertIn(f"RUNNING (pid={proc.pid})", status.stdout)
                inode = pidfile.stat().st_ino
                stop = subprocess.run([str(checkout / "foxora"), "stop"], cwd=checkout, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(stop.returncode, 0, stop.stderr)
                proc.wait(timeout=5)
                self.assertEqual(pidfile.stat().st_ino, inode)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

    def test_dotenv_and_environment_paths_match_runtime_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); checkout = root / 'checkout'
            (checkout / 'modules').mkdir(parents=True)
            for file in ('foxora', '.bot_instance'):
                shutil.copy(ROOT / file, checkout / file)
            for file in ('runtime_paths.py', 'foxora_control.py'):
                shutil.copy(ROOT / 'modules' / file, checkout / 'modules' / file)
            (checkout / 'modules' / '__init__.py').write_text('')
            (checkout / 'foxora').chmod(0o755)
            data = root / 'space path'
            sentinel = root / 'must-not-exist'
            (checkout / '.env').write_text(f'SOROUSH_BOT_DATA_DIR_FOXORA="{data}"\nDUMMY=$(touch {sentinel})\n')
            env = environment(root)
            env.pop('SOROUSH_BOT_DATA_DIR_FOXORA')
            for configured in (data, root / 'environment override'):
                if configured != data:
                    env['SOROUSH_BOT_DATA_DIR_FOXORA'] = str(configured)
                expected = subprocess.check_output([sys.executable, '-c', 'from modules.runtime_paths import DATA_DIR; print(DATA_DIR)'], cwd=checkout, env=env, text=True).strip()
                status = subprocess.run([str(checkout / 'foxora'), 'status'], cwd=checkout, env=env, capture_output=True, text=True, timeout=10)
                stop = subprocess.run([str(checkout / 'foxora'), 'stop'], cwd=checkout, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(expected, str(configured))
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn(f'DATA_DIR    = {expected}', status.stdout)
                self.assertEqual(stop.returncode, 0, stop.stderr)
                self.assertFalse(sentinel.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
