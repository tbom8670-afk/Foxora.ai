# -*- coding: utf-8 -*-
"""⚖️ دادگاه روباه — بازی گروهی ۵ نفره با قاضی داخلی ربات.

اجزای بسته:

    texts.py       همهٔ متن‌های فارسی + قالب Bold/نقل‌قول
    storage.py     ذخیره‌سازی دائمی داخل ``<project>/data/court_game``
    game.py        state بازی، نقش‌ها، مراحل و پرونده
    ai_judge.py    قاضی داخلی ربات (تحلیل شواهد، بدون شبکه)
    punishment.py  اجرای مجازات ۶۰ ثانیه‌ای با صف حذف موجود ربات

اتصال به ربات فقط از راه ``handlers/court_game_router.py`` انجام می‌شود؛
هیچ ماژول دیگری از این بسته در مسیر پیام‌های عادی ربات دخالت نمی‌کند.
"""
from modules.court_game import (  # noqa: F401
    ai_judge,
    game,
    punishment,
    storage,
    texts,
)

__all__ = ["ai_judge", "game", "punishment", "storage", "texts"]
