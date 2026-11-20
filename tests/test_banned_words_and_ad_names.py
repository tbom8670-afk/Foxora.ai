from types import SimpleNamespace
import pytest

from modules.ad_name_detector import reason as ad_name_reason, display_name as ad_display_name
from modules.group_words_storage import find_matching_filter_word, normalize_filter_text
from modules.spam_detector import SpamDetector


def test_banned_words_whole_word_matching():
    """Verify that standalone whole words are accurately detected."""
    words = ["پی"]
    assert find_matching_filter_word("پی", words) == "پی"
    assert find_matching_filter_word("این یک پی است", words) == "پی"


def test_banned_words_joined_and_zwnj():
    """Verify that words joined with space, ZWNJ (نیم‌فاصله), hyphens or tatweel are detected."""
    words = ["پی"]
    assert find_matching_filter_word("سگ پی", words) == "پی"
    assert find_matching_filter_word("سگ‌پی", words) == "پی"
    assert find_matching_filter_word("سگ-پی", words) == "پی"
    assert find_matching_filter_word("سگ_پی", words) == "پی"
    assert find_matching_filter_word("پـــــی", words) == "پی"


def test_banned_words_no_false_positive_inside_other_words():
    """Verify that substrings inside legitimate words (پیر, پیام, پیمان, پیش) are NEVER detected."""
    words = ["پی"]
    assert find_matching_filter_word("پیر", words) is None
    assert find_matching_filter_word("پیام", words) is None
    assert find_matching_filter_word("پیمان", words) is None
    assert find_matching_filter_word("پیش", words) is None
    assert find_matching_filter_word("سپیدار", words) is None
    assert find_matching_filter_word("پیامبر", words) is None
    assert find_matching_filter_word("این یک پیامبر است", words) is None
    assert find_matching_filter_word("پیشنهاد ویژه", words) is None


def test_spam_detector_banned_word_matching():
    """Verify SpamDetector matches words with boundary precision without false positives."""
    config = SimpleNamespace(
        banned_words=["پی", "تبلیغ"],
        get=lambda k, d=None: True,
        reload_if_needed=lambda: None,
    )
    detector = SpamDetector(config)
    detector._banned_words_version = -1
    detector._refresh_banned_word_patterns()

    # Exact matches and ZWNJ
    is_banned, _ = detector.check_banned_words("پی")
    assert is_banned is True

    is_banned, _ = detector.check_banned_words("سگ‌پی")
    assert is_banned is True

    # Legitimate non-matching words
    is_banned, _ = detector.check_banned_words("پیرمرد و دریا")
    assert is_banned is False

    is_banned, _ = detector.check_banned_words("پیام خود را بفرستید")
    assert is_banned is False

    is_banned, _ = detector.check_banned_words("پیمان ما پابرجاست")
    assert is_banned is False

    is_banned, _ = detector.check_banned_words("پیش از موعد")
    assert is_banned is False


def test_ad_name_detector_common_ad_names():
    """Verify fast and accurate detection of common ad names (فیلم, پیوی, پی وی, etc.)."""
    ad_users = [
        SimpleNamespace(first_name="فیلم", last_name=None, username=None),
        SimpleNamespace(first_name="فیلم سینمایی", last_name=None, username=None),
        SimpleNamespace(first_name="پیوی", last_name=None, username=None),
        SimpleNamespace(first_name="پی وی", last_name="بیا", username=None),
        SimpleNamespace(first_name="بیا پیوی", last_name=None, username=None),
        SimpleNamespace(first_name="بیو چک", last_name=None, username=None),
        SimpleNamespace(first_name="خاله نازنین", last_name=None, username=None),
        SimpleNamespace(first_name="شارژ رایگان", last_name=None, username=None),
        SimpleNamespace(first_name="فیلترشکن قوی", last_name=None, username=None),
    ]
    for user in ad_users:
        reason = ad_name_reason(user)
        assert reason is not None, f"Expected ad detection for user name: {user.first_name}"


def test_ad_name_detector_legitimate_names():
    """Verify legitimate Persian user names are not flagged as ads."""
    legit_users = [
        SimpleNamespace(first_name="علی", last_name="رضایی", username="ali_rezaei"),
        SimpleNamespace(first_name="محمد", last_name="حسینی", username="m_hosseini"),
        SimpleNamespace(first_name="سارا", last_name="احمدی", username=None),
        SimpleNamespace(first_name="رضا", last_name="پیروز", username=None),
    ]
    for user in legit_users:
        reason = ad_name_reason(user)
        assert reason is None, f"False positive for user name: {user.first_name}"


@pytest.mark.parametrize("name", [
    "عسل خانوم", "خانوم", "پی", "استوری", "استوریم", "استوررررری",
    "پیوی", "چنلم", "فیلم", "فلم", "فی لم", "🤤", "😈", "💦", "🔥",
    "فیللللللللم", "ف.ی.ل.ل.ل.م", "فـيـلـلـم", "ف🔥ی✨ل✨م",
    "عَسَل خـااانوم", "ا‌س‌ت‌و‌ررری", "چ.ن.ل.م", "پـی", "پ.پ.ی",
])
def test_builtin_name_filter_detects_new_names_censorship_and_elongation(name):
    user = SimpleNamespace(first_name=name, last_name=None, username=None)
    assert ad_name_reason(user) is not None, name


def test_builtin_name_filter_checks_last_name_and_username_too():
    assert ad_name_reason(SimpleNamespace(
        first_name="سارا", last_name="فی لللللم", username=None,
    )) is not None
    assert ad_name_reason(SimpleNamespace(
        first_name="سارا", last_name=None, username="چنل_م",
    )) is not None
    assert ad_name_reason(SimpleNamespace(
        first_name="سارا", last_name="🔥", username=None,
    )) == "🔥"


@pytest.mark.parametrize("name", [
    "پیام", "پیرمرد", "پیروز", "پیمان", "پیشرو", "سپیدار",
])
def test_short_pi_name_filter_does_not_match_parts_of_normal_names(name):
    user = SimpleNamespace(first_name=name, last_name=None, username=None)
    assert ad_name_reason(user) is None, name
