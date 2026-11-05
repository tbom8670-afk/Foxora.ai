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

---

# 🔁 راند ۱۲ — طوفانِ `GetParticipants`، باکتِ heavy و کشِ پروفایل

لاگِ دوم نشان داد کندی **واقعی** است ولی شبکه سالم (`socket_wait=0`,
`reconnects=0`). ریشه در کد بود: برای **یک** `chat_id` سیزده
`GetParticipantsRequest` با فاصلهٔ ~۱۰۰ms زده می‌شد، همه با `bucket=heavy`.

## A) ریشهٔ واقعی طوفان: پیمایشِ صفحه‌به‌صفحهٔ فهرست اعضا

**شواهد:** ۲۲:۵۳:۵۸٫۷۴۲ تا ۲۲:۵۴:۰۳٫۰۹۳ → ۷ درخواست، سپس
۲۲:۵۴:۰۶٫۵۱۴ تا ۲۲:۵۴:۰۹٫۱۲۶ → ۶ درخواست؛ همه برای `chat_id=22389465` و
همه `bucket=heavy`؛ فاصلهٔ «پاسخ → درخواستِ بعدی» ~۱۰۰ms
(= `FORCE_JOIN_SCAN_DELAY` پیش‌فرض ۰٫۱).

**علت:** بررسیِ «آیا کاربر عضو کانال/گروه است؟» روی سروش
`getParticipant` ندارد، پس `find_member` کلِ فهرست را صفحه‌به‌صفحه
(`ChannelParticipantsRecent`, `limit=100`) از `offset=0` می‌خواند. برای ۱۳۰۰
عضو این **دقیقاً ۱۳ RPC** است؛ و برای هر پیام/هر بررسیِ بعدی از صفر تکرار
می‌شد. تست `tests/test_heavy_poll_and_profile_cache.py` بخش ۹ همان ۱۳ صفحه را
بازتولید می‌کند.

**تغییر (حداقلی):** یک قیفِ کش روی همان درخواستِ صفحه در
`modules/force_join_api.py::participants_page` و
`modules/participant_cache.py`:

* کلید = کانال + نوعِ فیلتر + `offset` + `limit` (پس صفحه‌های مختلف قاطی نمی‌شوند)؛
* **TTL = ۸ ثانیه** (بازهٔ درخواستی ۵ تا ۱۰): تغییرِ لیست اعضا نادر است و سروش
  خودش update می‌فرستد؛ کوتاه‌تر فشار را کم نمی‌کند و بلندتر از ۱۰ کهنه می‌شود؛
* **حداقل فاصله = ۱٫۵ ثانیه** (بازهٔ ۱ تا ۲) بین دو RPC واقعی برای همان
  `chat_id`؛
* **coalescing:** درخواست‌های هم‌زمانِ همان کلید به یک Future وصل می‌شوند؛
* لاگ‌های خواسته‌شده: `GetParticipants cache hit chat=X` و
  `GetParticipants coalesced chat=X`.

⚠️ استثنای مستند: پیمایشِ صفحه‌به‌صفحه `min_interval=0` می‌فرستد. یک اسکن
«یک عملیات» است که خودش `delay` و `budget_seconds=20` دارد؛ اعمالِ ۱٫۵ ثانیه
بین ۱۳ صفحه، اسکن را از بودجه بیرون می‌انداخت (خراب‌کردنِ مسیرِ سالم). طوفان
هم برنمی‌گردد، چون تکرارِ همان اسکن در ۸ ثانیه صد درصد از کش می‌آید.

**سناریوی تست (بخش ۱ و ۹ تستِ راند):** ۱۳ فراخوانیِ پشتِ‌سرِهم → **۱ RPC +
۱۲ cache hit**؛ اسکنِ دومِ کامل (۱۳ صفحه) → **صفر RPC اضافه**.

## B) دکمه‌های دیگرِ همان مسیر

* `modules/participant_cache.py` TTL از ۶۰ → **۸ ثانیه** و حداقل فاصله ۲ →
  **۱٫۵ ثانیه** (طبقِ درخواست؛ مالکِ گروه هنوز حداکثر ۱ RPC در ۸ ثانیه).
* دو مسیرِ «سکوت/اخراج نام …» در `message_handler` و بازشماریِ
  `group_words_commands` هم به همان کش وصل شدند (قبلاً هر دستور یک
  `GetParticipants` کامل می‌زد).

## C) Governor — سه گزینه، مقایسه و انتخابِ شفاف

لاگ‌های مرتبط::

    22:54:00.753 OUTGOING RPC SLOW operation=GetParticipantsRequest governor_wait_ms=204.3
    22:54:00.982 OUTGOING RPC SLOW operation=GetParticipantsRequest governor_wait_ms=501.4
    22:54:01.834 RPC GOVERNOR WAIT GetFullUserRequest wait_ms=412.7
    22:54:08.716 GOVERNOR HOLD SLOW bucket=notice request=SendMessageRequest held_ms=503.9 reason=rpc_in_flight
    22:54:08.716 GOVERNOR HOLD SLOW bucket=delete request=DeleteMessagesRequest held_ms=502.0 reason=rpc_in_flight

| معیار | (الف) `delete_limit`/`notice_limit` را ۱→۲ کنیم | (ب) اولویت‌دهیِ buckets | (ج) سقفِ مستقلِ heavy/poll |
|---|---|---|---|
| اثر روی `governor_wait_ms` | کم (تعداد هم‌زمانی بالا می‌رود، صف کوتاه‌تر) | زیاد | متوسط |
| ریسکِ FloodWait | **بالا**: دو delete هم‌زمان = ریسکِ `FLOOD_WAIT` و توقفِ همهٔ حذف‌ها (شاملِ پاک‌سازی خودکار) | کم | کم |
| ریسکِ گرسنگی (starvation) | کم | **زیاد** اگر بی‌محافظ باشد (اولویت‌دادن به delete می‌تواند sync را گرسنه کند) | کم |
| تغییرِ معماری | یک خط | زیاد (preemptionِ باکت‌آگاه در صف) | کم |
| ترموکس | خنثی | منفی (ساختار صفِ اضافه) | خنثی |
| **انتخاب** | فقط به‌صورتِ اختیاری (`BOT_RPC_DELETE_LIMIT=auto` از راند ۱۱) | **انجام شد، محافظه‌کارانه** | **انجام شد** |

**سه تغییرِ پیاده‌شده در Governor:**

1. **سقفِ مستقل برای polling** (`bucket=poll`, `BOT_RPC_POLL_LIMIT=1`):
   `GetParticipantsRequest` از `heavy` جدا شد تا با خواندنِ تاریخچه/کانال
   سهمیهٔ مشترک نداشته باشد. (`GetHistoryRequest` همچنان `heavy` است.)
2. **read سنگین سهمِ send/notice را نمی‌خورد:** در `_eligible` شرطِ آخر با
   `_active_noncritical - read_active` سنجیده می‌شود (قبلاً خام). این همان
   `GOVERNOR HOLD SLOW bucket=notice held_ms=503.9` را می‌بندد: یک
   `GetParticipants` در جریان + یک `delete` دیگر همهٔ سهمِ ۲تایی را پر
   نمی‌کند.
3. **yieldِ heavy/poll:** اگر هر `notice`/`send`/`delete` در صف باشد،
   خواندنِ سنگینِ جدید **شروع نمی‌شود** (تا وقتی صف خالی شود). قابلِ برگشت با
   `BOT_RPC_HEAVY_YIELD_P2=0`.

**پاسخ به پرسشِ «P3 یا P4؟»:** P4 نیازمندِ یک سطحِ اولویتِ جدید + قاعدهٔ
promotionِ سنّی است تا اسکنِ عضویت در ترافیکِ سنگین هرگز گرسنه نشود؛ با
توجه به اینکه (الف) کشِ صفحه‌ها ۹۰٪+ بار را حذف می‌کند و (ب) heavy/poll
پایین‌ترین اولویت را دارند و حالا به صفِ بالاتر yield می‌دهند، افزودنِ P4 در
این راند ریسکِ بی‌فایده بود. اگر بعد از راند ۱۲ باز هم
`RPC GOVERNOR WAIT bucket=poll` دیدید، همان P4 با promotion گامِ بعد است.

**لاگ‌های غنی‌شده (بدون تغییرِ رفتار):**
`GOVERNOR HOLD SLOW … culprit=heavy:GetParticipantsRequest:chat=…:held_ms=612`
و `RPC GOVERNOR WAIT … holders=[…]` — یعنی دقیقاً معلوم است کدام RPC باعثِ
hold شده.

**سناریوهای تست:** بخش ۴ (باکت و سقفِ poll)، ۵ (notice با خواندنِ سنگین در
جریان)، ۶ (yield به صفِ notice).

## D) `GetFullUserRequest` — کشِ پروفایل

**شواهد:** `RPC GOVERNOR WAIT GetFullUserRequest wait_ms=412.7` و
`BIO LINK CHECK user_id=70874302 status=ok` و بعد
`ERROR PERF WARNING chat_id=24100530 stage=RECEIVE elapsed_ms=1218.02`.

**علت:** بررسیِ بیو برای هر **(گروه، کاربر)** ذخیره می‌شود، ولی پروفایل
مالِ **کاربر** است: یک کاربر در ۵ گروه ⇒ تا ۵ `GetFullUserRequest`؛ به‌علاوهٔ
`refresh_before_punish`. هر کدام یک RPC و داخلِ مسیرِ پیام.

**تغییر:** `modules/user_profile_cache.py` (TTL **۴۵ ثانیه**، بازهٔ معقول
۳۰–۶۰؛ حداقل فاصله ۲ ثانیه؛ coalescing؛ LRU ۴۰۰۰؛ فقط `ok`/`empty` کش
می‌شود و `error`/`no_access`/`flood_wait` هرگز) و اتصالِ آن به
`bio_link_service.fetch_about`. لاگ‌ها: `GETFULLUSER cache hit user=X` و
`GETFULLUSER coalesced user=X`.

**سناریوی تست (بخش ۷):** همان کاربر در ۵ گروه → ۱ RPC؛ خطا کش نمی‌شود.

## E) `PERF WARNING` — حالا می‌گوید وقت کجا خورده

قبلاً فقط `stage=RECEIVE elapsed_ms=1218.02` بود و معلوم نبود گلوگاه
`GetFullUser` است یا precheck یا بیو. حالا `MessagePerformance.note()` هست و
خطِ هشدار این‌طور می‌شود::

    PERF SLOW chat_id=24100530 total_ms=1218.02 slowest=receive:1200.4ms
      breakdown=receive=1200.4,admin_check=9.1 notes=bio_check=812.3,banned_word=0.6
    PERF WARNING chat_id=24100530 stage=RECEIVE elapsed_ms=1200.40
      slowest=receive:1200.4ms notes=bio_check=812.3,banned_word=0.6

`bio_check` و `banned_word` در `handlers/message_handler.py` اندازه‌گیری
می‌شوند (شاملِ انتظار در Governor برای `GetFullUserRequest`).

**سناریوی تست:** بخش ۸ — زیرمرحله‌ها در خطِ هشدار ظاهر می‌شوند.

## متغیرهای محیطیِ این راند

```env
BOT_RPC_POLL_LIMIT=1        # سقفِ هم‌زمانیِ GetParticipants (لِین مستقل)
BOT_RPC_HEAVY_YIELD_P2=1    # heavy/poll به صفِ send/notice yield بدهد
# از راند ۱۱:
BOT_RPC_DELETE_LIMIT=auto
BOT_RPC_FLOOD_GUARD_S=60
```

## فرضیات

1. `GetParticipantsRequest` تنها منبعِ پیمایشِ اعضاست و سروش `getParticipant`
   برای حسابِ ربات نمی‌دهد (کدِ فعلی همین را فرض کرده).
2. فهرست اعضا در ۸ ثانیه عملاً ثابت است (سروش update می‌فرستد).
3. بیوگرافی در ۴۵ ثانیه تغییرِ مؤثر ندارد؛ مجازاتِ بیو با «شاهدِ تازه»
   (`BIO_LINK_PUNISH_FRESHNESS_SECONDS`) جدا محافظت می‌شود.
4. `bucket=delete`/`notice` را ۲ نکردیم؛ ریسکِ FloodWait آن بیشتر از سودش است.
5. هیچ تغییر رفتاری در `SendMessage` معمولی، `GetDifference`،
   `GetChannelDifference` و مسیرهای بازی/دادگاه داده نشده است.
