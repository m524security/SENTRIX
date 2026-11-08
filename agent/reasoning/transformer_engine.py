"""
SENTRIX — HuggingFace Transformer Engine
-----------------------------------------
Provides AI-powered log analysis using pre-trained transformer models.

Models used (auto-downloaded from HuggingFace Hub on first use):
  - distilbert-base-uncased-finetuned-sst-2-english  → sentiment/anomaly polarity
  - facebook/bart-large-mnli                          → zero-shot log severity classification
  - sentence-transformers/all-MiniLM-L6-v2           → semantic similarity & clustering

All imports are lazy — the module loads without torch/transformers installed.
When models aren't available, every function returns a graceful fallback dict
so the agent never breaks because of a missing ML package.
"""

from __future__ import annotations

import hashlib
import re
import time
from functools import lru_cache
from typing import Optional

_TORCH_AVAILABLE = False
_TRANSFORMERS_AVAILABLE = False
_SENTENCE_TRANSFORMERS_AVAILABLE = False

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    pass

try:
    from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    pass

try:
    from sentence_transformers import SentenceTransformer, util as st_util
    _SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    pass

# ---- model singletons (lazy-loaded) ----------------------------------------

_sentiment_pipe = None
_zero_shot_pipe = None
_embed_model: Optional[object] = None
_model_load_errors: dict = {}
_model_ready: dict = {}


def _load_sentiment():
    global _sentiment_pipe
    if _sentiment_pipe is not None:
        return _sentiment_pipe
    if not _TRANSFORMERS_AVAILABLE:
        raise RuntimeError("transformers not installed")
    try:
        _sentiment_pipe = pipeline(
            "text-classification",
            model="distilbert-base-uncased-finetuned-sst-2-english",
            truncation=True,
            max_length=128,
        )
        _model_ready["sentiment"] = True
        return _sentiment_pipe
    except Exception as e:
        _model_load_errors["sentiment"] = str(e)
        raise


def _load_zero_shot():
    global _zero_shot_pipe
    if _zero_shot_pipe is not None:
        return _zero_shot_pipe
    if not _TRANSFORMERS_AVAILABLE:
        raise RuntimeError("transformers not installed")
    try:
        _zero_shot_pipe = pipeline(
            "zero-shot-classification",
            model="facebook/bart-large-mnli",
            device=0 if (_TORCH_AVAILABLE and torch.cuda.is_available()) else -1,
        )
        _model_ready["zero_shot"] = True
        return _zero_shot_pipe
    except Exception as e:
        _model_load_errors["zero_shot"] = str(e)
        raise


def _load_embedder():
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    if not _SENTENCE_TRANSFORMERS_AVAILABLE:
        raise RuntimeError("sentence-transformers not installed")
    try:
        _embed_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        _model_ready["embedder"] = True
        return _embed_model
    except Exception as e:
        _model_load_errors["embedder"] = str(e)
        raise


# ---- public API ------------------------------------------------------------

SEVERITY_LABELS = ["critical failure", "warning", "normal operation", "informational"]

ERROR_KEYWORDS = re.compile(
    r"fail|error|exception|crash|timeout|refused|invalid|denied|lost|reset|abort|down|dead",
    re.IGNORECASE,
)


def _keyword_severity(text: str) -> float:
    """Fast keyword-based severity score — 0.0 (fine) to 1.0 (critical)."""
    if not text:
        return 0.0
    words = ERROR_KEYWORDS.findall(text)
    return min(1.0, len(words) * 0.25)


def _stable_hash_float(text: str) -> float:
    """Deterministic float 0-1 from text, for reproducible fallback scores."""
    h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0


def classify_log_severity(log_line: str) -> dict:
    """
    Zero-shot classification of a log line into SEVERITY_LABELS.
    Returns:
        {
          "label":  "critical failure" | "warning" | "normal operation" | "informational",
          "score":  float 0-1,
          "method": "zero_shot" | "keyword_fallback",
          "scores": {label: score, ...}
        }
    """
    if not log_line or not log_line.strip():
        return {"label": "informational", "score": 0.1, "method": "empty", "scores": {}}

    try:
        pipe = _load_zero_shot()
        truncated = log_line[:512]
        result = pipe(truncated, candidate_labels=SEVERITY_LABELS, multi_label=False)
        return {
            "label": result["labels"][0],
            "score": round(result["scores"][0], 4),
            "method": "zero_shot",
            "scores": dict(zip(result["labels"], [round(s, 4) for s in result["scores"]])),
        }
    except Exception:
        pass

    # Fallback: keyword scoring
    kw_score = _keyword_severity(log_line)
    if kw_score >= 0.75:
        label = "critical failure"
    elif kw_score >= 0.25:
        label = "warning"
    else:
        label = "normal operation"
    return {
        "label": label,
        "score": round(kw_score, 4),
        "method": "keyword_fallback",
        "scores": {"critical failure": kw_score, "warning": max(0, kw_score - 0.2)},
    }


def anomaly_score_from_text(log_line: str) -> float:
    """
    Combines transformer sentiment + keyword analysis to produce a single
    anomaly score in [0, 1]. High = likely anomalous.
    """
    if not log_line:
        return 0.0

    keyword_s = _keyword_severity(log_line)

    try:
        pipe = _load_sentiment()
        result = pipe(log_line[:512])[0]
        # NEGATIVE label → high anomaly probability
        sentiment_s = result["score"] if result["label"] == "NEGATIVE" else 1.0 - result["score"]
        # Weighted blend: 60% transformer, 40% keyword
        return round(0.6 * sentiment_s + 0.4 * keyword_s, 4)
    except Exception:
        return round(keyword_s, 4)


def embed_log_lines(log_lines: list[str]) -> list:
    """
    Returns sentence embeddings for a list of log lines.
    Falls back to empty list if sentence-transformers not available.
    """
    if not log_lines:
        return []
    try:
        model = _load_embedder()
        embeddings = model.encode(log_lines, convert_to_tensor=_TORCH_AVAILABLE, show_progress_bar=False)
        if _TORCH_AVAILABLE:
            return embeddings.cpu().tolist()
        return embeddings.tolist()
    except Exception:
        return []


def find_similar_incidents(query_line: str, evidence_pool: list[str], top_k: int = 3) -> list[dict]:
    """
    Finds the most semantically similar past incidents to a current log line.
    Used to surface 'similar failures we've seen before'.
    """
    if not query_line or not evidence_pool:
        return []

    try:
        model = _load_embedder()
        q_emb = model.encode(query_line, convert_to_tensor=True)
        p_embs = model.encode(evidence_pool, convert_to_tensor=True)
        if _TORCH_AVAILABLE:
            cos_scores = st_util.cos_sim(q_emb, p_embs)[0]
            top_indices = torch.topk(cos_scores, k=min(top_k, len(evidence_pool))).indices
            results = []
            for idx in top_indices:
                results.append({
                    "line": evidence_pool[int(idx)],
                    "similarity": round(float(cos_scores[int(idx)]), 4),
                })
            return results
    except Exception:
        pass

    # Fallback: keyword overlap scoring
    query_words = set(re.findall(r"\w+", query_line.lower()))
    scored = []
    for line in evidence_pool:
        line_words = set(re.findall(r"\w+", line.lower()))
        overlap = len(query_words & line_words) / max(len(query_words | line_words), 1)
        scored.append({"line": line, "similarity": round(overlap, 4)})
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]


def analyze_fleet_logs(assets: list[dict]) -> dict:
    """
    Top-level function called by the reasoning core.
    Analyzes log evidence across the entire fleet and returns:
        {
          "analyzed": int,
          "critical_signals": [{"asset_id", "evidence", "anomaly_score", "severity_label"}],
          "fleet_anomaly_score": float,    # average across fleet
          "method": str,
        }
    """
    results = []
    scores = []

    for asset in assets:
        evidence = asset.get("evidence")
        if not evidence:
            continue

        a_score = anomaly_score_from_text(evidence)
        severity = classify_log_severity(evidence)
        scores.append(a_score)

        if a_score >= 0.35 or severity["label"] in ("critical failure", "warning"):
            results.append({
                "asset_id": asset.get("asset_id"),
                "asset_name": asset.get("asset_name"),
                "tier": asset.get("tier", 3),
                "evidence": evidence[:200],
                "anomaly_score": a_score,
                "severity_label": severity["label"],
                "severity_score": severity["score"],
                "method": severity["method"],
            })

    results.sort(key=lambda x: x["anomaly_score"], reverse=True)
    fleet_score = round(sum(scores) / len(scores), 4) if scores else 0.0
    method = "zero_shot+sentiment" if _model_ready.get("sentiment") or _model_ready.get("zero_shot") else "keyword_fallback"

    return {
        "analyzed": len(scores),
        "critical_signals": results[:20],
        "fleet_anomaly_score": fleet_score,
        "method": method,
    }


def ml_status() -> dict:
    """Returns current model loading state — surfaced via /api/ml-status."""
    return {
        "torch_available": _TORCH_AVAILABLE,
        "transformers_available": _TRANSFORMERS_AVAILABLE,
        "sentence_transformers_available": _SENTENCE_TRANSFORMERS_AVAILABLE,
        "cuda_available": bool(_TORCH_AVAILABLE and torch.cuda.is_available()) if _TORCH_AVAILABLE else False,
        "models_ready": dict(_model_ready),
        "load_errors": dict(_model_load_errors),
    }
