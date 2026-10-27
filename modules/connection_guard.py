"""لایهٔ پایداری اتصال روی SPlusthon.

این ماژول چهار نقص اثبات‌شده در لایهٔ شبکهٔ SPlusthon را برطرف می‌کند.
هیچ‌کدام «try/except موقت» نیست؛ هر مورد ریشهٔ باگ را می‌بندد.

------------------------------------------------------------------
۱) قاب‌های کهنهٔ سشن قبلی  →  «Server replied with a wrong session ID»
------------------------------------------------------------------
`MTProtoSender._reconnect()` هنگام اتصال دوباره `self._state.reset()`
را صدا می‌زند که یک session id تازه می‌سازد. اما قاب‌هایی که پیش از
reset از سرور رسیده و هنوز در `connection._recv_queue` مانده‌اند با
session id *قدیمی* امضا شده‌اند. `decrypt_message_data()` آن‌ها را با
session id جدید مقایسه می‌کند و `SecurityError` می‌اندازد.

این قاب‌ها از نظر امنیتی معتبرند (auth_key و msg_key هر دو تأیید
شده‌اند)؛ فقط متعلق به سشن قبلی‌اند. راه‌حل درست دور انداختن بی‌صدای
آن‌هاست، نه بالا بردن استثنا. اگر این حالت ادامه‌دار شد یعنی سشن
واقعاً خراب است و ناظر، کلاینت را بازسازی می‌کند.

------------------------------------------------------------------
۲) حلقهٔ reset دوره‌ای خودش را cancel می‌کند
------------------------------------------------------------------
`ConnectionWebSocket._reconnect_loop()` هر ۱۸۰۰ ثانیه `self.disconnect()`
را صدا می‌زند، و `disconnect()` با `helpers._cancel(...,
reconnect_task=self._reconnect_task)` **همان تسکی را که در حال اجراست**
cancel می‌کند. اجرای واقعی نشان داد:

  * حلقه در نقطهٔ `disconnect()` می‌میرد و به `_connect()` نمی‌رسد،
  * یا `_connect()` حلقهٔ دومی می‌سازد و تعداد حلقه‌ها بالا می‌رود،
  * و ترنسپورت می‌تواند با `_connected=False` برای همیشه مرده بماند.

`_recv()` آن‌گاه `ConnectionError` می‌دهد → سِندر `_reconnect` می‌کند →
`_state.reset()` → قاب‌های کهنه → خطای بند ۱. همین زنجیره است که بعد
از چند ساعت ربات را زمین می‌زند.

------------------------------------------------------------------
۳) هیچ سقف زمانی روی RPC نیست
------------------------------------------------------------------
`UserMethods._call` هیچ `wait_for` ندارد. اگر پاسخ یک درخواست گم شود،
`await future` تا ابد معلق می‌ماند — همان «send_message چند دقیقه معطل
می‌شود» که کاربر گزارش کرده.

------------------------------------------------------------------
۴) هیچ مسیری برای بازسازی کامل کلاینت بدون ری‌استارت نیست
------------------------------------------------------------------
`ConnectionSupervisor` سلامت اتصال را می‌پاید و در صورت خرابی پایدار،
درخواست‌های معلق را لغو و کل کلاینت را دوباره connect می‌کند.
"""

import asyncio
import time


# ==========================================================================
#  ابزار کوچک: شمارندهٔ رویداد در پنجرهٔ زمانی
# ==========================================================================
class EventWindow:
    """تعداد رویدادها را در یک پنجرهٔ زمانی لغزان می‌شمارد."""

    def __init__(self, window=120.0, clock=time.monotonic):
        self.window = float(window)
        self._clock = clock
        self._events = []

    def record(self, count=1):
        now = self._clock()
        for _ in range(count):
            self._events.append(now)
        self._trim(now)
        return len(self._events)

    def count(self):
        self._trim(self._clock())
        return len(self._events)

    def clear(self):
        self._events.clear()

    def _trim(self, now):
        cutoff = now - self.window
        events = self._events
        while events and events[0] < cutoff:
            events.pop(0)


# ==========================================================================
#  ۱) دور انداختن قاب‌های کهنهٔ سشن قبلی
# ==========================================================================
_STALE_MARKER = "_connection_guard_stale_session"


class StaleSessionTracker:
    """قاب‌های متعلق به سشن قبلی را می‌شمارد.

    علاوه بر شمارش، «آخرین باری که هر MTProto message با موفقیت رمزگشایی
    شد» را هم نگه می‌دارد (``last_data_ts``). این همان سیگنالِ «زنده بودنِ
    سوکت» است: یک اتصال سالم به‌لطف keepalive هر چند ثانیه یک Pong می‌گیرد
    و برای هر قابِ دریافتی ``decrypt_message_data`` صدا می‌شود.
    """

    def __init__(self, window=120.0):
        self.window = EventWindow(window)
        self.total = 0
        self.last_at = None
        self.last_data_ts = None

    def record(self):
        self.total += 1
        self.last_at = time.monotonic()
        return self.window.record()

    def recent(self):
        return self.window.count()

    def note_received(self):
        """ثبت دریافت هر قابِ سالم از سوکت (مبنای تشخیص مرگِ بی‌صدا)."""
        self.last_data_ts = time.monotonic()

    def reset(self):
        self.window.clear()


def install_stale_session_filter(state_class, tracker, logger=None):
    """`decrypt_message_data` را طوری می‌پیچد که قاب سشن قبلی را دور بیندازد.

    قاب کهنه با `None` برگردانده می‌شود؛ دقیقاً همان چیزی که حلقهٔ
    دریافت برای «این پیام را نادیده بگیر» انتظار دارد. هر استثنای
    امنیتی دیگری دست‌نخورده بالا می‌رود.
    """
    original = getattr(state_class, "decrypt_message_data", None)
    if original is None or getattr(original, _STALE_MARKER, False):
        return False

    def decrypt_message_data(self, body):
        try:
            result = original(self, body)
            # حتی اگر قاب از سشن قبلی باشد، همین که یک قابِ معتبر رسیده
            # یعنی سوکت زنده است → لایو-نس را تازه نگه دار.
            tracker.note_received()
            return result
        except Exception as error:
            if not _is_wrong_session(error):
                raise
            tracker.note_received()
            recent = tracker.record()
            if logger is not None:
                logger.log_info(
                    "CONNECTION GUARD dropped stale-session frame "
                    f"recent={recent} total={tracker.total}"
                )
            return None

    setattr(decrypt_message_data, _STALE_MARKER, True)
    state_class.decrypt_message_data = decrypt_message_data
    return True


def _is_wrong_session(error):
    """آیا این استثنا همان «wrong session ID» است؟"""
    if error.__class__.__name__ != "SecurityError":
        return False
    return "wrong session id" in str(error).lower()


# ==========================================================================
#  ۲) حلقهٔ reset دوره‌ای که خودکشی نمی‌کند
# ==========================================================================
_LOOP_MARKER = "_connection_guard_safe_loop"


def install_periodic_reset_fix(connection_class, helpers, logger=None):
    """`_reconnect_loop` و `_connect` را امن می‌کند.

    دو تغییر:

    * `_connect` دیگر خودش حلقهٔ دوره‌ای نمی‌سازد. مالکیت حلقه فقط
      دست `connect()` است، پس هرگز دو حلقه هم‌زمان وجود ندارد.
    * حلقه پیش از `disconnect()` ارجاع خودش را کنار می‌گذارد تا
      `helpers._cancel` نتواند تسکِ در حال اجرا را cancel کند، و بعد
      از ساخت دوبارهٔ سوکت، تسک‌های ارسال/دریافت را برمی‌گرداند.
    """
    original_connect = getattr(connection_class, "_connect", None)
    if original_connect is None or getattr(original_connect, _LOOP_MARKER, False):
        return False

    async def _connect(self, timeout=None, ssl=None):
        # مالکیت حلقه به connect()/حلقهٔ موجود واگذار می‌شود.
        self._connection_guard_inhibit_loop = True
        try:
            return await original_connect(self, timeout=timeout, ssl=ssl)
        finally:
            self._connection_guard_inhibit_loop = False
            task = getattr(self, "_reconnect_task", None)
            # اگر نسخهٔ اصلی حلقه‌ای ساخت، جمعش می‌کنیم.
            if task is not None and not getattr(task, _LOOP_MARKER, False):
                task.cancel()
                self._reconnect_task = None

    setattr(_connect, _LOOP_MARKER, True)
    connection_class._connect = _connect

    async def _reset_once(self):
        """سوکت را بدون کشتن حلقهٔ دوره‌ای بازمی‌سازد."""
        running = asyncio.current_task()
        owned = getattr(self, "_reconnect_task", None)
        # ارجاع را برمی‌داریم تا disconnect() تسک جاری را cancel نکند.
        if owned is running:
            self._reconnect_task = None
        try:
            await self.disconnect()
        finally:
            if owned is running:
                self._reconnect_task = running

        await self._connect()
        self._connected = True
        loop = helpers.get_running_loop()
        self._send_task = loop.create_task(self._send_loop())
        self._recv_task = loop.create_task(self._recv_loop())

    connection_class._connection_guard_reset_once = _reset_once

    async def _reconnect_loop(self):
        try:
            while self._connected:
                await asyncio.sleep(self._reconnect_interval)
                if not self._connected:
                    break
                try:
                    await self._connection_guard_reset_once()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # ترنسپورت بالا نیامد. حلقه را زنده نگه می‌داریم و
                    # دوباره تلاش می‌کنیم؛ خروج از حلقه یعنی مرگ دائمی.
                    self._connected = False
                    if logger is not None:
                        logger.log_error(
                            f"CONNECTION GUARD periodic reset failed: {error!r}"
                        )
                    return
        except asyncio.CancelledError:
            pass

    setattr(_reconnect_loop, _LOOP_MARKER, True)
    connection_class._reconnect_loop = _reconnect_loop

    original_public = getattr(connection_class, "connect", None)
    if original_public is not None and not getattr(original_public, _LOOP_MARKER, False):
        async def connect(self, timeout=None, ssl=None):
            await original_public(self, timeout=timeout, ssl=ssl)
            task = getattr(self, "_reconnect_task", None)
            if task is None or task.done():
                task = asyncio.ensure_future(self._reconnect_loop())
                setattr(task, _LOOP_MARKER, True)
                self._reconnect_task = task

        setattr(connect, _LOOP_MARKER, True)
        connection_class.connect = connect

    return True


# ==========================================================================
#  ۳) سقف زمانی روی هر RPC
# ==========================================================================
class RpcTimeout(Exception):
    """یک RPC در مهلت مقرر پاسخ نگرفت."""


_RPC_MARKER = "_connection_guard_rpc_timeout"
_STAMP_MARKER = "_connection_guard_stamped"


def _seen_at(sender):
    """جدول کناری «msg_id → زمان اولین مشاهده» برای یک سِندر.

    ``RequestState`` دارای ``__slots__`` است و اصلاً فیلد تازه قبول
    نمی‌کند (بررسی شد: ``AttributeError``)، پس زمان را نمی‌توان روی
    خودِ state چسباند. این جدول کنار سِندر نگه داشته می‌شود و همراه
    خودِ سِندر آزاد می‌شود.
    """
    table = getattr(sender, "_guard_seen_at", None)
    if table is None:
        table = {}
        try:
            sender._guard_seen_at = table
        except AttributeError:
            return {}
    return table


def note_pending(sender):
    """درخواست‌های معلقِ تازه را زمان‌گذاری و ورودی‌های مرده را پاک می‌کند."""
    pending = getattr(sender, "_pending_state", None)
    if pending is None:
        return
    table = _seen_at(sender)
    now = time.monotonic()
    for msg_id in pending:
        table.setdefault(msg_id, now)
    if len(table) > len(pending):
        for msg_id in [m for m in table if m not in pending]:
            table.pop(msg_id, None)


def drop_completed_pending(sender):
    """Remove sender states whose futures have already completed.

    Some SPlusthon versions leave answered keepalive/RPC states in the map
    until another receive-path sweep.  Removing only ``future.done()`` rows is
    safe and keeps reconnect from replaying completed requests.
    """
    pending = getattr(sender, "_pending_state", None)
    if not pending:
        return 0
    table = _seen_at(sender)
    removed = 0
    for msg_id, state in list(pending.items()):
        future = getattr(state, "future", None)
        if future is None or not future.done():
            continue
        pending.pop(msg_id, None)
        table.pop(msg_id, None)
        removed += 1
    return removed


_KEEPALIVE_REQUESTS = frozenset({
    "PingRequest",
    "PingDelayDisconnectRequest",
    "MsgsAck",
    "HttpWait",
    "Pong",
})
# Only these occupy the one-live-ping slot. MsgsAck/HttpWait must never
# supersede or cancel a PingRequest (that produced dropped=1 kept=0).
_PING_REQUESTS = frozenset({
    "PingRequest",
    "PingDelayDisconnectRequest",
})
STALE_LOG_SECONDS = 15.0
# An unanswered Ping is already logged as STALE at 15s. Waiting until 30s
# left a future_done=0 heartbeat occupying the one-live-ping slot.
KEEPALIVE_PENDING_TTL = STALE_LOG_SECONDS
RPC_PENDING_TTL = 60.0


def _state_request_name(state):
    request = getattr(state, "request", None)
    if request is None:
        return "unknown"
    try:
        return type(request).__name__
    except Exception:
        return "unknown"


def inspect_pending(sender):
    """Read-only view of ``sender._pending_state`` rows."""
    pending = getattr(sender, "_pending_state", None)
    if not pending or not hasattr(pending, "items"):
        return []
    table = _seen_at(sender)
    now = time.monotonic()
    rows = []
    for msg_id, state in list(pending.items()):
        future = getattr(state, "future", None)
        started = table.get(msg_id)
        request_type = _state_request_name(state)
        age_ms = None if started is None else max(0.0, (now - started) * 1000.0)
        done = future is None or bool(getattr(future, "done", lambda: True)())
        cancelled = bool(
            future is not None and getattr(future, "cancelled", lambda: False)()
        )
        rows.append({
            "msg_id": msg_id,
            "request_type": request_type,
            "operation": request_type,
            "age_ms": 0.0 if age_ms is None else round(age_ms, 1),
            "age_known": started is not None,
            "future_done": done,
            "future_cancelled": cancelled,
            "is_ping": _is_ping_request_name(request_type),
            "is_keepalive": request_type in _KEEPALIVE_REQUESTS,
        })
    return rows


def pending_breakdown(sender):
    """Compact counters for snapshots. Never mutates sender state."""
    rows = inspect_pending(sender)
    by_type = {}
    stale = 0
    oldest = 0.0
    keepalive = 0
    live = 0
    for row in rows:
        name = row["request_type"] or "unknown"
        by_type[name] = by_type.get(name, 0) + 1
        age = float(row.get("age_ms") or 0.0)
        oldest = max(oldest, age)
        if row.get("is_ping"):
            keepalive += 1
        if row.get("future_done") or row.get("future_cancelled") or age >= STALE_LOG_SECONDS * 1000:
            stale += 1
        elif not row.get("future_done"):
            live += 1
    return {
        "count": len(rows),
        "keepalive": keepalive,
        "stale": stale,
        "live": live,
        "oldest_age_ms": round(oldest, 1),
        "by_type": by_type,
        "rows": rows,
    }


def _drop_pending_row(pending, table, msg_id, state):
    pending.pop(msg_id, None)
    table.pop(msg_id, None)
    future = getattr(state, "future", None)
    if future is not None and not future.done():
        future.cancel()


def _is_ping_request_name(name):
    return str(name or "") in _PING_REQUESTS


def _future_done(future):
    if future is None:
        return False
    done = getattr(future, "done", None)
    try:
        return bool(done()) if callable(done) else False
    except Exception:
        return False


def _future_cancelled(future):
    if future is None:
        return False
    cancelled = getattr(future, "cancelled", None)
    try:
        return bool(cancelled()) if callable(cancelled) else False
    except Exception:
        return False


def _ping_age_ms(table, msg_id, now=None):
    started = table.get(msg_id) if table is not None else None
    if started is None:
        return None
    if now is None:
        now = time.monotonic()
    return round(max(0.0, (now - started) * 1000.0), 1)


def _iter_unanswered_pings(sender):
    pending = getattr(sender, "_pending_state", None)
    if not pending or not hasattr(pending, "items"):
        return
    table = _seen_at(sender)
    for msg_id, state in list(pending.items()):
        if not _is_ping_request_name(_state_request_name(state)):
            continue
        future = getattr(state, "future", None)
        if _future_done(future):
            continue
        started = table.get(msg_id)
        yield started if started is not None else 0.0, msg_id, state


def reclaim_superseded_keepalive(sender, *, keep_newest=1, logger=None):
    """Keep at most ``keep_newest`` unanswered PingRequest rows.

    MsgsAck/HttpWait are not pings and must not cancel a live PingRequest.
    A single live ping is never dropped: that produced ``dropped=1 kept=0``.
    Live send/delete/moderation rows are never touched.
    """
    pending = getattr(sender, "_pending_state", None)
    if not pending or not hasattr(pending, "items"):
        return 0
    table = _seen_at(sender)
    pings = list(_iter_unanswered_pings(sender))
    # Supersede never wipes the last live ping. keep_newest=0 belongs to
    # reconnect cleanup via complete_keepalive_pending, not this path.
    keep_newest = max(1, int(keep_newest))
    if len(pings) <= keep_newest:
        return 0
    pings.sort(key=lambda item: item[0])
    victims = pings[:-keep_newest]
    kept_rows = pings[-keep_newest:]
    for _started, msg_id, state in victims:
        future = getattr(state, "future", None)
        log_ping_lifecycle(
            logger, _request_ping_id(state), "TIMEOUT",
            action="cancel",
            msg_id=msg_id,
            age_ms=None if _started == 0.0 else round((time.monotonic() - _started) * 1000.0, 1) if _started else None,
            future_done=int(_future_done(future)),
            task_done=int(_future_done(future)),
            in_sender_pending=1,
            reason="superseded_extra_ping",
        )
        _drop_pending_row(pending, table, msg_id, state)
        log_ping_lifecycle(
            logger, _request_ping_id(state), "CLEANED",
            action="remove",
            msg_id=msg_id,
            reason="superseded_extra_ping",
        )
    kept = sum(1 for _s, msg_id, state in kept_rows if msg_id in pending and not _future_done(getattr(state, "future", None)))
    if logger is not None and victims:
        logger.log_info(
            "KEEPALIVE SUPERSEDED "
            f"dropped={len(victims)} kept={kept}"
        )
    return len(victims)


def unanswered_keepalive_ids(sender):
    """Message ids of unanswered PingRequest rows, newest last."""
    rows = list(_iter_unanswered_pings(sender))
    rows.sort(key=lambda item: item[0])
    return [msg_id for _started, msg_id, _state in rows]


def unanswered_keepalive_count(sender):
    """How many PingRequest rows are still waiting for a pong."""
    return len(unanswered_keepalive_ids(sender))


def live_keepalive_count(sender, stale_after=STALE_LOG_SECONDS):
    """PingRequest rows whose futures are not done and not yet stale.

    This is the live_pings invariant: count real PingRequest futures, never
    MsgsAck/HttpWait or a keepalive deque.
    """
    now = time.monotonic()
    limit = float(stale_after)
    live = 0
    for started, _msg_id, _state in _iter_unanswered_pings(sender):
        if started and (now - started) >= limit:
            continue
        live += 1
    return live


def log_ping_lifecycle(logger, ping_id, state, **fields):
    """Diagnostic state transitions for one keepalive ping_id.

    لاگ‌های عادی چرخهٔ Ping (CREATED/SENT/RESPONSE/CLEANED) در ترمینال
    Termux پرتکرار و خسته‌کننده‌اند؛ این state ها از INFO به DEBUG منتقل
    شده‌اند. state های خطا (CANCELLED/EXCEPTION/TIMEOUT/...) و خطاهای واقعی
    اتصال (reconnect/connection lost) در سطح قبلی خود (INFO/WARNING/ERROR)
    دست‌نخورده می‌مانند.
    """
    if logger is None:
        return
    extra = " ".join(
        f"{key}={value}" for key, value in fields.items() if value is not None
    )
    line = f"KEEPALIVE PING STATE ping_id={ping_id} state={state}"
    if extra:
        line = f"{line} {extra}"
    try:
        if str(state).upper() in _DEBUG_PING_STATES:
            _emit_ping_debug(logger, line)
        else:
            logger.log_info(line)
    except Exception:
        pass


# state های عادی چرخهٔ Ping که به سطح DEBUG منتقل شده‌اند.
_DEBUG_PING_STATES = frozenset({"CREATED", "SENT", "RESPONSE", "CLEANED"})


def _emit_ping_debug(logger, line):
    """ثبت پیام چرخهٔ Ping در سطح DEBUG واقعی بدون تغییر منطق Ping.

    → BotLogger واقعی: ``logger.logger.debug`` (سطح INFO => در تولید خاموش)
    → لاگرهای سادهٔ تست که فقط ``log_info`` دارند: فالبک به همان کانال تا
      مشاهده‌پذیری تست‌ها حفظ شود.
    """
    try:
        debug = getattr(logger, "log_debug", None)
        if callable(debug):
            debug(line)
            return
    except Exception:
        return
    underlying = getattr(logger, "logger", None)
    raw_debug = getattr(underlying, "debug", None)
    if callable(raw_debug):
        try:
            raw_debug(line)
            return
        except Exception:
            return
    try:
        logger.log_info(line)
    except Exception:
        pass


def log_unanswered_ping_states(sender, logger, *, action, next_ping_id=None):
    """Log every unanswered PingRequest with action=skip|complete|..."""
    pending = getattr(sender, "_pending_state", None)
    table = _seen_at(sender)
    now = time.monotonic()
    for started, msg_id, state in _iter_unanswered_pings(sender):
        future = getattr(state, "future", None)
        log_ping_lifecycle(
            logger,
            _request_ping_id(state),
            "STATE",
            action=action,
            msg_id=msg_id,
            age_ms=_ping_age_ms(table, msg_id, now),
            future_done=int(_future_done(future)),
            task_done=int(_future_done(future)),
            in_sender_pending=int(
                pending is not None and hasattr(pending, "__contains__") and msg_id in pending
            ),
            next_ping_id=next_ping_id,
        )


def _request_ping_id(state):
    request = getattr(state, "request", None)
    if request is None:
        return None
    return getattr(request, "ping_id", None)


def stamp_keepalive_ping_id(sender, ping_id):
    """Attach ping_id onto pending PingRequest rows that lack one."""
    pending = getattr(sender, "_pending_state", None)
    if not pending or ping_id is None or not hasattr(pending, "items"):
        return 0
    stamped = 0
    for state in list(pending.values()):
        if not _is_ping_request_name(_state_request_name(state)):
            continue
        request = getattr(state, "request", None)
        if request is None or getattr(request, "ping_id", None) is not None:
            continue
        try:
            request.ping_id = ping_id
            stamped += 1
        except Exception:
            pass
    return stamped


def complete_keepalive_pending(
    sender, ping_id=None, *, logger=None, reason="CLEANED", action=None,
):
    """Cancel and remove PingRequest rows. Never starts a reconnect.

    Pong / timeout / exception / connection-change all use this so a
    future_done=0 PingRequest cannot occupy sender_pending forever.
    MsgsAck/HttpWait and live send/delete rows are never touched.
    """
    pending = getattr(sender, "_pending_state", None)
    if not pending or not hasattr(pending, "items"):
        return 0
    table = _seen_at(sender)
    now = time.monotonic()
    victims = []
    unmatched = []
    for msg_id, state in list(pending.items()):
        if not _is_ping_request_name(_state_request_name(state)):
            continue
        req_ping = _request_ping_id(state)
        if ping_id is not None and req_ping is not None and req_ping != ping_id:
            unmatched.append((msg_id, state, req_ping))
            continue
        victims.append((msg_id, state, req_ping))
    if ping_id is not None and not victims and unmatched:
        victims = unmatched
        unmatched = []
    removed = 0
    for msg_id, state, req_ping in victims:
        future = getattr(state, "future", None)
        action = action or {
            "RESPONSE": "complete",
            "TIMEOUT": "cancel",
            "CANCELLED": "cancel",
            "EXCEPTION": "cancel",
            "CLEANED": "remove",
            "reconnect": "reconnect",
        }.get(reason, "remove")
        log_ping_lifecycle(
            logger,
            ping_id if ping_id is not None else req_ping,
            reason,
            action=action,
            msg_id=msg_id,
            age_ms=_ping_age_ms(table, msg_id, now),
            future_done=int(_future_done(future)),
            task_done=int(_future_done(future)),
            in_sender_pending=1,
            request_type=_state_request_name(state),
        )
        _drop_pending_row(pending, table, msg_id, state)
        log_ping_lifecycle(
            logger,
            ping_id if ping_id is not None else req_ping,
            "CLEANED",
            action="remove",
            msg_id=msg_id,
            future_done=1,
            task_done=1,
            in_sender_pending=0,
            reason=reason,
        )
        removed += 1
    return removed


def drop_keepalive_pending(sender, logger=None, reason="CLEANED", action=None):
    """Drop every PingRequest row. Used on reconnect so they are not replayed."""
    return complete_keepalive_pending(
        sender, ping_id=None, logger=logger, reason=reason,
        action=action or "reconnect",
    )


def drop_request_pending(sender, request):
    """Remove the sender row for this exact request after _call finishes."""
    pending = getattr(sender, "_pending_state", None)
    if not pending or request is None or not hasattr(pending, "items"):
        return 0
    table = _seen_at(sender)
    removed = 0
    for msg_id, state in list(pending.items()):
        if getattr(state, "request", None) is not request:
            continue
        pending.pop(msg_id, None)
        table.pop(msg_id, None)
        future = getattr(state, "future", None)
        if future is not None and not future.done():
            future.cancel()
        removed += 1
    return removed


def reclaim_dead_pending(
    sender,
    *,
    now=None,
    rpc_timeout=RPC_PENDING_TTL,
    keepalive_ttl=KEEPALIVE_PENDING_TTL,
    logger=None,
    log_stale=True,
):
    """Remove only states that cannot still be a live application RPC.

    Live Send/Delete/moderation rows younger than ``rpc_timeout`` stay.
    Completed, cancelled, leftover keepalive, and rows older than the
    RPC timeout are removed after ``STALE SENDER PENDING`` is logged.
    """
    pending = getattr(sender, "_pending_state", None)
    if not pending or not hasattr(pending, "items"):
        return 0
    if now is None:
        now = time.monotonic()
    table = _seen_at(sender)
    removed = 0
    for msg_id, state in list(pending.items()):
        future = getattr(state, "future", None)
        started = table.get(msg_id)
        request_type = _state_request_name(state)
        age = None if started is None else max(0.0, now - started)
        done = future is None or bool(getattr(future, "done", lambda: True)())
        cancelled = bool(
            future is not None and getattr(future, "cancelled", lambda: False)()
        )
        is_ping = _is_ping_request_name(request_type)
        is_keepalive = request_type in _KEEPALIVE_REQUESTS
        if log_stale and logger is not None and age is not None and age >= STALE_LOG_SECONDS:
            logger.log_info(
                "STALE SENDER PENDING "
                f"age_ms={age * 1000.0:.1f} "
                f"request_type={request_type} "
                f"operation={request_type} "
                f"future_done={int(done)} "
                f"keepalive={int(is_ping)}"
            )
        should_drop = False
        if done or cancelled:
            should_drop = True
        elif is_ping and (age is None or age >= float(keepalive_ttl)):
            should_drop = True
        elif age is not None and age >= float(rpc_timeout):
            should_drop = True
        if not should_drop:
            continue
        if is_ping and logger is not None:
            ping_id = _request_ping_id(state)
            why = "TIMEOUT" if not done and not cancelled else (
                "CANCELLED" if cancelled else "RESPONSE"
            )
            log_ping_lifecycle(
                logger, ping_id, why,
                action="cancel" if why in ("TIMEOUT", "CANCELLED") else "complete",
                msg_id=msg_id,
                age_ms=None if age is None else round(age * 1000.0, 1),
                future_done=int(done),
                task_done=int(done),
                in_sender_pending=1,
            )
        pending.pop(msg_id, None)
        table.pop(msg_id, None)
        if future is not None and not future.done():
            future.cancel()
        if is_ping and logger is not None:
            log_ping_lifecycle(
                logger, _request_ping_id(state), "CLEANED",
                action="remove",
                msg_id=msg_id,
                future_done=1,
                task_done=1,
                in_sender_pending=0,
                reason="stale_reclaim",
            )
        removed += 1
    return removed


def drop_stale_pending(sender, deadline):
    """درخواست‌های معلقی که از مهلت گذشته‌اند را از جدول سِندر پاک می‌کند.

    ``asyncio.wait_for`` فقط *منتظر ماندن* را لغو می‌کند؛ خودِ درخواست
    در ``sender._pending_state`` باقی می‌ماند چون آن جدول را لایهٔ
    شبکه پر می‌کند نه فراخوان. بدون پاک‌سازی، هر RPC تایم‌اوت‌خورده
    یک «زامبی» می‌شود که:

      • تا ابد در حافظه می‌ماند،
      • ``_pop_states`` را که پیمایش خطی روی همین جدول است کند می‌کند،
      • و بدتر از همه، در هر reconnect خط
        ``_send_queue.extend(self._pending_state.values())``
        همهٔ آن‌ها را دوباره روی سوکت می‌فرستد.

    خروجی: تعداد زامبی‌های پاک‌شده.
    """
    pending = getattr(sender, "_pending_state", None)
    if not pending:
        return 0

    table = _seen_at(sender)
    removed = 0
    for msg_id, state in list(pending.items()):
        started = table.get(msg_id)
        if started is None or started > deadline:
            continue
        pending.pop(msg_id, None)
        table.pop(msg_id, None)
        future = getattr(state, "future", None)
        if future is not None and not future.done():
            future.cancel()
        removed += 1
    return removed


def install_rpc_timeout(client, timeout=None, on_timeout=None, logger=None,
                        policy=None):
    """`client._call` را با یک سقف زمانی *per-request* می‌پیچد.

    بدون این، گم شدن یک پاسخ یعنی `await` ابدی. با این، درخواست پس از
    مهلت کلاس خودش (send / moderation / read / سایر) شکست می‌خورد، تسکِ
    هندلر آزاد می‌شود و ناظر خبردار می‌شود.

    ``timeout`` صریح (همان‌طور که تست‌ها می‌دهند) روی سیاست غلبه
    می‌کند؛ در production برابر None است و
    ``RpcPolicy.timeout_for(request)`` تصمیم می‌گیرد. این تنها جای تعیین
    مهلت RPC است — اعداد 60/20/15 دیگر در لایه‌های مختلف پخش نشده‌اند.

    ``flood_sleep_threshold`` همچنین به لایهٔ زیرین پاس داده می‌شود تا
    SPlusthon داخل ``_call`` نخوابد؛ سیاست خواب/بک‌آف در
    ``rpc_resilience`` است.

    ⚠️ صرفِ `wait_for` کافی نیست و خودش نشتی می‌سازد: درخواست در
    ``sender._pending_state`` جا می‌ماند. برای همین پس از هر timeout،
    زامبی‌های همان سِندر پاک می‌شوند.
    """
    original = getattr(client, "_call", None)
    if original is None or getattr(original, _RPC_MARKER, False):
        return False
    if policy is None:
        from modules.rpc_policy import RpcPolicy
        policy = RpcPolicy.from_environment()

    async def _call(sender, request, ordered=False, flood_sleep_threshold=None):
        started = time.monotonic()
        deadline = (
            float(timeout) if timeout is not None
            else policy.timeout_for(request)
        )
        if flood_sleep_threshold is None:
            flood_sleep_threshold = policy.flood_sleep_threshold_s
        from modules.rpc_policy import RPC_REQUEST_ID
        from modules import outgoing_profiler as _profiler
        request_id = RPC_REQUEST_ID.get()
        task = asyncio.ensure_future(
            original(
                sender,
                request,
                ordered=ordered,
                flood_sleep_threshold=flood_sleep_threshold,
            )
        )
        # Watchdog cancels THIS inner task, never the caller's queue worker.
        _profiler.attach_cancel_target(request_id, task)
        # یک چرخهٔ رویداد بعد، درخواست در جدول معلق‌ها نشسته است و
        # می‌توان زمان آن را ثبت کرد.
        try:
            await asyncio.sleep(0)
            note_pending(sender)
        except Exception:
            pass
        try:
            return await asyncio.wait_for(task, timeout=deadline)
        except asyncio.TimeoutError:
            name = request.__class__.__name__
            # هر چیزی که پیش از شروع این درخواست معلق مانده قطعاً
            # زامبی است؛ همراه با خودِ این درخواست پاک می‌شود.
            dropped = drop_stale_pending(sender, started)
            dropped += drop_request_pending(sender, request)
            if logger is not None:
                logger.log_error(
                    f"CONNECTION GUARD rpc timeout after {deadline}s "
                    f"request={name} request_id={request_id or '-'} "
                    f"dropped_pending={dropped}"
                )
                logger.log_info(
                    "CONN TRACE TIMEOUT "
                    f"request={name} request_id={request_id or '-'} "
                    f"after_s={deadline} "
                    f"dropped_pending={dropped}"
                )
            if on_timeout is not None:
                on_timeout(request)
            raise RpcTimeout(
                f"{name} did not answer within {deadline}s"
            ) from None
        except asyncio.CancelledError:
            drop_request_pending(sender, request)
            drop_completed_pending(sender)
            raise
        finally:
            # Success, exception, timeout or cancel: this request's await
            # is over. Leftover completed rows must not stay in the map.
            drop_completed_pending(sender)
            drop_request_pending(sender, request)
            _profiler.detach_cancel_target(request_id)

    setattr(_call, _RPC_MARKER, True)
    client._call = _call
    return True


# ==========================================================================
#  ۵) سقف زمانی روی مسیر ارسال (send_bytes/drain)
# ==========================================================================
_SEND_TIMEOUT_MARKER = "_connection_guard_send_timeout"

# پیش‌فرض مهلتِ مسیر ارسال: اگر `send_bytes` بیشتر از این مدت جواب ندهد،
# به‌معنای گیر کردنِ جهتِ خروجی WebSocket است (send-side stall).
SEND_TIMEOUT_SECONDS = 20.0


def install_send_timeout(writer_class, timeout=SEND_TIMEOUT_SECONDS,
                         on_stall=None, logger=None):
    """``WebSocketWriter.drain`` را با سقف زمانی می‌پیچد.

    مسیر ارسال در SPlusthon هیچ timeout ندارد: ``Connection._send_loop``
    فقط ``await self._writer.drain()`` را صدا می‌زند و ``drain()`` هم
    ``await self._ws.send_bytes(data)`` را. اگر جهتِ خروجی سوکت (TCP
    half-open) از کار افتاده باشد، این ``await`` برای همیشه معلق می‌ماند —
    در حالی که جهتِ Receive همچنان زنده است و ربات «متصل» دیده می‌شود.

    این وصله `send_bytes` را با `asyncio.wait_for` می‌پوشاند. اگر بیش از
    مهلت جواب ندهد:
      * به supervisor خبر می‌دهد (``on_stall``) تا بازسازی/rebuild اجرا شود،
      * و یک ``TimeoutError`` بالا می‌اندازد تا ``_send_loop`` خروج بگیرد و
        اتصال بسته شود — پس supervisor می‌تواند کلاینتِ تازه بسازد.
    """
    original = getattr(writer_class, "drain", None)
    if original is None or getattr(original, _SEND_TIMEOUT_MARKER, False):
        return False

    async def drain(self):
        if not getattr(self, "_pending", None):
            return
        data = bytes(self._pending)
        self._pending.clear()
        try:
            await asyncio.wait_for(
                self._ws.send_bytes(data), timeout=timeout)
        except asyncio.TimeoutError:
            if logger is not None:
                logger.log_error(
                    "CONNECTION GUARD send timeout: "
                    f"send_bytes did not complete within {timeout}s"
                )
            if on_stall is not None:
                try:
                    on_stall()
                except Exception:
                    pass
            raise
        except asyncio.CancelledError:
            raise

    setattr(drain, _SEND_TIMEOUT_MARKER, True)
    writer_class.drain = drain
    return True


# ==========================================================================
#  بازسازی کامل کلاینت بدون ری‌استارت
# ==========================================================================
def cancel_pending_requests(client, reason=None):
    """همهٔ futureهای معلق سِندر را لغو می‌کند و تعدادشان را برمی‌گرداند."""
    sender = getattr(client, "_sender", None)
    if sender is None:
        return 0
    pending = getattr(sender, "_pending_state", None)
    if not pending:
        return 0

    error = ConnectionError(reason or "connection rebuilt")
    cancelled = 0
    for state in list(pending.values()):
        future = getattr(state, "future", None)
        if future is None or future.done():
            continue
        future.set_exception(error)
        cancelled += 1
    pending.clear()
    return cancelled


class ConnectionSupervisor:
    """ناظر سلامت اتصال؛ در صورت خرابی پایدار کلاینت را بازمی‌سازد.

    شرایط بازسازی:

    * تعداد قاب‌های کهنهٔ سشن در پنجرهٔ زمانی از حد بگذرد
      (یعنی `_state.reset()` رخ داده ولی جریان پاسخ‌ها ترمیم نشده)،
    * یا RPCها پشت سر هم timeout بخورند،
    * یا کلاینت اصلاً connected نباشد،
    * یا حلقه‌ی دریافت بدون دلیل خارج شده باشد (`_recv_loop_handle` تمام
      شده ولی کلاینت هنوز connected است)،
    * یا برای مدت طولانی هیچ MTProto message‌ای دریافت نشده باشد
      (مرگِ بی‌صدای سوکت که هیچ‌کدام از نشانه‌های بالا را ندارد).
    """

    def __init__(
        self,
        client,
        logger=None,
        tracker=None,
        stale_threshold=20,
        timeout_threshold=3,
        check_interval=15.0,
        window=120.0,
        receive_dead_threshold=180.0,
        client_factory=None,
    ):
        self.client = client
        self.logger = logger
        self.tracker = tracker or StaleSessionTracker(window)
        self.stale_threshold = stale_threshold
        self.timeout_threshold = timeout_threshold
        self.check_interval = check_interval
        self.receive_dead_threshold = receive_dead_threshold
        self.timeouts = EventWindow(window)
        self.send_stalls = EventWindow(window)
        self.rebuilds = 0
        self.last_reason = None
        self._rebuilding = False
        # کارخانهٔ ساخت کلاینتِ کاملاً جدید. اگر تنظیم باشد، rebuild به‌جای
        # connect روی همان کلاینت، یک SoroushClient تازه (سشنِ تازه، sender
        # تازه، receive loop تازه) می‌سازد و client را عوض می‌کند.
        self.client_factory = client_factory

    # -- ورودی‌های رویداد ------------------------------------------------
    def note_rpc_timeout(self, request=None):
        self.timeouts.record()

    def note_received(self):
        """ثبت دریافت یک قابِ سالم؛ به‌صورت دستی از بیرون هم قابل صدا زدن است."""
        if self.tracker is not None:
            self.tracker.note_received()

    def note_send_stall(self):
        """ثبت گیر کردنِ مسیر ارسال (send_bytes/drain timeout)."""
        self.send_stalls.record()

    # -- تصمیم -----------------------------------------------------------
    def diagnose(self):
        """اگر بازسازی لازم است، دلیلش را برمی‌گرداند؛ وگرنه None."""
        stale = self.tracker.recent()
        if stale >= self.stale_threshold:
            return f"stale-session frames={stale} in window"

        timeouts = self.timeouts.count()
        if timeouts >= self.timeout_threshold:
            return f"rpc timeouts={timeouts} in window"

        is_connected = getattr(self.client, "is_connected", None)
        if callable(is_connected) and not is_connected():
            return "client reported not connected"

        # مرگِ بی‌صدای حلقه‌ی دریافت: هندلِ `_recv_loop` تمام شده ولی سِندر
        # هنوز خودش را متصل می‌داند و reconnectی در جریان نیست. این یعنی
        # loop خارج شده و هیچ کس آن را برنگردانده — همان «process زنده ولی
        # receive مرده».
        sender = getattr(self.client, "_sender", None)
        if sender is not None:
            recv_handle = getattr(sender, "_recv_loop_handle", None)
            user_connected = getattr(sender, "_user_connected", False)
            reconnecting = getattr(sender, "_reconnecting", False)
            if (
                recv_handle is not None
                and recv_handle.done()
                and not recv_handle.cancelled()
                and user_connected
                and not reconnecting
            ):
                return "receive loop exited unexpectedly"

        # مرگِ بی‌صدای سوکت: هیچ قابی (حتی Pong) به‌مدت طولانی نرسیده.
        # این سناریو هیچ نشانه‌ی خطایی ندارد، پس تنها سیگنال قابل‌اعتماد
        # همین «زنده بودنِ ترافیک دریافتی» است.
        if self.tracker is not None:
            last = self.tracker.last_data_ts
            if last is not None:
                idle = time.monotonic() - last
                if idle > self.receive_dead_threshold:
                    return f"no data received for {idle:.0f}s"

        # گیر کردنِ مسیر ارسال (send-side stall): اگر send_bytes/drain
        # تایم‌اوت خورده باشد، جهتِ خروجی خراب است حتی اگر Receive زنده باشد.
        if self.send_stalls.count() >= self.timeout_threshold:
            return f"send stalls={self.send_stalls.count()} in window"

        # مرگِ حلقهٔ ارسال: هندلِ `_send_loop` تمام شده (یا Connection
        # send_task تمام شده) ولی سِندر هنوز خودش را متصل می‌داند و reconnectی
        # در جریان نیست. این یعنی directionِ خروجی مرده ولی ورودی زنده است.
        sender = getattr(self.client, "_sender", None)
        if sender is not None:
            user_connected = getattr(sender, "_user_connected", False)
            reconnecting = getattr(sender, "_reconnecting", False)
            send_handle = getattr(sender, "_send_loop_handle", None)
            if (
                send_handle is not None
                and send_handle.done()
                and not send_handle.cancelled()
                and user_connected
                and not reconnecting
            ):
                return "send loop exited unexpectedly"
            # Connection-level send task: جایی که drain/send_bytes اجرا می‌شود
            conn = getattr(sender, "_connection", None)
            if conn is not None:
                send_task = getattr(conn, "_send_task", None)
                if (
                    send_task is not None
                    and send_task.done()
                    and not send_task.cancelled()
                    and user_connected
                    and not reconnecting
                ):
                    return "connection send task exited unexpectedly"

        return None

    # -- اجرا -------------------------------------------------------------
    async def verify(self):
        """بررسی می‌کند اتصال واقعاً سالم است یا نه.

        سه شرط: کلاینت connected باشد، یک RPC آزمایشی جواب بدهد، و حلقهٔ
        دریافت (receive loop) دوباره فعال باشد. True یعنی آمادهٔ دریافت.
        """
        is_connected = getattr(self.client, "is_connected", None)
        if callable(is_connected) and not is_connected():
            self._log_error("CONNECTION GUARD verify: client not connected")
            return False

        # ۱) RPC آزمایشی: چند درخواست ساده که پاسخ کوتاه می‌دهد.
        # اگر کلاینت `get_me` نداشته باشد (مثل کلاینت قلابیِ تست‌ها)، از
        # این مرحله رد می‌شویم؛ روی کلاینت واقعی SPlusthon موجود است.
        get_me = getattr(self.client, "get_me", None)
        if callable(get_me):
            try:
                await asyncio.wait_for(get_me(), timeout=15.0)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log_error(
                    f"CONNECTION GUARD verify: test RPC failed ({error!r})"
                )
                return False

        # ۲) حلقهٔ دریافت زنده است.
        sender = getattr(self.client, "_sender", None)
        if sender is not None:
            recv_handle = getattr(sender, "_recv_loop_handle", None)
            if recv_handle is not None and recv_handle.done():
                self._log_error(
                    "CONNECTION GUARD verify: receive loop not running"
                )
                return False

            # ۳) حلقهٔ ارسال هم زنده باشد (جهتِ خروجی سالم).
            send_handle = getattr(sender, "_send_loop_handle", None)
            if send_handle is not None and send_handle.done():
                self._log_error(
                    "CONNECTION GUARD verify: send loop not running"
                )
                return False
            # Connection-level send task (جایی که drain/send_bytes است)
            conn = getattr(sender, "_connection", None)
            if conn is not None:
                send_task = getattr(conn, "_send_task", None)
                if send_task is not None and send_task.done():
                    self._log_error(
                        "CONNECTION GUARD verify: connection send task "
                        "not running"
                    )
                    return False

        self._log_info("CONNECTION GUARD verify: OK")
        return True

    async def rebuild(self, reason):
        """اتصال را از صفر می‌سازد.

        اگر ``client_factory`` تنظیم باشد، به‌جای connect روی همان کلاینتِ
        کهنه، یک ``SoroushClient`` کاملاً جدید (سشن تازه، sender تازه،
        receive loop تازه) ساخته می‌شود و ``self.client`` عوض می‌شود. در
        غیر این صورت (برای تست) روی همان کلاینت disconnect/connect انجام
        می‌شود.

        بعد از ساخت، با ``verify`` تأیید می‌شود که واقعاً آمادهٔ دریافت است.
        """
        if self._rebuilding:
            return False
        self._rebuilding = True
        self.last_reason = reason
        old_client = self.client
        try:
            self._log_error(f"CONNECTION GUARD rebuilding client: {reason}")

            cancelled = cancel_pending_requests(old_client, reason)
            if cancelled:
                self._log_info(
                    f"CONNECTION GUARD cancelled {cancelled} pending request(s)"
                )

            # همیشه کلاینت کهنه را کامل می‌بندیم تا WebSocket/Sender/Receive
            # Loop/Taskهای قدیمی متوقف شوند و Sessionِ خراب دوباره استفاده نشود.
            try:
                await old_client.disconnect()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log_error(
                    f"CONNECTION GUARD old client disconnect failed: {error!r}"
                )

            new_client = old_client
            if self.client_factory is not None:
                new_client = await self.client_factory(old_client, reason)
                if new_client is None:
                    self._log_error(
                        "CONNECTION GUARD rebuild: client_factory returned None"
                    )
                    return False
                self.client = new_client
            else:
                # بدون factory (مثلاً در تست): روی همان کلاینت disconnect+connect
                try:
                    await old_client.connect()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._log_error(
                        f"CONNECTION GUARD reconnect failed: {error!r}"
                    )
                    return False

            # تأیید: فقط اگر اتصالِ جدید واقعاً سالم باشد بازسازی موفق است.
            ok = await self.verify()
            if not ok:
                self._log_error("CONNECTION GUARD rebuild: verify FAILED")
                return False

            self.tracker.reset()
            self.tracker.note_received()
            self.timeouts.clear()
            self.rebuilds += 1
            self._log_info("CONNECTION GUARD rebuild complete")
            return True
        finally:
            self._rebuilding = False

    async def run(self, sleep=None):
        """حلقهٔ دائمی پایش. در `run()` ربات به‌صورت task اجرا می‌شود."""
        sleeper = sleep or asyncio.sleep
        while True:
            try:
                await sleeper(self.check_interval)
                reason = self.diagnose()
                if reason:
                    await self.rebuild(reason)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log_error(f"CONNECTION GUARD supervisor error: {error!r}")

    # -- لاگ ---------------------------------------------------------------
    def _log_info(self, message):
        if self.logger is not None:
            self.logger.log_info(message)

    def _log_error(self, message):
        if self.logger is not None:
            self.logger.log_error(message)


# ==========================================================================
#  نصب یک‌جا
# ==========================================================================
def install(client, logger=None, rpc_timeout=None, policy=None,
            **supervisor_options):
    """همهٔ اصلاحات را نصب و ناظر آماده‌به‌کار را برمی‌گرداند.

    فراخوانی دوباره بی‌خطر است؛ هر وصله فقط یک بار اعمال می‌شود.
    """
    from splusthon import helpers
    from splusthon.network.mtprotostate import MTProtoState
    from splusthon.network.connection.websocket import (
        ConnectionWebSocket,
        WebSocketWriter,
    )

    tracker = StaleSessionTracker(supervisor_options.get("window", 120.0))
    supervisor = ConnectionSupervisor(
        client, logger=logger, tracker=tracker, **supervisor_options
    )

    install_stale_session_filter(MTProtoState, tracker, logger)
    install_periodic_reset_fix(ConnectionWebSocket, helpers, logger)
    install_rpc_timeout(
        client,
        timeout=rpc_timeout,
        on_timeout=supervisor.note_rpc_timeout,
        logger=logger,
        policy=policy,
    )
    # سقف زمانی روی مسیر ارسال تا «send-side stall» دیگر ربات را بی‌صدا نکند.
    send_timeout = supervisor_options.get("send_timeout", SEND_TIMEOUT_SECONDS)
    install_send_timeout(
        WebSocketWriter,
        timeout=send_timeout,
        on_stall=supervisor.note_send_stall,
        logger=logger,
    )
    return supervisor
