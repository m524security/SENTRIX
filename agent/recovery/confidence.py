"""
SENTRIX — Proven Recovery Confidence (PRC)
==========================================
The core thesis of SENTRIX, and the part no commercial backup tool models.

Every backup product on the market answers: "did the backup job succeed?"
A successful backup job is a CLAIM. A passed restore test is EVIDENCE.
Nobody models the difference — or the fact that evidence ROTS.

    A restore test that passed 214 days ago proves almost nothing today:
    configs drifted, dependencies moved, credentials and KMS keys rotated,
    schemas migrated, media aged. The evidence decayed.

So recovery readiness is not a monitoring problem. It is an
EVIDENCE-PROVABILITY PROBLEM UNDER DECAY.

    PRC(asset) = w_b·BackupFreshness
               + w_e·EvidenceStrength(test_type)·exp(-λ_tier · days_since_test)
               + w_c·ChainIntegrity
               - DriftPenalty(changes_since_last_proven_restore)

Three claims make this defensible:

  1. exp(-λ·days) — evidence decays exponentially, and λ is PER-TIER. A tier-1
     transactional DB drifts fast (evidence stale in weeks); a cold archive
     drifts slowly. λ is falsifiable: calibrate it against real restore-failure
     data (see calibrate_lambda()), don't assert it.

  2. EvidenceStrength — a checksum verify is not a restore drill. Three test
     types prove three different amounts. Nobody grades evidence quality.

  3. DriftPenalty — count config/schema/dependency changes since the last
     PROVEN restore. A backup whose source system changed 47 times since the
     last proven restore is a backup you cannot trust. This is the single
     strongest signal here, and it explains why "all-green" backups fail
     during real incidents.

DETERMINISM CONTRACT: this module is pure arithmetic over ledger facts. No LLM
touches the number. The LLM explains the score; it never computes it. That
separation is the reliability argument — the LLM's failure mode is "unhelpful
explanation", never "wrong confidence".
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Model parameters — every one of these is an explicit, auditable assumption.
# --------------------------------------------------------------------------- #

# Evidence half-life in DAYS, per tier. λ = ln(2)/half_life.
# Interpretation: after `half_life` days, a restore test proves half as much.
# Tier 1 transactional systems drift fastest; tier 4 cold archives barely drift.
# CALIBRATE THESE. Defaults are informed priors, not measurements.
EVIDENCE_HALF_LIFE_DAYS = {1: 30.0, 2: 45.0, 3: 90.0, 4: 180.0}

# How much each kind of restore evidence proves, at the moment it is produced.
# A checksum proves bits are readable. A drill proves you can actually recover.
EVIDENCE_STRENGTH = {
    "full_restore_drill": 1.00,   # isolated recovery, RTO measured, data verified
    "partial_restore":    0.65,   # subset restored, chain proven end-to-end
    "checksum_verify":    0.30,   # backup readable + integrity hash matches
    "none":               0.00,   # never tested — no evidence exists
}

# Component weights (must sum to 1.0 before the drift penalty is subtracted).
W_BACKUP_FRESHNESS = 0.30   # is there a recent recovery POINT?
W_EVIDENCE = 0.50           # is that point PROVABLY restorable?  <-- dominant
W_CHAIN_INTEGRITY = 0.20    # is the backup chain itself healthy?

# Drift: each change to the source system since the last proven restore erodes
# confidence. Saturating (not linear) — the 50th change matters less than the
# 5th, but many changes push the penalty toward its ceiling.
DRIFT_PENALTY_CEILING = 0.35   # max confidence a drift storm can subtract
DRIFT_HALF_SATURATION = 15.0   # changes at which half the ceiling is reached

# Confidence-interval width grows as evidence goes stale — an agent that says
# "31% ± 12%" is more trustworthy than one asserting a point estimate.
CI_BASE = 0.04
CI_STALENESS_COEFF = 0.16

# Readiness bands for the dashboard.
BANDS = [
    (0.80, "proven",     "Recovery proven by recent evidence"),
    (0.60, "probable",   "Likely recoverable, evidence aging"),
    (0.35, "unproven",   "Recovery not recently proven"),
    (0.00, "blind_spot", "No usable recovery evidence"),
]


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #
def backup_freshness(hours_since_last_backup: float, rpo_target_hours: float) -> float:
    """1.0 when just backed up, 0.0 once RPO is fully consumed and beyond.
    This is the ONLY thing conventional tools measure well."""
    rpo = max(float(rpo_target_hours or 1), 0.01)
    consumed = float(hours_since_last_backup or 0) / rpo
    return _clamp01(1.0 - consumed)


def evidence_decay_factor(days_since_test: float, tier: int) -> float:
    """exp(-λ·days) with λ = ln(2)/half_life(tier). The novel term."""
    half_life = EVIDENCE_HALF_LIFE_DAYS.get(int(tier or 3), 90.0)
    lam = math.log(2) / half_life
    return math.exp(-lam * max(float(days_since_test or 0), 0.0))


def evidence_component(test_type: str, days_since_test: float, tier: int,
                       passed: bool = True) -> float:
    """Strength of the evidence, decayed by its age.
    A FAILED restore test is not weak evidence of success — it is strong
    evidence of failure, so it floors this component at zero."""
    if not passed:
        return 0.0
    strength = EVIDENCE_STRENGTH.get(str(test_type or "none"), 0.0)
    return strength * evidence_decay_factor(days_since_test, tier)


def chain_integrity(consecutive_failures: int) -> float:
    """Health of the backup chain itself. Three consecutive failures means the
    chain is broken — no amount of old evidence rescues that."""
    fails = int(consecutive_failures or 0)
    if fails <= 0:
        return 1.0
    if fails >= 3:
        return 0.0
    return _clamp01(1.0 - (fails / 3.0))


def drift_penalty(changes_since_last_proven_restore: int) -> float:
    """Saturating penalty for source-system drift since the last PROVEN restore.

    Michaelis-Menten shape: penalty = ceiling · n / (n + half_saturation).
    The 47th config change since your last restore test doesn't add much to
    the 46th — but the fact that there have been 47 is damning."""
    n = max(int(changes_since_last_proven_restore or 0), 0)
    if n == 0:
        return 0.0
    return DRIFT_PENALTY_CEILING * (n / (n + DRIFT_HALF_SATURATION))


def confidence_interval(days_since_test: float, tier: int, has_evidence: bool) -> float:
    """± width. Widens as evidence decays: the agent is explicit about how
    much it doesn't know."""
    if not has_evidence:
        return 0.25  # never tested — we are largely guessing
    decayed_in = 1.0 - evidence_decay_factor(days_since_test, tier)
    return round(CI_BASE + CI_STALENESS_COEFF * decayed_in, 3)


def band(score: float) -> tuple:
    for threshold, name, label in BANDS:
        if score >= threshold:
            return name, label
    return BANDS[-1][1], BANDS[-1][2]


# --------------------------------------------------------------------------- #
# Main scorer
# --------------------------------------------------------------------------- #
def score_asset(asset: dict) -> dict:
    """Compute Proven Recovery Confidence for one asset.

    Expects (all optional, degrade gracefully):
      hours_since_last_backup, rpo_target_hours, tier, consecutive_failures,
      last_restore_test: {type, days_ago, passed, rto_actual_seconds}
      config_changes_since_restore_test, rto_target_seconds

    Returns a fully explainable verdict — every contribution is itemized, so
    the dashboard can show provenance rather than a mystery number.
    """
    tier = int(asset.get("tier", 3) or 3)
    test = asset.get("last_restore_test") or {}
    test_type = test.get("type", "none")
    days_ago = float(test.get("days_ago", 0) or 0)
    passed = bool(test.get("passed", False))
    has_evidence = test_type != "none" and passed

    fresh = backup_freshness(asset.get("hours_since_last_backup", 0),
                             asset.get("rpo_target_hours", 24))
    ev = evidence_component(test_type, days_ago, tier, passed)
    chain = chain_integrity(asset.get("consecutive_failures", 0))
    drift_n = int(asset.get("config_changes_since_restore_test", 0) or 0)
    drift = drift_penalty(drift_n)

    raw = (W_BACKUP_FRESHNESS * fresh
           + W_EVIDENCE * ev
           + W_CHAIN_INTEGRITY * chain)
    score = _clamp01(raw - drift)
    band_name, band_label = band(score)
    ci = confidence_interval(days_ago, tier, has_evidence)

    # RTO evidence: did the last drill actually meet the recovery-time target?
    rto_target = asset.get("rto_target_seconds")
    rto_actual = test.get("rto_actual_seconds")
    rto_proven = None
    if rto_target and rto_actual:
        rto_proven = bool(rto_actual <= rto_target)

    gaps = []
    if not has_evidence:
        gaps.append("no successful restore test on record")
    elif evidence_decay_factor(days_ago, tier) < 0.5:
        gaps.append(f"restore evidence {int(days_ago)}d old (past tier-{tier} half-life)")
    if test_type == "checksum_verify":
        gaps.append("only checksum-verified — never actually restored")
    if drift_n >= 10:
        gaps.append(f"{drift_n} config changes since last proven restore")
    if rto_proven is False:
        gaps.append("last drill missed the RTO target")
    if chain < 1.0:
        gaps.append(f"{int(asset.get('consecutive_failures', 0))} consecutive backup failures")

    return {
        "asset_id": asset.get("asset_id"),
        "asset_name": asset.get("asset_name"),
        "tier": tier,
        "confidence": round(score, 4),
        "confidence_pct": round(score * 100, 1),
        "confidence_interval": ci,
        "band": band_name,
        "band_label": band_label,
        "components": {
            "backup_freshness": round(fresh, 4),
            "evidence": round(ev, 4),
            "chain_integrity": round(chain, 4),
            "drift_penalty": round(drift, 4),
        },
        "contributions": {
            "backup_freshness": round(W_BACKUP_FRESHNESS * fresh, 4),
            "evidence": round(W_EVIDENCE * ev, 4),
            "chain_integrity": round(W_CHAIN_INTEGRITY * chain, 4),
            "drift_penalty": round(-drift, 4),
        },
        "evidence_provenance": {
            "test_type": test_type,
            "test_strength": EVIDENCE_STRENGTH.get(test_type, 0.0),
            "days_since_test": days_ago,
            "passed": passed,
            "decay_factor": round(evidence_decay_factor(days_ago, tier), 4),
            "evidence_half_life_days": EVIDENCE_HALF_LIFE_DAYS.get(tier, 90.0),
            "config_changes_since": drift_n,
            "rto_target_seconds": rto_target,
            "rto_actual_seconds": rto_actual,
            "rto_proven": rto_proven,
        },
        "gaps": gaps,
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }


def score_fleet(assets: list) -> dict:
    """Fleet-wide readiness. The headline number is deliberately NOT
    'backup success rate' — it is how much of the fleet's criticality is
    covered by PROVEN recovery."""
    scored = [score_asset(a) for a in assets or []]
    if not scored:
        return {"assets": [], "fleet_confidence": 0.0, "bands": {}, "blind_spots": []}

    # Criticality-weighted, because a proven cold archive doesn't offset an
    # unproven payments DB.
    total_w = 0.0
    acc = 0.0
    for s, a in zip(scored, assets):
        w = float(a.get("criticality_score", 50) or 50)
        acc += s["confidence"] * w
        total_w += w
    fleet_conf = acc / total_w if total_w else 0.0

    bands: dict = {}
    for s in scored:
        bands[s["band"]] = bands.get(s["band"], 0) + 1

    blind = sorted(
        [s for s in scored if s["band"] in ("blind_spot", "unproven")],
        key=lambda s: (s["confidence"], -s["tier"]),
    )

    return {
        "assets": scored,
        "fleet_confidence": round(fleet_conf, 4),
        "fleet_confidence_pct": round(fleet_conf * 100, 1),
        "asset_count": len(scored),
        "bands": bands,
        "blind_spots": blind[:20],
        "blind_spot_count": len(blind),
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Falsifiability: λ is a claim, not a magic number. Here is how you test it.
# --------------------------------------------------------------------------- #
def calibrate_lambda(history: list, tier: int) -> dict:
    """Fit the per-tier evidence half-life from observed restore outcomes.

    history: [{days_since_prior_test: float, restore_succeeded: bool}, ...]

    Method: bucket attempts by evidence age, compute empirical success rate per
    bucket, and fit λ by least squares on ln(success_rate) = -λ·days.
    Returns the fitted half-life plus the sample size behind it, so a small
    sample can be honestly labelled as such rather than silently trusted.
    """
    buckets: dict = {}
    for h in history or []:
        d = float(h.get("days_since_prior_test", 0) or 0)
        b = int(d // 15) * 15  # 15-day buckets
        hit = buckets.setdefault(b, [0, 0])
        hit[1] += 1
        if h.get("restore_succeeded"):
            hit[0] += 1

    pts = []
    for b, (ok, n) in sorted(buckets.items()):
        if n < 3:
            continue
        rate = ok / n
        if rate <= 0.01:
            continue
        pts.append((float(b), math.log(rate)))

    if len(pts) < 2:
        return {"ok": False, "reason": "insufficient data to fit λ",
                "samples": sum(n for _, n in buckets.values()),
                "current_half_life_days": EVIDENCE_HALF_LIFE_DAYS.get(tier, 90.0)}

    # Least squares through origin: slope = Σ(x·y)/Σ(x²), λ = -slope
    sxy = sum(x * y for x, y in pts)
    sxx = sum(x * x for x, _ in pts)
    if sxx == 0:
        return {"ok": False, "reason": "degenerate fit"}
    lam = -(sxy / sxx)
    if lam <= 0:
        return {"ok": False, "reason": "no observed decay in this sample"}

    fitted = math.log(2) / lam
    return {
        "ok": True,
        "tier": tier,
        "fitted_half_life_days": round(fitted, 1),
        "fitted_lambda": round(lam, 6),
        "current_half_life_days": EVIDENCE_HALF_LIFE_DAYS.get(tier, 90.0),
        "samples": sum(n for _, n in buckets.values()),
        "buckets_used": len(pts),
    }


def decay_curve(asset: dict, days_ahead: int = 120, step: int = 5) -> dict:
    """The chart that sells the model.

    Traces confidence backwards from the last restore test and forwards into
    the future as evidence keeps decaying — then marks where a fresh test
    would lift it back up. A CIO looking at a line falling toward a cliff,
    with a marker showing exactly when to intervene, understands this project
    in three seconds.
    """
    test = asset.get("last_restore_test") or {}
    days_ago = float(test.get("days_ago", 0) or 0)
    points = []
    d = -days_ago
    while d <= days_ahead:
        probe = dict(asset)
        probe["last_restore_test"] = dict(test)
        probe["last_restore_test"]["days_ago"] = max(days_ago + d, 0.0)
        # drift keeps accruing into the future at the observed rate
        base_drift = int(asset.get("config_changes_since_restore_test", 0) or 0)
        rate = base_drift / days_ago if days_ago > 0 else 0.0
        probe["config_changes_since_restore_test"] = int(max(base_drift + rate * d, 0))
        points.append({
            "days_from_now": round(d, 1),
            "confidence_pct": round(score_asset(probe)["confidence"] * 100, 1),
        })
        d += step
    tier = int(asset.get("tier", 3) or 3)
    return {
        "points": points,
        "today_index": next((i for i, p in enumerate(points)
                             if p["days_from_now"] >= 0), 0),
        "half_life_days": EVIDENCE_HALF_LIFE_DAYS.get(tier, 90.0),
        "unproven_threshold_pct": 35.0,
    }
