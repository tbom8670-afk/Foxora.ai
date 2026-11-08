# -*- coding: utf-8 -*-
"""🏛️ تستِ «عکسِ سالن دادگاه در شروعِ بازی».

چه چیزی اثبات می‌شود:

۱. فایلِ تصویر در مسیرِ ثابتِ پروژه وجود دارد و یک PNG/JPEGِ واقعی است
   (نه لینک، نه متن).
۲. با دستور «دادگاه»، تابعِ واقعیِ ``start_game`` تصویر را با
   ``client.send_file`` و همان متنِ شروع به‌عنوان ``caption`` می‌فرستد —
   یک درخواست، نه دو درخواستِ جدا و نه لینک.
۳. اگر ارسالِ عکس شکست بخورد، متنِ شروع باز هم می‌رود (بازی نمی‌خوابد).

اجرا: PYTHONPATH=. python3 tests/test_court_image_start.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SOROUSH_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="court-img-")

from handlers import court_game_router as router  # noqa: E402
from modules.court_game import game, storage, texts  # noqa: E402

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


class _Logger:
    def __init__(self):
        self.lines = []

    def log_info(self, m):
        self.lines.append(str(m))

    def log_error(self, m):
        self.lines.append("ERR " + str(m))

    def log_warning(self, m):
        self.lines.append(str(m))


class _Client:
    """کلاینتِ قلابی: هر مسیرِ ارسال را جدا ثبت می‌کند."""

    def __init__(self, fail_file=False):
        self.send_file_calls = []
        self.send_message_calls = []
        self.fail_file = fail_file

    async def send_file(self, entity, file, **kwargs):
        self.send_file_calls.append((entity, file, kwargs))
        if self.fail_file:
            raise RuntimeError("upload failed (simulated)")
        return {"id": 1001, "photo": True}

    async def send_message(self, entity, text="", **kwargs):
        self.send_message_calls.append((entity, text, kwargs))
        return {"id": 2001}

    async def get_me(self):
        return {"username": "fox_test"}


class _Event:
    is_private = False

    def __init__(self):
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return {"id": 3001}


class _Bot:
    def __init__(self, client):
        self.client = client
        self.logger = _Logger()

    async def award_coins(self, *a, **k):  # pragma: no cover - مسیرِ جایزه
        return 0


CHAT = -100778899001


def _reset():
    try:
        game.reset_all(CHAT) if hasattr(game, "reset_all") else None
    except Exception:
        pass
    try:
        storage.reset_chat(CHAT) if hasattr(storage, "reset_chat") else None
    except Exception:
        pass


def test_image_file_is_real():
    path = router.COURT_IMAGE_PATH
    check("مسیرِ تصویر داخلِ مخزن است", str(path).startswith(str(ROOT)),
          str(path))
    check("فایل موجود است", path.is_file())
    if not path.is_file():
        return
    size = path.stat().st_size
    check("حجمِ فایل معقول است (بیشتر از ۱۰ کیلوبایت)", size > 10_000, f"{size}")
    head = path.read_bytes()[:8]
    is_png = head == b"\x89PNG\r\n\x1a\n"
    is_jpeg = head[:3] == b"\xff\xd8\xff"
    check("فایل واقعاً تصویر است (PNG یا JPEG، نه متن/لینک)",
          is_png or is_jpeg, f"{head!r}")
    check("پسوند همان فرمتِ واقعی است (کتابخانه آن را عکس می‌شناسد)",
          (path.suffix.lower() == ".png" and is_png)
          or (path.suffix.lower() in (".jpg", ".jpeg") and is_jpeg),
          f"{path.suffix} / {head[:3]!r}")


def test_court_command_sends_photo_with_caption():
    _reset()
    client = _Client()
    bot = _Bot(client)
    event = _Event()

    async def scenario():
        return await router.start_game(bot, event, CHAT, 555, {"id": 555},
                                      logger=bot.logger)

    started = asyncio.run(scenario())
    check("start_game موفق برگشت", started is True, f"{started}")
    check("فقط یک درخواستِ ارسال انجام شد (send_file)",
          len(client.send_file_calls) == 1, f"{len(client.send_file_calls)}")
    check("هیچ پیامِ متنیِ جداگانه‌ای برای شروع نرفت",
          client.send_message_calls == [], f"{client.send_message_calls}")

    entity, file, kwargs = client.send_file_calls[0]
    check("مقصد همان گروه است", str(entity) == str(CHAT), str(entity))
    check("فایل = مسیرِ واقعیِ تصویر", str(file) == str(router.COURT_IMAGE_PATH),
          str(file))
    caption = kwargs.get("caption", "")
    check("متنِ شروع به‌عنوان caption همراهِ عکس رفت",
          texts.JOIN_WORD in caption and "دادگاه روباه" in caption, caption)
    check("caption با entityهای Bold ارسال شد",
          isinstance(kwargs.get("formatting_entities"), list)
          and len(kwargs["formatting_entities"]) >= 1,
          f"{kwargs.get('formatting_entities')}")
    check("هیچ URL‌ای به‌جای عکس فرستاده نشد",
          "http" not in str(file).lower(), str(file))
    check("لاگِ ارسالِ عکس ثبت شد",
          any("COURT IMAGE SENT" in line for line in bot.logger.lines),
          f"{bot.logger.lines[-3:]}")


def test_fallback_when_upload_fails():
    _reset()
    client = _Client(fail_file=True)
    bot = _Bot(client)
    event = _Event()

    async def scenario():
        return await router.start_game(bot, event, CHAT, 556, {"id": 556},
                                      logger=bot.logger)

    started = asyncio.run(scenario())
    check("بازی حتی وقتی آپلود شکست خورد شروع شد", started is True, f"{started}")
    check("تلاش برای عکس انجام شد", len(client.send_file_calls) == 1)
    check("متنِ شروع به‌صورت پیامِ معمولی رفت (fallback)",
          len(event.replies) == 1 and texts.JOIN_WORD in event.replies[0],
          f"{event.replies}")
    check("خطای ارسال لاگ شد",
          any("COURT IMAGE SEND FAILED" in line for line in bot.logger.lines))
    check("session بازی همچنان ساخته شد", game.is_active(CHAT) is True)


def test_missing_file_does_not_block_game():
    _reset()
    client = _Client()
    bot = _Bot(client)
    event = _Event()
    saved = router.COURT_IMAGE_PATH
    router.COURT_IMAGE_PATH = ROOT / "static" / "court" / "__does_not_exist__.png"
    try:
        async def scenario():
            return await router.start_game(bot, event, CHAT, 557, {"id": 557},
                                          logger=bot.logger)
        started = asyncio.run(scenario())
    finally:
        router.COURT_IMAGE_PATH = saved
    check("بدونِ فایل هم بازی شروع می‌شود", started is True)
    check("وقتی فایل نیست send_file صدا زده نمی‌شود",
          client.send_file_calls == [])
    check("متنِ شروع ارسال شد",
          len(event.replies) == 1 and texts.JOIN_WORD in event.replies[0])


def main():
    print("\n### ۱) فایلِ تصویر")
    test_image_file_is_real()
    print("\n### ۲) دستور «دادگاه» → عکس + caption")
    test_court_command_sends_photo_with_caption()
    print("\n### ۳) شکستِ آپلود → fallback متنی")
    test_fallback_when_upload_fails()
    print("\n### ۴) نبودِ فایل → بازی نباید بخوابد")
    test_missing_file_does_not_block_game()
    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
