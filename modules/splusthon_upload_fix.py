"""رفع خطای ``FILE_REQUEST_RECEIVED_ON_CONNECTION_SERVER`` در SPlusthon.

ریشهٔ مشکل
----------
طبق مستندات رسمی MTProto، درخواست‌های فایل مثل ``upload.saveFilePart`` /
``upload.saveBigFilePart`` باید از طریق یک «اتصال/جلسهٔ اختصاصیِ فایل» به
«DC رسانه‌ای» (media DC) ارسال شوند، نه از طریق اتصالِ اصلیِ
«connection server». اگر این درخواست‌ها به یک connection server برسد،
سرور آن را با خطای ``FILE_REQUEST_RECEIVED_ON_CONNECTION_SERVER`` رد
می‌کند.

SPlusthon آپلود را با ``self(request)`` یعنی از طریق همان ``self._sender``
اصلی (که به im-server.splus.ir یعنی connection server وصل است) می‌فرستد؛
بنابراین ``SaveFilePartRequest`` به سرورِ اتصال می‌رسد و رد می‌شود.

قانونِ این وصله
---------------
**هرگز** ``SaveFilePart``/``SaveBigFilePart`` را روی connection server
اصلی نفرست. اگر sender رسانه‌ای (media/file DC) ساخته شد → آپلود از طریقِ
آن برود. اگر ساخته نشد → به‌جای fallback روی sender اصلی، خطای واضحِ
``MediaSenderUnavailableError`` صادر می‌شود تا لایهٔ بالاتر (photo_download)
با ``InputMediaPhotoExternal`` ارسال کند؛ هیچ‌وقت آپلود روی سرورِ اتصال
انجام نمی‌شود.
"""
import logging

import asyncio
import logging

from splusthon import SoroushClient
from splusthon.network import MTProtoSender
from splusthon.tl import functions
from splusthon.tl.alltlobjects import LAYER

_log = logging.getLogger("splusthon_upload_fix")

_FILE_UPLOAD_TYPES = (
    functions.upload.SaveFilePartRequest,
    functions.upload.SaveBigFilePartRequest,
)

_INSTALLED = False
_ORIGINAL_CALL = None


class MediaSenderUnavailableError(RuntimeError):
    """sender رسانه‌ای/فایل در دسترس نیست؛ برای جلوگیری از ارسالِ
    SaveFilePart روی connection server اصلی صادر می‌شود."""


def _find_media_dc(client):
    """media DC را از config زندهٔ سرور پیدا می‌کند.

    طبق پروتکل، dc_option که ``media_only=True`` دارد همان سرورِ مخصوصِ
    فایل است. اگر چندتا بود، سرورِ مربوط به DC فعلی ترجیح داده می‌شود.
    """
    cfg = getattr(client, "_config", None)
    opts = getattr(cfg, "dc_options", None)
    if not opts:
        return None
    current_dc = getattr(getattr(client, "session", None), "dc_id", None)
    media = [d for d in opts if bool(getattr(d, "media_only", False))]
    if not media:
        return None
    for d in media:
        if getattr(d, "id", None) == current_dc:
            return d
    return media[0]


def _fallback_dc(client):
    """بدون media DC در config → از DC فعلی، مشخصاتِ اتصالِ جداگانه را می‌سازد."""
    session = getattr(client, "session", None)
    from types import SimpleNamespace
    return SimpleNamespace(
        id=getattr(session, "dc_id", 3),
        ip_address=getattr(session, "server_address", "im-server.splus.ir"),
        port=getattr(session, "port", 443),
    )


async def _ensure_config(client):
    """اطمینان از اینکه config زندهٔ سرور (dc_options) لود شده است."""
    if getattr(client, "_config", None) is None:
        try:
            client._config = await client(functions.help.GetConfigRequest())
            _log.info("MEDIA SENDER: config loaded with %d dc_options",
                      len(getattr(client._config, "dc_options", []) or []))
        except Exception as e:  # pragma: no cover - فقط دفاعی
            _log.warning("GetConfig failed while preparing upload: %s", e)
            return False
    return getattr(client, "_config", None) is not None


MEDIA_SENDER_TIMEOUT = 8  # حداکثرِ زمانِ ساختِ sender رسانه‌ای (اتصال + auth)


async def _create_media_sender(client, dc):
    """یک MTProtoSender اختصاصی به سرورِ فایل/DC داده‌شده می‌سازد.

    آدرسِ WebSocket/DC را log می‌کند تا علتِ قطعِ اتصال معلوم شود.
    کلِ عملیات (اتصال + export auth + import) سقفِ زمانی دارد تا یک اتصالِ
    مرده مدت‌ها کلِ عملیات را درگیر نکند. در صورتِ هر خطا، sender به‌درستی
    disconnect می‌شود تا aiohttp session نشت نکند (رفعِ «Unclosed client
    session»).
    """
    _log.info(
        "MEDIA SENDER connecting to ws dc=%s ip=%s port=%s "
        "(media_only=%s)",
        getattr(dc, "id", None),
        getattr(dc, "ip_address", None),
        getattr(dc, "port", None),
        getattr(dc, "media_only", None),
    )
    sender = MTProtoSender(None, loggers=getattr(client, "_log", None))
    try:
        await asyncio.wait_for(
            sender.connect(client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=getattr(client, "_log", None),
                proxy=getattr(client, "_proxy", None),
                local_addr=getattr(client, "_local_addr", None),
            )),
            timeout=MEDIA_SENDER_TIMEOUT,
        )
        _log.info("MEDIA SENDER connected; exporting auth for DC %s",
                  getattr(dc, "id", None))
        auth = await asyncio.wait_for(
            client(functions.auth.ExportAuthorizationRequest(dc.id)),
            timeout=MEDIA_SENDER_TIMEOUT,
        )
        client._init_request.query = functions.auth.ImportAuthorizationRequest(
            id=auth.id, bytes=auth.bytes)
        req = functions.InvokeWithLayerRequest(LAYER, client._init_request)
        await asyncio.wait_for(sender.send(req), timeout=MEDIA_SENDER_TIMEOUT)
        sender.dc_id = dc.id
        _log.info("MEDIA SENDER ready dc=%s", dc.id)
        return sender
    except Exception:
        # رفعِ نشتیِ session: sender را به‌درستی disconnect کن (خودش
        # connection و aiohttp session را می‌بندد).
        try:
            await sender.disconnect()
        except Exception:
            pass
        raise


def _sender_alive(sender):
    try:
        fut = getattr(sender, "disconnected", None)
        return fut is not None and not fut.done()
    except Exception:
        return True


async def _get_media_sender(client, force_reconnect=False):
    """sender مخصوصِ آپلود را برای این کلاینت می‌سازد/برمی‌گرداند (یا None).

    - اگر sender موجود و زنده است و force_reconnect=False → همان.
    - اگر force_reconnect=True → sender قبلی (حتی اگر ظاهراً زنده باشد) قطع و
      از نو ساخته می‌شود (برای تلاشِ مجددِ آپلود).
    - اگر ساخت در این لحظه شکست خورد → None (هرگز به main sender برنمی‌گردد).
    """
    sender = getattr(client, "_media_sender", None)
    if not force_reconnect and sender is not None and _sender_alive(sender):
        return sender

    # قطعِ sender قبلی (اگر هست) برای تلاشِ مجددِ تمیز
    if sender is not None:
        try:
            await sender.disconnect()
        except Exception:
            pass
        client._media_sender = None

    if not await _ensure_config(client):
        _log.error("MEDIA SENDER unavailable: client._config is None")
        return None

    dc = _find_media_dc(client) or _fallback_dc(client)
    _log.info(
        "MEDIA SENDER (re)build dc=%s ip=%s port=%s media_only=%s force=%s",
        getattr(dc, "id", None),
        getattr(dc, "ip_address", None),
        getattr(dc, "port", None),
        getattr(dc, "media_only", None),
        force_reconnect,
    )
    try:
        sender = await _create_media_sender(client, dc)
    except Exception as e:
        _log.error(
            "MEDIA SENDER creation FAILED dc=%s ip=%s reason=%s: %s "
            "(will NOT fall back to main sender)",
            getattr(dc, "id", None),
            getattr(dc, "ip_address", None),
            type(e).__name__,
            e,
        )
        return None
    client._media_sender = sender
    return sender


def _is_file_upload(request):
    """آیا درخواست (یا لیستی از درخواست‌ها) آپلودِ فایل است؟"""
    reqs = request if isinstance(request, (list, tuple)) else [request]
    return any(isinstance(r, _FILE_UPLOAD_TYPES) for r in reqs)


async def _redirected_call(self, request, ordered=False, flood_sleep_threshold=None,
                           _orig_call=None, _get_sender=None):
    """پیاده‌سازیِ واقعیِ هدایتِ آپلود (برای تست قابلِ فراخوانی).

    قانونِ کلیدی: اگر درخواستِ SaveFilePart/SaveBigFilePart باشد و sender
    رسانه‌ای در دسترس نباشد، **به sender اصلی برنمی‌گردد** و خطای واضح
    می‌دهد تا لایهٔ بالاتر با InputMediaPhotoExternal ارسال کند.
    """
    get_sender = _get_sender or _get_media_sender
    if _is_file_upload(request):
        media = await get_sender(self)
        if media is None:
            raise MediaSenderUnavailableError(
                "Media/file sender unavailable; refusing to send "
                "SaveFilePart on the main connection server "
                "(would trigger FILE_REQUEST_RECEIVED_ON_CONNECTION_SERVER)")
        return await self._call(
            media, request, ordered=ordered,
            flood_sleep_threshold=flood_sleep_threshold)
    orig = _orig_call or _ORIGINAL_CALL
    return await orig(self, request, ordered=ordered,
                      flood_sleep_threshold=flood_sleep_threshold)


def install_media_upload():
    """مونکی‌پچِ ``SoroushClient.__call__`` برای مسیریابیِ آپلود به media DC.

    - درخواست‌های SaveFilePart/SaveBigFilePart را در صورتِ در دسترس‌بودنِ
      sender رسانه‌ای به آن هدایت می‌کند.
    - اگر sender رسانه‌ای در دسترس نبود → خطای واضح (هرگز sender اصلی).
    - بقیهٔ درخواست‌ها دست‌نخورده. چند بار صدا زدن بی‌اثر است.
    """
    global _INSTALLED, _ORIGINAL_CALL
    if _INSTALLED:
        return
    _ORIGINAL_CALL = SoroushClient.__call__

    async def _bound_redirect(self, request, ordered=False, flood_sleep_threshold=None):
        return await _redirected_call(
            self, request, ordered=ordered,
            flood_sleep_threshold=flood_sleep_threshold)

    SoroushClient.__call__ = _bound_redirect
    _INSTALLED = True
    _log.info("MEDIA_UPLOAD_PATCH_LOADED=True")
