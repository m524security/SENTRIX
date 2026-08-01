"""
SENTRIX — PyTorch LSTM Anomaly Detector
---------------------------------------
Detects anomalies in backup-metric time series using a lightweight LSTM
autoencoder. High reconstruction error → anomalous behaviour.

Works in two modes:
  1. PyTorch LSTM  (if torch is installed) — learns what "normal" looks like
     for each asset and flags deviations.
  2. Statistical fallback (scipy/numpy) — Z-score + rolling-window IQR when
     torch is unavailable.

Usage:
    from agent.reasoning.anomaly_detector import score_asset_anomaly, score_fleet

The detector is stateless per call — it trains a tiny model on the asset's
own synthetic history every time (fast: ~50 ms on CPU) so there is nothing
to persist between runs.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Optional

_TORCH_AVAILABLE = False
_NUMPY_AVAILABLE = False
_SCIPY_AVAILABLE = False

try:
    import torch
    import torch.nn as nn
    _TORCH_AVAILABLE = True
except ImportError:
    pass

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    pass

try:
    from scipy import stats as scipy_stats
    _SCIPY_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Synthetic time-series generation
# (mirrors the deterministic tick logic from loghub_engine so the detector
# sees consistent data without needing the engine as a dependency)
# ---------------------------------------------------------------------------

def _det_seed(asset_id: str, salt) -> int:
    h = hashlib.sha256(f"{asset_id}:{salt}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def _synthetic_backup_series(asset: dict, n_ticks: int = 48) -> list[float]:
    """
    Generate a synthetic time series of 'hours_since_last_backup' for the
    last n_ticks world-ticks. Uses the same deterministic logic as the engine
    so values are reproducible for a given asset + tick.
    """
    import random
    rpo = max(float(asset.get("rpo_target_hours", 8) or 8), 0.1)
    err_rate = float(asset.get("baseline_error_rate", 0.1) or 0.1)
    effective_prob = max(0.05, min(0.65, 0.05 + err_rate * 0.55))

    import time as time_mod
    current_tick = int(time_mod.time() // 300)

    series = []
    for k in range(n_ticks - 1, -1, -1):
        tick = current_tick - k
        rng = random.Random(_det_seed(asset.get("asset_id", "x"), tick))
        incident = rng.random() < effective_prob
        if incident:
            hours = rpo * rng.uniform(1.0, 4.0)
        else:
            hours = rpo * rng.uniform(0.1, 0.85)
        series.append(hours)
    return series


# ---------------------------------------------------------------------------
# LSTM Autoencoder (PyTorch)
# ---------------------------------------------------------------------------

if _TORCH_AVAILABLE:
    class _LSTMAutoencoder(nn.Module):
        def __init__(self, input_size: int = 1, hidden_size: int = 16, num_layers: int = 1):
            super().__init__()
            self.encoder = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
            self.decoder = nn.LSTM(hidden_size, hidden_size, num_layers, batch_first=True)
            self.output_layer = nn.Linear(hidden_size, input_size)

        def forward(self, x):
            _, (h, c) = self.encoder(x)
            # Repeat the hidden state as decoder input
            decoder_input = h.permute(1, 0, 2).expand(-1, x.size(1), -1)
            out, _ = self.decoder(decoder_input, (h, c))
            return self.output_layer(out)


def _train_lstm_detector(series: list[float], epochs: int = 50) -> tuple:
    """
    Trains a tiny LSTM autoencoder on the normal portion of the series.
    Returns (model, threshold, mean, std) for anomaly scoring.
    """
    if not _TORCH_AVAILABLE or not _NUMPY_AVAILABLE or len(series) < 8:
        return None, None, None, None

    arr = np.array(series, dtype=np.float32)
    mean, std = arr.mean(), arr.std() + 1e-8
    normalized = (arr - mean) / std

    seq_len = min(8, len(normalized) // 2)
    sequences = []
    for i in range(len(normalized) - seq_len):
        sequences.append(normalized[i:i + seq_len])

    if not sequences:
        return None, None, None, None

    X = torch.tensor(np.array(sequences), dtype=torch.float32).unsqueeze(-1)

    model = _LSTMAutoencoder(input_size=1, hidden_size=16)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    loss_fn = nn.MSELoss()

    model.train()
    for _ in range(epochs):
        optimizer.zero_grad()
        output = model(X)
        loss = loss_fn(output, X)
        loss.backward()
        optimizer.step()

    model.eval()
    with torch.no_grad():
        recon = model(X)
        errors = ((recon - X) ** 2).mean(dim=(1, 2)).numpy()

    threshold = errors.mean() + 2.0 * errors.std()
    return model, float(threshold), float(mean), float(std)


def _score_lstm(model, threshold: float, mean: float, std: float,
                latest_series: list[float], seq_len: int = 8) -> float:
    """Score the most recent window; returns 0–1 anomaly probability."""
    if not _TORCH_AVAILABLE or model is None:
        return 0.0
    if len(latest_series) < seq_len:
        return 0.0
    arr = np.array(latest_series[-seq_len:], dtype=np.float32)
    normalized = (arr - mean) / (std + 1e-8)
    X = torch.tensor(normalized, dtype=torch.float32).unsqueeze(0).unsqueeze(-1)
    with torch.no_grad():
        recon = model(X)
        error = float(((recon - X) ** 2).mean().item())
    raw_score = error / (threshold + 1e-8)
    return round(min(1.0, raw_score), 4)


# ---------------------------------------------------------------------------
# Statistical fallback (scipy / numpy)
# ---------------------------------------------------------------------------

def _score_statistical(series: list[float]) -> float:
    """Z-score + IQR-based anomaly scoring. Returns 0–1."""
    if not _NUMPY_AVAILABLE or len(series) < 4:
        latest = series[-1] if series else 0.0
        baseline = series[0] if series else 1.0
        return min(1.0, latest / max(baseline, 1e-8) - 1.0) if latest > baseline else 0.0

    arr = np.array(series, dtype=np.float64)
    latest = arr[-1]

    # Z-score against historical window
    mean, std = arr[:-1].mean(), arr[:-1].std() + 1e-8
    z = abs((latest - mean) / std)
    z_score = min(1.0, z / 4.0)

    # IQR-based outlier flag
    if _SCIPY_AVAILABLE and len(arr) >= 5:
        q25, q75 = np.percentile(arr[:-1], [25, 75])
        iqr = q75 - q25
        iqr_score = 1.0 if latest > q75 + 1.5 * iqr else (0.5 if latest > q75 + iqr else 0.0)
    else:
        iqr_score = 0.0

    return round(min(1.0, 0.6 * z_score + 0.4 * iqr_score), 4)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_asset_anomaly(asset: dict, n_ticks: int = 48) -> dict:
    """
    Full anomaly analysis for a single asset.
    Returns:
        {
          "asset_id": str,
          "anomaly_score": float 0-1,
          "is_anomalous": bool,
          "method": "lstm" | "statistical" | "fallback",
          "series_length": int,
          "latest_value": float,
          "baseline_mean": float,
        }
    """
    series = _synthetic_backup_series(asset, n_ticks)
    asset_id = asset.get("asset_id", "unknown")

    if _TORCH_AVAILABLE and _NUMPY_AVAILABLE and len(series) >= 16:
        try:
            model, threshold, mean, std = _train_lstm_detector(series[:-4])
            if model is not None:
                score = _score_lstm(model, threshold, mean, std, series)
                return {
                    "asset_id": asset_id,
                    "anomaly_score": score,
                    "is_anomalous": score >= 0.6,
                    "method": "lstm",
                    "series_length": len(series),
                    "latest_value": round(series[-1], 2),
                    "baseline_mean": round(float(np.mean(series[:-4])), 2),
                }
        except Exception:
            pass

    if _NUMPY_AVAILABLE:
        score = _score_statistical(series)
        return {
            "asset_id": asset_id,
            "anomaly_score": score,
            "is_anomalous": score >= 0.6,
            "method": "statistical",
            "series_length": len(series),
            "latest_value": round(series[-1], 2) if series else 0.0,
            "baseline_mean": round(float(np.mean(series[:-1])), 2) if len(series) > 1 else 0.0,
        }

    # Bare fallback
    latest = series[-1] if series else 0.0
    rpo = float(asset.get("rpo_target_hours", 8) or 8)
    score = min(1.0, latest / rpo) if rpo > 0 else 0.0
    return {
        "asset_id": asset_id,
        "anomaly_score": round(score, 4),
        "is_anomalous": score >= 0.75,
        "method": "fallback",
        "series_length": len(series),
        "latest_value": round(latest, 2),
        "baseline_mean": round(rpo, 2),
    }


def score_fleet(assets: list[dict]) -> dict:
    """
    Run anomaly detection across the entire fleet.
    Returns a sorted list of anomalous assets and a fleet-level summary.
    """
    results = []
    for asset in assets:
        r = score_asset_anomaly(asset)
        r["asset_name"] = asset.get("asset_name", r["asset_id"])
        r["tier"] = asset.get("tier", 3)
        r["dataset"] = asset.get("dataset", "core")
        results.append(r)

    results.sort(key=lambda x: x["anomaly_score"], reverse=True)
    anomalous = [r for r in results if r["is_anomalous"]]

    method_used = results[0]["method"] if results else "none"

    return {
        "total_assets": len(results),
        "anomalous_count": len(anomalous),
        "fleet_anomaly_rate": round(len(anomalous) / max(len(results), 1), 4),
        "method": method_used,
        "top_anomalies": results[:15],
        "all_scores": results,
    }


def detector_status() -> dict:
    return {
        "torch_available": _TORCH_AVAILABLE,
        "numpy_available": _NUMPY_AVAILABLE,
        "scipy_available": _SCIPY_AVAILABLE,
        "mode": "lstm" if _TORCH_AVAILABLE and _NUMPY_AVAILABLE else ("statistical" if _NUMPY_AVAILABLE else "fallback"),
    }
