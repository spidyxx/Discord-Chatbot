"""Summary plugin — SUMMARY intent."""

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from plugins.base import Plugin, MessageContext, clean_chat_reply, split_message

_log = logging.getLogger(__name__)

_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))


class SummaryPlugin(Plugin):
    INTENTS = ["SUMMARY"]

    INTENT_LINES = [
        "SUMMARY – Nutzer fragt was passiert ist, was er verpasst hat, was es Neues gibt, "
        "oder möchte eine Zusammenfassung des Chats (z.B. 'was hab ich verpasst', "
        "'was gab's heute', 'was ist hier los', 'fass zusammen')\n",
    ]

    intent_order = 60

    async def handle(self, ctx: MessageContext) -> None:
        async with ctx.message.channel.typing():
            today_start = datetime.now(_TZ).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)

            all_msgs = []
            async for msg in ctx.message.channel.history(
                after=today_start, limit=500, oldest_first=True
            ):
                if msg.id == ctx.message.id:
                    continue
                all_msgs.append(msg)

            if not all_msgs:
                await ctx.message.reply("Heute ist noch nichts passiert.")
                return

            cutoff_ts = None
            for msg in reversed(all_msgs):
                if msg.author.id == ctx.message.author.id:
                    cutoff_ts = msg.created_at
                    break

            relevant = (
                [m for m in all_msgs if m.created_at > cutoff_ts]
                if cutoff_ts else all_msgs
            )

            if not relevant:
                await ctx.message.reply("Seit deiner letzten Nachricht ist nichts passiert.")
                return

            lines = []
            for msg in relevant:
                ts      = msg.created_at.astimezone(_TZ).strftime("%H:%M")
                content = msg.content or "[kein Text]"
                if msg.attachments:
                    content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
                lines.append(f"[{ts}] {msg.author.display_name}: {content}")

            summary = await ctx.ask_claude(
                ctx.system_prompt +
                "\nFasse die folgenden Discord-Nachrichten kurz in deinem typischen Stil zusammen. "
                "Konzentriere dich auf wichtige Themen und interessante Momente, nicht auf jede einzelne Nachricht.",
                [{"role": "user", "content": "\n".join(lines)}],
                max_tokens=600, tier=ctx.model_tier,
            )
        chunks = split_message(clean_chat_reply(summary))
        await ctx.message.reply(chunks[0])
        for chunk in chunks[1:]:
            await ctx.message.channel.send(chunk)


def setup(registry) -> None:
    registry.register(SummaryPlugin())
