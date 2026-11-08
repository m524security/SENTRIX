"""
SENTRIX — Real-Time Ingestion Gateway
=====================================
This is the answer to "build for real-time implementation, not simulation only."

The previous system could ONLY read a deterministic simulator. This gateway lets
SENTRIX consume genuine, live telemetry pushed from real infrastructure:

  • JSON events over HTTP  (POST /api/ingest)              -> json adapter
  • Syslog / RFC-style lines (agents, rsyslog forwards)    -> syslog adapter
  • Prometheus / metric scrapes (backup_age_seconds, ...)  -> prometheus adapter

Each real event is normalized into an *asset-state delta* and folded into a
live, in-memory fleet snapshot. The reasoning core, API and dashboard read that
live snapshot exactly like they read the simulator — so switching from demo to
production is a MODE flag, not a rewrite.

SENTRIX_MODE:
  simulation  (default) -> loghub_engine deterministic fleet (great for demos)
  live                  -> this gateway's real, pushed fleet state
  hybrid                -> simulator baseline, overridden by any live asset seen

Design constraints kept from the rest of the codebase: dependency-free core,
thread-safe, never throws on bad input (a malformed event is dropped, logged,
and the agent keeps running).
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

from agent.ingestion.adapters import json_adapter, syslog_adapter, prometheus_adapter

# Strict identifier allow-list: letters, digits, dot, underscore, hyphen; 1-128
# chars. Enforced at ingestion so a hostile asset_id can never reach the action
# executor's command templates (argument-injection defense-in-depth).
_VALID_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# asset_id -> live normalized state dict
_live_fleet: dict = {}
_lock = threading.RLock()
_stats = {"events_total": 0, "events_dropped": 0, "last_event_at": None, "sources": {}}

# Assets go "stale" if no telemetry arrives within this window (real infra that
# stops reporting is itself a signal — a silent asset is a risk).
STALE_AFTER_SECONDS = 900


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_ADAPTERS = {
    "json": json_adapter.normalize,
    "syslog": syslog_adapter.normalize,
    "prometheus": prometheus_adapter.normalize,
}


def ingest(payload, source: str = "json") -> dict:
    """
    Accept one real event (or a batch) from a real source, normalize it, and
    fold it into the live fleet. Returns a small receipt for the API.

    payload shape depends on `source`:
      json       -> dict or list[dict] of asset states / partial deltas
      syslog     -> str (one or many newline-separated log lines)
      prometheus -> str (Prometheus text exposition format) or dict of metrics
    """
    adapter = _ADAPTERS.get(source)
    if adapter is None:
        _stats["events_dropped"] += 1
        return {"ok": False, "error": f"unknown source '{source}'", "known": list(_ADAPTERS)}

    try:
        deltas = adapter(payload)  # -> list[dict] normalized asset deltas
    except Exception as e:
        _stats["events_dropped"] += 1
        return {"ok": False, "error": f"adapter '{source}' failed: {e}"}

    accepted = 0
    with _lock:
        for delta in deltas:
            aid = delta.get("asset_id")
            if not aid:
                _stats["events_dropped"] += 1
                continue
            # Security: asset_id flows from untrusted telemetry all the way into
            # templated action commands (executor._render_command). Allow-list it
            # HERE, at the trust boundary, so every downstream consumer gets a
            # safe identifier — a crafted id like "--delete" or "-x foo" could
            # otherwise inject flags into restic/restore commands.
            aid = str(aid).strip()
            if not _VALID_ASSET_ID.fullmatch(aid) or aid.startswith("-"):
                _stats["events_dropped"] += 1
                continue
            delta["asset_id"] = aid
            existing = _live_fleet.get(aid, {})
            merged = {**existing, **{k: v for k, v in delta.items() if v is not None}}
            merged["asset_id"] = aid
            merged.setdefault("asset_name", aid)
            merged.setdefault("tier", 3)
            merged.setdefault("criticality_score", 50)
            merged.setdefault("rpo_target_hours", 8)
            merged.setdefault("dataset", "live")
            merged.setdefault("source_label", f"live:{source}")
            merged.setdefault("consecutive_failures", 0)
            merged.setdefault("restore_test_days_overdue", 0)
            merged["_last_seen"] = time.time()
            merged["_last_seen_iso"] = _now_iso()
            _live_fleet[aid] = merged
            accepted += 1

    _stats["events_total"] += accepted
    _stats["last_event_at"] = _now_iso()
    _stats["sources"][source] = _stats["sources"].get(source, 0) + accepted
    return {"ok": True, "source": source, "accepted": accepted, "live_assets": len(_live_fleet)}


def get_live_fleet(include_stale: bool = True) -> list:
    """Current live fleet snapshot, staleness-annotated."""
    now = time.time()
    out = []
    with _lock:
        for aid, state in _live_fleet.items():
            age = now - state.get("_last_seen", now)
            stale = age > STALE_AFTER_SECONDS
            if stale and not include_stale:
                continue
            s = dict(state)
            s["_stale"] = stale
            s["_seconds_since_report"] = round(age, 1)
            # A silent asset is itself a risk signal — surface it as an incident.
            if stale and s.get("consecutive_failures", 0) == 0:
                s["consecutive_failures"] = max(s.get("consecutive_failures", 0), 1)
                s["evidence"] = s.get("evidence") or (
                    f"No telemetry for {int(age)}s (>{STALE_AFTER_SECONDS}s threshold) — asset may be down."
                )
            out.append(s)
    return out


def live_asset_count() -> int:
    with _lock:
        return len(_live_fleet)


def gateway_status() -> dict:
    return {
        "live_assets": live_asset_count(),
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "adapters": list(_ADAPTERS),
        **_stats,
    }


def reset():
    """Clear live state (used by tests and the /api/ingest/reset endpoint)."""
    with _lock:
        _live_fleet.clear()
    _stats.update({"events_total": 0, "events_dropped": 0, "last_event_at": None, "sources": {}})
