"""
Syslog adapter — turns real forwarded log lines into asset-state deltas.

Handles common shapes rsyslog / journald forwarders emit, e.g.:
  <34>1 2026-07-04T01:00:00Z host01 backupd - - Backup FAILED for asset WEB-SRV-03
  Jul  4 01:00:00 host01 backupd: backup success asset=DB-NODE-01

Heuristics only — a real deployment maps its own log grammar, but this gives a
working default so a forwarder can point at SENTRIX today. Failure keywords bump
the failure streak; success keywords clear it.
"""
from __future__ import annotations

import re

_ASSET_RE = re.compile(r"asset[=:\s]+([A-Za-z0-9._-]+)", re.IGNORECASE)
_HOST_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9-]*\d{1,3})\b")
_FAIL_RE = re.compile(r"fail|error|timeout|refused|unreachable|denied|abort|corrupt", re.IGNORECASE)
_OK_RE = re.compile(r"success|completed|ok\b|healthy|restored", re.IGNORECASE)


def _asset_from_line(line: str) -> str:
    m = _ASSET_RE.search(line)
    if m:
        return m.group(1)
    m = _HOST_RE.search(line)
    return m.group(1) if m else None


def normalize(payload) -> list:
    if payload is None:
        return []
    text = payload if isinstance(payload, str) else "\n".join(map(str, payload))
    deltas = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        aid = _asset_from_line(line)
        if not aid:
            continue
        d = deltas.setdefault(aid, {"asset_id": aid, "dataset": "live"})
        if _FAIL_RE.search(line):
            d["last_backup_status"] = "failed"
            d["consecutive_failures"] = d.get("consecutive_failures", 0) + 1
            d["evidence"] = line[:160]
        elif _OK_RE.search(line):
            d["last_backup_status"] = "success"
            d["consecutive_failures"] = 0
    return list(deltas.values())
