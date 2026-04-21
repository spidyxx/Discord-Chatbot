"""Snapshot plugin — SNAPSHOT intent (saves session as structured memory facts)."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from plugins.base import Plugin, MessageContext, _read
from plugins import state as bot_state

_log = logging.getLogger(__name__)

_TZ          = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))
_BOT_NAME    = os.environ.get("BOT_NAME", "Marvin")
_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
_MEMORY_FILE = Path(os.environ.get("DATA_DIR", "/app/data")) / "memory.json"


def _known_identities_block() -> str:
    seen: dict[str, set] = {}
    for m in _read(_MEMORY_FILE):
        if m.get("type") == "user" and m.get("subject"):
            subj = m["subject"]
            seen.setdefault(subj, set()).update(m.get("aliases") or [])
    if not seen:
        return ""
    lines = [
        f"- {s}" + (f" ({', '.join(sorted(a))})" if a else "")
        for s, a in sorted(seen.items())
    ]
    return "\n\nBereits bekannte Nutzeridentitäten (kein USER-Eintrag nötig, außer bei neuen Aliasen):\n" + "\n".join(lines)


def _parse_snapshot_facts(text: str) -> list[dict]:
    facts = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        ftype = parts[0].upper() if parts else ""
        def _expires(val: str):
            return val if val.upper() not in ("NONE", "-", "") else None

        try:
            if ftype == "BOT" and len(parts) >= 3:
                trigger = parts[2] if parts[2].upper() not in ("NONE", "-", "") else None
                expires = _expires(parts[3]) if len(parts) >= 4 else None
                facts.append({"type": "bot", "content": parts[1], "trigger": trigger, "expires": expires})
            elif ftype == "USER" and len(parts) >= 4:
                aliases_raw = parts[2] if parts[2].upper() not in ("NONE", "-", "") else ""
                aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
                facts.append({"type": "user", "subject": parts[1], "aliases": aliases, "content": parts[3]})
            elif ftype == "FLAVOR" and len(parts) >= 4:
                aliases_raw = parts[2] if parts[2].upper() not in ("NONE", "-", "") else ""
                aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
                expires = _expires(parts[4]) if len(parts) >= 5 else None
                facts.append({"type": "user", "flavor": True, "subject": parts[1], "aliases": aliases, "content": parts[3], "expires": expires})
            elif ftype == "GENERAL" and len(parts) >= 2:
                expires = _expires(parts[2]) if len(parts) >= 3 else None
                facts.append({"type": "general", "content": parts[1], "expires": expires})
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

            today      = datetime.now(_TZ)
            week_date  = (today + timedelta(days=7)).strftime("%d.%m.%Y")
            month_date = (today + timedelta(days=30)).strftime("%d.%m.%Y")
            client = bot_state.anthropic_client
            response = await asyncio.to_thread(
                client.messages.create,
                model=_MODEL,
                max_tokens=2000,
                system=(
                    f"Du analysierst einen Discord-Chatverlauf und extrahierst strukturierte Gedächtniseinträge für den Bot {_BOT_NAME}.\n\n"
                    "Ausgabeformat — eine Zeile pro atomarer Tatsache, KEIN Fließtext:\n"
                    f"BOT | <Fakt über den Bot selbst> | <Trigger oder NONE> | <Ablaufdatum DD.MM.YYYY oder NONE>\n"
                    f"USER | <Anzeigename exakt wie im Chat> | <echte Namen/Spitznamen kommagetrennt oder NONE> | <Identitätsfakt>\n"
                    f"FLAVOR | <Anzeigename> | <Aliase oder NONE> | <Persönlichkeitsfakt> | <Ablaufdatum DD.MM.YYYY oder NONE>\n"
                    f"GENERAL | <Fakt> | <Ablaufdatum DD.MM.YYYY oder NONE>\n\n"
                    f"Ablaufdaten (heute = {today.strftime('%d.%m.%Y')}):\n"
                    f"- Tagesereignisse, kurzfristige Pläne, aktuelle Stimmung → {week_date}\n"
                    f"- Laufende Projekte, aktuelle Situation → {month_date}\n"
                    f"- Dauerhafte Eigenschaften, Rollen, Verhaltensregeln → NONE\n\n"
                    "Regeln:\n"
                    "- Eine Zeile = eine Aussage. Nur gesicherte Fakten aus dem Chat, keine Interpretation.\n"
                    "- BOT: Titel, Rollen, Besitztümer, Verhaltensregeln, Dynamiken mit Usern.\n"
                    "- USER: Nur für neue Nutzer oder neu entdeckte Aliase. Max. einen USER-Eintrag pro Nutzer.\n"
                    "- FLAVOR: Persönlichkeit, Beziehungen, Vorlieben, Erlebnisse mit Wiederholungspotenzial.\n"
                    "- NICHT speichern: Smalltalk, Einzelereignisse ohne Relevanz für spätere Gespräche, "
                    "Fakten die in einer Woche sicher nicht mehr zutreffen.\n"
                    "- Kein Metakommentar, keine Leerzeilen, kein Markdown."
                    + _known_identities_block()
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
                    flavor      = fact_data.get("flavor", False),
                    expires     = fact_data.get("expires"),
                )
            _log.info(f"SNAPSHOT: {len(parsed)} Fakten gespeichert")

        await ctx.message.reply(f"Gespeichert. {len(parsed)} Einträge angelegt.")


def setup(registry) -> None:
    registry.register(SnapshotPlugin())
