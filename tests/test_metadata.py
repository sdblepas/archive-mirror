"""Unit tests for metadata.py — pure parsing functions, no network."""
import pytest
from src.metadata import _parse, _normalise_date, _parse_track_number, _coerce_str


class TestNormaliseDate:
    def test_full_date(self):
        assert _normalise_date("1990-11-09") == "1990-11-09"

    def test_year_month(self):
        assert _normalise_date("2005-06") == "2005-06"

    def test_year_only(self):
        assert _normalise_date("1999") == "1999"

    def test_datetime_strips_time(self):
        assert _normalise_date("2005-06-15T20:00:00") == "2005-06-15"

    def test_empty_returns_unknown(self):
        assert _normalise_date("") == "unknown"
        assert _normalise_date(None) == "unknown"

    def test_garbage_returns_unknown(self):
        assert _normalise_date("not a date") == "unknown"

    def test_extracts_year_from_concatenated_digits(self):
        # "20010315" contains no separator — the year regex extracts the first 4 digits
        assert _normalise_date("20010315") == "2001"


class TestParseTrackNumber:
    def test_integer_string(self):
        assert _parse_track_number("1") == 1

    def test_zero_padded(self):
        assert _parse_track_number("07") == 7

    def test_fraction_format(self):
        assert _parse_track_number("3/12") == 3

    def test_none(self):
        assert _parse_track_number(None) is None

    def test_non_numeric(self):
        assert _parse_track_number("n/a") is None


class TestCoerceStr:
    def test_plain_string(self):
        assert _coerce_str("hello") == "hello"

    def test_list_takes_first(self):
        assert _coerce_str(["first", "second"]) == "first"

    def test_empty_list_returns_none(self):
        assert _coerce_str([]) is None

    def test_none_returns_none(self):
        assert _coerce_str(None) is None

    def test_blank_string_returns_none(self):
        assert _coerce_str("   ") is None


class TestParse:
    """Test the full _parse() function against synthetic IA metadata blobs."""

    def _make_raw(self, meta: dict, files: list) -> dict:
        return {"metadata": meta, "files": files}

    def test_basic_concert(self):
        raw = self._make_raw(
            {"creator": "Aadam Jacobs", "date": "2005-06-15", "title": "Aadam Jacobs Live"},
            [
                {"name": "track01.flac", "format": "Flac", "size": "1000",
                 "md5": "abc", "sha1": "def", "title": "Opening Song", "track": "1"},
                {"name": "track02.flac", "format": "Flac", "size": "2000",
                 "md5": "ghi", "sha1": "jkl", "title": "Closing Song", "track": "2"},
            ],
        )
        info = _parse("test-id", raw)
        assert info.artist == "Aadam Jacobs"
        assert info.date == "2005-06-15"
        assert len(info.flac_tracks) == 2
        assert info.flac_tracks[0].title == "Opening Song"
        assert info.flac_tracks[0].track_number == 1

    def test_no_flac_files(self):
        raw = self._make_raw(
            {"creator": "Artist", "date": "2000-01-01"},
            [{"name": "track.mp3", "format": "VBR MP3", "size": "500"}],
        )
        info = _parse("test-id", raw)
        assert info.flac_tracks == []

    def test_mixed_formats_keeps_only_flac(self):
        raw = self._make_raw(
            {"creator": "Artist", "date": "2000-01-01"},
            [
                {"name": "t1.flac", "format": "Flac", "size": "1000", "track": "1"},
                {"name": "t2.mp3",  "format": "VBR MP3", "size": "500"},
            ],
        )
        info = _parse("test-id", raw)
        assert len(info.flac_tracks) == 1
        assert info.flac_tracks[0].ia_filename == "t1.flac"

    def test_missing_track_numbers_filled(self):
        raw = self._make_raw(
            {"creator": "Artist", "date": "2000-01-01"},
            [
                {"name": "a.flac", "format": "Flac", "size": "100"},
                {"name": "b.flac", "format": "Flac", "size": "100"},
            ],
        )
        info = _parse("test-id", raw)
        numbers = [t.track_number for t in info.flac_tracks]
        assert None not in numbers
        assert len(set(numbers)) == 2  # all unique

    def test_creator_as_list(self):
        """IA sometimes returns metadata values as lists."""
        raw = self._make_raw(
            {"creator": ["Aadam Jacobs", "Guest Artist"], "date": "2001-01-01"},
            [],
        )
        info = _parse("test-id", raw)
        assert info.artist == "Aadam Jacobs"

    def test_missing_date_becomes_unknown(self):
        raw = self._make_raw({"creator": "Artist"}, [])
        info = _parse("test-id", raw)
        assert info.date == "unknown"

    def test_venue_extracted(self):
        raw = self._make_raw(
            {"creator": "Artist", "date": "2000-01-01", "venue": "The Fillmore"},
            [],
        )
        info = _parse("test-id", raw)
        assert info.venue == "The Fillmore"

    def test_tracks_sorted_by_number(self):
        raw = self._make_raw(
            {"creator": "Artist", "date": "2000-01-01"},
            [
                {"name": "c.flac", "format": "Flac", "size": "100", "track": "3", "title": "C"},
                {"name": "a.flac", "format": "Flac", "size": "100", "track": "1", "title": "A"},
                {"name": "b.flac", "format": "Flac", "size": "100", "track": "2", "title": "B"},
            ],
        )
        info = _parse("test-id", raw)
        titles = [t.title for t in info.flac_tracks]
        assert titles == ["A", "B", "C"]
