# -*- coding: utf-8 -*-
"""⚖️ اتصال بازی «دادگاه روباه» به ربات.

این فایل تنها نقطهٔ تماس بازی با ربات است و عمداً نازک نگه داشته شده:
هیچ state اینجا زندگی نمی‌کند (همه در ``modules/court_game/game.py``)،
هیچ تصمیمی دربارهٔ اقتصاد یا مجازات گرفته نمی‌شود مگر با استفاده از
زیرساخت موجود ربات:

* **اقتصاد:** ``economy.award_game`` با ``reference`` یکتای هر پرونده
  (همان APIای که همهٔ بازی‌های Fox استفاده می‌کنند).
* **مجازات:** ``bot.message_delete_queue`` از مسیر
  ``modules/court_game/punishment.py``.
* **نمایش:** کل متن Bold + نقل‌قول شیشه‌ای سروش پلاس (``texts``).

مسیر صدا زدن: ``handlers/fox_games_router.handle`` → این ماژول.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from modules.court_game import ai_judge, game, punishment, storage, texts
from modules.fox_games.session_core import log, log_error, normalize_text

GAME_KEY = "court"
REWARD_CASE = {
    ai_judge.VERDICT_DEFENDANT: "court_acquittal",   # ۱۳ سکه برنز به متهم
    ai_judge.VERDICT_PLAINTIFF: "court_conviction",  # ۲۰ سکه برنز به شاکی
}

_RECOVERED = [False]
# سقفِ انتظارِ آپلود عکس (فقط همین مسیر؛ بقیهٔ دادگاه دست‌نخورده است).
COURT_IMAGE_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# ابزارهای ارسال
# ---------------------------------------------------------------------------
def ensure_ready(logger=None):
    """یک‌بار: ساخت پوشه‌ها، تنظیمات پیش‌فرض و بازیابی پرونده‌های نیمه‌کاره."""
    if _RECOVERED[0]:
        return
    _RECOVERED[0] = True
    try:
        storage.ensure_dirs()
        storage.write_default_settings()
        summary = storage.recover()
        if summary.get("sessions"):
            log(logger, "COURT RECOVERY sessions=%d cases=%d punishments=%d"
                % (summary["sessions"], summary["cases"],
                   summary["punishments"]))
    except Exception as error:  # pragma: no cover
        log_error(logger, f"COURT RECOVERY FAILED error={error!r}")


async def send_quoted(bot, event, chat_id, text):
    """ارسال پیام دادگاه: همهٔ متن **Bold** و بدون نقل‌قول.

    (قالب نقل‌قول حذف شد: پیام‌ها باید ردیفی و ساده دیده شوند.)
    """
    if not text:
        return None
    entities = texts.bold_entities(text)
    if event is not None:
        try:
            return await event.reply(text, formatting_entities=entities)
        except Exception as error:
            log_error(getattr(bot, "logger", None),
                      f"COURT REPLY FAILED chat_id={chat_id} error={error!r}")
            try:
                return await event.reply(text)
            except Exception:
                pass
    client = getattr(bot, "client", None)
    if client is not None and getattr(bot, "logger", None) is not None:
        try:
            return await client.send_message(
                chat_id, text, formatting_entities=entities)
        except Exception:
            try:
                return await client.send_message(chat_id, text)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# 🏛️ تصویر سالن دادگاه — یک بار در شروع بازی ارسال می‌شود
# ---------------------------------------------------------------------------
# مسیرِ ثابت داخل مخزن (نه لینک، نه دانلود): فایل کنارِ کد می‌ماند و
# SPlusthon 1.1.4 آن را با ``send_file`` به‌عنوان «عکسِ واقعی» آپلود می‌کند
# (پسوند png/jpg ⇒ ``utils.is_image`` ⇒ InputMediaUploadedPhoto).
# JPGِ ۱۲۸۰ پیکسلی (۱۸۰KB) برای آپلودِ سریع روی موبایل؛ نسخهٔ PNG در همان
# پوشه به‌عنوان فایلِ اصلی نگه داشته می‌شود.
COURT_IMAGE_PATH = (Path(__file__).resolve().parent.parent
                    / "static" / "court" / "court_hall.jpg")
_COURT_IMAGE_MISSING_LOGGED = [False]


def _court_image_file():
    """فایلِ تصویر اگر موجود بود؛ وگرنه None (بازی هرگز به‌خاطر عکس نمی‌خوابد)."""
    path = COURT_IMAGE_PATH
    try:
        if path.is_file() and path.stat().st_size > 1024:
            return path
    except OSError:
        pass
    if not _COURT_IMAGE_MISSING_LOGGED[0]:
        _COURT_IMAGE_MISSING_LOGGED[0] = True
        log_error(None, f"COURT IMAGE MISSING path={path}")
    return None


async def send_court_image(bot, event, chat_id, caption, logger=None):
    """ارسالِ عکسِ سالن دادگاه همراه با متنِ شروعِ بازی (یک پیام).

    * ابتدا ``client.send_file`` با ``caption`` و entityهای Bold — همان روشی
      که ``modules/photo_download.py`` در همین پروژه استفاده می‌کند.
    * اگر کتابخانه/شبکه اجازه نداد، همان متن به‌صورت پیام معمولی می‌رود تا
      شروعِ بازی هرگز از دست نرود.
    """
    path = _court_image_file()
    client = getattr(bot, "client", None)
    log(logger, f"COURT IMAGE STEP start chat_id={chat_id} path={path}")
    sender = getattr(client, "send_file", None)
    if path is not None and not callable(sender):
        # کلاینتی که send_file ندارد (مثلاً استابِ تست یا نسخهٔ قدیمی) —
        # این خطا نیست، فقط قابلیتِ عکس در دسترس نیست.
        log(logger, f"COURT IMAGE SKIPPED reason=no_send_file chat_id={chat_id}")
        sender = None
    if path is not None and sender is not None:
        entities = texts.bold_entities(caption)
        # آپلود باید از sender رسانه‌ای (media DC) برود؛ بدونِ آن SPlusthon
        # SaveFilePart را روی connection server می‌فرستد و سرور رد می‌کند
        # (FILE_REQUEST_RECEIVED_ON_CONNECTION_SERVER). دقیقاً همان کاری که
        # modules/photo_download.py قبل از آپلود می‌کند؛ اگر نشد، آپلودِ
        # عادی تلاش می‌شود و در صورتِ شکست مسیرِ متنی ادامه می‌یابد.
        try:
            if hasattr(client, "_call"):
                from modules.splusthon_upload_fix import _get_media_sender
                media_sender = await _get_media_sender(client)
                log(logger, f"COURT IMAGE MEDIA SENDER ready={media_sender is not None} "
                            f"chat_id={chat_id}")
            else:
                log(logger, f"COURT IMAGE MEDIA SENDER skipped reason=no_call "
                            f"chat_id={chat_id}")
        except asyncio.CancelledError:
            raise
        except Exception as media_error:
            log_error(logger, f"COURT IMAGE MEDIA SENDER FAILED "
                              f"chat_id={chat_id} error={media_error!r}")
        try:
            message = await asyncio.wait_for(
                sender(
                    chat_id, str(path),
                    caption=caption,
                    formatting_entities=entities,
                ),
                timeout=COURT_IMAGE_TIMEOUT,
            )
            log(logger, f"COURT IMAGE SENT chat_id={chat_id} bytes={path.stat().st_size}")
            return message
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log_error(logger, f"COURT IMAGE SEND TIMEOUT chat_id={chat_id} "
                              f"timeout_s={COURT_IMAGE_TIMEOUT}")
        except Exception as error:
            log_error(logger, f"COURT IMAGE SEND FAILED chat_id={chat_id} "
                              f"error={error!r}")
    elif path is None and event is not None:
        pass  # لاگ در خودِ _court_image_file
    return await send_quoted(bot, event, chat_id, caption)


async def _bot_username(bot):
    cached = getattr(bot, "_court_bot_username", None)
    if cached:
        return cached
    me = getattr(bot, "me", None)
    if me is None:
        try:
            me = await bot.client.get_me()
        except Exception:
            me = None
    username = str(getattr(me, "username", "") or "").strip().lstrip("@")
    value = f"@{username}" if username else (
        getattr(me, "first_name", None) or "ربات")
    try:
        bot._court_bot_username = value
    except Exception:
        pass
    return value


# ---------------------------------------------------------------------------
# اقتصاد و مجازات (روی زیرساخت موجود)
# ---------------------------------------------------------------------------
def _pay(bot, chat_id, user_id, amount, game_key, reference, name=None,
         logger=None):
    """پرداخت جایزه از سیستم بانک فعلی؛ ``reference`` از پرداخت دوباره جلوگیری می‌کند."""
    override = getattr(bot, "award_coins", None)
    try:
        if override is not None:
            balance = override(chat_id, user_id, name, amount)
        else:
            from economy import award_game as economy_award_game
            balance = economy_award_game(
                chat_id, user_id, game_key,
                reference=reference, name=name, amount=amount,
            )
        log(logger, f"COURT REWARD PAID chat_id={chat_id} user_id={user_id} "
                    f"game={game_key} amount={amount} balance={balance} "
                    f"reference={reference}")
        return True
    except Exception as error:
        log_error(logger, f"COURT REWARD FAILED chat_id={chat_id} "
                          f"user_id={user_id} amount={amount} "
                          f"reference={reference} error={error!r}")
        return False


def reward_amount(verdict_key) -> int:
    """مقدار جایزه از جدول واحد ``economy.rewards`` (نه عدد جادویی)."""
    from economy import rewards
    return rewards.amount_for(REWARD_CASE[verdict_key])


async def _start_punishment(bot, event, chat_id, case, verdict, logger=None):
    """اعلام حکم و اجرای مجازات ۶۰ ثانیه‌ای متهم."""
    defendant_id = verdict["defendant_user_id"]
    defendant_display = verdict.get("defendant_display") or "متهم"
    seconds = storage.settings()["punishment_seconds"]
    await send_quoted(bot, event, chat_id,
                      texts.punishment_notice(defendant_display))
    stats = punishment.start(chat_id, defendant_id, verdict["case_id"],
                             seconds=seconds)
    log(logger, f"COURT PUNISH START chat_id={chat_id} "
                f"user_id={defendant_id} seconds={seconds} "
                f"case_id={verdict['case_id']}")
    case_id = verdict["case_id"]

    async def _finish_later():
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        final = punishment.finish(chat_id, defendant_id) or stats
        summary = punishment.summary(final)
        log(logger, f"COURT PUNISH END chat_id={chat_id} "
                    f"deleted={summary['deleted']} failed={summary['failed']} "
                    f"observed={summary['observed']} status={summary['status']}")
        await send_quoted(bot, event, chat_id,
                          punishment.report_text(final))
        try:
            current = storage.read_case(case_id) or {}
            current["punishment"] = summary
            storage.write_case(current)
            result = storage.read_result(case_id) or {}
            result["punishment"] = summary
            storage.write_result(case_id, result)
        except Exception as error:
            log_error(logger, f"COURT PUNISH PERSIST FAILED {error!r}")

    return asyncio.create_task(_finish_later(), name=f"court-punish:{case_id}")


# ---------------------------------------------------------------------------
# ثبت تحلیل داخلی هر پیام در بایگانی پرونده (بدون پاسخ در گروه)
# ---------------------------------------------------------------------------
def _schedule_analysis(bot, case_snapshot, entry, logger=None):
    async def _run():
        if not ai_judge.is_configured():
            return
        try:
            record = await ai_judge.analyze_message(case_snapshot, entry, logger)
        except Exception as error:
            log_error(logger, f"COURT JUDGE ANALYSIS FAILED "
                              f"case_id={case_snapshot.get('case_id')} "
                              f"order={entry.get('order')} error={error!r}")
            return
        if record:
            storage.append_ai_log(case_snapshot.get("case_id"), record)

    try:
        return asyncio.create_task(
            _run(), name=f"court-judge:{entry.get('order')}")
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# رأی نهایی
# ---------------------------------------------------------------------------
async def _run_judging(bot, event, chat_id, case, logger=None):
    """داوری با قاضی داخلی ربات (بدون شبکه)، پرداخت جایزه و اعلام رأی."""
    await send_quoted(bot, event, chat_id, texts.judging())
    try:
        verdict, raw = await ai_judge.judge_case(case, logger)
    except ai_judge.JudgeError as error:
        # رکورد پرونده ناقص است؛ یک بار دیگر با تحلیل داخلی تلاش می‌شود
        # تا پرونده بی‌رأی نماند.
        log_error(logger, f"COURT JUDGE INVALID case_id={case.get('case_id')} "
                          f"error={error!r}")
        try:
            verdict = ai_judge.local_verdict(case, str(error))
            verdict["model"] = "local-evidence-judge"
            raw = {"fallback": True, "error": str(error)}
            log(logger, f"COURT LOCAL VERDICT case_id={verdict['case_id']} "
                        f"verdict={verdict['verdict']}")
        except Exception as fallback_error:
            log_error(logger, "COURT LOCAL VERDICT FAILED "
                              f"case_id={case.get('case_id')} "
                              f"error={fallback_error!r}")
            verdict = None
    if verdict is None:
        storage.append_ai_log(case.get("case_id"), {
            "stage": "verdict",
            "error": "no_verdict",
            "at": storage.now_iso(),
        })
        payment = {"status": "skipped", "reason": "invalid_case_record"}
        try:
            current = storage.read_case(case.get("case_id")) or {}
            current["verdict"] = None
            current["judge_error"] = "no_verdict"
            current["payment"] = payment
            storage.write_case(current)
            game.set_payment(chat_id, payment, logger)
            game.mark_judge_error(chat_id, "no_verdict", logger)
        except Exception as persist_error:
            log_error(logger, f"COURT ERROR PERSIST FAILED {persist_error!r}")
        await send_quoted(bot, event, chat_id, texts.ai_error())
        return None

    storage.append_ai_log(case.get("case_id"), {
        "stage": "verdict",
        "at": storage.now_iso(),
        "verdict": verdict,
        "raw": raw,
    })
    log(logger, f"COURT VERDICT chat_id={chat_id} case_id={verdict['case_id']} "
                f"verdict={verdict['verdict']} winner={verdict['winner_user_id']} "
                f"confidence={verdict['confidence']}")

    reasons = [verdict["reason_1"], verdict["reason_2"], verdict["reason_3"]]
    amount = reward_amount(verdict["verdict"])
    reference = f"court:{verdict['case_id']}:{verdict['winner_user_id']}"

    already_paid = False
    try:
        stored = storage.read_case(verdict["case_id"]) or {}
        already_paid = (stored.get("payment") or {}).get("status") == "paid"
    except Exception:
        already_paid = False

    paid = False
    if not already_paid:
        paid = _pay(bot, chat_id, verdict["winner_user_id"], amount,
                    REWARD_CASE[verdict["verdict"]], reference,
                    name=verdict.get("winner_display"), logger=logger)
        payment = {
            "status": "paid" if paid else "failed",
            "amount": amount,
            "coin": "bronze",
            "user_id": verdict["winner_user_id"],
            "reference": reference,
            "at": storage.now_iso(),
        }
    else:
        payment = {
            "status": "paid",
            "amount": amount,
            "coin": "bronze",
            "user_id": verdict["winner_user_id"],
            "reference": reference,
            "note": "already_paid",
        }
        paid = True

    try:
        current = storage.read_case(verdict["case_id"]) or {}
        current["verdict"] = verdict
        current["payment"] = payment
        storage.write_case(current)
        game.set_payment(chat_id, payment, logger)
    except Exception as error:
        log_error(logger, f"COURT VERDICT PERSIST FAILED {error!r}")

    if verdict["verdict"] == ai_judge.VERDICT_DEFENDANT:
        body = texts.verdict_acquittal(
            verdict.get("defendant_display") or "متهم", reasons, amount)
    else:
        body = texts.verdict_conviction(
            verdict.get("plaintiff_display") or "شاکی", reasons, amount)

    if not paid:
        body = body + "\n" + texts.reward_failed()
        log_error(logger, f"COURT REWARD NOT PAID case_id="
                          f"{verdict['case_id']} reference={reference}")

    await send_quoted(bot, event, chat_id, body)

    if verdict["verdict"] == ai_judge.VERDICT_PLAINTIFF and paid:
        try:
            await _start_punishment(bot, event, chat_id, case, verdict, logger)
        except Exception as error:
            log_error(logger, f"COURT PUNISH START FAILED {error!r}")
    return verdict


# ---------------------------------------------------------------------------
# چرخهٔ بازی
# ---------------------------------------------------------------------------
def _callbacks(bot, event, chat_id, logger):
    async def on_cancel(reason, count=0, role_label=None):
        if reason == "not_enough_players":
            await send_quoted(bot, event, chat_id,
                              texts.not_enough_players(count,
                                                       game.REQUIRED_PLAYERS))
        elif reason == "step_timeout":
            await send_quoted(bot, event, chat_id,
                              texts.step_timeout(role_label or "بازیکن"))
        else:
            log_error(logger, f"COURT CANCEL chat_id={chat_id} reason={reason}")

    async def on_start(case, roster):
        await send_quoted(bot, event, chat_id,
                          texts.roster_message(case.get("court"), roster))
        log(logger, f"COURT TRIAL STARTED chat_id={chat_id} "
                    f"case_id={case.get('case_id')}")

    async def on_prompt(phase, text):
        await send_quoted(bot, event, chat_id, text)

    async def on_judging(case):
        session = game.session_of(chat_id)
        session_id = (session or {}).get("session_id")
        verdict = None
        try:
            verdict = await _run_judging(bot, event, chat_id, case, logger)
        finally:
            if session_id is not None:
                game.finish(chat_id, session_id, verdict, logger,
                            status=(None if verdict else "judge_error"))

    async def bot_username():
        return await _bot_username(bot)

    return {
        "on_cancel": on_cancel,
        "on_start": on_start,
        "on_prompt": on_prompt,
        "on_judging": on_judging,
        "bot_username": bot_username,
    }


# ---------------------------------------------------------------------------
# نقطهٔ ورود از روتر بازی‌ها
# ---------------------------------------------------------------------------
def is_active(chat_id) -> bool:
    return game.is_active(chat_id)


def active_count(chat_id) -> int:
    return 1 if game.is_active(chat_id) else 0


def phase(chat_id):
    return game.phase(chat_id)


async def start_game(bot, event, chat_id, user_id, sender, logger=None,
                     other_game_active=None):
    """دستور «دادگاه»: ساخت پروندهٔ جدید و باز کردن ثبت‌نام."""
    ensure_ready(logger)
    if getattr(event, "is_private", False):
        await send_quoted(bot, event, chat_id, texts.private_only())
        return True
    if game.is_active(chat_id):
        await send_quoted(bot, event, chat_id, texts.already_running())
        return True
    if other_game_active is not None and other_game_active():
        await send_quoted(bot, event, chat_id, texts.other_game_running())
        return True

    settings = storage.settings()

    # اگر پروندهٔ این گروه از قبل باز است (مثلاً ربات ری‌استارت شده یا
    # کسی دوباره «دادگاه» زده)، فهرست ثبت‌نام‌شده‌ها بازیابی می‌شود؛
    # هرگز پروندهٔ نیمه‌کاره با یک پروندهٔ خالی جایگزین نمی‌شود.
    restored = game.restore(chat_id, logger)
    if restored is not None:
        log(logger, f"COURT START RESUMED chat_id={chat_id} "
                    f"players={len(restored.get('players', []))}")
        await send_quoted(bot, event, chat_id,
                          texts.join_resumed(len(restored.get("players", [])),
                                             game.REQUIRED_PLAYERS))
        game.schedule(chat_id, restored["session_id"],
                      _callbacks(bot, event, chat_id, logger),
                      logger=logger)
        return True

    session = game.start(chat_id, logger=logger, group_event=event)
    if session is None:
        await send_quoted(bot, event, chat_id, texts.already_running())
        return True

    # 🏛️ عکسِ سالن دادگاه + همان پیامِ شروع، در یک پیام (caption).
    await send_court_image(
        bot, event, chat_id,
        texts.join_prompt(game.REQUIRED_PLAYERS, settings["join_seconds"]),
        logger=logger,
    )
    game.schedule(chat_id, session["session_id"],
                  _callbacks(bot, event, chat_id, logger),
                  logger=logger,
                  join_seconds=settings["join_seconds"],
                  step_seconds=settings["step_seconds"])
    log(logger, f"COURT GAME START chat_id={chat_id} user_id={user_id}")
    return True


def has_pending_join(chat_id):
    """آیا پروندهٔ در حال ثبت‌نام این گروه روی دیسک هست؟

    برای این است که اگر ربات ری‌استارت شود، پیام «شرکت» کاربران هنوز به
    دادگاه برسد (در حافظه بازی فعال نیست، اما پرونده باز است).
    """
    try:
        data = storage.read_session(storage.chat_key_for(chat_id))
    except Exception:
        return False
    if not data or data.get("kind") != "case":
        return False
    return (data.get("phase") == game.PHASE_JOINING
            and bool(data.get("players")))


async def join_after_restart(bot, event, chat_id, user_id, sender, text,
                             logger=None, other_game_joining=None):
    """«شرکت» وقتی بازی در حافظه نیست: پروندهٔ باز را از دیسک بازیابی کن.

    این مسیر تضمین می‌کند اگر ربات وسط ۶۰ ثانیهٔ ثبت‌نام خاموش/روشن شود،
    «شرکت» بعدی همان پرونده را ادامه دهد و ثبت‌نام از صفر شروع نشود.
    """
    if normalize_text(text) != normalize_text(game.JOIN_WORD):
        return False
    ensure_ready(logger)
    restored = game.restore(chat_id, logger)
    if restored is None:
        return False
    log(logger, f"COURT JOIN AFTER RESTORE chat_id={chat_id} user_id={user_id} "
                f"players={len(restored.get('players', []))}")
    await send_quoted(bot, event, chat_id,
                      texts.join_resumed(len(restored.get("players", [])),
                                         game.REQUIRED_PLAYERS))
    game.schedule(chat_id, restored["session_id"],
                  _callbacks(bot, event, chat_id, logger),
                  logger=logger)
    return await handle_message(bot, event, chat_id, user_id, sender, text,
                                logger, other_game_joining=other_game_joining)


async def handle_message(bot, event, chat_id, user_id, sender, text,
                         logger=None, other_game_joining=None):
    """پیام‌های درون‌بازی: «شرکت» و متن مراحل پرونده."""
    if not game.is_active(chat_id):
        if normalize_text(text) == normalize_text(game.JOIN_WORD):
            log(logger, f"COURT JOIN IGNORED chat_id={chat_id} "
                        f"user_id={user_id} reason=no_open_case")
        return False
    ensure_ready(logger)
    phase = game.phase(chat_id)
    normalized = normalize_text(text)

    if phase == game.PHASE_JOINING:
        if normalized != normalize_text(game.JOIN_WORD):
            return False
        # اگر بازی دیگری هم در همین گروه در حال ثبت‌نام است، «شرکت» را
        # نمی‌بلعیم تا هر بازی ثبت‌نام خودش را داشته باشد.
        if other_game_joining is not None and other_game_joining():
            return False
        result, players = game.join(chat_id, user_id, sender, logger)
        if result == "joined":
            await send_quoted(bot, event, chat_id,
                              texts.join_confirmed(len(players),
                                                   game.REQUIRED_PLAYERS))
        elif result == "duplicate":
            await send_quoted(bot, event, chat_id, texts.join_duplicate())
        elif result == "full":
            await send_quoted(bot, event, chat_id, texts.join_full())
        return True

    # دستورهای خود ربات هرگز به‌عنوان «متن پرونده» بلعیده نمی‌شوند؛
    # بازی‌ها و فرمان‌های موجود ربات باید در طول دادگاه هم کار کنند.
    if _is_bot_command(text):
        return False

    # مراحل متنی: فقط بازیکنِ نوبت‌دار و فقط یک بار.
    message_id = getattr(getattr(event, "message", None), "id", None)
    status, info = game.submit(chat_id, user_id, text, message_id, logger)

    if status == "not_active":
        return False
    if status == "wrong_user":
        # بازیکنی که نوبتش نیست آزاد است هر چیزی بنویسد؛ ربات هیچ پیامی
        # نمی‌فرستد و هیچ یادآوری‌ای («الان نوبت فلانی است») تکرار نمی‌شود.
        # فقط منتظر پیام بازیکنِ نوبت‌دار می‌مانیم و پیام بقیه مسیر عادی
        # خودش را می‌رود.
        return False
    if status == "empty":
        await send_quoted(bot, event, chat_id, texts.too_short())
        return True
    if status == "invalid":
        await send_quoted(bot, event, chat_id, texts.crime_invalid())
        return True
    if status == "limit":
        session = game.session_of(chat_id)
        game.cancel(chat_id, (session or {}).get("session_id"),
                    "invalid_choices", logger)
        await send_quoted(bot, event, chat_id,
                          texts.too_many_invalid(game.MAX_INVALID_CHOICES))
        return True
    if status == "recorded":
        entry = (info["case"]["entries"] or [None])[-1]
        # انتخاب جرم یک «گزینش» است نه متن پرونده؛ برای آن تأیید ثبت
        # فرستاده نمی‌شود و بلافاصله درخواست شرح شکایت می‌آید.
        if entry and entry.get("kind") != "crime_choice":
            await send_quoted(bot, event, chat_id, texts.recorded())
        if entry:
            _schedule_analysis(bot, info["case"], entry, logger)
        return True
    return False


_COMMAND_LIKE = frozenset({
    "لیست بازی", "لیست بازی ها", "راهنما", "موجودی", "فروشگاه", "رتبه ها",
    "امتیاز من", "آمارم", "اصلم", "قوانین", "جک", "دانستنی",
})


def _is_bot_command(text) -> bool:
    """آیا متن، دستور خود ربات است (نه متن پرونده)؟"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.startswith(("!", "/", ".")):
        return True
    normalized = normalize_text(raw)
    if normalized in _COMMAND_LIKE:
        return True
    try:
        from modules import entertainment_control
        if entertainment_control.is_game_command(raw):
            return True
    except Exception:
        pass
    return False


def _player_ids(chat_id):
    session = game.session_of(chat_id) or {}
    return {int(p.get("user_id") or 0) for p in session.get("players", [])}


def reset_all(chat_id=None):
    game.reset_all(chat_id)
    punishment.reset_all()
    if chat_id is None:
        _RECOVERED[0] = False
