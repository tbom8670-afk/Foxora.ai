# -*- coding: utf-8 -*-
"""⚖️ قاضی داخلی دادگاه روباه — تصمیم‌گیری عادلانه بر پایهٔ شواهد، بدون هوش مصنوعی.

این ماژول جای «قاضی هوش مصنوعی» را گرفته است: **هیچ تماس شبکه‌ای، هیچ کلید
API و هیچ سرویس بیرونی** در داوری وجود ندارد. خودِ ربات پرونده را می‌خواند
و بر اساس شواهد متنِ بازیکنان رأی می‌دهد.

معیارهای داوری (همه قطعی و قابل بازبینی):

1. **مستند بودن**: وجود مدرک مشخص (دوربین، فیلم، رسید، پیام، سند، …) و
   دلیل/شاهد برای زمان حادثه.
2. **هم‌خوانی محتوایی شهادت‌ها**: هم‌پوشانی واژه‌های شهادت با اظهارات همان
   طرف. صرفِ «طرفداری» یا شهادت خالی ارزشی ندارد.
3. **اعتراف متهم**: پذیرش صریح، قوی‌ترین شاهد علیه متهم است.
4. **انسجام اظهارات**: متن کوتاه و بی‌جزئیات وزن کمتری دارد.
5. **دفاعیهٔ وکیل**: اشاره به تناقض/ابهام/نبود مدرک، وزن دفاع را بالا می‌برد.
6. **اصل برائت و شدت جرم**: برای جرایم سنگین‌تر (قتل، کلاهبرداری) آستانهٔ
   اثبات بالاتر است؛ در نبود شواهد کافی، رأی به نفع متهم است.

شناسه‌ها هرگز ساخته نمی‌شوند: برنده و متهم از رکورد واقعی پرونده
(``case["players"]``) خوانده می‌شوند و کلید ``case_id`` همان کلید پرونده است.
"""
from __future__ import annotations

import re

VERDICT_PLAINTIFF = "plaintiff"
VERDICT_DEFENDANT = "defendant"

# منبع رأی: همیشه قاضی داخلی ربات.
SOURCE_LOCAL = "local"
SOURCE_FALLBACK = SOURCE_LOCAL  # نام قدیمی، برای سازگاری

JUDGE_NAME = "قاضی داخلی دادگاه روباه"

MAX_FIELD_CHARS = 1500
MAX_REASON_CHARS = 180


class JudgeError(Exception):
    """رکورد پرونده ناقص است و رأی معتبر ساخته نمی‌شود."""


# ---------------------------------------------------------------------------
# ابزارهای متنی
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[\w\u0600-\u06FF]+")

_STOPWORDS = {
    "که", "را", "در", "به", "از", "این", "آن", "با", "برای", "من", "تو",
    "او", "ما", "شما", "است", "بود", "شد", "کرد", "می", "های", "ها", "یک",
    "دو", "تا", "هم", "یا", "اگر", "چون", "ولی", "اما", "خود", "داشت",
    "دارد", "کردم", "شده", "باشد", "نه", "بله", "چرا", "چه", "همان",
    "کسی", "چیز", "بار", "موقع", "روز", "حال", "شده‌ام", "بودم",
}

# مدرک/سند مشخص — قوی‌ترین نوع شاهد در پرونده.
_EVIDENCE_WORDS = (
    "دوربین", "دوربین مداربسته", "مداربسته", "فیلم", "ویدیو", "عکس",
    "تصویر", "ضبط", "صدا", "ویس", "مدرک", "مدارک", "سند", "رسید", "فیش",
    "پرینت", "پیام", "چت", "اسکرین", "اثر انگشت", "آزمایش", "پزشکی قانونی",
    "گزارش", "گواهی", "شماره پلاک", "کد رهگیری", "صورت‌حساب",
)

# پذیرش صریح متهم (قوی‌ترین شاهد علیه خودش).
_ADMISSION_WORDS = (
    "قبول دارم", "قبول می‌کنم", "قبول میکنم", "اعتراف", "اعتراف می‌کنم",
    "تقصیر من", "بله من", "من انجام دادم", "من دزدیدم", "من زدم",
    "درست می‌گوید", "درست میگه", "همان‌طور که گفت", "همان طور که گفت",
    "گناهکارم", "کار من بوده",
)

# دلیل/آلبای برای زمان حادثه.
_ALIBI_WORDS = (
    "نبودم", "شهر دیگری", "آلبای", "شاهد دارم", "شاهدم", "مدرک دارم",
    "دلیل دارم", "کارت بانکی", "دور بودم", "خانه بودم", "سفر بودم",
    "بیمارستان", "مرخصی", "شیفت", "لوکیشن", "مدارک پزشکی",
)

# واژه‌های تحلیلِ دفاعیهٔ وکیل.
_LAWYER_CONTRADICTION = (
    "تناقض", "ناسازگار", "اختلاف", "ابهام", "مدرک ندارد", "مستند نیست",
    "بدون مدرک", "غیرقابل اثبات", "شهادت کذب",
)
_LAWYER_STRENGTH = (
    "مدرک", "سند", "گزارش", "شاهد", "آلبای", "فیش", "رسید",
)

# آستانهٔ اثبات بر اساس شدت جرم (هر چه سنگین‌تر، سخت‌گیرانه‌تر).
_CRIME_THRESHOLDS = {
    "قتل": 1.6,
    "کلاهبرداری": 1.35,
    "دزدی": 1.0,
    "دعوا و درگیری": 0.9,
    "دعوا": 0.9,
    "توهین": 0.8,
}
_DEFAULT_THRESHOLD = 1.0

_DEFAULT_REASONS = (
    "مجموع اظهارات دو طرف بررسی شد",
    "شهادت شاهدان بر پایهٔ محتوا سنجیده شد",
    "اصل برائت و کافی نبودن شواهد رعایت شد",
)


def _tokens(text) -> set:
    return {
        word for word in _WORD_RE.findall(str(text or ""))
        if len(word) > 2 and word not in _STOPWORDS
    }


def _hits(text, words) -> int:
    haystack = str(text or "")
    return sum(1 for word in words if word in haystack)


def _found_terms(text, words) -> list:
    """واژه‌های یافت‌شده، بدون تکرارِ تو در تو («دوربین» و «دوربین مداربسته»)."""
    haystack = str(text or "")
    found = [word for word in words if word in haystack]
    longest_first = sorted(found, key=len, reverse=True)
    unique = []
    for word in longest_first:
        if any(word in kept for kept in unique):
            continue
        unique.append(word)
    return sorted(unique, key=found.index)


def _sanitize(value) -> str:
    text = str(value or "").replace("\x00", " ")
    return " ".join(text.split())[:MAX_FIELD_CHARS]


def _clean_reason(value) -> str:
    text = " ".join(str(value or "").replace("<", " ").replace(">", " ").split())
    return text.strip(" \"'`*-•")[:MAX_REASON_CHARS]


# ---------------------------------------------------------------------------
# تحلیل پرونده (قطعی، بدون شبکه)
# ---------------------------------------------------------------------------
def case_analysis(case) -> dict:
    """تحلیل کامل و قابل بازبینی یک پرونده."""
    case = case or {}
    sections = case.get("sections") or {}
    complaint = sections.get("complaint") or ""
    defense = sections.get("defense") or ""
    witness_p = sections.get("witness_p") or ""
    witness_d = sections.get("witness_d") or ""
    lawyer = sections.get("lawyer") or ""
    crime = str(case.get("crime") or "").strip()

    complaint_tokens = _tokens(complaint)
    defense_tokens = _tokens(defense)
    witness_p_tokens = _tokens(witness_p)
    witness_d_tokens = _tokens(witness_d)

    overlap_p = len(witness_p_tokens & complaint_tokens)
    overlap_d = len(witness_d_tokens & defense_tokens)

    complaint_evidence = _found_terms(complaint, _EVIDENCE_WORDS)
    witness_p_evidence = _found_terms(witness_p, _EVIDENCE_WORDS)
    defense_evidence = _found_terms(defense, _ALIBI_WORDS)
    witness_d_evidence = _found_terms(witness_d, _ALIBI_WORDS)
    admission = _found_terms(defense, _ADMISSION_WORDS)
    lawyer_contradiction = _found_terms(lawyer, _LAWYER_CONTRADICTION)
    lawyer_strength = _found_terms(lawyer, _LAWYER_STRENGTH)

    plaintiff_score = 0.0
    defendant_score = 0.0

    # ۱) انسجام و جزئیات اظهارات (سقف‌دار، تا متن بلند تقلب نباشد).
    plaintiff_score += min(2.0, len(complaint_tokens) / 6.0)
    defendant_score += min(2.0, len(defense_tokens) / 6.0)

    # ۲) مستند بودن.
    plaintiff_score += 0.8 * min(3, len(complaint_evidence))
    plaintiff_score += 0.5 * min(2, len(witness_p_evidence))
    defendant_score += 0.7 * min(3, len(defense_evidence))
    defendant_score += 0.4 * min(2, len(witness_d_evidence))

    # ۳) هم‌خوانی محتوایی شهادت‌ها (طرفداری تنها ارزش ندارد).
    plaintiff_score += 0.7 * min(3, overlap_p)
    defendant_score += 0.7 * min(3, overlap_d)
    if len(witness_p_tokens) < 3:
        plaintiff_score -= 0.6
    if len(witness_d_tokens) < 3:
        defendant_score -= 0.6

    # ۴) اعتراف متهم.
    plaintiff_score += 1.5 * min(2, len(admission))

    # ۵) دفاعیهٔ وکیل.
    defendant_score += 0.5 * min(2, len(lawyer_contradiction))
    defendant_score += 0.3 * min(2, len(lawyer_strength))
    if len(_tokens(lawyer)) >= 4:
        defendant_score += 0.4

    threshold = _CRIME_THRESHOLDS.get(crime, _DEFAULT_THRESHOLD)
    verdict = (VERDICT_PLAINTIFF
               if plaintiff_score >= defendant_score + threshold
               else VERDICT_DEFENDANT)

    gap = abs(plaintiff_score - defendant_score)
    confidence = round(0.5 + min(0.4, gap / 10.0), 2)
    if verdict == VERDICT_DEFENDANT and plaintiff_score < threshold:
        # نبود شواهد کافی، خودش یک تصمیم مطمئن است (اصل برائت).
        confidence = round(max(confidence, 0.55), 2)

    return {
        "verdict": verdict,
        "confidence": confidence,
        "crime": crime,
        "threshold": threshold,
        "scores": {"plaintiff": round(plaintiff_score, 2),
                   "defendant": round(defendant_score, 2)},
        "gap": round(gap, 2),
        "complaint_tokens": len(complaint_tokens),
        "defense_tokens": len(defense_tokens),
        "witness_p_tokens": len(witness_p_tokens),
        "witness_d_tokens": len(witness_d_tokens),
        "lawyer_tokens": len(_tokens(lawyer)),
        "overlap_witness_p": overlap_p,
        "overlap_witness_d": overlap_d,
        "complaint_evidence": complaint_evidence[:4],
        "witness_p_evidence": witness_p_evidence[:4],
        "defense_evidence": defense_evidence[:4],
        "witness_d_evidence": witness_d_evidence[:4],
        "admission": admission[:3],
        "lawyer_contradiction": lawyer_contradiction[:3],
    }


def _quote_terms(terms) -> str:
    unique = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return " و ".join(f"«{term}»" for term in unique[:2])


def _pick_reasons(analysis, verdict) -> list:
    """سه دلیل کوتاه که به شواهد واقعی پرونده اشاره می‌کند."""
    reasons = []
    if verdict == VERDICT_PLAINTIFF:
        if analysis["admission"]:
            reasons.append("متهم در اظهارات خود موضوع را پذیرفته است")
        evidence = (analysis["complaint_evidence"]
                    or analysis["witness_p_evidence"])
        if evidence:
            reasons.append(
                f"شکایت به مدرک {_quote_terms(evidence)} مستند است")
        if analysis["overlap_witness_p"] >= 2:
            reasons.append("شهادت شاهد شاکی با شرح شکایت هم‌خوان است")
        if analysis["defense_tokens"] < 4:
            reasons.append("دفاع متهم کوتاه و بدون مستند بود")
        if not analysis["defense_evidence"]:
            reasons.append("متهم دلیلی برای زمان حادثه ارائه نکرد")
        if analysis["overlap_witness_d"] < 2:
            reasons.append("شهادت شاهد متهم شکایت را رد نکرد")
        reasons.append("مجموع شواهد به نفع شاکی ارزیابی شد")
    else:
        if analysis["complaint_tokens"] < 5:
            reasons.append("شرح شکایت روشن و مستند نبود")
        if not (analysis["complaint_evidence"]
                or analysis["witness_p_evidence"]):
            reasons.append("شکایت مستند به مدرک مشخص نبود")
        if analysis["overlap_witness_p"] < 2:
            reasons.append("شهادت شاهد شاکی با متن شکایت هم‌خوان نبود")
        if analysis["defense_evidence"]:
            reasons.append("متهم برای زمان حادثه دلیل ارائه کرد")
        if analysis["overlap_witness_d"] >= 2:
            reasons.append("اظهارات متهم با شهادت شاهد متهم هم‌خوان بود")
        if analysis["lawyer_contradiction"]:
            reasons.append("وکیل متهم ابهام شکایت را برجسته کرد")
        reasons.append("اصل برائت: شواهد برای انتساب جرم کافی نبود")
    unique = []
    for reason in reasons:
        reason = _clean_reason(reason)
        if reason and reason not in unique:
            unique.append(reason)
    for fallback in _DEFAULT_REASONS:
        if len(unique) >= 3:
            break
        if fallback not in unique:
            unique.append(fallback)
    return unique[:3]


# ---------------------------------------------------------------------------
# ساخت رأی رسمی
# ---------------------------------------------------------------------------
def official_verdict(case, verdict, reasons, confidence,
                     source=SOURCE_LOCAL) -> dict:
    """ساخت رأی با شناسه‌های واقعی پرونده (هرگز شناسه‌سازی نمی‌شود)."""
    players = (case or {}).get("players") or {}
    plaintiff = players.get("plaintiff") or {}
    defendant = players.get("defendant") or {}
    if not plaintiff.get("user_id") or not defendant.get("user_id"):
        raise JudgeError("رکورد بازکنان پرونده ناقص است")
    if verdict not in (VERDICT_PLAINTIFF, VERDICT_DEFENDANT):
        raise JudgeError("verdict نامعتبر: %r" % (verdict,))

    winner_key = "plaintiff" if verdict == VERDICT_PLAINTIFF else "defendant"
    winner = players.get(winner_key) or {}
    if winner.get("user_id") not in (plaintiff.get("user_id"),
                                    defendant.get("user_id")):
        raise JudgeError("برندهٔ رأی یکی از بازیکنان پرونده نیست")

    cleaned = [_clean_reason(reason) for reason in list(reasons or [])[:3]]
    while len(cleaned) < 3:
        cleaned.append(_DEFAULT_REASONS[len(cleaned)])
    if any(not reason for reason in cleaned):
        cleaned = [_clean_reason(reason) or _DEFAULT_REASONS[index]
                   for index, reason in enumerate(cleaned)]

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    return {
        "case_id": (case or {}).get("case_id"),
        "verdict": verdict,
        "source": source,
        "winner_user_id": int(winner["user_id"]),
        "winner_username": winner.get("username"),
        "winner_display": winner.get("display"),
        "defendant_user_id": int(defendant["user_id"]),
        "defendant_username": defendant.get("username"),
        "defendant_display": defendant.get("display"),
        "plaintiff_user_id": int(plaintiff["user_id"]),
        "plaintiff_username": plaintiff.get("username"),
        "plaintiff_display": plaintiff.get("display"),
        "winner_role": winner_key,
        "reason_1": cleaned[0],
        "reason_2": cleaned[1],
        "reason_3": cleaned[2],
        "confidence": confidence,
    }


def local_verdict(case, reason=None) -> dict:
    """رأی قاضی داخلی ربات — همیشه معتبر و همیشه بر پایهٔ شواهد."""
    analysis = case_analysis(case)
    verdict = official_verdict(
        case, analysis["verdict"],
        _pick_reasons(analysis, analysis["verdict"]),
        analysis["confidence"], source=SOURCE_LOCAL,
    )
    verdict["analysis"] = analysis["scores"]
    verdict["evidence"] = {
        "complaint": analysis["complaint_evidence"],
        "witness_p": analysis["witness_p_evidence"],
        "defense": analysis["defense_evidence"],
        "witness_d": analysis["witness_d_evidence"],
        "admission": analysis["admission"],
        "crime": analysis["crime"],
        "threshold": analysis["threshold"],
    }
    return verdict


async def judge_case(case, logger=None):
    """داوری پرونده؛ بدون هیچ تماس شبکه‌ای.

    خروجی: ``(verdict, raw)`` — همان قرارداد قبلی روتر دادگاه.
    """
    verdict = local_verdict(case)
    _log(logger, f"COURT LOCAL VERDICT case_id={verdict.get('case_id')} "
                 f"verdict={verdict['verdict']} "
                 f"scores={verdict['analysis']} "
                 f"confidence={verdict['confidence']}")
    return verdict, {"judge": JUDGE_NAME, "network": False,
                     "analysis": verdict.get("evidence")}


async def analyze_message(case, entry, logger=None):
    """تحلیل داخلی یک پیام (بدون شبکه) — برای گزارش‌های خودِ ربات."""
    return {
        "case_id": (case or {}).get("case_id"),
        "order": (entry or {}).get("order"),
        "role": (entry or {}).get("role"),
        "tokens": len(_tokens((entry or {}).get("text"))),
        "evidence": (list(_found_terms((entry or {}).get("text"),
                                       _EVIDENCE_WORDS))
                     + list(_found_terms((entry or {}).get("text"),
                                         _ALIBI_WORDS)
                            + list(_found_terms((entry or {}).get("text"),
                                                _ADMISSION_WORDS))))[:5],
        "judge": JUDGE_NAME,
    }


def is_configured() -> bool:
    """سازگاری با کد قدیمی: قاضی داخلی همیشه آماده است."""
    return True


def config() -> dict:
    return {"configured": True, "judge": JUDGE_NAME, "network": False,
            "model": "local-evidence-judge"}


def describe() -> dict:
    return {"configured": True, "judge": JUDGE_NAME, "network": False,
            "model": "local-evidence-judge", "key_present": False}


def _log(logger, message):
    if logger is None:
        return
    try:
        logger.log_info(message)
    except Exception:
        pass
