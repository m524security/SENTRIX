"""
SENTRIX — Local SLM Reasoner (fine-tuned)
========================================
Runs SENTRIX's recovery-risk reasoning on a *locally fine-tuned* Small Language
Model (the LoRA adapter produced by scripts/slm_train.py), with zero cloud
dependency. This is the runtime for the "fine-tune an SLM on SENTRIX data" path.

Design mirrors the rest of the agent: every import is lazy and every failure
degrades gracefully, so importing this module never breaks the system even when
torch/peft aren't installed or no adapter has been trained yet.

The SLM is trained per-asset, so `reason_fleet()` loops over assets and assembles
a result dict in the exact shape `reasoning_core` returns — meaning the agent
loop and the API can consume it interchangeably. Decisions are still validated
against the deterministic risk math so a shaky generation can never produce an
out-of-policy action.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from agent.reasoning.reasoning_core import (compute_risk, decide_action,
                                            VALID_ACTIONS, _auto_summary)

_ADAPTER_DIR = Path(__file__).resolve().parent.parent.parent / "models" / "sentrix-slm-lora"

_model = None
_tok = None
_load_error: Optional[str] = None

# Imported lazily to avoid a hard dependency at module import time.
_SLM_SYSTEM_PROMPT = (
    "You are SENTRIX, an autonomous IT backup-recovery agent. For the given asset "
    "you output ONE JSON object and nothing else:\n"
    '{"asset_id","action","risk_score","rpo_consumed_pct","explanation","confidence"}\n'
    "action is one of: NONE, WARN, RETRY_BACKUP, SCHEDULE_RESTORE_TEST, "
    "ESCALATE_P2, ESCALATE_P1, MANUAL_REVIEW.\n"
    "Risk policy:\n"
    "- risk_score = rpo_consumed_pct * (criticality/100) * tier_multiplier "
    "(tier1=2.0, tier2=1.5, tier3=1.0, tier4=0.5); "
    "rpo_consumed_pct = hours_since_last_backup / rpo_target_hours * 100.\n"
    "- score>=501 => ESCALATE_P1; >=200 => ESCALATE_P2; >=50 => WARN; else NONE.\n"
    "- 3+ consecutive failures: tier1 => ESCALATE_P1, tier2 => ESCALATE_P2.\n"
    "- If action would be NONE/WARN and a restore test is overdue => SCHEDULE_RESTORE_TEST.\n"
    "- Tier 4 with score<300 => NONE.\n"
    "Explanation: one clear sentence citing the real numbers."
)


def is_available() -> bool:
    """True if an adapter has been trained and the ML stack can load it."""
    if not _ADAPTER_DIR.exists():
        return False
    try:
        import torch  # noqa: F401
        import peft  # noqa: F401
        return True
    except ImportError:
        return False


def _load():
    global _model, _tok, _load_error
    if _model is not None:
        return _model, _tok
    if _load_error is not None:
        raise RuntimeError(_load_error)
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel

        base_file = _ADAPTER_DIR / "sentrix_base.txt"
        if not base_file.exists():
            raise RuntimeError("adapter missing sentrix_base.txt — retrain with scripts/slm_train.py")
        base = base_file.read_text(encoding="utf-8").strip()

        _tok = AutoTokenizer.from_pretrained(str(_ADAPTER_DIR))
        model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float32)
        _model = PeftModel.from_pretrained(model, str(_ADAPTER_DIR))
        _model.eval()
        return _model, _tok
    except Exception as e:
        _load_error = f"{type(e).__name__}: {e}"
        raise


def _generate(asset: dict) -> Optional[dict]:
    import torch
    model, tok = _load()
    user = json.dumps({k: asset.get(k) for k in (
        "asset_id", "asset_name", "tier", "criticality_score", "rpo_target_hours",
        "hours_since_last_backup", "consecutive_failures", "restore_test_days_overdue",
    )} | ({"log_evidence": asset["evidence"]} if asset.get("evidence") else {}),
        separators=(",", ":"))
    msgs = [{"role": "system", "content": _SLM_SYSTEM_PROMPT},
            {"role": "user", "content": user}]
    ids = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                  return_tensors="pt", return_dict=True)
    in_len = ids["input_ids"].shape[1]
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=160, do_sample=False,
                             pad_token_id=tok.pad_token_id or tok.eos_token_id)
    text = tok.decode(out[0][in_len:], skip_special_tokens=True).strip()
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        return None


def reason_asset(asset: dict) -> dict:
    """Single-asset reasoning, validated against deterministic policy."""
    from agent import config
    risk = compute_risk(asset)
    truth_action = decide_action(asset, risk)
    gen = _generate(asset) or {}
    action = gen.get("action")
    if action not in VALID_ACTIONS:
        action = truth_action  # repair: never emit an out-of-policy action
    matched = action == truth_action
    # Safety guardrail: snap a disagreeing action to deterministic policy,
    # keeping the SLM's explanation. Lets a small model run the agent safely.
    explanation = (gen.get("explanation") or "").strip() or \
        f"Risk score {risk['risk_score']} on tier {risk['tier']} asset."
    if config.SLM_STRICT and not matched:
        # Keep the SLM's reasoning but make the policy override explicit so the
        # explanation never contradicts the final action shown on the dashboard.
        explanation = (f"[policy-enforced {truth_action}] " + explanation +
                       f" (SLM proposed {action}; deterministic policy requires {truth_action}.)")
        action = truth_action
    return {
        "asset_id": asset.get("asset_id"),
        "asset_name": asset.get("asset_name", asset.get("asset_id")),
        "tier": risk["tier"],
        "dataset": asset.get("dataset", "core"),
        "rpo_consumed_pct": risk["rpo_consumed_pct"],
        "risk_score": risk["risk_score"],
        "action": action,
        "explanation": explanation,
        "confidence": float(gen.get("confidence", 0.85) or 0.85),
        "evidence": asset.get("evidence"),
        "fallback_mode": False,
        "slm_action_matched_policy": matched,
    }


def reason_fleet(asset_batch: list, max_assets: int = 24) -> dict:
    """
    Reason over a fleet with the local fine-tuned SLM. CPU generation is slow,
    so by default only the first `max_assets` are SLM-reasoned; the remainder
    are filled deterministically (identical math) so the dashboard is complete.
    """
    cycle_id = uuid.uuid4().hex[:8]
    assessments = []
    slm_count = 0
    for i, asset in enumerate(asset_batch):
        if i < max_assets:
            try:
                assessments.append(reason_asset(asset))
                slm_count += 1
                continue
            except Exception:
                pass  # fall through to deterministic fill
        risk = compute_risk(asset)
        assessments.append({
            "asset_id": asset.get("asset_id"),
            "asset_name": asset.get("asset_name", asset.get("asset_id")),
            "tier": risk["tier"], "dataset": asset.get("dataset", "core"),
            "rpo_consumed_pct": risk["rpo_consumed_pct"], "risk_score": risk["risk_score"],
            "action": decide_action(asset, risk),
            "explanation": "Filled deterministically (beyond SLM batch budget).",
            "confidence": 0.7, "evidence": asset.get("evidence"), "fallback_mode": True,
        })

    critical = sum(1 for a in assessments if "ESCALATE" in a["action"])
    healthy = sum(1 for a in assessments if a["action"] == "NONE")
    return {
        "cycle_id": cycle_id,
        "assessments": assessments,
        "summary": _auto_summary(assessments),
        "critical_count": critical,
        "healthy_count": healthy,
        "fallback_mode": False,
        "provider": "slm_local",
        "model": f"lora:{_ADAPTER_DIR.name}",
        "slm_reasoned": slm_count,
    }


def status() -> dict:
    return {
        "adapter_present": _ADAPTER_DIR.exists(),
        "adapter_dir": str(_ADAPTER_DIR),
        "loaded": _model is not None,
        "load_error": _load_error,
        "available": is_available(),
    }
