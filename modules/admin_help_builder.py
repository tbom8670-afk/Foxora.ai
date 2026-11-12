"""قالب‌بندی و ساخت بخش «دستورات ادمین» راهنمای روباه مطابق استانداردهای سروش پلاس.

- b = متن مشخص‌شده Bold باشد (MessageEntityBold)
- n = دستور داخل نقل‌قول سروش‌پلاس باشد (MessageEntityBlockquote تک‌خطی)
- n2 = دو دستور مرتبط داخل یک نقل‌قول دوتایی سروش‌پلاس باشند (MessageEntityBlockquote دوخطی)
- n3 = سه دستور مرتبط داخل یک نقل‌قول سه‌تایی سروش‌پلاس باشند (MessageEntityBlockquote سه‌خطی)
"""
try:
    from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote
except ImportError:
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


#: 📊 متنِ راهنمای دستور «نمودار» — کامل Bold و دستور داخلِ نقل‌قول.
GROWTH_HELP_TEXT = (
    "📊 برای دیدن نمودار رشد گروه یا ریزش گروه دستور\n"
    "« نمودار »\n"
    "را ارسال کنید."
)

#: نشانهٔ «این ردیف یک بلوکِ آمادهٔ Bold/نقل‌قول است، نه عنوان + دستور».
READY_BLOCK = object()

#: ⚠️ سروش‌پلاس تعداد نقل‌قول‌های یک پیام را محدود می‌کند: در پیام‌های
#: خیلی بلند (این لیست ۵۲ نقل‌قول دارد) چند نقل‌قولِ **آخر** بی‌صدا
#: نادیده گرفته می‌شوند و فقط متنشان می‌ماند. برای همین «لیست ادمینی»
#: تکه‌تکه ارسال می‌شود تا هر پیام کمترین نقل‌قول را داشته باشد و هیچ
#: ردیفی بی‌نقل‌قول نماند.
MAX_QUOTES_PER_MESSAGE = 30
MAX_UNITS_PER_MESSAGE = 1800


def _row(title, cmds, bold_cmds=False, quote=True):
    """یک ردیف را با آفست‌های **نسبی** (از صفر) می‌سازد.

    خروجی ``(text, entities)`` است و در ``_join_rows`` به هم چسبانده
    می‌شود؛ آفست‌ها فقط یک بار در همان جا جابه‌جا می‌شوند.
    """
    parts = []
    entities = []
    start_t = 0
    parts.append(title)
    entities.append(MessageEntityBold(offset=start_t, length=_u16(title)))
    parts.append("\n")
    cmd_text = "\n".join(cmds)
    start_c = _u16("".join(parts))
    parts.append(cmd_text)
    if quote:
        entities.append(MessageEntityBlockquote(offset=start_c,
                                                length=_u16(cmd_text)))
    if bold_cmds:
        # هر دستور جداگانه Bold می‌شود (نقل‌قول یکی است، دستورها جدا).
        cursor = start_c
        for line in cmds:
            if line:
                entities.append(MessageEntityBold(offset=cursor,
                                                  length=_u16(line)))
            cursor += _u16(line) + 1
    return "".join(parts), entities


def _rich_row(chunk, quote_texts=()):
    """بلوکِ آماده: کلِ متن Bold، و فقط بخش‌های خواسته‌شده داخل نقل‌قول."""
    entities = [MessageEntityBold(offset=0, length=_u16(chunk))]
    for needle in quote_texts:
        index = chunk.find(needle)
        if index < 0:
            continue
        # اگر نیدل با «\n» شروع شود یعنی «این خط، از ابتدای خط»؛ خودِ
        # خط‌شکن داخل نقل‌قول نمی‌رود. لازم است چون عنوانِ بلوک هم می‌تواند
        # همان عبارت را داشته باشد و نقل‌قول نباید داخل عنوان بیفتد.
        lead = 1 if needle.startswith("\n") else 0
        text_part = needle[lead:]
        entities.append(MessageEntityBlockquote(
            offset=_u16(chunk[:index + lead]), length=_u16(text_part)))
    return chunk, entities


def _join_rows(rows):
    """ردیف‌ها را با «\\n\\n» به هم می‌چسباند و آفست‌ها را جابه‌جا می‌کند.

    جداکننده فقط **بین** ردیف‌ها می‌آید (بعد از آخرین ردیف چیزی اضافه
    نمی‌شود) تا خروجیِ تک‌پیامی عیناً مثل قبل بماند.
    """
    parts = []
    entities = []
    for position, (text, row_entities) in enumerate(rows):
        if position:
            parts.append("\n\n")
        start = _u16("".join(parts))
        parts.append(text)
        for entity in row_entities:
            entities.append(type(entity)(offset=start + entity.offset,
                                         length=entity.length))
    return "".join(parts), entities


def _split_rows(rows, max_quotes=MAX_QUOTES_PER_MESSAGE,
                max_units=MAX_UNITS_PER_MESSAGE):
    """ردیف‌ها را به تکه‌های امن تقسیم می‌کند (هیچ ردیفی نصف نمی‌شود)."""
    chunks = []
    current = []
    current_quotes = 0
    current_units = 0
    for text, row_entities in rows:
        quotes = sum(1 for entity in row_entities
                     if type(entity).__name__ == "MessageEntityBlockquote")
        units = _u16(text)
        if current and (current_quotes + quotes > max_quotes
                        or current_units + units > max_units):
            chunks.append(current)
            current = []
            current_quotes = 0
            current_units = 0
        current.append((text, row_entities))
        current_quotes += quotes
        current_units += units
    if current:
        chunks.append(current)
    return chunks


def _admin_rows():
    """همهٔ ردیف‌های «دستورات ادمین» — هر ردیف ``(text, entities)`` نسبی."""
    rows = []

    def add_item(title, cmds, bold_cmds=False, quote=True):
        rows.append(_row(title, cmds, bold_cmds=bold_cmds, quote=quote))

    def add_rich_item(chunk, quote_texts=()):
        rows.append(_rich_row(chunk, quote_texts=quote_texts))

    header = "👑 دستورات ادمین‌ها:"
    rows.append((header, [MessageEntityBold(offset=0, length=_u16(header))]))

    # 🛡️ ضد ربات‌های تبلیغاتی — عنوان Bold و هر دو دستور Bold داخل **یک**
    # نقل‌قول سروش پلاس (مطابق اسکرین‌شاتِ کارفرما).
    _ad_guard_header = "💥برای فعال کردن ضد ربات های تبلیغ گر بنویسید"
    _ad_guard_cmds = "\n".join(["تبلیغ گر روشن", "تبلیغ گر خاموش"])
    add_rich_item(
        _ad_guard_header + "\n" + _ad_guard_cmds,
        quote_texts=("\n" + _ad_guard_cmds,),
    )

    blocks_before_vip = [
        # 🔒 عضویت اجباری کانال — عنوان Bold و هر دو دستور Bold داخل **یک**
        # نقل‌قول سروش پلاس (نه دو نقل‌قول جدا).
        ("📣 اضافه کردن کانال من برای عضویت اجباری دستور",
         ["راهنمای کانال", "راهنمای اجرا"]),
        ("👮‍♀دیدن لیست ادمین ها با دستور", ["لیست ادمین"]),
        ("🔒قفل کردن گروه با دستور", ["قفل"]),
        ("🔓باز کردن گروه با دستور", ["باز"]),
        ("📖 ثبت قوانین گروه با دستورات", ["ثبت قوانین | قوانین | حذف قوانین"]),
        ("🗳حذف حافظه ثبت شده یک کاربر با دستور", ["ریپلای کنید : حذف حافظه"]),
        ("⚠️اخطار دادن به کاربر ریپلای با دستور", ["اخطار"]),
        ("⚙تایین سقف اخطار و تغییر اخطار دستور", ["تغییر اخطار"]),
        ("⚙تغییر مجازات از اخراج به سکوت و برعکس", ["تغییر مجازات"]),
        ("🪤فیلتر کردن کلمات گروه با دستورات", [
            "/فیلتر ← /فیلتر بیو",
            "/رفع ← / رفع بیو",
            "/فیلترها ← نمایش لیست فیلتر",
        ]),
        ("📊نمایش آمار گروه با دستور", ["آمار گپ | آمار گروه"]),
        ("📈صفر کردن  آمار گروه با دستور", ["ریست آمار"]),
        # 📊 نمودار رشد/ریزش — متن کامل Bold، و خودِ دستور داخل نقل‌قول.
        (READY_BLOCK, [GROWTH_HELP_TEXT, ("« نمودار »",)]),
        ("📇افزودن ادمین به ربات با دستور", ["ریپلای کنید : ثبت ادمین"]),
        ("🗃برکناری ادمین ثبت شده ربات با دستور", ["با ریپلای : لغو ادمین | برکناری ادمین"]),
    ]

    for index, (title, cmds) in enumerate(blocks_before_vip):
        if title is READY_BLOCK:
            add_rich_item(cmds[0], quote_texts=cmds[1])
            continue
        # فقط بلوک عضویت اجباری (اولین بلوک) دستورهایش Bold است.
        add_item(title, cmds, bold_cmds=(index == 0))

    # بخش VIP
    vip_sub = [
        ("🥇برای vip کردن یک کاربر با دستور", ["ریپلای کنید : vip"]),
        ("برای لغو vip یک کاربر با دستور", ["ریپلای کنید : لغو vip"]),
        ("برای دیدن لیست vip ها با دستور", ["لیست vip"]),
    ]
    for vt, vcmds in vip_sub:
        add_item(vt, vcmds)

    blocks_after_vip = [
        ("🔐حالت سختگیرانه کلمات ممنوعه با دستور", [
            "فعال سازی : فعال کلمات ممنوعه",
            "غیر فعال سازی : لغو کلمات ممنوعه",
        ]),
        ("🔘 حذف یک پیام با ریپلای دستور", ["ریپلای کنید : پاک"]),
        ("🔘 حذف گروهی چند پیام آخر با دستور", [
            "پاک + اعداد 1 تا 800",
            "پاک 200 یا پاک 600",
        ]),
        ("برای پاک کردن کل پیام های یک کاربر ریپلای کنید روش", ["پاک کامل"]),
        ("🔇 سکوت یک کاربر با دستور", ["ریپلای کنید : سکوت"]),
        ("🔊رفع سکوت کاربر با دستور", ["ریپلای کنید : رفع سکوت"]),
        ("رفع سکوت با یوزنیم", ["رفع سکوت @osine۲ اول یوزنیم نوشته شود"]),
        ("💭 اخراج کاربر با دستور", ["ریپلای کنید : اخراج"]),
        ("💬 آزاد کردن کاربر اخراجی از لیست بن ربات", ["ریپلای کنید : آزاد"]),
        ("🗯  آزاد کردن با یوزنیم", ["آزاد @osine۲ اول یوزنیم نوشته شود"]),
        ("📌 سنجاق کردن یک پیام در گروه با دستور", ["ریپلای کنید : سنجاق"]),
        ("✂️ نمایش پیام سنجاق شده گروه با دستور", ["پیام سنجاق"]),
        ("🗂  دیدن لیست اینکه ادمین ها چه کسی و چه پیامی را اخراج یا حذف کردند با دستور", ["لاگ مدیریتی"]),
        ("📋 برای دیدن سابقه اخراج شده ها سکوت شده ها و اخطار کاربران (۲۴ ساعت اخیر ) دستور", ["سابقه ها"]),
        ("🗑 پاکسازی خودکار پیام های گروه با دستور", ["پاکسازی خودکار"]),
        # 🕹️ «اختصاصی»: عنوان Bold، دو دستور پشت سر هم داخل **یک** نقل‌قول.
        # 🔐 «پیام زماندار»: همان الگو.
        (READY_BLOCK, [
            "🕹️:برای اینکه وقتی گروه قفله یک کاربر بتونه پیام ارسال کنه دستور\n"
            "اختصاصی\n"
            "لغو اختصاصی",
            ("\nاختصاصی\nلغو اختصاصی",),
        ]),
        (READY_BLOCK, [
            "🔐 : برای زماندار کردن پیام های ارسالی در گروه\n"
            "پیام زماندار\n"
            "لغو پیام زماندار",
            ("\nپیام زماندار\nلغو پیام زماندار",),
        ]),
        # ⏰ «تأیید دو مرحله‌ای دستورات خطرناک» — همان الگوی کارفرما:
        # عنوان + هر دو دستور پشتِ سرِ هم در **یک** نقل‌قول؛ کلِ متن Bold.
        (READY_BLOCK, [
            "⏰ : برای تایید دو مرحله ای دستورات خطرناک بنویسید\n"
            "دو مرحله ای\n"
            "لغو دو مرحله ای",
            ("\nدو مرحله ای\nلغو دو مرحله ای",),
        ]),
        # 🧬 متن دقیقِ کارفرما (دو خط، بدون تکرار): عنوان Bold و خودِ دستور
        # داخل نقل‌قول سروش پلاس. «برای پاکسازی کامل پیام های گروه دستور»
        # فقط یک بار نوشته می‌شود و زیرش «پاکسازی کامل».
        (READY_BLOCK, [
            "🧬برای پاکسازی کامل پیام های گروه دستور\n"
            "پاکسازی کامل",
            ("\nپاکسازی کامل",),
        ]),
        ("🤖 فعال کردن هوش مصنوعی گوگل با دستور", [
            "فعال کردن : هوش مصنوعی فعال",
            "خاموش کردن : هوش مصنوعی خاموش",
        ]),
        ("👾انتخاب یک کاربر برای اینکه بتواند از هوش مصنوعی سوال بپرسد با دستور", [
            "ریپلای کنید : مجاز",
            "حذف دسترسی : غیر مجاز",
        ]),
        ("🔸 صفر کردن اخطار های که خودکار ربات به کاربر داده با دستور", ["ریپلای کنید : صفر"]),
        ("🔹 حذف اخطار های که مدیران به یک کاربر داده اند با دستور", ["ریپلای کنید : حذف اخطار"]),
        ("🎮خاموش کردن بازی های عمومی روباه با دستور", [
            "خاموش کردن : سرگرمی خاموش",
            "روشن کردن : سرگرمی روشن",
        ]),
        ("🎯 دیدن مهلت باقی مانده گروه شما با دستور", ["مهلت گروه"]),
        ("📮 کپی بورد حافظه بریده دان روباه با دستور", [
            "برای ایجاد کردن : کپی بورد",
            "برای نمایش پیام : کپی",
        ]),
        ("🖍 فیلتر کردن نام های کاربران در گروه با دستور", [
            "فیلتر اسم ← فیلتر اسم عسل",
            "لغو اسم ← لغو اسم عسل",
            "نمایش لیست ← لیست فیلتر اسم",
        ]),
        ("🔍 دیدن لیست افراد سکوت شده با دستور", ["لیست سکوت"]),
        ("🔍 برای پاک شدن تمام افراد لیست سکوت با دستور", ["پاک لیست سکوت"]),
        ("🔍 دیدن لیست تمام افراد اخراج شده توسط ربات با دستور", ["لیست اخراج"]),
        ("🔍برای خارج کردن افراد از لیست اخراجی های ربات با دستور", ["پاک لیست اخراج"]),
        # فیلتر لینک/آیدی بیو خودکار است و نیاز به فعال‌کردن ندارد؛ به همین
        # دلیل دستورهایش عمداً در «لیست ادمینی» نمایش داده نمی‌شوند. خود
        # قابلیت (modules/bio_link*) دست‌نخورده و فعال باقی می‌ماند.
        ("📑 برای اخراج یا سکوت دست جمعی نام ها از تمام لیست اعضای گروه", [
            "اخراج نام ← اخراج نام بیو چک",
            "سکوت نام ← سکوت نام بیو چک",
        ]),
        # ⛔️ «متخلف»: عنوان Bold، و «متخلف»/«لغو متخلف» هر دو Bold و هر دو
        # داخل **یک** نقل‌قول سروش پلاس (همان الگوی 🕹️/🔐).
        # بلوکِ تازه از **پایین** لیست اضافه می‌شود تا هیچ‌کدام از بلوک‌های
        # بالای لیست — از جمله «پاک لیست اخراج» و بلوکِ زیرِ آن که دستورهایشان
        # در نقل‌قول است — جابه‌جا نشوند؛ آفست‌ها در زمانِ ساخت حساب می‌شوند.
        (READY_BLOCK, [
            "⛔️ : برای اینکه یک کاربر ربات اجازه نده پیامی ازش ارسال بشه داخل گروه با ریپلای\n"
            "و برای لغو با یوزرنیم\n"
            "متخلف\n"
            "لغو متخلف @یوزرنیم",
            ("\nمتخلف\nلغو متخلف",),
        ]),
        # 🛎 «محدودیت پیام»: کل بلوک Bold و سه خطِ زیر عنوان با هم داخل یک نقل‌قول واحد.
        (READY_BLOCK, [
            "🛎 برای ایجاد محدودیت ارسال پیام برای یک کاربر ریپلای کنید بنویسید\n"
            "محدودیت پیام › بعد عدد مثال\n"
            "محدودیت پیام 4\n"
            "لغو محدودیت",
            ("\nمحدودیت پیام › بعد عدد مثال\nمحدودیت پیام 4\nلغو محدودیت",),
        ]),
    ]

    for title, cmds in blocks_after_vip:
        if title is READY_BLOCK:
            add_rich_item(cmds[0], quote_texts=cmds[1])
            continue
        add_item(title, cmds)

    contact = "تماس با سازنده : @OSINE2"
    rows.append((contact, [
        MessageEntityBold(offset=0, length=_u16(contact)),
        MessageEntityBlockquote(offset=0, length=_u16(contact)),
    ]))
    return rows


def build_admin_help():
    """تولید متن کامل راهنمای ادمین به همراه آرایه دقیق موجودیت‌های نقل‌قول و ضخیم."""
    return _join_rows(_admin_rows())


def build_admin_help_messages(max_quotes=MAX_QUOTES_PER_MESSAGE,
                              max_units=MAX_UNITS_PER_MESSAGE):
    """همان لیست، تکه‌تکه — تا هیچ نقلیول به‌خاطر ازدحام حذف نشود.

    هر تکه تعداد محدودی نقل‌قول دارد و هیچ ردیفی بین دو پیام نصف نمی‌شود.
    """
    return [_join_rows(chunk) for chunk in _split_rows(
        _admin_rows(), max_quotes=max_quotes, max_units=max_units)]


ADMIN_HELP_TEXT, ADMIN_HELP_ENTITIES = build_admin_help()
#: تکه‌های آمادهٔ ارسال (اولی همان ADMIN_HELP_TEXT است، فقط جابه‌جاشده).
ADMIN_HELP_MESSAGES = build_admin_help_messages()


# ---------------------------------------------------------------------------
# 📖 «راهنمای کانال» و «🚀 راهنمای اجرا» — دو متن راهنمای دستورهای کانال
# ---------------------------------------------------------------------------
# هر دو از یک تابع ساخته می‌شوند تا متن و entity هرگز از هم جدا نیفتند:
# بُردار offset با طول UTF-16 حساب می‌شود (استاندارد سروش پلاس/تلگرام).

CHANNEL_GUIDE_TITLE = "راهنمای دستورات کانال من"

#: (دستور، توضیح) — دستور Bold می‌شود
CHANNEL_GUIDE_ITEMS = (
    ("کانال من",
     "این دستور کانال رو به لیست خودش اضافه می‌کنه، اول باید کانال عمومی "
     "باشه. به این صورت مثلا « کانال من @euwoq »"),
    ("تنظیم عضویت اجباری", "برای فعال کردن عضویت اجباری"),
    ("لغو عضویت اجباری", "برای خاموش کردن قابلیت"),
    ("تست عضویت", "برای اینکه تست کنید یک کاربر عضو شده یا نه"),
)

SETUP_GUIDE_SUPPORT = "@aifox_bot"
SETUP_GUIDE_EXAMPLE = "کانال من @ai_fox"

def build_channel_guide():
    """«راهنمای کانال»: فقط نام دستورها Bold، بدون نقل‌قول."""
    parts = [CHANNEL_GUIDE_TITLE, "\n\n"]
    entities = []
    for command, description in CHANNEL_GUIDE_ITEMS:
        parts.append("» ")
        start = _u16("".join(parts))
        parts.append(command)
        entities.append(MessageEntityBold(offset=start, length=_u16(command)))
        parts.append("\n" + description + "\n\n")
    return "".join(parts).rstrip() + "\n", entities


SETUP_GUIDE_LINES = (
    ("bold",
     "قابلیت کانال من برای عضویت اجباری کاربری می‌باشد که تازه عضو گروه "
     "شده، شما باید اول از همه ربات رو عضو کانال کنید و اگه نتوانستید از "
     "طریق"),
    ("quote", f"ربات پشتیبانی : {SETUP_GUIDE_SUPPORT}"),
    ("bold",
     "لینک کانالتون بفرستید تا ربات عضو شود. در صورتی که فقط دارید از ربات "
     "تو گروهتون استفاده می‌کنید، ربات رو ادمین می‌کنید در کانال و حتما "
     "کانالتون عمومی باشه و دستور کانال من رو می‌نویسید به روش آیدی کانال"),
    ("bold", "مثال"),
    ("quote", SETUP_GUIDE_EXAMPLE),
)


def build_setup_guide():
    """«راهنمای اجرا»: کل متن Bold؛ خط پشتیبانی و خط مثال داخل نقل‌قول."""
    parts = []
    entities = []
    for kind, chunk in SETUP_GUIDE_LINES:
        start = _u16("".join(parts))
        parts.append(chunk)
        entities.append(MessageEntityBold(offset=start, length=_u16(chunk)))
        if kind == "quote":
            entities.append(MessageEntityBlockquote(
                offset=start, length=_u16(chunk)))
        parts.append("\n\n")
    return "".join(parts).rstrip() + "\n", entities


CHANNEL_GUIDE_TEXT, CHANNEL_GUIDE_ENTITIES = build_channel_guide()
SETUP_GUIDE_TEXT, SETUP_GUIDE_ENTITIES = build_setup_guide()
