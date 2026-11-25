"""Private, owner-only reports of explicitly named topics in group messages.

The detector is keyword based, not a claim of semantic or insult detection.
It performs no automatic moderation. Report/message mapping contains IDs only;
message contents are delivered to the owner but are not stored in the mapping.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import closing
import os
import re
import sqlite3
import time
from pathlib import Path

from modules.owner_check import get_owner, is_global_owner
from modules.profile_guard import matcher
from modules.runtime_paths import runtime_db_file

# Specific names and date forms. Short ambiguous words such as «ایران» and
# «محمد» are intentionally excluded; an isolated «شاه» is included as asked.
TERMS = (
    'جاویدنام', 'جاوید نام', 'جاویدنامان', 'جاوید نامان',
    '۱۸ دی', '۱۹ دی', '۱۸ ۱۹ دی', '۱۸ و ۱۹ دی', '۱۸-۱۹ دی',
    'هجده دی', 'نوزده دی', 'هجدهم دی', 'نوزدهم دی',
    'بسیج', 'بسیجی', 'سازمان بسیج', 'سپاه', 'سپاه پاسداران',
    'جمهوری اسلامی', 'نظام جمهوری اسلامی', 'حکومت جمهوری اسلامی',
    'نظام ایران', 'حکومت ایران', 'رژیم ایران', 'حکومت اسلامی',
    'رهبر ایران', 'رهبران ایران', 'رهبر جمهوری اسلامی', 'رهبر انقلاب',
    'نیروی انتظامی', 'فراجا', 'پلیس ایران',
    'پهلوی', 'رضا شاه', 'رضاشاه', 'محمدرضا شاه', 'محمد رضا شاه',
    'رضا پهلوی', 'محمدرضا پهلوی', 'محمد رضا پهلوی', 'شاه', 'شاهنشاهی',
    'خامنه ای', 'خامنه‌ای', 'علی خامنه ای', 'مجتبی خامنه ای',
    'خمینی', 'روح الله خمینی', 'روح‌الله خمینی',
    'ابراهیم رئیسی', 'رئیسی', 'پزشکیان', 'مسعود پزشکیان',
    'حسن روحانی', 'احمدی نژاد', 'احمدی‌نژاد', 'رفسنجانی',
    'موساد', 'موساد اسرائیل', 'اسرائیل', 'اسراییل', 'نتانیاهو',
    'Mossad', 'Israel', 'Khamenei', 'Khomeini', 'Pahlavi',
    'Reza Shah', 'Mohammad Reza Shah', 'Islamic Republic',
)
_PATTERNS = matcher.build(TERMS)
_REPEAT = re.compile(r'([^\W_])(?:[\W_]*\1)+', re.UNICODE)


def _fold_stretched(value):
    return _REPEAT.sub(r'\1', matcher.normalize(value)).replace('_', ' ')


_FOLDED_PATTERNS = matcher.build(tuple(_fold_stretched(term) for term in TERMS))
_CMD = re.compile(r'^\s*هشدار\s*[:：]\s*')
MAX_REPORT_TEXT = 2400
MAX_INFLIGHT = 1024
DB_PATH = runtime_db_file('owner_topic_reports.sqlite3')
_tasks: set[asyncio.Task] = set()
_pending: set[tuple] = set()
_send_sem = None


def enabled():
    return str(os.getenv('OWNER_TOPIC_REPORT_ENABLED', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}


def find_topic(text):
    return (matcher.find_match(text, _PATTERNS)
            or matcher.find_match(_fold_stretched(text), _FOLDED_PATTERNS))


def _log(bot, message, *, error=False):
    try:
        logger = bot.logger
        (logger.log_error if error else logger.log_info)('OWNER TOPIC REPORT ' + message)
    except Exception:
        pass


def _db():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    conn.execute('''CREATE TABLE IF NOT EXISTS report_map (
        owner_id INTEGER NOT NULL, report_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(owner_id, report_id))''')
    return conn


def save_mapping(owner_id, report_id, group_id, message_id):
    with closing(_db()) as conn:
        with conn:
            conn.execute('INSERT OR REPLACE INTO report_map VALUES (?, ?, ?, ?, ?)',
                         (int(owner_id), int(report_id), int(group_id), int(message_id), time.time()))


def find_mapping(owner_id, report_id):
    with closing(_db()) as conn:
        row = conn.execute('SELECT group_id, source_message_id FROM report_map WHERE owner_id=? AND report_id=?',
                           (int(owner_id), int(report_id))).fetchone()
    return tuple(row) if row else None


def _u16(s):
    return len(str(s).encode('utf-16-le')) // 2


def warning_body(message):
    """Strip command prefix, preserving/clipping entity offsets in UTF-16."""
    raw = getattr(message, 'message', None) or ''
    match = _CMD.match(raw)
    if not match:
        return None
    start = match.end()
    body = raw[start:]
    shift = _u16(raw[:start])
    end = shift + _u16(body)
    entities = []
    for entity in getattr(message, 'entities', None) or ():
        try:
            lo = max(shift, int(entity.offset))
            hi = min(end, int(entity.offset) + int(entity.length))
            if hi <= lo:
                continue
            clone = copy.copy(entity)
            clone.offset, clone.length = lo - shift, hi - lo
            entities.append(clone)
        except (TypeError, ValueError, AttributeError):
            continue
    return body, entities


def _reply_id(event):
    msg = getattr(event, 'message', None)
    return (getattr(event, 'reply_to_msg_id', None)
            or getattr(getattr(event, 'reply_to', None), 'reply_to_msg_id', None)
            or getattr(msg, 'reply_to_msg_id', None)
            or getattr(getattr(msg, 'reply_to', None), 'reply_to_msg_id', None))


def _owner_private(event, sender_id):
    if not is_global_owner(sender_id):
        return False
    owner = get_owner().get('user_id')
    msg = getattr(event, 'message', None)
    peer = getattr(msg, 'peer_id', None)
    peer_user = getattr(peer, 'user_id', None)
    if peer_user is not None:
        return str(peer_user) == str(owner)
    return (getattr(event, 'is_private', False)
            and str(getattr(event, 'chat_id', '')) == str(owner))


async def handle_owner_reply(bot, event):
    """Consume only owner-private replies to a mapped report with هشدار: prefix."""
    if not enabled():
        return False
    msg = getattr(event, 'message', None)
    result = warning_body(msg) if msg is not None else None
    if result is None:
        return False
    sender_id = getattr(event, 'sender_id', None) or getattr(getattr(event, 'sender', None), 'id', None)
    if sender_id is None and getattr(event, 'is_private', False):
        try:
            sender = await asyncio.wait_for(event.get_sender(), timeout=10)
            sender_id = getattr(sender, 'id', None)
        except Exception:
            return False
    if not _owner_private(event, sender_id):
        return False
    report_id = _reply_id(event)
    if not report_id:
        return False
    try:
        mapping = await asyncio.to_thread(find_mapping, sender_id, report_id)
    except Exception as error:
        _log(bot, f'mapping lookup failed error={type(error).__name__}', error=True)
        await event.reply('⚠️ خواندن گزارش ناموفق بود؛ هشداری ارسال نشد.')
        return True
    if mapping is None:
        return False
    if not _owner_private(event, sender_id):
        return True  # Owner authority may have changed while mapping was read.
    text, entities = result
    if not text.strip():
        await event.reply('متن هشدار را بعد از «هشدار:» بنویسید.')
        return True
    from modules.manage_features.peers import group_peer
    group_id, source_id = mapping
    try:
        # The warning goes to this group only, as a reply to the source.
        # No retries: an ambiguous send failure must not duplicate notices.
        sent = await asyncio.wait_for(bot.client.send_message(
            group_peer(bot, group_id), text,
            reply_to=int(source_id), formatting_entities=entities or None,
            parse_mode=None,
        ), timeout=20)
        if sent is None:
            raise RuntimeError('warning send returned no message')
    except Exception as error:
        _log(bot, f'warning send failed group={group_id} error={type(error).__name__}', error=True)
        await event.reply('⚠️ ارسال هشدار تأیید نشد؛ ممکن است پیام حذف شده یا دسترسی ربات قطع شده باشد.')
        return True
    await event.reply('✅ هشدار به گروه ارسال شد.')
    return True


def _channel_id(event, chat):
    # The user supplied splus.ir/c/<channel_id>/<message_id>. Only construct
    # this format for a channel peer, not an ordinary chat or guessed ID.
    message = getattr(event, 'message', None)
    for peer in (getattr(message, 'peer_id', None), getattr(event, 'input_chat', None), chat):
        value = getattr(peer, 'channel_id', None)
        if value is not None:
            try:
                return int(value) if int(value) > 0 else None
            except (TypeError, ValueError):
                pass
    if getattr(event, 'is_channel', False) and getattr(event, 'is_group', False):
        try:
            from modules.group_id import normalize_group_id
            value = int(normalize_group_id(getattr(event, 'chat_id', None)))
            return value if value > 0 else None
        except (TypeError, ValueError):
            pass
    return None


def _links(event, chat, message_id):
    cid = _channel_id(event, chat)
    msg_link = f'https://splus.ir/c/{cid}/{int(message_id)}' if cid else 'در دسترس نیست'
    username = str(getattr(chat, 'username', '') or '').lstrip('@')
    group_link = f'https://splus.ir/{username}' if re.fullmatch(r'[A-Za-z0-9_]{4,}', username) else 'در دسترس نیست'
    return msg_link, group_link


def _report_payload(group, username, message, name, group_link, message_link, user_id):
    from splusthon.tl.types import MessageEntityBlockquote
    header = '🚨 گزارش کاربر'
    content = str(message)
    if len(content) > MAX_REPORT_TEXT:
        content = content[:MAX_REPORT_TEXT] + '\n… [متن کوتاه شده است]'
    text = (f'{header}\n\nگروه: {group}\n\nیوزنیم : {username}\n\n'
            f'پیام گفته شده : {content}\n\nاسم کاربر : {name}\n\n'
            f'لینک گروه : {group_link}\n\nلینک پیام : {message_link}\n\n'
            f'شناسه کاربر : {user_id}')
    return text, [MessageEntityBlockquote(offset=0, length=_u16(header))]


def _semaphore():
    global _send_sem
    if _send_sem is None:
        _send_sem = asyncio.Semaphore(3)
    return _send_sem


async def _deliver(bot, event, owner_id, chat_id, message_id, text, sender_id):
    from modules.owner_private import resolve_private_owner_peer
    try:
        async with _semaphore():
            if not is_global_owner(owner_id):
                return
            chat = getattr(event, 'chat', None)
            sender = getattr(event, 'sender', None)
            if chat is None:
                try:
                    chat = await asyncio.wait_for(event.get_chat(), timeout=10)
                except Exception:
                    pass
            if sender is None:
                try:
                    sender = await asyncio.wait_for(event.get_sender(), timeout=10)
                except Exception:
                    pass
            title = str(getattr(chat, 'title', None) or f'گروه {chat_id}')
            username = getattr(sender, 'username', None)
            username = '@' + str(username).lstrip('@') if username else 'ندارد'
            name = ' '.join(str(getattr(sender, part, '') or '').strip() for part in ('first_name', 'last_name')).strip() or 'نامشخص'
            sender_id = getattr(sender, 'id', None) or sender_id or 'نامشخص'
            message_link, group_link = _links(event, chat, message_id)
            payload, entities = _report_payload(title, username, text, name, group_link, message_link, sender_id)
            target = await asyncio.wait_for(resolve_private_owner_peer(bot.client, logger=getattr(bot, 'logger', None), context='OWNER_TOPIC_REPORT'), timeout=20)
            if target is None or not is_global_owner(owner_id):
                raise RuntimeError('owner peer unavailable or owner changed')
            sent = await asyncio.wait_for(bot.client.send_message(target, payload, formatting_entities=entities), timeout=20)
            report_id = getattr(sent, 'id', None) or getattr(sent, 'message_id', None)
            if not report_id:
                raise RuntimeError('report sent but returned message ID missing')
            await asyncio.to_thread(save_mapping, owner_id, report_id, chat_id, message_id)
            _log(bot, f'delivered group={chat_id} message={message_id} report={report_id}')
    except Exception as error:
        _log(bot, f'delivery failed group={chat_id} message={message_id} error={type(error).__name__}', error=True)


def observe(bot, event):
    """Nonblocking observer; no group-side actions or automatic punishments."""
    if not enabled() or not getattr(event, 'is_group', False) or getattr(event, 'out', False):
        return False
    msg = getattr(event, 'message', None)
    text = (getattr(msg, 'message', None) or getattr(msg, 'caption', None) or '') if msg else ''
    if not text or find_topic(text) is None:
        return False
    owner = get_owner().get('user_id')
    chat_id = getattr(event, 'chat_id', None)
    message_id = getattr(msg, 'id', None)
    if owner is None or chat_id is None or message_id is None:
        _log(bot, 'cannot queue: missing owner or source ID', error=True)
        return False
    sender_id = getattr(event, 'sender_id', None)
    own_id = getattr(bot, 'bot_account_id', None)
    if own_id is not None and sender_id is not None and str(sender_id) == str(own_id):
        return False
    key = (str(chat_id), str(message_id), text)
    if key in _pending:
        return False
    if len(_tasks) >= MAX_INFLIGHT:
        _log(bot, 'queue full; report not delivered', error=True)
        return False
    try:
        loop = asyncio.get_running_loop()
        _pending.add(key)
        async def job():
            try:
                await _deliver(bot, event, int(owner), int(chat_id), int(message_id), text, sender_id)
            finally:
                _pending.discard(key)
        task = loop.create_task(job())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except (RuntimeError, ValueError, TypeError) as error:
        _pending.discard(key)
        _log(bot, f'queue failed error={type(error).__name__}', error=True)
        return False
