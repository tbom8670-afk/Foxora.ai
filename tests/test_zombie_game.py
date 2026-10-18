"""تست‌های واحد و یکپارچه برای بازی «زامبی»، ادامهٔ «گروه آخرالزمان» و به‌روزرسانی «لیست بازی»."""
import asyncio
import sys
import types
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold

import economy
from economy import storage as eco_storage
from handlers import fox_games_router as router
from handlers.message_handler import handle_new_message
from modules import entertainment_control
from modules import group_storage
from modules.fox_games import zombie
from modules.group_dispatch import PRIORITY_COMMAND, classify_priority


def _slice_u16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le")


class FakeUser:
    def __init__(self, uid, first_name="کاربر", last_name="", username=None):
        self.id = uid
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.access_hash = uid * 10 + 1


class FakeClient:
    def __init__(self, fail_users=()):
        self.fail_users = set(fail_users)
        self.dm_messages = []     # (user_id, text, entities)
        self.group_messages = []  # (chat_id, text, entities)

    async def send_message(self, target, text, **kwargs):
        entities = kwargs.get("formatting_entities")
        uid = getattr(target, "id", None)
        if uid is None and isinstance(target, int):
            if target < 0:
                self.group_messages.append((target, text, entities))
                return types.SimpleNamespace(id=len(self.group_messages))
            uid = target
        if uid in self.fail_users:
            raise ValueError(f"user {uid} blocked DM")
        self.dm_messages.append((uid, text, entities))
        return types.SimpleNamespace(id=len(self.dm_messages))

    async def get_entity(self, target):
        uid = getattr(target, "id", target)
        return FakeUser(uid, first_name=f"کاربر{uid}")


class FakeEvent:
    def __init__(self, chat_id, sender, text, is_private=False, msg_id=100):
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = sender.id
        self._bot_cached_sender = sender
        self.is_private = is_private
        self.out = False
        self.reply_to = None
        self.replies = []  # (text, entities)
        self._chat = types.SimpleNamespace(
            id=chat_id,
            title="گروه تست زامبی" if not is_private else "",
        )
        self._bot_cached_chat = self._chat
        self.chat = self._chat
        self.message = types.SimpleNamespace(
            id=msg_id,
            message=text,
            caption=None,
            file=None,
            media=None,
            entities=None,
            fwd_from=None,
            forward=None,
            edit_date=None,
            reply_to=None,
            reply_to_msg_id=None,
        )

    async def get_chat(self):
        return self._chat

    async def get_sender(self):
        return self.sender

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs.get("formatting_entities")))
        return types.SimpleNamespace(id=len(self.replies) + 500)


class FakeLogger:
    def log_info(self, msg):
        pass

    def log_warning(self, msg):
        pass

    def log_error(self, msg):
        pass


class FakeBot:
    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.logger = FakeLogger()
        self.bot_account_id = 999999999
        self.reply_input_peer_cache = {}
        self.config_manager = types.SimpleNamespace(get=lambda k, d=None: d)

    def is_spam_locked(self, key):
        return False

    def clear_spam_lock(self, key):
        pass

    def debug_message_log(self, msg):
        pass


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    router.reset_all()
    eco_storage.use_file(tmp_path / "economy.json")
    economy.reset_all()
    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    group_storage._cache = None
    group_storage._cache_mtime = None
    monkeypatch.setattr(entertainment_control, "FILE", tmp_path / "entertainment_mode.json")
    entertainment_control._cache = None
    entertainment_control._cache_mtime = None
    yield
    router.reset_all()
    economy.reset_all()


def test_constants_and_dispatch_registration():
    """بررسی زمان‌های ۵۰ ثانیه ثبت‌نام، ۱۷ ثانیه انتخاب، ۴ بازیکن و ثبت دستورات."""
    assert zombie.REQUIRED_PLAYERS == 4
    assert zombie.JOIN_SECONDS == 50
    assert zombie.BITE_SECONDS == 17
    assert zombie.ZOMBIE_WIN_SILVER == 5
    assert zombie.BAIT_INFECTED_SILVER == 5
    assert zombie.HUNTER_WIN_BRONZE == 7
    assert zombie.SNIPER_WIN_BRONZE == 7

    assert "زامبی" in entertainment_control.GAME_COMMANDS
    assert entertainment_control.normalize("زامبی") == "زامبی"
    prio, _ = classify_priority("زامبی")
    assert prio == PRIORITY_COMMAND


def test_registration_timeout_50s_cancels_when_fewer_than_4():
    """اگر ظرف مهلت ثبت‌نام کمتر از ۴ نفر شرکت کنند، بازی لغو و پاک‌سازی می‌شود."""
    async def _run():
        chat_id = -10077001
        bot = FakeBot()
        u1 = FakeUser(101, "علی", username="ali_101")
        u2 = FakeUser(102, "رضا", username="reza_102")
        u3 = FakeUser(103, "سارا")

        start_ev = FakeEvent(chat_id, u1, "زامبی")
        await router.handle(bot, start_ev, chat_id, u1.id, u1, "زامبی", bot.logger)
        assert zombie.is_active(chat_id)
        assert "۵۰ ثانیه" in start_ev.replies[0][0]

        # جایگزینی تایمر با مهلت کوتاه برای تست سریع لغو ثبت‌نام
        session = zombie._STORE.get(chat_id)
        session_id = session["session_id"]

        for u in (u1, u2, u3):
            ev = FakeEvent(chat_id, u, "شرکت")
            await router.handle(bot, ev, chat_id, u.id, u, "شرکت", bot.logger)

        # تلاش تکراری کاربر اول رد می‌شود
        dup_ev = FakeEvent(chat_id, u1, "شرکت")
        await router.handle(bot, dup_ev, chat_id, u1.id, u1, "شرکت", bot.logger)
        assert any("قبلاً ثبت‌نام" in text for text, _ in dup_ev.replies)

        aborted = []

        async def on_abort():
            aborted.append(True)

        await zombie.run_game(
            chat_id, session_id,
            {"on_abort": on_abort, "on_roles": lambda a: None},
            logger=bot.logger,
            join_seconds=0.08,
            bite_seconds=0.08,
        )
        assert aborted == [True]
        assert not zombie.is_active(chat_id)

    asyncio.run(_run())


def test_private_roles_and_zombie_prompt_formatting():
    """نقش‌ها و پیام انتخاب هدف فقط در پیوی ارسال می‌شوند و قالب‌بندی کوت واقعی و Bold دارند."""
    async def _run():
        chat_id = -10077002
        client = FakeClient()
        bot = FakeBot(client=client)

        players = [
            FakeUser(201, "امین", username="amin_user"),
            FakeUser(202, "بهرام", last_name="راد"),       # بدون یوزرنیم -> نام نمایشی
            FakeUser(203, "پریسا", username="parisa_p"),
            FakeUser(204, "تینا", last_name="فرهمند"),     # بدون یوزرنیم -> نام نمایشی
        ]

        session = zombie.start(chat_id, logger=bot.logger)
        for p in players:
            res, _ = zombie.join(chat_id, p.id, p, bot.logger)
            assert res == "joined"

        assignment = zombie.assign_roles(chat_id, logger=bot.logger)
        assert set(assignment["roles"].values()) == set(zombie.ALL_ROLES)

        ok = await zombie.deliver_roles_and_prompt(
            client, chat_id, assignment, logger=bot.logger
        )
        assert ok is True
        # هیچ پیامی به گروه ارسال نشده است؛ هر ۴ پیام در پیوی خصوصی است
        assert len(client.group_messages) == 0
        assert len(client.dm_messages) == 4

        z_player = assignment["by_role"][zombie.ROLE_ZOMBIE]
        z_dms = [m for m in client.dm_messages if m[0] == z_player["user_id"]]
        assert len(z_dms) == 1
        _, z_text, z_entities = z_dms[0]

        expected_first_line = f"🧟‍♂ بازیکن زامبی انتخاب شد : {z_player['name']}"
        assert z_text.startswith(expected_first_line)
        assert "کاربر یک:" in z_text
        assert "کاربر دو:" in z_text
        assert "کاربر سه:" in z_text
        assert "یک نفر انتخاب کن؛ مهلت ۱۷ ثانیه" in z_text

        # بررسی کوت واقعی سروش پلاس روی خط اول و Bold روی ادامهٔ پیام
        quotes = [e for e in z_entities if isinstance(e, MessageEntityBlockquote)]
        bolds = [e for e in z_entities if isinstance(e, MessageEntityBold)]
        assert len(quotes) == 1
        assert _slice_u16(z_text, quotes[0].offset, quotes[0].length) == expected_first_line
        assert len(bolds) == 1
        bold_body = _slice_u16(z_text, bolds[0].offset, bolds[0].length)
        assert "دقت کن از سه کاربر زیر" in bold_body
        assert "یک نفر انتخاب کن؛ مهلت ۱۷ ثانیه" in bold_body

        # سه بازیکن دیگر در لیست گزینه‌های زامبی هستند (با یوزرنیم یا نام نمایشی)
        for role in (zombie.ROLE_HUNTER, zombie.ROLE_SNIPER, zombie.ROLE_BAIT):
            other = assignment["by_role"][role]
            assert other["name"] in bold_body

    asyncio.run(_run())


def test_zombie_win_flow_payouts_public_message_and_apocalypse():
    """انتخاب طعمه توسط زامبی: ۵ سکه نقره به زامبی و ۵ سکه نقره به طعمه، پیام عمومی در گروه و ثبت گروه آخرالزمان."""
    async def _run():
        chat_id = -10077003
        client = FakeClient()
        bot = FakeBot(client=client)

        players = [
            FakeUser(301, "کاوه", username="kaveh_z"),
            FakeUser(302, "نیما", username="nima_h"),
            FakeUser(303, "سهراب", last_name="سپهری"),
            FakeUser(304, "رویا", username="roya_b"),
        ]

        group_ev = FakeEvent(chat_id, players[0], "زامبی")
        session = zombie.start(chat_id, logger=bot.logger, group_event=group_ev)
        for p in players:
            zombie.join(chat_id, p.id, p, bot.logger)

        assignment = zombie.assign_roles(chat_id, logger=bot.logger)
        await zombie.deliver_roles_and_prompt(client, chat_id, assignment, logger=bot.logger)
        zombie.open_choosing(chat_id, session["session_id"], bot.logger, bite_seconds=17)

        z_player = assignment["by_role"][zombie.ROLE_ZOMBIE]
        bait_player = assignment["by_role"][zombie.ROLE_BAIT]
        hunter_player = assignment["by_role"][zombie.ROLE_HUNTER]
        sniper_player = assignment["by_role"][zombie.ROLE_SNIPER]

        # پیام کاربر غیر زامبی یا متن نامعتبر نادیده گرفته می‌شود
        not_z_res, _ = zombie.select_target(chat_id, hunter_player["user_id"], bait_player["name"])
        assert not_z_res == "not_zombie"
        inv_res, _ = zombie.select_target(chat_id, z_player["user_id"], "یک نام نامعتبر")
        assert inv_res == "invalid_target"
        # انتخاب خود زامبی هم نامعتبر است
        self_res, _ = zombie.select_target(chat_id, z_player["user_id"], z_player["name"])
        assert self_res == "invalid_target"

        # انتخاب معتبر طعمه توسط زامبی در پیوی خصوصی
        dm_count_before = len(client.dm_messages)
        consumed = await router.handle_private_zombie_selection(
            bot, z_player["user_id"], bait_player["name"], logger=bot.logger
        )
        assert consumed is True

        # تلاش دوم زامبی برای تغییر هدف پذیرفته نمی‌شود
        second_res, _ = zombie.select_target(chat_id, z_player["user_id"], hunter_player["name"])
        assert second_res == "closed"

        # هیچ پیام نتیجه یا جایزه‌ای در پیوی ارسال نشده است!
        assert len(client.dm_messages) == dm_count_before

        # پیام عمومی برد فقط در گروه ارسال شده است
        assert len(group_ev.replies) == 1
        win_text, win_entities = group_ev.replies[0]
        expected_bold = (
            "🧙‍♂ برنده شدی و درست گاز زدی\n"
            f"طعمه ﴿ {bait_player['name']} ﴾ زامبی شد"
        )
        expected_quote = "۵ سکه نقره دریافت کردی ادامه بده و در گروه آخرالزمان به وجود بیار"
        assert win_text == f"{expected_bold}\n{expected_quote}"

        bolds = [e for e in win_entities if isinstance(e, MessageEntityBold)]
        quotes = [e for e in win_entities if isinstance(e, MessageEntityBlockquote)]
        assert len(bolds) == 1
        assert len(quotes) == 1
        assert _slice_u16(win_text, bolds[0].offset, bolds[0].length) == expected_bold
        assert _slice_u16(win_text, quotes[0].offset, quotes[0].length) == expected_quote

        # بررسی دقیق سکه‌ها در اقتصاد: زامبی ۵ نقره، طعمه ۵ نقره، شکارچی و تک‌تیرانداز ۰
        assert economy.get_balance(chat_id, z_player["user_id"])[economy.SILVER] == 5
        assert economy.get_balance(chat_id, bait_player["user_id"])[economy.SILVER] == 5
        assert economy.get_balance(chat_id, hunter_player["user_id"])[economy.BRONZE] == 0
        assert economy.get_balance(chat_id, hunter_player["user_id"])[economy.SILVER] == 0
        assert economy.get_balance(chat_id, sniper_player["user_id"])[economy.BRONZE] == 0
        assert economy.get_balance(chat_id, sniper_player["user_id"])[economy.SILVER] == 0

        # بررسی ثبت مرحلهٔ «گروه آخرالزمان»
        apoc = zombie.get_apocalypse_state(chat_id)
        assert apoc is not None
        assert apoc["chat_id"] == chat_id
        assert apoc["stage"] == 1
        assert apoc["alpha_zombie"]["user_id"] == z_player["user_id"]
        assert apoc["bitten_bait"]["user_id"] == bait_player["user_id"]
        team_ids = {m["user_id"] for m in apoc["zombie_team"]}
        assert team_ids == {z_player["user_id"], bait_player["user_id"]}

        # دستور «گروه آخرالزمان» در گروه وضعیت را نمایش می‌دهد
        apoc_ev = FakeEvent(chat_id, players[0], "گروه آخرالزمان")
        await router.handle(bot, apoc_ev, chat_id, players[0].id, players[0], "گروه آخرالزمان", bot.logger)
        assert len(apoc_ev.replies) == 1
        assert "وضعیت گروه آخرالزمان" in apoc_ev.replies[0][0]
        assert z_player["name"] in apoc_ev.replies[0][0]
        assert bait_player["name"] in apoc_ev.replies[0][0]

    asyncio.run(_run())


def test_zombie_loss_flow_payouts_and_public_bold_message():
    """انتخاب شکارچی یا تک‌تیرانداز: باخت زامبی، ۷ سکه برنز به شکارچی و ۷ سکه برنز به تک‌تیرانداز، پیام Bold در گروه."""
    async def _run():
        chat_id = -10077004
        client = FakeClient()
        bot = FakeBot(client=client)

        players = [
            FakeUser(401, "مهدی", username="mehdi_1"),
            FakeUser(402, "سعید", username="saeed_2"),
            FakeUser(403, "نادیا", last_name="کوهی"),
            FakeUser(404, "فرهاد", username="farhad_4"),
        ]

        group_ev = FakeEvent(chat_id, players[0], "زامبی")
        session = zombie.start(chat_id, logger=bot.logger, group_event=group_ev)
        for p in players:
            zombie.join(chat_id, p.id, p, bot.logger)

        assignment = zombie.assign_roles(chat_id, logger=bot.logger)
        await zombie.deliver_roles_and_prompt(client, chat_id, assignment, logger=bot.logger)
        zombie.open_choosing(chat_id, session["session_id"], bot.logger, bite_seconds=17)

        z_player = assignment["by_role"][zombie.ROLE_ZOMBIE]
        bait_player = assignment["by_role"][zombie.ROLE_BAIT]
        hunter_player = assignment["by_role"][zombie.ROLE_HUNTER]
        sniper_player = assignment["by_role"][zombie.ROLE_SNIPER]

        dm_count_before = len(client.dm_messages)
        # زامبی به اشتباه تک‌تیرانداز را انتخاب می‌کند
        consumed = await router.handle_private_zombie_selection(
            bot, z_player["user_id"], sniper_player["name"], logger=bot.logger
        )
        assert consumed is True
        assert len(client.dm_messages) == dm_count_before

        assert len(group_ev.replies) == 1
        loss_text, loss_entities = group_ev.replies[0]
        assert "اشتباه حدس زدی و کشته شدی 🧌" in loss_text
        assert "به دو بازیکن شکارچی و تک‌تیرانداز ۷ سکه برنز اضافه شد" in loss_text
        assert hunter_player["name"] in loss_text
        assert sniper_player["name"] in loss_text

        # کل متن پیام باخت Bold است
        bolds = [e for e in loss_entities if isinstance(e, MessageEntityBold)]
        assert len(bolds) == 1
        assert _slice_u16(loss_text, bolds[0].offset, bolds[0].length) == loss_text

        # بررسی دقیق سکه‌ها: شکارچی ۷ برنز، تک‌تیرانداز ۷ برنز، زامبی و طعمه ۰
        assert economy.get_balance(chat_id, hunter_player["user_id"])[economy.BRONZE] == 7
        assert economy.get_balance(chat_id, sniper_player["user_id"])[economy.BRONZE] == 7
        assert economy.get_balance(chat_id, z_player["user_id"])[economy.BRONZE] == 0
        assert economy.get_balance(chat_id, z_player["user_id"])[economy.SILVER] == 0
        assert economy.get_balance(chat_id, bait_player["user_id"])[economy.BRONZE] == 0
        assert economy.get_balance(chat_id, bait_player["user_id"])[economy.SILVER] == 0

    asyncio.run(_run())


def test_zombie_17s_selection_timeout_no_reward():
    """اگر زامبی ظرف ۱۷ ثانیه انتخابی انجام ندهد، بازی بدون جایزه تمام و پاک‌سازی می‌شود."""
    async def _run():
        chat_id = -10077005
        client = FakeClient()
        bot = FakeBot(client=client)

        players = [
            FakeUser(501, "آرش", username="arash_1"),
            FakeUser(502, "بابک", username="babak_2"),
            FakeUser(503, "کیوان", username="keyvan_3"),
            FakeUser(504, "دارا", username="dara_4"),
        ]

        session = zombie.start(chat_id, logger=bot.logger)
        for p in players:
            zombie.join(chat_id, p.id, p, bot.logger)

        timed_out_calls = []

        async def on_roles(assignment):
            return await zombie.deliver_roles_and_prompt(
                client, chat_id, assignment, logger=bot.logger
            )

        async def on_timeout(info):
            timed_out_calls.append(info)

        await zombie.run_game(
            chat_id,
            session["session_id"],
            {
                "on_abort": lambda: None,
                "on_roles": on_roles,
                "on_timeout": on_timeout,
            },
            logger=bot.logger,
            join_seconds=0.05,
            bite_seconds=0.08,
        )

        assert len(timed_out_calls) == 1
        assert not zombie.is_active(chat_id)
        for p in players:
            bal = economy.get_balance(chat_id, p.id)
            assert bal[economy.BRONZE] == 0
            assert bal[economy.SILVER] == 0

    asyncio.run(_run())


def test_dm_failure_cleans_up_game_state():
    """در صورت خطا در ارسال پیام خصوصی نقش‌ها، وضعیت بازی پاک‌سازی می‌شود."""
    async def _run():
        chat_id = -10077006
        # کاربر ۶۰۳ پیوی ربات را بسته است
        client = FakeClient(fail_users={603})
        bot = FakeBot(client=client)

        players = [
            FakeUser(601, "نفر اول", username="u601"),
            FakeUser(602, "نفر دوم", username="u602"),
            FakeUser(603, "نفر سوم", username="u603"),
            FakeUser(604, "نفر چهارم", username="u604"),
        ]

        session = zombie.start(chat_id, logger=bot.logger)
        for p in players:
            zombie.join(chat_id, p.id, p, bot.logger)

        dm_failed_called = []

        async def on_roles(assignment):
            return await zombie.deliver_roles_and_prompt(
                client, chat_id, assignment, logger=bot.logger
            )

        async def on_dm_failed():
            dm_failed_called.append(True)

        await zombie.run_game(
            chat_id,
            session["session_id"],
            {
                "on_abort": lambda: None,
                "on_dm_failed": on_dm_failed,
                "on_roles": on_roles,
            },
            logger=bot.logger,
            join_seconds=0.05,
            bite_seconds=0.05,
        )
        assert dm_failed_called == [True]
        assert not zombie.is_active(chat_id)

    asyncio.run(_run())


def test_game_list_includes_speed_bomb_and_zombie_with_bold():
    """در دستور «لیست بازی»، بمب ساعتی و زامبی با متن دقیق و Bold نمایش داده می‌شوند."""
    from tests.test_speed_bomb import _make_fake_bot, FakeReplyEvent

    async def _run():
        chat_id = -10077007
        group_storage.activate_group(chat_id, "گروه تست لیست بازی")
        bot = _make_fake_bot()
        user = FakeUser(701, "کاربر", username="tester701")
        ev = FakeReplyEvent(chat_id, user, "لیست بازی")

        await handle_new_message(bot, ev)
        assert len(ev.replies) == 1
        text, kwargs = ev.replies[0]
        entities = kwargs.get("formatting_entities") or []

        assert "🎉بمب ساعتی\nزودتر از بقیه کلمه رو بنویس و برنده شو" in text
        assert "🧟‍♀زامبی\nدرست گاز بزن تا زنده بمونی و همه رو زامبی کنی" in text

        bold_slices = [
            _slice_u16(text, e.offset, e.length)
            for e in entities
            if isinstance(e, MessageEntityBold)
        ]
        assert "بمب ساعتی" in bold_slices
        assert "زامبی" in bold_slices

    asyncio.run(_run())


def test_multi_group_isolation():
    """اجرای هم‌زمان بازی زامبی در دو گروه بدون تداخل وضعیت یا جوایز."""
    async def _run():
        chat_a = -10077010
        chat_b = -10077020
        client = FakeClient()
        bot = FakeBot(client=client)

        group_a_players = [
            FakeUser(801, "الف۱", username="a801"),
            FakeUser(802, "الف۲", username="a802"),
            FakeUser(803, "الف۳", username="a803"),
            FakeUser(804, "الف۴", username="a804"),
        ]
        group_b_players = [
            FakeUser(901, "ب۱", username="b901"),
            FakeUser(902, "ب۲", username="b902"),
            FakeUser(903, "ب۳", username="b903"),
            FakeUser(904, "ب۴", username="b904"),
        ]

        ev_a = FakeEvent(chat_a, group_a_players[0], "زامبی")
        ev_b = FakeEvent(chat_b, group_b_players[0], "زامبی")
        s_a = zombie.start(chat_a, logger=bot.logger, group_event=ev_a)
        s_b = zombie.start(chat_b, logger=bot.logger, group_event=ev_b)
        assert zombie.is_active(chat_a) and zombie.is_active(chat_b)

        for p in group_a_players:
            zombie.join(chat_a, p.id, p, bot.logger)
        for p in group_b_players:
            zombie.join(chat_b, p.id, p, bot.logger)

        assign_a = zombie.assign_roles(chat_a, logger=bot.logger)
        assign_b = zombie.assign_roles(chat_b, logger=bot.logger)
        await zombie.deliver_roles_and_prompt(client, chat_a, assign_a, logger=bot.logger)
        await zombie.deliver_roles_and_prompt(client, chat_b, assign_b, logger=bot.logger)
        zombie.open_choosing(chat_a, s_a["session_id"], bot.logger, bite_seconds=17)
        zombie.open_choosing(chat_b, s_b["session_id"], bot.logger, bite_seconds=17)

        # در گروه A زامبی طعمه را گاز می‌زند (برد زامبی)
        za = assign_a["by_role"][zombie.ROLE_ZOMBIE]
        ba = assign_a["by_role"][zombie.ROLE_BAIT]
        await router.handle_private_zombie_selection(
            bot, za["user_id"], ba["name"], logger=bot.logger
        )
        assert not zombie.is_active(chat_a)
        assert zombie.is_active(chat_b)

        # در گروه B زامبی شکارچی را گاز می‌زند (باخت زامبی)
        zb = assign_b["by_role"][zombie.ROLE_ZOMBIE]
        hb = assign_b["by_role"][zombie.ROLE_HUNTER]
        sb = assign_b["by_role"][zombie.ROLE_SNIPER]
        await router.handle_private_zombie_selection(
            bot, zb["user_id"], hb["name"], logger=bot.logger
        )
        assert not zombie.is_active(chat_b)

        assert economy.get_balance(chat_a, za["user_id"])[economy.SILVER] == 5
        assert economy.get_balance(chat_a, ba["user_id"])[economy.SILVER] == 5
        assert economy.get_balance(chat_b, hb["user_id"])[economy.BRONZE] == 7
        assert economy.get_balance(chat_b, sb["user_id"])[economy.BRONZE] == 7
        assert zombie.get_apocalypse_state(chat_a) is not None
        assert zombie.get_apocalypse_state(chat_b) is None

    asyncio.run(_run())
