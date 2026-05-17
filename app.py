import os
from datetime import datetime, date
from functools import wraps
from flask import Flask, jsonify, request, abort, send_from_directory
from supabase import create_client

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
API_SECRET   = os.environ.get("API_SECRET", "")

app      = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

def require_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Secret") != API_SECRET:
            abort(401)
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/api/record")
def api_record():
    if not supabase:
        return jsonify({"wins":0,"losses":0,"pushes":0,"win_pct":0,"roi":0,"profit":0,"streak":0,"streak_type":""})
    rows = supabase.table("picks").select("result").execute().data or []
    wins = sum(1 for r in rows if (r.get("result") or "").upper() == "W")
    losses = sum(1 for r in rows if (r.get("result") or "").upper() == "L")
    pushes = sum(1 for r in rows if (r.get("result") or "").upper() == "P")
    total  = wins + losses
    win_pct = round(wins / total * 100, 1) if total else 0
    profit  = round((wins * 100) - (losses * 110), 2)
    roi     = round(profit / (total * 110) * 100, 1) if total else 0
    streak_rows = (supabase.table("picks").select("result").neq("result","").order("date", desc=True).order("id", desc=True).execute().data or [])
    streak, streak_type = 0, ""
    for r in streak_rows:
        res = (r.get("result") or "").upper()
        if res == "P": continue
        if not streak_type: streak_type = res
        if res == streak_type: streak += 1
        else: break
    return jsonify({"wins":wins,"losses":losses,"pushes":pushes,"win_pct":win_pct,"roi":roi,"profit":profit,"streak":streak,"streak_type":streak_type})

@app.route("/api/today")
def api_today():
    if not supabase: return jsonify([])
    today = date.today().isoformat()
    data  = supabase.table("picks").select("*").eq("date", today).order("margin", desc=True).execute()
    return jsonify(data.data or [])

@app.route("/api/picks")
def api_picks():
    if not supabase: return jsonify([])
    data = supabase.table("picks").select("*").order("date", desc=True).order("id", desc=True).limit(100).execute()
    return jsonify(data.data or [])

@app.route("/api/chart")
def api_chart():
    if not supabase: return jsonify([])
    rows = supabase.table("picks").select("date,result").neq("result","").order("date").order("id").execute().data or []
    points, cum = [], 0.0
    for r in rows:
        res = (r.get("result") or "").upper()
        if res == "W": cum += 100
        elif res == "L": cum -= 110
        points.append({"date": r["date"], "profit": round(cum, 2)})
    return jsonify(points)

@app.route("/api/log_pick", methods=["POST"])
@require_secret
def api_log_pick():
    if not supabase: abort(503)
    data = request.get_json(force=True, silent=True) or {}
    record = {
        "date": data.get("date"), "away_team": data.get("away_team"),
        "home_team": data.get("home_team"), "away_pitcher": data.get("away_pitcher",""),
        "home_pitcher": data.get("home_pitcher",""), "pick": data.get("pick"),
        "margin": data.get("margin"), "pick_odds": str(data["pick_odds"]) if data.get("pick_odds") else None,
        "type": data.get("type","strong"), "result": ""
    }
    resp = supabase.table("picks").insert(record).execute()
    return jsonify({"ok": True}), 201

@app.route("/api/update_results", methods=["POST"])
@require_secret
def api_update_results():
    if not supabase: abort(503)
    data = request.get_json(force=True, silent=True) or {}
    for item in data.get("results", []):
        res = (item.get("result") or "").upper()
        if res not in ("W","L","P"): continue
        supabase.table("picks").update({"result": res}).eq("date", data["date"]).eq("away_team", item["away_team"]).eq("home_team", item["home_team"]).execute()
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
