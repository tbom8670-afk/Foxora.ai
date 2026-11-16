"""Regression: AI anti-advertiser must PUNISH at the warning threshold.

Bug (task 5): the ad_spam_guard incremented the warning counter but never
applied the group's configured punishment (kick/ban or quick mute) when the
count reached the threshold — the counter just climbed forever.

This exercises ``detector._punish_at_threshold`` directly with a fake bot so it
runs without a live Soroush connection, mirroring how the public ``guard_message``
path now calls it after ``_bump_warnings``.

Run: python3 tests/test_ad_guard_punish_at_threshold.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.ad_spam_guard import detector  # noqa: E402


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class FakeSender:
    def __init__(self, username="spammer"):
        self.username = username
        self.first_name = "Spam"
        self.id = 999


class FakeTracker:
    def __init__(self, reset_raises=False):
        self.reset_calls = []
        self.reset_raises = reset_raises

    def reset_count(self, chat_id, user_id):
        if self.reset_raises:
            raise RuntimeError("tracker down")
        self.reset_calls.append((chat_id, user_id))


class FakeModerationQueue:
    def __init__(self, run_operations=False):
        self.jobs = []
        self.run_operations = run_operations

    def enqueue(self, chat_id, kind, user_id=None, timeout_seconds=None,
                operation=None, on_success=None, on_failure=None):
        self.jobs.append({
            "chat_id": chat_id,
            "kind": kind,
            "user_id": user_id,
            "timeout_seconds": timeout_seconds,
            "operation": operation,
        })
        if self.run_operations and operation is not None:
            operation()


class FakeAdminActions:
    def __init__(self):
        self.punish_calls = []

    def punish_user(self, chat_id, user_id, username=None, announce=True,
                    *, user=None, chat=None):
        self.punish_calls.append({
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "announce": announce,
            "user": user,
        })
        return True


class FakeConfig:
    def __init__(self, action="mute"):
        self._data = {"action_on_threshold": action, "action_duration_seconds": 600}

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, msg):
        self.lines.append(str(msg))

    def log_error(self, msg, *a, **k):
        self.lines.append(str(msg))


class FakeBot:
    def __init__(self, action="mute", with_tracker=True, with_queue=True,
                 with_admin=True, run_operations=False, reset_raises=False):
        self.chat_id = -100
        self.tracker = FakeTracker(reset_raises) if with_tracker else None
        self.moderation_queue = FakeModerationQueue(run_operations) if with_queue else None
        self.admin_actions = FakeAdminActions() if with_admin else None
        self.config_manager = FakeConfig(action)
        self.logger = FakeLogger()


def main():
    print("== ad-guard punishment at threshold ==")

    # 1) BELOW threshold -> no punishment enqueued, counter NOT reset.
    bot = FakeBot(run_operations=True)
    res = detector._punish_at_threshold(bot, bot.chat_id, 555, FakeSender(), count=2, threshold=5)
    check("below threshold returns False", res is False)
    check("below threshold: no punish job", len(bot.moderation_queue.jobs) == 0)
    check("below threshold: no punish call", len(bot.admin_actions.punish_calls) == 0)
    check("below threshold: counter not reset", bot.tracker.reset_calls == [])

    # 2) AT threshold -> punish enqueued + run + counter reset (mute action).
    bot = FakeBot(action="mute", run_operations=True)
    res = detector._punish_at_threshold(bot, bot.chat_id, 555, FakeSender("u555"), count=5, threshold=5)
    check("at threshold returns True", res is True)
    check("at threshold: one punish job enqueued", len(bot.moderation_queue.jobs) == 1)
    job = bot.moderation_queue.jobs[0]
    check("job kind == punish", job["kind"] == "punish")
    check("job targets correct user", job["user_id"] == 555)
    check("job chat_id correct", job["chat_id"] == bot.chat_id)
    check("punish_user actually invoked", len(bot.admin_actions.punish_calls) == 1)
    call = bot.admin_actions.punish_calls[0]
    check("punish_user got username", call["username"] == "u555")
    check("counter reset after punish", bot.tracker.reset_calls == [(bot.chat_id, 555)])
    check("PUNISH ENQUEUED log emitted",
          any("AD SPAM GUARD PUNISH ENQUEUED" in ln for ln in bot.logger.lines))

    # 3) ABOVE threshold (count 7 of 5) -> still punishes (>= semantics).
    bot = FakeBot(action="ban", run_operations=True)
    res = detector._punish_at_threshold(bot, bot.chat_id, 777, FakeSender(), count=7, threshold=5)
    check("above threshold returns True", res is True)
    check("above threshold punishes once", len(bot.admin_actions.punish_calls) == 1)
    check("above threshold resets counter", bot.tracker.reset_calls == [(bot.chat_id, 777)])

    # 4) Missing moderation queue / admin actions -> no crash, returns False.
    bot = FakeBot(with_queue=False)
    res = detector._punish_at_threshold(bot, bot.chat_id, 1, FakeSender(), count=5, threshold=5)
    check("no mod queue -> returns False (no crash)", res is False)

    bot = FakeBot(with_admin=False)
    res = detector._punish_at_threshold(bot, bot.chat_id, 1, FakeSender(), count=5, threshold=5)
    check("no admin_actions -> returns False (no crash)", res is False)

    # 5) Tracker reset raising must NOT break the punish path.
    bot = FakeBot(run_operations=True, reset_raises=True)
    res = detector._punish_at_threshold(bot, bot.chat_id, 222, FakeSender(), count=5, threshold=5)
    check("reset failure -> still returns True", res is True)
    check("reset failure -> punish still ran", len(bot.admin_actions.punish_calls) == 1)

    # 6) Defensive: bad/garbage count+threshold values -> False, no crash.
    bot = FakeBot(run_operations=True)
    check("garbage count None -> False",
          detector._punish_at_threshold(bot, bot.chat_id, 1, FakeSender(), count=None, threshold=5) is False)
    check("threshold 0 -> False",
          detector._punish_at_threshold(bot, bot.chat_id, 1, FakeSender(), count=5, threshold=0) is False)
    check("garbage produced no punish", len(bot.admin_actions.punish_calls) == 0)

    print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
