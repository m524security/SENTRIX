"""
SENTRIX — Predictive Engine
--------------------------
Forecasts an RPO breach *before* it happens by looking at the trend in an
asset's recent backup history rather than only its current snapshot. This is
what lifts SENTRIX from "alerts when something is already broken" to "warns
that something is about to break".

It reconstructs a short backup history for each asset from the deterministic
simulation engine (so predictions are reproducible and line up with what the
rest of the system shows), fits a simple linear trend on the failure rate,
and projects it forward. No numpy/scipy dependency — a tiny hand-rolled
least-squares fit keeps the deployment lean.
"""

from agent.ingestion import loghub_engine


def _linear_slope(ys: list) -> float:
    """Ordinary least-squares slope of ys against x=0..n-1. Pure Python."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def _reconstruct_history(asset_id: str, ticks_back: int = 12) -> list:
    """Replays the deterministic engine over the last N ticks to build a
    backup-job history (oldest first). Reproducible and consistent with the
    live state the dashboard shows."""
    universe = loghub_engine._build_asset_universe()
    static_def = universe.get(asset_id)
    if not static_def:
        return []
    now = loghub_engine.current_tick()
    history = []
    for t in range(now - ticks_back + 1, now + 1):
        state = loghub_engine._dynamic_state(asset_id, static_def, t)
        history.append({
            "tick": t,
            "status": state["last_backup_status"],
            "consecutive_failures": state["consecutive_failures"],
            "hours_since_last_backup": state["hours_since_last_backup"],
        })
    return history


def predict_rpo_breach(asset: dict, ticks_back: int = 12) -> dict:
    """
    Returns a forecast dict:
        {risk: low|medium|high, reason, predicted_breach_hours?, trend_pct_per_window?}
    'risk' here is a *predictive* signal about the near future, distinct from
    the current risk_score the reasoning core assigns to the present moment.
    """
    asset_id = asset.get("asset_id")
    rpo_hours = float(asset.get("rpo_target_hours", 8) or 8)
    history = _reconstruct_history(asset_id, ticks_back)

    if len(history) < 5:
        return {"asset_id": asset_id, "risk": "unknown", "reason": "Insufficient backup history to forecast."}

    # Failure rate per sliding window of 3 ticks.
    window = 3
    rates = []
    for i in range(0, len(history) - window + 1):
        chunk = history[i:i + window]
        failures = sum(1 for h in chunk if h["status"] == "failed")
        rates.append(failures / window)

    if len(rates) < 3:
        return {"asset_id": asset_id, "risk": "low", "reason": "Not enough windows to establish a trend."}

    slope = _linear_slope(rates)
    current_rate = rates[-1]
    projected = max(0.0, min(1.0, current_rate + slope * 2))  # project ~2 windows ahead

    hours_left = float(asset.get("rpo_target_hours", 8)) - float(asset.get("hours_since_last_backup", 0))

    if (projected > 0.5 and slope > 0.05) or asset.get("consecutive_failures", 0) >= 2:
        breach_h = max(1, round(rpo_hours * max(0.05, 1 - current_rate)))
        if slope > 0.02:
            reason = f"Failure rate trending up ({slope*100:+.0f}% per window); breach predicted in ~{breach_h}h."
        else:
            reason = (f"Active failure streak ({asset.get('consecutive_failures', 0)} consecutive); "
                      f"breach predicted in ~{breach_h}h.")
        return {
            "asset_id": asset_id,
            "risk": "high",
            "reason": reason,
            "predicted_breach_hours": breach_h,
            "current_failure_rate_pct": round(current_rate * 100, 1),
            "trend_pct_per_window": round(slope * 100, 1),
        }
    if projected > 0.25 or hours_left < rpo_hours * 0.25:
        return {
            "asset_id": asset_id,
            "risk": "medium",
            "reason": f"Elevated failure rate ({current_rate*100:.0f}%) — monitor closely.",
            "current_failure_rate_pct": round(current_rate * 100, 1),
            "trend_pct_per_window": round(slope * 100, 1),
        }
    return {
        "asset_id": asset_id,
        "risk": "low",
        "reason": "Failure rate within normal bounds; no breach forecast.",
        "current_failure_rate_pct": round(current_rate * 100, 1),
        "trend_pct_per_window": round(slope * 100, 1),
    }


def predict_fleet(assets: list, only_at_risk: bool = True) -> list:
    """Forecast every asset; by default returns only medium/high predictions
    (the ones worth surfacing on the dashboard)."""
    out = []
    for asset in assets:
        forecast = predict_rpo_breach(asset)
        forecast["asset_name"] = asset.get("asset_name", asset.get("asset_id"))
        forecast["tier"] = asset.get("tier")
        forecast["dataset"] = asset.get("dataset")
        if only_at_risk and forecast["risk"] in ("low", "unknown"):
            continue
        out.append(forecast)
    out.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2, "unknown": 3}[f["risk"]])
    return out


if __name__ == "__main__":
    from agent.ingestion import loghub_engine as le
    preds = predict_fleet(le.get_all_assets(), only_at_risk=False)
    highs = [p for p in preds if p["risk"] == "high"]
    meds = [p for p in preds if p["risk"] == "medium"]
    print(f"Predictions — {len(highs)} high, {len(meds)} medium, {len(preds)} total")
    for p in (highs + meds)[:5]:
        print(f"  {p['asset_id']:18s} {p['risk']:6s} {p['reason']}")
