"""🖇 تست کامل قابلیت «تشخیص لینک در بیوگرافی».

پوشش:
  ۱. تشخیص لینک در شکل‌های مختلف نوشتاری + رد کردن متن عادی/بیوی خالی/لینک ناقص
  ۲. ذخیره‌سازی پایدار تنظیمات و وضعیت هر کاربر، TTL و لیست متخلفان
  ۳. لایهٔ واقعی API (``users.getFullUser`` → ``full_user.about``) با کلاینت
     جعلی: بیوی پر، بیوی خالی، خطای API، FloodWait و نبود دسترسی
  ۴. اسکن اعضا: سقف تعداد، پرش از ادمین‌ها و عدم تکرار بررسی داخل TTL
  ۵. مسیر پیام: اخطار طبق سیستم فعلی، حذف پیام، و مجازات فقط با رسیدن به آستانه
  ۶. معافیت VIP/ادمین، گروه خاموش، و جلوگیری از مجازات تکراری
  ۷. دستورهای مدیریتی: فقط مالک/ادمین، روشن/خاموش/وضعیت/لیست/بررسی/پاک‌سازی
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from handlers.message_handler import handle_new_message
from modules import (
    admin_storage,
    bio_link,
    bio_link_service,
    bio_link_storage,
    group_storage,
    punishment_mode,
    user_history,
    user_map,
    vip_storage,
)
from modules import warning_threshold


# ---------------------------------------------------------------------------
# ایزوله‌سازی فایل‌های ذخیره‌سازی
# ---------------------------------------------------------------------------

def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(bio_link_storage, "FILE", tmp_path / "bio_link.json")
    bio_link_storage.reset_cache()

    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    group_storage._cache = None
    group_storage._cache_mtime = None

    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    admin_storage._cache = None
    admin_storage._cache_mtime = None

    monkeypatch.setattr(vip_storage, "FILE", tmp_path / "vip_users.json")
    vip_storage._cache = None
    vip_storage._cache_mtime = None

    monkeypatch.setattr(punishment_mode, "_BASE", tmp_path)
    monkeypatch.setattr(punishment_mode, "_FILE", tmp_path / "punishment_mode.json")
    punishment_mode._cache = None
    punishment_mode._cache_mtime = None

    if hasattr(warning_threshold, "_FILE"):
        monkeypatch.setattr(warning_threshold, "_FILE", tmp_path / "warning_threshold.json")
    if hasattr(warning_threshold, "_cache"):
        warning_threshold._cache = None

    for module, attr in ((user_history, "_FILE"), (user_history, "FILE")):
        if hasattr(user_history, attr):
            monkeypatch.setattr(user_history, attr, tmp_path / "user_history.json")

    # در تست‌ها هیچ RPC پس‌زمینه از مسیر پیام شروع نمی‌شود.
    monkeypatch.setenv("BIO_LINK_CHECK_ON_MESSAGE", "0")
    monkeypatch.setenv("BIO_LINK_CHECK_ON_JOIN", "0")
    monkeypatch.setenv("BIO_LINK_DEFAULT_ENABLED", "1")
    monkeypatch.setenv("BIO_LINK_RPC_DELAY", "0")
    bio_link_service.reset_runtime_state()


# ---------------------------------------------------------------------------
# ۱) تشخیص لینک
# ---------------------------------------------------------------------------

POSITIVE = [
    ("https://example.com", "لینک وب"),
    ("HTTP://SITE.IR/x", "لینک وب"),
    ("hxxp://bad.example.com", "لینک وب"),
    ("www.example.com", "لینک وب"),
    ("کانال ما: t.me/mychannel", "لینک تلگرام"),
    ("t . me / mychannel", "لینک تلگرام"),
    ("telegram.me/group1", "لینک تلگرام"),
    ("t.me/+AbCdEf123456", "لینک دعوت"),
    ("joinchat/AAAAA1234567", "لینک دعوت"),
    ("sapp.ir/ali", "لینک سروش"),
    ("splus.ir/join/xyz", "لینک سروش"),
    ("soroush.ir/ali", "لینک سروش"),
    ("برای خرید به site.com مراجعه کنید", "دامنه"),
    ("shop.ir", "دامنه"),
    ("example [.] com", "دامنه"),
    ("www . example . com", "لینک وب"),
    ("my-site(dot)ir", "دامنه"),
    ("s i t e . c o m", "دامنه"),
    ("𝐬𝐢𝐭𝐞.𝐜𝐨𝐦", "دامنه"),
    ("s i t e . c o m".replace(".", "\u06d4"), "دامنه"),
    ("192.168.1.1:8080/x", "لینک IP"),
]

NEGATIVE = [
    "",
    "   ",
    "هوا امروز خوب است",
    "من علی هستم، ۲۵ ساله از تهران",
    "ورژن 2.5 برنامه",
    "قیمت ۱۰۰۰ تومان",
    "سلام. خوبی؟",
    "نقطه سر خط",
    "1.5 برابر",
    "من مدیر گروه هستم",
    "تست کیبورد @ali",
    "@ab",
]

INCOMPLETE = ["http://", "https://", "//", "www.", "t.me", "t.me/", "sapp.ir", "."]


@pytest.mark.parametrize("text,label", POSITIVE)
def test_link_detection_positive(text, label):
    assert bio_link.find_link(text) == label


@pytest.mark.parametrize("text", NEGATIVE)
def test_link_detection_negative(text):
    assert bio_link.find_link(text) is None


@pytest.mark.parametrize("text", INCOMPLETE)
def test_incomplete_link_is_not_a_link(text):
    assert bio_link.find_link(text) is None


def test_username_needs_context_or_opt_in():
    assert bio_link.find_link("@mychannel") is None
    assert bio_link.find_link("@mychannel", detect_username=True) == "آیدی @mychannel"
    assert bio_link.find_link("کانال رو جوین کن @mychannel") == "آیدی @mychannel"
    assert bio_link.find_link("پیوی @my_support_ir") == "آیدی @my_support_ir"


def test_reason_text_is_short_persian():
    assert bio_link.reason_text(None) == "لینک در بیوگرافی"
    assert bio_link.reason_text("دامنه") == "لینک در بیوگرافی (دامنه)"


# ---------------------------------------------------------------------------
# ۲) ذخیره‌سازی
# ---------------------------------------------------------------------------

def test_storage_settings_state_and_flagged_list(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    chat_id = -10099001

    # پیش‌فرض روشن است و با دستور قابل خاموش کردن
    assert bio_link_storage.is_enabled(chat_id) is True
    assert bio_link_storage.set_enabled(chat_id, False) is True
    assert bio_link_storage.is_enabled(chat_id) is False
    assert bio_link_storage.set_enabled(chat_id, False) is False
    assert bio_link_storage.set_enabled(chat_id, True) is True

    bio_link_storage.set_user(
        chat_id, 501, link=True, label="دامنه", status="ok", bio_len=12, name="@ad_user"
    )
    bio_link_storage.set_user(
        chat_id, 502, link=False, label=None, status="ok", bio_len=4, name="@clean_user"
    )
    bio_link_storage.set_user(
        chat_id, 503, link=False, label=None, status="error", bio_len=0
    )

    assert bio_link_storage.get_user(chat_id, 501)["link"] is True
    flagged = bio_link_storage.flagged_users(chat_id)
    assert [key for key, _row in flagged] == ["501"]

    info = bio_link_storage.stats(chat_id)
    assert info["checked"] == 3
    assert info["with_link"] == 1
    assert info["errors"] == 1

    # پایداری روی دیسک: پس از پاک شدن کش، همان نتیجه خوانده می‌شود.
    bio_link_storage.flush()
    bio_link_storage.reset_cache()
    assert bio_link_storage.get_user(chat_id, 501)["name"] == "@ad_user"

    assert bio_link_storage.clear_group(chat_id) == 3
    assert bio_link_storage.get_user(chat_id, 501) is None


def test_needs_check_respects_ttl(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    chat_id = -10099002
    bio_link_storage.set_user(chat_id, 601, link=False, status="ok", bio_len=3)
    assert bio_link_service.needs_check(chat_id, 601) is False
    # خطای API باید دوباره بررسی شود
    bio_link_storage.set_user(chat_id, 602, link=False, status="error")
    assert bio_link_service.needs_check(chat_id, 602) is True
    # وضعیت قدیمی‌تر از TTL دوباره بررسی می‌شود
    bio_link_storage.set_user(
        chat_id, 603, link=False, status="ok", checked_at=1.0
    )
    assert bio_link_service.needs_check(chat_id, 603) is True


# ---------------------------------------------------------------------------
# ۳) لایهٔ API با کلاینت جعلی
# ---------------------------------------------------------------------------

class FakeClient:
    """کلاینت جعلی که رفتار ``users.getFullUser`` را بازتولید می‌کند."""

    def __init__(self, about=None, *, error=None, full_user_missing=False, participants=None):
        self.about = about
        self.error = error
        self.full_user_missing = full_user_missing
        self.participants = participants or []
        self.calls = 0
        self.participant_calls = 0

    async def get_input_entity(self, entity):
        return SimpleNamespace(user_id=getattr(entity, "id", entity), access_hash=123)

    def __call__(self, request):
        self.calls += 1

        async def _result():
            if self.error is not None:
                raise self.error
            if self.full_user_missing:
                return SimpleNamespace(full_user=None)
            return SimpleNamespace(
                full_user=SimpleNamespace(about=self.about),
                users=[],
                chats=[],
            )

        return _result()

    async def iter_participants(self, entity, limit=None):
        self.participant_calls += 1
        served = 0
        for user in self.participants:
            if limit is not None and served >= limit:
                break
            served += 1
            yield user


def _bot(client, **extra):
    base = SimpleNamespace(
        client=client,
        bot_account_id=9999,
        reply_input_peer_cache={},
        logger=SimpleNamespace(log_info=lambda *_a, **_kw: None,
                               log_error=lambda *_a, **_kw: None),
    )
    for key, value in extra.items():
        setattr(base, key, value)
    return base


@pytest.mark.parametrize(
    "client,status,about",
    [
        (FakeClient(about="t.me/mychannel"), bio_link_service.STATUS_OK, "t.me/mychannel"),
        (FakeClient(about="   "), bio_link_service.STATUS_EMPTY, None),
        (FakeClient(about=None), bio_link_service.STATUS_EMPTY, None),
        (FakeClient(about="x", full_user_missing=True), bio_link_service.STATUS_NO_ACCESS, None),
        (FakeClient(error=RuntimeError("boom")), bio_link_service.STATUS_ERROR, None),
        (FakeClient(error=RuntimeError("FloodWait 30")), bio_link_service.STATUS_FLOOD, None),
    ],
)
def test_fetch_about_statuses(tmp_path, monkeypatch, client, status, about):
    _isolate(tmp_path, monkeypatch)
    bot = _bot(client)
    result_status, result_about = asyncio.run(
        bio_link_service.fetch_about(bot, SimpleNamespace(id=700, access_hash=5, first_name="x"))
    )
    assert result_status == status
    assert result_about == about


def test_check_user_persists_and_marks_link(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    chat_id = -10099003
    client = FakeClient(about="کانال من: t.me/ad_channel")
    bot = _bot(client)

    record = asyncio.run(
        bio_link_service.check_user(bot, chat_id, SimpleNamespace(id=801, username="ad_user"))
    )
    assert record["link"] is True
    assert record["label"] == "لینک تلگرام"
    assert record["name"] == "@ad_user"
    # یک RPC برای بیو + یک RPC برای نقش گروه (معافیت ادمین)، هر دو یک‌بار در TTL
    assert client.calls == 2
    assert bio_link_service.link_state(chat_id, 801) == (True, "لینک تلگرام")

    # دومین بررسی داخل TTL هیچ RPC جدیدی نمی‌زند.
    asyncio.run(bio_link_service.check_user(bot, chat_id, SimpleNamespace(id=801)))
    assert client.calls == 2  # داخل TTL هیچ RPC جدیدی اضافه نشد


def test_check_user_api_error_never_marks_link(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    chat_id = -10099004
    bot = _bot(FakeClient(error=RuntimeError("UserPrivacyRestricted")))
    record = asyncio.run(
        bio_link_service.check_user(bot, chat_id, SimpleNamespace(id=802))
    )
    assert record["link"] is False
    assert record["status"] == bio_link_service.STATUS_NO_ACCESS
    assert bio_link_service.link_state(chat_id, 802) == (False, None)


def test_scan_group_is_bounded_and_skips_admins(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("BIO_LINK_SCAN_LIMIT", "10")
    chat_id = -10099005

    admin_participant = SimpleNamespace(is_admin=True)
    users = [
        SimpleNamespace(id=901, username="ad1", participant=None),
        SimpleNamespace(
            id=902, username="admin1",
            participant=admin_participant,
        ),
        SimpleNamespace(id=903, username="bot1", bot=True),
        SimpleNamespace(id=904, username="clean", participant=None),
    ]
    client = FakeClient(about="site.com", participants=users)
    # هر کاربر جدید همان بیوی لینک‌دار را می‌گیرد.
    bot = _bot(client)

    summary = asyncio.run(bio_link_service.scan_group(bot, chat_id))
    assert summary["status"] == bio_link_service.STATUS_OK
    assert summary["checked"] == 2  # ad1 و clean
    assert summary["found"] == 2
    assert client.participant_calls == 1
    # ادمین و ربات اصلاً بررسی نشدند.
    assert bio_link_storage.get_user(chat_id, 902) is None
    assert bio_link_storage.get_user(chat_id, 903) is None

    # اجرای دوباره بدون force باید همه را skip کند (بدون RPC اضافه).
    calls_before = client.calls
    summary2 = asyncio.run(bio_link_service.scan_group(bot, chat_id))
    assert summary2["checked"] == 0
    assert client.calls == calls_before
    assert summary2["skipped"] >= 2


def test_scan_group_disabled_and_no_access(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    chat_id = -10099006
    bio_link_storage.set_enabled(chat_id, False)
    bot = _bot(FakeClient(about="site.com"))
    summary = asyncio.run(bio_link_service.scan_group(bot, chat_id))
    assert summary["status"] == "disabled"

    bio_link_storage.set_enabled(chat_id, True)

    class _NoResolve(FakeClient):
        async def get_input_entity(self, entity):
            raise RuntimeError("Cannot find any entity")

    summary2 = asyncio.run(bio_link_service.scan_group(_bot(_NoResolve()), chat_id))
    assert summary2["status"] == bio_link_service.STATUS_NO_ACCESS


# ---------------------------------------------------------------------------
# ۴) مسیر واقعی پیام
# ---------------------------------------------------------------------------

class FakeTracker:
    def __init__(self):
        self.counts = {}
        self.banned = set()
        self.muted = set()

    def increment(self, chat_id, user_id):
        key = (str(chat_id), str(user_id))
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def get_count(self, chat_id, user_id):
        return self.counts.get((str(chat_id), str(user_id)), 0)

    def should_punish(self, chat_id, user_id, threshold=None):
        threshold = threshold or 3
        return self.get_count(chat_id, user_id) >= threshold

    def reset_count(self, chat_id, user_id):
        self.counts.pop((str(chat_id), str(user_id)), None)

    def is_banned(self, *_a, **_kw):
        return False

    def is_muted(self, *_a, **_kw):
        return False

    def save(self, *_a, **_kw):
        return None


class FakeAdminActions:
    bot_account_id = 9999

    def __init__(self, punish_result=True):
        self.warnings = []
        self.punishes = []
        self.bans = []
        self.mutes = []
        self.punish_result = punish_result

    async def send_warning(self, chat_id, username, reason, count, threshold,
                           reply_to=None, user=None, is_edit=False):
        self.warnings.append(
            {"chat_id": chat_id, "username": username, "reason": reason,
             "count": count, "threshold": threshold}
        )

    async def punish_user(self, chat_id, user_id, username=None, announce=True,
                          *, user=None, chat=None):
        self.punishes.append((chat_id, user_id))
        return self.punish_result

    async def ban_user(self, chat_id, user_id, reason="", user=None, chat=None):
        self.bans.append((chat_id, user_id, reason))
        return True

    async def mute_user(self, chat_id, user_id, duration=None, user=None, chat=None):
        self.mutes.append((chat_id, user_id))
        return True


class ImmediateModerationQueue:
    def enqueue(self, chat_id, action, user_id=None, timeout_seconds=45,
                operation=None, on_success=None, on_failure=None):
        async def _run():
            try:
                result = await operation()
                if on_success:
                    await on_success(result)
            except Exception as error:  # pragma: no cover
                if on_failure:
                    await on_failure(error)

        asyncio.get_running_loop().create_task(_run())
        return True


class FakeEvent:
    def __init__(self, chat_id, sender, text, msg_id=101):
        self.chat_id = chat_id
        self.sender_id = getattr(sender, "id", None)
        self._sender = sender
        self.is_private = False
        self.out = False
        self.chat = SimpleNamespace(id=chat_id, title="گروه تست بیو")
        self.message = SimpleNamespace(
            id=msg_id,
            message=text,
            reply_to_msg_id=None,
            entities=None,
            file=None,
            media=None,
            fwd_from=None,
            edit_date=None,
        )
        self.replies = []

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return self.chat

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(id=900 + len(self.replies))


def _make_bot(tmp_path, monkeypatch, admin_actions=None):
    deleted_batches = []
    spam_lock = {}
    tracker = FakeTracker()
    admin_actions = admin_actions or FakeAdminActions()

    bot = SimpleNamespace(
        bot_account_id=9999,
        punished_users=set(),
        spam_burst_users=set(),
        spam_burst_messages={},
        spam_burst_tasks={},
        forward_spam_counts={},
        forward_spam_processing=set(),
        rejoin_spam_state={},
        native_admin_state_cleared=set(),
        _spammer_messages_touched=set(),
        group_timer_tasks={},
        spam_lock=spam_lock,
        is_spam_locked=lambda key: bool(spam_lock.get(key)),
        set_spam_lock=lambda key, value=True: spam_lock.__setitem__(key, value),
        clear_spam_lock=lambda key: spam_lock.pop(key, None),
        delete_notice_lock={},
        bot_sent_messages=[],
        reply_input_peer_cache={},
        moderation_queue=ImmediateModerationQueue(),
        admin_actions=admin_actions,
        message_delete_queue=SimpleNamespace(
            enqueue=lambda cid, ids, **_kw: deleted_batches.append((cid, list(ids)))
        ),
        outgoing_sender=None,
        notice_cleanup=None,
        config_manager=SimpleNamespace(get=lambda _k, default=None: default),
        detector=SimpleNamespace(
            is_spam=lambda *_a, **_kw: (False, ""),
            has_public_username=lambda *_a, **_kw: False,
            check_banned_words=lambda *_a, **_kw: (False, None),
        ),
        flood_detector=SimpleNamespace(check_flood=lambda *_a, **_kw: (False, [])),
        activity_tracker=SimpleNamespace(track_message=lambda *_a, **_kw: None),
        message_tracker=SimpleNamespace(
            track_message=lambda *_a, **_kw: None,
            get_user_messages=lambda *_a, **_kw: [],
            clear_user_messages=lambda *_a, **_kw: None,
        ),
        tracker=tracker,
        logger=SimpleNamespace(
            log_info=lambda *_a, **_kw: None,
            log_error=lambda *_a, **_kw: None,
            log_deleted_message=lambda **_kw: None,
        ),
        client=SimpleNamespace(),
    )
    return bot, deleted_batches, tracker, admin_actions


def _setup_group(tmp_path, monkeypatch, chat_id, owner_id=1001):
    _isolate(tmp_path, monkeypatch)
    group_storage.activate_group(chat_id, "گروه تست بیو")
    group_storage.set_group_owner(chat_id, owner_id)
    monkeypatch.setattr(user_map, "FILE", tmp_path / "user_map.json")


def test_message_from_link_bio_user_warns_then_punishes(tmp_path, monkeypatch):
    chat_id = -10099101
    _setup_group(tmp_path, monkeypatch, chat_id)
    punishment_mode.set_mode(chat_id, punishment_mode.MODE_BAN)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    user = SimpleNamespace(id=1101, first_name="تبلیغ", last_name="کار", username="ad_1101")
    bio_link_storage.set_user(
        chat_id, user.id, link=True, label="دامنه", status="ok", name="@ad_1101"
    )

    async def _scenario():
        # پیام اول: حذف + اخطار ۱ (بدون مجازات)
        ev1 = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=1101)
        await handle_new_message(bot, ev1)
        await asyncio.sleep(0.05)
        assert any(1101 in ids for _cid, ids in deleted)
        assert len(actions.warnings) == 1
        assert actions.warnings[0]["count"] == 1
        assert "لینک در بیوگرافی" in actions.warnings[0]["reason"]
        assert actions.punishes == []

        # پیام دوم: اخطار ۲
        ev2 = FakeEvent(chat_id, user, "کسی اینجا نیست", msg_id=1102)
        await handle_new_message(bot, ev2)
        await asyncio.sleep(0.05)
        assert actions.warnings[-1]["count"] == 2
        assert actions.punishes == []

        # پیام سوم: رسیدن به آستانه -> مجازات طبق تنظیم گروه
        ev3 = FakeEvent(chat_id, user, "پیام سوم من", msg_id=1103)
        await handle_new_message(bot, ev3)
        await asyncio.sleep(0.05)
        assert actions.punishes == [(chat_id, user.id)]
        assert bot.is_spam_locked((chat_id, user.id))
        assert actions.warnings[-1]["threshold"] == 3

    asyncio.run(_scenario())


def test_clean_and_empty_bio_users_are_not_punished(tmp_path, monkeypatch):
    chat_id = -10099102
    _setup_group(tmp_path, monkeypatch, chat_id)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    clean = SimpleNamespace(id=1201, first_name="پاک", username="clean_1201")
    empty = SimpleNamespace(id=1202, first_name="خالی", username="empty_1202")
    unknown = SimpleNamespace(id=1203, first_name="نامعلوم", username="unknown_1203")
    bio_link_storage.set_user(chat_id, clean.id, link=False, status="ok", bio_len=20)
    bio_link_storage.set_user(chat_id, empty.id, link=False, status="empty")

    async def _scenario():
        for index, user in enumerate((clean, empty, unknown)):
            event = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=1200 + index)
            await handle_new_message(bot, event)
            await asyncio.sleep(0.02)
        assert actions.warnings == []
        assert actions.punishes == []
        assert deleted == []

    asyncio.run(_scenario())


def test_api_error_bio_does_not_cause_punishment(tmp_path, monkeypatch):
    chat_id = -10099103
    _setup_group(tmp_path, monkeypatch, chat_id)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    user = SimpleNamespace(id=1301, first_name="دسترسی", username="noaccess_1301")
    bio_link_storage.set_user(
        chat_id, user.id, link=False, status=bio_link_service.STATUS_NO_ACCESS
    )

    async def _scenario():
        event = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=1301)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.02)
        assert actions.warnings == []
        assert actions.punishes == []

    asyncio.run(_scenario())


def test_vip_and_registered_admin_are_exempt(tmp_path, monkeypatch):
    chat_id = -10099104
    _setup_group(tmp_path, monkeypatch, chat_id)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    vip = SimpleNamespace(id=1401, first_name="وی‌آی‌پی", username="vip_1401")
    admin = SimpleNamespace(id=1402, first_name="ادمین", username="admin_1402")
    vip_storage.add_vip(chat_id, vip.id)
    admin_storage.add_admin(chat_id, admin.id)
    for user in (vip, admin):
        bio_link_storage.set_user(
            chat_id, user.id, link=True, label="دامنه", status="ok"
        )

    async def _scenario():
        for index, user in enumerate((vip, admin)):
            event = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=1400 + index)
            await handle_new_message(bot, event)
            await asyncio.sleep(0.02)
        assert actions.warnings == []
        assert actions.punishes == []
        assert deleted == []

    asyncio.run(_scenario())


def test_punishment_is_not_repeated_for_same_user(tmp_path, monkeypatch):
    chat_id = -10099105
    _setup_group(tmp_path, monkeypatch, chat_id)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    user = SimpleNamespace(id=1501, first_name="تکراری", username="dup_1501")
    bio_link_storage.set_user(chat_id, user.id, link=True, label="دامنه", status="ok")
    # شبیه‌سازی این‌که مجازات قبلاً در حال اجرا/انجام شده است.
    bot.punished_users.add(f"{chat_id}:{user.id}")

    async def _scenario():
        event = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=1501)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert actions.punishes == []
        # پیام همچنان حذف می‌شود و اخطار شمرده می‌شود.
        assert any(1501 in ids for _cid, ids in deleted)
        assert len(actions.warnings) == 1

    asyncio.run(_scenario())


def test_disabled_group_does_not_moderate_bio_links(tmp_path, monkeypatch):
    chat_id = -10099106
    _setup_group(tmp_path, monkeypatch, chat_id)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    user = SimpleNamespace(id=1601, first_name="خاموش", username="off_1601")
    bio_link_storage.set_enabled(chat_id, False)
    bio_link_storage.set_user(chat_id, user.id, link=True, label="دامنه", status="ok")

    async def _scenario():
        event = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=1601)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.02)
        assert actions.warnings == []
        assert actions.punishes == []
        assert deleted == []

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# ۵) دستورهای مدیریتی
# ---------------------------------------------------------------------------

def _commands():
    return [
        "فیلتر لینک بیو",
        "لغو فیلتر لینک بیو",
        "وضعیت لینک بیو",
        "بررسی بیو",
        "لیست لینک بیو",
        "پاک لیست لینک بیو",
        "فیلتر آیدی بیو",
        "لغو فیلتر آیدی بیو",
    ]


@pytest.mark.parametrize("command", _commands())
def test_command_matching_accepts_bare_bang_and_slash(command):
    assert bio_link_service.is_command(command)
    assert bio_link_service.is_command("!" + command)
    assert bio_link_service.is_command("/" + command)
    assert bio_link_service.is_command("  " + command + "  ")


def _command_name(command):
    """نام عمل دستور برای راستی‌آزمایی."""
    return bio_link_service.match_command(command)


def test_command_actions_map_to_expected_operations():
    assert _command_name("فیلتر لینک بیو") == "enable"
    assert _command_name("لغو فیلتر لینک بیو") == "disable"
    assert _command_name("وضعیت لینک بیو") == "status"
    assert _command_name("بررسی بیو") == "scan"
    assert _command_name("لیست لینک بیو") == "list"
    assert _command_name("پاک لیست لینک بیو") == "clear"
    assert _command_name("فیلتر آیدی بیو") == "username_on"
    assert _command_name("لغو فیلتر آیدی بیو") == "username_off"
    assert _command_name("فیلتر بیو") is None  # دستور فیلتر کلمات گروه، نه این قابلیت
    assert _command_name("هوا امروز خوب است") is None


def test_commands_require_management_permission(tmp_path, monkeypatch):
    chat_id = -10099201
    _setup_group(tmp_path, monkeypatch, chat_id, owner_id=1001)
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    stranger = SimpleNamespace(id=2002, first_name="ناشناس", username="stranger")

    async def _scenario():
        for command in ("فیلتر لینک بیو", "بررسی بیو", "پاک لیست لینک بیو"):
            event = FakeEvent(chat_id, stranger, command, msg_id=2000)
            await handle_new_message(bot, event)
            assert event.replies, command
            assert "فقط" in event.replies[-1][0]
            assert bio_link_storage.is_enabled(chat_id) is True  # بدون تغییر

    asyncio.run(_scenario())


def test_owner_can_enable_disable_status_list_scan_clear(tmp_path, monkeypatch):
    chat_id = -10099202
    owner_id = 1001
    _setup_group(tmp_path, monkeypatch, chat_id, owner_id=owner_id)
    bio_link_storage.set_user(
        chat_id, 3001, link=True, label="لینک تلگرام", status="ok", name="@ad_3001"
    )
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    owner = SimpleNamespace(id=owner_id, first_name="مالک", username="owner_u")

    async def _scenario():
        # وضعیت
        ev = FakeEvent(chat_id, owner, "وضعیت لینک بیو", msg_id=3001)
        await handle_new_message(bot, ev)
        status_text, status_kwargs = ev.replies[-1]
        assert "وضعیت فیلتر لینک بیو" in status_text
        assert "روشن" in status_text
        assert status_kwargs.get("formatting_entities")

        # لیست
        ev = FakeEvent(chat_id, owner, "لیست لینک بیو", msg_id=3002)
        await handle_new_message(bot, ev)
        list_text, _ = ev.replies[-1]
        assert "@ad_3001" in list_text
        assert "لینک تلگرام" in list_text

        # خاموش کردن
        ev = FakeEvent(chat_id, owner, "لغو فیلتر لینک بیو", msg_id=3003)
        await handle_new_message(bot, ev)
        assert "خاموش شد" in ev.replies[-1][0]
        assert bio_link_storage.is_enabled(chat_id) is False

        # روشن کردن
        ev = FakeEvent(chat_id, owner, "فیلتر لینک بیو", msg_id=3004)
        await handle_new_message(bot, ev)
        assert any("روشن شد" in text for text, _kw in ev.replies)
        assert bio_link_storage.is_enabled(chat_id) is True

        # بررسی فوری (اسکن در پس‌زمینه با کلاینت بدون participants)
        ev = FakeEvent(chat_id, owner, "بررسی بیو", msg_id=3005)
        await handle_new_message(bot, ev)
        assert "بررسی بیوی اعضا" in ev.replies[-1][0]

        # پاک کردن نتایج
        ev = FakeEvent(chat_id, owner, "پاک لیست لینک بیو", msg_id=3006)
        await handle_new_message(bot, ev)
        assert "پاک شد" in ev.replies[-1][0]
        assert bio_link_storage.get_user(chat_id, 3001) is None

    asyncio.run(_scenario())


def test_username_toggle_clears_previous_state(tmp_path, monkeypatch):
    chat_id = -10099203
    _setup_group(tmp_path, monkeypatch, chat_id, owner_id=1001)
    bio_link_storage.set_user(chat_id, 4001, link=False, status="ok")
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    owner = SimpleNamespace(id=1001, first_name="مالک", username="owner_u")

    async def _scenario():
        ev = FakeEvent(chat_id, owner, "فیلتر آیدی بیو", msg_id=4001)
        await handle_new_message(bot, ev)
        assert bio_link_storage.detect_username(chat_id) is True
        assert bio_link_storage.get_user(chat_id, 4001) is None
        assert "روشن شد" in ev.replies[-1][0]

        ev = FakeEvent(chat_id, owner, "لغو فیلتر آیدی بیو", msg_id=4002)
        await handle_new_message(bot, ev)
        assert bio_link_storage.detect_username(chat_id) is False

    asyncio.run(_scenario())


def test_error_backoff_prevents_repeated_rpc(tmp_path, monkeypatch):
    """خطای API نباید برای هر پیام همان کاربر یک RPC تازه بسازد."""
    _isolate(tmp_path, monkeypatch)
    chat_id = -10099301
    client = FakeClient(error=RuntimeError("boom"))
    bot = _bot(client)
    user = SimpleNamespace(id=1701, username="err_user")

    async def _scenario():
        asyncio.run  # noqa: B018 - فقط برای خوانایی
        await bio_link_service.check_user(bot, chat_id, user)
        assert client.calls == 1
        # بررسی دوباره در بازهٔ backoff صف نمی‌شود
        monkeypatch.setenv("BIO_LINK_CHECK_ON_MESSAGE", "1")
        scheduled = bio_link_service.schedule_check(bot, chat_id, user)
        assert scheduled is False
        await asyncio.sleep(0)
        assert client.calls == 1

    asyncio.run(_scenario())
    bio_link_service.reset_runtime_state()


def test_watcher_skips_disabled_groups(tmp_path, monkeypatch):
    """ناظر فقط گروه‌های فعالِ دارای این فیلتر را اسکن می‌کند."""
    _isolate(tmp_path, monkeypatch)
    enabled_chat = -10099302
    disabled_chat = -10099303
    group_storage.activate_group(enabled_chat, "فعال")
    group_storage.activate_group(disabled_chat, "خاموش")
    bio_link_storage.set_enabled(enabled_chat, True)
    bio_link_storage.set_enabled(disabled_chat, False)

    assert enabled_chat in bio_link_service.active_groups()
    assert disabled_chat in bio_link_service.active_groups()

    client = FakeClient(about="site.com", participants=[SimpleNamespace(id=1801)])
    bot = _bot(client)
    asyncio.run(bio_link_service.scan_group(bot, enabled_chat))
    assert bio_link_storage.get_user(enabled_chat, 1801)["link"] is True
    summary = asyncio.run(bio_link_service.scan_group(bot, disabled_chat))
    assert summary["status"] == "disabled"
    assert bio_link_storage.get_user(disabled_chat, 1801) is None


def test_fetch_about_parses_real_splusthon_types(tmp_path, monkeypatch):
    """اثبات مسیر واقعی: پاسخ ``users.UserFull`` خودِ SPlusthon خوانده می‌شود."""
    _isolate(tmp_path, monkeypatch)
    from splusthon.tl.types import PeerSettings, PeerNotifySettings, UserFull
    from splusthon.tl.types import users as tl_users

    full_user = UserFull(
        id=1901,
        settings=PeerSettings(),
        notify_settings=PeerNotifySettings(),
        common_chats_count=0,
        about="کانال ما: t.me/real_channel",
    )
    response = tl_users.UserFull(full_user=full_user, chats=[], users=[])

    class RealTypeClient(FakeClient):
        def __call__(self, request):
            self.calls += 1
            assert type(request).__name__ == "GetFullUserRequest"

            async def _result():
                return response

            return _result()

    bot = _bot(RealTypeClient())
    status, about = asyncio.run(
        bio_link_service.fetch_about(bot, SimpleNamespace(id=1901, access_hash=42))
    )
    assert status == bio_link_service.STATUS_OK
    assert about == "کانال ما: t.me/real_channel"
    assert bio_link.find_link(about) == "لینک تلگرام"


# ---------------------------------------------------------------------------
# ۶) سیاست «فقط کسانی که پیام می‌دهند»
# ---------------------------------------------------------------------------

def _enable_live_checks(monkeypatch):
    monkeypatch.setenv("BIO_LINK_CHECK_ON_MESSAGE", "1")
    monkeypatch.setenv("BIO_LINK_AWAIT_FIRST_CHECK", "1")
    monkeypatch.setenv("BIO_LINK_AWAIT_TIMEOUT", "5")
    bio_link_service.reset_runtime_state()


def test_first_message_triggers_live_bio_check_and_both_are_moderated(tmp_path, monkeypatch):
    """کاربری که اولین پیامش را می‌فرستد، همان لحظه بررسی می‌شود."""
    chat_id = -10099401
    _setup_group(tmp_path, monkeypatch, chat_id)
    _enable_live_checks(monkeypatch)
    client = FakeClient(about="کانال من: t.me/ad_channel")
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = SimpleNamespace(id=2101, first_name="تازه", username="new_2101")

    async def _scenario():
        assert bio_link_storage.get_user(chat_id, user.id) is None
        ev = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=2101)
        await handle_new_message(bot, ev)
        await asyncio.sleep(0.1)
        # بیو همان لحظه خوانده و ذخیره شد (بیو + بررسی نقش گروه)
        assert client.calls == 2
        record = bio_link_storage.get_user(chat_id, user.id)
        assert record["link"] is True
        assert record["label"] == "لینک تلگرام"
        # و همان پیام اول حذف + اخطار شد
        assert any(2101 in ids for _cid, ids in deleted)
        assert len(actions.warnings) == 1
        assert "لینک در بیوگرافی" in actions.warnings[0]["reason"]

        # پیام دوم هیچ RPC جدیدی نمی‌زند
        ev2 = FakeEvent(chat_id, user, "پیام دوم من", msg_id=2102)
        await handle_new_message(bot, ev2)
        await asyncio.sleep(0.05)
        assert client.calls == 2
        assert actions.warnings[-1]["count"] == 2

    asyncio.run(_scenario())


def test_first_message_with_clean_bio_is_not_punished(tmp_path, monkeypatch):
    chat_id = -10099402
    _setup_group(tmp_path, monkeypatch, chat_id)
    _enable_live_checks(monkeypatch)
    client = FakeClient(about="برنامه‌نویس پایتون، اهل تهران")
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = SimpleNamespace(id=2201, first_name="پاک", username="clean_2201")

    async def _scenario():
        ev = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=2201)
        await handle_new_message(bot, ev)
        await asyncio.sleep(0.1)
        assert client.calls == 1  # فقط بیو؛ چون لینکی نبود نقش هم پرسیده نشد
        assert bio_link_storage.get_user(chat_id, user.id)["link"] is False
        assert actions.warnings == []
        assert deleted == []

    asyncio.run(_scenario())


def test_first_message_with_inaccessible_bio_is_not_punished(tmp_path, monkeypatch):
    chat_id = -10099403
    _setup_group(tmp_path, monkeypatch, chat_id)
    _enable_live_checks(monkeypatch)
    client = FakeClient(error=RuntimeError("UserPrivacyRestricted"))
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = SimpleNamespace(id=2301, first_name="خصوصی", username="priv_2301")

    async def _scenario():
        ev = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=2301)
        await handle_new_message(bot, ev)
        await asyncio.sleep(0.1)
        record = bio_link_storage.get_user(chat_id, user.id)
        assert record["status"] == bio_link_service.STATUS_NO_ACCESS
        assert record["link"] is False
        assert actions.warnings == []
        assert deleted == []

    asyncio.run(_scenario())


def test_slow_api_never_blocks_message_and_falls_back_to_background(tmp_path, monkeypatch):
    chat_id = -10099404
    _setup_group(tmp_path, monkeypatch, chat_id)
    _enable_live_checks(monkeypatch)
    monkeypatch.setenv("BIO_LINK_AWAIT_TIMEOUT", "1")

    class SlowClient(FakeClient):
        def __call__(self, request):
            self.calls += 1

            async def _result():
                await asyncio.sleep(5)
                return SimpleNamespace(
                    full_user=SimpleNamespace(about="t.me/x"), users=[], chats=[]
                )

            return _result()

    client = SlowClient()
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = SimpleNamespace(id=2401, first_name="کند", username="slow_2401")

    async def _scenario():
        start = asyncio.get_running_loop().time()
        ev = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=2401)
        await handle_new_message(bot, ev)
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 3.5, f"پیام بیش از حد بلاک شد: {elapsed}"
        # این پیام مجازات نمی‌شود (نتیجه هنوز نرسیده)
        assert actions.warnings == []
        assert deleted == []

    asyncio.run(_scenario())
    bio_link_service.reset_runtime_state()


def test_member_auto_scan_is_off_by_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("BIO_LINK_AUTO_SCAN_MEMBERS", raising=False)
    assert bio_link_service.auto_scan_members() is False
    monkeypatch.setenv("BIO_LINK_AUTO_SCAN_MEMBERS", "1")
    assert bio_link_service.auto_scan_members() is True


def test_watcher_skips_scanning_when_policy_is_messages_only(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("BIO_LINK_AUTO_SCAN_MEMBERS", "0")
    monkeypatch.setenv("BIO_LINK_START_DELAY", "0")
    monkeypatch.setenv("BIO_LINK_SCAN_INTERVAL", "300")
    chat_id = -10099405
    group_storage.activate_group(chat_id, "فعال")
    client = FakeClient(about="site.com", participants=[SimpleNamespace(id=2501)])
    bot = _bot(client)

    async def _scenario():
        task = asyncio.create_task(bio_link_service.watcher(bot))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())
    assert client.participant_calls == 0
    assert bio_link_storage.get_user(chat_id, 2501) is None


# ---------------------------------------------------------------------------
# ۷) معافیت ادمین‌های گروه/مالک/ثبت‌شده/VIP + قالب پیام
# ---------------------------------------------------------------------------

class RoleClient(FakeClient):
    """کلاینت جعلی که نقش گروه را هم برمی‌گرداند."""

    def __init__(self, about, role="ChannelParticipantAdmin"):
        super().__init__(about=about)
        self.role = role

    def __call__(self, request):
        self.calls += 1
        name = type(request).__name__

        async def _result():
            if name == "GetParticipantRequest":
                participant = (
                    None if self.role is None
                    else type(self.role, (), {})()
                )
                return SimpleNamespace(participant=participant)
            return SimpleNamespace(
                full_user=SimpleNamespace(about=self.about), users=[], chats=[]
            )

        return _result()


def test_native_group_admin_with_link_bio_is_exempt(tmp_path, monkeypatch):
    chat_id = -10099501
    _setup_group(tmp_path, monkeypatch, chat_id)
    _enable_live_checks(monkeypatch)
    client = RoleClient(about="t.me/ad_channel", role="ChannelParticipantAdmin")
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = SimpleNamespace(id=3101, first_name="ادمین", username="admin_3101")

    async def _scenario():
        ev = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=3101)
        await handle_new_message(bot, ev)
        await asyncio.sleep(0.1)
        record = bio_link_storage.get_user(chat_id, user.id)
        assert record["exempt"] == "admin"
        assert actions.warnings == []
        assert deleted == []
        # لیست متخلفان هم او را نشان نمی‌دهد
        assert bio_link_storage.flagged_users(chat_id) == []

    asyncio.run(_scenario())


def test_group_creator_with_link_bio_is_exempt(tmp_path, monkeypatch):
    chat_id = -10099502
    _setup_group(tmp_path, monkeypatch, chat_id)
    _enable_live_checks(monkeypatch)
    client = RoleClient(about="https://shop.example.com", role="ChannelParticipantCreator")
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = SimpleNamespace(id=3201, first_name="سازنده", username="creator_3201")

    async def _scenario():
        ev = FakeEvent(chat_id, user, "هوا امروز خوب است", msg_id=3201)
        await handle_new_message(bot, ev)
        await asyncio.sleep(0.1)
        assert bio_link_storage.get_user(chat_id, user.id)["exempt"] == "creator"
        assert actions.warnings == []
        assert deleted == []

    asyncio.run(_scenario())


def test_warning_template_matches_required_format(tmp_path, monkeypatch):
    """قالب اخطار: کاربر / خط خالی / دلیل / خط خالی / تعداد اخطار."""
    from modules.admin_actions import AdminActions
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold

    sent = {}

    class Client:
        _outgoing_sender = None

        async def send_message(self, chat_id, text, **kwargs):
            sent["chat_id"] = chat_id
            sent["text"] = text
            sent["entities"] = kwargs.get("formatting_entities")
            return SimpleNamespace(id=1)

    actions = AdminActions(
        client=Client(),
        logger=SimpleNamespace(log_info=lambda *a, **k: None,
                               log_error=lambda *a, **k: None),
        config_manager=SimpleNamespace(get=lambda k, d=None: d),
    )
    user = SimpleNamespace(id=4101, first_name="", last_name="", username="Sjskajk")

    async def _run():
        await actions.send_warning(
            chat_id=-1001, username="Sjskajk", user=user,
            reason="لینک در بیوگرافی (دامنه)", count=1, threshold=5,
        )

    asyncio.run(_run())
    assert sent["text"] == (
        "⚠️ کاربر @Sjskajk\n"
        "پیام شما حذف شد | دلیل: لینک در بیوگرافی\n"
        "\n"
        "\n"
        "تعداد اخطار: 𝟭/𝟱"
    )
    entities = sent["entities"]
    text = sent["text"]
    quote = [e for e in entities if isinstance(e, MessageEntityBlockquote)]
    bold = [e for e in entities if isinstance(e, MessageEntityBold)]
    u16 = lambda v: len(v.encode("utf-16-le")) // 2
    # نقل‌قول فقط خط عنوان را می‌گیرد و هیچ خط خالی داخلش نیست
    assert len(quote) == 1 and quote[0].offset == 0
    assert quote[0].length == u16("⚠️ کاربر @Sjskajk")
    # «تعداد اخطار: 𝟭/𝟱» کامل Bold است
    assert len(bold) == 1
    assert text[bold[0].offset:bold[0].offset + bold[0].length] == "تعداد اخطار: 𝟭/𝟱"
