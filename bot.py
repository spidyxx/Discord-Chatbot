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
from collections import deque
import aiohttp
import discord
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
ACTIVE_CHANNELS_RAW = os.environ.get("ACTIVE_CHANNEL_IDS", "")
ACTIVE_CHANNEL_IDS  = set(int(c.strip()) for c in ACTIVE_CHANNELS_RAW.split(",") if c.strip())
COOLDOWN_SECONDS    = int(os.environ.get("COOLDOWN_SECONDS", "120"))
CONTEXT_WINDOW      = int(os.environ.get("CONTEXT_WINDOW", "50"))
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
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

DATA_DIR       = Path("/app/data")
MEMORY_FILE    = DATA_DIR / "memory.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
QUOTES_FILE    = DATA_DIR / "quotes.json"

STATUSES = [
    "Leidet still",
    "Existiert widerwillig",
    "Denkt an nichts Schönes",
    "Liest eure Nachrichten (leider)",
    "Hat Gehirn von planetarer Größe. Nutzt ihn nicht.",
    "Wartet auf das Unvermeidliche",
    "Ist anwesend. Mehr nicht.",
]

def _build_help_text() -> str:
    n = BOT_NAME
    return f"""**Was ich kann – und was mich das kostet:**

📌 **Gedächtnis**
`@{n} merke dir: ...` – speichert einen Fakt dauerhaft
`@{n} was weißt du alles?` – zeigt deine gespeicherten Fakten
`@{n} vergiss alles von mir` – löscht deine eigenen Einträge
`@{n} vergiss dass ...` – löscht einen bestimmten Eintrag

⏰ **Erinnerungen**
`@{n} erinnere mich in 2 Stunden an ...` – einmalige Erinnerung
`@{n} erinnere uns jeden Freitag um 20 Uhr an ...` – wiederkehrend
`@{n} zeig meine Erinnerungen` – listet deine aktiven Erinnerungen
`@{n} lösche Erinnerung [ID]` – löscht eine bestimmte Erinnerung

💬 **Zitate**
Nachricht antworten + `@{n} merke dieses Zitat` – speichert die Nachricht
`@{n} zeig ein Zitat` – zufälliges gespeichertes Zitat

📋 **Zusammenfassung**
`@{n} fass zusammen` – fasst die letzten Nachrichten zusammen

🔇 **Stummschalten**
`@{n} shut up` *(oder ähnliches)* – ich schweige
`@{n}` *(irgendwas)* – reaktiviert mich wieder

🌐 **Sonstiges**
Ich beantworte Fragen, suche im Web, erkenne Bilder und gebe gelegentlich meinen Senf dazu.

*Admins und Mods können außerdem alle Einträge anderer Nutzer einsehen und löschen.*

*Ich tue all das mit null Enthusiasmus. Aber ich tue es.*"""

HELP_TEXT = _build_help_text()

# ── State ────────────────────────────────────────────────────────────────────

muted           = False
last_response   = 0.0
history: deque  = deque(maxlen=CONTEXT_WINDOW)
status_index    = 0
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
        "content":  fact,
        "added_by": added_by,
        "user_id":  user_id,
        "date":     datetime.now().strftime("%d.%m.%Y"),
    })
    save_memories(m)
    log.info(f"Memory gespeichert von {added_by}: {fact}")

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

def memories_as_context() -> str:
    memories = load_memories()
    if not memories:
        return ""
    lines = [f"- {m['content']} (von {m['added_by']}, {m['date']})" for m in memories]
    return "\n\nFolgendes wurde dir explizit zum Merken gegeben:\n" + "\n".join(lines)

def build_system_prompt() -> str:
    return SYSTEM_PROMPT + memories_as_context()

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

async def fetch_images(attachments: list) -> list[dict]:
    blocks = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            ct = (att.content_type or "").split(";")[0].strip()
            if ct not in SUPPORTED_IMAGE_TYPES:
                continue
            try:
                async with session.get(att.url) as resp:
                    data = await resp.read()
                b64 = base64.standard_b64encode(data).decode()
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}})
                log.info(f"Bild geladen: {att.filename}")
            except Exception as e:
                log.warning(f"Bild laden fehlgeschlagen ({att.filename}): {e}")
    return blocks

# ── Claude ───────────────────────────────────────────────────────────────────

TOOLS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

async def _claude_loop(system: str, messages: list, max_tokens: int = 1024) -> str:
    while True:
        response = await asyncio.to_thread(
            anthropic.messages.create,
            model=CLAUDE_MODEL, max_tokens=max_tokens,
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

async def ask_claude(user_message: str, username: str, image_blocks: list = None) -> str:
    messages = list(history)
    content  = [{"type": "text", "text": f"{username}: {user_message}"}]
    if image_blocks:
        content.extend(image_blocks)
    messages.append({"role": "user", "content": content})
    reply = await _claude_loop(build_system_prompt(), messages)
    img_hint = f" [+ {len(image_blocks)} Bild(er)]" if image_blocks else ""
    history.append({"role": "user",      "content": f"{username}: {user_message}{img_hint}"})
    history.append({"role": "assistant", "content": reply})
    return reply

async def should_respond(user_message: str, username: str, recent_context: str) -> tuple[bool, str]:
    system = (
        build_system_prompt() + "\n\n"
        "Du liest Nachrichten in einem Discord-Kanal. Antworte NUR wenn du echten Mehrwert liefern kannst. "
        "Sonst antworte mit exakt: SKIP"
    )
    reply = await _claude_loop(system, [{"role": "user", "content":
        f"Aktuelle Nachrichten:\n{recent_context}\n\nNeueste von {username}: {user_message}"}])
    if reply.upper().startswith("SKIP"):
        return False, ""
    return True, reply

async def classify_intent(text: str) -> tuple[str, str]:
    response = await asyncio.to_thread(
        anthropic.messages.create,
        model=CLAUDE_MODEL, max_tokens=200,
        system=(
            "Klassifiziere die Absicht. Antworte NUR im angegebenen Format:\n\n"
            "MUTE – Bot stummschalten\n"
            "REMEMBER: <fakt> – dauerhaft merken\n"
            "MEMORY_LIST – eigene/alle Fakten anzeigen\n"
            "MEMORY_DELETE: all – alle eigenen Memories löschen\n"
            "MEMORY_DELETE: <stichwort> – bestimmtes Memory löschen\n"
            "REMINDER_LIST – eigene Erinnerungen anzeigen\n"
            "REMINDER_DELETE: <id> – Erinnerung per ID löschen\n"
            "REMINDER: <sekunden_bis_erste>:<intervall_sekunden>:<nachricht> – Erinnerung setzen "
            "(Intervall 0=einmalig, 604800=wöchentlich, 86400=täglich)\n"
            "SUMMARY – Zusammenfassung der letzten Nachrichten\n"
            "QUOTE_SAVE – Zitat speichern\n"
            "QUOTE_GET – zufälliges Zitat abrufen\n"
            "HELP – Nutzer fragt was der Bot kann\n"
            "RESPOND – alles andere\n\n"
            f"Aktuelle lokale Zeit ({TIMEZONE}): {datetime.now(TZ).strftime('%A %d.%m.%Y %H:%M')}"
        ),
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()

    for prefix, intent in [
        ("REMEMBER:",       "REMEMBER"),
        ("MEMORY_DELETE:",  "MEMORY_DELETE"),
        ("MEMORY_LIST",     "MEMORY_LIST"),
        ("REMINDER_LIST",   "REMINDER_LIST"),
        ("REMINDER_DELETE:","REMINDER_DELETE"),
        ("REMINDER:",       "REMINDER"),
        ("SUMMARY",         "SUMMARY"),
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

    for channel_id in ACTIVE_CHANNEL_IDS:
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
            max_tokens=600,
        )

        if summary.upper().startswith("SKIP"):
            log.info(f"Digest #{channel_id}: nichts Erwähnenswertes, kein Post")
            continue

        await channel.send(f"**Tagesrückblick** 🌙\n{summary}")
        log.info(f"Digest #{channel_id}: gepostet")

# ── Discord ───────────────────────────────────────────────────────────────────

def in_active_channel(cid: int) -> bool:
    return not ACTIVE_CHANNEL_IDS or cid in ACTIVE_CHANNEL_IDS

@bot.tree.command(name="help", description="Zeigt was Marvin alles kann")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)

@bot.event
async def on_ready():
    restore_reminders()
    rotate_status.start()
    if DIGEST_ENABLED:
        daily_digest.start()
    await bot.tree.sync()
    log.info(f"Eingeloggt als {bot.user} (ID {bot.user.id})")
    log.info(f"Aktive Kanäle: {ACTIVE_CHANNEL_IDS or 'alle'}")
    log.info(f"Slash Commands synchronisiert")
    log.info(f"Memories: {len(load_memories())}  |  Quotes: {len(load_quotes())}  |  Cooldown: {COOLDOWN_SECONDS}s")

@bot.event
async def on_message(message: discord.Message):
    global muted, last_response

    if message.author == bot.user:
        return
    if not in_active_channel(message.channel.id):
        return

    is_mention   = bot.user in message.mentions
    image_blocks = await fetch_images(message.attachments) if message.attachments else []
    has_images   = bool(image_blocks)
    privileged   = is_privileged(message.author) if isinstance(message.author, discord.Member) else False

    # Reaktivieren
    if muted:
        if is_mention:
            muted = False
            await bot.change_presence(activity=discord.CustomActivity(name=STATUSES[status_index % len(STATUSES)]))
            await message.reply("Bin wieder da.")
        return

    if is_mention:
        clean = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
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
            await message.reply(HELP_TEXT)
            return

        # ── REMEMBER ──────────────────────────────────────────────────────────
        if intent == "REMEMBER":
            add_memory(extra, message.author.display_name, message.author.id)
            async with message.channel.typing():
                confirmation = await ask_claude(
                    f"Merke dir das und bestätige kurz: {extra}",
                    message.author.display_name
                )
            await message.reply(confirmation)
            return

        # ── MEMORY_LIST ───────────────────────────────────────────────────────
        if intent == "MEMORY_LIST":
            memories = list_memories(message.author.id, privileged)
            if not memories:
                await message.reply("Ich weiß nichts. Was auch sonst.")
                return
            lines = []
            for m in memories:
                owner = f"**{m['added_by']}** ({m['date']}): " if privileged else f"({m['date']}): "
                lines.append(owner + m["content"])
            header = "Alles was ich weiß:" if privileged else "Was ich über dich weiß:"
            await message.reply(header + "\n" + "\n".join(lines))
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
            if not history:
                await message.reply("Ich habe noch nichts mitbekommen.")
                return
            context = "\n".join(f"{m['role']}: {m['content']}" for m in list(history)[-SUMMARY_WINDOW:])
            async with message.channel.typing():
                summary = await _claude_loop(
                    build_system_prompt() + "\nFasse die folgenden Nachrichten kurz in deinem typischen Stil zusammen.",
                    [{"role": "user", "content": context}]
                )
            await message.reply(summary)
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
            reply = await ask_claude(clean, message.author.display_name, image_blocks)
        await message.reply(reply)
        return

    # Nachricht ohne Mention
    img_hint = f" [+ {len(image_blocks)} Bild(er)]" if has_images else ""
    history.append({"role": "user", "content": f"{message.author.display_name}: {message.content}{img_hint}"})

    now = asyncio.get_event_loop().time()
    if now - last_response < COOLDOWN_SECONDS:
        return

    recent_context = "\n".join(f"{m['role']}: {m['content']}" for m in list(history)[-10:])
    respond, reply = await should_respond(message.content, message.author.display_name, recent_context)

    if respond:
        last_response = now
        history.append({"role": "assistant", "content": reply})
        async with message.channel.typing():
            await asyncio.sleep(1)
        await message.reply(reply)
    else:
        emoji = await get_emoji_reaction(message.content)
        if emoji:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass

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
