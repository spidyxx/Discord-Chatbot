"""Lyrics plugin — LYRICS intent."""
import asyncio
import configparser
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

import aiohttp
from bs4 import BeautifulSoup

from plugins.base import Plugin, MessageContext, _read, _write, split_message

_log = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_CACHE_PATH = _DATA_DIR / "lyrics_cache.json"

# ── Config ──────────────────────────────────────────────────────────────────

_cfg = configparser.ConfigParser(inline_comment_prefixes=("#",))
_cfg.read(Path(__file__).with_suffix(".cfg"))
_PRIMARY   = _cfg.get("lyrics", "primary_source",  fallback="genius").strip()
_FALLBACK  = (_cfg.get("lyrics", "fallback_source", fallback="lyrics_ovh").strip() or None)
_TIMEOUT   = int(_cfg.get("lyrics", "http_timeout_sec",  fallback="12"))
_TTL       = int(_cfg.get("lyrics", "cache_ttl_seconds", fallback="2592000"))
_CACHE_MAX = int(_cfg.get("lyrics", "cache_max_entries", fallback="500"))

_GENIUS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN")

# ── Type definitions ────────────────────────────────────────────────────────

LyricsSource = Literal["genius", "lyrics_ovh"]


@dataclass
class LyricsQuery:
    artist: str | None
    title:  str | None
    url:    str | None = None


@dataclass
class GeniusHit:
    title:   str
    artist:  str
    url:     str
    song_id: int
    score:   float


@dataclass
class LyricsResult:
    title:      str
    artist:     str
    body:       str
    source:     LyricsSource
    source_url: str
    fetched_at: float


@dataclass
class LyricsError:
    kind: Literal[
        "no_match", "ambiguous", "network", "timeout",
        "rate_limit", "missing_key", "scrape_failed",
    ]
    detail: str = ""
    candidates: list[GeniusHit] | None = None


# Internal exceptions for clean error propagation across HTTP helpers.
class _RateLimit(Exception): pass
class _Network(Exception): pass


# ── Constants ───────────────────────────────────────────────────────────────

_GENIUS_URL_RE     = re.compile(r'https?://genius\.com/[A-Za-z0-9\-]+-lyrics')
_GENIUS_SEARCH_URL = "https://api.genius.com/search"
_LYRICS_OVH_URL    = "https://api.lyrics.ovh/v1"


# ── Parsing ─────────────────────────────────────────────────────────────────

def _parse_artist_title(s: str) -> tuple[str, str] | None:
    parts = s.split(" - ", 1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return (parts[0].strip(), parts[1].strip())
    return None


def parse_query(extra: str, classify_text: str, replied_content: str | None) -> LyricsQuery | None:
    for src in (extra, classify_text, replied_content or ""):
        m = _GENIUS_URL_RE.search(src)
        if m:
            return LyricsQuery(artist=None, title=None, url=m.group(0))
    parsed = _parse_artist_title(extra)
    if parsed:
        return LyricsQuery(artist=parsed[0], title=parsed[1], url=None)
    return None


# ── Source: Genius ──────────────────────────────────────────────────────────

async def genius_search(artist: str, title: str, token: str) -> list[GeniusHit]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _GENIUS_SEARCH_URL,
                params={"q": f"{artist} {title}"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                if resp.status == 429:
                    raise _RateLimit()
                if resp.status != 200:
                    _log.warning(f"Genius API returned {resp.status}")
                    raise _Network()
                data = await resp.json()
    except aiohttp.ClientError as exc:
        _log.warning(f"Genius search network error: {exc}")
        raise _Network() from exc

    hits: list[GeniusHit] = []
    for i, hit in enumerate(((data.get("response") or {}).get("hits") or [])):
        result = hit.get("result") or {}
        if not result.get("url"):
            continue
        hits.append(GeniusHit(
            title=result.get("title") or "",
            artist=(result.get("primary_artist") or {}).get("name") or "",
            url=result["url"],
            song_id=int(result.get("id") or 0),
            score=float(hit.get("score") or 1.0 / (i + 1)),
        ))
    return hits


def _parse_genius_body(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select("[data-lyrics-container='true']")
    if not containers:
        return None
    parts: list[str] = []
    for c in containers:
        for br in c.find_all("br"):
            br.replace_with("\n")
        parts.append(c.get_text())
    text = "\n".join(parts)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def _parse_genius_meta(html: str) -> tuple[str | None, str | None]:
    """Extract (title, artist) from a Genius lyrics page's og:title meta."""
    soup = BeautifulSoup(html, "html.parser")
    og = soup.select_one('meta[property="og:title"]')
    raw = (og.get("content") if og else None) or ""
    # Genius og:title format: "Artist – Song Title" (en-dash) or "Artist - Song Title"
    for sep in (" – ", " — ", " - "):
        if sep in raw:
            artist, title = raw.split(sep, 1)
            return (title.strip() or None, artist.strip() or None)
    return (raw.strip() or None, None)


async def genius_fetch_body(url: str) -> str | None:
    """Fetch a Genius lyrics page and return the body, or None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    _log.warning(f"Genius HTML scrape failed (HTTP {resp.status}): {url}")
                    return None
                html = await resp.text()
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        _log.warning(f"Genius HTML scrape network error: {exc}")
        return None
    return _parse_genius_body(html)


# ── Source: lyrics.ovh ──────────────────────────────────────────────────────

async def lyrics_ovh_fetch(artist: str, title: str) -> LyricsResult | None:
    a = urllib.parse.quote(artist, safe="")
    t = urllib.parse.quote(title, safe="")
    url = f"{_LYRICS_OVH_URL}/{a}/{t}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    _log.warning(f"lyrics.ovh returned {resp.status} for {artist} - {title}")
                    return None
                data = await resp.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        _log.warning(f"lyrics.ovh network error: {exc}")
        return None

    body = (data.get("lyrics") or "").strip()
    if not body:
        return None
    return LyricsResult(
        title=title,
        artist=artist,
        body=body,
        source="lyrics_ovh",
        source_url=url,
        fetched_at=time.time(),
    )


# ── Cache ───────────────────────────────────────────────────────────────────

def _is_expired(result: LyricsResult) -> bool:
    return time.time() - result.fetched_at > _TTL


def _from_dict(d: dict) -> LyricsResult | None:
    try:
        return LyricsResult(**d)
    except TypeError:
        return None


def cache_get(artist: str, title: str) -> LyricsResult | None:
    key = (artist.lower().strip(), title.lower().strip())
    for item in _read(_CACHE_PATH):
        r = _from_dict(item)
        if r and (r.artist.lower().strip(), r.title.lower().strip()) == key:
            return r
    return None


def cache_get_by_url(url: str) -> LyricsResult | None:
    for item in _read(_CACHE_PATH):
        if item.get("source_url") == url:
            return _from_dict(item)
    return None


def cache_put(result: LyricsResult) -> None:
    cache = _read(_CACHE_PATH)
    # Drop expired and any entry with the same source_url (refresh-in-place).
    kept: list[dict] = []
    for item in cache:
        r = _from_dict(item)
        if r is None or _is_expired(r):
            continue
        if r.source_url == result.source_url:
            continue
        kept.append(item)
    kept.append(asdict(result))
    if len(kept) > _CACHE_MAX:
        kept.sort(key=lambda x: x.get("fetched_at", 0))
        kept = kept[-_CACHE_MAX:]
    _write(_CACHE_PATH, kept)


# ── Orchestration ───────────────────────────────────────────────────────────

def _is_ambiguous(hits: list[GeniusHit]) -> bool:
    if len(hits) < 2:
        return False
    return hits[0].score < 0.95 and hits[1].score >= 0.80 * hits[0].score


async def _try_fallback(artist: str, title: str) -> LyricsResult | None:
    if _FALLBACK == "lyrics_ovh":
        return await lyrics_ovh_fetch(artist, title)
    return None


async def fetch_lyrics(artist: str, title: str) -> LyricsResult | LyricsError:
    cached = cache_get(artist, title)
    if cached and not _is_expired(cached):
        return cached

    no_match_detail = f"{artist} - {title}"

    if _PRIMARY == "genius":
        if not _GENIUS_TOKEN:
            if _FALLBACK is None:
                return LyricsError("missing_key")
            result = await _try_fallback(artist, title)
            if result:
                cache_put(result)
                return result
            return LyricsError("no_match", detail=no_match_detail)

        try:
            hits = await genius_search(artist, title, _GENIUS_TOKEN)
        except asyncio.TimeoutError:
            return LyricsError("timeout")
        except _RateLimit:
            return LyricsError("rate_limit")
        except _Network:
            return LyricsError("network")

        if not hits:
            result = await _try_fallback(artist, title)
            if result:
                cache_put(result)
                return result
            return LyricsError("no_match", detail=no_match_detail)

        if _is_ambiguous(hits):
            return LyricsError("ambiguous", candidates=hits[:5])

        body = await genius_fetch_body(hits[0].url)
        if not body:
            result = await _try_fallback(artist, title)
            if result:
                cache_put(result)
                return result
            return LyricsError("scrape_failed")

        result = LyricsResult(
            title=hits[0].title,
            artist=hits[0].artist,
            body=body,
            source="genius",
            source_url=hits[0].url,
            fetched_at=time.time(),
        )
        cache_put(result)
        return result

    # _PRIMARY == "lyrics_ovh"
    result = await lyrics_ovh_fetch(artist, title)
    if result:
        cache_put(result)
        return result
    return LyricsError("no_match", detail=no_match_detail)


async def fetch_lyrics_by_url(url: str) -> LyricsResult | LyricsError:
    cached = cache_get_by_url(url)
    if cached and not _is_expired(cached):
        return cached

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                if resp.status == 429:
                    return LyricsError("rate_limit")
                if resp.status != 200:
                    _log.warning(f"Genius URL fetch failed (HTTP {resp.status}): {url}")
                    return LyricsError("network")
                html = await resp.text()
    except asyncio.TimeoutError:
        return LyricsError("timeout")
    except aiohttp.ClientError as exc:
        _log.warning(f"Genius URL fetch network error: {exc}")
        return LyricsError("network")

    body = _parse_genius_body(html)
    if not body:
        return LyricsError("scrape_failed")
    title, artist = _parse_genius_meta(html)

    result = LyricsResult(
        title=title or "Unbekannt",
        artist=artist or "Unbekannt",
        body=body,
        source="genius",
        source_url=url,
        fetched_at=time.time(),
    )
    cache_put(result)
    return result


# ── Formatting ──────────────────────────────────────────────────────────────

def render(result: LyricsResult) -> str:
    return f"**{result.title}** — {result.artist}\n*Quelle: {result.source_url}*\n\n{result.body}"


def render_error(err: LyricsError) -> str:
    if err.kind == "no_match":
        target = err.detail or "diesem Titel"
        return f'Ich finde keinen Songtext zu „{target}".'
    if err.kind == "ambiguous":
        lines = ["Mehrere Treffer – welcher ist gemeint?"]
        for i, hit in enumerate(err.candidates or [], 1):
            lines.append(f"{i}. {hit.artist} – {hit.title}")
        return "\n".join(lines)
    if err.kind == "network":
        return "Die Lyrics-Quelle ist gerade nicht erreichbar."
    if err.kind == "timeout":
        return "Die Anfrage hat zu lange gedauert. Versuch's gleich nochmal."
    if err.kind == "rate_limit":
        return "Lyrics-Limit erreicht – bitte später nochmal probieren."
    if err.kind == "missing_key":
        return "Lyrics sind nicht konfiguriert (kein API-Key)."
    if err.kind == "scrape_failed":
        return "Ich konnte den Songtext zwar finden, aber nicht auslesen."
    return "Ein unbekannter Fehler ist aufgetreten."


# ── Plugin class ────────────────────────────────────────────────────────────

class LyricsPlugin(Plugin):
    INTENTS = ["LYRICS"]

    INTENT_PREFIXES = {"LYRICS": "LYRICS:"}

    INTENT_LINES = [
        "LYRICS: <interpret> - <titel> – Nutzer möchte den Songtext zu einem Lied "
        "(Format: 'Künstler - Titel' mit Bindestrich, oder eine genius.com-URL)\n",
    ]

    GATE_PATTERNS = [r"songtext", r"lyrics", r"liedtext", r"text\s+(?:von|zu|des)\b", r"genius\.com"]

    intent_order = 32

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        m = _GENIUS_URL_RE.search(clean)
        return ("LYRICS", m.group(0)) if m else None

    async def handle(self, ctx: MessageContext) -> None:
        async with ctx.message.channel.typing():
            replied = None
            if ctx.message.reference and ctx.message.reference.resolved:
                replied = getattr(ctx.message.reference.resolved, "content", None)

            query = parse_query(ctx.extra, ctx.classify_text, replied)
            if query is None:
                await ctx.message.reply(
                    'Sag mir bitte „Künstler - Titel" oder schick eine genius.com-URL.'
                )
                return

            if query.url:
                result = await fetch_lyrics_by_url(query.url)
            else:
                result = await fetch_lyrics(query.artist, query.title)

            if isinstance(result, LyricsError):
                await ctx.message.reply(render_error(result))
                return

        text = render(result)
        chunks = split_message(text)
        await ctx.message.reply(chunks[0])
        for chunk in chunks[1:]:
            await ctx.message.channel.send(chunk)


def setup(registry) -> None:
    registry.register(LyricsPlugin())
