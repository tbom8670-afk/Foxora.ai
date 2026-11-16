"""🩺 تشخیصِ عملکرد — «دقیقاً کدام مسیر باعثِ کندی شده؟»

این ماژول یک گزارشِ خوانا می‌سازد که برای **مالکِ سراسری در پیوی** ارسال
می‌شود و به‌جایِ کلی‌گویی، **مسیرِ گلوگاه** را نام می‌برد:

  ۱) مسیرِ بررسیِ عضویتِ هر کانالِ اجباری — آیا از مسیرِ ارزانِ
     ``getParticipant`` (۱ RPC) استفاده می‌کند یا مسیرِ گرانِ
     ``scan``/``snapshot`` (O(تعدادِ اعضا ÷ ۱۰۰) RPC)؟ ← ریشهٔ اصلیِ
     «گروه ۲۰هزار کندتر از ۴هزار».
  ۲) Governor — کدام bucket/RPC همین حالا اتصالِ یکتای سروش را نگه داشته
     (culprit) و چه چیزی در صف است.
  ۳) نرخِ برخوردِ کش‌ها (اعضا/پروفایل) — hit-rate پایین یعنی RPCِ تکراری.
  ۴) جمع‌بندیِ خودکارِ «کندترین مسیر» + راه‌حلِ پیشنهادی.

همهٔ بخش‌ها defensive هستند: نبودِ هر ماژول/داده هرگز گزارش را نمی‌شکند.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Optional

# مسیرهای عضویت (ارزان → گران). از force_join_service وام گرفته می‌شود تا
# برچسب‌ها و ترتیب دقیقاً همانی باشد که کدِ واقعی استفاده می‌کند.
try:
    from modules import force_join_service as _fjs
    from modules import force_join_storage as _fjstorage
    _ROUTE_LABELS = getattr(_fjs, "ROUTE_LABELS", {})
    _ROUTE_PARTICIPANT = getattr(_fjs, "ROUTE_PARTICIPANT", "get_participant")
    _ROUTE_BOT_API = getattr(_fjs, "ROUTE_BOT_API", "bot_api")
    _ROUTE_SEARCH = getattr(_fjs, "ROUTE_SEARCH", "search")
    _ROUTE_SCAN = getattr(_fjs, "ROUTE_SCAN", "scan")
    _ROUTE_SNAPSHOT = getattr(_fjs, "ROUTE_SNAPSHOT", "snapshot")
    _HAS_FORCE_JOIN = True
except Exception:  # pragma: no cover
    _ROUTE_LABELS = {}
    _ROUTE_PARTICIPANT = "get_participant"
    _ROUTE_BOT_API = "bot_api"
    _ROUTE_SEARCH = "search"
    _ROUTE_SCAN = "scan"
    _ROUTE_SNAPSHOT = "snapshot"
    _HAS_FORCE_JOIN = False

# مسیرهای «گران» برای بررسیِ عضویت: هزینهٔ آن‌ها با تعدادِ اعضا رشد می‌کند.
_EXPENSIVE_ROUTES = (_ROUTE_SCAN, _ROUTE_SNAPSHOT)

# نرمال‌سازیِ یکسان با بقیهٔ ربات (نیم‌فاصله/فاصلهٔ اضافه/حروف عربی) تا
# دستورِ مالک با هر املایی که می‌نویسد شناخته شود.
try:
    from modules.broadcast_state import normalize_command_text as _normalize
except Exception:  # pragma: no cover
    def _normalize(value):
        return " ".join(str(value or "").split())

_RAW_TRIGGERS = {"گزارش عملکرد", "تشخیص کندی", "گزارش کندی", "perf"}
# .lower() فقط روی «perf» اثر دارد (فارسی case ندارد) تا مالک بتواند
# «PERF»/«Perf» هم بنویسد.
PERF_DIAGNOSTIC_TRIGGERS = frozenset(_normalize(t).lower() for t in _RAW_TRIGGERS)


def is_perf_diagnostic_command(text) -> bool:
    """آیا این متن دستورِ «گزارشِ تشخیصِ عملکرد» است؟ (هرگز raise نمی‌کند)"""
    try:
        if not text:
            return False
        return _normalize(text).lower() in PERF_DIAGNOSTIC_TRIGGERS
    except Exception:
        return False


def _pct(numerator: float, denominator: float) -> str:
    try:
        if not denominator:
            return "—"
        return f"{100.0 * float(numerator) / float(denominator):.0f}%"
    except Exception:
        return "—"


# ------------------------------------------------------------------ #
# ۱) مسیرِ عضویتِ کانال‌های اجباری
# ------------------------------------------------------------------ #
def _membership_route_section(bot: Any) -> tuple[list[str], dict]:
    """برای هر کانالِ اجباری: مسیرِ فعالِ عضویت + پرچمِ «گران» بودن.

    خروجی: ``(lines, findings)`` که ``findings`` شاملِ
    ``expensive_channels`` (کانال‌هایی که مسیرِ گران دارند) و
    ``no_participant`` (کانال‌هایی که getParticipant برایشان فعال نیست) است.
    """
    lines: list[str] = ["🔎 مسیرِ بررسیِ عضویت (force-join):"]
    findings = {"expensive_channels": [], "no_participant": [], "total": 0}
    if not _HAS_FORCE_JOIN:
        lines.append("  • ماژولِ force-join در دسترس نیست.")
        return lines, findings
    try:
        records = _fjstorage.list_groups(enabled_only=True) or []
    except Exception as error:
        lines.append(f"  • خواندنِ کانال‌ها ناموفق بود: {type(error).__name__}")
        return lines, findings

    findings["total"] = len(records)
    if not records:
        lines.append("  • هیچ کانالِ اجباریِ فعالی ثبت نشده است.")
        return lines, findings

    expensive = 0
    for record in records:
        try:
            group_id = record.get("group_id")
            channel_id = record.get("channel_id")
            routes = _fjs.channel_routes(record)
            order = _fjs.route_order(routes)
            has_cheap = bool(routes.get(_ROUTE_PARTICIPANT)) or bool(
                routes.get(_ROUTE_BOT_API))
            # اولین مسیرِ «تصمیم‌گیر» که واقعاً استفاده می‌شود:
            primary = order[0] if order else None
            label = _ROUTE_LABELS.get(primary, primary) if primary else "بدون مسیر"
            is_expensive = (primary in _EXPENSIVE_ROUTES) or (
                not order and not has_cheap)
            if is_expensive:
                expensive += 1
                findings["expensive_channels"].append(
                    {"group_id": group_id, "channel_id": channel_id,
                     "primary": primary, "order": list(order)})
            if not routes.get(_ROUTE_PARTICIPANT):
                findings["no_participant"].append(
                    {"group_id": group_id, "channel_id": channel_id})
            flag = "⚠️ گران" if is_expensive else "✅ ارزان"
            order_txt = "→".join(
                _ROUTE_LABELS.get(r, r) for r in order) or "—"
            lines.append(
                f"  • group={group_id} channel={channel_id}: {flag} | "
                f"مسیرِ اصلی={label} | ترتیب={order_txt}")
        except Exception as error:
            lines.append(f"  • یک کانال خوانده نشد: {type(error).__name__}")

    lines.append(
        f"  ➕ خلاصه: {expensive} از {len(records)} کانال مسیرِ "
        f"گرانِ عضویت دارند (getParticipant فعال نیست).")
    return lines, findings


# ------------------------------------------------------------------ #
# ۲) Governor — چه چیزی همین حالا اتصال را گرفته؟
# ------------------------------------------------------------------ #
def _governor_section(bot: Any) -> tuple[list[str], dict]:
    lines: list[str] = ["", "🚦 Governor (اتصالِ یکتای سروش):"]
    findings = {"culprit": None, "busiest_waiting_bucket": None,
                "total_limit": None, "waiting": 0}
    gov = getattr(bot, "rpc_governor", None)
    if gov is None:
        lines.append("  • Governor در دسترس نیست.")
        return lines, findings
    try:
        snap = gov.snapshot()
    except Exception as error:
        lines.append(f"  • snapshot ناموفق: {type(error).__name__}")
        return lines, findings

    findings["total_limit"] = snap.get("total_limit")
    findings["waiting"] = snap.get("waiting", 0)
    mode = "shadow" if snap.get("shadow") else (
        "فعال" if snap.get("enabled") else "خاموش")
    lines.append(
        f"  • حالت={mode} | در جریان={snap.get('active', 0)}/"
        f"{snap.get('total_limit', 0)} | در صف={snap.get('waiting', 0)} | "
        f"بیشینهٔ صف={snap.get('max_waiting', 0)}")
    abw = snap.get("active_by_bucket", {}) or {}
    wbk = snap.get("waiting_by_bucket", {}) or {}
    if abw:
        lines.append("  • فعال به تفکیک bucket: " + ", ".join(
            f"{k}:{v}" for k, v in abw.items() if v))
    if wbk:
        busiest = max((k for k in wbk if wbk[k]), key=lambda k: wbk[k],
                      default=None)
        if busiest:
            findings["busiest_waiting_bucket"] = (busiest, wbk[busiest])
        lines.append("  • در صف به تفکیک bucket: " + ", ".join(
            f"{k}:{v}" for k, v in wbk.items() if v))

    # culprit: کندترین holderِ فعلی (همان چیزی که اتصال را نگه داشته).
    holders = snap.get("holders", []) or []
    slowest = None
    slowest_ms = -1.0
    for holder in holders:
        try:
            held = float(str(holder).split("held_ms=")[-1])
        except Exception:
            held = 0.0
        if held > slowest_ms:
            slowest_ms, slowest = held, holder
    if slowest is not None:
        findings["culprit"] = (slowest, slowest_ms)
        lines.append(f"  • 🔥 کندترین holderِ فعلی (culprit): {slowest}")
    try:
        culprit_fn = getattr(gov, "culprit_for", None)
        if callable(culprit_fn):
            extra = culprit_fn(None)
            if extra and extra != "-":
                lines.append(f"  • culprit (از دیدِ Governor): {extra}")
    except Exception:
        pass
    return lines, findings


# ------------------------------------------------------------------ #
# ۳) نرخِ برخوردِ کش‌ها
# ------------------------------------------------------------------ #
def _cache_section(bot: Any) -> list[str]:
    lines: list[str] = ["", "🗃 نرخِ برخوردِ کش‌ها (hit-rate پایین = RPCِ تکراری):"]
    try:
        from modules import participant_cache as _pc
        s = getattr(_pc, "STATS", {}) or {}
        calls = int(s.get("calls", 0) or 0)
        hits = int(s.get("hits", 0) or 0)
        lines.append(
            f"  • کشِ اعضا (GetParticipants): calls={calls} hits={hits} "
            f"hit-rate={_pct(hits, calls)} coalesced={s.get('coalesced', 0)} "
            f"throttled={s.get('throttled', 0)} errors={s.get('errors', 0)}")
        # راندِ ۱۸: تفکیکِ «خواندنِ واقعی» از «اسکن/تازه‌سازی» — hit-rateِ
        # تجمیعی با نوبت‌های اسکن رقیق می‌شد و ۹٪ نمایشیِ گمراه‌کننده بود.
        by_kind = getattr(_pc, "STATS_BY_KIND", {}) or {}
        real = {"calls": 0, "hits": 0}
        scan = {"calls": 0, "hits": 0}
        for kind, row in by_kind.items():
            bucket = scan if kind == "participants_page" else real
            bucket["calls"] += int(row.get("calls", 0) or 0)
            bucket["hits"] += int(row.get("hits", 0) or 0)
        if by_kind:
            lines.append(
                f"    ├ خواندنِ واقعی (owner/members): calls={real['calls']} "
                f"hits={real['hits']} hit-rate={_pct(real['hits'], real['calls'])}")
            lines.append(
                f"    └ اسکن/تازه‌سازی (participants_page): "
                f"calls={scan['calls']} hits={scan['hits']} "
                f"hit-rate={_pct(scan['hits'], scan['calls'])} "
                f"(نوبتِ اسکن همیشه callِ تازه است — hit-rateِ پایینش طبیعی)")
    except Exception:
        lines.append("  • کشِ اعضا: در دسترس نیست.")
    try:
        from modules import user_profile_cache as _upc
        s = getattr(_upc, "STATS", {}) or {}
        calls = int(s.get("calls", 0) or 0)
        hits = int(s.get("hits", 0) or 0)
        lines.append(
            f"  • کشِ پروفایل (GetFullUser): calls={calls} hits={hits} "
            f"hit-rate={_pct(hits, calls)} coalesced={s.get('coalesced', 0)} "
            f"throttled={s.get('throttled', 0)}")
    except Exception:
        lines.append("  • کشِ پروفایل: در دسترس نیست.")
    return lines


# ------------------------------------------------------------------ #
# ۴) جمع‌بندیِ «کندترین مسیر»
# ------------------------------------------------------------------ #
def _verdict_section(mem: dict, gov: dict) -> list[str]:
    lines: list[str] = ["", "🧭 جمع‌بندی — کندترین مسیر و راه‌حل:"]
    ranked: list[tuple[int, str]] = []

    expensive = mem.get("expensive_channels") or []
    if expensive:
        ranked.append((
            100 + len(expensive),
            f"مسیرِ گرانِ بررسیِ عضویت برای {len(expensive)} کانال "
            f"(scan/snapshot = O(اعضا÷۱۰۰) RPC). راه‌حل: اجرای «تست عضویت» "
            f"تا getParticipant (۱ RPC) فعال و ذخیره شود؛ سپس هر بررسی فقط "
            f"۱ درخواست می‌شود (مستقل از ۴هزار یا ۲۰هزار نفر)."))

    culprit = gov.get("culprit")
    if culprit:
        ranked.append((
            60,
            f"اتصالِ سروش همین حالا توسطِ «{culprit[0]}» اشغال شده "
            f"(~{culprit[1]:.0f}ms). اگر GetParticipants/GetFullUser است، "
            f"یعنی خواندنِ سنگین؛ اگر SendMessage/Delete است، یعنی حجمِ "
            f"ارسال/حذف."))

    busiest = gov.get("busiest_waiting_bucket")
    if busiest and int(busiest[1]) >= 3:
        ranked.append((
            40 + int(busiest[1]),
            f"صفِ bucket=«{busiest[0]}» با {busiest[1]} درخواست در انتظار "
            f"→ همان گلوگاهِ لحظه‌ای است."))

    if not ranked:
        lines.append(
            "  • گلوگاهِ آشکاری از مسیرهای عضویت/Governor/کش دیده نشد. "
            "احتمالاً علت، حجمِ بالای پیام یا محدودیتِ نرخِ سمتِ سروش "
            "(FloodWait) است؛ total_limit و critical_reserve را در .env "
            "ببینید.")
    else:
        ranked.sort(key=lambda x: x[0], reverse=True)
        for i, (_score, text) in enumerate(ranked[:3], start=1):
            lines.append(f"  {i}) {text}")
    return lines


#: 🏷️ راندِ استقرار — با هر راندِ رفعِ اشکال بالا می‌رود و در گزارشِ مالک چاپ
#: می‌شود تا مالک (و پشتیبانی) بتواند تأیید کند گوشی واقعاً کدام ساخت را
#: اجرا می‌کند (شکایتِ راندِ ۱۵: «رفع را push کردی ولی گوشی هنوز قدیمی بود»).
BUILD_ROUND = 17


def build_slow_path_report(bot: Any) -> str:
    """گزارشِ متمرکز بر «کدام مسیر کند است» (بدونِ گزارشِ عمومیِ سلامت)."""
    header = [
        "🩺 گزارشِ تشخیصِ کندی — کدام مسیر گلوگاه است؟",
        "═" * 38,
        f"🏷️ راندِ استقرار: {BUILD_ROUND}",
    ]
    mem_lines, mem = _membership_route_section(bot)
    gov_lines, gov = _governor_section(bot)
    cache_lines = _cache_section(bot)
    verdict_lines = _verdict_section(mem, gov)
    body = header + [""] + mem_lines + gov_lines + cache_lines + verdict_lines
    return "\n".join(body)


def build_full_report(bot: Any) -> str:
    """گزارشِ تشخیصِ کندی + گزارشِ جامعِ سلامت (Observability)."""
    parts = [build_slow_path_report(bot)]
    try:
        from modules.observability import MetricsCollector
        general = MetricsCollector.get_instance().format_diagnostic_report(bot)
        if general:
            parts.append("")
            parts.append("═" * 38)
            parts.append(general)
    except Exception:
        pass
    text = "\n".join(parts)
    # تلگرام/سروش پیامِ خیلی بلند را نمی‌پذیرد؛ در صورتِ نیاز قطعه‌قطعه می‌شود.
    return text


def _chunk(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        # سعی کن در مرزِ خط بشکنی.
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl > start:
                end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


async def send_perf_report_to_owner(bot: Any, *, context: str = "PERF_REPORT",
                                    send_timeout: float = 20.0) -> bool:
    """گزارشِ تشخیص را در **پیویِ مالکِ سراسری** می‌فرستد.

    از همان الگوی ``performance_monitor`` استفاده می‌کند
    (``resolve_private_owner_peer`` + ``client.send_message``). اگر مالک
    resolve نشود یا ارسال شکست بخورد، ``False`` برمی‌گرداند و در لاگ می‌آید.
    """
    client = getattr(bot, "client", None)
    logger = getattr(bot, "logger", None)
    if client is None:
        return False
    try:
        from modules.owner_private import resolve_private_owner_peer
    except Exception as error:
        if logger is not None:
            logger.log_error(f"PERF REPORT owner_private unavailable {error!r}")
        return False

    try:
        report = build_full_report(bot)
    except Exception as error:
        report = f"⚠️ ساختِ گزارشِ عملکرد ناموفق بود: {type(error).__name__}: {error}"

    try:
        target = await resolve_private_owner_peer(
            client, logger=logger, context=context)
    except Exception as error:
        if logger is not None:
            logger.log_error(f"PERF REPORT resolve owner peer failed {error!r}")
        return False
    if target is None:
        if logger is not None:
            logger.log_error("PERF REPORT owner peer unresolved (None)")
        return False

    sent_any = False
    for piece in _chunk(report):
        try:
            result = client.send_message(target, piece)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=send_timeout)
            sent_any = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if logger is not None:
                logger.log_error(f"PERF REPORT send failed {error!r}")
    if logger is not None and sent_any:
        try:
            logger.log_info("PERF REPORT SENT TO OWNER PV "
                            f"context={context} chars={len(report)}")
        except Exception:
            pass
    return sent_any
