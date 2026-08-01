"""
SENTRIX — Incident RAG (similar-incident recall)
================================================
Before deciding on a risky asset, SENTRIX recalls how similar past incidents
looked and what was done — like an engineer thinking "we saw this exact HDFS
block-serving error last month; a retry fixed it."

Deliberately dependency-free: bag-of-words cosine similarity over the SQLite
incident history. It is not a vector database and does not pretend to be one —
but for operational log text, token overlap is a strong, transparent baseline
that runs everywhere. Swap `_similarity` for embeddings when you add them.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from agent import persistence

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "with", "was", "this", "that", "from", "asset", "backup"}


def _tokens(text: str) -> Counter:
    return Counter(t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP)


def _similarity(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[t] * b[t] for t in common)
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def similar_incidents(evidence: str, asset_id: str = "", top_k: int = 3, min_score: float = 0.18) -> list:
    """Top-k past incidents most similar to this evidence text."""
    if not evidence:
        return []
    query = _tokens(f"{asset_id} {evidence}")
    scored = []
    for inc in persistence.load_incidents(limit=400):
        doc = _tokens(f"{inc.get('asset_id','')} {inc.get('evidence','')} {inc.get('explanation','')}")
        s = _similarity(query, doc)
        if s >= min_score:
            scored.append((s, inc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"similarity": round(s, 2), "when": i["ts"][:10], "asset_id": i["asset_id"],
             "action_taken": i["action"], "note": (i["explanation"] or "")[:140]}
            for s, i in scored[:top_k]]


def remember_incident(assessment: dict):
    """Store actionable decisions as searchable incidents."""
    if assessment.get("action", "NONE") == "NONE":
        return
    persistence.save_incident(
        assessment.get("asset_id", ""), assessment.get("action", ""),
        assessment.get("explanation", ""), assessment.get("evidence", "") or "",
        assessment.get("risk_score", 0))
