# -*- coding: utf-8 -*-
"""پروبِ باگِ «لینک در بیو» — دو شکایتِ کاربر:

۱) کاربری که لینک را از بیو **برداشته**، باز هم اخطار/مجازات می‌گیرد.
۲) کاربری که **لینک ندارد** («الکی») اخطار می‌گیرد (install.sh ، 1.0.0.0 …).

اجرا:  PYTHONPATH=. python3 tests/probe_bio_link_stale.py
"""
import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402

from modules import bio_link, bio_link_service, bio_link_storage  # noqa: E402
from modules import punishment_mode, user_profile_cache  # noqa: E402

PASSED = 0
FAILED = 0


def check(title, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {title}")
    else:
        FAILED += 1
        print(f"  ❌ {title}  {detail}")


def _guard():
    """ماژولِ تستِ موجود را به‌عنوان ابزار (FakeEvent/_make_bot) بار می‌کند."""
    spec = importlib.util.spec_from_file_location(
        "bioguard", str(pathlib.Path(__file__).resolve().parent / "test_bio_link_guard.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GUARD = _guard()


def _scenario(chat_id, *, bio_text, age_seconds, api_error=False):
    """رکوردِ «لینک دارد» با سنِ مشخص + بیوی فعلیِ داده‌شده."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch = MonkeyPatch()
    GUARD._setup_group(tmp, monkeypatch, chat_id)
    # مثل رباتِ واقعی: بررسی بیو روی پیام روشن است.
    os.environ["BIO_LINK_CHECK_ON_MESSAGE"] = "1"
    bio_link_service.reset_runtime_state()
    punishment_mode.set_mode(chat_id, punishment_mode.MODE_BAN)

    class _BioClient:
        async def get_input_entity(self, user):
            return SimpleNamespace(id=user.id, access_hash=1)

        async def __call__(self, request):
            if api_error:
                raise RuntimeError("FloodWait 30")
            return SimpleNamespace(
                full_user=SimpleNamespace(about=bio_text), users=[], chats=[])

        async def iter_participants(self, entity, limit=None, **_kw):
            if False:  # pragma: no cover
                yield None

    bot, _deleted, _tracker, actions = GUARD._make_bot(tmp, monkeypatch)
    bot.client = _BioClient()
    bot.reply_input_peer_cache = {}
    user = SimpleNamespace(id=3101, first_name="کاربر", username="user_3101")
    bio_link_storage.set_user(
        chat_id, user.id, link=True, label="لینک تلگرام", status="ok",
        checked_at=time.time() - age_seconds)
    user_profile_cache._CACHE.clear()
    user_profile_cache._LAST_CALL.clear()
    return bot, actions, user


async def _send_three(bot, actions, user, chat_id):
    for index in range(3):
        event = GUARD.FakeEvent(chat_id, user, f"پیام {index}", msg_id=6000 + index)
        await GUARD.handle_new_message(bot, event)
        await asyncio.sleep(0.15)
    return actions


def test_removed_link_does_not_punish():
    print("\n### ۱) کاربر لینک را از بیو برداشته → نباید اخطار/مجازات بگیرد")
    for chat_id, age, title in (
        (-10099501, 60, "شاهدِ ۶۰ ثانیه‌ای (پنجرهٔ تازگی)"),
        (-10099502, 300, "شاهدِ ۵ دقیقه‌ای"),
    ):
        bot, actions, user = _scenario(chat_id, bio_text="برنامه‌نویس پایتون",
                                       age_seconds=age)
        asyncio.run(_send_three(bot, actions, user, chat_id))
        record = bio_link_storage.get_user(chat_id, user.id)
        check(f"{title}: هیچ اخطاری نبود", not actions.warnings, f"{actions.warnings}")
        check(f"{title}: هیچ مجازاتی نبود", not actions.punishes, f"{actions.punishes}")
        check(f"{title}: رکورد از «لینک دارد» پاک شد",
              bool(record) and record.get("link") is False, f"{record}")


def test_api_error_keeps_old_state_but_never_punishes():
    print("\n### ۲) سرویس خطا می‌دهد → شاهدِ کهنه نباید مجازات کند")
    chat_id = -10099503
    bot, actions, user = _scenario(chat_id, bio_text="برنامه‌نویس پایتون",
                                   age_seconds=300, api_error=True)
    asyncio.run(_send_three(bot, actions, user, chat_id))
    check("هیچ اخطاری نبود", not actions.warnings, f"{actions.warnings}")
    check("هیچ مجازاتی نبود", not actions.punishes, f"{actions.punishes}")


def test_real_link_still_punishes():
    print("\n### ۳) بیوی واقعاً لینک‌دار → همان رفتار قبلی (اخطار تا آستانه)")

    class _AlwaysLink:
        async def get_input_entity(self, user):
            return SimpleNamespace(id=user.id, access_hash=1)

        async def __call__(self, request):
            return SimpleNamespace(
                full_user=SimpleNamespace(about="کانال من: t.me/ad_channel"),
                users=[], chats=[])

    chat_id = -10099504
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch = MonkeyPatch()
    GUARD._setup_group(tmp, monkeypatch, chat_id)
    os.environ["BIO_LINK_CHECK_ON_MESSAGE"] = "1"
    bio_link_service.reset_runtime_state()
    punishment_mode.set_mode(chat_id, punishment_mode.MODE_BAN)
    bot, _deleted, _tracker, actions = GUARD._make_bot(tmp, monkeypatch)
    bot.client = _AlwaysLink()
    bot.reply_input_peer_cache = {}
    user = SimpleNamespace(id=3102, first_name="تبلیغ", username="ad_3102")
    asyncio.run(_send_three(bot, actions, user, chat_id))
    check("اخطار داده شد", len(actions.warnings) >= 1, f"{actions.warnings}")
    check("در پیام سوم مجازات شد", len(actions.punishes) == 1, f"{actions.punishes}")
    check("دلیل همان «لینک در بیوگرافی» است",
          actions.warnings and "لینک در بیوگرافی" in actions.warnings[-1]["reason"])


def test_no_false_positive_on_file_names():
    print("\n### ۴) متن‌های شبیه لینک که لینک نیستند (گیرِ الکی)")
    negatives = [
        "اسکریپت نصب: install.sh",
        "فایل run.sh را اجرا کن",
        "مسیر: ./run.sh",
        "my.app را نصب کن",
        "نسخهٔ 1.0.0.0",
        "ورژن 2.0.0.0",
        "user.id من",
        "برنامه‌نویس پایتون و لینوکس",
        "متولد ۱۳۷۰.۰۵.۱۲",
        "قیمت: ۲۵.۰۰۰ تومان",
        "مدرک M.Sc. کامپیوتر",
        "علاقه‌مند به عکاسی . طبیعت",
        "ایمیل: ali@mail.com",
    ]
    for text in negatives:
        label = bio_link.find_link(text)
        check(f"لینک نیست: {text}", label is None, f"→ {label}")


def test_real_links_still_detected():
    print("\n### ۵) لینک‌های واقعی همچنان گرفته می‌شوند")
    positives = [
        "کانال من: t.me/ad_channel",
        "https://example.com",
        "آدرس سایت: example.ir",
        "github.com/x",
        "IP سرور 192.168.1.1",
        "www.example.com",
        "shop.ir",
        "linkedin.com/in/x",
    ]
    for text in positives:
        label = bio_link.find_link(text)
        check(f"لینک هست: {text}", label is not None, "→ None")


def main():
    test_removed_link_does_not_punish()
    test_api_error_keeps_old_state_but_never_punishes()
    test_real_link_still_punishes()
    test_no_false_positive_on_file_names()
    test_real_links_still_detected()
    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
