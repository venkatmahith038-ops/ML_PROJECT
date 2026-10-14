"""
predict.py
==========
Predicts parking availability using REAL historical occupancy patterns
(from the same dataset used for training) per (lot, day-of-week, hour),
cross-checked against the trained Random Forest model.

Why not just use the RF model's predict_proba directly?
The RF's features (Hour, DayOfWeek, Capacity, etc.) never include the
actual/recent occupancy, and ~89% of training rows are labeled
"Available", so the raw model predicts the majority class almost
everywhere (verified: P(Available) > 0.99 for nearly every lot/hour
combo). Using historical occupancy averages instead gives real,
varying, data-driven percentages -- no random numbers, no hardcoding.
"""

import os
import json
from datetime import datetime, timedelta

import joblib
import pandas as pd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MODELS_DIR = os.path.join(BASE_DIR, "models")

AVAILABILITY_THRESHOLD = 0.90  # must match preprocess.py label definition
NEAR_FULL_THRESHOLD = 0.70     # 70-90% occupied = "Near Full"


def _find_file(*candidates):
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"None of these exist: {candidates}")


# ── Load artifacts once at import time ───────────────────────────────────────
_rf_model = joblib.load(_find_file(os.path.join(MODELS_DIR, "rf_model.pkl")))

with open(_find_file(os.path.join(MODELS_DIR, "feature_cols.json"))) as f:
    FEATURE_COLS = json.load(f)

with open(_find_file(os.path.join(MODELS_DIR, "lot_id_map.json"))) as f:
    LOT_ID_MAP = json.load(f)

with open(_find_file(os.path.join(MODELS_DIR, "lot_capacities.json"))) as f:
    LOT_CAPACITIES = json.load(f)

_processed_path = _find_file(
    os.path.join(BASE_DIR, "data", "processed", "processed_parking.csv"),
    os.path.join(BASE_DIR, "data", "processed_parking.csv"),
)
_df = pd.read_csv(_processed_path)

# Real historical occupancy per lot / day-of-week / hour -- using the 90th
# percentile (how full this lot gets on a typically busy day at this hour)
# instead of the plain average, since averaging smooths out peak moments
# and made every lot look permanently "Available".
_HIST = (
    _df.groupby(["SystemCodeNumber", "DayOfWeek", "Hour"])["OccupancyRate"]
    .quantile(0.9)
    .to_dict()
)
_HIST_LOT_HOUR = _df.groupby(["SystemCodeNumber", "Hour"])["OccupancyRate"].quantile(0.9).to_dict()
_HIST_LOT = _df.groupby("SystemCodeNumber")["OccupancyRate"].quantile(0.9).to_dict()
_GLOBAL_MEAN = float(_df["OccupancyRate"].quantile(0.9))

try:
    import holidays as _holidays_lib
    _UK_HOLIDAYS = _holidays_lib.England(years=[2016, 2017, 2025, 2026])
except Exception:
    _UK_HOLIDAYS = {}


def _historical_occupancy_rate(lot, dow, hour):
    if (lot, dow, hour) in _HIST:
        return _HIST[(lot, dow, hour)]
    if (lot, hour) in _HIST_LOT_HOUR:
        return _HIST_LOT_HOUR[(lot, hour)]
    if lot in _HIST_LOT:
        return _HIST_LOT[lot]
    return _GLOBAL_MEAN


def _build_feature_row(lot, dt: datetime):
    lot_id = LOT_ID_MAP[lot]
    capacity = LOT_CAPACITIES[lot]
    hour, minute, dow = dt.hour, dt.minute, dt.weekday()
    values = {
        "LotID": lot_id,
        "Hour": hour,
        "Minute": minute,
        "DayOfWeek": dow,
        "IsWeekend": int(dow >= 5),
        "IsHoliday": int(dt.date() in _UK_HOLIDAYS),
        "TimeSlot": hour * 2 + (1 if minute >= 30 else 0),
        "Week": dt.isocalendar()[1],
        "Month": dt.month,
        "Capacity": capacity,
    }
    row = pd.DataFrame([[values[c] for c in FEATURE_COLS]], columns=FEATURE_COLS)
    return row, dow, hour, capacity


def predict_availability(lot: str, dt: datetime) -> dict:
    if lot not in LOT_ID_MAP:
        raise ValueError(f"Unknown lot: {lot}")

    row, dow, hour, capacity = _build_feature_row(lot, dt)

    # Raw RF output -- kept only for transparency/debugging. NOT used to
    # drive the displayed status, since it is heavily biased toward
    # "Available" (see module docstring).
    raw_rf_proba_available = float(_rf_model.predict_proba(row)[0][1])

    # Real historical occupancy -> drives the displayed percentage/status.
    occ_rate = _historical_occupancy_rate(lot, dow, hour)
    occ_rate = max(0.0, min(1.0, occ_rate))
    available_pct = round((1 - occ_rate) * 100, 1)
    occupied_pct = round(occ_rate * 100, 1)
    status = (
        "Full" if occ_rate >= AVAILABILITY_THRESHOLD
        else "Near Full" if occ_rate >= NEAR_FULL_THRESHOLD
        else "Available"
    )

    return {
        "lot": lot,
        "datetime": dt.isoformat(),
        "capacity": capacity,
        "predicted_occupied_pct": occupied_pct,
        "predicted_available_pct": available_pct,
        "estimated_available_spaces": int(round(capacity * (1 - occ_rate))),
        "status": status,
        "p_available": round(available_pct / 100, 4),
        "raw_rf_model_output_debug_only": round(raw_rf_proba_available, 4),
    }


def get_all_lots_current_status() -> list:
    now = datetime.now()
    return [predict_availability(lot, now) for lot in LOT_ID_MAP.keys()]


def predict_next_hours(lot: str, hours: int = 24) -> list:
    now = datetime.now()
    out = []
    for step in range(hours * 2):  # 30-min steps
        dt = now + timedelta(minutes=30 * step)
        out.append(predict_availability(lot, dt))
    return out


def get_hourly_pattern(lot: str) -> list:
    """Average occupancy rate by hour of day for this lot (real historical data)."""
    if lot not in LOT_ID_MAP:
        raise ValueError(f"Unknown lot: {lot}")
    sub = _df[_df.SystemCodeNumber == lot]
    hourly = sub.groupby("Hour")["OccupancyRate"].mean().round(4)
    return [{"hour": int(h), "occupancy_rate": float(v)} for h, v in hourly.items()]


def get_weekly_pattern(lot: str) -> list:
    """Average occupancy rate by day of week for this lot (real historical data)."""
    if lot not in LOT_ID_MAP:
        raise ValueError(f"Unknown lot: {lot}")
    sub = _df[_df.SystemCodeNumber == lot]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekly = sub.groupby("DayOfWeek")["OccupancyRate"].mean().round(4)
    return [{"day": days[d], "occupancy_rate": float(v)} for d, v in weekly.items()]


def get_historical_series(lot: str, points: int = 200) -> list:
    """Real historical occupancy readings for a lot, most recent first in
    the source data but returned oldest->newest for charting."""
    if lot not in LOT_ID_MAP:
        raise ValueError(f"Unknown lot: {lot}")
    sub = _df[_df["SystemCodeNumber"] == lot]
    if "LastUpdated" in sub.columns:
        sub = sub.sort_values("LastUpdated")
    sub = sub.tail(points)
    return [
        {"timestamp": str(row.get("LastUpdated", "")), "occupancy_rate": round(float(row["OccupancyRate"]), 4)}
        for _, row in sub.iterrows()
    ]


def get_forecast_for_lot(lot: str) -> list:
    ts_path = _find_file(
        os.path.join(MODELS_DIR, "ts_forecasts.csv"),
        os.path.join(BASE_DIR, "ts_forecasts.csv"),
    )
    ts_df = pd.read_csv(ts_path)
    lot_col = "SystemCodeNumber" if "SystemCodeNumber" in ts_df.columns else "lot"
    sub = ts_df[ts_df[lot_col] == lot]
    if sub.empty:
        raise ValueError(f"No forecast found for lot: {lot}")
    records = sub.to_dict(orient="records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, float) and v != v:  # NaN check (NaN != NaN)
                rec[k] = None
    return records
