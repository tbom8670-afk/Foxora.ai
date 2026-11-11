"""ردیابِ رفتارِ ربات‌های «کاربرنما» (behavior-based detection).

مشکلِ واقعیِ کارفرما:

    «تبلیغ حتماً جملهٔ تبلیغاتی نیست؛ رباتی هست که مثل کاربر عادی است و
     راحت این‌جوری عادی تبلیغ می‌دهد.»

یعنی ربات:
* نام و یوزرنیمِ کاملاً عادی دارد (مثل «نیکا») — پس مارکرِ هویت ندارد؛
* به پیامِ چند کاربرِ مختلف ریپلای می‌کند («سلام 👋»، «هوا چطوره خیلی سرد
  شده»، «ف میخوای ؟»، «جون منو نگا خوشگلم») تا طبیعی به‌نظر برسد؛
* و در آخر یک خطِ کوتاهِ فروش/خدمات می‌گذارد («خدمات میدم»).

بنابراین تصمیمِ این ماژول بر پایهٔ **رفتار** است، نه واژه‌های تبلیغاتی:

    سیگنال‌های رفتاری (پنجرهٔ زمانیِ کوتاه، فقط حافظه، بدونِ RPC)
        burst        — چند پیامِ پشتِ‌سرهم در ۲ دقیقه
        many_targets — ریپلای به چند کاربرِ **مختلف**
        chatter      — پیام‌های کوتاهِ «گپِ الکی»/فلیرت
        repeat       — تکرارِ عینِ یک متن
        cta          — خطِ خدمات/فروش/دعوت به پیامِ خصوصی
        contact      — آیدی/لینک/شماره در همین پیام

    اسپمِ رفتاری =
          (cta قوی  و  حداقل یکی از burst/many_targets/chatter/repeat)
        یا (cta ضعیف و contact و حداقل یکی از burst/many_targets/chatter)
        یا (repeat ≥ آستانه)                      ← حلقهٔ اسپم
        یا (many_targets و burst)                  ← رباتِ ریپلای‌زن

این قواعد عمداً چندشرطی‌اند: کاربرِ عادی که یک‌بار «خدمات میدم» می‌نویسد
گرفته نمی‌شود، ولی حسابی که رفتارِ رباتی دارد گرفته می‌شود.

علاوه بر خودِ پیام، «ردِ» همان کاربر در همان پنجره (گپ‌های قبلی‌اش) هم
برمی‌گردد تا هنگامِ حذف، کلِ کمپین پاک شود — وگرنه فقط خطِ آخر پاک می‌شد و
ربات با همان گپ‌ها به کارش ادامه می‌داد.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict, deque

from . import patterns as P

# --------------------------------------------------------------------------
#  آستانه‌ها (از ``patterns`` می‌آیند تا بدونِ دست‌زدن به منطق قابلِ تنظیم باشند)
# --------------------------------------------------------------------------
WINDOW = P.BEHAVIOR_WINDOW
BURST_WINDOW = P.BURST_WINDOW
BURST_MESSAGES = P.BURST_MESSAGES
TARGETS_WINDOW = P.TARGETS_WINDOW
MANY_TARGETS = P.MANY_TARGETS
CHATTER_WINDOW = P.CHATTER_WINDOW
CHATTER_MESSAGES = P.CHATTER_MESSAGES
REPEAT_COUNT = P.REPEAT_COUNT
TRAIL_WINDOW = P.TRAIL_WINDOW
TRAIL_MAX = P.TRAIL_MAX
MAX_KEYS = P.BEHAVIOR_MAX_KEYS
MAX_RECORDS = P.BEHAVIOR_MAX_RECORDS

_URL_OR_CONTACT_RE = re.compile(
    r"(@[A-Za-z0-9_]{4,}|t\.me/|telegram\.me/|wa\.me/|whatsapp|"
    r"(?:\+?\d[\d\s\-]{7,}\d))",
    re.IGNORECASE,
)


def _contains_any(text: str, phrases) -> str:
    for phrase in phrases:
        if phrase and phrase in text:
            return phrase
    return ""


class BehaviorTracker:
    """ردیابِ سبک و bounded برای رفتارِ per-(گروه، کاربر)."""

    def __init__(self, clock=time.monotonic, max_keys: int = MAX_KEYS,
                 max_records: int = MAX_RECORDS) -> None:
        self._clock = clock
        self._max_keys = max(1, int(max_keys))
        self._max_records = max(1, int(max_records))
        #: (chat_id, user_id) → deque[record]
        self._store: "OrderedDict[tuple[str, str], deque]" = OrderedDict()

    # ---------------------------------------------------------------- داخلی
    def _bucket(self, chat_id, user_id):
        key = (str(chat_id), str(user_id))
        bucket = self._store.get(key)
        if bucket is None:
            bucket = deque(maxlen=self._max_records)
            self._store[key] = bucket
            while len(self._store) > self._max_keys:
                self._store.popitem(last=False)
        else:
            self._store.move_to_end(key)
        return bucket

    def _prune(self, bucket, now):
        """رکوردهای قدیمی‌تر از بزرگ‌ترین پنجره را دور می‌ریزد."""
        keep = max(WINDOW, TRAIL_WINDOW)
        while bucket and (now - bucket[0]["ts"]) > keep:
            bucket.popleft()

    # ---------------------------------------------------------------- عمومی
    def forget_chat(self, chat_id) -> None:
        """پاک‌کردنِ وضعیتِ یک گروه (مثلاً وقتی قابلیت خاموش می‌شود)."""
        wanted = str(chat_id)
        for key in [k for k in self._store if k[0] == wanted]:
            self._store.pop(key, None)

    def reset(self) -> None:
        self._store.clear()

    def observe(self, chat_id, user_id, message_id, text,
                target_user_id=None, now=None) -> dict:
        """پیام را ثبت و سیگنال‌های رفتاری را برمی‌گرداند."""
        now = self._clock() if now is None else now
        normalized = P.normalize(text)
        bucket = self._bucket(chat_id, user_id)
        self._prune(bucket, now)

        strong = _contains_any(normalized, P.AD_CTA_PHRASES)
        weak = _contains_any(normalized, P.AD_CTA_WEAK_PHRASES)
        chit = _contains_any(normalized, P.CHITCHAT_PHRASES)
        contact = bool(_URL_OR_CONTACT_RE.search(str(text or "")))

        is_chatter = bool(chit) or (
            bool(target_user_id) and len(normalized) <= P.CHATTER_MAX_LEN
            and not strong
        )

        record = {
            "ts": now,
            "mid": message_id,
            "text": normalized,
            "cta": bool(strong),
            "contact": contact,
            "chatter": is_chatter,
            "target": None if target_user_id is None else str(target_user_id),
        }
        bucket.append(record)

        # ---- محاسبهٔ سیگنال‌ها ----
        fast = sum(1 for r in bucket if now - r["ts"] <= BURST_WINDOW)
        targets = {
            r["target"] for r in bucket
            if r["target"] and now - r["ts"] <= TARGETS_WINDOW
        }
        chatter_count = sum(
            1 for r in bucket
            if r["chatter"] and now - r["ts"] <= CHATTER_WINDOW
        )
        dup_key = normalized
        repeat_count = sum(
            1 for r in bucket
            if dup_key and r["text"] == dup_key and now - r["ts"] <= WINDOW
        )
        trail = [
            r["mid"] for r in bucket
            if r["mid"] is not None and now - r["ts"] <= TRAIL_WINDOW
        ]

        return {
            "cta": bool(strong),
            "cta_phrase": strong,
            "cta_weak": bool(weak),
            "cta_weak_phrase": weak,
            "contact": contact,
            "chatter": chatter_count >= CHATTER_MESSAGES,
            "chatter_count": chatter_count,
            "burst": fast >= BURST_MESSAGES,
            "burst_count": fast,
            "many_targets": len(targets) >= MANY_TARGETS,
            "targets_count": len(targets),
            "repeat": repeat_count >= REPEAT_COUNT,
            "repeat_count": repeat_count,
            "trail": trail[-TRAIL_MAX:],
            "window": WINDOW,
        }


#: نمونهٔ مشترکِ ردیاب (برای مسیرِ گرم؛ بی‌حالت و thread-safe به‌اندازهٔ کافی).
TRACKER = BehaviorTracker()


def observe(chat_id, user_id, message_id, text, target_user_id=None) -> dict:
    return TRACKER.observe(chat_id, user_id, message_id, text, target_user_id)


def decide(signals: dict) -> list:
    """دلایلِ «اسپمِ رفتاری» — فهرستِ خالی یعنی اسپم نیست.

    هر دلیل یک رشته است تا در لاگ معلوم شود چرا حذف شد (کفراودینگِ ساکت
    ممنوع؛ سیاستِ پروژه).
    """
    if not signals:
        return []
    reasons: list = []
    behavioural = (
        signals.get("burst")
        or signals.get("many_targets")
        or signals.get("chatter")
        or signals.get("repeat")
    )
    if signals.get("cta") and behavioural:
        reasons.append("cta+behavior")
    if (signals.get("cta_weak") and signals.get("contact") and behavioural):
        reasons.append("weak-cta+contact+behavior")
    if (signals.get("contact") and signals.get("repeat")
            and signals.get("repeat_count", 0) >= REPEAT_COUNT):
        reasons.append("contact+repeat")
    if signals.get("repeat_count", 0) >= P.REPEAT_HARD_COUNT:
        reasons.append("repeat-loop")
    if (signals.get("targets_count", 0) >= P.REPLY_BOT_TARGETS
            and signals.get("burst_count", 0) >= P.REPLY_BOT_BURST):
        # رباتِ ریپلای‌زن: به ۴ کاربرِ مختلف در ۲ دقیقه جواب می‌دهد.
        reasons.append("reply-bot")
    return reasons
