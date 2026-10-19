"""Banned-word obfuscation, original-word mapping, and advertising terms."""
from types import SimpleNamespace

from modules.ad_name_detector import reason as ad_name_reason
from modules.big_spam import looks_promotional
from modules.spam_detector import SpamDetector


class _FakeConfig:
    def __init__(self, words):
        self.banned_words = set(words)
        self._banned_words_version = 1

    def get(self, key, default=None):
        if key == "check_banned_words":
            return True
        return default

    def reload_if_needed(self):
        pass


WORDS = [
    "پی", "شقم", "ف پی", "دختر پی", "کیرتو میخام",
    "بی ام", "کیرم", "کیر", "شخصی", "یکی بیاد", "بکنمش", "کسش", "کس",
    "جق", "آبم", "با هزینه", "🌈", "🔞", "♂️", "⚧️",
    "حال پی", "تمام سانسور", "حال میدم", "فیلم پی",
]


def detector(words=None):
    return SpamDetector(_FakeConfig(words or WORDS))


def hit(text, words=None):
    found, reason = detector(words).check_banned_words(text)
    return found, reason


def test_obfuscated_kir_maps_to_original():
    for sample in ("کیر", "ک ی ر", "ک.ی.ر", "کـیر", "کییییر", "ك ي ر"):
        found, reason = hit(sample, ["کیر", "پی"])
        assert found is True, sample
        assert "banned_word" in reason
        assert "کلمه ممنوعه (کیر)" in reason, reason


def test_obfuscated_jagh_and_kos():
    found, reason = hit("ج ق", ["جق", "کس"])
    assert found and "جق" in reason
    found, reason = hit("جـق", ["جق"])
    assert found and "جق" in reason
    found, reason = hit("ک س", ["کس", "کیر"])
    assert found and "کلمه ممنوعه (کس)" in reason
    found, reason = hit("کـس", ["کس"])
    assert found and "کس" in reason
    found, reason = hit("کص", ["کس"])
    assert found and "کس" in reason


def test_no_false_positive_pi_and_kos():
    det = detector(["پی", "کس", "کیر"])
    for sample in (
        "پیام", "پیام داد", "پیر شدیم", "پیشش بودم", "پیمان", "پیرمرد",
        "کسی", "کسی نیومد", "کسی هست؟",
    ):
        found, reason = det.check_banned_words(sample)
        assert found is False, (sample, reason)


def test_phrase_space_required_for_bm():
    found, _ = hit("بی ام", ["بی ام", "پی"])
    assert found is True
    found, reason = hit("بیام", ["بی ام"])
    assert found is False, reason


def test_new_words_and_emojis():
    samples = {
        "شقم": "شقم",
        "ف پی": "ف پی",
        "دختر پی": "دختر پی",
        "کیرم": "کیرم",
        "شخصی": "شخصی",
        "یکی بیاد": "یکی بیاد",
        "بکنمش": "بکنمش",
        "کسش": "کسش",
        "آبم": "آبم",
        "با هزینه": "با هزینه",
        "🌈": "🌈",
        "🔞": "🔞",
        "حال پی": "حال پی",
        "تمام سانسور": "تمام سانسور",
        "حال میدم": "حال میدم",
        "فیلم پی": "فیلم پی",
        "کیرتو میخام": "ک یرتو میخام",
    }
    for original, sample in samples.items():
        found, reason = hit(sample, WORDS)
        assert found is True, (original, sample, reason)
        assert "banned_word" in reason
        assert original in reason, reason


def test_is_spam_checks_banned_words_first():
    det = detector(["کیر", "www.example.com"])
    is_spam, reason = det.is_spam("ک ی ر https://t.me/x")
    assert is_spam is True
    assert "banned_word" in reason
    assert "کیر" in reason


def test_advertising_name_and_promo_markers():
    for name in ("حال پی", "تمام سانسور", "حال میدم", "فیلم پی", "🔞"):
        user = SimpleNamespace(first_name=name, last_name=None, username=None)
        assert ad_name_reason(user) is not None, name
        assert looks_promotional(name) is True, name


def test_legitimate_name_not_ad():
    user = SimpleNamespace(first_name="رضا", last_name="پیروز", username=None)
    assert ad_name_reason(user) is None


def test_new_23_banned_words_and_pi_question_mark_vs_piyadeh():
    from pathlib import Path
    from modules.config_manager import ConfigManager

    repo_root = Path(__file__).resolve().parents[1]
    cm = ConfigManager(banned_words_path=str(repo_root / "config" / "banned_words.txt"))
    det = SpamDetector(cm)

    required_words = [
        "برده",
        "کال",
        "بیاد پی",
        "بیاد بی",
        "استوریم",
        "ف پ",
        "کیونی",
        "کونی",
        "کون",
        "شخصی",
        "دختر بیاد",
        "خانوم بیاد",
        "رل",
        "پوی",
        "ب ی پی",
        "بی‌غیرت",
        "بیغیرت",
        "جنده",
        "فلم",
        "پی؟",
        "ت پ",
        "یه خانوم",
        "بیاد لطفاً",
    ]
    for phrase in required_words:
        assert phrase in cm.banned_words, f"missing in banned_words.txt: {phrase}"
        found, reason = det.check_banned_words(f"سلام {phrase} خوبی")
        assert found is True, (phrase, reason)

    # جملهٔ «بیا لطفاً پی؟» باید شناسایی شود
    found_q, reason_q = det.check_banned_words("بیا لطفاً پی؟")
    assert found_q is True, reason_q

    # اما «بیا پیاده بدیم» نباید به خاطر «پی» داخل «پیاده» مسدود شود
    for clean_sentence in (
        "بیا پیاده بدیم",
        "بیا پیاده بریم پارک",
        "پیاده روی صبحگاهی",
        "کالاهای ایرانی",
        "مکالمه زبان انگلیسی",
    ):
        found_clean, reason_clean = det.check_banned_words(clean_sentence)
        assert found_clean is False, (clean_sentence, reason_clean)

