# Marvin — Discord Bot powered by Claude

A self-hosted Discord bot that uses the Anthropic Claude API to participate in server conversations, answer questions, summarize content, manage reminders, and maintain a persistent memory of server facts.

## Features

- **Conversational AI** — Responds to @mentions in any channel; in configured main channels, joins conversations autonomously when relevant
- **Memory system** *(main channels only)* — Stores and recalls facts about server members; relevance-filtered so only contextually matching memories are injected
- **YouTube summarization** — Summarize any YouTube video by sharing or replying to a URL
- **Reminders** — One-time and recurring reminders with natural language scheduling
- **Quotes** — Save and retrieve random quotes from the server
- **Chat summary** — Summarize what happened since you were last online
- **Session snapshot** — Save today's personality, running gags, and dynamics as a memory entry
- **Daily digest** — Automatic nightly summary posted to configured channels
- **Web search** — Built-in web search via Anthropic's web search tool
- **Image recognition** — Understands images and link previews attached to messages
- **Mute/unmute** — Silence the bot with a natural language command
- **Proactive messages** — Occasionally starts a conversation by picking up an unresolved topic or trailing discussion, within configurable hours and only when the channel has been quiet for a while

## Requirements

- Python 3.11+
- Discord bot token
- Anthropic API key

## Quick Start (Docker)

```bash
docker run -d \
  -e DISCORD_TOKEN=your_token \
  -e ANTHROPIC_API_KEY=your_key \
  -e MAIN_CHANNEL_IDS=123456789,987654321 \
  -v /path/to/data:/app/data \
  -v /path/to/logs:/app/logs \
  your-image-name
```

## Configuration

All configuration is via environment variables.

### Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your Discord bot token |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

### Bot Identity

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `Marvin` | Display name of the bot |
| `SYSTEM_PROMPT` | Neutral assistant | System prompt for non-main channels |
| `MAIN_SYSTEM_PROMPT` | Same as `SYSTEM_PROMPT` | System prompt for main channels (supports full personality) |

### Channels & Models

| Variable | Default | Description |
|---|---|---|
| `MAIN_CHANNEL_IDS` | *(none)* | Comma-separated channel IDs where the bot participates actively and uses memory |
| `MAIN_MODEL` | Same as `CLAUDE_MODEL` | Model used in main channels |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Model used in non-main channels |
| `CHEAP_MODEL` | `claude-haiku-4-5-20251001` | Model used for intent classification and memory filtering |

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `COOLDOWN_SECONDS` | `120` | Minimum seconds between passive responses in main channels |
| `CONTEXT_WINDOW` | `50` | Number of recent messages fetched as conversation context |
| `EMOJI_REACTION_RATE` | `0.20` | Probability (0–1) of adding an emoji reaction to skipped messages |
| `SUMMARY_WINDOW` | `30` | Message window for chat summaries |
| `FLAVOR_COOLDOWN_HOURS` | `6` | Minimum hours before the same flavor memory is injected again |
| `MOD_ROLE_NAMES` | `Mod,Admin` | Comma-separated role names with elevated memory permissions |

### Proactive Messages

| Variable | Default | Description |
|---|---|---|
| `PROACTIVE_ENABLED` | `true` | Enable/disable proactive conversation starter |
| `PROACTIVE_HOUR_START` | `15` | Earliest hour (local time) the bot may send a proactive message |
| `PROACTIVE_HOUR_END` | `23` | Latest hour |
| `PROACTIVE_SILENCE_MINUTES` | `45` | Minutes of channel silence required before the bot considers speaking |
| `PROACTIVE_COOLDOWN_HOURS` | `4` | Minimum hours between proactive messages per channel |
| `PROACTIVE_CHECK_MINUTES` | `15` | How often the bot checks whether to send a proactive message |

### Daily Digest

| Variable | Default | Description |
|---|---|---|
| `DIGEST_ENABLED` | `true` | Enable/disable automatic daily digest |
| `DIGEST_HOUR` | `23` | Hour (local time) to post the digest |
| `DIGEST_MINUTE` | `0` | Minute to post the digest |
| `TIMEZONE` | `UTC` | Timezone for digest scheduling and timestamps (e.g. `Europe/Berlin`) |

### Storage

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/app/data` | Directory for persistent data (memories, quotes, reminders) |
| `LOG_DIR` | `/app/logs` | Directory for log files (rotated daily, 30-day retention) |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Commands

All commands require an @mention unless the bot addresses you directly.

### Memory *(main channels only)*

| Command | Description |
|---|---|
| `@Marvin was weißt du alles?` | List stored facts |
| `@Marvin vergiss alles von mir` | Delete all your own memory entries |
| `@Marvin vergiss dass ...` | Delete a specific memory entry |
| `@Marvin speichere was heute passiert ist` | Snapshot today's session as a memory entry |

### Reminders

| Command | Description |
|---|---|
| `@Marvin erinnere mich in 2 Stunden an ...` | One-time reminder |
| `@Marvin erinnere uns jeden Freitag um 20 Uhr an ...` | Recurring reminder |
| `@Marvin zeig meine Erinnerungen` | List your active reminders |
| `@Marvin lösche Erinnerung [ID]` | Delete a reminder by ID |

### Quotes

| Command | Description |
|---|---|
| Reply to a message + `@Marvin merke dieses Zitat` | Save the quoted message |
| `@Marvin zeig ein Zitat` | Show a random saved quote |

### Summaries

| Command | Description |
|---|---|
| `@Marvin fass zusammen` | Summarize today's chat |
| `@Marvin fass dieses Video zusammen <youtube-url>` | Summarize a YouTube video (requires captions) |
| Reply to a YouTube link + `@Marvin fass das zusammen` | Summarize a video from a replied-to message |

### Other

| Command | Description |
|---|---|
| `@Marvin shut up` | Mute the bot |
| `@Marvin` *(anything)* | Unmute the bot |
| `@Marvin hilf mir` | Show help |

## Architecture Notes

- **Plugin system**: Features are implemented as plugins in `plugins/core/`. Drop a new file with a `setup()` function there and it is auto-discovered on startup — no changes to `bot.py` needed. See `CLAUDE.md` for the full plugin authoring guide.
- **Memory** is stored as a flat JSON file with typed entries (`bot`, `user`, `flavor`, `general`). Candidate memories are pre-filtered by type and speaker, then a Haiku call decides which trigger/general entries are actually relevant to the current message before injection.
- **Main vs. other channels**: Main channels get full personality, memory injection, and autonomous participation (debounced). Other channels respond only to @mentions — with channel history as context but without memory injection.
- **Prompt caching**: System prompt and conversation history are cached via Anthropic's prompt caching API, significantly reducing input token costs on repeated turns.
- **Question tracking**: When the bot ends a message with a question, the next user reply bypasses the cooldown and the relevance check, ensuring the answer is always processed.

## Dependencies

```
discord.py==2.3.2
anthropic>=0.49.0
aiohttp>=3.9.0
Pillow>=10.0.0
youtube-transcript-api>=0.6.0
```
