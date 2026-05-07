# Lyrics Plugin — Design Spec

Plugin for the `Marvin` Discord bot. Fetches song lyrics from a live source on @mention, returns title/artist/attribution and preserves verse/chorus structure.

---

## 1. Source selection

| Source | Auth | Rate limit | ToS | Reliability | Returns lyrics body? |
|---|---|---|---|---|---|
| **Genius API** (`api.genius.com`) | OAuth client token | Generous, undocumented soft limit (~unlimited for read) | Search/metadata via API is fine. **Lyrics body is NOT in the JSON response** — must scrape `genius.com/<path>` HTML | High (canonical source for many artists) | No (scrape needed) |
| **Musixmatch** | API key (free tier) | 2000 calls/day free | Free tier returns only **30%** of lyrics (`lyrics_body` truncated by ToS); full body needs commercial plan | High | Partial |
| **lyrics.ovh** (`api.lyrics.ovh/v1/{artist}/{title}`) | None | None published; community endpoint, occasional outages | Free, community-run, no commercial guarantees | Medium — outages, missing entries for non-English/obscure tracks | Yes (full plain-text) |

### Recommendation: **Genius API for search + scrape Genius HTML for body**, with **lyrics.ovh as fallback**.

Justification:
- Genius covers the largest catalog and is the strongest at disambiguation (search returns ranked hits with artist/title/url).
- Body must be scraped from `<div data-lyrics-container="true">` — already the de-facto pattern; no library required beyond `BeautifulSoup4` (add to `requirements.txt`).
- lyrics.ovh fallback handles outages and tracks Genius doesn't index. No key needed.
- Musixmatch ruled out: truncated free tier defeats the requirement.

---

## 2. Module breakdown

```
plugins/core/
├── lyrics.py        ← LyricsPlugin (intent handler, formatter, dispatch)
└── lyrics.cfg       ← model_tier + source preferences + cache TTL
```

Optional cache file (managed via existing `_read` / `_write` helpers from `plugins.base`):

```
$DATA_DIR/lyrics_cache.json   ← list of cached LyricsResult dicts
```

No new shared modules. No new HTTP client — reuse `aiohttp` (already used by `ardsounds.py`).

### Files Qwen must NOT modify

- `plugins/base.py`
- `plugins/registry.py`
- `bot.py`

### `requirements.txt` — add

```
beautifulsoup4>=4.12
```

---

## 3. Project conventions Qwen needs verbatim

### `MessageContext` (excerpt from `plugins/base.py` — relevant fields)

```python
@dataclass
class MessageContext:
    message:       discord.Message
    intent:        str
    extra:         str        = ""
    privileged:    bool       = False
    classify_text: str        = ""
    ask_claude:         object = None   # callable: (system, messages, max_tokens, tier) -> str
    system_prompt:      str    = ""
    model_tier:         str    = ""
```

### `Plugin` ABC contract (excerpt)

```python
class Plugin(ABC):
    INTENTS:         list[str]       = []
    INTENT_LINES:    list[str]       = []
    INTENT_PREFIXES: dict[str, str]  = {}
    intent_order: int = 50
    model_tier: str | None = None

    def pre_classify(self, clean: str) -> tuple[str, str] | None: return None
    async def on_ready(self) -> None: return
    @abstractmethod
    async def handle(self, ctx: MessageContext) -> None: ...
```

### Required `setup()` hook at module bottom

```python
def setup(registry) -> None:
    registry.register(LyricsPlugin())
```

### Helpers available from `plugins.base` (use these — do not reinvent)

```python
from plugins.base import Plugin, MessageContext, _read, _write, split_message
```

- `_read(path: Path) -> list` — JSON read with logging on failure
- `_write(path: Path, data: list)` — JSON write
- `split_message(text: str, limit: int = 2000) -> list[str]` — sentence-aware Discord chunker

### Logging convention

```python
_log = logging.getLogger(__name__)
```

### DATA_DIR convention

```python
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
```

### Reply convention (German bot, mirror existing plugins)

User-facing strings are German. Sending pattern from `youtube.py`:

```python
chunks = split_message(reply)
await ctx.message.reply(chunks[0])
for chunk in chunks[1:]:
    await ctx.message.channel.send(chunk)
```

For long-running fetches (mirror `ardsounds.py`): post a status message first, edit it once results arrive.

---

## 4. @mention syntax

Two paths, mirroring `REMINDER:` (LLM-classified, structured payload) and `YOUTUBE_SUMMARY:` (URL pre-classify):

| Trigger | Path | Example |
|---|---|---|
| Free-form German request | Haiku classifier emits `LYRICS: <artist> - <title>` | `@Marvin spiel mir den Songtext zu Bohemian Rhapsody von Queen` |
| Direct slash-style | Same payload pre-pended by user | `@Marvin LYRICS: Queen - Bohemian Rhapsody` |
| Genius URL in message or reply | `pre_classify` regex bypasses Haiku | `@Marvin fass das mal zusammen` (replying to a `https://genius.com/Queen-bohemian-rhapsody-lyrics` link) |

### Payload format

`LYRICS: <artist> - <title>` (separator: ` - `, surrounding spaces required for unambiguous artist/title split).

If Genius URL was pre-classified, `extra` carries the URL itself; the handler detects `http` prefix and routes to URL-direct fetch (skips search).

### `INTENT_LINES` (German, matches `youtube.py` style)

```python
INTENT_LINES = [
    "LYRICS: <interpret> - <titel> – Nutzer möchte den Songtext zu einem Lied "
    "(Format: 'Künstler - Titel' mit Bindestrich, oder eine genius.com-URL)\n",
]
```

### `INTENT_PREFIXES`

```python
INTENT_PREFIXES = {"LYRICS": "LYRICS:"}
```

### `intent_order`

`32` (slots after `youtube.py=30` and `ardsounds.py=31`).

---

## 5. Type definitions

```python
from dataclasses import dataclass, asdict
from typing import Literal

LyricsSource = Literal["genius", "lyrics_ovh"]

@dataclass
class LyricsQuery:
    artist: str | None      # None when query is a Genius URL
    title:  str | None      # None when query is a Genius URL
    url:    str | None = None   # set when pre_classify caught a Genius URL

@dataclass
class GeniusHit:
    title:      str
    artist:     str
    url:        str         # https://genius.com/...
    song_id:    int
    score:      float       # Genius "match score" or 1/rank as proxy

@dataclass
class LyricsResult:
    title:      str
    artist:     str
    body:       str         # plain text, verses separated by blank lines
    source:     LyricsSource
    source_url: str
    fetched_at: float       # unix ts; for cache eviction
```

---

## 6. Public function signatures

All in `plugins/core/lyrics.py`.

```python
# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_query(extra: str, classify_text: str, replied_content: str | None) -> LyricsQuery | None: ...
def _parse_artist_title(s: str) -> tuple[str, str] | None: ...   # splits on " - "

# ── Source: Genius ───────────────────────────────────────────────────────────
async def genius_search(artist: str, title: str, token: str) -> list[GeniusHit]: ...
async def genius_fetch_body(url: str) -> str | None: ...   # scrape <div data-lyrics-container>

# ── Source: lyrics.ovh ───────────────────────────────────────────────────────
async def lyrics_ovh_fetch(artist: str, title: str) -> LyricsResult | None: ...

# ── Orchestration ────────────────────────────────────────────────────────────
async def fetch_lyrics(artist: str, title: str) -> LyricsResult | LyricsError: ...
async def fetch_lyrics_by_url(url: str) -> LyricsResult | LyricsError: ...

# ── Cache ────────────────────────────────────────────────────────────────────
def cache_get(artist: str, title: str) -> LyricsResult | None: ...
def cache_put(result: LyricsResult) -> None: ...

# ── Formatting ───────────────────────────────────────────────────────────────
def render(result: LyricsResult) -> str: ...
def render_error(err: "LyricsError") -> str: ...
```

### Request/response schemas

**Genius search** — `GET https://api.genius.com/search?q=<artist>%20<title>`
Headers: `Authorization: Bearer <GENIUS_ACCESS_TOKEN>`
Response (subset):
```json
{ "response": { "hits": [
  { "result": { "id": 123, "title": "...", "primary_artist": {"name": "..."}, "url": "..." } }
]}}
```

**Genius HTML scrape** — `GET <hit.url>`
Parse with `BeautifulSoup`: collect `soup.select("[data-lyrics-container='true']")`, replace `<br>` with `\n`, strip tags, then collapse `\n{3,}` → `\n\n` to preserve verse breaks.

**lyrics.ovh** — `GET https://api.lyrics.ovh/v1/<artist>/<title>`
Response:
```json
{ "lyrics": "[verse 1]\n\n[verse 2]\n..." }
```
404 → no match.

---

## 7. Config schema — `plugins/core/lyrics.cfg`

```ini
[plugin]
model_tier = cheap

[lyrics]
primary_source     = genius        # genius | lyrics_ovh
fallback_source    = lyrics_ovh    # or empty for none
http_timeout_sec   = 12
cache_ttl_seconds  = 2592000       # 30 days
cache_max_entries  = 500
```

Read in module, mirroring `ardsounds.py`:

```python
import configparser
_cfg = configparser.ConfigParser()
_cfg.read(Path(__file__).with_suffix(".cfg"))
_PRIMARY   = _cfg.get("lyrics", "primary_source",  fallback="genius")
_FALLBACK  = _cfg.get("lyrics", "fallback_source", fallback="lyrics_ovh") or None
_TIMEOUT   = int(_cfg.get("lyrics", "http_timeout_sec",  fallback="12"))
_TTL       = int(_cfg.get("lyrics", "cache_ttl_seconds", fallback="2592000"))
_CACHE_MAX = int(_cfg.get("lyrics", "cache_max_entries", fallback="500"))
```

### New env var (document in `.env.example` + `CLAUDE.md`)

```
GENIUS_ACCESS_TOKEN=...   # optional; if unset, plugin falls back to lyrics.ovh only
```

Read at module level: `_GENIUS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN")`.

---

## 8. Error model

Project has no shared exception hierarchy — existing plugins return `None` from internal helpers and let `handle()` translate to user replies (see `youtube.py`, `ardsounds.py`). Match that pattern exactly.

```python
@dataclass
class LyricsError:
    kind: Literal[
        "no_match", "ambiguous", "network", "timeout",
        "rate_limit", "missing_key", "scrape_failed",
    ]
    detail: str = ""
    candidates: list[GeniusHit] | None = None   # populated for "ambiguous"
```

### User-facing messages (German, match existing plugin tone)

| `kind` | Trigger | Reply |
|---|---|---|
| `no_match` | Genius search 0 hits, lyrics.ovh 404 | `Ich finde keinen Songtext zu „<artist> - <title>".` |
| `ambiguous` | ≥2 Genius hits within 80% of top score AND top score < 0.95 | `Mehrere Treffer – welcher ist gemeint?\n` + numbered list `1. <artist> – <title>` (max 5) |
| `network` | `aiohttp.ClientError` (non-timeout) | `Die Lyrics-Quelle ist gerade nicht erreichbar.` |
| `timeout` | `asyncio.TimeoutError` | `Die Anfrage hat zu lange gedauert. Versuch's gleich nochmal.` |
| `rate_limit` | HTTP 429 | `Lyrics-Limit erreicht – bitte später nochmal probieren.` |
| `missing_key` | `_GENIUS_TOKEN is None` AND fallback also unavailable | `Lyrics sind nicht konfiguriert (kein API-Key).` |
| `scrape_failed` | Genius HTML parse returned empty body | `Ich konnte den Songtext zwar finden, aber nicht auslesen.` |

`_log.warning(...)` for `network`, `timeout`, `rate_limit`, `scrape_failed` (mirrors `ardsounds.py` logging style). No stack traces to the user.

---

## 9. Caching

- **Storage**: `$DATA_DIR/lyrics_cache.json` — list of `LyricsResult` dicts (use `dataclasses.asdict`).
- **Key**: `(artist.lower().strip(), title.lower().strip())`. Linear scan is fine at `cache_max_entries=500`.
- **Read**: on every `fetch_lyrics()` call, before any network request.
- **Write**: after successful fetch. Evict oldest by `fetched_at` once `len > cache_max_entries`.
- **TTL**: entries older than `cache_ttl_seconds` are ignored on read and pruned on write.
- **No cache** for ambiguous/error results.
- **URL-direct fetches** (Genius URL pre-classify): cache by `source_url` instead of `(artist, title)`.

Use `_read` / `_write` from `plugins.base`. Do not introduce `sqlite`, `diskcache`, etc.

---

## 10. Copyright / ToS

- Lyrics are copyrighted; redistribution is generally restricted. The bot serves them in a private/semi-private Discord channel — equivalent to other small-bot deployments using the same APIs.
- Genius API ToS permit search; lyrics body is technically scraped from the public HTML page, which is a grey area many open-source projects accept. **Document this in `CLAUDE.md`** so the maintainer understands the risk.
- Always include `source_url` attribution in the reply (already in spec). Never claim the bot wrote the lyrics.
- lyrics.ovh is a community proxy; rely on it as fallback only.
- Do **not** persist lyrics beyond the local cache file (no DB, no external sync).
- Maintainer should be aware: takedown requests, if any, would land at the deployer — not Anthropic, not Genius. Easy mitigation: delete `$DATA_DIR/lyrics_cache.json`.

---

## 11. Output formatting

Reply structure (plain Discord markdown, no embeds — matches existing plugins):

```
**<title>** — <artist>
*Quelle: <source_url>*

<lyrics body, verses separated by blank lines, ~40 lines>
```

- `render()` joins header + body, then is fed to `split_message()` for 2000-char chunking.
- Verse breaks come from the source (`\n\n`); preserve as-is.
- Strip Genius's bracketed section markers like `[Verse 1]` only if `lyrics.cfg` adds `strip_section_headers = true` (default: keep them — they help structure).

---

## 12. Pseudocode

### @mention parser

```
def parse_query(extra, classify_text, replied_content):
    # 1. Genius URL anywhere?
    for src in (extra, classify_text, replied_content or ""):
        m = GENIUS_URL_RE.search(src)
        if m: return LyricsQuery(artist=None, title=None, url=m.group(0))

    # 2. "artist - title" payload
    parsed = _parse_artist_title(extra)
    if parsed:
        return LyricsQuery(artist=parsed[0], title=parsed[1], url=None)

    # 3. give up — handler will reply with usage hint
    return None
```

`GENIUS_URL_RE = re.compile(r'https?://genius\.com/[A-Za-z0-9\-]+-lyrics')`

`_parse_artist_title` splits on first ` - `; both sides must be non-empty after `.strip()`.

### Retrieval flow

```
async def fetch_lyrics(artist, title):
    cached = cache_get(artist, title)
    if cached and not expired(cached): return cached

    if _PRIMARY == "genius":
        if not _GENIUS_TOKEN:
            return await _fallback_only(artist, title)
        try:
            hits = await genius_search(artist, title, _GENIUS_TOKEN)
        except Timeout:        return LyricsError("timeout")
        except RateLimit:      return LyricsError("rate_limit")
        except NetworkError:   return LyricsError("network")

        if not hits:           return await _try_fallback(artist, title) or LyricsError("no_match")
        if _is_ambiguous(hits): return LyricsError("ambiguous", candidates=hits[:5])

        body = await genius_fetch_body(hits[0].url)
        if not body:           return await _try_fallback(...) or LyricsError("scrape_failed")

        result = LyricsResult(hits[0].title, hits[0].artist, body, "genius", hits[0].url, time.time())
        cache_put(result)
        return result

    # _PRIMARY == "lyrics_ovh"
    return await lyrics_ovh_fetch(artist, title) or LyricsError("no_match")
```

Ambiguity heuristic:
```
def _is_ambiguous(hits):
    if len(hits) < 2: return False
    return hits[0].score < 0.95 and hits[1].score >= 0.80 * hits[0].score
```

(Score: if Genius API doesn't return one, use `1.0/(rank+1)` as proxy, or fuzzy ratio between `hit.title+hit.artist` and `query`. Use `difflib.SequenceMatcher` — stdlib, no new dep.)

### Handler skeleton

```python
async def handle(self, ctx: MessageContext) -> None:
    async with ctx.message.channel.typing():
        replied = ctx.message.reference.resolved.content if (
            ctx.message.reference and ctx.message.reference.resolved
        ) else None

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
```

---

## 13. Worked example

**Input** (in a main channel):
```
@Marvin spiel mir den Songtext zu Bohemian Rhapsody von Queen
```

**Classifier output** (Haiku, `cheap` tier):
```
LYRICS: Queen - Bohemian Rhapsody
```

**Dispatch**: `intent="LYRICS"`, `extra="Queen - Bohemian Rhapsody"`.

**Retrieval**:
1. `cache_get("queen", "bohemian rhapsody")` → miss
2. `genius_search("Queen", "Bohemian Rhapsody", token)` → top hit `{title: "Bohemian Rhapsody", artist: "Queen", url: "https://genius.com/Queen-bohemian-rhapsody-lyrics", score: 0.99}`
3. Not ambiguous (score 0.99 ≥ 0.95)
4. `genius_fetch_body(url)` → plain-text body (~45 lines, verse breaks preserved)
5. `cache_put(result)`

**Rendered reply** (single chunk under 2000 chars):
```
**Bohemian Rhapsody** — Queen
*Quelle: https://genius.com/Queen-bohemian-rhapsody-lyrics*

[Intro]
<lyrics body, ~45 lines, verse/chorus structure preserved>
```

(If body pushes total over 2000 chars, `split_message` cuts at the nearest sentence/newline boundary; subsequent chunks go via `channel.send`.)

---

## 14. Verification (per `CLAUDE.md`)

```bash
python -c "from plugins.registry import discover; print(discover())"

python -c "
from plugins.registry import discover, registry
discover()
for line in registry.intent_lines():
    if 'LYRICS' in line: print(repr(line))
"
```

Then mention the bot in Discord.

---

## 15. Acceptance criteria

- [ ] `plugins/core/lyrics.py` exists; `from plugins.registry import discover; discover()` lists `LyricsPlugin` with `INTENTS == ["LYRICS"]`.
- [ ] `plugins/core/lyrics.cfg` exists with `[plugin] model_tier = cheap` and a `[lyrics]` section.
- [ ] `LyricsPlugin.INTENT_LINES` produces a single German line containing `LYRICS:` and `Künstler - Titel` (or `Interpret - Titel`).
- [ ] `LyricsPlugin.INTENT_PREFIXES == {"LYRICS": "LYRICS:"}`.
- [ ] `LyricsPlugin.intent_order == 32`.
- [ ] `LyricsPlugin.pre_classify("https://genius.com/Queen-bohemian-rhapsody-lyrics")` returns `("LYRICS", "<that url>")`.
- [ ] `LyricsPlugin.pre_classify("hello world")` returns `None`.
- [ ] `_parse_artist_title("Queen - Bohemian Rhapsody") == ("Queen", "Bohemian Rhapsody")`.
- [ ] `_parse_artist_title("nope") is None`.
- [ ] With `GENIUS_ACCESS_TOKEN` set and network available, `await fetch_lyrics("Queen", "Bohemian Rhapsody")` returns a `LyricsResult` with `body` non-empty (>200 chars), `source == "genius"`, `source_url` starting with `https://genius.com/`.
- [ ] With `GENIUS_ACCESS_TOKEN` unset and `_FALLBACK == "lyrics_ovh"`, the same call returns a `LyricsResult` with `source == "lyrics_ovh"`.
- [ ] `await fetch_lyrics("xx_no_such_artist_xx", "xx_no_such_title_xx")` returns `LyricsError(kind="no_match")`.
- [ ] Simulated `aiohttp.ClientError` in `genius_search` → `LyricsError(kind="network")`; `asyncio.TimeoutError` → `LyricsError(kind="timeout")`; HTTP 429 → `LyricsError(kind="rate_limit")`.
- [ ] No `GENIUS_ACCESS_TOKEN` AND `_FALLBACK is None` → `LyricsError(kind="missing_key")`.
- [ ] After a successful fetch, a second identical call within `cache_ttl_seconds` makes **no** network calls (verified by patching `aiohttp.ClientSession`).
- [ ] `lyrics_cache.json` never grows beyond `cache_max_entries`.
- [ ] `render(result)` output begins with `**<title>** — <artist>\n*Quelle: <url>*\n\n` and is followed by the body verbatim.
- [ ] `render(result)` paired with `split_message` always produces chunks ≤ 2000 chars.
- [ ] `handle()` sends the first chunk via `ctx.message.reply()` and subsequent chunks via `ctx.message.channel.send()` (matches `youtube.py`).
- [ ] Ambiguous case: when ≥2 hits within 80% of top score and top < 0.95, reply contains a numbered list of up to 5 candidates (artist – title) and does **not** contain any lyrics body.
- [ ] No imports from `bot.py` anywhere in `plugins/core/lyrics.py`.
- [ ] No new third-party dependencies beyond `beautifulsoup4` (which must be added to `requirements.txt`).
- [ ] `_log = logging.getLogger(__name__)` is the only logger used.
- [ ] `setup(registry)` exists at module bottom and calls `registry.register(LyricsPlugin())`.
