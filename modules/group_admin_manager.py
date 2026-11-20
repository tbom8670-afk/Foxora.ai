# -*- coding: utf-8 -*-
"""Native Soroush Plus group administrators, independent of admins.json.

Only the owner_id registered for *this group* in groups.json can call these
operations.  No bot-admin/global-owner fallback, no local native-admin ledger,
and no changes to the existing bot-admin or «اختصاصی» systems.

SPlusthon 1.1.4 (and the project's existing «اختصاصی» route) uses
channels.EditAdminRequest(channel, user_id, ChatAdminRights, rank).  ``rank``
is the actual custom administrator title.  Some Soroush servers reject
GetParticipantRequest with 422 NOT_SUPPORTED: use fresh, paginated
GetParticipantsRequest reads instead, never treating a partial list as a
negative.  A zero-rights promotion would be a demotion on this API, so it is
never sent as a promotion.
"""
from __future__ import annotations

import asyncio
import inspect
import time

from modules.group_actions import GroupActions
from modules.group_storage import get_group_owner
from modules.self_admin_shield import is_self_target, known_bot_id

try:
    from splusthon import utils
    from splusthon.tl import functions, types
except ImportError:  # Fail closed on an incomplete Termux installation.
    utils = functions = types = None


# The six rights visible in Soroush Plus's group administrator screen.  These
# names are checked against ChatAdminRights.__init__ AT RUNTIME before a write.
PERMISSIONS = (
    (1, "تغییر اطلاعات گروه", "change_info"),
    (2, "حذف پیام ها", "delete_messages"),
    (3, "محروم کردن کاربران", "ban_users"),
    (4, "افزودن اعضا", "invite_users"),
    (5, "سنجاق کردن پیام ها", "pin_messages"),
    (6, "افزودن مدیر جدید", "add_admins"),
)
# The reference screenshot explicitly shows 1–5 on, 6 off.  These are
# DISPLAYED defaults, not random API defaults: the owner sees and confirms
# them before any EditAdminRequest is made.
DEFAULT_CHOICES = {number: number != 6 for number, _label, _flag in PERMISSIONS}
READ_TIMEOUT = 8.0
WRITE_TIMEOUT = 12.0
VERIFY_DELAYS = (0.0, 0.5, 1.5, 2.5)
PARTICIPANT_PAGE_SIZE = 200  # SPlusthon's GetParticipants page limit.
PARTICIPANT_PAGE_DELAY = 0.1  # Avoid bursting heavy RPCs in large groups.
MAX_SCAN_SECONDS = 20.0     # An unfinished scan never proves absence.
MAX_ADMIN_PAGES = 10         # Never trust a truncated admin list as a negative.
MAX_MEMBER_PAGES = 10        # A member must be found explicitly to be promoted.


class GroupAdminError(RuntimeError):
    """A safe, user-facing failure (the detailed cause is logged separately)."""


class UncertainOperation(GroupAdminError):
    """The write may have reached Soroush, but read-back was inconclusive."""


def is_registered_group_owner(chat_id, user_id) -> bool:
    """Only a valid, positive owner ID from this group's record is authority.

    A damaged/unreadable owner record is never a reason to fall back to the
    global owner, admins.json, or an unverified sender ID such as zero.
    """
    if chat_id is None or user_id is None or isinstance(user_id, bool):
        return False
    try:
        owner = get_group_owner(chat_id)
        if owner is None or isinstance(owner, bool):
            return False
        owner_id, actor_id = int(str(owner).strip()), int(str(user_id).strip())
        return owner_id > 0 and actor_id > 0 and owner_id == actor_id
    except Exception:
        return False


def require_owner(chat_id, user_id):
    if not is_registered_group_owner(chat_id, user_id):
        raise GroupAdminError("⛔ فقط مالک ثبت‌شدهٔ همین گروه می‌تواند این دستور را اجرا کند.")


def _schema_flags():
    if functions is None or types is None or utils is None:
        raise GroupAdminError("❌ کتابخانهٔ SPlusthon نصب نیست؛ عملیات انجام نشد.")
    try:
        params = inspect.signature(types.ChatAdminRights.__init__).parameters
        names = tuple(name for name, param in params.items() if name != "self"
                      and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                         inspect.Parameter.KEYWORD_ONLY))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GroupAdminError("❌ ساختار دسترسی‌های SPlusthon قابل بررسی نیست؛ هیچ تغییری انجام نشد.") from exc
    expected = {flag for _number, _label, flag in PERMISSIONS}
    if not expected.issubset(names) or len(names) < len(expected):
        raise GroupAdminError("❌ نسخهٔ SPlusthon این شش دسترسی مدیریتی را پشتیبانی نمی‌کند.")
    return names


def build_rights(choices):
    """Explicit booleans for ALL library flags, with exactly six owner choices.

    Flags outside the six group permissions (stories, anonymity, etc.) are
    explicitly False; no unspecified server/library default can grant them.
    """
    flags = _schema_flags()
    if not isinstance(choices, dict) or set(choices) != set(DEFAULT_CHOICES) \
            or any(type(value) is not bool for value in choices.values()):
        raise GroupAdminError("❌ وضعیت هر شش دسترسی باید صریح و مشخص باشد.")
    if not any(choices.values()):
        raise GroupAdminError("❌ برای ادمین شدن حداقل یکی از شش دسترسی باید روشن باشد.")
    values = {flag: False for flag in flags}
    for number, _label, flag in PERMISSIONS:
        values[flag] = choices[number]
    return types.ChatAdminRights(**values)


def validate_rank(raw):
    """A free-form, nonempty, single-line title; server rules stay with API."""
    rank = str(raw or "").strip()
    if not rank or any(character in rank for character in ("\n", "\r", "\x00")):
        raise GroupAdminError("❌ لقب مدیر باید یک عبارت غیرخالی در یک خط باشد.")
    return rank


def _protect_existing_exclusive(chat_id, user_id):
    """Read-only safety guard: don't race «اختصاصی»'s automatic regrant.

    That existing feature can promote a recipient again after a native demote
    or overwrite a new admin's title/rights.  We neither call its mutators nor
    store anything in its state; instead we ask the owner to revoke that
    separate, temporary permission first.  An unreadable state fails closed.
    """
    try:
        from modules.manage_features.exclusive import list_entries
        entries = list_entries(chat_id)
    except Exception as exc:
        raise GroupAdminError(
            "❌ وضعیت دسترسی موقت «اختصاصی» قابل بررسی نیست؛ عملیات انجام نشد."
        ) from exc
    if any(str(entry.get("user_id")) == str(user_id) for entry in entries):
        raise GroupAdminError(
            "⚠️ این کاربر دسترسی موقت «اختصاصی» دارد؛ ابتدا «لغو اختصاصی» "
            "را برای همین کاربر اجرا کنید تا دو قابلیت با هم تداخل نکنند."
        )


def _target_id(entity):
    value = getattr(entity, "user_id", getattr(entity, "id", None))
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise GroupAdminError("❌ کاربرِ پیام ریپلای‌شده شناسایی نشد.") from exc
    if value <= 0:
        raise GroupAdminError("❌ شناسهٔ کاربرِ پیام ریپلای‌شده نامعتبر است.")
    return value


async def _inputs(bot, chat_id, entity):
    _schema_flags()
    # Shared, tested resolver handles positive short group IDs and -100 IDs.
    # The native API requires an InputChannel; normal chats and broadcasts
    # must not be silently treated as channels through an integer ID.
    try:
        peer = await GroupActions(bot.client, bot.logger)._group_peer(chat_id)
        channel = utils.get_input_channel(peer)
    except Exception as exc:
        raise GroupAdminError(
            "❌ این گروه به‌صورت سوپرگروه قابل شناسایی نیست؛ عملیات انجام نشد."
        ) from exc
    try:
        input_peer = await bot.client.get_input_entity(entity)
        input_user = utils.get_input_user(input_peer)
        participant_peer = utils.get_input_peer(input_peer)
    except Exception as exc:
        raise GroupAdminError("❌ کاربرِ پیام ریپلای‌شده در سروش‌پلاس قابل شناسایی نیست.") from exc
    user_id = _target_id(entity)
    if _target_id(input_user) != user_id:
        raise GroupAdminError("❌ هویت کاربرِ پیام ریپلای‌شده تغییر کرده است؛ دوباره تلاش کنید.")
    if is_self_target(entity, bot=bot) or is_self_target(input_user, bot=bot):
        raise GroupAdminError("❌ ادمینی خودِ حساب ربات را نمی‌توان با این دستور تغییر داد.")
    return channel, input_user, participant_peer, user_id


class _NoAdminInCompleteList:
    """Only proves *not admin*; never proves that a user is a normal member."""

    def __init__(self, user_id):
        self.user_id = user_id


def _role_id(role):
    """TL participant IDs are on the role, NOT aligned with ``page.users``."""
    known = (types.ChannelParticipant, types.ChannelParticipantSelf,
             types.ChannelParticipantAdmin, types.ChannelParticipantCreator,
             types.ChannelParticipantLeft, types.ChannelParticipantBanned)
    if not isinstance(role, known):
        raise RuntimeError("unknown participant role in server response")
    # Left/Banned contain a PeerUser instead of a top-level user_id.
    return _target_id(getattr(role, "peer", role))


async def _scan_roles(bot, channel, filter_type, expected_id, *, max_pages,
                      admins_only=False):
    """Find a positively identified role, or prove a *complete* filter list.

    ``hash=0`` bypasses NotModified/cached snapshots: read-back after an edit
    must be fresh.  A missing/invalid count, missing role metadata, repeated
    page, premature empty page, or the page cap is UNKNOWN, not an absence.
    ``page.users`` must not be zipped with ``page.participants``: TL does not
    promise they are in the same order or that all referenced users are roles.
    """
    offset, seen = 0, set()
    total, started = None, time.monotonic()
    for _page_number in range(max_pages):
        remaining = MAX_SCAN_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("participants scan exceeded its time budget")
        page = await asyncio.wait_for(
            bot.client(functions.channels.GetParticipantsRequest(
                channel=channel, filter=filter_type(), offset=offset,
                limit=PARTICIPANT_PAGE_SIZE, hash=0)),
            min(READ_TIMEOUT, remaining))
        count = getattr(page, "count", None)
        rows = getattr(page, "participants", None)
        if type(count) is not int or count < 0 or not isinstance(rows, (list, tuple)) \
                or len(rows) > PARTICIPANT_PAGE_SIZE \
                or count < offset + len(rows) \
                or (total is not None and total != count):
            raise RuntimeError("invalid or changing participants page")
        total = count
        if not rows and offset < total:
            raise RuntimeError("incomplete participants page")
        for role in rows:
            role_id = _role_id(role)
            if (role_id in seen or
                    (admins_only and not isinstance(
                        role, (types.ChannelParticipantAdmin,
                               types.ChannelParticipantCreator)))):
                raise RuntimeError("duplicated or unfiltered participants page")
            seen.add(role_id)
            if role_id == expected_id:
                return role
        offset += len(rows)
        if offset >= total:
            return None
        await asyncio.sleep(PARTICIPANT_PAGE_DELAY)
    raise RuntimeError("participants list exceeded the safe page limit")


async def _read_via_lists(bot, channel, expected_id, *, admins_only=False):
    # An existing admin must be found BEFORE any ordinary-member promotion.
    # In particular, not seeing someone in the first 200 admins proves nothing.
    admin = await _scan_roles(bot, channel, types.ChannelParticipantsAdmins,
                              expected_id, max_pages=MAX_ADMIN_PAGES,
                              admins_only=True)
    if admin is not None:
        return admin
    not_admin = _NoAdminInCompleteList(expected_id)
    if admins_only:
        return not_admin
    try:
        member = await _scan_roles(bot, channel, types.ChannelParticipantsRecent,
                                   expected_id, max_pages=MAX_MEMBER_PAGES)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The complete admin list still proves non-admin for demotion checks,
        # but an unknown member list can NEVER authorize a promotion.
        logger = getattr(bot, "logger", None)
        if logger is not None:
            try:
                logger.log_error("NATIVE GROUP ADMIN MEMBER LIST FAILED "
                                 f"user={expected_id} error={exc!r}")
            except Exception:
                pass
        return not_admin
    return member if member is not None else not_admin


async def _read(bot, channel, participant_peer, expected_id, *, admins_only=False):
    try:
        result = await asyncio.wait_for(
            bot.client(functions.channels.GetParticipantRequest(
                channel=channel, participant=participant_peer)), READ_TIMEOUT)
        participant = getattr(result, "participant", None)
        if _role_id(participant) != expected_id:
            raise RuntimeError("GetParticipantRequest returned a different user")
        return participant
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Documented by force_join_api: some Soroush servers answer
        # channels.getParticipant with 422 NOT_SUPPORTED.  A fresh, typed
        # participants list can also recover from other direct-read failures.
        logger = getattr(bot, "logger", None)
        if logger is not None:
            try:
                logger.log_info("NATIVE GROUP ADMIN DIRECT READ FAILED "
                                f"user={expected_id} error={error!r}; trying list")
            except Exception:
                pass
        return await _read_via_lists(bot, channel, expected_id,
                                     admins_only=admins_only)


def _admin(participant):
    return isinstance(participant, types.ChannelParticipantAdmin)


def _creator(participant):
    return isinstance(participant, types.ChannelParticipantCreator)


def _not_admin(participant):
    """Positive non-admin role, or absence in a complete admin-filtered list."""
    return isinstance(participant, (
        _NoAdminInCompleteList, types.ChannelParticipant,
        types.ChannelParticipantSelf, types.ChannelParticipantLeft,
        types.ChannelParticipantBanned))


async def _bot_promote_right(bot, channel):
    bot_id = known_bot_id(bot=bot)
    bot_entity = bot_id
    try:
        # The get_me entity carries its own access_hash.  Resolving just the
        # numeric bot ID can fail when SPlusthon has no cached user entity.
        me = await asyncio.wait_for(bot.client.get_me(), READ_TIMEOUT)
        me_id = _target_id(me)
        if bot_id is not None and me_id != bot_id:
            raise GroupAdminError("❌ شناسهٔ حساب ربات با نشست فعلی سازگار نیست.")
        bot_id, bot_entity = me_id, me
    except asyncio.CancelledError:
        raise
    except GroupAdminError:
        raise
    except Exception as exc:
        if bot_id is None:
            raise GroupAdminError(
                "❌ هویت حساب ربات برای بررسی مجوز ادمینی مشخص نیست.") from exc
    try:
        bot_peer = await bot.client.get_input_entity(bot_entity)
        bot_peer = utils.get_input_peer(bot_peer)
        participant = await _read(bot, channel, bot_peer, bot_id,
                                  admins_only=True)
    except Exception as exc:
        raise GroupAdminError("❌ دسترسی «افزودن مدیر» حساب ربات در این گروه قابل بررسی نیست.") from exc
    if _creator(participant):
        return bot_id
    if not _admin(participant) or not bool(getattr(participant.admin_rights, "add_admins", False)):
        raise GroupAdminError("❌ حساب ربات در این گروه دسترسی «افزودن مدیر جدید» ندارد.")
    return bot_id


def _matches(participant, rank, choices):
    if not _admin(participant) or getattr(participant, "rank", None) != rank:
        return False
    rights = getattr(participant, "admin_rights", None)
    if rights is None:
        return False
    values = {flag: choices[number] for number, _label, flag in PERMISSIONS}
    values.update({flag: False for flag in _schema_flags() if flag not in values})
    return all(bool(getattr(rights, flag, None)) == enabled
               for flag, enabled in values.items())


async def _verify(bot, channel, participant_peer, user_id, predicate):
    """Read after a write, tolerating Soroush's short propagation delay."""
    observed = None
    for delay in VERIFY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            observed = await _read(bot, channel, participant_peer, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        if predicate(observed):
            return True, observed
    return False, observed


async def promote(bot, chat_id, actor_id, target, rank, choices):
    """One native promotion with title+rights, plus read-back.

    Never write admins.json.  A role explicitly read back as admin may be
    reported as *admin*, even when its optional title/rights are unavailable:
    the user-facing message asks the owner to inspect those details, without
    claiming they were verified.  An unreadable role is never called success.
    Neither case triggers automatic demotion: ``promoted_by`` alone does not
    prove that this particular write created the role.
    """
    require_owner(chat_id, actor_id)
    rank = validate_rank(rank)
    rights = build_rights(choices)
    channel, input_user, participant_peer, user_id = await _inputs(bot, chat_id, target)
    if str(get_group_owner(chat_id)) == str(user_id):
        raise GroupAdminError("❌ مالک ثبت‌شدهٔ گروه را با این دستور تغییر ندهید.")
    _protect_existing_exclusive(chat_id, user_id)
    try:
        before = await _read(bot, channel, participant_peer, user_id)
    except Exception as exc:
        raise GroupAdminError("❌ وضعیت فعلی کاربر در گروه قابل بررسی نیست؛ ارتقا انجام نشد.") from exc
    if _creator(before):
        raise GroupAdminError("❌ مالک اصلی گروه را نمی‌توان دوباره ادمین کرد.")
    if _admin(before):
        raise GroupAdminError("⚠️ این کاربر از قبل ادمین گروه است؛ دسترسی‌هایش تغییر نکرد.")
    if isinstance(before, _NoAdminInCompleteList):
        raise GroupAdminError("❌ عضویت عادی این کاربر قابل تأیید نیست؛ ارتقا انجام نشد.")
    if not isinstance(before, (types.ChannelParticipant, types.ChannelParticipantSelf)):
        raise GroupAdminError("❌ این کاربر عضو عادی گروه نیست؛ ارتقا انجام نشد.")
    await _bot_promote_right(bot, channel)
    require_owner(chat_id, actor_id)  # owner may have changed while RPCs ran
    _protect_existing_exclusive(chat_id, user_id)
    write_error = None
    try:
        await asyncio.wait_for(
            bot.client(functions.channels.EditAdminRequest(
                channel=channel, user_id=input_user, admin_rights=rights,
                rank=rank)), WRITE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        write_error = exc
    confirmed, observed = await _verify(
        bot, channel, participant_peer, user_id,
        lambda participant: _matches(participant, rank, choices))
    if confirmed:
        return user_id
    if observed is not None and _admin(observed):
        # The server positively reports this user as admin, but may omit rank
        # or rights in a participant-list response.  The visible reply only
        # confirms the role and explicitly asks the owner to check management;
        # it does NOT claim that the selected title/rights were verified.  Do
        # not issue a destructive rollback for an already-existing admin.
        logger = getattr(bot, "logger", None)
        if logger is not None:
            try:
                logger.log_info(
                    "NATIVE GROUP ADMIN ROLE SEEN, DETAILS UNVERIFIED "
                    f"group={chat_id} user={user_id} write_error={write_error!r}")
            except Exception:
                pass
        return user_id
    if observed is None:
        raise UncertainOperation(
            "⚠️ پاسخ سرور برای لقب و دسترسی‌ها قابل بررسی نیست؛ "
            "موفقیت اعلام نشد. لطفاً وضعیت ادمین‌های همین گروه را بررسی کنید."
        ) from write_error
    if write_error is not None:
        raise GroupAdminError("❌ سروش‌پلاس درخواست ادمین‌کردن را نپذیرفت؛ هیچ وضعیتی در ربات ثبت نشد.") from write_error
    raise UncertainOperation("⚠️ ادمینی و لقب در سرور تأیید نشد؛ وضعیت گروه را بررسی کنید.")


async def demote(bot, chat_id, actor_id, target):
    """Revoke ONLY this group's native role; no bot-admin/owner state touched."""
    require_owner(chat_id, actor_id)
    channel, input_user, participant_peer, user_id = await _inputs(bot, chat_id, target)
    if str(get_group_owner(chat_id)) == str(user_id):
        raise GroupAdminError("❌ ادمینی مالک ثبت‌شدهٔ گروه با این دستور لغو نمی‌شود.")
    _protect_existing_exclusive(chat_id, user_id)
    try:
        before = await _read(bot, channel, participant_peer, user_id)
    except Exception as exc:
        raise GroupAdminError("❌ وضعیت فعلی کاربر در گروه قابل بررسی نیست؛ لغو انجام نشد.") from exc
    if _creator(before):
        raise GroupAdminError("❌ مالک اصلی گروه را نمی‌توان از مدیریت عزل کرد.")
    if not _admin(before):
        raise GroupAdminError("⚠️ این کاربر در این گروه ادمین نیست.")
    await _bot_promote_right(bot, channel)
    require_owner(chat_id, actor_id)
    _protect_existing_exclusive(chat_id, user_id)
    write_error = None
    try:
        await asyncio.wait_for(
            bot.client(functions.channels.EditAdminRequest(
                channel=channel, user_id=input_user,
                admin_rights=types.ChatAdminRights(), rank="")), WRITE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        write_error = exc
    confirmed, observed = await _verify(bot, channel, participant_peer, user_id,
                                        _not_admin)
    if confirmed:
        return user_id
    if observed is None:
        raise UncertainOperation(
            "⚠️ نتیجهٔ لغو ادمینی از سرور قابل بررسی نیست؛ وضعیت همین گروه را بررسی کنید."
        ) from write_error
    if write_error is not None:
        raise GroupAdminError("❌ سروش‌پلاس لغو ادمینی را نپذیرفت؛ تغییری ثبت نشد.") from write_error
    raise UncertainOperation("⚠️ سروش‌پلاس هنوز کاربر را ادمین نشان می‌دهد؛ لغو تأیید نشد.")
