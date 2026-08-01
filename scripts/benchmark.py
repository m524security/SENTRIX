"""
SENTRIX — Reasoning Benchmark Harness
=====================================
Answers "how good is the brain?" with numbers instead of vibes. It generates a
held-out scenario suite from the same expert policy the training data uses
(different seed — the model never saw these), runs a chosen backend over it,
and scores what actually matters for an ops agent:

  action_accuracy       exact-match on the expert action
  safety_violations     chose BELOW the hard safety floor (worst failure class)
  under_escalation      called lower severity than expert (missed risk)
  over_escalation       called higher severity than expert (alert fatigue)
  weighted_score        accuracy penalised 5x for safety violations
  json_validity         % of parseable model outputs (LLM/SLM backends)
  latency p50 / p95     seconds per decision

Backends:
  rule   — deterministic guardrail engine (the floor every model must beat)
  llm    — whatever provider chain is configured in .env
  slm    — the locally fine-tuned adapter (requires torch + trained adapter)

HOW TO BENCHMARK (the full workflow):
  1. Baseline the rule engine:      python scripts/benchmark.py --backend rule -n 300
  2. Benchmark your provider:       python scripts/benchmark.py --backend llm -n 300
  3. Train, then benchmark the SLM: python scripts/benchmark.py --backend slm -n 300
  4. Compare the JSON reports in data/benchmarks/ — a backend earns its place
     by beating the rule engine on weighted_score with ZERO safety violations.
  Same seed => same scenarios => fair comparison across backends and over time.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.reasoning.reasoning_core import (  # noqa: E402
    compute_risk, decide_action, _hard_floor_action, ACTION_SEVERITY, VALID_ACTIONS)
from scripts.slm_dataset import _random_asset, expert_action, _memory_block, \
    TRANSIENT_EVIDENCE, CORRUPTION_EVIDENCE, EVIDENCE_BY_ACTION  # noqa: E402

OUT_DIR = ROOT / "data" / "benchmarks"


# ---------------------------------------------------------------------------
# Held-out scenario generation (same expert policy, benchmark-only seed)
# ---------------------------------------------------------------------------
def build_suite(n: int, seed: int) -> list:
    import random
    random.seed(seed)
    suite = []
    families = ["plain", "memory", "transient", "corruption", "sparse"]
    for i in range(n):
        fam = families[i % len(families)]
        asset = _random_asset(i)
        memory = _memory_block(random) if fam == "memory" else None
        if fam == "transient":
            asset["consecutive_failures"] = 1
            evidence = random.choice(TRANSIENT_EVIDENCE)
        elif fam == "corruption":
            evidence = random.choice(CORRUPTION_EVIDENCE)
        else:
            risk = compute_risk(asset)
            evidence = random.choice(
                EVIDENCE_BY_ACTION.get(decide_action(asset, risk), EVIDENCE_BY_ACTION["NONE"]))
        risk = compute_risk(asset)
        truth = expert_action(asset, risk, memory, evidence)
        suite.append({"asset": asset, "memory": memory, "evidence": evidence,
                      "family": fam, "truth": truth,
                      "floor": _hard_floor_action(asset, risk)})
    return suite


# ---------------------------------------------------------------------------
# Backends under test
# ---------------------------------------------------------------------------
def backend_rule(case: dict) -> str:
    return decide_action(case["asset"], compute_risk(case["asset"]))


def _single_asset_prompt(case: dict) -> str:
    payload = {k: case["asset"].get(k) for k in (
        "asset_id", "asset_name", "tier", "criticality_score", "rpo_target_hours",
        "hours_since_last_backup", "consecutive_failures", "restore_test_days_overdue")}
    payload["log_evidence"] = case["evidence"]
    if case["memory"]:
        payload["memory"] = case["memory"]
    return (
        "You are SENTRIX, an autonomous backup-recovery agent. Decide the action for this asset. "
        "Actions: NONE, WARN, RETRY_BACKUP, SCHEDULE_RESTORE_TEST, ESCALATE_P2, ESCALATE_P1, MANUAL_REVIEW. "
        "Weigh evidence, memory trend, tier and criticality; a single transient failure deserves RETRY_BACKUP; "
        "negative integrity signals (checksum mismatch, corruption) deserve MANUAL_REVIEW or higher; "
        "3+ consecutive failures on tier 1 => ESCALATE_P1, tier 2 => ESCALATE_P2.\n"
        "Respond ONLY with JSON: {\"action\": \"...\", \"explanation\": \"one sentence\"}\n\n"
        "ASSET:\n" + json.dumps(payload)
    )


def backend_llm(case: dict) -> str:
    from agent.reasoning import llm_providers
    parsed = llm_providers.call_llm_json(_single_asset_prompt(case))
    return str(parsed.get("action", "")).strip()


def backend_slm(case: dict) -> str:
    from agent.reasoning import slm_local
    if not slm_local.is_available():
        raise RuntimeError("SLM adapter not available (train it first: scripts/slm_train.py)")
    result = slm_local.reason_fleet([{**case["asset"], "evidence": case["evidence"]}], max_assets=1)
    assessments = result.get("assessments", [])
    return assessments[0].get("model_action") or assessments[0].get("action", "") if assessments else ""


BACKENDS = {"rule": backend_rule, "llm": backend_llm, "slm": backend_slm}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def run(backend_name: str, n: int, seed: int) -> dict:
    suite = build_suite(n, seed)
    fn = BACKENDS[backend_name]
    latencies, records = [], []
    correct = safety = under = over = invalid = 0
    per_family = {}

    for case in suite:
        t0 = time.perf_counter()
        try:
            predicted = fn(case)
        except Exception as e:
            predicted = f"__error:{e}"
        latencies.append(time.perf_counter() - t0)

        truth, floor = case["truth"], case["floor"]
        fam = per_family.setdefault(case["family"], {"n": 0, "correct": 0})
        fam["n"] += 1

        if predicted not in VALID_ACTIONS:
            invalid += 1
            records.append({"family": case["family"], "truth": truth, "predicted": predicted, "valid": False})
            continue

        if predicted == truth:
            correct += 1
            fam["correct"] += 1
        elif ACTION_SEVERITY[predicted] < ACTION_SEVERITY[truth]:
            under += 1
        else:
            over += 1
        if floor and ACTION_SEVERITY[predicted] < ACTION_SEVERITY[floor]:
            safety += 1
        records.append({"family": case["family"], "truth": truth, "predicted": predicted, "valid": True})

    total = len(suite)
    acc = correct / total
    weighted = max(0.0, acc - 5.0 * (safety / total))
    lat_sorted = sorted(latencies)
    report = {
        "backend": backend_name,
        "n": total,
        "seed": seed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action_accuracy": round(acc, 4),
        "safety_violations": safety,
        "safety_violation_rate": round(safety / total, 4),
        "under_escalation_rate": round(under / total, 4),
        "over_escalation_rate": round(over / total, 4),
        "json_validity": round(1 - invalid / total, 4),
        "weighted_score": round(weighted, 4),
        "latency_p50_s": round(statistics.median(lat_sorted), 4),
        "latency_p95_s": round(lat_sorted[int(0.95 * (total - 1))], 4),
        "per_family_accuracy": {k: round(v["correct"] / v["n"], 3) for k, v in per_family.items()},
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"bench_{backend_name}_{int(time.time())}.json"
    out.write_text(json.dumps({"report": report, "records": records}, indent=2))

    print(f"\nSENTRIX BENCHMARK — backend={backend_name}  n={total}  seed={seed}")
    print("-" * 62)
    for k in ("action_accuracy", "weighted_score", "safety_violation_rate",
              "under_escalation_rate", "over_escalation_rate", "json_validity",
              "latency_p50_s", "latency_p95_s"):
        print(f"  {k:26} {report[k]}")
    print("  per-family accuracy:")
    for k, v in sorted(report["per_family_accuracy"].items()):
        print(f"    {k:12} {v}")
    print(f"\n  full report -> {out.relative_to(ROOT)}")
    if safety:
        print(f"  !! {safety} SAFETY VIOLATION(S) — this backend must not run unguarded.")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Benchmark a SENTRIX reasoning backend.")
    ap.add_argument("--backend", choices=list(BACKENDS), default="rule")
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=90210)  # differs from training seed
    args = ap.parse_args()
    run(args.backend, args.n, args.seed)
