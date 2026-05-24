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

from flask import Flask, jsonify, request, render_template, abort, make_response, redirect
from supabase import create_client, Client

app = Flask(__name__, static_folder="static", template_folder=".")

# ── Supabase client ─────────────────────────────────────────────────────────────
SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
API_SECRET         = os.environ.get("API_SECRET", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Auth decorator ───────────────────────────────────────────────────────────────
def require_api_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_SECRET:
            abort(500, "API_SECRET not configured on server")
        # Accept secret via header OR URL param ?secret=...
        provided = (request.headers.get("X-API-Secret")
                    or request.args.get("secret", ""))
        if provided != API_SECRET:
            abort(401, "Invalid or missing API secret")
        return f(*args, **kwargs)
    return decorated


# ── Frontend ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if DASHBOARD_PASSWORD:
        auth = request.cookies.get("auth")
        if auth != DASHBOARD_PASSWORD:
            return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    password = request.form.get("password", "")
    if password == DASHBOARD_PASSWORD:
        resp = make_response(redirect("/"))
        resp.set_cookie("auth", password, max_age=60*60*24*30)
        return resp
    return render_template("login.html", error="Wrong password")


# ── Odds helper ──────────────────────────────────────────────────────────────────
def odds_payout(odds_str, bet=100):
    """Return profit on a winning bet.
    Auto-detects decimal odds (e.g. 1.91, 2.50) vs American odds (e.g. -110, +150).
    Defaults to decimal 1.91 (-110 equivalent) if missing or unparseable."""
    try:
        val = float(str(odds_str).replace('+', '').strip())
        if 1.01 <= val < 100:
            # Decimal odds: profit = bet * (odds - 1)
            return round(bet * (val - 1), 2)
        elif val >= 100:
            # American positive odds: +150 means win 150 per 100 risked
            return round(bet * val / 100, 2)
        elif val <= -100:
            # American negative odds: -164 means win 100 per 164 risked
            return round(bet * 100 / abs(val), 2)
        else:
            return round(bet * 0.9091, 2)  # fallback -110 / 1.91
    except (ValueError, TypeError):
        return round(bet * 0.9091, 2)  # fallback -110 / 1.91


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
        "date":            data["date"],
        "away_team":       data["away_team"],
        "home_team":       data["home_team"],
        "away_pitcher":    data.get("away_pitcher", ""),
        "home_pitcher":    data.get("home_pitcher", ""),
        "pick":            data["pick"],
        "margin":          data.get("margin"),
        "pick_odds":       str(data["pick_odds"]) if data.get("pick_odds") is not None else None,
        "type":            data.get("type", "pick"),
        "away_era":        data.get("away_era"),
        "home_era":        data.get("home_era"),
        "away_recent_era": data.get("away_recent_era"),
        "home_recent_era": data.get("home_recent_era"),
        "result":          "",
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


# ── Auto result checker ──────────────────────────────────────────────────────────
@app.route("/api/check_results", methods=["GET", "POST"])
@require_api_secret
def api_check_results():
    """
    Called by PebbleHost cron every 30 mins.
    Checks today's pending picks against the MLB Stats API and updates results.
    """
    import requests as _req

    today = date.today().isoformat()

    # 1. Fetch today's pending picks from Supabase
    resp = (supabase.table("picks")
            .select("*")
            .eq("date", today)
            .execute())
    all_picks = resp.data or []
    pending   = [p for p in all_picks if not (p.get("result") or "").strip()]

    if not pending:
        return jsonify({"ok": True, "message": "No pending picks", "updated": 0})

    # 2. Query MLB Stats API for today's final games
    mlb_url = (
        f"https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&startDate={today}&endDate={today}"
        f"&hydrate=linescore,game&gameType=R"
    )
    try:
        mlb_resp = _req.get(
            mlb_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; mlb-result-checker/1.0)"},
            timeout=20
        )
        mlb_resp.raise_for_status()
        mlb_data = mlb_resp.json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # 3. Build final scores dict keyed by (away, home)
    final_games = {}
    for date_entry in mlb_data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            innings = game.get("linescore", {}).get("innings", [])
            if len(innings) < 5:
                continue
            away      = game["teams"]["away"]["team"]["name"]
            home      = game["teams"]["home"]["team"]["name"]
            away_runs = sum(i.get("away", {}).get("runs", 0) for i in innings)
            home_runs = sum(i.get("home", {}).get("runs", 0) for i in innings)
            if away_runs > home_runs:
                final_games[(away, home)] = away
            elif home_runs > away_runs:
                final_games[(away, home)] = home
            else:
                final_games[(away, home)] = "TIE"

    # 4. Match picks to final games and update Supabase
    updated = 0
    details = []
    for pick in pending:
        away   = pick.get("away_team", "")
        home   = pick.get("home_team", "")
        chosen = pick.get("pick", "")
        winner = final_games.get((away, home))
        if winner is None:
            continue   # game not finished yet

        result = "P" if winner == "TIE" else ("W" if winner == chosen else "L")
        supabase.table("picks").update({"result": result}).eq("id", pick["id"]).execute()
        updated += 1
        details.append({"game": f"{away} @ {home}", "pick": chosen, "result": result})

    return jsonify({
        "ok":      True,
        "date":    today,
        "updated": updated,
        "details": details,
        "finals":  len(final_games),
        "pending": len(pending),
    })


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
