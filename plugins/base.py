"""Shared infrastructure for the plugin system."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

_log = logging.getLogger(__name__)

# ── Shared file helpers ───────────────────────────────────────────────────────
# Copied from bot.py so plugins can do file I/O without importing from bot.py
# (which would create a circular import).

def _read(path: Path) -> list:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning(f"Read failed ({path.name}): {e}")
    return []

def _write(path: Path, data: list):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log.warning(f"Write failed ({path.name}): {e}")

# ── MessageContext ────────────────────────────────────────────────────────────

@dataclass
class MessageContext:
    message:       discord.Message       # full Discord object (reply/reference/author)
    intent:        str                   # classified intent label, e.g. "QUOTE_SAVE"
    extra:         str        = ""       # classifier payload (e.g. reminder params)
    privileged:    bool       = False    # True if user is admin/mod (checked in on_message)
    classify_text: str        = ""       # text that was sent to classify_intent
    # Claude access — set by bot.py dispatch; None when running standalone tests
    ask_claude:         object = None    # Callable: _claude_loop(system, messages, max_tokens, model)
    system_prompt:      str    = ""      # pre-built system prompt for this channel
    model:              str    = ""      # model string for this channel
    add_memory_fn:        object = None   # Callable: add_memory(fact, added_by, user_id, ...)
    resolve_mentions_fn:  object = None  # Callable: resolve_mentions(content, mentions)
    list_memories_fn:     object = None  # Callable: list_memories() -> list
    delete_memories_fn:   object = None  # Callable: delete_memories(user_id, privileged, specific)
    # RESPOND-specific fields
    image_blocks:         object = None  # list of image content blocks (pre-fetched)
    clean:                str    = ""    # cleaned message text (before URL context appended)
    ask_full_fn:          object = None  # Callable: ask_claude(message, username, image_blocks, ...)
    fetch_webpage_fn:     object = None  # Callable: fetch_webpage_text(url) -> str | None

# ── Plugin ABC ────────────────────────────────────────────────────────────────

class Plugin(ABC):
    INTENTS:         list[str]       = []  # intent labels this plugin handles
    INTENT_LINES:    list[str]       = []  # lines injected into the Haiku classifier prompt
    INTENT_PREFIXES: dict[str, str]  = {}  # override prefix for classify_intent matching
                                           # e.g. {"REMINDER": "REMINDER:"} for colon payloads
                                           # defaults to {label: label} if not specified
    intent_order: int = 50                 # lower = appears earlier in the injected prompt section

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        """Optional deterministic pre-classification (runs before Haiku).
        Return (intent, extra) to bypass the LLM, or None to fall through."""
        return None

    async def on_ready(self) -> None:
        """Called once when the bot is ready. Override to restore state (e.g. tasks)."""
        return

    @abstractmethod
    async def handle(self, ctx: MessageContext) -> None:
        """Handle a classified intent. Must send its own reply via ctx.message.reply()."""
        ...
