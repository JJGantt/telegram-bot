#!/usr/bin/env python3
"""
Consolidated Telegram bot — runs ALL bots in a single process.

Usage:
    python3 multi_bot.py              # Run all bots
    python3 multi_bot.py --bots .env .env-opus   # Run specific bots only

Whisper backends (set WHISPER_BACKEND in .env):
    auto  — use local only when Pi is idle; fall back to API otherwise (default)
    local — always use the shared ai-terminal-server faster-whisper helper
    api   — always use OpenAI Whisper API
"""

import asyncio
import html
import logging
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

sys.path.insert(0, "/home/jaredgantt/claude-runner")
from runner import run_claude
from codex_runner import run_codex
from route_state import get_mode, set_mode, get_model, set_model, apply_switch_and_strip

logging.basicConfig(
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("multi_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

TEMP_FILE_PREFIX = "claude-bot-audio-"
TEMP_MAX_AGE_SECONDS = 24 * 60 * 60

# ---------------------------------------------------------------------------
# Whisper config — read from main .env at startup
# ---------------------------------------------------------------------------

def _read_whisper_config() -> tuple[str, str, float]:
    """Return (backend, openai_api_key, load_threshold) from the main .env file."""
    env_path = Path(__file__).parent / ".env"
    vals = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    backend = vals.get("WHISPER_BACKEND", "auto").lower()
    api_key = vals.get("OPENAI_API_KEY", "")
    threshold = float(vals.get("WHISPER_LOAD_THRESHOLD", "0.5"))
    return backend, api_key, threshold

WHISPER_BACKEND, _OPENAI_API_KEY, _LOAD_THRESHOLD = _read_whisper_config()
log.info(f"Whisper backend: {WHISPER_BACKEND} (load threshold: {_LOAD_THRESHOLD})")
_SHARED_WHISPER_SCRIPT = Path("/home/jaredgantt/ai-terminal-server/scripts/transcribe_local.py")
_SHARED_WHISPER_MODEL = "tiny.en"
_SHARED_WHISPER_COMPUTE_TYPE = "int8"
_SHARED_WHISPER_BEAM_SIZE = "1"
_SHARED_WHISPER_TIMEOUT_S = 90

# Track how many claude subprocesses are currently running across all bots.
_active_claude_count = 0

# ---------------------------------------------------------------------------
# Shared local Whisper path
# ---------------------------------------------------------------------------


def _get_load_avg() -> float:
    """Return 1-minute system load average."""
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 99.0  # fail safe: assume under load


def _transcribe_local(file_path: str) -> str:
    if not _SHARED_WHISPER_SCRIPT.exists():
        raise RuntimeError(f"shared whisper helper missing: {_SHARED_WHISPER_SCRIPT}")

    result = subprocess.run(
        [
            sys.executable,
            str(_SHARED_WHISPER_SCRIPT),
            file_path,
            _SHARED_WHISPER_MODEL,
            _SHARED_WHISPER_COMPUTE_TYPE,
            _SHARED_WHISPER_BEAM_SIZE,
        ],
        capture_output=True,
        text=True,
        timeout=_SHARED_WHISPER_TIMEOUT_S,
        check=False,
    )
    text = (result.stdout or "").strip()
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "unknown error"
        raise RuntimeError(f"shared whisper helper failed: {stderr}")
    return text


def _transcribe_api(file_path: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=_OPENAI_API_KEY)
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(model="whisper-1", file=f, language="en")
    return result.text


def transcribe_voice(file_path: str) -> str:
    if WHISPER_BACKEND == "api":
        return _transcribe_api(file_path)
    if WHISPER_BACKEND == "local":
        return _transcribe_local(file_path)
    # "auto": use local only when Pi is idle
    load = _get_load_avg()
    if load <= _LOAD_THRESHOLD and _active_claude_count == 0:
        log.info(f"Whisper: using local (load={load:.2f}, active_claude={_active_claude_count})")
        try:
            return _transcribe_local(file_path)
        except Exception as e:
            log.warning(f"Local Whisper failed, falling back to API: {e}")
            return _transcribe_api(file_path)
    else:
        log.info(f"Whisper: using API (load={load:.2f}, active_claude={_active_claude_count})")
        return _transcribe_api(file_path)


# ---------------------------------------------------------------------------
# .env parser (no dotenv dependency, no global env pollution)
# ---------------------------------------------------------------------------

def parse_env_file(path: Path) -> dict:
    vals = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


# ---------------------------------------------------------------------------
# Bot config
# ---------------------------------------------------------------------------

class BotConfig:
    def __init__(self, env_file: str):
        self.env_file = env_file
        env = parse_env_file(Path(__file__).parent / env_file)
        self.token = env["BOT_TOKEN"]
        self.authorized_user_id = int(env["AUTHORIZED_USER_ID"])
        self.hardwired_model = env.get("CLAUDE_MODEL", "").strip().lower() or None
        self.hardwired_mode = env.get("ROUTE_MODE", "").strip().lower() or None
        self.bot_source = env.get("BOT_SOURCE", "pi-telegram").strip().lower()
        self.warnings_enabled = env.get("WARNINGS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
        # Propagate env vars the runner needs
        self.env_vars = {}
        if "OPENAI_API_KEY" in env:
            self.env_vars["OPENAI_API_KEY"] = env["OPENAI_API_KEY"]
        if "BOT_TOKEN" in env:
            self.env_vars["BOT_TOKEN"] = env["BOT_TOKEN"]

    @property
    def label(self) -> str:
        return self.env_file.replace(".env-", "").replace(".env", "main")


# ---------------------------------------------------------------------------
# Handler factory — creates handlers bound to a specific BotConfig
# ---------------------------------------------------------------------------

def make_handlers(cfg: BotConfig):
    """Return (command_handlers, message_handlers) bound to this bot config."""
    bot_log = logging.getLogger(f"bot.{cfg.label}")

    def is_authorized(update: Update) -> bool:
        return update.effective_user.id == cfg.authorized_user_id

    async def maybe_send_warning(update: Update, text: str):
        if cfg.warnings_enabled:
            await update.message.reply_text(text)
        else:
            bot_log.info(f"Suppressed warning: {text}")

    async def send_response_chunks(update: Update, text: str, chunk_size: int = 3800, parse_mode=None):
        if not text:
            await update.message.reply_text("(No response)")
            return
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            try:
                await update.message.reply_text(chunk, parse_mode=parse_mode)
            except Exception:
                if parse_mode:
                    # Telegram rejected the HTML — fall back to plain text
                    bot_log.warning("HTML parse failed, retrying as plain text")
                    await update.message.reply_text(chunk)
                else:
                    raise

    async def _process_message(update: Update, text: str):
        global _active_claude_count
        import os as _os
        # Set env vars the runner subprocess needs
        for k, v in cfg.env_vars.items():
            _os.environ[k] = v

        cleaned, new_mode, new_model = apply_switch_and_strip(text)

        if (new_mode or new_model) and (cfg.hardwired_model or cfg.hardwired_mode):
            what = cfg.hardwired_model or f"{cfg.hardwired_mode} mode"
            if not cleaned:
                await send_response_chunks(update, f"This bot is hardwired to {what}. Use the Pi bot to switch.")
                return

        else:
            if new_mode:
                set_mode(new_mode)
            if new_model:
                set_model(new_model)

        mode = cfg.hardwired_mode or (get_mode() if not cfg.hardwired_model else "claude")
        model = cfg.hardwired_model or get_model()

        if not cleaned and (new_mode or new_model):
            parts = []
            if new_mode:
                parts.append(f"mode \u2192 {mode}")
            if new_model:
                parts.append(f"model \u2192 {model}")
            response = "Switched: " + ", ".join(parts) + "."
        else:
            _active_claude_count += 1
            try:
                if mode == "codex":
                    response = await asyncio.to_thread(run_codex, cleaned, cfg.bot_source)
                else:
                    response = await asyncio.to_thread(run_claude, cleaned, cfg.bot_source, model)
            finally:
                _active_claude_count -= 1
        await send_response_chunks(update, response, parse_mode="HTML")

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            bot_log.info(f"Unauthorized message from {update.effective_user.id}")
            return
        text = update.message.text or ""
        bot_log.info(f"Incoming: {text[:100]}")
        try:
            await _process_message(update, text)
        except Exception as e:
            await maybe_send_warning(update, f"Error: {e}")
            bot_log.error(f"Error: {e}")

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        tmp_path = None
        try:
            voice_file = await update.message.voice.get_file()
            with tempfile.NamedTemporaryFile(prefix=TEMP_FILE_PREFIX, suffix=".ogg", delete=False) as tmp:
                tmp_path = tmp.name
            await voice_file.download_to_drive(tmp_path)
            transcript = await asyncio.to_thread(transcribe_voice, tmp_path)
            bot_log.info(f"Voice transcript: {transcript}")
            try:
                await send_response_chunks(update, f"<i>{html.escape(transcript)}</i>", parse_mode="HTML")
            except Exception as echo_err:
                bot_log.warning(f"Failed to echo transcript: {echo_err}")
            await _process_message(update, transcript)
        except Exception as e:
            await maybe_send_warning(update, f"Transcription error: {e}")
            bot_log.error(f"Voice error: {e}")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        tmp_path = None
        try:
            audio_file = await update.message.audio.get_file()
            with tempfile.NamedTemporaryFile(prefix=TEMP_FILE_PREFIX, suffix=".ogg", delete=False) as tmp:
                tmp_path = tmp.name
            await audio_file.download_to_drive(tmp_path)
            transcript = await asyncio.to_thread(transcribe_voice, tmp_path)
            bot_log.info(f"Audio transcript: {transcript}")
            try:
                await send_response_chunks(update, f"<i>{html.escape(transcript)}</i>", parse_mode="HTML")
            except Exception as echo_err:
                bot_log.warning(f"Failed to echo transcript: {echo_err}")
            await _process_message(update, transcript)
        except Exception as e:
            await maybe_send_warning(update, f"Transcription error: {e}")
            bot_log.error(f"Audio error: {e}")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        doc = update.message.document
        mime = (doc.mime_type or "").lower() if doc else ""
        if not mime.startswith("audio/"):
            return
        tmp_path = None
        try:
            doc_file = await doc.get_file()
            suffix = Path(doc.file_name or "").suffix or ".ogg"
            with tempfile.NamedTemporaryFile(prefix=TEMP_FILE_PREFIX, suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
            await doc_file.download_to_drive(tmp_path)
            transcript = await asyncio.to_thread(transcribe_voice, tmp_path)
            bot_log.info(f"Document audio transcript: {transcript}")
            try:
                await send_response_chunks(update, f"<i>{html.escape(transcript)}</i>", parse_mode="HTML")
            except Exception as echo_err:
                bot_log.warning(f"Failed to echo transcript: {echo_err}")
            await _process_message(update, transcript)
        except Exception as e:
            await maybe_send_warning(update, f"Transcription error: {e}")
            bot_log.error(f"Document audio error: {e}")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        await update.message.reply_text(f"Pi is up. Bot [{cfg.label}] running.")

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update):
            return
        await update.message.reply_text(
            "Claude Bot commands:\n/status \u2014 check if Pi/bot is alive\n"
            "/help \u2014 this message\n\nJust send any text and Claude replies."
        )

    return (
        [("status", cmd_status), ("help", cmd_help)],
        [
            (filters.VOICE, handle_voice),
            (filters.AUDIO, handle_audio),
            (filters.Document.ALL, handle_document),
            (filters.TEXT & ~filters.COMMAND, handle_message),
        ],
    )


# ---------------------------------------------------------------------------
# Main — run all bots in one event loop
# ---------------------------------------------------------------------------

def cleanup_stale_temp_files():
    removed = 0
    now = time.time()
    tmp_dir = Path(tempfile.gettempdir())
    for path in tmp_dir.glob(f"{TEMP_FILE_PREFIX}*"):
        try:
            if path.is_file() and now - path.stat().st_mtime > TEMP_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
                removed += 1
        except Exception:
            pass
    return removed


# Default: all .env files
DEFAULT_ENV_FILES = [".env", ".env-opus", ".env-sonnet", ".env-haiku", ".env-codex"]


async def run_all(env_files: list[str]):
    removed = cleanup_stale_temp_files()
    if removed:
        log.info(f"Removed {removed} stale temp audio file(s).")

    if WHISPER_BACKEND == "local":
        log.info(f"Whisper: using shared helper {_SHARED_WHISPER_SCRIPT}")

    apps = []
    for env_file in env_files:
        env_path = Path(__file__).parent / env_file
        if not env_path.exists():
            log.warning(f"Skipping {env_file} — file not found")
            continue

        cfg = BotConfig(env_file)
        cmd_handlers, msg_handlers = make_handlers(cfg)

        app = Application.builder().token(cfg.token).concurrent_updates(True).build()
        for name, handler in cmd_handlers:
            app.add_handler(CommandHandler(name, handler))
        for filt, handler in msg_handlers:
            app.add_handler(MessageHandler(filt, handler))

        apps.append((cfg.label, app))
        log.info(f"Configured bot [{cfg.label}] (source={cfg.bot_source}, model={cfg.hardwired_model or 'default'})")

    if not apps:
        log.error("No bots configured. Exiting.")
        return

    # Initialize and start all bots
    for label, app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info(f"Bot [{label}] polling started.")

    log.info(f"All {len(apps)} bots running. Press Ctrl+C to stop.")

    # Wait for shutdown signal
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()

    # Graceful shutdown
    log.info("Shutting down...")
    for label, app in apps:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            log.info(f"Bot [{label}] stopped.")
        except Exception as e:
            log.warning(f"Error stopping [{label}]: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Consolidated Telegram multi-bot")
    parser.add_argument("--bots", nargs="+", default=DEFAULT_ENV_FILES,
                        help="Which .env files to load (default: all)")
    args = parser.parse_args()
    asyncio.run(run_all(args.bots))


if __name__ == "__main__":
    main()
