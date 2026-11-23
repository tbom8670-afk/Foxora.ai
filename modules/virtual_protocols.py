"""Per-group behavioral protocol and staged moderation.

Scores are heuristic evidence, not proof of account ownership or intent.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import json
import logging
import os
import sqlite3
import time
import unicodedata
import uuid
import weakref

from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file, runtime_db_file
from modules.ad_spam_guard import patterns

LOG = logging.getLogger(__name__)
WINDOW = 86400
MAX_TEXT = 1024
MAX_PENDING = 512
MAX_USER_MESSAGES = 200
QUEUE_TTL = 120
# Scores are heuristic, not calibrated probabilities or identity judgements.
WEIGHTS = {"repeat": 25, "burst": 15, "reply_repeat": 15,
           "same_reply_target": 10, "ad_repeat": 15, "coordination": 20,
           "conversation": -20, "cross_group": 15, "channel_repeat": 15,
           "history": 5, "bio_confirmed": 10, "manager_feedback": -30,
           "cycle": 20, "cycle_burst": 10, "periodic": 10}
THRESHOLDS = {"review": 25, "suspicious": 45, "strong": 70}


def normalize(text):
    # NFKC, Persian letter folding, then ignore spacing/punctuation/emoji.
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    s = s.translate(str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "آ": "ا", "أ": "ا", "إ": "ا"}))
    return "".join(c for c in s if c.isalnum())


def similar(a, b):
    if not a or not b or min(len(a), len(b)) < 6:
        return False
    if a == b:
        return True
    if max(len(a), len(b)) > 256:
        return False  # Long texts are compared exactly to bound CPU and avoid prefix matches.
    if abs(len(a) - len(b)) > max(len(a), len(b)) * .18:
        return False
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.quick_ratio() >= .88 and matcher.ratio() >= .88


def channel_signal(text):
    """Use the existing profile-guard lists and matcher; no copied word database."""
    from modules.profile_guard import filters, matcher
    phrases = matcher.compile_phrases(filters.channel_names() + filters.bio_sentences())
    return bool(matcher.find_match(text, phrases))


def cycle_size(sequence):
    for n in range(2, 6):
        if len(sequence) < n * 2:
            continue
        first, second = sequence[-2*n:-n], sequence[-n:]
        if len(set(first)) >= 2 and all(similar(a,b) for a,b in zip(first,second)):
            return n
    return 0


def regular_interval(timestamps):
    values = sorted(timestamps)
    if len(values) < 5:
        return False
    gaps = [b-a for a,b in zip(values, values[1:])]
    mean = sum(gaps)/len(gaps)
    return 5 <= mean <= 3600 and max(abs(g-mean) for g in gaps)/mean <= .12


def ad_signal(text):
    value = patterns.normalize(text)
    return any(patterns.normalize(p) in value for p in
               patterns.TEXT_MARKERS + patterns.SPAM_PHRASES + patterns.AD_CTA_PHRASES
               if patterns.normalize(p))


def load_policy(path):
    """Explicit administrator-controlled federation and scoring policy."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    weights = dict(WEIGHTS)
    overrides = data.get("weights")
    if not isinstance(overrides, dict):
        overrides = {}
    for key, value in overrides.items():
        if key in weights and type(value) is int and -100 <= value <= 100:
            weights[key] = value
    thresholds = dict(THRESHOLDS)
    supplied = data.get("thresholds") or {}
    if isinstance(supplied, dict):
        candidate = {k: supplied.get(k, v) for k, v in thresholds.items()}
        if (all(type(v) is int and 0 <= v <= 1000 for v in candidate.values())
                and candidate["review"] <= candidate["suspicious"] <= candidate["strong"]):
            thresholds = candidate
    federation = []
    pools = data.get("federation")
    if not isinstance(pools, list):
        pools = []
    for pool in pools[:20]:
        if isinstance(pool, list) and 2 <= len(pool) <= 20:
            ids = {normalize_group_id(x) for x in pool if type(x) in (int, str) and str(x).lstrip('-').isdigit()}
            if len(ids) >= 2:
                federation.append(ids)
    return weights, thresholds, federation


def history_snapshot(group, user, now):
    """Best-effort read of existing records, without mutating their stores.

    Missing data means unknown, not proof of release or innocence.  Existing
    history doesn't record the actor/release status for old entries.
    """
    result = {"status": "partial", "records": [], "manual_mute_active": None,
              "ban_records": [], "release_status": "unknown", "relevant_active": False}
    try:
        from modules import user_history
        data = user_history._load()
        for gid, users in data.items():
            if normalize_group_id(gid) != group or not isinstance(users, dict):
                continue
            entry = users.get(user, {})
            records = entry.get("records", []) if isinstance(entry, dict) else []
            result["records"].extend(r for r in records if isinstance(r, dict)
                and isinstance(r.get("_ts"), (float, int)) and now-WINDOW <= r["_ts"] <= now)
        result["records"] = result["records"][-50:]
    except Exception:
        result["history_error"] = True
    try:
        from modules import manual_mute_storage
        raw = json.loads(manual_mute_storage.FILE.read_text(encoding="utf-8")) if manual_mute_storage.FILE.exists() else {}
        entries = raw.get(group, []) if isinstance(raw, dict) else []
        result["manual_mute_active"] = any(isinstance(e, dict) and str(e.get("user_id")) == user for e in entries)
    except (OSError, ValueError):
        result["manual_mute_active"] = None
    try:
        from modules import banned_storage
        records = banned_storage.get_matching_ban_records(group, user)
        # Exact user IDs only, never aliases or display names.
        result["ban_records"] = [r for r in records if isinstance(r, dict) and str(r.get("user_id")) == user][:10]
    except Exception:
        result["ban_error"] = True
    terms = ("اسپم", "تبلیغ", "هرزنامه", "spam", "advert")
    active = result["manual_mute_active"] or bool(result["ban_records"])
    relevant = any(any(t in str(r.get("reason", "")).lower() for t in terms)
                   for r in result["records"] + result["ban_records"])
    result["relevant_active"] = bool(active and relevant)
    return result


def risk_level(signals, score, thresholds):
    positives = sum(v for k, v in signals.items() if k not in ("conversation", "manager_feedback"))
    if positives >= 4 and score >= thresholds["strong"]:
        return "strong"
    if positives >= 3 and score >= thresholds["suspicious"]:
        return "suspicious"
    if positives >= 2 and score >= thresholds["review"]:
        return "review"
    return "natural"


@dataclass(frozen=True)
class Job:
    group: str
    user: str
    message: int
    text: str
    reply: int | None
    timestamp: float
    generation: str
    ad: bool | None = None
    channel: bool | None = None


def content_signals(text):
    return ad_signal(text), channel_signal(text)


class Store:
    def __init__(self, path):
        self.path = path
        self.last_cleanup = 0.0

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=3)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS vp_messages (
            group_id TEXT NOT NULL, generation TEXT NOT NULL, user_id TEXT NOT NULL,
            message_id INTEGER NOT NULL, ts REAL NOT NULL, text TEXT NOT NULL,
            normalized TEXT NOT NULL, reply_id INTEGER, ad INTEGER NOT NULL,
            PRIMARY KEY(group_id, generation, message_id));
          CREATE INDEX IF NOT EXISTS vp_user_time ON vp_messages(group_id,generation,user_id,ts);
          CREATE INDEX IF NOT EXISTS vp_group_time ON vp_messages(group_id,generation,ts);
          CREATE INDEX IF NOT EXISTS vp_user_global ON vp_messages(user_id,ts);
          CREATE INDEX IF NOT EXISTS vp_reply ON vp_messages(group_id,generation,reply_id,ts);
          CREATE TABLE IF NOT EXISTS vp_evidence (
            group_id TEXT NOT NULL, generation TEXT NOT NULL, user_id TEXT NOT NULL,
            message_id INTEGER NOT NULL, ts REAL NOT NULL, score INTEGER NOT NULL,
            level TEXT NOT NULL, evidence TEXT NOT NULL,
            PRIMARY KEY(group_id,generation,message_id));
          CREATE INDEX IF NOT EXISTS vp_evidence_time ON vp_evidence(ts);
          CREATE TABLE IF NOT EXISTS vp_pending (
            group_id TEXT NOT NULL, generation TEXT NOT NULL, user_id TEXT NOT NULL,
            message_id INTEGER NOT NULL, ts REAL NOT NULL, text TEXT NOT NULL,
            reply_id INTEGER, PRIMARY KEY(group_id,generation,message_id));
          CREATE INDEX IF NOT EXISTS vp_pending_time ON vp_pending(ts);
          CREATE TABLE IF NOT EXISTS vp_feedback (
            id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
            actor_id TEXT NOT NULL, action TEXT NOT NULL, ts REAL NOT NULL,
            source TEXT NOT NULL, reason TEXT, linked_protocol INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS vp_feedback_user ON vp_feedback(group_id,user_id,ts);
          CREATE TABLE IF NOT EXISTS vp_actions (
            group_id TEXT NOT NULL, generation TEXT NOT NULL, user_id TEXT NOT NULL,
            message_id INTEGER NOT NULL, ts REAL NOT NULL, status TEXT NOT NULL,
            reason TEXT NOT NULL, warning_count INTEGER, source TEXT NOT NULL, penalty_kind TEXT,
            punished_at REAL, until_ts REAL, released_at REAL,
            PRIMARY KEY(group_id,generation,message_id));
          CREATE INDEX IF NOT EXISTS vp_actions_user ON vp_actions(group_id,user_id,ts);
          CREATE TABLE IF NOT EXISTS vp_cycles (
            group_id TEXT NOT NULL, user_id TEXT NOT NULL, stage INTEGER NOT NULL,
            last_activity REAL NOT NULL, last_decision REAL NOT NULL,
            PRIMARY KEY(group_id,user_id));
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(vp_actions)")}
        for name, sql_type in (("penalty_kind", "TEXT"), ("punished_at", "REAL"), ("until_ts", "REAL"), ("released_at", "REAL"), ("stage", "INTEGER")):
            if name not in columns:
                db.execute("ALTER TABLE vp_actions ADD COLUMN " + name + " " + sql_type)
        db.commit()
        return db

    def stage(self, job):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM vp_pending WHERE ts < ?", (time.time()-QUEUE_TTL,))
            exists = db.execute("SELECT 1 FROM vp_pending WHERE group_id=? AND generation=? AND message_id=?", (job.group,job.generation,job.message)).fetchone()
            if exists:
                return True
            if db.execute("SELECT COUNT(*) FROM vp_pending").fetchone()[0] >= MAX_PENDING:
                return False
            db.execute("INSERT INTO vp_pending VALUES(?,?,?,?,?,?,?)",
                       (job.group, job.generation, job.user, job.message, job.timestamp, job.text, job.reply))
            return True

    def acknowledge(self, job):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM vp_pending WHERE group_id=? AND generation=? AND message_id=?",
                       (job.group,job.generation,job.message))

    def recover(self, groups):
        if not self.path.exists() or not groups:
            return []
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM vp_pending WHERE ts < ?", (now-QUEUE_TTL,))
            rows = db.execute("SELECT group_id,generation,user_id,message_id,ts,text,reply_id FROM vp_pending ORDER BY ts LIMIT ?", (MAX_PENDING,)).fetchall()
        return [Job(g,u,m,text,reply,ts,gen) for g,gen,u,m,ts,text,reply in rows
                if groups.get(g) == gen and 0 <= now-ts <= QUEUE_TTL]

    def maintain(self):
        if not self.path.exists():
            return
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM vp_pending WHERE ts < ?", (now-QUEUE_TTL,))
            db.execute("DELETE FROM vp_messages WHERE ts < ?", (now-WINDOW,))
            db.execute("DELETE FROM vp_evidence WHERE ts < ?", (now-7*WINDOW,))
            db.execute("DELETE FROM vp_feedback WHERE ts < ?", (now-30*WINDOW,))
            db.execute("DELETE FROM vp_actions WHERE ts < ?", (now-30*WINDOW,))
            db.execute("DELETE FROM vp_cycles WHERE last_activity < ?", (now-WINDOW,))
            db.execute("DELETE FROM vp_actions WHERE rowid IN (SELECT rowid FROM vp_actions ORDER BY ts DESC LIMIT -1 OFFSET 20000)")
            db.execute("DELETE FROM vp_feedback WHERE rowid IN (SELECT rowid FROM vp_feedback ORDER BY ts DESC LIMIT -1 OFFSET 20000)")
            db.execute("DELETE FROM vp_messages WHERE rowid IN (SELECT rowid FROM vp_messages ORDER BY ts DESC LIMIT -1 OFFSET 100000)")
            db.execute("DELETE FROM vp_evidence WHERE rowid IN (SELECT rowid FROM vp_evidence ORDER BY ts DESC LIMIT -1 OFFSET 20000)")

    def reserve_stage(self, job, evidence):
        """Reserve one stage without claiming success for a failed RPC."""
        now = time.time()
        with closing(self.connect()) as db, db:
            # An interrupted prior action has an unknown remote outcome.
            db.execute("UPDATE vp_actions SET status='uncertain' WHERE group_id=? AND user_id=? AND status IN ('reserved','punishment_enqueued') AND ts<?", (job.group,job.user,now-QUEUE_TTL))
            inflight = db.execute("SELECT 1 FROM vp_actions WHERE group_id=? AND user_id=? AND status IN ('reserved','punishment_enqueued') AND ts>=? LIMIT 1", (job.group,job.user,now-QUEUE_TTL)).fetchone()
            uncertain = db.execute("SELECT 1 FROM vp_actions WHERE group_id=? AND user_id=? AND status='uncertain' AND ts>=? LIMIT 1", (job.group,job.user,now-WINDOW)).fetchone()
            row = db.execute("SELECT stage,last_activity,last_decision FROM vp_cycles WHERE group_id=? AND user_id=?", (job.group,job.user)).fetchone()
            current = int(row[0]) if row and job.timestamp-row[1] <= WINDOW else 0
            if row and job.timestamp > row[1] and current:
                db.execute("UPDATE vp_cycles SET last_activity=? WHERE group_id=? AND user_id=?", (job.timestamp,job.group,job.user))
            if inflight or uncertain or (row and job.timestamp <= row[2]) or current >= 3:
                return None
            feedback = db.execute("SELECT MAX(ts) FROM vp_feedback WHERE group_id=? AND user_id=? AND linked_protocol=1 AND action IN ('false_positive','unmute','unban') AND ts>=?", (job.group,job.user,now-30*WINDOW)).fetchone()[0]
            if feedback is not None:
                fresh = db.execute("SELECT normalized,reply_id,ad,ts FROM vp_messages WHERE group_id=? AND generation=? AND user_id=? AND ts>? AND ts<=? ORDER BY ts DESC LIMIT 200", (job.group,job.generation,job.user,feedback,job.timestamp)).fetchall()
                matching = [r for r in fresh if similar(normalize(job.text),r[0])]
                if (len(matching) < 6 or sum(job.timestamp-r[3] <= 300 for r in matching) < 4
                        or max(sum(r[1] is not None for r in matching), sum(bool(r[2]&3) for r in matching)) < 4):
                    return None
            stage = current + 1
            reasons = json.dumps([k for k,v in evidence['signals'].items() if v and WEIGHTS.get(k,0)>0], ensure_ascii=False)
            inserted = db.execute("INSERT OR IGNORE INTO vp_actions(group_id,generation,user_id,message_id,ts,status,reason,source,stage) VALUES(?,?,?,?,?,'reserved',?,'virtual_protocols',?)", (job.group,job.generation,job.user,job.message,now,reasons,stage)).rowcount
            return stage if inserted else None

    def confirm_stage(self, job, stage, *, kind=None, until=None):
        now = time.time()
        status = 'warned' if stage == 1 else 'punished'
        with closing(self.connect()) as db, db:
            db.execute("UPDATE vp_actions SET status=?,penalty_kind=?,punished_at=?,until_ts=? WHERE group_id=? AND generation=? AND message_id=?", (status,kind,now if stage > 1 else None,until,job.group,job.generation,job.message))
            db.execute("INSERT INTO vp_cycles(group_id,user_id,stage,last_activity,last_decision) VALUES(?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET stage=excluded.stage,last_activity=MAX(vp_cycles.last_activity,excluded.last_activity),last_decision=excluded.last_decision", (job.group,job.user,stage,job.timestamp,now))

    def action_status(self, job, status, count=None, penalty_kind=None, punished_at=None, until_ts=None):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE vp_actions SET status=?,warning_count=COALESCE(?,warning_count),penalty_kind=COALESCE(?,penalty_kind),punished_at=COALESCE(?,punished_at),until_ts=COALESCE(?,until_ts) WHERE group_id=? AND generation=? AND message_id=? AND status IN ('reserved','punishment_enqueued')", (status,count,penalty_kind,punished_at,until_ts,job.group,job.generation,job.message))

    def mark_false_positive(self, group, message_id, actor):
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT user_id FROM vp_evidence WHERE group_id=? AND message_id=? ORDER BY ts DESC LIMIT 1", (group, message_id)).fetchone()
            if row is None:
                return False
            db.execute("INSERT INTO vp_feedback(group_id,user_id,actor_id,action,ts,source,reason,linked_protocol) VALUES(?,?,?,?,?,?,?,1)",
                       (group,row[0],actor,"false_positive",time.time(),"manager","confirmed_false_positive"))
            return True

    def record_release(self, group, user, actor, action):
        with closing(self.connect()) as db, db:
            kinds = ("mute",) if action == "unmute" else ("ban", "kick")
            marks = ",".join("?" for _ in kinds)
            row = db.execute("SELECT generation,message_id FROM vp_actions WHERE group_id=? AND user_id=? AND status='punished' AND penalty_kind IN (" + marks + ") ORDER BY ts DESC LIMIT 1", (group,user,*kinds)).fetchone()
            linked = bool(row)
            db.execute("INSERT INTO vp_feedback(group_id,user_id,actor_id,action,ts,source,reason,linked_protocol) VALUES(?,?,?,?,?,?,?,?)",
                       (group,user,actor,action,time.time(),"manager",None,int(linked)))
            if linked:
                db.execute("UPDATE vp_actions SET status='released',released_at=? WHERE group_id=? AND generation=? AND message_id=?", (time.time(),group,row[0],row[1]))

    def update_bio(self, job, status, about, weights, thresholds, cached=None, profile=None):
        from modules import bio_link
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT evidence FROM vp_evidence WHERE group_id=? AND generation=? AND message_id=?", (job.group,job.generation,job.message)).fetchone()
            if not row:
                return
            evidence = json.loads(row[0])
            bio = {"status": str(status), "link": None, "ad_signal": None, "channel_signal": None, "confirmed": False, "matched": None}
            if cached is not None:
                bio = dict(cached)
                bio["confirmed"] = bool((evidence["ad_signal"] and (bio.get("ad_signal") or bio.get("link") or bio.get("matched"))) or (evidence["channel_signal"] and (bio.get("channel_signal") or bio.get("matched"))))
            elif status == "ok" and about:
                link = bool(bio_link.analyze(about, detect_username=True)[0])
                ad = ad_signal(about)
                channel = channel_signal(about)
                from modules.profile_guard import filters, matcher
                match = matcher.find_match(about, matcher.compile_phrases(filters.bio_sentences()))
                matched = match.raw if match is not None else None
                confirmed = ((evidence["ad_signal"] and (ad or link or bool(matched))) or
                             (evidence["channel_signal"] and (channel or bool(matched))))
                bio.update(link=link, ad_signal=ad, channel_signal=channel, confirmed=bool(confirmed), matched=matched)
            if profile and evidence.get('channel_signal'):
                from modules.profile_guard import filters, matcher
                title = profile.get('channel_title')
                found = matcher.find_match(title, matcher.compile_phrases(filters.channel_names())) if title else None
                if found:
                    bio['channel_profile_confirmed'] = True
                    bio['channel_title'] = str(title)[:256]
                    bio['channel_members'] = profile.get('participants')
                    bio['confirmed'] = True
            if cached and bio.get('channel_profile_confirmed') and not evidence.get('channel_signal'):
                bio['channel_profile_confirmed'] = False
            evidence["bio"] = bio
            evidence["signals"]["bio_confirmed"] = bio["confirmed"]
            score = max(0, sum(weights[k] for k, v in evidence["signals"].items() if v))
            level = risk_level(evidence["signals"], score, thresholds)
            db.execute("UPDATE vp_evidence SET score=?,level=?,evidence=? WHERE group_id=? AND generation=? AND message_id=?", (score,level,json.dumps(evidence,ensure_ascii=False),job.group,job.generation,job.message))
            return level, score, evidence

    def reports(self, group, limit=50):
        if not self.path.exists():
            return []
        with closing(self.connect()) as db:
            rows = db.execute("SELECT e.user_id,e.message_id,e.ts,e.score,e.level,e.evidence,a.status,a.reason,a.warning_count,a.penalty_kind,a.source,a.punished_at,a.until_ts,a.released_at FROM vp_evidence e LEFT JOIN vp_actions a ON e.group_id=a.group_id AND e.generation=a.generation AND e.message_id=a.message_id WHERE e.group_id=? ORDER BY e.ts DESC LIMIT ?", (group, min(100,max(1,int(limit))))).fetchall()
            result = []
            for u,m,ts,score,level,e,status,reason,count,kind,source,pat,until,rat in rows:
                feedback = db.execute("SELECT actor_id,action,ts,reason,linked_protocol FROM vp_feedback WHERE group_id=? AND user_id=? ORDER BY ts DESC LIMIT 10", (group,u)).fetchall()
                result.append({"user_id": u, "message_id": m, "time": ts, "score": score,
                    "level": level, "evidence": json.loads(e),
                    "action": {"status": status, "reasons": json.loads(reason) if reason else [], "warning_count": count, "penalty_kind": kind, "source": source, "punished_at": pat, "until": until, "released_at": rat},
                    "manager_feedback": [{"actor_id": a, "action": act, "time": fts, "reason": why, "linked_protocol": bool(linked)} for a,act,fts,why,linked in feedback]})
            return result

    def process(self, job, peers=None, weights=None, thresholds=None, history=None):
        weights = weights or WEIGHTS
        thresholds = thresholds or THRESHOLDS
        history = history or {}
        if not normalize(job.text):
            return None
        now = time.time()
        if now - job.timestamp > QUEUE_TTL or job.timestamp > now + 60:
            return None
        with closing(self.connect()) as db, db:
            # A replay must not increment features or generate a second report.
            if db.execute("SELECT 1 FROM vp_messages WHERE group_id=? AND generation=? AND message_id=?", (job.group, job.generation, job.message)).fetchone():
                return None
            params = (job.group, job.generation, job.user, job.timestamp-WINDOW, job.timestamp)
            rows = db.execute("SELECT message_id,ts,text,normalized,reply_id,ad FROM vp_messages WHERE group_id=? AND generation=? AND user_id=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?", (*params, MAX_USER_MESSAGES)).fetchall()
            norm = normalize(job.text)
            matches = [row for row in rows if similar(norm, row[3])]
            recent = [r for r in matches if job.timestamp-r[1] <= 300]
            reply_matches = [r for r in matches if r[4] is not None]
            same_target = [r for r in reply_matches if job.reply is not None and r[4] == job.reply]
            ad = job.ad if job.ad is not None else ad_signal(job.text)
            channel = job.channel if job.channel is not None else channel_signal(job.text)
            repeated_ad = ad and sum(bool(r[5] & 1) for r in rows) >= 2
            repeated_channel = channel and sum(bool(r[5] & 2) for r in rows) >= 2
            chronological = list(reversed(rows))[-19:]
            cycle = cycle_size([r[3] for r in chronological] + [norm])
            cycle_fast = bool(cycle and job.timestamp-chronological[-(2*cycle-1)][1] <= 300)
            periodic = regular_interval([r[1] for r in matches[:9]] + [job.timestamp])
            replies = ([job.reply] if job.reply is not None else []) + [r[4] for r in rows if r[4] is not None]
            target_users = set()
            if replies:
                marks = ','.join('?' for _ in replies[:200])
                target_users = {r[0] for r in db.execute('SELECT DISTINCT user_id FROM vp_messages WHERE group_id=? AND generation=? AND message_id IN ('+marks+') AND user_id!=?', (job.group,job.generation,*replies[:200],job.user)).fetchall()}
            # Cross-group comparisons require explicit federation AND currently
            # enabled generations for every participating group.
            others = [(g, gen) for g, gen in (peers or {}).items() if g != job.group]
            cross_rows = []
            if others:
                clauses = ' OR '.join('(group_id=? AND generation=?)' for _ in others)
                args = [v for pair in others for v in pair]
                cross_rows = db.execute(
                    'SELECT group_id,normalized FROM vp_messages WHERE user_id=? AND ts>=? AND ts<=? AND (' + clauses + ') ORDER BY ts DESC LIMIT 200',
                    (job.user,job.timestamp-WINDOW,job.timestamp,*args)).fetchall()
            cross_matches = [g for g,n in cross_rows if similar(norm,n)]
            cross_group = len(cross_matches) >= 2 and len(matches) >= 1
            # Count genuinely distinct other senders replying to a known message
            # of this user, not the number of replies made by the user.
            incoming = db.execute("""SELECT COUNT(DISTINCT r.user_id) FROM vp_messages r
                JOIN vp_messages p ON r.group_id=p.group_id AND r.generation=p.generation AND r.reply_id=p.message_id
                WHERE p.group_id=? AND p.generation=? AND p.user_id=? AND r.user_id!=? AND r.ts>=? AND r.ts<=?""", (job.group,job.generation,job.user,job.user,job.timestamp-WINDOW,job.timestamp)).fetchone()[0]
            distinct = len({r[3] for r in rows if r[3]})
            conversation = incoming >= 2 and len(rows) >= 4 and distinct / len(rows) >= .7
            # Similarity among two users once is not coordination: require
            # at least three matching messages, and at least two other users.
            nearby = db.execute("SELECT user_id,normalized FROM vp_messages WHERE group_id=? AND generation=? AND user_id!=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 300", (job.group,job.generation,job.user,job.timestamp-300,job.timestamp)).fetchall()
            related = [u for u, n in nearby if similar(norm, n)]
            coord_groups = set()
            if others:
                cross_nearby = db.execute(
                    'SELECT group_id,user_id,normalized FROM vp_messages WHERE user_id!=? AND ts>=? AND ts<=? AND (' + clauses + ') ORDER BY ts DESC LIMIT 200',
                    (job.user,job.timestamp-300,job.timestamp,*args)).fetchall()
                for other_group, other_user, other_text in cross_nearby:
                    if similar(norm, other_text):
                        related.append(other_user)
                        coord_groups.add(other_group)
            coordinated = len(related) >= 3 and len(set(related)) >= 2 and len(matches) >= 2
            signals = {"repeat": len(matches) >= 3,
                       "burst": len(recent) >= 3,
                       "reply_repeat": job.reply is not None and len(reply_matches) >= 3,
                       "same_reply_target": job.reply is not None and len(same_target) >= 3,
                       "ad_repeat": repeated_ad, "coordination": coordinated,
                       "channel_repeat": repeated_channel, "cross_group": cross_group,
                       "history": bool(history.get("relevant_active")),
                       "bio_confirmed": False, "manager_feedback": False, "conversation": conversation,
                       "cycle": bool(cycle), "cycle_burst": cycle_fast, "periodic": periodic}
            # Fetch existing moderation history only after multiple behavioral
            # indicators, never use old or unknown status as a standalone verdict.
            if sum(v for k, v in signals.items() if k not in ("history", "conversation")) >= 2 and not history:
                history = history_snapshot(job.group, job.user, job.timestamp)
                signals["history"] = bool(history.get("relevant_active"))
            feedback = db.execute("SELECT actor_id,action,ts,reason,linked_protocol FROM vp_feedback WHERE group_id=? AND user_id=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 10", (job.group,job.user,job.timestamp-30*WINDOW,job.timestamp)).fetchall()
            signals["manager_feedback"] = any(act in ("false_positive", "unmute", "unban") and linked for _a,act,_ts,_reason,linked in feedback)
            if history:
                past_actions = db.execute("SELECT status,penalty_kind,reason,punished_at,until_ts,released_at,source FROM vp_actions WHERE group_id=? AND user_id=? AND ts<=? ORDER BY ts DESC LIMIT 10", (job.group,job.user,job.timestamp)).fetchall()
                history["protocol_actions"] = [{"status": status, "kind": kind, "reasons": json.loads(reason), "punished_at": pat, "until": until, "released_at": rat, "source": source} for status,kind,reason,pat,until,rat,source in past_actions]
                history["manager_feedback"] = [{"actor_id": a, "action": act, "time": ts, "reason": reason, "linked_protocol": bool(linked)} for a,act,ts,reason,linked in feedback]
            score = max(0, sum(weights[k] for k, active in signals.items() if active))
            level = risk_level(signals, score, thresholds)
            reply_row = None
            if job.reply is not None:
                reply_row = db.execute("SELECT user_id,text FROM vp_messages WHERE group_id=? AND generation=? AND message_id=?", (job.group,job.generation,job.reply)).fetchone()
            evidence = {"text": job.text, "previous_text": matches[0][2] if matches else None,
                        "repeat_count": len(matches)+1, "window_seconds": WINDOW,
                        "repeat_span_seconds": job.timestamp-min([r[1] for r in matches], default=job.timestamp),
                        "reply_id": job.reply, "reply_text": reply_row[1] if reply_row else None,
                        "reply_user_id": reply_row[0] if reply_row else None,
                        "incoming_distinct_users": incoming, "conversation_indicator": conversation,
                        "outgoing_distinct_users": len(target_users),
                        "reply_only": bool(job.reply is not None and rows and all(r[4] is not None for r in rows)),
                        "cycle_size": cycle,
                        "coordinated_user_ids": sorted(set(related)) if coordinated else [],
                        "coordination_groups": sorted(coord_groups) if coordinated else [],
                        "ad_signal": ad, "channel_signal": channel, "signals": signals,
                        "bio": {"status": "not_checked"}, "history": history or {"status": "not_checked"},
                        "cross_group": {"status": "checked" if others else "not_authorized", "matching_groups": sorted(set(cross_matches)), "matches": len(cross_matches)}, "decision": "evidence_recorded"}
            db.execute("INSERT INTO vp_messages VALUES(?,?,?,?,?,?,?,?,?)", (job.group,job.generation,job.user,job.message,job.timestamp,job.text,norm,job.reply,int(ad) | (int(channel) << 1)))
            if level != "natural":
                db.execute("INSERT INTO vp_evidence VALUES(?,?,?,?,?,?,?,?)", (job.group,job.generation,job.user,job.message,job.timestamp,score,level,json.dumps(evidence,ensure_ascii=False)))
            # Bound individual profiles even under a sustained flood.
            db.execute("DELETE FROM vp_messages WHERE group_id=? AND generation=? AND user_id=? AND message_id NOT IN (SELECT message_id FROM vp_messages WHERE group_id=? AND generation=? AND user_id=? ORDER BY ts DESC LIMIT ?)", (job.group,job.generation,job.user,job.group,job.generation,job.user,MAX_USER_MESSAGES))
            if now-self.last_cleanup > 300:
                db.execute("DELETE FROM vp_messages WHERE ts < ?", (now-WINDOW,))
                db.execute("DELETE FROM vp_evidence WHERE ts < ?", (now-7*WINDOW,))
                # Global hard caps for installations with many different users.
                db.execute("DELETE FROM vp_messages WHERE rowid IN (SELECT rowid FROM vp_messages ORDER BY ts DESC LIMIT -1 OFFSET 100000)")
                db.execute("DELETE FROM vp_evidence WHERE rowid IN (SELECT rowid FROM vp_evidence ORDER BY ts DESC LIMIT -1 OFFSET 20000)")
                self.last_cleanup = now
            return level, score, evidence


class Service:
    def __init__(self, config_path=None, db_path=None, policy_path=None, bot=None):
        self.config_path = config_path or runtime_config_file("virtual_protocols.json")
        self.store = Store(db_path or runtime_db_file("virtual_protocols.sqlite3"))
        self.bot = bot
        self.weights, self.thresholds, self.federation = load_policy(
            policy_path or runtime_config_file("virtual_protocols_policy.json"))
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.groups = {str(k): v for k,v in data.items() if isinstance(v, str) and v} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.groups = {}
        self.queue = asyncio.Queue(maxsize=MAX_PENDING)
        self.word_queue = asyncio.Queue(maxsize=MAX_PENDING)
        self.word_task = None
        self.lock = asyncio.Lock()
        self.task = None
        self.pending = set()
        self.dropped = 0
        self.last_error = 0.0
        self.maintenance_task = None
        self.recovery_task = None
        self.retry_task = None
        self.bio_queue = asyncio.Queue(maxsize=64)
        self.bio_worker = None
        self.bio_active = None
        self.bio_recent = {}
        self.bio_results = {}
        self.bio_pending = set()
        self.bio_waiting = {}
        self.action_locks = weakref.WeakValueDictionary()
        self.feedback_tasks = set()
        self.action_queue = asyncio.Queue(maxsize=64)
        self.action_workers = set()
        self.action_probes = {}
        self.activations = {}

    def start(self):
        if not self.groups:
            return
        if self.maintenance_task is None or self.maintenance_task.done():
            self.maintenance_task = asyncio.create_task(self._maintenance(), name="virtual-protocol-maintenance")
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = asyncio.create_task(self._recover(), name="virtual-protocol-recovery")

    async def _recover(self):
        try:
            async with self.lock:
                jobs = await asyncio.to_thread(self.store.recover, dict(self.groups))
                for job in jobs:
                    key = (job.group, job.generation, job.message)
                    if key not in self.pending and len(self.pending) < MAX_PENDING and not self.word_queue.full():
                        self.word_queue.put_nowait(job)
                        self.pending.add(key)
                if not self.word_queue.empty() and (self.word_task is None or self.word_task.done()):
                    self.word_task = asyncio.create_task(self._word_worker(), name="virtual-protocol-words")
        except Exception:
            LOG.exception("virtual protocols recovery failed")

    async def _maintenance(self):
        while self.groups:
            await asyncio.sleep(300)
            if not self.groups:
                break
            try:
                async with self.lock:
                    if self.groups:
                        await asyncio.to_thread(self.store.maintain)
                await self._recover()
            except Exception:
                LOG.exception("virtual protocols maintenance failed")

    def schedule_release(self, group, user_id, actor_id, action):
        if normalize_group_id(group) not in self.groups or len(self.feedback_tasks) >= 64:
            return False
        async def run():
            try:
                await self.note_release(group, user_id, actor_id, action)
            except Exception:
                LOG.warning("virtual protocol feedback write failed", exc_info=True)
        task = asyncio.create_task(run(), name="virtual-protocol-feedback")
        self.feedback_tasks.add(task)
        task.add_done_callback(self.feedback_tasks.discard)
        return True

    async def note_release(self, group, user_id, actor_id, action):
        group = normalize_group_id(group)
        if action not in ("unmute", "unban") or group not in self.groups:
            return False
        try:
            user_id, actor_id = int(user_id), int(actor_id)
            if user_id <= 0 or actor_id <= 0:
                return False
        except (TypeError, ValueError):
            return False
        async with self.lock:
            if group not in self.groups:
                return False
            await asyncio.to_thread(self.store.record_release, group, str(user_id), str(actor_id), action)
        return True

    async def mark_false_positive(self, group, actor_id, message_id):
        from modules.admin_tools import has_admin_permission
        group = normalize_group_id(group)
        if group not in self.groups or not actor_id or not has_admin_permission(group, actor_id, None):
            raise PermissionError("not authorized")
        try:
            actor_id, message_id = int(actor_id), int(message_id)
            if actor_id <= 0 or message_id <= 0:
                return False
        except (TypeError, ValueError):
            return False
        async with self.lock:
            if group not in self.groups:
                return False
            return await asyncio.to_thread(self.store.mark_false_positive, group, message_id, str(actor_id))

    async def reports(self, group, actor_id, limit=50):
        from modules.admin_tools import has_admin_permission
        group = normalize_group_id(group)
        if group not in self.groups or not actor_id or not has_admin_permission(group, actor_id, None):
            raise PermissionError("not authorized")
        async with self.lock:
            if group not in self.groups:
                raise PermissionError("not enabled")
            return await asyncio.to_thread(self.store.reports, group, limit)

    def peers_for(self, group):
        allowed = {group}
        for pool in self.federation:
            if group in pool:
                allowed.update(pool)
        return {g: self.groups[g] for g in allowed if g in self.groups}

    def action_group_lock(self, group):
        lock = self.action_locks.get(group)
        if lock is None:
            lock = asyncio.Lock()
            self.action_locks[group] = lock
        return lock

    async def toggle(self, group, enabled):
        group = normalize_group_id(group)
        async with self.action_group_lock(group):
            await self._toggle_locked(group, enabled)

    async def _toggle_locked(self, group, enabled):
        async with self.lock:
            new = dict(self.groups)
            if enabled:
                if group not in new:
                    new[group] = uuid.uuid4().hex
            else:
                new.pop(group, None)
            # Publish only after durable atomic write. Worker holds this same
            # lock while committing, so no analysis continues after off returns.
            await asyncio.to_thread(write_json, self.config_path, new)
            self.groups = new
            if not enabled:
                self.bio_results = {k:v for k,v in self.bio_results.items() if k[0] != group}
                self.bio_recent = {k:v for k,v in self.bio_recent.items() if k[0] != group}
            if not enabled and self.bio_active and self.bio_active[0] == group:
                self.bio_active[1].cancel()
            if not enabled:
                for probe in tuple(self.action_probes.get(group, ())):
                    probe.cancel()
        if self.groups:
            self.start()
        elif self.maintenance_task is not None:
            self.maintenance_task.cancel()
            self.maintenance_task = None

    def submit(self, group, user, message, text, reply=None):
        group = normalize_group_id(group)
        generation = self.groups.get(group)
        if not generation:
            return False
        try:
            user, message = int(user), int(message)
            reply = int(reply) if reply is not None else None
            if user <= 0 or message <= 0 or (reply is not None and reply <= 0):
                return False
        except (ValueError, TypeError):
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        key = (group, generation, message)
        if key in self.pending:
            return False
        job = Job(group,str(user),message,text[:MAX_TEXT],reply,time.time(),generation)
        if len(self.pending) >= MAX_PENDING:
            self.dropped += 1
            return False
        try:
            self.word_queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self.pending.add(key)
        if self.word_task is None or self.word_task.done():
            self.word_task = asyncio.create_task(self._word_worker(), name="virtual-protocol-words")
        return True

    async def _word_worker(self):
        while not self.word_queue.empty():
            job = self.word_queue.get_nowait()
            forwarded = False
            try:
                async with self.lock:
                    if self.groups.get(job.group) == job.generation and time.time()-job.timestamp <= QUEUE_TTL:
                        ad, channel = await asyncio.to_thread(content_signals, job.text)
                        self.queue.put_nowait(replace(job, ad=ad, channel=channel))
                        forwarded = True
                        if self.task is None or self.task.done():
                            self.task = asyncio.create_task(self._worker(), name="virtual-protocols")
            except Exception:
                self.dropped += 1
                now = time.monotonic()
                if now - self.last_error >= 60:
                    LOG.warning("virtual protocol word check failed", exc_info=True)
                    self.last_error = now
            finally:
                if not forwarded:
                    self.pending.discard((job.group,job.generation,job.message))
                self.word_queue.task_done()

    async def _worker(self):
        while not self.queue.empty():
            job = self.queue.get_nowait()
            try:
                async with self.lock:
                    if self.groups.get(job.group) == job.generation and time.time()-job.timestamp <= QUEUE_TTL:
                        if not await asyncio.to_thread(self.store.stage, job):
                            self.dropped += 1
                            continue
                        result = await asyncio.to_thread(self.store.process, job, self.peers_for(job.group), self.weights, self.thresholds)
                        await asyncio.to_thread(self.store.acknowledge, job)
                        if result and result[0] in ("suspicious", "strong"):
                            scheduled = self._schedule_bio(job, result)
                            if not scheduled and result[0] == "strong":
                                self._schedule_action(job, result[2])
            except Exception:
                now = time.monotonic()
                if now - self.last_error >= 60:
                    LOG.exception("virtual protocols job failed")
                    self.last_error = now
                if self.retry_task is None or self.retry_task.done():
                    self.retry_task = asyncio.create_task(self._retry_later())
            finally:
                self.pending.discard((job.group,job.generation,job.message))
                self.queue.task_done()


    def _schedule_action(self, job, evidence):
        if self.bot is None or self.action_queue.full():
            return False
        self.action_queue.put_nowait((job, evidence))
        self.action_workers = {t for t in self.action_workers if not t.done()}
        if len(self.action_workers) < 4:
            task = asyncio.create_task(self._action_worker(), name="virtual-protocol-actions")
            self.action_workers.add(task)
            task.add_done_callback(self.action_workers.discard)
        return True

    async def _action_worker(self):
        from modules.admin_tools import has_admin_permission
        from modules import vip_storage, punishment_mode, virtual_protocol_ui, virtual_protocol_texts
        from modules.notice_cleanup import _chat_id_for_rpc
        while not self.action_queue.empty():
            job, evidence = self.action_queue.get_nowait()
            try:
                if self.groups.get(job.group) != job.generation or time.time()-job.timestamp > QUEUE_TTL:
                    continue
                bot = self.bot
                if (has_admin_permission(job.group, job.user, None) or vip_storage.is_vip(job.group, job.user)
                        or str(getattr(bot, "bot_account_id", "")) == job.user):
                    continue
                rpc_group = _chat_id_for_rpc(job.group)
                try:
                    probe = asyncio.create_task(bot.client.get_permissions(rpc_group, int(job.user)))
                    self.action_probes.setdefault(job.group, set()).add(probe)
                    permissions = await asyncio.wait_for(probe, 5)
                    if getattr(permissions, "is_admin", None) is not False or getattr(permissions, "is_creator", False):
                        continue
                except asyncio.CancelledError:
                    if self.groups.get(job.group) != job.generation:
                        continue
                    raise
                except Exception:
                    continue
                finally:
                    probes = self.action_probes.get(job.group)
                    if probes:
                        probes.discard(probe)
                        if not probes:
                            self.action_probes.pop(job.group, None)
                async with self.action_group_lock(job.group):
                    if self.groups.get(job.group) != job.generation or time.time()-job.timestamp > QUEUE_TTL:
                        continue
                    if not getattr(bot, 'message_delete_queue', None):
                        continue
                    stage = await asyncio.to_thread(self.store.reserve_stage, job, evidence)
                    if stage is None:
                        continue
                    try:
                        deleted, _remaining = await asyncio.wait_for(bot.message_delete_queue.enqueue(rpc_group, [job.message], priority=1), 20)
                    except Exception:
                        deleted = 0
                    if not deleted:
                        await asyncio.to_thread(self.store.action_status, job, 'failed')
                        continue
                    bio = evidence.get('bio') or {}
                    members = None
                    if bio.get('channel_profile_confirmed'):
                        category, matched, members = 'کانال', bio.get('channel_title'), bio.get('channel_members')
                    elif bio.get('confirmed') and bio.get('matched'):
                        category, matched = 'بیوگرافی', bio['matched']
                    elif evidence.get('ad_signal') or evidence.get('channel_signal'):
                        category, matched = 'تبلیغات', None
                    else:
                        category, matched = 'هرزنامه', None
                    user = await virtual_protocol_ui.user_label(bot, job.user)
                    if stage == 1:
                        try:
                            payload = virtual_protocol_texts.warning(user, category, evidence.get('repeat_count', 0), matched=matched, members=members)
                            sent = await virtual_protocol_ui.send(bot, rpc_group, payload, ttl=70)
                            if sent is None:
                                raise RuntimeError('warning delivery not confirmed')
                        except Exception:
                            await asyncio.to_thread(self.store.action_status, job, 'failed')
                            continue
                        await asyncio.to_thread(self.store.confirm_stage, job, stage)
                        continue
                    queue = getattr(bot, 'moderation_queue', None)
                    actions = getattr(bot, 'admin_actions', None)
                    if queue is None or actions is None:
                        await asyncio.to_thread(self.store.action_status, job, 'failed')
                        continue
                    completed = asyncio.get_running_loop().create_future()
                    async def operation(_job=job, _stage=stage, _category=category, _matched=matched, _members=members, _user=user, _rpc=rpc_group):
                        async with self.action_group_lock(_job.group):
                            if self.groups.get(_job.group) != _job.generation or time.time()-_job.timestamp > QUEUE_TTL:
                                return False
                            if has_admin_permission(_job.group, _job.user, None) or vip_storage.is_vip(_job.group, _job.user):
                                return False
                            try:
                                role = await asyncio.wait_for(bot.client.get_permissions(_rpc, int(_job.user)), 5)
                            except Exception:
                                return False
                            if getattr(role, 'is_admin', None) is not False or getattr(role, 'is_creator', False):
                                return False
                            mode = 'mute' if punishment_mode.is_mute(_job.group) else 'ban'
                            if _stage == 2:
                                ok = await actions.mute_user(_rpc, int(_job.user), 4*3600)
                                kind = 'mute'
                            else:
                                ok = await actions.ban_user(_rpc, int(_job.user), reason='virtual_protocols')
                                kind = mode
                            if not ok:
                                return False
                            until = time.time()+4*3600 if _stage == 2 else None
                            await asyncio.to_thread(self.store.confirm_stage, _job, _stage, kind=kind, until=until)
                            try:
                                payload = virtual_protocol_texts.punishment(_user, _category, mode, matched=_matched, members=_members, final=(_stage == 3))
                                await virtual_protocol_ui.send(bot, _rpc, payload, ttl=60)
                            except Exception:
                                LOG.warning('virtual protocol punishment notice failed', exc_info=True)
                            return True
                    async def ok_callback(_value, _future=completed):
                        if not _future.done(): _future.set_result(True)
                    async def failed_callback(_error, _future=completed, _job=job):
                        async with self.action_group_lock(_job.group):
                            if self.groups.get(_job.group) == _job.generation:
                                # A timeout/exception may arrive after the remote RPC
                                # committed. Avoid treating this as confirmed failure.
                                status = 'failed' if str(_error) == 'moderation operation returned False' else 'uncertain'
                                await asyncio.to_thread(self.store.action_status, _job, status)
                        if not _future.done(): _future.set_result(False)
                    queued = queue.enqueue(rpc_group, 'auto_mute' if stage == 2 else 'ban', user_id=int(job.user),
                        timeout_seconds=50, operation=operation, on_success=ok_callback, on_failure=failed_callback)
                    if not queued:
                        await asyncio.to_thread(self.store.action_status, job, 'failed')
                        continue
                    await asyncio.to_thread(self.store.action_status, job, 'punishment_enqueued')
                # The operation acquires the same lock; await only outside it.
                try:
                    await asyncio.wait_for(asyncio.shield(completed), 75)
                except asyncio.TimeoutError:
                    LOG.warning('virtual protocol moderation callback timed out')
            except Exception:
                LOG.warning('virtual protocol action skipped', exc_info=True)
            finally:
                self.action_queue.task_done()

    async def _retry_later(self):
        await asyncio.sleep(5)
        if self.groups:
            await self._recover()

    def _schedule_bio(self, job, result):
        if self.bot is None or self.bio_queue.full():
            return False
        now = time.monotonic()
        key = (job.group, job.user)
        # A later message received before the first lookup/warning is not a
        # fresh post-warning offense; don't dispatch it ahead of that lookup.
        if key in self.bio_pending:
            if len(self.bio_waiting) < 64 or key in self.bio_waiting:
                self.bio_waiting[key] = (job, result)
            return True
        cached = self.bio_results.get(key) if now-self.bio_recent.get(key, -3601) < 3600 else None
        if len(self.bio_recent) >= 1024:
            self.bio_recent = {k:v for k,v in self.bio_recent.items() if now-v < 3600}
            self.bio_results = {k:v for k,v in self.bio_results.items() if k in self.bio_recent}
            if len(self.bio_recent) >= 1024:
                old = next(iter(self.bio_recent))
                self.bio_recent.pop(old, None)
                self.bio_results.pop(old, None)
        self.bio_pending.add(key)
        self.bio_queue.put_nowait((job,result,cached))
        if self.bio_worker is None or self.bio_worker.done():
            self.bio_worker = asyncio.create_task(self._bio_worker(), name="virtual-protocol-bio")
        return True

    async def _bio_worker(self):
        from modules import bio_link_service
        while not self.bio_queue.empty():
            job, original, cached = self.bio_queue.get_nowait()
            key = (job.group,job.user)
            try:
                if self.groups.get(job.group) != job.generation:
                    continue
                status, about = ('cached', None)
                if cached is None:
                    request = asyncio.create_task(bio_link_service.fetch_about(self.bot, int(job.user), use_cache=True))
                    self.bio_active = (job.group, request)
                    try:
                        status, about = await asyncio.wait_for(request, timeout=8)
                    except asyncio.CancelledError:
                        if self.groups.get(job.group) != job.generation:
                            continue
                        raise
                    except Exception:
                        status, about = 'error', None
                    finally:
                        self.bio_active = None
                profile = None
                if cached is None and original[2].get('channel_signal') and self.groups.get(job.group) == job.generation:
                    async def fetch_profile():
                        from modules.profile_guard import service as profile_service
                        user = await asyncio.wait_for(self.bot.client.get_entity(int(job.user)), 3)
                        return await profile_service.snapshot(self.bot, user)
                    try:
                        profile_task = asyncio.create_task(fetch_profile())
                        self.bio_active = (job.group, profile_task)
                        profile = await asyncio.wait_for(profile_task, 8)
                    except asyncio.CancelledError:
                        if self.groups.get(job.group) != job.generation:
                            continue
                        raise
                    except Exception:
                        profile = None
                    finally:
                        self.bio_active = None
                async with self.lock:
                    if self.groups.get(job.group) == job.generation:
                        result = await asyncio.to_thread(self.store.update_bio, job, status, about, self.weights, self.thresholds, cached, profile)
                        if result is not None:
                            if cached is None:
                                self.bio_recent[key] = time.monotonic()
                                self.bio_results[key] = dict(result[2]['bio'])
                            if result[0] == 'strong':
                                self._schedule_action(job, result[2])
            except Exception:
                LOG.warning("virtual protocol bio lookup failed", exc_info=True)
                if self.groups.get(job.group) == job.generation and original[0] == 'strong':
                    self._schedule_action(job, original[2])
            finally:
                self.bio_pending.discard(key)
                pending = self.bio_waiting.pop(key, None)
                if pending and self.groups.get(job.group) == job.generation:
                    self._schedule_bio(*pending)
                self.bio_queue.task_done()


async def intercept(service, event, bot_id=None):
    """Return True only for protocol control commands, including unauthorized ones."""
    if getattr(event, "is_private", False):
        return False
    message = getattr(event, "message", None)
    text = getattr(message, "message", None) or getattr(message, "caption", None) or ""
    group = getattr(event, "chat_id", None)
    from modules.light_spam_ingest import _event_user_id
    uid = _event_user_id(event, message)
    if group is None:
        return False
    command = text.strip() if isinstance(text, str) else ""
    if command in ("پروتکل روشن", "پروتکل خاموش"):
        from modules.admin_tools import has_admin_permission
        if uid and has_admin_permission(group, uid, None):
            if service.bot is None:
                return True
            from modules import virtual_protocol_ui
            if command == "پروتکل روشن":
                virtual_protocol_ui.begin_activation(service, event)
            else:
                await virtual_protocol_ui.deactivate(service, event)
        return True
    if not service.groups.get(normalize_group_id(group)):
        return False
    if uid is None or (bot_id is not None and str(uid) == str(bot_id)):
        return False
    if getattr(event, "_is_edited_message", False) or getattr(event, "is_edit", False) or getattr(message, "edit_date", None) is not None:
        return False
    reply_obj = getattr(message, "reply_to", None) or getattr(event, "reply_to", None)
    reply = (getattr(message, "reply_to_msg_id", None) or getattr(event, "reply_to_msg_id", None) or getattr(reply_obj, "reply_to_msg_id", None))
    service.submit(group, uid, getattr(message, "id", None), text, reply)
    return False
