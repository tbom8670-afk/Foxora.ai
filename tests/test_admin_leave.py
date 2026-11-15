#!/usr/bin/env python3
"""🧪 تست قابلیت «مرخصی ادمین» (modules/manage_features/admin_leave*).

    PYTHONPATH=. python3 tests/test_admin_leave.py
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace as NS

_TMP = tempfile.mkdtemp(prefix="admin_leave_test_")
os.environ["HOME"] = _TMP
os.environ.setdefault("BOT_INSTANCE", "admin_leave_test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import admin_tools, entertainment_control  # noqa: E402
from modules import group_banned_words_control as banned  # noqa: E402
from modules.ad_spam_guard import storage as heavy  # noqa: E402
from modules.manage_features import (access, admin_leave,  # noqa: E402
                                     admin_leave_commands as cmd, texts)
from modules.profile_guard import storage as anti_channel  # noqa: E402

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
OWNER_MAIN, OWNER_GROUP = 1, 2
A1, A2, A3, NATIVE, USER = 11, 12, 13, 14, 99
ADMINS = {A1, A2, A3}

admin_tools.is_global_owner = lambda uid: uid == OWNER_MAIN
access.is_group_owner = lambda chat, uid: uid == OWNER_GROUP
admin_tools.is_admin = lambda chat, uid, username=None: uid in ADMINS


class Event:
    def __init__(self, reply_sender=None):
        self.replies = []
        self.is_private = False
        self.reply_to = NS(reply_to_msg_id=5) if reply_sender else None
        self.reply_to_sender = NS(id=reply_sender) if reply_sender else None

    async def reply(self, text, formatting_entities=None):
        self.replies.append((text, formatting_entities))
        return NS(id=len(self.replies))


class Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, peer, text, formatting_entities=None):
        self.sent.append((peer, text, formatting_entities))
        return NS(id=1000 + len(self.sent))

    async def get_me(self):
        return NS(username="foxora_bot")


def make_bot():
    return NS(client=Client(), reply_input_peer_cache={}, logger=None)


def reset():
    cmd.reset_all()
    for module in (anti_channel, heavy):
        module.save({})
    banned.save({})
    entertainment_control.save({})
    entertainment_control._LEAVE_NOTICE_AT.clear()


async def send(bot, chat, uid, text, reply_sender=None, native=False,
               username=None):
    event = Event(reply_sender)

    async def native_admin():
        return native

    sender = NS(id=uid, username=username or f"user{uid}", first_name="x")
    handled = await cmd.handle(bot, event, chat, uid, sender, text, None,
                               native_admin=native_admin)
    return handled, event


async def drain():
    await admin_leave.QUEUE.drain(timeout=2)
    await asyncio.sleep(0)


async def main():
    # ---------- پردازش مدت ----------
    P = admin_leave.parse_duration
    check("30 دقیقه", P("30 دقیقه") == 1800)
    check("2 ساعت", P("2 ساعت") == 7200)
    check("1 ساعت", P("1 ساعت") == 3600)
    check("45 دقیقه", P("45 دقیقه") == 2700)
    check("1 ساعت و 30 دقیقه", P("1 ساعت و 30 دقیقه") == 5400)
    check("ارقام فارسی", P("۳۰ دقیقه") == 1800)
    check("نیم ساعت", P("نیم ساعت") == 1800)
    check("یک ساعت و نیم", P("یک ساعت و نیم") == 5400)
    for bad in ("", "30", "دقیقه", "abc", "0 دقیقه", "-5 دقیقه", "30 روز",
                "25 ساعت", "30 دقیقه 20 دقیقه", "2 ساعت 3 ساعت",
                "30 دقیقه لطفا", "99999 دقیقه", "1.5 ساعت"):
        check(f"ورودی نامعتبر رد می‌شود: {bad!r}", P(bad) is None)
    check("تشخیص دستور", cmd.is_command("مرخصی") and
          cmd.is_command("مرخصی 30 دقیقه") and cmd.is_command("لغو مرخصی"))
    check("متن عادی دستور نیست", not cmd.is_command("سلام خوبی") and
          not cmd.is_command("مرخصیم تموم شد") and
          not cmd.is_command("فردا میرم مرخصی"))

    # ---------- قالب‌ها ----------
    text, entities = texts.leave_ask()
    quotes = [e for e in entities if type(e).__name__.endswith("Blockquote")]
    bolds = [e for e in entities if type(e).__name__.endswith("Bold")]
    check("راهنما: دو نقل‌قول", len(quotes) == 2)
    u16 = text.encode("utf-16-le")
    spans = [u16[q.offset * 2:(q.offset + q.length) * 2].decode("utf-16-le")
             for q in quotes]
    check("راهنما: متن نقل‌قول‌ها", spans == ["««« مرخصی 30 دقیقه",
                                             "««« مرخصی 2 ساعت"], spans)
    check("راهنما: کل متن Bold", len(bolds) == 1 and bolds[0].offset == 0
          and bolds[0].length == texts.u16(text))
    check("راهنما: خط اول", text.startswith("👒 : سلام ادمین گرامی"))
    text, entities = texts.leave_set(duration="30 دقیقه", admin="@a",
                                     substitute="@bot")
    quotes = [e for e in entities if type(e).__name__.endswith("Blockquote")]
    check("تنظیم شد: ۴ نقل‌قول (عنوان + ۳ قابلیت)", len(quotes) == 4)
    check("تنظیم شد: سطرها", "زمان استراحت : 30 دقیقه\nادمین : @a\n"
          "جایگزین فعالیت : @bot" in text and text.startswith("🎒 مرخصی شما"))
    text, entities = texts.leave_reminder("@owika")
    check("یادآوری", text == "👒 سلام : @owika\nنمیخای از مرخصی بیای خستم شد 🥲"
          and len(entities) == 2)
    text, entities = texts.leave_ended("@oajana")
    check("پایان", text.startswith("سلام دوست من : @oajana\nمرخصیت به پایان")
          and len(entities) == 2)
    check("لغو", texts.leave_cancelled()[0] ==
          "🔐 : مرخصی ادمین لغو شد\nربات به حالت عادی مدیریت بازگشت")
    check("بازی‌ها", texts.leave_games_blocked()[0].startswith("»»» ⚙️ «««"))

    # ---------- ۱ و ۸ و ۹ و ۱۰: مرخصی ۳۰ دقیقه + snapshot ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)          # خاموش
    banned.enable(G1)                 # روشن از قبل
    heavy.disable(G1)                 # خاموش
    handled, event = await send(bot, G1, A1, "مرخصی 30 دقیقه", username="owika")
    leave = admin_leave.get_leave(G1, A1)
    check("۱) مرخصی ۳۰ دقیقه ثبت شد", handled and leave is not None
          and leave["duration"] == 1800
          and abs(leave["end_ts"] - leave["start_ts"] - 1800) < 1e-6
          and leave["user_id"] == A1)
    check("پیام «تنظیم شد» با مشخصات", len(event.replies) == 1
          and "زمان استراحت : 30 دقیقه" in event.replies[0][0]
          and "ادمین : @owika" in event.replies[0][0]
          and "جایگزین فعالیت : @foxora_bot" in event.replies[0][0])
    check("هیچ پیام اضافه‌ای برای روشن‌شدن قابلیت‌ها نیامد",
          len(event.replies) == 1 and not bot.client.sent)
    check("زمان باقی‌مانده قابل محاسبه است",
          1790 <= admin_leave.remaining_seconds(G1, A1) <= 1800)
    check("حالت مدیریت پیشرفته روشن شد", admin_leave.is_active(G1))
    check("قابلیت‌های خاموش روشن شدند", anti_channel.is_enabled(G1)
          and heavy.is_enabled(G1) and banned.is_enabled(G1))
    snap = admin_leave.group_record(G1)["snapshot"]["features"]
    check("snapshot وضعیت قبلی را دارد",
          snap["anti_channel"] == {"before": False, "changed": True,
                                   "manual": False}
          and snap["banned_words"]["before"] is True
          and snap["banned_words"]["changed"] is False
          and snap["heavy_spam"]["changed"] is True, snap)

    # ---------- ۱۱: بازی‌ها ----------
    check("۱۱) بازی در مدت مرخصی مسدود است",
          entertainment_control.blocks(G1, "معما")
          and entertainment_control.is_enabled(G1))
    game_event = Event()
    blocked = await entertainment_control.guard(game_event, G1, "معما")
    check("پیام مخصوصِ مرخصی برای بازی", blocked and len(game_event.replies) == 1
          and game_event.replies[0][0] == texts.LEAVE_GAMES_BLOCKED)
    game_event2 = Event()
    blocked2 = await entertainment_control.guard(game_event2, G1, "زامبی")
    check("پیامِ بازی پشت سر هم تکرار نمی‌شود ولی بازی بسته می‌ماند",
          blocked2 and not game_event2.replies)
    check("دستور غیر بازی مسدود نمی‌شود",
          not entertainment_control.blocks(G1, "لیست بازی"))
    check("۱۶) گروه دیگر تحت تأثیر نیست", not admin_leave.is_active(G2)
          and not entertainment_control.blocks(G2, "معما")
          and not anti_channel.is_enabled(G2) and not heavy.is_enabled(G2))

    # ---------- ۴ و ۱۵: یادآوری دقیقه ۱۷ ----------
    start = leave["start_ts"]
    on_reminder, on_end = cmd._callbacks(bot, None)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 16 * 60 + 59)
    await drain()
    check("قبل از دقیقه ۱۷ یادآوری نمی‌آید", not bot.client.sent)
    _, _, next_due = admin_leave.due_actions(start + 60)
    check("سررسید بعدی دقیقاً دقیقه ۱۷ است", abs(next_due - (start + 1020)) < 1e-6)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 17 * 60)
    await drain()
    check("۴) یادآوری دقیقاً در دقیقه ۱۷", len(bot.client.sent) == 1
          and bot.client.sent[0][1] ==
          "👒 سلام : @owika\nنمیخای از مرخصی بیای خستم شد 🥲")
    for extra in (1, 30, 120):
        await admin_leave.tick(on_reminder, on_end, None,
                               now_ts=start + 17 * 60 + extra)
    await asyncio.gather(*[
        admin_leave.tick(on_reminder, on_end, None, now_ts=start + 1100)
        for _ in range(5)])
    await drain()
    check("۱۵) یادآوری تکراری ارسال نمی‌شود", len(bot.client.sent) == 1)

    # ---------- پایان خودکار ----------
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 1799)
    await drain()
    check("قبل از زمان پایان هنوز فعال است", admin_leave.get_leave(G1, A1)
          and len(bot.client.sent) == 1)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 1800)
    await drain()
    check("پایان خودکار در زمان دقیق", admin_leave.get_leave(G1, A1) is None
          and not admin_leave.is_active(G1)
          and admin_leave.group_record(G1) is None)
    check("پیام پایان", len(bot.client.sent) == 2 and bot.client.sent[1][1]
          .startswith("سلام دوست من : @owika\nمرخصیت به پایان رسید"))
    check("۸/۱۰) قابلیت‌هایی که مرخصی روشن کرده بود خاموش شدند",
          not anti_channel.is_enabled(G1) and not heavy.is_enabled(G1))
    check("۹) قابلیتی که از قبل روشن بود روشن ماند", banned.is_enabled(G1))
    check("بازی‌ها به وضعیت قبلی (روشن) برگشتند",
          not entertainment_control.blocks(G1, "معما"))
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 5000)
    await drain()
    check("پیام پایان تکرار نمی‌شود", len(bot.client.sent) == 2)

    # بازی‌هایی که از قبل خاموش بودند خاموش می‌مانند
    reset()
    bot = make_bot()
    entertainment_control.disable(G1)
    await send(bot, G1, A1, "مرخصی 20 دقیقه")
    admin_leave.end(G1, A1)
    check("بازیِ از قبل خاموش، بعد از مرخصی خاموش می‌ماند",
          not entertainment_control.is_enabled(G1)
          and entertainment_control.blocks(G1, "معما"))

    # ---------- ۲: دو ساعته ----------
    reset()
    bot = make_bot()
    _, event = await send(bot, G1, A1, "مرخصی 2 ساعت")
    leave = admin_leave.get_leave(G1, A1)
    check("۲) مرخصی ۲ ساعته", leave["duration"] == 7200
          and "زمان استراحت : 2 ساعت" in event.replies[0][0]
          and leave["reminder_ts"] == leave["start_ts"] + 1020)
    _, event = await send(bot, G1, A1, "مرخصی 1 ساعت و 30 دقیقه")
    check("ثبت دوباره همان ادمین رکورد تکراری نمی‌سازد",
          len(admin_leave.active_leaves(G1)) == 1
          and admin_leave.get_leave(G1, A1)["duration"] == 5400
          and "1 ساعت و 30 دقیقه" in event.replies[0][0])

    # ---------- ۳: کمتر از ۱۷ دقیقه ----------
    reset()
    bot = make_bot()
    await send(bot, G1, A1, "مرخصی 10 دقیقه")
    leave = admin_leave.get_leave(G1, A1)
    start = leave["start_ts"]
    on_reminder, on_end = cmd._callbacks(bot, None)
    check("۳) مرخصی کوتاه یادآوری ندارد", leave["reminder_ts"] is None)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 17 * 60)
    await drain()
    check("مرخصی کوتاه: فقط پیام پایان", len(bot.client.sent) == 1
          and bot.client.sent[0][1].startswith("سلام دوست من"))
    reset()
    bot = make_bot()
    await send(bot, G1, A1, "مرخصی 17 دقیقه")
    check("مرخصی دقیقاً ۱۷ دقیقه یادآوری ندارد",
          admin_leave.get_leave(G1, A1)["reminder_ts"] is None)

    # ---------- ورودی نامعتبر ----------
    reset()
    bot = make_bot()
    _, event = await send(bot, G1, A1, "مرخصی")
    check("«مرخصی» تنها ⇒ راهنما، بدون ثبت", event.replies
          and event.replies[0][0] == texts.leave_ask()[0]
          and not admin_leave.is_active(G1))
    _, event = await send(bot, G1, A1, "مرخصی فردا")
    check("مدت نامعتبر ⇒ راهنما، بدون ثبت", event.replies
          and event.replies[0][0] == texts.leave_ask()[0]
          and not admin_leave.is_active(G1)
          and not anti_channel.is_enabled(G1))

    # ---------- ۱۳: مالک‌ها ----------
    for owner, label in ((OWNER_MAIN, "مالک اصلی"), (OWNER_GROUP, "مالک ثبت‌شده")):
        for text in ("مرخصی 30 دقیقه", "مرخصی"):
            handled, event = await send(bot, G1, owner, text, native=True)
            check(f"۱۳) {label} نمی‌تواند مرخصی بگیرد ({text})",
                  handled and not event.replies
                  and admin_leave.get_leave(G1, owner) is None
                  and not admin_leave.is_active(G1))
    ADMINS.add(OWNER_GROUP)
    handled, event = await send(bot, G1, OWNER_GROUP, "مرخصی 30 دقیقه")
    check("مالکی که ادمین هم ثبت شده باز هم نمی‌تواند",
          not admin_leave.is_active(G1))
    ADMINS.discard(OWNER_GROUP)
    handled, event = await send(bot, G1, USER, "مرخصی 30 دقیقه", native=False)
    check("کاربر عادی نمی‌تواند مرخصی بگیرد", handled and not event.replies
          and not admin_leave.is_active(G1))
    handled, event = await send(bot, G1, NATIVE, "مرخصی 30 دقیقه", native=True)
    check("ادمین واقعی گروه (تأیید API) می‌تواند",
          admin_leave.get_leave(G1, NATIVE) is not None)
    admin_leave.end(G1, NATIVE)

    # ---------- ۵: لغو قبل از پایان ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    start = admin_leave.get_leave(G1, A1)["start_ts"]
    _, event = await send(bot, G1, OWNER_GROUP, "لغو مرخصی", reply_sender=A1)
    check("۵) مالک ثبت‌شده با ریپلای لغو می‌کند", event.replies
          and event.replies[0][0] == texts.leave_cancelled()[0]
          and admin_leave.get_leave(G1, A1) is None
          and not admin_leave.is_active(G1)
          and not anti_channel.is_enabled(G1))
    on_reminder, on_end = cmd._callbacks(bot, None)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 1020)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 1800)
    await drain()
    check("بعد از لغو، نه یادآوری می‌آید نه پیام پایان", not bot.client.sent)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    _, event = await send(bot, G1, OWNER_MAIN, "لغو مرخصی", reply_sender=A1)
    check("مالک اصلی هم می‌تواند لغو کند", event.replies
          and admin_leave.get_leave(G1, A1) is None)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    _, event = await send(bot, G1, A1, "لغو مرخصی")
    check("ادمین مرخصی خودش را لغو می‌کند", event.replies
          and admin_leave.get_leave(G1, A1) is None)
    _, event = await send(bot, G1, OWNER_GROUP, "لغو مرخصی", reply_sender=A2)
    check("لغو برای کسی که مرخصی ندارد بی‌اثر است", not event.replies)

    # ---------- ۱۴: لغو غیرمجاز ----------
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    for who, label in ((A2, "ادمین دیگر"), (USER, "کاربر عادی")):
        _, event = await send(bot, G1, who, "لغو مرخصی", reply_sender=A1)
        check(f"۱۴) {label} نمی‌تواند مرخصی دیگری را لغو کند",
              not event.replies and admin_leave.get_leave(G1, A1) is not None)
        _, event = await send(bot, G1, who, "لغو مرخصی")
        check(f"۱۴) {label} بدون ریپلای هم اثری ندارد",
              not event.replies and admin_leave.get_leave(G1, A1) is not None)
    admin_leave.end(G1, A1)

    # ---------- ۶ و ۷: چند ادمین هم‌زمان ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)
    heavy.disable(G1)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    snapshot_before = admin_leave.group_record(G1)["snapshot"]
    await send(bot, G1, A2, "مرخصی 2 ساعت")
    await send(bot, G1, A3, "مرخصی 45 دقیقه")
    leaves = {l["user_id"]: l for l in admin_leave.active_leaves(G1)}
    check("۶) سه مرخصی مستقل", set(leaves) == {A1, A2, A3}
          and leaves[A1]["duration"] == 1800 and leaves[A2]["duration"] == 7200
          and leaves[A3]["duration"] == 2700
          and len({l["token"] for l in leaves.values()}) == 3)
    check("فقط یک snapshot (از قبلِ اولین مرخصی)",
          admin_leave.group_record(G1)["snapshot"] == snapshot_before
          and snapshot_before["features"]["anti_channel"]["before"] is False)
    base = leaves[A1]["start_ts"]
    on_reminder, on_end = cmd._callbacks(bot, None)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=base + 1801)
    await drain()
    check("۷) پایان مرخصی اول، حالت پیشرفته را خاموش نمی‌کند",
          admin_leave.get_leave(G1, A1) is None and admin_leave.is_active(G1)
          and anti_channel.is_enabled(G1) and heavy.is_enabled(G1)
          and entertainment_control.blocks(G1, "معما")
          and admin_leave.group_record(G1)["snapshot"] == snapshot_before)
    check("مرخصی‌های دیگر دست نخوردند",
          admin_leave.get_leave(G1, A2)["end_ts"] == leaves[A2]["end_ts"]
          and admin_leave.get_leave(G1, A3)["end_ts"] == leaves[A3]["end_ts"])
    ended = [m for m in bot.client.sent if m[1].startswith("سلام دوست من")]
    check("فقط پیام پایانِ ادمین اول آمد", len(ended) == 1
          and "@user11" in ended[0][1])
    _, event = await send(bot, G1, OWNER_GROUP, "لغو مرخصی", reply_sender=A3)
    check("لغو مرخصی سوم؛ دومی هنوز فعال ⇒ حالت پیشرفته می‌ماند",
          event.replies and admin_leave.is_active(G1)
          and anti_channel.is_enabled(G1))
    await admin_leave.tick(on_reminder, on_end, None,
                           now_ts=leaves[A2]["end_ts"] + 1)
    await drain()
    check("بعد از آخرین مرخصی همه‌چیز برمی‌گردد",
          not admin_leave.is_active(G1) and not anti_channel.is_enabled(G1)
          and not heavy.is_enabled(G1)
          and not entertainment_control.blocks(G1, "معما"))

    # ---------- تغییر دستی در مدت مرخصی ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)
    heavy.disable(G1)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    anti_channel.enable(G1)           # مالک دستی «ضد کانال روشن» زد
    record = admin_leave.group_record(G1)["snapshot"]["features"]
    check("تغییر دستی ثبت شد", record["anti_channel"]["manual"] is True
          and record["heavy_spam"]["manual"] is False)
    admin_leave.end(G1, A1)
    check("تنظیم دستیِ مالک بعد از مرخصی از بین نمی‌رود",
          anti_channel.is_enabled(G1) and not heavy.is_enabled(G1))
    reset()
    anti_channel.disable(G1)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    anti_channel.disable(G1)          # مالک دستی خاموش کرد
    admin_leave.end(G1, A1)
    check("خاموشیِ دستی هم حفظ می‌شود", not anti_channel.is_enabled(G1))
    anti_channel.enable(G2)
    check("تغییر دستی بدون مرخصی فعال هیچ اثری ندارد",
          admin_leave.group_record(G2) is None)

    # ---------- ۱۶: دو گروه ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)
    anti_channel.disable(G2)
    await send(bot, G1, A1, "مرخصی 30 دقیقه")
    await send(bot, G2, A1, "مرخصی 1 ساعت")
    check("۱۶) همان ادمین در دو گروه، دو رکورد مستقل",
          admin_leave.get_leave(G1, A1)["duration"] == 1800
          and admin_leave.get_leave(G2, A1)["duration"] == 3600)
    admin_leave.end(G1, A1)
    check("۱۶) پایان در گروه اول، گروه دوم را دست نمی‌زند",
          not admin_leave.is_active(G1) and admin_leave.is_active(G2)
          and not anti_channel.is_enabled(G1) and anti_channel.is_enabled(G2)
          and entertainment_control.blocks(G2, "معما")
          and not entertainment_control.blocks(G1, "معما"))
    admin_leave.end(G2, A1)

    # ---------- ۱۲: ری‌استارت ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)
    await send(bot, G1, A1, "مرخصی 30 دقیقه", username="owika")
    await send(bot, G1, A2, "مرخصی 10 دقیقه")
    start = admin_leave.get_leave(G1, A1)["start_ts"]
    if admin_leave._WATCHER is not None:
        admin_leave._WATCHER.cancel()
        admin_leave._WATCHER = None

    def restart():
        admin_leave.STORAGE._invalidate()
        admin_leave._ACTIVE.clear()
        admin_leave._INDEX_READY = False

    restart()
    check("۱۲) بعد از ری‌استارت حالت پیشرفته از دیسک برمی‌گردد",
          admin_leave.is_active(G1)
          and entertainment_control.blocks(G1, "معما"))
    bot = make_bot()
    state = cmd.recover(bot, None)
    check("۱۲) بازیابی مرخصی‌ها", state == {"groups": 1, "leaves": 2})
    if admin_leave._WATCHER is not None:
        admin_leave._WATCHER.cancel()
        admin_leave._WATCHER = None
    on_reminder, on_end = cmd._callbacks(bot, None)
    # ربات از دقیقه ۵ تا ۲۰ خاموش بوده
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 20 * 60)
    await drain()
    texts_sent = [m[1] for m in bot.client.sent]
    check("۱۲) یادآوریِ جامانده (مرخصی هنوز فعال) یک بار ارسال شد",
          sum(t.startswith("👒 سلام : @owika") for t in texts_sent) == 1)
    check("۱۲) مرخصیِ گذشته پاکسازی شد و اولی باقی ماند",
          admin_leave.get_leave(G1, A2) is None
          and admin_leave.get_leave(G1, A1) is not None
          and admin_leave.is_active(G1) and anti_channel.is_enabled(G1))
    restart()
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 21 * 60)
    await drain()
    check("۱۲) ری‌استارتِ دوباره یادآوری را تکرار نمی‌کند",
          sum(m[1].startswith("👒 سلام") for m in bot.client.sent) == 1)
    # ری‌استارت بعد از پایانِ همه
    restart()
    bot = make_bot()
    on_reminder, on_end = cmd._callbacks(bot, None)
    await admin_leave.tick(on_reminder, on_end, None, now_ts=start + 3 * 3600)
    await drain()
    check("۱۲) مرخصیِ تمام‌شده در زمان خاموشی: پاکسازی + بازگشت تنظیمات",
          not admin_leave.is_active(G1) and not anti_channel.is_enabled(G1)
          and admin_leave.group_record(G1) is None)
    check("یادآوریِ مرخصیِ تمام‌شده ارسال نمی‌شود",
          not any(m[1].startswith("👒 سلام") for m in bot.client.sent))

    # ---------- زمان‌بندِ واقعی (ساعت دیوار) ----------
    reset()
    bot = make_bot()
    old = (admin_leave.MIN_SECONDS, admin_leave.REMINDER_AFTER_SECONDS)
    admin_leave.MIN_SECONDS, admin_leave.REMINDER_AFTER_SECONDS = 0, 0.3
    try:
        leave, _ = admin_leave.start(G1, A1, 1, username="owika")
        t1 = cmd.ensure_scheduler(bot, None)
        t2 = cmd.ensure_scheduler(bot, None)
        check("زمان‌بند فقط یک بار ساخته می‌شود", t1 is t2)
        await asyncio.sleep(0.6)
        check("حلقهٔ واقعی: یادآوری سر وقت", len(bot.client.sent) == 1
              and bot.client.sent[0][1].startswith("👒 سلام"))
        await asyncio.sleep(0.8)
        await drain()
        check("حلقهٔ واقعی: پایان سر وقت", len(bot.client.sent) == 2
              and not admin_leave.is_active(G1))
        # ثبتِ تازه وقتی حلقه بیکار خوابیده
        admin_leave.start(G1, A2, 1, username="x")
        await asyncio.sleep(1.4)
        await drain()
        check("حلقهٔ بیکار با ثبتِ تازه بیدار می‌شود",
              admin_leave.get_leave(G1, A2) is None)
    finally:
        admin_leave.MIN_SECONDS, admin_leave.REMINDER_AFTER_SECONDS = old

    # ---------- ثبت هم‌زمان ----------
    reset()
    bot = make_bot()
    anti_channel.disable(G1)
    await asyncio.gather(*[send(bot, G1, uid, "مرخصی 30 دقیقه")
                           for uid in (A1, A2, A3, A1, A2)])
    check("ثبت هم‌زمان: بدون رکورد تکراری و با یک snapshot",
          sorted(l["user_id"] for l in admin_leave.active_leaves(G1))
          == [A1, A2, A3]
          and admin_leave.group_record(G1)["snapshot"]["features"]
          ["anti_channel"]["before"] is False)
    reset()

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
