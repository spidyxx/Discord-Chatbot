"""Snapshot plugin — SNAPSHOT intent (saves session as structured memory facts)."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from plugins.base import Plugin, MessageContext
from plugins import state as bot_state

_log = logging.getLogger(__name__)

_TZ       = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))
_BOT_NAME = os.environ.get("BOT_NAME", "Marvin")
_MODEL    = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


def _parse_snapshot_facts(text: str) -> list[dict]:
    facts = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        ftype = parts[0].upper() if parts else ""
        try:
            if ftype == "BOT" and len(parts) >= 3:
                trigger = parts[2] if parts[2].upper() not in ("NONE", "-", "") else None
                facts.append({"type": "bot", "content": parts[1], "trigger": trigger})
            elif ftype == "USER" and len(parts) >= 4:
                aliases_raw = parts[2] if parts[2].upper() not in ("NONE", "-", "") else ""
                aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
                facts.append({"type": "user", "subject": parts[1], "aliases": aliases, "content": parts[3]})
            elif ftype == "GENERAL" and len(parts) >= 2:
                facts.append({"type": "general", "content": parts[1]})
        except Exception:
            continue
    return facts


class SnapshotPlugin(Plugin):
    INTENTS = ["SNAPSHOT"]

    INTENT_LINES = [
        "SNAPSHOT – Nutzer möchte die Persönlichkeit, Witze, Dynamiken und Ereignisse der letzten 24h "
        "als Memory speichern (z.B. 'speichere was heute passiert ist', 'merk dir die heutige Session', 'snapshot')\n",
    ]

    intent_order = 65

    async def handle(self, ctx: MessageContext) -> None:
        if not ctx.privileged:
            await ctx.message.reply("Das dürfen nur Admins und Mods.")
            return

        async with ctx.message.channel.typing():
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            lines = []
            async for msg in ctx.message.channel.history(after=since, limit=1000, oldest_first=True):
                if msg.id == ctx.message.id:
                    continue
                ts      = msg.created_at.astimezone(_TZ).strftime("%H:%M")
                content = ctx.resolve_mentions_fn(msg.content or "", msg.mentions) if ctx.resolve_mentions_fn else (msg.content or "")
                if msg.attachments:
                    content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
                bot_user = bot_state.bot.user if bot_state.bot else None
                name = bot_user.display_name if (bot_user and msg.author == bot_user) else msg.author.display_name
                lines.append(f"[{ts}] {name}: {content}")

            if not lines:
                await ctx.message.reply("Die letzten 24 Stunden waren leer. Nichts zu speichern.")
                return

            client = bot_state.anthropic_client
            response = await asyncio.to_thread(
                client.messages.create,
                model=_MODEL,
                max_tokens=2000,
                system=(
                    f"Du analysierst einen Discord-Chatverlauf und extrahierst strukturierte Gedächtniseinträge für den Bot {_BOT_NAME}.\n\n"
                    "Ausgabeformat — eine Zeile pro atomarer Tatsache, KEIN Fließtext:\n"
                    "BOT | <Fakt über den Bot selbst> | <Trigger/Kontext oder NONE>\n"
                    "USER | <Anzeigename wie im Chat> | <echte Namen und Spitznamen kommagetrennt oder NONE> | <Fakt>\n"
                    "GENERAL | <allgemeiner Fakt ohne klaren Nutzer- oder Bot-Bezug>\n\n"
                    "Regeln:\n"
                    "- Jede Zeile = genau eine Aussage. Keine Interpretationen, nur gesicherte Fakten aus dem Chat.\n"
                    "- BOT: Spitznamen, Rollen, Besitztümer, Verhaltensregeln, Dynamiken die der Bot eingegangen ist\n"
                    "- BOT-Trigger: Kontext in dem ein Fakt gilt (z.B. 'wenn BonusPizza schreibt'), sonst NONE\n"
                    "- USER: Anzeigename exakt so wie er im Chatverlauf steht. Aliases = alle anderen bekannten Namen.\n"
                    "- USER-Fakten: echte Namen, Spitznamen, Rollen, Besitztümer, Beziehungen zum Bot oder anderen\n"
                    "- Nur was explizit im Chat steht oder klar abgeleitet werden kann.\n"
                    "- Kein Metakommentar, keine Leerzeilen, kein Markdown."
                ),
                messages=[{"role": "user", "content": "Chatverlauf der letzten 24h:\n" + "\n".join(lines)}],
            )
            raw_facts = response.content[0].text.strip()
            parsed    = _parse_snapshot_facts(raw_facts)
            if not parsed:
                await ctx.message.reply("Konnte keine strukturierten Fakten extrahieren. Versuch's nochmal.")
                return

            for fact_data in parsed:
                ctx.add_memory_fn(
                    fact        = fact_data["content"],
                    added_by    = ctx.message.author.display_name,
                    user_id     = ctx.message.author.id,
                    memory_type = fact_data["type"],
                    subject     = fact_data.get("subject"),
                    aliases     = fact_data.get("aliases"),
                    trigger     = fact_data.get("trigger"),
                )
            _log.info(f"SNAPSHOT: {len(parsed)} Fakten gespeichert")

        await ctx.message.reply(f"Gespeichert. {len(parsed)} Einträge angelegt.")


def setup(registry) -> None:
    registry.register(SnapshotPlugin())
