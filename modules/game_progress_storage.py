"""Lazy, batched SQLite rows for legacy per-user game progress files."""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict

from modules import runtime_db
from modules.atomic_write import write_json

_CACHE_LIMIT = int(os.environ.get("BOT_GAME_PROGRESS_CACHE_USERS", "2000"))
_STORES = []


class SeenProgressStore:
    def __init__(self, namespace, legacy_file, mapping):
        self.namespace = str(namespace)
        self.legacy_file = legacy_file
        self.mapping = mapping
        self.sqlite = runtime_db.SQLITE_ENABLED
        self._dirty = {}  # item_key -> mutation generation
        self._recent = OrderedDict()
        self._lock = threading.RLock()
        _STORES.append(self)
        if self.sqlite:
            self._import_once()

    def _import_once(self):
        marker = f"{self.namespace}_json_import_v1"
        if runtime_db.meta_flag(marker):
            return
        existing = runtime_db.query_one(
            "SELECT COUNT(*) FROM runtime_kv WHERE namespace=?",
            (self.namespace,),
        )[0]
        if existing:
            runtime_db.meta_set(marker, "existing-db")
            return
        try:
            raw = json.loads(self.legacy_file.read_text(encoding="utf-8")) \
                if self.legacy_file.exists() else {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        with runtime_db.transaction() as conn:
            for key, values in raw.items():
                if not isinstance(values, (list, tuple, set)):
                    continue
                payload = json.dumps(
                    sorted({str(value) for value in values}),
                    ensure_ascii=False, separators=(",", ":"),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_kv("
                    "namespace,item_key,payload,updated_at) "
                    "VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (self.namespace, str(key), payload),
                )
            conn.execute(
                "INSERT OR REPLACE INTO storage_meta(key,value) VALUES(?,?)",
                (marker, "ok"),
            )

    def _load_locked(self, key):
        if key not in self.mapping and self.sqlite:
            values = runtime_db.kv_get(self.namespace, key, [])
            self.mapping[key] = set(str(value) for value in (values or []))
        value = self.mapping.setdefault(key, set())
        self._recent.pop(key, None)
        self._recent[key] = None
        return value

    def get(self, key):
        """Return a safe copy; callers publish mutations with ``replace``."""
        key = str(key)
        with self._lock:
            return set(self._load_locked(key))

    def replace(self, key, values):
        key = str(key)
        with self._lock:
            self.mapping[key] = {str(value) for value in values}
            self._recent.pop(key, None)
            self._recent[key] = None
            if self.sqlite:
                self._dirty[key] = int(self._dirty.get(key, 0)) + 1

    def mark(self, key):
        """Compatibility helper for code that already replaced the mapping."""
        if self.sqlite:
            key = str(key)
            with self._lock:
                self._dirty[key] = int(self._dirty.get(key, 0)) + 1
                self._recent.pop(key, None)
                self._recent[key] = None

    def delete(self, key):
        key = str(key)
        with self._lock:
            self.mapping.pop(key, None)
            self._dirty.pop(key, None)
            self._recent.pop(key, None)
            if self.sqlite:
                runtime_db.kv_delete(self.namespace, key)

    def clear(self):
        with self._lock:
            self.mapping.clear()
            self._dirty.clear()
            self._recent.clear()
            if self.sqlite:
                runtime_db.execute(
                    "DELETE FROM runtime_kv WHERE namespace=?", (self.namespace,)
                )

    def flush(self):
        if not self.sqlite:
            return False
        with self._lock:
            generations = dict(self._dirty)
            batch = {
                key: sorted(str(value) for value in self.mapping.get(key, ()))
                for key in generations
            }
        if not generations:
            return False
        with runtime_db.transaction() as conn:
            for key, values in batch.items():
                payload = json.dumps(
                    values, ensure_ascii=False, separators=(",", ":")
                )
                conn.execute(
                    "INSERT INTO runtime_kv(namespace,item_key,payload,updated_at) "
                    "VALUES(?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(namespace,item_key) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                    (self.namespace, key, payload),
                )
        with self._lock:
            for key, generation in generations.items():
                if self._dirty.get(key) == generation:
                    self._dirty.pop(key, None)
            self._evict_clean_cache_locked()
        return True

    def export_json(self):
        """Export this namespace to its retained legacy file for rollback."""
        if not self.sqlite:
            return self.legacy_file
        self.flush()
        payload = {}
        rows = runtime_db.query_all(
            "SELECT item_key,payload FROM runtime_kv WHERE namespace=?",
            (self.namespace,),
        )
        for key, raw in rows:
            try:
                values = json.loads(raw)
            except ValueError:
                values = []
            payload[str(key)] = sorted(str(value) for value in (values or []))
        write_json(self.legacy_file, payload)
        if json.loads(self.legacy_file.read_text(encoding="utf-8")) != payload:
            raise OSError(
                f"game progress JSON rollback export failed: {self.legacy_file}"
            )
        return self.legacy_file

    def _evict_clean_cache_locked(self):
        limit = max(10, _CACHE_LIMIT)
        scanned_dirty = 0
        while len(self._recent) > limit and scanned_dirty < len(self._recent):
            key, _ = self._recent.popitem(last=False)
            if key in self._dirty:
                self._recent[key] = None
                scanned_dirty += 1
                continue
            self.mapping.pop(key, None)
            scanned_dirty = 0


def flush_all():
    changed = False
    for store in tuple(_STORES):
        changed = store.flush() or changed
    return changed


def export_all_json():
    return [str(store.export_json()) for store in tuple(_STORES)]
