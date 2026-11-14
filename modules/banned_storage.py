"""Indexed permanent-ban storage with JSON rollback compatibility."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file
from modules import runtime_db
from modules.atomic_write import write_json

FILE = runtime_config_file("banned_users.json")
_USE_SQLITE = runtime_db.SQLITE_ENABLED
_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="banned-save")
_cache = None
_cache_mtime = None
_pending_writes = 0


def _normalise_identifier(value):
    if value is None:
        return None
    value = str(value).replace("@", "").strip().lower()
    return value or None


def _entry_matches(entry, user_id=None, username=None, display_name=None,
                   extra_identifiers=None):
    identifiers = {value for value in (
        _normalise_identifier(user_id), _normalise_identifier(username),
        _normalise_identifier(display_name),
        *(_normalise_identifier(v) for v in (extra_identifiers or [])),
    ) if value}
    if isinstance(entry, dict):
        values = [entry.get("user_id"), entry.get("username"),
                  entry.get("display_name"), *entry.get("username_aliases", [])]
    else:
        values = [entry]
    return any(_normalise_identifier(value) in identifiers
               for value in values if value is not None)


def _identity(user_id=None, username=None, display_name=None):
    uid = _normalise_identifier(user_id)
    if uid:
        return "id:" + uid
    uname = _normalise_identifier(username)
    if uname:
        return "u:" + uname
    display = _normalise_identifier(display_name)
    if display:
        return "n:" + display
    return "unknown:" + hashlib.sha256(os.urandom(16)).hexdigest()[:20]


def _row_entry(row, aliases=()):
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "reason": row["reason"] or "بن دائمی",
        "source": row["source"] or "system",
        "username_aliases": list(aliases),
    }


def _import_json_once():
    if not _USE_SQLITE:
        return
    marker = "banned_users_json_import_v1"
    if runtime_db.meta_flag(marker):
        return
    existing = runtime_db.query_one("SELECT COUNT(*) FROM banned_users")[0]
    if existing:
        runtime_db.meta_set(marker, "existing-db")
        return
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    with runtime_db.transaction() as conn:
        for group_id, entries in raw.items():
            gid = normalize_group_id(group_id)
            if not isinstance(entries, list):
                continue
            for legacy in entries:
                entry = legacy if isinstance(legacy, dict) else {"user_id": legacy}
                user_id = entry.get("user_id")
                username = entry.get("username")
                display = entry.get("display_name")
                identity = _identity(user_id, username, display)
                conn.execute(
                    "INSERT OR REPLACE INTO banned_users("
                    "group_id,identity_key,user_id,user_id_norm,username,username_norm,"
                    "display_name,display_norm,reason,source,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (gid, identity, str(user_id) if user_id is not None else None,
                     _normalise_identifier(user_id), username,
                     _normalise_identifier(username), display,
                     _normalise_identifier(display), entry.get("reason") or "بن دائمی",
                     entry.get("source") or "system"),
                )
                for alias in entry.get("username_aliases", []) or []:
                    norm = _normalise_identifier(alias)
                    if norm:
                        conn.execute(
                            "INSERT OR IGNORE INTO banned_aliases("
                            "group_id,identity_key,alias_norm,alias_value) VALUES(?,?,?,?)",
                            (gid, identity, norm, str(alias)),
                        )
        conn.execute(
            "INSERT OR REPLACE INTO storage_meta(key,value) VALUES(?,?)",
            (marker, "ok"),
        )


def _sqlite_records(group_id=None, user_id=None, username=None,
                    display_name=None, extra_identifiers=None):
    _import_json_once()
    identifiers = {v for v in (
        _normalise_identifier(user_id), _normalise_identifier(username),
        _normalise_identifier(display_name),
        *(_normalise_identifier(x) for x in (extra_identifiers or [])),
    ) if v}
    params = []
    where = []
    if group_id is not None:
        where.append("b.group_id=?")
        params.append(normalize_group_id(group_id))
    if identifiers:
        marks = ",".join("?" for _ in identifiers)
        values = list(identifiers)
        where.append(
            "(b.user_id_norm IN (" + marks + ") OR b.username_norm IN (" + marks +
            ") OR b.display_norm IN (" + marks + ") OR EXISTS ("
            "SELECT 1 FROM banned_aliases a WHERE a.group_id=b.group_id "
            "AND a.identity_key=b.identity_key AND a.alias_norm IN (" + marks + ")))"
        )
        params.extend(values + values + values + values)
    sql = "SELECT b.* FROM banned_users b"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = runtime_db.query_all(sql, tuple(params))
    result = []
    for row in rows:
        aliases = [r[0] for r in runtime_db.query_all(
            "SELECT COALESCE(alias_value,alias_norm) FROM banned_aliases "
            "WHERE group_id=? AND identity_key=?",
            (row["group_id"], row["identity_key"]),
        )]
        result.append((row["group_id"], row["identity_key"],
                       _row_entry(row, aliases)))
    return result


def _json_load():
    global _cache, _cache_mtime
    if _cache is not None and _pending_writes > 0:
        return _cache
    try: mtime = FILE.stat().st_mtime_ns
    except OSError: mtime = None
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    if mtime is None:
        _cache = {}
    else:
        try: _cache = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception: _cache = {}
    _cache_mtime = mtime
    return _cache


def load_banned():
    if not _USE_SQLITE:
        return _json_load()
    data = {}
    for gid, _identity_key, entry in _sqlite_records():
        data.setdefault(gid, []).append(entry)
    return data


def _write_payload(payload):
    global _cache_mtime, _pending_writes
    temp_path = None
    try:
        handle, temp_path = tempfile.mkstemp(dir=str(FILE.parent), suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_path, FILE); temp_path = None
        _cache_mtime = FILE.stat().st_mtime_ns
    except Exception:
        if temp_path is not None:
            try: os.unlink(temp_path)
            except OSError: pass
    finally:
        _pending_writes = max(0, _pending_writes - 1)


def save_banned(data):
    global _cache, _pending_writes
    if _USE_SQLITE:
        with runtime_db.transaction() as conn:
            conn.execute("DELETE FROM banned_users")
            conn.execute("DELETE FROM banned_aliases")
        # Reuse the validated upsert path after clearing.
        for gid, entries in dict(data or {}).items():
            for entry in entries if isinstance(entries, list) else []:
                record = entry if isinstance(entry, dict) else {"user_id": entry}
                add_banned(
                    gid, record.get("user_id"), record.get("username"),
                    record.get("display_name"), record.get("reason", ""),
                    record.get("source", "system"),
                    _aliases=record.get("username_aliases", ()),
                )
        return
    _cache = data
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    _pending_writes += 1
    try: _WRITER.submit(_write_payload, payload)
    except Exception: _write_payload(payload)


def add_banned(group_id, user_id, username=None, display_name=None,
               reason="", source="system", *, _aliases=()):
    if not _USE_SQLITE:
        data = _json_load(); gid = normalize_group_id(group_id)
        entries = data.setdefault(gid, [])
        record = {"user_id": str(user_id), "username": username or None,
                  "display_name": display_name or None,
                  "reason": reason or "بن دائمی", "source": source,
                  "username_aliases": []}
        for index, entry in enumerate(entries):
            if _entry_matches(entry, user_id, username, display_name):
                if isinstance(entry, dict):
                    aliases = [entry.get("username"),
                               *entry.get("username_aliases", [])]
                    record["username_aliases"] = sorted({a for a in aliases if a})
                entries[index] = record; save_banned(data); return
        entries.append(record); save_banned(data); return

    _import_json_once(); gid = normalize_group_id(group_id)
    matches = _sqlite_records(gid, user_id, username, display_name)
    identity = matches[0][1] if matches else _identity(user_id, username, display_name)
    aliases = {entry.get("username") for _, _, entry in matches
               if entry.get("username")}
    aliases.update(a for _, _, entry in matches
                   for a in entry.get("username_aliases", []) if a)
    aliases.update(a for a in (_aliases or ()) if a)
    if username:
        aliases.discard(username)
    with runtime_db.transaction() as conn:
        conn.execute(
            "INSERT INTO banned_users(group_id,identity_key,user_id,user_id_norm,username,"
            "username_norm,display_name,display_norm,reason,source,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(group_id,identity_key) DO UPDATE SET "
            "user_id=excluded.user_id,user_id_norm=excluded.user_id_norm,"
            "username=excluded.username,username_norm=excluded.username_norm,"
            "display_name=excluded.display_name,"
            "display_norm=excluded.display_norm,reason=excluded.reason,"
            "source=excluded.source,updated_at=CURRENT_TIMESTAMP",
            (gid, identity, str(user_id) if user_id is not None else None,
             _normalise_identifier(user_id), username or None,
             _normalise_identifier(username), display_name or None,
             _normalise_identifier(display_name), reason or "بن دائمی", source),
        )
        current_norm = _normalise_identifier(username)
        for alias in aliases:
            norm = _normalise_identifier(alias)
            if norm and norm != current_norm:
                conn.execute(
                    "INSERT OR IGNORE INTO banned_aliases("
                    "group_id,identity_key,alias_norm,alias_value) "
                    "VALUES(?,?,?,?)", (gid, identity, norm, str(alias)),
                )


def remove_banned(group_id, user_id=None, username=None, display_name=None):
    if not _USE_SQLITE:
        data = _json_load(); gid = normalize_group_id(group_id)
        if gid not in data: return 0
        original = len(data[gid])
        data[gid] = [e for e in data[gid]
                     if not _entry_matches(e, user_id, username, display_name)]
        removed = original - len(data[gid])
        if removed: save_banned(data)
        return removed
    matches = _sqlite_records(group_id, user_id, username, display_name)
    if not matches: return 0
    with runtime_db.transaction() as conn:
        for gid, identity, _entry in matches:
            conn.execute("DELETE FROM banned_users WHERE group_id=? AND identity_key=?",
                         (gid, identity))
    return len(matches)


def find_banned_records(user_id=None, username=None, display_name=None, data=None):
    if not _USE_SQLITE or data is not None:
        data = _json_load() if data is None else data
        return {gid: [e for e in entries if isinstance(entries, list)
                      and _entry_matches(e, user_id, username, display_name)]
                for gid, entries in data.items() if isinstance(entries, list)
                and any(_entry_matches(e, user_id, username, display_name)
                        for e in entries)}
    result = {}
    for gid, _identity_key, entry in _sqlite_records(
            None, user_id, username, display_name):
        result.setdefault(gid, []).append(entry)
    return result


def remove_banned_everywhere(user_id=None, username=None, display_name=None):
    before = find_banned_records(user_id, username, display_name)
    aliases = {alias for entries in before.values() for entry in entries
               if isinstance(entry, dict)
               for alias in [entry.get("username"),
                             *entry.get("username_aliases", [])] if alias}
    if not _USE_SQLITE:
        data = _json_load(); removed = 0
        for gid, entries in data.items():
            if not isinstance(entries, list): continue
            remaining = [e for e in entries if not _entry_matches(
                e, user_id, username, display_name, aliases)]
            removed += len(entries) - len(remaining); data[gid] = remaining
        if removed: save_banned(data)
        remaining = find_banned_records(user_id, username, display_name, data)
        return removed, before, remaining
    matches = _sqlite_records(None, user_id, username, display_name, aliases)
    with runtime_db.transaction() as conn:
        for gid, identity, _entry in matches:
            conn.execute("DELETE FROM banned_users WHERE group_id=? AND identity_key=?",
                         (gid, identity))
    remaining = find_banned_records(user_id, username, display_name)
    return len(matches), before, remaining


def get_matching_ban_records(group_id, user_id, username=None, data=None):
    if not _USE_SQLITE or data is not None:
        source = _json_load() if data is None else data
        return [e for e in source.get(normalize_group_id(group_id), [])
                if _entry_matches(e, user_id, username)]
    return [entry for _gid, _identity_key, entry in
            _sqlite_records(group_id, user_id, username)]


def is_banned(group_id, user_id, username=None, data=None):
    records = get_matching_ban_records(group_id, user_id, username, data)
    banned = bool(records)
    if banned and os.environ.get("BOT_VERBOSE_LOGS", "").lower() in {"1", "true", "on"}:
        print("BANNED STORAGE MATCH "
              f"user_id={user_id} username={username} group_id={group_id} "
              f"records_count={len(records)}")
    return banned


def get_banned_list(group_id):
    """لیست تمام رکوردهای اخراج/بن برای این گروه."""
    gid = normalize_group_id(group_id)
    if not _USE_SQLITE:
        data = _json_load()
        entries = data.get(gid, [])
        return [dict(e) if isinstance(e, dict) else {"user_id": str(e)} for e in entries]
    matches = _sqlite_records(gid)
    return [entry for _, _, entry in matches]


def clear_group_banned(group_id):
    """حذف تمام افراد از لیست بن ربات برای این گروه."""
    gid = normalize_group_id(group_id)
    if not _USE_SQLITE:
        data = _json_load()
        if gid not in data:
            return 0
        count = len(data[gid]) if isinstance(data[gid], list) else 0
        data.pop(gid, None)
        save_banned(data)
        return count
    matches = _sqlite_records(gid)
    if not matches:
        return 0
    with runtime_db.transaction() as conn:
        conn.execute("DELETE FROM banned_users WHERE group_id=?", (gid,))
        conn.execute("DELETE FROM banned_aliases WHERE group_id=?", (gid,))
    return len(matches)


def export_json(path=None):
    """Export current bans for emergency JSON-backend rollback."""
    target = Path(path) if path else FILE
    payload = load_banned()
    write_json(target, payload)
    if json.loads(target.read_text(encoding="utf-8")) != payload:
        raise OSError(f"ban JSON rollback export verification failed: {target}")
    return target


# Import is intentionally last: every query/mutation helper is defined before
# the one-time, transaction-protected migration can expose the SQLite backend.
_import_json_once()
