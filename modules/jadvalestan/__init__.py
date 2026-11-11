"""🧩 بازی «جدولستان» — جدولِ روزنامه‌ایِ تک‌نفره با ۳۲۰ مرحله.

استفاده در هندلر::

    from modules.jadvalestan import (
        answer as jadvalestan_answer,
        active as jadvalestan_active,
        start as jadvalestan_start,
        timeout as jadvalestan_timeout,
        TIME_LIMIT as JADVALESTAN_TIME_LIMIT,
        TOTAL as JADVALESTAN_TOTAL,
    )
"""
from modules.jadvalestan import storage, texts
from modules.jadvalestan.game import (
    GAME_KEY,
    MAX_IDLE_SECONDS,
    QUESTIONS,
    QUESTIONS_FILE,
    TIME_LIMIT,
    TOTAL,
    active,
    answer,
    compact,
    completed_stage,
    current,
    letters_of,
    next_stage_number,
    normalize,
    owner,
    question_for_stage,
    reset,
    stage_payload,
    start,
    timeout,
)

__all__ = [
    "GAME_KEY",
    "MAX_IDLE_SECONDS",
    "QUESTIONS",
    "QUESTIONS_FILE",
    "TIME_LIMIT",
    "TOTAL",
    "active",
    "answer",
    "compact",
    "completed_stage",
    "current",
    "letters_of",
    "next_stage_number",
    "normalize",
    "owner",
    "question_for_stage",
    "reset",
    "stage_payload",
    "start",
    "storage",
    "texts",
    "timeout",
]
