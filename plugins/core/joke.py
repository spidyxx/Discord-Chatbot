"""Daily joke plugin — deterministic joke from a fixed list, no Claude.

Commands (@mention):
  JOKE              – tell a joke right now
  JOKE_ON           – enable the daily joke
  JOKE_OFF          – disable the daily joke
  JOKE_TIME: HH:MM  – set the daily joke time (default 18:00)

Config persists in DATA_DIR/joke_config.json:
  {"enabled": bool, "hour": int, "minute": int, "last_index": int}
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from plugins.base import Plugin, MessageContext, _read, _write
from plugins import state as bot_state

_log = logging.getLogger(__name__)

_DATA_DIR    = Path(os.environ.get("DATA_DIR", "/app/data"))
_CONFIG_FILE = _DATA_DIR / "joke_config.json"
_TZ          = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))

# "told" holds the line-indices (0-based) of jokes already used this cycle.
# Append new jokes to the END of jokes.txt — that keeps existing indices stable.
# Reordering/deleting existing lines mid-cycle can shift tracking by one cycle.
_DEFAULT_CONFIG = {"enabled": True, "hour": 18, "minute": 0, "told": []}

# ── Joke list ───────────────────────────────────────────────────────────────────
# Jokes live in jokes.txt at the repo root (one per line, # for comments) — same
# pattern as statuses.txt, so deploy.sh seeds it once then preserves server edits.
_JOKES_FILE = Path(__file__).resolve().parents[2] / "jokes.txt"


def _load_jokes() -> list[str]:
    if not _JOKES_FILE.exists():
        _log.warning(f"jokes.txt not found at {_JOKES_FILE}")
        return ["Mir fällt gerade kein Witz ein."]
    jokes = [
        line.strip()
        for line in _JOKES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return jokes or ["Mir fällt gerade kein Witz ein."]


JOKES = _load_jokes()

# ── Config I/O ──────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    data = _read(_CONFIG_FILE)
    cfg = dict(_DEFAULT_CONFIG)
    if isinstance(data, dict):
        cfg.update(data)
    return cfg

def _save_cfg(cfg: dict) -> None:
    _write(_CONFIG_FILE, cfg)


# ── Joke picking ────────────────────────────────────────────────────────────────

def _pick_joke() -> str:
    """Return a joke not told in the current cycle. Once every joke has been
    used, the cycle resets and all jokes become available again — so nothing
    repeats until the whole list is exhausted. Persists progress to config."""
    cfg  = _load_cfg()
    n    = len(JOKES)
    # Keep only valid indices (drops stale ones if jokes.txt shrank).
    told = [i for i in cfg.get("told", []) if 0 <= i < n]

    remaining = [i for i in range(n) if i not in told]
    if not remaining:                 # whole list exhausted → start a fresh cycle
        last      = told[-1] if told else None
        told      = []
        remaining = [i for i in range(n) if i != last] or list(range(n))  # avoid back-to-back

    idx = random.choice(remaining)
    told.append(idx)
    cfg["told"] = told
    _save_cfg(cfg)
    return JOKES[idx]


# ── Scheduler ───────────────────────────────────────────────────────────────────

_scheduler_task: asyncio.Task | None = None


def _seconds_until(hour: int, minute: int) -> float:
    now    = datetime.now(_TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _fire() -> None:
    joke = _pick_joke()
    for channel_id in bot_state.main_channel_ids:
        channel = bot_state.bot.get_channel(channel_id) if bot_state.bot else None
        if channel:
            await channel.send(joke)
    _log.info(f"Daily joke posted: {joke[:60]}")


async def _scheduler() -> None:
    while True:
        cfg   = _load_cfg()
        delay = _seconds_until(cfg["hour"], cfg["minute"])
        await asyncio.sleep(delay)
        cfg = _load_cfg()  # reload — config may have changed during the sleep
        if cfg.get("enabled") and not bot_state.muted:
            try:
                await _fire()
            except Exception:
                _log.exception("Daily joke failed to send")


def _restart_scheduler() -> None:
    """(Re)start the scheduler so config changes (e.g. new time) take effect."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = asyncio.create_task(_scheduler())


# ── Plugin ────────────────────────────────────────────────────────────────────

class JokePlugin(Plugin):
    # Order matters: classify_intent matches prefixes via startswith in this
    # order, so the longer/specific labels must come before the bare "JOKE".
    INTENTS = ["JOKE_ON", "JOKE_OFF", "JOKE_TIME", "JOKE"]

    INTENT_PREFIXES = {
        "JOKE_TIME": "JOKE_TIME:",
    }

    INTENT_LINES = [
        "JOKE – Nutzer möchte JETZT einen Witz hören\n",
        "JOKE_ON – täglichen Witz aktivieren\n",
        "JOKE_OFF – täglichen Witz deaktivieren\n",
        "JOKE_TIME: <HH:MM> – Uhrzeit für den täglichen Witz festlegen\n",
    ]

    intent_order = 55

    async def on_ready(self) -> None:
        _restart_scheduler()
        cfg = _load_cfg()
        _log.info(f"Joke scheduler started (enabled={cfg['enabled']}, "
                  f"time={cfg['hour']:02d}:{cfg['minute']:02d})")

    async def handle(self, ctx: MessageContext) -> None:
        if ctx.intent == "JOKE":
            await ctx.message.reply(_pick_joke())
            return

        # Config changes are admin/mod only
        if not ctx.privileged:
            await ctx.message.reply("Das können nur Admins und Mods.")
            return

        cfg = _load_cfg()

        if ctx.intent == "JOKE_ON":
            cfg["enabled"] = True
            _save_cfg(cfg)
            _restart_scheduler()
            await ctx.message.reply(
                f"Täglicher Witz ist an – jeden Tag um {cfg['hour']:02d}:{cfg['minute']:02d}."
            )

        elif ctx.intent == "JOKE_OFF":
            cfg["enabled"] = False
            _save_cfg(cfg)
            _restart_scheduler()
            await ctx.message.reply("Täglicher Witz ist aus.")

        elif ctx.intent == "JOKE_TIME":
            parsed = _parse_time(ctx.extra)
            if parsed is None:
                await ctx.message.reply("Sag mir die Uhrzeit als `HH:MM`, z.B. `18:00`.")
                return
            cfg["hour"], cfg["minute"] = parsed
            _save_cfg(cfg)
            _restart_scheduler()
            state = "an" if cfg["enabled"] else "aus (gerade deaktiviert)"
            await ctx.message.reply(
                f"Täglicher Witz jetzt um {cfg['hour']:02d}:{cfg['minute']:02d} – {state}."
            )


def _parse_time(text: str) -> tuple[int, int] | None:
    """Parse 'HH:MM', 'HH.MM', 'HH Uhr', or bare 'HH' into (hour, minute)."""
    import re
    m = re.search(r"(\d{1,2})\s*(?:[:.]|\s*uhr|h)?\s*(\d{2})?", text.lower())
    if not m:
        return None
    hour   = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def setup(registry) -> None:
    registry.register(JokePlugin())
