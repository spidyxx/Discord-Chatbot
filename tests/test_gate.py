"""Classify pre-gate: plugin keyword patterns that decide whether the Haiku
classifier runs at all. Under-matching makes intents unreachable, so every
user-facing phrasing from help.py must hit a pattern."""

from plugins.registry import discover, registry


def _gate():
    discover()
    gate = registry.gate_regex()
    assert gate is not None, "a plugin with INTENT_LINES lacks GATE_PATTERNS — gate disabled"
    return gate


# Phrasings from build_help_text / capabilities_block — every advertised
# command must reach the classifier.
MUST_CLASSIFY = [
    "erinnere mich in 2 Stunden an Meeting",
    "erzähl mir jeden Tag um 13 Uhr einen Witz",
    "erinnere uns jeden Freitag um 20 Uhr an den Spieleabend",
    "zeig meine Erinnerungen",
    "lösche Erinnerung 8fa3",
    "fass zusammen",
    "was hab ich verpasst?",
    "was gab's heute",
    "was ist hier los",
    "zeig mir den Songtext zu Kraftklub - Songs für Liam",
    "erzähl einen Witz",
    "täglichen Witz an",
    "shut up",
    "halt die Klappe",
    "sei mal leise",
    "was weißt du alles?",
    "vergiss dass ich Pizza mag",
    "speichere was heute passiert ist",
    "merk dir die heutige Session",
    "was kannst du so?",
    "hilfe",
]

# Plain conversation that should skip the classifier.
PLAIN_CHAT = [
    "wie geht's dir?",
    "hast du das Spiel gestern gesehen",
    "ich glaub der FC steigt ab",
    "danke dir!",
    "guten Morgen zusammen",
    "das Essen war fantastisch",
]


def test_advertised_commands_reach_classifier():
    gate = _gate()
    missed = [t for t in MUST_CLASSIFY if not gate.search(t)]
    assert not missed, f"gate would swallow these commands: {missed}"


def test_plain_chat_skips_classifier():
    gate = _gate()
    leaked = [t for t in PLAIN_CHAT if gate.search(t)]
    assert not leaked, f"plain chat unnecessarily classified: {leaked}"


def test_url_reliant_intents_have_url_patterns():
    gate = _gate()
    assert gate.search("fass das mal zusammen https://youtube.com/watch?v=abcdefghijk")
    assert gate.search("https://www.ardsounds.de/episode/urn:ard:episode:abc123")
    assert gate.search("https://genius.com/Kraftklub-lyrics")
