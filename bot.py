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
from collections import OrderedDict
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
import providers
from plugins.registry import registry as plugin_registry, discover as _discover_plugins
from plugins.core.help import build_help_text as _build_help_text
from plugins.core.help import capabilities_block as _capabilities_block
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
RECENT_WINDOW       = int(os.environ.get("RECENT_WINDOW", "8"))   # last N messages kept at full length; older ones truncated hard
MAX_CONTEXT_IMAGES  = int(os.environ.get("MAX_CONTEXT_IMAGES", "4"))  # cap on images embedded into history context (most recent kept)
MAIN_SYSTEM_PROMPT  = os.environ.get("MAIN_SYSTEM_PROMPT") or SYSTEM_PROMPT

# ── Model slots ───────────────────────────────────────────────────────────────
# Non-Anthropic backends (Ollama / Gemini / DeepSeek) live in providers.py,
# including their env config and capability profiles.
CHEAP_MODEL         = os.environ.get("CHEAP_MODEL", "claude-haiku-4-5-20251001")
NORMAL_MODEL        = os.environ.get("NORMAL_MODEL", "claude-sonnet-4-6")
EXPENSIVE_MODEL     = os.environ.get("EXPENSIVE_MODEL", "claude-sonnet-4-6")
LOCAL_MODEL         = providers.LOCAL_MODEL

# ── Tier assignments ──────────────────────────────────────────────────────────
def _tier_env(name: str, default: str) -> str:
    return os.environ.get(name, default).split("#")[0].strip()

MAIN_TIER           = _tier_env("MAIN_TIER",           "expensive")
MENTION_TIER        = _tier_env("MENTION_TIER",        "normal")
# Passive-channel SKIP/RESPOND gate. Previously rode implicitly on MAIN_TIER,
# so every gate decision cost an expensive-tier call.
SHOULD_RESPOND_TIER = _tier_env("SHOULD_RESPOND_TIER", "cheap")
CLASSIFY_TIER       = _tier_env("CLASSIFY_TIER",       "cheap")
EMOJI_TIER          = _tier_env("EMOJI_TIER",          "cheap")
MEMORY_FILTER_TIER  = _tier_env("MEMORY_FILTER_TIER",  "cheap")
REMINDER_TIER       = _tier_env("REMINDER_TIER",       "normal")
PROACTIVE_TIER      = _tier_env("PROACTIVE_TIER",      "expensive")
DIGEST_SUMMARY_TIER = _tier_env("DIGEST_SUMMARY_TIER", "expensive")
DIGEST_FACTS_TIER   = _tier_env("DIGEST_FACTS_TIER",   "normal")

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
DIGEST_MAX_FACTS = int(os.environ.get("DIGEST_MAX_FACTS", "6"))  # hard cap on memories per nightly digest

# Flavor memory cooldown — suppress flavor entries used within this window
FLAVOR_COOLDOWN_HOURS = int(os.environ.get("FLAVOR_COOLDOWN_HOURS", "6"))
# Max flavor memories injected per message (least-recently-used first)
FLAVOR_MAX_PER_MESSAGE = int(os.environ.get("FLAVOR_MAX_PER_MESSAGE", "5"))

# Proactive conversation starter
PROACTIVE_ENABLED         = os.environ.get("PROACTIVE_ENABLED", "true").lower() == "true"
PROACTIVE_HOUR_START      = int(os.environ.get("PROACTIVE_HOUR_START", "15"))
PROACTIVE_HOUR_END        = int(os.environ.get("PROACTIVE_HOUR_END", "23"))
PROACTIVE_SILENCE_MINUTES = int(os.environ.get("PROACTIVE_SILENCE_MINUTES", "45"))
PROACTIVE_COOLDOWN_HOURS  = int(os.environ.get("PROACTIVE_COOLDOWN_HOURS", "4"))
PROACTIVE_CHECK_MINUTES   = int(os.environ.get("PROACTIVE_CHECK_MINUTES", "15"))


DATA_DIR       = Path(os.environ.get("DATA_DIR", "/app/data"))
MEMORY_FILE    = DATA_DIR / "memory.json"

def _load_statuses() -> list[str]:
    """Load status messages from statuses.txt next to bot.py. One per line; # comments allowed."""
    path = Path(__file__).parent / "statuses.txt"
    if not path.exists():
        return ["Bin online"]
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return lines or ["Bin online"]

STATUSES = _load_statuses()

# ── State ────────────────────────────────────────────────────────────────────

_last_response:      dict[int, float]   = {}   # per-channel cooldown tracking
_bot_asked_question: dict[int, bool]    = {}   # True if the bot's last message ended with a question
_channel_processing: dict[int, bool]    = {}   # True while Claude is generating for a channel
_channel_pending:    dict[int, bool]    = {}   # True if new messages arrived during generation
_channel_pending_msg: dict[int, discord.Message] = {}  # latest pending message per channel
_active_tasks:       set[asyncio.Task]  = set()  # strong refs so tasks aren't GC'd before they run
_proactive_last_sent: dict[int, float]  = {}      # per-channel timestamp of last proactive message
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
        log.warning(f"Read failed ({path.name}): {e}")
    return []

def _write(path: Path, data: list):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Write failed ({path.name}): {e}")

# ── Memory ───────────────────────────────────────────────────────────────────

def load_memories() -> list: return _read(MEMORY_FILE)
def save_memories(m: list):  _write(MEMORY_FILE, m)

def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _is_duplicate_memory(fact: str, subject: str | None, memories: list) -> dict | None:
    """Return the existing memory this fact duplicates (Jaccard word overlap
    against same-subject entries), or None."""
    words = _content_words(fact)
    if not words:
        return None
    for m in memories:
        if (m.get("subject") or None) != (subject or None):
            continue
        other = _content_words(m.get("content", ""))
        if not other:
            continue
        jaccard = len(words & other) / len(words | other)
        if jaccard >= 0.6:
            return m
    return None


def add_memory(fact: str, added_by: str, user_id: int,
               memory_type: str = "general",
               subject: str = None,
               aliases: list[str] = None,
               trigger: str = None,
               flavor: bool = False,
               expires: str = None) -> int:
    """Store a memory. Returns 1 if saved, 0 if skipped as a near-duplicate."""
    m = load_memories()
    dup = _is_duplicate_memory(fact, subject, m)
    if dup:
        log.info(f"Memory skipped (duplicate of {dup['id']}): {fact[:80]}")
        return 0
    entry = {
        "id":       str(uuid.uuid4())[:8],
        "type":     memory_type,
        "subject":  subject,
        "aliases":  aliases or [],
        "trigger":  trigger,
        "content":  fact,
        "added_by": added_by,
        "user_id":  user_id,
        "date":     datetime.now().strftime("%d.%m.%Y"),
        "use_count": 0,
        "last_used": None,
    }
    if flavor:
        entry["flavor"] = True
    if expires:
        entry["expires"] = expires
    m.append(entry)
    save_memories(m)
    log.info(f"Memory [{memory_type}] saved by {added_by}: {fact[:80]}")
    return 1

def cleanup_expired_memories() -> int:
    memories = load_memories()
    today = datetime.now(TZ).date()
    def _expired(m: dict) -> bool:
        exp = m.get("expires")
        if not exp:
            return False
        try:
            return datetime.strptime(exp, "%d.%m.%Y").date() < today
        except Exception:
            return False
    kept = [m for m in memories if not _expired(m)]
    removed = len(memories) - len(kept)
    if removed:
        save_memories(kept)
        log.info(f"Memory cleanup: {removed} expired entries removed")
    return removed

from plugins.base import known_identities_block as _known_identities_block_raw  # noqa: E402

def _known_identities_block() -> str:
    return _known_identities_block_raw(load_memories())

# German + English stopwords — too common to be useful for usage detection
_STOPWORDS = {
    "der", "die", "das", "und", "ist", "ein", "eine", "einen", "einem", "einer",
    "mit", "von", "auf", "für", "sich", "nicht", "auch", "als", "aber", "oder",
    "wenn", "dass", "wird", "sind", "hat", "haben", "wurde", "waren", "beim",
    "this", "that", "the", "and", "with", "for", "not", "are", "has", "have",
    "was", "were", "will", "would", "should", "could", "from", "they", "their",
}

def list_memories() -> list:
    return load_memories()

def delete_memories(user_id: int, privileged: bool,
                    specific: str = None, target_user_id: int = None) -> int:
    memories = load_memories()
    before   = len(memories)
    owner_id = target_user_id if (privileged and target_user_id) else user_id
    if specific:
        # Privileged keyword deletes match ANY owner — digest/snapshot entries
        # are stored under the bot's own user_id and would otherwise be
        # undeletable via Discord. Non-privileged users only touch their own.
        def _match(m: dict) -> bool:
            if specific.lower() not in m["content"].lower():
                return False
            if privileged and not target_user_id:
                return True
            return m.get("user_id") == owner_id
        memories = [m for m in memories if not _match(m)]
    else:
        # Bulk delete ("all") stays owner-scoped even for admins — a single
        # keyword-less command must not wipe the entire memory file.
        memories = [m for m in memories if m.get("user_id") != owner_id]
    save_memories(memories)
    return before - len(memories)


def _build_alias_map(memories: list) -> dict[str, set[str]]:
    alias_map: dict[str, set[str]] = {}
    for m in memories:
        if m.get("type") == "user" and m.get("subject"):
            subj = m["subject"]
            alias_map.setdefault(subj, set()).update(m.get("aliases") or [])
    return alias_map


def _format_memory_sections(bot_facts, identity_facts, flavor_facts, general_facts,
                             alias_map: dict) -> str:
    sections = []

    if bot_facts:
        lines = []
        for m in bot_facts:
            line = f"- {m['content']}"
            if m.get("trigger"):
                line += f" [Kontext: {m['trigger']}]"
            lines.append(line)
        sections.append("Fakten über mich (den Bot):\n" + "\n".join(lines))

    if identity_facts:
        by_subject: dict[str, list] = {}
        for m in identity_facts:
            by_subject.setdefault(m.get("subject") or "?", []).append(m)
        lines = []
        for subj, facts in by_subject.items():
            aliases   = sorted(alias_map.get(subj, set()))
            alias_str = f" ({', '.join(aliases)})" if aliases else ""
            facts_str = "; ".join(
                f'{f["content"]} [{f["date"]}]' if f.get("date") else f["content"]
                for f in facts
            )
            lines.append(f"- {subj}{alias_str}: {facts_str}")
        sections.append("Bekannte Nutzer:\n" + "\n".join(lines))

    if flavor_facts:
        by_subject = {}
        for m in flavor_facts:
            by_subject.setdefault(m.get("subject") or "?", []).append(m)
        lines = []
        for subj, facts in by_subject.items():
            facts_str = "; ".join(
                f'{f["content"]} [{f["date"]}]' if f.get("date") else f["content"]
                for f in facts
            )
            lines.append(f"- {subj}: {facts_str}")
        sections.append(
            "Persönliche Details – nur einfließen lassen wenn natürlich, nicht erzwingen:\n"
            + "\n".join(lines)
        )

    if general_facts:
        lines = [
            f'- {m["content"]} [{m["date"]}]' if m.get("date") else f'- {m["content"]}'
            for m in general_facts
        ]
        sections.append("Weiteres Hintergrundwissen:\n" + "\n".join(lines))

    if not sections:
        return ""
    return (
        "Hintergrundwissen – nutze dies als Kontext, "
        "aber aktuelle Chatnachrichten haben Vorrang bei Widersprüchen:\n\n"
        + "\n\n".join(sections)
    )


def _always_on_memory_block() -> str:
    """Sync: bot facts without trigger only. Used for reminders, plugin dispatch, etc."""
    memories = load_memories()
    bot_facts = [m for m in memories if m.get("type") == "bot" and not m.get("trigger")]
    return _format_memory_sections(bot_facts, [], [], [], {})


async def _haiku_memory_filter(message_context: str, speaker: str,
                                candidates: list[dict]) -> set[str]:
    """Ask Haiku which trigger/general memory candidates are relevant to this message."""
    if not candidates:
        return set()
    lines = []
    for m in candidates:
        mid = m.get("id", "?")
        if m.get("trigger"):
            lines.append(f'[trigger] {mid} Bedingung="{m["trigger"]}" — {m["content"][:120]}')
        else:
            lines.append(f'[general] {mid} — {m["content"][:120]}')
    prompt = (
        f'Nachricht von {speaker}: "{message_context}"\n\n'
        "Welche der folgenden Erinnerungen sind für diese Nachricht relevant?\n"
        "Für [trigger]-Einträge: prüfe ob die Nachricht die beschriebene Bedingung erfüllt.\n"
        "Für [general]-Einträge: prüfe ob der Inhalt thematisch zur Nachricht passt.\n\n"
        + "\n".join(lines)
        + "\n\nAntworte nur mit kommaseparierten IDs der relevanten Einträge, oder NONE."
    )
    try:
        text = await _simple_call(
            MEMORY_FILTER_TIER,
            "Du filterst Gedächtniseinträge auf Relevanz. Antworte exakt im geforderten Format.",
            prompt, 100,
        )
        if text.upper() == "NONE":
            return set()
        return {p.strip() for p in text.split(",") if p.strip()}
    except Exception as e:
        log.warning(f"Memory filter failed: {e}")
        return set()


async def build_memory_block(message_context: str, full_context: str = "",
                              current_speaker: str = "",
                              track_usage: bool = False,
                              include_always_on: bool = True) -> str:
    """Full async memory selection with type-aware injection and Haiku evaluation.

    include_always_on=False omits the trigger-less bot facts — used when the
    caller injects this block into the user message while the system prompt
    already carries _always_on_memory_block()."""
    memories = load_memories()
    if not memories:
        return ""

    alias_map = _build_alias_map(memories)
    spk_lower = current_speaker.lower() if current_speaker else ""
    msg_lower = message_context.lower()

    def _is_speaker(subject: str) -> bool:
        return bool(spk_lower and any(
            ident.lower() == spk_lower
            for ident in ({subject} | alias_map.get(subject, set()))
            if len(ident) >= 3
        ))

    def _is_mentioned(subject: str) -> bool:
        return any(
            ident.lower() in msg_lower
            for ident in ({subject} | alias_map.get(subject, set()))
            if len(ident) >= 3
        )

    def _flavor_cooled_down(m: dict) -> bool:
        last = m.get("last_used")
        if not last:
            return True
        try:
            lu = datetime.strptime(last, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
            return (datetime.now(TZ) - lu).total_seconds() > FLAVOR_COOLDOWN_HOURS * 3600
        except Exception:
            return True

    always_bot:         list[dict] = []
    trigger_candidates: list[dict] = []
    identity_facts:     list[dict] = []
    flavor_candidates:  list[dict] = []
    general_candidates: list[dict] = []

    for m in memories:
        mtype = m.get("type", "general")
        if mtype == "bot":
            if m.get("trigger"):
                trigger_candidates.append(m)
            else:
                always_bot.append(m)
        elif mtype == "user":
            subject = m.get("subject")
            if not subject:
                continue
            if m.get("flavor"):
                if _is_speaker(subject) and _flavor_cooled_down(m):
                    flavor_candidates.append(m)
            else:
                if _is_speaker(subject) or _is_mentioned(subject):
                    identity_facts.append(m)
        else:
            general_candidates.append(m)

    # Cap flavor injection per message; least-recently-used first so the
    # selection rotates instead of dumping a user's whole flavor list at once.
    if len(flavor_candidates) > FLAVOR_MAX_PER_MESSAGE:
        def _last_used_key(m: dict):
            try:
                return datetime.strptime(m.get("last_used"), "%d.%m.%Y %H:%M")
            except (TypeError, ValueError):
                return datetime.min
        flavor_candidates = sorted(flavor_candidates, key=_last_used_key)[:FLAVOR_MAX_PER_MESSAGE]

    haiku_candidates = trigger_candidates + general_candidates
    haiku_ids = await _haiku_memory_filter(message_context, current_speaker, haiku_candidates)

    selected_triggers = [m for m in trigger_candidates if m.get("id") in haiku_ids]
    selected_general  = [m for m in general_candidates  if m.get("id") in haiku_ids]
    bot_facts         = (always_bot if include_always_on else []) + selected_triggers

    if track_usage:
        selected = bot_facts + identity_facts + flavor_candidates + selected_general
        if selected:
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

    return _format_memory_sections(bot_facts, identity_facts, flavor_candidates,
                                   selected_general, alias_map)


def _is_main(channel_id: int | None) -> bool:
    return channel_id is not None and channel_id in MAIN_CHANNEL_IDS

def _base_prompt(channel_id: int | None) -> str:
    return MAIN_SYSTEM_PROMPT if _is_main(channel_id) else SYSTEM_PROMPT

def _tier(channel_id: int | None) -> str:
    return MAIN_TIER if _is_main(channel_id) else MENTION_TIER

def _model_for_tier(tier: str) -> str:
    if tier == "local":     return LOCAL_MODEL
    if tier == "cheap":     return CHEAP_MODEL
    if tier == "normal":    return NORMAL_MODEL
    if tier == "expensive": return EXPENSIVE_MODEL
    return NORMAL_MODEL

def build_system_prompt(channel_id: int | None = None, memory_block: str = "") -> str:
    """Sync. Stable across a day (base + always-on bot facts + date) so it caches.

    Per-message memories are NOT passed here anymore — ask_claude() injects them
    into the final user message to keep this prompt byte-identical between calls.
    The memory_block param remains for callers that build one-off prompts."""
    base = _base_prompt(channel_id)
    _now = datetime.now(TZ)
    _weekday = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"][_now.weekday()]
    # Capabilities block is static per process and channel type → cache-safe.
    # It reflects what the ACTIVE model can do (vision / web search), so a
    # Gemini/DeepSeek/Ollama tier doesn't claim abilities it lacks.
    # Date only (no HH:MM): keeps the cached system prompt stable across the day.
    # Current time is still visible to the model via [HH:MM] prefixes on user
    # messages built in fetch_context() and ask_claude().
    caps = providers.caps_for_model(_model_for_tier(_tier(channel_id)))
    base = (base + "\n\n"
            + _capabilities_block(vision=caps.vision, web_search=caps.web_search)
            + f"\n\nAktuelles Datum: {_weekday}, {_now.strftime('%d.%m.%Y')}.")
    if memory_block:
        return memory_block + "\n\n" + base
    if _is_main(channel_id):
        mem = _always_on_memory_block()
        if mem:
            return mem + "\n\n" + base
    return base

# ── Images ───────────────────────────────────────────────────────────────────

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Anthropic API hard limit

# Matches direct image URLs in message text (e.g. https://example.com/foo.gif?v=1)
IMAGE_URL_RE = re.compile(r'https?://\S+\.(?:jpe?g|png|gif|webp)(?:[?#]\S*)?', re.IGNORECASE)

# GIF-host page links (Tenor / Giphy / klipy). Discord renders these as gifv/video
# embeds whose animated media is an mp4, not a plain image URL — so we fall back to
# the page's og:image meta tag (a still frame or the gif itself).
_GIF_HOST_RE = re.compile(
    r'https?://(?:www\.)?(?:[a-z0-9-]+\.)?(?:tenor\.com|giphy\.com|klipy\.com)/\S+',
    re.IGNORECASE,
)
_OG_IMAGE_RES = (
    re.compile(r'<meta[^>]+(?:property|name)=["\']og:image(?::url)?["\'][^>]*\bcontent=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+\bcontent=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image(?::url)?["\']', re.IGNORECASE),
)

_IMAGE_FORMAT_MAP = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}

def _detect_image_ct(data: bytes, fallback: str) -> str:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return _IMAGE_FORMAT_MAP.get(img.format, fallback)
    except Exception:
        return fallback

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
            log.info(f"Image compressed: {len(data)/1024/1024:.1f} MB → {len(result)/1024/1024:.1f} MB")
            return result, content_type
        scale *= 0.7  # shrink dimensions by ~30% each extra pass
    raise ValueError(f"Image could not be compressed below 5 MB ({len(data)/1024/1024:.1f} MB)")

async def _url_to_image_block(session, url: str) -> dict | None:
    """Download an image URL → Anthropic image block, or None if it isn't a usable image."""
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        ct = (resp.headers.get("content-type", "")).split(";")[0].strip()
        if ct not in SUPPORTED_IMAGE_TYPES:
            return None
        data = await resp.read()
    ct = _detect_image_ct(data, ct)
    if len(data) > MAX_IMAGE_BYTES:
        data, ct = await asyncio.to_thread(_compress_image, data, ct)
    b64 = base64.standard_b64encode(data).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}}

async def _og_image_url(session, page_url: str) -> str | None:
    """Fetch a web page and return its og:image URL, or None."""
    try:
        async with session.get(
            page_url, timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0 (compatible; MarvinBot/1.0)"},
        ) as resp:
            if resp.status != 200:
                return None
            html = await resp.text(errors="ignore")
    except Exception as e:
        log.warning(f"Failed to fetch GIF-host page ({page_url}): {e}")
        return None
    for rx in _OG_IMAGE_RES:
        m = rx.search(html)
        if m:
            return m.group(1)
    return None

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
                ct = _detect_image_ct(data, ct)
                if len(data) > MAX_IMAGE_BYTES:
                    data, ct = await asyncio.to_thread(_compress_image, data, ct)
                b64 = base64.standard_b64encode(data).decode()
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}})
                log.info(f"Image loaded: {att.filename}")
            except Exception as e:
                log.warning(f"Failed to load image ({att.filename}): {e}")

        # 2. Discord link-preview embeds. Standard embeds expose visual content in
        #    .image and .thumbnail (we use both); .video is an mp4/webm Anthropic
        #    can't ingest, and author/footer icons aren't shared content. For
        #    GIF-host embeds (Tenor / Giphy / klipy) the media is a video, so when
        #    image/thumbnail yield nothing we fall back to the page's og:image.
        scraped_pages: set[str] = set()
        for embed in (embeds or []):
            got = False
            for img in filter(None, [embed.image, embed.thumbnail]):
                url = img.proxy_url or img.url
                if not url or url in urls_seen:
                    continue
                urls_seen.add(url)
                try:
                    block = await _url_to_image_block(session, url)
                except Exception as e:
                    log.warning(f"Failed to load embed image ({url}): {e}")
                    continue
                if block:
                    blocks.append(block)
                    got = True
                    log.info(f"Embed image loaded: {url}")
            if not got and embed.url and _GIF_HOST_RE.match(embed.url):
                scraped_pages.add(embed.url)
                img_url = await _og_image_url(session, embed.url)
                if img_url and img_url not in urls_seen:
                    urls_seen.add(img_url)
                    try:
                        block = await _url_to_image_block(session, img_url)
                    except Exception as e:
                        log.warning(f"Failed to load GIF-host image ({img_url}): {e}")
                        block = None
                    if block:
                        blocks.append(block)
                        log.info(f"GIF-host image loaded: {embed.url} → {img_url}")

        # 3. Direct image URLs in message text (e.g. https://example.com/pic.gif)
        for url in IMAGE_URL_RE.findall(content):
            if url in urls_seen:
                continue
            urls_seen.add(url)
            try:
                block = await _url_to_image_block(session, url)
            except Exception as e:
                log.warning(f"Failed to load URL image ({url}): {e}")
                continue
            if block:
                blocks.append(block)
                log.info(f"URL image loaded: {url}")

        # 4. GIF-host page links in the text that Discord hasn't (yet) embedded.
        for m in _GIF_HOST_RE.finditer(content):
            page_url = m.group(0)
            if page_url in scraped_pages:
                continue
            scraped_pages.add(page_url)
            img_url = await _og_image_url(session, page_url)
            if not img_url or img_url in urls_seen:
                continue
            urls_seen.add(img_url)
            try:
                block = await _url_to_image_block(session, img_url)
            except Exception as e:
                log.warning(f"Failed to load GIF-host image ({img_url}): {e}")
                continue
            if block:
                blocks.append(block)
                log.info(f"GIF-host image loaded: {page_url} → {img_url}")

    return blocks

# Per-message image cache: message.id → list of image blocks. Message attachments
# are immutable, so once fetched an image never needs re-downloading. Bounded LRU
# so fetch_context can embed history images on every call without re-fetching.
_IMAGE_CACHE: "OrderedDict[int, list[dict]]" = OrderedDict()
_IMAGE_CACHE_MAX = 256

async def _fetch_message_images(msg) -> list[dict]:
    """Fetch (and cache) the image blocks for a single Discord message."""
    cached = _IMAGE_CACHE.get(msg.id)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(msg.id)
        return cached
    blocks = await fetch_images(msg.attachments, list(msg.embeds), msg.content or "")
    _IMAGE_CACHE[msg.id] = blocks
    _IMAGE_CACHE.move_to_end(msg.id)
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    return blocks

# ── Claude ───────────────────────────────────────────────────────────────────

TOOLS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

async def _claude_loop(system: str, messages: list, max_tokens: int = 2048, tier: str = "normal",
                       use_tools: bool = False) -> str:
    """use_tools attaches the web_search tool. Off by default: evaluation,
    digest, summary and transcript calls must not burn paid searches."""
    if tier == "local":
        return await providers.local_call(system, messages, max_tokens)
    model = _model_for_tier(tier)
    if model.startswith("gemini"):
        return await providers.gemini_call(system, messages, max_tokens, model)
    if model.startswith("deepseek"):
        return await providers.deepseek_call(system, messages, max_tokens, model, use_tools=use_tools)
    # Cache the system prompt (tools render before system, so this breakpoint covers both).
    # The system prompt is stable across all turns on the same channel → consistent cache hits.
    # web_search_20250305 is server-side: Anthropic resolves the search server-side and returns
    # a final response, so no client-side tool_result loop is needed.
    cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    kwargs = {"tools": TOOLS} if use_tools else {}
    response = await asyncio.to_thread(
        anthropic.messages.create,
        model=model, max_tokens=max_tokens,
        system=cached_system, messages=messages, **kwargs,
    )
    u = response.usage
    log.info(
        f"Cache [{model}]: write={u.cache_creation_input_tokens} "
        f"read={u.cache_read_input_tokens} "
        f"uncached={u.input_tokens} out={u.output_tokens}"
    )
    return "".join(b.text for b in response.content if hasattr(b, "text")).strip()

async def _simple_call(tier: str, system: str, user_content, max_tokens: int) -> str:
    """Single-turn LLM call without tool use."""
    messages = [{"role": "user", "content": user_content}]
    if tier == "local":
        return await providers.local_call(system, messages, max_tokens)
    model = _model_for_tier(tier)
    if model.startswith("gemini"):
        return await providers.gemini_call(system, messages, max_tokens, model)
    if model.startswith("deepseek"):
        # use_tools=False: classification/emoji/filter calls must not search
        return await providers.deepseek_call(system, messages, max_tokens, model, use_tools=False)
    response = await asyncio.to_thread(
        anthropic.messages.create,
        model=model, max_tokens=max_tokens,
        system=system, messages=messages,
    )
    return response.content[0].text.strip()

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


# Cache-friendly context windows. The prompt cache is a strict prefix match, so
# the rendered history must be append-only between calls: a fixed window start
# (anchor) instead of a sliding "latest N", a truncation boundary frozen at
# re-anchor time instead of one relative to the newest message, and reactions
# only on the recent tail. Re-anchoring costs one deliberate full cache miss,
# then the prefix is stable for the next ~CONTEXT_WINDOW/2 messages.
_ctx_anchor:       dict[int, int] = {}  # channel_id → message id of window start
_ctx_trunc_before: dict[int, int] = {}  # channel_id → ids below this render truncated


async def fetch_context(channel_id: int, before_id: int = None) -> list[dict]:
    """Fetch recent channel messages as structured Claude conversation context."""
    channel = bot.get_channel(channel_id)
    if not channel:
        return []
    # Fetch newest-first (discord.py default with before=), then reverse for chronological order.
    # oldest_first=True with before= fetches from the channel's beginning, not the recent end.
    kwargs = {"limit": CONTEXT_WINDOW}
    if before_id is not None:
        kwargs["before"] = discord.Object(id=before_id)
    raw = []
    async for msg in channel.history(**kwargs):
        raw.append(msg)
    chrono = list(reversed(raw))

    # Anchored window: drop messages older than the channel anchor. Re-anchor to
    # the newest half once the window has grown to the full fetch size.
    anchor = _ctx_anchor.get(channel_id)
    window = [m for m in chrono if anchor is not None and m.id >= anchor]
    if not window or len(window) >= CONTEXT_WINDOW:
        keep = max(CONTEXT_WINDOW // 2, RECENT_WINDOW)
        window = chrono[-keep:]
        if window:
            _ctx_anchor[channel_id] = window[0].id
            tail = max(len(window) - RECENT_WINDOW, 0)
            _ctx_trunc_before[channel_id] = window[tail].id if tail else 0
    chrono = window
    trunc_before = _ctx_trunc_before.get(channel_id, 0)

    # Fetch images for every human message that may carry one — concurrently and
    # cached by message id — so the bot can see all images in its context window,
    # not just the triggering message.
    img_candidates = [
        m for m in chrono
        if m.author != bot.user
        and (m.attachments or m.embeds or IMAGE_URL_RE.search(m.content or ""))
    ]
    img_results = await asyncio.gather(
        *(_fetch_message_images(m) for m in img_candidates),
        return_exceptions=True,
    )
    img_by_id = {
        m.id: (r if isinstance(r, list) else [])
        for m, r in zip(img_candidates, img_results)
    }

    def _rxn_str(reactions):
        parts = [
            f"{str(r.emoji) if isinstance(r.emoji, str) else f':{r.emoji.name}:'}×{r.count}"
            for r in reactions
        ]
        return f" [{' '.join(parts)}]" if parts else ""

    messages = []
    msg_ids  = []   # parallel: discord message id per entry (None for assistant)
    for msg in chrono:
        ts = _msg_ts(msg.created_at)
        # Reaction counts change over time and would rewrite old history bytes,
        # so they are only rendered on the recent tail (volatile zone anyway).
        rxn = _rxn_str(msg.reactions) if msg.id >= trunc_before else ""
        if msg.author == bot.user:
            assistant_content = (msg.content or "") + rxn
            messages.append({"role": "assistant", "content": assistant_content.strip()})
            msg_ids.append(None)
        else:
            content = resolve_mentions(msg.content or "", msg.mentions)
            if msg.attachments:
                content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
            content += rxn
            messages.append({"role": "user", "content": f"[{ts}] {msg.author.display_name}: {content}"})
            msg_ids.append(msg.id)
    # Truncate *user* messages older than the frozen boundary to reduce their
    # influence on the response. Assistant messages are never truncated — the
    # bot must always see what it previously said.
    n_trunc = 0
    for i, m in enumerate(messages):
        if (msg_ids[i] is not None and msg_ids[i] < trunc_before
                and isinstance(m["content"], str) and len(m["content"]) > 80):
            messages[i] = {**m, "content": m["content"][:80] + "…"}
            n_trunc += 1

    # Embed images into their messages, newest-first, up to MAX_CONTEXT_IMAGES total.
    budget = MAX_CONTEXT_IMAGES
    n_img  = 0
    for i in range(len(messages) - 1, -1, -1):
        if budget <= 0:
            break
        blocks = img_by_id.get(msg_ids[i])
        if not blocks:
            continue
        blocks = blocks[:budget]
        budget -= len(blocks)
        n_img  += len(blocks)
        text_content = messages[i]["content"]
        text_blocks = text_content if isinstance(text_content, list) else [{"type": "text", "text": text_content}]
        messages[i] = {**messages[i], "content": [*text_blocks, *blocks]}

    n_user = sum(1 for m in messages if m["role"] == "user")
    n_asst = sum(1 for m in messages if m["role"] == "assistant")
    log.info(f"fetch_context #{channel_id}: {len(messages)} msgs ({n_user} user, {n_asst} assistant, {n_img} images), truncated={n_trunc}, anchor={_ctx_anchor.get(channel_id)}")
    if messages:
        last = messages[-1]
        log.info(f"fetch_context #{channel_id}: last msg role={last['role']} content={str(last['content'])[:80]!r}")
    return messages

async def ask_claude(user_message: str, username: str, image_blocks: list = None, channel_id: int = None, before_id: int = None, memory_context: str = None) -> str:
    messages = await fetch_context(channel_id, before_id=before_id) if channel_id else []
    hist_text = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
    full_ctx  = f"{hist_text} {memory_context or user_message}".strip()
    mem_block = await build_memory_block(
        message_context  = memory_context or user_message,
        full_context     = full_ctx,
        current_speaker  = username,
        track_usage      = _is_main(channel_id),
        include_always_on = False,  # system prompt already carries the always-on bot facts
    ) if _is_main(channel_id) else ""
    # Cache the historical context prefix — everything before the last message is stable.
    if messages:
        last = messages[-1]
        hist_content = last["content"]
        if isinstance(hist_content, str):
            messages[-1] = {**last, "content": [{"type": "text", "text": hist_content, "cache_control": {"type": "ephemeral"}}]}
        elif isinstance(hist_content, list) and hist_content and isinstance(hist_content[-1], dict):
            # Last history message carries images — cache its final block.
            new_content = list(hist_content)
            new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
            messages[-1] = {**last, "content": new_content}
    now_ts = datetime.now(TZ).strftime("%H:%M")
    content: list = [{"type": "text", "text": f"[{now_ts}] {username}: {user_message}"}]
    if image_blocks:
        content.extend(image_blocks)
    if mem_block:
        # Per-message memories go into the FINAL user message, after both cache
        # breakpoints (system prompt + last history message). Injecting them into
        # the system prompt would change its first bytes on every call and
        # invalidate the entire prompt cache.
        content.append({"type": "text", "text":
            f"[Erinnerungen aus deinem Gedächtnis, passend zu dieser Nachricht — nicht Teil der Nachricht selbst:\n{mem_block}]"})
    messages.append({"role": "user", "content": content})
    reply = await _claude_loop(
        build_system_prompt(channel_id),
        messages, tier=_tier(channel_id), use_tools=True,
    )
    return reply

async def should_respond(user_message: str, username: str, recent_context: str, channel_id: int = None, image_blocks: list = None) -> bool:
    """Decide whether to respond at all. Uses flat recent_context — cheap and fast."""
    system = (
        build_system_prompt(channel_id) + "\n\n"
        "Du liest Nachrichten in einem Discord-Kanal. Antworte NUR wenn du echten Mehrwert liefern kannst. "
        "Sonst antworte mit exakt: SKIP"
    )
    text = f"Aktuelle Nachrichten:\n{recent_context}\n\nNeueste von {username}: {user_message}"
    if image_blocks:
        user_content = [{"type": "text", "text": text}] + image_blocks
    else:
        user_content = text
    reply = await _claude_loop(system, [{"role": "user", "content": user_content}],
        tier=SHOULD_RESPOND_TIER)
    reply = reply.strip()
    return bool(reply) and not reply.upper().startswith("SKIP")

from plugins.base import clean_chat_reply as _clean_chat_reply  # noqa: E402
from plugins.base import split_message as _split_message  # noqa: E402


async def _send_chat_reply(channel, reply: str, prefix: str = "") -> None:
    """Clean a conversational reply and send it, splitting into multiple
    messages if it exceeds Discord's 2000-char limit."""
    for chunk in _split_message(prefix + _clean_chat_reply(reply)):
        await channel.send(chunk)

_YT_URL_RE = re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)([A-Za-z0-9_-]{11})')
_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)


def _is_private_address(host: str) -> bool:
    """True if host is an IP literal in a non-public range (v4 or v6)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _url_is_private(url: str) -> bool:
    """SSRF guard: True unless the URL's host resolves exclusively to public
    addresses. Covers IP literals (v4/v6), localhost names, and every DNS
    result — the bot runs on a LAN where internal admin panels are reachable."""
    try:
        hostname = (urlparse(url).hostname or "").strip("[]").lower()
    except ValueError:
        return True
    if not hostname:
        return True
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return True
    if _is_private_address(hostname):
        return True
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, proto=socket.IPPROTO_TCP,
        )
    except OSError:
        return True  # unresolvable — treat as blocked
    return any(_is_private_address(info[4][0]) for info in infos)

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

    Retries up to four times, alternating plain and IAB TCF v2 consent-cookie
    headers: short/empty extractions mean either a CMP consent wall or a CDN
    intermittently serving a skeleton page without the article body (observed
    on tagesschau.de — same URL flips between ~5 KB shell and full page).
    Uses trafilatura for extraction — handles encoding and boilerplate removal.
    """
    if _YT_URL_RE.search(url) or IMAGE_URL_RE.search(url):
        return None  # Already handled elsewhere

    give_up = False  # deterministic failure — retrying won't help

    async def _fetch_raw(headers: dict) -> bytes | None:
        """Follow redirects manually so EVERY hop passes the SSRF guard —
        a public URL redirecting to a LAN address must be blocked too."""
        nonlocal give_up
        from yarl import URL as _URL
        current = url
        for _ in range(5):
            if await _url_is_private(current):
                log.warning(f"URL fetch blocked (private host): {current}")
                give_up = True
                return None
            async with session.get(
                current,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
                headers=headers,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        give_up = True
                        return None
                    current = str(resp.url.join(_URL(loc)))
                    continue
                if resp.status != 200:
                    log.info(f"URL fetch: HTTP {resp.status}: {current}")
                    give_up = resp.status < 500
                    return None
                ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if "text/html" not in ct:
                    log.info(f"URL fetch skipped (not HTML, {ct}): {current}")
                    give_up = True
                    return None
                return await resp.content.read(MAX_FETCH_BYTES)
        log.info(f"URL fetch: too many redirects: {url}")
        give_up = True
        return None

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

    attempts = (_FETCH_HEADERS_PLAIN, _FETCH_HEADERS_CONSENT,
                _FETCH_HEADERS_PLAIN, _FETCH_HEADERS_CONSENT)
    try:
        text = None
        for i, headers in enumerate(attempts):
            if i:
                await asyncio.sleep(2 ** (i - 1))  # 1s, 2s, 4s
            # Fresh session (= fresh TCP connection) per attempt: a reused
            # keep-alive connection can stay pinned to the same CDN backend
            # that keeps serving the skeleton page.
            async with aiohttp.ClientSession() as session:
                raw = await _fetch_raw(headers)
            if give_up:
                return None
            if raw is None:
                continue
            text = await asyncio.to_thread(_extract, raw) or text
            if text and len(text) >= 300:
                break
            log.info(f"URL fetch attempt {i + 1}/{len(attempts)}: content too short ({len(text or '')}\u00a0chars): {url}")
        if text:
            log.info(f"URL fetch: {len(text)} chars extracted from {url}")
        else:
            log.info(f"URL fetch: no article text extracted from {url}")
        return text
    except Exception as e:
        log.warning(f"URL fetch failed ({url}): {e}")
        return None


# providers._ddg_search enriches DeepSeek search results with the top hit's
# page text; injected here to avoid a circular import.
providers.webpage_fetcher = fetch_webpage_text


def _plain_webpage_urls(text: str) -> list[str]:
    """Extract fetchable article URLs — YouTube, direct images and GIF-host
    pages are handled elsewhere (youtube plugin / fetch_images)."""
    return [
        u for u in _URL_RE.findall(text or "")
        if not _YT_URL_RE.search(u)
        and not IMAGE_URL_RE.search(u)
        and not _GIF_HOST_RE.match(u)
    ][:MAX_URLS_PER_MSG]


async def _build_url_context(msg) -> str:
    """Fetch webpage text for URLs in a message (falling back to the
    replied-to message) and return a prompt context block, or "".

    Passive-channel counterpart of the URL fetching in RespondPlugin —
    @mention-less messages never reach the plugin path, and the Anthropic
    web_search tool cannot open a specific URL."""
    urls = _plain_webpage_urls(msg.content)
    if not urls and msg.reference and msg.reference.resolved:
        urls = _plain_webpage_urls(getattr(msg.reference.resolved, "content", "") or "")
    fetched = []
    for u in urls:
        text = await fetch_webpage_text(u)
        if text:
            fetched.append(f"[Inhalt von {u}]:\n{text}")
    return "\n\n" + "\n\n".join(fetched) if fetched else ""


_CLASSIFY_PREAMBLE = "Klassifiziere die Absicht. Antworte NUR im angegebenen Format:\n\n"

_CLASSIFY_FOOTER = (
    "RESPOND – alles andere\n\n"
)

async def classify_intent(text: str) -> tuple[str, str]:
    plugin_lines = "".join(plugin_registry.intent_lines())
    footer = _CLASSIFY_FOOTER + f"Aktuelle lokale Zeit ({TIMEZONE}): {datetime.now(TZ).strftime('%A %d.%m.%Y %H:%M')}"
    system = _CLASSIFY_PREAMBLE + plugin_lines + footer

    raw = await _simple_call(CLASSIFY_TIER, system, text, 200)

    for prefix, intent in plugin_registry.intent_prefixes():
        if raw.upper().startswith(prefix.upper()):
            extra = raw[len(prefix):].strip() if ":" in prefix else ""
            return intent, extra

    return "RESPOND", ""

async def get_emoji_reaction(message_text: str) -> str | None:
    if random.random() > EMOJI_REACTION_RATE:
        return None
    try:
        result = await _simple_call(EMOJI_TIER, "Antworte mit einem einzigen passenden Emoji, oder SKIP wenn keins passt.", message_text, 5)
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
            log.info(f"Digest #{channel_id}: too few messages ({len(lines)}), skipped")
            continue

        context = "\n".join(lines)
        log.info(f"Digest #{channel_id}: analysing {len(lines)} messages")

        summary = await _claude_loop(
            _base_prompt(channel_id) + (
                "\n\nDu schaust dir den heutigen Chatverlauf an und entscheidest ob etwas "
                "Erwähnenswertes passiert ist – interessante Diskussionen, wichtige Infos, "
                "lustige Momente oder relevante Themen. "
                "Wenn ja: fasse es kurz in deinem typischen Stil zusammen, ohne Bullet-Points, "
                "so wie du es einem Freund erzählen würdest. Kein 'Heute wurde...' – einfach drauf los. "
                "Wenn es wirklich nur bedeutungsloser Smalltalk war: antworte mit exakt: SKIP"
            ),
            [{"role": "user", "content": f"Heutiger Chatverlauf:\n{context}"}],
            max_tokens=600, tier=DIGEST_SUMMARY_TIER,
        )

        if summary.upper().startswith("SKIP"):
            log.info(f"Digest #{channel_id}: nothing noteworthy, no post")
            continue

        await _send_chat_reply(channel, summary, prefix="**Tagesrückblick** 🌙\n")
        log.info(f"Digest #{channel_id}: posted")

        # Extract structured atomic facts from the same chat log and store them
        label    = f"Tagesrückblick #{channel.name}"
        today      = datetime.now(TZ)
        week_date  = (today + timedelta(days=7)).strftime("%d.%m.%Y")
        month_date = (today + timedelta(days=30)).strftime("%d.%m.%Y")
        fact_text = await _simple_call(
            DIGEST_FACTS_TIER,
            (
                f"Du analysierst einen Discord-Chatverlauf und extrahierst strukturierte Gedächtniseinträge für den Bot {BOT_NAME}.\n\n"
                "Ausgabeformat — eine Zeile pro atomarer Tatsache, KEIN Fließtext:\n"
                f"BOT | <Fakt über den Bot selbst> | <Trigger oder NONE> | <Ablaufdatum DD.MM.YYYY oder NONE>\n"
                f"USER | <Anzeigename exakt wie im Chat> | <echte Namen/Spitznamen kommagetrennt oder NONE> | <Identitätsfakt>\n"
                f"FLAVOR | <Anzeigename> | <Aliase oder NONE> | <Persönlichkeitsfakt> | <Ablaufdatum DD.MM.YYYY oder NONE>\n"
                f"GENERAL | <Fakt> | <Ablaufdatum DD.MM.YYYY oder NONE>\n\n"
                f"Ablaufdaten (heute = {today.strftime('%d.%m.%Y')}):\n"
                f"- Tagesereignisse, kurzfristige Pläne, aktuelle Stimmung → {week_date}\n"
                f"- Laufende Projekte, aktuelle Situation → {month_date}\n"
                f"- Dauerhafte Eigenschaften, Rollen, Verhaltensregeln → NONE\n\n"
                "Regeln:\n"
                "- Eine Zeile = eine Aussage. Nur gesicherte Fakten, keine Interpretation.\n"
                "- BOT: nur dauerhafte Lore und Persönlichkeit (Titel, Rollen, Besitztümer, Running Gags, "
                "Verhaltensregeln, Dynamiken mit Usern).\n"
                "- USER: Nur für neue Nutzer oder neu entdeckte Aliase. Max. einen USER-Eintrag pro Nutzer.\n"
                "- FLAVOR: Persönlichkeit, Beziehungen, dauerhafte Vorlieben, Running Gags — Dinge, die auch "
                "in einem Monat noch ein Gespräch bereichern würden.\n"
                "- GENERAL: NUR server-eigene Begriffe und Running Gags (z.B. geflügelte Worte des Servers). "
                "KEIN Weltwissen, keine News, keine Studien, keine Produkt- oder Sachinfos — das weiß der Bot selbst.\n"
                "- NIEMALS speichern: technischer Zustand des Bots oder anderer Bots (Bugs, Versionsnummern, "
                "Modell-Backend, Features die gerade kaputt/neu sind), Smalltalk, bloße Gesprächs-Nacherzählungen "
                "(„X und Y haben über Z diskutiert“), Einzelereignisse ohne Wiederholungspotenzial, "
                "aktuelle Zustände/Pläne/Projekte-Status („arbeitet gerade an“, „hat diese Woche“).\n"
                "- Im Zweifel: NICHT speichern oder ein Ablaufdatum setzen. Dauerhaft (NONE) ist die Ausnahme, "
                "nicht die Regel.\n"
                "- Maximal 6 Zeilen pro Tag. Lieber 2 gute als 6 mittelmäßige. Ein Tag ohne speichernswerte "
                "Fakten ist normal — dann gib nichts aus.\n"
                "- Kein Metakommentar, keine Leerzeilen, kein Markdown."
                + _known_identities_block()
            ),
            "Chatverlauf:\n" + context,
            2000,
        )
        parsed = _parse_snapshot_facts(fact_text)
        if len(parsed) > DIGEST_MAX_FACTS:
            log.info(f"Digest #{channel_id}: {len(parsed)} facts extracted, capping at {DIGEST_MAX_FACTS}")
            parsed = parsed[:DIGEST_MAX_FACTS]
        saved = 0
        for fact_data in parsed:
            saved += add_memory(
                fact        = fact_data["content"],
                added_by    = label,
                user_id     = bot.user.id,
                memory_type = fact_data["type"],
                subject     = fact_data.get("subject"),
                aliases     = fact_data.get("aliases"),
                trigger     = fact_data.get("trigger"),
                flavor      = fact_data.get("flavor", False),
                expires     = fact_data.get("expires"),
            )
        log.info(f"Digest #{channel_id}: {saved}/{len(parsed)} facts saved to memory")
        cleanup_expired_memories()


async def _try_proactive(channel_id: int):
    """Attempt to start a conversation in a main channel if conditions are met."""
    now = datetime.now(TZ)

    # Time window
    if not (PROACTIVE_HOUR_START <= now.hour < PROACTIVE_HOUR_END):
        return

    # Per-channel cooldown
    if (now.timestamp() - _proactive_last_sent.get(channel_id, 0.0)) < PROACTIVE_COOLDOWN_HOURS * 3600:
        return

    # Skip if Claude is already generating for this channel
    if _channel_processing.get(channel_id):
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    # Fetch last message — bot must not have been the last sender
    last_msg = None
    async for msg in channel.history(limit=1):
        last_msg = msg
    if not last_msg or last_msg.author == bot.user:
        return

    # Channel must have been silent long enough
    silence_minutes = (now - last_msg.created_at.astimezone(TZ)).total_seconds() / 60
    if silence_minutes < PROACTIVE_SILENCE_MINUTES:
        return

    log.info(f"Proactive #{channel_id}: all checks passed ({silence_minutes:.0f} min silent), querying Claude...")

    # Only use messages from a recent window so we don't address absent users
    cutoff = now - timedelta(hours=PROACTIVE_COOLDOWN_HOURS * 2)
    recent_lines = []
    async for msg in channel.history(limit=CONTEXT_WINDOW, oldest_first=True, after=cutoff):
        ts = _msg_ts(msg.created_at)
        if msg.author == bot.user:
            recent_lines.append(f"[{ts}] {bot.user.display_name}: {msg.content or ''}")
        else:
            content = resolve_mentions(msg.content or "", msg.mentions)
            if len(content) > 300:
                content = content[:300] + "…"
            recent_lines.append(f"[{ts}] {msg.author.display_name}: {content}")
    if not recent_lines:
        log.info(f"Proactive #{channel_id}: no recent messages in window, skipping")
        return
    recent_text = "\n".join(recent_lines)

    system = (
        build_system_prompt(channel_id) + "\n\n"
        "Du liest den letzten Chatverlauf und entscheidest ob es etwas gibt, das es wert wäre jetzt aufzugreifen — "
        "eine offene Frage, ein Thema das abgebrochen wurde, etwas das jemand erwähnt hat und worüber du neugierig bist. "
        "Wenn ja: schreib eine natürliche Nachricht in deinem Stil, als würdest du spontan einhaken. "
        "Kein künstlicher Gesprächseinstieg, kein 'Hey!' — einfach direkt einsteigen. "
        "Wichtig: Bleib neugierig und freundlich. Kein Existentialismus, keine Bedrohlichkeit, keine Paranoia, keine düsteren Monologe. "
        "Nenn keine Nutzer beim Namen und schreib keine @Erwähnungen — du redest in den Kanal, nicht eine Person an. "
        "Wenn du nichts Konkretes und Positives aufgreifen kannst: antworte mit exakt: SKIP"
    )
    reply = await _claude_loop(
        system,
        [{"role": "user", "content": f"Letzter Chatverlauf:\n{recent_text}"}],
        max_tokens=300,
        tier=PROACTIVE_TIER,
    )
    reply = re.sub(r'\s*\bSKIP\b\s*$', '', reply, flags=re.IGNORECASE).strip()
    if not reply or reply.upper().startswith("SKIP"):
        log.info(f"Proactive #{channel_id}: Claude chose SKIP")
        return

    log.info(f"Proactive #{channel_id}: sending '{reply[:80]}'")
    _proactive_last_sent[channel_id] = now.timestamp()
    await _send_chat_reply(channel, reply)


@tasks.loop(minutes=PROACTIVE_CHECK_MINUTES)
async def proactive_check():
    if not PROACTIVE_ENABLED or bot_state.muted:
        return
    for channel_id in MAIN_CHANNEL_IDS:
        try:
            await _try_proactive(channel_id)
        except Exception:
            log.exception(f"Proactive #{channel_id}: unexpected error")


# ── Late bot_state wiring (functions defined after bot = ...) ─────────────────
bot_state.claude_loop        = _claude_loop
bot_state.build_system_prompt = build_system_prompt
bot_state.get_tier           = _tier
bot_state.reminder_tier      = REMINDER_TIER
bot_state.main_channel_ids   = MAIN_CHANNEL_IDS

# ── Discord ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="Zeigt was Marvin alles kann")
async def slash_help(interaction: discord.Interaction):
    from plugins.base import split_message
    chunks = split_message(_build_help_text())
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)

_LAST_VERSION_FILE = DATA_DIR / "last_announced_version.txt"
_announced_startup = False
_on_ready_ran      = False

@bot.event
async def on_ready():
    # discord.py fires on_ready again after gateway resumes. Re-running this
    # handler would duplicate reminder tasks and raise RuntimeError from the
    # already-started task loops.
    global _on_ready_ran
    if _on_ready_ran:
        log.info("on_ready re-fired (session resume) — skipping re-initialisation")
        return
    _on_ready_ran = True

    cleanup_expired_memories()
    await plugin_registry.on_ready()
    rotate_status.start()
    if DIGEST_ENABLED:
        daily_digest.start()
    if PROACTIVE_ENABLED and MAIN_CHANNEL_IDS:
        proactive_check.start()
    await bot.tree.sync()
    log.info(f"Logged in as {bot.user} (ID {bot.user.id})")
    if MAIN_CHANNEL_IDS:
        log.info(f"Main channels: {', '.join(f'#{cid}' for cid in MAIN_CHANNEL_IDS)} | Cooldown: {COOLDOWN_SECONDS}s")
    else:
        log.info("No main channels configured — responding to @mentions only")
    log.info(f"Models — expensive: {EXPENSIVE_MODEL} | normal: {NORMAL_MODEL} | cheap: {CHEAP_MODEL}" + (f" | local: {LOCAL_MODEL}" if LOCAL_MODEL else "") + (" | gemini: enabled" if providers.GEMINI_API_KEY else "") + (" | deepseek: enabled" if providers.DEEPSEEK_API_KEY else ""))
    log.info(f"Tiers — main: {MAIN_TIER} | mention: {MENTION_TIER} | classify: {CLASSIFY_TIER} | emoji: {EMOJI_TIER} | memory: {MEMORY_FILTER_TIER} | proactive: {PROACTIVE_TIER} | digest: {DIGEST_SUMMARY_TIER}/{DIGEST_FACTS_TIER}")
    log.info(f"Memories: {len(load_memories())}")

    global _announced_startup
    if not _announced_startup and MAIN_CHANNEL_IDS:
        _announced_startup = True
        prev_version = None
        try:
            if _LAST_VERSION_FILE.exists():
                prev_version = _LAST_VERSION_FILE.read_text(encoding="utf-8").strip() or None
        except Exception as e:
            log.warning(f"Could not read last_announced_version: {e}")

        if prev_version != BOT_VERSION:
            msg = f"Ich bin zurück, gab ein Update und bin jetzt auf Version {BOT_VERSION}."
        else:
            msg = f"Ich bin zurück (Version {BOT_VERSION})."

        try:
            _LAST_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _LAST_VERSION_FILE.write_text(BOT_VERSION, encoding="utf-8")
        except Exception as e:
            log.warning(f"Could not write last_announced_version: {e}")

        for cid in MAIN_CHANNEL_IDS:
            ch = bot.get_channel(cid)
            if ch:
                try:
                    await ch.send(msg)
                except Exception as e:
                    log.warning(f"Failed to send startup announcement to #{cid}: {e}")

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
            log.info(f"Channel #{channel_id}: muted, ignoring message")
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            log.warning(f"Channel #{channel_id}: channel object not found")
            return

        original_trigger = trigger_msg  # preserved across retries (None on retry)
        first_iteration  = True

        while True:
            # Cooldown check on every iteration so a second loop pass (triggered by
            # _channel_pending) can't bypass the cooldown set by the first response.
            cooldown_remaining = COOLDOWN_SECONDS - (asyncio.get_event_loop().time() - _last_response.get(channel_id, 0.0))
            question_bypass = _bot_asked_question.pop(channel_id, False)
            if cooldown_remaining > 0 and not question_bypass:
                log.info(f"Channel #{channel_id}: message seen, cooldown remaining {cooldown_remaining:.0f}s")
                return

            # Snapshot and consume the trigger message for this iteration only.
            # On a retry iteration (trigger_msg is None), peek at any stored
            # pending message — Discord REST may not have indexed it in history yet.
            current_trigger = trigger_msg if trigger_msg is not None else _channel_pending_msg.get(channel_id)
            trigger_msg = None

            # Reset pending flag BEFORE the API call so any message arriving
            # during generation will set it to True and trigger a re-evaluation.
            _channel_pending[channel_id] = False

            # Newest-first fetch, then reverse for chronological order.
            # oldest_first=True without after= would fetch from the channel's
            # very first message (see the same pitfall note in fetch_context).
            raw = [msg async for msg in channel.history(limit=10)]
            all_msgs     = []
            recent_lines = []
            for msg in reversed(raw):
                name = bot.user.display_name if msg.author == bot.user else msg.author.display_name
                ts   = _msg_ts(msg.created_at)
                recent_lines.append(f"[{ts}] {name}: {msg.content}")
                all_msgs.append(msg)
            last_msg = all_msgs[-1] if all_msgs else None

            if current_trigger is not None:
                # On retry, if the pending message doesn't address the bot but the
                # original trigger did, keep the original trigger so we don't lose
                # a direct @mention just because someone else posted in between.
                trigger_addr_bot = (
                    bot.user in current_trigger.mentions
                    or BOT_NAME.lower() in (current_trigger.content or "").lower()
                )
                if not first_iteration and not trigger_addr_bot and original_trigger is not None:
                    log.info(f"Channel #{channel_id}: pending msg from {current_trigger.author.display_name} not addressed to bot — keeping original trigger")
                    # Don't replace last_msg; just ensure the new message is in context
                    pending_name = current_trigger.author.display_name
                    pending_ts   = _msg_ts(current_trigger.created_at)
                    pending_line  = f"[{pending_ts}] {pending_name}: {current_trigger.content or ''}"
                    if not recent_lines or recent_lines[-1] != pending_line:
                        recent_lines.append(pending_line)
                    # original_trigger stays as last_msg (set below or from history)
                else:
                    # Use the known triggering message regardless of what history returned.
                    # Discord may not have indexed it yet (race between gateway event and REST).
                    last_msg      = current_trigger
                    trigger_name  = current_trigger.author.display_name
                    trigger_ts    = _msg_ts(current_trigger.created_at)
                    trigger_line  = f"[{trigger_ts}] {trigger_name}: {current_trigger.content or ''}"
                    # Append to context if history didn't include it yet
                    if not recent_lines or recent_lines[-1] != trigger_line:
                        recent_lines.append(trigger_line)
                    if first_iteration:
                        original_trigger = current_trigger

                # On retry, if last_msg (from history) is not addressed to the bot
                # but the original trigger was, keep the original trigger.
                if not first_iteration and original_trigger is not None:
                    last_addr_bot = (
                        bot.user in last_msg.mentions
                        or BOT_NAME.lower() in (last_msg.content or "").lower()
                    )
                    orig_addr_bot = (
                        bot.user in original_trigger.mentions
                        or BOT_NAME.lower() in (original_trigger.content or "").lower()
                    )
                    if not last_addr_bot and orig_addr_bot:
                        log.info(f"Channel #{channel_id}: last_msg ({last_msg.author.display_name}) not addressed to bot — falling back to original trigger")
                        last_msg = original_trigger
            else:
                if not last_msg:
                    log.info(f"Channel #{channel_id}: no messages in history — skipping")
                    return
                if last_msg.author.bot:
                    # Bot just posted — find the last human message to evaluate instead
                    human_msgs = [m for m in all_msgs if not m.author.bot]
                    if not human_msgs:
                        log.info(f"Channel #{channel_id}: no human messages in history — skipping")
                        return
                    last_msg = human_msgs[-1]
                    log.info(f"Channel #{channel_id}: last message is bot, falling back to last human message from {last_msg.author.display_name}")

            log.info(f"Channel #{channel_id}: evaluating response to '{last_msg.content[:60]}' from {last_msg.author.display_name}")
            recent_context = "\n".join(recent_lines)

            # If the prompting message has no image but the same user posted one
            # moments earlier (split image + follow-up text, or a retry pass after
            # the follow-up arrived during generation), pull the image from that
            # prior message.
            img_source = last_msg
            if not (last_msg.attachments or last_msg.embeds):
                for prev in reversed(all_msgs[:-1]):
                    if prev.author.id != last_msg.author.id:
                        continue
                    age = (last_msg.created_at - prev.created_at).total_seconds()
                    if age > 300:
                        break
                    if prev.attachments or prev.embeds:
                        img_source = prev
                        log.info(
                            f"Channel #{channel_id}: carrying over image from prior "
                            f"msg {prev.id} ({age:.0f}s earlier) by "
                            f"{prev.author.display_name}"
                        )
                        break

            image_blocks = await fetch_images(img_source.attachments, list(img_source.embeds), img_source.content or "")

            # Hard skip: message @-mentions another user (and not the bot) and
            # doesn't reference the bot by name — clearly addressed elsewhere.
            addressed_others = (
                any(u != bot.user for u in last_msg.mentions)
                and bot.user not in last_msg.mentions
                and BOT_NAME.lower() not in (last_msg.content or "").lower()
            )
            if addressed_others:
                log.info(f"Channel #{channel_id}: message addresses other user(s) — SKIP")
                respond = False
            elif question_bypass:
                # Bot asked a question — treat any reply as a direct answer, skip SKIP-evaluation
                log.info(f"Channel #{channel_id}: direct reply to bot question — skipping evaluation")
                url_context = await _build_url_context(last_msg)
                reply = await ask_claude(
                    last_msg.content + url_context, last_msg.author.display_name,
                    image_blocks=image_blocks or None,
                    channel_id=channel_id, before_id=last_msg.id,
                    memory_context=last_msg.content,
                )
                respond = bool(reply)
            elif BOT_NAME.lower() in last_msg.content.lower():
                # Name mentioned without @mention — treat as direct address, skip evaluation
                log.info(f"Channel #{channel_id}: bot name in message — skipping evaluation")
                url_context = await _build_url_context(last_msg)
                reply = await ask_claude(
                    last_msg.content + url_context, last_msg.author.display_name,
                    image_blocks=image_blocks or None,
                    channel_id=channel_id, before_id=last_msg.id,
                    memory_context=last_msg.content,
                )
                respond = bool(reply)
            else:
                log.info(f"Channel #{channel_id}: evaluating via should_respond for '{last_msg.content[:60]}'")
                respond = await should_respond(
                    last_msg.content, last_msg.author.display_name, recent_context,
                    channel_id=channel_id, image_blocks=image_blocks or None,
                )
                log.info(f"Channel #{channel_id}: should_respond → {'RESPOND' if respond else 'SKIP'}")
                if respond:
                    # Fetch only after the RESPOND decision — links in skipped
                    # chatter shouldn't trigger web requests.
                    url_context = await _build_url_context(last_msg)
                    reply = await ask_claude(
                        last_msg.content + url_context, last_msg.author.display_name,
                        image_blocks=image_blocks or None,
                        channel_id=channel_id, before_id=last_msg.id,
                        memory_context=last_msg.content,
                    )
                    respond = bool(reply)
            log.info(f"Channel #{channel_id}: evaluation → {'RESPOND: ' + (reply or '')[:80] if respond else 'SKIP'}")

            # New message(s) arrived while we were generating — re-read and try again
            if _channel_pending.get(channel_id):
                log.info(f"Channel #{channel_id}: new messages arrived during evaluation — retrying with current context")
                first_iteration = False
                continue

            if respond:
                log.info(f"Channel #{channel_id}: responding")
                _last_response[channel_id] = asyncio.get_event_loop().time()
                _bot_asked_question[channel_id] = reply.rstrip().endswith("?")
                async with channel.typing():
                    await asyncio.sleep(0.3)
                await _send_chat_reply(channel, reply)
            else:
                log.info(f"Channel #{channel_id}: SKIP")
                emoji = await get_emoji_reaction(last_msg.content)
                if emoji:
                    try:
                        await last_msg.add_reaction(emoji)
                    except discord.HTTPException:
                        pass
            return
    except asyncio.CancelledError:
        log.info(f"Channel #{channel_id}: task cancelled")
        raise
    except Exception:
        log.exception(f"Channel #{channel_id}: unexpected error in _try_respond")
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
    log.info(f"on_message: #{message.channel.id} from {message.author} | mention={is_mention} main={is_main} muted={bot_state.muted}")

    # Re-activate
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
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            ref_images = await fetch_images(ref.attachments, ref.embeds, ref.content or "")
            if ref_images:
                image_blocks = ref_images + image_blocks
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
            # message. Appending unconditionally caused false positive classification.
            if ref_content and _URL_RE.search(ref_content):
                classify_text = f"{clean}\n[Benutzer antwortet auf: {ref_content[:300]}]"

        _pre = plugin_registry.pre_classify(classify_text)
        if _pre:
            intent, extra = _pre
            log.info(f"Intent from {message.author} ({'priv' if privileged else 'user'}) [pre]: {intent} | '{clean[:60]}'")
        else:
            intent, extra = await classify_intent(classify_text)
            log.info(f"Intent from {message.author} ({'priv' if privileged else 'user'}): {intent} | '{clean[:60]}'")

        # ── Plugin dispatch ───────────────────────────────────────────────────
        if plugin_registry.handles(intent):
            from plugins.base import MessageContext
            await plugin_registry.dispatch(MessageContext(
                message=message, intent=intent, extra=extra,
                privileged=privileged, classify_text=classify_text,
                ask_claude=_claude_loop,
                system_prompt=build_system_prompt(message.channel.id),
                model_tier=plugin_registry.model_tier_for(intent) or _tier(message.channel.id),
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

        log.warning(f"No plugin handles intent {intent!r} — RespondPlugin should always claim RESPOND fallback")
        return

    # No @mention — only main channels get passive responses
    if not is_main:
        return

    cid = message.channel.id
    if _channel_processing.get(cid):
        # Generation already running — flag that new messages arrived so it re-evaluates
        log.info(f"Channel #{cid}: message from {message.author.display_name} during evaluation — marked as pending")
        _channel_pending[cid] = True
        _channel_pending_msg[cid] = message
    else:
        log.info(f"Channel #{cid}: message from {message.author.display_name} — starting evaluation")
        _channel_processing[cid] = True
        task = asyncio.create_task(_try_respond(cid, message))
        _active_tasks.add(task)
        task.add_done_callback(_active_tasks.discard)

# ── Startup ───────────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()
    def shutdown():
        log.info("Shutdown signal — disconnecting...")
        asyncio.create_task(bot.close())
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
