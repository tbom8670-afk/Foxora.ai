from modules.group_words_storage import add_word, remove_word, get_words
from modules.group_names_storage import (
    add_name,
    remove_name,
    get_names,
    matches_filtered_name,
)
from modules.admin_storage import is_admin
from modules.group_storage import get_group_owner
from modules.owner_check import is_global_owner


_ADD_NAME_PREFIXES = (
    "/فیلتر اسم:", "!فیلتر اسم:", "فیلتر اسم:",
    "/فیلتر اسم ", "!فیلتر اسم ", "فیلتر اسم ",
    "/فیلتر نام:", "!فیلتر نام:", "فیلتر نام:",
    "/فیلتر نام ", "!فیلتر نام ", "فیلتر نام ",
)
_ADD_NAME_BARE = ("/فیلتر اسم", "!فیلتر اسم", "فیلتر اسم", "/فیلتر نام", "!فیلتر نام", "فیلتر نام")

_REMOVE_NAME_PREFIXES = (
    "/لغو اسم:", "!لغو اسم:", "لغو اسم:",
    "/لغو اسم ", "!لغو اسم ", "لغو اسم ",
    "/لغو نام:", "!لغو نام:", "لغو نام:",
    "/لغو نام ", "!لغو نام ", "لغو نام ",
    "/لغو فیلتر اسم ", "!لغو فیلتر اسم ", "لغو فیلتر اسم ",
    "/حذف فیلتر اسم ", "!حذف فیلتر اسم ", "حذف فیلتر اسم ",
    "/رفع فیلتر اسم ", "!رفع فیلتر اسم ", "رفع فیلتر اسم ",
)
_REMOVE_NAME_BARE = (
    "/لغو اسم", "!لغو اسم", "لغو اسم",
    "/لغو نام", "!لغو نام", "لغو نام",
    "/لغو فیلتر اسم", "!لغو فیلتر اسم", "لغو فیلتر اسم",
    "/حذف فیلتر اسم", "!حذف فیلتر اسم", "حذف فیلتر اسم",
    "/رفع فیلتر اسم", "!رفع فیلتر اسم", "رفع فیلتر اسم",
)

_LIST_NAME_COMMANDS = (
    "لیست فیلتر اسم", "/لیست فیلتر اسم", "!لیست فیلتر اسم",
    "لیست فیلتر نام", "/لیست فیلتر نام", "!لیست فیلتر نام",
    "فیلترهای اسم", "/فیلترهای اسم", "!فیلترهای اسم",
)


def is_group_filter_command_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return (
        t.startswith("/فیلتر ")
        or t.startswith("!فیلتر ")
        or t.startswith("/رفع ")
        or t.startswith("!رفع ")
        or t in ("/فیلترها", "فیلترها", "!فیلترها")
        or t in _ADD_NAME_BARE
        or t.startswith(_ADD_NAME_PREFIXES)
        or t in _REMOVE_NAME_BARE
        or t.startswith(_REMOVE_NAME_PREFIXES)
        or t in _LIST_NAME_COMMANDS
    )


def _can_manage_group_words(bot, chat_id, user_id, username):
    group_owner_id = get_group_owner(chat_id)
    return (
        is_global_owner(user_id)
        or (group_owner_id is not None and str(user_id) == str(group_owner_id))
        or is_admin(chat_id, user_id, username)
    )


def _is_member_native_admin(member) -> bool:
    participant = getattr(member, "participant", None)
    if participant is not None:
        p_cls = participant.__class__.__name__.lower()
        if "creator" in p_cls or "admin" in p_cls:
            return True
    if getattr(member, "is_admin", False) or getattr(member, "is_creator", False):
        return True
    return False


async def _punish_existing_members_by_filtered_name(bot, event, chat_id, actor_user_id, phrase: str) -> tuple[int, bool]:
    """بررسی اعضای فعلی گروه هنگام ثبت «فیلتر اسم» و اعمال مجازات گروه (سکوت یا اخراج)."""
    from modules import punishment_mode

    try:
        is_mute_mode = bool(punishment_mode.is_mute(chat_id))
    except Exception:
        is_mute_mode = False

    if bot is None:
        return 0, is_mute_mode
    client = getattr(bot, "client", None)
    admin_actions = getattr(bot, "admin_actions", None)
    if client is None or not callable(getattr(client, "get_participants", None)):
        return 0, is_mute_mode

    chat_target = getattr(event, "chat", None) or chat_id
    try:
        participants = await client.get_participants(chat_target)
    except Exception:
        return 0, is_mute_mode
    if not participants:
        return 0, is_mute_mode

    me_id = getattr(bot, "bot_account_id", None) or getattr(bot, "account_id", None) or getattr(admin_actions, "bot_account_id", None)
    group_owner_id = get_group_owner(chat_id)
    from modules import vip_storage, manual_mute_storage, banned_storage, user_history
    from modules.group_stats import add_kick, add_mute

    punished_count = 0
    for member in participants:
        m_id = getattr(member, "id", None)
        if not m_id:
            continue
        if (me_id is not None and str(m_id) == str(me_id)) or str(m_id) == str(actor_user_id):
            continue
        if is_global_owner(m_id):
            continue
        if group_owner_id is not None and str(m_id) == str(group_owner_id):
            continue
        m_user = getattr(member, "username", None)
        if is_admin(chat_id, m_id, m_user) or _is_member_native_admin(member):
            continue
        try:
            if vip_storage.is_vip(chat_id, m_id):
                continue
        except Exception:
            pass

        fn = getattr(member, "first_name", "") or ""
        ln = getattr(member, "last_name", "") or ""
        if not matches_filtered_name(phrase, fn, ln):
            continue

        dname = " ".join(p for p in (fn, ln) if p).strip()
        if is_mute_mode:
            ok = False
            try:
                if admin_actions is not None and callable(getattr(admin_actions, "mute_user", None)):
                    ok = bool(await admin_actions.mute_user(chat_id, m_id, user=member, chat=chat_target))
                elif callable(getattr(client, "edit_permissions", None)):
                    await client.edit_permissions(chat_id, member, until_date=None, send_messages=False)
                    ok = True
            except Exception:
                ok = False
            if ok:
                punished_count += 1
                try:
                    add_mute(chat_id)
                except Exception:
                    pass
                try:
                    manual_mute_storage.add_manual_mute(chat_id, m_id, username=m_user, display_name=dname)
                except Exception:
                    pass
                try:
                    user_history.add_mute(chat_id, member, f"فیلتر اسم {phrase}")
                except Exception:
                    pass
        else:
            kicked = False
            try:
                if admin_actions is not None and callable(getattr(admin_actions, "ban_user", None)):
                    kicked = bool(
                        await admin_actions.ban_user(
                            chat_id, m_id, reason=f"فیلتر اسم {phrase}", user=member, chat=chat_target
                        )
                    )
                elif callable(getattr(client, "edit_permissions", None)):
                    await client.edit_permissions(chat_id, member, until_date=None, view_messages=False)
                    kicked = True
            except Exception:
                try:
                    if callable(getattr(client, "edit_permissions", None)):
                        await client.edit_permissions(chat_id, member, until_date=None, view_messages=False)
                        kicked = True
                except Exception:
                    kicked = False
            if kicked:
                punished_count += 1
                try:
                    add_kick(chat_id)
                except Exception:
                    pass
                try:
                    banned_storage.add_banned(
                        chat_id,
                        m_id,
                        username=m_user,
                        display_name=dname,
                        reason=f"فیلتر اسم {phrase}",
                        source="name_filter",
                    )
                except Exception:
                    pass
                try:
                    user_history.add_kick(chat_id, member, f"فیلتر اسم {phrase}")
                except Exception:
                    pass

    return punished_count, is_mute_mode


def _reply_spoiler_list(title: str, items: list[str], *, blockquote_header: bool = False):
    try:
        from splusthon.tl.types import (
            MessageEntitySpoiler,
            MessageEntityBold,
            MessageEntityBlockquote,
        )
        has_entities = True
    except ImportError:
        has_entities = False

    u16 = lambda s: len(s.encode("utf-16-le")) // 2
    full_text = f"{title}\n\n"
    entities = []
    if has_entities:
        title_len = u16(title)
        entities.append(MessageEntityBold(offset=0, length=title_len))
        if blockquote_header:
            entities.append(MessageEntityBlockquote(offset=0, length=title_len))

    for i, item in enumerate(items):
        start = u16(full_text)
        full_text += item
        item_len = u16(item)
        if has_entities:
            entities.append(MessageEntitySpoiler(offset=start, length=item_len))
        if i < len(items) - 1:
            full_text += "\n"

    return full_text, entities


async def _reply_bold_blockquote(event, msg: str):
    try:
        from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote
        u16_len = len(msg.encode("utf-16-le")) // 2
        entities = [
            MessageEntityBold(offset=0, length=u16_len),
            MessageEntityBlockquote(offset=0, length=u16_len),
        ]
        return await event.reply(msg, formatting_entities=entities)
    except ImportError:
        return await event.reply(msg)


async def handle_group_word_command(bot, event, text, chat_id, user_id, username=None):

    text = (text or "").strip()

    if not username:
        try:
            sender = await event.get_sender()
            username = getattr(sender, "username", None)
        except Exception:
            username = None

    is_filter_command = is_group_filter_command_text(text)
    if not _can_manage_group_words(bot, chat_id, user_id, username):
        if is_filter_command:
            await event.reply("❌ فقط مالک یا ادمین ثبت‌شده اجازه استفاده از فیلترها را دارد")
            return True
        return False

    # ============================================================
    # ۱. دستورات «فیلتر اسم» / «لغو اسم» / «لیست فیلتر اسم»
    # ============================================================
    if text in _ADD_NAME_BARE or text.startswith(_ADD_NAME_PREFIXES):
        phrase = text
        for prefix in _ADD_NAME_PREFIXES + _ADD_NAME_BARE:
            if phrase.startswith(prefix):
                phrase = phrase[len(prefix):]
                break
        phrase = phrase.strip()
        if not phrase:
            await _reply_bold_blockquote(
                event,
                "⚠️ لطفاً نام مورد نظر را وارد کنید. مثال: فیلتر اسم عسل",
            )
            return True

        added = add_name(chat_id, phrase)
        await _punish_existing_members_by_filtered_name(
            bot, event, chat_id, user_id, phrase
        )
        if added:
            await _reply_bold_blockquote(
                event,
                f"☑️ نام «{phrase}» در فیلتر اسم ثبت شد",
            )
        else:
            await _reply_bold_blockquote(
                event,
                "⚠️ این نام قبلا در فیلتر اسم این گروه ثبت شده است",
            )
        return True

    if text in _REMOVE_NAME_BARE or text.startswith(_REMOVE_NAME_PREFIXES):
        phrase = text
        for prefix in _REMOVE_NAME_PREFIXES + _REMOVE_NAME_BARE:
            if phrase.startswith(prefix):
                phrase = phrase[len(prefix):]
                break
        phrase = phrase.strip()
        if not phrase:
            await _reply_bold_blockquote(
                event,
                "⚠️ لطفاً نام مورد نظر را وارد کنید. مثال: لغو اسم عسل",
            )
            return True

        if remove_name(chat_id, phrase):
            await _reply_bold_blockquote(
                event,
                f"✅ نام «{phrase}» از فیلتر اسم این گروه حذف شد",
            )
        else:
            await _reply_bold_blockquote(
                event,
                "⚠️ این نام در فیلتر اسم این گروه نیست",
            )
        return True

    if text in _LIST_NAME_COMMANDS:
        names = get_names(chat_id)
        if names:
            full_text, entities = _reply_spoiler_list(
                "📋 لیست فیلتر اسم:",
                names,
                blockquote_header=True,
            )
            if entities:
                await event.reply(full_text, formatting_entities=entities)
            else:
                await event.reply(full_text)
        else:
            await _reply_bold_blockquote(
                event,
                "📋 هیچ نامی در فیلتر اسم ثبت نشده",
            )
        return True

    # ============================================================
    # ۲. دستورات «/فیلتر» / «/رفع» / «/فیلترها» (کلمات متن پیام)
    # ============================================================
    if text.startswith("/فیلتر ") or text.startswith("!فیلتر "):
        prefix = "/فیلتر " if text.startswith("/فیلتر ") else "!فیلتر "
        word = text.replace(prefix, "", 1).strip()

        if word:
            if add_word(chat_id, word):
                await event.reply(f"☑️ کلمه «{word}» فیلتر شد")
            else:
                await event.reply("⚠️ این کلمه قبلا برای این گروه ثبت شده")

            return True


    if text.startswith("/رفع ") or text.startswith("!رفع "):
        prefix = "/رفع " if text.startswith("/رفع ") else "!رفع "
        word = text.replace(prefix, "", 1).strip()

        if word:
            if remove_word(chat_id, word):
                await event.reply(f"✅ کلمه «{word}» از فیلتر این گروه حذف شد")
            else:
                await event.reply("⚠️ این کلمه در فیلترها نیست")

            return True


    if text in ("/فیلترها", "فیلترها", "!فیلترها"):

        words = get_words(chat_id)

        if words:
            full_text, entities = _reply_spoiler_list("📋 کلمات فیلتر شده:", words)
            if entities:
                await event.reply(full_text, formatting_entities=entities)
            else:
                await event.reply(full_text)
        else:
            await event.reply("📋 هیچ کلمه‌ای فیلتر نشده")

        return True


    return False
