"""Shared mutable bot state — importable by plugins without circular imports."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

muted: bool = False
bot:              object = None  # discord.ext.commands.Bot instance
anthropic_client: object = None  # anthropic.Anthropic instance
claude_loop:      object = None  # _claude_loop(system, messages, max_tokens, tier) -> str
build_system_prompt: object = None  # build_system_prompt(channel_id) -> str
get_tier:         object = None  # _tier(channel_id) -> str
reminder_tier:    str    = "normal"
main_channel_ids: set    = set()  # populated from MAIN_CHANNEL_IDS on startup
