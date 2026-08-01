"""
SENTRIX — Reasoning Core (v4)
=============================
This is the fix for the single biggest flaw in the previous architecture.

BEFORE (SENTRIX v3): the prompt handed the LLM the exact deterministic rules,
the LLM answered, and then the validator OVERWROTE every decision, score and
action with the rule engine's output — keeping only the model's sentence. Three
"brains" (SLM → LLM → rules) were all forced to agree with the dumbest one. The
LLM had zero authority. That is why it felt like a rule engine wearing an AI
costume.

NOW (SENTRIX v4):
  • The LLM REASONS from evidence + memory and OWNS its decision.
  • The deterministic policy is no longer copied into the prompt; it runs
    afterwards as a GUARDRAIL that only intervenes on hard-safety boundaries
    (a genuine P1 the model under-called), and every intervention is recorded
    as a `diverged` flag with both opinions kept — divergence is signal, not
    something to silently erase.
  • Per-asset MEMORY is injected so the model reasons across time ("escalated
    twice, still failing") — the loop that makes it an agent.
  • It stays provider-agnostic and never crashes: SLM → cloud chain → rules.

The result: on the same fleet, a good model and the rule engine can legitimately
DISAGREE, and you can see exactly where and why. That disagreement surface is the
most valuable output in the system and v3 was throwing it away.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.reasoning import llm_providers
from agent.ingestion import loghub_engine
from agent.memory import decision_memory
from agent.logging_setup import get_logger

_log = get_logger("reasoning")

def _rag_enabled() -> bool:
    try:
        from agent import config as _c
        return bool(_c.RAG)
    except Exception:
        return False

_RAG_ENABLED = _rag_enabled()

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system_prompt.txt"
SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

TIER_MULTIPLIER = {1: 2.0, 2: 1.5, 3: 1.0, 4: 0.5}
VALID_ACTIONS = {
    "NONE", "WARN", "RETRY_BACKUP", "SCHEDULE_RESTORE_TEST",
    "ESCALATE_P2", "ESCALATE_P1", "MANUAL_REVIEW",
}
# Ordinal severity so the guardrail can reason about "under-called" vs "over-called".
ACTION_SEVERITY = {
    "NONE": 0, "WARN": 1, "RETRY_BACKUP": 1, "SCHEDULE_RESTORE_TEST": 2,
    "MANUAL_REVIEW": 3, "ESCALATE_P2": 4, "ESCALATE_P1": 5,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Shared risk math — still the single source of truth, but now used as a
# SANITY REFERENCE for the guardrail, not as a replacement for the model.
# ---------------------------------------------------------------------------
def compute_risk(asset: dict) -> dict:
    rpo_target = max(float(asset.get("rpo_target_hours", 1) or 1), 0.01)
    hours = float(asset.get("hours_since_last_backup", 0) or 0)
    rpo_pct = (hours / rpo_target) * 100.0
    tier = int(asset.get("tier", 3) or 3)
    crit = float(asset.get("criticality_score", 50) or 50)
    mult = TIER_MULTIPLIER.get(tier, 1.0)
    score = rpo_pct * (crit / 100.0) * mult
    return {"rpo_consumed_pct": round(rpo_pct, 1), "risk_score": round(score, 1), "tier": tier}


def decide_action(asset: dict, risk: dict) -> str:
    tier = risk["tier"]
    score = risk["risk_score"]
    consec = int(asset.get("consecutive_failures", 0) or 0)
    overdue = int(asset.get("restore_test_days_overdue", 0) or 0)

    if consec >= 3 and tier == 1:
        return "ESCALATE_P1"
    if consec >= 3 and tier == 2:
        return "ESCALATE_P2"

    if score >= 501:
        action = "ESCALATE_P1"
    elif score >= 200:
        action = "ESCALATE_P2"
    elif score >= 50:
        action = "WARN"
    else:
        action = "NONE"

    if action in ("NONE", "WARN") and overdue > 0:
        action = "SCHEDULE_RESTORE_TEST"
    if tier == 4 and score < 300:
        action = "NONE"
    return action


def _hard_floor_action(asset: dict, risk: dict) -> str:
    """
    The ONLY thing the guardrail hard-enforces: genuine safety floors the model
    is never allowed to fall below. Everything above this the model owns.
      • 3+ consecutive failures on a tier-1 asset is always at least a P1.
      • 3+ consecutive failures on a tier-2 asset is always at least a P2.
    Returns "" when there is no hard floor for this asset.
    """
    consec = int(asset.get("consecutive_failures", 0) or 0)
    if consec >= 3 and risk["tier"] == 1:
        return "ESCALATE_P1"
    if consec >= 3 and risk["tier"] == 2:
        return "ESCALATE_P2"
    return ""


# ---------------------------------------------------------------------------
# Prompt building — evidence + memory, NOT the rulebook.
# ---------------------------------------------------------------------------
def build_prompt(asset_batch: list, cycle_id: str) -> str:
    timestamp = _utcnow()

    compact_assets = []
    for a in asset_batch:
        compact = {
            "asset_id": a.get("asset_id"),
            "asset_name": a.get("asset_name"),
            "tier": a.get("tier"),
            "criticality_score": a.get("criticality_score"),
            "rpo_target_hours": a.get("rpo_target_hours"),
            "hours_since_last_backup": a.get("hours_since_last_backup"),
            "consecutive_failures": a.get("consecutive_failures"),
            "restore_test_days_overdue": a.get("restore_test_days_overdue"),
            "source": a.get("source_label"),
        }
        if a.get("evidence"):
            compact["log_evidence"] = a["evidence"]
        # Inject remembered history — the temporal context v3 never gave the model.
        mem = decision_memory.recall(a.get("asset_id"))
        if mem.get("seen_before"):
            compact["memory"] = {
                "last_action": mem["last_action"],
                "risk_trend": mem["risk_trend"],
                "escalations_recently": mem["escalations_in_window"],
                "persistent_incident": mem["persistent_incident"],
            }
        # RAG: recall the most similar past incidents for assets showing trouble.
        if _RAG_ENABLED and (a.get("consecutive_failures", 0) or 0) >= 1 and a.get("evidence"):
            try:
                from agent.memory import incident_rag
                sims = incident_rag.similar_incidents(a["evidence"], a.get("asset_id", ""), top_k=2)
                if sims:
                    compact["similar_past_incidents"] = sims
            except Exception:
                pass
        compact_assets.append(compact)

    context = {
        "cycle_id": cycle_id,
        "timestamp": timestamp,
        "total_assets": len(compact_assets),
        "assets": compact_assets,
    }

    digest = loghub_engine.failure_pattern_digest(max_datasets=6, lines_per_dataset=1)
    digest_block = ""
    if digest:
        digest_block = (
            "\n\nREAL LOG FAILURE SIGNATURES (sampled from the production datasets "
            "that calibrate this environment):\n" + digest
        )

    return (
        SYSTEM_PROMPT
        + digest_block
        + "\n\nCURRENT CYCLE DATA:\n"
        + json.dumps(context, indent=2)
        + "\n\nReason about each asset and respond with the JSON object now:"
    )


# ---------------------------------------------------------------------------
# LLM-owned reasoning, with a guardrail that FLAGS rather than overwrites.
# ---------------------------------------------------------------------------
def _apply_guardrail(model_action: str, asset: dict, risk: dict) -> tuple:
    """
    Returns (final_action, diverged, guardrail_note).
    Policy:
      • If the model's action is invalid → replace with deterministic action.
      • If a hard safety floor applies and the model called it LOWER → raise to
        the floor, flag divergence (the model under-escalated a real incident).
      • Otherwise the model's action STANDS, even if it disagrees with the
        soft rule engine — and if it disagrees we record it for review.
    """
    ref_action = decide_action(asset, risk)

    if model_action not in VALID_ACTIONS:
        return ref_action, False, f"model returned invalid action; used policy default {ref_action}"

    floor = _hard_floor_action(asset, risk)
    if floor and ACTION_SEVERITY[model_action] < ACTION_SEVERITY[floor]:
        return floor, True, (
            f"SAFETY FLOOR: model chose {model_action} but "
            f"{asset.get('consecutive_failures')} consecutive failures on a tier-{risk['tier']} "
            f"asset mandate {floor}. Raised."
        )

    if model_action != ref_action:
        # Legitimate, allowed disagreement — surface it, don't erase it.
        return model_action, True, (
            f"model chose {model_action}; deterministic policy would say {ref_action}. "
            f"Model decision kept (within safety bounds)."
        )

    return model_action, False, ""


def _validate_and_repair(result: dict, asset_batch: list, cycle_id: str, provider: str, model: str) -> dict:
    by_id = {a.get("asset_id"): a for a in asset_batch}
    raw_assessments = result.get("assessments") or result.get("decisions") or []

    repaired = []
    seen = set()
    diverged_count = 0

    for item in raw_assessments:
        asset_id = item.get("asset_id")
        asset = by_id.get(asset_id)
        if asset is None:
            continue  # hallucinated asset — drop
        seen.add(asset_id)
        risk = compute_risk(asset)

        model_action = item.get("action")
        final_action, diverged, note = _apply_guardrail(model_action, asset, risk)
        if diverged:
            diverged_count += 1

        repaired.append({
            "asset_id": asset_id,
            "asset_name": asset.get("asset_name", asset_id),
            "tier": risk["tier"],
            "dataset": asset.get("dataset", "core"),
            "rpo_consumed_pct": risk["rpo_consumed_pct"],
            "risk_score": risk["risk_score"],
            "action": final_action,
            "model_action": model_action if model_action in VALID_ACTIONS else None,
            "diverged": diverged,
            "guardrail_note": note or None,
            "explanation": (item.get("explanation") or "").strip()
                           or f"Risk {risk['risk_score']} on tier {risk['tier']} asset.",
            "confidence": float(item.get("confidence", 0.8) or 0.8),
            "consecutive_failures": asset.get("consecutive_failures", 0),
            "evidence": asset.get("evidence"),
            "fallback_mode": False,
        })

    # Assets the model skipped: fill deterministically so the fleet is complete.
    for asset in asset_batch:
        if asset.get("asset_id") in seen:
            continue
        risk = compute_risk(asset)
        repaired.append({
            "asset_id": asset.get("asset_id"),
            "asset_name": asset.get("asset_name", asset.get("asset_id")),
            "tier": risk["tier"],
            "dataset": asset.get("dataset", "core"),
            "rpo_consumed_pct": risk["rpo_consumed_pct"],
            "risk_score": risk["risk_score"],
            "action": decide_action(asset, risk),
            "model_action": None,
            "diverged": False,
            "guardrail_note": "model omitted this asset; filled deterministically",
            "explanation": "Filled deterministically (model omitted this asset).",
            "confidence": 0.7,
            "consecutive_failures": asset.get("consecutive_failures", 0),
            "evidence": asset.get("evidence"),
            "fallback_mode": True,
        })

    critical = sum(1 for a in repaired if "ESCALATE" in a["action"])
    healthy = sum(1 for a in repaired if a["action"] == "NONE")
    summary = (result.get("summary") or "").strip() or _auto_summary(repaired)

    # Close the agentic loop: remember what we decided this cycle.
    decision_memory.record_batch(repaired, cycle_id)
    _remember_incidents(repaired)

    return {
        "cycle_id": cycle_id,
        "assessments": repaired,
        "summary": summary,
        "critical_count": critical,
        "healthy_count": healthy,
        "diverged_count": diverged_count,
        "fallback_mode": False,
        "provider": provider,
        "model": model,
    }


def _auto_summary(assessments: list) -> str:
    total = len(assessments)
    p1 = sum(1 for a in assessments if a["action"] == "ESCALATE_P1")
    p2 = sum(1 for a in assessments if a["action"] == "ESCALATE_P2")
    healthy = sum(1 for a in assessments if a["action"] == "NONE")
    diverged = sum(1 for a in assessments if a.get("diverged"))
    tail = f" ({diverged} model/policy divergence(s) flagged for review)" if diverged else ""
    if p1:
        return f"{p1} P1 and {p2} P2 across {total} assets — immediate attention required.{tail}"
    if p2:
        return f"{p2} P2 escalation(s) across {total} assets; {healthy} healthy.{tail}"
    return f"Fleet stable — {healthy}/{total} assets healthy, no escalations.{tail}"


def _remember_incidents(assessments: list):
    """Feed actionable outcomes into the searchable incident history (RAG)."""
    if not _RAG_ENABLED:
        return
    try:
        from agent.memory import incident_rag
        for a in assessments:
            incident_rag.remember_incident(a)
    except Exception:
        pass


def _merge_batch_results(parts: list, cycle_id: str) -> dict:
    """Merge per-batch LLM results into one fleet-wide result."""
    assessments = []
    providers = set()
    diverged = 0
    any_llm = False
    for r in parts:
        assessments.extend(r.get("assessments", []))
        providers.add(r.get("provider", "unknown"))
        diverged += r.get("diverged_count", 0)
        if not r.get("fallback_mode", True):
            any_llm = True
    critical = sum(1 for a in assessments if "ESCALATE" in a["action"])
    healthy = sum(1 for a in assessments if a["action"] == "NONE")
    return {
        "cycle_id": cycle_id,
        "assessments": assessments,
        "summary": _auto_summary(assessments),
        "critical_count": critical,
        "healthy_count": healthy,
        "diverged_count": diverged,
        "fallback_mode": not any_llm,
        "provider": "+".join(sorted(p for p in providers if p)),
        "model": parts[0].get("model", "") if parts else "",
    }


def reason(asset_batch: list) -> dict:
    """Try SLM → LLM chain; on any failure drop cleanly to the rule engine.
    Fleets larger than SENTRIX_BATCH_SIZE are reasoned in chunks so free-tier
    models never receive an unanswerable 100-asset mega-prompt."""
    cycle_id = uuid.uuid4().hex[:8]

    if not asset_batch:
        return {
            "cycle_id": cycle_id, "assessments": [], "summary": "No assets to assess.",
            "critical_count": 0, "healthy_count": 0, "diverged_count": 0, "fallback_mode": False,
            "provider": "none", "model": "none",
        }

    from agent import config
    if config.USE_SLM:
        try:
            from agent.reasoning import slm_local
            if slm_local.is_available():
                res = slm_local.reason_fleet(asset_batch, max_assets=config.SLM_MAX_ASSETS)
                decision_memory.record_batch(res.get("assessments", []), res.get("cycle_id", cycle_id))
                return res
        except Exception as e:
            _log.warning("local SLM unavailable (%s); falling back to provider chain", e)

    batch_size = max(1, getattr(config, "BATCH_SIZE", 25))
    chunks = [asset_batch[i:i + batch_size] for i in range(0, len(asset_batch), batch_size)]
    parts = []
    for idx, chunk in enumerate(chunks):
        sub_id = cycle_id if len(chunks) == 1 else f"{cycle_id}-b{idx+1}"
        prompt = build_prompt(chunk, sub_id)
        try:
            parsed = llm_providers.call_llm_json(prompt)
            provider = parsed.pop("_provider", "unknown")
            model = parsed.pop("_model", "unknown")
            parts.append(_validate_and_repair(parsed, chunk, sub_id, provider, model))
            continue
        except llm_providers.LLMAllProvidersFailedError as e:
            if idx == 0:
                _log.warning("all LLM providers unavailable (%s); using rule engine", e)
            # No provider at all -> don't retry per-chunk, fall through for the rest too.
            parts.append(rule_engine_fallback(chunk, sub_id))
            parts.extend(rule_engine_fallback(c, f"{cycle_id}-b{j+1}")
                         for j, c in enumerate(chunks[idx+1:], start=idx+1))
            break
        except Exception as e:
            _log.warning("LLM error on batch %s/%s (%s); rule engine for this batch", idx + 1, len(chunks), e)
            parts.append(rule_engine_fallback(chunk, sub_id))
    if len(parts) == 1:
        return parts[0]
    return _merge_batch_results(parts, cycle_id)


async def reason_async(asset_batch: list) -> dict:
    """Non-blocking wrapper: runs the (network-bound) reasoning off the event
    loop so API endpoints and WebSocket broadcasts never stall behind an LLM."""
    import asyncio
    return await asyncio.to_thread(reason, asset_batch)


def rule_engine_fallback(asset_batch: list, cycle_id: str = None) -> dict:
    """Deterministic fallback — keeps SENTRIX fully operational with zero deps."""
    cycle_id = cycle_id or uuid.uuid4().hex[:8]
    assessments = []
    for asset in asset_batch:
        risk = compute_risk(asset)
        action = decide_action(asset, risk)
        evidence = asset.get("evidence")
        if action == "NONE":
            explanation = f"Healthy — {risk['rpo_consumed_pct']:.0f}% of RPO window consumed, no failures."
        elif evidence:
            explanation = f"Rule engine flagged {action} (score {risk['risk_score']}). Log evidence: {evidence}"
        else:
            explanation = f"Rule engine flagged {action} — risk score {risk['risk_score']} on tier {risk['tier']} asset."
        assessments.append({
            "asset_id": asset.get("asset_id"),
            "asset_name": asset.get("asset_name", asset.get("asset_id")),
            "tier": risk["tier"],
            "dataset": asset.get("dataset", "core"),
            "rpo_consumed_pct": risk["rpo_consumed_pct"],
            "risk_score": risk["risk_score"],
            "action": action,
            "model_action": None,
            "diverged": False,
            "guardrail_note": None,
            "explanation": explanation,
            "confidence": 0.75,
            "consecutive_failures": asset.get("consecutive_failures", 0),
            "evidence": evidence,
            "fallback_mode": True,
        })

    critical = sum(1 for a in assessments if "ESCALATE" in a["action"])
    healthy = sum(1 for a in assessments if a["action"] == "NONE")
    decision_memory.record_batch(assessments, cycle_id)
    _remember_incidents(assessments)
    return {
        "cycle_id": cycle_id,
        "assessments": assessments,
        "summary": _auto_summary(assessments),
        "critical_count": critical,
        "healthy_count": healthy,
        "diverged_count": 0,
        "fallback_mode": True,
        "provider": "rule_engine",
        "model": "deterministic",
    }


def reason_with_fallback(asset_batch: list) -> dict:
    """Public entry point used by the agent loop and the API."""
    return reason(asset_batch)


def test_connection():
    test_asset = [{
        "asset_id": "TEST-001", "asset_name": "Test Server", "dataset": "core",
        "tier": 2, "criticality_score": 50, "rpo_target_hours": 8,
        "hours_since_last_backup": 4, "consecutive_failures": 0,
        "restore_test_days_overdue": 0,
    }]
    result = reason(test_asset)
    if result.get("assessments") is not None:
        mode = "rule-engine" if result.get("fallback_mode") else f"LLM ({result.get('provider')}/{result.get('model')})"
        print(f"SENTRIX reasoning core OK [{mode}] — {result['summary']}")
        return True
    print("SENTRIX reasoning core FAILED")
    return False


if __name__ == "__main__":
    test_connection()
