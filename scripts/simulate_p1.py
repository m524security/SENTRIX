"""Simulate a P1 breach and verify SENTRIX escalates correctly."""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.reasoning.reasoning_core import reason

p1_scenario = [{
    "asset_id": "SAP-ERP-TEST",
    "asset_name": "SAP ERP (P1 Test)",
    "tier": 1,
    "criticality_score": 95,
    "rpo_target_hours": 4,
    "hours_since_last_backup": 20,  # 500% RPO consumed
    "consecutive_failures": 2,
    "last_backup_status": "failed",
    "restore_test_days_overdue": 0
}]

result = reason(p1_scenario)
assessments = result.get("assessments", [])

if assessments and assessments[0].get("action") == "ESCALATE_P1":
    print("✅ SENTRIX correctly escalated P1")
    print(f"   Explanation: {assessments[0].get('explanation')}")
else:
    print("❌ SENTRIX did NOT escalate P1 — check system prompt")
    print(f"   Got: {assessments[0].get('action') if assessments else 'NO RESPONSE'}")