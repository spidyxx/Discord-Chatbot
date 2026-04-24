"""Help plugin — HELP intent."""

import logging
import os

from plugins.base import Plugin, MessageContext
from version import BOT_VERSION

_log = logging.getLogger(__name__)

_BOT_NAME        = os.environ.get("BOT_NAME",        "Marvin")
_COOLDOWN        = int(os.environ.get("COOLDOWN_SECONDS", "120"))

def _model(tier_var: str, tier_default: str, model_var: str, model_default: str) -> str:
    tier = os.environ.get(tier_var, tier_default)
    models = {
        "local":     os.environ.get("LOCAL_MODEL",     ""),
        "cheap":     os.environ.get("CHEAP_MODEL",     "claude-haiku-4-5-20251001"),
        "normal":    os.environ.get("NORMAL_MODEL",    "claude-sonnet-4-6"),
        "expensive": os.environ.get("EXPENSIVE_MODEL", "claude-sonnet-4-6"),
    }
    return f"{models.get(tier, tier)} ({tier})"


def build_help_text() -> str:
    n = _BOT_NAME
    return f"""**Was ich kann:**

💬 **Allgemein** *(alle Kanäle)*
Ich beantworte Fragen, suche im Web und erkenne Bilder – immer auf @Mention.
In Hauptkanälen mische ich mich von selbst ein und nutze gespeichertes Hintergrundwissen.

⏰ **Erinnerungen** *(alle Kanäle)*
`@{n} erinnere mich in 2 Stunden an Meeting` – einmalige Benachrichtigung
`@{n} erzähl mir jeden Tag um 13 Uhr einen Witz` – wiederkehrende Aufgabe (ich generiere dann eine Antwort)
`@{n} erinnere uns jeden Freitag um 20 Uhr an ...` – wiederkehrende Benachrichtigung
`@{n} zeig meine Erinnerungen` – listet deine aktiven Erinnerungen (🤖 = Aufgabe, kein Text)
`@{n} lösche Erinnerung [ID]` – löscht eine bestimmte Erinnerung

📋 **Zusammenfassung** *(alle Kanäle)*
`@{n} fass zusammen` – fasst die letzten Nachrichten zusammen
`@{n} fass dieses Video zusammen <youtube-url>` – fasst ein YouTube-Video zusammen

🔇 **Stummschalten** *(alle Kanäle)*
`@{n} shut up` *(oder ähnliches)* – ich schweige
`@{n}` *(irgendwas)* – reaktiviert mich wieder

💩 **CDU Scheiße Counter** *(alle Kanäle)*
`@{n} CDU reset <Grund>` – Counter zurücksetzen mit Begründung
`@{n} CDU` – aktuellen Stand anzeigen (Zeit seit letztem Reset)
`@{n} CDU Protokoll` – vollständige Reset-Historie

🔒 **Admins & Mods**
`@{n} was weißt du alles?` – alle gespeicherten Fakten anzeigen
`@{n} vergiss dass ...` – bestimmten Eintrag löschen
`@{n} speichere was heute passiert ist` – Session als strukturierte Fakten speichern

⚙️ **Bot-Konfiguration**
Cooldown: `{_COOLDOWN}s`
Hauptkanal: `{_model('MAIN_TIER', 'expensive', 'EXPENSIVE_MODEL', 'claude-sonnet-4-6')}`
Mention-Kanal: `{_model('MENTION_TIER', 'normal', 'NORMAL_MODEL', 'claude-sonnet-4-6')}`
Klassifizierung: `{_model('CLASSIFY_TIER', 'cheap', 'CHEAP_MODEL', 'claude-haiku-4-5-20251001')}`
Emoji: `{_model('EMOJI_TIER', 'cheap', 'CHEAP_MODEL', 'claude-haiku-4-5-20251001')}`
Memory-Filter: `{_model('MEMORY_FILTER_TIER', 'cheap', 'CHEAP_MODEL', 'claude-haiku-4-5-20251001')}`
Proaktiv: `{_model('PROACTIVE_TIER', 'expensive', 'EXPENSIVE_MODEL', 'claude-sonnet-4-6')}`
Digest: `{_model('DIGEST_SUMMARY_TIER', 'expensive', 'EXPENSIVE_MODEL', 'claude-sonnet-4-6')}` / `{_model('DIGEST_FACTS_TIER', 'normal', 'NORMAL_MODEL', 'claude-sonnet-4-6')}`

`v{BOT_VERSION}`"""


class HelpPlugin(Plugin):
    INTENTS = ["HELP"]

    INTENT_LINES = [
        "HELP – Nutzer fragt was der Bot kann\n",
    ]

    intent_order = 90  # just before RESPOND (which is always last in the footer)

    async def handle(self, ctx: MessageContext) -> None:
        await ctx.message.reply(build_help_text())


def setup(registry) -> None:
    registry.register(HelpPlugin())
