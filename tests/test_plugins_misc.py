"""Deterministic plugin logic: joke picking/time parsing, CDU formatting, reminders."""

from plugins.core import joke as joke_mod
from plugins.core.joke import _parse_time
from plugins.core.cdu import _fmt_hm, _CDU_RE, _CDU_RESET_RE
from plugins.core.reminders import _fmt_duration
from plugins.core.lyrics import parse_query, _parse_artist_title


class TestJokeTimeParse:
    def test_hh_mm(self):
        assert _parse_time("18:30") == (18, 30)

    def test_hh_uhr(self):
        assert _parse_time("um 9 Uhr bitte") == (9, 0)

    def test_invalid_hour(self):
        assert _parse_time("25:00") is None

    def test_no_time(self):
        assert _parse_time("keine zeit") is None


class TestJokeCycle:
    def test_no_repeat_until_exhausted(self, monkeypatch):
        monkeypatch.setattr(joke_mod, "JOKES", ["w1", "w2", "w3"])
        cfg = {"enabled": True, "hour": 18, "minute": 0, "told": []}
        monkeypatch.setattr(joke_mod, "_load_cfg", lambda: cfg)
        monkeypatch.setattr(joke_mod, "_save_cfg", lambda c: cfg.update(c))
        picked = {joke_mod._pick_joke() for _ in range(3)}
        assert picked == {"w1", "w2", "w3"}

    def test_cycle_resets_without_backtoback(self, monkeypatch):
        monkeypatch.setattr(joke_mod, "JOKES", ["w1", "w2"])
        cfg = {"enabled": True, "hour": 18, "minute": 0, "told": [0, 1]}
        monkeypatch.setattr(joke_mod, "_load_cfg", lambda: cfg)
        monkeypatch.setattr(joke_mod, "_save_cfg", lambda c: cfg.update(c))
        # List exhausted; next pick must avoid the last-told joke (index 1)
        assert joke_mod._pick_joke() == "w1"


class TestCdu:
    def test_fmt_under_minute(self):
        assert _fmt_hm(30) == "weniger als 1 Minute"

    def test_fmt_hours(self):
        assert _fmt_hm(3 * 3600 + 5 * 60) == "3h 5min"

    def test_fmt_days(self):
        assert _fmt_hm(2 * 86400 + 3 * 3600) == "2T 3h"

    def test_word_boundary(self):
        assert _CDU_RE.search("Die CDU schon wieder")
        assert not _CDU_RE.search("procedure")

    def test_reset_keywords(self):
        assert _CDU_RESET_RE.search("cdu reset weil Grund")
        assert not _CDU_RESET_RE.search("cdu stand")


class TestReminderFormat:
    def test_minutes(self):
        assert _fmt_duration(120) == "2 Minute(n)"

    def test_hours(self):
        assert _fmt_duration(7200) == "2 Stunde(n)"

    def test_weeks(self):
        assert _fmt_duration(604800 * 2) == "2 Woche(n)"


class TestLyricsParse:
    def test_artist_title(self):
        assert _parse_artist_title("Kraftklub - Songs für Liam") == ("Kraftklub", "Songs für Liam")

    def test_missing_dash(self):
        assert _parse_artist_title("Kraftklub Songs") is None

    def test_genius_url_wins(self):
        q = parse_query("egal", "https://genius.com/Kraftklub-ich-will-nicht-nach-berlin-lyrics", None)
        assert q.url and "genius.com" in q.url

    def test_plain_query(self):
        q = parse_query("AnnenMayKantereit - Pocahontas", "", None)
        assert (q.artist, q.title) == ("AnnenMayKantereit", "Pocahontas")
