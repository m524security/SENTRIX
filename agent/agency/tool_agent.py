"""
SENTRIX — Tool-Use Agency (the model chooses its next step)
===========================================================
The fixed pipeline (perceive→reason→act) becomes genuine agency for the cases
that deserve it: when an initial assessment is an escalation candidate or a
model/policy divergence, SENTRIX lets the model INVESTIGATE before finalizing —
it picks tools, reads the results, and only then commits.

Tools available to the model:
  memory        — this asset's decision history and risk trend
  forecast      — predictive engine breach forecast for this asset
  similar       — RAG recall of the most similar past incidents
  fleet_context — health of sibling assets in the same dataset (systemic vs isolated)

Loop: up to MAX_STEPS tool calls, then a mandatory final decision. Every step
is captured in an `investigation` trace shown on the dashboard — you can watch
the agent think. Any failure at any step falls back to the initial assessment;
agency can never make SENTRIX less reliable than v4.0.
"""
from __future__ import annotations

import json

from agent.reasoning import llm_providers

MAX_STEPS = 4

_TOOL_PROMPT = """You are SENTRIX, investigating ONE asset before finalizing your action.

INITIAL ASSESSMENT:
{initial}

You may call tools to gather context before deciding. Available tools:
  memory        - this asset's recent decision history and risk trend
  forecast      - RPO-breach forecast for this asset
  similar       - most similar past incidents and what was done
  fleet_context - health of sibling assets in the same dataset (systemic vs isolated?)

TOOL RESULTS SO FAR:
{observations}

Respond with ONE JSON object, nothing else:
  To investigate:  {{"tool": "memory|forecast|similar|fleet_context"}}
  To finalize:     {{"final": {{"action": "<ACTION>", "explanation": "<one sentence citing what you found>", "confidence": 0.0-1.0}}}}
Do not call a tool you already called. Finalize once you have enough context."""


def _tool_memory(asset):
    from agent.memory import decision_memory
    return decision_memory.recall(asset.get("asset_id"))


def _tool_forecast(asset):
    from agent.reasoning import predictive_engine
    return predictive_engine.predict_rpo_breach(asset)


def _tool_similar(asset):
    from agent.memory import incident_rag
    return incident_rag.similar_incidents(asset.get("evidence") or "", asset.get("asset_id", ""))


def _tool_fleet_context(asset):
    from agent.ingestion import fleet_source
    ds = asset.get("dataset", "all")
    siblings = [a for a in fleet_source.get_fleet(ds) if a.get("asset_id") != asset.get("asset_id")]
    failing = [a["asset_id"] for a in siblings if a.get("consecutive_failures", 0) >= 1]
    return {"siblings": len(siblings), "siblings_failing": failing[:6],
            "pattern": "systemic" if len(failing) >= max(2, len(siblings) // 3) else "isolated"}


_TOOLS = {"memory": _tool_memory, "forecast": _tool_forecast,
          "similar": _tool_similar, "fleet_context": _tool_fleet_context}


def investigate(asset: dict, initial_assessment: dict) -> dict:
    """
    Run the tool-use loop. Returns the (possibly refined) assessment with an
    `investigation` trace attached. Never raises.
    """
    observations = {}
    trace = []
    initial_compact = {k: initial_assessment.get(k) for k in
                       ("asset_id", "asset_name", "action", "risk_score", "explanation")}
    initial_compact["evidence"] = asset.get("evidence")

    try:
        for step in range(MAX_STEPS + 1):
            force_final = step == MAX_STEPS
            prompt = _TOOL_PROMPT.format(
                initial=json.dumps(initial_compact),
                observations=json.dumps(observations) if observations else "(none yet)")
            if force_final:
                prompt += "\nYou MUST finalize now."
            reply = llm_providers.call_llm_json(prompt)
            reply.pop("_provider", None); reply.pop("_model", None)

            if "final" in reply and isinstance(reply["final"], dict):
                final = reply["final"]
                refined = dict(initial_assessment)
                if final.get("action"):
                    refined["action"] = final["action"]
                if final.get("explanation"):
                    refined["explanation"] = str(final["explanation"])[:400]
                try:
                    refined["confidence"] = float(final.get("confidence", refined.get("confidence", 0.8)))
                except (TypeError, ValueError):
                    pass
                refined["investigation"] = trace
                refined["agency"] = True
                return refined

            tool = str(reply.get("tool", "")).strip()
            if tool not in _TOOLS or tool in observations:
                continue  # invalid or repeated tool — reprompt
            result = _TOOLS[tool](asset)
            observations[tool] = result
            trace.append({"step": step + 1, "tool": tool,
                          "result_summary": json.dumps(result, default=str)[:220]})
    except Exception as e:
        trace.append({"error": str(e)[:150]})

    fallback = dict(initial_assessment)
    fallback["investigation"] = trace
    return fallback
