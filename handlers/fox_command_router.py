# -*- coding: utf-8 -*-
"""🦊 اتصال بازی «مرکز فرماندهی روباه» به روتر بازی‌های ربات.

این فایل نازک است و هیچ منطقی از بازی یا اقتصاد ندارد؛ فقط:

* دسترسی به state بازی (``modules/fox_command/game.py``)
* گارد سرگرمی و شمارش بازی‌های فعال برای روتر موجود
* بازیابی مسابقه‌های باز پس از ری‌استارت

نام دستور بازی ``مرکز فرماندهی`` است (و ``مرکز فرماندهی روباه`` / ``fox
command``)؛ پس با دستورهای موجود ربات (مثل «فروشگاه» یا «شرکت») تداخل
نمی‌کند. دستورهای درون‌بازی (پایگاه، حمله، دفاع، …) فقط وقتی مصرف می‌شوند
که مسابقهٔ همین بازی در همان گروه باز باشد.
"""
from __future__ import annotations

from modules.fox_command import COMMAND, COMMAND_ALT, JOIN_WORD, game

GAME_KEY = game.GAME_KEY
START_COMMANDS = (COMMAND, COMMAND_ALT)


def is_active(chat_id):
    return game.is_active(chat_id)


def active_count(chat_id=None):
    return game.active_count(chat_id)


def phase(chat_id):
    return game.phase(chat_id)


def claims(chat_id, text):
    return game.claims(chat_id, text)


async def start_game(bot, event, chat_id, user_id, sender, logger=None):
    return await game.start(bot, event, chat_id, user_id, sender, logger)


async def handle_message(bot, event, chat_id, user_id, sender, text, logger=None):
    return await game.handle_message(bot, event, chat_id, user_id, sender, text,
                                     logger)


async def recover(bot, logger=None):
    return await game.recover(bot, logger)


def reset_all(chat_id=None):
    game.reset_all(chat_id)
