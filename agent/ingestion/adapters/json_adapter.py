"""
JSON adapter — the primary real-time path.

Accepts a dict or list of dicts describing real asset state. Callers push
whatever their backup/monitoring system already knows. Only asset_id is
required; everything else is a partial delta merged into live state.

Recognized fields (all optional except asset_id):
  asset_id, asset_name, tier, criticality_score, rpo_target_hours,
  hours_since_last_backup, last_backup_status ("success"/"failed"),
  consecutive_failures, restore_test_days_overdue, evidence, dataset
"""
from __future__ import annotations

_NUM = ("tier", "criticality_score", "rpo_target_hours", "hours_since_last_backup",
        "consecutive_failures", "restore_test_days_overdue")


def _coerce(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k in _NUM and v is not None:
            try:
                out[k] = float(v) if "." in str(v) else int(v)
            except (TypeError, ValueError):
                continue
        else:
            out[k] = v
    # Derive a failed-status streak bump if the caller only sends status.
    status = str(d.get("last_backup_status", "")).lower()
    if status == "failed" and "consecutive_failures" not in out:
        out["consecutive_failures"] = 1
    return out


def normalize(payload) -> list:
    if payload is None:
        return []
    if isinstance(payload, dict):
        # Either a single asset, or {"assets": [...]}
        if "assets" in payload and isinstance(payload["assets"], list):
            payload = payload["assets"]
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("json adapter expects a dict or list of dicts")
    return [_coerce(item) for item in payload if isinstance(item, dict) and item.get("asset_id")]
