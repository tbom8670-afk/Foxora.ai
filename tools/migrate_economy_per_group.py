#!/usr/bin/env python3
"""🔀 تبدیل کیف پول سراسری اقتصاد به «هر گروه جداگانه».

کلید کیف پول از ``"<user_id>"`` به ``"<chat_id>:<user_id>"`` تغییر کرده
است. این ابزار فایل موجود را امن مهاجرت می‌دهد:

  • ابتدا از economy.json یک بکاپ با مهر زمانی گرفته می‌شود.
  • برای هر کاربر سراسری، گروه‌هایی که در آن‌ها فعال بوده از
    config/coins.json خوانده می‌شود و موجودی به همان گروه‌ها منتقل
    می‌گردد (به نسبت سکه‌های قدیمی همان گروه).
  • اگر گروهی پیدا نشد، کل موجودی به گروه پیش‌فرض ``--fallback`` می‌رود
    تا هیچ سکه‌ای گم نشود.
  • رکوردهایی که از قبل کلید ``chat:user`` دارند دست‌نخورده می‌مانند، پس
    اجرای دوباره بی‌خطر است.

    python3 tools/migrate_economy_per_group.py --dry-run
    python3 tools/migrate_economy_per_group.py
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from economy import storage
from economy.coins import accounts

LEGACY_COINS = ROOT / "config" / "coins.json"
COIN_KEYS = ("bronze", "silver", "gold")


def legacy_group_weights():
    """user_id → {chat_key: سکه‌های قدیمی} برای تقسیم منصفانه."""
    weights = {}
    if not LEGACY_COINS.exists():
        return weights
    try:
        raw = json.loads(LEGACY_COINS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return weights
    for group, members in (raw.get("users") or {}).items():
        if not isinstance(members, dict):
            continue
        for user_id, record in members.items():
            if not isinstance(record, dict):
                continue
            amount = int(record.get("coins", 0) or 0)
            bucket = weights.setdefault(str(user_id), {})
            key = accounts.chat_key(group)
            bucket[key] = bucket.get(key, 0) + max(amount, 0)
    return weights


def split_amount(total, weights, groups):
    """تقسیم ``total`` بین گروه‌ها به نسبت وزن؛ باقیمانده به بزرگ‌ترین."""
    if not groups:
        return {}
    usable = {g: weights.get(g, 0) for g in groups}
    if sum(usable.values()) <= 0:
        usable = {g: 1 for g in groups}
    pool = sum(usable.values())
    out, given = {}, 0
    ordered = sorted(usable.items(), key=lambda kv: -kv[1])
    for group, weight in ordered[1:]:
        share = total * weight // pool
        out[group] = share
        given += share
    out[ordered[0][0]] = total - given
    return out


def migrate(dry_run=False, fallback=None):
    data = storage.snapshot()
    users = data.get("users", {})
    if not users:
        print("economy.json خالی است؛ کاری لازم نیست.")
        return 0

    globals_ = {k: v for k, v in users.items() if ":" not in k}
    scoped = {k: v for k, v in users.items() if ":" in k}
    print(f"کیف پول‌های سراسری (نیازمند مهاجرت): {len(globals_)}")
    print(f"کیف پول‌های از قبل گروهی            : {len(scoped)}")
    if not globals_:
        print("همه چیز از قبل گروهی است؛ کاری لازم نیست.")
        return 0

    weights = legacy_group_weights()
    plan, orphan = {}, []
    for user_id, record in globals_.items():
        groups = sorted(weights.get(user_id, {}))
        if not groups:
            if fallback:
                groups = [accounts.chat_key(fallback)]
            else:
                orphan.append(user_id)
                continue
        shares = {
            coin: split_amount(int(record.get(coin, 0) or 0),
                               weights.get(user_id, {}), groups)
            for coin in COIN_KEYS
        }
        plan[user_id] = (groups, shares)

    print(f"قابل مهاجرت : {len(plan)} کاربر")
    if orphan:
        print(f"بدون گروه   : {len(orphan)} کاربر "
              f"(با --fallback <chat_id> منتقل می‌شوند)")

    if dry_run:
        print("\n--- حالت آزمایشی، چیزی نوشته نمی‌شود ---")
        for user_id, (groups, shares) in list(plan.items())[:8]:
            bronze = shares["bronze"]
            print(f"  user {user_id:12} → " +
                  ", ".join(f"{g}:{bronze.get(g,0)}" for g in groups))
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = storage.DATA_FILE.with_name(f"economy.backup_{stamp}.json")
    if storage.DATA_FILE.exists():
        shutil.copy2(storage.DATA_FILE, backup)
        print(f"\n📦 بکاپ: {backup.name}")

    moved = 0
    with storage.transaction() as live:
        bucket = live.setdefault("users", {})
        for user_id, (groups, shares) in plan.items():
            record = bucket.pop(user_id, None)
            if record is None:
                continue
            for group in groups:
                key = f"{group}:{user_id}"
                target = accounts._user(live, key)
                for coin in COIN_KEYS:
                    target[coin] = int(target.get(coin, 0)) + int(
                        shares[coin].get(group, 0))
                if record.get("name"):
                    target["name"] = record["name"]
                target["wins"] = int(target.get("wins", 0)) + (
                    int(record.get("wins", 0)) if group == groups[0] else 0)
                if group == groups[0]:
                    for field in ("transactions", "references",
                                  "purchases", "daily_claimed_at"):
                        if record.get(field):
                            target[field] = record[field]
                accounts._refresh_total(live, target)
            moved += 1

    print(f"✅ منتقل شد: {moved} کاربر")
    print(f"📁 coins.json دست‌نخورده باقی ماند")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fallback", default=None,
                        help="گروه پیش‌فرض برای کاربران بدون گروه")
    args = parser.parse_args()
    return migrate(dry_run=args.dry_run, fallback=args.fallback)


if __name__ == "__main__":
    sys.exit(main())
