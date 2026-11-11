"""🧩 بازی «پازل کلمه» — ۲۰۰ مرحله، تک‌نفره.

استفاده در هندلر::

    from modules.word_puzzle import (
        answer as answer_word_puzzle,
        active as word_puzzle_active,
        start as start_word_puzzle,
        timeout as word_puzzle_timeout,
        TIME_LIMIT as WORD_PUZZLE_SECONDS,
        TOTAL as WORD_PUZZLE_TOTAL,
    )
"""
from modules.word_puzzle import storage, texts
from modules.word_puzzle.game import (
    ALPHABET,
    GAME_KEY,
    MAX_FAKES,
    ORDERED_STAGES,
    STAGE_COLUMNS,
    TIME_LIMIT,
    TOTAL,
    WORDS,
    WORDS_FILE,
    active,
    answer,
    compact,
    completed_stage,
    current,
    fake_count,
    grid_rows,
    letters_of,
    next_stage_number,
    normalize,
    reset,
    stage_payload,
    start,
    timeout,
    word_for_stage,
)

__all__ = [
    "ALPHABET",
    "GAME_KEY",
    "MAX_FAKES",
    "ORDERED_STAGES",
    "STAGE_COLUMNS",
    "TIME_LIMIT",
    "TOTAL",
    "WORDS",
    "WORDS_FILE",
    "active",
    "answer",
    "compact",
    "completed_stage",
    "current",
    "fake_count",
    "grid_rows",
    "letters_of",
    "next_stage_number",
    "normalize",
    "reset",
    "stage_payload",
    "start",
    "storage",
    "texts",
    "timeout",
    "word_for_stage",
]
