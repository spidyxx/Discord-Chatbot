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
- `MAIN_MODEL` — full-personality responses in main channels (default: sonnet)
- `CLAUDE_MODEL` — neutral responses in other channels (default: sonnet)
- `CHEAP_MODEL` — intent classification and small tasks (default: haiku)

## Architecture

### Channel modes
- **Main channels** (`MAIN_CHANNEL_IDS`): full personality, memory injection, passive autonomous responses
- **Other channels**: neutral prompt, mention-only

### Prompt caching
`_claude_loop` applies `cache_control: ephemeral` to the system prompt and the last history message. **Do not modify `_claude_loop` without understanding the caching implications** — cache misses increase cost significantly.

### Intent classification
`classify_intent()` uses Haiku to classify each @mention into an intent label (REMINDER, SUMMARY, etc.). The classifier prompt is built dynamically: a static preamble + plugin-contributed lines + a static footer. Plugins register their own intent labels and prompt lines — see plugin conventions below.

---

## Plugin System

New features should be implemented as plugins rather than added directly to `bot.py`.

### Directory layout
```
plugins/
├── __init__.py
├── base.py          ← MessageContext dataclass, Plugin ABC, shared file helpers
├── registry.py      ← Registry singleton, discover()
└── core/
    ├── __init__.py
    └── quotes.py    ← example: QUOTE_SAVE and QUOTE_GET
```

Community plugins (not bundled) live in `plugins/community/` and are auto-discovered on startup.

### Creating a plugin

**1. Create `plugins/core/myplugin.py`:**

```python
import logging
import os
from pathlib import Path
from plugins.base import Plugin, MessageContext, _read, _write

_log = logging.getLogger(__name__)

class MyPlugin(Plugin):
    INTENTS = ["MY_INTENT"]   # what classify_intent must return

    INTENT_LINES = [
        "MY_INTENT – one-line description for the Haiku classifier\n",
    ]

    intent_order = 50   # controls position in the injected prompt section (lower = earlier)

    async def handle(self, ctx: MessageContext) -> None:
        # ctx.message      — the full discord.Message object
        # ctx.intent       — "MY_INTENT"
        # ctx.extra        — classifier payload (e.g. from "MY_INTENT: <extra>")
        # ctx.privileged   — True if user is admin/mod
        # ctx.classify_text — the text that was sent to classify_intent
        await ctx.message.reply("Hello from my plugin!")


def setup(registry) -> None:
    registry.register(MyPlugin())
```

**2. Auto-discovery**: plugins in `plugins/core/` and `plugins/community/` are discovered automatically at startup via `pkgutil.iter_modules`. No registration needed in `bot.py` — just create the file and add the `setup()` function.

### Rules for plugins

- **No bot.py imports** — would cause a circular import
- **All Discord access via `ctx.message`** — `ctx.message.reply()`, `ctx.message.reference`, `ctx.message.author.display_name`, etc.
- **File I/O**: use `_read(path)` / `_write(path, data)` from `plugins.base`; resolve paths from `os.environ.get("DATA_DIR", "/app/data")`
- **handle() is responsible for sending its own reply** — call `await ctx.message.reply(...)` directly. Return type is `None`.
- **Logging**: `log = logging.getLogger(__name__)` — uses the module path as the logger name

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
