"""
SENTRIX — Persistence Layer (SQLite, zero external deps)
========================================================
v4.0 kept cycle history, live fleet and approvals in per-process memory: lost
on restart, split-brained under multiple workers. This module gives SENTRIX a
durable spine using stdlib sqlite3 (WAL mode), with the same graceful-
degradation contract as everything else — any storage failure logs and the
agent keeps running from memory.

Tables:
  cycles(cycle_id PK, ts, payload JSON)          — full cycle records
  actions(id PK, ts, asset_id, action, status,
          mode, payload JSON)                    — executed/pending actions
  incidents(id PK, ts, asset_id, action,
            explanation, evidence, risk_score)   — searchable incident history (RAG source)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent import config
from agent.logging_setup import get_logger

_log = get_logger("persistence")

DB_PATH = Path(config.DB_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()
_conn = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""CREATE TABLE IF NOT EXISTS cycles(
            cycle_id TEXT PRIMARY KEY, ts TEXT, payload TEXT)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS actions(
            id TEXT PRIMARY KEY, ts TEXT, asset_id TEXT, action TEXT,
            status TEXT, mode TEXT, payload TEXT)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS incidents(
            id TEXT PRIMARY KEY, ts TEXT, asset_id TEXT, action TEXT,
            explanation TEXT, evidence TEXT, risk_score REAL)""")
        _conn.commit()
    return _conn


def save_cycle(entry: dict):
    try:
        with _lock:
            _db().execute("INSERT OR REPLACE INTO cycles VALUES(?,?,?)",
                          (entry.get("cycle_id", uuid.uuid4().hex[:8]),
                           entry.get("timestamp", _now()),
                           json.dumps(entry, default=str)))
            _db().commit()
    except Exception as e:
        _log.error("cycle save failed (memory-only this cycle): %s", e)


def load_recent_cycles(limit: int = 50) -> list:
    try:
        with _lock:
            rows = _db().execute(
                "SELECT payload FROM cycles ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception:
        return []


def save_action(rec: dict):
    try:
        with _lock:
            _db().execute("INSERT OR REPLACE INTO actions VALUES(?,?,?,?,?,?,?)",
                          (rec["id"], rec.get("ts", _now()), rec.get("asset_id", ""),
                           rec.get("action", ""), rec.get("status", ""),
                           rec.get("mode", ""), json.dumps(rec, default=str)))
            _db().commit()
    except Exception as e:
        _log.error("action save failed: %s", e)


def update_action_status(action_id: str, status: str, extra: dict = None):
    try:
        with _lock:
            row = _db().execute("SELECT payload FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                return False
            payload = json.loads(row[0]); payload["status"] = status
            if extra:
                payload.update(extra)
            _db().execute("UPDATE actions SET status=?, payload=? WHERE id=?",
                          (status, json.dumps(payload, default=str), action_id))
            _db().commit()
        return True
    except Exception:
        return False


def list_actions(status: str = None, limit: int = 50) -> list:
    try:
        with _lock:
            if status:
                rows = _db().execute("SELECT payload FROM actions WHERE status=? ORDER BY ts DESC LIMIT ?",
                                     (status, limit)).fetchall()
            else:
                rows = _db().execute("SELECT payload FROM actions ORDER BY ts DESC LIMIT ?",
                                     (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception:
        return []


def get_action(action_id: str):
    try:
        with _lock:
            row = _db().execute("SELECT payload FROM actions WHERE id=?", (action_id,)).fetchone()
        return json.loads(row[0]) if row else None
    except Exception:
        return None


def save_incident(asset_id: str, action: str, explanation: str, evidence: str, risk_score: float):
    try:
        with _lock:
            _db().execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?)",
                          (uuid.uuid4().hex, _now(), asset_id, action,
                           (explanation or "")[:500], (evidence or "")[:300], float(risk_score or 0)))
            _db().commit()
    except Exception:
        pass


def load_incidents(limit: int = 400) -> list:
    try:
        with _lock:
            rows = _db().execute(
                "SELECT ts, asset_id, action, explanation, evidence, risk_score "
                "FROM incidents ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "asset_id": r[1], "action": r[2],
                 "explanation": r[3], "evidence": r[4], "risk_score": r[5]} for r in rows]
    except Exception:
        return []
