"""
SENTRIX — Dashboard & Control API
--------------------------------
FastAPI backend the React dashboard talks to. Responsibilities:
  - serve current fleet state, risk summary, datasets (all LogHub-grounded)
  - accept completed agent cycles and fan them out live over WebSocket
  - run on-demand simulations and predictions
  - back the conversational ChatBox with a real, context-grounded LLM call
  - expose provider/health status so the UI can show what's actually running
  - AI Engine: transformer log analysis, LSTM anomaly detection, YOLO vision,
    time-series ML forecasting, HuggingFace dataset browsing

Key fix vs the old version: the agent now POSTs cycles to /api/agent/cycle,
which broadcasts to every connected dashboard. Previously the agent pushed a
websocket message type the server never handled, so the live feed stayed empty.
"""

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config
from agent.logging_setup import get_logger

_log = get_logger("api")


def _utcnow() -> datetime:
    """Timezone-aware UTC now, formatted like the naive datetime.utcnow() this
    replaced (deprecated in 3.12+) so existing consumers keep working."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from agent.ingestion import loghub_engine
from agent.reasoning.reasoning_core import reason_with_fallback, reason_async, rule_engine_fallback, compute_risk, decide_action
from agent.reasoning import predictive_engine, llm_providers
from agent.memory import audit_logger, decision_memory
from agent.ingestion import realtime_gateway, fleet_source
from agent import security, persistence, ai_safety
from agent.recovery import confidence as prc
from agent.recovery import evidence_ledger, evidence_scheduler
from agent.ratelimit import limiter
from agent.actions import executor

# --- AI Engine modules (lazy-import so missing deps never break the API) ---
def _import_transformer_engine():
    try:
        from agent.reasoning import transformer_engine
        return transformer_engine
    except Exception:
        return None

def _import_anomaly_detector():
    try:
        from agent.reasoning import anomaly_detector
        return anomaly_detector
    except Exception:
        return None

def _import_yolo_monitor():
    try:
        from agent.vision import yolo_monitor
        return yolo_monitor
    except Exception:
        return None

def _import_time_series():
    try:
        from agent.ml import time_series
        return time_series
    except Exception:
        return None

def _import_hf_loader():
    try:
        from agent.ingestion import hf_dataset_loader
        return hf_dataset_loader
    except Exception:
        return None

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown. Replaces the deprecated @app.on_event hooks, which
    are slated for removal in a future FastAPI release."""
    loghub_engine.warm_cache()
    evidence_ledger.load()
    try:
        for entry in reversed(persistence.load_recent_cycles(50)):
            cycle_history.append(entry)
        if cycle_history:
            _log.info("restored %s cycle(s) from persistence", len(cycle_history))
    except Exception as e:
        _log.warning("persistence restore skipped: %s", e)
    _log.info("API v%s up — fleet mode: %s", config.VERSION, config.MODE.upper())
    yield


app = FastAPI(title="SENTRIX API", description="Autonomous Recovery & Resilience Intelligence — Control API", version=config.VERSION, lifespan=lifespan)
security.enforce_startup_policy()
# CORS: never silently fall back to "*" — if SENTRIX_CORS_ORIGINS is somehow
# empty, default to the local dashboard rather than every origin on the web.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS or ["http://localhost:3000"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    """Standard browser-hardening headers (audit recommendation)."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    # This API serves JSON, not HTML — a restrictive CSP is safe and blocks
    # any accidental rendering of LLM-generated text as active content.
    response.headers.setdefault("Content-Security-Policy",
                                "default-src 'none'; frame-ancestors 'none'")
    if config.ENV_NAME == "production":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response

cycle_history = []
CYCLE_HISTORY_MAX = 200


class ConnectionManager:
    def __init__(self):
        self.active = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self):
        return len(self.active)


manager = ConnectionManager()


def add_cycle(entry: dict):
    cycle_history.insert(0, entry)
    del cycle_history[CYCLE_HISTORY_MAX:]
    persistence.save_cycle(entry)


def normalize_assessments(assessments: list) -> list:
    # AI safety gate first: LLM output is untrusted input. Enforces the action
    # allow-list, clamps numeric ranges, and drops entries whose asset_id fails
    # the ingestion-boundary regex (so a hostile id can't ride an assessment
    # into the approve-action → executor path).
    assessments = ai_safety.validate_assessments(assessments)
    out = []
    for a in assessments:
        out.append({
            "asset_id": a.get("asset_id", ""),
            "asset_name": a.get("asset_name", a.get("asset_id", "")),
            "risk_score": round(float(a.get("risk_score", 0) or 0), 1),
            "rpo_percentage": round(float(a.get("rpo_consumed_pct", a.get("rpo_percentage", 0)) or 0), 1),
            "action": a.get("action", "NONE"),
            "explanation": a.get("explanation", ""),
            "evidence": a.get("evidence"),
            "mode": "rule" if a.get("fallback_mode") else "llm",
            "tier": a.get("tier", 3),
            "dataset": a.get("dataset", "core"),
            "confidence": a.get("confidence", 0.75),
        })
    return out


def _cycle_summary_record(result: dict, assets: list, kind: str, scenario_id=None, dataset="all") -> dict:
    decisions = normalize_assessments(result.get("assessments", []))
    return {
        "cycle_id": result.get("cycle_id", f"sim-{uuid.uuid4().hex[:8]}"),
        "type": kind,
        "scenario_id": scenario_id,
        "dataset": dataset,
        "timestamp": _utcnow().isoformat(),
        "fallback_mode": result.get("fallback_mode", False),
        "provider": result.get("provider", "unknown"),
        "model": result.get("model", ""),
        "critical_count": result.get("critical_count", 0),
        "healthy_count": result.get("healthy_count", 0),
        "summary": result.get("summary", ""),
        "asset_count": len(assets),
        "decisions": decisions,
    }


# --------------------------------------------------------------------------- #
# Core / health
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {"agent": config.PRODUCT_NAME, "version": config.VERSION, "mode": config.MODE, "status": "running", "websocket_clients": manager.count}


@app.get("/api/health")
def health():
    try:
        assets = fleet_source.get_fleet('all')
        total = len(assets)
        healthy = sum(1 for a in assets if a.get("consecutive_failures", 0) == 0)
        critical = sum(1 for a in assets if a.get("consecutive_failures", 0) >= 3)
        success_rate = (healthy / max(total, 1)) * 100
    except Exception:
        total = success_rate = critical = 0
    return {
        "total_assets": total,
        "backup_success_rate": round(success_rate, 1),
        "active_alerts": critical,
        "cycles_recorded": len(cycle_history),
        "websocket_clients": manager.count,
        "provider": llm_providers.provider_status(),
        "last_updated": _utcnow().isoformat(),
    }


# --------------------------------------------------------------------------- #
# Observability: k8s-style probes + Prometheus metrics (stdlib only)
# --------------------------------------------------------------------------- #
_started_at = _utcnow()


@app.get("/livez")
def livez():
    """Liveness probe: the process is up and the event loop responds."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness probe: dependencies needed to serve real traffic are ready.
    Returns 503 (via status field + code) if the fleet source can't be read,
    so orchestrators stop routing traffic to a broken replica."""
    from fastapi.responses import JSONResponse
    try:
        fleet_source.get_fleet("all")
        persistence.load_recent_cycles(1)
        return {"status": "ready"}
    except Exception as e:
        return JSONResponse(status_code=503,
                            content={"status": "not_ready", "detail": str(e)[:200]})


@app.get("/metrics")
def metrics():
    """Prometheus text-exposition metrics — no client library needed.
    Scrape with a standard prometheus job; pairs with Grafana dashboards."""
    from fastapi.responses import PlainTextResponse
    try:
        assets = fleet_source.get_fleet("all")
        total = len(assets)
        healthy = sum(1 for a in assets if a.get("consecutive_failures", 0) == 0)
        critical = sum(1 for a in assets if a.get("consecutive_failures", 0) >= 3)
    except Exception:
        total = healthy = critical = 0
    uptime = (_utcnow() - _started_at).total_seconds()
    lines = [
        "# HELP sentrix_up 1 if the API is serving.",
        "# TYPE sentrix_up gauge",
        "sentrix_up 1",
        "# HELP sentrix_uptime_seconds Seconds since API start.",
        "# TYPE sentrix_uptime_seconds counter",
        f"sentrix_uptime_seconds {uptime:.0f}",
        "# HELP sentrix_assets_total Assets currently tracked.",
        "# TYPE sentrix_assets_total gauge",
        f"sentrix_assets_total {total}",
        "# HELP sentrix_assets_healthy Assets with zero consecutive failures.",
        "# TYPE sentrix_assets_healthy gauge",
        f"sentrix_assets_healthy {healthy}",
        "# HELP sentrix_assets_critical Assets with 3+ consecutive failures.",
        "# TYPE sentrix_assets_critical gauge",
        f"sentrix_assets_critical {critical}",
        "# HELP sentrix_cycles_recorded Cycles held in the in-memory history.",
        "# TYPE sentrix_cycles_recorded gauge",
        f"sentrix_cycles_recorded {len(cycle_history)}",
        "# HELP sentrix_websocket_clients Connected dashboard websockets.",
        "# TYPE sentrix_websocket_clients gauge",
        f"sentrix_websocket_clients {manager.count}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4")


@app.get("/api/provider")
def provider_status():
    """What LLM backend is configured and what last answered — for the UI badge."""
    return llm_providers.provider_status()


# --------------------------------------------------------------------------- #
# Assets / datasets / risk
# --------------------------------------------------------------------------- #
@app.get("/api/assets")
def get_assets(dataset: str = "all"):
    try:
        assets = fleet_source.get_fleet(dataset)
        return {"count": len(assets), "dataset": dataset, "assets": assets}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/datasets")
def list_datasets():
    try:
        return loghub_engine.dataset_registry()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/datasets/{dataset_id}/assets")
def dataset_assets(dataset_id: str):
    try:
        assets = loghub_engine.get_assets_for_dataset(dataset_id)
        return {"dataset": dataset_id, "count": len(assets), "assets": assets}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/risk-summary")
def risk_summary():
    try:
        assets = fleet_source.get_fleet('all')
    except Exception:
        assets = []
    tiers = {t: {"total": 0, "healthy": 0, "critical": 0} for t in (1, 2, 3, 4)}
    for a in assets:
        t = a.get("tier", 3)
        t = t if t in tiers else 3
        tiers[t]["total"] += 1
        consec = a.get("consecutive_failures", 0)
        if consec == 0:
            tiers[t]["healthy"] += 1
        if consec >= 3:
            tiers[t]["critical"] += 1
    return {
        "tier_1": tiers[1], "tier_2": tiers[2], "tier_3": tiers[3], "tier_4": tiers[4],
        "total_assets": sum(t["total"] for t in tiers.values()),
        "total_critical": sum(t["critical"] for t in tiers.values()),
    }


# --------------------------------------------------------------------------- #
# Predictions
# --------------------------------------------------------------------------- #
@app.get("/api/predictions")
def predictions(dataset: str = "all"):
    try:
        assets = fleet_source.get_fleet(dataset)
        forecasts = predictive_engine.predict_fleet(assets, only_at_risk=False)
        at_risk = [f for f in forecasts if f["risk"] in ("high", "medium")]
        return {
            "count": len(forecasts),
            "high": sum(1 for f in forecasts if f["risk"] == "high"),
            "medium": sum(1 for f in forecasts if f["risk"] == "medium"),
            "forecasts": forecasts,
            "at_risk": at_risk,
        }
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
@app.get("/api/simulate/scenarios")
def list_scenarios():
    scenarios_dir = ROOT / "tests" / "scenarios"
    all_scenarios = []
    if scenarios_dir.exists():
        for f in sorted(scenarios_dir.glob("*scenarios*.json")):
            try:
                data = json.loads(f.read_text())
                for s in data:
                    s["file"] = f.name
                    all_scenarios.append(s)
            except Exception:
                pass
    return {"count": len(all_scenarios), "scenarios": all_scenarios}


@app.post("/api/simulate/trigger")
async def trigger_simulation(
    body: dict = Body(...),
    _auth=Depends(security.require_write_token),
    _rl=Depends(limiter("simulate", rate_per_min=10, burst=5)),
):
    """Runs the LLM reasoning chain and broadcasts the result to every connected
    dashboard as a live cycle — the most expensive endpoint in the app and a
    forgery vector, so it's write-token protected AND rate limited (this was
    the unauthenticated cost/forgery hole flagged in the security audit)."""
    scenario_id = body.get("scenario_id")
    dataset = body.get("dataset", "all")
    use_fallback = body.get("use_fallback", False)
    try:
        if scenario_id:
            assets = _assets_from_scenario(scenario_id)
            if assets is None:
                return {"error": f"Scenario '{scenario_id}' not found"}
        else:
            assets = fleet_source.get_fleet(dataset)

        result = rule_engine_fallback(assets) if use_fallback else await reason_async(assets)
        result["assessments"] = normalize_assessments(result.get("assessments", []))
        result["simulation"] = {
            "scenario_id": scenario_id, "dataset": dataset, "use_fallback": use_fallback,
            "asset_count": len(assets), "timestamp": _utcnow().isoformat(),
        }
        entry = _cycle_summary_record(result, assets, "simulation", scenario_id, dataset)
        add_cycle(entry)
        await manager.broadcast({"type": "cycle_update", "cycle": entry})
        return result
    except Exception as e:
        _log.error("simulate error", exc_info=True)
        return {"error": "simulation failed", "detail": str(e)[:200]}


def _assets_from_scenario(scenario_id: str):
    scenarios_dir = ROOT / "tests" / "scenarios"
    universe = loghub_engine._build_asset_universe()
    for f in scenarios_dir.glob("*.json"):
        try:
            scenarios = json.loads(f.read_text())
        except Exception:
            continue
        for sc in scenarios:
            sid = sc.get("id") or sc.get("scenario_id")
            if sid != scenario_id:
                continue
            asset_map = sc.get("assets") or {}
            if not asset_map and sc.get("asset_state"):
                asset_map = {sc["asset_state"]["asset_id"]: sc["asset_state"]}
            assets = []
            for aid, override in asset_map.items():
                base = universe.get(aid, {
                    "asset_id": aid, "asset_name": aid, "tier": 2,
                    "criticality_score": 50, "rpo_target_hours": 8, "dataset": sc.get("category", "core"),
                })
                merged = {**base, **(override or {})}
                merged.setdefault("hours_since_last_backup", 0)
                merged.setdefault("consecutive_failures", 0)
                merged.setdefault("restore_test_days_overdue", 0)
                assets.append(merged)
            return assets
    return None


# --------------------------------------------------------------------------- #
# Agent cycle ingest (called by the agent loop) + cycle history
# --------------------------------------------------------------------------- #
@app.post("/api/agent/cycle")
async def ingest_agent_cycle(cycle: dict = Body(...), _auth=Depends(security.require_write_token)):
    """The autonomous agent POSTs completed cycles here; we normalize, store,
    and fan out to every connected dashboard. THIS is the live pipeline."""
    decisions = normalize_assessments(cycle.get("decisions", []))
    entry = {
        "cycle_id": cycle.get("cycle_id", f"cycle-{uuid.uuid4().hex[:8]}"),
        "type": "agent_cycle",
        "cycle_number": cycle.get("cycle_number"),
        "dataset": cycle.get("dataset", "all"),
        "timestamp": cycle.get("timestamp", _utcnow().isoformat()),
        "fallback_mode": cycle.get("fallback_mode", False),
        "provider": cycle.get("provider", "unknown"),
        "model": cycle.get("model", ""),
        "critical_count": cycle.get("critical_count", 0),
        "healthy_count": cycle.get("healthy_count", 0),
        "summary": cycle.get("summary", ""),
        "asset_count": cycle.get("asset_count", len(decisions)),
        "action_count": cycle.get("action_count", 0),
        "decisions": decisions,
        "forecasts": cycle.get("forecasts", []),
    }
    add_cycle(entry)
    await manager.broadcast({"type": "cycle_update", "cycle": entry})
    return {"ok": True, "cycle_id": entry["cycle_id"], "broadcast_to": manager.count}


@app.get("/api/cycles")
def get_cycles(limit: int = 20):
    return {"cycles": cycle_history[:limit]}


@app.get("/api/cycles/{cycle_id}")
def get_cycle_detail(cycle_id: str):
    for entry in cycle_history:
        if entry.get("cycle_id") == cycle_id:
            return entry
    return {"error": "Cycle not found"}


# --------------------------------------------------------------------------- #
# Audit & restore tests
# --------------------------------------------------------------------------- #
@app.get("/api/audit")
def get_audit_log(limit: int = 50):
    records = audit_logger.read_recent(limit)
    if records:
        return records
    return cycle_history[:limit]


@app.get("/api/restore-tests")
def get_restore_tests():
    """Derive restore-test evidence from current fleet state (assets whose
    restore drill is overdue), so this panel works with zero external DB."""
    try:
        assets = fleet_source.get_fleet('all')
    except Exception:
        return []
    records = []
    for a in assets:
        overdue = a.get("restore_test_days_overdue", 0)
        if overdue > 0:
            records.append({
                "asset_id": a["asset_id"],
                "asset_name": a["asset_name"],
                "tier": a["tier"],
                "status": "overdue",
                "days_overdue": overdue,
                "cadence_days": a.get("cadence_days"),
                "created_at": _utcnow().isoformat(),
            })
    records.sort(key=lambda r: r["days_overdue"], reverse=True)
    return records[:25]


# --------------------------------------------------------------------------- #
# Conversational copilot
# --------------------------------------------------------------------------- #
_CHAT_PROMPT_PATH = ROOT / "agent" / "prompts" / "chat_prompt.txt"
_CHAT_SYSTEM = _CHAT_PROMPT_PATH.read_text(encoding="utf-8") if _CHAT_PROMPT_PATH.exists() else \
    "You are SENTRIX, an IT recovery readiness copilot. Answer from the provided context."


def _build_chat_context() -> dict:
    assets = fleet_source.get_fleet('all')
    # Compute current risk + action for each so chat answers match the dashboard.
    enriched = []
    for a in assets:
        risk = compute_risk(a)
        enriched.append({
            "asset_id": a["asset_id"], "asset_name": a["asset_name"], "tier": a["tier"],
            "dataset": a["dataset"], "rpo_consumed_pct": risk["rpo_consumed_pct"],
            "risk_score": risk["risk_score"], "action": decide_action(a, risk),
            "consecutive_failures": a.get("consecutive_failures", 0),
            "hours_since_last_backup": a.get("hours_since_last_backup"),
            "evidence": a.get("evidence"),
            "note": (f"{a.get('consecutive_failures', 0)} consecutive failures, "
                     f"{risk['rpo_consumed_pct']:.0f}% of RPO window consumed"),
        })
    escalations = [e for e in enriched if "ESCALATE" in e["action"]]
    forecasts = predictive_engine.predict_fleet(assets, only_at_risk=True)
    return {
        "fleet_size": len(enriched),
        "escalations": escalations[:20],
        "at_risk_forecasts": forecasts[:15],
        "healthy_count": sum(1 for e in enriched if e["action"] == "NONE"),
        "top_risk_assets": sorted(enriched, key=lambda e: e["risk_score"], reverse=True)[:15],
    }


@app.post("/api/chat")
async def chat(body: dict = Body(...), _rl=Depends(limiter("chat"))):
    """Conversational endpoint backing the dashboard ChatBox. Grounds every
    answer in the real current fleet state, then routes through the same
    multi-provider LLM chain. Degrades to a deterministic summary if no LLM
    provider is available."""
    user_message = (body.get("message") or "").strip()
    history = body.get("history", [])
    if not user_message:
        return {"reply": "Ask me about fleet health, risks, or what to prioritize.", "provider": "none"}

    # AI safety: cap sizes, strip control chars, flag prompt-injection framing.
    # The chat LLM has no tools and its output is display-only, but a poisoned
    # history could still make it emit misleading operator guidance.
    user_message, history, injection_suspected = ai_safety.guard_chat_input(user_message, history)

    context = _build_chat_context()
    history_text = ""
    for turn in history[-6:]:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        history_text += f"\n{role.upper()}: {content}"

    prompt = (
        _CHAT_SYSTEM
        + "\n\nLIVE FLEET CONTEXT (JSON):\n" + json.dumps(context, indent=2)
        + (f"\n\nRECENT CONVERSATION:{history_text}" if history_text else "")
        + f"\n\nUSER: {user_message}\nSENTRIX:"
    )

    try:
        result = await asyncio.to_thread(llm_providers.call_llm, prompt)
        reply = ai_safety.scrub_llm_reply(result["text"].strip())
        if reply.startswith("{") and reply.endswith("}"):
            try:
                parsed = json.loads(reply)
                reply = parsed.get("message") or parsed.get("reply") or parsed.get("summary") or parsed.get("text") or reply
            except (json.JSONDecodeError, AttributeError):
                pass
        if reply.startswith("```") and reply.endswith("```"):
            reply = reply.strip("`").strip()
            if reply.startswith("json"):
                reply = reply[4:].strip()
        return {"reply": reply, "provider": result["provider"], "model": result["model"]}
    except llm_providers.LLMAllProvidersFailedError:
        return {"reply": _deterministic_chat_reply(user_message, context),
                "provider": "rule_engine", "model": "deterministic"}
    except Exception as e:
        return {"reply": _deterministic_chat_reply(user_message, context),
                "provider": "rule_engine", "model": f"deterministic (llm error: {e})"}


def _deterministic_chat_reply(message: str, context: dict) -> str:
    """A useful answer with no LLM available — keeps the ChatBox functional."""
    esc = context["escalations"]
    forecasts = context["at_risk_forecasts"]
    lines = [
        f"Fleet status: {context['fleet_size']} assets, {context['healthy_count']} healthy, "
        f"{len(esc)} currently escalated.",
    ]
    if esc:
        lines.append("Active escalations (most urgent first):")
        for e in esc[:5]:
            lines.append(f"  - {e['asset_id']} (tier {e['tier']}, {e['action']}, risk {e['risk_score']}): {e.get('note','')}")
    if forecasts:
        lines.append("Predicted to breach RPO soon:")
        for f in forecasts[:4]:
            lines.append(f"  - {f['asset_id']} [{f['risk']}]: {f['reason']}")
    if not esc and not forecasts:
        lines.append("No active escalations or near-term breach forecasts. Fleet looks stable.")
    lines.append("(LLM provider unavailable — this is a deterministic summary. Add a free OpenRouter or NVIDIA key to enable full chat.)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# AI Engine endpoints
# --------------------------------------------------------------------------- #

@app.get("/api/ml-status")
def ml_status():
    """Overall status of all AI/ML modules — transformer, anomaly, YOLO, time-series."""
    te = _import_transformer_engine()
    ad = _import_anomaly_detector()
    ym = _import_yolo_monitor()
    ts = _import_time_series()
    hf = _import_hf_loader()
    try:
        from agent.reasoning import slm_local
        slm_status = slm_local.status()
        slm_status["enabled"] = config.USE_SLM
    except Exception as e:
        slm_status = {"available": False, "error": str(e)}
    return {
        "transformer_engine": te.ml_status() if te else {"available": False, "error": "import failed"},
        "anomaly_detector": ad.detector_status() if ad else {"available": False, "error": "import failed"},
        "yolo_monitor": ym.monitor_status() if ym else {"available": False, "error": "import failed"},
        "time_series_forecaster": ts.forecaster_status() if ts else {"available": False, "error": "import failed"},
        "hf_dataset_loader": hf.loader_status() if hf else {"available": False, "error": "import failed"},
        "slm_local": slm_status,
        "timestamp": _utcnow().isoformat(),
    }


@app.get("/api/ai-insights")
def ai_insights(dataset: str = "all"):
    """
    Transformer-based log analysis across the fleet.
    Uses DistilBERT + zero-shot classification to score each asset's log evidence.
    """
    te = _import_transformer_engine()
    try:
        assets = fleet_source.get_fleet(dataset)
    except Exception as e:
        return {"error": str(e)}

    if te is None:
        return {
            "ok": False,
            "error": "transformer_engine module unavailable",
            "analyzed": 0,
            "critical_signals": [],
            "fleet_anomaly_score": 0.0,
            "method": "unavailable",
        }

    try:
        result = te.analyze_fleet_logs(assets)
        result["dataset"] = dataset
        result["asset_count"] = len(assets)
        result["timestamp"] = _utcnow().isoformat()
        result["model_status"] = te.ml_status()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "method": "error"}


@app.get("/api/anomaly-scores")
def anomaly_scores(dataset: str = "all"):
    """
    LSTM + statistical anomaly detection across the fleet time series.
    Returns per-asset anomaly scores and top anomalies.
    """
    ad = _import_anomaly_detector()
    try:
        assets = fleet_source.get_fleet(dataset)
    except Exception as e:
        return {"error": str(e)}

    if ad is None:
        return {"ok": False, "error": "anomaly_detector module unavailable"}

    try:
        result = ad.score_fleet(assets)
        result["dataset"] = dataset
        result["timestamp"] = _utcnow().isoformat()
        result["detector_status"] = ad.detector_status()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


_predictions_cache: dict = {}

@app.get("/api/ml-predictions")
def ml_predictions(dataset: str = "all", horizon: int = 6, max_assets: int = 20):
    """
    Deep-learning enhanced RPO breach forecasts.
    Uses Transformer → Exp.Smoothing → Linear cascade for each asset.
    Capped at max_assets (default 20) for response-time; increase for deeper runs.
    Results cached for 120s to keep the dashboard fast.
    """
    import time
    cache_key = f"{dataset}:{horizon}:{max_assets}"
    cached = _predictions_cache.get(cache_key)
    if cached and (time.time() - cached["_ts"]) < 120:
        return cached["data"]

    ts = _import_time_series()
    try:
        assets = fleet_source.get_fleet(dataset)
    except Exception as e:
        return {"error": str(e)}

    if ts is None:
        return {"ok": False, "error": "time_series module unavailable"}

    try:
        assets_subset = assets[:max_assets]
        result = ts.forecast_fleet(assets_subset, horizon=min(horizon, 12))
        result["dataset"] = dataset
        result["total_fleet_size"] = len(assets)
        result["assets_sampled"] = len(assets_subset)
        result["timestamp"] = _utcnow().isoformat()
        result["forecaster_status"] = ts.forecaster_status()
        _predictions_cache[cache_key] = {"data": result, "_ts": time.time()}
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/visual-analysis")
def visual_analysis():
    """
    YOLO-based visual fleet analysis.
    Generates a synthetic dashboard frame from current asset state and runs
    object detection on it. Upload a real screenshot via POST /api/visual-analysis/upload.
    """
    ym = _import_yolo_monitor()
    try:
        assets = fleet_source.get_fleet('all')
    except Exception as e:
        return {"error": str(e)}

    if ym is None:
        return {"ok": False, "error": "yolo_monitor module unavailable"}

    try:
        result = ym.analyze_fleet_frames(assets)
        result["asset_count"] = len(assets)
        result["timestamp"] = _utcnow().isoformat()
        result["monitor_status"] = ym.monitor_status()
        # Don't return full base64 in the list endpoint — too large
        result.pop("frame_base64", None)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB decoded — plenty for a screenshot
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpg",
}


def _sniff_image(data: bytes) -> str | None:
    for magic, kind in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return kind
    return None


@app.post("/api/visual-analysis/upload")
async def visual_upload(
    body: dict = Body(...),
    _auth=Depends(security.require_write_token),
    _rl=Depends(limiter("visual_upload", rate_per_min=12, burst=6)),
):
    """
    Analyze a user-provided base64-encoded PNG/JPG screenshot.
    Body: { "image_base64": "...", "filename": "screenshot.png" }

    Hardened per security audit: this endpoint decodes attacker-supplied bytes
    and writes them to disk, then feeds them to image-parsing libraries — so it
    now requires the write token, rate limits, caps the decoded size, rejects
    invalid base64, and verifies PNG/JPEG magic bytes before anything touches
    disk or OpenCV/YOLO.
    """
    import base64
    ym = _import_yolo_monitor()
    if ym is None:
        return {"ok": False, "error": "yolo_monitor module unavailable"}

    b64 = body.get("image_base64", "")
    filename = str(body.get("filename", "upload.png"))[:128]
    if not b64 or not isinstance(b64, str):
        return {"ok": False, "error": "image_base64 required"}
    # Fast pre-decode size gate: base64 inflates ~4/3, so cap the encoded length too.
    if len(b64) > MAX_UPLOAD_BYTES * 4 // 3 + 16:
        return {"ok": False, "error": f"image too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)"}

    try:
        img_bytes = base64.b64decode(b64, validate=True)
    except Exception:
        return {"ok": False, "error": "invalid base64 payload"}
    if len(img_bytes) > MAX_UPLOAD_BYTES:
        return {"ok": False, "error": f"image too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)"}
    if _sniff_image(img_bytes) is None:
        return {"ok": False, "error": "unsupported file type — PNG or JPEG only"}

    tmp = Path(ROOT) / "data" / "sample" / f"_upload_{uuid.uuid4().hex[:8]}.png"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(img_bytes)
        result = ym.analyze_screenshot(str(tmp))
        result["filename"] = filename
        result["timestamp"] = _utcnow().isoformat()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/hf-datasets")
def hf_datasets_info():
    """Browse available HuggingFace + local LogHub datasets."""
    hf = _import_hf_loader()
    if hf is None:
        return {"ok": False, "error": "hf_dataset_loader module unavailable"}
    try:
        return {"ok": True, **hf.available_datasets(), "timestamp": _utcnow().isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/hf-datasets/{dataset_key}")
def hf_dataset_load(dataset_key: str, source: str = "local", max_samples: int = 200):
    """
    Load a specific dataset.
    source=local  → use bundled LogHub CSVs (instant, no network)
    source=hf     → download from HuggingFace Hub (requires 'datasets' package)
    """
    hf = _import_hf_loader()
    if hf is None:
        return {"ok": False, "error": "hf_dataset_loader module unavailable"}
    try:
        if source == "hf":
            return hf.load_hf_dataset(dataset_key, max_samples=max_samples)
        return hf.load_local_loghub_as_hf(dataset_key, max_samples=max_samples)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ai-insights/classify")
async def classify_log(body: dict = Body(...)):
    """
    Classify a single log line via zero-shot transformer.
    Body: { "log_line": "...", "find_similar": true }
    """
    te = _import_transformer_engine()
    if te is None:
        return {"ok": False, "error": "transformer_engine unavailable"}

    log_line = body.get("log_line", "").strip()
    if not log_line:
        return {"ok": False, "error": "log_line required"}

    try:
        severity = te.classify_log_severity(log_line)
        anomaly_score = te.anomaly_score_from_text(log_line)
        result = {
            "ok": True,
            "log_line": log_line[:200],
            "anomaly_score": anomaly_score,
            **severity,
        }

        if body.get("find_similar"):
            try:
                assets = fleet_source.get_fleet('all')
                pool = [a["evidence"] for a in assets if a.get("evidence")][:50]
                similar = te.find_similar_incidents(log_line, pool, top_k=5)
                result["similar_incidents"] = similar
            except Exception:
                pass

        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# REAL-TIME INGESTION  (production path — real telemetry, not simulation)
# --------------------------------------------------------------------------- #
@app.post("/api/ingest")
async def ingest_telemetry(body: dict = Body(...), _auth=Depends(security.require_write_token), _rl=Depends(limiter("ingest", rate_per_min=240, burst=120))):
    """
    Push REAL telemetry into SENTRIX. The agent will reason over it on the next
    cycle when SENTRIX_MODE is 'live' or 'hybrid'.

    Body: { "source": "json|syslog|prometheus", "payload": <see adapter> }
      json       -> payload is a dict/list of asset states
      syslog     -> payload is a string of log line(s)
      prometheus -> payload is Prometheus text or a metrics dict
    """
    source = (body.get("source") or "json").lower()
    payload = body.get("payload")
    receipt = realtime_gateway.ingest(payload, source=source)
    # If we're live, immediately reflect the new state to dashboards.
    if config.MODE in ("live", "hybrid") and receipt.get("ok"):
        try:
            await manager.broadcast({"type": "live_ingest", "receipt": receipt,
                                     "timestamp": _utcnow().isoformat()})
        except Exception:
            pass
    return receipt


@app.get("/api/ingest/status")
def ingest_status():
    return fleet_source.source_status()


@app.post("/api/ingest/reset")
def ingest_reset(_auth=Depends(security.require_write_token)):
    realtime_gateway.reset()
    return {"ok": True, "message": "live fleet cleared"}


@app.get("/api/mode")
def get_mode():
    """Current fleet mode + how to change it."""
    return {
        "mode": config.MODE,
        "options": ["simulation", "live", "hybrid"],
        "how_to_change": "set SENTRIX_MODE in .env and restart",
        "live_assets": realtime_gateway.live_asset_count(),
    }


# --------------------------------------------------------------------------- #
# AGENTIC INTELLIGENCE INTROSPECTION
# --------------------------------------------------------------------------- #
@app.get("/api/divergence")
def divergence(limit: int = 20):
    """
    Where the AI and the deterministic policy DISAGREED — the highest-signal
    output in the system, and exactly what v3 used to throw away. Each entry is
    a case the model reasoned to a different call than the rulebook, kept for
    human review.
    """
    out = []
    for cyc in cycle_history:
        for d in cyc.get("decisions", []):
            if d.get("diverged"):
                out.append({
                    "cycle_id": cyc.get("cycle_id"),
                    "timestamp": cyc.get("timestamp"),
                    "asset_id": d.get("asset_id"),
                    "asset_name": d.get("asset_name"),
                    "model_action": d.get("model_action"),
                    "final_action": d.get("action"),
                    "note": d.get("guardrail_note"),
                    "explanation": d.get("explanation"),
                })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return {"count": len(out), "divergences": out}


@app.get("/api/memory")
def agent_memory(limit: int = 50):
    """What the agent currently remembers per asset — the loop that makes it evolve."""
    return decision_memory.snapshot(limit)


@app.get("/api/memory/signals")
def memory_signals(dataset: str = "all"):
    """Assets whose remembered risk is climbing — early warning before threshold."""
    assets = fleet_source.get_fleet(dataset)
    return {"signals": decision_memory.outcome_signal(assets)}


# --------------------------------------------------------------------------- #
# ACTION EXECUTION — approvals & history (SENTRIX_ACTION_MODE=approve)
# --------------------------------------------------------------------------- #
@app.get("/api/actions")
def list_actions(status: str = None, limit: int = 50):
    """Action records: pending approvals, executed, dry-run, failed."""
    return {"mode": config.ACTION_MODE, "actions": persistence.list_actions(status, limit)}


@app.post("/api/actions/approve")
async def approve_action(body: dict = Body(...), _auth=Depends(security.require_write_token)):
    action_id = (body.get("id") or "").strip()
    if not action_id:
        return {"ok": False, "error": "id required"}
    result = await executor.approve(action_id)
    if result.get("ok"):
        await manager.broadcast({"type": "action_update", "action": result})
    return result


@app.post("/api/actions/reject")
def reject_action(body: dict = Body(...), _auth=Depends(security.require_write_token)):
    action_id = (body.get("id") or "").strip()
    return executor.reject(action_id) if action_id else {"ok": False, "error": "id required"}


# --------------------------------------------------------------------------- #
# STREAMING INGEST — persistent WebSocket for high-frequency real telemetry
#   Connect: ws://host:8000/ws/ingest?token=<SENTRIX_API_TOKEN>
#   Send:    {"source":"json|syslog|prometheus","payload":...}   (one per message)
#   Recv:    per-message receipt
# --------------------------------------------------------------------------- #
@app.websocket("/ws/ingest")
async def websocket_ingest(ws: WebSocket, token: str = ""):
    # Constant-time comparison — `!=` short-circuits on the first differing byte
    # and leaks the token prefix through close-timing. Same fix as
    # security.require_write_token.
    if not security.check_ws_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_json({"ok": False, "error": "invalid JSON"})
                continue
            receipt = realtime_gateway.ingest(msg.get("payload"), source=(msg.get("source") or "json").lower())
            await ws.send_json(receipt)
            if receipt.get("ok") and config.MODE in ("live", "hybrid"):
                await manager.broadcast({"type": "live_ingest", "receipt": receipt,
                                         "timestamp": _utcnow().isoformat()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    # Read-only broadcast, but it streams live asset names, criticality and
    # cycle telemetry — that is fleet intelligence, not public data. Auth is
    # enforced whenever a token is configured (always, in production).
    if not security.check_ws_token(token):
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        # Send current history immediately so a freshly-opened dashboard isn't blank.
        await ws.send_json({"type": "cycle_history", "cycles": cycle_history[:20]})
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif msg.get("type") == "subscribe_cycles":
                await ws.send_json({"type": "cycle_history", "cycles": cycle_history[:20]})
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# =========================================================================== #
# PS284 — Recovery readiness API
# =========================================================================== #
# The deliverable is a "recovery readiness dashboard". These endpoints back it.
# The framing difference from every backup tool: we do not report whether
# backups SUCCEEDED, we report whether recovery is PROVEN — and we schedule the
# tests that close the gap.

@app.get("/api/recovery/readiness")
def recovery_readiness(dataset: str = "all"):
    """Fleet recovery readiness: criticality-weighted Proven Recovery
    Confidence, band distribution, and the ranked blind spots.

    Deterministic — pure arithmetic over ledger facts, no LLM involved."""
    try:
        assets = evidence_ledger.enrich(fleet_source.get_fleet(dataset))
        result = prc.score_fleet(assets)
        result["dataset"] = dataset
        result["evidence_stats"] = evidence_ledger.stats()
        return result
    except Exception as e:
        _log.error("readiness scoring failed", exc_info=True)
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/recovery/asset/{asset_id}")
def recovery_asset(asset_id: str):
    """Full provenance for one asset: the score, every weighted contribution,
    the evidence chain behind it, and its restore-test history.

    This is the screen that answers "why should I believe this number?"."""
    try:
        assets = evidence_ledger.enrich(fleet_source.get_fleet("all"))
        asset = next((a for a in assets if a.get("asset_id") == asset_id), None)
        if not asset:
            return {"ok": False, "error": "asset not found"}
        scored = prc.score_asset(asset)
        scored["history"] = evidence_ledger.history(asset_id, limit=20)
        scored["decay_curve"] = prc.decay_curve(asset)
        scored["next_test"] = evidence_scheduler.marginal_value(asset)
        return scored
    except Exception as e:
        _log.error("asset readiness failed", exc_info=True)
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/recovery/plan")
def recovery_plan(dataset: str = "all", budget_hours: float = 24.0):
    """THE AGENTIC ENDPOINT. Given a test budget, which restore tests most
    reduce fleet-wide recovery uncertainty?

    Returns the schedule, the projected confidence uplift if every test passes,
    and — honestly — what stays unproven after the whole budget is spent."""
    try:
        assets = evidence_ledger.enrich(fleet_source.get_fleet(dataset))
        p = evidence_scheduler.plan(assets, budget_hours=max(0.25, min(budget_hours, 200)))
        p["summary"] = evidence_scheduler.explain_plan(p)
        p["dataset"] = dataset
        return p
    except Exception as e:
        _log.error("evidence planning failed", exc_info=True)
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/recovery/evidence")
async def record_restore_test(
    body: dict = Body(...),
    _auth=Depends(security.require_write_token),
    _rl=Depends(limiter("evidence", rate_per_min=120, burst=60)),
):
    """CLOSES THE LOOP. A scheduled restore test completed — record the result.

    The next scoring pass picks it up automatically: confidence rises (or, if
    the drill failed, drops to prove non-recoverability). Write-protected and
    HMAC-signed, because restore evidence is exactly what gets quietly edited
    after a bad audit.

    Body: {asset_id, test_type, outcome, rto_actual_seconds?, bytes_restored?,
           checksum_verified?, notes?}
    """
    try:
        rec = evidence_ledger.record_test(
            asset_id=body.get("asset_id", ""),
            test_type=body.get("test_type", ""),
            outcome=body.get("outcome", ""),
            rto_actual_seconds=body.get("rto_actual_seconds"),
            bytes_restored=body.get("bytes_restored"),
            checksum_verified=body.get("checksum_verified"),
            notes=body.get("notes", ""),
            started_at=body.get("started_at"),
            cycle_id=body.get("cycle_id", ""),
        )
        # Re-score immediately so the caller sees the loop close.
        assets = evidence_ledger.enrich(fleet_source.get_fleet("all"))
        asset = next((a for a in assets if a.get("asset_id") == rec["asset_id"]), None)
        rescored = prc.score_asset(asset) if asset else None
        payload = {"ok": True, "record": rec, "rescored": rescored}
        await manager.broadcast({"type": "evidence_recorded", "data": payload})
        return payload
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        _log.error("evidence write failed", exc_info=True)
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/recovery/evidence")
def list_evidence(asset_id: str = None, limit: int = 100):
    """Append-only restore-test ledger with per-record signature verification."""
    return {
        "records": evidence_ledger.history(asset_id, limit=min(limit, 500)),
        "stats": evidence_ledger.stats(),
    }


@app.get("/api/recovery/calibration")
def recovery_calibration(tier: int = 1):
    """Falsifiability endpoint: fit the evidence half-life λ from observed
    restore outcomes instead of asserting it.

    λ is the model's central claim — this is how you prove or disprove it."""
    records = evidence_ledger.history(limit=500)
    history = []
    for r in records:
        if r.get("test_type") == "none":
            continue
        history.append({
            "days_since_prior_test": r.get("days_since_prior_test", 0),
            "restore_succeeded": r.get("outcome") == "passed",
        })
    return prc.calibrate_lambda(history, tier)
