"""Reminders plugin — REMINDER, REMINDER_LIST, REMINDER_DELETE intents."""

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from plugins.base import Plugin, MessageContext, _read, _write
from plugins import state as bot_state

_log = logging.getLogger(__name__)

_DATA_DIR      = Path(os.environ.get("DATA_DIR", "/app/data"))
_REMINDERS_FILE = _DATA_DIR / "reminders.json"
_TZ            = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))

_reminder_tasks: dict[str, asyncio.Task] = {}


# ── File I/O ──────────────────────────────────────────────────────────────────

def _load() -> list: return _read(_REMINDERS_FILE)
def _save(r: list):  _write(_REMINDERS_FILE, r)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_duration(seconds: int) -> str:
    if seconds < 3600:   return f"{seconds // 60} Minute(n)"
    if seconds < 86400:  return f"{seconds // 3600} Stunde(n)"
    if seconds < 604800: return f"{seconds // 86400} Tag(en)"
    return f"{seconds // 604800} Woche(n)"

def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%d.%m.%Y %H:%M")


# ── Reminder lifecycle ────────────────────────────────────────────────────────

async def _classify_mode(message: str) -> str:
    text = await bot_state.claude_loop(
        (
            "Classify this reminder as PROMPT or NOTIFY.\n"
            "PROMPT = the bot must generate a response (tell a joke, write a poem, ask a question, etc.)\n"
            "NOTIFY = just remind the user about something (meeting, medication, task name, event, etc.)\n"
            "Reply with exactly one word: PROMPT or NOTIFY"
        ),
        [{"role": "user", "content": message}],
        max_tokens=10,
        tier="cheap",
    )
    return "prompt" if text.strip().upper().startswith("PROMPT") else "notify"


async def _fire(entry: dict):
    channel = bot_state.bot.get_channel(entry["channel_id"])
    if not channel:
        return
    user_id = entry["user_id"]
    message = entry["message"]
    if entry.get("mode") == "prompt":
        main_channel_id = next(iter(bot_state.main_channel_ids), entry["channel_id"])
        reply = await bot_state.claude_loop(
            bot_state.build_system_prompt(main_channel_id),
            [{"role": "user", "content": message}],
            tier=bot_state.reminder_tier,
        )
        await channel.send(f"<@{user_id}> {reply}")
        _log.info(f"Prompt reminder fired for {user_id}: {message[:60]}")
    else:
        await channel.send(f"<@{user_id}> Erinnerung: {message}")
        _log.info(f"Reminder sent to {user_id}: {message[:60]}")


async def _task(entry: dict):
    while True:
        delay = entry["due_ts"] - datetime.now(timezone.utc).timestamp()
        if delay > 0:
            await asyncio.sleep(delay)
        await _fire(entry)

        if entry.get("interval_seconds"):
            entry["due_ts"] += entry["interval_seconds"]
            reminders = _load()
            for r in reminders:
                if r["id"] == entry["id"]:
                    r["due_ts"] = entry["due_ts"]
                    break
            _save(reminders)
        else:
            _save([r for r in _load() if r["id"] != entry["id"]])
            _reminder_tasks.pop(entry["id"], None)
            break


def _add(channel_id: int, user_id: int, username: str,
         message: str, seconds_until: int, interval_seconds: int = 0,
         mode: str = "notify") -> str:
    entry = {
        "id":               str(uuid.uuid4())[:6],
        "channel_id":       channel_id,
        "user_id":          user_id,
        "username":         username,
        "message":          message,
        "mode":             mode,
        "due_ts":           datetime.now(timezone.utc).timestamp() + seconds_until,
        "interval_seconds": interval_seconds,
    }
    reminders = _load()
    reminders.append(entry)
    _save(reminders)
    task = asyncio.create_task(_task(entry))
    _reminder_tasks[entry["id"]] = task
    _log.info(f"Reminder [{entry['id']}] set for {username}: '{message}'")
    return entry["id"]


def _list(user_id: int, privileged: bool) -> list:
    reminders = _load()
    return reminders if privileged else [r for r in reminders if r["user_id"] == user_id]


def _delete(rid: str, user_id: int, privileged: bool) -> bool:
    reminders = _load()
    target    = next((r for r in reminders if r["id"] == rid), None)
    if not target:
        return False
    if not privileged and target["user_id"] != user_id:
        return False
    _save([r for r in reminders if r["id"] != rid])
    if rid in _reminder_tasks:
        _reminder_tasks.pop(rid).cancel()
    return True


def _restore():
    now       = datetime.now(timezone.utc).timestamp()
    reminders = _load()
    active    = []
    for r in reminders:
        if r.get("interval_seconds"):
            while r["due_ts"] <= now:
                r["due_ts"] += r["interval_seconds"]
            active.append(r)
            _reminder_tasks[r["id"]] = asyncio.create_task(_task(r))
        elif r["due_ts"] > now:
            active.append(r)
            _reminder_tasks[r["id"]] = asyncio.create_task(_task(r))
    _save(active)
    if active:
        _log.info(f"{len(active)} reminder(s) restored")


# ── Plugin ────────────────────────────────────────────────────────────────────

class RemindersPlugin(Plugin):
    INTENTS = ["REMINDER", "REMINDER_LIST", "REMINDER_DELETE"]

    INTENT_PREFIXES = {
        "REMINDER":        "REMINDER:",
        "REMINDER_DELETE": "REMINDER_DELETE:",
    }

    INTENT_LINES = [
        "REMINDER_LIST – eigene Erinnerungen anzeigen\n",
        "REMINDER_DELETE: <id> – Erinnerung per ID löschen\n",
        "REMINDER: <sekunden_bis_erste>:<intervall_sekunden>:<nachricht> – Erinnerung setzen "
        "(Intervall 0=einmalig, 604800=wöchentlich, 86400=täglich)\n",
    ]

    intent_order = 50

    async def on_ready(self) -> None:
        _restore()

    async def handle(self, ctx: MessageContext) -> None:
        if ctx.intent == "REMINDER_LIST":
            reminders = _list(ctx.message.author.id, ctx.privileged)
            if not reminders:
                await ctx.message.reply("Keine aktiven Erinnerungen.")
                return
            lines = []
            for r in reminders:
                owner    = f"**{r.get('username', '?')}** – " if ctx.privileged else ""
                interval = f", dann alle {_fmt_duration(r['interval_seconds'])}" if r.get("interval_seconds") else ""
                mode_tag = " 🤖" if r.get("mode") == "prompt" else ""
                lines.append(f"`[{r['id']}]`{mode_tag} {owner}\"{r['message']}\" – nächste: {_fmt_ts(r['due_ts'])}{interval}")
            header = "Alle aktiven Erinnerungen:" if ctx.privileged else "Deine aktiven Erinnerungen:"
            await ctx.message.reply(header + "\n" + "\n".join(lines))

        elif ctx.intent == "REMINDER_DELETE":
            rid = ctx.extra.strip().strip("`[]")
            if _delete(rid, ctx.message.author.id, ctx.privileged):
                await ctx.message.reply(f"Erinnerung `{rid}` gelöscht.")
            else:
                await ctx.message.reply(f"Keine Erinnerung mit ID `{rid}` gefunden – oder sie gehört dir nicht.")

        elif ctx.intent == "REMINDER":
            parts = ctx.extra.split(":", 2)
            if len(parts) == 3:
                try:
                    sec_until  = int(re.sub(r"[^\d]", "", parts[0]))
                    interval   = int(re.sub(r"[^\d]", "", parts[1]))
                    remind_msg = parts[2].strip()
                    mode       = await _classify_mode(remind_msg)
                    rid        = _add(ctx.message.channel.id, ctx.message.author.id,
                                      ctx.message.author.display_name, remind_msg,
                                      sec_until, interval, mode)
                    time_str  = _fmt_duration(sec_until)
                    mode_hint = "*(ich generiere dann eine Antwort)*" if mode == "prompt" else "*(Erinnerungstext)*"
                    if interval:
                        reply_txt = f'Mach ich `[{rid}]` {mode_hint}. Erste Ausführung in {time_str}, dann alle {_fmt_duration(interval)}: "{remind_msg}"'
                    else:
                        reply_txt = f'Mach ich `[{rid}]` {mode_hint}. In {time_str}: "{remind_msg}"'
                    await ctx.message.reply(reply_txt)
                    return
                except (ValueError, IndexError):
                    pass
            await ctx.message.reply("Mit der Zeit hab ich's nicht so. Sag mir genauer wann.")


def setup(registry) -> None:
    registry.register(RemindersPlugin())
