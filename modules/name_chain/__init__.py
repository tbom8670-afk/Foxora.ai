"""⛄️ بازی «اسم‌چین» — رقابت با ربات با آخرِ اسم.

استفاده در هندلر::

    from modules.name_chain import (
        answer as answer_name_chain,
        is_active as name_chain_active,
        record_prompt as record_name_chain_prompt,
        reply_message_id as name_chain_reply_id,
        reply_sender_id as name_chain_reply_user,
        start as start_name_chain,
    )
"""
from modules.name_chain import texts
from modules.name_chain.game import (
    ALPHABET,
    MAX_IDLE_SECONDS,
    MAX_WORDS,
    NAME_DISPLAY,
    NAMES,
    NAMES_BY_LETTER,
    NAMES_FILE,
    answer,
    compact,
    first_letter,
    is_active,
    is_available,
    is_real_name,
    is_unknown_phrase,
    last_letter,
    normalize,
    record_prompt,
    reply_message_id,
    reply_sender_id,
    reset,
    start,
    state,
    stop,
)

__all__ = [
    "ALPHABET",
    "MAX_IDLE_SECONDS",
    "MAX_WORDS",
    "NAME_DISPLAY",
    "NAMES",
    "NAMES_BY_LETTER",
    "NAMES_FILE",
    "answer",
    "compact",
    "first_letter",
    "is_active",
    "is_available",
    "is_real_name",
    "is_unknown_phrase",
    "last_letter",
    "normalize",
    "record_prompt",
    "reply_message_id",
    "reply_sender_id",
    "reset",
    "start",
    "state",
    "stop",
    "texts",
]
