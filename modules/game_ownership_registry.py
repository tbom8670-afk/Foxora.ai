# -*- coding: utf-8 -*-
"""📋 ثبت بازی‌هایی که واژهٔ «شرکت» را می‌شناسند.

هر بازی فقط چهار پرسش را جواب می‌دهد و همهٔ آن‌ها از خودِ ماژول بازی
خوانده می‌شوند (هیچ منطقی اینجا تکرار نمی‌شود):

* ``joining(chat_id)``  → آیا پنجرهٔ ثبت‌نام باز است؟
* ``has_room(chat_id)`` → آیا جا برای یک بازیکن دیگر هست؟
* ``member(chat_id, user_id)`` → آیا این کاربر از قبل عضو است؟
* ``age(chat_id)`` → چند ثانیه از شروع مسابقه گذشته؟ (برای تساوی «تازه‌ترین»)
* ``players(chat_id)``  → چند بازیکن دارد؟ (فقط برای لاگ/تساوی)

بازی «مرکز فرماندهی» فقط دو فرمانده می‌پذیرد، بقا/خون‌آشام/کارگاه تا چهار
نفر، زامبی چهار نفر. هیچ سقفی اینجا هارد‌کد نمی‌شود؛ همه از خودِ بازی
خوانده می‌شود تا با تغییر تنظیمات، این فایل کهنه نشود.
"""
from __future__ import annotations

import time


def _age_monotonic(session) -> float:
    """عمر مسابقه بر حسب ثانیه (SessionStore زمان را با ``time.monotonic()`` می‌نویسد)."""
    created = float((session or {}).get("created_at") or 0)
    if not created:
        return 0.0
    return max(0.0, time.monotonic() - created)


def _survival(chat_id):
    from modules.fox_games import survival

    def joining():
        return survival.is_active(chat_id) and survival.phase(chat_id) == "joining"

    def has_room():
        return not survival.is_full(chat_id)

    def member(_chat, user_id):
        session = survival._STORE.get(_chat) or {}
        return str(user_id) in (session.get("players") or {})

    def age():
        session = survival._STORE.get(chat_id) or {}
        return _age_monotonic(session)

    def players():
        return survival.player_count(chat_id)

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


def _vampire(chat_id):
    from modules.fox_games import vampire

    def joining():
        return vampire.is_active(chat_id) and vampire.phase(chat_id) == "joining"

    def has_room():
        return not vampire.is_full(chat_id)

    def member(_chat, user_id):
        session = vampire._STORE.get(_chat) or {}
        return user_id in (session.get("ids") or set())

    def age():
        session = vampire._STORE.get(chat_id) or {}
        return _age_monotonic(session)

    def players():
        return vampire.player_count(chat_id)

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


def _karagah(chat_id):
    from modules.fox_games import karagah

    def joining():
        return karagah.is_active(chat_id) and karagah.phase(chat_id) == "joining"

    def has_room():
        return not karagah.is_full(chat_id)

    def member(_chat, user_id):
        session = karagah._STORE.get(_chat) or {}
        return user_id in (session.get("ids") or set())

    def age():
        session = karagah._STORE.get(chat_id) or {}
        return _age_monotonic(session)

    def players():
        return karagah.player_count(chat_id)

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


def _zombie(chat_id):
    from modules.fox_games import zombie

    def joining():
        return zombie.is_active(chat_id) and zombie.phase(chat_id) == "joining"

    def has_room():
        return not zombie.is_full(chat_id)

    def member(_chat, user_id):
        session = zombie._STORE.get(_chat) or {}
        return any(p.get("user_id") == user_id
                   for p in (session.get("players") or []))

    def age():
        session = zombie._STORE.get(chat_id) or {}
        return _age_monotonic(session)

    def players():
        session = zombie._STORE.get(chat_id) or {}
        return len(session.get("players") or [])

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


def _battle(chat_id):
    from modules.fox_games import battle

    def joining():
        return battle.is_active(chat_id) and battle.phase(chat_id) == "joining"

    def has_room():
        session = battle._STORE.get(chat_id) or {}
        return session.get("p2") is None

    def member(_chat, user_id):
        session = battle._STORE.get(_chat) or {}
        return bool(session) and (session.get("p1") or {}).get("user_id") == user_id

    def age():
        session = battle._STORE.get(chat_id) or {}
        return _age_monotonic(session)

    def players():
        session = battle._STORE.get(chat_id) or {}
        return 1 + (1 if session.get("p2") else 0)

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


def _court(chat_id):
    from modules.court_game import game as court

    def joining():
        return court.is_active(chat_id) and court.phase(chat_id) == "joining"

    def has_room():
        return not court.is_full(chat_id)

    def member(_chat, user_id):
        session = court._STORE.get(_chat) or {}
        return user_id in (session.get("ids") or set())

    def age():
        session = court._STORE.get(chat_id) or {}
        return _age_monotonic(session)

    def players():
        return court.player_count(chat_id)

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


def _fox_command(chat_id):
    from modules.fox_command import engine
    from modules.fox_command import game as fox

    def joining():
        match = fox.match_of(chat_id)
        return bool(match) and match.get("phase") == engine.PHASE_JOINING

    def has_room():
        match = fox.match_of(chat_id) or {}
        return len(match.get("players") or []) < engine.PLAYERS

    def member(_chat, user_id):
        match = fox.match_of(_chat)
        if not match:
            return False
        return engine.player_of(match, user_id) is not None

    def age():
        # Fox Command زمان شروع را با time.time() می‌نویسد.
        match = fox.match_of(chat_id) or {}
        created = float(match.get("created_at") or 0)
        return max(0.0, time.time() - created) if created else 0.0

    def players():
        match = fox.match_of(chat_id) or {}
        return len(match.get("players") or [])

    return {"joining": joining, "has_room": has_room, "member": member,
            "age": age, "players": players}


_PROVIDERS = {
    "survival": _survival,
    "vampire": _vampire,
    "karagah": _karagah,
    "zombie": _zombie,
    "battle": _battle,
    "court": _court,
    "fox_command": _fox_command,
}


def keys():
    """کلید همهٔ بازی‌هایی که «شرکت» را می‌شناسند."""
    return tuple(_PROVIDERS)


def build(key, chat_id):
    """provider آمادهٔ یک گروه/چت مشخص."""
    return _PROVIDERS[key](chat_id)


def all_for(chat_id):
    """``[(key, provider), …]`` برای یک گروه — هر provider با همان chat_id."""
    return [(key, factory(chat_id)) for key, factory in _PROVIDERS.items()]
