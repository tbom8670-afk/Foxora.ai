import os
import sys
import atexit

# launcher این clone — BOT_INSTANCE باید قبل از هر import پروجه
# (و قبل از لود شدن .env) قطعی شود: modules/runtime_paths در زمان
# import مقدارهای INSTANCE_NAME/DATA_DIR را از همین متغیر می‌سازد.
#
# ⚠️ این clone ممکن است instance غیر-main باشد (bot2/bot3). نام instance
# از فایل نشانگر `.bot_instance` کنار همین فایل خوانده می‌شود تا اجرای
# مستقیم `python3 main.py` هرگز به runtime ربات main برنگردد.
def _default_instance():
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".bot_instance")
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            name = fh.read().strip()
        if name:
            return name
    except OSError:
        pass
    return "main"


os.environ.setdefault("BOT_INSTANCE", _default_instance())

# .env باید قبل از هر import پروجه لود شود (override=False معنی
# متگیرهای محیتهای فعلی — از جمله BOT_INSTANCE بالا — بر .env برتری دارند).
from dotenv import load_dotenv
load_dotenv(override=False)

from core.bot_working_split_ok import FatalAuthError, SoroushAntiSpamBot
from modules.time_utils import TEHRAN, now_local
from modules.watchdog_reporting import FATAL_AUTH_EXIT_CODE
import asyncio


def _acquire_instance_lock():
    """Hold a kernel lock on the stable per-instance inode for this process."""
    try:
        import fcntl
        from modules.runtime_paths import runtime_config_file
        lock = runtime_config_file("bot.pid", migrate=False)
        stream = lock.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise SystemExit("BOT INSTANCE ALREADY RUNNING: bot.pid is locked")
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()) + "\n")
            stream.flush()
        except BaseException:
            stream.close()
            raise
    except SystemExit:
        raise
    except Exception as error:
        raise SystemExit(f"BOT INSTANCE LOCK ERROR: {error}") from error
    atexit.register(_release_instance_lock, stream, os.getpid())
    return stream


def _release_instance_lock(stream, pid):
    # Clear only our own PID while still holding the lock. Never unlink: a
    # waiter may already have the inode open when this process releases it.
    try:
        if os.getpid() == pid:
            stream.seek(0)
            if stream.read().strip() == str(pid):
                stream.seek(0)
                stream.truncate()
                stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        stream.close()  # releases flock even after an error


async def main():
    _acquire_instance_lock()
    try:
        from modules import runtime_paths
        _info = runtime_paths.describe()
        print(
            f"[INSTANCE] bot_instance={_info['instance']} "
            f"data_dir={_info['data_dir']} config_dir={_info['config_dir']} "
            f"log_dir={_info['log_dir']} db_dir={_info['db_dir']}"
        )
    except Exception:
        pass
    # نمایشِ زمانِ واقعیِ تهران در ترمینال هنگام راه‌اندازی، تا مشخص باشد
    # ربات واقعاً با چه زمانی کار می‌کند.
    _now = now_local()
    print(f"[START] Tehran time: {_now.strftime('%Y-%m-%d %H:%M:%S')} "
          f"({TEHRAN})")
    bot = SoroushAntiSpamBot()
    try:
        await bot.run()
    except FatalAuthError as error:
        # خروجِ قطعی، تمیز و فقط یک بار: نشست منقضی با reconnect درمان
        # نمی‌شود؛ watchdog با همین کد خاص پروسه را دوباره بالا نمی‌آورد.
        print("=" * 62)
        print(" ⛔ نشست سروش Foxora نامعتبر/منقضی شده است:")
        print(f"    {error}")
        print()
        print(" راه‌حل (فقط یک بار انجام می‌شود):")
        print("    1) python3 tools/login_primary_once.py")
        print("    2) ./foxora")
        print(" نشست جدید فقط در SOROUSH_SESSION_STRING_FOXORA ذخیره می‌شود؛")
        print(" نشست/دادهٔ ربات‌های دیگر (B2) تغییر نمی‌کند.")
        print("=" * 62)
        raise SystemExit(FATAL_AUTH_EXIT_CODE)

if __name__ == "__main__":
    asyncio.run(main())
