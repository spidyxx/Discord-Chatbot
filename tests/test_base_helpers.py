"""plugins.base pure helpers: split_message, clean_chat_reply, known_identities_block."""

from plugins.base import split_message, clean_chat_reply, known_identities_block


class TestSplitMessage:
    def test_short_text_single_chunk(self):
        assert split_message("hallo") == ["hallo"]

    def test_exactly_limit(self):
        text = "x" * 2000
        assert split_message(text) == [text]

    def test_splits_at_sentence_boundary(self):
        text = "Erster Satz. " + "b" * 1995
        chunks = split_message(text)
        assert chunks[0] == "Erster Satz."
        assert "".join(c.replace(" ", "") for c in chunks).startswith("ErsterSatz.")

    def test_all_chunks_within_limit(self):
        text = ("Ein Satz mit Inhalt. " * 300).strip()
        chunks = split_message(text)
        assert all(len(c) <= 2000 for c in chunks)
        # No content lost (modulo boundary whitespace)
        assert "".join(chunks).replace(" ", "") == text.replace(" ", "")

    def test_no_separator_hard_cut(self):
        text = "x" * 4500
        chunks = split_message(text)
        assert all(len(c) <= 2000 for c in chunks)
        assert sum(len(c) for c in chunks) == 4500

    def test_newline_boundary(self):
        text = "a" * 1500 + "\n" + "b" * 1000
        chunks = split_message(text)
        assert chunks[0] == "a" * 1500
        assert chunks[1] == "b" * 1000


class TestCleanChatReply:
    def test_collapses_blank_lines(self):
        assert clean_chat_reply("a\n\n\nb") == "a\nb"

    def test_strips_outer_whitespace(self):
        assert clean_chat_reply("  hallo \n") == "hallo"

    def test_single_newline_kept(self):
        assert clean_chat_reply("a\nb") == "a\nb"


class TestSendTimeStrip:
    def test_clean_chat_reply_strips_leaked_tool_markup(self):
        text = "Moment. ｜DSML｜<tool_calls>web_search</tool_calls>\n\n\nDas Wetter wird gut."
        out = clean_chat_reply(text)
        assert "DSML" not in out and "<tool_calls>" not in out
        assert "Das Wetter wird gut." in out
        assert "\n\n" not in out


class TestKnownIdentities:
    def test_empty(self):
        assert known_identities_block([]) == ""

    def test_subjects_with_aliases(self):
        mems = [
            {"type": "user", "subject": "Spidy", "aliases": ["Torsten"]},
            {"type": "user", "subject": "Anna", "aliases": []},
            {"type": "general", "content": "irrelevant"},
        ]
        block = known_identities_block(mems)
        assert "Spidy (Torsten)" in block
        assert "- Anna" in block
        assert "irrelevant" not in block
