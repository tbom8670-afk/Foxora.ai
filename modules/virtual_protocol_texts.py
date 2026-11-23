"""User-supplied virtual-protocol templates, with explicit UTF-16 entities."""
from modules.profile_guard.texts import _Builder, notice_bio, notice_channel, u16

try:
    from splusthon.tl.types import MessageEntityMention
except ImportError:
    class MessageEntityMention:
        def __init__(self, offset=0, length=0):
            self.offset, self.length = offset, length


def _mention(builder, value, prefix=''):
    import re
    value = str(value or '')
    builder.add(prefix, bold=True)
    offset = u16(builder._text)
    builder.add(value, bold=True)
    if re.fullmatch(r'@[A-Za-z0-9_]{4,32}', value):
        builder._entities.append(MessageEntityMention(offset=offset, length=u16(value)))


def progress(count):
    count = max(1, min(20, int(count)))
    b = _Builder()
    b.add('⚙ پروتکل روباه در حال فعال سازی', quote=True, bold=True)
    b.add('\n\n')
    b.add('20 ثانیه تا کامل شدن فعال سازی', quote=True, bold=True)
    b.add('\n')
    b.add('❏' * count, bold=True)
    return b.build()


def enabled(group_id, actor):
    b = _Builder()
    b.add('✅  پـروتـکل امـنیتی فـعال شـد....')
    rows = ['سطح امنیت : 98٪', f'گروه : {group_id}', f'روشن کننده : {actor}',
            'امنیت سختگیری : 68٪', 'اثر انگشت امنیتی : هرزنامه و ربات ها',
            'لایه ضد امنیت : 100٪', 'درصد تشخیص سریع : 100٪']
    for row in rows:
        b.add('\n\n')
        offset = u16(b._text)
        b.add(row, quote=True, bold=True)
        if row.startswith('روشن کننده : ') and str(actor).startswith('@'):
            import re
            if re.fullmatch(r'@[A-Za-z0-9_]{4,32}', str(actor)):
                b._entities.append(MessageEntityMention(offset=offset+u16('روشن کننده : '), length=u16(actor)))
    return b.build()


def disabled():
    return '🔺 پروتکل امنیتی روباه خاموش شد', []


def warning(user, kind, count, *, matched=None, members=None):
    b = _Builder()
    b.add('⚠️ : هشدار پروتکل امنیتی روباه', quote=True, bold=True)
    b.add('\n\n\n')
    _mention(b, user, '⟲ کـاربر : ')
    b.add(f'\n\n⟲ نـوع تشـخیص پـیام : {kind}\n\n', bold=True)
    if kind == 'کانال':
        b.add('⟲ نام کانال بیوگرافی : ', bold=True)
        b.add(str(matched or ''), spoiler=True)
        b.add('\n\n⟲ تعداد اعضا : ', bold=True)
        b.add(str(members) if members is not None else 'نامشخص', spoiler=True)
        b.add('\n\n')
    elif kind == 'بیوگرافی':
        b.add('⟲ کـلمه بیـوگرافی : ', bold=True)
        b.add(str(matched or ''), spoiler=True)
        b.add('\n\n')
    else:
        b.add(f'⟲ تـعداد تـکرار کـلمه : {count} بار\n\n', bold=True)
    b.add('⟲مـقام در گـروه : کاربر عادی', bold=True)
    b.add('\n\n\n')
    b.add('🔺  تـوضـیحات پـروتـکل', quote=True, bold=True)
    b.add('\n')
    if kind == 'تبلیغات':
        line = 'شما در حال ارسال تبلیغات هستید در صورت تکرار پروتکل برای شما مجازات 4 ساعت سکوت در نظر خواهد گرفت'
    elif kind == 'کانال':
        line = 'شما در حال استفاده از کانال فیلتر شده هستید در صورت تکرار پروتکل برای شما مجازات 4 ساعت سکوت در نظر خواهد گرفت'
    elif kind == 'بیوگرافی':
        line = 'شما در حال استفاده از عبارت تبلیغاتی در بیوگرافی هستید در صورت تکرار پروتکل برای شما مجازات 4 ساعت سکوت در نظر خواهد گرفت'
    else:
        line = 'شما در حال ارسال کلمات تکراری و تشخیص رفتار ربات یا تبلیغات هستید در صورت تکرار پروتکل برای شما مجازات 4 ساعت سکوت در نظر خواهد گرفت'
    b.add(line, bold=True)
    return b.build()


def punishment(user, kind, group_mode, *, matched=None, members=None, final=False):
    if kind in ('بیوگرافی', 'کانال'):
        if not final:
            if kind == 'بیوگرافی':
                return notice_bio(user, matched, duration_seconds=4*3600)
            return notice_channel(user, matched, members, duration_seconds=4*3600)
        from modules.profile_guard import texts as profile_texts
        b = _Builder()
        b.add(profile_texts.HEADER.format(user=user), bold=profile_texts.BOLD_HEADER, quote=True)
        b.add('\n' + profile_texts.BODY.format(reason=''))
        b.add(profile_texts.REASON_BIO if kind == 'بیوگرافی' else profile_texts.REASON_CHANNEL, bold=profile_texts.BOLD_REASON)
        b.add('\n\n')
        rows = ([(profile_texts.LABEL_BIO_WORD, matched or profile_texts.UNKNOWN_VALUE, True)]
                if kind == 'بیوگرافی' else [(profile_texts.LABEL_CHANNEL_NAME, matched or profile_texts.UNKNOWN_VALUE, True),
                      (profile_texts.LABEL_MEMBERS, profile_texts.format_members(members), True)])
        rows.append((profile_texts.LABEL_PUNISH, 'سکوت دائمی' if group_mode == 'mute' else 'اخراج', False))
        profile_texts._bullets(b, rows)
        return b.build()
    b = _Builder()
    header = f'🔺کاربر : {user}'
    offset = u16(b._text)
    b.add(header, quote=True, bold=True)
    import re
    if re.fullmatch(r'@[A-Za-z0-9_]{4,32}', str(user)):
        b._entities.append(MessageEntityMention(offset=offset+u16('🔺کاربر : '), length=u16(user)))
    b.add('\n\n\n')
    if final:
        b.add(f'پیام شما پاک شد | دلیل : {kind}\nمجازات: ' + ('سکوت دائمی' if group_mode == 'mute' else 'اخراج'), bold=True)
    else:
        b.add(f'پیام شما پاک شد | دلیل : {kind}\nمجازات: 4 ساعت سکوت\nپروتکل : درصورت تکرار دوباره ', bold=True)
        b.add('سکوت' if group_mode == 'mute' else 'اخراج', bold=True)
    return b.build()
