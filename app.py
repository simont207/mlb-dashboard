#!/usr/bin/env python3
"""
MLB Picks Dashboard — Flask + Supabase backend
================================================
Supabase table (create this manually in Supabase SQL editor):

    CREATE TABLE picks (
      id           BIGSERIAL PRIMARY KEY,
      date         DATE,
      away_team    TEXT,
      home_team    TEXT,
      away_pitcher TEXT,
      home_pitcher TEXT,
      pick         TEXT,
      margin       FLOAT,
      pick_odds    TEXT,
      type         TEXT,
      result       TEXT DEFAULT '',
      created_at   TIMESTAMPTZ DEFAULT NOW()
    );

Environment variables required:
    SUPABASE_URL  — your Supabase project URL
    SUPABASE_KEY  — your Supabase anon/service key
    API_SECRET    — shared secret the bot sends in X-API-Secret header
"""

import os
from datetime import datetime, date, timedelta
from functools import wraps

from flask import Flask, jsonify, request, render_template, abort
from supabase import create_client, Client

app = Flask(__name__, static_folder="static", template_folder=".")

# ── Supabase client ─────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_SECRET   = os.environ.get("API_SECRET", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Auth decorator ───────────────────────────────────────────────────────────────
def require_api_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_SECRET:
            abort(500, "API_SECRET not configured on server")
        if request.headers.get("X-API-Secret") != API_SECRET:
            abort(401, "Invalid or missing X-API-Secret header")
        return f(*args, **kwargs)
    return decorated


# ── Frontend ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── Odds helper ──────────────────────────────────────────────────────────────────
def odds_payout(odds_str, bet=100):
    """Return profit on a winning $100 bet given American odds string.
       Defaults to -110 if odds missing or unparseable."""
    try:
        odds = int(str(odds_str).replace('+', '').strip())
        if odds < 0:
            return round(bet * 100 / abs(odds), 2)
        else:
            return round(bet * odds / 100, 2)
    except (ValueError, TypeError):
        return round(bet * 100 / 110, 2)  # default -110


# ── API: Overall record ──────────────────────────────────────────────────────────
@app.route("/api/record")
def api_record():
    resp = supabase.table("picks").select("result, type, pick_odds").execute()
    rows = resp.data or []

    wins = losses = pushes = 0
    profit = 0.0
    total_risked = 0.0

    for row in rows:
        r = (row.get("result") or "").strip().upper()
        if r == "W":
            wins += 1
            profit += odds_payout(row.get("pick_odds"))
            total_risked += 100
        elif r == "L":
            losses += 1
            profit -= 100
            total_risked += 100
        elif r == "P":
            pushes += 1

    total_decided = wins + losses
    win_pct = round(wins / total_decided * 100, 1) if total_decided > 0 else 0.0
    roi     = round(profit / total_risked * 100, 2) if total_risked > 0 else 0.0
    profit  = round(profit, 2)

    # Current streak
    all_resp = (supabase.table("picks")
                .select("result, date")
                .not_.is_("result", "null")
                .neq("result", "")
                .order("date", desc=True)
                .order("id", desc=True)
                .execute())
    streak_rows = all_resp.data or []

    streak = 0
    streak_type = ""
    for row in streak_rows:
        r = (row.get("result") or "").strip().upper()
        if r == "P":
            continue
        if streak == 0:
            streak_type = r
            streak = 1
        elif r == streak_type:
            streak += 1
        else:
            break

    return jsonify({
        "wins":        wins,
        "losses":      losses,
        "pushes":      pushes,
        "win_pct":     win_pct,
        "roi":         roi,
        "profit":      profit,
        "streak":      streak,
        "streak_type": streak_type,
    })


# ── API: Today's picks ───────────────────────────────────────────────────────────
@app.route("/api/today")
def api_today():
    today = date.today().isoformat()
    resp  = (supabase.table("picks")
             .select("*")
             .eq("date", today)
             .order("margin", desc=True)
             .execute())
    return jsonify(resp.data or [])


# ── API: Pick history (last 100) ─────────────────────────────────────────────────
@app.route("/api/picks")
def api_picks():
    resp = (supabase.table("picks")
            .select("*")
            .order("date", desc=True)
            .order("id", desc=True)
            .limit(100)
            .execute())
    return jsonify(resp.data or [])


# ── API: Chart data ───────────────────────────────────────────────────────────────
@app.route("/api/chart")
def api_chart():
    resp = (supabase.table("picks")
            .select("date, result, pick_odds")
            .not_.is_("result", "null")
            .neq("result", "")
            .order("date")
            .order("id")
            .execute())
    rows = resp.data or []

    points     = []
    cumulative = 0.0

    for row in rows:
        r = (row.get("result") or "").strip().upper()
        if r == "W":
            cumulative += odds_payout(row.get("pick_odds"))
        elif r == "L":
            cumulative -= 100
        # pushes don't change profit
        points.append({"date": row["date"], "profit": round(cumulative, 2)})

    return jsonify(points)


# ── API: Log a pick (bot → dashboard) ────────────────────────────────────────────
@app.route("/api/log_pick", methods=["POST"])
@require_api_secret
def api_log_pick():
    """
    Expected JSON body:
    {
        "date":         "2026-05-17",
        "away_team":    "New York Yankees",
        "home_team":    "Boston Red Sox",
        "away_pitcher": "Gerrit Cole",
        "home_pitcher": "Nathan Eovaldi",
        "pick":         "New York Yankees",
        "margin":       1.45,
        "pick_odds":    "-130",
        "type":         "strong"
    }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        abort(400, "JSON body required")

    required = ["date", "away_team", "home_team", "pick"]
    missing  = [k for k in required if not data.get(k)]
    if missing:
        abort(400, f"Missing fields: {', '.join(missing)}")

    record = {
        "date":         data["date"],
        "away_team":    data["away_team"],
        "home_team":    data["home_team"],
        "away_pitcher": data.get("away_pitcher", ""),
        "home_pitcher": data.get("home_pitcher", ""),
        "pick":         data["pick"],
        "margin":       data.get("margin"),
        "pick_odds":    str(data["pick_odds"]) if data.get("pick_odds") is not None else None,
        "type":         data.get("type", "lean"),
        "result":       "",
    }

    resp = supabase.table("picks").insert(record).execute()
    return jsonify({"ok": True, "id": resp.data[0]["id"] if resp.data else None}), 201


# ── API: Update results (bot → dashboard) ────────────────────────────────────────
@app.route("/api/update_results", methods=["POST"])
@require_api_secret
def api_update_results():
    """
    Expected JSON body:
    {
        "date": "2026-05-16",
        "results": [
            {
                "away_team": "New York Yankees",
                "home_team": "Boston Red Sox",
                "result":    "W"
            },
            ...
        ]
    }
    """
    data = request.get_json(force=True, silent=True)
    if not data or "date" not in data or "results" not in data:
        abort(400, "JSON body with 'date' and 'results' required")

    target_date = data["date"]
    updated     = 0

    for item in data["results"]:
        result = (item.get("result") or "").strip().upper()
        if result not in ("W", "L", "P", "?"):
            continue
        (supabase.table("picks")
         .update({"result": result})
         .eq("date", target_date)
         .eq("away_team", item["away_team"])
         .eq("home_team", item["home_team"])
         .execute())
        updated += 1

    return jsonify({"ok": True, "updated": updated})


# ── Health check ─────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()})


# ── Debug endpoint ────────────────────────────────────────────────────────────────
@app.route("/debug")
def debug_info():
    import httpx
    url_safe = SUPABASE_URL[:40] + "..." if len(SUPABASE_URL) > 40 else SUPABASE_URL
    key_safe = SUPABASE_KEY[:12] + "..." if len(SUPABASE_KEY) > 12 else "MISSING"
    results  = {}

    # Test 1: raw HTTP call bypassing supabase-py
    try:
        rest_url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/picks?select=id&limit=1"
        headers  = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        r = httpx.get(rest_url, headers=headers, timeout=10)
        results["raw_http"] = {
            "status":   r.status_code,
            "url":      rest_url,
            "response": r.text[:500],
        }
    except Exception as e:
        results["raw_http"] = {"error": str(e)}

    # Test 2: supabase-py client
    try:
        resp = supabase.table("picks").select("id").limit(1).execute()
        results["supabase_client"] = {"ok": True, "rows": resp.data}
    except Exception as e:
        results["supabase_client"] = {"error": str(e)}

    return jsonify({
        "url_prefix": url_safe,
        "key_prefix": key_safe,
        "results":    results,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


