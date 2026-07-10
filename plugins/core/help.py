"""Help plugin — HELP intent."""

import logging
import os

from plugins.base import Plugin, MessageContext, split_message
from version import BOT_VERSION

_log = logging.getLogger(__name__)

_BOT_NAME        = os.environ.get("BOT_NAME",        "Marvin")
_COOLDOWN        = int(os.environ.get("COOLDOWN_SECONDS", "120"))

def _model(tier_var: str, tier_default: str) -> str:
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
Ich beantworte Fragen, suche im Web, erkenne Bilder und lese verlinkte Artikel automatisch – immer auf @Mention.
In Hauptkanälen mische ich mich von selbst ein, nutze gespeichertes Hintergrundwissen und poste abends einen Tagesrückblick, wenn was los war.

⏰ **Erinnerungen** *(alle Kanäle)*
`@{n} erinnere mich in 2 Stunden an Meeting` – einmalige Benachrichtigung
`@{n} erzähl mir jeden Tag um 13 Uhr einen Witz` – wiederkehrende Aufgabe (ich generiere dann eine Antwort)
`@{n} erinnere uns jeden Freitag um 20 Uhr an ...` – wiederkehrende Benachrichtigung
`@{n} zeig meine Erinnerungen` – listet deine aktiven Erinnerungen (🤖 = Aufgabe, kein Text)
`@{n} lösche Erinnerung [ID]` – löscht eine bestimmte Erinnerung

📋 **Zusammenfassung** *(alle Kanäle)*
`@{n} fass zusammen` – fasst die letzten Nachrichten zusammen
`@{n} fass dieses Video zusammen <youtube-url>` – fasst ein YouTube-Video zusammen
`@{n} fass diese Episode zusammen <ardsounds.de-url>` – transkribiert + fasst eine Podcast-Episode zusammen

🎵 **Songtexte** *(alle Kanäle)*
`@{n} zeig mir den Songtext zu <Künstler> - <Titel>` – holt den Songtext (Format mit Bindestrich)
`@{n} <genius.com-url>` – Songtext direkt von einer Genius-URL

😂 **Witze** *(alle Kanäle)*
`@{n} erzähl einen Witz` – Witz aus der Liste, sofort
`@{n} täglichen Witz an` / `aus` – täglichen Witz ein-/ausschalten *(Admins/Mods)*
`@{n} täglichen Witz um 18:00` – Uhrzeit festlegen *(Admins/Mods)*

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
Hauptkanal: `{_model('MAIN_TIER', 'expensive')}`
Mention-Kanal: `{_model('MENTION_TIER', 'normal')}`
Klassifizierung: `{_model('CLASSIFY_TIER', 'cheap')}`
Emoji-Reaktionen: `Keyword-Map (kein API-Call)`
Memory-Filter: `{_model('MEMORY_FILTER_TIER', 'cheap')}`
Proaktiv: `{_model('PROACTIVE_TIER', 'expensive')}`
Digest: `{_model('DIGEST_SUMMARY_TIER', 'expensive')}`

`v{BOT_VERSION}`"""


def capabilities_block(vision: bool = True, web_search: bool = True) -> str:
    """Model-facing summary of the bot's abilities and limits, injected into the
    system prompt so the bot can answer "kannst du X?" correctly instead of
    guessing. Keep in sync with build_help_text() when features change.

    vision/web_search reflect the ACTIVE model's capabilities (providers.
    caps_for_model) — a Gemini/DeepSeek/Ollama-backed tier must not be told
    it can see images or search the web when it can't."""
    n = _BOT_NAME

    web_line = "- Webseiten: verlinkte Artikel liest du automatisch"
    if web_search:
        web_line += "; du kannst auch im Web suchen"

    ability_lines = [web_line]
    limit_lines = []
    if vision:
        ability_lines.append("- Bilder: kannst du sehen und beschreiben — bei unscharfen/kleinen Bildern nichts dazuerfinden")
    else:
        limit_lines.append("- Bilder kannst du NICHT sehen — sag ehrlich, dass du sie nicht sehen kannst")
    if not web_search:
        limit_lines.append("- Im Web suchen kannst du NICHT — sag das ehrlich, wenn aktuelle Infos gefragt sind")

    return f"""Deine Funktionen (Befehle funktionieren per @{n}-Mention):
- Erinnerungen: einmalig ("erinnere mich in 2 Stunden an ...") und wiederkehrend ("... jeden Freitag um 20 Uhr"); anzeigen ("zeig meine Erinnerungen") und löschen ("lösche Erinnerung [ID]")
- Zusammenfassungen: Chatverlauf ("fass zusammen"), YouTube-Videos (nur mit Untertiteln — du liest das Transkript, kein echtes Video-Verständnis) und ARD-Sounds-Podcast-Episoden (per Link)
- Songtexte: "zeig mir den Songtext zu <Künstler> - <Titel>" oder per genius.com-Link
- Witze: auf Zuruf; täglicher Witz an/aus/Uhrzeit (nur Admins/Mods)
- Stummschalten: "shut up" o.ä.; jede weitere Mention weckt dich wieder
- CDU-Counter: "CDU" (Stand), "CDU reset <Grund>", "CDU Protokoll"
- Gedächtnis: du merkst dir Fakten über Nutzer und den Server; Admins/Mods können Einträge ansehen ("was weißt du alles?"), löschen ("vergiss dass ...") und den Tag als Fakten speichern ("speichere was heute passiert ist")
{chr(10).join(ability_lines)}
- "/help" bzw. "was kannst du?" zeigt Nutzern die vollständige Befehlsliste

Deine Grenzen (nicht behaupten, dass du es kannst):
- Dateianhänge außer Bildern (PDF, Word, Audio ...) siehst du NICHT
- Weitergeleitete Discord-Nachrichten kannst du nicht lesen
- YouTube-Videos ohne Untertitel kannst du nicht zusammenfassen
- Keine Sprachkanäle, keine DMs, kein Erstellen von Bildern
{chr(10).join(limit_lines) if limit_lines else ""}
Wenn jemand nach einer Funktion fragt, die es nicht gibt: sag das ehrlich und nenne ggf. die nächstliegende vorhandene Funktion."""


class HelpPlugin(Plugin):
    INTENTS = ["HELP"]

    INTENT_LINES = [
        "HELP – Nutzer fragt was der Bot kann\n",
    ]

    GATE_PATTERNS = [
        r"\bhilfe\b", r"\bhelp\b", r"was kannst", r"kannst du (?:alles|so)",
        r"funktionen", r"befehle", r"\bcommands\b", r"anleitung",
    ]

    intent_order = 90  # just before RESPOND (which is always last in the footer)

    async def handle(self, ctx: MessageContext) -> None:
        chunks = split_message(build_help_text())
        await ctx.message.reply(chunks[0])
        for chunk in chunks[1:]:
            await ctx.message.channel.send(chunk)


def setup(registry) -> None:
    registry.register(HelpPlugin())
