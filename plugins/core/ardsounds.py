"""ARD Sounds summary plugin — ARDSOUNDS_SUMMARY intent."""

import asyncio
import configparser
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import aiohttp

from plugins.base import Plugin, MessageContext, split_message

_log = logging.getLogger(__name__)

_DATA_DIR        = Path(os.environ.get("DATA_DIR", "/app/data"))
_WHISPER_DIR     = _DATA_DIR / "whisper_models"
_WHISPER_MODEL   = os.environ.get("WHISPER_MODEL", "base")
_WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "0"))  # 0 = all cores
_ARD_GRAPHQL     = "https://api.ardaudiothek.de/graphql"

def _read_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).with_suffix(".cfg"))
    return cfg

_cfg = _read_cfg()
_UPDATE_INTERVAL = int(_cfg.get("plugin", "update_interval", fallback="60"))

_ARDSOUNDS_URL_RE = re.compile(
    r'https?://(?:www\.)?ardsounds\.de/episode/(urn:ard:episode:[A-Za-z0-9]+)/?'
)

_whisper_model  = None
_whisper_lock   = asyncio.Lock()
_transcribe_sem = asyncio.Semaphore(1)


def _load_whisper_model():
    from faster_whisper import WhisperModel
    _WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    cpu_threads = _WHISPER_THREADS or (os.cpu_count() or 4)
    return WhisperModel(
        _WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
        download_root=str(_WHISPER_DIR),
        cpu_threads=cpu_threads,
    )


async def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    async with _whisper_lock:
        if _whisper_model is None:
            _log.info(f"Loading Whisper model '{_WHISPER_MODEL}' into {_WHISPER_DIR}")
            _whisper_model = await asyncio.to_thread(_load_whisper_model)
            _log.info("Whisper model ready")
    return _whisper_model


async def _fetch_episode_metadata(urn: str) -> dict | None:
    query = '{ item(id: "%s") { title audios { url mimeType } } }' % urn
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _ARD_GRAPHQL,
                json={"query": query},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    _log.warning(f"ARD GraphQL returned {resp.status} for {urn}")
                    return None
                data = await resp.json()
    except Exception as exc:
        _log.warning(f"ARD API request failed for {urn}: {exc}")
        return None

    item = (data.get("data") or {}).get("item") or {}
    title = item.get("title") or "Unbekannte Episode"
    mp3_url = next(
        (a["url"] for a in (item.get("audios") or []) if a.get("url")),
        None,
    )
    if not mp3_url:
        _log.warning(f"No audio URL in ARD response for {urn}: {data}")
        return None
    return {"title": title, "mp3_url": mp3_url}


async def _download_mp3(mp3_url: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                mp3_url,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status != 200:
                    _log.warning(f"MP3 download failed (HTTP {resp.status}): {mp3_url}")
                    return None
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp_path = tmp.name
                    async for chunk in resp.content.iter_chunked(65536):
                        tmp.write(chunk)
        return tmp_path
    except Exception as exc:
        _log.warning(f"MP3 download failed ({mp3_url}): {exc}")
        return None


def _transcribe_sync(model, path: str, progress: dict) -> str:
    """Blocking — run via asyncio.to_thread. Writes processed/total audio-seconds into progress."""
    segments_gen, info = model.transcribe(
        path,
        beam_size=5,
        language=None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    progress["total"] = info.duration  # available before first segment is yielded
    texts = []
    t0 = time.monotonic()
    for seg in segments_gen:
        if seg.text.strip():
            texts.append(seg.text.strip())
        progress["processed"] = seg.end
    elapsed = time.monotonic() - t0
    if elapsed > 0:
        _log.info(
            f"Transcription done: {info.duration:.0f}s audio in {elapsed:.1f}s "
            f"({info.duration / elapsed:.1f}x real-time, {os.cpu_count()} logical CPUs, "
            f"threads={_WHISPER_THREADS or os.cpu_count()})"
        )
    return " ".join(texts)


async def _transcribe(path: str, progress: dict) -> str | None:
    try:
        model = await _get_whisper_model()
        async with _transcribe_sem:
            transcript = await asyncio.to_thread(_transcribe_sync, model, path, progress)
        return transcript or None
    except Exception as exc:
        _log.warning(f"Whisper transcription failed: {exc}")
        return None


def _fmt_eta(elapsed: float, processed: float, total: float) -> str:
    if processed <= 0:
        return "unbekannt"
    rate    = processed / elapsed
    eta_sec = (total - processed) / rate
    minutes = max(1, round(eta_sec / 60))
    return f"ca. {minutes} Min."


class ArdSoundsPlugin(Plugin):
    INTENTS         = ["ARDSOUNDS_SUMMARY"]
    INTENT_PREFIXES = {"ARDSOUNDS_SUMMARY": "ARDSOUNDS_SUMMARY:"}
    INTENT_LINES    = [
        "ARDSOUNDS_SUMMARY: <url> – Nutzer möchte eine ardsounds.de-Podcast-Episode "
        "zusammengefasst haben (URL im Format ardsounds.de/episode/urn:ard:episode:...)\n",
    ]
    intent_order = 31

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        m = _ARDSOUNDS_URL_RE.search(clean)
        return ("ARDSOUNDS_SUMMARY", m.group(1)) if m else None

    async def handle(self, ctx: MessageContext) -> None:
        urn = ctx.extra or None
        if not urn:
            m = _ARDSOUNDS_URL_RE.search(ctx.classify_text)
            if m:
                urn = m.group(1)
        if not urn and ctx.message.reference and ctx.message.reference.resolved:
            m = _ARDSOUNDS_URL_RE.search(ctx.message.reference.resolved.content or "")
            if m:
                urn = m.group(1)
        if not urn:
            await ctx.message.reply("Ich konnte keine gültige ardsounds.de-Episode-URL finden.")
            return

        meta = await _fetch_episode_metadata(urn)
        if meta is None:
            await ctx.message.reply(
                "Ich konnte die Episode-Informationen von ARD leider nicht abrufen."
            )
            return

        title   = meta["title"]
        mp3_url = meta["mp3_url"]

        status = await ctx.message.reply(f"Lade **{title}** herunter…")

        tmp_path = await _download_mp3(mp3_url)
        if tmp_path is None:
            await status.edit(content=f"Die Episode **{title}** konnte nicht heruntergeladen werden.")
            return

        await status.edit(content=f"Transkribiere **{title}**…")

        progress   = {"processed": 0.0, "total": 0.0}
        start_time = time.monotonic()

        async def _progress_loop():
            first_sent = False
            while True:
                await asyncio.sleep(30 if not first_sent else _UPDATE_INTERVAL)
                elapsed   = time.monotonic() - start_time
                processed = progress["processed"]
                total     = progress["total"]
                if total <= 0 or processed <= 0:
                    continue
                pct = processed / total * 100
                eta = _fmt_eta(elapsed, processed, total)
                await status.edit(
                    content=f"Transkribiere **{title}**… {pct:.0f}% fertig, noch {eta}."
                )
                first_sent = True

        progress_task = asyncio.create_task(_progress_loop())
        transcript = None
        try:
            transcript = await _transcribe(tmp_path, progress)
        finally:
            progress_task.cancel()
            Path(tmp_path).unlink(missing_ok=True)

        if not transcript:
            await status.edit(
                content=f"Die Transkription der Episode **{title}** ist leider fehlgeschlagen."
            )
            return

        await status.edit(content=f"Fasse **{title}** zusammen…")

        if len(transcript) > 25000:
            transcript = transcript[:25000] + " [...]"

        summary = await ctx.ask_claude(
            ctx.system_prompt +
            "\nFasse die folgende Podcast-Episode in deinem typischen Stil zusammen. "
            "Gib die wichtigsten Themen, Aussagen und Erkenntnisse wieder. "
            "Ziel: ca. 200–300 Wörter – prägnant, aber vollständig abgeschlossen. "
            "Antworte auf Deutsch.",
            [{"role": "user", "content": f"Episode: {title}\n\nTranskript:\n{transcript}"}],
            max_tokens=1500,
            tier=ctx.model_tier,
        )

        chunks = split_message(summary)
        await status.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await ctx.message.channel.send(chunk)


def setup(registry) -> None:
    registry.register(ArdSoundsPlugin())
