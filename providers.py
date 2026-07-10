"""Model-provider layer: capability registry + non-Anthropic backends.

bot.py routes every LLM call through _claude_loop/_simple_call; calls for
gemini-*/deepseek-* model names and the local Ollama slot land here. Each
provider has an explicit capability profile (caps_for_model) so the rest of
the code — and the system prompt via capabilities_block — can stop guessing
what the active model can do.

Capability matrix:
                    vision  web_search  prompt_caching  documents
  Anthropic (claude)  ✅       ✅ (server tool)  ✅        ✅
  Gemini              ✅       ❌               ❌        ❌
  DeepSeek            ❌       ✅ (DDG loop)     ❌        ❌
  Ollama (local)      ❌       ❌               ❌        ❌
  Gemini receives images as OpenAI-style image_url data URIs
  (to_openai_messages) through Google's OpenAI-compatible endpoint.

DeepSeek V4 notes (deepseek-v4-pro / deepseek-v4-flash via OpenAI-compatible
endpoint at api.deepseek.com/v1): text-only — the API rejects image_url
blocks (confirmed in DeepSeek docs: type="image" is "Not Supported").
Web search is client-side: DuckDuckGo + wttr.in weather fallback through a
function-calling loop (max 4 rounds). No prompt caching (cost impact only).
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from plugins.base import strip_raw_tool_calls

log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL  = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
LOCAL_MODEL      = os.environ.get("LOCAL_MODEL", "")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# Gemini/DeepSeek reasoning models spend hidden reasoning tokens against the
# output budget. The caller's max_tokens is multiplied so deep reasoning still
# leaves headroom for a complete visible reply (capped at 65536, Gemini Pro's
# hard ceiling).
REASONING_TOKEN_MULTIPLIER = int(os.environ.get("REASONING_TOKEN_MULTIPLIER", "16"))
_OUTPUT_TOKEN_CEILING = 65536


def _expand_budget(max_tokens: int) -> int:
    return min(max_tokens * REASONING_TOKEN_MULTIPLIER, _OUTPUT_TOKEN_CEILING)


# Set by bot.py after fetch_webpage_text is defined — used to enrich DDG
# results with the top hit's page text without a circular import.
webpage_fetcher = None

# ── Capability registry ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelCaps:
    vision:         bool = True   # image blocks reach the model
    web_search:     bool = True   # server tool (Anthropic) or DDG loop (DeepSeek)
    prompt_caching: bool = True
    documents:      bool = True   # PDF document blocks


def caps_for_model(model: str) -> ModelCaps:
    """Capability profile by model-name prefix. Anthropic is the default."""
    if not model:
        return ModelCaps(vision=False, web_search=False, prompt_caching=False, documents=False)
    if model.startswith("gemini"):
        return ModelCaps(vision=True, web_search=False, prompt_caching=False, documents=False)
    if model.startswith("deepseek"):
        return ModelCaps(vision=False, web_search=True, prompt_caching=False, documents=False)
    if model == LOCAL_MODEL and model:
        return ModelCaps(vision=False, web_search=False, prompt_caching=False, documents=False)
    return ModelCaps()


# ── Clients (lazy singletons; only created when the API key/URL is set) ──────

_ollama_client = None
if OLLAMA_BASE_URL and LOCAL_MODEL:
    from openai import AsyncOpenAI
    _ollama_client = AsyncOpenAI(base_url=f"{OLLAMA_BASE_URL}/v1", api_key="ollama")

_gemini_client = None
if GEMINI_API_KEY:
    from openai import AsyncOpenAI as _AsyncOpenAI
    _gemini_client = _AsyncOpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=GEMINI_API_KEY,
    )

_deepseek_client = None
if DEEPSEEK_API_KEY:
    from openai import AsyncOpenAI as _AsyncOpenAI2
    _deepseek_client = _AsyncOpenAI2(
        base_url="https://api.deepseek.com/v1",
        api_key=DEEPSEEK_API_KEY,
    )

# ── Message conversion ────────────────────────────────────────────────────────


def to_openai_messages(messages: list) -> list:
    """Convert Anthropic-style messages to OpenAI chat format with vision.

    Anthropic image blocks in user messages become OpenAI image_url blocks.
    Images in assistant messages are stripped (OpenAI-compatible endpoints
    only accept images in user role). Merges consecutive same-role text
    messages. Also strips raw tool-call XML from assistant messages.
    """
    result = []
    for msg in messages:
        content = msg["content"]
        role = msg["role"]
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = []
            text_parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b["text"])
                elif b.get("type") == "image" and role == "user":
                    src = b.get("source", {})
                    mediatype = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mediatype};base64,{data}"}
                    })
                elif b.get("type") == "document":
                    # Anthropic-only block type — replaced with an honest note.
                    text_parts.append("[HINWEIS: PDF-Dokument angehängt — du kannst PDFs NICHT lesen. Sag das ehrlich.]")
            if text_parts:
                parts.insert(0, {"type": "text", "text": " ".join(text_parts)})
            if not parts:
                continue
        else:
            continue

        if role == "assistant":
            for p in parts:
                if p["type"] == "text":
                    p["text"] = strip_raw_tool_calls(p["text"])
            parts = [p for p in parts if p["type"] != "text" or p["text"].strip()]
        if not parts:
            continue

        # If only one text part, use string content; otherwise use array
        if len(parts) == 1 and parts[0]["type"] == "text":
            new_content = parts[0]["text"]
        else:
            new_content = parts

        if result and result[-1]["role"] == role:
            prev = result[-1]["content"]
            if isinstance(prev, str) and isinstance(new_content, str):
                result[-1]["content"] = prev + "\n" + new_content
            else:
                result.append({"role": role, "content": new_content})
        else:
            result.append({"role": role, "content": new_content})
    return result


def to_text_messages(messages: list, annotate_images: bool = False) -> list:
    """Flatten Anthropic-style messages to text for non-vision models.

    Strips image blocks and cache_control. Merges consecutive same-role
    messages. Also strips raw tool-call XML from assistant messages.

    If annotate_images is True, adds [HINWEIS: N Bild(er) ...] markers so
    text-only models know images exist but can honestly say they can't see them.
    """
    result = []
    for msg in messages:
        content = msg["content"]
        image_count = 0
        doc_count = 0
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(b["text"])
                elif b.get("type") == "image":
                    image_count += 1
                elif b.get("type") == "document":
                    doc_count += 1
            text = " ".join(texts).strip()
        else:
            continue
        if annotate_images and image_count:
            note = f"[HINWEIS: {image_count} Bild(er) angehängt — du kannst Bilder NICHT sehen, nur Text. Sag ehrlich, dass du das Bild nicht sehen kannst.]"
            text = f"{text}\n{note}" if text else note
        if annotate_images and doc_count:
            note = f"[HINWEIS: {doc_count} PDF-Dokument(e) angehängt — du kannst PDFs NICHT lesen. Sag das ehrlich.]"
            text = f"{text}\n{note}" if text else note
        if not text:
            continue
        if msg["role"] == "assistant":
            text = strip_raw_tool_calls(text)
            if not text:
                continue
        if result and result[-1]["role"] == msg["role"]:
            result[-1]["content"] += "\n" + text
        else:
            result.append({"role": msg["role"], "content": text})
    return result


# ── Ollama (local) ────────────────────────────────────────────────────────────


async def local_call(system: str, messages: list, max_tokens: int) -> str:
    # annotate_images: the model can't see stripped images and must know they
    # exist — otherwise "Was siehst du auf diesem Bild?" produces hallucinated
    # descriptions instead of an honest "kann ich nicht sehen".
    openai_messages = [{"role": "system", "content": system}] + to_text_messages(messages, annotate_images=True)
    response = await _ollama_client.chat.completions.create(
        model=LOCAL_MODEL, messages=openai_messages, max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


# ── Gemini ────────────────────────────────────────────────────────────────────


async def gemini_call(system: str, messages: list, max_tokens: int, model: str) -> str:
    # Gemini is vision-capable: pass images through as image_url data URIs
    # instead of stripping them (Google's OpenAI-compatible endpoint accepts
    # the standard OpenAI vision format).
    openai_messages = [{"role": "system", "content": system}] + to_openai_messages(messages)
    response = await _gemini_client.chat.completions.create(
        model=model, messages=openai_messages, max_tokens=_expand_budget(max_tokens),
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        finish = getattr(response.choices[0], "finish_reason", "?")
        log.warning(f"Empty reply from {model} (finish_reason={finish}, usage={response.usage})")
    return text


# ── Client-side web search for DeepSeek (DuckDuckGo HTML, no API key) ─────────

_DEEPSEEK_TOOLS = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web via DuckDuckGo for current information. "
            "Use simple keyword queries (2-5 words), NO operators like site:, AND, OR, quotes. "
            "For weather: 'Wetter Stadtname' or 'Wetter Stadtname Wochenende'. "
            "Read results carefully — the answer is often in the first results. "
            "Make at most 2 searches, then answer from what you have."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Simple keyword search query, no operators"}
            },
            "required": ["query"]
        }
    }
}]


async def _ddg_search(query: str) -> str:
    """Search DuckDuckGo via duckduckgo_search library (handles everything reliably)."""
    try:
        from duckduckgo_search import DDGS
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=5))
        )
        # If no results, retry with a broader query (strip quotes and date specifics)
        if not results:
            broader = query.replace('"', '').strip()
            broader = re.sub(r'\d{1,2}[\.\s]+(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|Jän|Feb|Mär|Apr|Jun|Jul|Aug|Sep|Okt|Nov|Dez)\s*\d{4}', '', broader)
            broader = re.sub(r'\d{1,2}\.\d{1,2}\.\d{4}', '', broader)
            broader = " ".join(broader.split())
            if broader != query.replace('"', '').strip():
                results = await asyncio.to_thread(
                    lambda: list(DDGS().text(broader, max_results=5))
                )
    except Exception as e:
        log.warning(f"DDG search failed for '{query[:60]}': {e}")
        return ""

    lines = []
    for r in results:
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"- {title}\n  {body}\n  {href}")

    # Fetch top result's page content for richer context
    if results and results[0].get("href") and webpage_fetcher is not None:
        try:
            text = await webpage_fetcher(results[0]["href"])
            if text:
                lines.insert(0, f"[Page content from {results[0]['href']}]:\n{text[:3000]}")
        except Exception:
            pass

    if not lines:
        # DDGS returned nothing — try wttr.in as last-resort weather fallback
        wttr = await _wttr_fallback(query)
        if wttr:
            lines.append(wttr)

    return "\n".join(lines) if lines else ""


_WEATHER_HINT_RE = re.compile(r'(?i)wetter|temperatur|grad|regen|vorhersage|wochenende')
# Words that are part of the weather question, not the place name.
_WEATHER_NOISE_RE = re.compile(
    r'(?i)\b(wetter|temperatur(?:en)?|vorhersage|prognose|regen|grad|celsius|'
    r'wochenende|morgen|heute|übermorgen|montag|dienstag|mittwoch|donnerstag|'
    r'freitag|samstag|sonntag|in|im|am|an|auf|für|bei|von|nach|der|die|das|'
    r'den|und|wie|wird|ist|es|was|nächste[nrs]?|diese[nrs]?|woche)\b'
)


def _extract_city(query: str) -> str | None:
    """Best-effort place-name extraction from a weather query. Users type
    lowercase in chat ('wetter in berlin morgen'), so capitalization is a
    preference, not a requirement."""
    cleaned = _WEATHER_NOISE_RE.sub(" ", query.replace('"', ""))
    words = [w.strip(".,!?:;") for w in cleaned.split()]
    candidates = [w for w in words if len(w) > 2]
    if not candidates:
        return None
    capitalized = [w for w in candidates if w[0].isupper()]
    return (capitalized or candidates)[0]


async def _wttr_fallback(query: str) -> str | None:
    """wttr.in one-liner forecast when DDG yields nothing for a weather query."""
    if not _WEATHER_HINT_RE.search(query):
        return None
    city = _extract_city(query)
    if not city:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://wttr.in/{quote(city)}?format=4",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                text = (await resp.text()).strip()
        return f"wttr.in forecast for {city}: {text}" if text else None
    except Exception:
        return None


# ── DeepSeek ──────────────────────────────────────────────────────────────────


async def deepseek_call(system: str, messages: list, max_tokens: int, model: str,
                        use_tools: bool = True) -> str:
    """Call DeepSeek via OpenAI-compatible endpoint with function-calling web search.

    DeepSeek V4 is text-only — images are stripped and replaced with
    [HINWEIS: ...] annotations via to_text_messages(). See the module
    docstring for the full capability matrix.
    """
    expanded = _expand_budget(max_tokens)
    openai_messages = [{"role": "system", "content": system}] + to_text_messages(messages, annotate_images=True)

    if not use_tools:
        response = await _deepseek_client.chat.completions.create(
            model=model, messages=openai_messages, max_tokens=expanded,
        )
        text = strip_raw_tool_calls((response.choices[0].message.content or "").strip())
        if not text:
            log.warning(f"Empty reply from {model} (no-tools call, usage={response.usage})")
        return text

    for _ in range(4):  # max 4 tool-call rounds
        response = await _deepseek_client.chat.completions.create(
            model=model, messages=openai_messages, max_tokens=expanded,
            tools=_DEEPSEEK_TOOLS, tool_choice="auto",
        )
        msg = response.choices[0].message
        finish = getattr(response.choices[0], "finish_reason", "")

        if not msg.tool_calls:
            text = (msg.content or "").strip()
            if not text:
                log.warning(f"Empty reply from {model} (finish_reason={finish}, usage={response.usage})")
            # Strip any raw XML tool-call syntax the model may have hallucinated into the text
            text = strip_raw_tool_calls(text)
            return text

        # Execute tool calls
        openai_messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                }
                for tc in msg.tool_calls
            ]
        })
        for tc in msg.tool_calls:
            if tc.function.name == "web_search":
                import json as _json
                try:
                    args = _json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                query = args.get("query", "")
                log.info(f"DeepSeek web_search: '{query[:80]}'")
                result = await _ddg_search(query)
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result or "(no results)",
                })
            else:
                # Every tool_call_id needs a tool message, or the next request
                # is rejected as an invalid sequence. Hallucinated tool names
                # get an error result instead of crashing the whole reply.
                log.warning(f"DeepSeek called unknown tool {tc.function.name!r}")
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"Error: unknown tool '{tc.function.name}'. Only 'web_search' exists.",
                })

    # Max rounds reached — one final call without tools
    response = await _deepseek_client.chat.completions.create(
        model=model, messages=openai_messages, max_tokens=expanded,
    )
    text = strip_raw_tool_calls((response.choices[0].message.content or "").strip())
    if not text:
        log.warning(f"Empty reply from {model} after max rounds (raw was {len(response.choices[0].message.content or '')} chars)")
    return text or "(no response — tool loop exhausted all rounds)"
