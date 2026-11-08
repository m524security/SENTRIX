"""
SENTRIX — SLM Training Dataset Builder
=====================================
Generates a supervised fine-tuning (SFT) dataset that teaches a Small Language
Model to reproduce SENTRIX's recovery-risk reasoning *exactly*.

Why this works without any cloud labelling:
    SENTRIX already has a deterministic source of truth for every decision —
    `reasoning_core.compute_risk()` + `reasoning_core.decide_action()`. We
    sample thousands of realistic asset states, compute the ground-truth
    decision + a natural-language rationale, and emit chat-format examples.
    The SLM learns SENTRIX's risk policy AND its strict JSON output format.

Output: data/slm/train.jsonl, data/slm/val.jsonl
    Each line: {"messages": [system, user, assistant]} in OpenAI chat format,
    which both TRL/transformers chat templates and Ollama understand.

Run:
    venv\\Scripts\\python scripts\\slm_dataset.py --n 4000
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.reasoning.reasoning_core import compute_risk, decide_action  # ground truth

OUT_DIR = ROOT / "data" / "slm"

# Compact policy prompt the SLM is trained against. Far shorter than the full
# operator prompt so a 0.5-1.5B model can attend to it on CPU, but it carries
# the exact rules the deterministic engine enforces.
SLM_SYSTEM_PROMPT = (
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

ASSET_POOL = [
    ("SAP ERP Production", "database"), ("CRM Database Primary", "database"),
    ("Oracle Finance DB", "database"), ("Payroll Database", "database"),
    ("Exchange Mail Server", "vm"), ("HR SQL Server", "database"),
    ("K8s Production Cluster", "container"), ("NAS File Share", "nas"),
    ("MongoDB Analytics", "database"), ("Dev Server 01", "vm"),
    ("Redis Cache Node", "cache"), ("Jenkins CI Host", "vm"),
    ("Data Lake Bronze", "storage"), ("Auth Service DB", "database"),
    ("Billing Service", "vm"), ("Logging Pipeline", "container"),
]

EVIDENCE_BY_ACTION = {
    "ESCALATE_P1": [
        "FATAL backup agent unreachable; snapshot aborted after 3 retries",
        "ERROR repository offline — last good restore point exceeds RPO target",
        "CRITICAL checksum verification failed on full backup set",
    ],
    "ESCALATE_P2": [
        "ERROR incremental backup timed out; window compliance breached",
        "WARN replication lag growing, last successful job delayed",
    ],
    "WARN": [
        "WARN backup completed late, RPO window partially consumed",
        "INFO transient network reset during transfer, job recovered",
    ],
    "SCHEDULE_RESTORE_TEST": [
        "INFO restore-test interval exceeded; integrity proof stale",
    ],
    "NONE": [
        "INFO nightly incremental completed successfully, checksum verified",
        "INFO snapshot OK, RPO well within target",
    ],
}


def _explanation(asset: dict, risk: dict, action: str) -> str:
    name = asset["asset_name"]
    tier = risk["tier"]
    score = risk["risk_score"]
    pct = risk["rpo_consumed_pct"]
    consec = asset.get("consecutive_failures", 0)
    overdue = asset.get("restore_test_days_overdue", 0)
    ev = asset.get("log_evidence")
    ev_clause = f" Log shows: {ev[:80]}." if ev else ""
    if action == "ESCALATE_P1":
        if consec >= 3:
            return (f"{name} hit {consec} consecutive backup failures on a mission-critical Tier {tier} "
                    f"asset; a persistent streak at this tier is a page-now situation.{ev_clause}")
        return (f"{name} is a Tier {tier} asset at risk {score} ({pct:.0f}% of its RPO window gone) — "
                f"the business impact justifies an immediate P1 page.{ev_clause}")
    if action == "ESCALATE_P2":
        if consec >= 3:
            return (f"{name} reached {consec} consecutive failures on a Tier {tier} asset — raise a P2 "
                    f"ticket before it degrades further.{ev_clause}")
        return (f"{name} scored {score} ({pct:.0f}% RPO consumed) on a Tier {tier} asset — high but not "
                f"page-worthy; a P2 ticket is the proportionate call.{ev_clause}")
    if action == "WARN":
        return (f"{name} is at {pct:.0f}% of its RPO window (score {score}); not urgent yet, "
                f"but worth watching closely this cycle.{ev_clause}")
    if action == "SCHEDULE_RESTORE_TEST":
        return (f"{name} is healthy on RPO but its restore drill is {overdue} day(s) overdue — recovery "
                f"readiness is unverified, so book a restore test.")
    if action == "RETRY_BACKUP":
        return (f"{name} saw a single transient failure worth one automatic retry before escalating.{ev_clause}")
    if action == "MANUAL_REVIEW":
        return f"{name} sends an ambiguous integrity signal; a human should look before automating a call."
    return (f"{name} is healthy — only {pct:.0f}% of the RPO window consumed and no failures, so log only.")


def _random_asset(i: int) -> dict:
    name, atype = random.choice(ASSET_POOL)
    tier = random.choices([1, 2, 3, 4], weights=[0.25, 0.3, 0.25, 0.2])[0]
    crit = {1: (80, 99), 2: (55, 80), 3: (30, 55), 4: (5, 30)}[tier]
    rpo_target = random.choice({1: [2, 4], 2: [8, 12], 3: [24], 4: [48, 72]}[tier])
    # Spread hours so we hit every decision branch.
    roll = random.random()
    if roll < 0.45:
        hours = rpo_target * random.uniform(0.05, 0.7)      # healthy
    elif roll < 0.7:
        hours = rpo_target * random.uniform(0.7, 1.8)       # warn-ish
    elif roll < 0.9:
        hours = rpo_target * random.uniform(1.8, 5.0)       # P2/P1
    else:
        hours = rpo_target * random.uniform(5.0, 12.0)      # severe
    consec = random.choices([0, 1, 2, 3, 4], weights=[0.6, 0.15, 0.1, 0.1, 0.05])[0]
    overdue = random.choices([0, random.randint(1, 40)], weights=[0.7, 0.3])[0]
    return {
        "asset_id": f"ASSET-{i:04d}",
        "asset_name": name,
        "asset_type": atype,
        "tier": tier,
        "criticality_score": random.randint(*crit),
        "rpo_target_hours": rpo_target,
        "hours_since_last_backup": round(hours, 2),
        "consecutive_failures": consec,
        "restore_test_days_overdue": overdue,
    }


def _example(asset: dict) -> dict:
    risk = compute_risk(asset)
    action = decide_action(asset, risk)
    # Attach realistic evidence so explanations learn to cite signatures.
    asset_for_user = dict(asset)
    asset_for_user["log_evidence"] = random.choice(EVIDENCE_BY_ACTION.get(action, EVIDENCE_BY_ACTION["NONE"]))
    target = {
        "asset_id": asset["asset_id"],
        "action": action,
        "risk_score": risk["risk_score"],
        "rpo_consumed_pct": risk["rpo_consumed_pct"],
        "explanation": _explanation(asset, risk, action),
        "confidence": round(random.uniform(0.82, 0.97), 2),
    }
    user = json.dumps({k: asset_for_user[k] for k in (
        "asset_id", "asset_name", "tier", "criticality_score", "rpo_target_hours",
        "hours_since_last_backup", "consecutive_failures", "restore_test_days_overdue",
        "log_evidence")}, separators=(",", ":"))
    return {
        "messages": [
            {"role": "system", "content": SLM_SYSTEM_PROMPT},
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(target, separators=(",", ":"))},
        ],
        "action": action,  # kept for stratified stats; trainer ignores extra keys
    }




# ---------------------------------------------------------------------------
# v4.1 EXPERT POLICY — the richer teacher signal.
# The deterministic rules are the floor; the expert layer teaches the model
# DETECTION patterns rules can't see: transient-vs-persistent failures,
# integrity/corruption evidence, memory trends, and systemic context.
# ---------------------------------------------------------------------------
TRANSIENT_EVIDENCE = [
    "Connection reset by peer during transfer, retry recommended",
    "Temporary DNS resolution failure for backup target",
    "Timeout waiting for storage endpoint; endpoint healthy on probe",
    "Network path flapped once during window; link restored",
]
CORRUPTION_EVIDENCE = [
    "Checksum mismatch detected on backup archive segment 7",
    "Snapshot metadata integrity check failed: unexpected block hash",
    "Archive verification error: truncated object detected in repository",
    "Restore-point catalog inconsistent with stored chunks",
]

def expert_action(asset: dict, risk: dict, memory: dict = None, evidence: str = "") -> str:
    """Memory- and evidence-aware ground truth used for v4.1 training data."""
    base = decide_action(asset, risk)
    consec = asset.get("consecutive_failures", 0)
    ev = (evidence or "").lower()
    # NEGATIVE integrity signals demand a human regardless of score. Phrases like
    # "checksum verified" are positive confirmations and must NOT trigger this.
    integrity_bad = any(k in ev for k in (
        "checksum mismatch", "checksum verification failed", "integrity check failed",
        "corrupt", "truncated", "inconsistent with"))
    if integrity_bad:
        if base in ("NONE", "WARN", "SCHEDULE_RESTORE_TEST", "RETRY_BACKUP"):
            return "MANUAL_REVIEW"
        return base
    # A single transient failure at moderate risk deserves one automatic retry, not noise
    if consec == 1 and base in ("NONE", "WARN") and risk["risk_score"] < 200 and \
       any(k in ev for k in ("retry", "temporary", "timeout", "reset", "flap")):
        return "RETRY_BACKUP"
    # Memory: persistent quiet-looking incident or rising trend -> earlier WARN
    if memory:
        if base == "NONE" and (memory.get("persistent_incident") or memory.get("risk_trend") == "rising"):
            return "WARN"
    return base


def _memory_block(rnd: "random.Random") -> dict:
    trend = rnd.choice(["rising", "flat", "falling"])
    return {
        "last_action": rnd.choice(["NONE", "WARN", "ESCALATE_P2"]),
        "risk_trend": trend,
        "escalations_recently": rnd.randint(0, 2),
        "persistent_incident": rnd.random() < (0.5 if trend == "rising" else 0.15),
    }


def _example_v41(asset: dict, family: str) -> dict:
    """Reasoning-rich example families:
       plain | memory | transient | corruption | sparse (missing fields)"""
    rnd = random
    memory = None
    evidence = None
    a = dict(asset)

    if family == "memory":
        memory = _memory_block(rnd)
    elif family == "transient":
        a["consecutive_failures"] = 1
        evidence = rnd.choice(TRANSIENT_EVIDENCE)
    elif family == "corruption":
        evidence = rnd.choice(CORRUPTION_EVIDENCE)
    elif family == "sparse":
        for k in rnd.sample(["restore_test_days_overdue", "consecutive_failures"], 1):
            a[k] = None
        a["consecutive_failures"] = a.get("consecutive_failures") or 0
        a["restore_test_days_overdue"] = a.get("restore_test_days_overdue") or 0

    risk = compute_risk(a)
    if evidence is None:
        provisional = decide_action(a, risk)
        evidence = rnd.choice(EVIDENCE_BY_ACTION.get(provisional, EVIDENCE_BY_ACTION["NONE"]))
    action = expert_action(a, risk, memory, evidence)

    user_payload = {k: a.get(k) for k in (
        "asset_id", "asset_name", "tier", "criticality_score", "rpo_target_hours",
        "hours_since_last_backup", "consecutive_failures", "restore_test_days_overdue")}
    user_payload["log_evidence"] = evidence
    if memory:
        user_payload["memory"] = memory

    a2 = dict(a); a2["log_evidence"] = evidence
    target = {
        "asset_id": a["asset_id"],
        "action": action,
        "risk_score": risk["risk_score"],
        "rpo_consumed_pct": risk["rpo_consumed_pct"],
        "explanation": _explanation(a2, risk, action),
        "confidence": round(random.uniform(0.82, 0.97), 2),
    }
    return {
        "messages": [
            {"role": "system", "content": SLM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, separators=(",", ":"))},
            {"role": "assistant", "content": json.dumps(target, separators=(",", ":"))},
        ],
        "action": action,
        "family": family,
    }


def main(n: int, val_frac: float, seed: int):
    random.seed(seed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # v4.1 curriculum mix — plain policy plus reasoning-heavy families that
    # teach detection: transient-vs-persistent, integrity signals, memory
    # trends, and robustness to sparse inputs.
    FAMILY_MIX = [("plain", 0.42), ("memory", 0.22), ("transient", 0.14),
                  ("corruption", 0.12), ("sparse", 0.10)]
    examples = []
    fam_counts = {}
    for i in range(n):
        r = random.random(); acc = 0.0; family = "plain"
        for fam, w in FAMILY_MIX:
            acc += w
            if r <= acc:
                family = fam; break
        ex = _example_v41(_random_asset(i), family)
        fam_counts[family] = fam_counts.get(family, 0) + 1
        examples.append(ex)
    random.shuffle(examples)
    n_val = max(1, int(n * val_frac))
    val, train = examples[:n_val], examples[n_val:]

    dist = {}
    for e in examples:
        dist[e["action"]] = dist.get(e["action"], 0) + 1

    for split, rows in (("train", train), ("val", val)):
        path = OUT_DIR / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({"messages": r["messages"]}) + "\n")
        print(f"  wrote {len(rows):5d} -> {path.relative_to(ROOT)}")

    print("\nAction distribution:")
    for k in sorted(dist):
        print(f"  {k:24} {dist[k]:5d}  ({dist[k]/n*100:.1f}%)")
    print("\nFamily mix:")
    for k in sorted(fam_counts):
        print(f"  {k:12} {fam_counts[k]:5d}  ({fam_counts[k]/n*100:.1f}%)")
    print(f"\nDone. {len(train)} train / {len(val)} val examples.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build SENTRIX SLM SFT dataset")
    ap.add_argument("--n", type=int, default=4000, help="total examples")
    ap.add_argument("--val-frac", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    main(args.n, args.val_frac, args.seed)
