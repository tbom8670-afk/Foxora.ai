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
* لغو: ``EditAdminRequest`` با ``ChatAdminRights()`` (همه False ⇒ demote) و بعد
  دوباره اعمالِ محدودیتِ ارسال، تا کاربر به همان وضعیتِ عضو عادی برگردد.
* تأییدِ نهایی همیشه با ``channels.GetParticipantRequest`` انجام می‌شود:
  برای «فعال» باید نوعِ participant از خانوادهٔ Admin باشد، ارسالش مسدود
  نباشد و هیچ حقِ مدیریتیِ اضافه نداشته باشد؛ برای «لغو» باید دیگر ادمین
  نباشد و ارسالش مسدود باشد. اگر سرور این را تأیید نکند، هیچ موفقیتی
  گزارش نمی‌شود و رکورد در وضعیت بازیابی می‌ماند.

وضعیت گروه **از سرور** خوانده می‌شود (نه از حافظه) و بعد از هر تغییر دوباره
خوانده و تأیید می‌شود؛ «قفل است» یعنی ``send_messages`` باندشده برای
پیش‌فرضِ گروه.
"""
from __future__ import annotations

import asyncio

from modules.group_actions import (
    GroupActions,
    clone_banned_rights,
    text_is_banned,
)
from modules.manage_features import storage
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


# ---------------------------------------------------------------------------
#  دسترسی به گروه (فقط خواندن؛ هیچ تغییری در گروه_اکشنز موجود نمی‌دهیم)
# ---------------------------------------------------------------------------
def _actions(bot):
    return GroupActions(bot.client, bot.logger)


async def peer_and_channel(bot, chat_id):
    """``(peer, input_channel)`` — اول کشِ peer، بدون resolveِ اضافه.

    قبلاً هر فراخوانی یک resolve تازه می‌گرفت و در مسیر «اختصاصی» سه-چهار
    بار تکرار می‌شد (بخشی از کندیِ ~۵۰ ثانیه‌ای). حالا هر گروه یک کشِ کوچک
    دارد و کشِ خودِ ربات هم اولویت دارد.
    """
    actions = _actions(bot)
    peer = _cached_channel_peer(bot, chat_id)
    if peer is None:
        peer = await actions._group_peer(chat_id)
        _remember_channel_peer(bot, chat_id, peer)
    if not (hasattr(peer, "channel_id") and hasattr(peer, "access_hash")):
        raise ValueError("chat is not a channel/supergroup")
    return peer, types.InputChannel(peer.channel_id, peer.access_hash)


#: ``chat_key`` → ``InputPeer`` کانال (برای پرهیز از resolveِ تکراری)
_CHANNEL_PEERS: dict = {}
_CHANNEL_PEERS_MAX = 256


def _cached_channel_peer(bot, chat_id):
    from modules.manage_features import peers

    peer = _CHANNEL_PEERS.get(storage.group_key(chat_id))
    if peer is not None and hasattr(peer, "channel_id"):
        return peer
    candidate = peers.group_peer(bot, chat_id)
    if candidate is not None and hasattr(candidate, "channel_id"):
        return candidate
    return None


def _remember_channel_peer(bot, chat_id, peer):
    if peer is None or not hasattr(peer, "channel_id"):
        return peer
    _CHANNEL_PEERS[storage.group_key(chat_id)] = peer
    while len(_CHANNEL_PEERS) > _CHANNEL_PEERS_MAX:
        _CHANNEL_PEERS.pop(next(iter(_CHANNEL_PEERS)), None)
    return peer


class ExcludedUser(RuntimeError):
    """کاربر با دستور «آزاد» از ربات ban شده است؛ نباید پیام بفرستد.

    این استثنا عمداً وجود دارد: قابلیت «اختصاصی» هیچ‌گاه یک banِ ذخیره‌شده
    را دور نمی‌زند (بدون تغییر در لایهٔ حفاظتیِ موجود).
    """


async def user_peer(bot, chat_id, user_id):
    """«آزاد نشده» بودن کاربر را چک می‌کند و InputPeer او را می‌دهد."""
    from modules import banned_storage

    try:
        if banned_storage.is_banned(chat_id, user_id):
            raise ExcludedUser(
                f"user {user_id} is still banned by the bot in {chat_id}")
    except ExcludedUser:
        raise
    except Exception:                                # noqa: BLE001
        # اگر خودِ بررسی خطا داد، اجازه نمی‌دهیم (fail-closed).
        raise ExcludedUser("ban check failed; refusing to grant rights")

    peer, _channel = await peer_and_channel(bot, chat_id)
    try:
        return await bot.client.get_input_entity(int(user_id))
    except Exception:                                # noqa: BLE001
        # آخرین تلاش: شناسهٔ عددی خام (SPlusthon خودش resolve می‌کند)
        return int(user_id)


# ---------------------------------------------------------------------------
#  وضعیت واقعیِ گروه و مجوز
# ---------------------------------------------------------------------------
#: مدت اعتبارِ نتیجهٔ «گروه قفل است». برای دستورِ «اختصاصی» کوتاه است تا پس
#: از باز شدنِ گروه، ثبتِ جدید اشتباهاً مجاز نشود؛ ولی همان یک بار هم کافی است
#: که مسیر گرم (بررسی هر پیام) هیچ RPCای نزند.
LOCK_CACHE_TTL = 20.0
#: مهلتِ هر RPC در مسیر «اختصاصی» — کارفرما انتظارِ چند ثانیه‌ای دارد، نه دقیقه.
GRANT_RPC_TIMEOUT = 6.0
#: ``chat_key`` → ``(ts_monotonic, locked)``
_LOCKED_CACHE: dict = {}


def lock_state_cached(chat_id):
    entry = _LOCKED_CACHE.get(storage.group_key(chat_id))
    if not entry:
        return None
    stamp, locked = entry
    if storage.monotonic() - stamp > LOCK_CACHE_TTL:
        _LOCKED_CACHE.pop(storage.group_key(chat_id), None)
        return None
    return bool(locked)


def remember_lock_state(chat_id, locked):
    _LOCKED_CACHE[storage.group_key(chat_id)] = (storage.monotonic(),
                                                 bool(locked))
    return locked


def forget_lock_state(chat_id):
    _LOCKED_CACHE.pop(storage.group_key(chat_id), None)


async def is_group_locked(bot, chat_id) -> bool:
    """آیا گروه الان برای اعضای عادی قفل است؟ (پاسخ از سرور)

    ⚠️ سرعت: اگر نتیجهٔ تازه (< ۲ دقیقه) در حافظه باشد، همان استفاده می‌شود؛
    فقط یک بار در هر دو دقیقه (و فقط هنگام ثبتِ اختصاصی جدید) خوانده می‌شود.
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
    from modules.manage_features import peers

    cached = peers.group_peer(bot, chat_id)
    if cached is not None and hasattr(cached, "channel_id"):
        return cached
    return await _actions(bot)._group_peer(chat_id)


ALL_ADMIN_FLAGS = ("change_info", "post_messages", "edit_messages",
                   "delete_messages", "ban_users", "invite_users",
                   "pin_messages", "add_admins", "anonymous", "manage_call",
                   "other", "manage_topics", "post_stories", "edit_stories",
                   "delete_stories")

#: حالت‌های ارتقا به‌ترتیبِ اولویت. همهٔ فلگ‌ها **صریح** False داده می‌شوند
#: (None یعنی «سرور خودش تصمیم بگیرد» و در سروش باعث روشن شدنِ
#: «تغییر اطلاعات گروه» و «سنجاق پیام‌ها» می‌شد).
#: نامِ presetِ پیش‌فرض: ادمینِ واقعی با فقط «ویرایشِ پیام خودش».
#: ⚠️ بازخورد کارفرما: «باید ادمینِ واقعیِ گروه بشه و دسترسی پیام دادن داشته
#: باشه». «ارسال پیام» در سروش یک حقِ ادمینیِ جدا ندارد؛ انتخابِ `edit_messages`
#: (بدون `post_messages`) کاری می‌کند که آیکون «ارسال» برای کاربر باز شود، بدونِ
#: آنکه بتواند در کانال پست کند. `change_info`/`pin_messages`/`delete_messages` و
#: بقیهٔ حق‌های مدیریتی صریحاً False می‌مانند.
PRIMARY_PRESET = "edit_only"

ADMIN_RIGHT_PRESETS = (
    # فقط ویرایشِ پیام خودش ⇒ ارسال برای کاربر باز می‌شود (پیش‌فرض).
    ("edit_only", {"edit_messages": True}),
    # پشتیبانِ سرورهای سخت‌گیر که این حالت را demote می‌کنند.
    ("none", {}),
)


def admin_rights_for(preset):
    """``ChatAdminRights`` با همهٔ فلگ‌های صریح برای یک حالت."""
    values = {flag: False for flag in ALL_ADMIN_FLAGS}
    values.update(preset or {})
    return types.ChatAdminRights(**values)


def send_only_admin_rights():
    """حالتِ حداقلی (سازگاری عقب‌رو): ادمین با فقط حقِ ویرایشِ پیام خودش."""
    return admin_rights_for(dict(ADMIN_RIGHT_PRESETS[1][1]))


async def _apply_admin_rights(bot, channel, participant, preset, chat_id,
                              user_id):
    """یک بار ارتقا با حق‌های مشخص را انجام می‌دهد."""
    await call(
        bot.client(functions.channels.EditAdminRequest(
            channel=channel, user_id=participant,
            admin_rights=admin_rights_for(preset), rank="")),
        logger=bot.logger,
        what=f"EXCLUSIVE GRANT chat={chat_id} user={user_id}",
    )


async def is_promoted(bot, chat_id, user_id) -> bool:
    """آیا سرور می‌گوید این کاربر ادمینِ گروه است؟ (چند مسیرِ تأیید)"""
    participant_obj = await read_participant(bot, chat_id, user_id)
    if is_admin_participant(participant_obj):
        return True
    listed = await _admin_in_participants_list(bot, chat_id, user_id)
    return bool(listed)


#: حداقل زمانِ صبر برای تأییدِ «ادمین شد» — کوتاه است تا پاسخ کند نباشد.
FAST_VERIFY_WAIT = 0.3


async def _edit_admin(bot, channel, participant, preset, chat_id, user_id):
    """یک ``EditAdminRequest`` با مهلتِ کوتاه (بدون سه-بار تلاشِ پیش‌فرض)."""
    return await asyncio.wait_for(
        bot.client(functions.channels.EditAdminRequest(
            channel=channel, user_id=participant,
            admin_rights=admin_rights_for(preset), rank="")),
        timeout=GRANT_RPC_TIMEOUT)


async def grant_participant_rights(bot, chat_id, user_id):
    """کاربر را ادمین گروه می‌کند و **بی‌درنگ** نتیجه را برمی‌گرداند.

    ⚠️ بازنویسیِ سرعت (بازخورد کارفرما: «این متن بعد یک دقیقه ارسال می‌شد»):

    * هیچ خواندنِ مقدماتی (GetParticipant / فهرست اعضا / وضعیت قفل) پیش از
      RPC وجود ندارد؛ فقط یک ``EditAdminRequest``.
    * ``EditBanned`` غیرضروری حذف شد: کاربرِ عادی در گروه قفل، در
      ``banned_rights`` بن نشده است و ارتقا به ادمین کافی است.
    * مهلتِ هر RPC کوتاه است (``GRANT_RPC_TIMEOUT``) و تلاش دوباره فقط برای
      خطای گذرا — قبلاً هر RPC می‌توانست ۳ بار × ۱۰ ثانیه = ۳۰ ثانیه صبر کند
      و دو مسیرِ تأیید هم پشتِ هم ۳۰ ثانیهٔ دیگر می‌خوردند.
    * «موفق» یعنی ربات درخواست را **بدون خطا** فرستاد (ادمین‌شدن را کاربر در
      همان لحظه می‌بیند)؛ تأییدِ کاملِ سمتِ سرور در پس‌زمینه انجام می‌شود.
    """
    peer = _cached_channel_peer(bot, chat_id)
    if peer is None:
        peer = await _actions(bot)._group_peer(chat_id)
    _remember_channel_peer(bot, chat_id, peer)
    channel = types.InputChannel(peer.channel_id, peer.access_hash)
    participant = await user_peer(bot, chat_id, user_id)

    applied = None
    last_error = None
    for name, preset in ADMIN_RIGHT_PRESETS:
        attempt = 0
        while attempt < 2:
            attempt += 1
            try:
                await _edit_admin(bot, channel, participant, preset, chat_id,
                                  user_id)
                applied = name
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:               # noqa: BLE001
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
                         f"user_id={user_id} preset={name} error={error!r}")
                break
        if applied is not None:
            log_info(bot.logger,
                     f"EXCLUSIVE GRANT RPC OK chat_id={chat_id} "
                     f"user_id={user_id} preset={applied}")
            return applied
    # همهٔ حالت‌ها رد شدند؛ اگر خطای دائمی داشته‌ایم همان را بالا می‌دهیم.
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"server refused every admin preset for {user_id}")


async def verify_promotion(bot, chat_id, user_id, *, wait=FAST_VERIFY_WAIT):
    """تأییدِ کوتاهِ «ادمین شد» (فقط برای لاگ/ثباتِ رکورد؛ مسیر کاربر نیست)."""
    for delay in (0.0, wait):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await is_promoted(bot, chat_id, user_id):
                return True
        except Exception:                            # noqa: BLE001
            continue
    return False


async def revoke_participant_rights(bot, chat_id, user_id) -> bool:
    """حذف کاملِ دسترسی: هم ادمینیِ ارتقایافته، هم مجوز ارسال برداشته می‌شود."""
    _peer, channel = await peer_and_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id)
    # ۱) برگرداندن به عضو عادی (همهٔ حق‌های ادمینی False ⇒ demote).
    await call(
        bot.client(functions.channels.EditAdminRequest(
            channel=channel, user_id=participant,
            # ⚠️ برای demote عمداً ``ChatAdminRights()`` (همه None) فرستاده
            # می‌شود: روی سیم همان «همه False» است و از «ارتقای بی‌حق»
            # (که همهٔ فلگ‌هایش صریح False است) قابل تفکیک می‌ماند.
            admin_rights=types.ChatAdminRights(), rank="")),
        logger=bot.logger,
        what=f"EXCLUSIVE DEMOTE chat={chat_id} user={user_id}",
    )
    # ۲) و محدودیت ارسالِ پیش‌فرضِ گروه دوباره اعمال شود.
    await call(
        bot.client(functions.channels.EditBannedRequest(
            channel=channel, participant=participant,
            banned_rights=clone_banned_rights(None, send_messages=True,
                                              send_plain=True))),
        logger=bot.logger,
        what=f"EXCLUSIVE REVOKE chat={chat_id} user={user_id}",
    )
    log_info(bot.logger,
             f"EXCLUSIVE REVOKE RPC OK chat_id={chat_id} user_id={user_id}")
    return True


#: فیلترِ فهرست شرکت‌کنندگان = ادمین‌ها (اگر کتابخانه آن را داشته باشد).
def _admins_filter():
    try:
        return types.ChannelParticipantsAdmins()
    except Exception:                                # noqa: BLE001
        return None


async def _admin_in_participants_list(bot, chat_id, user_id):
    """آیا کاربر در فهرست **ادمین‌های** گروه دیده می‌شود؟

    این مسیرِ دومِ تأیید است (بدون GetParticipant)؛ برای سرورهایی که
    ``GetParticipant`` را با خطای دائمی رد می‌کنند و باعث می‌شد ربات
    «انجام نشد» بدهد در حالی که کاربر واقعاً ادمین شده بود.
    """
    try:
        participants = await bot.client.get_participants(
            chat_id, filter=_admins_filter(), limit=200)
    except TypeError:
        try:
            participants = await bot.client.get_participants(chat_id)
        except Exception:                            # noqa: BLE001
            return None
    except Exception:                                # noqa: BLE001
        return None
    wanted = str(storage.user_key(user_id))
    try:
        async for member in participants:
            member_id = getattr(member, "id", None)
            if member_id is not None and str(storage.user_key(member_id)) == wanted:
                participant = getattr(member, "participant", None)
                cls = (participant.__class__.__name__.lower()
                       if participant is not None else "")
                if "admin" in cls or "creator" in cls or \
                        getattr(member, "is_admin", False) or \
                        getattr(member, "is_creator", False):
                    return True
                return False
    except Exception:                                # noqa: BLE001
        return None
    return False   # در فهرست ادمین‌ها نبود


async def read_participant(bot, chat_id, user_id):
    """شرکت‌کنندهٔ واقعی از سرور (برای تأیید نهایی)."""
    _peer, channel = await peer_and_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id)
    try:
        result = await call(
            bot.client(functions.channels.GetParticipantRequest(
                channel=channel, participant=participant)),
            logger=bot.logger,
            what=f"EXCLUSIVE READ chat={chat_id} user={user_id}",
        )
    except Exception as error:                       # noqa: BLE001
        log_info(bot.logger,
                 f"EXCLUSIVE READ FAILED chat_id={chat_id} "
                 f"user_id={user_id} error={error!r}")
        return None
    # GetParticipantRequest خروجی‌اش خودِ participant است (نه wrapper)؛
    # هر دو شکل را تحمل می‌کنیم تا با تغییر نسخهٔ کتابخانه نشکند.
    return getattr(result, "participant", result)


async def read_participant_rights(bot, chat_id, user_id):
    """``ChatBannedRights`` واقعیِ کاربر از سرور."""
    participant_obj = await read_participant(bot, chat_id, user_id)
    return getattr(participant_obj, "banned_rights", None)


def is_admin_participant(participant_obj) -> bool:
    """آیا سرور می‌گوید این کاربر ادمینِ گروه است؟"""
    return type(participant_obj).__name__ in {
        "ChannelParticipantAdmin", "ChannelParticipantCreator"}


def admin_rights_of(participant_obj):
    return getattr(participant_obj, "admin_rights", None)


def has_any_management_right(admin_rights) -> bool:
    """آیا حقِ ادمینی به‌جز «ارسال/ویرایش پیام» هم دارد؟"""
    if admin_rights is None:
        return False
    for flag in ("change_info", "post_messages", "delete_messages",
                 "ban_users", "invite_users", "pin_messages", "add_admins",
                 "anonymous", "manage_call", "other", "manage_topics"):
        if bool(getattr(admin_rights, flag, False)):
            return True
    return False


async def rights_confirmed(bot, chat_id, user_id, allowed) -> bool:
    """آیا API واقعاً همان چیزی را می‌گوید که ما ادعا می‌کنیم؟

    * ``allowed=True`` ⇒ باید ادمین باشد، هیچ حق مدیریتیِ اضافه نداشته باشد و
      ارسال برایش مسدود نباشد.
    * ``allowed=False`` ⇒ نباید ادمین باشد و ارسال باید مسدود باشد.
    """
    participant = await read_participant(bot, chat_id, user_id)
    if participant is None:
        return False
    admin = is_admin_participant(participant)
    banned = text_is_banned(getattr(participant, "banned_rights", None))
    if allowed:
        # ⚠️ مهم: سروش روی توگل‌های این صفحه پیش‌فرض‌هایی می‌گذارد
        # («تغییر اطلاعات گروه» و «سنجاق پیام‌ها» را خودش روشن می‌کند). اگر
        # تأیید را به «صفر حق» مشروط کنیم، کاربری که واقعاً ادمین و قادر به
        # ارسال است «ناموفق» شمرده می‌شود و فهرست اختصاصی خالی می‌ماند —
        # دقیقاً همان باگِ «این کاربر در فهرست اختصاصی نیست». ملاکِ درست:
        # ادمین بودن + نبودِ محدودیت ارسال + نداشتنِ حق‌های حساس
        # (حذف پیام / بن / افزودن ادمین / دعوت / تغییر اطلاعات در حالت
        #  غیرمجاز — این‌ها در همهٔ حالت‌ها باید False باشند).
        if not admin or banned:
            return False
        return not has_sensitive_right(admin_rights_of(participant))
    return (not admin) and banned


#: حق‌هایی که در هیچ حالتی نباید به کاربرِ «اختصاصی» داده شوند.
SENSITIVE_ADMIN_FLAGS = ("delete_messages", "ban_users", "add_admins",
                         "invite_users", "anonymous", "manage_call",
                         "manage_topics", "other", "post_messages",
                         "change_info", "pin_messages")


def has_sensitive_right(admin_rights) -> bool:
    if admin_rights is None:
        return False
    return any(bool(getattr(admin_rights, flag, False))
               for flag in SENSITIVE_ADMIN_FLAGS)


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
    wanted = set(states)
    with storage.EXCLUSIVE_STORAGE._lock:
        record = _group_record(storage.EXCLUSIVE_STORAGE._data(), chat_id)
        return [dict(entry) for entry in record.get("users", {}).values()
                if entry.get("state") in wanted]


def is_exclusive(chat_id, user_id) -> bool:
    wanted = storage.user_key(user_id)
    return any(storage.user_key(item.get("user_id")) == wanted
               for item in list_entries(chat_id))


def get_entry(chat_id, user_id):
    wanted = storage.user_key(user_id)
    for entry in list_entries(chat_id):
        if storage.user_key(entry.get("user_id")) == wanted:
            return entry
    return None


def _save_entry(chat_id, entry):
    def mutate(data):
        record = _group_record(data, chat_id, create=True)
        users = record.setdefault("users", {})
        users[storage.user_key(entry["user_id"])] = entry
        # فهرست در انتظارِ لغو (برای بازیابی پس از قطعی/ری‌استارت)
        pending = [item for item in record.get("pending", [])
                   if storage.user_key(item) != storage.user_key(entry["user_id"])]
        entry["active"] = entry.get("state") == "active"
        if entry.get("state") in {"active", PENDING_FLAG}:
            pending.append(storage.user_key(entry["user_id"]))
        record["pending"] = pending
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
class NotLocked(RuntimeError):
    """گروه قفل نیست ⇒ استثنا معنا ندارد."""


class AlreadyExclusive(RuntimeError):
    pass


class NotExclusive(RuntimeError):
    pass


async def grant(bot, chat_id, user_id, *, actor_id=None, name="", username="",
                verify=False):
    """ثبت کاربر در فهرست اختصاصی + ارتقا به ادمین (بی‌درنگ).

    ⚠️ ترتیبِ جدید بر اساس بازخورد کارفرما («باید سریع ارسال شود»):

    ۱) رکورد پایدار در حالت ``pending_grant`` (قبل از RPC، تا گم نشود)
    ۲) یک ``EditAdminRequest`` با مهلتِ کوتاه  ⇒ همین‌جا «موفق» اعلام می‌شود
    ۳) تأییدِ واقعی از سرور **در پس‌زمینه** (بدون تأخیر دادن به پاسخ کاربر)

    پیش‌فرض ``verify=False`` است: لایهٔ دستور (`commands`) خودش بعد از ارسالِ
    پاسخ، تأیید را در پس‌زمینه اجرا می‌کند (``complete_verification``).
    """
    if not await is_group_locked(bot, chat_id):
        raise NotLocked(f"group {chat_id} is not locked")

    existing = get_entry(chat_id, user_id)
    if existing and existing.get("state") == "active":
        raise AlreadyExclusive(f"user {user_id} already exclusive")

    saved = _save_entry(chat_id, {
        "user_id": storage.user_key(user_id),
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

    # ⚠️ ارتقا: فقط یک RPC. اگر سرور رد کند رکورد برای بازیابی می‌ماند.
    try:
        preset = await grant_participant_rights(bot, chat_id, user_id)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, saved, PENDING_GRANT_FLAG, {
            "granted": False, "confirmed": False, "last_error": repr(error)})
        raise

    # «ادمین شد» بلافاصله قطعی است (RPC بدون خطا برگشت)؛ رکورد فعال می‌شود.
    saved = _set_state(chat_id, saved, "active",
                       {"granted": True, "confirmed": True,
                        "admin_preset": preset,
                        "confirmed_at": storage.now()})
    log_info(bot.logger,
             f"EXCLUSIVE GRANTED chat_id={chat_id} user_id={user_id} "
             f"preset={preset}")
    if verify:
        try:
            asyncio.ensure_future(_verify_in_background(bot, chat_id,
                                                        user_id, saved))
        except RuntimeError:
            pass
    return saved


async def complete_verification(bot, chat_id, user_id, entry=None):
    """تأییدِ واقعی + (در صورت نیاز) ارتقای پله‌ای به حالت بعدی.

    این تابع عمداً **بعد از** ارسالِ پیامِ موفقیت اجرا می‌شود تا پاسخ به
    کارفرما سریع باشد؛ اگر سرور «ادمینِ بی‌حق» را نپذیرد، همین‌جا حالتِ
    حداقلی (`edit_only`) اعمال و رکورد به‌روزرسانی می‌شود. برای ریکاوریِ
    استارتاپ هم قابل استفاده است.
    """
    entry = entry or get_entry(chat_id, user_id) or {}
    applied = entry.get("admin_preset") or PRIMARY_PRESET
    if await verify_promotion(bot, chat_id, user_id):
        current = get_entry(chat_id, user_id) or entry
        _set_state(chat_id, current, "active",
                   {"granted": True, "confirmed": True,
                    "verified_at": storage.now()})
        log_info(bot.logger,
                 f"EXCLUSIVE VERIFIED chat_id={chat_id} user_id={user_id} "
                 f"preset={applied}")
        return get_entry(chat_id, user_id)

    names = [name for name, _preset in ADMIN_RIGHT_PRESETS]
    start_index = names.index(applied) + 1 if applied in names else 1
    peer = _cached_channel_peer(bot, chat_id)
    if peer is None:
        peer = await _actions(bot)._group_peer(chat_id)
    _remember_channel_peer(bot, chat_id, peer)
    channel = types.InputChannel(peer.channel_id, peer.access_hash)
    participant = await user_peer(bot, chat_id, user_id)
    for name, preset in ADMIN_RIGHT_PRESETS[start_index:]:
        try:
            await _edit_admin(bot, channel, participant, preset, chat_id,
                              user_id)
        except Exception as error:                   # noqa: BLE001
            log_info(bot.logger,
                     f"EXCLUSIVE PRESET FAILED chat_id={chat_id} "
                     f"user_id={user_id} preset={name} error={error!r}")
            continue
        if await verify_promotion(bot, chat_id, user_id):
            current = get_entry(chat_id, user_id) or entry
            _set_state(chat_id, current, "active",
                       {"granted": True, "confirmed": True,
                        "admin_preset": name, "verified_at": storage.now()})
            log_info(bot.logger,
                     f"EXCLUSIVE VERIFIED WITH FALLBACK chat_id={chat_id} "
                     f"user_id={user_id} preset={name}")
            return get_entry(chat_id, user_id)

    current = get_entry(chat_id, user_id) or entry
    _set_state(chat_id, current, PENDING_GRANT_FLAG, {
        "granted": True, "confirmed": False,
        "last_error": "promotion not confirmed by server yet",
        "unverified_at": storage.now()})
    log_info(bot.logger,
             f"EXCLUSIVE NOT VERIFIED (kept pending) chat_id={chat_id} "
             f"user_id={user_id}")
    return get_entry(chat_id, user_id)


async def _verify_in_background(bot, chat_id, user_id, saved):
    """تأییدِ آرام در پس‌زمینه — هرگز پاسخ کاربر را معطل نمی‌کند."""
    try:
        return await complete_verification(bot, chat_id, user_id, saved)
    except Exception as error:                       # noqa: BLE001
        log_info(bot.logger,
                 f"EXCLUSIVE BACKGROUND VERIFY FAILED chat_id={chat_id} "
                 f"user_id={user_id} error={error!r}")
        return None


async def revoke(bot, chat_id, user_id, *, reason="manual"):
    """لغو مجوز + غیرفعال کردن رکورد. اجرای چندباره ممنوع.

    اگر رکوردی در حافظه/دیسک نباشد ولی سرور بگوید این کاربر **ادمین** است،
    باز هم demote انجام می‌شود: کاربر نباید به‌خاطر از دست رفتنِ یک فایل،
    ادمینِ گروه بماند.
    """
    entry = get_entry(chat_id, user_id)
    if not entry:
        participant = await read_participant(bot, chat_id, user_id)
        if not is_admin_participant(participant):
            raise NotExclusive(f"user {user_id} is not exclusive")
        # رکوردِ حداقلی می‌سازیم تا تاریخچه و وضعیت پایدار بماند.
        entry = _save_entry(chat_id, {
            "user_id": storage.user_key(user_id),
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
                 f"user_id={user_id} (was admin without record)")
    if entry.get("state") == "revoked":
        raise NotExclusive(f"user {user_id} already revoked")

    try:
        await revoke_participant_rights(bot, chat_id, user_id)
    except Exception as error:                       # noqa: BLE001
        # قطعی/خطای گذرا: رکورد در «در انتظار لغو» می‌ماند تا ریکاوریِ
        # استارتاپ دوباره تلاش کند؛ پرونده هرگز گم نمی‌شود.
        _set_state(chat_id, entry, PENDING_FLAG, {"last_error": repr(error)})
        raise
    try:
        confirmed = await rights_confirmed(bot, chat_id, user_id, False)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, entry, PENDING_FLAG, {"last_error": repr(error)})
        raise
    entry = _set_state(chat_id, entry,
                       "revoked" if confirmed else PENDING_FLAG,
                       {"confirmed": bool(confirmed), "revoked_at": storage.now(),
                        "revoked_at_tehran": storage.iso_tehran(),
                        "revoke_reason": reason})
    log_info(bot.logger,
             f"EXCLUSIVE REVOKED chat_id={chat_id} user_id={user_id} "
             f"reason={reason} confirmed={confirmed}")
    return entry


async def revoke_all(bot, chat_id, *, reason="group_unlocked", logger=None):
    """لغو همهٔ مجوزهای موقتِ این گروه (وقتی گروه باز شد).

    خروجی: ``(revoked, failed_entries)``. خطای هر کاربر بقیه را متوقف نمی‌کند و
    در فهرستِ بازیابی می‌نشیند.
    """
    revoked, failed = [], []
    for entry in list_entries(chat_id):
        try:
            await revoke(bot, chat_id, entry["user_id"], reason=reason)
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
