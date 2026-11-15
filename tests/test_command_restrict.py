#!/usr/bin/env python3
"""🧪 تست «محدودیت دستورات» (command_registry + command_restrict).

    PYTHONPATH=. python3 tests/test_command_restrict.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace as NS

_TMP = tempfile.mkdtemp(prefix="command_restrict_test_")
os.environ["HOME"] = _TMP
os.environ.setdefault("BOT_INSTANCE", "command_restrict_test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import admin_storage, admin_tools, group_dispatch  # noqa: E402
from modules.manage_features import (access, command_registry,  # noqa: E402
                                     command_restrict as cr, texts)

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


G1, G2 = -1002000000001, -1002000000002
OWNER_MAIN, OWNER_G1, OWNER_G2 = 1, 2, 3
A1, A2, A3, USER = 11, 12, 13, 99
OWNERS = {G1: OWNER_G1, G2: OWNER_G2}

admin_tools.is_global_owner = lambda uid: uid == OWNER_MAIN
access.is_group_owner = lambda chat, uid: OWNERS.get(chat) == uid

for group in (G1, G2):
    admin_storage.add_admin(group, A1, "ali")
    admin_storage.add_admin(group, A2, "sara")
    admin_storage.add_admin(group, A3, None)          # بدون یوزرنیم
admin_storage.add_admin(G1, OWNER_G1, "boss")         # مالک در لیست نمی‌آید


class Event:
    def __init__(self):
        self.replies = []
        self.is_private = False

    async def reply(self, text, formatting_entities=None):
        self.replies.append((text, formatting_entities))
        return NS(id=len(self.replies))


class Client:
    async def get_entity(self, target):
        if target == A3:
            return NS(id=A3, username=None, first_name="Eryqn", last_name="")
        raise ValueError("unknown")


BOT = NS(client=Client(), logger=None)


async def send(chat, uid, text, username=None, edited=False):
    event = Event()
    sender = NS(id=uid, username=username or f"user{uid}", first_name="x")
    if not cr.interest(chat, uid, text):
        return False, event
    handled = await cr.intercept(BOT, event, chat, uid, sender, text, None,
                                 edited=edited)
    return handled, event


def spans(payload, kind):
    text, entities = payload
    raw = text.encode("utf-16-le")
    return [raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
            for e in entities if type(e).__name__.endswith(kind)]


async def main():
    registry = command_registry.load()

    # ---------- فهرستِ خودکار ----------
    check("registry finds many admin commands", len(registry) >= 80,
          str(len(registry)))
    for name in ("اخراج", "بن", "سکوت", "رفع سکوت", "پاک", "پاک کامل",
                 "پاکسازی کامل", "قفل", "باز", "اخطار", "حذف اخطار",
                 "ثبت ادمین", "لغو ادمین", "برکناری ادمین", "مرخصی",
                 "لغو مرخصی", "اختصاصی", "پیام زماندار", "متخلف",
                 "خاموشی گروه", "محدودیت پیام", "دو مرحله ای", "سنجاق",
                 "لیست ادمین", "تغییر اخطار", "تغییر مجازات", "ریست آمار",
                 "فعال کلمات ممنوعه", "ضد کانال روشن", "تبلیغ گر خاموش",
                 "فیلتر اسم", "سکوت نام", "اخراج نام", "آزاد"):
        check(f"registry has «{name}»", registry.resolve_name(name) is not None)
    for name in ("معما", "موجودی", "راهنما", "آمار گروه", "فروشگاه", "سلام",
                 "لیست بازی", "تایید", "لغو", "1", "",
                 "محدودیت دستورات", "لغو دستور"):
        check(f"registry excludes «{name}»",
              registry.resolve_name(name) is None)
    check("alias: بن = اخراج",
          registry.resolve_name("بن") == registry.resolve_name("اخراج"))
    check("alias: برکناری ادمین = لغو ادمین",
          registry.resolve_name("برکناری ادمین")
          == registry.resolve_name("لغو ادمین"))
    check("opposites differ: قفل ≠ باز",
          registry.resolve_name("قفل") != registry.resolve_name("باز"))
    check("opposites differ: سکوت ≠ رفع سکوت",
          registry.resolve_name("سکوت") != registry.resolve_name("رفع سکوت"))
    check("no fuzzy: پاکسازی کامللل",
          registry.resolve_name("پاکسازی کامللل") is None)
    check("no fuzzy: اخراجی", registry.resolve_name("اخراجی") is None)
    check("name with argument is not a name",
          registry.resolve_name("اخراج @ali") is None)
    check("match: longest wins",
          registry.match_message("پاک کامل") == registry.resolve_name("پاک کامل"))
    check("match: argument form",
          registry.match_message("اخراج @ali") == registry.resolve_name("اخراج"))
    check("match: prefix form",
          registry.match_message("!اخراج") == registry.resolve_name("اخراج"))
    check("match: zwnj/arabic letters",
          registry.match_message("پاك\u200cکامل")
          == registry.resolve_name("پاک کامل"))
    check("match: chat text is not a command",
          registry.match_message("باز هم اومدی") is None)
    check("every command has handler + id",
          all(c["handler"] and c["id"] for c in registry.commands.values()))
    check("ids unique per name set",
          len(set(registry.names.values())) == len(registry.commands))

    # ---------- ۱۰) فقط مالک ----------
    for uid, label in ((A1, "admin"), (USER, "user")):
        handled, event = await send(G1, uid, "محدودیت دستورات")
        check(f"{label} cannot start restrict (silent)",
              handled and not event.replies)
        handled, event = await send(G1, uid, "لغو دستور")
        check(f"{label} cannot start unrestrict (silent)",
              handled and not event.replies)
    handled, event = await send(G1, A1, "1")
    check("non-owner number is not a flow step", not handled)

    # ---------- ۱) محدودسازیِ درست ----------
    handled, event = await send(G1, OWNER_G1, "محدودیت دستورات")
    payload = event.replies[0]
    check("start: template text", payload[0] == (
        "⚙️سیستم محدودیت دستورات ادمین ها\n\n\n"
        "یکی از ادمین های ثبت شده زیر را انتخاب کنید گزینه مورد نظر عدد "
        "ارسال کنید\n\n⚙️1 - @ali\n⚙️2 - @sara\n⚙️3 - Eryqn"), repr(payload[0]))
    check("start: quote = title only",
          spans(payload, "Blockquote") == ["⚙️سیستم محدودیت دستورات ادمین ها"])
    check("start: owner not listed", "@boss" not in payload[0])
    handled, event = await send(G1, OWNER_G1, "1")
    payload = event.replies[0]
    check("admin chosen: template", payload[0] == (
        "🔐ادمین انتخاب شده : @ali\nکدام دستور رو انتخاب میکنید ؟\n"
        "این ادمین نتواند ازش استفاده کند اسم دستور رو بفرستید اگر دستورات "
        "رو نمی‌دانید از قسمت لیست ادمینی مطاله کنید"), repr(payload[0]))
    check("admin chosen: quote",
          spans(payload, "Blockquote") == ["🔐ادمین انتخاب شده : @ali"])

    # ---------- ۲) نام اشتباه / ۳) دستورِ غیرمدیریتی ----------
    handled, event = await send(G1, OWNER_G1, "پاکسازی کامللل")
    check("wrong name: consumed, notice, nothing stored",
          handled and not cr.restrictions(G1, A1)
          and [r[0] for r in event.replies]
          == ["⛔️ : این دستور وجود ندارد یا اشتباه نوشتید"]
          and spans(event.replies[0], "Bold") == [event.replies[0][0]])
    handled, event = await send(G1, OWNER_G1, "معما")
    check("non-admin command: consumed, notice, nothing stored",
          handled and len(event.replies) == 1
          and event.replies[0][0] == texts.RESTRICT_UNKNOWN_COMMAND
          and not cr.restrictions(G1, A1))
    handled, event = await send(G1, OWNER_G1, "اخراج", username="boss")
    payload = event.replies[0]
    check("done: template", payload[0] == (
        "⚙️ دستور انتخاب شد\nادمین محدود شده : @ali\nنام دستور : اخراج\n"
        "انجام دهنده : @boss\n\n\n\n"
        "🔶 برای لغو محدودیت دستور زیر را بفرستید\n»» لغو دستور"),
        repr(payload[0]))
    check("done: three quotes", spans(payload, "Blockquote") == [
        "ادمین محدود شده : @ali", "نام دستور : اخراج", "انجام دهنده : @boss"])
    check("done: bold lines", spans(payload, "Bold") == [
        "⚙️ دستور انتخاب شد", "»» لغو دستور"])
    kick = registry.resolve_name("اخراج")
    check("stored by command id", list(cr.restrictions(G1, A1)) == [kick])
    handled, event = await send(G1, OWNER_G1, "اخراج")
    check("flow finished: owner's own command passes", not handled)

    # ---------- ۴) اجرا مسدود / ۱۳) ریپلای / ۱۴) نام‌های جایگزین ----------
    for text in ("اخراج", "بن", "!اخراج", "/بن", "اخراج @x", "بن 12345",
                 "ا\u200cخراج".replace("\u200c", ""), "اخراج  "):
        handled, event = await send(G1, A1, text)
        check(f"blocked: {text!r}", handled and len(event.replies) == 1)
    check("blocked: template", event.replies[0][0] == (
        "⚙️ : شما نمیتوانید از این دستور استفاده کنید\n"
        "دسترسی شما توسط مالک محدود شده"))
    check("blocked: whole text bold",
          spans(event.replies[0], "Bold") == [event.replies[0][0]])
    handled, event = await send(G1, A1, "اخراج", edited=True)
    check("blocked: edited message too", handled)
    from handlers import message_handler
    check("fast paths are held for a restricted command",
          message_handler.command_restrict_interest(G1, A1, "بن"))
    source = inspect.getsource(message_handler.handle_new_message)
    check("gate runs before every executor in handle_new_message",
          0 < source.index("_restrict.intercept(")
          < source.index("is_fast_owner_command(early_command)")
          < source.index("_two_factor_gate"))
    import core.bot_working_split_ok as core
    core_source = inspect.getsource(core)
    check("core: fast moderation honours the gate",
          core_source.count("if restrict_hold:\n") == 2
          and "elif is_fast_moderation_command(text):" in core_source
          and "elif is_fast_owner_command(text):" in core_source)

    # ---------- ۵) بقیهٔ دستورها آزاد ----------
    for text in ("سکوت", "پاک 20", "قفل", "اخراج نام بیو", "سلام", "معما"):
        handled, _ = await send(G1, A1, text)
        check(f"allowed: {text!r}", not handled)
    handled, _ = await send(G1, A2, "اخراج")
    check("other admin not affected", not handled)
    handled, _ = await send(G2, A1, "اخراج")
    check("other group not affected", not handled)

    # ---------- ۶) چند دستور برای یک ادمین ----------
    async def restrict(chat, owner, index, name):
        await send(chat, owner, "محدودیت دستورات")
        await send(chat, owner, str(index))
        return await send(chat, owner, name)

    await restrict(G1, OWNER_G1, 1, "پاک کامل")
    await restrict(G1, OWNER_G1, 1, "بن")              # تکراری (alias)
    check("several commands, no duplicate", sorted(
        r["name"] for r in cr.restrictions(G1, A1).values())
        == ["اخراج", "پاک کامل"])
    handled, _ = await send(G1, A1, "پاک کامل")
    check("second command blocked", handled)
    handled, _ = await send(G1, A1, "پاک 10")
    check("«پاک» still allowed when only «پاک کامل» is restricted",
          not handled)

    # ---------- ۷) یک دستور برای چند ادمین ----------
    await restrict(G1, OWNER_MAIN, 2, "اخراج")
    await restrict(G1, OWNER_G1, 3, "اخراج")
    check("same command for three admins", all(
        kick in cr.restrictions(G1, admin) for admin in (A1, A2, A3)))
    handled, _ = await send(G1, A3, "اخراج")
    check("admin without username blocked by numeric id", handled)

    # ---------- ۱۱) owners never restricted ----------
    cr.add(G1, OWNER_G1, kick, name="اخراج", owner_id=OWNER_MAIN)
    cr.add(G1, OWNER_MAIN, kick, name="اخراج", owner_id=OWNER_MAIN)
    handled, _ = await send(G1, OWNER_G1, "اخراج")
    check("registered owner is never blocked", not handled)
    handled, _ = await send(G1, OWNER_MAIN, "اخراج")
    check("main owner is never blocked", not handled)
    cr.remove(G1, OWNER_G1, kick)
    cr.remove(G1, OWNER_MAIN, kick)

    # ---------- ۱۶) دو گروه همزمان ----------
    await send(G1, OWNER_G1, "محدودیت دستورات")
    await send(G2, OWNER_G2, "محدودیت دستورات")
    await send(G1, OWNER_G1, "2")
    await send(G2, OWNER_G2, "1")
    handled, _ = await send(G1, OWNER_G2, "سکوت")
    check("flow of another owner/group does not leak", not handled)
    await asyncio.gather(send(G1, OWNER_G1, "سکوت"),
                         send(G2, OWNER_G2, "قفل"))
    mute, lock = registry.resolve_name("سکوت"), registry.resolve_name("قفل")
    check("concurrent groups: G1/A2 got سکوت",
          mute in cr.restrictions(G1, A2) and lock not in cr.restrictions(G1, A2))
    check("concurrent groups: G2/A1 got قفل only",
          list(cr.restrictions(G2, A1)) == [lock])
    handled, _ = await send(G2, A1, "اخراج")
    check("G2/A1 «اخراج» still free", not handled)
    handled, _ = await send(G1, A1, "قفل")
    check("G1/A1 «قفل» still free", not handled)

    # ---------- ۱۲) ماندگاری پس از ری‌استارت ----------
    before = json.dumps(cr._index(), sort_keys=True)
    cr.reset_memory()
    cr.STORAGE._cache = None
    command_registry.reset()
    check("restart: same data", json.dumps(cr._index(), sort_keys=True)
          == before)
    handled, _ = await send(G1, A1, "بن")
    check("restart: still blocked", handled)
    stored = json.loads(cr.STORAGE.path.read_text(encoding="utf-8"))
    record = stored["groups"][str(G1) if str(G1) in stored["groups"]
                              else list(stored["groups"])[0]]
    sample = next(iter(next(iter(record.values()))["commands"].values()))
    check("record fields", set(sample) == {"name", "by", "ts"}
          and isinstance(sample["by"], int) and sample["ts"] > 0)

    # ---------- ۱۷) سازگاری با مرخصی ----------
    from modules.manage_features import admin_leave, admin_leave_commands
    admin_tools.is_admin = lambda chat, uid, username=None: uid in (A1, A2, A3)
    snapshot = json.dumps(cr._index(), sort_keys=True)
    leave_bot = NS(client=NS(send_message=None, get_me=None),
                   reply_input_peer_cache={}, logger=None)
    leave_event = Event()
    leave_event.reply_to = None
    try:
        await admin_leave_commands.handle(
            leave_bot, leave_event, G1, A1,
            NS(id=A1, username="ali", first_name="x"), "مرخصی 30 دقیقه", None)
        check("leave started", admin_leave.is_active(G1))
        handled, _ = await send(G1, A1, "اخراج")
        check("leave active: restriction still enforced", handled)
        await admin_leave_commands.handle(
            leave_bot, leave_event, G1, A1,
            NS(id=A1, username="ali", first_name="x"), "لغو مرخصی", None)
        check("leave ended", not admin_leave.is_active(G1))
    except Exception as error:                       # noqa: BLE001
        check("leave scenario ran", False, repr(error))
    check("leave start/end did not touch restrictions",
          json.dumps(cr._index(), sort_keys=True) == snapshot)
    handled, _ = await send(G1, A1, "اخراج")
    check("after leave: still blocked", handled)

    # ---------- ۸/۹) لغوِ فقط یک دستور ----------
    handled, event = await send(G1, OWNER_G1, "لغو دستور")
    payload = event.replies[0]
    check("cancel start: template", payload[0] == (
        "⚙️لغو دستور های ادمین ها\nلیست ادمین های محدود شده انتخاب کنید\n"
        "1ـ @ali\n2ـ @sara\n3ـ Eryqn"), repr(payload[0]))
    check("cancel start: quote",
          spans(payload, "Blockquote") == ["⚙️لغو دستور های ادمین ها"])
    handled, event = await send(G1, OWNER_G1, "9")
    check("out-of-range number ignored", not handled)
    handled, event = await send(G1, OWNER_G1, "۱")
    payload = event.replies[0]
    check("cancel pick: template", payload[0] == (
        "⚙️ادمین انتخاب شده : @ali\nدستوراتی که ازشون محدود هست برای لغو یک "
        "گزینه را ارسال کنید\n\n1 ـ اخراج\n2 ـ پاک کامل"), repr(payload[0]))
    handled, event = await send(G1, OWNER_G1, "2")
    check("cancel done: template",
          event.replies[0][0] == "🔐 محدودیت دستور لغو شد")
    check("only that command removed",
          [r["name"] for r in cr.restrictions(G1, A1).values()] == ["اخراج"])
    check("other admins untouched", kick in cr.restrictions(G1, A2)
          and mute in cr.restrictions(G1, A2)
          and kick in cr.restrictions(G1, A3))
    handled, _ = await send(G1, A1, "پاک کامل")
    check("released command works again", not handled)
    handled, _ = await send(G1, A1, "اخراج")
    check("remaining command still blocked", handled)
    await send(G1, OWNER_G1, "لغو دستور")
    await send(G1, OWNER_G1, "1")
    await send(G1, OWNER_G1, "1")
    handled, event = await send(G1, OWNER_G1, "لغو دستور")
    check("admin with no restriction leaves the list",
          "@ali" not in event.replies[0][0]
          and event.replies[0][0].endswith("1ـ @sara\n2ـ Eryqn"),
          repr(event.replies[0][0]))

    # ---------- ۱۱) وضعیتِ انتخاب ----------
    cr._clear_flow(G1, OWNER_G1)
    await send(G1, OWNER_G1, "محدودیت دستورات")
    handled, _ = await send(G1, OWNER_G1, "سلام بچه ها")
    check("chat during number step is not swallowed", not handled)
    cr._FLOWS[(str(cr.storage.group_key(G1)), str(OWNER_G1))]["expires"] = \
        time.monotonic() - 1
    handled, _ = await send(G1, OWNER_G1, "1")
    check("expired flow is dropped", not handled and not cr._FLOWS)
    await send(G1, OWNER_G1, "محدودیت دستورات")
    await send(G1, OWNER_G1, "1")
    handled, event = await send(G1, OWNER_G1, "لغو دستور")
    check("new command replaces the pending flow",
          handled and event.replies and "لغو دستور های" in event.replies[0][0])
    cr._clear_flow(G1, OWNER_G1)
    cr.reset_memory()
    cr.STORAGE.reset()
    handled, event = await send(G1, OWNER_G1, "لغو دستور")
    check("no restrictions: silent", handled and not event.replies)

    # ---------- ۱۵) دستورِ مدیریتیِ تازه خودکار شناخته می‌شود ----------
    original = group_dispatch._ADMIN_EXACT
    group_dispatch._ADMIN_EXACT = frozenset(original | {"دستور آزمایشی نو"})
    try:
        fresh = command_registry.load(force=True)
        check("new admin command auto-detected",
              fresh.resolve_name("دستور آزمایشی نو") is not None)
        await restrict(G1, OWNER_G1, 1, "دستور آزمایشی نو")
        handled, _ = await send(G1, A1, "دستور آزمایشی نو")
        check("new admin command can be restricted and is enforced", handled)
    finally:
        group_dispatch._ADMIN_EXACT = original
        command_registry.load(force=True)

    # ---------- نمایشِ ادمینِ بدون یوزرنیم / باگِ «لیست ادمین» ----------
    from modules import admin_display
    G3, GHOST = -1002000000003, 77
    admin_storage.add_admin(G3, GHOST, None, name="نام ذخیره شده")
    admin_storage.add_admin(G3, 78, None)
    entries = admin_storage.load_admins()[str(G3)] \
        if str(G3) in admin_storage.load_admins() \
        else list(admin_storage.load_admins().values())[-1]
    labels = [await admin_display.admin_label(BOT, G3, e) for e in entries]
    check("unresolvable admin shows the stored display name",
          labels[0] == "نام ذخیره شده", repr(labels))
    check("admin is never dropped from the list",
          len(labels) == 2 and all(labels), repr(labels))
    known = {78: NS(id=78, username=None, first_name="رضا", last_name="")}
    check("name taken from the group's admin list (no extra request)",
          await admin_display.admin_label(BOT, G3, entries[1], known)
          == "رضا")
    check("resolved name is remembered for next time",
          admin_display.entry_fields(
              admin_storage.load_admins()[next(
                  k for k, v in admin_storage.load_admins().items()
                  if any(isinstance(x, dict) and x.get("user_id") == "78"
                         for x in v))][1])[2] == "رضا")
    calls = []

    class StrictClient:
        async def get_entity(self, target):
            calls.append(target)
            if target == 79:
                return NS(id=79, username=None, first_name="Int", last_name="Only")
            raise ValueError("string ids are not resolvable")

    admin_storage.add_admin(G3, 79, None)
    entry = admin_storage.load_admins()[next(
        k for k, v in admin_storage.load_admins().items()
        if any(isinstance(x, dict) and x.get("user_id") == "79" for x in v))][2]
    check("stored string id is resolved as a number (root cause)",
          await admin_display.admin_label(NS(client=StrictClient()), G3, entry)
          == "Int Only" and calls == [79], repr(calls))
    handled, event = await send(G3, OWNER_MAIN, "محدودیت دستورات")
    check("restrict list shows admins without username by name",
          event.replies and event.replies[0][0].endswith(
              "⚙️1 - نام ذخیره شده\n⚙️2 - رضا\n⚙️3 - Int Only"),
          repr(event.replies[0][0] if event.replies else None))
    cr._clear_flow(G3, OWNER_MAIN)
    from modules import group_expiry
    expired_text, expired_spans = group_expiry.build_expired_message()
    check("expiry notice text", expired_text == (
        "⛔️ مهلت فعالیت ربات در این گروه به پایان رسید برای تمدید اشتراک "
        "دستور مهلت گروه را ارسال کنید"))
    check("expiry notice fully bold",
          expired_spans == [("bold", 0, len(expired_text.encode("utf-16-le")) // 2)])

    # ---------- کارایی مسیرِ گرم ----------
    started = time.perf_counter()
    for _ in range(20000):
        cr.interest(G2, USER, "سلام خوبی؟ امروز چه خبر")
    per_call = (time.perf_counter() - started) / 20000 * 1e6
    check("hot path stays cheap (<20µs)", per_call < 20, f"{per_call:.1f}µs")
    check("dispatch routes both commands on the admin lane",
          {"محدودیت دستورات", "لغو دستور"} <= group_dispatch._ADMIN_EXACT)
    check("texts: blocked constant", texts.RESTRICT_BLOCKED.count("\n") == 1)


asyncio.run(main())
print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
