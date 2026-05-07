"""CDU shit counter plugin — pure Python, no API cost."""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from plugins.base import Plugin, MessageContext, _read, _write, split_message

_log = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_CDU_FILE = _DATA_DIR / "cdu_counter.json"

_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))

_CDU_RE         = re.compile(r'\bcdu\b', re.IGNORECASE)
_CDU_RESET_RE   = re.compile(r'\b(reset|resettet|zurücksetzen|neustart|neu)\b', re.IGNORECASE)
_CDU_HISTORY_RE = re.compile(r'\b(protokoll|verlauf|history|liste)\b', re.IGNORECASE)


def _fmt_hm(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return "weniger als 1 Minute"
    if s < 3600:
        return f"{s // 60} Minute(n)"
    if s < 86400:
        h, m = s // 3600, (s % 3600) // 60
        return f"{h}h {m}min" if m else f"{h}h"
    d, h = s // 86400, (s % 86400) // 3600
    return f"{d}T {h}h" if h else f"{d} Tag(e)"


def _cdu_reset(reason: str) -> str:
    entries = _read(_CDU_FILE)
    entries.append({"ts": datetime.now(timezone.utc).timestamp(), "reason": reason})
    _write(_CDU_FILE, entries)
    total = len(entries)
    return f"💩 **CDU Scheiße Counter resettet** (#{total})\nGrund: _{reason}_\nTimer läuft wieder."


def _cdu_status() -> str:
    entries = _read(_CDU_FILE)
    if not entries:
        return "💩 CDU Scheiße Counter wurde noch nie gestartet."
    last    = entries[-1]
    elapsed = datetime.now(timezone.utc).timestamp() - last["ts"]
    started = datetime.fromtimestamp(last["ts"], tz=_TZ).strftime("%d.%m.%Y %H:%M")
    return (
        f"💩 **CDU Scheiße Counter**\n"
        f"Läuft seit: **{_fmt_hm(elapsed)}**\n"
        f"Grund des letzten Resets: _{last['reason']}_\n"
        f"Gestartet: {started}  |  Resets gesamt: {len(entries)}"
    )


def _cdu_history() -> str:
    entries = _read(_CDU_FILE)
    if not entries:
        return "💩 Noch keine Resets aufgezeichnet."
    now_ts = datetime.now(timezone.utc).timestamp()
    lines  = [f"💩 **CDU Scheiße Counter – Protokoll** ({len(entries)} Resets)\n"]
    for i, entry in enumerate(entries):
        dt_str = datetime.fromtimestamp(entry["ts"], tz=_TZ).strftime("%d.%m.%Y %H:%M")
        if i + 1 < len(entries):
            dur_str = f"Dauer: {_fmt_hm(entries[i + 1]['ts'] - entry['ts'])}"
        else:
            dur_str = f"noch laufend: {_fmt_hm(now_ts - entry['ts'])}"
        lines.append(f"**[{i + 1}]** {dt_str} – {dur_str}")
        lines.append(f"    _{entry['reason']}_")
    return "\n".join(lines)


class CduPlugin(Plugin):
    INTENTS = ["CDU"]

    INTENT_LINES = []  # pure Python — never goes to Haiku

    intent_order = 5  # run before everything else

    def pre_classify(self, clean: str) -> tuple[str, str] | None:
        if _CDU_RE.search(clean):
            return "CDU", clean
        return None

    async def handle(self, ctx: MessageContext) -> None:
        clean = ctx.extra  # original clean text passed from pre_classify
        if _CDU_RESET_RE.search(clean):
            m      = _CDU_RESET_RE.search(clean)
            reason = clean[m.end():].strip().lstrip(",:;– ")
            if not reason:
                await ctx.message.reply("Sag mir den Grund für den Reset.")
                return
            await ctx.message.reply(_cdu_reset(reason))
        elif _CDU_HISTORY_RE.search(clean):
            chunks = split_message(_cdu_history())
            await ctx.message.reply(chunks[0])
            for chunk in chunks[1:]:
                await ctx.message.channel.send(chunk)
        else:
            await ctx.message.reply(_cdu_status())


def setup(registry) -> None:
    registry.register(CduPlugin())
