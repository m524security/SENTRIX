"""
SENTRIX — Decision Memory (the loop that makes it an agent, not a pipeline)
===========================================================================
The original system wrote an audit trail that nobody ever read back. An agent
that escalated an asset yesterday, watched it recover, and reasons about it
identically today is not an agent — it is a stateless function with a log file.

This module closes that loop. It maintains a compact, rolling per-asset memory
of recent decisions and outcomes, and exposes it in two forms:

  • `recall(asset_id)`  -> the short decision history for one asset, injected
                            into the reasoning prompt so the model can say
                            "escalated twice in the last 3 cycles, still failing".
  • `outcome_signal()`  -> fleet-level drift signals (an asset whose risk is
                            climbing cycle-over-cycle) the agent can act on
                            *before* a threshold is crossed.

It is deliberately dependency-free and process-local with an optional JSONL
mirror, mirroring the rest of SENTRIX's "never goes dark" philosophy. When a
real datastore (Redis / Supabase) is wired in, `_persist` / `_load` are the two
seams to swap.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

_MEM_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "decision_memory.jsonl"
_MEM_PATH.parent.mkdir(parents=True, exist_ok=True)

# Per-asset rolling window of the last N assessments.
_WINDOW = 8
_memory: dict = defaultdict(lambda: deque(maxlen=_WINDOW))
_lock = threading.Lock()
_loaded = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_once():
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _MEM_PATH.exists():
        return
    try:
        with _MEM_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                _memory[rec["asset_id"]].append(rec)
    except Exception:
        pass


def _persist(rec: dict):
    try:
        with _MEM_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def record(assessment: dict, cycle_id: str = ""):
    """Store one assessment into the asset's rolling memory."""
    _load_once()
    aid = assessment.get("asset_id")
    if not aid:
        return
    rec = {
        "asset_id": aid,
        "cycle_id": cycle_id,
        "timestamp": _now(),
        "action": assessment.get("action", "NONE"),
        "risk_score": float(assessment.get("risk_score", 0) or 0),
        "rpo_consumed_pct": float(assessment.get("rpo_consumed_pct", 0) or 0),
        "consecutive_failures": int(assessment.get("consecutive_failures", 0) or 0),
        "diverged": bool(assessment.get("diverged", False)),
    }
    with _lock:
        _memory[aid].append(rec)
    _persist(rec)


def record_batch(assessments: list, cycle_id: str = ""):
    for a in assessments:
        record(a, cycle_id)


def recall(asset_id: str) -> dict:
    """
    Compact recent history for one asset, in the shape the reasoning prompt
    consumes. Returns None-ish empties when the asset is new so the prompt
    builder can skip it cleanly.
    """
    _load_once()
    hist = list(_memory.get(asset_id, []))
    if not hist:
        return {"seen_before": False}

    scores = [h["risk_score"] for h in hist]
    actions = [h["action"] for h in hist]
    escalations = sum(1 for a in actions if "ESCALATE" in a)
    trend = "flat"
    if len(scores) >= 2:
        delta = scores[-1] - scores[0]
        if delta > 15:
            trend = "rising"
        elif delta < -15:
            trend = "falling"

    return {
        "seen_before": True,
        "cycles_remembered": len(hist),
        "last_action": actions[-1],
        "recent_actions": actions[-4:],
        "escalations_in_window": escalations,
        "risk_trend": trend,
        "risk_now": round(scores[-1], 1),
        "risk_window_ago": round(scores[0], 1),
        # A persistent-failure flag is exactly the kind of context a threshold
        # rule can't see but an agent should reason about.
        "persistent_incident": all(h["consecutive_failures"] >= 1 for h in hist[-3:]) if len(hist) >= 3 else False,
    }


def outcome_signal(assets: list) -> list:
    """
    Fleet-level drift detector. Surfaces assets whose remembered risk is
    climbing steadily even if they haven't tripped a hard threshold yet —
    the agent's early-warning input, distinct from the current snapshot.
    """
    _load_once()
    signals = []
    for a in assets:
        aid = a.get("asset_id")
        mem = recall(aid)
        if mem.get("seen_before") and mem.get("risk_trend") == "rising" and mem.get("risk_now", 0) >= 30:
            signals.append({
                "asset_id": aid,
                "asset_name": a.get("asset_name", aid),
                "risk_trend": "rising",
                "risk_now": mem["risk_now"],
                "risk_window_ago": mem["risk_window_ago"],
                "note": f"Risk climbed {mem['risk_window_ago']}→{mem['risk_now']} over "
                        f"{mem['cycles_remembered']} cycles; act before breach.",
            })
    signals.sort(key=lambda s: s["risk_now"], reverse=True)
    return signals


def snapshot(limit: int = 200) -> dict:
    """Debug / API view of what the agent currently remembers."""
    _load_once()
    return {
        "assets_tracked": len(_memory),
        "window": _WINDOW,
        "sample": {aid: list(hist)[-3:] for aid, hist in list(_memory.items())[:limit]},
    }
