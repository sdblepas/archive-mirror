"""Unit tests for file_naming.py — pure functions, no I/O, no network."""
import pytest
from src.file_naming import (
    sanitize,
    make_folder_name,
    make_track_filename,
    deduplicate_filenames,
    build_album_tag,
)


class TestSanitize:
    def test_strips_slashes(self):
        assert "/" not in sanitize("AC/DC")

    def test_strips_null_bytes(self):
        assert "\x00" not in sanitize("bad\x00name")

    def test_replaces_colon(self):
        result = sanitize("Live: Vol 1")
        assert ":" not in result

    def test_collapses_whitespace(self):
        assert sanitize("too   many   spaces") == "too many spaces"

    def test_strips_leading_trailing_dots(self):
        result = sanitize("...leading")
        assert not result.startswith(".")

    def test_empty_string_becomes_untitled(self):
        assert sanitize("") == "untitled"
        assert sanitize("   ") == "untitled"

    def test_max_len(self):
        long = "a" * 300
        assert len(sanitize(long, max_len=50)) <= 50

    def test_unicode_normalisation(self):
        # Full-width ASCII → regular ASCII via NFKC
        result = sanitize("\uff21\uff22\uff23")  # ＡＢＣ
        assert result == "ABC"


class TestMakeFolderName:
    def test_basic(self):
        assert make_folder_name("Aadam Jacobs", "2005-06-15") == "Aadam Jacobs - 2005-06-15"

    def test_unknown_date(self):
        assert make_folder_name("Aadam Jacobs", "unknown") == "Aadam Jacobs - unknown"

    def test_special_chars_in_artist(self):
        name = make_folder_name("AC/DC", "1980-01-01")
        assert "/" not in name


class TestMakeTrackFilename:
    def test_zero_padding_two_digits(self):
        name = make_track_filename(1, "Song", "Artist", total_tracks=9)
        assert name.startswith("01 - ")

    def test_zero_padding_three_digits(self):
        name = make_track_filename(1, "Song", "Artist", total_tracks=100)
        assert name.startswith("001 - ")

    def test_format(self):
        name = make_track_filename(3, "Run Like an Antelope", "Phish", total_tracks=20)
        assert name == "03 - Run Like an Antelope - Phish.flac"

    def test_sanitizes_title(self):
        name = make_track_filename(1, "Bad/Title", "Artist")
        assert "/" not in name

    def test_always_ends_in_flac(self):
        name = make_track_filename(1, "Song", "Artist")
        assert name.endswith(".flac")


class TestDeduplicateFilenames:
    def test_no_duplicates_unchanged(self):
        names = ["01 - A - X.flac", "02 - B - X.flac"]
        assert deduplicate_filenames(names) == names

    def test_duplicate_gets_suffix(self):
        names = ["01 - Same - X.flac", "01 - Same - X.flac"]
        result = deduplicate_filenames(names)
        assert result[0] == "01 - Same - X.flac"
        assert result[1] == "01 - Same - X (2).flac"

    def test_triple_duplicate(self):
        names = ["t.flac"] * 3
        result = deduplicate_filenames(names)
        assert len(set(result)) == 3

    def test_case_insensitive(self):
        names = ["Song.flac", "SONG.flac"]
        result = deduplicate_filenames(names)
        assert result[0] != result[1]


class TestBuildAlbumTag:
    def test_with_venue(self):
        tag = build_album_tag("Aadam Jacobs", "2005-06-15", "The Fillmore")
        assert tag == "Aadam Jacobs - 2005-06-15 - The Fillmore"

    def test_without_venue(self):
        tag = build_album_tag("Aadam Jacobs", "2005-06-15", None)
        assert tag == "Aadam Jacobs - 2005-06-15"
