# 🚦 تنظیمِ RPC — گزارشِ راند ۱۱

این سند فقط دربارهٔ **مسیرهایی** است که در لاگِ کلاینت (سروش پلاس) کند یا
خطادار بودند. هر تغییری با خطِ لاگی که آن را ثابت می‌کند و یک سناریوی تستِ
ساده آمده است. مسیرهای سالم (شبکه، `socket_wait=0`، `reconnects=0`،
RPC معمولیِ ۵۰–۳۱۰ms) دست نخورده‌اند.

| # | علامت در لاگ | جای تغییر | فایل |
|---|---|---|---|
| ۱ | `SNAPSHOT RETRY target_type=int error=IndexError('list index out of range')` | حذفِ شناسهٔ خام از مسیرِ snapshot + abort شفاف | `modules/admin_tools.py` |
| ۲ | `GetUsersRequest … 404 NOT_FOUND (user_id=7704762870259379992)` | خواندنِ مقاومِ کاربر (batch → fallbackِ تک‌به‌تک) | `modules/safe_rpc.py` |
| ۳ | ~۱۰ بار `GetParticipantsRequest` در ۱۴ ثانیه (chat_id=22389465) | کشِ ۶۰ ثانیه + single-flight + حداقل ۲ ثانیه فاصله | `modules/participant_cache.py` |
| ۴ | `GOVERNOR HOLD SLOW bucket=delete held_ms=530.4 limit=1 active=3` | حالتِ `auto` برای باکتِ delete + نگهبانِ FloodWait | `modules/rpc_governor.py` |
| ۵ | ترموکس بدونِ traceback بسته می‌شود | نگهبانِ حافظه + هرسِ کش‌ها + سقفِ snapshot | `modules/memory_guard.py`, `core/bot_working_split_ok.py` |

---

## ۱) پاک‌سازی خودکار: هرگز `channels[0]` روی لیستِ خالی

**علت:** `_cleanup_rpc_targets()` می‌توانست شناسهٔ **خام** (`int`) برگرداند. با
`int`، کتابخانه برای رسیدن به peer یک `GetChannelsRequest` می‌زند و بعد
`channels[0]` را می‌خواند؛ اگر لیست خالی باشد →
`IndexError: list index out of range` و کلِ snapshot شکست می‌خورد.

**تغییر:** خروجیِ `_cleanup_rpc_targets()` حالا **فقط** `InputPeer` است.
مسیرِ ساختِ peer:

1. `group_actions._group_peer` (اگر موجود بود)
2. `client.get_input_entity(chat_id)`
3. جایگزین‌های ساخته‌شده از `normalize_group_id` (`PeerChannel` / `InputPeerChannel`)
4. اگر هیچ‌کدام نشد: `SNAPSHOT FAILED … reason=no_resolvable_peer` و
   `ABORT reason=snapshot_failed action=cleanup_cancelled schedule_kept_for_retry`

خطاها به شکلِ `Type: msg` لاگ می‌شوند (نه `!r`) تا لاگ خوانا بماند. تعدادِ
snapshot به بازهٔ **۱ تا ۱۰۰۰۰** محدود شد (سقفِ حافظه/زمان روی ترموکس).

**سناریوی تست:** `tests/test_slow_and_error_fixes.py` بخش ۱ و ۹ —
`_snapshot_cleanup_message_ids()` با یک bot ساختگی که `iter_messages` دارد؛
بررسی می‌کند (الف) هیچ targetای `int` نیست، (ب) جایگزینِ `PeerChannel`
شناسهٔ نرمال‌شده را در خود دارد، (ج) اگر هیچ peerای قابلِ ساخت نبود،
پاک‌سازی **اجرا نمی‌شود** و گروه قفل نمی‌شود.

## ۲) `GetUsersRequest 404`: یک کاربرِ حذف‌شده کلِ batch را نمی‌کشد

**علت:** سروش اگر **یک** شناسه در لیست حذف شده باشد، کلِ درخواست را ۴۰۴
می‌کند؛ مسیرِ قبلی همان خطا را بالا می‌داد و snapshot شکست می‌خورد.

**تغییر:** `modules/safe_rpc.py::get_users_safe()`:

- شناسه‌های نامعتبر (غیرعدد، صفر، منفی) و تکراری از پیش حذف می‌شوند؛
- اول یک batch (۵۰تایی)؛ اگر شکست خورد، همان لیست **تک‌به‌تک** خوانده می‌شود؛
- شناسه‌های حذف‌شده با `GET USERS SKIP DELETED USER id=… reason=not_found`
  رد می‌شوند و بقیه برمی‌گردند؛
- در حالتِ سالم **هیچ RPC اضافه‌ای** زده نمی‌شود.

**سناریوی تست:** بخش ۴ همان فایل — یک `client.get_users` ساختگی که با
batch می‌شکند و تک‌به‌تک جواب می‌دهد؛ تست ثابت می‌کند کاربرانِ سالم
برمی‌گردند، شناسهٔ حذف‌شده حذف می‌شود و شناسه‌های نامعتبر فیلتر می‌شوند.

## ۳) `GetParticipants` — مقدارها و دلیلشان

| مقدار | انتخاب | چرا |
|---|---|---|
| TTL | **۶۰ ثانیه** | نقشِ مالک/ادمینِ اصلی در یک دقیقه عملاً تغییر نمی‌کند؛ نتیجه: هر گروه حداکثر **۱ RPC در دقیقه** به‌جای ~۱۰ در ۱۴ ثانیه. |
| حداقل فاصله بین دو RPC | **۲ ثانیه** | در حالتِ خطا (مثلاً FloodWait) polling به یک تلاش در ۲ ثانیه محدود می‌شود؛ هم دستورها پاسخ می‌مانند، هم فشارِ RPC کم است. |
| single-flight | دارد | ۱۰ درخواستِ هم‌زمان → **۱** RPC واقعی؛ بقیه منتظرِ همان future می‌مانند. |
| سقف | ۲۰۰۰ ورودی (LRU) | رشدِ نامحدود روی ترموکس ممنوع؛ ورودی‌های قدیمی هرس می‌شوند. |
| خطا | کش نمی‌شود | یک شکستِ موقت نباید ۶۰ ثانیه «مالک ندارد» را ثابت کند. |

آمار در `participant_cache.STATS` (`calls/hits/coalesced/throttled/errors`)
نگه داشته می‌شود تا بتوان از لاگ فهمید کش کار می‌کند.

**سناریوی تست:** بخش ۵ و ۶ همان فایل — ۱۰ فراخوانیِ هم‌زمان → فقط ۱ RPC؛
فراخوانیِ بعدی از کش؛ در حالتِ خطا بین دو تلاش ≥ ~۲ ثانیه فاصله.

## ۴) Governor: `bucket=delete` — مقایسهٔ دو گزینه (بدونِ انتخابِ دلبخواه)

وضعیتِ لاگ: `GOVERNOR HOLD SLOW bucket=delete held_ms=530.4 reason=rpc_in_flight
limit=1 active=3` و `OUTGOING RPC SLOW operation=delete_message
governor_wait_ms=250.7 rpc_await_ms=283.2`. یعنی حذف **منتظرِ نوبت** است، نه
اینکه شبکه کند باشد (`rpc_await_ms` نزدیک به نرمال).

| معیار | (الف) `delete_limit=2` ساده | (ب) اولویت/خروج از صف تا حذف پشتِ `GetChannelDifference` نماند | (ج) حالتِ `auto` (پیاده‌شده) |
|---|---|---|---|
| اثر روی `governor_wait_ms` | ~نصف می‌شود | نزدیکِ صفر وقتی باکتِ دیگری صف را گرفته | مثل (الف) در حالتِ سالم |
| ریسکِ `FLOOD_WAIT` | **دو برابر شدن هم‌زمانیِ `delete_messages`** → اگر سروش فشار را زیاد ببیند، `FLOOD_WAIT` می‌دهد و **همهٔ** حذف‌ها (از جمله پاک‌سازیِ خودکار) عقب می‌افتند | کم (هم‌زمانیِ delete بالا نمی‌رود) | با نگهبان مهار می‌شود: به‌محضِ یک FloodWait روی delete، سقف تا پایانِ cooldown به ۱ برمی‌گردد |
| پیچیدگی | یک خط | زیاد: نیاز به preemptionِ باکت‌آگاه، منصفانه‌بودن و جلوگیری از starvation در `_queues`/`_chat_rounds`؛ احتمالِ باگِ ظریف در قفل‌ها | متوسط: یک تابعِ سقف + یک هوک |
| ریسکِ starvationِ باکت‌های دیگر | کم | **زیاد** (اولویت‌دادن به delete می‌تواند `GetChannelDifference`/sync را گرسنه کند) | کم |
| مناسبِ ترموکس (حافظه/CPU) | خنثی | منفی (ساختار صفِ اضافه) | خنثی |

**نتیجه‌گیری (شفاف):** گزینهٔ (ب) بهترین «تجربه» را می‌دهد ولی تغییرِ
معماریِ صف است و در راندی که قرار است «فقط مسیرِ مشکل‌دار» عوض شود، ریسکش
بیشتر از سودش است. گزینهٔ (ج) همان سودِ (الف) را می‌دهد و ریسکِ FloodWait را
خودکار مهار می‌کند. بنابراین:

```
BOT_RPC_DELETE_LIMIT=auto     # سقف delete = ۲ فقط در حالتِ سالم
BOT_RPC_FLOOD_GUARD_S=60      # بعد از هر FloodWait روی delete، ۶۰ ثانیه سقف = ۱
```

**پیش‌فرضِ پروژه عوض نشد:** بدونِ این متغیرها همان `delete_limit=1`
محافظه‌کارانه می‌ماند. اگر بعد از چند روز `wait_s` در لاگِ
`GOVERNOR FLOOD GUARD` صفر بود، می‌شود `auto` را دائمی کرد؛ اگر FloodWait
تکرار شد، برگشتن به `1` یک خط است.

خطوطِ لاگِ جدید: `GOVERNOR FLOOD GUARD bucket=delete wait_s=… cooldown_s=60
delete_limit=1`.

**سناریوی تست:** بخش ۷ همان فایل — `auto` سقف ۲ می‌دهد، بعد از
FloodWait به ۱ برمی‌گردد، بعد از پایانِ cooldown دوباره ۲ می‌شود؛ بدونِ
تنظیم، سقف ۱ می‌ماند.

## ۵) ترموکس خودش بسته می‌شود

اندروید پروسهٔ پرمصرف را بی‌رحمانه (بدونِ traceback) kill می‌کند؛ آستانه
حدودِ ۳۵۰–۴۰۰MB است. `modules/memory_guard.py`:

- RSS از `/proc/self/status` هر **۶۰ ثانیه** خوانده می‌شود (ارزان، بدونِ RPC)؛
- **soft = 220MB**: هرسِ کش‌ها تا سقفِ معمولی؛
- **hard = 320MB**: هرسِ شدید + `gc.collect()`؛
- کش‌های ثبت‌شده: `participant_cache`، `reply_input_peer_cache`،
  `native_group_admin_ids_cache`؛
- سقفِ snapshot پاک‌سازی هم داخلِ همین منطق جا می‌گیرد (۱..۱۰۰۰۰).

خطِ لاگ: `MEMORY GUARD rss_mb=… level=soft|hard pruned=…`.

**سناریوی تست:** بخش ۸ همان فایل — RSS خوانده می‌شود، در فشارِ حافظه هرسِ
اضطراری انجام و کش تا سقفِ اضطراری کوچک می‌شود، و نگهبان در `core` راه‌اندازی
می‌شود.

---

## اجرا

```bash
PYTHONPATH=. python3 tests/test_slow_and_error_fixes.py     # ۳۵ بررسی
PYTHONPATH=. python3 -m pytest tests/test_auto_cleanup_resolution.py \
    tests/test_rpc_governor.py tests/test_rpc_send_resilience.py \
    tests/test_outgoing_rpc_lifecycle.py tests/test_foxora_rpc_queue.py -q
```
