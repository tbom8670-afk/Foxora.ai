# -*- coding: utf-8 -*-
"""Screenshot-matched native group-admin messages with real TL entities.

Keep the visible text, row spacing, and UTF-16 Bold/Blockquote spans here;
changing presentation must not change the rights actually sent to Soroush.
"""
from __future__ import annotations

from modules.group_admin_manager import PERMISSIONS

try:
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:  # The API itself fails closed if SPlusthon is absent.
    MessageEntityBlockquote = MessageEntityBold = None


def u16(text):
    return len(text.encode("utf-16-le")) // 2


class _Message:
    def __init__(self):
        self.parts = []
        self.entities = []

    def add(self, text, *, bold=False, quote=False):
        offset = u16("".join(self.parts))
        self.parts.append(text)
        length = u16(text)
        if length and bold and MessageEntityBold is not None:
            self.entities.append(MessageEntityBold(offset=offset, length=length))
        if length and quote and MessageEntityBlockquote is not None:
            self.entities.append(MessageEntityBlockquote(offset=offset, length=length))
        return self

    def done(self):
        return "".join(self.parts), self.entities


def bold(text):
    return _Message().add(text, bold=True).done()


def title_prompt():
    return bold("🦊 لقب مدیر رو بنویسید")


# Per-row spaces preserve the two-column look of the supplied RTL screenshot
# in the app's proportional font.  The short captions need wider gaps; the
# six rows remain consecutive, never separated by blank lines.
_ROW_SPACES = (18, 28, 16, 28, 17, 21)


def rights_board(label, choices):
    """Screenshot layout: one quoted name, six bold rows, two bold hints."""
    message = _Message()
    message.add(f"👨‍🌾 کاربر برای ادمین گروه  : {label}",
                quote=True, bold=True)
    message.add("\n\n")
    message.add("کدام یک از قابلیت های زیر برای کاربر غیر فعال شود !؟",
                bold=True)
    message.add("\n\n\n")
    for index, (number, description, _flag) in enumerate(PERMISSIONS):
        state = "روشن" if choices[number] else "خاموش"
        icon = "◨" if choices[number] else "◧"
        message.add(f"{number}_ {description}{' ' * _ROW_SPACES[index]}{state} {icon}",
                    bold=True)
        if index != len(PERMISSIONS) - 1:
            message.add("\n")
    message.add("\n\n\n")
    message.add("⚙ برای انتخاب مثلا بنویسید 1 و 2", bold=True)
    message.add("\n\n")
    message.add("⚙ اگر انتخابی ندارید بنویسید هیچکدام", bold=True)
    return message.done()


def promoted(label):
    message = _Message()
    message.add(f"⚙ کاربر : {label}", bold=True)
    message.add("\n\n")
    message.add("ادمین گروه شد مدیریت گروه را برسی کنید", bold=True)
    return message.done()


def demoted(label):
    return bold(f"☑️ کاربر : {label} از ادمینی گروه لغو شد")
