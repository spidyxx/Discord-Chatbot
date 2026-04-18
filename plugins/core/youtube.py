"""YouTube summary plugin — YOUTUBE_SUMMARY intent."""

import asyncio
import logging
import re

from plugins.base import Plugin, MessageContext

_log = logging.getLogger(__name__)

_YT_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})'
)


def _extract_youtube_id(text: str) -> str | None:
    m = _YT_URL_RE.search(text)
    return m.group(1) if m else None


async def _fetch_transcript(video_id: str) -> str | None:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

    def _fetch():
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        transcript = transcript_list.find_transcript(["de", "en", "a.de", "a.en"])
        return transcript.fetch()

    try:
        entries = await asyncio.to_thread(_fetch)
        return " ".join(e.text for e in entries)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except Exception as exc:
        _log.warning(f"YouTube-Transkript konnte nicht geladen werden ({video_id}): {exc}")
        return None


class YoutubePlugin(Plugin):
    INTENTS = ["YOUTUBE_SUMMARY"]

    INTENT_LINES = [
        "YOUTUBE_SUMMARY: <url> – Nutzer möchte ein YouTube-Video zusammengefasst haben "
        "(URL im Format youtube.com/watch?v=... oder youtu.be/...)\n",
    ]

    intent_order = 30

    async def handle(self, ctx: MessageContext) -> None:
        async with ctx.message.channel.typing():
            video_id = (
                _extract_youtube_id(ctx.extra)
                or _extract_youtube_id(ctx.classify_text)
            )
            if not video_id and ctx.message.reference and ctx.message.reference.resolved:
                video_id = _extract_youtube_id(ctx.message.reference.resolved.content or "")
            if not video_id:
                await ctx.message.reply("Ich konnte keine gültige YouTube-URL finden.")
                return

            transcript = await _fetch_transcript(video_id)
            if transcript is None:
                await ctx.message.reply(
                    "Für dieses Video sind keine Untertitel verfügbar – "
                    "ich kann es leider nicht zusammenfassen."
                )
                return

            if len(transcript) > 12000:
                transcript = transcript[:12000] + " [...]"

            summary = await ctx.ask_claude(
                ctx.system_prompt +
                "\nFasse das folgende YouTube-Video-Transkript in deinem typischen Stil zusammen. "
                "Gib die wichtigsten Punkte und Erkenntnisse wieder. Sei prägnant aber vollständig.",
                [{"role": "user", "content": f"Transkript:\n{transcript}"}],
                max_tokens=800, model=ctx.model,
            )
        await ctx.message.reply(summary)


def setup(registry) -> None:
    registry.register(YoutubePlugin())
