"""Pure helpers in bot.py: import works offline via the tests/conftest.py env stubs."""

import bot


class TestStripRawToolCalls:
    def test_plain_text_untouched(self):
        assert bot._strip_raw_tool_calls("Hallo Welt") == "Hallo Welt"

    def test_dsml_marker_removed(self):
        assert "DSML" not in bot._strip_raw_tool_calls("Hi ｜DSML｜<tool_calls>x</tool_calls> da")

    def test_function_calls_block_removed(self):
        text = "Vorher <function_calls><invoke name=\"web_search\"></invoke></function_calls> nachher"
        out = bot._strip_raw_tool_calls(text)
        assert "invoke" not in out and "function_calls" not in out
        assert out.startswith("Vorher") and out.endswith("nachher")

    def test_orphan_tags_removed(self):
        out = bot._strip_raw_tool_calls("a <tool_calls> b")
        assert "<tool_calls>" not in out


class TestDuplicateMemory:
    def test_near_duplicate_detected(self):
        existing = [{"id": "1", "subject": None, "content": "Spidy arbeitet gern mit Python und Discord Bots"}]
        dup = bot._is_duplicate_memory("Spidy arbeitet gern mit Discord Bots und Python", None, existing)
        assert dup is not None

    def test_different_fact_passes(self):
        existing = [{"id": "1", "subject": None, "content": "Spidy mag Python"}]
        assert bot._is_duplicate_memory("Anna spielt Gitarre seit Jahren", None, existing) is None

    def test_subject_scoping(self):
        existing = [{"id": "1", "subject": "Anna", "content": "spielt gerne Schach im Verein"}]
        # Same words, different subject → not a duplicate
        assert bot._is_duplicate_memory("spielt gerne Schach im Verein", "Ben", existing) is None


class TestUrlHelpers:
    def test_plain_urls_extracted(self):
        urls = bot._plain_webpage_urls("Guck mal https://example.com/artikel und https://heise.de/x")
        assert urls == ["https://example.com/artikel", "https://heise.de/x"]

    def test_youtube_and_images_excluded(self):
        text = "https://youtube.com/watch?v=abcdefghijk https://x.com/a.png https://example.com/b"
        assert bot._plain_webpage_urls(text) == ["https://example.com/b"]

    def test_cap_at_max(self):
        text = " ".join(f"https://example.com/{i}" for i in range(5))
        assert len(bot._plain_webpage_urls(text)) == bot.MAX_URLS_PER_MSG


def test_no_unanchored_oldest_first_history_calls():
    """history(oldest_first=True) without after= paginates from the channel's
    FIRST message ever — regression guard for the should_respond context bug."""
    import re
    from pathlib import Path

    root = Path(bot.__file__).parent
    sources = [root / "bot.py", *root.glob("plugins/**/*.py")]
    offenders = []
    for path in sources:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\.history\(([^)]*)\)", src, re.DOTALL):
            args = m.group(1)
            if "oldest_first=True" in args and "after" not in args:
                offenders.append(f"{path.name}: history({args.strip()})")
    assert not offenders, f"Unanchored oldest_first=True history calls: {offenders}"


class TestResolveMentions:
    def test_replaces_both_syntaxes(self):
        class M:
            id = 42
            display_name = "Anna"
        out = bot.resolve_mentions("Hi <@42> und <@!42>", [M()])
        assert out == "Hi @Anna und @Anna"
