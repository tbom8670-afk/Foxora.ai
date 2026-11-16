"""🧵 بن/سکوتِ خودکار: هر گروه صفِ مستقل دارد (پشتِ هم نمی‌مانند).

قبلاً یک ``Semaphore(1)`` **سراسری** در ``ModerationQueue`` همهٔ گروه‌ها را
یکی‌یکی می‌کرد؛ یعنی بنِ خودکارِ گروهِ A پشتِ بنِ خودکارِ گروهِ B می‌ماند.
حالا هر گروه بودجهٔ مستقلِ کوچکِ خودش را دارد و گروه‌های مختلف **هم‌زمان**
پیش می‌روند. سقفِ سراسریِ اتصالِ سروش همچنان بر عهدهٔ ``RpcGovernor`` است.

اجرا:
    python tests/test_moderation_per_chat_concurrency.py
    pytest tests/test_moderation_per_chat_concurrency.py
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.moderation_queue import ModerationQueue, _automatic_per_chat_limit

PASSED = FAILED = 0


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Logger:
    def log_info(self, *a, **k):
        pass

    def log_error(self, *a, **k):
        pass


def _make_op(trace, name, hold):
    async def _op():
        trace.append((name, "start", time.monotonic()))
        await asyncio.sleep(hold)
        trace.append((name, "end", time.monotonic()))
        return True
    return _op


def test_per_chat_limit_default():
    print("\n[۱] بودجهٔ هر گروه")
    limit = _automatic_per_chat_limit()
    check("پیش‌فرضِ بودجهٔ هر گروه ≥ ۲ است (دیگر ۱ِ سراسری نیست)",
          limit >= 2, f"-> {limit}")
    check("سقفِ هر گروه از ۳ بیشتر نیست", limit <= 3, f"-> {limit}")


def test_two_groups_run_concurrently():
    """بنِ خودکارِ دو گروهِ مختلف باید هم‌زمان اجرا شود (نه پشتِ هم)."""
    print("\n[۲] دو گروهِ مختلف → هم‌زمان (نه سراسریِ یکی‌یکی)")

    async def run():
        q = ModerationQueue(Logger())
        trace = []
        hold = 0.20
        # دو گروهِ کاملاً متفاوت، هرکدام یک بنِ خودکار.
        q.enqueue(-1001111111111, "ban", _make_op(trace, "A", hold),
                  user_id=1)
        q.enqueue(-1002222222222, "ban", _make_op(trace, "B", hold),
                  user_id=2)
        started = time.monotonic()
        # تا پایانِ هر دو صبر کن.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len([t for t in trace if t[1] == "end"]) >= 2:
                break
        elapsed = time.monotonic() - started
        await q.close()
        return trace, elapsed

    trace, elapsed = asyncio.run(run())
    starts = {n: t for n, s, t in trace if s == "start"}
    ends = {n: t for n, s, t in trace if s == "end"}
    check("هر دو عملیات اجرا شدند", len(starts) == 2 and len(ends) == 2,
          f"-> starts={list(starts)} ends={list(ends)}")
    if len(starts) == 2 and len(ends) == 2:
        # هم‌پوشانی: آخرین شروع باید پیش از اولین پایان باشد.
        last_start = max(starts.values())
        first_end = min(ends.values())
        overlap = last_start < first_end
        check("دو گروه هم‌پوشانی دارند (هم‌زمان‌اند، نه سریالِ سراسری)",
              overlap, f"-> last_start={last_start:.3f} first_end={first_end:.3f}")
        # اگر سریالِ سراسری بود، زمانِ کل ≈ ۲×hold می‌شد؛ هم‌زمان ≈ ۱×hold.
        check("زمانِ کل نزدیکِ یک hold است نه دو تا (موازی)",
              elapsed < 0.20 * 1.7, f"-> elapsed={elapsed:.3f}s (hold=0.20)")


def test_within_one_group_respects_per_chat_cap():
    """در یک گروه، بیش از «بودجهٔ هر گروه» بنِ خودکار هم‌زمان نمی‌شود."""
    print("\n[۳] یک گروه → سقفِ هم‌زمانیِ خودش رعایت می‌شود")

    async def run():
        q = ModerationQueue(Logger())
        limit = q._automatic_per_chat_limit
        trace = []
        chat = -1003333333333
        # کارهای بیشتری از سقفِ worker (per_chat_limit=3) و بودجهٔ خودکار.
        for i in range(6):
            q.enqueue(chat, "ban", _make_op(trace, f"u{i}", 0.15), user_id=100 + i)
        peak = 0
        for _ in range(300):
            await asyncio.sleep(0.01)
            active = 0
            # شمارشِ هم‌زمانیِ لحظه‌ای از روی trace
            started = {n for n, s, _ in trace if s == "start"}
            finished = {n for n, s, _ in trace if s == "end"}
            active = len(started - finished)
            peak = max(peak, active)
            if len(finished) >= 6:
                break
        await q.close()
        return peak, limit, trace

    peak, limit, trace = asyncio.run(run())
    finished = {n for n, s, _ in trace if s == "end"}
    check("همهٔ ۶ کار در یک گروه انجام شدند", len(finished) == 6,
          f"-> finished={len(finished)}")
    # هم‌زمانیِ بنِ خودکار در یک گروه نباید از بودجهٔ هر گروه بیشتر شود.
    # (per_chat_limit=3 worker هم سقفِ بالاتری است؛ ملاکِ اصلی بودجهٔ خودکار است.)
    check(f"اوجِ هم‌زمانیِ یک گروه ≤ max(per_chat_limit, automatic_limit) است",
          peak <= 3, f"-> peak={peak} automatic_limit={limit}")


def test_manual_mute_not_blocked_by_automatic_sem():
    """سکوتِ دستیِ مالک نباید پشتِ بنِ خودکار بماند (مسیرِ بدونِ semaphore)."""
    print("\n[۴] سکوتِ دستی پشتِ بنِ خودکار نمی‌ماند")

    async def run():
        q = ModerationQueue(Logger())
        trace = []
        chat = -1004444444444
        # یک بنِ خودکارِ کند در حالِ اجرا…
        q.enqueue(chat, "ban", _make_op(trace, "auto_ban", 0.30), user_id=1)
        await asyncio.sleep(0.02)
        # …سکوتِ دستی (action=mute) برای کاربری دیگر باید بدونِ انتظار اجرا شود.
        q.enqueue(chat, "mute", _make_op(trace, "manual_mute", 0.05), user_id=2)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any(n == "manual_mute" and s == "end" for n, s, _ in trace):
                break
        await q.close()
        return trace

    trace = asyncio.run(run())
    mute_start = next((t for n, s, t in trace if n == "manual_mute" and s == "start"), None)
    ban_end = next((t for n, s, t in trace if n == "auto_ban" and s == "end"), None)
    check("سکوتِ دستی اجرا شد", mute_start is not None)
    if mute_start is not None and ban_end is not None:
        check("سکوتِ دستی پیش از پایانِ بنِ خودکار شروع شد (پشتِ آن نماند)",
              mute_start < ban_end,
              f"-> mute_start={mute_start:.3f} ban_end={ban_end:.3f}")


def _run_all():
    test_per_chat_limit_default()
    test_two_groups_run_concurrently()
    test_within_one_group_respects_per_chat_cap()
    test_manual_mute_not_blocked_by_automatic_sem()
    print(f"\n{'=' * 46}")
    print(f"  PASSED={PASSED}  FAILED={FAILED}")
    print(f"{'=' * 46}")
    return FAILED


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
