"""یک GetFullUserRequest مشترک بین «پروفایل گارد» و «لینک بیو».

    python tests/test_full_user_shared.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import full_user_shared as shared  # noqa: E402

PASSED = FAILED = 0


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Client:
    def __init__(self, delay=0.02, fail=False):
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def __call__(self, request):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return {"n": self.calls, "request": request}


def make():
    return "GetFullUserRequest"


async def scenario():
    shared.clear()
    client = Client()
    a, b = await asyncio.gather(
        shared.get_full_user(client, make, 7, timeout=2),
        shared.get_full_user(client, make, 7, timeout=2),
    )
    check("دو درخواستِ هم‌زمان ⇒ یک RPC", client.calls == 1, client.calls)
    check("هر دو همان پاسخ را گرفتند", a is b)

    c = await shared.get_full_user(client, make, 7, timeout=2)
    check("پاسخِ چند ثانیهٔ پیش دوباره استفاده شد", client.calls == 1 and c is a)

    d = await shared.get_full_user(client, make, 7, timeout=2, fresh=True)
    check("fresh=True ⇒ RPC تازه", client.calls == 2 and d is not a)

    await shared.get_full_user(client, make, 8, timeout=2)
    check("کاربرِ دیگر ⇒ RPC جدا", client.calls == 3)

    shared.invalidate(7)
    await shared.get_full_user(client, make, 7, timeout=2)
    check("invalidate ⇒ RPC تازه", client.calls == 4)

    import os
    os.environ["FULL_USER_SHARE_SECONDS"] = "0"
    await asyncio.sleep(0.01)
    await shared.get_full_user(client, make, 7, timeout=2)
    check("بازهٔ صفر ⇒ بدون بازاستفاده", client.calls == 5)
    os.environ.pop("FULL_USER_SHARE_SECONDS")

    shared.clear()
    bad = Client(fail=True)
    results = await asyncio.gather(
        shared.get_full_user(bad, make, 9, timeout=2),
        shared.get_full_user(bad, make, 9, timeout=2),
        return_exceptions=True,
    )
    check("خطا به هر دو منتظر می‌رسد",
          all(isinstance(r, RuntimeError) for r in results), results)
    check("خطا ذخیره نمی‌شود", bad.calls == 1 and not shared._RECENT)
    bad.fail = False
    ok = await shared.get_full_user(bad, make, 9, timeout=2)
    check("بعد از خطا دوباره تلاش می‌شود", bad.calls == 2 and ok["n"] == 2)

    shared.clear()
    slow = Client(delay=0.3)
    try:
        await shared.get_full_user(slow, make, 10, timeout=0.05)
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
    check("تایم‌اوتِ مصرف‌کننده بالا می‌رود", timed_out)
    late = await shared.get_full_user(slow, make, 10, timeout=2)
    check("درخواستِ در راه گم نمی‌شود (RPC دوباره زده نشد)",
          slow.calls == 1 and late["n"] == 1)
    check("چیزی در راه نمانده", not shared._INFLIGHT)


asyncio.run(scenario())
print("=" * 52)
print(f"passed={PASSED} failed={FAILED}")
print("=" * 52)
sys.exit(1 if FAILED else 0)
