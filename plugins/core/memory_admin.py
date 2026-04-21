"""Memory admin plugin — MEMORY_LIST and MEMORY_DELETE intents."""

import logging

from plugins.base import Plugin, MessageContext

_log = logging.getLogger(__name__)


class MemoryAdminPlugin(Plugin):
    INTENTS = ["MEMORY_LIST", "MEMORY_DELETE"]

    INTENT_PREFIXES = {
        "MEMORY_DELETE": "MEMORY_DELETE:",
    }

    INTENT_LINES = [
        "MEMORY_LIST – gespeicherte Fakten anzeigen (nur Admins/Mods)\n",
        "MEMORY_DELETE: <stichwort> – bestimmten Fakt löschen (nur Admins/Mods)\n",
    ]

    intent_order = 20

    async def handle(self, ctx: MessageContext) -> None:
        if ctx.intent == "MEMORY_LIST":
            if not ctx.privileged:
                await ctx.message.reply("Das können nur Admins und Mods.")
                return
            memories = ctx.list_memories_fn()
            if not memories:
                await ctx.message.reply("Keine Einträge vorhanden.")
                return
            lines = []
            for m in memories:
                mtype   = m.get("type", "general")
                preview = m["content"]
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                if mtype == "bot":
                    label = "[Bot]"
                    if m.get("trigger"):
                        label += f" (wenn: {m['trigger']})"
                elif mtype == "user":
                    subj    = m.get("subject") or "?"
                    aliases = m.get("aliases") or []
                    prefix  = "Flavor" if m.get("flavor") else "User"
                    label   = f"[{prefix}: {subj}" + (f" / {', '.join(aliases)}" if aliases else "") + "]"
                else:
                    label = "[Allgemein]"
                uses    = m.get("use_count", 0)
                expires = f", läuft ab {m['expires']}" if m.get("expires") else ""
                lines.append(f"**{label}** ({m['date']}{expires}, ×{uses}): {preview}")
            header  = "Alles was ich weiß:"
            chunks  = []
            current = header
            for line in lines:
                candidate = current + "\n" + line
                if len(candidate) > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = candidate
            chunks.append(current)
            await ctx.message.reply(chunks[0])
            for chunk in chunks[1:]:
                await ctx.message.channel.send(chunk)

        elif ctx.intent == "MEMORY_DELETE":
            if not ctx.privileged:
                await ctx.message.reply("Das können nur Admins und Mods.")
                return
            specific = None if ctx.extra.lower() == "all" else ctx.extra
            count    = ctx.delete_memories_fn(ctx.message.author.id, ctx.privileged, specific)
            if count == 0:
                await ctx.message.reply("Nichts gefunden.")
            else:
                await ctx.message.reply(f"{count} Eintrag/Einträge gelöscht.")


def setup(registry) -> None:
    registry.register(MemoryAdminPlugin())
