# -*- coding: utf-8 -*-
"""🔐 «اختصاصی» — اجازهٔ ارسال پیام به یک کاربر در گروهِ قفل‌شده.

روش اثبات‌شده روی سرور سروش (اجرای واقعی، نه حدس):

* برداشتنِ محدودیتِ ارسال به‌تنهایی (``EditBannedRequest`` با همهٔ فلگ‌ها
  ``False``) در گروهِ قفل **کاربر را قادر به ارسال نمی‌کند**؛ ربات «انجام نشد»
  می‌داد. پس مسیرِ نهایی، همان دستور کارفرما است:
* **کاربر ادمینِ گروه می‌شود، ولی فقط با حق ارسال پیام**
  (``EditAdminRequest`` + ``ChatAdminRights`` که همهٔ فلگ‌ها ``False`` است جز
  ``edit_messages``). هیچ حقِ حذف پیام / سنجاق / افزودن ادمین / تغییر اطلاعات /
  بن / دعوت / anonymous ندارد.
* لغو: ``EditAdminRequest`` با ``ChatAdminRights()`` (همه None ⇒ demote).
  **محدودیتِ شخصی (mute) روی کاربر گذاشته نمی‌شود**: عضو عادی در گروه قفل از
  پیش‌فرضِ گروه ارث می‌برد و بعد از «باز شدن» گروه باید بتواند بنویسد.
  اگر از قبل محدودیتِ شخصی داشت، با ``banned_rights`` همه-False پاک می‌شود.
* تأییدِ نهایی با ``channels.GetParticipantRequest`` انجام می‌شود و
  **سه‌حالته** است: ``True`` (ادمین است) / ``False`` (ادمین نیست) /
  ``None`` (نامعلوم: خطای خواندن یا تأخیرِ سرور). در حالت نامعلوم **هیچ RPCِ
  تغییردهنده‌ای** زده نمی‌شود؛ رکورد در وضعیت بازیکابی می‌ماند.

⚠️ اصلاحات مهمِ این نسخه (هر کدام با شمارهٔ گزارشِ کارفرما):

1. پریستِ ``none`` حذف شد: روی سیم همان «عزل ادمین» بود (``EditAdminRequest``
   با همهٔ فلگ‌های ``None``). ترتیبِ پریست‌ها این‌جا معنادار نیست؛ فقط یک
   پریستِ صریح (``edit_only``) استفاده می‌شود و «موفق» فقط با RPCِ بدونِ خطا +
   تأییدِ سرور اعلام می‌شود.
2. تأییدِ سه‌حالته با backoffِ چندمرحله‌ای (نه ۰٫۳ ثانیه).
3. ``get_participants`` خروجی‌اش **لیست** است (``TotalList``)، نه async
   iterator؛ و به‌جای ``chat_id`` مثبت، ``InputPeer`` کانال داده می‌شود.
4. لایهٔ دستور خطاها را به پیامِ فارسی نگاشت می‌کند (اینجا نوع‌های خطای
   معنادار تعریف شده‌اند).
5. ``entity``/``InputPeer`` کاربر در کل مسیر پاس داده می‌شود (بدون resolveِ
   دوباره با ``get_input_entity(int)``).
6. اگر رکورد ``active`` بود ولی سرور می‌گوید ادمین نیست ⇒ دوباره grant.
7. هیچ ``call(client(req))`` با coroutineِ آماده: همه‌جا ``lambda``/factory.
8. لغو، کاربر را mute نمی‌کند و ``rights_confirmed(allowed=False)`` فقط
   «دیگر ادمین نیست» را بررسی می‌کند.
9. قبل از grant، حقوقِ خودِ ربات خوانده می‌شود؛ اگر ``add_admins`` ندارد
   پیام روشن داده می‌شود و ارتقای بی‌اثر «موفق» شمرده نمی‌شود.
10. فلگ‌های ``ChatAdminRights`` با امضای واقعیِ نصب‌شده فیلتر می‌شوند،
    ``user_peer`` خطای فنی را از «کاربر بن است» جدا می‌کند و کشِ قفل واحد است.
"""
from __future__ import annotations

import asyncio
import inspect

from modules.group_actions import (
    GroupActions,
    clone_banned_rights,
    text_is_banned,
)
from modules.manage_features import peers, storage
from modules.manage_features.rpc import (call, flood_seconds, is_transient,
                                         log_info)

try:
    from splusthon.tl import functions, types
except ImportError:                                  # pragma: no cover
    functions = types = None


PENDING_FLAG = "pending_revoke"
#: ارتقا هنوز از سوی سرور تأیید نشده (برای تلاش دوباره بعد از ری‌استارت).
PENDING_GRANT_FLAG = "pending_grant"
#: وضعیت‌هایی که باید بازیابی/تکرار شوند (فعال‌ها هم بعد از ری‌استارت
#: دوباره با سرور تطبیق داده می‌شوند).
RECOVERABLE = ("active", PENDING_FLAG, PENDING_GRANT_FLAG)

#: وضعیت‌های نتیجهٔ تأیید (سه‌حالته).
VERIFIED = True
NOT_VERIFIED = False
UNKNOWN = None

#: مهلتِ هر RPC در مسیر «اختصاصی» — کارفرما انتظارِ چند ثانیه‌ای دارد، نه دقیقه.
GRANT_RPC_TIMEOUT = 6.0
#: مهلتِ خواندن‌های تأیید (کمی بیشتر؛ خواندنِ وضعیت سبک است).
VERIFY_RPC_TIMEOUT = 8.0
#: backoffِ خواندنِ تأیید (ثانیه). قبلاً یک ``sleep(0.3)`` بود که برای سرورِ
#: سروش کافی نیست؛ این ترتیب جمعاً ~۴٫۵ ثانیه صبر می‌کند.
VERIFY_BACKOFF = (0.0, 0.5, 1.5, 2.5)


class ExcludedUser(RuntimeError):
    """کاربر با دستور «آزاد» از ربات ban شده است؛ نباید پیام بفرستد.

    این استثنا عمداً وجود دارد: قابلیت «اختصاصی» هیچ‌گاه یک banِ ذخیره‌شده
    را دور نمی‌زند (بدون تغییر در لایهٔ حفاظتیِ موجود).
    """


class GrantRejected(RuntimeError):
    """سرور ارتقا را رد کرد (خطای قطعی، بدون ابهام)."""


class BotLacksRights(RuntimeError):
    """ربات خودش حقِ لازم را در گروه ندارد (مثلاً «افزودن ادمین»).

    در این حالت هیچ درخواستی فرستاده نمی‌شود؛ «ارتقای بی‌اثر» هرگز موفق
    شمرده نمی‌شود.
    """


class TargetChanged(RuntimeError):
    """هدف پیدا نشد/کش نشد (entity نامعتبر) — نباید «موفق» اعلام شود."""


class NotLocked(RuntimeError):
    """گروه قفل نیست ⇒ استثنا معنا ندارد."""


class AlreadyExclusive(RuntimeError):
    pass


class NotExclusive(RuntimeError):
    pass


# ---------------------------------------------------------------------------
#  دسترسی به گروه (فقط خواندن؛ هیچ تغییری در group_actions نمی‌دهیم)
# ---------------------------------------------------------------------------
def _actions(bot):
    return GroupActions(bot.client, bot.logger)


def _cached_channel(bot, chat_id):
    """InputPeerِ کانال از کش (بدون RPC) یا ``None``."""
    return peers.channel_peer(bot, chat_id)


def _remember_channel(bot, chat_id, peer):
    return peers.remember_channel_peer(bot, chat_id, peer)


async def _require_channel(bot, chat_id):
    """``(peer, InputChannel)`` با خطای **واضح** اگر گروه کانال/سوپرگروه نبود.

    ``EditAdminRequest`` فقط ``InputChannel`` می‌پذیرد؛ اگر peer از نوع چتِ
    معمولی باشد نتیجه TypeError/خطای مبهم است. این‌جا یک استثنای معنادار
    می‌دهیم تا لایهٔ دستور پیامِ فارسیِ درست بدهد.
    """
    # ترتیب: ۱) کشِ کانالِ این ماژول، ۲) کشِ ربات + resolveِ واقعی، ۳) خطا.
    peer = _cached_channel(bot, chat_id)
    if peer is None:
        try:
            peer = await _actions(bot)._group_peer(chat_id)
        except Exception as error:                   # noqa: BLE001
            raise TargetChanged(
                f"could not resolve group peer for {chat_id}: {error!r}"
            ) from error
    if not (hasattr(peer, "channel_id") and hasattr(peer, "access_hash")):
        raise TargetChanged(
            "group peer is not a channel/supergroup "
            f"(type={type(peer).__name__}); EditAdmin needs InputChannel "
            "⇒ گروه باید سوپرگروه/کانال باشد و ربات باید آن را resolve کند")
    _remember_channel(bot, chat_id, peer)
    return peer, types.InputChannel(peer.channel_id, peer.access_hash)


async def peer_and_channel(bot, chat_id):
    """``(peer, input_channel)`` — امضای قدیمی، حالا با خطای واضح."""
    return await _require_channel(bot, chat_id)


# ---------------------------------------------------------------------------
#  وضعیت قفل (کشِ واحد در peers)
# ---------------------------------------------------------------------------
def lock_state_cached(chat_id):
    return peers.lock_state_cached(chat_id)


def remember_lock_state(chat_id, locked):
    return peers.remember_lock_state(chat_id, locked)


def forget_lock_state(chat_id):
    return peers.forget_lock_state(chat_id)


async def is_group_locked(bot, chat_id) -> bool:
    """آیا گروه الان برای اعضای عادی قفل است؟ (پاسخ از سرور)

    ⚠️ سرعت: اگر نتیجهٔ تازه (< ``peers.LOCK_CACHE_TTL``) در حافظه باشد، همان
    استفاده می‌شود؛ فقط یک بار در هر دوره (و فقط هنگام ثبتِ اختصاصی جدید)
    خوانده می‌شود.
    """
    cached = lock_state_cached(chat_id)
    if cached is not None:
        return cached
    peer = await _group_peer_fast(bot, chat_id)
    rights, _source = await _actions(bot)._fetch_default_banned_rights(
        chat_id, peer)
    locked = text_is_banned(rights)
    remember_lock_state(chat_id, locked)
    return locked


async def _group_peer_fast(bot, chat_id):
    """``InputPeer`` گروه از کش یا (در بدترین حالت) یک resolve."""
    cached = _cached_channel(bot, chat_id)
    if cached is not None:
        return cached
    return await _actions(bot)._group_peer(chat_id)


# ---------------------------------------------------------------------------
#  حقوق ادمینی — فقط فلگ‌های واقعیِ نسخهٔ نصب‌شده
# ---------------------------------------------------------------------------
def _admin_rights_signature():
    """نامِ فلگ‌های واقعیِ ``ChatAdminRights`` در نسخهٔ نصب‌شده.

    اگر کتابخانه در دسترس نباشد، فهرستِ محافظه‌کارانهٔ زیر استفاده می‌شود.
    """
    fallback = ("change_info", "post_messages", "edit_messages",
                "delete_messages", "ban_users", "invite_users", "pin_messages",
                "add_admins", "anonymous", "manage_call", "other",
                "manage_topics")
    if types is None:
        return fallback
    try:
        params = inspect.signature(types.ChatAdminRights.__init__).parameters
    except (TypeError, ValueError):                   # pragma: no cover
        return fallback
    names = [name for name in params if name != "self"]
    return tuple(names) if names else fallback


#: همهٔ فلگ‌های معتبرِ همین نسخه (شامل post_stories/… اگر وجود داشته باشند).
ALL_ADMIN_FLAGS = _admin_rights_signature()

#: حق‌هایی که در هیچ حالتی به کاربرِ «اختصاصی» داده نمی‌شوند.
SENSITIVE_ADMIN_FLAGS = tuple(
    flag for flag in ALL_ADMIN_FLAGS
    if flag not in {"edit_messages"}
)
#: حق‌هایی که واقعاً برای «فقط ارسال پیام» لازم است.
SEND_ONLY_FLAGS = {"edit_messages": True}

#: تنها پریستِ مجاز. ``none`` (همه False) روی سیم = **عزلِ ادمین** است و
#: هرگز نباید در مسیر ارتقا استفاده شود.
ADMIN_RIGHT_PRESETS = (("edit_only", dict(SEND_ONLY_FLAGS)),)
PRIMARY_PRESET = "edit_only"


def admin_rights_for(preset=None):
    """``ChatAdminRights`` با فلگ‌های **صریح** برای حالتِ ``edit_only``.

    همهٔ فلگ‌های شناخته‌شده صریح ``False`` هستند (نه ``None``)، چون ``None``
    یعنی «سرور خودش تصمیم بگیرد» و در سروش «تغییر اطلاعات گروه» و «سنجاق
    پیام‌ها» را روشن می‌کند.
    """
    values = {flag: False for flag in ALL_ADMIN_FLAGS}
    values.update(SEND_ONLY_FLAGS)
    return types.ChatAdminRights(**values)


def send_only_admin_rights():
    """سازگاری عقب‌رو: همان پریستِ ``edit_only``."""
    return admin_rights_for(PRIMARY_PRESET)


def has_sensitive_right(admin_rights) -> bool:
    """آیا حقِ حساسی (به‌جز ویرایشِ پیامِ خودش) روشن است؟"""
    if admin_rights is None:
        return False
    return any(bool(getattr(admin_rights, flag, False))
               for flag in SENSITIVE_ADMIN_FLAGS)


def has_any_management_right(admin_rights) -> bool:
    """آیا حقِ ادمینی به‌جز «ارسال/ویرایش پیام» هم دارد؟ (نام قدیمی)"""
    return has_sensitive_right(admin_rights)


# ---------------------------------------------------------------------------
#  کاربر: entity و InputUser
# ---------------------------------------------------------------------------
async def user_peer(bot, chat_id, user_id, *, entity=None):
    """InputUser/entity کاربر + بررسی «آزاد نشده».

    * خطای **فنی**ِ ``banned_storage`` دیگر با «کاربر واقعاً بن است» قاطی
      نمی‌شود: اگر خواندنِ وضعیت بن شکست بخورد فقط لاگ می‌شود و جلوی کار را
      نمی‌گیرد (fail-open در برابر خرابیِ فایل، ولی با لاگِ صریح).
    * اگر ``entity`` از قبل داریم، هیچ RPCای زده نمی‌شود.
    * شناسه‌های ``str``/``int`` نرمال می‌شوند.
    """
    from modules import banned_storage

    identifier = peers.user_identifier(entity) if entity is not None \
        else peers.user_identifier(user_id)
    if identifier is None:
        raise TargetChanged(f"invalid user identity: {user_id!r}")

    try:
        banned = banned_storage.is_banned(chat_id, identifier)
    except Exception as error:                       # noqa: BLE001
        # خطای فنی (فایل/SQLite) — «بن است» نیست، فقط لاگ می‌شود.
        log_info(bot.logger,
                 "EXCLUSIVE BAN CHECK FAILED (technical, continuing) "
                 f"chat_id={chat_id} user_id={identifier} error={error!r}")
        banned = False
    if banned:
        raise ExcludedUser(
            f"user {identifier} is still banned by the bot in {chat_id}")

    resolved, resolved_id = await peers.resolve_user(bot, entity if entity
                                                     is not None
                                                     else identifier,
                                                     chat_id=chat_id)
    if resolved is None:
        raise TargetChanged(f"could not resolve user {identifier}")
    return resolved


# ---------------------------------------------------------------------------
#  خواندنِ وضعیت واقعی (سه‌حالته)
# ---------------------------------------------------------------------------
async def read_participant(bot, chat_id, user_id, *, entity=None,
                           attempts=2, backoff=(0.0, 0.5)):
    """شرکت‌کنندهٔ واقعی از سرور یا ``None`` (و «نمی‌دانم» را از «خطا» جدا می‌کند).

    خروجی: ``(participant_or_None, error_or_None)``.

    * ``(obj, None)``    ⇒ خواندن موفق بود.
    * ``(None, error)``  ⇒ خطا (شبکه/مجوز) — یعنی وضعیت **نامعلوم** است.
    """
    _peer, channel = await _require_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id, entity=entity)
    last_error = None
    for index, delay in enumerate(tuple(backoff)[:max(1, attempts)]):
        if delay:
            await asyncio.sleep(delay)
        try:
            # ⚠️ factory (lambda) نه coroutine آماده: هر تلاش باید بتواند
            # درخواستِ تازه بسازد (وگرنه RuntimeError).
            result = await call(
                (lambda: bot.client(functions.channels.GetParticipantRequest(
                    channel=channel, participant=participant))),
                logger=bot.logger,
                timeout=VERIFY_RPC_TIMEOUT,
                max_retry=1,
                what=f"EXCLUSIVE READ chat={chat_id} user={user_id}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            last_error = error
            log_info(bot.logger,
                     "EXCLUSIVE READ FAILED "
                     f"chat_id={chat_id} user_id={user_id} "
                     f"attempt={index + 1} error={error!r}")
            continue
        # خروجیِ GetParticipantRequest یک ``channels.ChannelParticipant`` است
        # که فیلد ``participant`` دارد؛ شکل‌های دیگر هم تحمل می‌شوند.
        return getattr(result, "participant", result), None
    return None, last_error


async def _admin_in_participants_list(bot, chat_id, user_id):
    """تأییدِ دوم: آیا کاربر در فهرست **ادمین‌های** گروه است؟

    خروجی سه‌حالته: ``True`` / ``False`` / ``None`` (نامعلوم).

    ⚠️ اصلاح باگِ واقعی: ``client.get_participants(...)`` در SPlusthon یک
    **``TotalList``** برمی‌گرداند (نه async-iterator)؛ پیمایش با ``async for``
    در نسخهٔ قبلی ``TypeError`` می‌داد و چون خفه می‌شد، همیشه «نامعلوم»
    می‌شد. هم‌چنین به‌جای ``chat_id`` مثبت باید ``InputPeer`` کانال داده شود.
    """
    peer = await _group_peer_fast(bot, chat_id)
    wanted = peers.user_key(user_id)
    kwargs_variants = (
        {"filter": types.ChannelParticipantsAdmins(), "limit": 200},
        {"limit": 200},
        {},
    )
    last_error = None
    participants = None
    for kwargs in kwargs_variants:
        try:
            participants = await call(
                (lambda kw=dict(kwargs): bot.client.get_participants(
                    peer, **kw)),
                logger=bot.logger,
                timeout=VERIFY_RPC_TIMEOUT,
                max_retry=1,
                what=f"EXCLUSIVE LIST ADMINS chat={chat_id}",
            )
            break
        except asyncio.CancelledError:
            raise
        except TypeError as error:                   # فیلترِ ناموجود در این نسخه
            last_error = error
            continue
        except Exception as error:                   # noqa: BLE001
            last_error = error
            break
    if participants is None:
        log_info(bot.logger,
                 "EXCLUSIVE LIST ADMINS FAILED "
                 f"chat_id={chat_id} user_id={user_id} error={last_error!r}")
        return UNKNOWN

    # TotalList ⇒ لیستِ ساده؛ اما هر شکلِ قابلِ پیمایش یا async را هم می‌پذیریم.
    members = participants
    if hasattr(participants, "__aiter__") and not isinstance(
            participants, (list, tuple)):
        try:
            members = [member async for member in participants]
        except Exception as error:                   # noqa: BLE001
            log_info(bot.logger,
                     "EXCLUSIVE LIST ADMINS ITER FAILED "
                     f"chat_id={chat_id} error={error!r}")
            return UNKNOWN
    try:
        rows = list(members)
    except TypeError:
        rows = [participants]

    for member in rows:
        member_id = peers.user_identifier(member)
        if member_id is None or peers.user_key(member_id) != wanted:
            continue
        participant = getattr(member, "participant", None)
        cls = (type(participant).__name__.lower()
               if participant is not None else "")
        if "admin" in cls or "creator" in cls or \
                getattr(member, "is_admin", False) or \
                getattr(member, "is_creator", False):
            return True
        return False
    return False   # در فهرستِ برگشتی نبود ⇒ (در فهرست ادمین‌ها) نیست


def is_admin_participant(participant_obj) -> bool:
    """آیا سرور می‌گوید این کاربر ادمینِ گروه است؟"""
    return type(participant_obj).__name__ in {
        "ChannelParticipantAdmin", "ChannelParticipantCreator"}


def admin_rights_of(participant_obj):
    return getattr(participant_obj, "admin_rights", None)


async def promotion_state(bot, chat_id, user_id, *, entity=None,
                          attempts=3, backoff=VERIFY_BACKOFF):
    """وضعیتِ «ادمین بودنِ» کاربر با سه مقدار: ``True`` / ``False`` / ``None``.

    ترتیب: ``GetParticipant`` → (اگر خطا/نامعلوم) فهرستِ ادمین‌ها → تکرار با
    backoff. **در حالت نامعلوم هیچ تصمیمِ تغییردهنده‌ای گرفته نمی‌شود.**
    """
    delays = tuple(backoff)[:max(1, attempts)]
    for index, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        participant, error = await read_participant(
            bot, chat_id, user_id, entity=entity, attempts=1, backoff=(0.0,))
        if participant is not None:
            if is_admin_participant(participant):
                return VERIFIED
            # ⚠️ ایمنیِ مهم: ``GetParticipant`` در بعضی حالت‌ها (ادمینِ
            # سراسری، دیر رسیدنِ به‌روزرسانی) «ادمین نیست» می‌گوید در حالی
            # که کاربر ادمین است. اگر بر اساسِ همین نتیجه دوباره ارتقا بدهیم،
            # ممکن است ارتقای موجود **عزل** شود. پس «ادمین نیست» فقط وقتی
            # قطعی است که مسیرِ دوم (فهرستِ ادمین‌ها) هم صریحاً بگوید نه.
            listed = await _admin_in_participants_list(bot, chat_id, user_id)
            if listed is UNKNOWN:
                log_info(bot.logger,
                         "EXCLUSIVE PROMOTION UNKNOWN (list failed) "
                         f"chat_id={chat_id} user_id={user_id} "
                         f"attempt={index + 1} read_error={error!r}")
                continue
            if listed:
                return VERIFIED
            # هر دو مسیر می‌گویند ادمین نیست ⇒ همین را برمی‌گردانیم (با
            # اطمینان) ولی تصمیمِ تغییردهنده با لایهٔ بالاست.
            return NOT_VERIFIED
        # خواندنِ اول ناموفق ⇒ مسیر دوم را امتحان کن (ممکن است سالم باشد).
        listed = await _admin_in_participants_list(bot, chat_id, user_id)
        if listed is UNKNOWN:
            log_info(bot.logger,
                     "EXCLUSIVE PROMOTION UNKNOWN (both paths failed) "
                     f"chat_id={chat_id} user_id={user_id} "
                     f"attempt={index + 1} error={error!r}")
            continue
        # اگر مسیرِ دوم می‌گوید «ادمین است» ⇒ قطعاً تأییدشده.
        if listed:
            return VERIFIED
        # «ادمین نیست» از یک مسیرِ تنها برای ادامهٔ کار کافی نیست (ممکن است
        # دسترسیِ ربات به فهرست ناقص باشد) ⇒ باز هم تلاش می‌کنیم.
        log_info(bot.logger,
                 "EXCLUSIVE PROMOTION NOT CONFIRMED (retrying) "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"attempt={index + 1}")
        log_info(bot.logger,
                 "EXCLUSIVE PROMOTION UNKNOWN "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"attempt={index + 1} error={error!r}")
    return UNKNOWN


async def is_promoted(bot, chat_id, user_id, *, entity=None):
    """سازگاری عقب‌رو: ``True`` فقط وقتی سرور قطعاً می‌گوید ادمین است."""
    return (await promotion_state(bot, chat_id, user_id, entity=entity)) \
        is VERIFIED


async def verify_promotion(bot, chat_id, user_id, *, entity=None,
                           wait=None, attempts=None, backoff=None):
    """تأییدِ ارتقا (نامِ قدیمی). خروجی: ``True``/``False``/``None``.

    ``wait`` برای سازگاری نگه داشته شده است: اگر عدد باشد، به‌عنوان فاصلهٔ
    backoffِ دوم استفاده می‌شود (قبلاً ``sleep(0.3)`` بود).
    """
    if backoff is None and wait is not None:
        backoff = (0.0, float(wait), 1.5, 2.5)
    kwargs = {}
    if attempts is not None:
        kwargs["attempts"] = attempts
    if backoff is not None:
        kwargs["backoff"] = backoff
    return await promotion_state(bot, chat_id, user_id, entity=entity, **kwargs)


async def read_participant_rights(bot, chat_id, user_id, *, entity=None):
    """``ChatBannedRights`` واقعیِ کاربر از سرور (یا ``None``)."""
    participant_obj, _error = await read_participant(
        bot, chat_id, user_id, entity=entity)
    return getattr(participant_obj, "banned_rights", None)


async def rights_confirmed(bot, chat_id, user_id, allowed, *, entity=None):
    """آیا API واقعاً همان چیزی را می‌گوید که ما ادعا می‌کنیم؟

    * ``allowed=True`` ⇒ باید ادمین باشد، ارسالش مسدود نباشد و هیچ حقِ حساسی
      نداشته باشد.
    * ``allowed=False`` ⇒ **فقط** «دیگر ادمین نیست». (قبلاً «ارسال باید مسدود
      باشد» هم شرط بود؛ همین باعث می‌شد کاربر بعد از باز شدنِ گروه mute بماند
      و لغو نیمه‌کاره گزارش شود.)

    خروجی: ``True``/``False``/``None`` (نامعلوم).
    """
    participant, error = await read_participant(bot, chat_id, user_id,
                                                entity=entity)
    if participant is None:
        log_info(bot.logger,
                 "EXCLUSIVE RIGHTS CHECK UNKNOWN "
                 f"chat_id={chat_id} user_id={user_id} error={error!r}")
        return UNKNOWN
    admin = is_admin_participant(participant)
    if not allowed:
        return not admin
    banned = text_is_banned(getattr(participant, "banned_rights", None))
    if not admin or banned:
        return NOT_VERIFIED
    return not has_sensitive_right(admin_rights_of(participant))


# ---------------------------------------------------------------------------
#  حقوقِ خودِ ربات در گروه (قبل از هر ارتقا)
# ---------------------------------------------------------------------------
async def bot_admin_rights(bot, chat_id):
    """``ChatAdminRights`` ربات در گروه، یا ``None`` اگر خواندنش ممکن نبود.

    * اگر ``GetParticipantRequest`` برای خودِ ربات کار کند ⇒ همان.
    * وگرنه (اگر ربات ادمین باشد) از ``GetParticipants`` با فیلترِ ادمین‌ها
      استفاده می‌شود.
    * اگر هیچ‌کدام کار نکرد ⇒ ``None`` = «نامعلوم» (نه «بدون حق»).
    """
    peer, channel = await _require_channel(bot, chat_id)
    me = None
    my_id = None
    try:
        me = await bot.client.get_me()
        my_id = peers.user_identifier(me)
    except Exception as error:                       # noqa: BLE001
        log_info(bot.logger,
                 f"EXCLUSIVE BOT SELF READ FAILED chat_id={chat_id} "
                 f"error={error!r}")
    if my_id is None:
        # شناسهٔ ربات از هر منبعِ قابل‌اعتمادِ دیگری (بدون RPC).
        for candidate in (getattr(bot, "bot_account_id", None),
                          getattr(bot.client, "bot_id", None),
                          getattr(getattr(bot, "me", None), "id", None)):
            my_id = peers.user_identifier(candidate)
            if my_id is not None:
                break

    if channel is not None:
        try:
            # ⚠️ خودِ شیءِ ``get_me()`` یک entity معتبر است (access_hash دارد)
            # ⇒ بدون get_entity و بدون RPC اضافه — **فقط اگر** شناسه‌اش
            # خواندنی باشد؛ وگرنه شناسهٔ عددی/InputPeerSelf.
            if me is not None and peers.user_identifier(me) is not None:
                target = me
            elif my_id is not None:
                target = peers.as_input_user(my_id) or my_id
            else:
                target = types.InputPeerSelf()
            result = await call(
                (lambda: bot.client(functions.channels.GetParticipantRequest(
                    channel=channel, participant=target))),
                logger=bot.logger,
                timeout=VERIFY_RPC_TIMEOUT,
                max_retry=1,
                what=f"EXCLUSIVE BOT RIGHTS chat={chat_id}",
            )
            participant = getattr(result, "participant", result)
            if is_admin_participant(participant):
                return admin_rights_of(participant) or types.ChatAdminRights()
            # ربات ادمین نیست ⇒ حقِ افزودن ادمین هم ندارد.
            return types.ChatAdminRights(**{flag: False
                                            for flag in ALL_ADMIN_FLAGS})
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            log_info(bot.logger,
                     "EXCLUSIVE BOT RIGHTS READ FAILED (trying list) "
                     f"chat_id={chat_id} error={error!r}")

    if my_id is not None:
        try:
            listed = await call(
                (lambda: bot.client.get_participants(
                    peer, filter=types.ChannelParticipantsAdmins(),
                    limit=200)),
                logger=bot.logger,
                timeout=VERIFY_RPC_TIMEOUT,
                max_retry=1,
                what=f"EXCLUSIVE BOT RIGHTS LIST chat={chat_id}",
            )
            rows = list(listed) if not hasattr(listed, "__aiter__") else [
                member async for member in listed]
            wanted = peers.user_key(my_id)
            for member in rows:
                if peers.user_key(member) != wanted:
                    continue
                participant = getattr(member, "participant", None)
                rights = admin_rights_of(participant)
                if rights is not None:
                    return rights
                return types.ChatAdminRights()   # ادمین است، جزئیات نامعلوم
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            log_info(bot.logger,
                     "EXCLUSIVE BOT RIGHTS LIST FAILED "
                     f"chat_id={chat_id} error={error!r}")
    return None


async def ensure_bot_can_promote(bot, chat_id):
    """قبل از ارتقا مطمئن شو ربات **حقِ افزودن ادمین** دارد.

    * ``True``  ⇒ مطمئنیم حق دارد.
    * ``False`` ⇒ مطمئنیم حق **ندارد** ⇒ ``BotLacksRights`` بالا می‌رود.
    * ``None``  ⇒ نامعلوم ⇒ جلوی کار گرفته **نمی‌شود** (فقط لاگ)، چون ممکن
      است خطای خواندن موقتی باشد و رد کردنِ دستور بدتر از تلاشِ بی‌خطر است.
    """
    rights = await bot_admin_rights(bot, chat_id)
    if rights is None:
        log_info(bot.logger,
                 f"EXCLUSIVE BOT RIGHTS UNKNOWN chat_id={chat_id} "
                 "(proceeding)")
        return None
    can_add = bool(getattr(rights, "add_admins", False))
    log_info(bot.logger,
             f"EXCLUSIVE BOT RIGHTS chat_id={chat_id} add_admins={can_add}")
    if not can_add:
        raise BotLacksRights(
            f"bot cannot add admins in {chat_id} (add_admins={can_add})")
    return True


# ---------------------------------------------------------------------------
#  ارتقا
# ---------------------------------------------------------------------------
async def _edit_admin(bot, channel, participant, chat_id, user_id):
    """یک ``EditAdminRequest`` با مهلتِ کوتاه و پریستِ ``edit_only``."""
    return await asyncio.wait_for(
        bot.client(functions.channels.EditAdminRequest(
            channel=channel, user_id=participant,
            admin_rights=admin_rights_for(PRIMARY_PRESET), rank="")),
        timeout=GRANT_RPC_TIMEOUT)


async def grant_participant_rights(bot, chat_id, user_id, *, entity=None):
    """کاربر را ادمینِ گروه می‌کند (فقط حقِ ارسال) و نامِ پریست را برمی‌گرداند.

    «موفق» یعنی RPC **بدون خطا** برگشت. هیچ پریستِ جایگزینی (به‌ویژه
    ``none`` که روی سیم = عزل است) امتحان نمی‌شود؛ اگر سرور رد کند
    ``GrantRejected`` بالا می‌رود و رکورد برای بازیابی می‌ماند.
    """
    _peer, channel = await _require_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id, entity=entity)

    attempt = 0
    last_error = None
    while attempt < 2:
        attempt += 1
        try:
            await _edit_admin(bot, channel, participant, chat_id, user_id)
            log_info(bot.logger,
                     f"EXCLUSIVE GRANT RPC OK chat_id={chat_id} "
                     f"user_id={user_id} preset={PRIMARY_PRESET}")
            return PRIMARY_PRESET
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            last_error = error
            seconds = flood_seconds(error)
            if seconds is not None and seconds <= 2 and attempt < 2:
                await asyncio.sleep(seconds)
                continue
            if is_transient(error) and attempt < 2:
                await asyncio.sleep(0.2)
                continue
            log_info(bot.logger,
                     f"EXCLUSIVE GRANT RPC FAILED chat_id={chat_id} "
                     f"user_id={user_id} preset={PRIMARY_PRESET} "
                     f"error={error!r}")
            break
    raise GrantRejected(
        f"server refused admin promotion for {user_id}: {last_error!r}")


async def revoke_participant_rights(bot, chat_id, user_id, *, entity=None):
    """لغو کامل: demote + پاک کردنِ محدودیتِ شخصیِ احتمالی.

    ⚠️ اصلاح باگِ «بعد از باز شدن گروه کاربر mute می‌ماند»:
    قبلاً بعد از demote یک ``EditBannedRequest`` با
    ``send_messages=True/send_plain=True`` فرستاده می‌شد؛ یعنی یک **محدودیتِ
    شخصی دائمی** که با باز شدنِ گروه هم پاک نمی‌شد. حالا:

    * اگر کاربر از قبل محدودیتِ شخصی داشت ⇒ با ``banned_rights`` **همه False**
      پاک می‌شود (BACK TO NORMAL).
    * اگر محدودیتِ شخصی ندارد ⇒ هیچ ``EditBanned`` زده نمی‌شود.
    """
    _peer, channel = await _require_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id, entity=entity)

    await call(
        (lambda: bot.client(functions.channels.EditAdminRequest(
            channel=channel, user_id=participant,
            # برای demote عمداً ``ChatAdminRights()`` (همه None) فرستاده
            # می‌شود: روی سیم دقیقاً همان «عزل» است.
            admin_rights=types.ChatAdminRights(), rank=""))),
        logger=bot.logger,
        timeout=GRANT_RPC_TIMEOUT,
        what=f"EXCLUSIVE DEMOTE chat={chat_id} user={user_id}",
    )
    log_info(bot.logger,
             f"EXCLUSIVE DEMOTE RPC OK chat_id={chat_id} user_id={user_id}")

    # محدودیتِ شخصیِ قبلی (اگر بود) پاک شود تا کاربر عضوِ عادیِ سالم باشد.
    banned_rights = await read_participant_rights(bot, chat_id, user_id,
                                                  entity=entity)
    if _has_personal_restriction(banned_rights):
        await call(
            (lambda: bot.client(functions.channels.EditBannedRequest(
                channel=channel, participant=participant,
                banned_rights=clone_banned_rights(None)))),
            logger=bot.logger,
            timeout=GRANT_RPC_TIMEOUT,
            what=f"EXCLUSIVE CLEAR RESTRICTION chat={chat_id} user={user_id}",
        )
        log_info(bot.logger,
                 "EXCLUSIVE PERSONAL RESTRICTION CLEARED "
                 f"chat_id={chat_id} user_id={user_id}")
    else:
        log_info(bot.logger,
                 "EXCLUSIVE NO PERSONAL RESTRICTION (skipping EditBanned) "
                 f"chat_id={chat_id} user_id={user_id}")
    return True


def _has_personal_restriction(banned_rights) -> bool:
    """آیا کاربر محدودیتِ شخصی (غیر از پیش‌فرضِ گروه) دارد؟"""
    if banned_rights is None:
        return False
    try:
        flags = inspect.signature(types.ChatBannedRights.__init__).parameters
    except (TypeError, ValueError):                   # pragma: no cover
        flags = ("send_messages", "send_plain", "send_media")
    for flag in flags:
        if flag in {"self", "until_date"}:
            continue
        if bool(getattr(banned_rights, flag, False)):
            return True
    return False


# ---------------------------------------------------------------------------
#  فهرست «اختصاصی» — ذخیره‌سازی
# ---------------------------------------------------------------------------
def _group_record(data, chat_id, create=False):
    groups = data.setdefault("groups", {})
    key = storage.group_key(chat_id)
    if create:
        return groups.setdefault(key, {"users": {}, "pending": []})
    return groups.get(key) or {"users": {}, "pending": []}


def list_entries(chat_id, *, states=("active",) + RECOVERABLE):
    """رکوردهای این گروه که در یکی از ``states`` هستند (پیش‌فرض: فعال).

    منبعِ حقیقت ``state`` است؛ کلید ``active`` تنها برای خواناییِ فایل نگه داشته
    می‌شود و هرگز جای ``state`` را نمی‌گیرد. رکوردهای نیمه‌کاره
    (``pending_grant``/``pending_revoke``) هم برگردانده می‌شوند تا «لغو»
    هرگز به بهانهٔ «در فهرست نیست» رها نشود.
    """
    data = storage.EXCLUSIVE_STORAGE.load()
    record = _group_record(data, chat_id)
    result = []
    for entry in (record.get("users") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("state") in states:
            result.append(dict(entry))
    return result


def is_exclusive(chat_id, user_id) -> bool:
    entry = get_entry(chat_id, user_id)
    return bool(entry) and entry.get("state") == "active"


def get_entry(chat_id, user_id):
    data = storage.EXCLUSIVE_STORAGE.load()
    record = _group_record(data, chat_id)
    return (record.get("users") or {}).get(peers.user_key(user_id))


def _save_entry(chat_id, entry):
    entry = dict(entry)

    def mutate(data):
        record = _group_record(data, chat_id, create=True)
        users = record.setdefault("users", {})
        users[peers.user_key(entry.get("user_id"))] = entry
        return entry

    return storage.EXCLUSIVE_STORAGE.update(mutate)


def _set_state(chat_id, entry, state, extra=None):
    entry = dict(entry)
    entry["state"] = state
    entry["updated_at"] = storage.now()
    if extra:
        entry.update(extra)
    return _save_entry(chat_id, entry)


# ---------------------------------------------------------------------------
#  عملیات «اختصاصی» / «لغو اختصاصی»
# ---------------------------------------------------------------------------
async def grant(bot, chat_id, user_id, *, actor_id=None, name="", username="",
                entity=None, verify=False):
    """ثبت کاربر در فهرست اختصاصی + ارتقا به ادمین (بی‌درنگ).

    ترتیب:

    ۱) ``is_group_locked`` (کش‌شده؛ فقط یک خواندن در هر دوره)
    ۲) اگر رکورد ``active`` هست ⇒ **وضعیت واقعی از سرور** خوانده می‌شود:
       اگر واقعاً ادمین است ⇒ ``AlreadyExclusive``؛ وگرنه ادامه می‌دهیم و
       دوباره ارتقا می‌دهیم (باگِ «یک‌بار ادمین شد، بعد خطا داد»).
    ۳) حقوقِ خودِ ربات بررسی می‌شود (``add_admins``).
    ۴) رکورد ``pending_grant`` + یک ``EditAdminRequest`` با مهلتِ کوتاه.
    ۵) رکورد ``active`` ⇒ پاسخِ کاربر همین‌جا آزاد می‌شود.
    ۶) تأییدِ سه‌حالته در پس‌زمینه (``complete_verification``).

    ``verify=False`` پیش‌فرض است: لایهٔ دستور خودش تأیید را در پس‌زمینه اجرا
    می‌کند. اگر ``True`` باشد همین‌جا (بعد از رکورد) تأیید هم انجام می‌شود.
    """
    if not await is_group_locked(bot, chat_id):
        raise NotLocked(f"group {chat_id} is not locked")

    identifier = peers.user_identifier(entity) if entity is not None \
        else peers.user_identifier(user_id)
    if identifier is None:
        raise TargetChanged(f"invalid user identity: {user_id!r}")

    existing = get_entry(chat_id, identifier)
    if existing and existing.get("state") == "active":
        state = await promotion_state(bot, chat_id, identifier, entity=entity,
                                      attempts=2, backoff=(0.0, 0.5))
        if state is VERIFIED:
            raise AlreadyExclusive(f"user {identifier} already exclusive")
        if state is UNKNOWN:
            # نمی‌دانیم ادمین است یا نه ⇒ هیچ تغییری نمی‌دهیم (ضدِ عزلِ تصادفی).
            log_info(bot.logger,
                     "EXCLUSIVE GRANT SKIPPED (state unknown) "
                     f"chat_id={chat_id} user_id={identifier}")
            raise AlreadyExclusive(
                f"user {identifier} state unknown; not touching it")
        # قطعاً ادمین نیست ⇒ رکورد کهنه است؛ دوباره ارتقا می‌دهیم.
        log_info(bot.logger,
                 "EXCLUSIVE RE-GRANT (record active but server says not admin) "
                 f"chat_id={chat_id} user_id={identifier}")

    # ۳) حقوقِ ربات
    await ensure_bot_can_promote(bot, chat_id)

    saved = _save_entry(chat_id, {
        "user_id": storage.user_key(identifier),
        "chat_id": storage.group_key(chat_id),
        "name": name or "",
        "username": (username or "").lstrip("@"),
        "actor_id": storage.user_key(actor_id) if actor_id else None,
        "created_at": storage.now(),
        "created_at_tehran": storage.iso_tehran(),
        "updated_at": storage.now(),
        "state": PENDING_GRANT_FLAG,
        "granted": False,
        "confirmed": False,
        "last_error": "",
    })

    try:
        preset = await grant_participant_rights(bot, chat_id, identifier,
                                                entity=entity)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, saved, PENDING_GRANT_FLAG, {
            "granted": False, "confirmed": False, "last_error": repr(error)})
        raise

    saved = _set_state(chat_id, saved, "active",
                       {"granted": True,
                        # «بدون خطا» است، ولی «تأیید سرور» هنوز نه.
                        "confirmed": False,
                        "verified": "pending",
                        "admin_preset": preset,
                        "granted_at": storage.now()})
    log_info(bot.logger,
             f"EXCLUSIVE GRANTED chat_id={chat_id} user_id={identifier} "
             f"preset={preset}")
    if verify:
        try:
            asyncio.ensure_future(_verify_in_background(bot, chat_id,
                                                        identifier, saved))
        except RuntimeError:
            pass
    return saved


async def complete_verification(bot, chat_id, user_id, entry=None, *,
                                entity=None):
    """تأییدِ سه‌حالته + (در صورت لزوم) تلاش دوباره.

    * ``VERIFIED``     ⇒ رکورد ``active``/``confirmed=True``.
    * ``NOT_VERIFIED`` ⇒ **یک بار** تلاش دوبارهٔ ارتقا (اگر ربات حق دارد)، سپس
      تأیید دوباره؛ اگر باز هم نه ⇒ ``pending_grant`` برای بازیابی.
    * ``UNKNOWN``      ⇒ **هیچ RPC تغییردهنده‌ای**؛ رکورد در ``pending_grant``
      می‌ماند تا ریکاوری/نگهبان بعداً بررسی کند.
    """
    entry = entry or get_entry(chat_id, user_id) or {}
    identifier = peers.user_identifier(user_id) or user_id
    state = await promotion_state(bot, chat_id, identifier, entity=entity)

    if state is VERIFIED:
        current = get_entry(chat_id, identifier) or entry
        _set_state(chat_id, current, "active",
                   {"granted": True, "confirmed": True, "verified": "yes",
                    "verified_at": storage.now()})
        log_info(bot.logger,
                 f"EXCLUSIVE VERIFIED chat_id={chat_id} user_id={identifier}")
        return get_entry(chat_id, identifier)

    if state is UNKNOWN:
        current = get_entry(chat_id, identifier) or entry
        _set_state(chat_id, current, PENDING_GRANT_FLAG, {
            "granted": True, "confirmed": False, "verified": "unknown",
            "last_error": "promotion state unknown (read failed/late)",
            "unverified_at": storage.now()})
        log_info(bot.logger,
                 "EXCLUSIVE VERIFY UNKNOWN (no change RPC; kept pending) "
                 f"chat_id={chat_id} user_id={identifier}")
        return get_entry(chat_id, identifier)

    # ---- NOT_VERIFIED: حداکثر یک تلاش دوباره، و فقط اگر ربات حق دارد ----
    try:
        await ensure_bot_can_promote(bot, chat_id)
        await grant_participant_rights(bot, chat_id, identifier, entity=entity)
    except BotLacksRights as error:
        log_info(bot.logger,
                 "EXCLUSIVE RETRY SKIPPED (bot lacks rights) "
                 f"chat_id={chat_id} user_id={identifier} error={error!r}")
    except Exception as error:                       # noqa: BLE001
        log_info(bot.logger,
                 "EXCLUSIVE RETRY GRANT FAILED "
                 f"chat_id={chat_id} user_id={identifier} error={error!r}")
    else:
        state = await promotion_state(bot, chat_id, identifier, entity=entity)
        if state is VERIFIED:
            current = get_entry(chat_id, identifier) or entry
            _set_state(chat_id, current, "active",
                       {"granted": True, "confirmed": True, "verified": "yes",
                        "verified_at": storage.now()})
            log_info(bot.logger,
                     "EXCLUSIVE VERIFIED AFTER RETRY "
                     f"chat_id={chat_id} user_id={identifier}")
            return get_entry(chat_id, identifier)

    current = get_entry(chat_id, identifier) or entry
    _set_state(chat_id, current, PENDING_GRANT_FLAG, {
        "granted": True, "confirmed": False, "verified": "no",
        "last_error": "server does not report the user as admin",
        "unverified_at": storage.now()})
    log_info(bot.logger,
             "EXCLUSIVE NOT VERIFIED (kept pending) "
             f"chat_id={chat_id} user_id={identifier}")
    return get_entry(chat_id, identifier)


async def _verify_in_background(bot, chat_id, user_id, saved):
    """تأییدِ آرام در پس‌زمینه — هرگز پاسخ کاربر را معطل نمی‌کند."""
    try:
        return await complete_verification(bot, chat_id, user_id, saved)
    except Exception as error:                       # noqa: BLE001
        log_info(bot.logger,
                 f"EXCLUSIVE BACKGROUND VERIFY FAILED chat_id={chat_id} "
                 f"user_id={user_id} error={error!r}")
        return None


async def revoke(bot, chat_id, user_id, *, reason="manual", entity=None):
    """لغو مجوز + غیرفعال کردن رکورد. اجرای چندباره ممنوع.

    اگر رکوردی در حافظه/دیسک نباشد ولی سرور بگوید این کاربر **ادمین** است،
    باز هم demote انجام می‌شود: کاربر نباید به‌خاطر از دست رفتنِ یک فایل،
    ادمینِ گروه بماند. اگر وضعیت **نامعلوم** باشد، هیچ تغییری انجام نمی‌شود.
    """
    identifier = peers.user_identifier(entity) if entity is not None \
        else peers.user_identifier(user_id)
    if identifier is None:
        raise TargetChanged(f"invalid user identity: {user_id!r}")

    entry = get_entry(chat_id, identifier)
    if not entry:
        state = await promotion_state(bot, chat_id, identifier, entity=entity,
                                      attempts=2, backoff=(0.0, 0.5))
        if state is UNKNOWN:
            raise TargetChanged(
                f"cannot read state of user {identifier}; nothing changed")
        if state is NOT_VERIFIED:
            raise NotExclusive(f"user {identifier} is not exclusive")
        # رکوردِ حداقلی می‌سازیم تا تاریخچه و وضعیت پایدار بماند.
        entry = _save_entry(chat_id, {
            "user_id": storage.user_key(identifier),
            "chat_id": storage.group_key(chat_id),
            "name": "",
            "username": "",
            "actor_id": None,
            "created_at": storage.now(),
            "created_at_tehran": storage.iso_tehran(),
            "updated_at": storage.now(),
            "state": PENDING_GRANT_FLAG,
            "granted": True,
            "confirmed": False,
            "last_error": "revoked without local record",
            "adopted_from_server": True,
        })
        log_info(bot.logger,
                 f"EXCLUSIVE ADOPTED FROM SERVER chat_id={chat_id} "
                 f"user_id={identifier} (was admin without record)")
    if entry.get("state") == "revoked":
        raise NotExclusive(f"user {identifier} already revoked")

    try:
        await revoke_participant_rights(bot, chat_id, identifier,
                                        entity=entity)
    except Exception as error:                       # noqa: BLE001
        # قطعی/خطای گذرا: رکورد در «در انتظار لغو» می‌ماند تا ریکاوریِ
        # استارتاپ دوباره تلاش کند؛ پرونده هرگز گم نمی‌شود.
        _set_state(chat_id, entry, PENDING_FLAG, {"last_error": repr(error)})
        raise

    confirmed = await rights_confirmed(bot, chat_id, identifier, False,
                                       entity=entity)
    if confirmed is UNKNOWN:
        # نتیجه نامعلوم ⇒ رکورد در «در انتظار لغو» می‌ماند (نباید ادعای
        # موفقیت کنیم)، ولی demote انجام شده است.
        entry = _set_state(chat_id, entry, PENDING_FLAG,
                           {"last_error": "revoke state unknown",
                            "revoked_at": storage.now(),
                            "revoke_reason": reason})
        log_info(bot.logger,
                 "EXCLUSIVE REVOKE UNCONFIRMED (kept for recovery) "
                 f"chat_id={chat_id} user_id={identifier}")
        return entry

    entry = _set_state(chat_id, entry,
                       "revoked" if confirmed else PENDING_FLAG,
                       {"confirmed": bool(confirmed),
                        "revoked_at": storage.now(),
                        "revoked_at_tehran": storage.iso_tehran(),
                        "revoke_reason": reason})
    log_info(bot.logger,
             f"EXCLUSIVE REVOKED chat_id={chat_id} user_id={identifier} "
             f"reason={reason} confirmed={confirmed}")
    return entry


async def revoke_all(bot, chat_id, *, reason="group_unlocked", logger=None):
    """لغو همهٔ مجوزهای موقتِ این گروه (وقتی گروه باز شد).

    خروجی: ``(revoked, failed_entries)``. خطای هر کاربر بقیه را متوقف نمی‌کند و
    در فهرستِ بازیابی می‌نشیند. هیچ کاربری mute نمی‌شود (بعد از باز شدنِ گروه
    باید بتواند بنویسد).
    """
    revoked, failed = [], []
    for entry in list_entries(chat_id):
        try:
            await revoke(bot, chat_id, entry["user_id"], reason=reason)
            revoked.append(entry["user_id"])
        except NotExclusive:
            revoked.append(entry["user_id"])
        except Exception as error:                   # noqa: BLE001
            try:
                _set_state(chat_id, entry, PENDING_FLAG,
                           {"last_error": repr(error)})
            except Exception:                        # noqa: BLE001
                pass
            failed.append(dict(entry, last_error=repr(error)))
            if logger is not None:
                try:
                    logger.log_error(
                        "EXCLUSIVE REVOKE ALL FAILED "
                        f"chat_id={chat_id} user_id={entry.get('user_id')} "
                        f"error={error!r}")
                except Exception:                    # noqa: BLE001
                    pass
    return revoked, failed


def pending_jobs(chat_id=None):
    """کارهای در انتظارِ بازیابی (برای استارتاپ/صف اختصاصی)."""
    data = storage.EXCLUSIVE_STORAGE.load()
    jobs = []
    groups = data.get("groups", {})
    keys = [storage.group_key(chat_id)] if chat_id else list(groups.keys())
    for key in keys:
        record = groups.get(key) or {}
        for entry in record.get("users", {}).values():
            if entry.get("state") in RECOVERABLE:
                jobs.append(dict(entry, group_key=key))
    return jobs


def forget_group(chat_id):
    """پاک‌سازی رکوردهای یک گروه (تست/ری‌ست)."""

    def mutate(data):
        data.setdefault("groups", {}).pop(storage.group_key(chat_id), None)

    storage.EXCLUSIVE_STORAGE.update(mutate)
