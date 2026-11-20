"""Default channel/bio phrases, including instances with an older runtime copy."""
import os

import pytest

from modules.profile_guard import filters, matcher


NEW_PHRASES = (
    "چنلم", "کانال", "چنل", "کانالم", "میشه عضو شی", "عضو شو",
)


@pytest.mark.parametrize("list_file", [filters.channel_names, filters.bio_sentences])
def test_new_phrases_present_and_matchable_in_both_lists(list_file):
    entries = list_file()
    built = matcher.build(entries)
    for phrase in NEW_PHRASES:
        assert phrase in entries, phrase
        assert matcher.find_match(phrase, matcher.build([phrase])) is not None
        assert matcher.find_match(phrase, built) is not None


@pytest.mark.parametrize("sample,phrase", [
    ("چ.ن.ل.م", "چنلم"),
    ("کـانـال", "کانال"),
    ("چ‌ن‌ل", "چنل"),
    ("ک ا ن ا ل م", "کانالم"),
    ("میشه‌عُضو‌شی", "میشه عضو شی"),
    ("ع.ض.و ش.و", "عضو شو"),
])
def test_new_channel_and_bio_phrases_handle_separators(sample, phrase):
    assert matcher.find_match(sample, matcher.build([phrase])) is not None


def test_new_release_defaults_reach_newer_runtime_copy_without_overwriting_customizations(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for filename in (filters.CHANNEL_LIST_FILE, filters.BIO_LIST_FILE):
        path = runtime / filename
        path.write_text("عبارت اختصاصی\n# کانال\n", encoding="utf-8")
        shipped = filters.PROJECT_ROOT / "config" / filename
        newer = shipped.stat().st_mtime + 10
        os.utime(path, (newer, newer))

    monkeypatch.setattr(filters, "runtime_config_file", lambda name: runtime / name)
    filters.reload()
    try:
        for filename, get_list in (
            (filters.CHANNEL_LIST_FILE, filters.channel_names),
            (filters.BIO_LIST_FILE, filters.bio_sentences),
        ):
            assert filters._resolve(filename) == runtime / filename
            items = get_list()
            assert "عبارت اختصاصی" in items
            assert "کانال" not in items  # explicit '# کانال' still disables it
            assert all(p in items for p in NEW_PHRASES if p != "کانال")
            assert get_list() == items  # cached on the hot path
    finally:
        filters.reload()
