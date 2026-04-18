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
        _log.warning(f"Lesen fehlgeschlagen ({path.name}): {e}")
    return []

def _write(path: Path, data: list):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log.warning(f"Schreiben fehlgeschlagen ({path.name}): {e}")

# ── MessageContext ────────────────────────────────────────────────────────────

@dataclass
class MessageContext:
    message:       discord.Message       # full Discord object (reply/reference/author)
    intent:        str                   # classified intent label, e.g. "QUOTE_SAVE"
    extra:         str        = ""       # classifier payload (e.g. reminder params)
    privileged:    bool       = False    # True if user is admin/mod (checked in on_message)
    classify_text: str        = ""       # text that was sent to classify_intent

# ── Plugin ABC ────────────────────────────────────────────────────────────────

class Plugin(ABC):
    INTENTS:      list[str] = []   # intent labels this plugin handles
    INTENT_LINES: list[str] = []   # lines injected into the Haiku classifier prompt
    intent_order: int       = 50   # lower = appears earlier in the injected prompt section

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        """Optional deterministic pre-classification (runs before Haiku).
        Return (intent, extra) to bypass the LLM, or None to fall through."""
        return None

    @abstractmethod
    async def handle(self, ctx: MessageContext) -> None:
        """Handle a classified intent. Must send its own reply via ctx.message.reply()."""
        ...
