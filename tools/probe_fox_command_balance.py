"""🦊 پروب تعادل «مرکز فرماندهی روباه» — شبیه‌سازی چند استراتژی.

اجرا:  PYTHONPATH=. python3 tools/probe_fox_command_balance.py

برای هر جفت استراتژی چند مسابقه اجرا می‌شود و نتیجه (برد/باخت/تساوی) و
میانگین تعداد نوبت‌ها چاپ می‌گردد. اگر تعادل را عوض کردی، همین ابزار نشان
می‌دهد جنگ کوتاه/طولانی یا یک‌طرفه شده یا نه.
"""
import os, random, statistics, sys, tempfile
sys.path.insert(0, '.')
os.environ["FOX_COMMAND_DATA_DIR"] = tempfile.mkdtemp()
from modules import fox_command as fc
from modules.fox_command import engine

STRATEGIES = {
    "بالستیک-سنگین": ["trident2"],                     # ۱۱۰ برنز
    "متوازن":        ["emad", "bavar373", "s300"],      # 55+50+45 = 150
    "دفاع-محور":     ["thaad", "gmd", "patriot", "sommar"],  # 60+65+55+35
    "کروز-ارزان":    ["paveh", "paveh", "paveh", "mersad", "somkhordad"],
}

def build(match_id, chat, a_items, b_items):
    m = engine.new_match(chat, match_id)
    engine.join(m, 1, "A"); engine.join(m, 2, "B")
    engine.choose_country(m, 1, "iran"); engine.choose_country(m, 2, "usa")
    for uid, items in ((1, a_items), (2, b_items)):
        for item in items:
            status, _info = engine.purchase(m, uid, item)
            assert status == "ok", (item, status)
    engine.start_war(m)
    return m

def fight(m, max_turns=40):
    turns = 0
    while m["phase"] != "finished" and turns < max_turns:
        turns += 1
        for uid in (1, 2):
            player = engine.player_of(m, uid)
            defender = engine.opponent_of(m, uid)
            arsenal = engine.arsenal_list(player)
            if not arsenal:
                # خرید موشک با برنز باقی‌مانده (رفتار طبیعی بازیکن)
                for candidate in ("paveh", "hovyzeh", "sommar", "emad", "kheibar"):
                    if engine.purchase(m, uid, candidate)[0] == "ok":
                        arsenal = engine.arsenal_list(player)
                        break
                # تعمیر/خرید با برنز باقی‌مانده
                if player["bronze"] >= 9 and player["health"] < 100:
                    player["last_repair_at"] = 0
                    engine.repair(m, uid, min(10, int(100 - player["health"])))
                continue
            # انتخاب هوشمند: قوی‌ترین موشک موجود
            arsenal.sort(key=lambda row: -fc.WEAPONS[row[0]]["damage"])
            item_id = arsenal[0][0]
            status, attack = engine.launch(m, uid, item_id,
                                           f"{m['match_id']}-{uid}-{turns}")
            if status != "ok":
                continue
            # مدافع تصمیم می‌گیرد دفاع کند یا نه (اگر سامانه دارد)
            systems = engine.defense_list(defender)
            if systems and random.random() < 0.85:
                engine.defend(m, defender["user_id"], systems[0][0],
                              attack["attack_id"])
            engine.settle(m, attack["attack_id"], "sim")
        winner, loser, reason = engine.check_end(m)
        if winner:
            engine.finish(m, winner, loser, reason)
        m["round"] = m.get("round", 0) + 1
    return m, turns

random.seed(7)
results = []
matchup = list(STRATEGIES.items())
for i in range(len(matchup)):
    for j in range(i + 1, len(matchup)):
        (name_a, items_a), (name_b, items_b) = matchup[i], matchup[j]
        wins_a = wins_b = draws = 0
        turns_list = []
        for game_index in range(24):
            if game_index % 2 == 0:
                m = build(f"sim-{i}-{j}-{game_index}", -1, items_a, items_b)
                a_id = 1
            else:
                m = build(f"sim-{i}-{j}-{game_index}", -1, items_b, items_a)
                a_id = 2
            m, turns = fight(m)
            turns_list.append(turns)
            if m.get("winner") == a_id:
                wins_a += 1
            elif m.get("winner"):
                wins_b += 1
            else:
                draws += 1
        results.append((f"{name_a} vs {name_b}", wins_a, wins_b, draws,
                        statistics.mean(turns_list)))

print(f"{'matchup':<40}{'A':>4}{'B':>4}{'draw':>6}{'turns':>8}")
print("-" * 62)
for row in results:
    print(f"{row[0]:<40}{row[1]:>4}{row[2]:>4}{row[3]:>6}{row[4]:>8.1f}")
