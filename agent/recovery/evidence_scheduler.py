"""
SENTRIX — Evidence Acquisition Scheduler
========================================
THIS is what makes SENTRIX an agent rather than a dashboard.

A monitoring tool reports "no restore test in 214 days" and stops. It has
identified a gap in ITS OWN KNOWLEDGE and done nothing about it.

An agent recognises that its confidence is low *because evidence is missing*
(as opposed to low because backups are failing — a different problem needing a
different action), and then TAKES ACTION TO MANUFACTURE THE MISSING EVIDENCE.

    An agent that knows what it doesn't know about recoverability,
    and schedules the test that finds out.

The optimisation
----------------
You cannot restore-test everything every week — drills cost machine time,
staff time, and isolated infrastructure. So: given a test budget, WHICH assets,
tested this week, most reduce fleet-wide recovery uncertainty?

That is a bounded knapsack-shaped problem. We solve it greedily on

    marginal_value = Δ(criticality-weighted confidence) / test_cost

Greedy is deliberate, not lazy: it is O(n log n), explainable line-by-line to
an auditor ("we picked this asset because it buys the most proven confidence
per hour of drill time"), and for a submodular gain function it is within
(1 - 1/e) of optimal. An unexplainable optimum is worth less here than a
defensible near-optimum.

The output is not an alert. It is a WORK QUEUE THAT PROVABLY SHRINKS THE
FLEET'S BLIND SPOT — and when those tests land back in the evidence ledger,
confidence re-scores and the loop closes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.recovery import confidence as prc

# Cost of each restore-test type, in engineer-hours (config, not physics —
# override per environment).
TEST_COST_HOURS = {
    "checksum_verify":    0.25,
    "partial_restore":    2.0,
    "full_restore_drill": 6.0,
}

# What we're willing to spend on evidence acquisition per cycle/week.
DEFAULT_BUDGET_HOURS = 24.0

# Which test type to propose for a given tier. Tier-1 systems earn a real
# drill; tier-4 cold archives don't justify six hours of anyone's week.
TIER_TEST_POLICY = {
    1: "full_restore_drill",
    2: "full_restore_drill",
    3: "partial_restore",
    4: "checksum_verify",
}

# Assets already inside their cadence with strong fresh evidence aren't worth
# retesting — spend the budget on blind spots instead.
RETEST_CONFIDENCE_CEILING = 0.85


def _proposed_test(asset: dict) -> str:
    return TIER_TEST_POLICY.get(int(asset.get("tier", 3) or 3), "partial_restore")


def _simulate_post_test(asset: dict, test_type: str) -> dict:
    """What would this asset look like the moment a fresh test PASSES?

    Fresh evidence of `test_type`, zero days old, and the drift counter resets
    — a proven restore re-baselines drift by definition: you just proved you
    can recover the system as it exists NOW.
    """
    after = dict(asset)
    after["last_restore_test"] = {
        "type": test_type,
        "days_ago": 0.0,
        "passed": True,
        "rto_actual_seconds": (asset.get("last_restore_test") or {}).get("rto_actual_seconds"),
    }
    after["config_changes_since_restore_test"] = 0
    return after


def marginal_value(asset: dict, test_type: str = None) -> dict:
    """Confidence gain per engineer-hour if we test this asset now.

    Weighted by criticality: proving a tier-1 payments DB is worth more than
    proving an archive, even for an identical raw confidence delta.
    """
    test_type = test_type or _proposed_test(asset)
    before = prc.score_asset(asset)["confidence"]
    after = prc.score_asset(_simulate_post_test(asset, test_type))["confidence"]
    delta = max(after - before, 0.0)

    crit = float(asset.get("criticality_score", 50) or 50) / 100.0
    cost = TEST_COST_HOURS.get(test_type, 2.0)
    weighted_gain = delta * crit
    return {
        "asset_id": asset.get("asset_id"),
        "asset_name": asset.get("asset_name"),
        "tier": int(asset.get("tier", 3) or 3),
        "criticality": float(asset.get("criticality_score", 50) or 50),
        "test_type": test_type,
        "cost_hours": cost,
        "confidence_before": round(before, 4),
        "confidence_after": round(after, 4),
        "confidence_gain": round(delta, 4),
        "weighted_gain": round(weighted_gain, 4),
        "value_per_hour": round(weighted_gain / cost, 5) if cost else 0.0,
    }


def plan(assets: list, budget_hours: float = DEFAULT_BUDGET_HOURS,
         start: datetime = None) -> dict:
    """Build the restore-test schedule that buys the most proven confidence
    for the available budget.

    Returns the plan, the fleet confidence before/after, and — importantly —
    what is STILL unproven after spending the whole budget. An agent that
    hides its residual blind spot is worse than useless.
    """
    assets = list(assets or [])
    if not assets:
        return {"scheduled": [], "deferred": [], "budget_hours": budget_hours,
                "spent_hours": 0.0, "fleet_confidence_before": 0.0,
                "fleet_confidence_after": 0.0, "residual_blind_spots": []}

    before_fleet = prc.score_fleet(assets)

    candidates = []
    for a in assets:
        cur = prc.score_asset(a)
        if cur["confidence"] >= RETEST_CONFIDENCE_CEILING:
            continue  # already proven and fresh — don't burn budget here
        mv = marginal_value(a)
        if mv["weighted_gain"] <= 0.0:
            continue  # testing wouldn't help (e.g. chain is broken — fix that first)
        mv["current_band"] = cur["band"]
        mv["gaps"] = cur["gaps"]
        candidates.append(mv)

    # ---------------------------------------------------------------------- #
    # Two-tranche scheduling. Pure value-per-hour greedy is WRONG here, and the
    # failure is instructive: cheap tier-4 checksum verifies have the best
    # ratio, so an unguarded knapsack schedules 15 IoT devices and defers an
    # unproven tier-1 database. You cannot offset an unproven payments DB with
    # fifteen proven laptops — aggregate confidence hides categorical risk.
    #
    #   Tranche 1 (mandatory): every tier-1/tier-2 asset in a blind_spot or
    #     unproven band gets covered first, ordered by criticality. This mirrors
    #     real IT-resilience practice: critical systems carry a mandatory drill
    #     cadence, not "whatever is cheapest this week".
    #   Tranche 2 (opportunistic): remaining budget goes to value-per-hour
    #     greedy over everything else — that is where the ratio genuinely is
    #     the right call.
    # ---------------------------------------------------------------------- #
    MANDATORY_TIERS = (1, 2)
    MANDATORY_BANDS = ("blind_spot", "unproven")

    mandatory = [c for c in candidates
                 if c["tier"] in MANDATORY_TIERS and c["current_band"] in MANDATORY_BANDS]
    opportunistic = [c for c in candidates if c not in mandatory]

    mandatory.sort(key=lambda c: (-c["criticality"], -c["weighted_gain"]))
    opportunistic.sort(key=lambda c: (c["value_per_hour"], -c["tier"]), reverse=True)

    scheduled, deferred = [], []
    spent = 0.0
    start = start or datetime.now(timezone.utc)
    slot = 0

    for tranche, reason in ((mandatory, "mandatory"), (opportunistic, "opportunistic")):
        for c in tranche:
            if spent + c["cost_hours"] > budget_hours:
                deferred.append(c)
                continue
            c = dict(c)
            c["scheduled_for"] = (start + timedelta(hours=spent)).isoformat()
            c["slot"] = slot
            c["tranche"] = reason
            if reason == "mandatory":
                c["rationale"] = (
                    f"Tier-{c['tier']} asset is {c['current_band'].replace('_', ' ')} — "
                    f"critical systems must be provably recoverable regardless of test "
                    f"cost. {c['test_type'].replace('_', ' ')} ({c['cost_hours']}h) buys "
                    f"{c['confidence_gain'] * 100:.1f} pts."
                )
            else:
                c["rationale"] = (
                    f"{c['test_type'].replace('_', ' ')} buys "
                    f"{c['confidence_gain'] * 100:.1f} pts for {c['cost_hours']}h — "
                    f"best remaining value per hour after critical coverage."
                )
            scheduled.append(c)
            spent += c["cost_hours"]
            slot += 1

    # Surface unfunded critical work explicitly — an agent that silently drops a
    # tier-1 blind spot because the budget ran out is lying by omission.
    unfunded_critical = [d for d in deferred if d["tier"] in MANDATORY_TIERS
                         and d["current_band"] in MANDATORY_BANDS]

    # What the fleet looks like if every scheduled test passes.
    sched_ids = {c["asset_id"] for c in scheduled}
    projected = []
    for a in assets:
        if a.get("asset_id") in sched_ids:
            tt = next(c["test_type"] for c in scheduled if c["asset_id"] == a.get("asset_id"))
            projected.append(_simulate_post_test(a, tt))
        else:
            projected.append(a)
    after_fleet = prc.score_fleet(projected)

    return {
        "scheduled": scheduled,
        "deferred": deferred[:20],
        "deferred_count": len(deferred),
        "unfunded_critical": [
            {"asset_id": d["asset_id"], "asset_name": d["asset_name"], "tier": d["tier"],
             "current_band": d["current_band"], "cost_hours": d["cost_hours"]}
            for d in unfunded_critical
        ],
        "unfunded_critical_count": len(unfunded_critical),
        "budget_hours": budget_hours,
        "spent_hours": round(spent, 2),
        "fleet_confidence_before": before_fleet["fleet_confidence"],
        "fleet_confidence_before_pct": before_fleet["fleet_confidence_pct"],
        "fleet_confidence_after": after_fleet["fleet_confidence"],
        "fleet_confidence_after_pct": after_fleet["fleet_confidence_pct"],
        "confidence_uplift_pct": round(
            after_fleet["fleet_confidence_pct"] - before_fleet["fleet_confidence_pct"], 2),
        "blind_spots_before": before_fleet["blind_spot_count"],
        "blind_spots_after": after_fleet["blind_spot_count"],
        # Honest residual: still unproven even after the full budget is spent.
        "residual_blind_spots": [
            {"asset_id": s["asset_id"], "asset_name": s["asset_name"],
             "tier": s["tier"], "confidence_pct": s["confidence_pct"],
             "gaps": s["gaps"]}
            for s in after_fleet["blind_spots"][:10]
        ],
        "planned_at": start.isoformat(),
    }


def explain_plan(p: dict) -> str:
    """One-paragraph plain-English summary for the dashboard / LLM context."""
    if not p.get("scheduled"):
        return ("No restore tests scheduled: every asset is either already proven "
                "within its evidence half-life, or has a broken backup chain that "
                "must be repaired before a restore test would prove anything.")
    n = len(p["scheduled"])
    mand = sum(1 for c in p["scheduled"] if c.get("tranche") == "mandatory")
    msg = (
        f"Scheduled {n} restore test(s) ({mand} on critical tier-1/2 assets) using "
        f"{p['spent_hours']}h of a {p['budget_hours']}h budget. If all pass, fleet "
        f"recovery confidence rises from {p['fleet_confidence_before_pct']}% to "
        f"{p['fleet_confidence_after_pct']}% (+{p['confidence_uplift_pct']} pts) and "
        f"blind spots fall from {p['blind_spots_before']} to {p['blind_spots_after']}. "
        f"{p['deferred_count']} asset(s) deferred — budget exhausted."
    )
    if p.get("unfunded_critical_count"):
        need = sum(c["cost_hours"] for c in p["unfunded_critical"])
        msg += (f" WARNING: {p['unfunded_critical_count']} critical tier-1/2 asset(s) "
                f"remain unproven and unfunded — {need:.1f}h more budget would cover them.")
    return msg
