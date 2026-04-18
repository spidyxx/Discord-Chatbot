"""Mute plugin — MUTE intent."""

import logging

from plugins.base import Plugin, MessageContext
from plugins import state as bot_state

_log = logging.getLogger(__name__)


class MutePlugin(Plugin):
    INTENTS = ["MUTE"]

    INTENT_LINES = [
        "MUTE – Bot stummschalten\n",
    ]

    intent_order = 10  # first in prompt

    async def handle(self, ctx: MessageContext) -> None:
        import discord
        bot_state.muted = True
        if bot_state.bot:
            await bot_state.bot.change_presence(
                activity=discord.CustomActivity(name="Hält die Klappe 🤐")
            )
        await ctx.message.reply("Schon gut, ich halt die Klappe.")


def setup(registry) -> None:
    registry.register(MutePlugin())
