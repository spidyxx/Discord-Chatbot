# Plugin Migration Status

## Migrated to plugins/

| Feature | Plugin file | Intents |
|---|---|---|
| Quotes | `plugins/core/quotes.py` | `QUOTE_SAVE`, `QUOTE_GET` |

## Still in bot.py

| Feature | Intent(s) |
|---|---|
| Mute/unmute | `MUTE` |
| Memory list | `MEMORY_LIST` |
| Memory delete | `MEMORY_DELETE` |
| Reminder list | `REMINDER_LIST` |
| Reminder delete | `REMINDER_DELETE` |
| Set reminder | `REMINDER` |
| YouTube summary | `YOUTUBE_SUMMARY` |
| Chat summary | `SUMMARY` |
| Session snapshot | `SNAPSHOT` |
| Help | `HELP` |
| General response | `RESPOND` |
| Passive response loop | `_try_respond` (no intent) |
| CDU counter | (pre-intent regex, no classify call) |

## Dead code in bot.py (safe to remove in a follow-up)

- `load_quotes`, `save_quotes`, `add_quote`, `get_random_quote`, `QUOTES_FILE`
  (superseded by `plugins/core/quotes.py`; only remaining usage is the `on_ready` log line)
