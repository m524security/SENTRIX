"""
SENTRIX — Notification Fan-Out
==============================
One alert, every channel you've configured: Telegram (existing), Slack,
Discord, PagerDuty Events v2, and SMTP email. Each channel is optional,
lazy, and failure-isolated — one channel erroring never blocks another.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import httpx

from agent import config
from agent.actions.telegram_client import send_alert as _telegram


async def _slack(asset: str, severity: str, message: str) -> bool:
    if not config.SLACK_WEBHOOK_URL:
        return False
    icon = {"P1": ":red_circle:", "P2": ":large_yellow_circle:"}.get(severity, ":bell:")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(config.SLACK_WEBHOOK_URL, json={
                "text": f"{icon} *SENTRIX {severity}* — *{asset}*\n{message}"})
        return r.status_code == 200
    except Exception:
        return False


async def _discord(asset: str, severity: str, message: str) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        return False
    color = {"P1": 15158332, "P2": 15844367}.get(severity, 3447003)
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(config.DISCORD_WEBHOOK_URL, json={
                "embeds": [{"title": f"SENTRIX {severity} — {asset}",
                            "description": message[:1900], "color": color}]})
        return r.status_code in (200, 204)
    except Exception:
        return False


async def _pagerduty(asset: str, severity: str, message: str) -> bool:
    if not config.PAGERDUTY_ROUTING_KEY or severity not in ("P1", "P2"):
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post("https://events.pagerduty.com/v2/enqueue", json={
                "routing_key": config.PAGERDUTY_ROUTING_KEY,
                "event_action": "trigger",
                "payload": {
                    "summary": f"SENTRIX {severity}: {asset} — {message[:900]}",
                    "source": "sentrix",
                    "severity": "critical" if severity == "P1" else "error",
                }})
        return r.status_code == 202
    except Exception:
        return False


def _email_sync(asset: str, severity: str, message: str) -> bool:
    if not (config.SMTP_HOST and config.ALERT_EMAIL_TO):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = f"[SENTRIX {severity}] {asset}"
        msg["From"] = config.SMTP_USER or "sentrix@localhost"
        msg["To"] = config.ALERT_EMAIL_TO
        msg.set_content(f"SENTRIX {severity} alert\n\nAsset: {asset}\n\n{message}")
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
            s.starttls()
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        return False


async def fan_out(asset: str, severity: str, message: str) -> dict:
    """Send to every configured channel concurrently; report per-channel result."""
    telegram_t = asyncio.create_task(_telegram(asset, severity, message))
    slack_t = asyncio.create_task(_slack(asset, severity, message))
    discord_t = asyncio.create_task(_discord(asset, severity, message))
    pd_t = asyncio.create_task(_pagerduty(asset, severity, message))
    email_t = asyncio.to_thread(_email_sync, asset, severity, message)
    tg, sl, dc, pd, em = await asyncio.gather(telegram_t, slack_t, discord_t, pd_t, email_t,
                                              return_exceptions=True)
    def b(x): return x is True
    results = {"telegram": b(tg), "slack": b(sl), "discord": b(dc), "pagerduty": b(pd), "email": b(em)}
    results["any_delivered"] = any(results.values())
    return results
