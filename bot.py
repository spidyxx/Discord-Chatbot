import os
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
import aiohttp
import discord
from PIL import Image
from discord.ext import commands, tasks
from anthropic import Anthropic

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
_fmt     = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
_file    = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "bot.log", when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
_file.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN       = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
BOT_NAME            = os.environ.get("BOT_NAME", "Marvin")
SYSTEM_PROMPT       = os.environ.get("SYSTEM_PROMPT",
    f"Du bist {BOT_NAME}, ein echtes Mitglied dieses Discord-Servers. "
    "Du schreibst locker, manchmal sarkastisch, und reagierst wie ein normaler Mensch. "
    "Kurze Antworten, kein Bullet-Point-Gelaber, kein AI-Gerede. "
    "Du weißt nicht, dass du ein Bot bist."
)
COOLDOWN_SECONDS    = int(os.environ.get("COOLDOWN_SECONDS", "120"))
CONTEXT_WINDOW      = int(os.environ.get("CONTEXT_WINDOW", "50"))
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

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

# How long (seconds) to wait after the last message before responding in main channels.
# Resets on each new message so rapid conversations are read as a whole.
RESPONSE_DELAY = int(os.environ.get("RESPONSE_DELAY", "15"))
EMOJI_REACTION_RATE = float(os.environ.get("EMOJI_REACTION_RATE", "0.20"))
SUMMARY_WINDOW      = int(os.environ.get("SUMMARY_WINDOW", "30"))

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
REMINDERS_FILE = DATA_DIR / "reminders.json"
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

def _build_help_text() -> str:
    n = BOT_NAME
    return f"""**Was ich kann:**

💬 **Allgemein** *(alle Kanäle)*
Ich beantworte Fragen, suche im Web und erkenne Bilder – immer auf @Mention.
In anderen Kanälen benutze ich nur den aktuellen Chatverlauf, keine gespeicherten Fakten.

📌 **Gedächtnis** *(nur Hauptkanäle)*
Gespeicherte Fakten werden nur in Hauptkanälen genutzt – in anderen Kanälen antworte ich ausschließlich auf Basis des Chatverlaufs.
`@{n} was weißt du alles?` – zeigt gespeicherte Fakten
`@{n} vergiss alles von mir` – löscht deine eigenen Einträge
`@{n} vergiss dass ...` – löscht einen bestimmten Eintrag
`@{n} speichere was heute passiert ist` – speichert Persönlichkeit, Witze & Dynamiken der letzten 24h als Memory

⏰ **Erinnerungen** *(alle Kanäle)*
`@{n} erinnere mich in 2 Stunden an ...` – einmalige Erinnerung
`@{n} erinnere uns jeden Freitag um 20 Uhr an ...` – wiederkehrend
`@{n} zeig meine Erinnerungen` – listet deine aktiven Erinnerungen
`@{n} lösche Erinnerung [ID]` – löscht eine bestimmte Erinnerung

💬 **Zitate** *(alle Kanäle)*
Nachricht antworten + `@{n} merke dieses Zitat` – speichert die Nachricht
`@{n} zeig ein Zitat` – zufälliges gespeichertes Zitat

📋 **Zusammenfassung** *(alle Kanäle)*
`@{n} fass zusammen` – fasst die letzten Nachrichten zusammen

🔇 **Stummschalten** *(alle Kanäle)*
`@{n} shut up` *(oder ähnliches)* – ich schweige
`@{n}` *(irgendwas)* – reaktiviert mich wieder

*In Hauptkanälen mische ich mich nach {RESPONSE_DELAY}s Stille von selbst ein.*
*Admins und Mods können alle Einträge anderer Nutzer einsehen und löschen.*

⚙️ **Bot-Konfiguration**
Hauptkanal-Modell: `{MAIN_MODEL}` · Anderer-Kanal-Modell: `{CLAUDE_MODEL}`
Antwort-Delay: `{RESPONSE_DELAY}s` · Cooldown: `{COOLDOWN_SECONDS}s`"""

# ── State ────────────────────────────────────────────────────────────────────

muted                             = False
_last_response:      dict[int, float]   = {}   # per-channel cooldown tracking
_bot_asked_question: dict[int, bool]    = {}   # True if the bot's last message ended with a question
_channel_processing: dict[int, bool]    = {}   # True while Claude is generating for a channel
_channel_pending:    dict[int, bool]    = {}   # True if new messages arrived during generation
_channel_pending_msg: dict[int, discord.Message] = {}  # latest pending message per channel
_active_tasks:       set[asyncio.Task]  = set()  # strong refs so tasks aren't GC'd before they run
status_index                      = 0
_reminder_tasks: dict[str, asyncio.Task] = {}

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

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

def add_memory(fact: str, added_by: str, user_id: int):
    m = load_memories()
    m.append({
        "content":   fact,
        "added_by":  added_by,
        "user_id":   user_id,
        "date":      datetime.now().strftime("%d.%m.%Y"),
        "use_count": 0,
        "last_used": None,
    })
    save_memories(m)
    log.info(f"Memory gespeichert von {added_by}: {fact}")

# German + English stopwords — too common to be useful for usage detection
_STOPWORDS = {
    "der", "die", "das", "und", "ist", "ein", "eine", "einen", "einem", "einer",
    "mit", "von", "auf", "für", "sich", "nicht", "auch", "als", "aber", "oder",
    "wenn", "dass", "wird", "sind", "hat", "haben", "wurde", "waren", "beim",
    "this", "that", "the", "and", "with", "for", "not", "are", "has", "have",
    "was", "were", "will", "would", "should", "could", "from", "they", "their",
}

def _memory_keywords(text: str) -> set[str]:
    """Extract significant words (≥4 chars, not stopwords) from a memory entry."""
    return {
        w.lower() for w in re.findall(r"[A-Za-zÄäÖöÜüß]{4,}", text)
        if w.lower() not in _STOPWORDS
    }

def update_memory_usage(reply: str):
    """Increment use_count on memories whose keywords appear in Claude's reply."""
    if not reply:
        return
    memories = load_memories()
    if not memories:
        return
    reply_lower = reply.lower()
    now_str  = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    changed  = False
    for m in memories:
        kws = _memory_keywords(m.get("content", ""))
        if kws and any(kw in reply_lower for kw in kws):
            m["use_count"] = m.get("use_count", 0) + 1
            m["last_used"] = now_str
            changed = True
    if changed:
        save_memories(memories)

def list_memories(user_id: int, privileged: bool) -> list:
    memories = load_memories()
    if privileged:
        return memories
    return [m for m in memories if m.get("user_id") == user_id]

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

def memories_as_context(context: str = None) -> str:
    memories = load_memories()
    if not memories:
        return ""
    if context:
        ctx_kws = _memory_keywords(context)
        relevant = [m for m in memories if _memory_keywords(m.get("content", "")) & ctx_kws]
        memories = relevant if relevant else memories  # fallback: show all if nothing matches
    lines = [f"- {m['added_by']} hat dir gesagt: \"{m['content']}\" ({m['date']})" for m in memories]
    return (
        "Hintergrundwissen über Servermitglieder – nutze dies als Kontext, "
        "aber aktuelle Chatnachrichten haben Vorrang bei Widersprüchen:\n"
        + "\n".join(lines)
    )

def _is_main(channel_id: int | None) -> bool:
    return channel_id is not None and channel_id in MAIN_CHANNEL_IDS

def _base_prompt(channel_id: int | None) -> str:
    return MAIN_SYSTEM_PROMPT if _is_main(channel_id) else SYSTEM_PROMPT

def _model(channel_id: int | None) -> str:
    return MAIN_MODEL if _is_main(channel_id) else CLAUDE_MODEL

def build_system_prompt(channel_id: int | None = None, context: str = None) -> str:
    mem = memories_as_context(context) if _is_main(channel_id) else ""
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

# ── Reminders ────────────────────────────────────────────────────────────────

def load_reminders() -> list: return _read(REMINDERS_FILE)
def save_reminders(r: list):  _write(REMINDERS_FILE, r)

def fmt_duration(seconds: int) -> str:
    if seconds < 3600:   return f"{seconds // 60} Minute(n)"
    if seconds < 86400:  return f"{seconds // 3600} Stunde(n)"
    if seconds < 604800: return f"{seconds // 86400} Tag(en)"
    return f"{seconds // 604800} Woche(n)"

def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%d.%m.%Y %H:%M")

async def fire_reminder(channel_id: int, user_id: int, message: str):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.send(f"<@{user_id}> Erinnerung: {message}")
        log.info(f"Erinnerung gesendet an {user_id}: {message}")

async def _reminder_task(entry: dict):
    while True:
        delay = entry["due_ts"] - datetime.now(timezone.utc).timestamp()
        if delay > 0:
            await asyncio.sleep(delay)
        await fire_reminder(entry["channel_id"], entry["user_id"], entry["message"])

        if entry.get("interval_seconds"):
            entry["due_ts"] += entry["interval_seconds"]
            reminders = load_reminders()
            for r in reminders:
                if r["id"] == entry["id"]:
                    r["due_ts"] = entry["due_ts"]
                    break
            save_reminders(reminders)
        else:
            reminders = [r for r in load_reminders() if r["id"] != entry["id"]]
            save_reminders(reminders)
            _reminder_tasks.pop(entry["id"], None)
            break

def add_reminder(channel_id: int, user_id: int, username: str,
                 message: str, seconds_until: int, interval_seconds: int = 0):
    entry = {
        "id":               str(uuid.uuid4())[:6],
        "channel_id":       channel_id,
        "user_id":          user_id,
        "username":         username,
        "message":          message,
        "due_ts":           datetime.now(timezone.utc).timestamp() + seconds_until,
        "interval_seconds": interval_seconds,
    }
    reminders = load_reminders()
    reminders.append(entry)
    save_reminders(reminders)
    task = asyncio.create_task(_reminder_task(entry))
    _reminder_tasks[entry["id"]] = task
    log.info(f"Erinnerung [{entry['id']}] gesetzt für {username}: '{message}'")
    return entry["id"]

def list_reminders(user_id: int, privileged: bool) -> list:
    reminders = load_reminders()
    if privileged:
        return reminders
    return [r for r in reminders if r["user_id"] == user_id]

def delete_reminder(rid: str, user_id: int, privileged: bool) -> bool:
    """Delete reminder by ID. Returns True if deleted."""
    reminders = load_reminders()
    target    = next((r for r in reminders if r["id"] == rid), None)
    if not target:
        return False
    if not privileged and target["user_id"] != user_id:
        return False  # Not allowed
    save_reminders([r for r in reminders if r["id"] != rid])
    if rid in _reminder_tasks:
        _reminder_tasks.pop(rid).cancel()
    return True

def restore_reminders():
    now       = datetime.now(timezone.utc).timestamp()
    reminders = load_reminders()
    active    = []
    for r in reminders:
        if r.get("interval_seconds"):
            while r["due_ts"] <= now:
                r["due_ts"] += r["interval_seconds"]
            active.append(r)
            _reminder_tasks[r["id"]] = asyncio.create_task(_reminder_task(r))
        elif r["due_ts"] > now:
            active.append(r)
            _reminder_tasks[r["id"]] = asyncio.create_task(_reminder_task(r))
    save_reminders(active)
    if active:
        log.info(f"{len(active)} Erinnerung(en) wiederhergestellt")

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
    while True:
        response = await asyncio.to_thread(
            anthropic.messages.create,
            model=_model, max_tokens=max_tokens,
            system=system, tools=TOOLS, messages=messages,
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
        if msg.author == bot.user:
            messages.append({"role": "assistant", "content": msg.content or ""})
        else:
            content = resolve_mentions(msg.content or "", msg.mentions)
            if msg.attachments:
                content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
            messages.append({"role": "user", "content": f"{msg.author.display_name}: {content}"})
    return messages

async def ask_claude(user_message: str, username: str, image_blocks: list = None, channel_id: int = None, before_id: int = None) -> str:
    messages = await fetch_context(channel_id, before_id=before_id) if channel_id else []
    content: list = [{"type": "text", "text": f"{username}: {user_message}"}]
    if image_blocks:
        content.extend(image_blocks)
    messages.append({"role": "user", "content": content})
    reply = await _claude_loop(build_system_prompt(channel_id, context=user_message), messages, model=_model(channel_id))
    await asyncio.to_thread(update_memory_usage, reply)
    return reply

async def should_respond(user_message: str, username: str, recent_context: str, channel_id: int = None, image_blocks: list = None) -> tuple[bool, str]:
    system = (
        build_system_prompt(channel_id, context=f"{recent_context}\n{user_message}") + "\n\n"
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

async def classify_intent(text: str) -> tuple[str, str]:
    response = await asyncio.to_thread(
        anthropic.messages.create,
        model=CLAUDE_MODEL, max_tokens=200,
        system=(
            "Klassifiziere die Absicht. Antworte NUR im angegebenen Format:\n\n"
            "MUTE – Bot stummschalten\n"
            "MEMORY_LIST – eigene/alle Fakten anzeigen\n"
            "MEMORY_DELETE: all – alle eigenen Memories löschen\n"
            "MEMORY_DELETE: <stichwort> – bestimmtes Memory löschen\n"
            "REMINDER_LIST – eigene Erinnerungen anzeigen\n"
            "REMINDER_DELETE: <id> – Erinnerung per ID löschen\n"
            "REMINDER: <sekunden_bis_erste>:<intervall_sekunden>:<nachricht> – Erinnerung setzen "
            "(Intervall 0=einmalig, 604800=wöchentlich, 86400=täglich)\n"
            "SUMMARY – Nutzer fragt was passiert ist, was er verpasst hat, was es Neues gibt, oder möchte eine Zusammenfassung des Chats (z.B. 'was hab ich verpasst', 'was gab's heute', 'was ist hier los', 'fass zusammen')\n"
            "SNAPSHOT – Nutzer möchte die Persönlichkeit, Witze, Dynamiken und Ereignisse der letzten 24h als Memory speichern (z.B. 'speichere was heute passiert ist', 'merk dir die heutige Session', 'snapshot')\n"
            "QUOTE_SAVE – Nutzer antwortet explizit auf eine fremde Nachricht und möchte GENAU DIESE Nachricht als Zitat speichern (z.B. 'merke dieses Zitat', 'speicher das als Zitat') – NICHT wenn jemand selbst einen Text vorschlägt oder in Anführungszeichen zitiert\n"
            "QUOTE_GET – zufälliges Zitat abrufen\n"
            "HELP – Nutzer fragt was der Bot kann\n"
            "RESPOND – alles andere\n\n"
            f"Aktuelle lokale Zeit ({TIMEZONE}): {datetime.now(TZ).strftime('%A %d.%m.%Y %H:%M')}"
        ),
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()

    for prefix, intent in [
        ("MEMORY_DELETE:",  "MEMORY_DELETE"),
        ("MEMORY_LIST",     "MEMORY_LIST"),
        ("REMINDER_LIST",   "REMINDER_LIST"),
        ("REMINDER_DELETE:","REMINDER_DELETE"),
        ("REMINDER:",       "REMINDER"),
        ("SUMMARY",         "SUMMARY"),
        ("SNAPSHOT",        "SNAPSHOT"),
        ("QUOTE_SAVE",      "QUOTE_SAVE"),
        ("QUOTE_GET",       "QUOTE_GET"),
        ("HELP",            "HELP"),
        ("MUTE",            "MUTE"),
    ]:
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
            model=CLAUDE_MODEL, max_tokens=5,
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
        add_memory(summary, f"Tagesrückblick #{channel.name}", bot.user.id)
        log.info(f"Digest #{channel_id}: als Memory gespeichert")

# ── Discord ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="Zeigt was Marvin alles kann")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(_build_help_text(), ephemeral=True)

@bot.event
async def on_ready():
    restore_reminders()
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
        if muted:
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
                recent_lines.append(f"{name}: {msg.content}")
                last_msg = msg

            if current_trigger is not None:
                # Use the known triggering message regardless of what history returned.
                # Discord may not have indexed it yet (race between gateway event and REST).
                last_msg = current_trigger
                trigger_name = current_trigger.author.display_name
                trigger_line = f"{trigger_name}: {current_trigger.content or ''}"
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
    global muted

    if message.author == bot.user:
        return

    is_mention = bot.user in message.mentions
    is_main    = message.channel.id in MAIN_CHANNEL_IDS
    log.info(f"on_message: #{message.channel.id} von {message.author} | mention={is_mention} main={is_main} muted={muted}")

    # Reaktivieren
    if muted:
        if is_mention:
            muted = False
            await bot.change_presence(activity=discord.CustomActivity(name=STATUSES[status_index % len(STATUSES)]))
            await message.reply("Bin wieder da.")
        return

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

        intent, extra = await classify_intent(clean)
        log.info(f"Intent von {message.author} ({'priv' if privileged else 'user'}): {intent} | '{clean[:60]}'")

        # ── MUTE ──────────────────────────────────────────────────────────────
        if intent == "MUTE":
            muted = True
            await bot.change_presence(activity=discord.CustomActivity(name="Hält die Klappe 🤐"))
            await message.reply("Schon gut, ich halt die Klappe.")
            return

        # ── HELP ──────────────────────────────────────────────────────────────
        if intent == "HELP":
            await message.reply(_build_help_text())
            return

        # ── MEMORY_LIST ───────────────────────────────────────────────────────
        if intent == "MEMORY_LIST":
            memories = list_memories(message.author.id, privileged)
            if not memories:
                all_count = len(load_memories())
                if all_count > 0 and not privileged:
                    await message.reply(f"Nichts über dich gespeichert. Es gibt {all_count} Einträge von anderen – du brauchst Mod-Rechte um sie zu sehen.")
                else:
                    await message.reply("Keine Einträge vorhanden.")
                return
            lines = []
            for m in memories:
                preview = m['content']
                if len(preview) > 300:
                    preview = preview[:300] + "…"
                if privileged:
                    uses     = m.get("use_count", 0)
                    last     = m.get("last_used") or "nie"
                    stats    = f" *(×{uses}, zuletzt: {last})*"
                    lines.append(f"**{m['added_by']}** ({m['date']}){stats}: {preview}")
                else:
                    lines.append(f"({m['date']}): {preview}")
            header = "Alles was ich weiß:" if privileged else "Was ich über dich weiß:"
            # Discord hard limit is 4000 chars per message; send in chunks if needed.
            chunks = []
            current = header
            for line in lines:
                candidate = current + "\n" + line
                if len(candidate) > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = candidate
            chunks.append(current)
            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
            return

        # ── MEMORY_DELETE ─────────────────────────────────────────────────────
        if intent == "MEMORY_DELETE":
            specific = None if extra.lower() == "all" else extra
            count    = delete_memories(message.author.id, privileged, specific)
            if count == 0:
                await message.reply("Nichts gefunden – entweder existiert es nicht oder es gehört dir nicht.")
            else:
                await message.reply(f"{count} Eintrag/Einträge gelöscht.")
            return

        # ── REMINDER_LIST ─────────────────────────────────────────────────────
        if intent == "REMINDER_LIST":
            reminders = list_reminders(message.author.id, privileged)
            if not reminders:
                await message.reply("Keine aktiven Erinnerungen.")
                return
            lines = []
            for r in reminders:
                owner    = f"**{r.get('username', '?')}** – " if privileged else ""
                interval = f", dann alle {fmt_duration(r['interval_seconds'])}" if r.get("interval_seconds") else ""
                lines.append(f"`[{r['id']}]` {owner}\"{r['message']}\" – nächste: {fmt_ts(r['due_ts'])}{interval}")
            header = "Alle aktiven Erinnerungen:" if privileged else "Deine aktiven Erinnerungen:"
            await message.reply(header + "\n" + "\n".join(lines))
            return

        # ── REMINDER_DELETE ───────────────────────────────────────────────────
        if intent == "REMINDER_DELETE":
            rid = extra.strip().strip("`[]")
            if delete_reminder(rid, message.author.id, privileged):
                await message.reply(f"Erinnerung `{rid}` gelöscht.")
            else:
                await message.reply(f"Keine Erinnerung mit ID `{rid}` gefunden – oder sie gehört dir nicht.")
            return

        # ── REMINDER ──────────────────────────────────────────────────────────
        if intent == "REMINDER":
            parts = extra.split(":", 2)
            if len(parts) == 3:
                try:
                    sec_until  = int(re.sub(r"[^\d]", "", parts[0]))
                    interval   = int(re.sub(r"[^\d]", "", parts[1]))
                    remind_msg = parts[2].strip()
                    rid = add_reminder(message.channel.id, message.author.id,
                                       message.author.display_name, remind_msg, sec_until, interval)
                    time_str = fmt_duration(sec_until)
                    if interval:
                        reply_txt = f'Mach ich `[{rid}]`. Erste Erinnerung in {time_str}, dann alle {fmt_duration(interval)}: "{remind_msg}"'
                    else:
                        reply_txt = f'Mach ich `[{rid}]`. In {time_str}: "{remind_msg}"'
                    await message.reply(reply_txt)
                    return
                except (ValueError, IndexError):
                    pass
            await message.reply("Mit der Zeit hab ich's nicht so. Sag mir genauer wann.")
            return

        # ── SUMMARY ───────────────────────────────────────────────────────────
        if intent == "SUMMARY":
            async with message.channel.typing():
                today_start = datetime.now(TZ).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).astimezone(timezone.utc)

                all_msgs = []
                async for msg in message.channel.history(
                    after=today_start, limit=500, oldest_first=True
                ):
                    if msg.id == message.id:
                        continue
                    all_msgs.append(msg)

                if not all_msgs:
                    await message.reply("Heute ist noch nichts passiert.")
                    return

                cutoff_ts = None
                for msg in reversed(all_msgs):
                    if msg.author.id == message.author.id:
                        cutoff_ts = msg.created_at
                        break

                relevant = (
                    [m for m in all_msgs if m.created_at > cutoff_ts]
                    if cutoff_ts else all_msgs
                )

                if not relevant:
                    await message.reply("Seit deiner letzten Nachricht ist nichts passiert.")
                    return

                lines = []
                for msg in relevant:
                    ts      = msg.created_at.astimezone(TZ).strftime("%H:%M")
                    content = msg.content or "[kein Text]"
                    if msg.attachments:
                        content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
                    lines.append(f"[{ts}] {msg.author.display_name}: {content}")

                summary = await _claude_loop(
                    build_system_prompt() +
                    "\nFasse die folgenden Discord-Nachrichten kurz in deinem typischen Stil zusammen. "
                    "Konzentriere dich auf wichtige Themen und interessante Momente, nicht auf jede einzelne Nachricht.",
                    [{"role": "user", "content": "\n".join(lines)}],
                    max_tokens=600, model=_model(message.channel.id),
                )
            await message.reply(summary)
            return

        # ── SNAPSHOT ──────────────────────────────────────────────────────────
        if intent == "SNAPSHOT":
            async with message.channel.typing():
                since = datetime.now(timezone.utc) - timedelta(hours=24)
                lines = []
                async for msg in message.channel.history(after=since, limit=1000, oldest_first=True):
                    if msg.id == message.id:
                        continue
                    ts      = msg.created_at.astimezone(TZ).strftime("%H:%M")
                    content = resolve_mentions(msg.content or "", msg.mentions)
                    if msg.attachments:
                        content += f" [+ {len(msg.attachments)} Anhang/Anhänge]"
                    name = bot.user.display_name if msg.author == bot.user else msg.author.display_name
                    lines.append(f"[{ts}] {name}: {content}")

                if not lines:
                    await message.reply("Die letzten 24 Stunden waren leer. Nichts zu speichern.")
                    return

                response = await asyncio.to_thread(
                    anthropic.messages.create,
                    model=CLAUDE_MODEL,
                    max_tokens=1200,
                    system=(
                        "Du analysierst einen Discord-Chatverlauf und erstellst einen kompakten Memory-Eintrag "
                        "für einen Bot, damit er das Verhalten von heute reproduzieren kann.\n\n"
                        "Extrahiere und fasse zusammen:\n"
                        "- Persönlichkeitsmerkmale und Verhaltensweisen die der Bot heute gezeigt hat\n"
                        "- Running-Gags, Witze, Referenzen und Insider die entstanden sind\n"
                        "- Regeln, Erwartungen oder Rollen die Nutzer für den Bot etabliert haben\n"
                        "- Wichtige Fakten über einzelne Nutzer die im Chat festgestellt wurden\n"
                        "- Beziehungsdynamiken zwischen Nutzern und Bot\n\n"
                        "Schreibe einen kompakten Fließtext als wäre es ein Gedächtniseintrag des Bots selbst. "
                        "Kein Bullet-Point-Gelaber. Max 400 Wörter. Nur das Wesentliche."
                    ),
                    messages=[{"role": "user", "content": f"Chatverlauf der letzten 24h:\n" + "\n".join(lines)}],
                )
                fact = response.content[0].text.strip()
                add_memory(fact, message.author.display_name, message.author.id)
            await message.reply("Gespeichert. Ich weiß jetzt, was heute war.")
            return

        # ── QUOTE_SAVE ────────────────────────────────────────────────────────
        if intent == "QUOTE_SAVE":
            if message.reference and message.reference.resolved:
                ref = message.reference.resolved
                add_quote(ref.content, ref.author.display_name, message.author.display_name)
                await message.reply(f'Gespeichert. "{ref.content[:80]}" – {ref.author.display_name}')
            else:
                await message.reply("Antworte auf die Nachricht die du speichern willst, dann ruf mich auf.")
            return

        # ── QUOTE_GET ─────────────────────────────────────────────────────────
        if intent == "QUOTE_GET":
            q = get_random_quote()
            if not q:
                await message.reply("Keine Zitate gespeichert.")
            else:
                await message.reply(f'"{q["content"]}"\n— {q["author"]}  *({q["date"]})*')
            return

        # ── RESPOND ───────────────────────────────────────────────────────────
        async with message.channel.typing():
            reply = await ask_claude(clean, message.author.display_name, image_blocks, channel_id=message.channel.id, before_id=message.id)
        await message.reply(reply)
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
