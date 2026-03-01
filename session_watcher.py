#!/usr/bin/env python3
"""
Claude session progress watcher — sends Telegram updates while Claude works.

Triggered by hooks in ~/.claude/settings.json:
  UserPromptSubmit → session_watcher.py --start   (spawns background watcher)
  Stop             → session_watcher.py --stop    (signals watcher to exit)

Update schedule (elapsed time from first tool use):
  20s → 1m → 3m → every 5m after that

Only sends if Claude is actually doing tool calls — silent for quick responses.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

INTERVALS = [20, 60, 180]   # seconds from start for first 3 updates
REPEAT_EVERY = 300          # every 5m after that
POLL_SECS = 3               # transcript poll interval

SCRIPTS_DIR = Path(__file__).parent


def _signal_path(session_id: str) -> Path:
    return Path(f"/tmp/claude_watcher_{session_id}.done")


def _pid_path(session_id: str) -> Path:
    return Path(f"/tmp/claude_watcher_{session_id}.pid")


def _token_path(session_id: str) -> Path:
    """Persisted bot token file — ensures updates route to the correct bot."""
    return Path(f"/tmp/claude_watcher_{session_id}.token")


def _source_path(session_id: str) -> Path:
    """Persisted source identifier — for logging which bot spawned this session."""
    return Path(f"/tmp/claude_watcher_{session_id}.source")


# ── Telegram ──────────────────────────────────────────────────────────────────

def notify(msg: str, bot_token: str | None = None):
    cmd = ["python3", str(SCRIPTS_DIR / "notify.py"), msg]
    if bot_token:
        cmd.extend(["--bot-token", bot_token])
    try:
        subprocess.run(cmd, timeout=10, capture_output=True)
    except Exception:
        pass


# ── Haiku summarizer ──────────────────────────────────────────────────────────

def summarize(actions: list[str]) -> str:
    if not actions:
        return "working..."
    lines = "\n".join(f"- {a}" for a in actions[-25:])
    prompt = (
        f"Claude is working on a task. Here are the tool calls it made:\n{lines}\n\n"
        "Summarize what Claude is doing in one sentence, max 15 words. "
        "If you can infer the higher-level goal, describe that (e.g. \"updating the "
        "Telegram bot config and restarting the service\"). If the actions are too "
        "ambiguous to infer a goal, just describe what's happening (e.g. \"SSHing "
        "into the Mac and reading log files\"). Never ask questions. Never say you "
        "lack context. No preamble, no period."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--dangerously-skip-permissions"],
            input=prompt, capture_output=True, text=True, timeout=20,
        )
        return (result.stdout or "").strip() or "still working..."
    except Exception:
        return "still working..."


# ── Transcript reader ─────────────────────────────────────────────────────────

def extract_actions(path: Path, from_pos: int) -> tuple[list[str], int]:
    """Read new JSONL lines, extract tool-use events into readable strings."""
    actions = []
    new_pos = from_pos
    try:
        with open(path, "r") as f:
            f.seek(from_pos)
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if name == "Bash":
                        cmd = (inp.get("command", "") or "").replace("\n", " ")[:80]
                        actions.append(f"Bash: {cmd}")
                    elif name in ("Write", "Edit", "Read"):
                        fp = inp.get("file_path", inp.get("path", "")) or ""
                        actions.append(f"{name}: {Path(fp).name or fp}")
                    elif name == "Task":
                        desc = (inp.get("description", inp.get("prompt", "")) or "")[:60]
                        actions.append(f"Task: {desc}")
                    elif name in ("Glob", "Grep"):
                        pat = inp.get("pattern", "") or ""
                        actions.append(f"{name}: {pat[:60]}")
                    elif name == "WebFetch":
                        actions.append(f"WebFetch")
                    elif name == "WebSearch":
                        actions.append(f"WebSearch: {inp.get('query', '')[:60]}")
                    else:
                        actions.append(name)
            new_pos = f.tell()
    except Exception:
        pass
    return actions, new_pos


# ── Watcher loop ──────────────────────────────────────────────────────────────

def watch(transcript_path: str, session_id: str, bot_token: str | None = None):
    done = _signal_path(session_id)
    path = Path(transcript_path)

    start = time.time()
    pos = 0
    pending_actions: list[str] = []
    interval_idx = 0
    last_update_elapsed = 0.0

    while True:
        if done.exists():
            done.unlink(missing_ok=True)
            break

        new_actions, pos = extract_actions(path, pos)
        pending_actions.extend(new_actions)

        elapsed = time.time() - start

        # Determine next target
        if interval_idx < len(INTERVALS):
            target = INTERVALS[interval_idx]
        else:
            target = last_update_elapsed + REPEAT_EVERY

        if elapsed >= target:
            interval_idx += 1
            last_update_elapsed = elapsed
            t = f"{int(elapsed)}s" if elapsed < 60 else f"{int(elapsed // 60)}m{int(elapsed % 60)}s"
            if pending_actions:
                summary = summarize(pending_actions)
                pending_actions = []
            else:
                summary = "thinking..."
            notify(f"⚙️ [{t}] {summary}", bot_token=bot_token)

        time.sleep(POLL_SECS)


# ── Entry points ──────────────────────────────────────────────────────────────

def cmd_start():
    """UserPromptSubmit hook — spawn background watcher only for Telegram sessions."""
    source = os.environ.get("CLAUDE_SOURCE", "")
    if "telegram" not in source:
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "")
    if not transcript_path or not session_id:
        sys.exit(0)

    pid_file = _pid_path(session_id)
    if pid_file.exists():
        sys.exit(0)  # already watching this session

    bot_token = os.environ.get("BOT_TOKEN", "")

    # Persist bot token and source to files keyed by session_id.
    # This ensures the background watcher always routes updates to the
    # correct bot, even if environment inheritance breaks.
    if bot_token:
        _token_path(session_id).write_text(bot_token)
    if source:
        _source_path(session_id).write_text(source)

    cmd = [sys.executable, __file__, "--watch", transcript_path, session_id]
    if bot_token:
        cmd.extend(["--bot-token", bot_token])
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file.write_text(str(proc.pid))
    sys.exit(0)


def cmd_stop():
    """Stop hook — signal watcher to exit."""
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    session_id = payload.get("session_id", "")
    if session_id:
        _signal_path(session_id).touch()
        _pid_path(session_id).unlink(missing_ok=True)
        _token_path(session_id).unlink(missing_ok=True)
        _source_path(session_id).unlink(missing_ok=True)
    sys.exit(0)


def cmd_watch(transcript_path: str, session_id: str, bot_token: str | None = None):
    """Background watcher process."""
    # If bot_token wasn't passed via CLI, try to read from persisted file.
    if not bot_token:
        tf = _token_path(session_id)
        if tf.exists():
            bot_token = tf.read_text().strip() or None
    try:
        watch(transcript_path, session_id, bot_token=bot_token)
    finally:
        _pid_path(session_id).unlink(missing_ok=True)
        _token_path(session_id).unlink(missing_ok=True)
        _source_path(session_id).unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "--start":
        cmd_start()
    elif mode == "--stop":
        cmd_stop()
    elif mode == "--watch":
        # Parse: --watch <transcript_path> <session_id> [--bot-token <token>]
        args = sys.argv[2:]
        if len(args) < 2:
            sys.exit(1)
        tp, sid = args[0], args[1]
        bt = None
        if "--bot-token" in args:
            idx = args.index("--bot-token")
            if idx + 1 < len(args):
                bt = args[idx + 1]
        cmd_watch(tp, sid, bot_token=bt)
    else:
        sys.exit(1)
