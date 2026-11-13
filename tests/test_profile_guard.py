# -*- coding: utf-8 -*-
"""🧪 تست قابلیت «ضد کانال و بیوگرافی» ( modules/profile_guard ).

اجرا:

    PYTHONPATH=. python3 tests/test_profile_guard.py

پوشش:
 ۱. تطبیق دور-زدن‌ناپذیر نام کانال/جملهٔ بیو (فاصله، نقطه، نیم‌فاصله،
    کشیده، اعراب، نویسهٔ بی‌عرض، حروف عربی، ایموجی، چسبیده‌نویسی)
 ۲. **نبودِ تطبیق اشتباه**: کلمهٔ یک‌کلمه‌ای روی کلمهٔ بلندتر اثر نکند
 ۳. قالب‌های پیام عیناً مثل اسکرین‌شات (متن + آفستِ entityها)
 ۴. دستورهای روشن/خاموش per-group
 ۵. خالی‌بودن فهرست‌ها ⇒ هیچ فیلتری اعمال نمی‌شود
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# محیط آزمون: فایل‌های runtime در پوشهٔ موقت (state واقعی دست نخورد).
_TMP = tempfile.mkdtemp(prefix="profile_guard_test_")
os.environ.setdefault("BOT_HOME", _TMP)
os.environ.setdefault("BOT_INSTANCE", "profile_guard_test")

from modules.profile_guard import filters, matcher, texts  # noqa: E402
from modules.profile_guard.storage import toggle_mode  # noqa: E402

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ✅ %s" % label)
    else:
        FAIL += 1
        print("  ❌ %s %s" % (label, detail))


print("\n=== ۱) تطبیق نام کانال: دور زدن‌ها ===")
channel_phrases = matcher.build(["فیلم و سریال"])

for sample in (
    "فیلم و سریال",              # ساده
    "فیلموسریال",                # چسبیده
    "فیـلم و سـریال",            # کشیده
    "فیلم‌و‌سریال",              # نیم‌فاصله
    "ف ی ل م و س ر ی ا ل",       # حروف جدا
    "فیلم.و.سریال",              # نقطه
    "فیلم و  سریال",             # فاصلهٔ اضافه
    "فیلم و سریال 🎬",           # ایموجی
    "فيلم و سريال",              # ي عربی
    "فِیلم و سِریال",            # اعراب
    "فی\u200cلم و سریال",        # نویسهٔ بی‌عرض دیگر
):
    check(f"«{sample}» گرفته شود",
          matcher.find_match(sample, channel_phrases) is not None)

print("\n=== ۲) نبودِ تطبیق اشتباه ===")
single = matcher.build(["فیلم"])
check("«فیلم» روی متن ساده", matcher.matches_any("کانال فیلم من", single))
check("«فیلم» روی «فیلمبرداری» اثر نکند",
      not matcher.matches_any("کار فیلمبرداری حرفه‌ای", single))
check("«فیلم‌ها» با پسوندِ جمع گرفته شود",
      matcher.matches_any("فیلمها و سریالها", single))
check("«فیلمی» با پسوندِ نکره گرفته شود",
      matcher.matches_any("یه فیلمی دارم", single))
check("«فیلمبرداری» باز هم گرفته نشود (پسوندِ ناشناس)",
      not matcher.matches_any("کار فیلمبرداری حرفه‌ای", single))
check("«فیلم» روی «فیلم-بازار» (کلمهٔ مستقل) بگیرد",
      matcher.matches_any("فیلم-بازار", single))
check("متن بی‌ربط تطبیق نشود",
      not matcher.matches_any("سلام خوبی چه خبر", channel_phrases))
check("متن خالی تطبیق نشود", not matcher.matches_any("", channel_phrases))
check("فهرست خالی هیچ‌چیز را نگیرد",
      not matcher.matches_any("هرچیزی", matcher.build([])))
check("عبارت یک‌حرفی نادیده گرفته شود",
      matcher.build(["ا"]) == [])

print("\n=== ۲ب) پسوندهای چسبیده (مثل «استوریمو») ===")
_estori = matcher.build(["استوری", "استور", "استوریمو ببینین"])
for sample in (
    "استوریمو",
    "استوریمو ببینین",
    "⭕️استوریمو⭕️",
    "استوريمو",
    "استوریـمو",
    "استوری هامو ببین",
    "استورییییی",
    "استوریموببینین",
):
    check(f"«{sample}» گرفته شود", matcher.matches_any(sample, _estori))
check("«کسی» با عبارتِ ۲ حرفی «کس» گرفته نشود (پسوند ممنوع)",
      not matcher.matches_any("کسی نیومد", matcher.build(["کس"])))
check("«سکسی» با «سکس» گرفته شود",
      matcher.matches_any("بیو سکسی", matcher.build(["سکس"])))
check("«بی‌سکس» گرفته نشود (مرزِ چپ)", 
      not matcher.matches_any("بیسکس", matcher.build(["سکس"])))
check("«بیوگرافیم» با «بیوگرافی» گرفته شود",
      matcher.matches_any("بیوگرافیم رو ببین", matcher.build(["بیوگرافی"])))

print("\n=== ۳) تطبیق جملهٔ بیوگرافی ===")
bio_phrases = matcher.build(["بیا تو کانال ما فیلم هست"])
for sample in (
    "بیا تو کانال ما فیلم هست",
    "بیاتوکانالمافیلمهست",
    "ب.ی.ا تو کانال ما فیلم هست",
    "بیا  تو  كانال ما فيلم هست",
    "🎬بیا تو کانال ما فیلم هست🎬",
):
    check(f"بیو «{sample}» گرفته شود",
          matcher.matches_any(sample, bio_phrases))
check("بیوی سالم گرفته نشود",
      not matcher.matches_any("عاشق فیلم و کتاب", bio_phrases))

print("\n=== ۴) قالب پیام‌ها (متن + entity) ===")
text, entities = texts.notice_channel(
    "@foxora", "فیلم و سریال", 1245, duration_seconds=None
)
expected_lines = text.split("\n")
check("خط اول: نقل‌قول کاربر", expected_lines[0] == "⛔ کاربر : @foxora",
      repr(expected_lines[0]))
check("خط دوم: دلیلِ کانال",
      expected_lines[1] == "پیام شما پاک شد | دلیل : کانال فیلتر شده",
      repr(expected_lines[1]))
check("خط خالی بعد از دلیل", expected_lines[2] == "", repr(expected_lines))
check("نام کانال", expected_lines[3].startswith("↻- نام کانال بیوگرافی : فیلم و سریال"),
      repr(expected_lines[3]))
check("تعداد اعضا", expected_lines[4] == "↻- تعداد اعضا : 1245",
      repr(expected_lines[4]))
check("مجازات دائمی", expected_lines[5] == "↻- مجازات : سکوت دائمی",
      repr(expected_lines[5]))
check("entity نقل‌قول روی خط اول",
      any(isinstance(e, texts.MessageEntityBlockquote) and e.offset == 0
          for e in entities),
      repr(entities))
check("bolt روی مقدار کانال",
      any(isinstance(e, texts.MessageEntityBold) and e.length > 0
          for e in entities))

# --- اسپویلر: نامِ کانال و تعداد اعضا پنهان، مجازات عادی -------------------
def _spoiler_texts(t, ents):
    out = []
    for e in ents:
        if isinstance(e, texts.MessageEntitySpoiler):
            u16 = t.encode("utf-16-le")
            out.append(u16[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le"))
    return out

_sp = _spoiler_texts(text, entities)
check("اسپویلر: نام کانال پنهان است", "فیلم و سریال" in _sp, repr(_sp))
check("اسپویلر: تعداد اعضا پنهان است", "1245" in _sp, repr(_sp))
check("اسپویلر: مجازات پنهان نیست", "سکوت دائمی" not in _sp, repr(_sp))

text_bio, _ = texts.notice_bio("@foxora", "بیا تو کانال ما فیلم هست",
                               duration_seconds=4 * 3600)
bio_lines = text_bio.split("\n")
check("دلیلِ بیوگرافی",
      bio_lines[1] == "پیام شما پاک شد | دلیل : فیلتر بیوگرافی",
      repr(bio_lines[1]))
check("برچسب کلمهٔ فیلتر",
      bio_lines[3].startswith("↻- کلمه فیلتر بیوگرافی : بیا تو کانال"),
      repr(bio_lines[3]))
check("مجازات ۴ ساعته", bio_lines[4] == "↻- مجازات : 4 ساعت سکوت",
      repr(bio_lines[4]))
_bio_sp = _spoiler_texts(text_bio, _)
check("اسپویلر بیو: جملهٔ فیلتر پنهان است",
      "بیا تو کانال ما فیلم هست" in _bio_sp, repr(_bio_sp))
check("اسپویلر بیو: مجازات پنهان نیست", "4 ساعت سکوت" not in _bio_sp,
      repr(_bio_sp))
check("برچسب یک‌ساعته", texts.punish_label(3600) == "1 ساعت سکوت")
check("برچسب نامعلوم‌بودن اعضا", texts.format_members(None) == "نامشخص")

# --- حالت‌های پنهان‌سازیِ مقدار (PROFILE_GUARD_HIDE) ---------------------
import os as _os  # noqa: E402

_os.environ.pop("PROFILE_GUARD_HIDE", None)
_t_spoiler, _e_spoiler = texts.notice_bio("x", "استوری", duration_seconds=14400)
_os.environ["PROFILE_GUARD_HIDE"] = "mask"
_t_mask, _e_mask = texts.notice_bio("x", "استوری", duration_seconds=14400)
_os.environ["PROFILE_GUARD_HIDE"] = "both"
_t_both, _e_both = texts.notice_bio("x", "استوری", duration_seconds=14400)
_os.environ.pop("PROFILE_GUARD_HIDE", None)

check("حالتِ پیش‌فرض = اسپویلر (متن اصلی موجود، entity اسپویلر)",
      "استوری" in _t_spoiler
      and any(isinstance(e, texts.MessageEntitySpoiler) for e in _e_spoiler))
check("حالتِ پیش‌فرض: روی محدودهٔ اسپویلر Bold نیست",
      not any(isinstance(e, texts.MessageEntityBold) and e.offset ==
              [x for x in _e_spoiler
               if isinstance(x, texts.MessageEntitySpoiler)][0].offset
              for e in _e_spoiler), repr(_e_spoiler))
check("حالتِ mask: متنِ اصلی هرگز فرستاده نمی‌شود",
      "استوری" not in _t_mask and texts.HIDE_MASK in _t_mask)
check("حالتِ mask: بدونِ entity اسپویلر",
      not any(isinstance(e, texts.MessageEntitySpoiler) for e in _e_mask))
check("حالتِ both: ماسک + اسپویلر",
      texts.HIDE_MASK in _t_both
      and any(isinstance(e, texts.MessageEntitySpoiler) for e in _e_both))
check("مجازات در همهٔ حالت‌ها دیده می‌شود",
      all("4 ساعت سکوت" in t for t in (_t_spoiler, _t_mask, _t_both)))
check("مقدارِ نامشخص هم پنهان می‌شود",
      texts.UNKNOWN_VALUE in texts.notice_channel("u", None)[0])

enable_text, enable_entities = texts.enabled(67)
check("پیام فعال‌شدن، خط اول",
      enable_text.split("\n")[0] == "⚠️ ضد کانال و بیوگرافی کاربران فعال شد",
      repr(enable_text))
check("پیام فعال‌شدن، شمارش",
      enable_text.split("\n")[1] == "نام ها و جملات فیلتر شده « 67 »",
      repr(enable_text))
check("پیام خاموش‌شدن",
      texts.disabled() == "▪️ضد فیلتر کانال و بیوگرافی روباه خاموش شد",
      repr(texts.disabled()))

print("\n=== ۵) دستورهای روشن/خاموش ===")
for sample in ("ضد کانال", "ضد كانال", "ضدکانال", "ضد کانال روشن",
               "ضد کانال  روشن.", "ضد‌کانال"):
    check(f"«{sample}» = روشن", toggle_mode(sample) == "on",
          repr(toggle_mode(sample)))
for sample in ("ضد کانال خاموش", "ضدكانالخاموش", "ضد کانال خاموش!"):
    check(f"«{sample}» = خاموش", toggle_mode(sample) == "off",
          repr(toggle_mode(sample)))
check("متن نامربوط دستور نیست", toggle_mode("ضد فیلتر") is None)
check("پیام معمولی دستور نیست", toggle_mode("سلام ضد کانال چطوری") is None)

print("\n=== ۶) فهرست‌های پیش‌فرض ===")
check("فایل نام کانال موجود است",
      filters._resolve(filters.CHANNEL_LIST_FILE).exists(),
      str(filters._resolve(filters.CHANNEL_LIST_FILE)))
check("فایل جملهٔ بیو موجود است",
      filters._resolve(filters.BIO_LIST_FILE).exists(),
      str(filters._resolve(filters.BIO_LIST_FILE)))
check("فهرست نام کانال خوانده شد",
      len(filters.channel_names()) >= 1, str(filters.channel_names()))
check("فهرست جملهٔ بیو خوانده شد",
      len(filters.bio_sentences()) >= 1, str(filters.bio_sentences()))
check("شمارش کل = نام‌ها + جمله‌ها",
      filters.filter_count() == len(filters.channel_names())
      + len(filters.bio_sentences()), str(filters.filter_count()))
_default_channel = matcher.build(filters.channel_names())
_default_bio = matcher.build(filters.bio_sentences())
check("پیش‌فرض: کانال تبلیغاتی گرفته می‌شود",
      matcher.matches_any("ف ی ل م و س ر ی ا ل", _default_channel))
check("پیش‌فرض: بیوی سالم گرفته نمی‌شود",
      not matcher.matches_any("عاشق فیلم و کتاب و موسیقی", _default_bio))

print("\n=== ۷) آزمون سرتاسری با کلاینتِ تقلبی (بدون شبکه) ===")
import asyncio  # noqa: E402
import types as _pytypes  # noqa: E402

os.environ["PROFILE_GUARD_RPC_MIN_INTERVAL"] = "0"

from modules.profile_guard import guard, service  # noqa: E402


# --- استابِ حداقلیِ splusthon (کتابخانه در محیط تست نصب نیست) -------------
def _install_splusthon_stub():
    if "splusthon" in sys.modules:
        return
    root = _pytypes.ModuleType("splusthon")
    tl = _pytypes.ModuleType("splusthon.tl")
    fns = _pytypes.ModuleType("splusthon.tl.functions")
    f_users = _pytypes.ModuleType("splusthon.tl.functions.users")
    f_channels = _pytypes.ModuleType("splusthon.tl.functions.channels")
    tl_types = _pytypes.ModuleType("splusthon.tl.types")
    utils_mod = _pytypes.ModuleType("splusthon.utils")

    class _Req:
        def __init__(self, *args):
            self.args = args

    class PeerChannel:
        def __init__(self, channel_id, access_hash=0):
            self.channel_id = channel_id
            self.access_hash = access_hash

    class InputChannel:
        def __init__(self, channel_id=0, access_hash=0):
            self.channel_id = channel_id
            self.access_hash = access_hash

    f_users.GetFullUserRequest = _Req
    f_channels.GetChannelsRequest = _Req
    f_channels.GetFullChannelRequest = _Req
    tl_types.PeerChannel = PeerChannel
    tl_types.InputChannel = InputChannel
    utils_mod.get_input_user = lambda user: user
    root.tl = tl
    root.utils = utils_mod
    sys.modules.update({
        "splusthon": root,
        "splusthon.tl": tl,
        "splusthon.tl.functions": fns,
        "splusthon.tl.functions.users": f_users,
        "splusthon.tl.functions.channels": f_channels,
        "splusthon.tl.types": tl_types,
        "splusthon.utils": utils_mod,
    })


_install_splusthon_stub()


class _Logger:
    def log_info(self, *a, **k):
        pass

    def log_error(self, *a, **k):
        pass


class _AdminActions:
    def __init__(self):
        self.calls = []

    async def mute_user(self, chat_id, user_id, duration_seconds=None, *,
                        user=None, chat=None):
        self.calls.append((chat_id, user_id, duration_seconds))
        return True


class _Channel:
    def __init__(self, cid, title, participants=None, username=None):
        self.id = cid
        self.title = title
        self.participants_count = participants
        self.access_hash = 777
        self.username = username


class _FullUser:
    def __init__(self, about=None, channel_id=None):
        self.about = about
        self.personal_channel_id = channel_id
        self.personal_channel_message = 11


class _UsersUserFull:
    def __init__(self, full_user, chats):
        self.full_user = full_user
        self.chats = chats
        self.users = []


class _Client:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.deleted = []
        self.rpc = 0

    async def __call__(self, request):
        self.rpc += 1
        return self.response

    async def get_input_entity(self, entity):
        return entity

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))
        return True

    async def send_message(self, target, text, **kwargs):
        self.sent.append({"text": text,
                          "entities": kwargs.get("formatting_entities")})
        return _pytypes.SimpleNamespace(id=1)


class _NoticeCleanup:
    """جانشینِ سرویسِ حذفِ خودکار (۶۰ ثانیه) در رباتِ واقعی."""

    def __init__(self):
        self.scheduled = []

    def schedule(self, chat_id, sent):
        self.scheduled.append((chat_id, getattr(sent, "id", None)))
        return True


class _Bot:
    def __init__(self, client):
        self.client = client
        self.logger = _Logger()
        self.admin_actions = _AdminActions()
        self.notice_cleanup = _NoticeCleanup()
        self.bot_account_id = 999


class _Event:
    is_private = False

    def __init__(self):
        self.message = _pytypes.SimpleNamespace(id=42)
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class _Sender:
    def __init__(self, uid=555, username="osieka"):
        self.id = uid
        self.username = username
        self.first_name = ""
        self.last_name = ""


def _run(coro):
    return asyncio.run(coro)


# فیلترها را برای این آزمون دستی ست می‌کنیم (فایل‌ها پیش‌فرض خالی‌اند).
_CHANNEL_LIST = ["فیلم و سریال"]
_BIO_LIST = ["بیا تو کانال ما فیلم هست"]
filters.channel_names = lambda: list(_CHANNEL_LIST)
filters.bio_sentences = lambda: list(_BIO_LIST)
filters.is_empty = lambda: not (_CHANNEL_LIST or _BIO_LIST)
filters.filter_count = lambda: len(_CHANNEL_LIST) + len(_BIO_LIST)

from modules.profile_guard.storage import enable as _enable  # noqa: E402

CHAT = -1001234567890
_enable(CHAT)

# --- حالت کانال: پروفایل کانالِ فیلترشده دارد ---------------------------
service.clear()
client = _Client(_UsersUserFull(
    _FullUser(about="سلام", channel_id=4321),
    [_Channel(4321, "فیـلم و سريال", participants=1245)],
))
bot = _Bot(client)
event = _Event()
handled = _run(guard.guard_message(bot, event, CHAT, 555, _Sender(), 42))
check("کانالِ فیلترشده ⇒ پیام مصرف شد", handled is True)
check("کد مجازات: سکوت دائمی",
      bot.admin_actions.calls and bot.admin_actions.calls[-1][2] is None,
      repr(bot.admin_actions.calls))
check("پیام کاربر حذف شد", client.deleted and client.deleted[-1][1] == [42],
      repr(client.deleted))
notice = client.sent[-1]["text"] if client.sent else ""
check("قالب کانال: دلیل درست", "دلیل : کانال فیلتر شده" in notice, repr(notice))
check("قالب کانال: نام کانال", "↻- نام کانال بیوگرافی : فیـلم و سريال" in notice,
      repr(notice))
check("قالب کانال: تعداد اعضا", "↻- تعداد اعضا : 1245" in notice, repr(notice))
check("قالب کانال: مجازات", "↻- مجازات : سکوت دائمی" in notice, repr(notice))
check("قالب کانال: نقل‌قول کاربر", notice.startswith("⛔ کاربر : @osieka"),
      repr(notice.split("\n")[0]))


def _hidden(entities, text):
    """متنِ زیرِ entityهای اسپویلر (مستقل از کلاسِ کتابخانه)."""
    if not entities:
        return []
    u16 = text.encode("utf-16-le")
    return [
        u16[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
        for e in entities
        if type(e).__name__ == "MessageEntitySpoiler"
    ]


_hidden_channel = _hidden(client.sent[-1]["entities"] if client.sent else [], notice)
check("ارسال: نام کانال اسپویلر است",
      "فیـلم و سريال" in _hidden_channel, repr(_hidden_channel))
check("ارسال: تعداد اعضا اسپویلر است",
      "1245" in _hidden_channel, repr(_hidden_channel))
check("ارسال: مجازات اسپویلر نیست",
      "سکوت دائمی" not in _hidden_channel, repr(_hidden_channel))
check("هشدار در صفِ حذفِ خودکار (۶۰ ثانیه) ثبت شد",
      len(bot.notice_cleanup.scheduled) == 1,
      repr(bot.notice_cleanup.scheduled))
check("یک RPC برای پروفایل (کش بعدی رایگان است)", client.rpc == 1,
      str(client.rpc))

# --- همان کاربر، پیام دوم: هیچ RPC جدیدی نباید بزند ---------------------
event2 = _Event()
_run(guard.guard_message(bot, event2, CHAT, 555, _Sender(), 43))
check("پیام دوم: صفر RPC اضافه", client.rpc == 1, str(client.rpc))

# --- حالت بیوگرافی: کانال ندارد ولی جملهٔ فیلتر دارد ---------------------
service.clear()
client2 = _Client(_UsersUserFull(_FullUser(about="بیاتوکانالمافیلمهست",
                                           channel_id=None), []))
bot2 = _Bot(client2)
event3 = _Event()
handled = _run(guard.guard_message(bot2, event3, CHAT, 556, _Sender(556), 44))
check("جملهٔ بیو ⇒ پیام مصرف شد", handled is True)
check("کد مجازات: ۴ ساعت",
      bot2.admin_actions.calls and bot2.admin_actions.calls[-1][2] == 14400,
      repr(bot2.admin_actions.calls))
notice2 = client2.sent[-1]["text"] if client2.sent else ""
check("قالب بیو: دلیل", "دلیل : فیلتر بیوگرافی" in notice2, repr(notice2))
check("قالب بیو: جملهٔ تطبیق‌یافته",
      "↻- کلمه فیلتر بیوگرافی : بیا تو کانال ما فیلم هست" in notice2,
      repr(notice2))
check("قالب بیو: مجازات", "↻- مجازات : 4 ساعت سکوت" in notice2, repr(notice2))
_hidden_bio = _hidden(client2.sent[-1]["entities"] if client2.sent else [], notice2)
check("ارسال: جملهٔ فیلتر اسپویلر است",
      "بیا تو کانال ما فیلم هست" in _hidden_bio, repr(_hidden_bio))
check("ارسال: مجازات بیو اسپویلر نیست",
      "4 ساعت سکوت" not in _hidden_bio, repr(_hidden_bio))

# --- کاربر سالم: هیچ کاری نباید بشود ------------------------------------
service.clear()
client3 = _Client(_UsersUserFull(_FullUser(about="عاشق فیلم و کتاب",
                                           channel_id=None), []))
bot3 = _Bot(client3)
handled = _run(guard.guard_message(bot3, _Event(), CHAT, 557, _Sender(557), 45))
check("کاربر سالم: پیام دست‌نخورده", handled is False)
check("کاربر سالم: هیچ حذفی", client3.deleted == [], repr(client3.deleted))
check("کاربر سالم: هیچ سکوتی", bot3.admin_actions.calls == [])

# --- کاربر بدون کانال و بدون جمله: هیچ‌چیز ---------------------------------
service.clear()
client4 = _Client(_UsersUserFull(_FullUser(about=None, channel_id=None), []))
bot4 = _Bot(client4)
handled = _run(guard.guard_message(bot4, _Event(), CHAT, 558, _Sender(558), 46))
check("بیوی خالی ⇒ بدون مجازات", handled is False)

print("\n=== ۸) ورودِ جمله‌ها از فایل (Termux) ===")
import shutil  # noqa: E402
from modules.profile_guard import importer  # noqa: E402

work = Path(_TMP) / "import_case"
work.mkdir(parents=True, exist_ok=True)

# فهرست‌های تمیز و مستقل برای این آزمون
ch_file = work / "profile_channel_names.txt"
bio_file = work / "profile_bio_sentences.txt"
ch_file.write_text("# خالی\n", encoding="utf-8")
bio_file.write_text("# خالی\n", encoding="utf-8")
filters._resolve = lambda name: ch_file if name == filters.CHANNEL_LIST_FILE else bio_file
filters.reload()

sample = work / "filters.txt"
sample.write_text("""# فایل کاربر
CHANNEL
فیلم و سریال
فیلم سوپـر

[کانال]:
کانال تبلیغاتی

BIO
بیا تو کانال ما فیلم هست
فیلم و سریال جدید در کانال ما
""", encoding="utf-8")

report = importer.import_file(str(sample))
check("ورود موفق بود", report.get("ok") is True, str(report.get("errors")))
check("۳ نام کانال اضافه شد", report["added"][importer.SECTION_CHANNEL] == 3,
      str(report["added"]))
check("۲ جملهٔ بیو اضافه شد", report["added"][importer.SECTION_BIO] == 2,
      str(report["added"]))
content_ch = ch_file.read_text(encoding="utf-8")
content_bio = bio_file.read_text(encoding="utf-8")
check("نام‌ها در فایل کانال نشستند",
      "فیلم و سریال" in content_ch and "کانال تبلیغاتی" in content_ch
      and "بیا تو کانال" not in content_ch, content_ch)
check("جمله‌ها در فایل بیو نشستند",
      "بیا تو کانال ما فیلم هست" in content_bio
      and "کانال تبلیغاتی" not in content_bio, content_bio)
check("نرمال‌سازی هنگام تطبیق کار می‌کند",
      matcher.matches_any("ف ی ل م و س ر ی ا ل", matcher.build(["فیلم و سریال"])))

# اجرای دوباره ⇒ هیچ تکرار
report2 = importer.import_file(str(sample))
check("اجرای دوباره چیزی اضافه نکرد",
      report2["added"][importer.SECTION_CHANNEL] == 0
      and report2["added"][importer.SECTION_BIO] == 0, str(report2["added"]))
check("تکراری‌ها شمرده شدند",
      report2["skipped_duplicates"][importer.SECTION_CHANNEL] == 3,
      str(report2["skipped_duplicates"]))

# فایل بدون سرخط
plain = work / "plain.txt"
plain.write_text("عبارت اول\nعبارت دوم\n", encoding="utf-8")
bad = importer.import_file(str(plain))
check("فایل بی‌سرخط بدون بخش ⇒ خطا",
      bad.get("ok") is False and bad.get("errors"), str(bad.get("errors")))
good = importer.import_file(str(plain), default_section=importer.SECTION_BIO)
check("با بخش صریح ⇒ وارد شد", good["added"][importer.SECTION_BIO] == 2,
      str(good["added"]))

# dry-run چیزی نمی‌نویسد
before = bio_file.read_text(encoding="utf-8")
dry = importer.import_file(str(plain), default_section=importer.SECTION_BIO,
                           dry_run=True)
check("dry-run: نوشتن صفر", dry["added"][importer.SECTION_BIO] == 0,
      str(dry["added"]))
check("dry-run: فایل دست‌نخورده",
      bio_file.read_text(encoding="utf-8") == before)

# فایل ناموجود
missing = importer.import_file(str(work / "nope.txt"))
check("فایل ناموجود ⇒ خطای روشن",
      missing.get("ok") is False and "پیدا نشد" in " ".join(missing["errors"]),
      str(missing.get("errors")))

# --- دستور چت «ضد کانال فایل …» ---------------------------------------
from modules.profile_guard import guard as _guard  # noqa: E402

check("دستور فایل با بخش کانال",
      _guard.parse_file_command("ضد کانال فایل کانال /sdcard/f.txt")
      == {"section": "channel", "path": "/sdcard/f.txt"},
      str(_guard.parse_file_command("ضد کانال فایل کانال /sdcard/f.txt")))
check("دستور فایل با بخش بیو",
      _guard.parse_file_command("ضد كانال فایل بیو ~/b.txt")
      == {"section": "bio", "path": "~/b.txt"})
check("دستور فایل بدون بخش",
      _guard.parse_file_command("ضد کانال فایل /storage/emulated/0/f.txt")
      == {"section": None, "path": "/storage/emulated/0/f.txt"})
check("پیام معمولی دستور فایل نیست",
      _guard.parse_file_command("ضد کانال") is None)
check("مسیر با فاصله حفظ می‌شود",
      _guard.parse_file_command("ضد کانال فایل /sdcard/my files/f.txt")
      == {"section": None, "path": "/sdcard/my files/f.txt"})

# --- خطوط فقط‌ایموجی هرگز نوشته نمی‌شوند -----------------------------
unusable_file = work / "unusable.txt"
unusable_file.write_text("CHANNEL\n😍\n🔥\nفیلم خوب\n", encoding="utf-8")
rep = importer.import_file(str(unusable_file))
check("فقط‌ایموجی‌ها نوشته نشدند",
      rep["skipped_unusable"][importer.SECTION_CHANNEL] == 2, str(rep))
check("عبارت سالمِ همان فایل نوشته شد",
      rep["added"][importer.SECTION_CHANNEL] == 1, str(rep["added"]))
check("گزارش، تعداد بی‌حرف را می‌گوید", "ایموجی" in importer.summary(rep))

print("\n=== ۹) بررسیِ همیشگیِ بیوگرافی + دوامِ سکوت پس از ری‌استارت ===")
import time  # noqa: E402
from modules.profile_guard import penalties  # noqa: E402
from modules.profile_guard import storage as _storage  # noqa: E402

CHAT2 = -1009999999999
_storage.enable(CHAT)
_storage.disable(CHAT2)

# فهرست‌های مستقلِ این بخش (بعد از بازنویسی‌های بخش ۸)
_CHANNEL_LIST = ["فیلم و سریال"]
_BIO_LIST = ["سکس", "استوری"]
filters.channel_names = lambda: list(_CHANNEL_LIST)
filters.bio_sentences = lambda: list(_BIO_LIST)
filters.is_empty = lambda: not (_CHANNEL_LIST or _BIO_LIST)
filters.filter_count = lambda: len(_CHANNEL_LIST) + len(_BIO_LIST)

service.clear()
penalties.clear()
guard._notified.clear()
guard._healed.clear()

# --- ۱) تخلفِ بیو ⇒ سکوت ۴ ساعته + ثبتِ پایدار --------------------------
client = _Client(_UsersUserFull(_FullUser(about="سکس", channel_id=None), []))
bot = _Bot(client)
_run(guard.guard_message(bot, _Event(), CHAT, 660, _Sender(660), 71))
check("بیو ۱: پیام پاک شد", client.deleted == [(CHAT, [71])],
      repr(client.deleted))
check("بیو ۱: سکوت ۴ ساعته",
      bot.admin_actions.calls and bot.admin_actions.calls[-1][2] == 14400,
      repr(bot.admin_actions.calls))
_penalty = penalties.get(CHAT, 660)
check("بیو ۱: مجازات در دفترچه ثبت شد", bool(_penalty), repr(_penalty))
check("بیو ۱: پایانِ ثبت‌شده ≈ ۴ ساعت",
      _penalty and _penalty.get("until") is not None
      and abs(float(_penalty["until"]) - (time.time() + 14400)) < 300,
      repr(_penalty))
_notice_count = len(client.sent)
check("کارتِ هشدار در صفِ حذفِ خودکار رفت",
      len(bot.notice_cleanup.scheduled) == 1,
      repr(bot.notice_cleanup.scheduled))

# --- پاسخِ دستورها هرگز در صفِ حذف نمی‌رود ------------------------------
_toggle_client = _Client(_UsersUserFull(_FullUser(), []))
_toggle_bot = _Bot(_toggle_client)
_original_is_admin = guard._is_admin
guard._is_admin = lambda *a, **k: True          # نقشِ ادمین را شبیه‌سازی می‌کنیم
try:
    _run(guard.handle_toggle(_toggle_bot, _Event(), CHAT, 555, _Sender(555), "on"))
finally:
    guard._is_admin = _original_is_admin
check("کارتِ «فعال شد» در صفِ حذف نمی‌رود",
      _toggle_bot.notice_cleanup.scheduled == [],
      repr(_toggle_bot.notice_cleanup.scheduled))
check("کارتِ «فعال شد» ارسال شد", len(_toggle_client.sent) == 1)

# --- ۲) بیو عوض می‌شود؛ کش تازه است ولی کاربر «زیرِ نظر» ⇒ باید دیده شود --
client.response = _UsersUserFull(_FullUser(about="استوری من", channel_id=None), [])
_run(guard.guard_message(bot, _Event(), CHAT, 660, _Sender(660), 72))
check("تغییرِ بیو ⇒ تخلفِ تازه گرفته شد",
      client.deleted[-1] == (CHAT, [72]), repr(client.deleted))
check("تغییرِ بیو ⇒ هشدارِ دوباره فرستاده شد",
      len(client.sent) == _notice_count + 1, str(len(client.sent)))
check("کارتِ دوم مقدارِ تازه را دارد",
      "استوری" in (client.sent[-1]["text"] if client.sent else ""),
      repr(client.sent[-1]["text"] if client.sent else ""))
check("برای بیوی تازه RPC زده شد (نه کش)", client.rpc >= 2, str(client.rpc))

# --- ۳) تکرارِ عینِ همان تخلف: کارتِ سوم نه ------------------------------
_run(guard.guard_message(bot, _Event(), CHAT, 660, _Sender(660), 73))
check("تکرارِ همان مقدار: کارتِ اضافه نمی‌آید",
      len(client.sent) == _notice_count + 1, str(len(client.sent)))

# --- ۴) سکوتِ دائمیِ کانال + شبیه‌سازیِ ری‌استارت -------------------------
service.clear()
guard._notified.clear()
guard._healed.clear()
client2 = _Client(_UsersUserFull(
    _FullUser(about="سالم", channel_id=4321),
    [_Channel(4321, "فیلم و سریال", participants=99)],
))
bot2 = _Bot(client2)
_run(guard.guard_message(bot2, _Event(), CHAT, 815, _Sender(815), 81))
check("کانال: سکوت دائمی اعمال شد",
      bot2.admin_actions.calls and bot2.admin_actions.calls[-1][2] is None,
      repr(bot2.admin_actions.calls))
check("کانال: در دفترچه دائمی ثبت شد", penalties.is_permanent(CHAT, 815))

# ری‌استارت: همهٔ کش‌های حافظه‌ای خالی، فقط دفترچهٔ روی دیسک می‌ماند
service.clear()
guard._notified.clear()
guard._healed.clear()
client3 = _Client(_UsersUserFull(_FullUser(about="سالمِ سالم", channel_id=None), []))
bot3 = _Bot(client3)
_handled = _run(guard.guard_message(bot3, _Event(), CHAT, 815, _Sender(815), 82))
check("پس از ری‌استارت: پیامِ کاربرِ دائم‌ساکت پاک می‌شود",
      _handled is True and client3.deleted == [(CHAT, [82])],
      repr(client3.deleted))
check("پس از ری‌استارت: سکوت دوباره اعمال شد",
      bot3.admin_actions.calls and bot3.admin_actions.calls[-1][2] is None,
      repr(bot3.admin_actions.calls))
check("پس از ری‌استارت: کارتِ تکراری فرستاده نمی‌شود",
      client3.sent == [], repr(client3.sent))
check("پس از ری‌استارت: برای سکوتِ دوباره RPCِ پروفایل لازم نیست",
      client3.rpc == 0, str(client3.rpc))

# --- ۵) قلابِ استارتاپ: اعمالِ دوبارهٔ سکوت‌های دائمی ---------------------
service.clear()
penalties.clear()
penalties.record(CHAT, 815, "channel", None)
penalties.record(CHAT2, 816, "channel", None)


async def _recover_case():
    bot4 = _Bot(_Client(_UsersUserFull(_FullUser(), [])))
    total = guard.recover(bot4, None)
    await asyncio.sleep(0.8)
    return total, bot4.admin_actions.calls


_total, _calls = asyncio.run(_recover_case())
check("recover: فقط گروهِ روشن شمرده می‌شود", _total == 1, str(_total))
check("recover: سکوتِ دائمیِ همان کاربر دوباره اعمال شد",
      _calls == [(CHAT, 815, None)], repr(_calls))

# --- ۶) محرمانگی و هرسِ دفترچه ------------------------------------------
_raw = ""
try:
    _raw = Path(str(penalties.FILE)).read_text(encoding="utf-8")
except Exception:
    _raw = ""
check("دفترچه هیچ متنِ بیو/کانال را ذخیره نمی‌کند",
      "سکس" not in _raw and "استوری" not in _raw, _raw[:200])

penalties.clear()
penalties.record(CHAT, 900, "bio", time.time() - 7200)
check("رکوردِ موقتِ کهنه هرس می‌شود",
      penalties.prune() == 1 and penalties.get(CHAT, 900) is None)
penalties.record(CHAT, 901, "channel", None)
check("رکوردِ دائمی هرس نمی‌شود", penalties.prune() == 0)
penalties.clear()

print("\n----------------------------------------")
print(f"PASS={PASS}  FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
