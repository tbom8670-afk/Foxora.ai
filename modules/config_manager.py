"""
مدیریت تنظیمات ربات ضد هرزنامه سروش پلاس
"""
import json
import os
from typing import List, Set

from modules.runtime_paths import PROJECT_ROOT, runtime_config_file


class ConfigManager:
    def __init__(self, config_path: str = "config/config.json",
                 banned_words_path: str = "config/banned_words.txt",
                 whitelist_path: str = "config/whitelist.txt"):
        # config.json is deployment configuration and stays beside the source.
        # Mutable lists move to the private runtime directory on Termux.
        self.config_path = str(
            PROJECT_ROOT / config_path if not os.path.isabs(config_path)
            else config_path
        )
        self._uses_default_banned_words = (banned_words_path == "config/banned_words.txt")
        self._repo_banned_words_path = str(PROJECT_ROOT / "config" / "banned_words.txt")
        self.banned_words_path = str(
            runtime_config_file("banned_words.txt")
            if self._uses_default_banned_words
            else banned_words_path
        )
        self.whitelist_path = str(
            runtime_config_file("whitelist.txt")
            if whitelist_path == "config/whitelist.txt"
            else whitelist_path
        )
        self.config = {}
        self.banned_words: Set[str] = set()
        self.whitelisted_ids: Set[int] = set()
        self.whitelisted_usernames: Set[str] = set()
        self._banned_words_mtime = None
        self._whitelist_mtime = None
        self._banned_words_version = 0
        self.load_all()

    def load_all(self):
        self.load_config()
        self.load_banned_words()
        self.load_whitelist()

    def load_config(self):
        """خواندن فایل config.json"""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"فایل تنظیمات یافت نشد: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        return self.config

    def load_banned_words(self) -> Set[str]:
        """خواندن کلمات ممنوعه از فایل txt + config"""
        words = set()
        # از فایل txt زمان اجرا
        if os.path.exists(self.banned_words_path):
            with open(self.banned_words_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    words.add(line.lower())
        # همگام‌سازی کلمات جدید اضافه شده در مخزن (config/banned_words.txt) با فایل runtime
        if (
            getattr(self, "_uses_default_banned_words", False)
            and os.path.exists(self._repo_banned_words_path)
        ):
            missing_in_runtime = []
            with open(self._repo_banned_words_path, 'r', encoding='utf-8') as rf:
                for line in rf:
                    clean = line.strip()
                    if not clean or clean.startswith('#'):
                        continue
                    lowered = clean.lower()
                    if lowered not in words:
                        words.add(lowered)
                        missing_in_runtime.append(clean)
            if (
                missing_in_runtime
                and os.path.abspath(self.banned_words_path)
                != os.path.abspath(self._repo_banned_words_path)
            ):
                try:
                    with open(self.banned_words_path, 'a', encoding='utf-8') as wf:
                        wf.write("\n" + "\n".join(missing_in_runtime) + "\n")
                except OSError:
                    pass
        # از config.json اگر وجود داشته باشد
        if "banned_words" in self.config and isinstance(self.config["banned_words"], list):
            for w in self.config["banned_words"]:
                words.add(str(w).lower())
        
        self.banned_words = words
        self._banned_words_version += 1
        self._banned_words_mtime = self._file_mtime(self.banned_words_path)
        return words

    def load_whitelist(self):
        """خواندن لیست سفید"""
        ids = set(self.config.get("whitelisted_user_ids", []))
        admin_ids = set(self.config.get("admin_user_ids", []))
        ids.update(admin_ids)

        usernames = set()

        if os.path.exists(self.whitelist_path):
            with open(self.whitelist_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('@'):
                        usernames.add(line[1:].lower())
                    else:
                        try:
                            ids.add(int(line))
                        except ValueError:
                            usernames.add(line.lower().lstrip('@'))

        self.whitelisted_ids = ids
        self.whitelisted_usernames = usernames
        self._whitelist_mtime = self._file_mtime(self.whitelist_path)
        return ids, usernames

    def is_whitelisted(self, user_id: int, username: str = None) -> bool:
        """بررسی اینکه کاربر در لیست سفید است یا نه"""
        if user_id in self.whitelisted_ids:
            return True
        if username and username.lower().lstrip('@') in self.whitelisted_usernames:
            return True
        return False

    def is_admin(self, user_id=None, username=None) -> bool:
        try:
            # بررسی آیدی (اگر وجود داشت)
            if user_id:
                if user_id in set(self.config.get("admin_user_ids", [])):
                    return True

            # بررسی یوزرنیم سروش پلاس
            if username:
                username = str(username).replace("@", "").lower()

                admins = self.config.get("admin_usernames", [])

                for admin in admins:
                    if username == str(admin).replace("@", "").lower():
                        return True

        except Exception:
            pass

        return False

    def add_banned_word(self, word: str) -> bool:
        """افزودن کلمه ممنوعه به فایل"""
        word = word.strip().lower()
        if not word:
            return False
        if word in self.banned_words:
            return False
        
        with open(self.banned_words_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{word}\n")
        self.banned_words.add(word)
        self._banned_words_version += 1
        return True

    def remove_banned_word(self, word: str) -> bool:
        """حذف کلمه ممنوعه از فایل"""
        word = word.strip().lower()
        if word not in self.banned_words:
            return False
        
        # بازنویسی فایل بدون کلمه مورد نظر
        if os.path.exists(self.banned_words_path):
            with open(self.banned_words_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            with open(self.banned_words_path, 'w', encoding='utf-8') as f:
                for line in lines:
                    if line.strip().lower() != word:
                        f.write(line)
        
        self.banned_words.discard(word)
        self._banned_words_version += 1
        return True

    def get(self, key, default=None):
        return self.config.get(key, default)

    @staticmethod
    def _file_mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def reload_if_needed(self):
        """برای hot-reload تنظیمات"""
        if self.config.get("enable_hot_reload_banned_words"):
            if self._file_mtime(self.banned_words_path) != self._banned_words_mtime:
                self.load_banned_words()
            if self._file_mtime(self.whitelist_path) != self._whitelist_mtime:
                self.load_whitelist()
