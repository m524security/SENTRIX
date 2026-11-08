"""
SENTRIX — Fleet Source Selector
===============================
Single place that decides WHERE the current fleet state comes from, based on
SENTRIX_MODE. Everything downstream (agent loop, API, dashboard) asks this
module and stays mode-agnostic.

  simulation -> deterministic LogHub-grounded simulator (demo / offline)
  live       -> real telemetry pushed into realtime_gateway (production)
  hybrid     -> simulator baseline, any live asset overrides its simulated twin
"""
from __future__ import annotations

from agent import config
from agent.ingestion import loghub_engine, realtime_gateway


def _mode() -> str:
    return (config.MODE or "simulation").lower()


def get_fleet(dataset: str = "all") -> list:
    mode = _mode()
    if mode == "live":
        fleet = realtime_gateway.get_live_fleet()
        if dataset not in ("all", "", None):
            fleet = [a for a in fleet if a.get("dataset") == dataset]
        return fleet

    sim = loghub_engine.get_assets_for_dataset(dataset)
    if mode != "hybrid":
        return sim

    # hybrid: overlay live assets on top of the simulated baseline
    live = {a["asset_id"]: a for a in realtime_gateway.get_live_fleet()}
    merged = []
    seen = set()
    for a in sim:
        aid = a.get("asset_id")
        if aid in live:
            merged.append(live[aid]); seen.add(aid)
        else:
            merged.append(a)
    for aid, a in live.items():
        if aid not in seen:
            merged.append(a)
    return merged


def source_status() -> dict:
    return {
        "mode": _mode(),
        "live": realtime_gateway.gateway_status(),
        "simulation_assets": len(loghub_engine.get_all_assets()) if _mode() != "live" else 0,
    }
