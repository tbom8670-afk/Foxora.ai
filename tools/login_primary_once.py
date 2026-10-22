#!/usr/bin/env python3
"""One-shot primary Soroush login without client.start retry loops.

Run only while the bot is stopped.  It makes exactly one connect/code/sign-in
attempt and exits on any network failure.

نشست جدید **فقط** داخل متغیر اختصاصی همین instance ذخیره می‌شود
(``SOROUSH_SESSION_STRING_<INSTANCE>`` — برای Foxora یعنی
``SOROUSH_SESSION_STRING_FOXORA``) و به متغیر عمومی
``SOROUSH_SESSION_STRING`` — که ممکن است متعلق به ربات دیگری (B2) باشد —
هیچ‌وقت دست نمی‌زند.
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def session_env_key() -> str:
    """نام متغیر نشستِ اختصاصی همین instance.

    منبع حقیقت: ``BOT_INSTANCE`` اگر ست شده باشد، در غیر این صورت فایل
    نشانگر ``.bot_instance`` کنار ریشهٔ پروژه؛ در این مخزن هر دو «foxora»
    هستند پس خروجی «SOROUSH_SESSION_STRING_FOXORA» است.  حتی در نبود هر دو،
    متغیر عمومی هدف نمی‌شود.
    """
    instance = (os.environ.get("BOT_INSTANCE") or "").strip()
    if not instance:
        try:
            instance = (ROOT / ".bot_instance").read_text(encoding="utf-8").strip()
        except OSError:
            instance = ""
    if not instance:
        instance = "foxora"  # هویت این مخزن؛ هرگز به متغیر عمومی برنمی‌گردد
    return f"SOROUSH_SESSION_STRING_{instance.upper()}"


def load_env():
    try: rows = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError: return
    for row in rows:
        if "=" not in row or row.lstrip().startswith("#"): continue
        key, value = row.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def save_primary(value: str, *, key: str | None = None, env_file: Path | None = None):
    """نشست را فقط در متغیر اختصاصی instance می‌نویسد.

    خط ``SOROUSH_SESSION_STRING=`` (عمومی، احتمالاً متعلق به B2) و هر متغیر
    اختصاصی دیگر دقیقاً همان‌طور که هست باقی می‌ماند.  تمام داده‌ها فقط در
    ``.env`` همین مخزن (کنار ریشهٔ Foxora) نوشته می‌شود.
    """
    key = key or session_env_key()
    target = env_file or ENV_FILE
    prefix = key + "="
    rows = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    output, found = [], False
    for row in rows:
        if row.startswith(prefix):
            output.append(prefix + value)
            found = True
        else:
            output.append(row)
    if not found:
        if output and output[-1].strip():
            output.append("")
        output.append(prefix + value)
    fd, tmp = tempfile.mkstemp(prefix=".env.login.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(output) + "\n")
        os.replace(tmp, target)
        try: target.chmod(0o600)
        except OSError: pass
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


async def main():
    load_env()
    try:
        from splusthon import SoroushClient
        from splusthon.sessions import StringSession
    except ImportError as error:
        raise SystemExit("با python3 همان محیط Termux اجرا کنید.") from error
    api_id, api_hash = os.getenv("API_ID"), os.getenv("API_HASH")
    client = SoroushClient(StringSession(), int(api_id), api_hash) if api_id and api_hash else SoroushClient(StringSession())
    try:
        print("اتصال یک‌باره به سروش؛ در صورت خطا برنامه متوقف می‌شود.")
        await asyncio.wait_for(client.connect(), timeout=25)
        phone = input("شماره تلفن با +98: ").strip()
        await asyncio.wait_for(client.send_code_request(phone), timeout=25)
        code = input("کد تأیید: ").strip()
        try:
            await asyncio.wait_for(client.sign_in(phone, code), timeout=30)
        except Exception as error:
            if "password" not in type(error).__name__.lower():
                raise
            password = input("رمز دومرحله‌ای: ")
            await asyncio.wait_for(client.sign_in(password=password), timeout=30)
        value = client.session.save()
        if not value: raise RuntimeError("session string خالی است")
        key = session_env_key()
        save_primary(value, key=key)
        print(f"✅ نشست جدید فقط در {key} داخل .env ذخیره شد.")
        print("   به SOROUSH_SESSION_STRING عمومی و نشست ربات‌های دیگر دست زده نشد.")
    except Exception as error:
        print(f"❌ ورود انجام نشد: {error!r}")
        print("هیچ retry خودکاری انجام نشد؛ بعداً دوباره فقط همین ابزار را اجرا کنید.")
        raise SystemExit(1)
    finally:
        try: await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception: pass

if __name__ == "__main__":
    asyncio.run(main())
