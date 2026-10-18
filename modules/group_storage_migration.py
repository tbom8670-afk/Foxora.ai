"""One-time, lossless normalization of historical SPlusthon group IDs."""
import json
import os
import tempfile
from pathlib import Path

from modules.group_id import merge_unique, normalize_group_id
from modules.runtime_paths import runtime_config_file, runtime_log_file


def _load(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save(path, value):
    """Atomic JSON write; an interrupted migration cannot truncate state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _merge_group_record(current, incoming):
    current = dict(current) if isinstance(current, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    current["active"] = bool(current.get("active", False) or incoming.get("active", False))
    for key, value in incoming.items():
        if key not in current or current[key] in (None, ""):
            current[key] = value
    return current


def _merge_list(current, incoming):
    return merge_unique(current if isinstance(current, list) else [],
                        incoming if isinstance(incoming, list) else [])


def _merge_flag(current, incoming):
    return bool(current) and bool(incoming)


def _merge_mapping(current, incoming):
    current = dict(current) if isinstance(current, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    for key, value in incoming.items():
        if key not in current:
            current[key] = value
        elif isinstance(current[key], dict) and isinstance(value, dict):
            current[key] = _merge_mapping(current[key], value)
        elif isinstance(current[key], list) and isinstance(value, list):
            current[key] = _merge_list(current[key], value)
    return current


def _merge_counters(current, incoming):
    current = dict(current) if isinstance(current, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    for key, value in incoming.items():
        if key not in current:
            current[key] = value
        elif isinstance(current[key], dict) and isinstance(value, dict):
            current[key] = _merge_counters(current[key], value)
        elif isinstance(current[key], (int, float)) and isinstance(value, (int, float)):
            current[key] += value
        elif isinstance(current[key], list) and isinstance(value, list):
            current[key] = _merge_list(current[key], value)
    return current


def _migrate_file(path, merger):
    data = _load(path)
    migrated = {}
    changed = False
    for old_key, value in data.items():
        new_key = normalize_group_id(old_key)
        changed = changed or new_key != str(old_key)
        if new_key in migrated:
            migrated[new_key] = merger(migrated[new_key], value)
            changed = True
        else:
            migrated[new_key] = value
    if changed:
        _save(path, migrated)
    return changed


def migrate_all_group_storage():
    migrations = (
        (runtime_config_file("groups.json"), _merge_group_record),
        (runtime_config_file("admins.json"), _merge_list),
        (runtime_config_file("group_words.json"), _merge_list),
        (runtime_config_file("group_filtered_names.json"), _merge_list),
        (runtime_config_file("group_banned_words.json"), _merge_flag),
        (runtime_config_file("banned_users.json"), _merge_list),
        (runtime_log_file("group_stats.json", migrate=True), _merge_counters),
        (runtime_log_file("spam_counts.json", migrate=True), _merge_counters),
        (runtime_log_file("user_map.json", migrate=True), _merge_mapping),
        (runtime_config_file("user_activity.json"), _merge_mapping),
    )
    return [str(path) for path, merger in migrations
            if _migrate_file(path, merger)]
