"""Bounded per-user activity counters.

Termux/production stores indexed rows in SQLite and batches hot-path updates.
The JSON backend remains available for rollback and existing tests.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_archive_file, runtime_config_file
from modules import runtime_db
from modules.atomic_write import write_json

FILE = runtime_config_file("user_activity.json")
ARCHIVE_FILE = runtime_archive_file("user_activity_archive.json")
_USE_SQLITE = runtime_db.SQLITE_ENABLED
PRUNE_AFTER_DAYS = int(os.environ.get("BOT_ACTIVITY_RETENTION_DAYS", "180"))
_CACHE = {}
# key -> mutation generation; prevents a flush racing with a new event from
# clearing a row that changed again while the batch was on disk.
_DIRTY = {}
_CACHE_LOCK = threading.RLock()
_CACHE_RECENT = OrderedDict()
_CACHE_LIMIT = int(os.environ.get("BOT_ACTIVITY_CACHE_USERS", "50000"))
_JSON_LOADED = False


def _import_json_once():
    """Copy legacy aggregate counters into empty SQLite storage once."""
    if not _USE_SQLITE:
        return
    marker = "user_activity_json_import_v1"
    if runtime_db.meta_flag(marker):
        return
    if runtime_db.query_one("SELECT COUNT(*) FROM user_activity")[0]:
        runtime_db.meta_set(marker, "existing-db")
        return
    try:
        raw = json.loads(FILE.read_text(encoding="utf8")) if FILE.exists() else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    now = time.time()
    with runtime_db.transaction() as conn:
        for group_id, users in raw.items():
            if not isinstance(users, dict):
                continue
            gid = normalize_group_id(group_id)
            for user_id, info in users.items():
                if not isinstance(info, dict):
                    continue
                try:
                    gifs = max(0, int(info.get("gifs", 0) or 0))
                    videos = max(0, int(info.get("videos", 0) or 0))
                    first = float(info.get("first", now) or now)
                    last = float(info.get("last", first) or first)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO user_activity("
                    "group_id,user_id,gifs,videos,first_seen,last_seen) "
                    "VALUES(?,?,?,?,?,?)",
                    (gid, str(user_id), gifs, videos, first, last),
                )
        conn.execute(
            "INSERT OR REPLACE INTO storage_meta(key,value) VALUES(?,?)",
            (marker, "ok"),
        )


def _key(chat_id, user_id):
    return normalize_group_id(chat_id), str(user_id)


def _json_load():
    global _CACHE, _JSON_LOADED
    if _JSON_LOADED:
        return _CACHE
    if not FILE.exists():
        _CACHE = {}
    else:
        try: _CACHE = json.loads(FILE.read_text(encoding="utf8"))
        except Exception: _CACHE = {}
    _JSON_LOADED = True
    return _CACHE


def _touch_cache(key):
    _CACHE_RECENT.pop(key, None)
    _CACHE_RECENT[key] = None


def _evict_clean_cache():
    limit = max(100, _CACHE_LIMIT)
    scanned_dirty = 0
    while len(_CACHE_RECENT) > limit and scanned_dirty < len(_CACHE_RECENT):
        key, _ = _CACHE_RECENT.popitem(last=False)
        if key in _DIRTY:
            _CACHE_RECENT[key] = None
            scanned_dirty += 1
            continue
        _CACHE.pop(key, None)
        scanned_dirty = 0


def _sqlite_entry(chat_id, user_id):
    key = _key(chat_id, user_id)
    if key in _CACHE:
        _touch_cache(key)
        return _CACHE[key]
    row = runtime_db.query_one(
        "SELECT gifs,videos,first_seen,last_seen FROM user_activity "
        "WHERE group_id=? AND user_id=?", key,
    )
    if row:
        value = {"gifs": int(row[0]), "videos": int(row[1]),
                 "first": float(row[2]), "last": float(row[3])}
    else:
        now = time.time()
        value = {"gifs": 0, "videos": 0, "first": now, "last": now}
    _CACHE[key] = value
    _touch_cache(key)
    return value


def _prune_json(data, now=None):
    now = time.time() if now is None else now
    cutoff = now - PRUNE_AFTER_DAYS * 86400
    removed = 0
    for gid in list(data.keys()):
        users = data[gid]
        for uid in list(users.keys()):
            try: inactive = float(users[uid].get("last", 0) or 0) < cutoff
            except (TypeError, ValueError): inactive = False
            if inactive:
                users.pop(uid, None); removed += 1
        if not users: data.pop(gid, None)
    return removed


def flush():
    """Persist only changed activity rows and prune inactive users."""
    global _DIRTY
    if _USE_SQLITE:
        cutoff = time.time() - PRUNE_AFTER_DAYS * 86400
        with _CACHE_LOCK:
            generations = dict(_DIRTY)
            batch = {
                key: dict(_CACHE[key])
                for key in generations if key in _CACHE
            }
        if not generations:
            # Pruning is cheap and indexed, but need not run on every empty flush.
            return False
        with runtime_db.transaction() as conn:
            for (gid, uid), info in batch.items():
                conn.execute(
                    "INSERT INTO user_activity(group_id,user_id,gifs,videos,"
                    "first_seen,last_seen) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(group_id,user_id) DO UPDATE SET "
                    "gifs=excluded.gifs,videos=excluded.videos,"
                    "first_seen=MIN(user_activity.first_seen,excluded.first_seen),"
                    "last_seen=excluded.last_seen",
                    (gid, uid, int(info.get("gifs", 0)), int(info.get("videos", 0)),
                     float(info.get("first", time.time())),
                     float(info.get("last", time.time()))),
                )
            conn.execute("DELETE FROM user_activity WHERE last_seen < ?", (cutoff,))
        with _CACHE_LOCK:
            for key, generation in generations.items():
                if _DIRTY.get(key) == generation:
                    _DIRTY.pop(key, None)
            # Keep only recent/touched cache rows; DB is canonical.
            for key, info in list(_CACHE.items()):
                if (key not in _DIRTY and
                        float(info.get("last", 0) or 0) < cutoff):
                    _CACHE.pop(key, None)
                    _CACHE_RECENT.pop(key, None)
            _evict_clean_cache()
        return True

    with _CACHE_LOCK:
        data = _json_load()
        if not _DIRTY:
            return False
        _prune_json(data)
        FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = FILE.with_name(FILE.name + ".tmp")
        with temp_path.open("w", encoding="utf8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_path, FILE)
        _DIRTY.clear()
        return True


def record(chat_id, user_id, message):
    gid, uid = _key(chat_id, user_id)
    doc = getattr(message, "document", None) or getattr(
        getattr(message, "media", None), "document", None
    )
    mime = (getattr(doc, "mime_type", None) or "").lower()
    is_gif = (bool(getattr(message, "gif", False)) or
              bool(getattr(message, "animation", None)) or mime == "image/gif")
    with _CACHE_LOCK:
        if _USE_SQLITE:
            user = _sqlite_entry(gid, uid)
        else:
            data = _json_load()
            group = data.setdefault(gid, {})
            now = time.time()
            user = group.setdefault(uid, {
                "gifs": 0, "videos": 0, "first": now, "last": now,
            })
        now = time.time()
        user["last"] = now; user.setdefault("first", now)
        if is_gif:
            user["gifs"] = int(user.get("gifs", 0)) + 1
        elif mime.startswith("video/"):
            user["videos"] = int(user.get("videos", 0)) + 1
        key = (gid, uid)
        _DIRTY[key] = int(_DIRTY.get(key, 0)) + 1


def get(chat_id, user_id):
    gid, uid = _key(chat_id, user_id)
    with _CACHE_LOCK:
        if _USE_SQLITE:
            return dict(_sqlite_entry(gid, uid))
        return dict(_json_load().get(gid, {}).get(
            uid, {"gifs": 0, "videos": 0, "first": 0, "last": 0}
        ))


def export_json(path=None):
    """Export canonical activity counters for JSON-backend rollback."""
    target = Path(path) if path else FILE
    flush()
    if _USE_SQLITE:
        payload = {}
        rows = runtime_db.query_all(
            "SELECT group_id,user_id,gifs,videos,first_seen,last_seen "
            "FROM user_activity"
        )
        for row in rows:
            payload.setdefault(str(row[0]), {})[str(row[1])] = {
                "gifs": int(row[2]), "videos": int(row[3]),
                "first": float(row[4]), "last": float(row[5]),
            }
    else:
        with _CACHE_LOCK:
            payload = {
                str(group): {str(user): dict(info) for user, info in users.items()}
                for group, users in _json_load().items()
            }
    write_json(target, payload)
    if json.loads(target.read_text(encoding="utf8")) != payload:
        raise OSError(f"activity JSON rollback export verification failed: {target}")
    return target


_import_json_once()
