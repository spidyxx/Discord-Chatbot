"""Quotes plugin — QUOTE_SAVE and QUOTE_GET intents."""

import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path

from plugins.base import Plugin, MessageContext, _read, _write

# Matches text in any common quotation style, including straight ASCII " (U+0022)
_INLINE_QUOTE_RE  = re.compile(r'["\u201C\u201E\u00AB](.+?)["\u201D\u00BB]', re.DOTALL)
# Trigger words that indicate a save intent when combined with quoted text
_SAVE_TRIGGER_RE  = re.compile(r'\b(speicher[nt]?|merken?|save|zitat|quote)\b', re.IGNORECASE)

_log = logging.getLogger(__name__)

_DATA_DIR    = Path(os.environ.get("DATA_DIR", "/app/data"))
_QUOTES_FILE = _DATA_DIR / "quotes.json"


def _load() -> list:
    return _read(_QUOTES_FILE)

def _save(q: list) -> None:
    _write(_QUOTES_FILE, q)

def _add(content: str, author: str, added_by: str) -> None:
    q = _load()
    q.append({
        "content":  content,
        "author":   author,
        "added_by": added_by,
        "date":     datetime.now().strftime("%d.%m.%Y"),
    })
    _save(q)
    _log.info(f"Quote saved by {added_by}: '{content[:60]}'")

def _random() -> dict | None:
    q = _load()
    return random.choice(q) if q else None


class QuotesPlugin(Plugin):
    INTENTS = ["QUOTE_SAVE", "QUOTE_GET"]

    INTENT_LINES = [
        "QUOTE_SAVE – Nutzer möchte ein Zitat speichern. Entweder (a) antwortet auf eine fremde "
        "Nachricht und sagt 'merke dieses Zitat' / 'speicher das' etc., ODER (b) gibt den Text "
        "direkt in Anführungszeichen an, z.B. 'speichere dieses Zitat: \"Text\"' oder "
        "'merke dir: \"Text\"'. Gilt NICHT für beiläufige Anführungszeichen ohne Speicher-Intent.\n",
        "QUOTE_GET – zufälliges Zitat abrufen\n",
    ]

    intent_order = 40  # same relative slot as in bot.py — before HELP (footer)

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        # Deterministic detection: save/store trigger word + inline quoted text
        # Runs before Haiku so the LLM can't misclassify obvious patterns.
        if _SAVE_TRIGGER_RE.search(clean) and _INLINE_QUOTE_RE.search(clean):
            return "QUOTE_SAVE", ""
        return None

    async def handle(self, ctx: MessageContext) -> None:
        if ctx.intent == "QUOTE_SAVE":
            if ctx.message.reference and ctx.message.reference.resolved:
                ref = ctx.message.reference.resolved
                # Refuse to save the bot's own messages (confirmation replies, responses, etc.)
                bot_id = ctx.message.guild.me.id if ctx.message.guild else None
                if bot_id and ref.author.id == bot_id:
                    await ctx.message.reply(
                        "Meine eigenen Nachrichten speichere ich nicht als Zitate. "
                        "Antworte auf eine Nachricht eines anderen Nutzers."
                    )
                    return
                _add(ref.content, ref.author.display_name, ctx.message.author.display_name)
                await ctx.message.reply(
                    f'Gespeichert. "{ref.content[:80]}" – {ref.author.display_name}'
                )
            else:
                # Try to extract inline quoted text: speichere dieses Zitat: "..."
                m = _INLINE_QUOTE_RE.search(ctx.classify_text)
                if m:
                    text = m.group(1).strip()
                    _add(text, ctx.message.author.display_name, ctx.message.author.display_name)
                    await ctx.message.reply(f'Gespeichert. "{text[:80]}"')
                else:
                    await ctx.message.reply(
                        "Antworte auf die Nachricht die du speichern willst, "
                        "oder schreib: merke dir \"Text\"."
                    )

        elif ctx.intent == "QUOTE_GET":
            q = _random()
            if not q:
                await ctx.message.reply("Keine Zitate gespeichert.")
            else:
                await ctx.message.reply(
                    f'"{q["content"]}"\n— {q["author"]}  *({q["date"]})*'
                )


def setup(registry) -> None:
    registry.register(QuotesPlugin())
