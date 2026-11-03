"""🧪 تست سه اصلاح ریشه‌ای (مهلت گروه، کش بیوگرافی، حرف «ک»).

هر سه مورد با «علت» آزموده می‌شوند، نه فقط ظاهر:

۱. پیام‌های «اعلام مهلت گروه» باید پس از پایان مهلت (و با تمدید بعدی)
   خودکار پاک شوند — بدون هیچ دخالت مدیر.
۲. مجازات «لینک در بیوگرافی» فقط با شاهد تازه انجام شود؛ بعد از حذف لینک
   از بیو، پیام‌های بعدی مجازات نشوند.
۳. حرف «ک» از کلمات ممنوعه حذف شده و حتی اگر برگردد هم فیلتر نمی‌شود؛
   سایر کلمات بدون تغییر کار می‌کنند.

    python -m pytest tests/test_three_bugfixes.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import handlers.group_expiry_handler as geh
import modules.bio_link_service as bio_service
import modules.bio_link_storage as bio_storage
import modules.group_expiry as ge
import modules.group_names_storage as names_storage
from modules.config_manager import ConfigManager
from modules.spam_detector import SpamDetector

CHAT = -1001234567890


# ---------------------------------------------------------------------------
# کارگاه
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self):
        self.info, self.errors = [], []

    def log_info(self, message):
        self.info.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))

    def has(self, needle):
        return any(needle in m for m in self.info + self.errors)


class Queue:
    """صف حذف جعلی: فقط ثبت می‌کند چه چیزی سپرده شده است."""

    def __init__(self):
        self.batches = []

    def enqueue(self, chat_id, message_ids, priority=0, **_kwargs):
        self.batches.append((chat_id, list(message_ids)))
        return len(message_ids)


class Sent:
    def __init__(self, message_id):
        self.id = message_id


class Client:
    def __init__(self, *, fail=False, message_id=900, deleted=None):
        self.fail = fail
        self.message_id = message_id
        self.sent = []
        self.deleted = deleted if deleted is not None else []

    async def send_message(self, target, text, formatting_entities=None, **_kw):
        if self.fail:
            raise ValueError("cannot resolve peer")
        self.sent.append((target, text))
        return Sent(self.message_id)

    async def delete_messages(self, chat_id, message_ids, **_kw):
        self.deleted.append((chat_id, list(message_ids)))
        return True


class Bot:
    def __init__(self, client=None, *, with_queue=True):
        self.client = client or Client()
        self.logger = Logger()
        self.message_delete_queue = Queue() if with_queue else None


class Event:
    def __init__(self, message_id=321):
        self.out = []
        self.entities = []
        self.message_id = message_id
        self.is_private = False

    async def reply(self, text, formatting_entities=None, **_kw):
        self.out.append(text)
        self.entities.append(formatting_entities or [])
        return Sent(self.message_id)

    async def get_chat(self):
        return type("Chat", (), {"title": "گروه تست"})()


class User:
    def __init__(self, user_id, name="کاربر"):
        self.id = user_id
        self.first_name = name
        self.last_name = None
        self.username = None
        self.access_hash = 1


class Owner(User):
    pass


def _fresh_expiry_file():
    ge.FILE = Path(tempfile.mkdtemp()) / "group_expiry.json"
    ge._cache = None
    ge._cache_mtime = None


def _owner():
    from modules.owner_check import get_owner
    return Owner(get_owner()["user_id"])


# ===========================================================================
# ۱) پیام «اعلام مهلت» پس از پایان مهلت پاک می‌شود
# ===========================================================================

def test_deadline_notice_is_tracked_and_scheduled():
    """پیام تأیید مهلت باید هم در رکورد ذخیره و هم برای TTL زمان‌بندی شود."""
    _fresh_expiry_file()

    class Cleanup:
        def __init__(self):
            self.scheduled = []

        def schedule(self, chat_id, message, ttl=None):
            self.scheduled.append((chat_id, ttl))
            return True

    bot, event = Bot(), Event(message_id=4321)
    cleanup = Cleanup()
    bot.notice_cleanup = cleanup

    consumed = asyncio.run(geh.handle(bot, event, CHAT, _owner(), "یک هفته", bot.logger))

    assert consumed is True
    assert event.out, "پیام تأیید باید ارسال شود"
    # ریشهٔ باگ قبلی: هیچ شناسه‌ای ذخیره نمی‌شد.
    assert ge.notice_ids(CHAT) == [4321]
    # و TTL برابر زمان باقی‌مانده تا انقضا (~۷ روز) است، نه TTL کوتاه پیش‌فرض.
    assert cleanup.scheduled and cleanup.scheduled[0][0] == CHAT
    ttl = cleanup.scheduled[0][1]
    assert ttl is not None
    assert 6.9 * 24 * 3600 < ttl <= 7 * 24 * 3600 + 10


def test_expiry_deletes_deadline_notice_at_deadline():
    """در لحظهٔ انقضا، پیام اعلام مهلت به صف حذف سپرده می‌شود."""
    _fresh_expiry_file()
    # مهلت «یک هفته» از ۱۰ روز پیش ⇒ قطعاً منقضی شده است.
    past = datetime.now(timezone.utc) - timedelta(days=10)
    ge.set_expiry(CHAT, "یک هفته", title="گروه تست", now=past)
    ge.add_notice(CHAT, 777)
    ge.add_notice(CHAT, 778)

    bot = Bot()
    deactivated = []
    closed = asyncio.run(
        geh.check_once(bot, lambda c, t: deactivated.append(c), logger=bot.logger)
    )

    assert closed == 1 and deactivated == [CHAT]
    assert bot.message_delete_queue.batches == [(CHAT, [777, 778])]
    # پس از انقضا دیگر شناسه‌ای برای حذف باقی نمی‌ماند (حذف تکراری نمی‌شود).
    assert ge.notice_ids(CHAT) == []
    assert bot.logger.has("EXPIRY NOTICE CLEARED")


def test_expiry_deletes_without_queue_using_client():
    """اگر صف حذف روی bot نبود، حذف مستقیم با کلاینت انجام می‌شود."""
    _fresh_expiry_file()
    past = datetime.now(timezone.utc) - timedelta(days=10)
    ge.set_expiry(CHAT, "یک هفته", now=past)
    ge.add_notice(CHAT, 555)

    client = Client()
    bot = Bot(client, with_queue=False)
    asyncio.run(geh.check_once(bot, lambda c, t: None, logger=bot.logger))

    assert (CHAT, [555]) in client.deleted


def test_renewal_deletes_previous_notice():
    """با تعیین مهلت تازه، پیام اعلام مهلت قبلی پاک می‌شود."""
    _fresh_expiry_file()
    ge.set_expiry(CHAT, "۵ روز")
    ge.add_notice(CHAT, 111)

    bot, event = Bot(), Event(message_id=222)
    asyncio.run(geh.handle(bot, event, CHAT, _owner(), "یک ماه", bot.logger))

    assert bot.message_delete_queue.batches == [(CHAT, [111])]
    assert ge.notice_ids(CHAT) == [222]


def test_notice_ids_survive_restart():
    """شناسه‌ها روی دیسک می‌مانند تا پس از ری‌استارت هم حذف شوند."""
    _fresh_expiry_file()
    ge.set_expiry(CHAT, "دو هفته")
    ge.add_notice(CHAT, 909)
    ge._cache = None           # شبیه‌سازی ری‌استارت
    ge._cache_mtime = None
    assert ge.notice_ids(CHAT) == [909]


def test_notice_tracking_is_idempotent_and_bounded():
    """ثبت تکراری، شناسه را دوباره اضافه نمی‌کند و فهرست بی‌نهایت رشد نمی‌کند."""
    _fresh_expiry_file()
    ge.set_expiry(CHAT, "یک هفته")
    for _ in range(3):
        ge.add_notice(CHAT, 300)
    assert ge.notice_ids(CHAT) == [300]
    for mid in range(1000, 1000 + ge.NOTICE_LIMIT + 5):
        ge.add_notice(CHAT, mid)
    assert len(ge.notice_ids(CHAT)) == ge.NOTICE_LIMIT
    # آخرین‌ها نگه داشته می‌شوند.
    assert ge.notice_ids(CHAT)[-1] == 1000 + ge.NOTICE_LIMIT + 4


def test_no_notice_tracking_for_invalid_command():
    """دستور نامعتبر هیچ رکورد و هیچ شناسه‌ای نمی‌سازد."""
    _fresh_expiry_file()
    bot, event = Bot(), Event()
    asyncio.run(geh.handle(bot, event, CHAT, _owner(), "سه هفته", bot.logger))
    assert event.out == []
    assert ge.notice_ids(CHAT) == []


# ===========================================================================
# ۲) بیوگرافی: مجازات فقط با شاهد تازه
# ===========================================================================

@pytest.fixture()
def bio_env(tmp_path, monkeypatch):
    monkeypatch.setattr(bio_storage, "FILE", tmp_path / "bio_link.json")
    bio_storage.reset_cache()
    bio_service._retry_after.clear()
    bio_service._inflight.clear()
    monkeypatch.setattr(names_storage, "FILE", tmp_path / "group_names.json")
    names_storage._cache = None
    monkeypatch.setattr(names_storage, "_cache_mtime", None)
    return tmp_path


def test_stale_link_record_is_not_grounds_for_punishment(bio_env, monkeypatch):
    """رکورد «لینک دارد» که کهنه است، پیش از مجازات تازه می‌شود."""
    monkeypatch.setenv("BIO_LINK_PUNISH_FRESHNESS_SECONDS", "60")
    assert bio_service.PUNISH_FRESHNESS_SECONDS <= 120
    bio_storage.set_user(-1001, 55, link=True, label="t.me/x",
                         checked_at=0)  # خیلی کهنه
    assert bio_service.needs_refresh_before_punish(-1001, 55) is True

    bio_storage.set_user(-1001, 56, link=True, label="t.me/x")  # همین حالا
    assert bio_service.needs_refresh_before_punish(-1001, 56) is False

    bio_storage.set_user(-1001, 57, link=False)  # بدون لینک
    assert bio_service.needs_refresh_before_punish(-1001, 57) is False


def test_refresh_clears_flag_after_link_removed(bio_env, monkeypatch):
    """کاربر لینک را حذف کرده → پرچم پاک می‌شود و دیگر مجازات نمی‌شود."""
    calls = []

    async def fake_fetch_about(bot, user):
        calls.append(user.id)
        return bio_service.STATUS_OK, "سلام، خوش آمدید"   # بدون لینک

    monkeypatch.setattr(bio_service, "fetch_about", fake_fetch_about)
    bio_storage.set_enabled(-1001, True)
    bio_storage.set_user(-1001, 60, link=True, label="t.me/old", checked_at=0)

    bot = Bot()
    bot.bot_account_id = 0
    record = asyncio.run(
        bio_service.refresh_before_punish(bot, -1001, User(60))
    )

    assert calls == [60], "بیو باید دوباره خوانده شود"
    assert isinstance(record, dict) and record["link"] is False
    assert bio_service.link_state(-1001, 60) == (False, None)
    assert bio_storage.flagged_users(-1001) == []
    assert bot.logger.has("REFRESH CLEARED")


def test_refresh_keeps_flag_when_link_still_present(bio_env, monkeypatch):
    """اگر لینک هنوز در بیو باشد، مجازات با شاهد تازه ادامه می‌یابد."""
    async def fake_fetch_about(bot, user):
        return bio_service.STATUS_OK, "بیو من t.me/spam"

    monkeypatch.setattr(bio_service, "fetch_about", fake_fetch_about)
    bio_storage.set_enabled(-1002, True)
    bio_storage.set_user(-1002, 61, link=True, label="t.me/spam", checked_at=0)

    bot = Bot()
    bot.bot_account_id = 0
    asyncio.run(bio_service.refresh_before_punish(bot, -1002, User(61)))

    hit, label = bio_service.link_state(-1002, 61)
    assert hit is True and label
    assert any(user_id == "61" for user_id, _ in bio_storage.flagged_users(-1002))


def test_refresh_failure_means_no_punishment(bio_env, monkeypatch):
    """خطای API = شاهد نداریم → مجازات ممنوع (نه مجازات بر پایهٔ کش کهنه)."""
    async def boom(bot, user):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(bio_service, "fetch_about", boom)
    bio_storage.set_enabled(-1003, True)
    bio_storage.set_user(-1003, 62, link=True, label="t.me/old", checked_at=0)

    bot = Bot()
    bot.bot_account_id = 0
    record = asyncio.run(bio_service.refresh_before_punish(bot, -1003, User(62)))
    assert record is None
    # پنجرهٔ تلاش دوباره ثبت شد تا هر پیام یک RPC نسازد.
    assert bio_service._retry_after


def test_fresh_record_costs_no_rpc(bio_env, monkeypatch):
    """رکورد تازه → هیچ RPC؛ تضمین «بدون درخواست اضافی»."""
    calls = []

    async def fake_fetch_about(bot, user):
        calls.append(user.id)
        return bio_service.STATUS_OK, ""

    monkeypatch.setattr(bio_service, "fetch_about", fake_fetch_about)
    bio_storage.set_enabled(-1004, True)
    bio_storage.set_user(-1004, 63, link=True, label="t.me/x")
    bot = Bot()
    bot.bot_account_id = 0
    asyncio.run(bio_service.refresh_before_punish(bot, -1004, User(63)))
    assert calls == []


# ===========================================================================
# ۳) حرف «ک» از کلمات ممنوعه
# ===========================================================================

def test_letter_k_removed_from_banned_words():
    words = {
        line.strip()
        for line in (Path(__file__).resolve().parents[1]
                     / "config" / "banned_words.txt").read_text("utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "ک" not in words
    assert len(words) > 100, "بقیهٔ کلمات باید سر جایشان باشند"
    assert "بیو" in words


def test_letter_k_is_retired_everywhere():
    manager = ConfigManager()
    assert "ک" not in manager.banned_words
    assert "ک" in manager.retired_banned_words()


def test_single_letter_never_matches():
    import modules.group_banned_words_control as control

    manager = ConfigManager()
    detector = SpamDetector(manager)
    control.enable(-2001)
    for text in ("سلام خوبی؟ 😀ک😀", "ک", "بریم ک بازی کنیم", "کتاب", "کیک"):
        hit, _reason = detector.check_banned_words(text, -2001)
        assert hit is False, f"پیام سالم نباید فیلتر شود: {text!r}"


def test_degenerate_words_are_ignored_even_if_readded(monkeypatch):
    """حتی اگر «ک» به فهرست برگردد، محافظ تشخیص جلوی فیلتر غلط را می‌گیرد."""
    import modules.group_banned_words_control as control

    manager = ConfigManager()
    manager.banned_words = set(manager.banned_words) | {"ک", "ی", "x"}
    manager._banned_words_version += 1
    detector = SpamDetector(manager)
    control.enable(-2002)

    assert detector._is_degenerate_banned_word("ک") is True
    assert detector._is_degenerate_banned_word("ی") is True
    assert detector._is_degenerate_banned_word("x") is True
    assert detector._is_degenerate_banned_word("بیو") is False
    assert detector._is_degenerate_banned_word("vpn") is False

    for text in ("سلام 😀ک😀 خوبی", "ی چی", "x"):
        hit, _reason = detector.check_banned_words(text, -2002)
        assert hit is False


def test_other_banned_words_still_work():
    """سایر کلمات ممنوعه بدون تغییر کار می‌کنند (بدون افت عملکرد فیلتر)."""
    import modules.group_banned_words_control as control

    manager = ConfigManager()
    detector = SpamDetector(manager)
    control.enable(-2003)

    for word in ("بیو", "vpn", "www"):
        hit, reason = detector.check_banned_words(f"متن {word} متن", -2003)
        assert hit is True and word in (reason or "")

    # حالت سخت‌گیرانهٔ خاموش → هیچ کلمه‌ای فیلتر نمی‌شود (رفتار قبلی حفظ شد)
    control.disable(-2004)
    hit, _reason = detector.check_banned_words("بیو", -2004)
    assert hit is False


def test_retired_word_is_purged_from_runtime_copy(tmp_path, monkeypatch):
    """نسخهٔ runtime که «ک» را دارد باید پاک‌سازی شود (حذف ماندگار)."""
    runtime = tmp_path / "banned_words.txt"
    repo_file = Path(__file__).resolve().parents[1] / "config" / "banned_words.txt"
    runtime.write_text(repo_file.read_text("utf-8") + "\nک\n", encoding="utf-8")

    manager = ConfigManager()
    monkeypatch.setattr(manager, "banned_words_path", str(runtime))
    moved = manager._drop_retired_from_runtime(manager.retired_banned_words())

    assert moved == 1
    assert "ک" not in runtime.read_text("utf-8").splitlines()
    assert "بیو" in runtime.read_text("utf-8")
