"""_parse_snapshot_facts: the BOT|USER|FLAVOR|GENERAL line format."""

from plugins.core.snapshot import _parse_snapshot_facts


def test_bot_fact_with_trigger_and_expiry():
    facts = _parse_snapshot_facts("BOT | Ist Ritter vom Server | wenn jemand Ritter sagt | 01.01.2027")
    assert facts == [{
        "type": "bot", "content": "Ist Ritter vom Server",
        "trigger": "wenn jemand Ritter sagt", "expires": "01.01.2027",
    }]


def test_bot_fact_none_fields():
    facts = _parse_snapshot_facts("BOT | Mag Kekse | NONE | NONE")
    assert facts[0]["trigger"] is None
    assert facts[0]["expires"] is None


def test_user_fact_with_aliases():
    facts = _parse_snapshot_facts("USER | Spidy | Torsten, Spider | Heißt eigentlich Torsten")
    assert facts == [{
        "type": "user", "subject": "Spidy",
        "aliases": ["Torsten", "Spider"], "content": "Heißt eigentlich Torsten",
    }]


def test_flavor_fact():
    facts = _parse_snapshot_facts("FLAVOR | Anna | NONE | Trinkt gern Mate | 17.07.2026")
    f = facts[0]
    assert f["type"] == "user" and f["flavor"] is True
    assert f["expires"] == "17.07.2026"


def test_general_fact():
    facts = _parse_snapshot_facts("GENERAL | 'Gemütlichkeit' ist das Server-Motto | NONE")
    assert facts[0]["type"] == "general"
    assert facts[0]["expires"] is None


def test_garbage_and_empty_lines_skipped():
    text = "\n".join([
        "",
        "# Kommentar",
        "Fließtext ohne Format",
        "BOT | Gültiger Fakt | NONE | NONE",
        "USER | zu wenige Teile",
    ])
    facts = _parse_snapshot_facts(text)
    assert len(facts) == 1
    assert facts[0]["content"] == "Gültiger Fakt"
