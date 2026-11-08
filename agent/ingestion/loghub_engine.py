"""
SENTRIX — LogHub-Grounded Simulation Engine
-------------------------------------------
Why this file exists:
The original ingestion modules called random.random() on every single call,
which meant the dashboard, the heat map, the simulation tab and the agent's
own perception loop could each see a *different* "current state" for the
same asset within the same second — because every read re-rolled the dice.
It also meant the ~150MB of real LogHub log data sitting in /loghub was
never actually used for anything.

This module fixes both problems at once:

1. It parses every bundled LogHub structured CSV once (cached) and computes
   a genuine error/warning rate per dataset from the real log content —
   these numbers are printed at startup and used to *calibrate* the
   simulation, so e.g. Zookeeper (66% WARN in the real sample) behaves like
   a noisier service than Windows (0% in the real sample).

2. Instead of calling random.random() fresh every time, every dynamic
   field is a deterministic function of (asset_id, world_tick). The world
   tick advances every WORLD_TICK_SECONDS of real wall-clock time. Within
   one tick window, every process (agent, API, every dashboard tab) that
   asks "what is HDFS-NODE-03 doing right now" gets the exact same answer,
   because they're all evaluating the same pure function of the same tick
   number — no shared database required. Tick to tick, state evolves
   smoothly (failure streaks persist or clear; backup age grows or resets)
   instead of jumping to unrelated random noise.
"""

import csv
import hashlib
import random
import re
import time
from pathlib import Path

from agent import config

LOGHUB_ROOT = Path(__file__).resolve().parent.parent.parent / "loghub"

KEYWORD_RE = re.compile(
    r"fail|error|invalid|denied|refused|timeout|exceed|lost connection|"
    r"closed|reset|unable|cannot|not found|exception|crash|drop",
    re.IGNORECASE,
)

# tier -> (criticality base, rpo choices in hours)
TIER_CRITICALITY_BASE = {1: 90, 2: 70, 3: 48, 4: 25}
TIER_RPO_CHOICES = {
    1: [3, 4, 6],
    2: [8, 12, 16],
    3: [24, 36, 48],
    4: [48, 72, 96],
}
TIER_LABEL = {1: "critical", 2: "high", 3: "medium", 4: "low"}
RESTORE_CADENCE_DAYS = {1: 30, 2: 45, 3: 90, 4: 180}

# Each dataset def: real LogHub folder, IT-recovery framing, asset naming,
# and a tier pattern (one tier per generated asset) reflecting how critical
# that category of infrastructure realistically tends to be.
DATASET_DEFS = {
    "hdfs":        {"folder": "HDFS",        "label": "Distributed Storage (HDFS)",        "category": "storage",
                     "prefix": "HDFS-NODE",      "display": "HDFS Node",          "tiers": [1, 1, 2, 1, 2, 3, 2, 1]},
    "apache":      {"folder": "Apache",      "label": "Web Servers (Apache)",              "category": "web",
                     "prefix": "WEB-SRV",        "display": "Web Server",         "tiers": [2, 2, 3, 1, 2, 3, 2, 3]},
    "windows":     {"folder": "Windows",     "label": "Windows Infrastructure",            "category": "windows",
                     "prefix": "WIN-SRV",        "display": "Windows Server",     "tiers": [1, 2, 2, 3, 2, 3, 1, 2]},
    "openssh":     {"folder": "OpenSSH",     "label": "Remote Access / SSH Bastions",       "category": "security",
                     "prefix": "BASTION",        "display": "SSH Bastion Host",   "tiers": [1, 2, 2, 3, 2, 1]},
    "openstack":   {"folder": "OpenStack",   "label": "Cloud Platform (OpenStack)",         "category": "cloud",
                     "prefix": "CLOUD-NODE",     "display": "Cloud Compute Node", "tiers": [1, 1, 2, 2, 3, 2, 1]},
    "linux":       {"folder": "Linux",       "label": "Linux Application Servers",          "category": "servers",
                     "prefix": "LNX-APP",        "display": "Linux App Server",   "tiers": [2, 2, 3, 3, 2, 4, 3, 2]},
    "bgl":         {"folder": "BGL",         "label": "HPC / Supercomputing (BlueGene)",    "category": "hpc",
                     "prefix": "HPC-RACK",       "display": "BlueGene Rack",      "tiers": [2, 3, 3, 4, 2, 3]},
    "zookeeper":   {"folder": "Zookeeper",   "label": "Distributed Coordination (ZooKeeper)", "category": "coordination",
                     "prefix": "ZK-COORD",       "display": "ZooKeeper Coordinator", "tiers": [1, 2, 2, 1, 2]},
    "spark":       {"folder": "Spark",       "label": "Big Data Compute (Spark)",           "category": "bigdata",
                     "prefix": "SPARK-WORKER",   "display": "Spark Worker",       "tiers": [2, 3, 3, 4, 2, 3]},
    "hadoop":      {"folder": "Hadoop",      "label": "Big Data Cluster (Hadoop)",          "category": "bigdata",
                     "prefix": "HADOOP-NODE",    "display": "Hadoop Cluster Node", "tiers": [2, 2, 3, 3, 2, 3]},
    "proxifier":   {"folder": "Proxifier",   "label": "Network Proxy Gateways",             "category": "network",
                     "prefix": "NET-PROXY",      "display": "Network Proxy Gateway", "tiers": [2, 3, 3, 2, 4]},
    "mac":         {"folder": "Mac",         "label": "Endpoint Fleet (macOS)",             "category": "endpoint",
                     "prefix": "MAC-WS",         "display": "macOS Workstation",  "tiers": [3, 4, 4, 3, 4, 3]},
    "thunderbird": {"folder": "Thunderbird", "label": "Messaging Infrastructure",           "category": "messaging",
                     "prefix": "MAIL-RELAY",     "display": "Mail Relay",         "tiers": [1, 2, 3, 2, 3]},
    "hpc":         {"folder": "HPC",         "label": "HPC Cluster",                        "category": "hpc",
                     "prefix": "HPC-NODE",       "display": "HPC Node",           "tiers": [2, 3, 4, 3, 2, 4]},
    "android":     {"folder": "Android",     "label": "Mobile Device Fleet",                "category": "mobile",
                     "prefix": "MOBILE-FLEET",   "display": "Mobile Fleet Device", "tiers": [3, 4, 4, 3, 4]},
    "healthapp":   {"folder": "HealthApp",   "label": "IoT Health Monitoring Hubs",          "category": "iot",
                     "prefix": "IOT-HUB",        "display": "IoT Health Hub",      "tiers": [3, 4, 4, 3, 4]},
}

_STATS_CACHE = {}
_ASSET_STATIC = None  # built once, lazily


def current_tick() -> int:
    return int(time.time() // max(config.WORLD_TICK_SECONDS, 1))


def tier_to_label(tier: int) -> str:
    return TIER_LABEL.get(tier, "medium")


def _det_random(key: str, salt) -> random.Random:
    """A Random() instance seeded deterministically from (key, salt) using a
    stable hash (NOT Python's built-in hash(), which is randomized per
    process) — guarantees the agent process and the API process compute
    identical values for the same logical moment in simulated time."""
    h = hashlib.sha256(f"{key}:{salt}".encode("utf-8")).digest()
    seed = int.from_bytes(h[:8], "big")
    return random.Random(seed)


def _load_dataset_stats(key: str) -> dict:
    """Parse the real LogHub structured CSV for this dataset once and cache
    the result: a genuine error rate plus a handful of real example lines."""
    if key in _STATS_CACHE:
        return _STATS_CACHE[key]

    d = DATASET_DEFS[key]
    folder = d["folder"]
    csv_path = LOGHUB_ROOT / folder / f"{folder}_2k.log_structured.csv"

    stats = {
        "total_lines": 0,
        "error_rate": 0.08,
        "distinct_templates": 0,
        "evidence": [],
        "no_evidence_reason": (
            f"The real {folder}_2k.log sample contained no error/warning lines — "
            f"this asset's current state is a synthetic stress scenario for demo purposes."
        ),
        "source_file": f"loghub/{folder}/{folder}_2k.log_structured.csv",
        "available": False,
    }

    if not csv_path.exists():
        _STATS_CACHE[key] = stats
        return stats

    total = 0
    hits = 0
    templates = set()
    evidence = []

    try:
        with open(csv_path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total += 1
                content = (row.get("Content") or "").strip()
                level = (row.get("Level") or "").strip().upper()
                label = (row.get("Label") or "").strip()
                event_id = row.get("EventId")
                if event_id:
                    templates.add(event_id)

                is_hit = False
                if level:
                    is_hit = level in ("WARN", "WARNING", "ERROR", "FATAL", "E", "W")
                elif label:
                    is_hit = label != "-"
                else:
                    is_hit = bool(KEYWORD_RE.search(content))

                if is_hit:
                    hits += 1
                    if content and len(evidence) < 12:
                        evidence.append(content[:160])
    except (OSError, csv.Error):
        _STATS_CACHE[key] = stats
        return stats

    if total > 0:
        stats["total_lines"] = total
        stats["error_rate"] = hits / total
        stats["distinct_templates"] = len(templates)
        stats["evidence"] = evidence or [stats["no_evidence_reason"]]
        stats["available"] = True

    _STATS_CACHE[key] = stats
    return stats


def _effective_incident_probability(baseline_error_rate: float) -> float:
    """Maps a real observed error-rate (which can be 0% to ~70%+ depending
    on dataset) into a per-tick incident probability that keeps the demo
    interesting at both ends — a dataset with a 0% sample error rate still
    occasionally has a bad tick, and a 70% dataset doesn't fail every tick."""
    return max(0.05, min(0.65, 0.05 + baseline_error_rate * 0.55))


def _build_asset_universe() -> dict:
    global _ASSET_STATIC
    if _ASSET_STATIC is not None:
        return _ASSET_STATIC

    universe = {}
    for dataset_key, d in DATASET_DEFS.items():
        stats = _load_dataset_stats(dataset_key)
        for i, tier in enumerate(d["tiers"], start=1):
            asset_id = f"{d['prefix']}-{i:02d}"
            jitter = _det_random(asset_id, "static").randint(-8, 8)
            criticality_score = max(5, min(100, TIER_CRITICALITY_BASE[tier] + jitter))
            rpo_choices = TIER_RPO_CHOICES[tier]
            rpo_target_hours = rpo_choices[i % len(rpo_choices)]

            universe[asset_id] = {
                "asset_id": asset_id,
                "asset_name": f"{d['display']} {i:02d}",
                "dataset": dataset_key,
                "category": d["category"],
                "source_label": d["label"],
                "tier": tier,
                "criticality_label": tier_to_label(tier),
                "criticality_score": criticality_score,
                "rpo_target_hours": rpo_target_hours,
                "baseline_error_rate": round(stats["error_rate"], 4),
                "source_file": stats["source_file"],
                "data_grounded": stats["available"],
            }

    _ASSET_STATIC = universe
    return universe


def _dynamic_state(asset_id: str, static_def: dict, tick: int) -> dict:
    """The deterministic, history-aware walk described in the module
    docstring — bounded lookback so it's cheap regardless of how long the
    process has been running."""
    effective_prob = _effective_incident_probability(static_def["baseline_error_rate"])
    hours_per_tick = 2.5
    max_lookback = 8

    consecutive_failures = 0
    hours_since_last_backup = 0.0
    found_clean_tick = False

    for k in range(max_lookback):
        t = tick - k
        r = _det_random(asset_id, t)
        incident = r.random() < effective_prob
        if incident:
            consecutive_failures += 1
            hours_since_last_backup += hours_per_tick * (0.8 + 0.4 * r.random())
        else:
            if k == 0:
                hours_since_last_backup = r.uniform(0.15, hours_per_tick * 0.85)
            found_clean_tick = True
            break

    if not found_clean_tick:
        hours_since_last_backup += hours_per_tick * 1.5

    last_incident = consecutive_failures > 0

    # Restore-test overdue tracking moves on a slower clock (days, not ticks)
    cadence = RESTORE_CADENCE_DAYS.get(static_def["tier"], 90)
    day_index = tick // 12  # ~12 ticks per simulated "day" of restore-test bookkeeping
    r_restore = _det_random(asset_id + ":restore", day_index)
    roll = r_restore.random()
    if roll < 0.05:
        restore_test_days_overdue = int(r_restore.uniform(8, 21))
    elif roll < 0.20:
        restore_test_days_overdue = int(r_restore.uniform(1, 7))
    else:
        restore_test_days_overdue = 0

    stats = _load_dataset_stats(static_def["dataset"])
    evidence_pool = stats["evidence"] or ["No log evidence available for this dataset."]
    evidence_idx = _det_random(asset_id, "evidence_pick").randrange(len(evidence_pool))
    evidence_line = evidence_pool[evidence_idx] if last_incident else None

    return {
        "hours_since_last_backup": round(hours_since_last_backup, 1),
        "consecutive_failures": consecutive_failures,
        "last_backup_status": "failed" if last_incident else "success",
        "restore_test_days_overdue": restore_test_days_overdue,
        "evidence": evidence_line,
        "cadence_days": cadence,
    }


def get_all_assets() -> list:
    """Every simulated asset across every LogHub-grounded dataset, with
    current dynamic state for *this* world tick."""
    tick = current_tick()
    universe = _build_asset_universe()
    out = []
    for asset_id, static_def in universe.items():
        state = dict(static_def)
        state.update(_dynamic_state(asset_id, static_def, tick))
        out.append(state)
    return out


def get_assets_for_dataset(dataset_key: str) -> list:
    if dataset_key in ("all", "", None):
        return get_all_assets()
    tick = current_tick()
    universe = _build_asset_universe()
    out = []
    for asset_id, static_def in universe.items():
        if static_def["dataset"] != dataset_key:
            continue
        state = dict(static_def)
        state.update(_dynamic_state(asset_id, static_def, tick))
        out.append(state)
    return out


def get_asset_by_id(asset_id: str):
    universe = _build_asset_universe()
    static_def = universe.get(asset_id)
    if not static_def:
        return None
    state = dict(static_def)
    state.update(_dynamic_state(asset_id, static_def, current_tick()))
    return state


def dataset_registry() -> dict:
    """Metadata for /api/datasets — one entry per LogHub-grounded category,
    including the real, computed error rate so the dashboard can show it
    was calibrated from genuine log data rather than invented."""
    universe = _build_asset_universe()
    registry = {}
    for key, d in DATASET_DEFS.items():
        stats = _load_dataset_stats(key)
        asset_count = sum(1 for a in universe.values() if a["dataset"] == key)
        registry[key] = {
            "id": key,
            "label": d["label"],
            "category": d["category"],
            "asset_count": asset_count,
            "source_file": stats["source_file"],
            "real_observed_error_rate_pct": round(stats["error_rate"] * 100, 2),
            "log_lines_analyzed": stats["total_lines"],
            "distinct_event_templates": stats["distinct_templates"],
            "data_grounded": stats["available"],
        }
    return registry


def failure_pattern_digest(max_datasets: int = 6, lines_per_dataset: int = 1) -> str:
    """A short, human-readable digest of real failure signatures, used to
    ground the LLM reasoning prompt in genuine production log content
    rather than reasoning purely from synthetic numbers."""
    lines = []
    for key in list(DATASET_DEFS.keys())[:max_datasets]:
        stats = _load_dataset_stats(key)
        if not stats["available"]:
            continue
        label = DATASET_DEFS[key]["label"]
        sample = stats["evidence"][:lines_per_dataset]
        for s in sample:
            lines.append(f"- [{label}] {s} (observed error rate: {stats['error_rate']*100:.1f}%)")
    return "\n".join(lines)


def warm_cache():
    """Call once at process startup so the first real request isn't slowed
    down by parsing 16 CSV files, and so startup logs show real numbers."""
    for key in DATASET_DEFS:
        _load_dataset_stats(key)
    _build_asset_universe()
    return dataset_registry()
