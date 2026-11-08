"""
SENTRIX — Telegram Alerting (optional)
-------------------------------------
Sends P1/P2 alerts to Telegram if a bot token and a *numeric* chat ID are
configured. The original .env shipped a getUpdates API URL in TELEGRAM_CHAT
instead of a chat ID, which silently disabled alerting — this version
detects that case, tries to recover a real chat ID automatically, and
otherwise degrades gracefully (logs the alert locally instead of crashing).
"""

import re

import httpx

from agent import config
from agent.logging_setup import get_logger

_log = get_logger("telegram")

_TOKEN = config.TELEGRAM_BOT_TOKEN
_chat_id = None
_resolution_attempted = False


def _looks_like_chat_id(value: str) -> bool:
    return bool(value) and bool(re.fullmatch(r"-?\d+", value.strip()))


async def _resolve_chat_id() -> str:
    """Figure out a usable numeric chat ID from whatever is in TELEGRAM_CHAT.
    Accepts a raw numeric ID directly; if given a getUpdates URL (as the
    original .env did), calls it once to pull the most recent chat ID."""
    global _chat_id, _resolution_attempted
    if _chat_id is not None:
        return _chat_id
    _resolution_attempted = True

    raw = config.TELEGRAM_CHAT.strip()
    if _looks_like_chat_id(raw):
        _chat_id = raw
        return _chat_id

    # If it's a Telegram API URL, try to read a chat id from getUpdates.
    if raw.startswith("http") and _TOKEN:
        url = raw
        if "getUpdates" not in url:
            url = f"https://api.telegram.org/bot{_TOKEN}/getUpdates"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                data = resp.json()
            for update in reversed(data.get("result", [])):
                msg = update.get("message") or update.get("channel_post") or {}
                chat = msg.get("chat", {})
                if "id" in chat:
                    _chat_id = str(chat["id"])
                    return _chat_id
        except Exception:
            pass

    _chat_id = ""  # mark "tried and failed" so we don't retry every alert
    return _chat_id


async def send_alert(asset_name: str, severity: str, message: str) -> bool:
    """Returns True if an alert was actually delivered, False otherwise."""
    if not _TOKEN:
        _log.info("not configured — would send %s for %s", severity, asset_name)
        return False

    chat_id = await _resolve_chat_id()
    if not chat_id:
        _log.warning(f"no usable chat id (TELEGRAM_CHAT was '{config.TELEGRAM_CHAT[:24]}...') "
              f"— would send {severity} for {asset_name}")
        return False

    icons = {"P1": "\U0001f534", "P2": "\U0001f7e1", "WARN": "\u26a0\ufe0f", "INFO": "\u2139\ufe0f"}
    icon = icons.get(severity, "\U0001f4e2")
    text = (
        f"{icon} *SENTRIX {severity} Alert*\n\n"
        f"*Asset:* {asset_name}\n"
        f"*Detail:* {message}\n"
        f"_Recovery Readiness Agent_"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            ok = resp.status_code == 200 and resp.json().get("ok", False)
            if not ok:
                _log.error("send failed: %s %s", resp.status_code, resp.text[:120])
            return ok
    except Exception as e:
        _log.error("send error: %s", e)
        return False
