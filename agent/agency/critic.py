"""
SENTRIX — Analyst + Critic (second opinion before any page)
===========================================================
A false P1 at 3am costs trust. Before SENTRIX pages a human, a CRITIC pass
reviews the analyst's escalation with fresh eyes and one job: try to talk it
down. If the critic can't justify a downgrade, the page proceeds with a
`critic_confirmed` stamp. If it can — and ONLY within safety bounds (never
below the hard floor) — the action is downgraded with the critic's reasoning
attached. LLM unavailable → the escalation proceeds untouched.
"""
from __future__ import annotations

import json

from agent.reasoning import llm_providers

_CRITIC_PROMPT = """You are the SENTRIX CRITIC — an independent reviewer whose job is to catch
false escalations before an on-call engineer gets paged at 3am.

The analyst decided:
{assessment}

Asset state:
{asset}

Challenge it: is this escalation genuinely warranted RIGHT NOW, or is it
premature (transient blip, low-tier asset, evidence doesn't support the
severity)? Be skeptical but honest — a REAL persistent incident on a critical
asset must go through.

Respond ONE JSON object only:
{{"verdict": "confirm" | "downgrade",
  "action": "<same action if confirm; the lower action if downgrade>",
  "reason": "<one sharp sentence>"}}"""

_DOWNGRADE_OK = {
    "ESCALATE_P1": {"ESCALATE_P2", "WARN", "MANUAL_REVIEW"},
    "ESCALATE_P2": {"WARN", "MANUAL_REVIEW", "RETRY_BACKUP"},
}


def review(assessment: dict, asset: dict, hard_floor: str = "") -> dict:
    """Returns assessment, possibly downgraded, always annotated. Never raises."""
    action = assessment.get("action", "")
    if action not in _DOWNGRADE_OK:
        return assessment
    try:
        prompt = _CRITIC_PROMPT.format(
            assessment=json.dumps({k: assessment.get(k) for k in
                                   ("asset_id", "action", "risk_score", "explanation")}),
            asset=json.dumps({k: asset.get(k) for k in
                              ("tier", "criticality_score", "consecutive_failures",
                               "hours_since_last_backup", "rpo_target_hours", "evidence")}))
        reply = llm_providers.call_llm_json(prompt)
        verdict = str(reply.get("verdict", "confirm")).lower()
        out = dict(assessment)
        if verdict == "downgrade":
            new_action = str(reply.get("action", "")).strip()
            floor_blocks = hard_floor and new_action != hard_floor and \
                new_action not in ("ESCALATE_P1",) and hard_floor in ("ESCALATE_P1", "ESCALATE_P2")
            if new_action in _DOWNGRADE_OK[action] and not floor_blocks:
                out["action"] = new_action
                out["critic"] = {"verdict": "downgraded", "from": action,
                                 "reason": str(reply.get("reason", ""))[:250]}
                return out
        out["critic"] = {"verdict": "confirmed",
                         "reason": str(reply.get("reason", ""))[:250]}
        return out
    except Exception:
        return assessment  # no critic available — escalation proceeds
