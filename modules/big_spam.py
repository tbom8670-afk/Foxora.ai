"""Early Big Spam detection and delete-batch sizing.

Pure helpers: no client, no queue, no moderation policy besides detection.

Two detection paths stay separate:

* Intra-message Big Spam — a single payload packed with the same stem/phrase.
* Short separate promotional repeats — four standalone ad tokens in a row.
* Multi-message promotional wave — several clearly promotional messages that
  collapse to the same campaign after normalization.

Raw text is never the comparison key. IDs still come from the tracker rows.
"""
import re
from collections import Counter
from difflib import SequenceMatcher

REPEAT_WINDOW_SECONDS = 60
SIMILAR_MESSAGE_THRESHOLD = 2
WAVE_SHORT_THRESHOLD = 4
SHORT_SEPARATE_THRESHOLD = 4
PHRASE_REPEAT_THRESHOLD = 6  # more than 5 meaningful phrases
PACKED_TOKEN_THRESHOLD = 6
LARGE_PAYLOAD_CHARS = 120
DELETE_BATCH_MAX = 100
# Generic text floods are detected before the heavy handler/dispatcher.  Four
# identical non-trivial messages are enough even when no ad marker is known.
# This is intentionally sender-scoped (the caller supplies one user's rows): a busy
# group never combines different members into one incident.
IDENTICAL_MESSAGE_THRESHOLD = 4
IDENTICAL_MIN_CHARS = 3
GENERIC_FLOOD_WINDOW_SECONDS = 10
GENERIC_FLOOD_MESSAGE_THRESHOLD = 10
GENERIC_FLOOD_DISTINCT_MAX = 4
LOW_ENTROPY_MESSAGE_THRESHOLD = 3
LOW_ENTROPY_MIN_CHARS = 5

# ---------------------------------------------------------------------------
# 🎨 اسپم تزئینی — الگوهایی مثل «▃▅▆█ 웃 - 웃 █▆▅▃» و «■■□□□ 40%» که
# normalize آن‌ها را خالی می‌کند و از همهٔ فیلترها رد می‌شدند. این‌ها
# قالب‌های آمادهٔ تبلیغ‌اند و از «اولین پیام» موج حساب می‌شوند.
# ---------------------------------------------------------------------------
_DECORATIVE_RANGES = (
    (0x2500, 0x257F),   # Box Drawing ─ ═ ║ ╔ ...
    (0x2580, 0x259F),   # Block Elements ▃ ▅ ▆ █ ...
    (0x25A0, 0x25FF),   # Geometric Shapes ■ □ ● ◆ ...
    (0x2716, 0x2716),
    (0x2726, 0x2729),   # ✦ ✧ ...
    (0x2B1B, 0x2B1C),   # ⬛ ⬜
    (0x1100, 0x11FF),   # Hangul Jamo (تزئینی مثل 웃)
    (0x3130, 0x318F),
    (0xAC00, 0xD7AF),   # Hangul Syllables
)
_DECORATIVE_EXTRA = frozenset("▰▱◾◽▪▫★☆➖➕〰❚❙❘➤➣►◄\u0640")
# ران‌های بی‌خطر که کاربران عادی زیاد می‌فرستند؛ هرگز اسپم حساب نمی‌شوند.
_INNOCENT_RUN_CHARS = frozenset(
    ".!?؟،,…-_*~():;\"'`^#\u2764\u2665"
) | frozenset("😂🤣😹😅😭😍🥰❤♥🙂😐🗿💔✨⭐🌟")
_RUN_RE = re.compile(r"(.)\1{4,}")  # پنج بار یا بیشتر همان کاراکتر
DECORATIVE_MIN_SYMBOLS = 4


def _is_decorative_char(ch):
    if ch in _DECORATIVE_EXTRA:
        return True
    code = ord(ch)
    return any(low <= code <= high for low, high in _DECORATIVE_RANGES)


def decorative_spam(text):
    """True برای قالب‌های تزئینی تبلیغاتی؛ از اولین پیام."""
    raw = str(text or "")
    if not raw.strip():
        return False
    decorative = sum(1 for ch in raw if _is_decorative_char(ch))
    if decorative >= DECORATIVE_MIN_SYMBOLS:
        return True
    for match in _RUN_RE.finditer(raw):
        ch = match.group(1)
        if ch.isalnum() or ch.isspace():
            continue
        if ch in _INNOCENT_RUN_CHARS:
            continue
        return True
    return False

_AD_MARKERS = (
    "بیو چک",
    "چک بیو",
    "بیوچک",
    "بیا پیوی",
    "بیا پی وی",
    "بیا پیویم",
    "فیلم گذاشتم",
    "فیلم گذاشتم بیوم",
    "عضو شو",
    "جوین شو",
    "جوین کانال",
    "اد پیوی",
    "اد پی وی",
    "پیوی پیام",
    "حال پی",
    "تمام سانسور",
    "حال میدم",
    "فیلم پی",
    "🔞",
)

_COMPACT_STEMS = (
    "بیوچک",
    "چکبیو",
    "بیاپیوی",
    "بیاپیویم",
    "بیاگروه",
    "بیاچنل",
    "بیاکانال",
    "فالوکن",
    "فولوکن",
    "جوینشو",
    "جوینکن",
    "جوینکانال",
    "عضوشو",
    "فیلمگذاشتم",
    "فیلمدارم",
    "ادپیوی",
    "تادیرنشده",
    "بکوب",
    "حالپی",
    "تمامسانسور",
    "حالمیدم",
    "فیلمپی",
)

_PROMO_RE = re.compile(
    r"بیو\s*چک|چک\s*بیو|"
    r"بیا\s*پی\s*وی|"
    r"بیا\s*(?:گروه|چنل|کانال|پیویم)|"
    r"فالو\s*کن|فولو\s*کن|"
    r"(?:جوین|عضو)\s*(?:شو|کن|کانال)|"
    r"فیلم\s*(?:گذاشتم|دارم|جدید)|"
    r"اد\s*پی\s*وی|"
    r"تا\s*دیر\s*نشده|بکوب|"
    r"حال\s*پی|تمام\s*سانسور|حال\s*می(?:د|ذ)م|فیلم\s*پی|🔞"
)

_TRANSLATE = str.maketrans({
    "ي": "ی",
    "ى": "ی",
    "ئ": "ی",
    "ك": "ک",
    "ة": "ه",
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ؤ": "و",
    "\u0640": "",
    "\u200c": " ",
    "\u200d": " ",
    "\u200f": "",
    "\u200e": "",
    "\u202a": "",
    "\u202b": "",
    "\u202c": "",
    "\u202d": "",
    "\u202e": "",
    "\u064b": "",
    "\u064c": "",
    "\u064d": "",
    "\u064e": "",
    "\u064f": "",
    "\u0650": "",
    "\u0651": "",
    "\u0652": "",
    "\u0653": "",
    "\u0654": "",
    "\u0655": "",
    "\ufe0f": "",
    "\ufe0e": "",
})

_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_REPEAT_RE = re.compile(r"(.)\1+", re.UNICODE)
_TOKEN_RE = re.compile(r"[\wآ-ی]+", re.UNICODE)

_SHORT_CHAT = frozenset({
    "سلام", "سللام", "خوبی", "خوبین", "چطوری", "ممنون", "مرسی",
    "باشه", "آره", "اره", "نه", "خیلی", "اوکی", "ok", "صبح",
    "بخیر", "شب", "خداحافظ", "امروز",
})

# Whole-message tokens only. Never match these as a substring of a sentence.
_SHORT_PROMO_TOKENS = frozenset({
    "جوین", "بیوچک", "چکبیو", "فیلم", "نود", "پیوی", "پیویم",
    "لینک", "فالو", "فولو", "بکوب",
})


def normalize_text(text):
    """Stable comparison form: drop decorations, collapse stretched letters."""
    value = str(text or "").translate(_TRANSLATE).lower()
    value = _NON_WORD_RE.sub(" ", value)
    value = _REPEAT_RE.sub(r"\1", value)
    return " ".join(value.split())


def compact_text(text):
    return re.sub(r"\s+", "", normalize_text(text))


def tokens(text):
    return _TOKEN_RE.findall(normalize_text(text))


def flood_fingerprint(text):
    """Comparison key for arbitrary rapid floods.

    Normal campaign comparison collapses stretched letters completely.  That
    is useful for ads, but strings such as ``خخخخخ`` would become one character
    and evade the generic duplicate path.  A flood key keeps at most two copies
    of a stretched character while still dropping spaces/decorations.
    """
    value = str(text or "").translate(_TRANSLATE).lower()
    value = _NON_WORD_RE.sub("", value)
    value = re.sub(r"(.)\1{2,}", r"\1\1", value, flags=re.UNICODE)
    return value


def _alnum_chars(text):
    value = str(text or "").translate(_TRANSLATE).lower()
    return [ch for ch in value if ch.isalnum()]


def low_entropy_text(text):
    """True for repeated-letter/gibberish payloads, not one normal short word."""
    chars = _alnum_chars(text)
    if len(chars) < LOW_ENTROPY_MIN_CHARS:
        return False
    counts = Counter(chars)
    dominant = max(counts.values())
    # A long payload made from one/two characters, or one character occupying
    # at least two thirds of it, is a strong flood signal only when repeated
    # across several messages (never from this predicate alone).
    return (
        (len(counts) <= 2 and len(chars) >= 7)
        or dominant / len(chars) >= 0.66
    )


def _innocent_short_message(text):
    toks = tokens(text)
    if not toks or len(toks) > 3:
        return False
    short_keys = {compact_text(word) for word in _SHORT_CHAT}
    return all(token in _SHORT_CHAT or compact_text(token) in short_keys for token in toks)


def is_contentful(text):
    """Skip greetings and single-word chatter; keep real repeated campaigns."""
    toks = tokens(text)
    distinct = [token for token in dict.fromkeys(toks)]
    if not distinct:
        return False
    if looks_promotional(text) and (len(distinct) >= 2 or len(compact_text(text)) >= 6):
        return True
    if len(distinct) < 2:
        return False
    if len(toks) <= 2 and all(token in _SHORT_CHAT or len(token) <= 2 for token in distinct):
        return False
    compact = compact_text(text)
    if len(toks) >= 3 and len(compact) >= 8:
        return True
    if len(distinct) >= 2 and len(compact) >= 14:
        return True
    return False


_COOKED_MARKERS = None


def _cooked_markers():
    """``(marker, normalized, normalized-without-spaces)`` — built once.

    The marker list is a constant; normalising all of it again for every
    incoming message was the single largest CPU cost of the spam check.
    """
    global _COOKED_MARKERS
    if _COOKED_MARKERS is None:
        rows = []
        for marker in _AD_MARKERS:
            cooked = normalize_text(marker)
            if cooked:
                rows.append((marker, cooked, cooked.replace(" ", "")))
        _COOKED_MARKERS = tuple(rows)
    return _COOKED_MARKERS


def looks_promotional(text):
    raw = str(text or "")
    if "🔞" in raw:
        return True
    normalized = normalize_text(text)
    if not normalized:
        return False
    compact = compact_text(normalized)
    for _marker, cooked, squeezed in _cooked_markers():
        if cooked in normalized or squeezed in compact:
            return True
    if _PROMO_RE.search(normalized):
        return True
    return any(stem and stem in compact for stem in _COMPACT_STEMS)


def markers_in(text):
    normalized = normalize_text(text)
    compact = compact_text(normalized)
    found = []
    for marker, cooked, squeezed in _cooked_markers():
        if cooked in normalized or squeezed in compact:
            found.append(marker)
    return found


def _usable_token(token):
    return token not in _SHORT_CHAT and len(token) >= 3


def max_token_repeats(text):
    """Most-repeated non-chat token inside one message."""
    counts = Counter(token for token in tokens(text) if _usable_token(token))
    return max(counts.values()) if counts else 0


def max_phrase_repeats(text):
    """Count the most-repeated 2–5 token phrase in one message."""
    toks = tokens(text)
    if len(toks) < 2:
        return 0
    highest = 0
    for size in (2, 3, 4, 5):
        if len(toks) < size:
            break
        counts = Counter()
        for index in range(len(toks) - size + 1):
            piece = toks[index:index + size]
            if len(set(piece)) < 2:
                continue
            if all(part in _SHORT_CHAT for part in piece):
                continue
            if sum(len(token) for token in piece) < 4:
                continue
            counts[" ".join(piece)] += 1
        if counts:
            highest = max(highest, max(counts.values()))
    return highest


def max_compact_repeats(text):
    """Catch packed stems even when the sender omitted spaces."""
    toks = tokens(text)
    if toks and all(token in _SHORT_CHAT for token in toks):
        return 0
    compact = compact_text(text)
    length = len(compact)
    if length < 9:
        return 0
    highest = 0
    seen = set(toks)
    for token in seen:
        if _usable_token(token):
            highest = max(highest, compact.count(token))
    limit = min(16, length // 2)
    for size in range(3, limit + 1):
        for offset in range(size):
            piece = compact[offset:offset + size]
            if len(piece) < 3 or piece in _SHORT_CHAT or piece in seen:
                continue
            if not _TOKEN_RE.fullmatch(piece):
                continue
            highest = max(highest, compact.count(piece))
        if highest >= PACKED_TOKEN_THRESHOLD:
            return highest
    return highest


def intra_message_spam(text):
    """True only when one payload itself is a packed repeat."""
    raw = str(text or "").strip()
    if not raw:
        return False
    token_repeats = max_token_repeats(raw)
    phrase_repeats = max_phrase_repeats(raw)
    compact_repeats = max_compact_repeats(raw)
    if token_repeats >= PACKED_TOKEN_THRESHOLD:
        return True
    if phrase_repeats >= PHRASE_REPEAT_THRESHOLD:
        return True
    if compact_repeats >= PACKED_TOKEN_THRESHOLD:
        return True
    # A known advertising phrase repeated in one message does not need six
    # copies.  Two copies are already unambiguous while a single «بیو چک» is
    # still left to the normal word-filter/warning policy.
    if looks_promotional(raw) and (token_repeats >= 2 or phrase_repeats >= 2):
        return True
    return False


def _ngrams(toks, size):
    return {" ".join(toks[index:index + size]) for index in range(len(toks) - size + 1)}


def shares_phrase(left, right):
    """True when two texts share a consecutive 2–4 word phrase."""
    first = tokens(left)
    second = tokens(right)
    for size in (4, 3, 2):
        if len(first) < size or len(second) < size:
            continue
        shared = _ngrams(first, size) & _ngrams(second, size)
        if not shared:
            continue
        if size >= 3:
            return True
        for phrase in shared:
            parts = phrase.split()
            if len(set(parts)) < 2:
                continue
            if all(part in _SHORT_CHAT for part in parts):
                continue
            return True
    return False


def similarity_score(left, right):
    """0..1 similarity on the normalized form only."""
    first = normalize_text(left)
    second = normalize_text(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    compact_first = first.replace(" ", "")
    compact_second = second.replace(" ", "")
    if compact_first == compact_second:
        return 1.0
    if first in second or second in first:
        shorter = first if len(first) <= len(second) else second
        longer = second if shorter is first else first
        return len(shorter) / max(len(longer), 1)
    if compact_first in compact_second or compact_second in compact_first:
        shorter = compact_first if len(compact_first) <= len(compact_second) else compact_second
        longer = compact_second if shorter is compact_first else compact_first
        return len(shorter) / max(len(longer), 1)
    return max(
        SequenceMatcher(None, first, second).ratio(),
        SequenceMatcher(None, compact_first, compact_second).ratio(),
    )


def similar_promotional(left, right):
    """True when two promotional texts are clearly the same campaign."""
    if not looks_promotional(left) or not looks_promotional(right):
        return False
    first = normalize_text(left)
    second = normalize_text(right)
    if not first or not second:
        return False
    if first == second or compact_text(first) == compact_text(second):
        return True
    score = similarity_score(left, right)
    if score >= 0.72:
        return True
    if shares_phrase(left, right):
        return True
    if set(markers_in(left)) & set(markers_in(right)):
        return True
    compact_first = compact_text(first)
    compact_second = compact_text(second)
    if (
        compact_first and compact_second
        and min(len(compact_first), len(compact_second)) >= 5
        and (compact_first in compact_second or compact_second in compact_first)
    ):
        return True
    return False


def similar_repeat(left, right):
    """Legacy helper: same campaign after normalization.

    Multi-message Big Spam no longer uses this for ordinary chat. Promotional
    waves go through ``similar_promotional`` instead.
    """
    if not is_contentful(left) or not is_contentful(right):
        return False
    score = similarity_score(left, right)
    if score >= 0.78:
        return True
    if score >= 0.52 and shares_phrase(left, right):
        return True
    return False


def is_strong_promotional(text):
    """Multi-word / packed ads can start a wave on the second copy."""
    if not looks_promotional(text):
        return False
    toks = tokens(text)
    compact = compact_text(text)
    if len(toks) >= 2:
        return True
    # Compact «بیوچک» is an explicit campaign token, not ordinary use of a
    # generic word such as «فیلم» or «لینک»; detect its second copy too.
    if compact in {"بیوچک", "چکبیو"}:
        return True
    if len(compact) >= 10:
        return True
    promo_hits = sum(1 for token in toks if looks_promotional(token))
    if promo_hits >= 2:
        return True
    if any(compact.count(stem) >= 2 for stem in _COMPACT_STEMS):
        return True
    return False


def wave_needed(text):
    """How many similar promotional messages are required, or None."""
    if not looks_promotional(text):
        return None
    if is_strong_promotional(text):
        return SIMILAR_MESSAGE_THRESHOLD
    return WAVE_SHORT_THRESHOLD



def short_promo_key(text):
    """Stable key when the whole message is one short ad token.

    Ordinary sentences that merely contain «فیلم» or «گروه» stay None.
    """
    toks = tokens(text)
    compact = compact_text(text)
    if not compact or not toks:
        return None
    if any(token in _SHORT_CHAT for token in toks):
        return None
    if len(toks) == 1 and toks[0] in _SHORT_PROMO_TOKENS:
        return toks[0]
    if compact in _SHORT_PROMO_TOKENS:
        return compact
    if len(toks) <= 2 and compact in _SHORT_PROMO_TOKENS:
        return compact
    return None


def consecutive_short_promo_rows(text, in_window):
    """Newest run of the same short promotional token, after normalization."""
    key = short_promo_key(text)
    if not key:
        return []
    matched = []
    for row in reversed(list(in_window or ())):
        if short_promo_key(row.get("text", "")) == key:
            matched.append(row)
        else:
            break
    matched.reverse()
    return matched


def _row_ids(rows):
    return {
        row.get("message_id") for row in rows
        if isinstance(row.get("message_id"), int) and row["message_id"] > 0
    }


def detect_big_spam(text, recent_rows, *, now=None, allow_generic=True):
    """Return ``(is_big, reason, ids)`` from the current text and tracker rows.

    ``recent_rows`` should already include the current message. On a hit the
    IDs contain the detected sender-scoped wave only: matching generic spam or
    promotional payloads, never unrelated ordinary rows retained by tracker.
    """
    raw = str(text or "").strip()
    if not raw:
        return False, "", set()

    window = REPEAT_WINDOW_SECONDS
    stamp = time_now(now)
    in_window = [
        row for row in recent_rows
        if (stamp - float(row.get("timestamp", stamp))) <= window
    ]
    current_rows = (in_window or list(recent_rows or ()))[-1:]

    if intra_message_spam(raw):
        # A packed payload must not sweep preceding ordinary chat. If it is an
        # explicit promotion, however, retain every other promotional payload
        # from this sender's active window as part of the same wave.
        selected = current_rows
        if looks_promotional(raw):
            selected = [
                row for row in in_window
                if looks_promotional(row.get("text", ""))
            ]
        return True, "repeated_promotional_phrase", _row_ids(selected)

    # 🎨 قالب تزئینی تبلیغاتی: از اولین پیام موج حساب می‌شود.
    if decorative_spam(raw):
        selected = current_rows
        if looks_promotional(raw):
            selected = [
                row for row in in_window
                if looks_promotional(row.get("text", ""))
            ]
        return True, "decorative_spam", _row_ids(selected)

    short_wave = consecutive_short_promo_rows(raw, in_window)
    if len(short_wave) >= SHORT_SEPARATE_THRESHOLD:
        return True, "repeated_short_promotional_messages", _row_ids(short_wave)

    # Generic flood detection belongs here, before GroupDispatcher.  The older
    # copy lived near the end of the heavy handler, so a burst could fill the
    # queue or return through a word-filter branch before ever reaching it.
    flood_rows = [
        row for row in in_window
        if (stamp - float(row.get("timestamp", stamp))) <= GENERIC_FLOOD_WINDOW_SECONDS
    ]
    flood_key = flood_fingerprint(raw)
    if (
        allow_generic
        and len(flood_key) >= IDENTICAL_MIN_CHARS
        and not _innocent_short_message(raw)
    ):
        identical = [
            row for row in flood_rows
            if flood_fingerprint(row.get("text", "")) == flood_key
        ]
        if len(identical) >= IDENTICAL_MESSAGE_THRESHOLD:
            if wave_needed(raw) is not None:
                promotional_rows = [
                    row for row in in_window
                    if looks_promotional(row.get("text", ""))
                ]
                return (
                    True,
                    "repeated_promotional_messages",
                    _row_ids(promotional_rows),
                )
            return True, "repeated_identical_messages", _row_ids(identical)

    # Obfuscated nonsense often changes one stretched letter on every line,
    # so exact equality is deliberately not required for this low-entropy
    # path.  Three sender-scoped messages in ten seconds are enough.
    if allow_generic and low_entropy_text(raw):
        low_entropy_rows = [
            row for row in flood_rows
            if low_entropy_text(row.get("text", ""))
        ]
        if len(low_entropy_rows) >= LOW_ENTROPY_MESSAGE_THRESHOLD:
            return True, "repeated_gibberish_messages", _row_ids(low_entropy_rows)

    # Ten messages from one member using only a few rotating payloads are a
    # flood.  Requiring a bounded distinct set protects fast game answers or a
    # real conversation whose messages are all different. Different members
    # are never combined.
    flood_keys = {
        flood_fingerprint(row.get("text", ""))
        for row in flood_rows
        if flood_fingerprint(row.get("text", ""))
    }
    if (
        allow_generic
        and len(flood_rows) >= GENERIC_FLOOD_MESSAGE_THRESHOLD
        and len(flood_keys) <= GENERIC_FLOOD_DISTINCT_MAX
    ):
        return True, "rapid_message_flood", _row_ids(flood_rows)

    needed = wave_needed(raw)
    if needed is not None:
        promotional_rows = [
            row for row in in_window
            if looks_promotional(row.get("text", ""))
        ]
        promo = [
            row for row in promotional_rows
            if similar_promotional(raw, row.get("text", ""))
        ]
        if len(promo) >= needed:
            # Preserve every promotional payload in the sender's active
            # window, including heavily obfuscated variants, but never pull in
            # unrelated ordinary chat merely because it is still in tracker.
            return True, "repeated_promotional_messages", _row_ids(promotional_rows)

    return False, "", set()


def time_now(now=None):
    import time
    return time.time() if now is None else now


def chunk_ids(message_ids, batch_size=DELETE_BATCH_MAX):
    """Split IDs into immediate batches. Size is a max, never a start gate."""
    pending = sorted({
        message_id for message_id in message_ids
        if isinstance(message_id, int) and message_id > 0
    })
    if not pending:
        return []
    size = max(int(batch_size), 1)
    return [pending[index:index + size] for index in range(0, len(pending), size)]
