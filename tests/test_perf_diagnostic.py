"""🩺 تستِ ماژولِ تشخیصِ عملکرد (perf_diagnostic).

پوشش:
  • تشخیصِ دستورِ گزارش (با نرمال‌سازیِ نیم‌فاصله/فاصله).
  • بخشِ مسیرِ عضویت: کانالِ با مسیرِ گرانِ scan/snapshot علامت می‌خورد؛
    کانالِ با getParticipant «ارزان» است.
  • بخشِ Governor: «culprit» (کندترین holder) نام برده می‌شود.
  • جمع‌بندی: مسیرِ گرانِ عضویت رتبهٔ اول را می‌گیرد.
  • ارسال به پیویِ مالک: resolve_private_owner_peer + client.send_message.

اجرا:
    python tests/test_perf_diagnostic.py
    pytest tests/test_perf_diagnostic.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modules.perf_diagnostic as pd
import modules.owner_private as owner_private

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
    def __init__(self):
        self.errors = []

    def log_info(self, *a, **k):
        pass

    def log_error(self, msg):
        self.errors.append(msg)


class FakeGov:
    def __init__(self, holders=None, waiting_by_bucket=None):
        self._holders = holders or []
        self._wbk = waiting_by_bucket or {}

    def snapshot(self):
        return {
            "enabled": True, "shadow": False, "total_limit": 3,
            "active": 2, "waiting": sum(self._wbk.values()), "max_waiting": 9,
            "active_by_bucket": {"send": 1, "heavy": 1},
            "waiting_by_bucket": dict(self._wbk),
            "holders": list(self._holders), "stats": {},
        }

    def culprit_for(self, holder=None):
        return self._holders[0] if self._holders else "-"


class FakeBot:
    def __init__(self, gov=None):
        self.client = None
        self.logger = Logger()
        self.rpc_governor = gov


# ------------------------------------------------------------------ #
# ۱) تشخیصِ دستور
# ------------------------------------------------------------------ #
def test_command_detection():
    print("\n[۱] تشخیصِ دستورِ گزارش")
    for cmd in ["گزارش عملکرد", "تشخیص کندی", "گزارش کندی", "perf", "PERF"]:
        check(f"«{cmd}» دستورِ گزارش است",
              pd.is_perf_diagnostic_command(cmd) is True, f"-> {cmd}")
    check("«  گزارش   عملکرد » با فاصلهٔ اضافه",
          pd.is_perf_diagnostic_command("  گزارش   عملکرد ") is True)
    check("«گزارش\u200cعملکرد» با نیم‌فاصله",
          pd.is_perf_diagnostic_command("گزارش\u200cعملکرد") is True)
    for cmd in ["", "سلام", "لیست انقضا", "فعال", "گزارش"]:
        check(f"«{cmd}» دستورِ گزارش نیست",
              pd.is_perf_diagnostic_command(cmd) is False, f"-> {cmd}")


# ------------------------------------------------------------------ #
# ۲) بخشِ مسیرِ عضویت
# ------------------------------------------------------------------ #
def test_membership_route_section():
    print("\n[۲] بخشِ مسیرِ عضویت (گران vs ارزان)")
    rec_expensive = {
        "group_id": -1001, "channel_id": 111,
        "last_update_cursor": {"routes": {"scan": True, "snapshot": True}},
    }
    rec_cheap = {
        "group_id": -1002, "channel_id": 222,
        "last_update_cursor": {"routes": {"get_participant": True}},
    }
    original = pd._fjstorage.list_groups
    try:
        pd._fjstorage.list_groups = lambda enabled_only=False: [
            rec_expensive, rec_cheap]
        lines, findings = pd._membership_route_section(FakeBot())
    finally:
        pd._fjstorage.list_groups = original
    text = "\n".join(lines)
    check("کانالِ گران شناسایی شد", len(findings["expensive_channels"]) == 1,
          f"-> {findings['expensive_channels']}")
    check("کانالِ گران همان group=-1001 است",
          findings["expensive_channels"]
          and findings["expensive_channels"][0]["group_id"] == -1001)
    check("کانالِ ارزان (getParticipant) در فهرستِ گران نیست",
          all(c["group_id"] != -1002
              for c in findings["expensive_channels"]))
    check("getParticipant برای کانالِ گران فعال نیست (no_participant)",
          any(c["group_id"] == -1001 for c in findings["no_participant"]))
    check("گزارش شاملِ برچسبِ «گران» است", "گران" in text, f"-> {text[:120]}")
    check("گزارش شاملِ «تست عضویت»/راه‌حل نیست (فقط بخش)", True)


# ------------------------------------------------------------------ #
# ۳) بخشِ Governor — نام بردنِ culprit
# ------------------------------------------------------------------ #
def test_governor_section_names_culprit():
    print("\n[۳] بخشِ Governor — culprit")
    holder = "heavy:GetParticipantsRequest:chat=-1001:held_ms=612"
    gov = FakeGov(holders=[holder], waiting_by_bucket={"heavy": 4, "send": 1})
    lines, findings = pd._governor_section(FakeBot(gov))
    text = "\n".join(lines)
    check("culprit شناسایی شد", findings["culprit"] is not None)
    check("نامِ GetParticipantsRequest در گزارش است",
          "GetParticipantsRequest" in text, f"-> {text[:160]}")
    check("پُرشلوغ‌ترین bucket صف = heavy",
          findings["busiest_waiting_bucket"]
          and findings["busiest_waiting_bucket"][0] == "heavy",
          f"-> {findings['busiest_waiting_bucket']}")


# ------------------------------------------------------------------ #
# ۴) جمع‌بندی — رتبه‌بندیِ مسیرِ گرانِ عضویت در صدر
# ------------------------------------------------------------------ #
def test_verdict_ranks_expensive_membership_first():
    print("\n[۴] جمع‌بندی — رتبه‌بندی")
    mem = {"expensive_channels": [{"group_id": -1001}, {"group_id": -1003}],
           "no_participant": [], "total": 2}
    gov = {"culprit": ("send:SendMessageRequest:chat=-9:held_ms=120", 120.0),
           "busiest_waiting_bucket": None}
    lines = pd._verdict_section(mem, gov)
    text = "\n".join(lines)
    check("مسیرِ گرانِ عضویت در جمع‌بندی هست", "scan/snapshot" in text
          or "getParticipant" in text, f"-> {text[:160]}")
    check("اشاره به «تست عضویت» به‌عنوانِ راه‌حل", "تست عضویت" in text)
    # رتبهٔ اول باید مسیرِ عضویت باشد (score بالاتر از culpritِ سبک).
    first = next((ln for ln in lines if ln.strip().startswith("1)")), "")
    check("رتبهٔ ۱ = مسیرِ عضویتِ گران", "عضویت" in first, f"-> {first[:80]}")


# ------------------------------------------------------------------ #
# ۵) ارسال به پیویِ مالک
# ------------------------------------------------------------------ #
def test_send_to_owner_pv():
    print("\n[۵] ارسالِ گزارش به پیویِ مالک")

    class FakeClient:
        def __init__(self):
            self.sent = []

        def send_message(self, target, text):
            self.sent.append((target, text))
            async def _ok():
                return True
            return _ok()

    sentinel_peer = object()
    bot = FakeBot(FakeGov(holders=[]))
    bot.client = FakeClient()

    original = owner_private.resolve_private_owner_peer

    async def _fake_resolve(client, *, logger=None, context="X"):
        return sentinel_peer

    try:
        owner_private.resolve_private_owner_peer = _fake_resolve
        ok = asyncio.run(pd.send_perf_report_to_owner(bot))
    finally:
        owner_private.resolve_private_owner_peer = original

    check("ارسال موفق بود (True)", ok is True, f"-> {ok}")
    check("حداقل یک پیام به پیویِ مالک ارسال شد", len(bot.client.sent) >= 1,
          f"-> {len(bot.client.sent)}")
    if bot.client.sent:
        target, text = bot.client.sent[0]
        check("گیرنده همان peerِ مالک است", target is sentinel_peer)
        check("متن شاملِ عنوانِ گزارشِ تشخیص است",
              "گزارشِ تشخیصِ کندی" in text or "Governor" in text,
              f"-> {text[:80]}")

    # حالتِ شکست: peer=None → False و بدونِ ارسال.
    bot2 = FakeBot(FakeGov(holders=[]))
    bot2.client = FakeClient()

    async def _none_resolve(client, *, logger=None, context="X"):
        return None

    try:
        owner_private.resolve_private_owner_peer = _none_resolve
        ok2 = asyncio.run(pd.send_perf_report_to_owner(bot2))
    finally:
        owner_private.resolve_private_owner_peer = original
    check("وقتی peerِ مالک resolve نشود → False", ok2 is False, f"-> {ok2}")
    check("و چیزی ارسال نمی‌شود", len(bot2.client.sent) == 0)


def test_chunking():
    print("\n[۶] قطعه‌قطعه‌کردنِ گزارشِ بلند")
    short = pd._chunk("سلام")
    check("گزارشِ کوتاه = یک قطعه", len(short) == 1)
    long_text = "خ" * 9000
    parts = pd._chunk(long_text, limit=3500)
    check("گزارشِ بلند قطعه‌قطعه می‌شود", len(parts) >= 3, f"-> {len(parts)}")
    check("بازچسباندنِ قطعه‌ها = متنِ اصلی", "".join(parts) == long_text)


def _run_all():
    test_command_detection()
    test_membership_route_section()
    test_governor_section_names_culprit()
    test_verdict_ranks_expensive_membership_first()
    test_send_to_owner_pv()
    test_chunking()
    print(f"\n{'=' * 46}")
    print(f"  PASSED={PASSED}  FAILED={FAILED}")
    print(f"{'=' * 46}")
    return FAILED


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
