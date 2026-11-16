"""🚪 گیتِ واحدِ «اشتراک گروه» — تست کامل و مستقل.

پوشش:
  • گروهی که هنوز «فعال» نشده → ربات هیچ کاری نمی‌کند (مسدود).
  • گروه فعال و سالم → کاری ندارد (باز).
  • گروه منقضی (حتی اگر is_active هنوز True باشد) → از همان لحظه مسدود.
  • گروه منقضیِ deactivate‌شده توسط watcher → مسدود.
  • تنها دستورِ فعال‌سازی/تمدیدِ «مالکِ سراسری» از گیت عبور می‌کند.
  • کاربر عادی در گروه منقضی/غیرفعال → حتی یک پاسخ هم رد نمی‌شود (مسدود).
  • fail-closed: اگر بررسیِ انقضا خطا دهد، گروه مسدود فرض می‌شود.

اجرا:
    python tests/test_subscription_gate.py
    pytest tests/test_subscription_gate.py
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modules.group_expiry as ge
import modules.group_storage as gs
import modules.owner_check as oc
import core.bot_working_split_ok as core

CHAT = -1001234567890
OTHER = -1009999999999
try:
    OWNER_ID = oc.get_owner()["user_id"]
except Exception:  # pragma: no cover
    OWNER_ID = 111111
NON_OWNER = OWNER_ID + 1

PASSED = FAILED = 0


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Sender:
    def __init__(self, uid):
        self.id = uid


class Message:
    def __init__(self, text="", caption=None):
        self.message = text
        self.caption = caption
        self.id = 1


class Event:
    def __init__(self, chat_id=CHAT, text="", sender_id=None, private=False,
                 caption=None, use_sender_obj=False):
        self.chat_id = chat_id
        self.is_private = private
        self.message = Message(text, caption)
        self.sender_id = sender_id
        self.sender = Sender(sender_id) if use_sender_obj else None
        self._bot_cached_sender = None


def _fresh_storage():
    gs.FILE = Path(tempfile.mkdtemp()) / "groups.json"
    gs._cache = None
    gs._cache_mtime = None
    ge.FILE = Path(tempfile.mkdtemp()) / "group_expiry.json"
    ge._cache = None
    ge._cache_mtime = None


# ------------------------------------------------------------------ #
# ۱) تشخیصِ دستورِ فعال‌سازی/تمدید
# ------------------------------------------------------------------ #
def test_renewal_command_detection():
    print("\n[۱] تشخیصِ دستورِ فعال‌سازی/تمدید")
    R = core._is_group_renewal_command
    for cmd in ["فعال", "فعال سازی", "فعال‌سازی", "ثبت گروه", "غیر فعال"]:
        check(f"«{cmd}» دستورِ فعال‌سازی است", R(cmd) is True, f"-> {R(cmd)!r}")
    for cmd in ["۵ روز", "یک هفته", "دو هفته", "یک ماه"]:
        check(f"«{cmd}» دستورِ تمدید است", R(cmd) is True, f"-> {R(cmd)!r}")
    # نیم‌فاصله/فاصلهٔ اضافه نباید فعال‌سازی را بشکند
    check("«  فعال   سازی » با فاصلهٔ اضافه", R("  فعال   سازی ") is True)
    check("«فعال\u200cسازی» با نیم‌فاصله", R("فعال\u200cسازی") is True)
    # پیام/دستورِ عادی کاربر نباید «تمدید» حساب شود
    for cmd in ["", "سلام", "اخراج @ali", "ban", "مهلت گروه", "راهنما"]:
        check(f"«{cmd}» دستورِ فعال‌سازی نیست", R(cmd) is False, f"-> {R(cmd)!r}")


# ------------------------------------------------------------------ #
# ۲) وضعیت‌های «مسدود/باز» بودنِ گروه
# ------------------------------------------------------------------ #
def test_is_group_blocked_states():
    print("\n[۲] وضعیت‌های مسدود/باز بودنِ گروه")
    _fresh_storage()
    now = datetime.now(timezone.utc)

    # الف) هرگز فعال‌نشده → مسدود
    check("گروه هرگز فعال‌نشده مسدود است",
          core._is_group_blocked(CHAT) is True)

    # ب) فعال و بدون انقضا → باز
    gs.activate_group(CHAT, "گروه تست")
    check("گروه فعالِ بدون انقضا باز است",
          core._is_group_blocked(CHAT) is False)

    # پ) فعال ولی منقضی (هنوز is_active=True) → مسدود
    ge.set_expiry(CHAT, "یک هفته", title="گروه تست", now=now - timedelta(days=30))
    check("رکورد انقضا ثبت شد", ge.is_expired(CHAT) is True)
    check("گروه فعالِ منقضی مسدود است (بدون انتظار برای watcher)",
          core._is_group_blocked(CHAT) is True)

    # ت) منقضی و watcher آن را deactivate کرده → همچنان مسدود
    gs.deactivate_group(CHAT, "گروه تست")
    check("گروه منقضیِ deactivate‌شده مسدود است",
          core._is_group_blocked(CHAT) is True)

    # ث) تمدیدِ مالک → دوباره فعال و باز
    gs.activate_group(CHAT, "گروه تست")
    ge.set_expiry(CHAT, "یک ماه", title="گروه تست", now=now)
    check("پس از تمدید، انقضا پاک/آینده است", ge.is_expired(CHAT) is False)
    check("گروه تمدیدشده باز است", core._is_group_blocked(CHAT) is False)

    # ج) chat_id=None → مسدود نیست (privates/no-chat از گیتِ گروه رد نمی‌شوند)
    check("chat_id=None مسدود نیست", core._is_group_blocked(None) is False)


# ------------------------------------------------------------------ #
# ۳) fail-closed وقتی بررسیِ انقضا خطا می‌دهد
# ------------------------------------------------------------------ #
def test_fail_closed_on_expiry_error():
    print("\n[۳] fail-closed هنگام خطای بررسیِ انقضا")
    _fresh_storage()
    gs.activate_group(CHAT, "گروه تست")
    check("پیش از خطا: گروه باز است", core._is_group_blocked(CHAT) is False)

    original = ge.is_expired

    def _boom(_chat):
        raise RuntimeError("expiry store unavailable")

    try:
        ge.is_expired = _boom
        check("هنگام خطای انقضا → گروه مسدود فرض می‌شود (fail-closed)",
              core._is_group_blocked(CHAT) is True)
    finally:
        ge.is_expired = original
    check("پس از رفع خطا: گروه دوباره باز است",
          core._is_group_blocked(CHAT) is False)


# ------------------------------------------------------------------ #
# ۴) تصمیمِ کاملِ گیت برای یک رویداد
# ------------------------------------------------------------------ #
def test_subscription_gate_decisions():
    print("\n[۴] تصمیمِ گیت برای رویدادها")
    _fresh_storage()
    now = datetime.now(timezone.utc)
    gate = core._group_subscription_gate

    # --- گروه هرگز فعال‌نشده ---
    drop, reason, *_ = gate(Event(text="سلام", sender_id=NON_OWNER))
    check("غیرفعال: پیامِ کاربرِ عادی مسدود است", drop is True and reason == "not_active",
          f"-> drop={drop} reason={reason!r}")
    drop, reason, *_ = gate(Event(text="اخراج @ali", sender_id=NON_OWNER))
    check("غیرفعال: دستورِ moderation مسدود است", drop is True, f"-> drop={drop}")
    drop, reason, *_ = gate(Event(text="فعال", sender_id=OWNER_ID))
    check("غیرفعال: «فعال»ِ مالک عبور می‌کند", drop is False and reason == "owner_renewal",
          f"-> drop={drop} reason={reason!r}")
    drop, reason, *_ = gate(Event(text="فعال", sender_id=NON_OWNER))
    check("غیرفعال: «فعال»ِ غیرمالک مسدود است", drop is True, f"-> drop={drop}")

    # --- گروه فعال و سالم ---
    gs.activate_group(CHAT, "گروه تست")
    ge.set_expiry(CHAT, "یک ماه", title="گروه تست", now=now)
    drop, reason, *_ = gate(Event(text="سلام", sender_id=NON_OWNER))
    check("فعالِ سالم: پیامِ کاربر باز است", drop is False, f"-> drop={drop}")

    # --- گروه منقضی ---
    ge.set_expiry(CHAT, "یک هفته", title="گروه تست", now=now - timedelta(days=30))
    drop, reason, *_ = gate(Event(text="سلام", sender_id=NON_OWNER))
    check("منقضی: پیامِ کاربرِ عادی مسدود است", drop is True and reason == "expired",
          f"-> drop={drop} reason={reason!r}")
    drop, reason, *_ = gate(Event(text="hi there", sender_id=NON_OWNER))
    check("منقضی: پیامِ غیرفارسی هم مسدود است", drop is True, f"-> drop={drop}")
    # پیامِ رسانه‌ای بدون متن (کپشن) هم مسدود است
    drop, reason, *_ = gate(Event(text="", caption="عکس تبلیغاتی", sender_id=NON_OWNER))
    check("منقضی: رسانه با کپشن مسدود است", drop is True, f"-> drop={drop}")
    drop, reason, *_ = gate(Event(text="۵ روز", sender_id=OWNER_ID))
    check("منقضی: تمدیدِ «۵ روز»ِ مالک عبور می‌کند", drop is False and reason == "owner_renewal",
          f"-> drop={drop} reason={reason!r}")
    drop, reason, *_ = gate(Event(text="فعال", sender_id=OWNER_ID))
    check("منقضی: «فعال»ِ مالک عبور می‌کند", drop is False and reason == "owner_renewal",
          f"-> drop={drop} reason={reason!r}")
    drop, reason, *_ = gate(Event(text="۵ روز", sender_id=NON_OWNER))
    check("منقضی: «۵ روز»ِ غیرمالک مسدود است", drop is True, f"-> drop={drop}")

    # --- sender به‌صورت آبجکت (بدون sender_id مستقیم) ---
    ev = Event(text="فعال", sender_id=None)
    ev.sender = Sender(OWNER_ID)
    drop, reason, *_ = gate(ev)
    check("شناساییِ مالک از event.sender (آبجکت)", drop is False and reason == "owner_renewal",
          f"-> drop={drop} reason={reason!r}")

    # --- چتِ خصوصی هرگز مسدود نمی‌شود ---
    drop, reason, *_ = gate(Event(chat_id=OWNER_ID, text="سلام", sender_id=OWNER_ID,
                                  private=True))
    check("چتِ خصوصی از گیتِ گروه رد نمی‌شود", drop is False, f"-> drop={drop}")

    # --- گروه دیگرِ فعال و سالم مستقل است ---
    gs.activate_group(OTHER, "گروه دیگر")
    drop, reason, *_ = gate(Event(chat_id=OTHER, text="سلام", sender_id=NON_OWNER))
    check("گروهِ فعالِ دیگر باز است (جداسازی per-group)", drop is False,
          f"-> drop={drop}")


# ------------------------------------------------------------------ #
# ۵) یکپارچگی: چرخهٔ انقضا → deactivate → گیت
# ------------------------------------------------------------------ #
def test_watcher_deactivation_then_gate():
    print("\n[۵] یکپارچگی با چرخهٔ انقضا (deactivate) و گیت")
    _fresh_storage()
    now = datetime.now(timezone.utc)
    gs.activate_group(CHAT, "گروه تست")
    ge.set_expiry(CHAT, "یک هفته", title="گروه تست", now=now - timedelta(days=30))

    # پیش از watcher: فعال ولی منقضی → گیت باید مسدود کند
    check("پیش از watcher: گروه منقضی مسدود است",
          core._is_group_blocked(CHAT) is True)

    # watcher گروه را deactivate می‌کند (همان کاری که check_once انجام می‌دهد)
    gs.deactivate_group(CHAT, "گروه تست")
    check("پس از deactivate: گروه همچنان مسدود است",
          core._is_group_blocked(CHAT) is True)

    drop, reason, *_ = core._group_subscription_gate(
        Event(text="بن @ali", sender_id=NON_OWNER))
    check("پس از deactivate: moderation مسدود است", drop is True, f"-> drop={drop}")

    # مالک تمدید می‌کند → باید عبور کند و سپس گروه باز شود
    drop, reason, *_ = core._group_subscription_gate(
        Event(text="یک ماه", sender_id=OWNER_ID))
    check("تمدیدِ مالک عبور می‌کند", drop is False and reason == "owner_renewal",
          f"-> drop={drop} reason={reason!r}")
    gs.activate_group(CHAT, "گروه تست")
    ge.set_expiry(CHAT, "یک ماه", title="گروه تست", now=now)
    check("پس از تمدید: گروه باز است", core._is_group_blocked(CHAT) is False)
    drop, reason, *_ = core._group_subscription_gate(
        Event(text="بن @ali", sender_id=NON_OWNER))
    check("پس از تمدید: moderation باز است", drop is False, f"-> drop={drop}")


# ------------------------------------------------------------------ #
# ۶) لاگِ گیت بدون خطا اجرا می‌شود
# ------------------------------------------------------------------ #
def test_log_gate_no_crash():
    print("\n[۶] لاگِ گیت")

    class _Logger:
        def __init__(self):
            self.errors = []

        def log_error(self, msg):
            self.errors.append(msg)

        def log_info(self, msg):
            pass

    class _Bot:
        def __init__(self):
            self.logger = _Logger()

    _fresh_storage()
    bot = _Bot()
    core._EXPIRY_GATE_LOGGED.clear()
    try:
        core._log_expiry_gate(bot, CHAT, "سلام", "not_active")
        core._log_expiry_gate(bot, CHAT, "سلام", "expired")  # نرخ‌محدود: دومی لاگ نمی‌شود
        check("لاگِ گیت بدون خطا اجرا شد", len(bot.logger.errors) == 1,
              f"-> {len(bot.logger.errors)} entries")
        check("لاگ شاملِ شناسهٔ گروه است",
              any(str(CHAT) in e for e in bot.logger.errors))
    except Exception as error:  # pragma: no cover
        check("لاگِ گیت بدون خطا اجرا شد", False, f"-> {error!r}")


def _run_all():
    test_renewal_command_detection()
    test_is_group_blocked_states()
    test_fail_closed_on_expiry_error()
    test_subscription_gate_decisions()
    test_watcher_deactivation_then_gate()
    test_log_gate_no_crash()
    print(f"\n{'=' * 46}")
    print(f"  PASSED={PASSED}  FAILED={FAILED}")
    print(f"{'=' * 46}")
    return FAILED


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
