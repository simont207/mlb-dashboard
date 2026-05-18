#!/usr/bin/env python3
import os
from datetime import datetime, date
from functools import wraps
from flask import Flask, jsonify, request, render_template, abort, make_response, redirect
from supabase import create_client, Client

app = Flask(__name__, static_folder="static", template_folder=".")

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
API_SECRET         = os.environ.get("API_SECRET", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def require_api_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_SECRET:
            abort(500, "API_SECRET not configured")
        if request.headers.get("X-API-Secret") != API_SECRET:
            abort(401, "Invalid or missing X-API-Secret header")
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    if DASHBOARD_PASSWORD:
        if request.cookies.get("auth") != DASHBOARD_PASSWORD:
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

@app.route("/guest/<expiry_date>")
def guest_link(expiry_date):
    try:
        from datetime import datetime as dt
        expiry = dt.strptime(expiry_date, "%Y-%m-%d").date()
        if date.today() > expiry:
            return render_template("login.html", error="This link has expired")
        max_age = int((dt(expiry.year, expiry.month, expiry.day, 23, 59, 59) - dt.now()).total_seconds())
        resp = make_response(redirect("/"))
        resp.set_cookie("auth", DASHBOARD_PASSWORD, max_age=max(max_age, 0))
        return resp
    except ValueError:
        return render_template("login.html", error="Invalid link")

def odds_payout(odds_str, bet=100):
    try:
        val = float(str(odds_str).replace('+', '').strip())
        if 1.01 <= val < 100:
            return round(bet * (val - 1), 2)
        elif val >= 100:
            return round(bet * val / 100, 2)
        elif val <= -100:
            return round(bet * 100 / abs(val), 2)
        else:
            return round(bet * 0.9091, 2)
    except (ValueError, TypeError):
        return round(bet * 0.9091, 2)

@app.route("/api/record")
def api_record():
    resp = supabase.table("picks").select("result, type, pick_odds").execute()
    rows = resp.data or []
    wins = losses = pushes = 0
    profit = total_risked = 0.0
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
    all_resp = (supabase.table("picks").select("result, date")
                .not_.is_("result", "null").neq("result", "")
                .order("date", desc=True).order("id", desc=True).execute())
    streak = 0
    streak_type = ""
    for row in (all_resp.data or []):
        r = (row.get("result") or "").strip().upper()
        if r == "P": continue
        if streak == 0: streak_type = r; streak = 1
        elif r == streak_type: streak += 1
        else: break
    return jsonify({"wins": wins, "losses": losses, "pushes": pushes,
                    "win_pct": win_pct, "roi": roi, "profit": round(profit, 2),
                    "streak": streak, "streak_type": streak_type})

@app.route("/api/today")
def api_today():
    resp = (supabase.table("picks").select("*")
            .eq("date", date.today().isoformat())
            .order("margin", desc=True).execute())
    return jsonify(resp.data or [])

@app.route("/api/picks")
def api_picks():
    resp = (supabase.table("picks").select("*")
            .order("date", desc=True).order("id", desc=True)
            .limit(100).execute())
    return jsonify(resp.data or [])

@app.route("/api/chart")
def api_chart():
    resp = (supabase.table("picks").select("date, result, pick_odds")
            .not_.is_("result", "null").neq("result", "")
            .order("date").order("id").execute())
    points = []
    cumulative = 0.0
    for row in (resp.data or []):
        r = (row.get("result") or "").strip().upper()
        if r == "W": cumulative += odds_payout(row.get("pick_odds"))
        elif r == "L": cumulative -= 100
        points.append({"date": row["date"], "profit": round(cumulative, 2)})
    return jsonify(points)

@app.route("/api/log_pick", methods=["POST"])
@require_api_secret
def api_log_pick():
    data = request.get_json(force=True, silent=True)
    if not data: abort(400, "JSON body required")
    missing = [k for k in ["date","away_team","home_team","pick"] if not data.get(k)]
    if missing: abort(400, f"Missing fields: {', '.join(missing)}")
    record = {
        "date": data["date"], "away_team": data["away_team"],
        "home_team": data["home_team"], "away_pitcher": data.get("away_pitcher", ""),
        "home_pitcher": data.get("home_pitcher", ""), "pick": data["pick"],
        "margin": data.get("margin"),
        "pick_odds": str(data["pick_odds"]) if data.get("pick_odds") is not None else None,
        "type": data.get("type", "lean"), "result": "",
    }
    resp = supabase.table("picks").insert(record).execute()
    return jsonify({"ok": True, "id": resp.data[0]["id"] if resp.data else None}), 201

@app.route("/api/update_results", methods=["POST"])
@require_api_secret
def api_update_results():
    data = request.get_json(force=True, silent=True)
    if not data or "date" not in data or "results" not in data:
        abort(400, "JSON body with 'date' and 'results' required")
    updated = 0
    for item in data["results"]:
        result = (item.get("result") or "").strip().upper()
        if result not in ("W", "L", "P", "?"): continue
        (supabase.table("picks").update({"result": result})
         .eq("date", data["date"]).eq("away_team", item["away_team"])
         .eq("home_team", item["home_team"]).execute())
        updated += 1
    return jsonify({"ok": True, "updated": updated})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


