"""SENTRIX v4.1 test suite — core guarantees that must never regress."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTRIX_DB_PATH", "data/test_sentrix.db")

from agent.reasoning import reasoning_core as rc
from agent.ingestion.adapters import json_adapter, syslog_adapter, prometheus_adapter
from agent.ingestion import realtime_gateway
from agent.memory import decision_memory


def test_guardrail_raises_undercalled_p1():
    a = {"asset_id": "T1", "tier": 1, "criticality_score": 90,
         "rpo_target_hours": 4, "hours_since_last_backup": 2, "consecutive_failures": 3}
    final, diverged, note = rc._apply_guardrail("WARN", a, rc.compute_risk(a))
    assert final == "ESCALATE_P1" and diverged and "SAFETY FLOOR" in note


def test_model_authority_kept_on_soft_disagreement():
    a = {"asset_id": "T2", "tier": 3, "criticality_score": 40,
         "rpo_target_hours": 24, "hours_since_last_backup": 4, "consecutive_failures": 0}
    final, diverged, _ = rc._apply_guardrail("WARN", a, rc.compute_risk(a))
    assert final == "WARN" and diverged  # rules say NONE; model's WARN stands, flagged


def test_invalid_action_falls_back_to_policy():
    a = {"asset_id": "T3", "tier": 3, "criticality_score": 40,
         "rpo_target_hours": 24, "hours_since_last_backup": 4, "consecutive_failures": 0}
    final, diverged, _ = rc._apply_guardrail("LAUNCH_MISSILES", a, rc.compute_risk(a))
    assert final in rc.VALID_ACTIONS and not diverged


def test_rule_engine_full_fleet_never_crashes():
    from agent.ingestion import loghub_engine
    result = rc.rule_engine_fallback(loghub_engine.get_all_assets())
    assert result["assessments"] and result["fallback_mode"] is True


def test_batching_merges_full_fleet():
    from agent.ingestion import loghub_engine
    assets = loghub_engine.get_all_assets()
    r = rc.reason(assets)  # no provider in CI -> rule path through batch merge
    assert len(r["assessments"]) == len(assets)


def test_adapters_normalize():
    assert json_adapter.normalize({"asset_id": "A", "tier": "2"})[0]["tier"] == 2
    d = syslog_adapter.normalize("backupd Backup FAILED for asset=WEB-01")
    assert d and d[0]["consecutive_failures"] == 1
    p = prometheus_adapter.normalize('backup_age_seconds{asset="X"} 7200')
    assert p and p[0]["hours_since_last_backup"] == 2.0


def test_gateway_ingest_and_stale_flag():
    realtime_gateway.reset()
    r = realtime_gateway.ingest({"asset_id": "LIVE-1", "tier": 1}, source="json")
    assert r["ok"] and realtime_gateway.live_asset_count() == 1


def test_memory_trend_detection():
    for score in (10, 40, 90):
        decision_memory.record({"asset_id": "TREND-X", "action": "WARN",
                                "risk_score": score, "rpo_consumed_pct": score,
                                "consecutive_failures": 1})
    mem = decision_memory.recall("TREND-X")
    assert mem["risk_trend"] == "rising"


def test_executor_dry_run_records():
    from agent.actions import executor
    rec = asyncio.run(executor.submit(
        {"asset_id": "E1", "asset_name": "E1", "action": "RETRY_BACKUP",
         "explanation": "t"}, "cy"))
    assert rec["status"] in ("dry_run", "logged")


def test_expert_policy_detection_patterns():
    from scripts.slm_dataset import expert_action
    base = {"asset_id": "X", "tier": 2, "criticality_score": 60,
            "rpo_target_hours": 8, "hours_since_last_backup": 2,
            "consecutive_failures": 0, "restore_test_days_overdue": 0}
    risk = rc.compute_risk(base)
    assert expert_action(base, risk, None, "Checksum mismatch detected on backup archive") == "MANUAL_REVIEW"
    t = dict(base, consecutive_failures=1)
    assert expert_action(t, rc.compute_risk(t), None,
                         "Connection reset by peer during transfer, retry recommended") == "RETRY_BACKUP"
    assert expert_action(base, risk, {"persistent_incident": True, "risk_trend": "flat"}, "") == "WARN"


def test_rag_similarity_recall():
    from agent.memory import incident_rag
    incident_rag.remember_incident({"asset_id": "RAG-1", "action": "RETRY_BACKUP",
                                    "risk_score": 100, "explanation": "transient block serving exception",
                                    "evidence": "Got exception while serving blk_123 to client"})
    hits = incident_rag.similar_incidents("exception while serving blk_999", "RAG-2")
    assert hits and hits[0]["action_taken"] == "RETRY_BACKUP"
