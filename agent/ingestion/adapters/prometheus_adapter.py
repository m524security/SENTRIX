"""
Prometheus adapter — turns scraped metrics into asset-state deltas.

Accepts Prometheus text exposition format (string) or a pre-parsed dict.
Recognized metric names (labelled by asset):
  backup_age_seconds{asset="X"}          -> hours_since_last_backup
  backup_consecutive_failures{asset="X"} -> consecutive_failures
  restore_test_overdue_days{asset="X"}   -> restore_test_days_overdue
  asset_criticality{asset="X"}           -> criticality_score
  asset_tier{asset="X"}                  -> tier
"""
from __future__ import annotations

import re

_LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([0-9eE+.\-]+)')
_LABEL_RE = re.compile(r'asset="([^"]+)"')

_MAP = {
    "backup_age_seconds": ("hours_since_last_backup", lambda v: round(v / 3600.0, 2)),
    "backup_consecutive_failures": ("consecutive_failures", lambda v: int(v)),
    "restore_test_overdue_days": ("restore_test_days_overdue", lambda v: int(v)),
    "asset_criticality": ("criticality_score", lambda v: int(v)),
    "asset_tier": ("tier", lambda v: int(v)),
}


def _from_text(text: str) -> list:
    deltas = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        metric, labels, raw_val = m.group(1), m.group(2), m.group(3)
        if metric not in _MAP:
            continue
        lm = _LABEL_RE.search(labels)
        if not lm:
            continue
        aid = lm.group(1)
        try:
            val = float(raw_val)
        except ValueError:
            continue
        field, fn = _MAP[metric]
        d = deltas.setdefault(aid, {"asset_id": aid, "dataset": "live"})
        d[field] = fn(val)
        if field == "consecutive_failures" and d[field] > 0:
            d["last_backup_status"] = "failed"
    return list(deltas.values())


def normalize(payload) -> list:
    if payload is None:
        return []
    if isinstance(payload, str):
        return _from_text(payload)
    if isinstance(payload, dict):
        # {"asset_id": {...}} or {"metrics": "<text>"}
        if "metrics" in payload and isinstance(payload["metrics"], str):
            return _from_text(payload["metrics"])
        out = []
        for aid, fields in payload.items():
            if isinstance(fields, dict):
                out.append({"asset_id": aid, "dataset": "live", **fields})
        return out
    raise ValueError("prometheus adapter expects text or dict")
