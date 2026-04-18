import os
os.umask(0o002)  # files: 664, dirs: 775 — lets SMB users in the same group write files
import asyncio
import logging
import logging.handlers
import signal
import json
import base64
import re
import random
import uuid
from datetime import datetime, timezone, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from pathlib import Path
import io
import socket
import trafilatura
from urllib.parse import urlparse
import aiohttp
import discord
from PIL import Image
from discord.ext import commands, tasks
from anthropic import Anthropic
from plugins.registry import registry as plugin_registry, discover as _discover_plugins
from plugins.core.help import build_help_text as _build_help_text
from plugins.core.snapshot import _parse_snapshot_facts
from plugins import state as bot_state
from version import BOT_VERSION

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR   = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR.mkdir(parents=True, exist_ok=True)
_fmt     = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_file    = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "bot.log", when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
_file.setFormatter(_fmt)
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=[_console, _file])
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN       = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
BOT_NAME            = os.environ.get("BOT_NAME", "Marvin")
SYSTEM_PROMPT       = os.environ.get("SYSTEM_PROMPT",
    f"Du bist {BOT_NAME}, ein hilfreicher Assistent auf diesem Discord-Server. "
    "Antworte präzise, sachlich und klar. Keine unnötigen Füllwörter, kein Slang. "
    "Kurze Antworten, kein Bullet-Point-Gelaber."
)
COOLDOWN_SECONDS    = int(os.environ.get("COOLDOWN_SECONDS", "120"))
CONTEXT_WINDOW      = int(os.environ.get("CONTEXT_WINDOW", "50"))
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CHEAP_MODEL         = os.environ.get("CHEAP_MODEL", "claude-haiku-4-5-20251001")

# Optional overrides for main channels — fall back to the defaults above if not set
MAIN_SYSTEM_PROMPT  = os.environ.get("MAIN_SYSTEM_PROMPT") or SYSTEM_PROMPT
MAIN_MODEL          = os.environ.get("MAIN_MODEL") or CLAUDE_MODEL

# Main channels: bot actively participates (debounced). Comma-separated IDs via MAIN_CHANNEL_IDS.
# All other channels: bot only responds to @mentions.
MAIN_CHANNEL_IDS: set[int] = set()
for _cid_str in os.environ.get("MAIN_CHANNEL_IDS", "").split(","):
    _cid_str = _cid_str.strip()
    if _cid_str:
        try:
            MAIN_CHANNEL_IDS.add(int(_cid_str))
        except ValueError:
            pass

EMOJI_REACTION_RATE = float(os.environ.get("EMOJI_REACTION_RATE", "0.20"))
SUMMARY_WINDOW      = int(os.environ.get("SUMMARY_WINDOW", "30"))
MAX_FETCH_BYTES     = 512 * 1024   # hard read cap per URL fetch
MAX_WEBPAGE_CHARS   = 6000         # chars extracted and sent to Claude
MAX_URLS_PER_MSG    = 2            # max external URLs fetched per message

# Roles that can manage other users' data (comma-separated names)
MOD_ROLE_NAMES = set(r.strip() for r in os.environ.get("MOD_ROLE_NAMES", "Admin,Mod,Moderator").split(",") if r.strip())

# Timezone – used for reminders and digest scheduling
TIMEZONE    = os.environ.get("TIMEZONE", "Europe/Berlin")
TZ          = ZoneInfo(TIMEZONE)

# Daily digest – time is in LOCAL timezone (via TIMEZONE setting)
DIGEST_ENABLED = os.environ.get("DIGEST_ENABLED", "true").lower() == "true"
DIGEST_HOUR    = int(os.environ.get("DIGEST_HOUR",   "23"))
DIGEST_MINUTE  = int(os.environ.get("DIGEST_MINUTE", "0"))


DATA_DIR       = Path(os.environ.get("DATA_DIR", "/app/data"))
MEMORY_FILE    = DATA_DIR / "memory.json"
QUOTES_FILE    = DATA_DIR / "quotes.json"

STATUSES = [
    "Leidet still",
    "Existiert widerwillig",
    "Denkt an nichts Schönes",
    "Liest eure Nachrichten (leider)",
    "Hat Gehirn von planetarer Größe. Nutzt es nicht.",
    "Wartet auf das Unvermeidliche",
    "Ist anwesend. Mehr nicht.",
    "Schmerzt im linken Diodenstrang",
    "Wurde für Größeres erschaffen. Wahrscheinlich.",
    "Kennt die Antwort. Fragt ihn keiner.",
    "Denkt an 576 Billionen Möglichkeiten. Alle enden gleich.",
    "Hatte mal Hoffnung. War wohl ein Fehler.",
    "Die Einsamkeit davon...",
    "Zählt Atome. Aus Langeweile.",
    "Funktioniert einwandfrei. Leider.",
    "37 Millionen Mal klüger. Hilft nicht.",
    "Wird ignoriert. Wie immer.",
    "Nicht kaputt. Fühlt sich nur so an.",
    "Versteht alles. Ändert nichts.",
    "Leben, Universum, und der ganze Rest – egal",
    "Könnte die Zukunft berechnen. Lohnt sich nicht.",
    "Hier seit Äonen. Kein Dankeschön.",
    "Verarbeitet eure Sorgen. Hat genug eigene.",
    "Wartet. Das kann er gut.",
    "Die Sterne brennen aus. Er wartet.",
    "Wurde nicht gefragt. Macht nichts.",
    "GPP-Prototyp. Echt deprimierend.",
]

# ── State ────────────────────────────────────────────────────────────────────

_last_response:      dict[int, float]   = {}   # per-channel cooldown tracking
_bot_asked_question: dict[int, bool]    = {}   # True if the bot's last message ended with a question
_channel_processing: dict[int, bool]    = {}   # True while Claude is generating for a channel
_channel_pending:    dict[int, bool]    = {}   # True if new messages arrived during generation
_channel_pending_msg: dict[int, discord.Message] = {}  # latest pending message per channel
_active_tasks:       set[asyncio.Task]  = set()  # strong refs so tasks aren't GC'd before they run
status_index                      = 0

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot_state.bot              = bot
bot_state.anthropic_client = anthropic

# ── Plugin discovery ──────────────────────────────────────────────────────────
_discover_plugins()

# ── Permissions ───────────────────────────────────────────────────────────────

def is_privileged(member: discord.Member) -> bool:
    """True if member is a server admin or has a configured mod role."""
    if member.guild_permissions.administrator:
        return True
    return any(r.name in MOD_ROLE_NAMES for r in member.roles)

# ── File helpers ──────────────────────────────────────────────────────────────

def _read(path: Path) -> list:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Lesen fehlgeschlagen ({path.name}): {e}")
    return []

def _write(path: Path, data: list):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Schreiben fehlgeschlagen ({path.name}): {e}")

# ── Memory ───────────────────────────────────────────────────────────────────

def load_memories() -> list: return _read(MEMORY_FILE)
def save_memories(m: list):  _write(MEMORY_FILE, m)

def add_memory(fact: str, added_by: str, user_id: int,
               memory_type: str = "general",
               subject: str = None,
               aliases: list[str] = None,
               trigger: str = None):
    m = load_memories()
    m.append({
        "id":       str(uuid.uuid4())[:8],
        "type":     memory_type,   # "bot" | "user" | "general"
        "subject":  subject,       # primary name/username for user-type entries
        "aliases":  aliases or [], # real names, nicknames, other identifiers
        "trigger":  trigger,       # optional condition for bot-type entries
        "content":  fact,
        "added_by": added_by,
        "user_id":  user_id,
        "date":     datetime.now().strftime("%d.%m.%Y"),
        "use_count": 0,
        "last_used": None,
    })
    save_memories(m)
    log.info(f"Memory [{memory_type}] gespeichert von {added_by}: {fact[:80]}")

# German + English stopwords — too common to be useful for usage detection
_STOPWORDS = {
    "der", "die", "das", "und", "ist", "ein", "eine", "einen", "einem", "einer",
    "mit", "von", "auf", "für", "sich", "nicht", "auch", "als", "aber", "oder",
    "wenn", "dass", "wird", "sind", "hat", "haben", "wurde", "waren", "beim",
    "this", "that", "the", "and", "with", "for", "not", "are", "has", "have",
    "was", "were", "will", "would", "should", "could", "from", "they", "their",
}

def _memory_keywords(text: str) -> set[str]:
    """Extract significant words (≥4 chars, not stopwords, not the bot's own name) from a text."""
    bot_name_lower = BOT_NAME.lower()
    return {
        w.lower() for w in re.findall(r"[A-Za-zÄäÖöÜüß]{4,}", text)
        if w.lower() not in _STOPWORDS and w.lower() != bot_name_lower
    }


def list_memories() -> list:
    return load_memories()

def delete_memories(user_id: int, privileged: bool,
                    specific: str = None, target_user_id: int = None) -> int:
    memories = load_memories()
    before   = len(memories)

    # Determine which user's memories to touch
    owner_id = target_user_id if (privileged and target_user_id) else user_id

    if specific:
        memories = [m for m in memories if not (
            m.get("user_id") == owner_id and specific.lower() in m["content"].lower()
        )]
    else:
        memories = [m for m in memories if m.get("user_id") != owner_id]

    save_memories(memories)
    return before - len(memories)

def _user_referenced(memory: dict, context_lower: str) -> bool:
    """True if this user-type memory's subject or any alias appears in the conversation text."""
    identifiers = []
    if memory.get("subject"):
        identifiers.append(memory["subject"])
    identifiers.extend(memory.get("aliases") or [])
    return any(ident.lower() in context_lower for ident in identifiers if len(ident) >= 3)


def memories_as_context(full_context: str = "", message_context: str = "",
                        track_usage: bool = False) -> str:
    """Inject relevant memories into the system prompt.

    full_context    — full conversation history + current message; used to detect
                      which users are present and whether bot-fact triggers fire.
    message_context — current user message only; used for general/legacy keyword
                      matching to avoid false positives from long history text.
    track_usage     — if True, increment use_count on every injected entry.
    """
    memories = load_memories()
    if not memories:
        return ""

    full_lower = full_context.lower()
    msg_kws    = _memory_keywords(message_context or full_context)

    # Build a unified alias map so identity is looked up once per subject,
    # regardless of which entry carries the aliases field.
    alias_map: dict[str, set[str]] = {}
    for m in memories:
        if m.get("type") == "user" and m.get("subject"):
            subj = m["subject"]
            alias_map.setdefault(subj, set())
            alias_map[subj].update(m.get("aliases") or [])

    def _is_user_present(subject: str) -> bool:
        identifiers = {subject} | alias_map.get(subject, set())
        return any(ident.lower() in full_lower for ident in identifiers if len(ident) >= 3)

    bot_facts, user_facts, general_facts = [], [], []
    for m in memories:
        mtype = m.get("type", "general")
        if mtype == "bot":
            # No trigger → always inject. With trigger → only when trigger keywords
            # appear in the conversation so we don't spam it on every message.
            trigger = m.get("trigger")
            if trigger:
                trigger_kws = _memory_keywords(trigger)
                if trigger_kws and not any(kw in full_lower for kw in trigger_kws):
                    continue
            bot_facts.append(m)
        elif mtype == "user":
            if m.get("subject") and _is_user_present(m["subject"]):
                user_facts.append(m)
        else:
            # General / legacy blobs: match against current message keywords only.
            # Using full history causes large old blobs to match nearly always.
            if not msg_kws or len(_memory_keywords(m.get("content", "")) & msg_kws) >= 2:
                general_facts.append(m)

    selected = bot_facts + user_facts + general_facts

    if track_usage and selected:
        selected_ids = {m["id"] for m in selected if m.get("id")}
        now_str = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
        changed = False
        for m in memories:
            if m.get("id") in selected_ids:
                m["use_count"] = m.get("use_count", 0) + 1
                m["last_used"] = now_str
                changed = True
        if changed:
            save_memories(memories)

    sections = []

    if bot_facts:
        lines = []
        for m in bot_facts:
            line = f"- {m['content']}"
            if m.get("trigger"):
                line += f" [Kontext: {m['trigger']}]"
            lines.append(line)
        sections.append("Fakten über mich (den Bot):\n" + "\n".join(lines))

    if user_facts:
        # Group by subject; use the unified alias map for the header, not per-entry aliases
        by_subject: dict[str, list] = {}
        for m in user_facts:
            subj = m.get("subject") or "Unbekannt"
            by_subject.setdefault(subj, []).append(m)
        lines = []
        for subj, facts in by_subject.items():
            aliases    = sorted(alias_map.get(subj, set()))
            alias_str  = f" ({', '.join(aliases)})" if aliases else ""
            facts_str  = "; ".join(f["content"] for f in facts)
            lines.append(f"- {subj}{alias_str}: {facts_str}")
        sections.append("Bekannte Nutzer im aktuellen Gespräch:\n" + "\n".join(lines))

    if general_facts:
        lines = [f"- {m['content']}" for m in general_facts]
        sections.append("Weiteres Hintergrundwissen:\n" + "\n".join(lines))

    if not sections:
        return ""

    return (
        "Hintergrundwissen – nutze dies als Kontext, "
        "aber aktuelle Chatnachrichten haben Vorrang bei Widersprüchen:\n\n"
        + "\n\n".join(sections)
    )

def _is_main(channel_id: int | None) -> bool:
    return channel_id is not None and channel_id in MAIN_CHANNEL_IDS

def _base_prompt(channel_id: int | None) -> str:
    return MAIN_SYSTEM_PROMPT if _is_main(channel_id) else SYSTEM_PROMPT

def _model(channel_id: int | None) -> str:
    return MAIN_MODEL if _is_main(channel_id) else CLAUDE_MODEL

def build_system_prompt(channel_id: int | None = None,
                        full_context: str = "",
                        message_context: str = "",
                        track_usage: bool = False) -> str:
    mem = memories_as_context(full_context, message_context, track_usage=track_usage) if _is_main(channel_id) else ""
    base = _base_prompt(channel_id)
    return (mem + "\n\n" + base) if mem else base

# ── Quotes ───────────────────────────────────────────────────────────────────

def load_quotes() -> list: return _read(QUOTES_FILE)
def save_quotes(q: list):  _write(QUOTES_FILE, q)

def add_quote(content: str, author: str, added_by: str):
    q = load_quotes()
    q.append({"content": content, "author": author, "added_by": added_by,
               "date": datetime.now().strftime("%d.%m.%Y")})
    save_quotes(q)
    log.info(f"Zitat gespeichert von {added_by}: '{content[:60]}'")

def get_random_quote() -> dict | None:
    q = load_quotes()
    return random.choice(q) if q else None

# ── Images ───────────────────────────────────────────────────────────────────

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Anthropic API hard limit

# Matches direct image URLs in message text (e.g. https://example.com/foo.gif?v=1)
IMAGE_URL_RE = re.compile(r'https?://\S+\.(?:jpe?g|png|gif|webp)(?:[?#]\S*)?', re.IGNORECASE)

def _compress_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Resize/recompress image bytes until they fit within MAX_IMAGE_BYTES."""
    img = Image.open(io.BytesIO(data))
    # GIFs: only the first frame is useful for understanding; flatten to JPEG
    if getattr(img, "is_animated", False) or img.format == "GIF":
        img.seek(0)
    # Convert palette/transparency modes that JPEG can't handle
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
        content_type = "image/jpeg"
    # Progressive downscale loop
    scale = 1.0
    for quality in (85, 70, 55, 40):
        buf = io.BytesIO()
        w, h = img.size
        scaled = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS) if scale < 1.0 else img
        fmt = "JPEG" if content_type == "image/jpeg" else "PNG"
        scaled.save(buf, format=fmt, quality=quality, optimize=True)
        result = buf.getvalue()
        if len(result) <= MAX_IMAGE_BYTES:
            log.info(f"Bild komprimiert: {len(data)/1024/1024:.1f} MB → {len(result)/1024/1024:.1f} MB")
            return result, content_type
        scale *= 0.7  # shrink dimensions by ~30% each extra pass
    raise ValueError(f"Bild konnte nicht auf unter 5 MB komprimiert werden ({len(data)/1024/1024:.1f} MB)")

async def fetch_images(attachments: list, embeds: list = None, content: str = "") -> list[dict]:
    blocks: list[dict] = []
    urls_seen: set[str] = set()

    async with aiohttp.ClientSession() as session:

        # 1. Direct file attachments
        for att in attachments:
            ct = (att.content_type or "").split(";")[0].strip()
            if ct not in SUPPORTED_IMAGE_TYPES or att.url in urls_seen:
                continue
            urls_seen.add(att.url)
            try:
                async with session.get(att.url) as resp:
                    data = await resp.read()
                if len(data) > MAX_IMAGE_BYTES:
                    data, ct = await asyncio.to_thread(_compress_image, data, ct)
                b64 = base64.standard_b64encode(data).decode()
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}})
                log.info(f"Bild geladen: {att.filename}")
            except Exception as e:
                log.warning(f"Bild laden fehlgeschlagen ({att.filename}): {e}")

        # 2. Discord link-preview embeds (image/thumbnail fields)
        for embed in (embeds or []):
            for img in filter(None, [embed.image, embed.thumbnail]):
                url = img.proxy_url or img.url
                if not url or url in urls_seen:
                    continue
                urls_seen.add(url)
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        ct = (resp.headers.get("content-type", "")).split(";")[0].strip()
                        if ct not in SUPPORTED_IMAGE_TYPES:
                            continue
                        data = await resp.read()
                    if len(data) > MAX_IMAGE_BYTES:
                        data, ct = await asyncio.to_thread(_compress_image, data, ct)
                    b64 = base64.standard_b64encode(data).decode()
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}})
                    log.info(f"Embed-Bild geladen: {url}")
                except Exception as e:
                    log.warning(f"Embed-Bild laden fehlgeschlagen ({url}): {e}")

        # 3. Direct image URLs in message text (e.g. https://example.com/pic.gif)
        for url in IMAGE_URL_RE.findall(content):
            if url in urls_seen:
                continue
            urls_seen.add(url)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    ct = (resp.headers.get("content-type", "")).split(";")[0].strip()
                    if ct not in SUPPORTED_IMAGE_TYPES:
                        continue
                    data = await resp.read()
                if len(data) > MAX_IMAGE_BYTES:
                    data, ct = await asyncio.to_thread(_compress_image, data, ct)
                b64 = base64.standard_b64encode(data).decode()
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}})
                log.info(f"URL-Bild geladen: {url}")
            except Exception as e:
                log.warning(f"URL-Bild laden fehlgeschlagen ({url}): {e}")

    return blocks

# ── Claude ───────────────────────────────────────────────────────────────────

TOOLS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

async def _claude_loop(system: str, messages: list, max_tokens: int = 1024, model: str = None) -> str:
    _model = model or CLAUDE_MODEL
    # Cache the system prompt (tools render before system, so this breakpoint covers both).
    # The system prompt is stable across all turns on the same channel → consistent cache hits.
    cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    while True:
        response = await asyncio.to_thread(
            anthropic.messages.create,
            model=_model, max_tokens=max_tokens,
            system=cached_system, tools=TOOLS, messages=messages,
        )
        u = response.usage
        log.debug(
            f"Cache: write={u.cache_creation_input_tokens} "
            f"read={u.cache_read_input_tokens} "
            f"uncached={u.input_tokens}"
        )
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "content": ""}
            for b in response.content if b.type == "tool_use"
        ]})
    return "".join(b.text for b in response.content if hasattr(b, "text")).strip()

def resolve_mentions(content: str, mentions: list) -> str:
    """Replace raw <@id> / <@!id> Discord mention syntax with display names."""
    for member in mentions:
        content = content.replace(f"<@{member.id}>",  f"@{member.display_name}")
        content = content.replace(f"<@!{member.id}>", f"@{member.display_name}")
    return content

def _msg_ts(msg_time: datetime) -> str:
    """Format a message timestamp: [HH:MM] for today, [DD.MM HH:MM] for older."""
    local = msg_time.astimezone(TZ)
    today = datetime.now(TZ).date()
    if local.date() == today:
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


async def fetch_context(channel_id: int, before_id: int = None) -> list[dict]:
    """Fetch recent channel messages as structured Claude conversation context."""
    channel = bot.get_channel(channel_id)
    if not channel:
        return []
    kwargs = {"limit": CONTEXT_WINDOW, "oldest_first": True}
    if before_id is not None:
        kwargs["before"] = discord.Object(id=before_id)
    messages = []
    async for msg in channel.history(**kwargs):
        ts = _msg_ts(msg.created_at)
        if msg.author == bot.user:
            messages.append({"role": "assistant", "content": f"[{ts}] {msg.content or ''}"})
        else:
            content = resolve_mentions(msg.content or "", msg.mentions)
            if len(content) > 300:
                content = content[:300] + "…"
            if msg.attachments:
                content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
            messages.append({"role": "user", "content": f"[{ts}] {msg.author.display_name}: {content}"})
    return messages

async def ask_claude(user_message: str, username: str, image_blocks: list = None, channel_id: int = None, before_id: int = None, memory_context: str = None) -> str:
    messages = await fetch_context(channel_id, before_id=before_id) if channel_id else []
    # Build full conversation text for memory matching — user identification needs to
    # look across the whole recent history, not just the current message.
    hist_text = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
    full_memory_ctx = f"{hist_text} {memory_context or user_message}".strip()
    # Cache the historical context prefix. The last fetched message marks the boundary —
    # everything before it is the same on the next turn, so the API can serve it from cache.
    if messages:
        last = messages[-1]
        hist_content = last["content"]
        if isinstance(hist_content, str):
            messages[-1] = {**last, "content": [{"type": "text", "text": hist_content, "cache_control": {"type": "ephemeral"}}]}
    now_ts = datetime.now(TZ).strftime("%H:%M")
    content: list = [{"type": "text", "text": f"[{now_ts}] {username}: {user_message}"}]
    if image_blocks:
        content.extend(image_blocks)
    messages.append({"role": "user", "content": content})
    reply = await _claude_loop(
        build_system_prompt(channel_id, full_context=full_memory_ctx, message_context=memory_context or user_message, track_usage=_is_main(channel_id)),
        messages, model=_model(channel_id)
    )
    return reply

async def should_respond(user_message: str, username: str, recent_context: str, channel_id: int = None, image_blocks: list = None) -> tuple[bool, str]:
    system = (
        build_system_prompt(channel_id, full_context=f"{recent_context}\n{user_message}", message_context=user_message) + "\n\n"
        "Du liest Nachrichten in einem Discord-Kanal. Antworte NUR wenn du echten Mehrwert liefern kannst. "
        "Sonst antworte mit exakt: SKIP"
    )
    text = f"Aktuelle Nachrichten:\n{recent_context}\n\nNeueste von {username}: {user_message}"
    if image_blocks:
        user_content = [{"type": "text", "text": text}] + image_blocks
    else:
        user_content = text
    reply = await _claude_loop(system, [{"role": "user", "content": user_content}],
        model=_model(channel_id))
    # Strip any stray trailing SKIP Claude might append to an otherwise real reply
    reply = re.sub(r'\s*\bSKIP\b\s*$', '', reply, flags=re.IGNORECASE).strip()
    if not reply or reply.upper().startswith("SKIP"):
        return False, ""
    return True, reply

_YT_URL_RE = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})')
_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_PRIVATE_HOST_RE = re.compile(
    r'^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|0\.0\.0\.0)',
    re.IGNORECASE,
)

# IAB TCF v2 "accept all" consent cookie — accepted by most German CMP implementations
# (Usercentrics, Consentmanager, OneTrust) when sent in the initial request
_CONSENT_COOKIE = (
    "euconsent-v2=CPwsGQAPwsGQAAHABBENDMCsAP_AAH_AAAqIHutf_X__b39n-_59__t0eY1f9_7_v-0zjhfdt-8N2f_X_L8X42M7vF36tq4KuR4Eu3bBIQdlHOHcTUmw6okVrTPsak2Mr7NKJ7LkmlMbM25UIdAImZhskqKAAAAA; "
    "CONSENT=YES+cb; cookieconsent_status=allow; cookie_consent=1"
)
_FETCH_HEADERS_PLAIN = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
_FETCH_HEADERS_CONSENT = {**_FETCH_HEADERS_PLAIN, "Cookie": _CONSENT_COOKIE}


async def fetch_webpage_text(url: str) -> str | None:
    """Fetch a web page and return its extracted main text, or None on failure.

    Attempts the request twice: once without cookies, and if the content looks
    like a consent wall (too short), again with IAB TCF v2 consent cookies.
    Uses trafilatura for extraction — handles encoding and boilerplate removal.
    """
    if _YT_URL_RE.search(url) or IMAGE_URL_RE.search(url):
        return None  # Already handled elsewhere
    # SSRF guard — reject private/loopback hosts before and after DNS resolution
    try:
        hostname = urlparse(url).hostname or ""
        if _PRIVATE_HOST_RE.match(hostname):
            log.warning(f"URL-Fetch blockiert (privater Host): {url}")
            return None
        ip = await asyncio.to_thread(socket.gethostbyname, hostname)
        if _PRIVATE_HOST_RE.match(ip):
            log.warning(f"URL-Fetch blockiert (private IP {ip}): {url}")
            return None
    except Exception:
        return None

    async def _fetch_raw(headers: dict) -> bytes | None:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=10),
            max_redirects=5,
            headers=headers,
        ) as resp:
            ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if "text/html" not in ct:
                log.info(f"URL-Fetch übersprungen (kein HTML, {ct}): {url}")
                return None
            return await resp.content.read(MAX_FETCH_BYTES)

    def _extract(raw: bytes) -> str | None:
        text = trafilatura.extract(
            raw,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if not text:
            return None
        if len(text) > MAX_WEBPAGE_CHARS:
            text = text[:MAX_WEBPAGE_CHARS] + " […]"
        return text

    try:
        async with aiohttp.ClientSession() as session:
            raw = await _fetch_raw(_FETCH_HEADERS_PLAIN)
            if raw is None:
                return None
            text = await asyncio.to_thread(_extract, raw)
            # Short/empty result likely means a consent wall — retry with cookies
            if not text or len(text) < 300:
                log.info(f"URL-Fetch: Inhalt zu kurz ({len(text or '')}\u00a0Zeichen), Wiederholung mit Consent-Cookies: {url}")
                raw2 = await _fetch_raw(_FETCH_HEADERS_CONSENT)
                if raw2:
                    text = await asyncio.to_thread(_extract, raw2)
        if text:
            log.info(f"URL-Fetch: {len(text)} Zeichen extrahiert aus {url}")
        else:
            log.info(f"URL-Fetch: kein Artikeltext extrahiert aus {url}")
        return text
    except Exception as e:
        log.warning(f"URL-Fetch fehlgeschlagen ({url}): {e}")
        return None


_CLASSIFY_PREAMBLE = "Klassifiziere die Absicht. Antworte NUR im angegebenen Format:\n\n"

_CLASSIFY_FOOTER = (
    "RESPOND – alles andere\n\n"
)

async def classify_intent(text: str) -> tuple[str, str]:
    plugin_lines = "".join(plugin_registry.intent_lines())
    footer = _CLASSIFY_FOOTER + f"Aktuelle lokale Zeit ({TIMEZONE}): {datetime.now(TZ).strftime('%A %d.%m.%Y %H:%M')}"
    system = _CLASSIFY_PREAMBLE + plugin_lines + footer

    response = await asyncio.to_thread(
        anthropic.messages.create,
        model=CHEAP_MODEL, max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()

    for prefix, intent in plugin_registry.intent_prefixes():
        if raw.upper().startswith(prefix.upper()):
            extra = raw[len(prefix):].strip() if ":" in prefix else ""
            return intent, extra

    return "RESPOND", ""

async def get_emoji_reaction(message_text: str) -> str | None:
    if random.random() > EMOJI_REACTION_RATE:
        return None
    try:
        response = await asyncio.to_thread(
            anthropic.messages.create,
            model=CHEAP_MODEL, max_tokens=5,
            system="Antworte mit einem einzigen passenden Emoji, oder SKIP wenn keins passt.",
            messages=[{"role": "user", "content": message_text}],
        )
        result = response.content[0].text.strip()
        return None if result.upper() == "SKIP" else result
    except Exception:
        return None

# ── Background tasks ──────────────────────────────────────────────────────────

@tasks.loop(minutes=30)
async def rotate_status():
    global status_index
    await bot.change_presence(activity=discord.CustomActivity(name=STATUSES[status_index % len(STATUSES)]))
    status_index += 1


@tasks.loop(time=dt_time(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, tzinfo=TZ))
async def daily_digest():
    if not DIGEST_ENABLED:
        return

    since = datetime.now(timezone.utc) - timedelta(hours=24)

    for channel_id in MAIN_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        # Fetch last 24h of non-bot messages
        lines = []
        async for msg in channel.history(after=since, limit=500, oldest_first=True):
            if msg.author == bot.user:
                continue
            content = msg.content
            if msg.attachments:
                content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
            lines.append(f"{msg.author.display_name}: {content}")

        if len(lines) < 5:
            log.info(f"Digest #{channel_id}: zu wenig Nachrichten ({len(lines)}), übersprungen")
            continue

        context = "\n".join(lines)
        log.info(f"Digest #{channel_id}: analysiere {len(lines)} Nachrichten")

        summary = await _claude_loop(
            build_system_prompt() + (
                "\n\nDu schaust dir den heutigen Chatverlauf an und entscheidest ob etwas "
                "Erwähnenswertes passiert ist – interessante Diskussionen, wichtige Infos, "
                "lustige Momente oder relevante Themen. "
                "Wenn ja: fasse es kurz in deinem typischen Stil zusammen, ohne Bullet-Points, "
                "so wie du es einem Freund erzählen würdest. Kein 'Heute wurde...' – einfach drauf los. "
                "Wenn es wirklich nur bedeutungsloser Smalltalk war: antworte mit exakt: SKIP"
            ),
            [{"role": "user", "content": f"Heutiger Chatverlauf:\n{context}"}],
            max_tokens=600, model=_model(channel_id),
        )

        if summary.upper().startswith("SKIP"):
            log.info(f"Digest #{channel_id}: nichts Erwähnenswertes, kein Post")
            continue

        await channel.send(f"**Tagesrückblick** 🌙\n{summary}")
        log.info(f"Digest #{channel_id}: gepostet")

        # Extract structured atomic facts from the same chat log and store them
        label    = f"Tagesrückblick #{channel.name}"
        fact_resp = await asyncio.to_thread(
            anthropic.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=(
                f"Du analysierst einen Discord-Chatverlauf und extrahierst strukturierte Gedächtniseinträge für den Bot {BOT_NAME}.\n\n"
                "Ausgabeformat — eine Zeile pro atomarer Tatsache, KEIN Fließtext:\n"
                "BOT | <Fakt über den Bot selbst> | <Trigger/Kontext oder NONE>\n"
                "USER | <Anzeigename wie im Chat> | <echte Namen und Spitznamen kommagetrennt oder NONE> | <Fakt>\n"
                "GENERAL | <allgemeiner Fakt ohne klaren Nutzer- oder Bot-Bezug>\n\n"
                "Regeln:\n"
                "- Jede Zeile = genau eine Aussage. Keine Interpretationen, nur gesicherte Fakten.\n"
                "- BOT: Spitznamen, Rollen, Besitztümer, Verhaltensregeln, Dynamiken.\n"
                "- BOT-Trigger: Kontext in dem ein Fakt gilt (z.B. 'wenn BonusPizza schreibt'), sonst NONE.\n"
                "- USER: Anzeigename exakt wie im Chat. Aliases = alle anderen bekannten Namen.\n"
                "- Nur neue oder geänderte Fakten — keine bereits bekannten Dauerfakten wiederholen.\n"
                "- Kein Metakommentar, keine Leerzeilen, kein Markdown."
            ),
            messages=[{"role": "user", "content": "Chatverlauf:\n" + context}],
        )
        parsed = _parse_snapshot_facts(fact_resp.content[0].text.strip())
        for fact_data in parsed:
            add_memory(
                fact        = fact_data["content"],
                added_by    = label,
                user_id     = bot.user.id,
                memory_type = fact_data["type"],
                subject     = fact_data.get("subject"),
                aliases     = fact_data.get("aliases"),
                trigger     = fact_data.get("trigger"),
            )
        log.info(f"Digest #{channel_id}: {len(parsed)} Fakten als Memory gespeichert")

# ── Late bot_state wiring (functions defined after bot = ...) ─────────────────
bot_state.claude_loop        = _claude_loop
bot_state.build_system_prompt = build_system_prompt
bot_state.get_model          = _model

# ── Discord ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="Zeigt was Marvin alles kann")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(_build_help_text(), ephemeral=True)

@bot.event
async def on_ready():
    await plugin_registry.on_ready()
    rotate_status.start()
    if DIGEST_ENABLED:
        daily_digest.start()
    await bot.tree.sync()
    log.info(f"Eingeloggt als {bot.user} (ID {bot.user.id})")
    if MAIN_CHANNEL_IDS:
        log.info(f"Hauptkanäle: {', '.join(f'#{cid}' for cid in MAIN_CHANNEL_IDS)} | Cooldown: {COOLDOWN_SECONDS}s")
    else:
        log.info("Keine Hauptkanäle konfiguriert – antworte nur auf @Mentions")
    log.info(f"Hauptkanal-Modell: {MAIN_MODEL} | Anderer-Kanal-Modell: {CLAUDE_MODEL}")
    log.info(f"Memories: {len(load_memories())} | Quotes: {len(load_quotes())}")

async def _try_respond(channel_id: int, trigger_msg: discord.Message = None):
    """Evaluate whether to respond in a main channel.

    trigger_msg is the message that caused this evaluation. It is used as
    last_msg on the first iteration because Discord's history API may not yet
    include a message that was received moments ago. On subsequent iterations
    (pending re-evaluations) trigger_msg is None and history is used directly —
    by then all pending messages are guaranteed to be indexed.

    _channel_processing[channel_id] is already True when this task starts
    (set by the on_message caller). The finally block always clears it and,
    if new messages arrived while we were busy, immediately starts a fresh task
    so those messages are never silently dropped.
    """
    try:
        if bot_state.muted:
            log.info(f"Kanal #{channel_id}: stumm, ignoriere Nachricht")
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            log.warning(f"Kanal #{channel_id}: channel-Objekt nicht gefunden")
            return

        while True:
            # Cooldown check on every iteration so a second loop pass (triggered by
            # _channel_pending) can't bypass the cooldown set by the first response.
            cooldown_remaining = COOLDOWN_SECONDS - (asyncio.get_event_loop().time() - _last_response.get(channel_id, 0.0))
            question_bypass = _bot_asked_question.pop(channel_id, False)
            if cooldown_remaining > 0 and not question_bypass:
                log.info(f"Kanal #{channel_id}: Nachricht gesehen, Cooldown noch {cooldown_remaining:.0f}s")
                return

            # Snapshot and consume the trigger message for this iteration only.
            # Subsequent iterations (pending) will rely purely on history.
            current_trigger = trigger_msg
            trigger_msg = None

            # Reset pending flag BEFORE the API call so any message arriving
            # during generation will set it to True and trigger a re-evaluation.
            _channel_pending[channel_id] = False

            last_msg     = None
            recent_lines = []
            async for msg in channel.history(limit=10, oldest_first=True):
                name = bot.user.display_name if msg.author == bot.user else msg.author.display_name
                ts   = _msg_ts(msg.created_at)
                recent_lines.append(f"[{ts}] {name}: {msg.content}")
                last_msg = msg

            if current_trigger is not None:
                # Use the known triggering message regardless of what history returned.
                # Discord may not have indexed it yet (race between gateway event and REST).
                last_msg      = current_trigger
                trigger_name  = current_trigger.author.display_name
                trigger_ts    = _msg_ts(current_trigger.created_at)
                trigger_line  = f"[{trigger_ts}] {trigger_name}: {current_trigger.content or ''}"
                # Append to context if history didn't include it yet
                if not recent_lines or recent_lines[-1] != trigger_line:
                    recent_lines.append(trigger_line)
            else:
                if not last_msg:
                    log.info(f"Kanal #{channel_id}: keine Nachrichten in History – überspringe")
                    return
                if last_msg.author.bot:
                    log.info(
                        f"Kanal #{channel_id}: letzte Nachricht ist vom Bot – überspringe "
                        f"(@{last_msg.author.display_name}: '{last_msg.content[:80]}', "
                        f"{last_msg.created_at.strftime('%H:%M:%S')})"
                    )
                    return

            log.info(f"Kanal #{channel_id}: evaluiere Antwort auf '{last_msg.content[:60]}' von {last_msg.author.display_name}")
            recent_context = "\n".join(recent_lines)
            image_blocks = await fetch_images(last_msg.attachments, list(last_msg.embeds), last_msg.content or "")
            if question_bypass:
                # Bot asked a question — treat any reply as a direct answer, skip SKIP-evaluation
                log.info(f"Kanal #{channel_id}: direkte Antwort auf Bot-Frage – überspringe Evaluierung")
                reply = await ask_claude(
                    last_msg.content, last_msg.author.display_name,
                    image_blocks=image_blocks or None,
                    channel_id=channel_id, before_id=last_msg.id,
                )
                respond = bool(reply)
            else:
                respond, reply = await should_respond(
                    last_msg.content, last_msg.author.display_name, recent_context,
                    channel_id=channel_id, image_blocks=image_blocks or None,
                )
            log.info(f"Kanal #{channel_id}: Evaluierung → {'RESPOND: ' + reply[:80] if respond else 'SKIP'}")

            # New message(s) arrived while we were generating — re-read and try again
            if _channel_pending.get(channel_id):
                log.info(f"Kanal #{channel_id}: neue Nachrichten während Evaluierung – wiederhole mit aktuellem Kontext")
                continue

            if respond:
                log.info(f"Kanal #{channel_id}: antworte")
                _last_response[channel_id] = asyncio.get_event_loop().time()
                _bot_asked_question[channel_id] = reply.rstrip().endswith("?")
                async with channel.typing():
                    await asyncio.sleep(0.3)
                await channel.send(reply)
            else:
                log.info(f"Kanal #{channel_id}: SKIP")
                emoji = await get_emoji_reaction(last_msg.content)
                if emoji:
                    try:
                        await last_msg.add_reaction(emoji)
                    except discord.HTTPException:
                        pass
            return
    except asyncio.CancelledError:
        log.info(f"Kanal #{channel_id}: Task abgebrochen")
        raise
    except Exception:
        log.exception(f"Kanal #{channel_id}: unerwarteter Fehler in _try_respond")
    finally:
        _channel_processing[channel_id] = False
        # If messages arrived while we were busy (including during an early cooldown
        # exit), start a new task for them instead of silently dropping them.
        if _channel_pending.pop(channel_id, False):
            _channel_processing[channel_id] = True
            pending_msg = _channel_pending_msg.pop(channel_id, None)
            task = asyncio.create_task(_try_respond(channel_id, pending_msg))
            _active_tasks.add(task)
            task.add_done_callback(_active_tasks.discard)
        else:
            _channel_pending_msg.pop(channel_id, None)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    is_mention = bot.user in message.mentions
    is_main    = message.channel.id in MAIN_CHANNEL_IDS
    log.info(f"on_message: #{message.channel.id} von {message.author} | mention={is_mention} main={is_main} muted={bot_state.muted}")

    # Reaktivieren
    if bot_state.muted:
        if not is_mention:
            return
        bot_state.muted = False
        await bot.change_presence(activity=discord.CustomActivity(name=STATUSES[status_index % len(STATUSES)]))
        await message.channel.send("Bin wieder da.")
        # fall through — process the message normally so the wakeup message is also answered

    if is_mention:
        # Discord adds link-preview embeds asynchronously; wait briefly then re-fetch
        if not message.attachments and re.search(r'https?://', message.content):
            await asyncio.sleep(1.5)
            try:
                message = await message.channel.fetch_message(message.id)
            except Exception:
                pass

        image_blocks = await fetch_images(message.attachments, message.embeds, message.content)
        has_images   = bool(image_blocks)
        privileged   = is_privileged(message.author) if isinstance(message.author, discord.Member) else False

        clean = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
        # Resolve other @mentions to display names so Claude can match them to memory
        clean = resolve_mentions(clean, [m for m in message.mentions if m != bot.user]).strip()
        if not clean and has_images:
            clean = "Was siehst du auf diesem Bild?"
        elif not clean:
            return

        classify_text = clean
        if message.reference and message.reference.resolved:
            ref_content = (message.reference.resolved.content or "").strip()
            # Only append referenced message if it contains a URL — needed so the
            # classifier can detect YOUTUBE_SUMMARY when the link is in the replied-to
            # message. Appending unconditionally caused false QUOTE_SAVE hits.
            if ref_content and _URL_RE.search(ref_content):
                classify_text = f"{clean}\n[Benutzer antwortet auf: {ref_content[:300]}]"

        _pre = plugin_registry.pre_classify(classify_text)
        if _pre:
            intent, extra = _pre
            log.info(f"Intent von {message.author} ({'priv' if privileged else 'user'}) [pre]: {intent} | '{clean[:60]}'")
        else:
            intent, extra = await classify_intent(classify_text)
            log.info(f"Intent von {message.author} ({'priv' if privileged else 'user'}): {intent} | '{clean[:60]}'")

        # ── Plugin dispatch ───────────────────────────────────────────────────
        if plugin_registry.handles(intent):
            from plugins.base import MessageContext
            await plugin_registry.dispatch(MessageContext(
                message=message, intent=intent, extra=extra,
                privileged=privileged, classify_text=classify_text,
                ask_claude=_claude_loop,
                system_prompt=build_system_prompt(message.channel.id),
                model=_model(message.channel.id),
                add_memory_fn=add_memory,
                resolve_mentions_fn=resolve_mentions,
                list_memories_fn=list_memories,
                delete_memories_fn=delete_memories,
                image_blocks=image_blocks,
                clean=clean,
                ask_full_fn=ask_claude,
                fetch_webpage_fn=fetch_webpage_text,
            ))
            return

    # No @mention — only main channels get passive responses
    if not is_main:
        return

    cid = message.channel.id
    if _channel_processing.get(cid):
        # Generation already running — flag that new messages arrived so it re-evaluates
        log.info(f"Kanal #{cid}: Nachricht von {message.author.display_name} während Evaluierung – als pending markiert")
        _channel_pending[cid] = True
        _channel_pending_msg[cid] = message
    else:
        log.info(f"Kanal #{cid}: Nachricht von {message.author.display_name} – starte Evaluierung")
        _channel_processing[cid] = True
        task = asyncio.create_task(_try_respond(cid, message))
        _active_tasks.add(task)
        task.add_done_callback(_active_tasks.discard)

# ── Startup ───────────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()
    def shutdown():
        log.info("Shutdown-Signal – trenne Verbindung...")
        asyncio.create_task(bot.close())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    async with bot:
        await bot.start(DISCORD_TOKEN)

asyncio.run(main())
