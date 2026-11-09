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

from modules.group_actions import (
    GroupActions,
    clone_banned_rights,
    text_is_banned,
)
from modules.manage_features import storage
from modules.manage_features.rpc import call, log_info

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
    """``(peer, input_channel)`` با همان منطقِ موجودِ پروژه."""
    actions = _actions(bot)
    peer = await actions._group_peer(chat_id)
    if not (hasattr(peer, "channel_id") and hasattr(peer, "access_hash")):
        raise ValueError("chat is not a channel/supergroup")
    return peer, types.InputChannel(peer.channel_id, peer.access_hash)


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
async def is_group_locked(bot, chat_id) -> bool:
    """آیا گروه الان برای اعضای عادی قفل است؟ (پاسخ از سرور)"""
    peer, _channel = await peer_and_channel(bot, chat_id)
    rights, _source = await _actions(bot)._fetch_default_banned_rights(
        chat_id, peer)
    return text_is_banned(rights)


ALL_ADMIN_FLAGS = ("change_info", "post_messages", "edit_messages",
                   "delete_messages", "ban_users", "invite_users",
                   "pin_messages", "add_admins", "anonymous", "manage_call",
                   "other", "manage_topics", "post_stories", "edit_stories",
                   "delete_stories")

#: حالت‌های ارتقا به‌ترتیبِ اولویت. همهٔ فلگ‌ها **صریح** False داده می‌شوند
#: (None یعنی «سرور خودش تصمیم بگیرد» و در سروش باعث روشن شدنِ
#: «تغییر اطلاعات گروه» و «سنجاق پیام‌ها» می‌شد).
ADMIN_RIGHT_PRESETS = (
    # ۱) ادمین بدون هیچ حق مدیریتی — دقیقاً خواستهٔ کارفرما.
    ("none", {}),
    # ۲) اگر سرور «ادمینِ بدون حق» را demote کرد: تنها کمترین حقِ ممکن.
    ("edit_only", {"edit_messages": True}),
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


async def grant_participant_rights(bot, chat_id, user_id):
    """کاربر را ادمین گروه می‌کند؛ اول بدون هیچ حق مدیریتی.

    ترتیب: «ادمینِ بدون حق» → (اگر سرور demote کرد) «فقط ویرایش پیام خودش».
    خروجی: نامِ حالتی که واقعاً اعمال شد یا ``None``.
    """
    _peer, channel = await peer_and_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id)
    # ۱) اگر محدودیت/بن دارد برداشته شود تا ارسالش آزاد شود.
    await call(
        bot.client(functions.channels.EditBannedRequest(
            channel=channel, participant=participant,
            banned_rights=clone_banned_rights(None))),
        logger=bot.logger,
        what=f"EXCLUSIVE UNBAN chat={chat_id} user={user_id}",
    )

    applied = None
    for name, preset in ADMIN_RIGHT_PRESETS:
        await _apply_admin_rights(bot, channel, participant, preset, chat_id,
                                  user_id)
        participant_obj = await read_participant(bot, chat_id, user_id)
        if is_admin_participant(participant_obj):
            applied = name
            break
        log_info(bot.logger,
                 f"EXCLUSIVE GRANT REJECTED BY SERVER chat_id={chat_id} "
                 f"user_id={user_id} preset={name} — trying next preset")
    if applied is None:
        raise RuntimeError(
            "server refused every admin preset (user was not promoted)")
    log_info(bot.logger,
             f"EXCLUSIVE GRANT RPC OK chat_id={chat_id} user_id={user_id} "
             f"preset={applied}")
    return applied


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
                         "manage_topics", "other")


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


async def grant(bot, chat_id, user_id, *, actor_id=None, name="", username=""):
    """ثبت کاربر در فهرست اختصاصی + اعطای مجوز ارسالِ واقعی."""
    if not await is_group_locked(bot, chat_id):
        raise NotLocked(f"group {chat_id} is not locked")

    existing = get_entry(chat_id, user_id)
    if existing and existing.get("state") == "active":
        raise AlreadyExclusive(f"user {user_id} already exclusive")

    # ۱) رکوردِ «در انتظار» **قبل** از RPC نوشته می‌شود؛ اگر ارتقا رد شود یا
    #    اتصال قطع شود، رکورد گم نمی‌شود و در بازیابی دوباره تلاش می‌شود.
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

    # ۲) ارتقا به ادمین با حقِ ارسال.
    try:
        preset = await grant_participant_rights(bot, chat_id, user_id)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, saved, PENDING_GRANT_FLAG, {
            "granted": False, "confirmed": False, "last_error": repr(error)})
        raise
    saved = _set_state(chat_id, saved, PENDING_GRANT_FLAG,
                       {"admin_preset": preset})
    # ۳) تأیید واقعی با API؛ اگر تأیید نشد، رکورد فعال نمی‌ماند.
    try:
        confirmed = await rights_confirmed(bot, chat_id, user_id, True)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, saved, PENDING_FLAG, {"last_error": repr(error)})
        raise
    saved = _set_state(chat_id, saved,
                       "active" if confirmed else PENDING_FLAG,
                       {"granted": True, "confirmed": bool(confirmed)})
    log_info(bot.logger,
             f"EXCLUSIVE GRANTED chat_id={chat_id} user_id={user_id} "
             f"confirmed={confirmed}")
    return saved


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
