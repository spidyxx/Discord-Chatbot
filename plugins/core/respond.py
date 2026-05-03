"""Respond plugin — RESPOND intent (general @mention replies with web fetch)."""

import logging
import os
import re

from plugins.base import Plugin, MessageContext, clean_chat_reply, split_message

_log = logging.getLogger(__name__)

_MAX_URLS = int(os.environ.get("MAX_URLS_PER_MSG", "2"))

_URL_RE       = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_YT_URL_RE    = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})')
_IMAGE_URL_RE = re.compile(r'https?://\S+\.(?:jpe?g|png|gif|webp)(?:[?#]\S*)?', re.IGNORECASE)


def _is_plain_url(u: str) -> bool:
    return not _YT_URL_RE.search(u) and not _IMAGE_URL_RE.search(u)


class RespondPlugin(Plugin):
    INTENTS = ["RESPOND"]

    INTENT_LINES = []  # "RESPOND – alles andere" stays in _CLASSIFY_FOOTER

    intent_order = 100  # last

    async def handle(self, ctx: MessageContext) -> None:
        clean        = ctx.clean or ctx.classify_text
        image_blocks = ctx.image_blocks or []

        # Extract plain web URLs from current message
        webpage_urls = [u for u in _URL_RE.findall(clean) if _is_plain_url(u)]

        # Fallback: replied-to message, then last 5 history messages
        if not webpage_urls:
            candidates: list[str] = []
            if ctx.message.reference and ctx.message.reference.resolved:
                candidates.extend(_URL_RE.findall(ctx.message.reference.resolved.content or ""))
            if not candidates:
                async for hist in ctx.message.channel.history(limit=5, before=ctx.message):
                    if hist.author == ctx.message.guild.me if ctx.message.guild else False:
                        continue
                    candidates.extend(_URL_RE.findall(hist.content or ""))
                    if candidates:
                        break
            webpage_urls = [u for u in candidates if _is_plain_url(u)]

        webpage_urls = webpage_urls[:_MAX_URLS]
        url_context  = ""
        if webpage_urls and ctx.fetch_webpage_fn:
            fetched = []
            for u in webpage_urls:
                text = await ctx.fetch_webpage_fn(u)
                if text:
                    fetched.append(f"[Inhalt von {u}]:\n{text}")
            if fetched:
                url_context = "\n\n" + "\n\n".join(fetched)

        async with ctx.message.channel.typing():
            reply = await ctx.ask_full_fn(
                clean + url_context,
                ctx.message.author.display_name,
                image_blocks,
                channel_id=ctx.message.channel.id,
                before_id=ctx.message.id,
                memory_context=clean,
            )
        chunks = split_message(clean_chat_reply(reply))
        await ctx.message.reply(chunks[0])
        for chunk in chunks[1:]:
            await ctx.message.channel.send(chunk)


def setup(registry) -> None:
    registry.register(RespondPlugin())
