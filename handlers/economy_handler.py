"""💰 هندلر اقتصاد — تنها نقطهٔ اتصال اقتصاد به ربات.

سه بخش مستقل:
    «موجودی»  → منوی واحد شامل همهٔ قابلیت‌های اقتصاد
    «فروشگاه» → بخش جدا برای لیست و خرید
    «پروفایل» → ثبت اطلاعات، کارت پروفایل و خرید نشان/سطح/لقب

هیچ قابلیتی دستور جداگانه ندارد. این فایل هیچ بازی‌ای را import نمی‌کند
و هرگز مستقیماً به دیتابیس اقتصاد دست نمی‌زند؛ همه چیز از راه API.
"""
try:
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:
    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

import contextvars
import time

import economy
from economy import profiles
from economy.ui import balance_menu, profile_menu, shop_menu
from modules.user_display import format_user

CANCEL = "0"
_COMMAND_TRACE = contextvars.ContextVar("economy_command_trace", default=None)


def _entities(spans):
    built = []
    for kind, offset, length in spans:
        if kind == "bold":
            built.append(MessageEntityBold(offset=offset, length=length))
        elif kind == "blockquote":
            built.append(MessageEntityBlockquote(offset=offset,
                                                 length=length))
    return built


def _log(logger, message):
    if logger is not None:
        try:
            logger.log_info(message)
        except Exception:
            pass


def _log_error(logger, message):
    if logger is not None:
        try:
            logger.log_error(message)
        except Exception:
            pass


async def _send(event, payload, logger=None):
    """``payload`` یا رشته است یا ``(text, spans)``.

    اگر سرور entityها را نپذیرد (که روی برخی نسخه‌های Soroush Plus رخ
    می‌دهد)، همان متن بدون قالب‌بندی فرستاده می‌شود. قالب‌بندی یک تزئین
    است و نباید باعث شود کاربر «هیچ خروجی» ببیند.
    """
    trace = _COMMAND_TRACE.get()

    async def reply_timed(text, **kwargs):
        started = time.perf_counter()
        try:
            return await event.reply(text, **kwargs)
        finally:
            rpc_ms = (time.perf_counter() - started) * 1000
            if trace is not None:
                command, command_started = trace
                _log(logger,
                     "ECONOMY COMMAND RESPONSE "
                     f"command={command} rpc_ms={rpc_ms:.2f} "
                     f"command_to_reply_done_ms={(time.perf_counter() - command_started) * 1000:.2f}")

    if not isinstance(payload, tuple):
        await reply_timed(payload)
        return

    text, spans = payload
    try:
        await reply_timed(text, formatting_entities=_entities(spans))
    except Exception as error:
        _log_error(logger,
                   "ECONOMY SEND WITH ENTITIES FAILED -> retrying plain "
                   f"error={error!r}")
        await reply_timed(text)


# ---------------------------------------------------------------------------
# بخش موجودی
# ---------------------------------------------------------------------------
async def _handle_balance_step(bot, event, chat_id, user_id, sender, text,
                               logger):
    state = balance_menu.session(chat_id, user_id)
    if state is None:
        return False
    choice = balance_menu._english(text)

    # --- گام ۱: یوزرنیم مقصد ---
    # انتقال با ریپلای کاملاً حذف شده؛ فقط یوزرنیم پذیرفته می‌شود.
    if state["step"] == "transfer":
        if choice == CANCEL:
            balance_menu.close_session(chat_id, user_id)
            await _send(event, "لغو شد.", logger)
            return True

        target_id, username, error = balance_menu.resolve_target(
            chat_id, text, user_id)
        if error:
            await _send(event, error, logger)
            return True

        balance_menu.open_session(chat_id, user_id, step="transfer_amount",
                                  coin=state["coin"], target=target_id,
                                  username=username)
        await _send(event, balance_menu.transfer_amount_prompt(
            chat_id, state["coin"], user_id, username), logger)
        _log(logger, f"ECONOMY TRANSFER TARGET chat_id={chat_id} "
                     f"from={user_id} to={target_id} username={username!r}")
        return True

    # --- گام ۲: مقدار سکه ---
    if state["step"] == "transfer_amount":
        if choice == CANCEL:
            balance_menu.close_session(chat_id, user_id)
            await _send(event, "لغو شد.", logger)
            return True
        amount = balance_menu.parse_transfer_amount(text)
        if amount is None:
            await _send(event, "❌ مقدار باید یک عدد مثبت باشد. مثال:\n10",
                        logger)
            return True

        target_id = state["target"]
        coin_type = state["coin"]
        reference = (f"transfer:{chat_id}:{user_id}:{target_id}:"
                     f"{coin_type}:{amount}:{event.message.id}")
        ok, message = balance_menu.do_transfer(
            chat_id, user_id, target_id, coin_type, amount,
            reference=reference)
        if ok:
            economy.set_name(chat_id, target_id, f"@{state['username']}")
        balance_menu.close_session(chat_id, user_id)
        await _send(event, message, logger)
        _log(logger, f"ECONOMY TRANSFER chat_id={chat_id} from={user_id} "
                     f"to={target_id} coin={coin_type} amount={amount} "
                     f"ok={ok}")
        return True

    # --- منوی اصلی ---
    if choice == CANCEL:
        balance_menu.close_session(chat_id, user_id)
        await _send(event, "بسته شد.", logger)
        return True

    if choice == balance_menu.MENU_BRONZE_TO_SILVER:
        ok, message = balance_menu.do_convert_bronze(chat_id, user_id)
        await _send(event, message, logger)
        _log(logger, f"ECONOMY CONVERT bronze->silver user_id={user_id} "
                     f"ok={ok}")
        return True

    if choice == balance_menu.MENU_SILVER_TO_GOLD:
        ok, message = balance_menu.do_convert_silver(chat_id, user_id)
        await _send(event, message, logger)
        _log(logger, f"ECONOMY CONVERT silver->gold user_id={user_id} ok={ok}")
        return True

    coin_type = balance_menu.coin_for_option(choice)
    if coin_type is not None:
        balance_menu.open_session(chat_id, user_id, step="transfer",
                                  coin=coin_type)
        await _send(event, balance_menu.transfer_prompt(chat_id, coin_type, user_id),
                    logger)
        return True

    if choice == balance_menu.MENU_HISTORY:
        await _send(event, balance_menu.render_history(chat_id, user_id), logger)
        return True

    if choice == balance_menu.MENU_DAILY:
        ok, message = balance_menu.do_daily(chat_id, user_id)
        await _send(event, message, logger)
        _log(logger, f"ECONOMY DAILY user_id={user_id} granted={ok}")
        return True

    # هر متن دیگری: منو را نمی‌بندیم و پیام را هم مصرف نمی‌کنیم.
    return False


# ---------------------------------------------------------------------------
# بخش فروشگاه
# ---------------------------------------------------------------------------
async def _handle_shop_step(bot, event, chat_id, user_id, sender, text,
                            logger):
    state = shop_menu.session(chat_id, user_id)
    if state is None:
        return False
    step = state.get("step", shop_menu.STEP_MENU)
    numeric = balance_menu._english(text)

    # --- تایید نهایی خرید ------------------------------------------------
    if step == shop_menu.STEP_CONFIRM:
        pending = state.get("item_id")
        if shop_menu.is_decline(text):
            shop_menu.open_session(chat_id, user_id, step=shop_menu.STEP_BUY)
            await _send(event, "❌ خرید لغو شد. هیچ سکه‌ای کسر نشد.", logger)
            _log(logger, f"SHOP BUY DECLINED chat_id={chat_id} "
                         f"user_id={user_id} item={pending!r}")
            return True
        if not shop_menu.is_confirm(text):
            await _send(event, "برای تایید ✅ یا ۱ و برای لغو ❌ یا ۰ "
                               "بفرستید.", logger)
            return True
        reference = f"shop:{chat_id}:{user_id}:{pending}:{state.get('msg')}"
        ok, message = shop_menu.do_buy(chat_id, user_id, pending,
                                       reference=reference)
        shop_menu.open_session(chat_id, user_id, step=shop_menu.STEP_BUY)
        await _send(event, message, logger)
        _log(logger, f"SHOP BUY CONFIRMED chat_id={chat_id} "
                     f"user_id={user_id} item={pending!r} ok={ok}")
        return True

    # --- انتخاب آیتم -----------------------------------------------------
    if step == shop_menu.STEP_BUY:
        if numeric == CANCEL:
            shop_menu.close_session(chat_id, user_id)
            await _send(event, "بسته شد.", logger)
            return True
        item, message = shop_menu.select_item(chat_id, user_id, text)
        if item is None:
            await _send(event, message, logger)
            return True
        shop_menu.open_session(chat_id, user_id,
                               step=shop_menu.STEP_CONFIRM,
                               item_id=item["id"],
                               msg=getattr(event.message, "id", 0))
        await _send(event, message, logger)
        _log(logger, f"SHOP ITEM SELECTED chat_id={chat_id} "
                     f"user_id={user_id} item={item['id']!r}")
        return True

    # --- منوی اصلی -------------------------------------------------------
    if numeric == CANCEL:
        shop_menu.close_session(chat_id, user_id)
        await _send(event, "بسته شد.", logger)
        return True

    if numeric == shop_menu.MENU_BUY:
        shop_menu.open_session(chat_id, user_id, step=shop_menu.STEP_BUY)
        await _send(event, shop_menu.buy_prompt(chat_id, user_id), logger)
        return True

    return False


# ---------------------------------------------------------------------------
# بخش پروفایل
# ---------------------------------------------------------------------------
async def _handle_profile_step(bot, event, chat_id, user_id, sender, text,
                               logger):
    state = profile_menu.session(chat_id, user_id)
    if state is None:
        return False
    step = state["step"]
    numeric = profile_menu._english(text)
    draft = state.setdefault("draft", {})

    # --- ثبت اطلاعات اولیه (اسم ← شهر ← سن ← لقب) ----------------------
    if step == profile_menu.STEP_NAME:
        try:
            draft["name"] = profiles.validate_name(text)
        except profiles.ProfileError as error:
            await _send(event, f"❌ {error}", logger)
            return True
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_CITY)
        await _send(event, profile_menu.PROMPT_CITY, logger)
        return True

    if step == profile_menu.STEP_CITY:
        try:
            draft["city"] = profiles.validate_city(text)
        except profiles.ProfileError as error:
            await _send(event, f"❌ {error}", logger)
            return True
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_AGE)
        await _send(event, profile_menu.PROMPT_AGE, logger)
        return True

    if step == profile_menu.STEP_AGE:
        try:
            draft["age"] = profiles.validate_age(text)
        except profiles.ProfileError as error:
            await _send(event, f"❌ {error}", logger)
            return True
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_NICKNAME)
        await _send(event, profile_menu.PROMPT_NICKNAME, logger)
        return True

    if step == profile_menu.STEP_NICKNAME:
        try:
            draft["nickname"] = profiles.validate_nickname(text)
        except profiles.ProfileError as error:
            await _send(event, f"❌ {error}", logger)
            return True
        try:
            profiles.register(chat_id, user_id, **draft)
        except profiles.ProfileError as error:
            profile_menu.close_session(chat_id, user_id)
            await _send(event, f"❌ {error}", logger)
            return True
        _log(logger, f"PROFILE REGISTERED chat_id={chat_id} "
                     f"user_id={user_id}")
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_MENU)
        await _send(event, "✅ پروفایل شما ثبت شد.", logger)
        await _send(event,
                    profile_menu.render_menu(chat_id, user_id, sender), logger)
        return True

    # --- تایید نهایی خرید -----------------------------------------------
    if step == profile_menu.STEP_CONFIRM:
        pending = state.get("item_id")
        if profile_menu.is_decline(text):
            profile_menu.touch(chat_id, user_id, profile_menu.STEP_BUY)
            await _send(event, "❌ خرید لغو شد. هیچ سکه‌ای کسر نشد.", logger)
            _log(logger, f"PROFILE BUY DECLINED chat_id={chat_id} "
                         f"user_id={user_id} item={pending!r}")
            return True
        if not profile_menu.is_confirm(text):
            await _send(event, "برای تایید ✅ یا ۱ و برای لغو ❌ یا ۰ "
                               "بفرستید.", logger)
            return True
        reference = (f"profile:{chat_id}:{user_id}:{pending}:"
                     f"{state.get('msg')}")
        ok, message = profile_menu.do_buy(chat_id, user_id, pending,
                                          reference=reference)
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_BUY)
        await _send(event, message, logger)
        _log(logger, f"PROFILE BUY CONFIRMED chat_id={chat_id} "
                     f"user_id={user_id} item={pending!r} ok={ok}")
        return True

    # --- انتخاب آیتم ----------------------------------------------------
    if step == profile_menu.STEP_BUY:
        if numeric == CANCEL:
            profile_menu.touch(chat_id, user_id, profile_menu.STEP_MENU)
            await _send(event, "لغو شد.", logger)
            return True
        item, message = profile_menu.select_item(chat_id, user_id, text)
        if item is None:
            await _send(event, message, logger)
            return True
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_CONFIRM,
                           item_id=item["id"],
                           msg=getattr(event.message, "id", 0))
        await _send(event, message, logger)
        _log(logger, f"PROFILE ITEM SELECTED chat_id={chat_id} "
                     f"user_id={user_id} item={item['id']!r}")
        return True

    # --- ویرایش ---------------------------------------------------------
    if step == profile_menu.STEP_EDIT:
        if numeric == CANCEL:
            profile_menu.touch(chat_id, user_id, profile_menu.STEP_MENU)
            await _send(event,
                        profile_menu.render_menu(chat_id, user_id, sender),
                        logger)
            return True
        field = profile_menu._EDIT_FIELDS.get(numeric)
        if field is None:
            return False
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_EDIT_VALUE,
                           field=field[0])
        await _send(event, profile_menu.edit_prompt(field[1]), logger)
        return True

    if step == profile_menu.STEP_EDIT_VALUE:
        field = state.get("field")
        if not field:
            profile_menu.touch(chat_id, user_id, profile_menu.STEP_MENU)
            return False
        try:
            profiles.update(chat_id, user_id, **{field: text})
        except profiles.ProfileError as error:
            await _send(event, f"❌ {error}", logger)
            return True
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_MENU)
        await _send(event, "✅ ذخیره شد.", logger)
        await _send(event,
                    profile_menu.render_menu(chat_id, user_id, sender), logger)
        return True

    # --- منوی اصلی ------------------------------------------------------
    if numeric == CANCEL:
        profile_menu.close_session(chat_id, user_id)
        await _send(event, "بسته شد.", logger)
        return True

    if numeric == profile_menu.MENU_BUY:
        # فهرست و راهنمای انتخاب با هم؛ گزینهٔ جدا برای «لیست» وجود ندارد.
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_BUY)
        await _send(event, profile_menu.render_items(chat_id, user_id),
                    logger)
        await _send(event, profile_menu.buy_prompt(chat_id, user_id), logger)
        return True

    if numeric == profile_menu.MENU_EDIT:
        profile_menu.touch(chat_id, user_id, profile_menu.STEP_EDIT)
        await _send(event, profile_menu.PROMPT_EDIT, logger)
        return True

    # هر متن دیگری: منو باز می‌ماند و پیام مصرف نمی‌شود.
    return False


# ---------------------------------------------------------------------------
# ورودی اصلی
# ---------------------------------------------------------------------------
async def handle(bot, event, chat_id, user_id, sender, text, logger=None):
    """``True`` یعنی پیام مصرف شد و هندلر اصلی نباید ادامه دهد."""
    # --- ردیابی ورود ---------------------------------------------------
    # هر پیامی که «شبیه» دستور اقتصاد است اینجا لاگ می‌شود، حتی اگر تطبیق
    # نکند. با این لاگ می‌توان فهمید پیام اصلاً به هندلر رسیده یا نه، و
    # اگر رسیده چرا تطبیق نکرده است.
    normalized = balance_menu.normalize(text)
    if normalized == "موجودی":
        _COMMAND_TRACE.set(("موجودی", time.perf_counter()))
    elif normalized == "فروشگاه":
        _COMMAND_TRACE.set(("فروشگاه", time.perf_counter()))
    elif balance_menu.is_open(chat_id, user_id):
        _COMMAND_TRACE.set(("موجودی/session", time.perf_counter()))
    elif shop_menu.is_open(chat_id, user_id):
        _COMMAND_TRACE.set(("فروشگاه/session", time.perf_counter()))
    if normalized in {"موجودی", "فروشگاه"} \
            or profile_menu.is_command(text) or balance_menu.is_open(
            chat_id, user_id) or shop_menu.is_open(chat_id, user_id) \
            or profile_menu.is_open(chat_id, user_id):
        _log(logger,
             "ECONOMY HANDLER ENTER "
             f"chat_id={chat_id} user_id={user_id} "
             f"raw_text={text!r} normalized={normalized!r} "
             f"balance_open={balance_menu.is_open(chat_id, user_id)} "
             f"shop_open={shop_menu.is_open(chat_id, user_id)} "
             f"profile_open={profile_menu.is_open(chat_id, user_id)}")

    # ۱) باز کردن بخش‌ها
    if profile_menu.is_command(text):
        try:
            balance_menu.close_session(chat_id, user_id)
            shop_menu.close_session(chat_id, user_id)
            display = _display_name(sender)
            if display:
                economy.set_name(chat_id, user_id, display)
            registered = profiles.is_registered(chat_id, user_id)

            # --- حذف پرفایل ---
            if profile_menu.is_delete_command(text):
                profile_menu.close_session(chat_id, user_id)
                if not registered:
                    await _send(event, profile_menu.PROMPT_NOT_REGISTERED,
                                logger)
                else:
                    profiles.delete(chat_id, user_id)
                    await _send(event, profile_menu.PROMPT_DELETED, logger)
                    _log(logger, f"PROFILE DELETED chat_id={chat_id} "
                                 f"user_id={user_id}")
                return True

            # --- ثبت پرفایل ---
            if profile_menu.is_register_command(text):
                if registered:
                    profile_menu.open_session(chat_id, user_id,
                                              profile_menu.STEP_MENU)
                    await _send(event,
                                profile_menu.PROMPT_ALREADY_REGISTERED,
                                logger)
                    return True
                profile_menu.open_session(chat_id, user_id,
                                          profile_menu.STEP_NAME)
                _log(logger, "PROFILE REGISTRATION START "
                             f"chat_id={chat_id} user_id={user_id}")
                await _send(event, profile_menu.PROMPT_NAME, logger)
                return True

            # --- پرفایلم ---
            if not registered:
                profile_menu.close_session(chat_id, user_id)
                await _send(event, profile_menu.PROMPT_NOT_REGISTERED, logger)
                return True
            profile_menu.open_session(chat_id, user_id,
                                      profile_menu.STEP_MENU)
            payload = profile_menu.render_menu(chat_id, user_id, sender)
            _log(logger,
                 "PROFILE CARD RENDERED "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"text_len={len(payload[0])} entities={len(payload[1])}")
            await _send(event, payload, logger)
            _log(logger, f"PROFILE MENU SENT chat_id={chat_id} "
                         f"user_id={user_id}")
        except Exception as error:
            import traceback
            _log_error(logger,
                       "PROFILE MENU FAILED "
                       f"chat_id={chat_id} user_id={user_id} "
                       f"error={error!r}\n{traceback.format_exc()}")
            try:
                await event.reply(f"❌ خطا در نمایش پروفایل: {error}")
            except Exception as reply_error:
                _log_error(logger,
                           "PROFILE FALLBACK REPLY FAILED "
                           f"chat_id={chat_id} error={reply_error!r}")
        return True

    if balance_menu.is_command(text):
        try:
            shop_menu.close_session(chat_id, user_id)
            profile_menu.close_session(chat_id, user_id)
            balance_menu.open_session(chat_id, user_id)
            display = _display_name(sender)
            if display:
                economy.set_name(chat_id, user_id, display)

            # مقدار خوانده‌شده از دیتابیس پیش از ارسال لاگ می‌شود تا اگر
            # پیام نرسید، بدانیم مشکل از خواندن است یا از ارسال.
            balance = economy.get_balance(chat_id, user_id)
            _log(logger,
                 "ECONOMY BALANCE READ "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"bronze={balance[economy.BRONZE]} "
                 f"silver={balance[economy.SILVER]} "
                 f"gold={balance[economy.GOLD]} "
                 f"total_coin_value={balance['total_coin_value']} "
                 f"db_file={economy.storage.DATA_FILE}")

            rank = economy.get_rank(chat_id, user_id)
            payload = balance_menu.render_menu(
                chat_id, user_id, balance=balance, rank=rank
            )
            _log(logger,
                 "ECONOMY BALANCE RENDERED "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"text_len={len(payload[0])} entities={len(payload[1])}")

            await _send(event, payload, logger)
            _log(logger, f"ECONOMY BALANCE MENU SENT chat_id={chat_id} "
                         f"user_id={user_id}")
        except Exception as error:
            # هیچ خطایی بی‌صدا نمی‌ماند: بدون این، شکست ارسال یا رندر
            # باعث می‌شد کاربر «هیچ خروجی» ببیند و لاگی هم نباشد.
            import traceback
            _log_error(logger,
                       "ECONOMY BALANCE MENU FAILED "
                       f"chat_id={chat_id} user_id={user_id} "
                       f"error={error!r}\n{traceback.format_exc()}")
            try:
                await event.reply(f"❌ خطا در نمایش موجودی: {error}")
            except Exception as reply_error:
                _log_error(logger,
                           "ECONOMY BALANCE FALLBACK REPLY FAILED "
                           f"chat_id={chat_id} error={reply_error!r}")
        return True

    if shop_menu.is_command(text):
        try:
            balance_menu.close_session(chat_id, user_id)
            profile_menu.close_session(chat_id, user_id)
            shop_menu.open_session(chat_id, user_id)
            balance = economy.get_balance(chat_id, user_id)
            _log(logger,
                 "ECONOMY SHOP READ "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"bronze={balance[economy.BRONZE]} "
                 f"total_coin_value={balance['total_coin_value']} "
                 f"items={len(economy.shop.list_items())}")
            await _send(event, shop_menu.render_entry(chat_id, user_id),
                        logger)
            _log(logger, f"ECONOMY SHOP MENU SENT chat_id={chat_id} "
                         f"user_id={user_id}")
        except Exception as error:
            import traceback
            _log_error(logger,
                       "ECONOMY SHOP MENU FAILED "
                       f"chat_id={chat_id} user_id={user_id} "
                       f"error={error!r}\n{traceback.format_exc()}")
            try:
                await event.reply(f"❌ خطا در نمایش فروشگاه: {error}")
            except Exception as reply_error:
                _log_error(logger,
                           "ECONOMY SHOP FALLBACK REPLY FAILED "
                           f"chat_id={chat_id} error={reply_error!r}")
        return True

    # ۲) ادامهٔ گفتگوی باز
    try:
        if profile_menu.is_open(chat_id, user_id):
            if await _handle_profile_step(bot, event, chat_id, user_id,
                                          sender, text, logger):
                return True
        if balance_menu.is_open(chat_id, user_id):
            if await _handle_balance_step(bot, event, chat_id, user_id,
                                          sender, text, logger):
                return True
        if shop_menu.is_open(chat_id, user_id):
            if await _handle_shop_step(bot, event, chat_id, user_id,
                                       sender, text, logger):
                return True
    except Exception as error:
        import traceback
        _log_error(logger, f"ECONOMY HANDLER FAILED chat_id={chat_id} "
                           f"user_id={user_id} error={error!r}\n"
                           f"{traceback.format_exc()}")
        balance_menu.close_session(chat_id, user_id)
        shop_menu.close_session(chat_id, user_id)
        profile_menu.close_session(chat_id, user_id)
        await _send(event, "❌ خطایی رخ داد؛ دوباره تلاش کنید.", logger)
        return True

    # 🦊 بازی «مرکز فرماندهی روباه» — دستورهای درون‌بازی («حمله»، «دفاع»،
    # «تعمیر پایگاه» …) پیش از رسیدن به روتر بازی‌ها همین‌جا مصرف می‌شوند تا
    # با منوهای اقتصاد و بقیهٔ بازی‌ها تداخل نکنند. اگر مسابقه‌ای باز نباشد،
    # این قلاب هیچ کاری نمی‌کند و مسیر قبلی دست‌نخورده است.
    try:
        from handlers import fox_command_router
        if fox_command_router.claims(chat_id, text):
            if await fox_command_router.handle_message(
                bot, event, chat_id, user_id, sender, text, logger
            ):
                return True
    except Exception as error:
        _log_error(logger, f"FOX COMMAND HOOK FAILED chat_id={chat_id} "
                           f"user_id={user_id} error={error!r}")
    return False


def _display_name(user):
    return format_user(user)


def reset_all():
    balance_menu.reset_all()
    shop_menu.reset_all()
    profile_menu.reset_all()
