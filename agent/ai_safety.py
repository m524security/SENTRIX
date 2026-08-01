"""
SENTRIX — AI Safety Guards
==========================
SENTRIX is an AI-driven platform whose LLM outputs influence real operator
decisions (and, in `auto` action mode, real commands). This module puts a
safety layer between untrusted text and the LLM pipeline:

  guard_chat_input()   — caps input size and flags prompt-injection patterns
                         in user chat messages BEFORE they reach the prompt.
                         Heuristic, not a silver bullet: the real containment
                         is architectural (the chat LLM has no tools and its
                         output is displayed, never executed).

  validate_assessment()— schema/range validation for LLM-produced assessments:
                         action must be in the known allow-list, risk scores
                         numeric and clamped, asset_id must match the same
                         allow-list used at the ingestion boundary. An LLM
                         can never invent a new action verb or smuggle a
                         hostile asset_id into the action executor.

  scrub_llm_reply()    — strips control characters and hard-caps length on
                         text that will be rendered in the dashboard.

Telemetry itself is attacker-influenceable (log lines feed the prompt), so
"treat every LLM output as untrusted input" is the design rule here.
"""
from __future__ import annotations

import re

from agent.logging_setup import get_logger

_log = get_logger("ai_safety")

# The reasoning core's actual action vocabulary (decide_action + system prompt).
# Anything else the LLM invents is downgraded to MANUAL_REVIEW — an unknown verb
# must surface to a human, never silently pass toward the executor.
ALLOWED_ACTIONS = {
    "NONE", "WARN", "MANUAL_REVIEW",
    "ESCALATE_P1", "ESCALATE_P2",
    "RETRY_BACKUP", "SCHEDULE_RESTORE_TEST",
}

# Same allow-list as agent/ingestion/realtime_gateway._VALID_ASSET_ID
_VALID_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

MAX_CHAT_MESSAGE_CHARS = 4000
MAX_HISTORY_TURNS = 6
MAX_HISTORY_TURN_CHARS = 2000
MAX_REPLY_CHARS = 8000

# Heuristic prompt-injection markers. Matching one doesn't block the request
# (false positives would break legit questions); it strips known-dangerous
# framing and records the attempt in the audit-friendly log.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
        r"disregard\s+(your|the)\s+(system\s+)?prompt",
        r"you\s+are\s+now\s+(?!looking|viewing)",       # "you are now DAN/root/..."
        r"reveal\s+(your|the)\s+(system\s+)?prompt",
        r"\bBEGIN\s+SYSTEM\b|\[/?(system|inst)\]|<\|im_start\|>",
        r"execute\s+(the\s+)?(command|shell|code)\b",
    )
]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def guard_chat_input(message: str, history: list) -> tuple[str, list, bool]:
    """Returns (safe_message, safe_history, injection_suspected)."""
    suspected = False

    msg = _CONTROL_CHARS.sub("", str(message))[:MAX_CHAT_MESSAGE_CHARS]
    for pat in _INJECTION_PATTERNS:
        if pat.search(msg):
            suspected = True
            break

    safe_history = []
    for turn in list(history or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = "assistant" if str(turn.get("role", "user")).lower() == "assistant" else "user"
        content = _CONTROL_CHARS.sub("", str(turn.get("content", "")))[:MAX_HISTORY_TURN_CHARS]
        for pat in _INJECTION_PATTERNS:
            if pat.search(content):
                suspected = True
                break
        safe_history.append({"role": role, "content": content})

    if suspected:
        _log.warning("possible prompt-injection attempt in chat input",
                     extra={"preview": msg[:120]})
    return msg, safe_history, suspected


def scrub_llm_reply(text: str) -> str:
    """Sanitize LLM output destined for the dashboard."""
    return _CONTROL_CHARS.sub("", str(text))[:MAX_REPLY_CHARS]


def _clamp(value, lo: float, hi: float, default: float):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def validate_assessment(assessment: dict) -> dict | None:
    """Validate one LLM-produced assessment. Returns a cleaned copy, or None
    if it is structurally unusable (missing/invalid asset_id)."""
    if not isinstance(assessment, dict):
        return None
    a = dict(assessment)

    aid = str(a.get("asset_id", "")).strip()
    if not _VALID_ASSET_ID.fullmatch(aid) or aid.startswith("-"):
        _log.warning("dropped LLM assessment with invalid asset_id",
                     extra={"asset_id_preview": aid[:64]})
        return None
    a["asset_id"] = aid

    action = str(a.get("action", "NONE")).strip().upper()
    if action not in ALLOWED_ACTIONS:
        _log.warning("LLM produced unknown action '%s' — downgraded to MANUAL_REVIEW", action[:32],
                     extra={"asset_id": aid})
        action = "MANUAL_REVIEW"
    a["action"] = action

    if "risk_score" in a:
        # SENTRIX risk scores are rpo_pct * criticality * multiplier and
        # legitimately exceed 500 (>=501 triggers P1) — clamp only to a sane
        # ceiling to stop NaN/inf/absurd LLM values, not to a 0-100 scale.
        a["risk_score"] = _clamp(a.get("risk_score"), 0, 10000, 50)
    if "confidence" in a:
        a["confidence"] = _clamp(a.get("confidence"), 0.0, 1.0, 0.5)
    if "explanation" in a:
        a["explanation"] = _CONTROL_CHARS.sub("", str(a["explanation"]))[:2000]

    return a


def validate_assessments(assessments: list) -> list:
    """Vector version — drops invalid entries, cleans the rest."""
    out = []
    for item in assessments or []:
        cleaned = validate_assessment(item)
        if cleaned is not None:
            out.append(cleaned)
    return out
