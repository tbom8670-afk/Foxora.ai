"""📥 ورودِ جمله‌ها از فایلِ کاربر (Termux) به فهرست‌های فیلتر.

کارفرما یک فایل متنی روی گوشی/Termux دارد و می‌خواهد جمله‌های مربوط به
**کانال** و **بیوگرافی** یک‌جا از همان فایل خوانده و در دو فهرستِ درست
نشسته شوند. این ماژول همان کار را می‌کند — بدون شبکه، بدون دست‌زدن به
هیچ فایل دیگری.

قالب فایل (هر دو شکل پذیرفته می‌شود)::

    CHANNEL
    فیلم و سریال
    فیلم سوپر

    BIO
    بیا تو کانال ما فیلم هست

یا::

    [channel]
    فیلم و سریال
    [bio]
    بیا تو کانال ما فیلم هست

قاعده‌ها:

* خطوط خالی و خطوطی که با ``#`` شروع می‌شوند نادیده گرفته می‌شوند.
* اگر فایل هیچ سرخطی نداشته باشد، باید بخش (`channel`/`bio`) صریحاً
  داده شود؛ وگرنه هیچ چیزی نوشته نمی‌شود (تا اشتباهی در فهرست غلط ننشیند).
* تکرارِ جمله‌ها (با همان نرمال‌سازیِ تطبیق) نوشته نمی‌شود؛ پس اجرای
  دوبارهٔ همان فایل چیزی را دوباره اضافه نمی‌کند.
* نوشتن اتمیک است و در **تازه‌ترین** نسخهٔ فایلِ فهرست انجام می‌شود؛
  ربات در همان لحظه (بدون ری‌استارت) آن را می‌خواند.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from modules.atomic_write import write_text
from modules.profile_guard import filters, matcher

SECTION_CHANNEL = "channel"
SECTION_BIO = "bio"

MAX_FILE_BYTES = 2 * 1024 * 1024  # ۲ مگابایت سقفِ معقول برای یک فایل متنی

#: سرخط‌ها (بدون حساسیت به حروف بزرگ/کوچک و با نویسه‌های فارسی)
_HEADERS: Dict[str, set] = {
    SECTION_CHANNEL: {"channel", "channels", "کانال", "کانالها", "کانال ها",
                      "نام کانال", "نامکانال"},
    SECTION_BIO: {"bio", "biography", "بیو", "بیوگرافی", "بیوگرافیها",
                  "بیو گرافی"},
}


def _squash(text) -> str:
    folded = matcher.normalize(text)
    return "".join(ch for ch in folded if ch.isalnum())


def _header_section(line: str) -> Optional[str]:
    """اگر خط یک سرخطِ بخش باشد، نامِ بخش را برمی‌گرداند."""
    value = str(line).strip()
    bracketed = value.startswith("[") and value.endswith("]")
    if bracketed:
        value = value[1:-1]
    elif value.endswith(":"):
        value = value[:-1]
    key = _squash(value)
    if not key:
        return None
    for section, names in _HEADERS.items():
        if any(key == _squash(name) for name in names):
            return section
    return None


def _key(phrase: str) -> str:
    """کلید یکدست برای تشخیص تکرار (هم‌معنای تطبیق)."""
    return _squash(phrase)


def parse(text: str) -> Dict[str, object]:
    """متن فایل → ``{"channel": [...], "bio": [...], "skipped": n}``."""
    out: Dict[str, object] = {SECTION_CHANNEL: [], SECTION_BIO: [], "skipped": 0}
    current: Optional[str] = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        section = _header_section(line)
        if section is not None:
            current = section
            continue
        if current is None:
            out["skipped"] = int(out["skipped"]) + 1
            continue
        out[current].append(line)  # type: ignore[index]
    return out


def _existing_keys(path: Path) -> set:
    keys = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            keys.add(_key(text))
    except OSError:
        pass
    return keys


def _append(path: Path, lines: List[str], source: str) -> int:
    """خطوط تازه را به فایل اضافه می‌کند و تعدادِ نوشته‌شده را می‌دهد."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    block = [f"# ↳ افزوده‌شده از فایل: {source} — {stamp}"]
    block.extend(lines)
    write_text(path, existing + "\n".join(block) + "\n")
    return len(lines)


def import_text(text: str, *, source: str = "input",
                default_section: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, object]:
    """متن را می‌خواند و به فهرست‌ها اضافه می‌کند (بدون تکرار)."""
    parsed = parse(text)
    report: Dict[str, object] = {
        "ok": False,
        "added": {SECTION_CHANNEL: 0, SECTION_BIO: 0},
        "skipped_duplicates": {SECTION_CHANNEL: 0, SECTION_BIO: 0},
        "errors": [],
        "files": {},
        "source": source,
    }
    channel_items = list(parsed[SECTION_CHANNEL])  # type: ignore[arg-type]
    bio_items = list(parsed[SECTION_BIO])  # type: ignore[arg-type]

    # بدونِ سرخط: فقط اگر بخش صریحاً خواسته شده باشد
    if not channel_items and not bio_items and int(parsed["skipped"]):
        if default_section not in (SECTION_CHANNEL, SECTION_BIO):
            report["errors"].append(
                "فایل هیچ سرخطی ندارد. یا بالای بخش‌ها «CHANNEL» و «BIO» "
                "بنویسید، یا بخش را صریح بدهید (کانال/بیو)."
            )
            return report
        collected = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            collected.append(line)
        if default_section == SECTION_CHANNEL:
            channel_items = collected
        else:
            bio_items = collected

    targets = (
        (SECTION_CHANNEL, channel_items, filters.CHANNEL_LIST_FILE),
        (SECTION_BIO, bio_items, filters.BIO_LIST_FILE),
    )
    for section, items, filename in targets:
        if not items:
            continue
        path = filters._resolve(filename)
        report["files"][section] = str(path)
        seen = _existing_keys(path)
        fresh: List[str] = []
        for phrase in items:
            key = _key(phrase)
            if not key or key in seen:
                report["skipped_duplicates"][section] += 1  # type: ignore[index]
                continue
            seen.add(key)
            fresh.append(str(phrase))
        if not fresh:
            continue
        if dry_run:
            report["added"][section] = len(fresh)  # type: ignore[index]
            continue
        try:
            report["added"][section] = _append(path, fresh, source)  # type: ignore[index]
        except Exception as error:  # noqa: BLE001 — فایلِ قفل/مجوز
            report["errors"].append(f"نوشتن در {filename} نشد: {error}")

    try:
        filters.reload()
    except Exception:
        pass
    report["ok"] = bool(
        not report["errors"]
        and (report["added"][SECTION_CHANNEL]  # type: ignore[index]
             or report["added"][SECTION_BIO])  # type: ignore[index]
    )
    report["totals"] = {
        SECTION_CHANNEL: len(filters.channel_names()),
        SECTION_BIO: len(filters.bio_sentences()),
    }
    return report


def import_file(path: str, *, default_section: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, object]:
    """فایل را (با پشتیبانی ``~``) می‌خواند و وارد می‌کند."""
    resolved = Path(str(path).strip()).expanduser()
    report: Dict[str, object] = {
        "ok": False, "added": {}, "errors": [], "files": {},
        "source": str(resolved),
    }
    try:
        size = resolved.stat().st_size
    except OSError as error:
        report["errors"] = [f"فایل پیدا نشد: {resolved} ({error.strerror})"]
        return report
    if size > MAX_FILE_BYTES:
        report["errors"] = [f"فایل بزرگ‌تر از {MAX_FILE_BYTES} بایت است."]
        return report
    try:
        text = resolved.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            report["errors"] = [f"خواندن فایل نشد: {error}"]
            return report
    except OSError as error:
        report["errors"] = [f"خواندن فایل نشد: {error}"]
        return report
    return import_text(text, source=resolved.name,
                       default_section=default_section, dry_run=dry_run)


def summary(report: Dict[str, object]) -> str:
    """گزارشِ کوتاهِ فارسی برای نمایش در چت."""
    errors: Iterable[str] = report.get("errors") or []
    added = report.get("added") or {}
    dup = report.get("skipped_duplicates") or {}
    totals = report.get("totals") or {}
    if errors:
        return "\n".join(["❌ ورود فایل انجام نشد:"] + [f"• {e}" for e in errors])
    lines = [
        "✅ فایل خوانده شد:",
        f"➖ نام کانال اضافه‌شده : {int(added.get(SECTION_CHANNEL, 0) or 0)}",
        f"➖ جمله بیو اضافه‌شده : {int(added.get(SECTION_BIO, 0) or 0)}",
    ]
    dup_channels = int(dup.get(SECTION_CHANNEL, 0) or 0)
    dup_bio = int(dup.get(SECTION_BIO, 0) or 0)
    if dup_channels or dup_bio:
        lines.append(f"➖ تکراری (نادیده) : {dup_channels + dup_bio}")
    if totals:
        lines.append(
            "➖ مجموع فهرست‌ها : "
            f"{int(totals.get(SECTION_CHANNEL, 0) or 0)} نام"
            f" + {int(totals.get(SECTION_BIO, 0) or 0)} جمله"
        )
    return "\n".join(lines)
