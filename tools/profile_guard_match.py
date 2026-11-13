#!/usr/bin/env python3
"""🔎 آزمونِ سریعِ تطبیق: یک متن/بیو را روی فهرست‌های واقعی امتحان می‌کند.

کاربرد (روی سرور یا Termux، در ریشهٔ پروژه):

    python3 tools/profile_guard_match.py "استوریمو ببینین"
    python3 tools/profile_guard_match.py --channel "فیلم و سریال"
    python3 tools/profile_guard_match.py --file ~/sample_bios.txt

برای هر متن می‌گوید کدام عبارت از کدام فهرست گرفته شد (یا هیچ‌کدام).
هیچ RPC و هیچ نوشتنی انجام نمی‌دهد.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.profile_guard import filters, matcher  # noqa: E402


def _load(kind: str):
    if kind == "channel":
        return "کانال", filters.channel_names()
    if kind == "bio":
        return "بیو", filters.bio_sentences()
    return None, []


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("texts", nargs="*", help="متن‌هایی که باید آزمایش شوند")
    parser.add_argument("--channel", action="store_true", help="فقط فهرست کانال")
    parser.add_argument("--bio", action="store_true", help="فقط فهرست بیو")
    parser.add_argument("--file", help="فایلِ متنی؛ هر خط یک نمونه")
    parser.add_argument("--all", action="store_true",
                        help="همهٔ عبارت‌ها را نشان بده (نه فقط اولین تطبیق)")
    args = parser.parse_args()

    samples = list(args.texts)
    if args.file:
        try:
            samples += [line.strip() for line in
                        Path(args.file).expanduser().read_text(
                            encoding="utf-8").splitlines() if line.strip()]
        except OSError as error:
            print(f"⛔️ فایل خوانده نشد: {error}")
            return 2
    if not samples:
        print("⛔️ نمونه‌ای ندادی. مثال: python3 tools/profile_guard_match.py "
              "\"استوریمو\"")
        return 2

    kinds = []
    if not args.channel and not args.bio:
        kinds = ["channel", "bio"]
    else:
        if args.channel:
            kinds.append("channel")
        if args.bio:
            kinds.append("bio")

    built = {}
    for kind in kinds:
        title, items = _load(kind)
        built[kind] = (title, items, matcher.build(items))

    for kind in kinds:
        title, items, _ = built[kind]
        print(f"— فهرست {title}: {len(items)} عبارت")

    for sample in samples:
        print(f"\n«{sample}»")
        for kind in kinds:
            title, _, phrases = built[kind]
            if args.all:
                hits = [p.raw for p in phrases
                        if p.pattern.search(matcher.normalize(sample))]
            else:
                hit = matcher.find_match(sample, phrases)
                hits = [hit.raw] if hit else []
            if hits:
                print(f"  ✅ {title}: {' | '.join(hits[:10])}")
            else:
                print(f"  ➖ {title}: تطبیق نبود")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
