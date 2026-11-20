# -*- coding: utf-8 -*-
"""Interactive native-group-admin text/real TL entities.

The user has supplied one reference image and plans to supply more.  Keep all
interactive wording/layout here so it can be matched to the remaining images
without changing permissions, the workflow or the native RPC implementation.
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


def rights_board(label, choices):
    """The six displayed booleans are exactly the booleans sent on confirm."""
    message = _Message()
    message.add(f"🧑‍🌾 کاربر برای ادمینی گروه انتخاب شد : {label}",
                quote=True, bold=True)
    message.add("\nکدام یک از قابلیت های زیر برای کاربر غیر فعال شود ؟!\n\n")
    for number, description, _flag in PERMISSIONS:
        state = "روشن" if choices[number] else "خاموش"
        message.add(f"{number}_ {description}     ◐ {state}\n")
    message.add("\n⚙️ برای خاموش کردن مثلا بنویسید 1 و 2\n")
    message.add("⚙️ برای روشن کردن بنویسید روشن 6\n")
    message.add("⚙️ اگر انتخابی ندارید بنویسید هیچکدام\n")
    message.add("⚙️ برای ثبت بنویسید تایید ادمین گروه؛ برای انصراف بنویسید انصراف ادمین گروه")
    return message.done()


def rights_changed(numbers, enabled):
    sequence = " - ".join(str(number) for number in numbers)
    state = "روشن" if enabled else "خاموش"
    return bold(f"🪁 انجام شد گزینه {sequence} {state} شد")


def promoted(label, rank):
    return bold(f"✅ کاربر : {label} ادمین گروه شد\nلقب مدیر : {rank}")


def demoted(label):
    return bold(f"☑️ کاربر : {label} از ادمینی گروه لغو شد")
