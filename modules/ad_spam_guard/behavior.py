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
FAST_GAP = P.FAST_GAP
FAST_MESSAGES = P.FAST_MESSAGES
LOW_VOCAB_MIN = P.LOW_VOCAB_MIN
LOW_VOCAB_RATIO = P.LOW_VOCAB_RATIO
BIO_BAIT_MAX_LEN = P.BIO_BAIT_MAX_LEN
BIO_BAIT_COUNT = P.BIO_BAIT_COUNT
BOT_REPLY_COUNT = P.BOT_REPLY_COUNT
BOT_REPLY_ANY = P.BOT_REPLY_ANY
SAME_TARGET_COUNT = P.SAME_TARGET_COUNT
ONE_WORD_MAX_LEN = P.ONE_WORD_MAX_LEN
ONE_WORD_REPEAT = P.ONE_WORD_REPEAT
HIT_COOLDOWN = P.HIT_COOLDOWN
RETURN_WINDOW = P.RETURN_WINDOW
RETURN_GAP = P.RETURN_GAP
RETURN_COUNT = P.RETURN_COUNT
RETURN_MAX_LEN = P.RETURN_MAX_LEN
RETURN_KEEP = P.RETURN_KEEP

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
        #: (chat_id, user_id) → زمانِ آخرین برخورد (برای کول‌داون)
        self._hits: "OrderedDict[tuple[str, str], float]" = OrderedDict()
        #: حافظهٔ بلندمدت: (chat,user) → {متن: deque[(ts, target?)]}
        #: برای «می‌رود و یکی-دو دقیقه بعد همان پیام را دوباره می‌زند».
        self._returns: "OrderedDict[tuple[str, str], OrderedDict]" = OrderedDict()

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
    # ---------------------------------------------------------------- کول‌داون
    def mark_hit(self, chat_id, user_id, now=None) -> None:
        """پس از یک برخورد: پنجرهٔ رفتاریِ این کاربر صفر می‌شود + کول‌داون.

        بدونِ این کار، «گپ‌های تکراریِ» قبلی تا ۵ دقیقه در حافظه می‌ماندند و
        کاربرِ عادی که همان لحظه دست از کار کشیده، بازهم پیامِ بعدی‌اش حذف
        می‌شد. با پاک‌شدنِ پنجره، هر اکشنِ بعدی نیاز به شاهدِ تازه دارد.
        """
        now = self._clock() if now is None else now
        key = (str(chat_id), str(user_id))
        self._hits[key] = now
        self._hits.move_to_end(key)
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        self._store.pop(key, None)

    def in_cooldown(self, chat_id, user_id, now=None) -> bool:
        """آیا این کاربر تازه جریمه شده و باید فقط شواهدِ محتوایی اکشن بدهد؟"""
        now = self._clock() if now is None else now
        key = (str(chat_id), str(user_id))
        stamp = self._hits.get(key)
        if stamp is None:
            return False
        if now - stamp > HIT_COOLDOWN:
            self._hits.pop(key, None)
            return False
        return True

    def forget_chat(self, chat_id) -> None:
        """پاک‌کردنِ وضعیتِ یک گروه (مثلاً وقتی قابلیت خاموش می‌شود)."""
        wanted = str(chat_id)
        for key in [k for k in self._store if k[0] == wanted]:
            self._store.pop(key, None)
        for key in [k for k in self._hits if k[0] == wanted]:
            self._hits.pop(key, None)
        for key in [k for k in self._returns if k[0] == wanted]:
            self._returns.pop(key, None)

    def reset(self) -> None:
        self._store.clear()
        self._hits.clear()
        self._returns.clear()

    def observe(self, chat_id, user_id, message_id, text,
                target_user_id=None, now=None, bot_user_id=None) -> dict:
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
            "greet": bool(chit) and len(normalized) <= BIO_BAIT_MAX_LEN and (
                "سلام" in normalized or normalized in P.GREETING_PHRASES
            ),
            "target": None if target_user_id is None else str(target_user_id),
            "to_bot": bool(
                bot_user_id is not None and target_user_id is not None
                and str(target_user_id) == str(bot_user_id)
            ),
            "one_word": bool(normalized) and len(normalized) <= ONE_WORD_MAX_LEN
                        and len(normalized.split()) <= 1,
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

        window_records = [r for r in bucket if now - r["ts"] <= WINDOW]

        # «سریع‌سریع ریپلای می‌کند»: چند پیام با فاصلهٔ خیلی کم.
        gaps = [
            window_records[i]["ts"] - window_records[i - 1]["ts"]
            for i in range(1, len(window_records))
        ]
        fast_count = sum(1 for gap in gaps if gap <= FAST_GAP) + (
            1 if window_records else 0
        )
        fast_replying = bool(gaps) and (
            sum(1 for gap in gaps if gap <= FAST_GAP) >= FAST_MESSAGES - 1
        )

        # «حرف‌هایش تکراری است و تمام نمی‌شود»: تنوعِ واژگانِ پایین.
        texts_in_window = [r["text"] for r in window_records if r["text"]]
        unique_count = len(set(texts_in_window))
        total_count = len(texts_in_window)
        low_vocabulary = (
            total_count >= LOW_VOCAB_MIN
            and unique_count / float(total_count) <= LOW_VOCAB_RATIO
        )

        # «فقط یک سلام می‌فرستد تا بیو را ببینید» (bio-bait).
        greeting_only = bool(chit) and len(normalized) <= BIO_BAIT_MAX_LEN and (
            "سلام" in normalized or normalized in P.GREETING_PHRASES
        )
        greeting_count = sum(
            1 for r in window_records
            if r["greet"] and now - r["ts"] <= WINDOW
        )
        greeting_targets = {
            r["target"] for r in window_records
            if r["greet"] and r["target"] and now - r["ts"] <= WINDOW
        }

        short_text = 0 < len(normalized) <= P.CHATTER_MAX_LEN

        # «رفتن و برگشتن»: همان متن بعد از یک وقفهٔ واقعی دوباره تکرار شود.
        key = (str(chat_id), str(user_id))
        history = self._returns.get(key)
        if history is None:
            history = OrderedDict()
            self._returns[key] = history
            while len(self._returns) > self._max_keys:
                self._returns.popitem(last=False)
        else:
            self._returns.move_to_end(key)
        return_count = 0
        return_gap_max = 0.0
        if normalized and len(normalized) <= RETURN_MAX_LEN:
            stamps = history.get(normalized)
            if stamps is None:
                stamps = deque(maxlen=8)
                history[normalized] = stamps
                while len(history) > RETURN_KEEP:
                    history.popitem(last=False)
            while stamps and (now - stamps[0][0]) > RETURN_WINDOW:
                stamps.popleft()
            stamps.append((now, bool(target_user_id)))
            times = [stamp for stamp, _tg in stamps]
            gaps = [times[i] - times[i - 1] for i in range(1, len(times))]
            return_count = len(times)
            return_gap_max = max(gaps) if gaps else 0.0
        had_reply = bool(
            normalized and any(
                tg for _ts, tg in (history.get(normalized) or ())
            )
        )
        returning_repeater = bool(
            return_count >= RETURN_COUNT
            and return_gap_max >= RETURN_GAP
            and had_reply
        )

        # «یک‌کلمه‌ای‌ها» و «تکرار برای همان مخاطب»
        one_word = bool(normalized) and len(normalized) <= ONE_WORD_MAX_LEN \
            and len(normalized.split()) <= 1
        one_word_count = sum(
            1 for r in window_records
            if r["one_word"] and now - r["ts"] <= WINDOW
        )
        same_text_one_word = sum(
            1 for r in window_records
            if r["one_word"] and r["text"] == normalized
            and now - r["ts"] <= WINDOW
        ) if one_word else 0

        per_target: dict = {}
        for r in window_records:
            if r["target"]:
                per_target[r["target"]] = per_target.get(r["target"], 0) + 1
        same_target_max = max(per_target.values()) if per_target else 0

        bot_records = [
            r for r in bucket
            if r["to_bot"] and now - r["ts"] <= BURST_WINDOW
        ]
        bot_replies = len(bot_records)
        bot_same_text = max(
            (sum(1 for r in bot_records if r["text"] == rr["text"])
             for rr in bot_records),
            default=0,
        )

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
            "short_text": short_text,
            "fast_replying": fast_replying,
            "fast_count": fast_count,
            "low_vocabulary": low_vocabulary,
            "unique_count": unique_count,
            "window_count": total_count,
            "greeting_bait": greeting_count >= BIO_BAIT_COUNT,
            "greeting_count": greeting_count,
            "greeting_targets": len(greeting_targets),
            "one_word": one_word,
            "one_word_count": one_word_count,
            "same_one_word_count": same_text_one_word,
            "same_target_max": same_target_max,
            "bot_replies": bot_replies,
            "bot_same_text": bot_same_text,
            "trail": trail[-TRAIL_MAX:],
            "returning_repeater": returning_repeater,
            "return_count": return_count,
            "return_gap_max": return_gap_max,
            # «برگشتن با همان پیام» مثلِ شاهدِ محتوایی حساب می‌شود تا در
            # کول‌داون هم رباتِ برگشته گرفته شود.
            "content_evidence": bool(strong or contact or returning_repeater),
            "window": WINDOW,
        }


#: نمونهٔ مشترکِ ردیاب (برای مسیرِ گرم؛ بی‌حالت و thread-safe به‌اندازهٔ کافی).
TRACKER = BehaviorTracker()


def mark_hit(chat_id, user_id) -> None:
    """ثبتِ برخورد (پنجره صفر + کول‌داون)."""
    TRACKER.mark_hit(chat_id, user_id)


def in_cooldown(chat_id, user_id) -> bool:
    """آیا کاربر تازه جریمه شده است؟"""
    return TRACKER.in_cooldown(chat_id, user_id)


def observe(chat_id, user_id, message_id, text, target_user_id=None,
            bot_user_id=None) -> dict:
    return TRACKER.observe(chat_id, user_id, message_id, text, target_user_id,
                           bot_user_id=bot_user_id)


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
    # ── «رباتِ گپ‌زن»: متنِ تکراری/کوتاه + ریپلای به کاربرانِ مختلف ──
    if (signals.get("repeat") and signals.get("short_text")
            and signals.get("targets_count", 0) >= 1):
        # یک «سلام 👋» را عمداً چند بار به شکلِ ریپلای می‌فرستد.
        reasons.append("repeat-short-reply")
    if (signals.get("repeat") and signals.get("targets_count", 0) >= 2):
        reasons.append("repeat-to-many")
    if (signals.get("greeting_bait")
            and (signals.get("greeting_targets", 0) >= 2
                 or signals.get("greeting_count", 0) >= BIO_BAIT_COUNT + 1)):
        # «سلام»های کوتاه به چند نفر ⇒ بیو-چک.
        reasons.append("bio-bait")
    if (signals.get("low_vocabulary")
            and signals.get("targets_count", 0) >= 1):
        # حرف‌های تکراری که تمام نمی‌شوند.
        reasons.append("low-vocabulary")
    if (signals.get("fast_replying") and signals.get("chatter")
            and signals.get("targets_count", 0) >= 1):
        # سریع‌سریع به بقیه جواب می‌دهد و حرفش هم الکی است.
        reasons.append("fast-replier")
    # ── «به ربات روباه / به یک نفر زیاد ریپلای می‌کند و یک جمله را تکرار
    #    می‌کند» — اکثراً جمله‌های یک‌کلمه‌ای ──
    if signals.get("one_word") and signals.get("same_one_word_count", 0) >= ONE_WORD_REPEAT:
        reasons.append("one-word-repeat")
    if (signals.get("same_target_max", 0) >= SAME_TARGET_COUNT
            and (signals.get("one_word") or signals.get("repeat")
                 or signals.get("short_text"))):
        # همان جمله را چند بار برای **یک** مخاطب (کاربر یا ربات) می‌فرستد.
        reasons.append("same-target-spam")
    if (signals.get("bot_same_text", 0) >= BOT_REPLY_COUNT
            or signals.get("bot_replies", 0) >= BOT_REPLY_ANY):
        # «ربات‌ها بیشتر روی رباتِ روباه ریپلای می‌کنند» — این خودش امضای
        # ربات است: کاربرِ واقعی چند بار پشتِ‌سرهم به ربات جواب نمی‌دهد.
        reasons.append("bot-reply-spam")
    if signals.get("returning_repeater"):
        # «یک-دو دقیقه بعد برمی‌گردد و همان ریپلای/همان پیام را تکرار می‌کند»
        reasons.append("returning-repeater")
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
            and signals.get("burst_count", 0) >= P.REPLY_BOT_BURST
            and (signals.get("chatter") or signals.get("low_vocabulary"))):
        # رباتِ ریپلای‌زن: به ۳ کاربرِ مختلف در ۲ دقیقه جواب می‌دهد و
        # حرفش هم «گپِ الکی» است (وگرنه ممکن است کاربرِ عادی باشد).
        reasons.append("reply-bot")
    return reasons
