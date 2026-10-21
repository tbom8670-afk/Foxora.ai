# 🦊 Foxora

ربات مدیریت و ضد-هرزنامهٔ **سروش پلاس** — نسخهٔ مستقل ساخته‌شده از روی کد
واقعی و فعلی ربات **B2** (`splusesupor-oss/soroush-plus-antispam-bot-v2`)،
همراه با قابلیت‌های حفظ‌شدهٔ لاگ/CPU/صفِ نسخهٔ قبلی Foxora.

## ویژگی‌های کلیدی

- ضد-اسپم لایه‌ای (لینک، شماره، یوزرنیم، کلمات ممنوعه، فوروارد، GIF و رسانه)
- مدیریت کامل گروه: سکوت/بن/اخطار، قوانین، وایت‌لیست، انقضای گروه
- اقتصاد داخلی (سکه، فروشگاه، رتبه‌بندی) و بانک سرگرمی/بازی‌ها
- Watchdog با ری‌استارت خودکار و گزارش خصوصی به مالک
- معماری صف کران‌دار و منصفانه برای درخواست‌ها + governor اتصال مشترک
- لاگ غیرمسدودکننده با چرخش خودکار و سرکوب خطوط پرتکرار

## مسیر اجرا و جدایی از B2

| مورد | Foxora | B2 (دست‌نخورده) |
|---|---|---|
| دایرکتوری پروژه | `~/Foxora` | مسیر clone خودش |
| دادهٔ اجرایی (session/DB/config/log/backup) | `~/.foxora` | `~/.local/share/soroush-bot-bot2` |
| فایل اجرای اصلی | `foxora.py` | `bot2.py` |
| دستور اجرا | `./foxora` | `b2` |
| نشانگر instance | `.bot_instance` = `foxora` | `bot2` |
| متغیر session | `SOROUSH_SESSION_STRING_FOXORA` | `SOROUSH_SESSION_STRING_BOT2` |

Foxora هیچ فایل، session یا دیتابیسی از B2 را نمی‌خواند و نمی‌نویسد؛ قفل PID
هر instance جداست و دو ربات می‌توانند هم‌زمان روی یک دستگاه کار کنند.

## نصب (Termux)

```bash
git clone https://github.com/tbom8670-afk/Foxora.ai.git ~/Foxora
cd ~/Foxora
pip install -r requirements.txt
cp .env.example .env   # سپس session را داخل .env پر کنید
```

### اجرا

```bash
cd ~/Foxora
./foxora           # اجرا با Watchdog (پیشنهادی؛ ری‌استارت خودکار)
./foxora direct    # اجرای مستقیم بدون Watchdog
./foxora status    # وضعیت
./foxora stop      # توقف فقط Foxora
```

اولین اجرا برای ساخت session از شما ورود تعاملی می‌خواهد؛ session فقط در
حافظه نگه داشته می‌شود (StringSession) و فایلی روی دیسک نوشته نمی‌شود.

### مالک

مالک در `config/owner.json` (canonical) تعریف می‌شود و در اولین اجرا به
`~/.foxora/config/owner.json` هم‌گام می‌شود.

## ساختار

```
foxora.py        لانچر instance (BOT_INSTANCE=foxora → ~/.foxora)
foxora           اسکریپت فرمان run/direct/stop/status
main.py          entrypoint اصلی (ارثی از B2)
watchdog.py      ناظر دائمی crash/restart
core/            مغز ربات (SoroushAntiSpamBot)
handlers/        روترهای پیام/ادمین/پخش/اقتصاد/بازی/…
modules/         ماژول‌ها: runtime_paths, rpc_governor, moderation_queue,
                 outgoing_sender, logger_module, rpc_queue, fair_queue,
                 fair_delete_queue (قابلیت‌های محفوظ Foxora) و…
economy/         سکه/فروشگاه/رتبه‌بندی
tests/           ~۱۱۰ تست B2 + تست‌های Foxora (مسیر/صف/لاگ/pacing)
docs/FOXORA_PRESERVED_FEATURES.md   نگاشت دقیق قابلیت‌های حفظ‌شده
```

## قابلیت‌های حفظ‌شده از Foxora قبلی

قبل از جایگزینی با کد B2، قابلیت‌های زیر شناسایی و **حفظ** شدند:

1. **بهینه‌سازی لاگ و CPU** — لاگ غیرمسدود با صف کران‌دار و drop-on-saturation،
   سرکوب لاگ‌های پرتکرار پیام‌محور (`BOT_VERBOSE_LOGS`)، چرخش خودکار
   (`BOT_LOG_MAX_BYTES/BOT_LOG_BACKUPS`).
2. **صف درخواست‌های گروه‌ها** — `SerialRpcQueue` (یک RPC در هر لحظه، fairness
   بین گروه‌ها، bounded، dedupe فقط با کلید صریح)، `FairModerationQueue`،
   `FairDeleteQueue` و knob pacing `RPC_MIN_INTERVAL_SECONDS` که روی مسیر
   زندهٔ `RpcGovernor` هم اعمال می‌شود.

نگاشت کامل: [`docs/FOXORA_PRESERVED_FEATURES.md`](docs/FOXORA_PRESERVED_FEATURES.md)

## تست

```bash
python3 -m pytest tests/ -q
```

## مشارکت‌های شخص ثالث

کد پایه از مخزن «soroush-plus-antispam-bot-v2» (ربات B2) منشعب شده است؛ به
توسعه‌دهندگان آن اعتبار تعلق دارد.

---

## 🖇 تشخیص لینک در بیوگرافی کاربران

کاربری که بیوگرافی‌اش لینک دارد با همان سیستم اخطار/حذف/سکوت یا اخراج فعلی
ربات مدیریت می‌شود. **بیوی هر کاربری که در گروه پیام می‌دهد، همان لحظهٔ اولین
پیام بررسی می‌شود** (و ۶ ساعت کش می‌شود). اسکن کامل اعضا پیش‌فرض خاموش است.

معاف: مالک ربات، مالک گروه، ادمین‌های ثبت‌شده، ادمین/سازندهٔ واقعی گروه و VIP.

- دستورها: `فیلتر لینک بیو` ، `لغو فیلتر لینک بیو` ، `وضعیت لینک بیو` ،
  `بررسی بیو` ، `لیست لینک بیو` ، `پاک لیست لینک بیو` ،
  `فیلتر آیدی بیو` / `لغو فیلتر آیدی بیو`
- مستندات و محدودیت‌ها: [`docs/BIO_LINK_FILTER.md`](docs/BIO_LINK_FILTER.md)
