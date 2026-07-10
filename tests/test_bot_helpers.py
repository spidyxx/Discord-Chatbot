"""Pure helpers in bot.py: import works offline via the tests/conftest.py env stubs."""

import asyncio
from types import SimpleNamespace

import bot
from plugins.base import strip_raw_tool_calls


class _FakeUsage:
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0
    input_tokens = 0
    output_tokens = 0


def _fake_anthropic_response(text="ok"):
    return SimpleNamespace(usage=_FakeUsage(), content=[SimpleNamespace(text=text)])


def test_claude_loop_web_search_is_opt_in(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _fake_anthropic_response()

    monkeypatch.setattr(bot.anthropic.messages, "create", fake_create)
    msgs = [{"role": "user", "content": "hi"}]

    async def scenario():
        out = await bot._claude_loop("sys", msgs, tier="normal")
        assert out == "ok"
        assert "tools" not in captured

        await bot._claude_loop("sys", msgs, tier="normal", use_tools=True)
        assert captured["tools"] == bot.TOOLS

    asyncio.run(scenario())


class TestStripRawToolCalls:
    def test_plain_text_untouched(self):
        assert strip_raw_tool_calls("Hallo Welt") == "Hallo Welt"

    def test_dsml_marker_removed(self):
        assert "DSML" not in strip_raw_tool_calls("Hi ｜DSML｜<tool_calls>x</tool_calls> da")

    def test_function_calls_block_removed(self):
        text = "Vorher <function_calls><invoke name=\"web_search\"></invoke></function_calls> nachher"
        out = strip_raw_tool_calls(text)
        assert "invoke" not in out and "function_calls" not in out
        assert out.startswith("Vorher") and out.endswith("nachher")

    def test_orphan_tags_removed(self):
        out = strip_raw_tool_calls("a <tool_calls> b")
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


class TestSsrfGuard:
    def test_private_ip_literals_blocked(self):
        for host in ["127.0.0.1", "10.1.2.3", "192.168.178.70", "172.16.0.1",
                     "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "fd00::1"]:
            assert bot._is_private_address(host), host

    def test_public_ips_allowed(self):
        for host in ["8.8.8.8", "142.250.180.1", "2606:4700::1111"]:
            assert not bot._is_private_address(host), host

    def test_hostnames_not_ip_literals(self):
        assert not bot._is_private_address("example.com")

    def test_url_guard_blocks_local_names_and_literals(self):
        async def scenario():
            assert await bot._url_is_private("http://localhost:8080/admin")
            assert await bot._url_is_private("http://foo.localhost/x")
            assert await bot._url_is_private("http://192.168.178.1/router")
            assert await bot._url_is_private("http://[::1]/")
            assert await bot._url_is_private("http://nonexistent-host-xyz.invalid/")

        asyncio.run(scenario())


class TestDeleteMemories:
    ADMIN, USER, BOT_UID = 1, 2, 999

    def _seed(self):
        bot.save_memories([
            {"id": "a", "user_id": self.ADMIN,   "content": "Admin-Notiz über Kekse"},
            {"id": "b", "user_id": self.USER,    "content": "User mag Kekse"},
            {"id": "c", "user_id": self.BOT_UID, "content": "Digest: Server mag Kekse"},
            {"id": "d", "user_id": self.USER,    "content": "User spielt Schach"},
        ])

    def test_privileged_keyword_deletes_any_owner(self):
        self._seed()
        assert bot.delete_memories(self.ADMIN, True, specific="Kekse") == 3
        remaining = {m["id"] for m in bot.load_memories()}
        assert remaining == {"d"}

    def test_unprivileged_keyword_only_own(self):
        self._seed()
        assert bot.delete_memories(self.USER, False, specific="Kekse") == 1
        remaining = {m["id"] for m in bot.load_memories()}
        assert remaining == {"a", "c", "d"}

    def test_privileged_bulk_stays_owner_scoped(self):
        self._seed()
        assert bot.delete_memories(self.ADMIN, True) == 1
        remaining = {m["id"] for m in bot.load_memories()}
        assert remaining == {"b", "c", "d"}

    def test_privileged_targeted_keyword_scoped_to_target(self):
        self._seed()
        assert bot.delete_memories(self.ADMIN, True, specific="Kekse", target_user_id=self.USER) == 1
        remaining = {m["id"] for m in bot.load_memories()}
        assert remaining == {"a", "c", "d"}


class TestSplitDigestReply:
    def test_summary_and_facts(self):
        raw = ("War heute einiges los, vor allem die Diskussion über Pizza.\n"
               "===FAKTEN===\n"
               "GENERAL | 'Ananas gehört auf Pizza' ist jetzt Server-Running-Gag | NONE")
        summary, facts = bot._split_digest_reply(raw)
        assert summary.startswith("War heute einiges los")
        assert len(facts) == 1 and facts[0]["type"] == "general"

    def test_keine_facts(self):
        summary, facts = bot._split_digest_reply("Netter Tag heute.\n===FAKTEN===\nKEINE")
        assert summary == "Netter Tag heute."
        assert facts == []

    def test_skip(self):
        summary, facts = bot._split_digest_reply("SKIP")
        assert summary.upper().startswith("SKIP")
        assert facts == []

    def test_missing_marker_degrades_to_summary_only(self):
        summary, facts = bot._split_digest_reply("Nur eine Zusammenfassung ohne Marker.")
        assert summary == "Nur eine Zusammenfassung ohne Marker."
        assert facts == []


class TestMemoryFilter:
    def test_keyword_relevant_two_word_overlap(self):
        words = bot._content_words("Wer will nachher Minecraft auf dem Server zocken?")
        assert bot._keyword_relevant(words, "Der Server hat einen eigenen Minecraft-Server")

    def test_keyword_relevant_single_distinctive_word(self):
        words = bot._content_words("was heißt hier eigentlich Gemütlichkeit?")
        assert bot._keyword_relevant(words, "Gemütlichkeit ist das Server-Motto")

    def test_keyword_irrelevant(self):
        words = bot._content_words("wie war dein Tag?")
        assert not bot._keyword_relevant(words, "Gemütlichkeit ist das Server-Motto")

    def test_no_haiku_call_without_trigger_memories(self, monkeypatch):
        bot.save_memories([
            {"id": "g1", "type": "general", "content": "Gemütlichkeit ist das Server-Motto",
             "user_id": 1, "date": "01.01.2026"},
            {"id": "g2", "type": "general", "content": "Freitags ist Zockerabend",
             "user_id": 1, "date": "01.01.2026"},
        ])
        calls = {"n": 0}

        async def fake_simple_call(*a, **k):
            calls["n"] += 1
            return "NONE"

        monkeypatch.setattr(bot, "_simple_call", fake_simple_call)
        block = asyncio.run(bot.build_memory_block(
            "was bedeutet Gemütlichkeit für euch?", current_speaker="Anna"))
        assert calls["n"] == 0            # no trigger memories → no API call
        assert "Server-Motto" in block    # keyword-selected general memory
        assert "Zockerabend" not in block

    def test_haiku_still_called_for_triggers(self, monkeypatch):
        bot.save_memories([
            {"id": "t1", "type": "bot", "trigger": "wenn jemand nach Rittern fragt",
             "content": "Du bist der Ritter des Servers", "user_id": 1, "date": "01.01.2026"},
        ])
        calls = {"n": 0}

        async def fake_simple_call(*a, **k):
            calls["n"] += 1
            return "t1"

        monkeypatch.setattr(bot, "_simple_call", fake_simple_call)
        block = asyncio.run(bot.build_memory_block("erzähl mal was über Ritter", current_speaker="Anna"))
        assert calls["n"] == 1
        assert "Ritter des Servers" in block


class TestEmojiReaction:
    def test_keyword_match(self, monkeypatch):
        monkeypatch.setattr(bot.random, "random", lambda: 0.0)  # pass the rate gate
        assert bot.get_emoji_reaction("hahaha das war so witzig") in ("😂", "🤣")
        assert bot.get_emoji_reaction("danke dir!") == "🙏"
        assert bot.get_emoji_reaction("herzlichen Glückwunsch!") in ("🎉", "🥳")

    def test_no_keyword_no_reaction(self, monkeypatch):
        monkeypatch.setattr(bot.random, "random", lambda: 0.0)
        assert bot.get_emoji_reaction("ich fahre nachher zur Tankstelle") is None

    def test_rate_gate(self, monkeypatch):
        monkeypatch.setattr(bot.random, "random", lambda: 1.0)  # always above rate
        assert bot.get_emoji_reaction("hahaha witzig") is None


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
