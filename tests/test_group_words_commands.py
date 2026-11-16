"""Tests for slashless and multiline group word-filter commands."""
import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import group_words_commands as gwc
from modules.group_words_commands import (
    _is_unmute_command,
    _parse_bulk_filter,
    is_group_filter_command_text,
)


def test_slashless_single_word_commands_are_recognized():
    assert is_group_filter_command_text("فیلتر بیو چک")
    assert is_group_filter_command_text("رفع بیوچک")
    assert is_group_filter_command_text("/فیلتر بیو چک")
    assert is_group_filter_command_text("!رفع بیوچک")


def test_bulk_add_parses_one_word_or_phrase_per_line():
    assert _parse_bulk_filter("فیلتر\nبیو چک\nتبلیغ\nپیوی") == (
        "add", ["بیو چک", "تبلیغ", "پیوی"]
    )


def test_bulk_remove_accepts_slash_prefix_and_inline_first_item():
    assert _parse_bulk_filter("/رفع بیوچک\nتبلیغ") == (
        "remove", ["بیوچک", "تبلیغ"]
    )


def test_bulk_parser_does_not_swallow_name_filter_or_unmute():
    assert _parse_bulk_filter("فیلتر اسم\nعسل") == (None, [])
    assert _parse_bulk_filter("رفع سکوت\nعلی") == (None, [])
    assert _is_unmute_command("رفع سکوت")
    assert _is_unmute_command("/رفع سکوت @ali")
    assert not is_group_filter_command_text("رفع سکوت")


def test_bare_multiline_filter_is_a_command_but_bare_text_is_not_bulk():
    assert is_group_filter_command_text("فیلتر")
    assert _parse_bulk_filter("فیلتر") == (None, [])
    assert _parse_bulk_filter("فیلتر\nبیو") == ("add", ["بیو"])


def test_bulk_handler_adds_each_line_and_reports_bold_template(monkeypatch):
    saved = []
    replies = []
    monkeypatch.setattr(gwc, "_can_manage_group_words", lambda *args: True)
    monkeypatch.setattr(gwc, "add_word", lambda chat, word: saved.append(word) or True)

    class Event:
        message = SimpleNamespace(message="فیلتر\nبیو چک\nتبلیغ")

        async def reply(self, text, **kwargs):
            replies.append((text, kwargs))

    handled = asyncio.run(gwc.handle_group_word_command(
        None, Event(), "فیلتر بیو چک تبلیغ", -100, 123,
        username="admin", raw_text=Event.message.message,
    ))
    assert handled is True
    assert saved == ["بیو چک", "تبلیغ"]
    assert replies[0][0] == "🔐 کلمات کلیدی فیلتر شدند\n\nتعداد کلمات : 2"
