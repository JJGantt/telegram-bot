# telegram-bot

Telegram bot for Claude. Runs as a systemd service on the Raspberry Pi and polls Telegram for messages.

## Overview

**telegram-bot** is a multi-instance bot system. Several bot instances can run simultaneously, each with its own:
- Telegram bot token
- Model preference (Claude, Codex, etc.)
- Hardwired or shared model/mode state

Perfect for having different bots with different personalities or capabilities on the same Pi.

## Usage

Each bot is launched with an optional `.env` file that overrides defaults:

```bash
# Main Sonnet bot (uses shared mode/model state)
python3 claude_bot.py

# Opus bot (hardwired to Opus)
python3 claude_bot.py .env-opus

# Haiku bot (hardwired to Haiku)
python3 claude_bot.py .env-haiku
```

## Features

- Text messages → Claude
- Voice messages → Whisper transcription → Claude
- Mode switching (`/claude`, `/codex`) — persisted to [route_state](https://github.com/JJGantt/claude-runner)
- Model switching (`/sonnet`, `/opus`, `/haiku`) — hardwired or shared
- All exchanges logged to shared history

## Configuration

Each bot has a `.env` file with:
- `BOT_TOKEN` — Telegram bot token
- `AUTHORIZED_USER_ID` — Only this user can use the bot
- `WHISPER_MODEL` — Whisper size (tiny, base, small, medium, large-v3)
- `CLAUDE_MODEL` — (optional) Hardwire to specific model
- `ROUTE_MODE` — (optional) Hardwire to mode (claude or codex)
- `BOT_SOURCE` — Identifier for history logging (e.g., "opus-telegram")

## Service

Each bot runs as its own systemd service:

```bash
systemctl start claude-bot-sonnet
systemctl start claude-bot-opus
systemctl status telegram-bot
```

## Related

- **Executor:** [claude-runner](https://github.com/JJGantt/claude-runner) — Core Claude/Codex logic
- **Server:** [pi-server](https://github.com/JJGantt/pi-server) — HTTP interface
- **History:** [mcp-history](https://github.com/JJGantt/mcp-history) — Conversation logging
