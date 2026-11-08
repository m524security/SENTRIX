"""
SENTRIX — Autonomous Agent Loop
Perceive -> Reason -> Predict -> Act -> Publish

Every cycle the agent:
  1. Perceives current asset state (LogHub-grounded, deterministic per tick)
  2. Reasons over it (multi-provider LLM, deterministic rule-engine fallback)
  3. Forecasts near-term RPO breaches (predictive engine)
  4. Acts (alerts, restore-test scheduling, audit logging)
  5. Publishes the cycle to the API, which fans it out to dashboard clients

The publish step POSTs to the API's /api/agent/cycle endpoint. The previous
version pushed an 'agent_cycle' message over a websocket the API never
processed, so live cycles never reached the dashboard — fixed here.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx

from agent import config
from agent.ingestion import loghub_engine
from agent.ingestion import fleet_source
from agent.reasoning.reasoning_core import reason_with_fallback, reason_async, compute_risk, _hard_floor_action
from agent.reasoning.predictive_engine import predict_fleet
from agent.memory.audit_logger import write_audit_log
from agent.actions.notifiers import fan_out
from agent.actions import executor
from agent import ai_safety
from agent.recovery import confidence as prc
from agent.recovery import evidence_ledger, evidence_scheduler

from agent.logging_setup import get_logger

API_BASE = config.API_WS_URL.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")

_logger = get_logger("agent")
_LEVEL_MAP = {"INFO": _logger.info, "OK": _logger.info, "WARN": _logger.warning,
              "ERROR": _logger.error, "P1": _logger.critical, "P2": _logger.warning}


def log(level: str, message: str):
    """Kept as the module-wide helper, now backed by structured logging:
    human-readable lines in dev, one JSON object per line in production
    (SENTRIX_LOG_JSON=true or SENTRIX_ENV=production)."""
    _LEVEL_MAP.get(level, _logger.info)(message, extra={"tag": level})


async def publish_cycle(cycle_entry: dict):
    """POST the completed cycle to the API so every connected dashboard sees
    it live. Best-effort — a missing API must never stall the agent loop."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{API_BASE}/api/agent/cycle", json=cycle_entry)
    except Exception as e:
        log("WARN", f"Publish to API skipped ({e}) — agent continues")


async def dispatch_action(assessment: dict, cycle_id: str):
    action = assessment.get("action", "NONE")
    asset_id = assessment.get("asset_id", "unknown")
    asset_name = assessment.get("asset_name", asset_id)
    reason_text = assessment.get("explanation", "")
    severity = "info"
    delivered = None

    if action == "ESCALATE_P1":
        severity = "p1"
        log("P1", f"{asset_name}: {reason_text}")
        fan = await fan_out(asset_name, "P1", reason_text)
        delivered = fan["any_delivered"]
    elif action == "ESCALATE_P2":
        severity = "p2"
        log("P2", f"{asset_name}: {reason_text}")
        fan = await fan_out(asset_name, "P2", reason_text)
        delivered = fan["any_delivered"]
    elif action == "WARN":
        severity = "warning"
        log("WARN", f"{asset_name}: {reason_text}")
    elif action == "SCHEDULE_RESTORE_TEST":
        log("INFO", f"Restore test scheduled — {asset_name}")
    elif action == "RETRY_BACKUP":
        log("INFO", f"Retrying backup — {asset_name}")
    elif action == "MANUAL_REVIEW":
        severity = "warning"
        log("WARN", f"Manual review needed — {asset_name}: {reason_text}")

    # v4.1 — real action execution (dry_run / approve / auto, see agent/actions/executor.py)
    if action in executor.EXECUTABLE_ACTIONS:
        try:
            record = await executor.submit(assessment, cycle_id)
            log("INFO", f"Action {record['status']} [{record['mode']}] — {action} on {asset_name} (id {record['id']})")
        except Exception as e:
            log("WARN", f"Action executor error: {e}")

    result_note = None
    if delivered is True:
        result_note = "alert_delivered"
    elif delivered is False:
        result_note = "alert_not_delivered"

    try:
        await write_audit_log(
            cycle_id=cycle_id, event_type="action", asset_id=asset_id, severity=severity,
            input_summary=f"{action} on {asset_name}", reasoning_output=reason_text,
            action_taken=action, action_result=result_note,
        )
    except Exception as e:
        log("WARN", f"Audit write failed: {e}")


async def refine_with_agency(result: dict, assets: list) -> dict:
    """v4.1 — for escalation candidates and divergences, run the tool-use
    investigation (SENTRIX_AGENCY) and the critic review (SENTRIX_CRITIC)
    before actions dispatch. Both are opt-in and can only refine, never break."""
    if not (config.AGENCY or config.CRITIC) or result.get("fallback_mode"):
        return result
    by_id = {a.get("asset_id"): a for a in assets}
    refined = []
    for assessment in result.get("assessments", []):
        asset = by_id.get(assessment.get("asset_id"))
        needs_look = assessment.get("action") in ("ESCALATE_P1", "ESCALATE_P2") or assessment.get("diverged")
        if asset is None or not needs_look:
            refined.append(assessment); continue
        a = assessment
        if config.AGENCY:
            try:
                from agent.agency import tool_agent
                a = await asyncio.to_thread(tool_agent.investigate, asset, a)
            except Exception as e:
                log("WARN", f"agency skipped: {e}")
        if config.CRITIC and a.get("action") in ("ESCALATE_P1", "ESCALATE_P2"):
            try:
                from agent.agency import critic
                floor = _hard_floor_action(asset, compute_risk(asset))
                a = await asyncio.to_thread(critic.review, a, asset, floor)
            except Exception as e:
                log("WARN", f"critic skipped: {e}")
        refined.append(a)
    result["assessments"] = refined
    result["critical_count"] = sum(1 for x in refined if "ESCALATE" in x.get("action",""))
    result["healthy_count"] = sum(1 for x in refined if x.get("action") == "NONE")
    return result


async def run_cycle(cycle_number: int, dataset: str = None):
    log("INFO", f"Cycle {cycle_number} starting")
    try:
        if dataset and dataset != "all":
            assets = fleet_source.get_fleet(dataset)
            log("INFO", f"Dataset '{dataset}' — {len(assets)} assets")
        else:
            assets = fleet_source.get_fleet('all')
            log("INFO", f"Perceived {len(assets)} assets across all datasets")

        # --- PS284 core: recovery readiness, not backup monitoring -----------
        # 1. Attach real restore-test evidence from the ledger (falls back to
        #    simulated evidence only where the ledger is empty).
        assets = evidence_ledger.enrich(assets)
        # 2. Score PROVEN recovery confidence — deterministic arithmetic over
        #    ledger facts. No LLM touches this number.
        readiness = prc.score_fleet(assets)
        log("INFO", f"Fleet recovery confidence {readiness['fleet_confidence_pct']}% "
                    f"— {readiness['blind_spot_count']} blind spot(s)")
        # 3. AGENTIC STEP: the agent knows what it doesn't know. Where confidence
        #    is low because EVIDENCE IS MISSING (not because backups are failing),
        #    schedule the restore tests that most reduce fleet-wide uncertainty.
        test_plan = evidence_scheduler.plan(assets)
        if test_plan["scheduled"]:
            log("INFO", evidence_scheduler.explain_plan(test_plan))
        # 4. Feed confidence into reasoning as evidence, not as the rulebook.
        for a in assets:
            s = next((x for x in readiness["assets"] if x["asset_id"] == a.get("asset_id")), None)
            if s:
                a["recovery_confidence_pct"] = s["confidence_pct"]
                a["recovery_band"] = s["band"]
                a["recovery_gaps"] = s["gaps"]

        result = await reason_async(assets)
        result = await refine_with_agency(result, assets)
        cycle_id = result.get("cycle_id", f"cycle-{cycle_number}")
        critical = result.get("critical_count", 0)
        healthy = result.get("healthy_count", 0)
        summary = result.get("summary", "")
        fallback = result.get("fallback_mode", False)
        provider = result.get("provider", "unknown")

        mode = "rule-engine" if fallback else f"LLM:{provider}"
        log("INFO", f"Reasoning [{mode}] — {critical} critical, {healthy} healthy")

        assessments = result.get("assessments", [])
        # AI safety gate: LLM output is untrusted input. Validate schema, clamp
        # scores, enforce the action allow-list, and drop any assessment whose
        # asset_id fails the same regex the ingestion boundary enforces —
        # BEFORE anything can reach the action executor.
        assessments = ai_safety.validate_assessments(assessments)
        result["assessments"] = assessments
        # Publish readiness + the evidence-acquisition plan with the cycle so the
        # dashboard shows PROVEN recoverability and the queue that improves it.
        result["recovery_readiness"] = {
            "fleet_confidence_pct": readiness["fleet_confidence_pct"],
            "blind_spot_count": readiness["blind_spot_count"],
            "bands": readiness["bands"],
            "blind_spots": readiness["blind_spots"][:10],
        }
        result["evidence_plan"] = {
            "scheduled": test_plan["scheduled"],
            "spent_hours": test_plan["spent_hours"],
            "budget_hours": test_plan["budget_hours"],
            "confidence_uplift_pct": test_plan["confidence_uplift_pct"],
            "summary": evidence_scheduler.explain_plan(test_plan),
        }
        forecasts = predict_fleet(assets, only_at_risk=True)
        high_forecasts = [f for f in forecasts if f["risk"] == "high"]
        if high_forecasts:
            log("WARN", f"Predictive engine: {len(high_forecasts)} asset(s) forecast to breach RPO soon")

        action_count = 0
        for a in assessments:
            if a.get("action", "NONE") != "NONE":
                await dispatch_action(a, cycle_id)
                action_count += 1
        log("OK", f"Cycle {cycle_number} complete — {action_count} actions dispatched")

        cycle_entry = {
            "cycle_id": cycle_id,
            "type": "agent_cycle",
            "cycle_number": cycle_number,
            "dataset": dataset or "all",
            "timestamp": datetime.utcnow().isoformat(),
            "fallback_mode": fallback,
            "provider": provider,
            "model": result.get("model", ""),
            "critical_count": critical,
            "healthy_count": healthy,
            "summary": summary,
            "asset_count": len(assets),
            "action_count": action_count,
            "decisions": assessments,
            "forecasts": forecasts,
        }
        await publish_cycle(cycle_entry)

        try:
            await write_audit_log(
                cycle_id=cycle_id, event_type="perception", asset_id="SYSTEM", severity="info",
                input_summary=f"Cycle {cycle_number}: {len(assets)} assets, mode={mode}",
                reasoning_output=summary, action_taken="cycle_complete",
            )
        except Exception as e:
            log("WARN", f"Audit write failed: {e}")
        return result
    except Exception as e:
        import traceback
        log("ERROR", f"Cycle {cycle_number} failed: {e}")
        log("ERROR", traceback.format_exc())
        return None


async def sentrix_main():
    print("")
    print("  S E N T R I X  —  Autonomous Recovery & Resilience Intelligence")
    print("  Recover faster. Risk smarter.")
    print("  " + "-" * 44)
    reg = loghub_engine.warm_cache()
    grounded = sum(1 for v in reg.values() if v["data_grounded"])
    total_assets = sum(v["asset_count"] for v in reg.values())
    log("OK", f"Loaded {len(reg)} LogHub datasets ({grounded} data-grounded), {total_assets} simulated assets")

    chain = config.active_provider_names()
    if chain:
        log("OK", f"LLM provider chain: {' -> '.join(chain)}"
                  + (" -> ollama" if config.USE_LOCAL_FALLBACK else "") + " -> rule-engine")
    else:
        log("WARN", "No LLM provider configured — running on deterministic rule-engine only")
        log("INFO", "Add OPENROUTER_API_KEY or NVIDIA_API_KEY (both free) to .env to enable LLM reasoning")

    log("OK", f"Agent starting [mode={config.MODE}] — cycle every {config.CYCLE_SECONDS}s. Ctrl+C to stop.")
    print("")

    cycle = 0
    dataset_arg = sys.argv[1] if len(sys.argv) > 1 else None
    heartbeat = Path(__file__).resolve().parent.parent / "data" / "agent.heartbeat"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    while True:
        cycle += 1
        await run_cycle(cycle, dataset=dataset_arg)
        # Liveness heartbeat consumed by the Docker HEALTHCHECK: a hung loop
        # stops touching this file and the container is flagged unhealthy.
        try:
            heartbeat.touch()
        except Exception:
            pass
        log("INFO", f"Sleeping {config.CYCLE_SECONDS}s...")
        await asyncio.sleep(config.CYCLE_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(sentrix_main())
    except KeyboardInterrupt:
        print("")
        log("INFO", "SENTRIX stopped by operator")
