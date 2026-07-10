"""providers.py: capability registry, message conversion, weather fallback helpers."""

import providers
from plugins.core.help import capabilities_block


class TestCapabilityRegistry:
    def test_anthropic_default(self):
        caps = providers.caps_for_model("claude-sonnet-4-6")
        assert caps.vision and caps.web_search and caps.prompt_caching and caps.documents

    def test_gemini(self):
        caps = providers.caps_for_model("gemini-2.5-pro")
        assert caps.vision and not caps.web_search and not caps.prompt_caching

    def test_deepseek(self):
        caps = providers.caps_for_model("deepseek-v4-flash")
        assert not caps.vision and caps.web_search and not caps.prompt_caching

    def test_empty_model(self):
        caps = providers.caps_for_model("")
        assert not caps.vision and not caps.web_search


class TestCapabilitiesBlock:
    def test_full_caps_claims_vision_and_search(self):
        block = capabilities_block(vision=True, web_search=True)
        assert "Bilder: kannst du sehen" in block
        assert "im Web suchen" in block
        assert "NICHT sehen" not in block

    def test_no_vision_moves_to_limits(self):
        block = capabilities_block(vision=False, web_search=True)
        assert "Bilder: kannst du sehen" not in block
        assert "Bilder kannst du NICHT sehen" in block

    def test_no_web_search(self):
        block = capabilities_block(vision=True, web_search=False)
        assert "du kannst auch im Web suchen" not in block
        assert "Im Web suchen kannst du NICHT" in block


class TestToTextMessages:
    def test_images_stripped_and_annotated(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "Was siehst du?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aaa"}},
        ]}]
        out = providers.to_text_messages(msgs, annotate_images=True)
        assert len(out) == 1
        assert "Was siehst du?" in out[0]["content"]
        assert "NICHT sehen" in out[0]["content"]

    def test_consecutive_same_role_merged(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "c"},
        ]
        out = providers.to_text_messages(msgs)
        assert [m["role"] for m in out] == ["user", "assistant"]
        assert out[0]["content"] == "a\nb"

    def test_assistant_tool_xml_stripped(self):
        msgs = [{"role": "assistant", "content": "ok <tool_calls>x</tool_calls>"}]
        out = providers.to_text_messages(msgs)
        assert "<tool_calls>" not in out[0]["content"]

    def test_documents_stripped_and_annotated(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "lies das mal"},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "x"}},
        ]}]
        out = providers.to_text_messages(msgs, annotate_images=True)
        assert "PDF-Dokument(e) angehängt" in out[0]["content"]


class TestToOpenaiMessages:
    def test_user_image_becomes_image_url(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "guck"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "xyz"}},
        ]}]
        out = providers.to_openai_messages(msgs)
        parts = out[0]["content"]
        assert parts[0] == {"type": "text", "text": "guck"}
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_assistant_images_dropped(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "hier"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
        ]}]
        out = providers.to_openai_messages(msgs)
        assert out[0]["content"] == "hier"


class TestWeatherCityExtraction:
    def test_capitalized_city(self):
        assert providers._extract_city("Wetter Hamburg Wochenende") == "Hamburg"

    def test_lowercase_chat_style(self):
        assert providers._extract_city("wetter in berlin morgen") == "berlin"

    def test_question_form(self):
        assert providers._extract_city("wie wird das Wetter am Sonntag in Köln") == "Köln"

    def test_no_city(self):
        assert providers._extract_city("wetter morgen") is None

    def test_weather_hint_gate(self):
        assert providers._WEATHER_HINT_RE.search("Wettervorhersage Kiel")
        assert not providers._WEATHER_HINT_RE.search("beste Pizza Rezepte")
