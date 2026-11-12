import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import economy
from modules import fox_game_tokens, group_storage
from handlers.message_handler import handle_new_message


_NEXT_MESSAGE_ID = [2000]


class MockEvent:
    def __init__(self, text, chat_id=5001, user_id=9001, first_name="علی",
                 message_id=None):
        self.raw_text = text
        # شناسهٔ یکتا: هر پیامِ تازه شناسهٔ تازه دارد (مثل سرور واقعی).
        if message_id is None:
            _NEXT_MESSAGE_ID[0] += 1
            message_id = _NEXT_MESSAGE_ID[0]
        self.message = SimpleNamespace(message=text, id=message_id,
                                       entities=None)
        self.chat_id = chat_id
        self.sender_id = user_id
        self.is_private = False
        self.sender = SimpleNamespace(
            id=user_id,
            first_name=first_name,
            last_name=None,
            username="ali_test",
        )
        self.replies = []

    async def reply(self, msg, *args, **kwargs):
        self.replies.append(msg)
        return SimpleNamespace(id=len(self.replies))

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return SimpleNamespace(id=self.chat_id)


def test_token_creation_and_device_binding():
    """Verify exclusive token creation, group binding, and device binding protection."""
    chat_id = "test_chat_1"
    user_id = "user_8899"

    # Activate group first
    group_storage.activate_group(chat_id, "گروه تستی شماره ۱")

    # Create token
    token = fox_game_tokens.create_token(chat_id, user_id, "کاربر تستی")
    assert token is not None
    assert len(token) >= 32

    # First device validates token -> Success and bound
    device_a = "device_fingerprint_alpha"
    valid, record, err = fox_game_tokens.validate_token(token, device_a)
    assert valid is True
    assert record["user_id"] == user_id
    assert record["chat_id"] == chat_id
    assert record["device_id"] == device_a

    # Second device tries to use the same token -> Access denied
    device_b = "device_fingerprint_beta"
    valid_b, record_b, err_b = fox_game_tokens.validate_token(token, device_b)
    assert valid_b is False
    assert "مخصوص کاربر و دستگاه دیگری است" in err_b


def test_token_group_binding_and_deactivation():
    """Verify tokens are strictly tied to active groups and revoked when group is deactivated."""
    chat_id = "test_group_bind_99"
    user_id = "user_bind_11"
    device_id = "dev_bind_alpha"

    # 1. Activate group
    group_storage.activate_group(chat_id, "گروه تست وابستگی")
    token = fox_game_tokens.create_token(chat_id, user_id, "کاربر تست وابستگی")

    # 2. Token should be valid in active group
    valid, record, err = fox_game_tokens.validate_token(token, device_id)
    assert valid is True
    assert record["chat_id"] == chat_id

    # 3. Token cannot be validated against a different expected group
    valid_diff, _, err_diff = fox_game_tokens.validate_token(token, device_id, expected_chat_id="other_group_88")
    assert valid_diff is False
    assert "برای این گروه صادر نشده است" in err_diff

    # 4. Deactivate group -> Token must immediately fail and be revoked
    group_storage.deactivate_group(chat_id, "گروه تست وابستگی")
    valid_after, _, err_after = fox_game_tokens.validate_token(token, device_id)
    assert valid_after is False
    assert "معتبر نیست" in err_after or "فعال نیست" in err_after or "منقضی" in err_after


def test_token_expiration():
    """Verify expired tokens are rejected."""
    chat_id = "test_chat_2"
    user_id = "user_7766"

    group_storage.activate_group(chat_id, "گروه تستی ۲")
    token = fox_game_tokens.create_token(chat_id, user_id, "منقضی")
    # Manually expire
    data = fox_game_tokens._load_json(fox_game_tokens.TOKEN_FILE)
    data[token]["expires_at"] = time.time() - 10
    fox_game_tokens._save_json(fox_game_tokens.TOKEN_FILE, data)

    valid, record, err = fox_game_tokens.validate_token(token, "dev_1")
    assert valid is False
    assert "منقضی" in err or "پایان رسیده" in err


def _reset_site_wallet(chat_id, user_id):
    """Isolate site-command tests from leftover tokens and balances."""
    fox_game_tokens.revoke_group_tokens(chat_id)
    try:
        fox_game_tokens.revoke_group_tokens(economy.chat_key(chat_id))
    except Exception:
        pass
    current = economy.get_balance(chat_id, user_id).get(economy.BRONZE, 0)
    if current > 0:
        economy.remove_bronze(chat_id, user_id, current)


def make_mock_bot():
    return SimpleNamespace(
        client=MagicMock(),
        logger=MagicMock(),
        tracker=MagicMock(get_count=lambda c, u: 0),
        punished_users=set(),
        spam_burst_users=set(),
        spam_lock=set(),
        is_spam_locked=lambda k: False,
        config_manager=SimpleNamespace(get=lambda k, d=None: d),
        detector=SimpleNamespace(
            check_banned_words=lambda *a, **k: (False, None),
        ),
    )


def test_bot_command_site_group_inactive():
    """Verify bot replies with inactive group warning if bot is not activated in group."""
    async def scenario():
        chat_id = 6099
        user_id = 9099

        # Ensure group is inactive
        group_storage.deactivate_group(chat_id, "گروه غیرفعال")

        bot = make_mock_bot()
        event = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id)
        await handle_new_message(bot, event)

        assert len(event.replies) == 1
        assert "روباه در این گروه فعال نیست" in event.replies[0]

    asyncio.run(scenario())


def test_bot_command_site_insufficient_balance():
    """Verify bot replies with insufficient balance warning when bronze < 20 in active group."""
    async def scenario():
        chat_id = 6001
        user_id = 9002

        group_storage.activate_group(chat_id, "گروه تستی ۳")
        _reset_site_wallet(chat_id, user_id)

        bot = make_mock_bot()

        event = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id)
        await handle_new_message(bot, event)

        assert len(event.replies) == 1
        assert "موجودی کافی نیست" in event.replies[0]
        assert "حداقل ۲۰ برنز" in event.replies[0]

    asyncio.run(scenario())


def test_bot_command_site_accepts_exact_twenty_bronze():
    """Exactly 20 bronze must unlock the site, matching the user-facing threshold."""
    async def scenario():
        chat_id = 6310
        user_id = 9310
        group_storage.activate_group(chat_id, "گروه آستانه ۲۰")
        _reset_site_wallet(chat_id, user_id)
        economy.add_bronze(chat_id, user_id, 20, note="شارژ آستانه")

        event = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id)
        await handle_new_message(make_mock_bot(), event)
        assert len(event.replies) == 1
        assert "https://fox-game.aifox-chat.workers.dev/" in event.replies[0]
        assert "توکن اختصاصی" not in event.replies[0]
        assert economy.get_balance(chat_id, user_id).get(economy.BRONZE, 0) == 0

    asyncio.run(scenario())


def test_bot_command_site_finds_wallet_across_chat_id_aliases():
    """Coins stored under one Soroush chat-id spelling must still unlock the site."""
    async def scenario():
        stored_chat = 22770888
        command_chat = -10022770888
        user_id = 9411
        group_storage.activate_group(command_chat, "گروه املای شناسه")
        group_storage.activate_group(stored_chat, "گروه املای شناسه")
        _reset_site_wallet(stored_chat, user_id)
        _reset_site_wallet(command_chat, user_id)
        economy.add_bronze(stored_chat, user_id, 31, note="شارژ املا")

        assert economy.get_balance(command_chat, user_id).get(economy.BRONZE, 0) == 31

        event = MockEvent("سایت بازی", chat_id=command_chat, user_id=str(user_id))
        await handle_new_message(make_mock_bot(), event)
        assert len(event.replies) == 1
        assert "https://fox-game.aifox-chat.workers.dev/" in event.replies[0]
        assert "توکن اختصاصی" not in event.replies[0]
        assert economy.get_balance(stored_chat, user_id).get(economy.BRONZE, 0) == 11

    asyncio.run(scenario())


def test_bot_command_site_charges_twenty_each_time():
    """Each سایت بازی request costs 20 bronze and only returns the site link."""
    async def scenario():
        chat_id = 6320
        user_id = 9320
        group_storage.activate_group(chat_id, "گروه لینک سایت")
        _reset_site_wallet(chat_id, user_id)
        economy.add_bronze(chat_id, user_id, 50, note="شارژ لینک")

        bot = make_mock_bot()
        first = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id)
        await handle_new_message(bot, first)
        second = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id)
        await handle_new_message(bot, second)

        assert "https://fox-game.aifox-chat.workers.dev/" in first.replies[0]
        assert "توکن اختصاصی" not in first.replies[0]
        assert "https://fox-game.aifox-chat.workers.dev/" in second.replies[0]
        assert economy.get_balance(chat_id, user_id).get(economy.BRONZE, 0) == 10

    asyncio.run(scenario())


def test_bot_command_site_successful_deduction_and_link():
    """Verify bot deducts 20 bronze and returns the site link without a token."""
    async def scenario():
        chat_id = 6402
        user_id = 9403

        group_storage.activate_group(chat_id, "گروه تستی ۴")
        _reset_site_wallet(chat_id, user_id)
        economy.add_bronze(chat_id, user_id, 50, note="شارژ تست")

        bot = make_mock_bot()

        event = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id, first_name="رضا")
        await handle_new_message(bot, event)

        assert len(event.replies) == 1
        reply = event.replies[0]
        assert "توکن اختصاصی" not in reply
        assert "https://fox-game.aifox-chat.workers.dev/" in reply
        assert "گروه فعال شده" in reply

        b = economy.get_balance(chat_id, user_id)
        assert b.get(economy.BRONZE, 0) == 30  # 50 - 20 = 30

    asyncio.run(scenario())


def test_user_data_and_leaderboard_persistence_across_new_tokens():
    """Verify that user progress, nickname, wins, and coins persist when new tokens are generated or after group reactivation."""
    unique_suffix = str(int(time.time() * 1000))
    chat_id = f"test_chat_persist_{unique_suffix}"
    user_id = f"user_permanent_{unique_suffix}"
    device_id = f"dev_persist_{unique_suffix}"

    group_storage.activate_group(chat_id, "گروه پایداری داده")

    # 1. Create first token & play
    token1 = fox_game_tokens.create_token(chat_id, user_id, "کاربر تستی")
    fox_game_tokens.update_nickname(token1, "قهرمان همیشگی روباه", device_id)
    fox_game_tokens.record_win(token1, "رمز مخفی", bronze_won=100, silver_won=10, gold_won=5, device_id=device_id)

    # 2. Group gets deactivated (e.g. expired or bot removed) -> Token 1 is revoked
    group_storage.deactivate_group(chat_id, "گروه پایداری داده")
    valid_old, _, _ = fox_game_tokens.validate_token(token1, device_id)
    assert valid_old is False

    # 3. Leaderboard data is STILL preserved
    lb = fox_game_tokens.get_real_leaderboard()
    player = next((p for p in lb if p["user_id"] == user_id), None)
    assert player is not None
    assert player["nickname"] == "قهرمان همیشگی روباه"
    assert player["wins"] == 1
    assert player["gold_won"] == 5

    # 4. Group is reactivated -> User gets a brand new token
    group_storage.activate_group(chat_id, "گروه پایداری داده")
    token2 = fox_game_tokens.create_token(chat_id, user_id, "نام جدید از تلگرام")

    # 5. Token 2 must automatically preserve previous custom nickname and stats
    valid_new, record_new, _ = fox_game_tokens.validate_token(token2, device_id)
    assert valid_new is True
    assert record_new["nickname"] == "قهرمان همیشگی روباه"

    # 6. Additional wins increment existing stats seamlessly
    fox_game_tokens.record_win(token2, "تک‌تیرانداز روباه", bronze_won=2, silver_won=0, gold_won=0, device_id=device_id)
    lb_after = fox_game_tokens.get_real_leaderboard()
    player_after = next((p for p in lb_after if p["user_id"] == user_id), None)
    assert player_after["wins"] == 2
    assert player_after["bronze_won"] == 102
    assert player_after["gold_won"] == 5


def test_nickname_update_and_real_leaderboard():
    """Verify player nickname updates and real leaderboard ranking."""
    chat_id = "test_chat_3"
    user_id = "user_5544"

    group_storage.activate_group(chat_id, "گروه تستی ۵")
    token = fox_game_tokens.create_token(chat_id, user_id, "روباه اولیه")
    device_id = "dev_real_test"

    # 1. Update nickname
    ok, new_name = fox_game_tokens.update_nickname(token, "سلطان روباه ۷۷", device_id)
    assert ok is True
    assert new_name == "سلطان روباه ۷۷"

    # 2. Record game wins
    fox_game_tokens.record_win(token, "رمز مخفی", bronze_won=100, silver_won=0, gold_won=15, device_id=device_id)

    # 3. Check real leaderboard
    lb = fox_game_tokens.get_real_leaderboard()
    assert len(lb) >= 1
    found = next((p for p in lb if p["user_id"] == user_id), None)
    assert found is not None
    assert found["nickname"] == "سلطان روباه ۷۷"
    assert found["wins"] >= 1
    assert found["gold_won"] >= 15
    assert found["bronze_won"] >= 100

@pytest.fixture(autouse=True)
def _isolate_fox_token_store(tmp_path, monkeypatch):
    monkeypatch.setattr(fox_game_tokens, "TOKEN_FILE", tmp_path / "fox_game_tokens.json")
    monkeypatch.setattr(fox_game_tokens, "LEADERBOARD_FILE", tmp_path / "fox_game_leaderboard.json")


def _expire_token(token):
    data = fox_game_tokens._load_json(fox_game_tokens.TOKEN_FILE)
    data[token]["expires_at"] = time.time() - 10
    data[token]["retired"] = False
    fox_game_tokens._save_json(fox_game_tokens.TOKEN_FILE, data)


def test_new_user_gets_token_a():
    chat_id = "cycle_chat"
    user_id = "user_a"
    group_storage.activate_group(chat_id, "گروه چرخه")
    token = fox_game_tokens.create_token(chat_id, user_id, "کاربر آ")
    assert token
    found, record = fox_game_tokens.find_active_token(chat_id, user_id)
    assert found == token
    assert record["user_id"] == user_id
    assert record["created_at"] <= time.time()
    assert record["expires_at"] > time.time()
    assert record["expires_at"] - record["created_at"] == pytest.approx(
        fox_game_tokens.TOKEN_LIFETIME_SECONDS, abs=2
    )


def test_same_user_before_24h_gets_same_token():
    chat_id = "cycle_chat"
    user_id = "user_same"
    group_storage.activate_group(chat_id, "گروه چرخه")
    first = fox_game_tokens.create_token(chat_id, user_id, "یک")
    second = fox_game_tokens.create_token(chat_id, user_id, "دو")
    assert first == second
    data = fox_game_tokens._load_json(fox_game_tokens.TOKEN_FILE)
    live = [
        key for key, rec in data.items()
        if rec.get("user_id") == user_id and not rec.get("retired")
        and float(rec.get("expires_at", 0)) > time.time()
    ]
    assert live == [first]


def test_same_user_after_24h_gets_new_token_not_previous():
    chat_id = "cycle_chat"
    user_id = "user_exp"
    group_storage.activate_group(chat_id, "گروه چرخه")
    first = fox_game_tokens.create_token(chat_id, user_id, "قدیم")
    _expire_token(first)
    second, created = fox_game_tokens.issue_user_token(chat_id, user_id, "جدید")
    assert created is True
    assert second != first
    found, record = fox_game_tokens.find_active_token(chat_id, user_id)
    assert found == second
    assert record["created_at"] <= time.time()
    assert record["expires_at"] - record["created_at"] == pytest.approx(
        fox_game_tokens.TOKEN_LIFETIME_SECONDS, abs=2
    )
    data = fox_game_tokens._load_json(fox_game_tokens.TOKEN_FILE)
    assert first in data
    assert data[first].get("retired") is True


def test_restart_before_24h_still_returns_same_token():
    chat_id = "cycle_chat"
    user_id = "user_restart"
    group_storage.activate_group(chat_id, "گروه چرخه")
    first = fox_game_tokens.create_token(chat_id, user_id, "پایدار")
    # Simulate process restart: no in-memory cache, only the JSON file.
    found, record = fox_game_tokens.find_active_token(chat_id, user_id)
    again = fox_game_tokens.create_token(chat_id, user_id, "پایدار")
    assert found == first
    assert again == first
    assert record["issued_at"] == record["created_at"] or "created_at" in record


def test_restart_after_expiry_assigns_new_token():
    chat_id = "cycle_chat"
    user_id = "user_restart_exp"
    group_storage.activate_group(chat_id, "گروه چرخه")
    first = fox_game_tokens.create_token(chat_id, user_id, "قدیم")
    _expire_token(first)
    found, _ = fox_game_tokens.find_active_token(chat_id, user_id)
    assert found is None
    second = fox_game_tokens.create_token(chat_id, user_id, "بعد ریستارت")
    assert second != first


def test_concurrent_requests_same_user_one_token():
    chat_id = "cycle_chat"
    user_id = "user_race"
    group_storage.activate_group(chat_id, "گروه چرخه")
    barrier = threading.Barrier(8)
    results = []

    def worker():
        barrier.wait()
        results.append(fox_game_tokens.create_token(chat_id, user_id, "همزمان"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1
    data = fox_game_tokens._load_json(fox_game_tokens.TOKEN_FILE)
    live = [
        key for key, rec in data.items()
        if rec.get("user_id") == user_id and not rec.get("retired")
        and float(rec.get("expires_at", 0)) > time.time()
    ]
    assert len(live) == 1


def test_two_users_never_receive_the_same_token():
    chat_id = "cycle_chat"
    group_storage.activate_group(chat_id, "گروه چرخه")
    barrier = threading.Barrier(6)
    results = []

    def worker(uid):
        barrier.wait()
        results.append((uid, fox_game_tokens.create_token(chat_id, uid, uid)))

    threads = [
        threading.Thread(target=worker, args=(f"user_{i}",))
        for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tokens = [token for _uid, token in results]
    assert len(tokens) == 6
    assert len(set(tokens)) == 6


def test_empty_pool_does_not_mint_or_reuse(monkeypatch):
    chat_id = "cycle_chat"
    group_storage.activate_group(chat_id, "گروه چرخه")
    monkeypatch.setattr(fox_game_tokens, "load_official_tokens", lambda: ["ONLY-TOKEN-1"])
    first = fox_game_tokens.create_token(chat_id, "owner", "مالک")
    assert first == "ONLY-TOKEN-1"
    with pytest.raises(ValueError, match="فعلاً توکن جدیدی موجود نیست"):
        fox_game_tokens.create_token(chat_id, "other", "دیگر")
    found, _ = fox_game_tokens.find_active_token(chat_id, "other")
    assert found is None
    still, _ = fox_game_tokens.find_active_token(chat_id, "owner")
    assert still == first


def test_expired_token_not_returned_to_same_user(monkeypatch):
    chat_id = "cycle_chat"
    user_id = "user_no_recycle"
    group_storage.activate_group(chat_id, "گروه چرخه")
    monkeypatch.setattr(
        fox_game_tokens, "load_official_tokens",
        lambda: ["POOL-A", "POOL-B"],
    )
    first = fox_game_tokens.create_token(chat_id, user_id, "اول")
    assert first == "POOL-A"
    _expire_token(first)
    second = fox_game_tokens.create_token(chat_id, user_id, "دوم")
    assert second == "POOL-B"
    assert second != first


def test_empty_pool_command_shows_message(monkeypatch):
    async def scenario():
        chat_id = 7011
        user_id = 8011
        group_storage.activate_group(chat_id, "گروه خالی")
        _reset_site_wallet(chat_id, user_id)
        economy.add_bronze(chat_id, user_id, 90, note="شارژ")
        monkeypatch.setattr(fox_game_tokens, "load_official_tokens", lambda: ["SITE-ONLY"])
        first = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id)
        await handle_new_message(make_mock_bot(), first)
        second_user = MockEvent("سایت بازی", chat_id=chat_id, user_id=user_id + 1)
        _reset_site_wallet(chat_id, user_id + 1)
        economy.add_bronze(chat_id, user_id + 1, 90, note="شارژ")
        await handle_new_message(make_mock_bot(), second_user)
        assert "https://fox-game.aifox-chat.workers.dev/" in first.replies[0]
        assert "توکن اختصاصی" not in first.replies[0]
        assert "https://fox-game.aifox-chat.workers.dev/" in second_user.replies[0]
        assert "SITE-ONLY" not in second_user.replies[0]

    asyncio.run(scenario())

