"""🏅 دستاورد کاربران — تست کامل و واقعی.

پوشش خواسته‌های کارفرما:
  ۱) «دستاورد» برای خود کاربر    ۲) «دستاورد @username» برای کاربر دیگر
  ۳) کاربر بدون سابقه            ۴) کاربر دارای تخلف ثبت‌شده
  ۵) کاربر دارای رتبه در بازی    ۶) ماندگاری پس از ری‌استارت (پروسهٔ جدا،
     هر دو بک‌اند JSON و SQLite) ۷) جداسازی آمار گروه‌ها
  ۸) تطابق کامل خروجی با قالب مصوب

    python tests/test_user_achievements.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import group_stats, user_achievements as ach, user_activity, user_history

PASSED = FAILED = 0
CHAT = -100111
CHAT2 = -100222
NOW = 1_800_000_000.0  # زمان ثابت برای تست‌های قطعی


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


def fresh(tmp):
    """همهٔ ذخیره‌های درگیر را روی فایل‌های موقت تازه می‌نشاند."""
    ach.FILE = Path(tmp) / "user_achievements.json"
    ach._USE_SQLITE = False
    ach._reset_state()
    group_stats.FILE = str(Path(tmp) / "group_stats.json")
    group_stats.BACKUP_FILE = group_stats.FILE + ".bak"
    group_stats._stats_cache = None
    group_stats._stats_cache_mtime = None
    group_stats._stats_dirty = False
    user_history._BASE = Path(tmp)
    user_history._FILE = Path(tmp) / "user_history.json"
    user_activity.FILE = Path(tmp) / "user_activity.json"
    user_activity._USE_SQLITE = False
    user_activity._CACHE = {}
    user_activity._DIRTY = {}
    user_activity._JSON_LOADED = False
    return Path(tmp)


def fresh_economy(tmp):
    import economy
    from economy import settings, storage
    from economy.shop import store
    storage.use_file(Path(tmp) / "economy.json")
    store.ITEMS_FILE = Path(tmp) / "shop.json"
    store._cache = None
    store._cache_mtime = None
    settings.SETTINGS_FILE = Path(tmp) / "settings.json"
    settings.reset_cache()
    return economy


def set_messages(chat, user_id, username, name, count):
    data = group_stats.load_stats()
    group = group_stats.ensure_group(data, chat)
    user = group_stats.ensure_user(group, user_id, username, name)
    user["messages"] = count
    group_stats.save_stats(data)
    group_stats.flush()


def record_days(chat, user_id, days, *, base_now=NOW):
    """``days`` روز فعالیت متمایز، آخرینش «امروزِ» ثابت تست."""
    for index in range(days):
        ts = base_now - (days - 1 - index) * 86400
        ach.record_message(chat, user_id, now=ts, today=f"2026-11-{index + 1:02d}")


# ===========================================================================
def test_parse_command():
    print("\n### 🧭 تجزیهٔ دستور")
    check("دستاورد ساده", ach.parse_command("دستاورد") == ("self", None))
    check("دستاورد @user", ach.parse_command("دستاورد @Olpea") == ("user", "olpea"))
    check("متن نامرتبط", ach.parse_command("دستاوردها") is None)
    check("بدون @", ach.parse_command("دستاورد olpea") is None)
    check("خالی", ach.parse_command("") is None)


def test_exact_template():
    print("\n### 🪟 تطابق کامل با قالب مصوب")
    tmp = fresh(tempfile.mkdtemp())
    set_messages(CHAT, 42, "olpea", "Ol Pea", 723)
    record_days(CHAT, 42, 12)
    card = ach.build_card(CHAT, 42, now=NOW)
    expected = (
        "🪽کاربر : @olpea  ⇾\n\n"
        "◩ نـشان عضویت : عضو جدید\n\n"
        "◩ تـعداد پیام : 𝟳𝟮𝟯\n\n"
        "◩ اسـتاد بازی : ـــــ\n\n"
        "◩ نـشان فعالیت : کاربر فعال\n\n"
        "◩ تـعداد تخلف : 𝟬\n\n"
        "◩ امـتیاز دریافتی : 𝟭𝟬 / 𝟵\n\n"
        "◩ نـشان دستاورد : کاربر طلایی\n\n"
        "◩ روز هـای فعالیت : 𝟭𝟮 روز"
    )
    check("خروجی دقیقاً قالب نمونه است", card == expected,
          f"\n--- got ---\n{card}\n--- expected ---\n{expected}")
    check("هیچ ردیف اضافه‌ای نیست", len(card.split("\n\n")) == 9)


def test_self_and_other_and_unknown():
    print("\n### 👥 خود کاربر / کاربر دیگر / ناشناس")
    tmp = fresh(tempfile.mkdtemp())
    set_messages(CHAT, 1, "sender", "Sender", 10)
    set_messages(CHAT, 2, "target", "Target", 250)
    record_days(CHAT, 2, 5)
    ach.record_violation(CHAT, 2)

    target_id, target_display = ach.resolve_target(CHAT, "@target")
    check("@target در همین گروه پیدا شد", target_id == "2", f"got={target_id}")
    card = ach.build_card(CHAT, target_id, display=target_display, now=NOW)
    check("کارت کاربر دیگر نام خودش را دارد", "🪽کاربر : @target  ⇾" in card)
    check("پیام‌های کاربر دیگر است نه فرستنده", "◩ تـعداد پیام : 𝟮𝟱𝟬" in card)
    check("تخلف کاربر دیگر شمرده شده", "◩ تـعداد تخلف : 𝟭" in card)

    ghost_id, ghost_display = ach.resolve_target(CHAT, "@ghost_user")
    check("یوزرنیم ناشناس user_id ندارد", ghost_id is None)
    ghost_card = ach.build_card(CHAT, ghost_id, display=ghost_display, now=NOW)
    check("کارت ناشناس با همان @ و پیش‌فرض‌ها",
          ghost_card.startswith("🪽کاربر : @ghost_user  ⇾")
          and "◩ تـعداد پیام : 𝟬" in ghost_card
          and "◩ نـشان دستاورد : کاربر برنزی" in ghost_card)


def test_no_history_user():
    print("\n### 🆕 کاربر بدون هیچ سابقه")
    tmp = fresh(tempfile.mkdtemp())
    card = ach.build_card(CHAT, 999, display="کاربر تازه", now=NOW)
    check("عضو جدید", "◩ نـشان عضویت : عضو جدید" in card)
    check("غیرفعال", "◩ نـشان فعالیت : کاربر غیرفعال" in card)
    check("صفر پیام/تخلف/روز", "◩ تـعداد پیام : 𝟬" in card
          and "◩ تـعداد تخلف : 𝟬" in card and "𝟭𝟬 / 𝟬" in card
          and "◩ روز هـای فعالیت : 𝟬 روز" in card)
    check("برنزی", "◩ نـشان دستاورد : کاربر برنزی" in card)
    check("بدون استاد بازی", "◩ اسـتاد بازی : ـــــ" in card)


def test_violations_persistent():
    print("\n### ⚠️ تخلف ماندگار (مستقل از سابقهٔ ۲۴ساعته و چرخهٔ اخطار)")
    tmp = fresh(tempfile.mkdtemp())
    user = {"id": 7, "first_name": "Bad", "username": "baduser"}
    user_history.add_warn(CHAT, user, "اسپم")
    user_history.add_warn(CHAT, user, "تبلیغ")
    user_history.add_mute(CHAT, user, "فحاشی")
    check("سه تخلف ثبت شد", ach.get_record(CHAT, 7)["violations"] == 3)
    # ریست کامل سابقهٔ موقت — شمارندهٔ دستاورد نباید تکان بخورد.
    user_history.reset(CHAT)
    check("بعد از ریست سابقهٔ موقت هم ۳ ماند",
          ach.get_record(CHAT, 7)["violations"] == 3)
    ach.flush()
    ach._reset_state()  # ری‌استارت شبیه‌سازی‌شده
    check("بعد از ری‌استارت هم ۳ ماند",
          ach.get_record(CHAT, 7)["violations"] == 3)
    check("تخلف امتیاز را کم می‌کند",
          ach.compute_score(723, 12, 3) == 6)


def test_game_master():
    print("\n### 🎮 استاد بازی از بردهای واقعی")
    tmp = fresh(tempfile.mkdtemp())
    economy = fresh_economy(tmp)
    for n in range(2):
        economy.award_game(CHAT, 5, "emoji", reference=f"t-emoji-{n}")
    card = ach.build_card(CHAT, 5, now=NOW)
    check("دو برد زیر حد نصاب است", "◩ اسـتاد بازی : ـــــ" in card)
    economy.award_game(CHAT, 5, "emoji", reference="t-emoji-2")
    # مرجع تکراری نباید برد دوم بسازد.
    economy.award_game(CHAT, 5, "emoji", reference="t-emoji-2")
    economy.award_game(CHAT, 5, "flag", reference="t-flag-0")
    check("بردها per-game ثبت شده‌اند",
          ach.get_record(CHAT, 5)["games"] == {"حدس ایموجی": 3, "حدس پرچم": 1},
          f"got={ach.get_record(CHAT, 5)['games']}")
    card = ach.build_card(CHAT, 5, now=NOW)
    check("استاد بازی = پرتکرارترین بازی", "◩ اسـتاد بازی : حدس ایموجی" in card)

    # سابقهٔ پیش از قابلیت: فقط دفتر تراکنش‌ها هست، رکورد per-game نیست.
    ach.FILE = Path(tmp) / "achievements_empty.json"
    ach._reset_state()
    card = ach.build_card(CHAT, 5, now=NOW)
    check("بازیابی از دفتر تراکنش‌های واقعی", "◩ اسـتاد بازی : حدس ایموجی" in card)


def test_group_isolation():
    print("\n### 🧱 جداسازی آمار گروه‌ها")
    tmp = fresh(tempfile.mkdtemp())
    set_messages(CHAT, 3, "multi", "Multi", 500)
    set_messages(CHAT2, 3, "multi", "Multi", 5)
    record_days(CHAT, 3, 10)
    record_days(CHAT2, 3, 1)
    ach.record_violation(CHAT, 3)
    card_a = ach.build_card(CHAT, 3, now=NOW)
    card_b = ach.build_card(CHAT2, 3, now=NOW)
    check("گروه اول آمار خودش", "𝟱𝟬𝟬" in card_a and "◩ تـعداد تخلف : 𝟭" in card_a)
    check("گروه دوم آمار خودش", "◩ تـعداد پیام : 𝟱" in card_b
          and "◩ تـعداد تخلف : 𝟬" in card_b
          and "◩ روز هـای فعالیت : 𝟭 روز" in card_b)


def test_score_and_badges():
    print("\n### 🧮 فرمول امتیاز و نشان‌ها")
    check("نمونهٔ کارفرما: ۷۲۳ پیام، ۱۲ روز، بدون تخلف = ۹",
          ach.compute_score(723, 12, 0) == 9)
    check("سقف ۱۰", ach.compute_score(100000, 365, 0) == 10)
    check("کف ۰", ach.compute_score(0, 0, 50) == 0)
    check("طلایی/نقره‌ای/برنزی", ach.tier_for_score(8) == "کاربر طلایی"
          and ach.tier_for_score(5) == "کاربر نقره‌ای"
          and ach.tier_for_score(4) == "کاربر برنزی")
    check("عضو قدیمی بعد از ۳۰ روز",
          ach.membership_badge(NOW - 40 * 86400, now=NOW) == "عضو قدیمی")
    check("عضو جدید", ach.membership_badge(NOW - 86400, now=NOW) == "عضو جدید")
    check("فعال/غیرفعال", ach.activity_badge(NOW - 3600, now=NOW) == "کاربر فعال"
          and ach.activity_badge(NOW - 30 * 86400, now=NOW) == "کاربر غیرفعال")
    tmp = fresh(tempfile.mkdtemp())
    ach.record_message(CHAT, 8, now=NOW, today="2026-11-01")
    ach.record_message(CHAT, 8, now=NOW, today="2026-11-01")
    card = ach.build_card(CHAT, 8, now=NOW)
    check("۱۰۰ پیام در یک روز = یک روز فعالیت",
          "◩ روز هـای فعالیت : 𝟭 روز" in card)


_CHILD = r"""
import json, sys
sys.path.insert(0, {root!r})
from modules import user_achievements as ach
if sys.argv[1] == "write":
    ach.record_message(111, 222, "restartuser", "Restart User", today="2026-11-01")
    ach.record_message(111, 222, "restartuser", "Restart User", today="2026-11-02")
    ach.record_violation(111, 222)
    ach.record_game_win(111, 222, "حدس ایموجی")
    ach.flush()
    print("written")
else:
    record = ach.get_record(111, 222)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
"""


def _run_child(tmp, mode, backend_env):
    env = dict(os.environ)
    env["SOROUSH_BOT_DATA_DIR"] = str(tmp)
    env.update(backend_env)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD.format(root=str(ROOT)), mode],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_restart_persistence():
    print("\n### 💾 ماندگاری پس از ری‌استارت واقعی (پروسهٔ جدا)")
    for label, backend_env in (
        ("JSON", {"RUNTIME_STATE_BACKEND": "json"}),
        ("SQLite", {"RUNTIME_STATE_BACKEND": "sqlite"}),
    ):
        tmp = tempfile.mkdtemp()
        _run_child(tmp, "write", backend_env)
        out = _run_child(tmp, "read", backend_env)
        record = json.loads(out)
        check(f"[{label}] روزها و تخلف بعد از ری‌استارت ماندند",
              record["days"] == ["2026-11-01", "2026-11-02"]
              and record["violations"] == 1
              and record["games"] == {"حدس ایموجی": 1},
              f"got={out}")


def test_wiring():
    print("\n### 🔌 سیم‌کشی هندلر و لیست کاربران")
    source = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    check("دستور در هندلر سیم‌کشی شده",
          "user_achievements.parse_command(clean_text)" in source
          and "user_achievements.build_card" in source)
    check("ثبت روز فعالیت کنار ثبت فعالیت پیام",
          source.count("user_achievements.record_message") >= 3)
    check("لیست کاربران متن دستاورد را دارد",
          "برای دستاورد ها و دیدن فعالیت خود یا کاربر بنویسید:" in source
          and "دستاورد @aifox" in source)
    core = (ROOT / "core" / "bot_working_split_ok.py").read_text(encoding="utf-8")
    check("flush دوره‌ای در حلقهٔ اصلی", "flush_user_achievements" in core)


def main():
    test_parse_command()
    test_exact_template()
    test_self_and_other_and_unknown()
    test_no_history_user()
    test_violations_persistent()
    test_game_master()
    test_group_isolation()
    test_score_and_badges()
    test_restart_persistence()
    test_wiring()
    print(f"\n{'=' * 50}\n✅ PASSED: {PASSED}   ❌ FAILED: {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
