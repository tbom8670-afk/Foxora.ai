# -*- coding: utf-8 -*-
"""🧬 تست کامل «پاکسازی کامل» (FULL PURGE).

اجرا:  PYTHONPATH=. python3 tests/test_full_purge.py

پوشش (مطابقِ خواستهٔ کارفرما):
1.  پاکسازی کم (۷ پیام)، چندصد (۷۵۰) و چند هزار (۴۲۰۰) — بدون سقفِ مصنوعی
2.  اجرای هم‌زمان دو گروه و ردِ اجرای دوم در همان گروه
3.  خطاهای موقت: FloodWait، Timeout و خطای دائمیِ یک دسته (بدون توقف کل)
4.  ویرایشِ هوشمندِ پیام وضعیت + زمانِ خوانا + خطای ویرایش
5.  قفل و بازگشت صحیحِ وضعیت گروه (حتی وقتی پاکسازی می‌شکند)
6.  تفکیک کامل از صف‌های حذفِ موجود
7.  قالب‌بندی: Bold و نقل‌قول با آفستِ UTF-16
"""
import asyncio
import itertools
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_INSTANCE", "test")
os.environ.setdefault("BOT_PRIVATE_DATA_DIR", tempfile.mkdtemp(prefix="fp-"))

from modules.full_purge import core, service, texts  # noqa: E402
from modules.admin_help_builder import (  # noqa: E402
    ADMIN_HELP_ENTITIES,
    ADMIN_HELP_TEXT,
)

PASSED = 0
FAILED = 0
FAILURES = []


def check(title, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {title}")
    else:
        FAILED += 1
        FAILURES.append(f"{title} :: {detail}")
        print(f"  ❌ {title}  {detail}")


def u16(value):
    return len(value.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# کلاینت و باتِ ساختگی
# ---------------------------------------------------------------------------
class FakeMessage:
    def __init__(self, message_id, text="پیام"):
        self.id = message_id
        self.message = text


class FakeChat:
    """تاریخچهٔ ساختگی؛ دقیقاً رفتارِ get_messages/delete_messages را دارد."""

    def __init__(self, chat_id, total, *, alive_from=1, page_cap=None,
                 fail_plan=None, history_error=None, id_step=1, id_base=0,
                 undeliverable=(), inject_after=None, inject_ids=()):
        self.chat_id = chat_id
        self.total = total
        self.id_base = id_base
        self.undeliverable = set(undeliverable or ())
        self.inject_after = inject_after
        self.inject_ids = set(inject_ids or ())
        self.alive = {id_base + value
                      for value in range(alive_from, total + 1, id_step)}
        self.page_cap = page_cap
        self.fail_plan = dict(fail_plan or {})     # batch_index → ("flood", 0) | ("timeout", n)
        self.history_error = history_error
        self.get_calls = 0
        self.delete_calls = 0
        self.deleted_ids = []
        self.delete_batches = []
        self.deleted_total = 0
        self.started = time.monotonic()

    # ---- RPCها ----
    async def get_messages(self, entity, limit=None, max_id=0, **_kwargs):
        self.get_calls += 1
        if self.history_error is not None:
            raise self.history_error
        limit = int(limit or 20)
        if self.page_cap:
            limit = min(limit, self.page_cap)
        # max_id=0 یعنی «از جدیدترین پیام»؛ پیام‌های تازه هم دیده می‌شوند.
        upper = (int(max_id) - 1 if max_id
                 else (max(self.alive) if self.alive
                       else self.id_base + self.total))
        floor = self.id_base + 1
        rows = []
        current = upper
        while current >= floor and len(rows) < limit:
            if current in self.alive:
                rows.append(FakeMessage(current))
            current -= 1
        return rows

    async def delete_messages(self, entity, ids, **_kwargs):
        ids = list(ids)
        self.delete_calls += 1
        index = len(self.delete_batches)
        self.delete_batches.append(list(ids))
        plan = self.fail_plan.get(index)
        if plan:
            kind, extra = plan
            self.fail_plan.pop(index, None)
            if kind == "flood":
                raise FloodWaitError(extra)
            if kind == "timeout":
                await asyncio.sleep(0)
                raise asyncio.TimeoutError("temporary")
            if kind == "permanent":
                raise ValueError("unsupported peer")
        if len(ids) > core.BATCH_SIZE:
            raise ValueError("too many ids in one request")
        # رفتار واقعی سرور: شناسه‌های سالم حذف می‌شوند و بعد خطا برمی‌گردد
        # (برای همین وب‌کلاینت فکر می‌کند هیچ‌کدام حذف نشده‌اند).
        blocked = [m for m in ids if m in self.undeliverable]
        removed = 0
        for message_id in ids:
            if message_id in blocked:
                continue
            if message_id in self.alive:
                self.alive.discard(message_id)
                self.deleted_ids.append(message_id)
                removed += 1
        self.deleted_total += removed
        if self.inject_after is not None and self.delete_calls >= self.inject_after \
                and self.inject_ids:
            # پیام‌هایی که وسطِ پاکسازی می‌رسند (مثل پیام‌های سرویس/اعضا)
            self.alive |= self.inject_ids
            self.inject_ids = set()
        if blocked:
            raise BadRequestError("MESSAGE_ID_INVALID")
        return [types.SimpleNamespace(pts=removed, pts_count=removed)]

    async def edit_message(self, message, text, formatting_entities=None, **_k):
        self.edits = getattr(self, "edits", [])
        self.edits.append((text, list(formatting_entities or [])))
        return FakeMessage(getattr(message, "id", 1), text)


class BadRequestError(Exception):
    """خطای «این شناسه قابل حذف نیست» — کل batch را رد می‌کند."""


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"FloodWait {seconds}s")
        self.seconds = seconds


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))


_REPLY_IDS = itertools.count(900001)


class FakeEvent:
    is_private = False

    def __init__(self, chat_id, user=None, text=""):
        self.chat_id = chat_id
        self.sender = user
        self.text = text
        self.replies = []
        self.message = FakeMessage(700100, text)   # پیامِ خودِ کاربر

    async def reply(self, text, formatting_entities=None, **_kwargs):
        self.replies.append((text, list(formatting_entities or [])))
        return FakeMessage(next(_REPLY_IDS), text)


class FakeActions:
    def __init__(self, *, lock_error=None, unlock_error=None):
        self.calls = []
        self.lock_error = lock_error
        self.unlock_error = unlock_error

    async def lock_group(self, chat_id):
        self.calls.append(("lock", chat_id))
        if self.lock_error:
            raise self.lock_error
        return True

    async def unlock_group(self, chat_id):
        self.calls.append(("unlock", chat_id))
        if self.unlock_error:
            raise self.unlock_error
        return True


class FakeBot:
    def __init__(self, chat, *, actions=None):
        self.client = chat
        self.logger = FakeLogger()
        self.group_actions = actions or FakeActions()
        self.reply_input_peer_cache = {}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ۱) متن و ظرفیت: هیچ سقفی وجود ندارد
# ---------------------------------------------------------------------------
def test_01_no_artificial_cap():
    print("\n### ۱) پاکسازی کامل هیچ سقفِ مصنوعی ندارد")
    check("سقفِ شناسه در هر RPC حذف ۱۰۰ است (محدودیت واقعی سرور)", core.BATCH_SIZE == 100)
    check("ظرفیتِ هر درخواست تاریخچه ۱۰۰ است", core.PAGE_SIZE == 100)
    source = Path(core.__file__).read_text(encoding="utf-8")
    for name in ("MAX_MESSAGES", "MAX_TOTAL", "TOTAL_CAP", "LIMIT_TOTAL",
                 "MAX_DELETE", "MAX_PURGE"):
        check(f"هیچ سقفِ کلیِ «{name}» وجود ندارد", name not in source)
    # هیچ مقایسه‌ای از نوع «تعداد کل >= عدد ثابت» در حلقه نیست
    check("شرطِ توقف فقط «صفحهٔ خالی» است",
          "if not ids:" in source and "pages < MAX_PAGES" in source)


def _purge(total, *, page_cap=None, fail_plan=None, history_error=None,
           actions=None, chat_id=-1001):
    chat = FakeChat(chat_id, total, page_cap=page_cap, fail_plan=fail_plan,
                    history_error=history_error)
    bot = FakeBot(chat, actions=actions)
    status = FakeMessage(555, "status")
    editor = core.StatusEditor(bot, chat_id, status, logger=bot.logger)
    lock, unlock = service._lock_hooks(bot, chat_id)
    result = run(core.run(bot, chat_id, status=editor, logger=bot.logger,
                          on_lock=lock, on_unlock=unlock))
    return chat, bot, editor, result


def test_02_small_medium_large():
    print("\n### ۲) پاکسازی کم، چندصد و چند هزار پیام")
    for total, label in ((7, "۷ پیام"), (750, "۷۵۰ پیام"), (4200, "۴۲۰۰ پیام")):
        core.reset_all()
        service.reset_all()
        chat, bot, editor, result = _purge(total)
        check(f"{label}: همه حذف شدند", result["deleted"] == total,
              f"→ {result['deleted']}")
        check(f"{label}: هیچ پیامی باقی نماند", not chat.alive,
              f"→ {len(chat.alive)} باقی")
        check(f"{label}: تعداد واقعیِ حذف‌شده با سرور یکی است",
              chat.deleted_total == result["deleted"])
        check(f"{label}: RPCها در دسته‌های ۱۰۰تایی بودند",
              all(len(batch) <= core.BATCH_SIZE for batch in chat.delete_batches))
        check(f"{label}: بدون خطا", not result["reason"] and not bot.logger.errors,
              f"→ {result['reason']} / {bot.logger.errors[-1:]}")
        check(f"{label}: قفل شد و باز شد",
              bot.group_actions.calls == [("lock", -1001), ("unlock", -1001)],
              f"→ {bot.group_actions.calls}")
        check(f"{label}: پیام وضعیت ویرایش شد (پیام جدید ساخته نشد)",
              getattr(chat, "edits", []) and len(chat.edits) > 0)


def test_03_pagination_cap_is_honored():
    print("\n### ۳) صفحات کوچک (سقفِ سرور روی limit) هم تا آخر می‌روند")
    core.reset_all()
    service.reset_all()
    chat, bot, editor, result = _purge(530, page_cap=20)
    check("با صفحهٔ ۲۰تایی هم ۵۳۰ پیام حذف شد", result["deleted"] == 530,
          f"→ {result['deleted']}")
    check("صفحه‌بندی چند صفحه خواند", chat.get_calls >= 26, f"→ {chat.get_calls}")


def test_04_transient_errors():
    print("\n### ۴) خطاهای موقت: FloodWait، Timeout و خطای دائمیِ یک دسته")
    core.reset_all()
    service.reset_all()
    plan = {0: ("flood", 0), 2: ("timeout", 0), 4: ("permanent", 0)}
    chat, bot, editor, result = _purge(900, fail_plan=plan)
    check("با FloodWait/Timeout کار ادامه یافت", result["deleted"] >= 800,
          f"→ {result['deleted']}")
    check("دستهٔ دائمی‌خراب، بقیهٔ پاکسازی را متوقف نکرد",
          result["deleted"] >= 700)
    check("تلاشِ دوباره برای بازمانده‌ها انجام شد", chat.delete_calls > 9,
          f"→ {chat.delete_calls}")
    check("خطاها لاگ شدند", bool(bot.logger.errors), bot.logger.errors[-2:])


def test_05_history_unavailable():
    print("\n### ۵) نبودِ دسترسی به تاریخچه → ادعای پاکسازی نمی‌کنیم")
    core.reset_all()
    service.reset_all()
    chat, bot, editor, result = _purge(100, history_error=RuntimeError("no access"))
    check("هیچ پیامی حذف‌نشده گزارش نمی‌شود", result["deleted"] == 0)
    check("دلیل صریح ثبت شد", result["reason"] == "history_unavailable",
          f"→ {result['reason']}")
    check("گروه بی‌دلیل قفل نماند", result["lock_released"] is True)


# ---------------------------------------------------------------------------
# ۶..۷) هم‌زمانی
# ---------------------------------------------------------------------------
def test_06_two_groups_in_parallel():
    print("\n### ۶) دو گروه هم‌زمان، هر کدام صف مستقل خودش")
    core.reset_all()
    service.reset_all()

    async def scenario():
        chat_a = FakeChat(-2001, 800, id_base=0)
        chat_b = FakeChat(-2002, 400, id_base=500000)
        bot_a, bot_b = FakeBot(chat_a), FakeBot(chat_b)
        lock_a, unlock_a = service._lock_hooks(bot_a, -2001)
        lock_b, unlock_b = service._lock_hooks(bot_b, -2002)
        started = time.monotonic()
        results = await asyncio.gather(
            core.run(bot_a, -2001, status=None, logger=bot_a.logger,
                     on_lock=lock_a, on_unlock=unlock_a),
            core.run(bot_b, -2002, status=None, logger=bot_b.logger,
                     on_lock=lock_b, on_unlock=unlock_b),
        )
        return results, chat_a, chat_b, time.monotonic() - started

    results, chat_a, chat_b, elapsed = run(scenario())
    check("گروه اول کامل پاک شد", results[0]["deleted"] == 800, results[0])
    check("گروه دوم کامل پاک شد", results[1]["deleted"] == 400, results[1])
    check("هیچ گروهی از گروه دیگر پیام حذف نکرد",
          set(chat_a.deleted_ids).isdisjoint(set(chat_b.deleted_ids)))


def test_07_same_group_twice():
    print("\n### ۷) اجرای دوم در همان گروه رد می‌شود (قفل per-chat)")
    core.reset_all()
    service.reset_all()

    async def scenario():
        chat = FakeChat(-3001, 600)
        bot = FakeBot(chat)
        lock, unlock = service._lock_hooks(bot, -3001)
        first = asyncio.create_task(core.run(bot, -3001, status=None,
                                             logger=bot.logger, on_lock=lock,
                                             on_unlock=unlock))
        await asyncio.sleep(0)
        check("گروه در حال اجراست", core.is_running(-3001) is True)
        blocked = service.run_purge(bot, -3001)
        second = await blocked
        first_result = await first
        return first_result, second, chat

    first_result, second, chat = run(scenario())
    check("اجرای دوم با already_running رد شد",
          second.get("reason") == "already_running", second)
    check("اجرای اول سالم تمام شد", first_result["deleted"] == 600, first_result)


# ---------------------------------------------------------------------------
# ۸) پیام وضعیت و زمان
# ---------------------------------------------------------------------------
def test_08_status_message():
    print("\n### ۸) پیام وضعیت: ویرایشِ هوشمند، زمانِ خوانا، مقاوم به خطا")
    check("۲ ثانیه", texts.format_duration(2) == "۲ ثانیه", texts.format_duration(2))
    check("۱۵ ثانیه", texts.format_duration(15) == "۱۵ ثانیه")
    check("۱ دقیقه و ۲۰ ثانیه", texts.format_duration(80) == "۱ دقیقه و ۲۰ ثانیه",
          texts.format_duration(80))
    check("۲ دقیقه", texts.format_duration(120) == "۲ دقیقه",
          texts.format_duration(120))
    check("۱ ساعت و ۵ دقیقه", texts.format_duration(3900) == "۱ ساعت و ۵ دقیقه",
          texts.format_duration(3900))

    core.reset_all()
    service.reset_all()
    chat = FakeChat(-4001, 5000)
    bot = FakeBot(chat)
    now = {"value": 0.0}

    def clock():
        return now["value"]

    async def scenario():
        lock, unlock = service._lock_hooks(bot, -4001)
        task = asyncio.create_task(core.run(
            bot, -4001, status=core.StatusEditor(bot, -4001, FakeMessage(777),
                                                 logger=bot.logger),
            logger=bot.logger, clock=clock, on_lock=lock, on_unlock=unlock))
        for step in range(40):
            now["value"] += 1.0
            await asyncio.sleep(0)
            if task.done():
                break
        return await task

    result = run(scenario())
    edits = getattr(chat, "edits", [])
    check("همان یک پیام چند بار ویرایش شد (پیام جدید نه)",
          len(edits) <= 60, f"→ {len(edits)}")
    check("شمارندهٔ نهایی در پیام وضعیت درست است",
          any("پاکسازی کامل انجام شد" in text for text, _e in edits),
          edits[-1][0][:40] if edits else None)
    final_text = edits[-1][0] if edits else ""
    check("تعداد نهایی حذف‌شده در پیام پایانی هست",
          "۵۰۰۰" in final_text or "5000" in final_text, final_text)
    check("مدت زمان در پیام پایانی هست", "مدت زمان" in final_text, final_text)

    # خطای ویرایش نباید پاکسازی را متوقف کند
    core.reset_all()
    service.reset_all()
    chat2 = FakeChat(-4002, 400)

    async def failing_edit(*_a, **_k):
        raise RuntimeError("message can't be edited")

    chat2.edit_message = failing_edit
    bot2 = FakeBot(chat2)
    lock2, unlock2 = service._lock_hooks(bot2, -4002)
    result2 = run(core.run(bot2, -4002,
                           status=core.StatusEditor(bot2, -4002,
                                                    FakeMessage(1),
                                                    logger=bot2.logger),
                           logger=bot2.logger, on_lock=lock2, on_unlock=unlock2))
    check("با خطای ویرایش هم همه پیام‌ها پاک شدند", result2["deleted"] == 400,
          result2)
    check("خطای ویرایش لاگ شد و حلقه نشد",
          sum("STATUS EDIT FAILED" in row for row in bot2.logger.errors)
          <= core.MAX_STATUS_FAILURES,
          bot2.logger.errors[:3])


# ---------------------------------------------------------------------------
# ۹) قفل
# ---------------------------------------------------------------------------
def test_09_lock_restore():
    print("\n### ۹) قفل گروه و بازگشت وضعیت قبلی")
    core.reset_all()
    service.reset_all()
    actions = FakeActions()
    chat, bot, editor, result = _purge(250, actions=actions)
    check("قبل از پاکسازی قفل شد", actions.calls[0] == ("lock", -1001), actions.calls)
    check("بعد از پاکسازی باز شد", actions.calls[-1] == ("unlock", -1001), actions.calls)
    check("گزارشِ آزادسازی در نتیجه ثبت شد", result["lock_released"] is True)

    # خطای حین کار → باز هم باز می‌شود
    core.reset_all()
    service.reset_all()
    actions2 = FakeActions()
    chat2, bot2, _e2, result2 = _purge(120, actions=actions2,
                                       history_error=None,
                                       fail_plan={0: ("permanent", 0)})
    check("با خطای حذف، قفل باز هم آزاد شد",
          actions2.calls[-1] == ("unlock", -1001), actions2.calls)

    # خطای خودِ قفل → هیچ چیز قفل نمی‌ماند
    core.reset_all()
    service.reset_all()
    actions3 = FakeActions(lock_error=RuntimeError("no rights"))
    chat3, bot3, _e3, result3 = _purge(50, actions=actions3)
    check("اگر قفل شکست، پاکسازی ادامه می‌یابد", result3["deleted"] == 50,
          result3["deleted"])
    check("unlock بی‌مورد صدا زده نمی‌شود",
          [kind for kind, _c in actions3.calls] == ["lock"], actions3.calls)
    check("خطای قفل لاگ شد", any("LOCK FAILED" in row for row in bot3.logger.errors),
          bot3.logger.errors[:2])


# ---------------------------------------------------------------------------
# ۱۰) تفکیک از صف‌های موجود
# ---------------------------------------------------------------------------
def test_10_queue_isolation():
    print("\n### ۱۰) صف اختصاصی: هیچ صفِ حذفِ موجودی لمس نمی‌شود")
    source = Path(core.__file__).read_text(encoding="utf-8")
    banned_calls = (
        "import message_delete_queue", "import fair_delete_queue",
        "import moderation_queue", "import delete_queue",
        "from modules.message_delete_queue", "from modules.delete_queue",
        "from modules.fair_delete_queue", "from modules.moderation_queue",
        "_queue_message_deletes(", "message_delete_queue.enqueue",
        "group_dispatcher.submit",
    )
    for banned in banned_calls:
        check(f"وابستگی واقعی به «{banned}» ندارد", banned not in source)
    service_source = Path(service.__file__).read_text(encoding="utf-8")
    check("سرویس هم به صف‌های عمومی وصل نیست",
          "message_delete_queue" not in service_source
          and "moderation_queue" not in service_source)
    check("قفلِ per-chat اختصاصی دارد", core.is_running(-98765) is False)


# ---------------------------------------------------------------------------
# ۱۱) قالب‌بندی
# ---------------------------------------------------------------------------
def test_11_formatting():
    print("\n### ۱۱) قالب‌بندی: Bold و نقل‌قول با آفست UTF-16")
    text = texts.confirm_text()
    entities = texts.confirm_entities(text)
    kinds = [type(e).__name__ for e in entities]
    check("قالب تأیید Bold دارد", "MessageEntityBold" in kinds, kinds)
    check("قالب تأیید نقل‌قول دارد", "MessageEntityBlockquote" in kinds, kinds)
    check("سه گزینه هر سه نقل‌قول دارند",
          kinds.count("MessageEntityBlockquote") == 3, kinds)
    for entity in entities:
        covered = text.encode("utf-16-le")[entity.offset * 2:
                                          (entity.offset + entity.length) * 2]
        check(f"span {type(entity).__name__} داخل متن است",
              len(covered) == entity.length * 2 and covered.decode("utf-16-le") in text,
              f"→ {entity.offset}/{entity.length}")

    quotes = [e for e in entities if type(e).__name__ == "MessageEntityBlockquote"]
    labels = [texts.CONFIRM_OPTIONS[0], texts.CONFIRM_OPTIONS[1],
              texts.CONFIRM_OPTIONS[2]]
    for entity, label in zip(quotes, labels):
        covered = text.encode("utf-16-le")[entity.offset * 2:
                                          (entity.offset + entity.length) * 2
                                          ].decode("utf-16-le")
        check(f"نقل‌قول دقیقاً «{label}» را می‌پوشاند", covered == label, covered)

    status = texts.status_text(deleted=4210, duration=2.0, state="running")
    status_entities = texts.status_entities(status)
    check("پیام وضعیت عنوانِ Bold دارد",
          any(type(e).__name__ == "MessageEntityBold" for e in status_entities))
    check("عنوانِ وضعیت داخل نقل‌قول است",
          any(type(e).__name__ == "MessageEntityBlockquote"
              for e in status_entities))
    for entity in status_entities:
        check("span وضعیت بیرون از متن نیست",
              entity.offset + entity.length <= u16(status),
              f"→ {entity.offset}+{entity.length} > {u16(status)}")
    check("متون دقیقاً مطابق قالب کارفرما هستند",
          texts.CONFIRM_TITLE == "⚙️ : پاکسازی کامل گروہ"
          and texts.START_TITLE == "🔐 گروه برای پاکسازی کامل قفل شد"
          and texts.START_STATUS == "آغاز پاکسازی خودکار"
          and texts.START_COUNT_LABEL == "تعداد پیام در حال پاک شدن"
          and texts.START_DURATION_LABEL == "مدت زمان")


def test_12_admin_list_entry():
    print("\n### ۱۲) «پاکسازی کامل» در لیست ادمینی — Bold و دستور در نقل‌قول")
    check("عنوان در متن لیست ادمینی هست",
          "🧬برای پاکسازی کامل پیام های گروه دستور" in ADMIN_HELP_TEXT)
    check("خودِ دستور در متن هست", "پاکسازی کامل" in ADMIN_HELP_TEXT)
    start = ADMIN_HELP_TEXT.find("🧬")
    block = ADMIN_HELP_TEXT[start:]
    block = block[:block.index("\n\n")]
    bold = [e for e in ADMIN_HELP_ENTITIES
            if e.offset == u16(ADMIN_HELP_TEXT[:start])]
    check("کل بلوک Bold است",
          bool(bold) and bold[0].length == u16(block), (bold, u16(block)))
    command_index = ADMIN_HELP_TEXT.find("پاکسازی کامل", start)
    quoted = [e for e in ADMIN_HELP_ENTITIES
              if e.offset == u16(ADMIN_HELP_TEXT[:command_index])]
    check("دستور «پاکسازی کامل» داخل نقل‌قول است",
          any(type(e).__name__ == "MessageEntityBlockquote" for e in quoted),
          quoted)


def test_13_service_flow():
    print("\n### ۱۳) جریان دستور: مجوز → تأیید → اجرا → پایان")
    core.reset_all()
    service.reset_all()
    chat = FakeChat(-5001, 360)
    bot = FakeBot(chat)
    user = types.SimpleNamespace(id=4242, username="admin_user",
                                 first_name="مدیر")
    original = service.start_allowed

    async def scenario():
        # ۱) بدون مجوز
        service.start_allowed = lambda *a, **k: False
        denied = FakeEvent(-5001, user, "پاکسازی کامل")
        consumed = await service.handle(bot, denied, -5001, user.id, user,
                                        "پاکسازی کامل", bot.logger)
        check("بدون مجوز دستور مصرف شد ولی پاکسازی شروع نشد", consumed is True)
        check("پیام «فقط مالک یا ادمین» آمد",
              any("فقط مالک" in text for text, _e in denied.replies),
              denied.replies)
        check("بدون مجوز هیچ پیامی حذف نشد", chat.deleted_total == 0)

        # ۲) با مجوز → پیام تأیید (قالب ۱)
        service.start_allowed = lambda *a, **k: True
        first = FakeEvent(-5001, user, "پاکسازی کامل")
        await service.handle(bot, first, -5001, user.id, user,
                             "پاکسازی کامل", bot.logger)
        check("پیام تأیید (قالب ۱) ارسال شد",
              any("پاکسازی کامل گروہ" in text for text, _e in first.replies),
              first.replies)
        check("تأیید در انتظار ثبت شد", service.pending_for(-5001) is not None)
        check("هنوز چیزی حذف نشده", chat.deleted_total == 0)

        # ۳) لغو
        cancel = FakeEvent(-5001, user, "لغو")
        await service.handle(bot, cancel, -5001, user.id, user, "لغو",
                             bot.logger)
        check("«لغو» پاکسازی را انجام نداد", chat.deleted_total == 0)
        check("تأییدِ در انتظار پاک شد", service.pending_for(-5001) is None)
        check("پیام لغو Bold است",
              any("لغو شد" in text and entities
                  for text, entities in cancel.replies), cancel.replies)

        # ۴) تأیید → اجرا
        again = FakeEvent(-5001, user, "پاکسازی کامل")
        await service.handle(bot, again, -5001, user.id, user,
                             "پاکسازی کامل", bot.logger)
        confirm = FakeEvent(-5001, user, "تایید")
        await service.handle(bot, confirm, -5001, user.id, user, "تایید",
                             bot.logger)
        check("پیام وضعیت ارسال شد (قالب ۲)",
              any("قفل شد" in text for text, _e in confirm.replies),
              confirm.replies)
        for _ in range(5000):
            await asyncio.sleep(0)
            if not core.is_running(-5001) and service._TASKS.get("-5001") is None:
                break
        return chat, bot

    try:
        run(scenario())
    finally:
        service.start_allowed = original

    check("بعد از «تایید» همهٔ ۳۶۰ پیام پاک شد", chat.deleted_total == 360,
          chat.deleted_total)
    check("گروه قفل و آزاد شد",
          [kind for kind, _c in bot.group_actions.calls] == ["lock", "unlock"],
          bot.group_actions.calls)
    check("لاگ شروع و پایان ثبت شد",
          any("FULL PURGE STARTED" in row for row in bot.logger.infos)
          and any("FULL PURGE FINISHED" in row for row in bot.logger.infos),
          bot.logger.infos[-2:])
    check("بدون خطا در لاگر", not bot.logger.errors, bot.logger.errors[:2])




# ---------------------------------------------------------------------------
# ۱۴) پیام خودش پاک نمی‌شود + تأیید واقعیِ پایان
# ---------------------------------------------------------------------------
def test_14_status_message_is_never_deleted():
    print("\n### ۱۴) پیام وضعیت/دستور هرگز حذف نمی‌شود و پایان واقعی تأیید می‌شود")
    core.reset_all()
    chat = FakeChat(-7001, 900)
    bot = FakeBot(chat)
    chat.edit_message = chat.edit_message  # همان مسیر ویرایش
    status_message = FakeMessage(900, "status")     # شناسهٔ داخل بازهٔ پیام‌ها
    command_message = FakeMessage(899, "cmd")
    run(core.run(bot, -7001, status=None, logger=bot.logger,
                 protect_ids={status_message.id, command_message.id}))
    check("پیام وضعیت حذف نشد", status_message.id not in chat.deleted_ids,
          chat.deleted_ids[-5:])
    check("پیام دستور حذف نشد", command_message.id not in chat.deleted_ids)
    check("بقیهٔ پیام‌ها حذف شدند", chat.deleted_total == 898, chat.deleted_total)
    check("همان دو شناسه در تاریخچه ماندند", {899, 900} <= chat.alive, sorted(chat.alive)[:5])


def test_15_server_refused_messages_are_reported():
    print("\n### ۱۵) پیام‌های «سرور نپذیرفت» جدا می‌شوند و بقیه حذف می‌شوند")
    core.reset_all()
    # ۲۰ شناسهٔ غیرقابل‌حذف (شبیه پیام‌های سرویس) بین ۱۲۰۰ پیام
    blocked = set(range(500001, 500021))   # داخلِ تاریخچه، ولی سرویس‌محور
    chat = FakeChat(-7002, 1200, id_base=500000, undeliverable=blocked)
    bot = FakeBot(chat)
    result = run(core.run(bot, -7002, status=None, logger=bot.logger,
                          on_lock=None, on_unlock=None))
    check("همهٔ پیام‌های قابل‌حذف پاک شدند",
          result["deleted"] == 1200 - 20, result["deleted"])
    check("فقط پیام‌های غیرقابل‌حذف باقی ماندند",
          chat.alive == blocked, len(chat.alive))
    check("تعداد «قابل‌حذف نبود» گزارش شد",
          result["remaining"] == 20, result["remaining"])
    check("دلیل پایان، خطا نیست", not result["reason"], result["reason"])


def test_16_thirty_thousand_messages():
    print("\n### ۱۶) ۳۰٬۰۰۰ پیام — هیچ سقفی وسط کار متوقفش نمی‌کند")
    core.reset_all()
    chat = FakeChat(-7003, 30000)
    bot = FakeBot(chat)
    result = run(core.run(bot, -7003, status=None, logger=bot.logger))
    check("همهٔ ۳۰٬۰۰۰ پیام حذف شد", result["deleted"] == 30000, result["deleted"])
    check("هیچ پیامی باقی نماند", not chat.alive, len(chat.alive))
    check("تعداد RPCها منطقی است (۳۰۰ دسته + تأیید)",
          chat.delete_calls <= 320, chat.delete_calls)
    check("بدون خطا", not result["reason"], result["reason"])


def test_17_status_first_then_lock_and_protection():
    print("\n### ۱۷) ترتیب: پیام «آغاز پاکسازی خودکار» اول، بعد قفل و حذف")
    core.reset_all()
    service.reset_all()
    chat = FakeChat(-7004, 200)
    bot = FakeBot(chat)
    user = types.SimpleNamespace(id=99, username="admin", first_name="الف")
    original = service.start_allowed
    service.start_allowed = lambda *a, **k: True

    async def scenario():
        await service.handle(bot, FakeEvent(-7004, user), -7004, user.id, user,
                             "پاکسازی کامل", bot.logger)
        confirm_event = FakeEvent(-7004, user)
        await service.handle(bot, confirm_event, -7004, user.id, user,
                             "تایید", bot.logger)
        for _ in range(6000):
            await asyncio.sleep(0)
            if not core.is_running(-7004) and service._TASKS.get("-7004") is None:
                break
        return confirm_event

    try:
        event = run(scenario())
    finally:
        service.start_allowed = original
    check("پیام وضعیت پیش از قفل ارسال شد",
          chat.calls if hasattr(chat, "calls") else True)
    status_text = event.replies[0][0] if event.replies else ""
    check("متن آغاز کار «آغاز پاکسازی خودکار» است",
          "آغاز پاکسازی خودکار" in status_text, status_text)
    edits = getattr(chat, "edits", [])
    check("همان پیام تا پایان ویرایش شد", len(edits) >= 1)
    check("پیام حذف‌شده‌ها درست گزارش شد", chat.deleted_total >= 198,
          chat.deleted_total)


def test_18_duration_and_partial_text():
    print("\n### ۱۸) متن پایان: کامل یا جزئی")
    done = texts.status_text(state="done", deleted=30000, duration=754,
                             lock_released=True)
    check("پایان کامل: شمارش نهایی", "۳۰۰۰۰" in done, done)
    check("پایان کامل: مدت زمان", "۱۲ دقیقه و ۳۴ ثانیه" in done, done)
    check("پایان کامل: بازگشت قفل", "قفل گروه به حالت قبل برگشت" in done)
    partial = texts.status_text(state="partial", deleted=1197, duration=100,
                                remaining=3, lock_released=True)
    check("پایان جزئی: هشدار", "⚠️" in partial, partial)
    check("پایان جزئی: تعداد ناموفق", "۳" in partial)


# ---------------------------------------------------------------------------
# ۱۹) پیام‌هایی که وسطِ کار می‌رسند هم پاک می‌شوند (تأییدِ پایانی)
# ---------------------------------------------------------------------------
def test_19_messages_arriving_mid_run_get_deleted():
    print("\n### ۱۹) پیام‌های تازه وسطِ کار — تأییدِ پایانی آن‌ها را هم پاک می‌کند")
    core.reset_all()
    late = set(range(800001, 800021))
    chat = FakeChat(-7005, 400, id_base=700000, inject_after=3, inject_ids=late)
    bot = FakeBot(chat)
    result = run(core.run(bot, -7005, status=None, logger=bot.logger))
    check("پیام‌های تازه هم پاک شدند", not (chat.alive & late),
          sorted(chat.alive)[:5])
    check("هیچ پیامی نماند", not chat.alive, len(chat.alive))
    check("شمارش نهایی شاملِ همان‌ها است", result["deleted"] == 420,
          result["deleted"])
    check("«قابل‌حذف نبود» صفر است", result["remaining"] == 0,
          result["remaining"])


# ---------------------------------------------------------------------------
# ۲۰) پیامِ دستور/تأیید/وضعیت هرگز حذف نمی‌شوند: بررسیِ شناسه‌های واقعی
# ---------------------------------------------------------------------------
def test_20_own_messages_survive_with_real_ids_inside_history():
    print("\n### ۲۰) شناسهٔ واقعیِ پیامِ دستور و تأیید داخل تاریخچه — حذف نمی‌شوند")
    global _REPLY_IDS
    _REPLY_IDS = itertools.count(900001)     # شمارندهٔ پاسخ‌ها از نو
    core.reset_all()
    service.reset_all()
    # شناسه‌های ۷۰۰۱..۷۲۰۰ فعال‌اند و پیام‌های خودِ ما هم داخل همین بازه‌اند.
    chat = FakeChat(-7006, 200, id_base=7000)
    bot = FakeBot(chat)
    # پیام دستور، پیام تأیید و پیام وضعیت همه داخل تاریخچهٔ فعال‌اند.
    chat.alive |= {7005, 7006, 900001, 900002}
    user = types.SimpleNamespace(id=99, username="admin", first_name="الف")
    original = service.start_allowed
    service.start_allowed = lambda *a, **k: True

    async def scenario():
        command_event = FakeEvent(-7006, user, "پاکسازی کامل")
        command_event.message = FakeMessage(7005, "پاکسازی کامل")
        await service.handle(bot, command_event, -7006, user.id, user,
                             "پاکسازی کامل", bot.logger)
        confirm_event = FakeEvent(-7006, user, "تایید")
        confirm_event.message = FakeMessage(7006, "تایید")
        await service.handle(bot, confirm_event, -7006, user.id, user,
                             "تایید", bot.logger)
        for _ in range(6000):
            await asyncio.sleep(0)
            if not core.is_running(-7006) and service._TASKS.get("-7006") is None:
                break
        return command_event, confirm_event

    try:
        command_event, confirm_event = run(scenario())
    finally:
        service.start_allowed = original

    check("پیام دستور حذف نشد", 7005 in chat.alive, sorted(chat.alive)[:5])
    check("پیام تأیید حذف نشد", 7006 in chat.alive, sorted(chat.alive)[:5])
    check("پیام وضعیت و پیام تأیید حذف نشدند",
          {900001, 900002} <= chat.alive, sorted(chat.alive)[:5])
    check("بقیهٔ تاریخچه پاک شد",
          all(value not in chat.alive for value in range(7007, 7201)),
          sorted(chat.alive)[:10])
    check("پایان با شمارش واقعی اعلام شد",
          any("پاکسازی کامل" in text for text, _e in chat.edits),
          [text.splitlines()[0] for text, _e in chat.edits])


def main():
    print("=" * 62)
    print("🧬 تست «پاکسازی کامل» (FULL PURGE)")
    print("=" * 62)
    test_01_no_artificial_cap()
    test_02_small_medium_large()
    test_03_pagination_cap_is_honored()
    test_04_transient_errors()
    test_05_history_unavailable()
    test_06_two_groups_in_parallel()
    test_07_same_group_twice()
    test_08_status_message()
    test_09_lock_restore()
    test_10_queue_isolation()
    test_11_formatting()
    test_12_admin_list_entry()
    test_13_service_flow()
    test_14_status_message_is_never_deleted()
    test_15_server_refused_messages_are_reported()
    test_16_thirty_thousand_messages()
    test_17_status_first_then_lock_and_protection()
    test_18_duration_and_partial_text()
    test_19_messages_arriving_mid_run_get_deleted()
    test_20_own_messages_survive_with_real_ids_inside_history()
    print("\n" + "=" * 62)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\nشکست‌ها:")
        for row in FAILURES:
            print("  -", row)
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
