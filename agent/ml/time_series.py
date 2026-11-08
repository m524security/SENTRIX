"""
SENTRIX — ML Time-Series Forecasting Module
--------------------------------------------
Provides enhanced RPO-breach predictions using deep learning and classical
time-series methods.

Model hierarchy (first available wins):
  1. PyTorch Transformer (self-attention over backup history windows)
  2. PyTorch LSTM (from anomaly_detector module)
  3. statsmodels exponential smoothing
  4. scipy linear regression
  5. Pure-Python linear extrapolation (always available)

The module enhances the existing predictive_engine.py — it runs in parallel
and its forecasts are merged into the /api/predictions endpoint response.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Optional

_TORCH_AVAILABLE = False
_NUMPY_AVAILABLE = False
_STATSMODELS_AVAILABLE = False
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
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    _STATSMODELS_AVAILABLE = True
except ImportError:
    pass

try:
    from scipy import stats as scipy_stats
    _SCIPY_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Transformer model for sequence prediction (PyTorch)
# ---------------------------------------------------------------------------

if _TORCH_AVAILABLE:
    class _TimeSeriesTransformer(nn.Module):
        """
        Tiny Transformer encoder → linear head for next-step prediction.
        Input:  (batch, seq_len, 1)
        Output: (batch, 1)         — next value prediction
        """
        def __init__(self, d_model: int = 16, nhead: int = 2, num_layers: int = 1, seq_len: int = 8):
            super().__init__()
            self.input_proj = nn.Linear(1, d_model)
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=d_model, nhead=nhead, dim_feedforward=32,
                dropout=0.0, batch_first=True,
            )
            self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
            self.head = nn.Linear(d_model * seq_len, 1)
            self.seq_len = seq_len

        def forward(self, x):
            x = self.input_proj(x)
            x = self.encoder(x)
            x = x.reshape(x.size(0), -1)
            return self.head(x)


def _train_transformer_forecaster(series: list[float], seq_len: int = 8, epochs: int = 60):
    """
    Trains the Transformer on the series and returns (model, mean, std).
    Returns (None, None, None) on failure.
    """
    if not _TORCH_AVAILABLE or not _NUMPY_AVAILABLE or len(series) < seq_len + 2:
        return None, None, None

    arr = np.array(series, dtype=np.float32)
    mean, std = arr.mean(), arr.std() + 1e-8
    norm = (arr - mean) / std

    X_list, y_list = [], []
    for i in range(len(norm) - seq_len):
        X_list.append(norm[i:i + seq_len])
        y_list.append(norm[i + seq_len])

    if not X_list:
        return None, None, None

    X = torch.tensor(np.array(X_list), dtype=torch.float32).unsqueeze(-1)
    y = torch.tensor(np.array(y_list), dtype=torch.float32).unsqueeze(-1)

    model = _TimeSeriesTransformer(d_model=16, nhead=2, num_layers=1, seq_len=seq_len)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.005)
    loss_fn = nn.MSELoss()

    model.train()
    for _ in range(epochs):
        optimizer.zero_grad()
        pred = model(X)
        loss = loss_fn(pred, y)
        loss.backward()
        optimizer.step()

    return model, float(mean), float(std)


def _forecast_transformer(model, mean: float, std: float,
                           series: list[float], horizon: int = 3, seq_len: int = 8) -> list[float]:
    """Roll the transformer forward `horizon` steps."""
    if not _TORCH_AVAILABLE or model is None or len(series) < seq_len:
        return []
    arr = np.array(series[-seq_len:], dtype=np.float32)
    norm = (arr - mean) / (std + 1e-8)
    preds = []
    window = list(norm)
    model.eval()
    with torch.no_grad():
        for _ in range(horizon):
            x = torch.tensor(window[-seq_len:], dtype=torch.float32).unsqueeze(0).unsqueeze(-1)
            p = float(model(x).item())
            window.append(p)
            preds.append(round(float(p * std + mean), 2))
    return preds


# ---------------------------------------------------------------------------
# Exponential smoothing (statsmodels)
# ---------------------------------------------------------------------------

def _forecast_exp_smoothing(series: list[float], horizon: int = 3) -> list[float]:
    if not _STATSMODELS_AVAILABLE or not _NUMPY_AVAILABLE or len(series) < 6:
        return []
    try:
        model = ExponentialSmoothing(
            np.array(series, dtype=np.float64),
            trend="add", seasonal=None, initialization_method="estimated",
        ).fit(optimized=True, disp=False)
        return [round(float(v), 2) for v in model.forecast(horizon)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Linear regression fallback (scipy)
# ---------------------------------------------------------------------------

def _forecast_linear(series: list[float], horizon: int = 3) -> list[float]:
    if not series:
        return []
    n = len(series)
    if _SCIPY_AVAILABLE and _NUMPY_AVAILABLE and n >= 3:
        x = np.arange(n, dtype=np.float64)
        slope, intercept, *_ = scipy_stats.linregress(x, np.array(series, dtype=np.float64))
        return [round(float(slope * (n + i) + intercept), 2) for i in range(horizon)]
    # Pure-Python linear extrapolation
    if n >= 2:
        slope = (series[-1] - series[0]) / max(n - 1, 1)
        return [round(series[-1] + slope * (i + 1), 2) for i in range(horizon)]
    return [round(series[-1], 2)] * horizon


# ---------------------------------------------------------------------------
# Synthetic series generator (mirrors anomaly_detector logic)
# ---------------------------------------------------------------------------

def _synthetic_series(asset: dict, n_ticks: int = 32) -> list[float]:
    import random
    rpo = max(float(asset.get("rpo_target_hours", 8) or 8), 0.1)
    err_rate = float(asset.get("baseline_error_rate", 0.1) or 0.1)
    effective_prob = max(0.05, min(0.65, 0.05 + err_rate * 0.55))

    current_tick = int(time.time() // 300)
    series = []
    for k in range(n_ticks - 1, -1, -1):
        tick = current_tick - k
        h = hashlib.sha256(f"{asset.get('asset_id','x')}:{tick}".encode()).digest()
        seed = int.from_bytes(h[:8], "big")
        rng = random.Random(seed)
        incident = rng.random() < effective_prob
        hours = rpo * rng.uniform(1.0, 4.0) if incident else rpo * rng.uniform(0.1, 0.85)
        series.append(hours)
    return series


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def forecast_asset_rpo(asset: dict, horizon: int = 6) -> dict:
    """
    Forecast next `horizon` ticks of hours_since_last_backup and
    determine whether an RPO breach is predicted.

    Returns:
        {
          "asset_id": str,
          "rpo_target_hours": float,
          "forecast": [float, ...],          # predicted hours values
          "breach_predicted": bool,
          "breach_at_step": int | None,       # 1-indexed step where breach occurs
          "method": str,
          "confidence": float,
        }
    """
    series = _synthetic_series(asset, n_ticks=32)
    rpo = float(asset.get("rpo_target_hours", 8) or 8)
    asset_id = asset.get("asset_id", "unknown")

    forecast = []
    method = "linear"

    # Try Transformer
    if _TORCH_AVAILABLE and _NUMPY_AVAILABLE and len(series) >= 10:
        try:
            model, mean, std = _train_transformer_forecaster(series[:-horizon], seq_len=8, epochs=60)
            if model is not None:
                forecast = _forecast_transformer(model, mean, std, series[:-horizon], horizon=horizon)
                method = "transformer"
        except Exception:
            pass

    # Try Exponential Smoothing
    if not forecast and _STATSMODELS_AVAILABLE:
        forecast = _forecast_exp_smoothing(series, horizon=horizon)
        if forecast:
            method = "exp_smoothing"

    # Linear fallback
    if not forecast:
        forecast = _forecast_linear(series, horizon=horizon)
        method = "linear"

    # Determine breach
    breach_at = None
    for i, val in enumerate(forecast):
        if val >= rpo:
            breach_at = i + 1
            break

    breach_predicted = breach_at is not None
    confidence = 0.85 if method == "transformer" else (0.70 if method == "exp_smoothing" else 0.50)

    return {
        "asset_id": asset_id,
        "asset_name": asset.get("asset_name", asset_id),
        "tier": asset.get("tier", 3),
        "dataset": asset.get("dataset", "core"),
        "rpo_target_hours": rpo,
        "series_length": len(series),
        "forecast": forecast,
        "breach_predicted": breach_predicted,
        "breach_at_step": breach_at,
        "method": method,
        "confidence": confidence,
        "current_hours": round(series[-1] if series else 0.0, 2),
        "trend": _trend_label(series),
    }


def _trend_label(series: list[float]) -> str:
    if len(series) < 4:
        return "unknown"
    recent = series[-4:]
    if recent[-1] > recent[0] * 1.2:
        return "deteriorating"
    if recent[-1] < recent[0] * 0.8:
        return "improving"
    return "stable"


def forecast_fleet(assets: list[dict], horizon: int = 6) -> dict:
    """Forecast RPO breach risk across the entire fleet."""
    forecasts = []
    for asset in assets:
        try:
            f = forecast_asset_rpo(asset, horizon=horizon)
            forecasts.append(f)
        except Exception:
            pass

    at_risk = [f for f in forecasts if f["breach_predicted"]]
    at_risk.sort(key=lambda x: (x.get("breach_at_step") or 99, -x.get("confidence", 0)))

    methods = {}
    for f in forecasts:
        m = f["method"]
        methods[m] = methods.get(m, 0) + 1

    return {
        "total_assets": len(forecasts),
        "breach_predicted_count": len(at_risk),
        "horizon_steps": horizon,
        "method_distribution": methods,
        "at_risk": at_risk[:20],
        "all_forecasts": forecasts,
    }


def forecaster_status() -> dict:
    return {
        "torch_available": _TORCH_AVAILABLE,
        "numpy_available": _NUMPY_AVAILABLE,
        "statsmodels_available": _STATSMODELS_AVAILABLE,
        "scipy_available": _SCIPY_AVAILABLE,
        "best_method": (
            "transformer" if _TORCH_AVAILABLE and _NUMPY_AVAILABLE else
            ("exp_smoothing" if _STATSMODELS_AVAILABLE else
             ("linear_scipy" if _SCIPY_AVAILABLE else "linear_python"))
        ),
    }
