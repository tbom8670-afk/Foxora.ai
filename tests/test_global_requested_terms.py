"""Requested terms across name, profile-bio and global banned-word paths."""
import os
from types import SimpleNamespace

import pytest

from modules import ad_name_detector, group_banned_words_control
from modules.config_manager import ConfigManager
from modules.global_profile_terms import GLOBAL_PROFILE_TERMS
from modules.profile_guard import filters, guard, matcher, storage
from modules.spam_detector import SpamDetector


@pytest.mark.parametrize("phrase", GLOBAL_PROFILE_TERMS)
def test_each_phrase_in_all_three_real_matchers(phrase):
    assert ad_name_detector.reason(SimpleNamespace(first_name=phrase, last_name='', username=''))
    assert guard._evaluate({'about': phrase}, [], matcher.build([phrase])) == ('bio', phrase)
    config = SimpleNamespace(banned_words={phrase}, _banned_words_version=1,
                             get=lambda key, default=None: True)
    assert SpamDetector(config).check_banned_words(phrase)[0]


@pytest.mark.parametrize("digits", ["۱۸ ۱۹", "18 19", "١٨ ١٩"])
def test_digit_forms_in_each_path(digits):
    phrase = 'محرم ۱۸ ۱۹'
    sample = 'محرم ' + digits
    assert ad_name_detector.reason(SimpleNamespace(first_name=sample, last_name='', username=''))
    assert guard._evaluate({'about': sample}, [], matcher.build([phrase])) == ('bio', phrase)
    config = SimpleNamespace(banned_words={phrase}, _banned_words_version=1,
                             get=lambda key, default=None: True)
    assert SpamDetector(config).check_banned_words(sample)[0]


def test_shipped_words_are_loaded_by_default_config(tmp_path, monkeypatch):
    # A pre-existing runtime copy is merged with the shipped list, without
    # replacing the existing custom entry.
    runtime = tmp_path / 'banned_words.txt'
    runtime.write_text('واژه اختصاصی\n', encoding='utf-8')
    monkeypatch.setattr('modules.config_manager.runtime_config_file',
                        lambda name: runtime if name == 'banned_words.txt' else tmp_path / name)
    manager = ConfigManager()
    assert set(GLOBAL_PROFILE_TERMS) <= manager.banned_words
    assert 'واژه اختصاصی' in manager.banned_words


def test_new_bio_defaults_merge_with_newer_custom_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / filters.BIO_LIST_FILE
    runtime.write_text('عبارت اختصاصی\n# پهلوی\n', encoding='utf-8')
    repo = filters.PROJECT_ROOT / 'config' / filters.BIO_LIST_FILE
    newer = repo.stat().st_mtime + 10
    os.utime(runtime, (newer, newer))
    monkeypatch.setattr(filters, 'runtime_config_file', lambda name: tmp_path / name)
    filters.reload()
    try:
        loaded = filters.bio_sentences()
        assert 'عبارت اختصاصی' in loaded
        assert 'پهلوی' not in loaded  # explicitly disabled remains disabled
        assert set(GLOBAL_PROFILE_TERMS) - {'پهلوی'} <= set(loaded)
    finally:
        filters.reload()


def test_existing_group_switches_are_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'FILE', tmp_path / 'profile_guard.json')
    monkeypatch.setattr(storage, '_cache', None)
    monkeypatch.setattr(storage, '_cache_mtime', None)
    assert not storage.is_enabled('987')
    monkeypatch.setattr(group_banned_words_control, 'FILE', str(tmp_path / 'group_banned_words.json'))
    monkeypatch.setattr(group_banned_words_control, '_cache', None)
    monkeypatch.setattr(group_banned_words_control, '_cache_mtime', None)
    group_banned_words_control.disable('987')
    config = SimpleNamespace(banned_words={'محرم دی'}, _banned_words_version=1,
                             get=lambda key, default=None: True)
    assert not SpamDetector(config).check_banned_words('محرم دی', '987')[0]
    assert SpamDetector(config).check_banned_words('محرم دی')[0]
