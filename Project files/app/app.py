"""
app.py -- SmartPark AI Flask dashboard
"""
import os
import sys
import json
from datetime import datetime

from flask import Flask, jsonify, render_template, request, redirect, session

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, SRC_DIR)

import predict as pred_module
import history_db

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
from portal import bp as portal_bp, init_db
app.register_blueprint(portal_bp)
init_db()

LOT_ID_MAP_PATH = os.path.join(BASE_DIR, "models", "lot_id_map.json")
RF_METRICS_PATH = os.path.join(BASE_DIR, "models", "rf_metrics.json")


def _load_lots():
    with open(LOT_ID_MAP_PATH) as f:
        return list(json.load(f).keys())


@app.route("/")
def index():
    if "uid" not in session:
        return redirect("/login")
    return render_template("index.html", lots=_load_lots())


@app.route("/analytics")
def analytics():
    return redirect("/")


@app.route("/api/lots")
def api_lots():
    return jsonify(_load_lots())


@app.route("/api/status")
def api_status():
    try:
        return jsonify(pred_module.get_all_lots_current_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/predict")
def api_predict():
    lot = request.args.get("lot")
    if not lot:
        return jsonify({"error": "Missing 'lot' parameter"}), 400
    dt_str = request.args.get("datetime")
    dt = datetime.fromisoformat(dt_str) if dt_str else datetime.now()
    try:
        result = pred_module.predict_availability(lot, dt)
        history_db.log_prediction(result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/forecast")
def api_forecast():
    lot = request.args.get("lot")
    if not lot:
        return jsonify({"error": "Missing 'lot' parameter"}), 400
    try:
        return jsonify(pred_module.get_forecast_for_lot(lot))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/next_hours")
def api_next_hours():
    lot = request.args.get("lot")
    if not lot:
        return jsonify({"error": "Missing 'lot' parameter"}), 400
    hours = int(request.args.get("hours", 24))
    try:
        return jsonify(pred_module.predict_next_hours(lot, hours))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history_occupancy")
def api_history_occupancy():
    lot = request.args.get("lot")
    if not lot:
        return jsonify({"error": "Missing 'lot' parameter"}), 400
    points = int(request.args.get("points", 200))
    try:
        return jsonify(pred_module.get_historical_series(lot, points))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history")
def api_history():
    lot = request.args.get("lot")  # optional
    limit = int(request.args.get("limit", 50))
    try:
        return jsonify(history_db.get_history(lot=lot, limit=limit))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hourly")
def api_hourly():
    lot = request.args.get("lot")
    if not lot:
        return jsonify({"error": "Missing 'lot' parameter"}), 400
    try:
        return jsonify(pred_module.get_hourly_pattern(lot))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/weekly")
def api_weekly():
    lot = request.args.get("lot")
    if not lot:
        return jsonify({"error": "Missing 'lot' parameter"}), 400
    try:
        return jsonify(pred_module.get_weekly_pattern(lot))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rf_metrics")
def api_rf_metrics():
    if os.path.exists(RF_METRICS_PATH):
        with open(RF_METRICS_PATH) as f:
            return jsonify(json.load(f))
    return jsonify({"error": "Metrics file not found"}), 404


if __name__ == "__main__":
    print("[APP] Starting SmartPark AI Dashboard on http://localhost:5000/login")
    app.run(debug=False, host="0.0.0.0", port=5000)
