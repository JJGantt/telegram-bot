#!/usr/bin/env python3
"""
Sends a Telegram message to Jared. Used by laptop scripts to report failures/events.
Also logs the notification to Pi history so it can be referenced in future conversations.

Call directly: python3 notify.py "your message"
Or import:     from notify import send_telegram
"""
import sys
import json
import urllib.request
from datetime import datetime

DEFAULT_BOT_TOKEN = "8775470090:AAESuwzgJcsfUnZIDXUJhDLZf6LEZ5yc56M"
CHAT_ID = 8334576932
PI_HOSTS = ["10.0.0.14", "100.104.197.58"]
PORT = 8765

def send_telegram(message: str, bot_token: str | None = None) -> bool:
    token = bot_token or DEFAULT_BOT_TOKEN
    try:
        body = json.dumps({"chat_id": CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False

def log_to_history(message: str):
    """Try to log this notification to Pi history. Silent if Pi unreachable."""
    for host in PI_HOSTS:
        try:
            status_req = urllib.request.Request(f"http://{host}:{PORT}/status")
            urllib.request.urlopen(status_req, timeout=5)
            body = json.dumps({
                "user": "[system notification]",
                "claude": message,
                "source": "system",
                "timestamp": datetime.now().isoformat(),
            }).encode()
            req = urllib.request.Request(
                f"http://{host}:{PORT}/log",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            return
        except Exception:
            continue

def notify(message: str, bot_token: str | None = None):
    """Send Telegram notification and log to history."""
    send_telegram(message, bot_token=bot_token)
    log_to_history(message)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("message", nargs="*", default=["Pi notification (no message)"])
    parser.add_argument("--bot-token", default=None)
    args = parser.parse_args()
    notify(" ".join(args.message), bot_token=args.bot_token)
