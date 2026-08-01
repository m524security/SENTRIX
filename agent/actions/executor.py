"""
SENTRIX — Action Executor
=========================
Turns SENTRIX from an advisor into an operator. In v4.0, RETRY_BACKUP and
SCHEDULE_RESTORE_TEST were log lines; now every actionable decision flows
through a connector with four safety modes (SENTRIX_ACTION_MODE):

  off      — log only (legacy behavior)
  dry_run  — connector resolved and recorded, nothing executed (DEFAULT)
  approve  — queued as 'pending'; a human approves via POST /api/actions/approve
  auto     — executed immediately (for mature deployments)

Connectors (each degrades gracefully if unconfigured):
  command  — templated shell command (e.g. SENTRIX_RETRY_COMMAND="restic backup {asset_id}")
             executed with a hard timeout, never with shell=True on user data
  webhook  — POST to SENTRIX_ACTION_WEBHOOK with the full action record,
             so any orchestrator (Ansible/AWX, n8n, Lambda) can take over

Every attempt — pending, executed, failed, dry-run — is persisted to SQLite
and HMAC-audited, so the action trail is as tamper-evident as decisions.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import shlex
import uuid
from datetime import datetime, timezone

import httpx

from agent import config, persistence

EXECUTABLE_ACTIONS = {"RETRY_BACKUP", "SCHEDULE_RESTORE_TEST"}
_COMMAND_FOR = {
    "RETRY_BACKUP": lambda: config.RETRY_COMMAND,
    "SCHEDULE_RESTORE_TEST": lambda: config.RESTORE_TEST_COMMAND,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Second layer of the argument-injection defense (first layer: the ingestion
# gateway validates asset_id at the trust boundary). Even if a hostile value
# slips in via another path, it is stripped to a safe character set here and
# can never start with '-' — so it can't smuggle a flag like '--delete' into
# restic or a restore script.
_SAFE_FIELD = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_field(value) -> str:
    cleaned = _SAFE_FIELD.sub("", str(value))[:128]
    return cleaned.lstrip("-")


def _render_command(template: str, asset: dict) -> list:
    """Safe templating: only whitelisted + sanitized fields, shell-split, no shell=True."""
    safe = {k: _sanitize_field(asset.get(k, "")) for k in ("asset_id", "asset_name", "dataset", "tier")}
    try:
        rendered = template.format(**safe)
    except (KeyError, IndexError):
        rendered = template
    return shlex.split(rendered)


async def _run_command(argv: list, timeout: int = 120) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "detail": f"timed out after {timeout}s"}
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "output": (out or b"").decode(errors="ignore")[-800:]}
    except FileNotFoundError:
        return {"ok": False, "detail": f"command not found: {argv[0] if argv else '?'}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


def _sign_payload(payload_bytes: bytes) -> dict:
    """HMAC-SHA256 signature headers for outgoing webhooks (uses SENTRIX_HMAC_KEY).

    The action webhook can trigger real orchestrator work (Ansible/AWX, n8n,
    Lambda). Without a signature, anything that learns the URL can spoof it.
    Receivers verify with:
        hmac.compare_digest(sig, hmac.new(key, ts.encode() + b"." + body, sha256).hexdigest())
    The timestamp is included in the signed material to block replay attacks
    (receivers should reject signatures older than ~5 minutes).
    """
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    mac = hmac.new(config.HMAC_KEY.encode(), ts.encode() + b"." + payload_bytes, hashlib.sha256)
    return {
        "X-Sentrix-Timestamp": ts,
        "X-Sentrix-Signature": f"sha256={mac.hexdigest()}",
    }


async def _fire_webhook(record: dict) -> dict:
    if not config.ACTION_WEBHOOK_URL:
        return {"ok": False, "detail": "no webhook configured"}
    try:
        body = json.dumps(record, default=str).encode()
        headers = {"Content-Type": "application/json", **_sign_payload(body)}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(config.ACTION_WEBHOOK_URL, content=body, headers=headers)
        return {"ok": 200 <= r.status_code < 300, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


async def _execute(record: dict, asset: dict) -> dict:
    """Run all configured connectors for this action; aggregate results."""
    results = {}
    template = _COMMAND_FOR.get(record["action"], lambda: "")()
    if template:
        results["command"] = await _run_command(_render_command(template, asset))
    if config.ACTION_WEBHOOK_URL:
        results["webhook"] = await _fire_webhook(record)
    if not results:
        results["noop"] = {"ok": True, "detail": "no connector configured; recorded only"}
    ok = any(v.get("ok") for v in results.values())
    return {"ok": ok, "connectors": results}


async def submit(assessment: dict, cycle_id: str = "") -> dict:
    """
    Entry point called by the agent loop for every actionable decision.
    Returns the persisted action record (status: logged|dry_run|pending|executed|failed).
    """
    action = assessment.get("action", "NONE")
    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "cycle_id": cycle_id,
        "asset_id": assessment.get("asset_id", ""),
        "asset_name": assessment.get("asset_name", ""),
        "action": action,
        "mode": config.ACTION_MODE,
        "explanation": assessment.get("explanation", ""),
        "status": "logged",
        "result": None,
    }

    if action not in EXECUTABLE_ACTIONS or config.ACTION_MODE == "off":
        persistence.save_action(record)
        return record

    if config.ACTION_MODE == "dry_run":
        template = _COMMAND_FOR.get(action, lambda: "")()
        record["status"] = "dry_run"
        record["result"] = {
            "would_run": _render_command(template, assessment) if template else None,
            "would_webhook": bool(config.ACTION_WEBHOOK_URL),
        }
        persistence.save_action(record)
        return record

    if config.ACTION_MODE == "approve":
        record["status"] = "pending"
        persistence.save_action(record)
        return record

    # auto
    record["result"] = await _execute(record, assessment)
    record["status"] = "executed" if record["result"]["ok"] else "failed"
    persistence.save_action(record)
    return record


async def approve(action_id: str) -> dict:
    """Human approval path (approve mode): execute a pending action now."""
    record = persistence.get_action(action_id)
    if not record:
        return {"ok": False, "error": "action not found"}
    if record.get("status") != "pending":
        return {"ok": False, "error": f"action is '{record.get('status')}', not pending"}
    result = await _execute(record, record)
    status = "executed" if result["ok"] else "failed"
    persistence.update_action_status(action_id, status, {"result": result, "approved_at": _now()})
    return {"ok": True, "id": action_id, "status": status, "result": result}


def reject(action_id: str) -> dict:
    if persistence.update_action_status(action_id, "rejected"):
        return {"ok": True, "id": action_id, "status": "rejected"}
    return {"ok": False, "error": "action not found"}
