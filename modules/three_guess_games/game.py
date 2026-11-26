"""Three owner-only, thirty-second guessing games.

Each title has a separate 220-item progress history per group/user. Questions
are marked seen at start, including timeouts, just like emoji guess. No data is
synthesized at runtime. A completed bank stops until new stages are added.
"""
from __future__ import annotations

import random
import re
import time
import unicodedata
import uuid
from pathlib import Path

from economy import award_game, game_progress
from modules.group_id import normalize_group_id

TIME_LIMIT = 30
GAMES = {
    'سرنخ': ('clue_guess', '🔍 سرنخ', 4),
    'سه سرنخ': ('clue_guess', '🔍 سرنخ', 4),
    'کلمه ناقص': ('missing_word', '♟️ کلمه ناقص', 4),
    'کلمه مرتبط': ('related_word', '🦊 کلمه مرتبط', 5),
}
ACTIVE = {}
_RNG = random.SystemRandom()


def _load_bank():
    rows = []
    for line in (Path(__file__).parent / 'concepts.txt').read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        answer, description, clues, aliases = (part.strip() for part in line.split('|'))
        clues = tuple(part.strip() for part in clues.split('/') if part.strip())
        aliases = tuple(part.strip() for part in aliases.split('،') if part.strip())
        if not answer or not description or len(clues) != 3:
            raise ValueError('invalid three-guess content row')
        rows.append({'answer': answer, 'description': description, 'clues': clues, 'aliases': aliases})
    if len(rows) != 220 or len({_norm(row['answer']) for row in rows}) != 220:
        raise ValueError('three-guess bank must contain 220 unique answers')
    return tuple(rows)


def _norm(value):
    value = unicodedata.normalize('NFKC', str(value or '')).lower()
    value = value.translate(str.maketrans({'ي': 'ی', 'ى': 'ی', 'ك': 'ک', 'ة': 'ه', 'ۀ': 'ه', 'أ': 'ا', 'إ': 'ا', 'آ': 'ا'}))
    value = re.sub(r'[\u064b-\u065f\u0670\u0640\u200b\u200c\u200d\u200e\u200f\ufeff]', '', value)
    return ''.join(c for c in value if c.isalnum())


BANK = _load_bank()


def _key(chat_id, user_id):
    return str(normalize_group_id(chat_id)), str(user_id)


def current(chat_id, user_id):
    return ACTIVE.get(_key(chat_id, user_id))


def active(chat_id, user_id=None):
    if user_id is not None:
        return current(chat_id, user_id) is not None
    chat = str(normalize_group_id(chat_id))
    return any(k[0] == chat for k in ACTIVE)


def _masked(answer, seed):
    # One dash per missing letter; whitespace and ZWNJ are not letters.
    value = unicodedata.normalize('NFC', answer).replace('\u200c', ' ')
    positions = [i for i, c in enumerate(value) if c.isalpha()]
    count = 1 if len(positions) < 6 else 2 if len(positions) < 11 else 3
    chosen = set(random.Random(seed).sample(positions[1:-1] or positions, count))
    return ' / '.join(' '.join('ـ' if i in chosen else c for i, c in enumerate(value) if start <= i < end)
                      for start, end in [(m.start(), m.end()) for m in re.finditer(r'\S+', value)])


def payload(state):
    """Screenshot layout: one real quote entity around header only."""
    from splusthon.tl.types import MessageEntityBlockquote
    header = state['header']
    item = state['item']
    if state['game'] == 'clue_guess':
        body = f"{item['description']}\nمن چی هستم؟؟"
    elif state['game'] == 'missing_word':
        body = f"{item['description']}\n{_masked(item['answer'], state['index'])}\nکلمه ناقص رو با سرنخ کامل کن"
    else:
        body = f"{' / '.join(item['clues'])}\nیک کلمه مرتبط با اینا بگو"
    text = f'{header}\n\n{body}\n⏰ ۳۰ ثانیه مهلت'
    length = len(header.encode('utf-16-le')) // 2
    return text, [MessageEntityBlockquote(offset=0, length=length)]


def start(chat_id, user_id, command):
    game, header, reward = GAMES[command]
    key = _key(chat_id, user_id)
    if key in ACTIVE:
        return None
    seen = game_progress.seen(chat_id, user_id, game)
    if len(seen) >= len(BANK):
        return {'kind': 'finished', 'game': game}
    remaining = [(i, row) for i, row in enumerate(BANK) if str(i) not in seen]
    # Avoid simultaneous identical answers in the same group.
    used = {state['index'] for (chat, _), state in ACTIVE.items() if chat == key[0]}
    options = [entry for entry in remaining if entry[0] not in used]
    if not options:
        return {'kind': 'busy', 'game': game}
    index, item = _RNG.choice(options)
    stage = len(seen) + 1
    state = {'kind': 'started', 'game': game, 'header': header, 'reward': reward,
             'index': index, 'item': item, 'stage': stage, 'token': uuid.uuid4().hex,
             'deadline': 0.0, 'prompt_id': None, 'published': False}
    ACTIVE[key] = state
    return dict(state)


def bind_prompt(chat_id, user_id, token, message_id):
    state = current(chat_id, user_id)
    if state and state['token'] == token and not state['published']:
        game_progress.mark_seen(chat_id, user_id, state['game'], str(state['index']))
        state['prompt_id'] = message_id
        state['published'] = True
        state['deadline'] = time.monotonic() + TIME_LIMIT
        return True
    return False


def answer(chat_id, user_id, text, *, name=None, reply_id=None):
    key = _key(chat_id, user_id)
    state = ACTIVE.get(key)
    if not state or not state['published'] or time.monotonic() >= state['deadline']:
        return None
    # Replying to another player's prompt must not solve your own round.
    if reply_id is not None:
        for (chat, owner), other in ACTIVE.items():
            if chat == key[0] and owner != key[1] and other['prompt_id'] is not None and str(other['prompt_id']) == str(reply_id):
                return None
    if reply_id is not None and state['prompt_id'] is not None:
        if str(reply_id) != str(state['prompt_id']):
            return None
    values = [state['item']['answer']]
    if state['game'] != 'missing_word':
        values.extend(state['item']['aliases'])
    if _norm(text) not in {_norm(value) for value in values}:
        return None
    # Consume before paying, so rapid duplicate updates cannot double reward.
    ACTIVE.pop(key, None)
    try:
        balance = award_game(chat_id, user_id, state['game'],
                             reference=f"guess:{state['game']}:{state['token']}", name=name)
        error = False
    except Exception:
        balance, error = None, True
    return {'answer': state['item']['answer'], 'reward': state['reward'],
            'balance': balance, 'payment_error': error, 'game': state['game'], 'stage': state['stage']}


def finish(chat_id, user_id, token):
    state = current(chat_id, user_id)
    if not state or state['token'] != token:
        return None
    ACTIVE.pop(_key(chat_id, user_id), None)
    return state['item']['answer']
