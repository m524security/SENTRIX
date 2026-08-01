"""
SENTRIX — Tamper-Evident Audit Logger
-------------------------------------
Every agent decision is written to an append-only audit trail with an HMAC
signature so records can be verified later. Writes to Supabase when it's
configured; ALWAYS also mirrors to a local JSONL file so the audit trail
survives even when Supabase is unreachable (the original version simply
swallowed the error and lost the record).
"""

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from agent import config
from agent.logging_setup import get_logger

_log = get_logger("audit")

# Key lives in agent/config.py so security.enforce_startup_policy() can
# hard-fail production when it's still the public default (audit signatures
# signed with a known key are forgeable, i.e. not tamper-evident at all).
HMAC_KEY = config.HMAC_KEY.encode()
LOCAL_AUDIT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "audit_log.jsonl"
LOCAL_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

_supabase = None
if config.SUPABASE_URL and config.SUPABASE_KEY:
    try:
        from supabase import create_client
        _supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    except Exception as e:
        _log.warning("Supabase init failed, using local file only: %s", e)


def create_signature(data: dict) -> str:
    content = json.dumps(data, sort_keys=True, default=str)
    return hmac.new(HMAC_KEY, content.encode(), hashlib.sha256).hexdigest()[:32]


def verify_signature(record: dict) -> bool:
    sig = record.get("signature")
    if not sig:
        return False
    clone = {k: v for k, v in record.items() if k != "signature"}
    return hmac.compare_digest(sig, create_signature(clone))


def _write_local(record: dict):
    try:
        with open(LOCAL_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        _log.error("local audit write failed: %s", e)


async def write_audit_log(cycle_id, event_type, asset_id, severity,
                          input_summary, reasoning_output,
                          action_taken=None, action_result=None):
    record = {
        "log_id": str(uuid.uuid4()),
        "cycle_id": cycle_id,
        "timestamp": datetime.utcnow().isoformat(),
        "agent_version": "3.0.0",
        "event_type": event_type,
        "asset_id": asset_id,
        "severity_level": severity,
        "input_summary": input_summary,
        "reasoning_output": reasoning_output,
        "action_taken": action_taken,
        "action_result": action_result,
    }
    record["signature"] = create_signature(record)

    # Local mirror always happens first so the record can't be lost.
    _write_local(record)

    if _supabase is not None:
        try:
            _supabase.table("agent_audit_log").insert(record).execute()
        except Exception as e:
            _log.warning("Supabase write failed (kept locally): %s", e)

    return record


def read_recent(limit: int = 50) -> list:
    """Read recent audit records, preferring Supabase, falling back to the
    local JSONL mirror. Used by the API's /api/audit endpoint."""
    if _supabase is not None:
        try:
            res = (_supabase.table("agent_audit_log")
                   .select("*").order("timestamp", desc=True).limit(limit).execute())
            if res.data:
                return res.data
        except Exception:
            pass

    if not LOCAL_AUDIT_PATH.exists():
        return []
    try:
        with open(LOCAL_AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        records = [json.loads(ln) for ln in lines if ln.strip()]
        return list(reversed(records))[:limit]
    except Exception:
        return []
