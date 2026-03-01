# telegram-bot

Telegram bot running on the Raspberry Pi. Handles 5 bot instances (default Claude, Haiku, Sonnet, Opus, Codex) via a single script with per-instance .env files.

## Service
- `claude-bot.service` — default bot (.env)
- `claude-bot-haiku.service` — Haiku variant (.env-haiku)
- `claude-bot-sonnet.service` — Sonnet variant (.env-sonnet)
- `claude-bot-opus.service` — Opus variant (.env-opus)
- `claude-bot-codex.service` — Codex variant (.env-codex)

Restart: `sudo systemctl restart claude-bot.service`
Logs: `journalctl -u claude-bot.service -f` or `tail -f /home/jaredgantt/telegram-bot/bot.log`

## Files
- `claude_bot.py` — main bot entry point
- `runner.py` — Claude CLI runner (shared with pi-server — keep in sync if modified)
- `claude_runner.py` — Sonnet/Haiku/Opus variant
- `codex_runner.py` — Codex variant (shared with pi-server — keep in sync)
- `route_state.py` — routing state between claude/codex (shared with pi-server — keep in sync)
- `notify.py` — notification utilities
- `session_watcher.py` — session monitoring

## Shared code note
`runner.py`, `codex_runner.py`, `route_state.py` are duplicated in `pi-server/`. If you modify one, update the other. A shared library is a future improvement.

## Data
All conversation history → `/home/jaredgantt/data/history/`
Voice transcription via Whisper (local).
