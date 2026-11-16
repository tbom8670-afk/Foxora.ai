"""تستِ سراسریِ «حذفِ پاسخِ ربات همراه با پیامِ ویرایش‌شده».

جریانِ واقعی از میانِ ``handle_new_message``:

۱. کاربر «سلام» می‌فرستد → ربات «سلام 👋» می‌دهد و شناسهٔ پاسخ ثبت می‌شود.
۲. همان کاربر پیامش را (با همان شناسه) به متنِ فیلترشده ویرایش می‌کند →
   پیامِ ویرایش‌شده اسپم تشخیص داده می‌شود و باید هم پیامِ کاربر و هم پاسخِ
   خودِ ربات از صفِ حذف بگذرد.

این تست با ربات/رویدادِ ساختگی (مثلِ بقیهٔ تست‌های همین ریپو) هندلرِ واقعی را
اجرا می‌کند، بنابراین سیم‌کشیِ ``_schedule_reply`` و بلوکِ پاکسازیِ پاسخ را
واقعاً می‌سنجد — نه فقط وجودِ متن در سورس.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from handlers.message_handler import handle_new_message
from modules import bot_reply_registry as reg


CHAT_ID = -1001234567890
USER_ID = 4242
MSG_ID = 5001            # شناسهٔ پیامِ کاربر (با ویرایش تغییر نمی‌کند)
BOT_REPLY_ID = 5002      # شناسهٔ پاسخِ ربات به «سلام»
SPAM_TEXT = "تبلیغ هرزنامه تست"   # نه دستور، نه در SIMPLE_REPLIES، نه فحشِ لیست


class FakeSentMessage:
    """نتیجهٔ event.reply — extract_sent_id باید شناسه‌اش را بخواند."""
    def __init__(self, mid):
        self.id = mid
        self.out = True
        self.peer_id = object()


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.first_name = "کاربر"
        self.last_name = ""
        self.username = None


class FakeMessage:
    def __init__(self, text, mid, edit_date=None):
        self.id = mid
        self.message = text
        self.edit_date = edit_date
        self.reply_to_msg_id = None
        self.entities = None
        self.file = None
        self.photo = None
        self.via_bot = None
        self.fwd_from = None
        self.grouped_id = None


class FakeChat:
    def __init__(self, cid):
        self.id = cid
        self.title = "گروه تست"


class FakeEvent:
    def __init__(self, text, *, edit_date=None, reply_id=BOT_REPLY_ID):
        self.chat_id = CHAT_ID
        self.sender = FakeUser(USER_ID)
        self.sender_id = USER_ID
        self.message = FakeMessage(text, MSG_ID, edit_date=edit_date)
        self.reply_to = None
        self.is_private = False
        self.out = False
        self.replies = []
        self._reply_id = reply_id
        self._chat = FakeChat(CHAT_ID)

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return self._chat

    async def get_input_chat(self):
        return self._chat

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return FakeSentMessage(self._reply_id)

    async def respond(self, text, **kwargs):
        self.replies.append(text)
        return FakeSentMessage(self._reply_id)


class FakeDeleteQueue:
    def __init__(self):
        self.calls = []          # [(chat_id, (ids...), priority)]

    def enqueue(self, chat_id, message_ids, *, priority=1, rpc_peer=None):
        ids = tuple(m for m in message_ids if isinstance(m, int) and m > 0)
        self.calls.append((chat_id, ids, priority))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result((len(ids), []))
        return fut

    @property
    def deleted_ids(self):
        out = set()
        for _chat, ids, _prio in self.calls:
            out.update(ids)
        return out


class FakeLogger:
    def log_info(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_deleted_message(self, *a, **k): pass


class FakeModerationQueue:
    _pending_keys = set()

    def enqueue(self, chat_id, action, operation=None, on_success=None,
                on_failure=None, **kwargs):
        if operation:
            res = operation()
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
        if on_success:
            asyncio.create_task(on_success(True))
        return True


class FakeClient:
    async def get_me(self):
        return FakeUser(999999)

    async def delete_messages(self, chat_id, ids, **kwargs):
        return True

    async def get_messages(self, *a, **k):
        return []

    def iter_messages(self, *a, **k):
        async def _empty():
            if False:
                yield None
        return _empty()


class FakeBot:
    def __init__(self, spam_words):
        self.client = FakeClient()
        self.logger = FakeLogger()
        self.moderation_queue = FakeModerationQueue()
        self.message_delete_queue = FakeDeleteQueue()
        self.punished_users = set()
        self.spam_lock = set()
        self.rejoin_spam_state = {}
        self.bot_account_id = 999999
        self.group_timer_tasks = {}
        self.banned_words_local = set()
        self.spam_burst_messages = {}
        self.spam_burst_users = set()
        self.spammer_messages = {}
        self.user_messages = {}
        self.repeat_messages = {}
        self._big_spam_incidents = {}
        self._auto_spam_cleanup_pending = {}
        self._auto_spam_cleanup_tasks = {}
        self._spam_cleanup_incidents = {}
        self.reply_input_peer_cache = {}
        self.native_group_admin_cache = {}
        self._spam_words = spam_words

        words = self._spam_words

        self.detector = type("Detector", (), {
            "is_spam": lambda self, text, chat_id: (
                (True, "کلمه ممنوعه (تست)")
                if any(w in (text or "") for w in words) else (False, "")
            ),
            "check_banned_words": lambda self, text, chat_id: (False, ""),
            "check_message": lambda self, *a, **k: (False, ""),
            "has_public_username": lambda self, text: False,
        })()
        self.tracker = type("Tracker", (), {
            "muted_users": {},
            "banned_users": {},
            "reset_count": lambda *a, **k: None,
            "get_count": lambda *a, **k: 0,
            "increment": lambda *a, **k: 1,
            "decrement": lambda *a, **k: 0,
            "should_punish": lambda *a, **k: False,
        })()
        self.config_manager = type("Config", (), {
            "get": lambda self, k, d=None: d,
            "banned_words": [],
        })()
        self.admin_actions = type("AA", (), {
            "send_warning": lambda *a, **k: asyncio.sleep(0, result=True),
            "ban_user": lambda *a, **k: asyncio.sleep(0, result=True),
            "punish_user": lambda *a, **k: asyncio.sleep(0, result=True),
            "mute_user": lambda *a, **k: asyncio.sleep(0, result=True),
        })()

    def is_spam_locked(self, *a, **k): return False
    def set_spam_lock(self, *a, **k): pass
    def clear_spam_lock(self, *a, **k): pass
    def clear_released_user_state(self, *a, **k): pass


@pytest.fixture(autouse=True)
def _clean_registry():
    reg.clear()
    yield
    reg.clear()


@pytest.mark.anyio
async def test_greeting_reply_is_deleted_when_message_edited_to_spam():
    bot = FakeBot(spam_words=("هرزنامه",))

    # ۱) کاربر «سلام» می‌فرستد → ربات پاسخ می‌دهد.
    greeting_event = FakeEvent("سلام")
    await handle_new_message(bot, greeting_event)
    await asyncio.sleep(0.05)     # اجازهٔ اجرای تسکِ _schedule_reply

    assert "سلام 👋" in greeting_event.replies
    # پاسخِ ربات باید برای همین (گروه، پیام) ثبت شده باشد.
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == [BOT_REPLY_ID]
    # هنوز چیزی حذف نشده.
    assert bot.message_delete_queue.deleted_ids == set()

    # ۲) همان پیام ویرایش می‌شود به متنِ فیلترشده.
    edited_event = FakeEvent(SPAM_TEXT, edit_date=1_700_000_000)
    await handle_new_message(bot, edited_event)
    await asyncio.sleep(0.05)

    deleted = bot.message_delete_queue.deleted_ids
    # پاسخِ خودِ ربات باید پاک شده باشد…
    assert BOT_REPLY_ID in deleted, f"bot reply not deleted; deleted={deleted}"
    # …و پیامِ ویرایش‌شدهٔ کاربر هم.
    assert MSG_ID in deleted, f"edited user message not deleted; deleted={deleted}"
    # ورودیِ رجیستری باید مصرف (pop) شده باشد.
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == []


@pytest.mark.anyio
async def test_greeting_reply_survives_when_not_edited():
    bot = FakeBot(spam_words=("هرزنامه",))

    greeting_event = FakeEvent("سلام")
    await handle_new_message(bot, greeting_event)
    await asyncio.sleep(0.05)

    assert "سلام 👋" in greeting_event.replies
    # بدونِ ویرایش، هیچ حذفی رخ نمی‌دهد و پاسخ سر جایش می‌ماند.
    assert bot.message_delete_queue.deleted_ids == set()
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == [BOT_REPLY_ID]


@pytest.mark.anyio
async def test_fresh_spam_without_prior_reply_deletes_no_bot_message():
    bot = FakeBot(spam_words=("هرزنامه",))

    # پیامِ اسپمِ مستقیم (بدونِ «سلام» و بدونِ پاسخِ ثبت‌شدهٔ ربات).
    spam_event = FakeEvent(SPAM_TEXT)
    await handle_new_message(bot, spam_event)
    await asyncio.sleep(0.05)

    # فقط پیامِ خودِ کاربر باید حذف شود، نه شناسهٔ پاسخِ ربات.
    assert BOT_REPLY_ID not in bot.message_delete_queue.deleted_ids


@pytest.mark.anyio
async def test_reply_deleted_when_message_edited_to_empty():
    """ویرایش به متنِ خالی: هیچ فیلتری پیام را حذف نمی‌کند، ولی پاسخِ ربات باید برود."""
    bot = FakeBot(spam_words=("هرزنامه",))

    greeting_event = FakeEvent("سلام")
    await handle_new_message(bot, greeting_event)
    await asyncio.sleep(0.05)
    assert "سلام 👋" in greeting_event.replies
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == [BOT_REPLY_ID]

    # ویرایش به متنِ کاملاً خالی.
    empty_event = FakeEvent("", edit_date=1_700_000_001)
    await handle_new_message(bot, empty_event)
    await asyncio.sleep(0.05)

    deleted = bot.message_delete_queue.deleted_ids
    # پاسخِ ربات باید پاک شده باشد…
    assert BOT_REPLY_ID in deleted, f"bot reply not deleted on empty edit; deleted={deleted}"
    # …ولی پیامِ خالیِ خودِ کاربر حذف نمی‌شود (محتوای هرزنامه نیست).
    assert MSG_ID not in deleted, f"empty user message should NOT be deleted; deleted={deleted}"
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == []


@pytest.mark.anyio
async def test_reply_deleted_when_message_edited_to_invisible_chars():
    """ویرایش به نویسه‌های نامرئی (ZWSP) باید مثلِ «خالی» شمرده شود."""
    bot = FakeBot(spam_words=("هرزنامه",))

    greeting_event = FakeEvent("سلام")
    await handle_new_message(bot, greeting_event)
    await asyncio.sleep(0.05)
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == [BOT_REPLY_ID]

    # «خالیِ» فریبنده: فقط فاصلهٔ صفر‌عرض و BOM.
    sneaky_event = FakeEvent("\u200b\ufeff \u200c", edit_date=1_700_000_002)
    await handle_new_message(bot, sneaky_event)
    await asyncio.sleep(0.05)

    assert BOT_REPLY_ID in bot.message_delete_queue.deleted_ids


@pytest.mark.anyio
async def test_empty_edit_without_prior_reply_is_noop():
    """پیامِ خالیِ ویرایش‌شدهٔ کاربری که ربات به او پاسخ نداده: هیچ حذفی رخ نمی‌دهد."""
    bot = FakeBot(spam_words=("هرزنامه",))

    empty_event = FakeEvent("", edit_date=1_700_000_003)
    await handle_new_message(bot, empty_event)
    await asyncio.sleep(0.05)

    assert bot.message_delete_queue.deleted_ids == set()


@pytest.mark.anyio
async def test_non_empty_edit_keeps_reply():
    """ویرایشِ «سلام» به یک متنِ تمیزِ غیرخالی: پاسخِ ربات باید بماند."""
    bot = FakeBot(spam_words=("هرزنامه",))

    greeting_event = FakeEvent("سلام")
    await handle_new_message(bot, greeting_event)
    await asyncio.sleep(0.05)
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == [BOT_REPLY_ID]

    # ویرایش به متنِ تمیز (نه خالی، نه هرزنامه) — باید بی‌اثر باشد.
    clean_event = FakeEvent("امروز هوا خوبه", edit_date=1_700_000_004)
    await handle_new_message(bot, clean_event)
    await asyncio.sleep(0.05)

    assert BOT_REPLY_ID not in bot.message_delete_queue.deleted_ids
    # چون متن تمیز است و حذفی رخ نداده، پاسخ دست‌نخورده می‌ماند.
    assert reg.peek_reply_ids(CHAT_ID, MSG_ID) == [BOT_REPLY_ID]
