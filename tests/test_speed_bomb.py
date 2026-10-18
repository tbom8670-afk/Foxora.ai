# -*- coding: utf-8 -*-
"""تست‌های واحد و یکپارچه برای بازی «بمب سرعت روباه» (Speed Bomb)."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import economy
from economy import rewards, storage as eco_storage
from handlers.message_handler import handle_new_message
from modules import (
    entertainment_control,
    group_storage,
    speed_bomb,
)

MessageEntityBold = speed_bomb.MessageEntityBold


class FakeReplyEvent:
    def __init__(self, chat_id, sender, text, msg_id=1, msg_date=None):
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, title="گروه بمب سرعت")
        self.sender = sender
        self.sender_id = getattr(sender, "id", None)
        self.is_private = False
        self.out = False
        self.reply_to = None
        self.message = SimpleNamespace(
            id=msg_id,
            message=text,
            caption=None,
            media=None,
            fwd_from=None,
            reply_to=None,
            date=msg_date,
        )
        self.replies = []

    async def get_chat(self):
        return self.chat

    async def get_sender(self):
        return self.sender

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(id=9000 + len(self.replies))


@pytest.fixture(autouse=True)
def _clean_speed_bomb_and_economy(tmp_path, monkeypatch):
    speed_bomb.reset_all()
    eco_storage.use_file(tmp_path / "economy.json")
    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    group_storage._cache = None
    group_storage._cache_mtime = None
    monkeypatch.setattr(entertainment_control, "FILE", tmp_path / "entertainment_mode.json")
    entertainment_control._cache = None
    entertainment_control._cache_mtime = None
    yield
    speed_bomb.reset_all()


def _make_fake_bot():
    spam_lock = {}
    return SimpleNamespace(
        client=SimpleNamespace(),
        bot_account_id=999999,
        punished_users=set(),
        spam_lock=spam_lock,
        repeat_messages={},
        flood_messages={},
        user_messages={},
        spam_burst_users=set(),
        spam_burst_messages={},
        spammer_messages={},
        rejoin_spam_state={},
        forward_spam_counts={},
        is_spam_locked=lambda key: bool(spam_lock.get(key)),
        set_spam_lock=lambda key, val=True: spam_lock.__setitem__(key, val),
        clear_spam_lock=lambda key: spam_lock.pop(key, None),
        touch_temporary_state=lambda *_a, **_kw: None,
        debug_message_log=lambda *_a, **_kw: None,
        delete_notice_lock={},
        bot_sent_messages=[],
        reply_input_peer_cache={},
        group_timer_tasks={},
        outgoing_sender=None,
        config_manager=SimpleNamespace(
            get=lambda _k, default=None: default,
            whitelisted_ids=set(),
            banned_words=set(),
        ),
        detector=SimpleNamespace(
            is_spam=lambda *_a, **_kw: (False, None),
            has_public_username=lambda *_a, **_kw: False,
            check_banned_words=lambda *_a, **_kw: (False, None),
            check_spam_score=lambda *_a, **_kw: (0, []),
            analyze=lambda *_a, **_kw: {"is_spam": False, "score": 0, "reasons": [], "reason_str": ""},
        ),
        flood_detector=SimpleNamespace(check_flood=lambda *_a, **_kw: (False, [])),
        activity_tracker=SimpleNamespace(track_message=lambda *_a, **_kw: None),
        message_tracker=SimpleNamespace(
            track_message=lambda *_a, **_kw: None,
            get_user_messages=lambda *_a, **_kw: [],
            clear_user_messages=lambda *_a, **_kw: None,
        ),
        tracker=SimpleNamespace(
            get_count=lambda *_a: 0,
            add_violation=lambda *_a: 1,
            is_banned=lambda *_a: False,
            is_muted=lambda *_a: False,
            banned_users={},
            muted_users={},
        ),
        logger=SimpleNamespace(
            log_info=lambda *_a, **_kw: None,
            log_error=lambda *_a, **_kw: None,
            log_warning=lambda *_a, **_kw: None,
            log_deleted_message=lambda **_kw: None,
        ),
        check_group_word_commands=lambda *_a, **_kw: asyncio.sleep(0, result=False),
    )


def test_templates_and_bold_entities():
    stage1 = {
        "stage": 1,
        "category": "میوه",
        "letter": "ز",
    }
    msg = speed_bomb.format_stage_message(stage1)
    expected = (
        "🦊 روباه | بمب سرعت\n\n"
        "💣 دور اول شروع شد!\n\n"
        "🍎 یک میوه با حرف «ز» بگو!\n\n"
        "⏳ فرصت: ۱۵ ثانیه\n\n"
        "هرکس زودتر جواب صحیح بده، برنده است!"
    )
    assert msg == expected
    ents = speed_bomb.bold_entities_for(msg)
    assert len(ents) == 1
    assert isinstance(ents[0], MessageEntityBold)
    assert ents[0].offset == 0
    assert ents[0].length == speed_bomb.u16_length(msg)

    stage2_country = {
        "stage": 2,
        "category": "کشور",
        "letter": "د",
    }
    msg2 = speed_bomb.format_stage_message(stage2_country)
    assert "💣 دور دوم شروع شد!" in msg2
    assert "🌍 نام یک کشور با حرف «د» بگو!" in msg2

    win_msg = speed_bomb.format_winner_message("علی")
    assert win_msg == (
        "🦊 روباه: علی برنده شد! 🎉\n\n"
        "چون جواب صحیحش زودتر از بقیه ثبت شد"
    )
    assert speed_bomb.NO_WINNER_STAGE_MESSAGE == "جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود"


def test_answer_validation_and_arabic_persian_normalization():
    # پاسخ صحیح برای «میوه با ز» -> زیتون
    ok, norm = speed_bomb.validate_answer("میوه", "ز", "زیتون")
    assert ok is True
    assert norm == "زیتون"

    # حروف عربی ي/ك، فاصله‌های اضافی و کشیده‌نویسی
    ok_ar, norm_ar = speed_bomb.validate_answer("میوه", "ز", "  زيتونـ  ")
    assert ok_ar is True
    assert norm_ar == "زیتون"

    ok_k, norm_k = speed_bomb.validate_answer("کشور", "د", "  دانمارك ")
    assert ok_k is True
    assert norm_k == "دانمارک"

    # نیم‌فاصله و بدون فاصله در کلمات ترکیبی
    ok_tf1, _ = speed_bomb.validate_answer("میوه", "ت", "توت‌فرنگی")
    ok_tf2, _ = speed_bomb.validate_answer("میوه", "ت", "توت فرنگی")
    ok_tf3, _ = speed_bomb.validate_answer("میوه", "ت", "توتفرنگی")
    assert ok_tf1 and ok_tf2 and ok_tf3

    # پاسخ نامعتبر یا ساختگی با همان حرف ز نباید پذیرفته شود
    assert speed_bomb.validate_answer("میوه", "ز", "زنگوله")[0] is False
    assert speed_bomb.validate_answer("میوه", "ز", "زرتشتک")[0] is False
    assert speed_bomb.validate_answer("میوه", "ز", "سلام بچه‌ها چطورید")[0] is False
    assert speed_bomb.validate_answer("میوه", "ز", "")[0] is False

    # پاسخ معتبر در دسته، اما با حرف شروع اشتباه نباید پذیرفته شود
    assert speed_bomb.validate_answer("میوه", "ز", "سیب")[0] is False
    assert speed_bomb.validate_answer("کشور", "د", "ایران")[0] is False

    # قابلیت توسعهٔ فهرست کلمات
    speed_bomb.register_words("میوه", ["زغال اخته کوهی"])
    assert speed_bomb.validate_answer("میوه", "ز", "زغال اخته کوهی")[0] is True


def test_three_stages_first_valid_wins_and_economy_pays_3_bronze_once():
    chat_id = -10088001
    user_wrong = SimpleNamespace(id=101, first_name="کاربر", last_name="اشتباه", username="u101")
    user_ali = SimpleNamespace(id=102, first_name="علی", last_name="", username="ali102")
    user_sara = SimpleNamespace(id=103, first_name="", last_name="", username="sara_user")

    assert rewards.amount_for("speed_bomb") == 3
    assert rewards.coin_for("speed_bomb") == economy.BRONZE

    # شروع بازی با ۳ جفت مشخص برای تست قطعی
    s1 = speed_bomb.start_game(
        chat_id,
        stage_pairs=[("میوه", "ز"), ("کشور", "د"), ("حیوان", "ش")],
    )
    assert s1 is not None
    assert s1["stage"] == 1
    assert speed_bomb.is_active(chat_id) is True

    # اجرای دوبارهٔ دستور شروع در حین بازی نباید بازی جدیدی بسازد
    assert speed_bomb.start_game(chat_id) is None

    # ۱) پیام قدیمی‌تر از شروع مرحله نباید پذیرفته شود
    old_res = speed_bomb.submit_answer(
        chat_id,
        user_ali.id,
        "زیتون",
        sender=user_ali,
        received_at=s1["started_at"] - 0.5,
    )
    assert old_res is None
    assert economy.get_balance(chat_id, user_ali.id)[economy.BRONZE] == 0

    # ۲) پیام نامعتبر، حرف اشتباه یا ساختگی برنده نمی‌شود و سکه‌ای نمی‌گیرد
    assert speed_bomb.submit_answer(chat_id, user_wrong.id, "زنگوله", sender=user_wrong) is None
    assert speed_bomb.submit_answer(chat_id, user_wrong.id, "سیب", sender=user_wrong) is None
    assert speed_bomb.submit_answer(chat_id, user_wrong.id, "سلام", sender=user_wrong) is None
    assert economy.get_balance(chat_id, user_wrong.id)[economy.BRONZE] == 0

    # ۳) اولین پاسخ صحیح در مرحلهٔ اول (علی -> زیتون) برنده می‌شود و ۳ سکه برنز می‌گیرد
    res1 = speed_bomb.submit_answer(
        chat_id,
        user_ali.id,
        "زیتون",
        sender=user_ali,
        message_id=501,
    )
    assert res1 is not None
    assert res1["winner_id"] == user_ali.id
    assert res1["reward"] == 3
    assert res1["winner_message"] == (
        "🦊 روباه: علی برنده شد! 🎉\n\n"
        "چون جواب صحیحش زودتر از بقیه ثبت شد"
    )
    assert economy.get_balance(chat_id, user_ali.id)[economy.BRONZE] == 3
    assert res1["finished"] is False
    assert res1["next_stage"]["stage"] == 2
    assert res1["next_stage"]["category"] == "کشور"
    assert res1["next_stage"]["letter"] == "د"

    # ارسال مجدد همان پیام یا پاسخ دوم به مرحلهٔ اول، جایزهٔ تکراری نمی‌دهد
    dup_res = speed_bomb.submit_answer(
        chat_id,
        user_ali.id,
        "زیتون",
        sender=user_ali,
        message_id=501,
    )
    assert dup_res is None
    assert economy.get_balance(chat_id, user_ali.id)[economy.BRONZE] == 3

    # ۴) مرحلهٔ دوم (کشور با د): سارا بدون نام نمایشی، با یوزرنیم برنده می‌شود
    res2 = speed_bomb.submit_answer(
        chat_id,
        user_sara.id,
        "دانمارک",
        sender=user_sara,
        message_id=502,
    )
    assert res2 is not None
    assert res2["winner_id"] == user_sara.id
    assert res2["winner_name"] == "@sara_user"
    assert "🦊 روباه: @sara_user برنده شد! 🎉" in res2["winner_message"]
    assert economy.get_balance(chat_id, user_sara.id)[economy.BRONZE] == 3
    assert res2["finished"] is False
    assert res2["next_stage"]["stage"] == 3

    # ۵) مرحلهٔ سوم (حیوان با ش): علی دوباره برنده می‌شود -> مجموع علی = ۶ برنز، پایان بازی
    res3 = speed_bomb.submit_answer(
        chat_id,
        user_ali.id,
        "شیر",
        sender=user_ali,
        message_id=503,
    )
    assert res3 is not None
    assert res3["finished"] is True
    assert speed_bomb.is_active(chat_id) is False
    assert economy.get_balance(chat_id, user_ali.id)[economy.BRONZE] == 6
    assert economy.get_balance(chat_id, user_sara.id)[economy.BRONZE] == 3
    assert "علی" in res3["summary_message"]
    assert "@sara_user" in res3["summary_message"]
    assert "۶ سکه برنز" in res3["summary_message"]
    assert "۳ سکه برنز" in res3["summary_message"]


def test_timeout_progression_and_group_isolation():
    chat_a = -10088002
    chat_b = -10088003
    user_b = SimpleNamespace(id=201, first_name="مریم", last_name="", username="maryam")

    s_a1 = speed_bomb.start_game(
        chat_a,
        stage_pairs=[("میوه", "ز"), ("کشور", "د"), ("شهر", "ت")],
    )
    s_b1 = speed_bomb.start_game(
        chat_b,
        stage_pairs=[("حیوان", "ب"), ("غذا", "پ"), ("اسم", "م")],
    )
    assert s_a1 is not None and s_b1 is not None

    # پیام در گروه B روی گروه A اثر ندارد
    res_b = speed_bomb.submit_answer(chat_b, user_b.id, "ببر", sender=user_b)
    assert res_b is not None
    assert speed_bomb.current_stage(chat_a)["stage"] == 1
    assert speed_bomb.current_stage(chat_b)["stage"] == 2
    assert economy.get_balance(chat_a, user_b.id)[economy.BRONZE] == 0
    assert economy.get_balance(chat_b, user_b.id)[economy.BRONZE] == 3

    # پایان مهلت ۱۵ ثانیه‌ای در گروه A برای هر ۳ مرحله بدون برنده
    t1 = speed_bomb.timeout_stage(chat_a, s_a1["token"])
    assert t1 is not None
    assert t1["timeout_message"] == "جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود"
    assert t1["finished"] is False
    assert t1["next_stage"]["stage"] == 2

    t2 = speed_bomb.timeout_stage(chat_a, t1["next_stage"]["token"])
    assert t2 is not None
    assert t2["timeout_message"] == "جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود"
    assert t2["finished"] is False
    assert t2["next_stage"]["stage"] == 3

    t3 = speed_bomb.timeout_stage(chat_a, t2["next_stage"]["token"])
    assert t3 is not None
    assert t3["timeout_message"] == "جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود"
    assert t3["finished"] is True
    assert speed_bomb.is_active(chat_a) is False
    assert "هیچ پاسخ صحیحی ثبت نشد" in t3["summary_message"]


def test_end_to_end_message_handler_with_both_commands_and_timer(monkeypatch):
    chat_id = -10088004
    group_storage.activate_group(chat_id, "گروه بمب سرعت")
    fake_bot = _make_fake_bot()

    ali = SimpleNamespace(id=301, first_name="علی", last_name="", username="ali301")
    reza = SimpleNamespace(id=302, first_name="رضا", last_name="", username="reza302")

    # برای تست سریع تایمر، زمان هر مرحله را ۰.۰۸ ثانیه قرار می‌دهیم
    monkeypatch.setattr(speed_bomb, "STAGE_SECONDS", 0.08)

    async def _scenario():
        # ۱. شروع بازی با دستور «بمب ساعتی»
        ev_start = FakeReplyEvent(chat_id, ali, "بمب ساعتی", msg_id=1001)
        await handle_new_message(fake_bot, ev_start)
        assert len(ev_start.replies) == 1
        start_text, start_kw = ev_start.replies[0]
        assert "🦊 روباه | بمب سرعت" in start_text
        assert "💣 دور اول شروع شد!" in start_text
        assert any(isinstance(e, MessageEntityBold) for e in start_kw.get("formatting_entities", []))

        # ۲. دستور مجدد «بمب سرعت» در حین بازی، بازی جدیدی شروع نمی‌کند
        ev_dup = FakeReplyEvent(chat_id, reza, "بمب سرعت", msg_id=1002)
        await handle_new_message(fake_bot, ev_dup)
        assert len(ev_dup.replies) == 1
        assert "در جریان است" in ev_dup.replies[0][0]

        # ۳. در مرحلهٔ اول یک پاسخ معتبر از بانک همان موضوع/حرف ارسال می‌کنیم
        st1 = speed_bomb.current_stage(chat_id)
        valid_word_1 = next(iter(speed_bomb.get_valid_words(st1["category"], st1["letter"])))
        ev_ans1 = FakeReplyEvent(
            chat_id,
            ali,
            valid_word_1,
            msg_id=1003,
            msg_date=datetime.now(timezone.utc),
        )
        await handle_new_message(fake_bot, ev_ans1)
        # باید پیام برنده شدن علی + پیام شروع دور دوم ارسال شده باشد
        assert len(ev_ans1.replies) == 2
        assert "🦊 روباه: علی برنده شد! 🎉" in ev_ans1.replies[0][0]
        assert "💣 دور دوم شروع شد!" in ev_ans1.replies[1][0]
        assert economy.get_balance(chat_id, ali.id)[economy.BRONZE] == 3

        # ۴. اجازه می‌دهیم مهلت مرحلهٔ دوم و سوم منقضی شود تا تایمر خودکار بازی را تمام کند
        await asyncio.sleep(0.25)
        assert speed_bomb.is_active(chat_id) is False
        all_texts = [t for t, _ in ev_ans1.replies]
        assert any("جواب صحیح ثبت. نشد مرحله بعد شروع می‌شود" in t for t in all_texts)
        assert any("💣 دور سوم شروع شد!" in t for t in all_texts)
        assert any("بازی «بمب سرعت روباه» تمام شد" in t for t in all_texts)
        assert economy.get_balance(chat_id, ali.id)[economy.BRONZE] == 3

    asyncio.run(_scenario())
