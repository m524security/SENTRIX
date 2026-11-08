"""
SENTRIX — HuggingFace Dataset Loader
--------------------------------------
Loads open-source IT operations / log-anomaly datasets from HuggingFace Hub
and converts them into SENTRIX-compatible asset state records.

Datasets targeted (all free, no auth required for public datasets):
  - "logpai/loghub"           — real system logs with anomaly labels
  - "Naereen/kaggle-log"      — web server access logs
  - any custom dataset path

Falls back gracefully if 'datasets' package is not installed or network
is unavailable — returns empty list with a status message.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

_DATASETS_AVAILABLE = False
try:
    import datasets as hf_datasets
    _DATASETS_AVAILABLE = True
except ImportError:
    pass

_NUMPY_AVAILABLE = False
try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    pass

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "hf_cache"

# ---------------------------------------------------------------------------
# Known dataset adapters
# ---------------------------------------------------------------------------

_KNOWN_DATASETS = {
    "loghub_hdfs": {
        "hf_path": "logpai/loghub",
        "hf_name": "HDFS",
        "label_col": "Label",
        "content_col": "Content",
        "description": "HDFS distributed system logs with anomaly labels",
    },
    "log_anomaly_detection": {
        "hf_path": "hkust-nlp/deita-6k-v0",    # placeholder — real datasets vary
        "hf_name": None,
        "label_col": None,
        "content_col": "conversations",
        "description": "General log anomaly detection dataset",
    },
}

_CACHE: dict = {}


def _det_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _label_to_bool(label) -> bool:
    if label is None:
        return False
    s = str(label).strip().lower()
    return s in ("anomaly", "1", "true", "error", "fail", "failure")


def _content_to_anomaly_score(content: str) -> float:
    """Quick keyword-based anomaly score when no label column."""
    keywords = re.findall(
        r"fail|error|exception|crash|timeout|refused|invalid|denied|lost|reset|abort",
        content or "", re.IGNORECASE,
    )
    return min(1.0, len(keywords) * 0.2)


def load_hf_dataset(dataset_key: str = "loghub_hdfs", max_samples: int = 200) -> dict:
    """
    Load a HuggingFace dataset and return a structured result.
    Returns:
        {
          "ok": bool,
          "dataset_key": str,
          "description": str,
          "samples": [{"content", "is_anomaly", "anomaly_score"}, ...],
          "total_loaded": int,
          "anomaly_rate": float,
          "error": str | None,
        }
    """
    cache_key = f"{dataset_key}:{max_samples}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    meta = _KNOWN_DATASETS.get(dataset_key)
    if not meta:
        result = {
            "ok": False, "dataset_key": dataset_key,
            "description": "Unknown dataset key",
            "samples": [], "total_loaded": 0, "anomaly_rate": 0.0,
            "error": f"Unknown key '{dataset_key}'. Known: {list(_KNOWN_DATASETS)}",
        }
        _CACHE[cache_key] = result
        return result

    if not _DATASETS_AVAILABLE:
        result = {
            "ok": False, "dataset_key": dataset_key,
            "description": meta["description"],
            "samples": [], "total_loaded": 0, "anomaly_rate": 0.0,
            "error": "HuggingFace 'datasets' package not installed. Run: pip install datasets",
        }
        _CACHE[cache_key] = result
        return result

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        load_kwargs = {"path": meta["hf_path"], "cache_dir": str(CACHE_DIR)}
        if meta.get("hf_name"):
            load_kwargs["name"] = meta["hf_name"]

        ds = hf_datasets.load_dataset(**load_kwargs, split="train", trust_remote_code=False)

        samples = []
        anomaly_count = 0

        for i, row in enumerate(ds):
            if i >= max_samples:
                break

            content_col = meta.get("content_col", "Content")
            content = str(row.get(content_col, row.get("text", row.get("message", ""))))

            label_col = meta.get("label_col")
            if label_col and label_col in row:
                is_anomaly = _label_to_bool(row[label_col])
                score = 1.0 if is_anomaly else _content_to_anomaly_score(content)
            else:
                score = _content_to_anomaly_score(content)
                is_anomaly = score >= 0.4

            if is_anomaly:
                anomaly_count += 1

            samples.append({
                "content": content[:256],
                "is_anomaly": is_anomaly,
                "anomaly_score": round(score, 4),
            })

        anomaly_rate = anomaly_count / max(len(samples), 1)
        result = {
            "ok": True,
            "dataset_key": dataset_key,
            "description": meta["description"],
            "samples": samples,
            "total_loaded": len(samples),
            "anomaly_rate": round(anomaly_rate, 4),
            "error": None,
        }
        _CACHE[cache_key] = result
        return result

    except Exception as e:
        result = {
            "ok": False,
            "dataset_key": dataset_key,
            "description": meta["description"],
            "samples": [],
            "total_loaded": 0,
            "anomaly_rate": 0.0,
            "error": str(e),
        }
        _CACHE[cache_key] = result
        return result


def load_local_loghub_as_hf(dataset_key: str = "hdfs", max_samples: int = 500) -> dict:
    """
    Loads the already-bundled LogHub CSV files (in /loghub/) and presents
    them in the same format as load_hf_dataset() — so the rest of the system
    has one unified interface regardless of source.
    """
    import csv
    loghub_root = Path(__file__).resolve().parent.parent.parent / "loghub"
    folder_map = {
        "hdfs": ("HDFS", "HDFS_2k.log_structured.csv"),
        "apache": ("Apache", "Apache_2k.log_structured.csv"),
        "windows": ("Windows", "Windows_2k.log_structured.csv"),
        "linux": ("Linux", "Linux_2k.log_structured.csv"),
        "openssh": ("OpenSSH", "OpenSSH_2k.log_structured.csv"),
        "spark": ("Spark", "Spark_2k.log_structured.csv"),
        "hadoop": ("Hadoop", "Hadoop_2k.log_structured.csv"),
    }
    if dataset_key not in folder_map:
        return {
            "ok": False, "dataset_key": dataset_key,
            "description": "Unknown local dataset",
            "samples": [], "total_loaded": 0, "anomaly_rate": 0.0,
            "error": f"Key '{dataset_key}' not in {list(folder_map)}",
        }

    folder, filename = folder_map[dataset_key]
    csv_path = loghub_root / folder / filename

    if not csv_path.exists():
        # Try alternate filename pattern
        alt = loghub_root / folder / f"{folder}_2k.log_structured.csv"
        if alt.exists():
            csv_path = alt
        else:
            return {
                "ok": False, "dataset_key": dataset_key,
                "description": f"LogHub {folder} dataset",
                "samples": [], "total_loaded": 0, "anomaly_rate": 0.0,
                "error": f"File not found: {csv_path}",
            }

    samples = []
    anomaly_count = 0

    try:
        with open(csv_path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= max_samples:
                    break
                content = (row.get("Content") or "").strip()
                level = (row.get("Level") or "").strip().upper()
                label = (row.get("Label") or "").strip()

                if label and label not in ("-", ""):
                    is_anomaly = _label_to_bool(label)
                elif level:
                    is_anomaly = level in ("ERROR", "FATAL", "WARN", "WARNING")
                else:
                    is_anomaly = bool(re.search(r"fail|error|crash|timeout", content, re.IGNORECASE))

                score = 1.0 if is_anomaly else _content_to_anomaly_score(content)
                if is_anomaly:
                    anomaly_count += 1

                samples.append({
                    "content": content[:256],
                    "is_anomaly": is_anomaly,
                    "anomaly_score": round(score, 4),
                    "level": level,
                })

        return {
            "ok": True,
            "dataset_key": dataset_key,
            "description": f"LogHub {folder} — {len(samples)} lines, "
                           f"{round(anomaly_count / max(len(samples), 1) * 100, 1)}% anomalous",
            "samples": samples,
            "total_loaded": len(samples),
            "anomaly_rate": round(anomaly_count / max(len(samples), 1), 4),
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False, "dataset_key": dataset_key,
            "description": f"LogHub {folder}",
            "samples": [], "total_loaded": 0, "anomaly_rate": 0.0,
            "error": str(e),
        }


def available_datasets() -> dict:
    """Summary of all available datasets (HF + local)."""
    loghub_root = Path(__file__).resolve().parent.parent.parent / "loghub"
    local_keys = ["hdfs", "apache", "windows", "linux", "openssh", "spark", "hadoop"]
    local_available = [k for k in local_keys if (loghub_root / k.upper()).exists()
                       or any((loghub_root / k.capitalize()).glob("*.csv"))]

    return {
        "hf_datasets_package": _DATASETS_AVAILABLE,
        "hf_dataset_keys": list(_KNOWN_DATASETS.keys()),
        "local_loghub_keys": local_available,
        "total": len(_KNOWN_DATASETS) + len(local_available),
    }


def loader_status() -> dict:
    return {
        "hf_datasets_available": _DATASETS_AVAILABLE,
        "numpy_available": _NUMPY_AVAILABLE,
        "cache_entries": len(_CACHE),
        "available": available_datasets(),
    }
