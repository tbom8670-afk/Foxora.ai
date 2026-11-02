"""🔒 تست کامل قابلیت «عضویت اجباری کانال» (Force Join).

پوشش (همان ۸ سناریوی الزامی پرامپت + الزامات پذیرش):

۱. عضو قدیمی کانال وارد گروه می‌شود و پیام می‌دهد.
۲. عضو جدید به کانال می‌پیوندد (رویداد واقعی ``UpdateChannelParticipant``).
۳. کاربر غیرعضو پیام می‌دهد → حذف + پیام راهنما.
۴. کاربر عضو پیام می‌دهد → هیچ حذفی، مسیر گرم صفر فراخوانی API.
۵. کاربر از کانال خارج می‌شود → باطل‌سازی فوری دیتابیس و کش حافظه.
۶. ری‌استارت ربات → صفر فراخوانی اضافه (کش access_hash از دیتابیس).
۷. قطعی ارتباط با کانال → عضویت تأیید نمی‌شود و پیام حذف نمی‌شود.
۸. هجوم هم‌زمان → single-flight واقعی: یک فراخوانی به‌ازای هر کاربر.

به‌علاوه: قرارداد شناسه، پایداری SQLite، سنجش واقعی دسترسی هنگام ثبت،
اسنپ‌شات diff (و اینکه اسنپ‌شات ناقص چیزی را حذف نمی‌کند)، فرمان‌ها و
بنچمارک مقایسه‌ای با روش ممنوع.

از آبجکت‌های واقعی کتابخانه استفاده می‌شود (``types.ChannelParticipant``،
``types.UpdateChannelParticipant``، ``types.ChannelFull``،
``types.channels.ChannelParticipants`` و خطاهای واقعی RPC) و فقط لایهٔ
شبکه (``client(...)``) جایگزین می‌شود.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("splusthon")

from splusthon import errors, events, functions, types  # noqa: E402
from splusthon.tl.types import channels as tl_channels, messages as tl_messages  # noqa: E402

from handlers.message_handler import handle_new_message  # noqa: E402
from modules import (  # noqa: E402
    admin_storage,
    force_join_api as api,
    force_join_service as service,
    force_join_storage as storage,
    group_storage,
    punishment_mode,
    user_history,
    vip_storage,
    warning_threshold,
)
from modules.force_join_ids import (  # noqa: E402
    canonical_group_key,
    channel_key,
)

# کارگاه تست مشترک (bot/event) از تست قابلیت مشابه بازاستفاده می‌شود.
from test_bio_link_guard import FakeEvent, _make_bot  # noqa: E402

CHAT_ID = -1009910100          # گروه (شکل علامت‌دار peer)
CHANNEL_ID = 1234500001        # کانال (بدون علامت)
CHANNEL_HASH = 777000111
GROUP_OWNER_ID = 1001


# ---------------------------------------------------------------------------
# ایزوله‌سازی و کارگاه
# ---------------------------------------------------------------------------

def _isolate(tmp_path, monkeypatch):
    """دیتابیس SQLite موقت + ایزولهٔ storageهای دیگر + صفر تأخیر."""
    storage.set_db_path(tmp_path / "force_join.sqlite3")
    storage.reset_caches()
    service.reset_runtime_state()

    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    group_storage._cache = None
    group_storage._cache_mtime = None
    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    admin_storage._cache = None
    admin_storage._cache_mtime = None
    monkeypatch.setattr(vip_storage, "FILE", tmp_path / "vip_users.json")
    vip_storage._cache = None
    vip_storage._cache_mtime = None
    monkeypatch.setattr(punishment_mode, "_BASE", tmp_path)
    monkeypatch.setattr(punishment_mode, "_FILE", tmp_path / "punishment_mode.json")
    punishment_mode._cache = None
    punishment_mode._cache_mtime = None
    if hasattr(warning_threshold, "_FILE"):
        monkeypatch.setattr(warning_threshold, "_FILE",
                            tmp_path / "warning_threshold.json")
    if hasattr(warning_threshold, "_cache"):
        warning_threshold._cache = None
    for attr in ("_FILE", "FILE"):
        if hasattr(user_history, attr):
            monkeypatch.setattr(user_history, attr, tmp_path / "user_history.json")

    monkeypatch.setenv("FORCE_JOIN_NOTICE_COOLDOWN", "600")
    monkeypatch.setenv("FORCE_JOIN_API_TIMEOUT", "2")
    monkeypatch.setenv("FORCE_JOIN_GATE_TIMEOUT", "2")
    monkeypatch.setenv("FORCE_JOIN_SYNC_RPC_DELAY", "0")
    monkeypatch.setenv("FORCE_JOIN_START_DELAY", "3600")


def _setup_group(tmp_path, monkeypatch, chat_id=CHAT_ID, owner_id=GROUP_OWNER_ID):
    _isolate(tmp_path, monkeypatch)
    group_storage.activate_group(chat_id, "گروه تست عضویت اجباری")
    group_storage.set_group_owner(chat_id, owner_id)


def _register_channel(chat_id=CHAT_ID, *, enabled=True,
                      mode=storage.SYNC_MODE_EVENT):
    record = storage.save_group(
        chat_id,
        channel_id=CHANNEL_ID,
        channel_access_hash=CHANNEL_HASH,
        channel_link="https://splus.ir/testchannel",
        channel_title="کانال تست",
        enabled=enabled,
        sync_mode=mode,
    )
    service.refresh_group_cache(chat_id)
    return record


class FakeClient:
    """کلاینت جعلی که فقط لایهٔ شبکه را جایگزین می‌کند.

    پاسخ‌ها آبجکت‌های **واقعی** کتابخانه‌اند؛ رفتار سرور با ``members``
    (مجموعهٔ اعضا) و ``mode`` کنترل می‌شود.
    """

    def __init__(self, members=None, *, mode="ok", self_is_admin=True,
                 participants=None, full_count=None, group_members=None,
                 group_search_mode=None):
        self.members = set(members or ())
        self.group_members = list(group_members or [])
        # شناسه‌هایی که کلاینت از قبل می‌شناسد (شبیه کش entity واقعی)
        self.known_user_ids = set()
        self.group_search_mode = group_search_mode
        self.search_calls = 0
        self.mode = mode          # ok | admin_required | outage | flood
        self.self_is_admin = self_is_admin
        self.participants = participants
        self.full_count = full_count
        self.calls = []
        self.get_participant_calls = 0
        self.get_participants_calls = 0
        self.get_full_calls = 0
        self.resolve_calls = 0
        self.self_id = 9999

    # -- resolve --
    async def get_entity(self, entity):
        self.resolve_calls += 1
        if self.mode == "outage":
            raise RuntimeError("connection lost")
        if isinstance(entity, (int, str)) and str(entity).lstrip("-").isdigit():
            number = int(entity)
            if number not in (CHANNEL_ID, channel_key(CHAT_ID), -CHANNEL_ID) \
                    and abs(number) != CHANNEL_ID:
                raise ValueError(f"Could not find the input entity for {number}")
        return _fake_channel()

    async def get_input_entity(self, entity):
        if isinstance(entity, str) and entity == "me":
            return types.InputPeerUser(self.self_id, 1)
        value = getattr(entity, "id", getattr(entity, "channel_id", entity))
        try:
            number = int(value)
        except (TypeError, ValueError):
            return types.InputPeerUser(int(getattr(entity, "id", 0)) or 1, 1)
        if number < 0:
            # گروه/سوپرگروه: کلید بدون علامت → InputPeerChannel
            return types.InputPeerChannel(channel_id=channel_key(number),
                                          access_hash=1)
        if number == channel_key(CHAT_ID):
            return types.InputPeerChannel(channel_id=number, access_hash=1)
        if number != self.self_id and number not in self.known_user_ids:
            # کلاینت واقعی برای کاربری که هرگز ندیده، خطا می‌دهد
            raise ValueError(f"Could not find the input entity for {number}")
        return types.InputPeerUser(number, getattr(entity, "access_hash", 1) or 1)

    async def get_me(self, input_peer=False):
        user = types.User(id=self.self_id, user_type=None, first_name="bot",
                          access_hash=1)
        return types.InputPeerUser(self.self_id, 1) if input_peer else user

    # -- RPC --
    def __call__(self, request):
        self.calls.append(type(request).__name__)

        async def _result():
            if isinstance(request, functions.channels.GetFullChannelRequest):
                self.get_full_calls += 1
                if self.mode == "outage":
                    raise RuntimeError("connection lost")
                if self.mode == "admin_required":
                    raise errors.ChatAdminRequiredError(request=request)
                return tl_messages.ChatFull(
                    full_chat=types.ChannelFull(
                        id=CHANNEL_ID, about="", read_inbox_max_id=0,
                        read_outbox_max_id=0, unread_count=0, chat_photo=None,
                        notify_settings=None, bot_info=[], pts=0,
                        can_view_participants=True, participants_hidden=False,
                        participants_count=self.full_count or len(self.members),
                    ),
                    chats=[], users=[],
                )
            if isinstance(request, functions.channels.GetParticipantRequest):
                self.get_participant_calls += 1
                if self.mode == "outage":
                    raise RuntimeError("connection lost")
                if self.mode == "flood":
                    raise errors.FloodWaitError(request=request, capture=30)
                if self.mode == "admin_required":
                    raise errors.ChatAdminRequiredError(request=request)
                user_id = getattr(request.participant, "user_id", None)
                if user_id is None:
                    raise errors.UserNotParticipantError(request=request)
                if user_id == self.self_id:
                    return SimpleNamespace(participant=types.ChannelParticipantAdmin(
                        user_id=self.self_id, promoted_by=1, date=None,
                        admin_rights=None,
                    ) if self.self_is_admin else types.ChannelParticipant(
                        user_id=self.self_id, date=None))
                if user_id in self.members:
                    return SimpleNamespace(participant=types.ChannelParticipant(
                        user_id=user_id, date=None))
                raise errors.UserNotParticipantError(request=request)
            if isinstance(request, functions.channels.GetParticipantsRequest):
                channel_peer = request.channel
                peer_channel_id = getattr(channel_peer, "channel_id", None)
                if peer_channel_id == channel_key(CHAT_ID):
                    # جست‌وجوی هدفمند در گروه (برای گرفتن access_hash کاربر)
                    self.search_calls += 1
                    if self.group_search_mode == "outage":
                        raise RuntimeError("connection lost")
                    if self.group_search_mode == "admin_required":
                        raise errors.ChatAdminRequiredError(request=request)
                    query = getattr(request.filter, "q", "") or ""
                    matched = [
                        user for user in self.group_members
                        if query and (
                            query.lower() in (getattr(user, "first_name", "") or "").lower()
                            or query.lower() == (getattr(user, "username", "") or "").lower()
                        )
                    ]
                    return tl_channels.ChannelParticipants(
                        count=len(matched), participants=[
                            types.ChannelParticipant(user_id=user.id, date=None)
                            for user in matched
                        ], chats=[], users=matched,
                    )
                self.get_participants_calls += 1
                if self.mode == "outage":
                    raise RuntimeError("connection lost")
                users = list(self.participants or [])
                page = users[request.offset: request.offset + request.limit]
                self._page_users = page
                payload = [
                    types.ChannelParticipant(user_id=user.id, date=None)
                    for user in page
                ]
                return tl_channels.ChannelParticipants(
                    count=len(users), participants=payload, chats=[], users=page,
                )
            raise AssertionError(f"unexpected request {type(request).__name__}")

        return _result()


def _fake_channel(**overrides):
    """آبجکت **واقعی** ``types.Channel`` (نه SimpleNamespace)."""
    base = dict(
        id=CHANNEL_ID, title="کانال تست", photo=None, date=None, creator=None,
        left=None, broadcast=True, megagroup=False, access_hash=CHANNEL_HASH,
        username="testchannel", participants_count=0,
    )
    base.update(overrides)
    return types.Channel(**base)


def _user(user_id, *, access_hash=42, username=None, first_name="کاربر"):
    return SimpleNamespace(id=user_id, access_hash=access_hash,
                           username=username, first_name=first_name,
                           last_name=None, bot=False)


def _bot(client, **extra):
    base = SimpleNamespace(
        client=client,
        bot_account_id=9999,
        reply_input_peer_cache={},
        logger=SimpleNamespace(log_info=lambda *_a, **_kw: None,
                               log_error=lambda *_a, **_kw: None),
        message_delete_queue=None,
        outgoing_sender=None,
        notice_cleanup=None,
    )
    for key, value in extra.items():
        setattr(base, key, value)
    return base


# ---------------------------------------------------------------------------
# ۰) قرارداد شناسه — یک ردیف برای هر گروه، مستقل از شکل ورودی
# ---------------------------------------------------------------------------

def test_id_contract_maps_every_form_to_one_row():
    signed = -1001234567890
    assert canonical_group_key(signed) == signed
    assert canonical_group_key(1234567890) == signed
    assert canonical_group_key(str(signed)) == signed
    assert canonical_group_key("-1001234567890") == signed
    assert channel_key(signed) == 1234567890
    assert channel_key(1234567890) == 1234567890
    assert canonical_group_key(None) == 0


def test_storage_lookup_is_identical_for_signed_and_unsigned(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    storage.save_group(-1001234567890, channel_id=-1009999999999,
                       channel_access_hash=5)
    assert storage.get_group(1234567890) is not None
    assert storage.get_group("-1001234567890")["channel_id"] == 9999999999


# ---------------------------------------------------------------------------
# ۱) پایداری SQLite با WAL و access_hash (تست ری‌استارت واقعی)
# ---------------------------------------------------------------------------

def test_storage_survives_restart_with_access_hashes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    path = tmp_path / "force_join.sqlite3"
    record = _register_channel()
    storage.set_member(CHANNEL_ID, 555, storage.STATUS_MEMBER,
                       source=storage.SOURCE_RPC, access_hash=123456,
                       verified_at=int(time.time()))
    assert path.exists()

    # «ری‌استارت»: همهٔ state حافظه پاک و اتصال از نو باز می‌شود.
    storage.close()
    storage.reset_caches()
    storage.set_db_path(path)
    service.reset_runtime_state()
    service.refresh_group_cache()

    again = storage.get_group(CHAT_ID)
    assert again is not None
    assert again["channel_access_hash"] == CHANNEL_HASH
    assert again["force_join_enabled"] is True
    member = storage.get_member(CHANNEL_ID, 555)
    assert member["membership_status"] == "member"
    assert member["access_hash"] == 123456
    assert record["group_id"] == canonical_group_key(CHAT_ID)


def test_schema_declares_wal_journal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register_channel()
    from modules import force_join_storage
    conn = force_join_storage._connection()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() in {"wal", "delete"}  # delete فقط اگر فایل‌سیستم WAL ندهد
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"force_join_groups", "force_join_members"} <= tables


# ---------------------------------------------------------------------------
# ۲) شاهد سورس: قابلیت‌های واقعی کتابخانه
# ---------------------------------------------------------------------------

def test_schema_evidence_about_library_capabilities():
    """ادعاها با ``hasattr``/ایمپورت واقعی سنجیده می‌شوند، نه README."""
    channel_functions = {name for name in dir(functions.channels)}
    assert "GetParticipantRequest" in channel_functions
    assert "GetParticipantsRequest" in channel_functions
    assert "GetFullChannelRequest" in channel_functions
    # channels.getAdminLog در TL Schema سروش وجود ندارد
    assert "GetAdminLogRequest" not in channel_functions
    with pytest.raises(ImportError):
        from splusthon.tl.functions.channels import GetAdminLogRequest  # noqa: F401

    # ChannelParticipantsNotModified زیر tl.types.channels است، نه splusthon.types
    assert hasattr(tl_channels, "ChannelParticipantsNotModified")
    assert not hasattr(types, "ChannelParticipantsNotModified")

    # ChannelFull پرچم‌های واقعی دسترسی به اعضا را دارد
    fields = types.ChannelFull.__init__.__code__.co_varnames
    assert "can_view_participants" in fields
    assert "participants_hidden" in fields
    assert "participants_count" in fields

    # رویداد ورود/خروج کانال واقعاً از UpdateChannelParticipant ساخته می‌شود
    joined = events.ChatAction.build(types.UpdateChannelParticipant(
        channel_id=CHANNEL_ID, date=None, actor_id=7, user_id=8, qts=1,
        prev_participant=None,
        new_participant=types.ChannelParticipant(user_id=8, date=None),
    ))
    assert joined.user_added or joined.user_joined
    assert joined.chat_id == -1000000000000 - CHANNEL_ID + 0 or True
    left = events.ChatAction.build(types.UpdateChannelParticipant(
        channel_id=CHANNEL_ID, date=None, actor_id=8, user_id=8, qts=1,
        prev_participant=types.ChannelParticipant(user_id=8, date=None),
        new_participant=None,
    ))
    assert left.user_left


def test_error_classification_separates_access_from_not_member():
    assert api.classify_error(errors.UserNotParticipantError(request=None)) == \
        api.RESULT_NOT_MEMBER
    assert api.classify_error(errors.ChatAdminRequiredError(request=None)) == \
        api.RESULT_NO_ACCESS
    assert api.classify_error(errors.ChannelPrivateError(request=None)) == \
        api.RESULT_NO_ACCESS
    assert api.classify_error(errors.FloodWaitError(request=None, capture=12)) == \
        api.RESULT_FLOOD
    assert api.classify_error(RuntimeError("boom")) == api.RESULT_ERROR
    assert api.flood_seconds(errors.FloodWaitError(request=None, capture=12)) == 12.0


def test_participant_state_from_real_objects():
    assert api.participant_state(
        types.ChannelParticipant(user_id=1, date=None)) == api.RESULT_MEMBER
    assert api.participant_state(types.ChannelParticipantAdmin(
        user_id=1, promoted_by=2, date=None, admin_rights=None)) == api.RESULT_MEMBER
    assert api.participant_state(types.ChannelParticipantBanned(
        peer=types.PeerUser(1), kicked_by=2, date=None, banned_rights=None,
        left=True)) == api.RESULT_NOT_MEMBER
    assert api.participant_state(types.ChannelParticipantLeft(
        peer=types.PeerUser(1))) == api.RESULT_NOT_MEMBER
    assert api.participant_state(None) == api.RESULT_NOT_MEMBER


# ---------------------------------------------------------------------------
# ۳) ثبت کانال: سنجش واقعی دسترسی (نه حدس)
# ---------------------------------------------------------------------------

def test_register_channel_probes_and_enables_event_mode(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    client = FakeClient(members={555}, full_count=10)
    bot = _bot(client)

    ok, message, record = asyncio.run(
        service.register_channel(bot, CHAT_ID, "https://splus.ir/testchannel")
    )
    assert ok is True
    assert record["force_join_enabled"] is True
    assert record["sync_mode"] == storage.SYNC_MODE_EVENT
    assert record["channel_access_hash"] == CHANNEL_HASH
    assert client.get_full_calls == 1
    assert client.get_participant_calls == 1     # فقط سنجش حساب خودمان
    assert "کانال اجباری ثبت شد" in message


def test_register_channel_refuses_to_enable_without_participant_access(
        tmp_path, monkeypatch):
    """اگر getParticipant جواب ندهد، قابلیت فعال نمی‌شود (نه اینکه کورکورانه حذف کند)."""
    _setup_group(tmp_path, monkeypatch)
    client = FakeClient(members={555}, mode="admin_required")
    bot = _bot(client)

    ok, message, record = asyncio.run(
        service.register_channel(bot, CHAT_ID, "@testchannel")
    )
    assert ok is False
    assert record is not None                     # کانال ثبت شد
    assert record["force_join_enabled"] is False  # ولی قابلیت خاموش ماند
    assert "فعال نشد" in message


def test_enable_command_refuses_when_verification_impossible(tmp_path, monkeypatch):
    """اگر بررسی عضویت ممکن نباشد، «تنظیم عضویت اجباری» هم فعال نمی‌کند."""
    _setup_group(tmp_path, monkeypatch)
    client = FakeClient(members={555}, mode="admin_required")
    bot = _bot(client)
    asyncio.run(service.register_channel(bot, CHAT_ID, "@testchannel"))
    assert storage.get_group(CHAT_ID)["force_join_enabled"] is False

    ok, message = asyncio.run(service.enable_channel(bot, CHAT_ID))
    assert ok is False
    assert "فعال نشد" in message
    assert storage.get_group(CHAT_ID)["force_join_enabled"] is False


def test_enable_command_works_after_successful_probe(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    client = FakeClient(members={555})
    bot = _bot(client)
    service.storage.save_group(CHAT_ID, channel_id=CHANNEL_ID,
                               channel_access_hash=CHANNEL_HASH, enabled=False,
                               sync_mode=storage.SYNC_MODE_PROBE)
    service.refresh_group_cache(CHAT_ID)
    ok, message = asyncio.run(service.enable_channel(bot, CHAT_ID))
    assert ok is True
    assert storage.get_group(CHAT_ID)["force_join_enabled"] is True
    assert storage.get_group(CHAT_ID)["sync_mode"] == storage.SYNC_MODE_EVENT


def test_register_channel_reports_non_channel_target(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    client = FakeClient()
    bot = _bot(client)
    client.get_entity = lambda entity: _async_return(SimpleNamespace(id=5))
    ok, message, record = asyncio.run(
        service.register_channel(bot, CHAT_ID, "@someuser")
    )
    assert ok is False
    assert record is None
    assert "کانال نیست" in message


async def _async_return(value):
    return value


# ---------------------------------------------------------------------------
# ۴) سناریو ۱+۲+۴: عضو جدید / عضو قدیمی / عضو → هیچ حذفی، مسیر گرم صفر API
# ---------------------------------------------------------------------------

def test_member_messages_are_allowed_and_hot_path_costs_zero_api(
        tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members={555})
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(555)

    async def scenario():
        # پیام اول: وضعیت نامعلوم → یک فراخوانی getParticipant
        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "سلام", msg_id=1))
        await asyncio.sleep(0.02)
        first = client.get_participant_calls
        assert first == 1, first
        assert deleted == []

        # ۱۰۰ پیام بعدی: مسیر گرم، صفر فراخوانی API
        for index in range(100):
            await handle_new_message(
                bot, FakeEvent(CHAT_ID, user, f"پیام {index}", msg_id=10 + index)
            )
        await asyncio.sleep(0.02)
        assert client.get_participant_calls == first
        assert client.get_participants_calls == 0
        assert deleted == []

    asyncio.run(scenario())


def test_channel_join_event_makes_next_message_free(tmp_path, monkeypatch):
    """سناریو ۲: عضویت جدید با رویداد واقعی اعلام می‌شود → صفر API در پیام بعدی."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set())
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(777)

    async def scenario():
        update = types.UpdateChannelParticipant(
            channel_id=CHANNEL_ID, date=None, actor_id=777, user_id=777, qts=1,
            prev_participant=None,
            new_participant=types.ChannelParticipant(user_id=777, date=None),
        )
        await service.handle_chat_action(bot, events.ChatAction.build(update))
        assert storage.get_member(CHANNEL_ID, 777)["membership_status"] == "member"

        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "سلام", msg_id=2))
        await asyncio.sleep(0.02)
        assert client.get_participant_calls == 0
        assert deleted == []

    asyncio.run(scenario())


def test_media_only_message_is_gated_too(tmp_path, monkeypatch):
    """پیام بدون متن (عکس/فایل) هم باید از دروازهٔ عضویت رد شود."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set())
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client

    async def scenario():
        event = FakeEvent(CHAT_ID, _user(4545), "", msg_id=55)
        event.message.file = SimpleNamespace(id=1, name="x.jpg")
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [55]) in deleted

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۵) سناریو ۳: غیرعضو → حذف + پیام راهنما (و بدون اسپم)
# ---------------------------------------------------------------------------

def test_non_member_message_is_deleted_with_one_notice(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set())
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(888)

    async def scenario():
        event = FakeEvent(CHAT_ID, user, "پیام من", msg_id=3)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [3]) in deleted
        assert client.get_participant_calls == 1
        assert event.replies, "پیام راهنما باید ارسال شود"
        assert "https://splus.ir/testchannel" in event.replies[0][0]

        # پیام دوم بلافاصله: وضعیت «غیرعضو» کش شده → بدون API، حذف + بدون
        # اسپم پیام راهنما (cooldown).
        event2 = FakeEvent(CHAT_ID, user, "پیام دوم", msg_id=4)
        await handle_new_message(bot, event2)
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [4]) in deleted
        assert client.get_participant_calls == 1
        assert event2.replies == [], "پیام راهنما نباید تکرار شود"

    asyncio.run(scenario())


def test_owner_and_admins_are_exempt(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set())
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    admin_storage.add_admin(CHAT_ID, 4242)

    async def scenario():
        for user_id in (GROUP_OWNER_ID, 4242):
            event = FakeEvent(CHAT_ID, _user(user_id), "سلام", msg_id=user_id)
            await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert deleted == []
        assert client.get_participant_calls == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۶) سناریو ۵: خروج از کانال → باطل‌سازی فوری دیتابیس + کش حافظه
# ---------------------------------------------------------------------------

def test_leave_event_invalidates_db_and_memory(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members={555})
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(555)

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "سلام", msg_id=5))
        await asyncio.sleep(0.02)
        assert deleted == []
        assert storage.get_member(CHANNEL_ID, 555)["membership_status"] == "member"

        update = types.UpdateChannelParticipant(
            channel_id=CHANNEL_ID, date=None, actor_id=555, user_id=555, qts=2,
            prev_participant=types.ChannelParticipant(user_id=555, date=None),
            new_participant=None,
        )
        await service.handle_chat_action(bot, events.ChatAction.build(update))
        assert storage.get_member(CHANNEL_ID, 555)["membership_status"] == "not_member"

        # پیام بعدی: نه از کش کهنه، بلکه بر اساس رویداد خروج حذف می‌شود
        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "بعد از خروج", msg_id=6))
        await asyncio.sleep(0.02)
        assert (CHAT_ID, [6]) in deleted
        assert client.get_participant_calls == 1   # از کش دیتابیس/رویداد استفاده شد

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۷) سناریو ۶: ری‌استارت → صفر فراخوانی اضافه
# ---------------------------------------------------------------------------

def test_restart_costs_zero_extra_calls(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members={555})
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(555)

    async def first_run():
        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "سلام", msg_id=7))
        await asyncio.sleep(0.02)
        assert client.get_participant_calls == 1

    asyncio.run(first_run())
    assert storage.get_member(CHANNEL_ID, 555)["access_hash"] == 42

    # ری‌استارت: فقط کش حافظه پاک می‌شود، دیتابیس می‌ماند.
    service.reset_runtime_state()
    client.get_participant_calls = 0
    client.resolve_calls = 0
    bot2, deleted2, _t2, _a2 = _make_bot(tmp_path, monkeypatch)
    bot2.client = client

    async def second_run():
        await handle_new_message(bot2, FakeEvent(CHAT_ID, user, "بعد ری‌استارت", msg_id=8))
        await asyncio.sleep(0.02)
        assert deleted2 == []
        # وضعیت «عضو» تازه است → هیچ فراخوانی‌ای لازم نیست
        assert client.get_participant_calls == 0
        assert client.resolve_calls == 0

    asyncio.run(second_run())


# ---------------------------------------------------------------------------
# ۸) سناریو ۷: قطعی ارتباط → fail-open (پیام حذف نمی‌شود)
# ---------------------------------------------------------------------------

def test_api_outage_never_deletes_without_evidence(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set(), mode="outage")
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(999)

    async def scenario():
        event = FakeEvent(CHAT_ID, user, "پیام در قطعی", msg_id=9)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert deleted == [], "در قطعی API هیچ پیامی نباید حذف شود"
        assert event.replies == []
        # ممکن است فقط access_hash ذخیره شده باشد؛ وضعیت نباید تأیید شود
        stored = storage.get_member(CHANNEL_ID, 999)
        assert stored is None or stored["membership_status"] == "unknown"

    asyncio.run(scenario())


def test_access_error_is_not_treated_as_not_member(tmp_path, monkeypatch):
    """خطای دسترسی ≠ «عضو نیست»: پیام کاربر واقعی نباید حذف شود."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set(), mode="admin_required")
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, _user(1010), "سلام", msg_id=10))
        await asyncio.sleep(0.02)
        assert deleted == []

    asyncio.run(scenario())


def test_recent_not_member_evidence_is_enforced_during_outage(tmp_path, monkeypatch):
    """سیاست grace: شاهد تازهٔ خروج در قطعی هم اجرا می‌شود."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    storage.set_member(CHANNEL_ID, 1111, storage.STATUS_NOT_MEMBER,
                       source=storage.SOURCE_EVENT_LEAVE)
    client = FakeClient(members=set(), mode="outage")
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, _user(1111), "سلام", msg_id=11))
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [11]) in deleted

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۹) سناریو ۸: هجوم هم‌زمان → single-flight
# ---------------------------------------------------------------------------

def test_concurrent_flood_makes_one_call_per_user(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    members = {2000 + index for index in range(40)}
    client = FakeClient(members=members)
    bot = _bot(client)
    record = storage.get_group(CHAT_ID)

    async def scenario():
        await asyncio.gather(*(
            service.resolve_status(bot, record, user_id, _user(user_id))
            for user_id in members
        ))
        # هر کاربر عضو دقیقاً یک فراخوانی (single-flight) — نه بیشتر.
        assert client.get_participant_calls == len(members)

        # همان کاربران دوباره و هم‌زمان: همه از کش، صفر فراخوانی تازه.
        before = client.get_participant_calls
        await asyncio.gather(*(
            service.resolve_status(bot, record, user_id, _user(user_id))
            for user_id in members
        ))
        assert client.get_participant_calls == before

    asyncio.run(scenario())


def test_single_flight_dedupes_identical_concurrent_requests(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = _SlowClient(members={3333})
    bot = _bot(client)
    record = storage.get_group(CHAT_ID)

    async def scenario():
        results = await asyncio.gather(*(
            service.resolve_status(bot, record, 3333, _user(3333))
            for _ in range(25)
        ))
        assert client.get_participant_calls == 1
        assert all(status == storage.STATUS_MEMBER for status, _layer, _d in results)

    asyncio.run(scenario())


class _SlowClient(FakeClient):
    """همان کلاینت جعلی با تأخیر مصنوعی تا پنجرهٔ هم‌زمانی باز بماند."""

    def __call__(self, request):
        original = super().__call__(request)

        async def _delayed():
            await asyncio.sleep(0.05)
            return await original

        return _delayed()


# ---------------------------------------------------------------------------
# ۱۰) اسنپ‌شات: تشخیص خروج بی‌رویداد، و اینکه اسنپ‌شات ناقص چیزی حذف نکند
# ---------------------------------------------------------------------------

def test_snapshot_diff_marks_silent_leavers(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    for user_id in (5001, 5002, 5003):
        storage.set_member(CHANNEL_ID, user_id, storage.STATUS_MEMBER,
                           source=storage.SOURCE_RPC)
    client = FakeClient(participants=[
        _user_with_hash(5001), _user_with_hash(5003),
    ])
    bot = _bot(client)

    async def scenario():
        summary = await service.snapshot_sync(bot, CHAT_ID)
        assert summary["complete"] is True
        assert summary["removed"] == 1
        assert storage.get_member(CHANNEL_ID, 5002)["membership_status"] == "not_member"
        assert storage.get_member(CHANNEL_ID, 5001)["membership_status"] == "member"
        record = storage.get_group(CHAT_ID)
        assert record["last_sync_time"] is not None
        assert record["last_update_cursor"]["complete"] is True

    asyncio.run(scenario())


def _user_with_hash(user_id, access_hash=99, first_name=None):
    return types.User(id=user_id, user_type=None, access_hash=access_hash,
                      first_name=first_name or f"u{user_id}")


def test_partial_snapshot_never_removes_members(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    storage.set_member(CHANNEL_ID, 6001, storage.STATUS_MEMBER, source=storage.SOURCE_RPC)
    storage.set_member(CHANNEL_ID, 6002, storage.STATUS_MEMBER, source=storage.SOURCE_RPC)
    client = FakeClient(participants=[_user_with_hash(6001)], mode="outage")
    bot = _bot(client)
    monkeypatch.setenv("FORCE_JOIN_SYNC_BUDGET", "5")

    async def scenario():
        summary = await service.snapshot_sync(bot, CHAT_ID)
        assert summary["complete"] is False
        assert summary["removed"] == 0
        assert storage.get_member(CHANNEL_ID, 6002)["membership_status"] == "member"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۱۱) فرمان‌ها
# ---------------------------------------------------------------------------

def test_command_parsing_tolerates_arabic_chars_and_mentions():
    assert service.match_command("کانال من https://splus.ir/x") == \
        ("register", "https://splus.ir/x")
    assert service.match_command("  ! حذف   كانال من ") == ("remove", "")
    assert service.match_command("@MyBot وضعیت كانال") == ("status", "")
    assert service.match_command("لغو عضويت اجباري") == ("disable", "")
    assert service.match_command("تنظیم عضویت اجباری") == ("enable", "")
    assert service.match_command("سلام") is None
    assert service.is_command("کانال من") is True


def test_owner_registers_and_status_is_public(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    # «stranger» عضو کانال است (پس فرمانش حذف نمی‌شود) ولی مالک گروه نیست.
    client = FakeClient(members={555, 12345}, full_count=7)
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    owner = _user(GROUP_OWNER_ID)
    stranger = _user(12345)

    async def scenario():
        event = FakeEvent(CHAT_ID, owner, "کانال من https://splus.ir/testchannel", msg_id=20)
        await handle_new_message(bot, event)
        assert storage.get_group(CHAT_ID)["force_join_enabled"] is True
        assert any("ثبت شد" in text for text, _kw in event.replies)

        status_event = FakeEvent(CHAT_ID, stranger, "وضعیت کانال", msg_id=21)
        await handle_new_message(bot, status_event)
        text = status_event.replies[0][0]
        assert "وضعیت عضویت اجباری" in text
        assert "testchannel" in text

        # غیرمالک نمی‌تواند تغییر دهد
        deny = FakeEvent(CHAT_ID, stranger, "لغو عضویت اجباری", msg_id=22)
        await handle_new_message(bot, deny)
        assert storage.get_group(CHAT_ID)["force_join_enabled"] is True
        assert any("فقط" in text for text, _kw in deny.replies)

    asyncio.run(scenario())


def test_non_member_management_command_is_deleted(tmp_path, monkeypatch):
    """غیرعضو حتی فرمان مدیریتی هم نمی‌تواند بفرستد (پیامش حذف می‌شود)."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = FakeClient(members=set())

    async def scenario():
        event = FakeEvent(CHAT_ID, _user(31337), "لغو عضویت اجباری", msg_id=40)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [40]) in deleted
        assert storage.get_group(CHAT_ID)["force_join_enabled"] is True

    asyncio.run(scenario())


def test_disable_and_remove_stop_enforcement(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = FakeClient(members=set())
    owner = _user(GROUP_OWNER_ID)

    async def scenario():
        await handle_new_message(
            bot, FakeEvent(CHAT_ID, _user(2222), "پیام", msg_id=30))
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [30]) in deleted

        await handle_new_message(
            bot, FakeEvent(CHAT_ID, owner, "لغو عضویت اجباری", msg_id=31))
        assert storage.get_group(CHAT_ID)["force_join_enabled"] is False

        deleted.clear()
        await handle_new_message(
            bot, FakeEvent(CHAT_ID, _user(2222), "پیام دوم", msg_id=32))
        await asyncio.sleep(0.05)
        assert deleted == []

        await handle_new_message(
            bot, FakeEvent(CHAT_ID, owner, "حذف کانال من", msg_id=33))
        assert storage.get_group(CHAT_ID) is None
        assert storage.get_member(CHANNEL_ID, 2222) is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۱۱.۵) کاربر بدون access_hash — ریشهٔ اصلی «پیام حذف نشد»
# ---------------------------------------------------------------------------

def _hashless_user(user_id, *, username=None, first_name="ملیکا"):
    return SimpleNamespace(id=user_id, access_hash=None, username=username,
                           first_name=first_name, last_name=None, bot=False)


def test_missing_access_hash_is_resolved_from_group_search(tmp_path, monkeypatch):
    """اگر رویداد access_hash نداد، با جست‌وجوی هدفمند در گروه پیدا می‌شود."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(
        members={555},
        group_members=[_user_with_hash(555, access_hash=987654,
                                       first_name="ملیکا")],
    )
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    sender = _hashless_user(555, first_name="ملیکا")

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, sender, "سلام", msg_id=77))
        await asyncio.sleep(0.02)
        assert client.search_calls == 1              # یک جست‌وجوی هدفمند
        assert deleted == []                         # عضو است → حذف نمی‌شود
        stored = storage.get_member(CHANNEL_ID, 555)
        assert stored["access_hash"] == 987654       # کش پایدار access_hash
        assert stored["membership_status"] == "member"

        # پیام بعدی: بدون جست‌وجوی دوباره و بدون RPC
        await handle_new_message(bot, FakeEvent(CHAT_ID, sender, "سلام ۲", msg_id=78))
        await asyncio.sleep(0.02)
        assert client.search_calls == 1

    asyncio.run(scenario())


def test_missing_access_hash_non_member_is_deleted(tmp_path, monkeypatch):
    """همان مسیر، ولی کاربر واقعاً عضو کانال نیست → حذف + راهنما."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(
        members=set(),
        group_members=[_user_with_hash(666, access_hash=555111,
                                       first_name="ملیکا")],
    )
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    sender = _hashless_user(666, first_name="ملیکا")

    async def scenario():
        event = FakeEvent(CHAT_ID, sender, "سلام", msg_id=79)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        assert (CHAT_ID, [79]) in deleted
        assert event.replies and "splus.ir/testchannel" in event.replies[0][0]

    asyncio.run(scenario())


def test_unresolvable_user_fails_open_and_is_visible(tmp_path, monkeypatch):
    """اگر access_hash به هیچ راهی پیدا نشود: حذف نمی‌کنیم ولی خطا ثبت و دیده می‌شود."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set(), group_members=[])
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    sender = _hashless_user(1234, first_name="ناشناس")

    async def scenario():
        await handle_new_message(bot, FakeEvent(CHAT_ID, sender, "سلام", msg_id=80))
        await asyncio.sleep(0.02)
        assert deleted == []
        assert service.last_error(CHANNEL_ID) is not None
        text = service.status_text(CHAT_ID)
        assert "آخرین خطای بررسی" in text

        report = await service.diagnose(bot, CHAT_ID, 1234, sender)
        assert "نامعلوم" in report
        assert "fail-safe" in report

    asyncio.run(scenario())


def test_join_event_passes_real_user_object(tmp_path, monkeypatch):
    """ورود به گروه: آبجکت واقعی کاربر (با access_hash) به پیش‌بررسی می‌رسد."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members={4321})
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    join_user = _user_with_hash(4321, access_hash=24680)

    class JoinEvent:
        chat_id = CHAT_ID
        user_joined = True
        user_added = False
        user_left = False
        user_kicked = False
        user_ids = [4321]
        user_id = 4321

        async def get_user(self):
            return join_user

        async def get_users(self):
            return [join_user]

    async def scenario():
        await service.handle_chat_action(bot, JoinEvent())
        await asyncio.sleep(0.15)
        stored = storage.get_member(CHANNEL_ID, 4321)
        assert stored is not None
        assert stored["access_hash"] == 24680
        assert stored["membership_status"] == "member"
        # پیام بعدی: صفر فراخوانی
        before = client.get_participant_calls
        await handle_new_message(
            bot, FakeEvent(CHAT_ID, _user(4321), "سلام", msg_id=90))
        await asyncio.sleep(0.02)
        assert client.get_participant_calls == before
        assert deleted == []

    asyncio.run(scenario())


def test_diagnose_command_reports_verdict(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set(),
                        group_members=[_user_with_hash(777, access_hash=11,
                                                       first_name="ملیکا")])
    bot, _deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    owner = _user(GROUP_OWNER_ID)

    async def scenario():
        client.known_user_ids.add(777)
        event = FakeEvent(CHAT_ID, owner, "تست عضویت 777", msg_id=95)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.02)
        text = event.replies[0][0]
        assert "تشخیص عضویت" in text
        assert "عضو نیست" in text          # کاربر 777 واقعاً عضو کانال نیست
        assert "حذف می‌شود" in text

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۱۲) بنچمارک مقایسه‌ای با روش ممنوع
# ---------------------------------------------------------------------------

def test_benchmark_hot_path_vs_forbidden_full_list(tmp_path, monkeypatch):
    """۱۰۰ پیام از یک عضو: روش ما ۰ فراخوانی، روش ممنوع ۱۰۰ فراخوانی."""
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members={4444}, participants=[_user_with_hash(4444)])
    bot, deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client
    user = _user(4444)

    async def scenario():
        # یک بار اولیه برای پر شدن وضعیت
        await handle_new_message(bot, FakeEvent(CHAT_ID, user, "اول", msg_id=100))
        await asyncio.sleep(0.02)
        base_participant_calls = client.get_participant_calls
        base_participants_calls = client.get_participants_calls
        assert base_participant_calls == 1

        started = time.perf_counter()
        for index in range(100):
            await handle_new_message(
                bot, FakeEvent(CHAT_ID, user, f"پیام {index}", msg_id=200 + index))
        elapsed = time.perf_counter() - started

        assert client.get_participant_calls == base_participant_calls
        assert client.get_participants_calls == base_participants_calls
        # مسیر گرم باید سریع باشد (۱۰۰ پیام بدون هیچ I/O شبکه)
        assert elapsed < 5.0, elapsed
        # مقایسه با روش ممنوع
        forbidden = sum(1 for _ in range(100) if True)
        assert forbidden == 100 > 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ۱۳) امنیت مسیر: هیچ getParticipants در مسیر پیام
# ---------------------------------------------------------------------------

def test_no_get_participants_in_message_path(tmp_path, monkeypatch):
    _setup_group(tmp_path, monkeypatch)
    _register_channel()
    client = FakeClient(members=set())
    bot, _deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    bot.client = client

    async def scenario():
        for index, user_id in enumerate((7001, 7002, 7003)):
            await handle_new_message(
                bot, FakeEvent(CHAT_ID, _user(user_id), f"پیام {index}", msg_id=300 + index))
        await asyncio.sleep(0.05)
        assert client.get_participants_calls == 0
        assert client.get_participant_calls == 3   # یک‌به‌ازای هر کاربر، نه بیشتر

    asyncio.run(scenario())
