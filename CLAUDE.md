# Marvin — Discord Bot

## Stack
- Python 3.12, discord.py 2.3.2, Anthropic SDK (Claude API)
- Entry point: `bot.py`
- Runs in Docker; see `Dockerfile` and `docker-compose.yml`
- All state in JSON files under `DATA_DIR` (default `/app/data`)

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

### Gemini support

Set `GEMINI_API_KEY` to enable Google Gemini models. Then set any tier's model to a `gemini-*` name:

```
GEMINI_API_KEY=AIza...
EXPENSIVE_MODEL=gemini-2.5-pro
```

The bot detects model names starting with `gemini` and routes those calls through Google's OpenAI-compatible endpoint (`generativelanguage.googleapis.com/v1beta/openai/`) via the `openai` Python package. Prompt caching and web search tools are disabled for Gemini tiers — they use plain chat completions. Image blocks and `cache_control` markers are stripped automatically (reuses the same `_to_text_messages` helper as Ollama).

Each feature is assigned a tier via its own env var (e.g. `CLASSIFY_TIER=local`). Defaults:

| Env var | Default | Feature |
|---|---|---|
| `MAIN_TIER` | `expensive` | Main channel responses |
| `MENTION_TIER` | `normal` | Mention-only channel responses |
| `CLASSIFY_TIER` | `cheap` | Intent classification |
| `EMOJI_TIER` | `cheap` | Emoji reactions |
| `MEMORY_FILTER_TIER` | `cheap` | Memory relevance filtering |
| `PROACTIVE_TIER` | `expensive` | Proactive messages |
| `DIGEST_SUMMARY_TIER` | `expensive` | Daily digest summary |
| `DIGEST_FACTS_TIER` | `normal` | Daily digest fact extraction |

## Architecture

### Channel modes
- **Main channels** (`MAIN_CHANNEL_IDS`): full personality, memory injection, passive autonomous responses
- **Other channels**: neutral prompt, mention-only

### System prompt
`build_system_prompt()` assembles: memory block + base prompt + current date/time (German weekday, `DD.MM.YYYY, HH:MM Uhr`, injected fresh on every call using `TIMEZONE`).

### Chat reply post-processing
`_clean_chat_reply()` collapses multiple blank lines (`\n\n+` → `\n`) before all conversational `channel.send` / `message.reply` calls. Plugin replies (summaries etc.) bypass this and are sent as-is.

### Prompt caching
`_claude_loop` applies `cache_control: ephemeral` to the system prompt and the last history message. **Do not modify `_claude_loop` without understanding the caching implications** — cache misses increase cost significantly.

### Intent classification
`classify_intent()` uses the `cheap` tier to classify each @mention into an intent label (REMINDER, SUMMARY, etc.). The classifier prompt is built dynamically: a static preamble + plugin-contributed lines + a static footer. Plugins register their own intent labels and prompt lines — see plugin conventions below.

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
    ├── quotes.py    ← example: QUOTE_SAVE and QUOTE_GET
    ├── youtube.py   ← YOUTUBE_SUMMARY — fetches transcript, summarises with Claude
    └── ardsounds.py ← ARDSOUNDS_SUMMARY — downloads MP3, transcribes with Whisper, summarises
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

- **No bot.py imports** — would cause a circular import
- **All Discord access via `ctx.message`** — `ctx.message.reply()`, `ctx.message.reference`, `ctx.message.author.display_name`, etc.
- **File I/O**: use `_read(path)` / `_write(path, data)` from `plugins.base`; resolve paths from `os.environ.get("DATA_DIR", "/app/data")`
- **handle() is responsible for sending its own reply** — call `await ctx.message.reply(...)` directly. Return type is `None`.
- **Multi-chunk replies**: use `split_message(text)` from `plugins.base` — splits at sentence boundaries before the 2000-char Discord limit.
- **Logging**: `log = logging.getLogger(__name__)` — uses the module path as the logger name

### pre_classify vs INTENT_LINES

| | `pre_classify` | `INTENT_LINES` / Haiku |
|---|---|---|
| Cost | Free | ~1 Haiku call per mention |
| Use when | URL/pattern match is deterministic | Natural language intent needed |
| Input | Full `classify_text` (incl. replied-to message if it contains a URL) | Same |

### Verification after adding a plugin

```bash
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
