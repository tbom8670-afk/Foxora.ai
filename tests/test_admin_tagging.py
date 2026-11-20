"""Read-only group/bot-admin tag command, TL mentions and real message route."""
import asyncio
from types import SimpleNamespace

import pytest
from splusthon.tl import functions, types

from modules import admin_storage, admin_tagging as tagging
from modules.group_dispatch import LANE_ADMIN, classify_priority
from modules.group_id import normalize_group_id
from modules.manage_features import group_shutdown_commands
from handlers import message_handler as main_handler

CHAT = 1234
OWNER = 11
BOT_ID = 99


def run(coroutine):
    return asyncio.run(coroutine)


def person(uid, username=None, name="مدیر", role="admin"):
    if role == "creator":
        participant = types.ChannelParticipantCreator(
            user_id=uid, admin_rights=types.ChatAdminRights(add_admins=True))
    elif role == "admin":
        participant = types.ChannelParticipantAdmin(
            user_id=uid, promoted_by=OWNER, date=None,
            admin_rights=types.ChatAdminRights(change_info=True))
    else:
        participant = types.ChannelParticipant(user_id=uid, date=None)
    return SimpleNamespace(id=uid, username=username, first_name=name,
                           last_name="", access_hash=uid * 13,
                           participant=participant)


class Client:
    def __init__(self):
        self.people = []
        self.cached_ids = set()
        self.requests = []
        self.fail_list = False
        self.total_override = None
        self.wrong_identity = {}

    async def get_input_entity(self, entity):
        if isinstance(entity, (types.InputPeerChannel, types.InputPeerChat,
                               types.InputPeerUser)):
            return entity
        if getattr(entity, "id", None) is not None:
            uid = entity.id
            return types.InputPeerUser(uid, uid * 13)
        if isinstance(entity, int) and entity in self.cached_ids:
            uid = self.wrong_identity.get(entity, entity)
            return types.InputPeerUser(uid, uid * 13)
        raise ValueError("not cached")

    async def get_participants(self, peer, *, filter, limit):
        assert isinstance(peer, types.InputPeerChat)
        assert isinstance(filter, types.ChannelParticipantsAdmins)
        assert limit == tagging.PAGE_SIZE
        return self.people

    async def get_entity(self, username):
        for user in self.people:
            if str(username).lower() == "@" + str(user.username).lower():
                return types.User(id=user.id, user_type=types.UserTypeNormal(),
                                  username=user.username, access_hash=user.access_hash,
                                  first_name=user.first_name)
        raise ValueError("username not found")

    async def __call__(self, request):
        assert isinstance(request, functions.channels.GetParticipantsRequest)
        assert isinstance(request.filter, types.ChannelParticipantsAdmins)
        assert request.hash == 0 and request.channel.channel_id == CHAT
        self.requests.append(request)
        if self.fail_list:
            raise TimeoutError("admin list unavailable")
        # A malicious/buggy response may contain a normal member; do not tag it.
        rows = self.people[request.offset:request.offset + request.limit]
        return SimpleNamespace(
            count=self.total_override if self.total_override is not None else len(self.people),
            participants=[user.participant for user in rows],
            users=list(reversed(rows)),  # TL users need not match participant order.
        )


class Event:
    def __init__(self, uid=OWNER, *, username=None, private=False, edited=False):
        self.is_private = private
        self.sender_id = uid
        self.is_edit = edited
        self.sender = SimpleNamespace(id=uid, username=username)
        self.replies = []
        self.mentions = []
        self.fail_send = False

    async def reply(self, text, **_kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=501)

    async def respond(self, text, **kwargs):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.mentions.append((text, kwargs.get("formatting_entities")))
        return SimpleNamespace(id=502)


class Logger:
    def __init__(self):
        self.errors = []

    def log_error(self, text):
        self.errors.append(text)

    def log_info(self, _text):
        pass


@pytest.fixture
def setup(monkeypatch, tmp_path):
    tagging.reset_memory()
    monkeypatch.setattr(tagging, "get_group_owner", lambda chat: OWNER if
                        normalize_group_id(chat) == str(CHAT) else None)
    monkeypatch.setattr(tagging, "is_active", lambda chat: normalize_group_id(chat) == str(CHAT))
    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    monkeypatch.setattr(admin_storage, "_cache", None)
    monkeypatch.setattr(admin_storage, "_cache_mtime", None)
    client = Client()
    bot = SimpleNamespace(client=client, bot_account_id=BOT_ID, logger=Logger(),
                          reply_input_peer_cache={CHAT: types.InputPeerChannel(CHAT, 17)})
    yield bot
    tagging.reset_memory()


def send(bot, uid=OWNER, *, username=None, private=False, edited=False):
    event = Event(uid, username=username, private=private, edited=edited)
    result = run(tagging.handle(bot, event, CHAT, uid, event.sender, tagging.COMMAND,
                                edited=edited))
    return result, event


def mention_text(text, entity):
    raw = text.encode("utf-16-le")
    return raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


def test_union_dedup_screenshot_phrases_and_real_utf16_mentions(setup):
    bot = setup
    bot.client.people = [
        person(21, "nativex", "سارا"),
        person(22, None, "🌸مینا"),
        person(BOT_ID, "mybot"),
        person(23, "notadmin", role="member"),
    ]
    bot.client.cached_ids = {22, 31}
    admin_storage.add_admin(CHAT, 22, name="مینا")  # Also native: tag only once.
    admin_storage.add_admin(-1000000001234, 31, username="registered",
                            name="ثبت‌شده")
    admin_storage.add_admin(7890, 77, username="othergroup")
    records = admin_storage.load_admins()
    records[str(CHAT)].extend(["@legacyuser", "@LEGACYUSER", "@mybot"])
    admin_storage.save_admins(records)
    before = admin_storage.FILE.read_bytes()

    handled, event = send(bot)
    assert handled and "فهرست ادمین‌های گروه کامل نبود" in event.replies[0]
    assert len(bot.client.requests) == 1  # No Recent/all-member scan or per-admin role RPC.
    assert len(event.mentions) == 4  # 21, 22, 31, legacy; owner isn't listed.
    labels = []
    for i, (text, entities) in enumerate(event.mentions):
        assert text.startswith("سلام ") and len(entities) == 1
        assert text.endswith(tagging.GREETINGS[i][1])
        ent = entities[0]
        assert ent.offset == tagging._u16("سلام ")
        labels.append(mention_text(text, ent))
    assert labels == ["@nativex", "🌸مینا", "@registered", "@legacyuser"]
    assert isinstance(event.mentions[0][1][0], types.MessageEntityMention)
    assert isinstance(event.mentions[1][1][0], types.InputMessageEntityMentionName)
    assert event.mentions[1][1][0].user_id.user_id == 22
    assert event.mentions[2][1][0].user_id.user_id == 31
    assert isinstance(event.mentions[3][1][0], types.MessageEntityMention)
    # Both outgoing entity variants must serialize as real SPlusthon TL.
    for text, entities in event.mentions:
        request = functions.messages.SendMessageRequest(
            peer=types.InputPeerChannel(CHAT, 17), message=text, entities=entities)
        assert request._bytes()
    assert all("@notadmin" not in text and "@mybot" not in text
               for text, _ in event.mentions)
    assert admin_storage.FILE.read_bytes() == before  # Absolutely no writes.


def test_group_owner_tagged_only_when_native_or_registered(setup):
    bot = setup
    bot.client.people = [person(21, "friend")]
    _, first = send(bot)
    assert len(first.mentions) == 1 and "@friend" in first.mentions[0][0]
    tagging.reset_memory()
    bot.client.people.append(person(OWNER, "ownername", role="creator"))
    _, second = send(bot)
    assert len(second.mentions) == 2 and not second.replies
    assert any("@ownername" in text for text, _ in second.mentions)
    tagging.reset_memory()
    bot.client.people = [person(21, "friend")]
    bot.client.cached_ids.add(OWNER)
    admin_storage.add_admin(CHAT, OWNER, username="ownername")
    _, third = send(bot)
    assert len(third.mentions) == 2
    assert any(entity[0].user_id.user_id == OWNER for text, entity in third.mentions
               if "@ownername" in text)


def test_permissions_native_only_global_owner_private_and_edited(setup, monkeypatch):
    bot = setup
    bot.client.people = [person(21, "native")]
    for uid in (21, 54, 1):
        _, event = send(bot, uid=uid)
        assert not event.mentions and "فقط مالک ثبت‌شده" in event.replies[0]
    assert not bot.client.requests
    admin_storage.add_admin(CHAT, 31, username="registered")
    bot.client.cached_ids.add(31)
    _, allowed = send(bot, uid=31, username="registered")
    assert len(allowed.mentions) == 2
    tagging.reset_memory()
    read_count = len(bot.client.requests)
    _, private = send(bot, private=True)
    assert not private.mentions and len(bot.client.requests) == read_count
    _, edit = send(bot, edited=True)
    assert not edit.mentions and not edit.replies
    assert len(bot.client.requests) == read_count
    monkeypatch.setattr(tagging, "is_active", lambda _chat: False)
    _, inactive = send(bot)
    assert "گروه فعال" in inactive.replies[0] and not inactive.mentions


def test_stale_username_never_tags_wrong_id_and_native_failure_is_disclosed(setup):
    bot = setup
    bot.client.fail_list = True
    admin_storage.add_admin(CHAT, 31, username="reassigned", name="Registered")
    bot.client.people = [person(777, "reassigned")]
    _, not_found = send(bot)
    assert not not_found.mentions
    assert "0 نفر از 1" in not_found.replies[0]
    # Even with the client's ID cached, the mention targets ID 31 (not 777).
    tagging.reset_memory()
    bot.client.cached_ids.add(31)
    _, delivered = send(bot)
    assert len(delivered.mentions) == 1
    mention = delivered.mentions[0][1][0]
    assert isinstance(mention, types.InputMessageEntityMentionName)
    assert mention.user_id.user_id == 31
    assert "فهرست ادمین‌های گروه کامل نبود" in delivered.replies[0]


def test_registered_id_resolves_by_username_only_if_identity_matches(setup):
    bot = setup
    bot.client.fail_list = True
    admin_storage.add_admin(CHAT, 31, username="validname")
    bot.client.people = [person(31, "validname")]
    _, event = send(bot)
    assert len(event.mentions) == 1
    entity = event.mentions[0][1][0]
    assert isinstance(entity, types.InputMessageEntityMentionName)
    assert entity.user_id.user_id == 31
    assert "@validname" in event.mentions[0][0]


def test_stale_username_collision_does_not_hide_second_id(setup):
    bot = setup
    bot.client.people = [person(21, "oldname")]
    bot.client.cached_ids.add(31)
    admin_storage.add_admin(CHAT, 31, username="oldname", name="registered")
    _, event = send(bot)
    assert len(event.mentions) == 2
    assert isinstance(event.mentions[0][1][0], types.MessageEntityMention)
    assert event.mentions[1][1][0].user_id.user_id == 31


def test_failed_send_no_success_then_cooldown_is_per_group(setup):
    bot = setup
    bot.client.people = [person(21, "native")]
    event = Event()
    event.fail_send = True
    run(tagging.handle(bot, event, CHAT, OWNER, event.sender, tagging.COMMAND))
    assert not event.mentions and "0 نفر از 1" in event.replies[0]
    _, good = send(bot)
    assert len(good.mentions) == 1
    _, repeat = send(bot)
    assert "کمی بعد" in repeat.replies[0] and not repeat.mentions
    assert len(bot.client.requests) == 2  # no extra read on a cooldown hit


def test_partial_pagination_is_reported_without_full_member_scan(setup):
    bot = setup
    bot.client.people = [person(21, "native")]
    bot.client.total_override = 250
    _, event = send(bot)
    assert len(event.mentions) == 1
    assert len(bot.client.requests) == 2
    assert [req.offset for req in bot.client.requests] == [0, 1]
    assert "فهرست ادمین‌های گروه کامل نبود" in event.replies[0]


def test_simple_group_only_tags_users_with_admin_roles(setup):
    bot = setup
    bot.reply_input_peer_cache[CHAT] = types.InputPeerChat(CHAT)
    bot.client.people = [person(21, "native"),
                         person(22, "regular", role="member")]
    _, event = send(bot)
    assert len(event.mentions) == 1 and "@native" in event.mentions[0][0]
    assert not bot.client.requests  # Simple chats read the actual role list.


def test_large_group_is_bounded_and_reports_omitted_admins(setup, monkeypatch):
    bot = setup
    monkeypatch.setattr(tagging, "MAX_TAGS", 2)
    bot.client.people = [person(21, "native21"), person(22, "native22"),
                         person(23, "native23")]
    _, event = send(bot)
    assert len(event.mentions) == 2
    assert "2 نفر از 3" in event.replies[0]
    assert "سقف هر نوبت 2 نفر" in event.replies[0]


def test_multiple_mentions_are_sent_concurrently_but_bounded(setup):
    bot = setup
    bot.client.people = [person(20 + i, f"native{i}") for i in range(7)]

    class SlowEvent(Event):
        def __init__(self):
            super().__init__()
            self.running = 0
            self.peak = 0

        async def respond(self, text, **kwargs):
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0)  # Let the other RPCs start before this one ends.
            result = await super().respond(text, **kwargs)
            self.running -= 1
            return result

    event = SlowEvent()
    run(tagging.handle(bot, event, CHAT, OWNER, event.sender, tagging.COMMAND))
    assert len(event.mentions) == 7
    assert event.peak == tagging.SEND_CONCURRENCY == 4


def test_stalled_server_stops_with_partial_result_and_no_orphan_sends(setup, monkeypatch):
    bot = setup
    bot.client.people = [person(21, "native")]
    monkeypatch.setattr(tagging, "SEND_BUDGET", 0.01)

    class HangingEvent(Event):
        def __init__(self):
            super().__init__()
            self.running = 0

        async def respond(self, *_args, **_kwargs):
            self.running += 1
            try:
                await asyncio.Future()
            finally:
                self.running -= 1

    event = HangingEvent()
    run(tagging.handle(bot, event, CHAT, OWNER, event.sender, tagging.COMMAND))
    assert event.running == 0 and not event.mentions
    assert "0 نفر از 1" in event.replies[0]
    assert "زمان ارسال به پایان رسید" in event.replies[0]


def test_priority_and_pending_other_commands_do_not_consume_it():
    assert classify_priority(tagging.COMMAND)[1] == LANE_ADMIN
    assert not main_handler._should_check_native_admin(False, False, tagging.COMMAND)
    assert tagging.COMMAND in main_handler._INTERNAL_EXACT_COMMANDS
    from handlers import group_admin_handler
    assert group_admin_handler._other_project_command(tagging.COMMAND)
    group_shutdown_commands.set_awaiting(CHAT, OWNER)
    try:
        assert not group_shutdown_commands.is_command(tagging.COMMAND, CHAT, OWNER)
    finally:
        group_shutdown_commands.clear_awaiting(CHAT, OWNER)


def test_actual_message_pipeline_invokes_tagging_once(setup, monkeypatch):
    """The real handle_new_message route reaches the new code, not a mock route."""
    import test_admin_commands as fakes

    bot = setup
    bot.client.people = [person(21, "native")]
    bot.is_spam_locked = lambda _key: False
    bot.clear_spam_lock = lambda _key: None
    bot.detector = fakes.Detector()
    bot.tracker = fakes.Tracker()
    bot.config_manager = fakes.ConfigManager()
    bot.punished_users = set()
    bot.spam_burst_messages = {}
    bot.spammer_messages = {}
    bot.spam_burst_users = set()
    bot.moderation_queue = SimpleNamespace(enqueue=lambda *a, **kw: True)
    monkeypatch.setattr(main_handler.admin_tools, "get_group_owner", lambda _chat: OWNER)
    monkeypatch.setattr(main_handler, "get_group_owner", lambda _chat: OWNER)

    class PipelineEvent(fakes.Event):
        def __init__(self):
            super().__init__(tagging.COMMAND, OWNER, chat_id=CHAT)
            self.sender_id = OWNER
            self.is_group = True
            self._bot_cached_chat = SimpleNamespace(id=CHAT, megagroup=True)
            self._bot_cached_sender = self._user
            self.mentions = []

        async def respond(self, text, **kwargs):
            self.mentions.append((text, kwargs.get("formatting_entities")))
            return SimpleNamespace(id=700)

    event = PipelineEvent()
    run(main_handler.handle_new_message(bot, event))
    assert len(event.mentions) == 1, (event.replies, bot.logger.errors)
    assert "@native" in event.mentions[0][0]
    assert len(bot.client.requests) == 1
