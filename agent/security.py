"""
SENTRIX — API Security
======================
Fixes the v3 hole where anyone who could reach the port could forge agent
cycles and push fake alerts to every dashboard. Write endpoints now require a
bearer token; read endpoints stay open for the dashboard.

  SENTRIX_API_TOKEN  — shared secret for write/ingest endpoints.
                       If unset in development, writes are allowed (with a
                       one-time warning) so local dev isn't blocked.
                       If unset in production (SENTRIX_ENV=production), the
                       app refuses to start — no accidental open write API.

  SENTRIX_HMAC_KEY   — signs the tamper-evident audit trail and outgoing
                       action-webhook payloads. Production refuses to start
                       while it's the public default: signatures made with a
                       known key are forgeable, which defeats the entire
                       point of a tamper-evident log.

Token comparison uses hmac.compare_digest (constant-time) — the same pattern
audit_logger.verify_signature already uses — so the bearer check can't leak
prefix information through response-timing differences.
"""
from __future__ import annotations

import hmac
import sys

from fastapi import Header, HTTPException

from agent import config
from agent.logging_setup import get_logger

_log = get_logger("security")

_warned = False


def enforce_startup_policy():
    """Call at app startup. Hard-fail an unprotected production API."""
    if config.ENV_NAME == "production":
        fatal = []
        if not config.API_TOKEN:
            fatal.append("SENTRIX_API_TOKEN is required in production "
                         "(protects write/ingest endpoints).")
        allowed, host = config.llm_host_allowed()
        if not allowed:
            fatal.append(
                f"LLM_BASE_URL host '{host}' is not in SENTRIX_LLM_ALLOWED_HOSTS "
                f"({', '.join(config.LLM_ALLOWED_HOSTS)}). Every cycle sends asset "
                f"names, criticality and recovery gaps to this host — refusing to "
                f"leak fleet data to an unexpected destination.")
        if config.HMAC_KEY_IS_DEFAULT:
            fatal.append("SENTRIX_HMAC_KEY is unset or still a known default in "
                         "production (audit-trail and webhook signatures would be "
                         "forgeable). Set a strong random value, e.g.: "
                         "python -c \"import secrets; print(secrets.token_hex(32))\"")
        if config.LLM_BASE_URL and not config.LLM_ALLOWED_HOSTS:
            _log.warning(
                "SENTRIX_LLM_ALLOWED_HOSTS is unset in production — LLM egress is "
                "unrestricted. Asset names, criticality and recovery gaps are sent "
                "to LLM_BASE_URL every cycle. Pin the expected hosts, or use the "
                "offline SLM/Ollama path (see SECURITY.md).")
        if fatal:
            for msg in fatal:
                _log.critical(msg)
            _log.critical("Refusing to start.")
            sys.exit(1)


async def require_write_token(authorization: str = Header(default="")):
    """FastAPI dependency guarding write/ingest endpoints."""
    global _warned
    if not config.API_TOKEN:
        if config.ENV_NAME != "production" and not _warned:
            _log.warning("No SENTRIX_API_TOKEN set — write endpoints are "
                         "OPEN (development only). Set one before deploying.")
            _warned = True
        return True
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    # Constant-time comparison — plain != short-circuits on the first
    # differing byte, which leaks token prefixes via response timing.
    if not hmac.compare_digest(token.encode(), config.API_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing write token")
    return True


def check_ws_token(token: str) -> bool:
    """Constant-time token check for WebSocket endpoints.

    WebSockets can't carry an Authorization header from a browser, so the token
    arrives as a query param (`/ws?token=...`). Same comparison discipline as
    require_write_token — a plain `!=` leaks the token prefix through
    close-timing, which is just as exploitable over a socket as over HTTP.

    When no token is configured (development), sockets stay open so local dev
    isn't blocked; enforce_startup_policy() guarantees a token exists in
    production, which makes this fail-closed where it matters.
    """
    if not config.API_TOKEN:
        return True
    return hmac.compare_digest((token or "").encode(), config.API_TOKEN.encode())
