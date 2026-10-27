"""
اقدامات مدیریتی - حذف، سایلنت، بن
"""
import asyncio
import re
from modules.group_stats import add_deleted
from modules.group_id import normalize_group_id
from modules.cache_manager import PermissionCircuitBreaker, TtlCache
from datetime import timedelta

try:
    from splusthon.errors import ChatAdminRequiredError, UserAdminInvalidError
    from splusthon import types
    from splusthon.tl import functions
except ImportError:
    # برای زمانی که کتابخانه نصب نیست، کلاس‌های ساختگی
    class ChatAdminRequiredError(Exception): pass
    class UserAdminInvalidError(Exception): pass
    class _ChatBannedRights:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    class _EditBannedRequest:
        def __init__(self, channel=None, participant=None, banned_rights=None):
            self.channel = channel
            self.participant = participant
            self.banned_rights = banned_rights
    class _ChannelsModule:
        EditBannedRequest = _EditBannedRequest
    class _Functions:
        channels = _ChannelsModule()
    class _Types:
        ChatBannedRights = _ChatBannedRights
    types = _Types()
    functions = _Functions()


def _is_flood_wait(error):
    name = error.__class__.__name__.lower()
    return "flood" in name or ("wait" in name and "second" in str(error).lower())


_INTERNAL_REASON_TAGS = re.compile(
    r"\b(?:spam_banned_word|banned_word|bannde_word)\b\s*",
    re.IGNORECASE,
)


def public_warning_reason_line(reason):
    """User-facing warning reason. Internal detector tags never appear."""
    raw = str(reason or "نامشخص").strip()
    raw = _INTERNAL_REASON_TAGS.sub("", raw).strip()
    banned_match = re.search(r"کلمه ممنوعه\s*\((.*?)\)", raw)
    group_filter_match = re.search(r"فیلتر گروه\s*\((.*?)\)", raw)
    if banned_match:
        return f"دلیل کلمه ممنوعه : ({banned_match.group(1)})"
    if group_filter_match:
        return f"دلیل فیلتر گروه : ({group_filter_match.group(1)})"
    # 🖇 لینک در بیوگرافی: قالب کوتاه و ثابت «دلیل: لینک در بیوگرافی».
    # نوع لینک فقط در لاگ داخلی می‌ماند، نه در پیام عمومی.
    if "لینک در بیوگرافی" in raw:
        return "دلیل: لینک در بیوگرافی"
    return f"دلیل فیلتر گروه : {raw}"


class AdminActions:
    def __init__(self, client, logger, config_manager, *, peer_cache=None,
                 bot_account_id=None, circuit_breaker=None):
        self.client = client
        self.logger = logger
        self.config = config_manager
        self.peer_cache = peer_cache
        self.bot_account_id = bot_account_id
        self.circuit_breaker = circuit_breaker or PermissionCircuitBreaker.get_default(self.logger)
        self.entity_cache = TtlCache(default_ttl=300.0, max_size=3000)

    def bind_runtime_context(self, *, peer_cache=None, bot_account_id=None):
        if peer_cache is not None:
            self.peer_cache = peer_cache
        if bot_account_id is not None:
            self.bot_account_id = bot_account_id

    def _cached_chat_peer(self, chat_id):
        cache = self.peer_cache
        if not cache:
            return None
        direct = cache.get(chat_id)
        if direct is not None:
            return direct
        wanted = normalize_group_id(chat_id)
        for cached_id, peer in list(cache.items()):
            if peer is not None and normalize_group_id(cached_id) == wanted:
                return peer
        return None

    async def _input_entity(self, value):
        if value is None:
            return None
        name = type(value).__name__
        if (
            name.startswith("InputPeer")
            or name.startswith("InputUser")
            or name.startswith("InputChannel")
            or name.startswith("InputChat")
            or "InputPeer" in name
            or "InputUser" in name
            or "InputChannel" in name
            or "InputChat" in name
        ):
            return value
        # Check cached chat peer from peer_cache if value is chat_id
        if isinstance(value, (int, str)):
            cached_peer = self._cached_chat_peer(value)
            if cached_peer is not None:
                return cached_peer

        cache_key = str(value)
        cached = self.entity_cache.get(cache_key)
        if cached is not None:
            return cached

        resolver = getattr(self.client, "get_input_entity", None)
        resolved = None
        if callable(resolver):
            try:
                resolved = await resolver(value)
            except Exception:
                # If a positive integer group ID failed with GetUsers / ValueError / KeyError,
                # try the channel format (-1000000000000 - int(value))
                if isinstance(value, int) and value > 0:
                    try:
                        resolved = await resolver(-1000000000000 - value)
                    except Exception:
                        pass
                if resolved is None:
                    get_ent = getattr(self.client, "get_entity", None)
                    if callable(get_ent):
                        try:
                            resolved = await get_ent(value)
                        except Exception:
                            pass
        else:
            get_ent = getattr(self.client, "get_entity", None)
            if callable(get_ent):
                try:
                    resolved = await get_ent(value)
                except Exception:
                    pass

        if resolved is not None:
            self.entity_cache.set(cache_key, resolved)
        return resolved or value

    async def _run_moderation_with_timeout(self, action, user_id, timeout_seconds, operation):
        """حد بالای عملیات؛ FloodWait را برای worker نگه می‌دارد."""
        try:
            return await asyncio.wait_for(operation, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.logger.log_error(
                f"MODERATION ACTION TIMEOUT action={action} user_id={user_id} "
                f"timeout_seconds={timeout_seconds}"
            )
            return False
        except Exception as error:
            if _is_flood_wait(error):
                raise
            raise

    async def delete_message(self, chat_id, message_id=None, event=None) -> bool:
        """حذف پیام"""
        if not self.circuit_breaker.can_execute(chat_id, "delete"):
            return False
        try:
            if event and hasattr(event, 'delete'):
                await event.delete()
                self.circuit_breaker.record_success(chat_id)
                return True
            elif message_id:
                await self.client.delete_messages(chat_id, message_id)

                try:
                    add_deleted(chat_id, 0, "system")
                except Exception:
                    pass

                self.circuit_breaker.record_success(chat_id)
                return True
        except (ChatAdminRequiredError, UserAdminInvalidError) as e:
            self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"❌ دسترسی ادمین برای حذف پیام در {chat_id} ندارید: {e}")
            return False
        except Exception as e:
            from modules.cache_manager import is_permission_error
            if is_permission_error(e):
                self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در حذف پیام {message_id} در {chat_id}: {e}")
            return False
        return False

    async def mute_user(self, chat_id, user_id, duration_seconds=None, *,
                        user=None, chat=None):
        return await self._run_moderation_with_timeout(
            "mute", user_id, 45,
            self._mute_user_rpc(
                chat_id, user_id, duration_seconds, user=user, chat=chat,
            )
        )

    async def _mute_user_rpc(self, chat_id, user_id, duration_seconds=None, *,
                             user=None, chat=None):
        if not self.circuit_breaker.can_execute(chat_id, "mute"):
            return False
        try:
            from datetime import datetime, timedelta, timezone

            user_peer = await self._input_entity(
                user if user is not None else user_id
            )
            cached_chat = self._cached_chat_peer(chat_id)
            chat_peer = await self._input_entity(
                chat if chat is not None else (
                    cached_chat if cached_chat is not None else chat_id
                )
            )

            until_date = (
                None if duration_seconds is None
                else datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            )

            chat_peer_name = type(chat_peer).__name__
            is_channel = (
                chat_peer_name in {"InputPeerChannel", "InputChannel"}
                or "Channel" in chat_peer_name
                or getattr(chat_peer, "channel_id", None) is not None
            )

            if is_channel and functions is not None and types is not None:
                rights = types.ChatBannedRights(
                    until_date=until_date,
                    view_messages=False,
                    send_messages=True,
                    send_media=True,
                    send_stickers=True,
                    send_gifs=True,
                    send_games=True,
                    send_inline=True,
                    embed_links=True,
                    send_polls=True,
                    change_info=False,
                    invite_users=False,
                    pin_messages=False
                )
                await self.client(functions.channels.EditBannedRequest(
                    channel=chat_peer,
                    participant=user_peer,
                    banned_rights=rights
                ))
            else:
                # Basic chat or high-level client wrapper
                await self.client.edit_permissions(
                    chat_peer,
                    user_peer,
                    send_messages=False,
                    send_media=False,
                    send_stickers=False,
                    send_gifs=False,
                    send_games=False,
                    send_inline=False,
                    embed_links=False,
                    send_polls=False,
                    until_date=until_date,
                )

            self.circuit_breaker.record_success(chat_id)

            self.logger.log_action(
                "MUTE",
                user_id,
                chat_id,
                "سکوت دائم" if duration_seconds is None
                else f"به مدت {duration_seconds} ثانیه"
            )

            return True

        except (ChatAdminRequiredError, UserAdminInvalidError) as e:
            self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در سکوت کاربر (عدم دسترسی ادمین): {e}")
            return False
        except Exception as e:
            if _is_flood_wait(e):
                raise
            from modules.cache_manager import is_permission_error
            if is_permission_error(e):
                self.circuit_breaker.record_failure(chat_id, e)
            print("MUTE ERROR:", repr(e))
            self.logger.log_error(f"خطا در سکوت کاربر: {e}")
            return False

    async def unmute_user(self, chat_id, user_id, *, user=None,
                          chat=None) -> bool:
        return await self._run_moderation_with_timeout(
            "unmute", user_id, 15,
            self._unmute_user_rpc(
                chat_id, user_id, user=user, chat=chat,
            )
        )

    async def _unmute_user_rpc(self, chat_id, user_id, *, user=None,
                               chat=None) -> bool:
        if not self.circuit_breaker.can_execute(chat_id, "unmute"):
            return False
        try:
            user_peer = await self._input_entity(
                user if user is not None else user_id
            )
            cached_chat = self._cached_chat_peer(chat_id)
            chat_peer = await self._input_entity(
                chat if chat is not None else (
                    cached_chat if cached_chat is not None else chat_id
                )
            )

            # کانال‌ها (گروه‌های سروش): عین الگوی unban_user — مستقیم
            # EditBannedRequest با همهٔ پرچب‌ها False (= مجاز). wrapper
            # بالادستی edit_permissions در این fork پرچب‌ها را برای
            # کانال درست اعمال نمی‌کرد و سکوت‌ها برمی‌نمی‌داشتند.
            chat_peer_name = type(chat_peer).__name__
            is_channel = (
                chat_peer_name in {"InputPeerChannel", "InputChannel"}
                or "Channel" in chat_peer_name
                or getattr(chat_peer, "channel_id", None) is not None
            )
            if is_channel and functions is not None and types is not None:
                await self.client(
                    functions.channels.EditBannedRequest(
                        channel=chat_peer,
                        participant=user_peer,
                        banned_rights=types.ChatBannedRights(
                            until_date=None,
                            view_messages=False,
                            send_messages=False,
                            send_media=False,
                            send_stickers=False,
                            send_gifs=False,
                            send_games=False,
                            send_inline=False,
                            embed_links=False,
                            send_polls=False,
                            change_info=False,
                            invite_users=False,
                            pin_messages=False
                        )
                    )
                )
            else:
                await self.client.edit_permissions(
                    chat_peer,
                    user_peer,
                    send_messages=True,
                    send_media=True,
                    send_stickers=True,
                    send_gifs=True,
                    send_games=True,
                    send_inline=True,
                    send_polls=True,
                    until_date=None
                )
            self.circuit_breaker.record_success(chat_id)

            self.logger.log_action("UNMUTE", user_id, chat_id)
            return True

        except (ChatAdminRequiredError, UserAdminInvalidError) as e:
            self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در unmute {user_id} (عدم دسترسی ادمین): {e}")
            return False
        except Exception as e:
            if _is_flood_wait(e):
                raise
            from modules.cache_manager import is_permission_error
            if is_permission_error(e):
                self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در unmute {user_id}: {e}")
            return False


    async def ban_user(self, chat_id, user_id,
                       reason="حذف دائمی به دلیل اسپم", *, user=None,
                       chat=None) -> bool:
        # 🔇 حالت مجازات گروه: اگر مالک «تغییر مجازات» را روی سکوت گذاشته
        # باشد، همین‌جا به جای بن، سکوت دائمی اعمال می‌شود. مقدار برگشتی و
        # مسیر موفقیت/شکست عیناً مثل بن است تا callback ها و سیستم پاکسازی
        # (حذف پیام‌های موج، اعلان نهایی، punished_users) هیچ تغییری نکنند.
        # دستورهای دستی (مثل «اخراج») از این تابع عبور نمی‌کنند.
        try:
            from modules import punishment_mode
            mute_instead = punishment_mode.is_mute(chat_id)
        except Exception:
            mute_instead = False
        if mute_instead:
            success = await self.mute_user(
                chat_id, user_id, None, user=user, chat=chat,
            )
            if success:
                self.logger.log_action(
                    "MUTE_INSTEAD_OF_BAN", user_id, chat_id, reason
                )
            return success
        return await self._run_moderation_with_timeout(
            "ban", user_id, 45,
            self._ban_user_rpc(
                chat_id, user_id, reason, user=user, chat=chat,
            )
        )

    async def _ban_user_rpc(self, chat_id, user_id,
                            reason="حذف دائمی به دلیل اسپم", *, user=None,
                            chat=None) -> bool:
        """بن دائمی و ثبت پایدار کاربر برای جلوگیری از بازگشت."""
        if not self.circuit_breaker.can_execute(chat_id, "ban"):
            return False
        try:
            # Startup already resolved the bot identity. Repeating get_me for
            # every ban added an avoidable Soroush request to the hot path.
            if self.bot_account_id is None:
                me = await self.client.get_me()
                self.bot_account_id = getattr(me, "id", None)
            if str(user_id) == str(self.bot_account_id):
                self.logger.log_error(
                    "LEAVE REQUEST DEBUG\n"
                    f"chat_id={chat_id}\n"
                    "reason=blocked self-targeted ban\n"
                    "trigger_file=modules/admin_actions.py\n"
                    "trigger_function=AdminActions.ban_user"
                )
                return False

            source_user = user
            user_peer = await self._input_entity(
                source_user if source_user is not None else user_id
            )
            cached_chat = self._cached_chat_peer(chat_id)
            chat_peer = await self._input_entity(
                chat if chat is not None else (
                    cached_chat if cached_chat is not None else chat_id
                )
            )

            # A permanent ban remains one EditBannedRequest. Passing resolved
            # peers prevents edit_permissions from re-resolving short IDs.
            await self.client.edit_permissions(
                chat_peer,
                user_peer,
                until_date=None,
                view_messages=False,
            )
            self.circuit_breaker.record_success(chat_id)

            try:
                from modules.banned_storage import add_banned

                metadata = source_user if source_user is not None else user_peer
                username = getattr(metadata, "username", None)
                display_name = " ".join(
                    part for part in (
                        getattr(metadata, "first_name", None),
                        getattr(metadata, "last_name", None),
                    ) if part
                ).strip()
                add_banned(
                    chat_id,
                    user_id,
                    username=username,
                    display_name=display_name,
                    reason=reason,
                )

            except Exception as e:
                self.logger.log_error(f"storage ban error: {e}")

            self.logger.log_action(
                "BAN",
                user_id,
                chat_id,
                reason
            )

            return True

        except (ChatAdminRequiredError, UserAdminInvalidError) as e:
            self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در بن دائمی {user_id} (عدم دسترسی ادمین): {e}")
            return False
        except Exception as e:
            if _is_flood_wait(e):
                raise
            from modules.cache_manager import is_permission_error
            if is_permission_error(e):
                self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در بن دائمی {user_id}: {e}")
            return False

    async def send_warning(self, chat_id, username: str, reason: str, count: int,
                           threshold: int, reply_to=None, user=None, is_edit=False):
        """ارسال هشدار با قالب کوتاه و entityهای واقعی SPlus."""
        if not self.config.get("send_warning", True):
            return
        # ⏱️ پنجرهٔ ۲۰ ثانیه‌ای اخطار: در موج اسپم، برای هر کاربر در هر
        # گروه فقط «اولین» متن اخطار ارسال می‌شود؛ حذف پیام و افزایش
        # شمارندهٔ تخلف مثل قبل برای همهٔ پیام‌ها انجام شده است. این فقط
        # از تکرار ۴-۵ بارهٔ متن اخطار و خفه شدن صف ارسال گروه جلوگیری
        # می‌کند. تخلف‌های با فاصلهٔ بیش از ۲۰ ثانیه مثل قبل همگی اخطار
        # می‌گیرند.
        import time as _time
        gate = getattr(self, "_warning_gate", None)
        if gate is None:
            gate = self._warning_gate = {}
        user_key = getattr(user, "id", None) if user is not None else None
        gate_key = (str(chat_id), str(user_key if user_key is not None else username))
        group_gate_key = ("group", str(chat_id))
        now_mono = _time.monotonic()
        # One warning per user remains available, but a spam wave with many
        # users must not turn warnings into a P0 flood that starves deletes
        # and real owner commands on the single Soroush connection.
        if not is_edit and (now_mono - gate.get(gate_key, -999.0) < 20.0
                or now_mono - gate.get(group_gate_key, -999.0) < 3.0):
            return
        gate[gate_key] = now_mono
        gate[group_gate_key] = now_mono
        if len(gate) > 2000:
            cutoff = now_mono - 60.0
            for stale in [k for k, v in gate.items() if v < cutoff]:
                gate.pop(stale, None)
        try:
            try:
                from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
                has_entities = True
            except ImportError:
                has_entities = False

            from modules.user_display import format_user
            # A complete entity is preferred; legacy callers may only provide
            # a username, which is still rendered with the same policy.
            display_source = user if user is not None else {"username": username}
            name = format_user(display_source)
            reason_line = public_warning_reason_line(reason)
            digits = str(count).translate(str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"))
            max_digits = str(threshold).translate(str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"))
            # قالب استاندارد Foxora (دقیقاً مطابق تصویر تأییدشده):
            #   [نقل‌قول] ⚠️ کاربر @name
            #   پیام شما حذف شد | دلیل: …        ← بدون خط فاصله، چسبیده به بالا
            #   (دو خط خالی)
            #   [Bold] تعداد اخطار: 𝟮/𝟱
            header = f"⚠️ کاربر {name}"
            prefix = header + "\n"
            if is_edit:
                body = f"پیام ویرایش‌شده شما حذف شد | {reason_line}\n"
            else:
                body = f"پیام شما حذف شد | {reason_line}\n"
            warning_label = "تعداد اخطار:" + f" {digits}/{max_digits}"
            # body خودش با \n تمام می‌شود؛ دو \n دیگر = دو خط خالی قبل از شمارنده.
            msg = prefix + body + "\n\n" + warning_label
            u16 = lambda value: len(value.encode("utf-16-le")) // 2
            if has_entities:
                entities = [
                    # نقل‌قول فقط روی خط عنوان؛ هیچ خط خالی داخلش نیست.
                    MessageEntityBlockquote(offset=0, length=u16(header)),
                    MessageEntityBold(
                        offset=u16(prefix + body + "\n\n"),
                        length=u16(warning_label),
                    ),
                ]
            else:
                entities = None
            # Use outgoing sender queue if available so this warning does not block the caller's worker
            sender = getattr(self.client, "_outgoing_sender", None)
            if sender is not None:
                def _factory_warn():
                    return self.client.send_message(chat_id, msg, reply_to=reply_to, formatting_entities=entities)
                # Capture sent for cleanup via on_done
                def _on_done_warn(sent):
                    cl = getattr(self, "notice_cleanup", None)
                    if cl is not None and sent is not None:
                        cl.schedule(chat_id, sent)
                # Warnings are cosmetic background notices. They must never
                # occupy the critical lane ahead of delete/ban/owner commands.
                sender.enqueue_notice(chat_id, lambda: self.client.send_message(chat_id, msg, reply_to=reply_to, formatting_entities=entities), priority=1, on_done=_on_done_warn)
            else:
                sent = await self.client.send_message(
                    chat_id, msg, reply_to=reply_to, formatting_entities=entities)
                cleanup = getattr(self, "notice_cleanup", None)
                if cleanup is not None and sent is not None:
                    cleanup.schedule(chat_id, sent)
        except Exception as e:
            print("WARNING ERROR:", repr(e))
            self.logger.log_error(f"خطا در ارسال هشدار: {e}")

    async def punish_user(
        self, chat_id, user_id, username: str = None, announce: bool = True,
        *, user=None, chat=None,
    ):
        """اعمال مجازات بر اساس تنظیمات"""
        action = self.config.get("action_on_threshold", "mute")
        duration = self.config.get("action_duration_seconds", 3600)

        # Gate announcement to prevent duplicate announcement spam in waves
        import time as _time
        punish_gate = getattr(self, "_punish_gate", None)
        if punish_gate is None:
            punish_gate = self._punish_gate = {}
        punish_key = (str(chat_id), str(user_id))
        now_mono = _time.monotonic()
        if now_mono - punish_gate.get(punish_key, -999.0) < 10.0:
            announce = False
        else:
            punish_gate[punish_key] = now_mono

        if action == "mute":
            success = await self.mute_user(
                chat_id, user_id, duration, user=user, chat=chat,
            )
            if success and announce:
                try:
                    sender2 = getattr(self.client, "_outgoing_sender", None)
                    if sender2 is not None:
                        txt_mute = f"🔇 کاربر @{username or user_id} به دلیل ارسال {self.config.get('spam_threshold')} هرزنامه مکرر، به مدت {duration//60} دقیقه سایلنت شد."
                        def _on_done_mute(sent):
                            cl = getattr(self, "notice_cleanup", None)
                            if cl is not None and sent is not None:
                                cl.schedule(chat_id, sent)
                        sender2.enqueue_notice(chat_id, lambda: self.client.send_message(chat_id, txt_mute), priority=0, on_done=_on_done_mute)
                    else:
                        sent = await self.client.send_message(
                            chat_id,
                            f"🔇 کاربر @{username or user_id} به دلیل ارسال {self.config.get('spam_threshold')} هرزنامه مکرر، به مدت {duration//60} دقیقه سایلنت شد."
                        )
                        cleanup = getattr(self, "notice_cleanup", None)
                        if cleanup is not None:
                            cleanup.schedule(chat_id, sent)
                except:
                    pass
            return success
        elif action in ["ban", "kick"]:
            success = await self.ban_user(
                chat_id, user_id, reason="رسیدن به آستانه تخلفات",
                user=user, chat=chat,
            )
            if success and announce:
                try:
                    sender3 = getattr(self.client, "_outgoing_sender", None)
                    if sender3 is not None:
                        txt_ban = f"⛔️ کاربر @{username or user_id} به دلیل اسپم مکرر از گروه حذف شد."
                        def _on_done_ban(sent):
                            cl = getattr(self, "notice_cleanup", None)
                            if cl is not None and sent is not None:
                                cl.schedule(chat_id, sent)
                        sender3.enqueue_notice(chat_id, lambda: self.client.send_message(chat_id, txt_ban), priority=0, on_done=_on_done_ban)
                    else:
                        sent = await self.client.send_message(
                            chat_id,
                            f"⛔️ کاربر @{username or user_id} به دلیل اسپم مکرر از گروه حذف شد."
                        )
                        cleanup = getattr(self, "notice_cleanup", None)
                        if cleanup is not None:
                            cleanup.schedule(chat_id, sent)
                except:
                    pass
            return success
        return False


    async def unban_user(self, chat_id, user_id, username=None):
        return await self._run_moderation_with_timeout(
            "unban", user_id, 20, self._unban_user_rpc(chat_id, user_id, username)
        )

    async def _unban_user_rpc(self, chat_id, user_id, username=None):
        if not self.circuit_breaker.can_execute(chat_id, "unban"):
            return False
        try:
            from modules.banned_storage import (
                is_banned,
                remove_banned_everywhere,
            )
            from splusthon.tl import functions, types

            user = await self.client.get_entity(user_id)
            entity = await self.client.get_input_entity(chat_id)
            user_entity = await self.client.get_input_entity(user)

            await self.client(
                functions.channels.EditBannedRequest(
                    channel=entity,
                    participant=user_entity,
                    banned_rights=types.ChatBannedRights(
                        until_date=None,
                        view_messages=False,
                        send_messages=False,
                        send_media=False,
                        send_stickers=False,
                        send_gifs=False,
                        send_games=False,
                        send_inline=False,
                        embed_links=False,
                        send_polls=False,
                        change_info=False,
                        invite_users=False,
                        pin_messages=False
                    )
                )
            )
            self.circuit_breaker.record_success(chat_id)

            display_name = " ".join(
                part for part in (
                    getattr(user, "first_name", None),
                    getattr(user, "last_name", None),
                ) if part
            ).strip()
            removed_count, before_records, remaining_records = (
                remove_banned_everywhere(user_id, username, display_name)
            )
            still_banned = is_banned(chat_id, user_id, username)
            print(
                "UNBAN DEBUG BEFORE "
                f"user_id={user_id} username={username} records={before_records}"
            )
            print(
                "UNBAN DEBUG AFTER "
                f"user_id={user_id} remaining={remaining_records} "
                f"is_banned={still_banned}"
            )
            self.logger.log_info(
                "UNBAN DEBUG "
                f"user_id={user_id} username={username} "
                f"removed={removed_count} is_banned={still_banned}"
            )
            if still_banned or remaining_records:
                self.logger.log_error(
                    f"رکورد بن {user_id} پس از آزادسازی هنوز در ذخیره‌سازی باقی مانده است"
                )
                return False

            self.logger.log_action(
                "UNBAN",
                user_id,
                chat_id,
                "رفع بن دائمی"
            )

            return True

        except (ChatAdminRequiredError, UserAdminInvalidError) as e:
            self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در unban {user_id} (عدم دسترسی ادمین): {e}")
            return False
        except Exception as e:
            if _is_flood_wait(e):
                raise
            from modules.cache_manager import is_permission_error
            if is_permission_error(e):
                self.circuit_breaker.record_failure(chat_id, e)
            self.logger.log_error(f"خطا در unban {user_id}: {e}")
            return False
