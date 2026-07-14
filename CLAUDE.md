# Marvin — Discord Bot

## Stack
- Python 3.12, discord.py 2.7.1, Anthropic SDK (Claude API)
- Entry points: `bot.py` (Discord loop) + `providers.py` (non-Anthropic model backends & capability registry)
- Runs in Docker; see `Dockerfile`; deployed via `deploy.sh` (post-commit hook — **every git commit deploys Marvin to production**; Bot 2/Snoop is disabled and only deployed with an explicit `deploy.sh snoop`)
- All state in JSON files under `DATA_DIR` (default `/app/data`)
- Tests: `pytest tests/` (see `requirements-dev.txt`); pure-Python helpers are covered, run before every commit

## Required env vars
| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `MAIN_CHANNEL_IDS` | Comma-separated channel IDs for active participation |

See `.env.example` for all optional variables.

## Models

Four model slots, each independently configurable:

| Slot | Env var | Default |
|---|---|---|
| `local` | `LOCAL_MODEL` + `OLLAMA_BASE_URL` | *(disabled)* |
| `cheap` | `CHEAP_MODEL` | haiku |
| `normal` | `NORMAL_MODEL` | sonnet |
| `expensive` | `EXPENSIVE_MODEL` | sonnet |

### Provider layer (`providers.py`)

All non-Anthropic backends live in `providers.py`: client init, message-format conversion, the DeepSeek DDG web-search loop, weather fallback, token-usage accounting, and an explicit **capability registry** (`caps_for_model`). Model routing is by name prefix (`gemini-*`, `deepseek-*`, the `local` slot → Ollama; everything else → Anthropic).

| | vision | web search | prompt caching | PDF documents |
|---|---|---|---|---|
| Anthropic (`claude-*`) | ✅ | ✅ server tool | ✅ | ✅ |
| Gemini (`gemini-*` via `GEMINI_API_KEY`) | ✅ image_url data URIs | ❌ | ❌ | ❌ |
| DeepSeek (`deepseek-*` via `DEEPSEEK_API_KEY`) | ❌ (stripped + annotated) | ✅ DDG + wttr.in loop | ❌ | ❌ |
| Ollama (`LOCAL_MODEL` + `OLLAMA_BASE_URL`) | ❌ (stripped + annotated) | ❌ | ❌ | ❌ |

The system prompt's `capabilities_block(vision=, web_search=, documents=)` is rendered from these caps — a tier backed by a model without vision is told it cannot see images instead of hallucinating. When adding a provider, extend `caps_for_model` and the call routing in `_claude_loop`/`_simple_call`; the capabilities block follows automatically.

Gemini/DeepSeek reasoning models spend hidden reasoning tokens against the output budget; the caller's `max_tokens` is multiplied by `REASONING_TOKEN_MULTIPLIER` (default 16, env-configurable, capped at 65536).

Token usage is recorded per day and model in `DATA_DIR/usage_stats.json` (90 days); a summary is logged daily at digest time.

Each feature is assigned a tier via its own env var (e.g. `CLASSIFY_TIER=local`). Defaults:

| Env var | Default | Feature |
|---|---|---|
| `MAIN_TIER` | `expensive` | Main channel responses |
| `MENTION_TIER` | `normal` | Mention-only channel responses |
| `SHOULD_RESPOND_TIER` | `cheap` | Passive-channel SKIP/RESPOND gate (reply itself uses `MAIN_TIER`) |
| `CLASSIFY_TIER` | `cheap` | Intent classification |
| `MEMORY_FILTER_TIER` | `cheap` | Trigger-memory relevance filtering |
| `PROACTIVE_TIER` | `expensive` | Proactive messages |
| `DIGEST_SUMMARY_TIER` | `expensive` | Daily digest (single call: summary + fact extraction) |

Removed (now pure Python, no API call): emoji reactions (keyword map in `bot.py`), general-memory relevance (keyword overlap), reminder PROMPT/NOTIFY mode (classified in the same REMINDER call).

## Architecture

### Channel modes
- **Main channels** (`MAIN_CHANNEL_IDS`): full personality, memory injection, passive autonomous responses
- **Other channels**: neutral prompt, mention-only

### Status messages
Discord presence/status strings rotate from `statuses.txt` at the repo root (one per line, `#` for comments). `bot.py` loads it at startup via `_load_statuses()`. `deploy.sh` first-seeds the file then preserves server-side edits via `--ignore-existing`, identical to the plugin `.cfg` handling — so per-deployment customisation (e.g. a Snoop bot with stoner statuses) survives subsequent deploys.

### System prompt
`build_system_prompt()` assembles: always-on bot facts + base prompt + capabilities block + current date (German weekday, `DD.MM.YYYY`, using `TIMEZONE`). Everything in it is stable across a day so the cached prefix survives between calls. Time-of-day is **not** in the system prompt — it's added per-message via `[HH:MM]` prefixes in `fetch_context()` and `ask_claude()`. **Per-message memories are not in the system prompt either**: `ask_claude()` appends the `build_memory_block()` selection as a text block on the *final user message*, after both cache breakpoints, because the selection changes per message and would otherwise invalidate the whole cache.

### Chat reply post-processing
`_clean_chat_reply()` collapses multiple blank lines (`\n\n+` → `\n`) before all conversational `channel.send` / `message.reply` calls. Plugin replies (summaries etc.) bypass this and are sent as-is.

### Prompt caching
`_claude_loop` applies `cache_control: ephemeral` to the system prompt and the last history message, and logs `Cache [model]: write/read/uncached` per call at INFO. The cache is a strict prefix match, so everything ahead of those breakpoints must stay byte-identical between calls. Three mechanisms in `fetch_context()` enforce that:
- **Anchored window** (`_ctx_anchor`): the history window starts at a fixed message id and grows, instead of sliding by one each message; when it reaches `CONTEXT_WINDOW` it re-anchors to the newest half (one deliberate cache miss).
- **Frozen truncation boundary** (`_ctx_trunc_before`): which old user messages render truncated is decided at re-anchor time, not relative to the newest message.
- **Tail-only reactions**: reaction counts are rendered only on messages newer than the truncation boundary, since counts change over time.

**Known accepted gap**: the per-history image budget (`MAX_CONTEXT_IMAGES`, newest-first). When more images exist in the window than the budget allows and a new image arrives, an older message loses its embedded blocks — mid-history bytes change and the prefix cache misses once. Accepted trade-off: the bot must always see the newest images (see design intent in project memory).

**Do not modify `_claude_loop`, `fetch_context`, or `build_system_prompt` without understanding these invariants** — anything that rewrites history bytes or the system prompt per call silently turns every request into a full-price cache write.

### web_search opt-in
The Anthropic `web_search` server tool is attached only where searching helps: conversational replies (`ask_claude`) and prompt-mode reminders. Evaluation, digest, summaries, snapshot and proactive calls run without tools (`use_tools=False`, the default in `_claude_loop`). DeepSeek's DDG loop honors the same flag.

### Intent classification
`classify_intent()` uses the `cheap` tier to classify each @mention into an intent label (REMINDER, SUMMARY, etc.). The classifier prompt is built dynamically: a static preamble + plugin-contributed lines + a static footer. Plugins register their own intent labels and prompt lines — see plugin conventions below.

For direct replies and follow-up-window messages the classifier input additionally carries the bot message the user is reacting to as a `[Kontext …]` line (`_classify_input()`), and the footer instructs that commands only count from the user's own text — otherwise meta-comments like "wasn das für ein Witz?" right after a joke classify as a joke command. The context goes into the **classifier input only**; `pre_classify`, the gate and `ctx.classify_text` see the unchanged text so URL pre-classification can't fire on the bot's own links.

**Keyword pre-gate**: before the classifier runs, `registry.gate_regex()` (union of all plugins' `GATE_PATTERNS`) is matched against the mention text. No pattern hit and no URL → the message goes straight to RESPOND with **no classify call**. Over-matching is harmless (costs one classify call); under-matching makes an intent unreachable for that phrasing — `tests/test_gate.py` pins every advertised /help phrasing. A plugin with `INTENT_LINES` but no `GATE_PATTERNS` disables the gate globally (fail open), so community plugins keep working.

### Reading messages
Forwarded messages (discord.py `message_snapshots`) are readable: their text is merged via `_display_text()`/`_msg_media_sources()` everywhere messages are read, including images inside forwards. PDF (≤5 MB, Anthropic document blocks) and text attachments (≤200 KB, inlined, 8k-char cap) are read from the **trigger message only** — never from history (token cost).

---

## Plugin System

New features should be implemented as plugins rather than added directly to `bot.py`.

### Directory layout
```
plugins/
├── __init__.py
├── base.py          ← MessageContext dataclass, Plugin ABC, shared helpers
│                      (includes split_message() for sentence-aware Discord chunking)
├── registry.py      ← Registry singleton, discover()
└── core/
    ├── __init__.py
    ├── cdu.py          ← CDU — pure-Python counter, pre_classify only
    ├── help.py         ← HELP — static help text
    ├── memory_admin.py ← MEMORY_LIST / MEMORY_DELETE (admin only)
    ├── mute.py         ← MUTE — silences the bot
    ├── reminders.py    ← REMINDER / REMINDER_LIST / REMINDER_DELETE
    ├── respond.py      ← RESPOND — default @mention reply (web fetch)
    ├── snapshot.py     ← SNAPSHOT — saves session as structured memory
    ├── summary.py      ← SUMMARY — recap recent channel activity
    ├── youtube.py      ← YOUTUBE_SUMMARY — transcript + Claude summary
    └── ardsounds.py    ← ARDSOUNDS_SUMMARY — MP3 + Whisper + summary
```

Community plugins (not bundled) live in `plugins/community/` and are auto-discovered on startup.

### Creating a plugin

**1. Create `plugins/core/myplugin.py`:**

```python
import logging
import os
from pathlib import Path
from plugins.base import Plugin, MessageContext, _read, _write, split_message

_log = logging.getLogger(__name__)

class MyPlugin(Plugin):
    INTENTS = ["MY_INTENT"]   # what classify_intent must return

    INTENT_LINES = [
        "MY_INTENT – one-line description for the Haiku classifier\n",
    ]

    # REQUIRED alongside INTENT_LINES: keyword regex fragments for the classify
    # pre-gate. If none of any plugin's patterns (and no URL) match a mention,
    # the Haiku classify call is skipped. A plugin with INTENT_LINES but no
    # GATE_PATTERNS disables the gate for the whole bot (fail open, logged).
    # Over-match generously; under-matching makes your intent unreachable.
    GATE_PATTERNS = [r"mein\s+feature", r"\bmyword\b"]

    intent_order = 50   # controls position in the injected prompt section (lower = earlier)

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        # Optional: deterministic pre-classification bypasses Haiku entirely.
        # Return (intent, extra) if matched, else None.
        # `clean` is the full classify_text including any replied-to message context.
        return None

    async def handle(self, ctx: MessageContext) -> None:
        # ctx.message      — the full discord.Message object
        # ctx.intent       — "MY_INTENT"
        # ctx.extra        — classifier payload (e.g. from "MY_INTENT: <extra>")
        # ctx.privileged   — True if user is admin/mod
        # ctx.classify_text — the text that was sent to classify_intent
        # ctx.model_tier   — "local" | "cheap" | "normal" | "expensive" (set by plugin .cfg or channel default)
        reply = await ctx.ask_claude(ctx.system_prompt + "\nDo something.", [...], max_tokens=500, tier=ctx.model_tier)
        chunks = split_message(reply)   # sentence-aware split at 2000 chars
        await ctx.message.reply(chunks[0])
        for chunk in chunks[1:]:
            await ctx.message.channel.send(chunk)


def setup(registry) -> None:
    registry.register(MyPlugin())
```

**2. Auto-discovery**: plugins in `plugins/core/` and `plugins/community/` are discovered automatically at startup via `pkgutil.iter_modules`. No registration needed in `bot.py` — just create the file and add the `setup()` function.

**3. (Optional) Create `plugins/core/myplugin.cfg`** to configure the plugin:

```ini
[plugin]
model_tier = expensive
# Any extra keys are plugin-defined; read them in the plugin via configparser:
# cfg = configparser.ConfigParser(); cfg.read(Path(__file__).with_suffix(".cfg"))
my_option = 42
```

`model_tier` is the only key read by the registry. All other keys are ignored by the registry — plugins that need them read their own `.cfg` directly at module load time (see `ardsounds.py` for an example with `update_interval`).

Valid `model_tier` values: `local` | `cheap` | `normal` | `expensive`. If no `.cfg` exists, the plugin uses the channel default (`expensive` for main channels, `normal` for others).

**Deploy note**: `deploy.sh` never overwrites an existing `.cfg` on the server. New `.cfg` files are copied once (first deploy after creation); after that, server-side edits are preserved across all subsequent deploys.

### Rules for plugins

- **Update `plugins/core/help.py` when adding user-facing features** — both `build_help_text()` (the /help reply) and `capabilities_block()` (injected into every system prompt so the bot knows what it can and cannot do). A feature missing there means the bot will deny having it.
- **No bot.py imports** — would cause a circular import
- **All Discord access via `ctx.message`** — `ctx.message.reply()`, `ctx.message.reference`, `ctx.message.author.display_name`, etc.
- **File I/O**: use `_read(path)` / `_write(path, data)` from `plugins.base`; resolve paths from `os.environ.get("DATA_DIR", "/app/data")`
- **handle() is responsible for sending its own reply** — call `await ctx.message.reply(...)` directly. Return type is `None`.
- **Multi-chunk replies**: use `split_message(text)` from `plugins.base` — splits at sentence boundaries before the 2000-char Discord limit.
- **Logging**: `log = logging.getLogger(__name__)` — uses the module path as the logger name

### pre_classify vs GATE_PATTERNS vs INTENT_LINES

| | `pre_classify` | `GATE_PATTERNS` | `INTENT_LINES` / Haiku |
|---|---|---|---|
| Cost | Free | Free | ~1 Haiku call per gated mention |
| Role | Deterministic classification (bypasses Haiku with a result) | Decides whether Haiku runs at all | Natural-language intent decision |
| Use when | URL/pattern match IS the intent | Always, alongside INTENT_LINES | Phrasing varies |
| Input | Full `classify_text` (incl. replied-to message if it contains a URL) | Same | Same |

### Verification after adding a plugin

```bash
# Full test suite (includes gate-coverage tests — extend tests/test_gate.py
# with your plugin's phrasings)
.venv/bin/pytest tests/

# Plugin discovery (no bot.py or Discord token needed)
python -c "from plugins.registry import discover; print(discover())"

# Check intent lines are correct
python -c "
from plugins.registry import discover, registry
discover()
for line in registry.intent_lines():
    print(repr(line))
"
```

Then test manually in Discord.

---

## ARD Sounds plugin

Transcribes and summarises podcast episodes from ardsounds.de.

### How it works
1. `pre_classify` regex-matches `ardsounds.de/episode/urn:ard:episode:…` URLs (current message or replied-to message)
2. Queries ARD GraphQL API (`api.ardaudiothek.de/graphql`) for title + MP3 URL
3. Downloads MP3 to temp file (deleted after transcription)
4. Transcribes with `faster-whisper` (local, CPU, int8) — model stored in `DATA_DIR/whisper_models/`
5. Sends transcript (≤ 25 000 chars) to Claude for German summary
6. Edits a status message with live progress: first update within ~30 s of segments appearing, then every `update_interval` seconds

### Whisper env vars
| Variable | Default | Description |
|---|---|---|
| `WHISPER_MODEL` | `base` | Model size: `tiny` / `base` / `small` / `medium` |
| `WHISPER_THREADS` | `0` (all cores) | CPU threads; set e.g. `4` to limit |

### ardsounds.cfg options
```ini
[plugin]
model_tier = expensive
update_interval = 60   # seconds between progress message edits after the first
```

## Dev Tools
- Caveman skill active — Claude Code output is intentionally terse.
  Check status with `/caveman status`. It should be set to full.
