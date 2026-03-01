#!/usr/bin/env python3
"""
Claude's Telegram bot — runs as a systemd service on the Pi.
Polls Telegram for messages from the authorized user and responds via Claude.

Config: /home/jaredgantt/scripts/.env (gitignored, not committed)

All exchanges are saved to /home/jaredgantt/data/history/YYYY-MM-DD.json via
runner.py, which also injects the last 24 hours of history as context
so Claude has continuity across sessions and message sources (Telegram + HTTP).
"""

import html
import logging
import asyncio
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

# --- Config ---
from dotenv import load_dotenv
import os

# Allow passing a custom .env file as the first argument (e.g. .env-opus, .env-haiku)
_env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
load_dotenv(Path(__file__).parent / _env_file)

BOT_TOKEN = os.environ["BOT_TOKEN"]
AUTHORIZED_USER_ID = int(os.environ["AUTHORIZED_USER_ID"])
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Feature flag for non-essential warning/status messages sent to Telegram.
WARNINGS_ENABLED = os.getenv("WARNINGS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
# If CLAUDE_MODEL is set in the env file, this bot is hardwired to that model.
# Otherwise falls back to the shared route_state (used by the main sonnet bot).
HARDWIRED_MODEL = os.getenv("CLAUDE_MODEL", "").strip().lower() or None
# If ROUTE_MODE is set, this bot is hardwired to that mode (e.g. "codex").
HARDWIRED_MODE = os.getenv("ROUTE_MODE", "").strip().lower() or None
# Source identifier used when logging exchanges and loading context.
BOT_SOURCE = os.getenv("BOT_SOURCE", "pi-telegram").strip().lower()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

TEMP_FILE_PREFIX = "claude-bot-audio-"
TEMP_MAX_AGE_SECONDS = 24 * 60 * 60


def cleanup_stale_temp_files(max_age_seconds: int = TEMP_MAX_AGE_SECONDS) -> int:
    """Remove old temporary audio files left behind by interrupted runs."""
    removed = 0
    now = time.time()
    tmp_dir = Path(tempfile.gettempdir())
    for path in tmp_dir.glob(f"{TEMP_FILE_PREFIX}*"):
        try:
            if not path.is_file():
                continue
            age = now - path.stat().st_mtime
            if age > max_age_seconds:
                path.unlink(missing_ok=True)
                removed += 1
        except Exception as e:
            log.warning(f"Temp cleanup skipped for {path}: {e}")
    return removed


def is_authorized(update: Update) -> bool:
    if AUTHORIZED_USER_ID is None:
        return True  # setup mode — accept anyone until ID is configured
    return update.effective_user.id == AUTHORIZED_USER_ID


async def maybe_send_warning(update: Update, text: str):
    """Send warning-style message only when WARNINGS_ENABLED is on."""
    if WARNINGS_ENABLED:
        await update.message.reply_text(text)
    else:
        log.info(f"Suppressed warning/status message: {text}")


def transcribe_voice(file_path: str) -> str:
    """Send audio file to OpenAI Whisper API and return transcript."""
    import openai
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            prompt="Include all words verbatim, including profanity.",
            file=f,
        )
    return result.text


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        if WARNINGS_ENABLED:
            log.warning(f"Unauthorized voice from {update.effective_user.id}")
        else:
            log.info(f"Unauthorized voice from {update.effective_user.id}")
        return

    if not OPENAI_API_KEY:
        await maybe_send_warning(update, "Voice transcription not configured — add OPENAI_API_KEY to .env.")
        return

    tmp_path: str | None = None
    try:
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(prefix=TEMP_FILE_PREFIX, suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)

        transcript = await asyncio.to_thread(transcribe_voice, tmp_path)

        log.info(f"Voice transcript: {transcript}")
        # Echo transcript (chunked so long voice messages don't blow up)
        try:
            await send_response_chunks(update, f"<i>{html.escape(transcript)}</i>", parse_mode="HTML")
        except Exception as echo_err:
            log.warning(f"Failed to echo voice transcript: {echo_err}")

        # Route transcript through normal message handling
        await _process_message(update, transcript)
    except Exception as e:
        await maybe_send_warning(update, f"Transcription error: {e}")
        log.error(f"Voice error: {e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        if WARNINGS_ENABLED:
            log.warning(f"Unauthorized audio from {update.effective_user.id}")
        else:
            log.info(f"Unauthorized audio from {update.effective_user.id}")
        return

    if not OPENAI_API_KEY:
        await maybe_send_warning(update, "Audio transcription not configured — add OPENAI_API_KEY to .env.")
        return

    tmp_path: str | None = None
    try:
        audio_file = await update.message.audio.get_file()
        with tempfile.NamedTemporaryFile(prefix=TEMP_FILE_PREFIX, suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await audio_file.download_to_drive(tmp_path)

        transcript = await asyncio.to_thread(transcribe_voice, tmp_path)

        log.info(f"Audio transcript: {transcript}")
        try:
            await send_response_chunks(update, f"<i>{html.escape(transcript)}</i>", parse_mode="HTML")
        except Exception as echo_err:
            log.warning(f"Failed to echo audio transcript: {echo_err}")

        # Route transcript through normal message handling
        await _process_message(update, transcript)
    except Exception as e:
        await maybe_send_warning(update, f"Transcription error: {e}")
        log.error(f"Audio error: {e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        if WARNINGS_ENABLED:
            log.warning(f"Unauthorized document from {update.effective_user.id}")
        else:
            log.info(f"Unauthorized document from {update.effective_user.id}")
        return

    doc = update.message.document
    mime = (doc.mime_type or "").lower() if doc else ""
    if not mime.startswith("audio/"):
        return

    if not OPENAI_API_KEY:
        await maybe_send_warning(update, "Audio transcription not configured — add OPENAI_API_KEY to .env.")
        return

    tmp_path: str | None = None
    try:
        doc_file = await doc.get_file()
        suffix = Path(doc.file_name or "").suffix or ".ogg"
        with tempfile.NamedTemporaryFile(prefix=TEMP_FILE_PREFIX, suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await doc_file.download_to_drive(tmp_path)

        transcript = await asyncio.to_thread(transcribe_voice, tmp_path)

        log.info(f"Document audio transcript: {transcript}")
        try:
            await send_response_chunks(update, f"<i>{html.escape(transcript)}</i>", parse_mode="HTML")
        except Exception as echo_err:
            log.warning(f"Failed to echo document transcript: {echo_err}")

        # Route transcript through normal message handling
        await _process_message(update, transcript)
    except Exception as e:
        await maybe_send_warning(update, f"Transcription error: {e}")
        log.error(f"Document audio error: {e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


async def _process_message(update: Update, text: str):
    """Core message routing — used by both text and voice handlers."""
    cleaned, new_mode, new_model = apply_switch_and_strip(text)

    # Hardwired bots reject switch commands without modifying shared state.
    if (new_mode or new_model) and (HARDWIRED_MODEL or HARDWIRED_MODE):
        what = HARDWIRED_MODEL or f"{HARDWIRED_MODE} mode"
        if not cleaned:
            await send_response_chunks(update, f"This bot is hardwired to {what}. Use the Pi bot to switch.")
            return
        # Had other text alongside the switch command — ignore the switch, process the text.
    else:
        if new_mode:
            set_mode(new_mode)
        if new_model:
            set_model(new_model)

    mode = HARDWIRED_MODE or (get_mode() if not HARDWIRED_MODEL else "claude")
    model = HARDWIRED_MODEL or get_model()

    if not cleaned and (new_mode or new_model):
        parts = []
        if new_mode:
            parts.append(f"mode → {mode}")
        if new_model:
            parts.append(f"model → {model}")
        response = "Switched: " + ", ".join(parts) + "."
    else:
        if mode == "codex":
            response = await asyncio.to_thread(run_codex, cleaned, BOT_SOURCE)
        else:
            response = await asyncio.to_thread(run_claude, cleaned, BOT_SOURCE, model)
    await send_response_chunks(update, response)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        if WARNINGS_ENABLED:
            log.warning(f"Unauthorized message from {update.effective_user.id}")
        else:
            log.info(f"Unauthorized message from {update.effective_user.id}")
        return

    text = update.message.text or ""
    user_id = update.effective_user.id
    log.info(f"Incoming from user_id={user_id}: {text}")

    try:
        await _process_message(update, text)
    except Exception as e:
        await maybe_send_warning(update, f"Error: {e}")
        log.error(f"Error: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("Pi is up. Claude bot running.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    help_text = (
        "Claude Bot commands:\n"
        "/status — check if Pi/bot is alive\n"
        "/help — this message\n\n"
        "Just send any text and Claude replies."
    )
    await update.message.reply_text(help_text)

async def send_response_chunks(update: Update, text: str, chunk_size: int = 3800, parse_mode: str | None = None):
    # Telegram hard limit is 4096, keep some buffer for safety.
    if not text:
        await update.message.reply_text("(No response)")
        return
    for i in range(0, len(text), chunk_size):
        await update.message.reply_text(text[i:i + chunk_size], parse_mode=parse_mode)


def main():
    removed = cleanup_stale_temp_files()
    if removed:
        log.info(f"Removed {removed} stale temp audio file(s).")
    log.info("Starting Claude bot...")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
