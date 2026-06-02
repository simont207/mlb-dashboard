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
import time
import json
import secrets
import string
from datetime import datetime, date, timedelta, timezone
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

# ── VAPID (Web Push) ──────────────────────────────────────────────────────────────
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY",  "BJGPf1HT0hcpH-MzrSs6Tz_zlBMmJcCusJ75ZPEgcvVleyOlGrO2Szw_3eT5Ok0n1gBYL9Uhy_ke7aJUs_vxPyA")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "HcrEnucNdJ-ft7XGbzIPatJOPETnk2lZNjCQw9N6BEU")
VAPID_CLAIMS      = {"sub": "mailto:simontierney365@gmail.com"}

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


# ── Access code helper ────────────────────────────────────────────────────────────
def _gen_code(length=8):
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


# ── Frontend ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/sw.js")
def service_worker():
    import os as _os
    from flask import send_from_directory, make_response as _mkr
    resp = _mkr(send_from_directory(
        _os.path.join(_os.path.dirname(__file__), "static"), "sw.js"
    ))
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Content-Type"] = "application/javascript"
    return resp

@app.route("/admin/codes")
def admin_codes():
    provided = request.args.get("secret", "")
    if not API_SECRET or provided != API_SECRET:
        abort(401, "Access denied — add ?secret=YOUR_SECRET to the URL")
    return render_template("admin_codes.html")


# ── Access pass API ───────────────────────────────────────────────────────────────
@app.route("/api/access/validate", methods=["POST"])
def api_access_validate():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "").strip()

    if not code:
        return jsonify({"valid": False, "error": "No code provided"}), 400

    resp = supabase.table("access_codes").select("*").eq("code", code).execute()
    rows = resp.data or []
    if not rows:
        return jsonify({"valid": False, "error": "Invalid access code"}), 403

    row = rows[0]
    now = datetime.now(timezone.utc)

    if row.get("expires_at"):
        exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if now > exp:
            return jsonify({"valid": False, "error": "This pass has expired"}), 403

    user_id = None
    if row.get("multi_use"):
        # Multi-use: create a new user entry each time
        u = supabase.table("access_users").insert({
            "code": code, "name": name, "last_seen": now.isoformat()
        }).execute()
        user_id = u.data[0]["id"] if u.data else None
    else:
        update = {"last_seen": now.isoformat()}
        if name:
            update["name"] = name
        supabase.table("access_codes").update(update).eq("code", code).execute()

    return jsonify({
        "valid":      True,
        "code":       code,
        "name":       name or row.get("name", ""),
        "expires_at": row.get("expires_at"),
        "user_id":    user_id,
        "multi_use":  row.get("multi_use", False),
    })


@app.route("/api/access/heartbeat", methods=["POST"])
def api_access_heartbeat():
    data    = request.get_json(force=True, silent=True) or {}
    code    = (data.get("code") or "").strip().upper()
    user_id = data.get("user_id")
    if not code:
        return jsonify({"ok": False}), 400

    resp = supabase.table("access_codes").select("expires_at").eq("code", code).execute()
    rows = resp.data or []
    if not rows:
        return jsonify({"ok": False, "expired": True}), 403

    now = datetime.now(timezone.utc)
    row = rows[0]
    if row.get("expires_at"):
        exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if now > exp:
            return jsonify({"ok": False, "expired": True}), 403

    if user_id:
        supabase.table("access_users").update({"last_seen": now.isoformat()}).eq("id", user_id).execute()
    else:
        supabase.table("access_codes").update({"last_seen": now.isoformat()}).eq("code", code).execute()

    return jsonify({"ok": True})


@app.route("/api/access/generate", methods=["POST"])
@require_api_secret
def api_access_generate():
    data      = request.get_json(force=True, silent=True) or {}
    permanent = data.get("permanent", False)
    multi     = data.get("multi_use", False)
    code      = _gen_code()
    now       = datetime.now(timezone.utc)
    expires   = None if permanent else (now + timedelta(days=7)).isoformat()

    supabase.table("access_codes").insert({
        "code": code, "expires_at": expires, "multi_use": multi
    }).execute()

    base = request.host_url.rstrip('/')
    link = f"{base}/?code={code}"
    return jsonify({"ok": True, "code": code, "link": link, "expires_at": expires, "multi_use": multi})


@app.route("/api/access/delete", methods=["POST"])
@require_api_secret
def api_access_delete():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "No code"}), 400
    supabase.table("access_users").delete().eq("code", code).execute()
    supabase.table("access_codes").delete().eq("code", code).execute()
    return jsonify({"ok": True})


@app.route("/api/access/codes", methods=["GET"])
@require_api_secret
def api_access_codes():
    resp  = supabase.table("access_codes").select("*").order("created_at", desc=True).execute()
    codes = resp.data or []
    uresp = supabase.table("access_users").select("*").order("joined_at").execute()
    users = uresp.data or []
    now   = datetime.now(timezone.utc)

    result = []
    for row in codes:
        if row.get("expires_at"):
            exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            row["expired"]   = now > exp
            row["days_left"] = max(0, (exp - now).days)
        else:
            row["expired"]   = False
            row["days_left"] = None

        if row.get("multi_use"):
            code_users = [u for u in users if u["code"] == row["code"]]
            for u in code_users:
                if u.get("last_seen"):
                    ls = datetime.fromisoformat(u["last_seen"].replace("Z", "+00:00"))
                    u["online"] = (now - ls).total_seconds() < 600
                else:
                    u["online"] = False
            row["users"]        = code_users
            row["user_count"]   = len(code_users)
            row["online_count"] = sum(1 for u in code_users if u.get("online"))
            row["online"]       = row["online_count"] > 0
        else:
            row["users"]        = []
            row["user_count"]   = 1 if row.get("name") else 0
            if row.get("last_seen"):
                ls = datetime.fromisoformat(row["last_seen"].replace("Z", "+00:00"))
                row["online"] = (now - ls).total_seconds() < 600
            else:
                row["online"] = False

        result.append(row)

    return jsonify(result)


# ── Unit sizing (mirrors dashboard logic) ────────────────────────────────────────
def get_units(margin):
    """Return recommended bet units based on edge/margin score."""
    try:
        m = float(margin)
        if m >= 2.5: return 2.5
        if m >= 2.0: return 2.0
        if m >= 1.5: return 1.5
        return 1.0
    except (TypeError, ValueError):
        return 1.0


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
        r     = (row.get("result") or "").strip().upper()
        if r == "V": continue   # voided picks excluded from record
        units = get_units(row.get("margin"))
        bet   = 100 * units
        if r == "W":
            wins += 1
            profit += odds_payout(row.get("pick_odds"), bet=bet)
            total_risked += bet
        elif r == "L":
            losses += 1
            profit -= bet
            total_risked += bet
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
        r     = (row.get("result") or "").strip().upper()
        if r == "V": continue   # voided picks excluded from chart
        units = get_units(row.get("margin"))
        bet   = 100 * units
        if r == "W":
            cumulative += odds_payout(row.get("pick_odds"), bet=bet)
        elif r == "L":
            cumulative -= bet
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


# ── Today's games proxy ───────────────────────────────────────────────────────────
@app.route("/api/todays_games", methods=["GET"])
@require_api_secret
def api_todays_games():
    import requests as _req
    from datetime import datetime as _dt

    target = request.args.get("date") or _dt.today().strftime("%Y-%m-%d")
    mlb_date = _dt.strptime(target, "%Y-%m-%d").strftime("%m/%d/%Y")

    url = (f"https://statsapi.mlb.com/api/v1/schedule"
           f"?sportId=1&date={mlb_date}"
           f"&hydrate=probablePitcher,linescore&gameType=R")
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = _req.get(url, headers=hdrs, timeout=25)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("gameType") != "R":
                continue
            teams     = game.get("teams", {})
            away      = teams.get("away", {})
            home      = teams.get("home", {})
            away_p    = away.get("probablePitcher", {}).get("fullName", "")
            home_p    = home.get("probablePitcher", {}).get("fullName", "")
            games.append({
                "game_id":               str(game.get("gamePk", "")),
                "game_type":             game.get("gameType", "R"),
                "away_name":             away.get("team", {}).get("name", ""),
                "home_name":             home.get("team", {}).get("name", ""),
                "away_probable_pitcher": away_p,
                "home_probable_pitcher": home_p,
                "venue_name":            game.get("venue", {}).get("name", ""),
                "game_datetime":         game.get("gameDate", ""),
                "status":                game.get("status", {}).get("abstractGameState", ""),
            })

    return jsonify({"ok": True, "games": games})


# ── F5 stats proxy (Render IP not blocked by MLB API) ────────────────────────────
@app.route("/api/f5_stats", methods=["GET"])
@require_api_secret
def api_f5_stats():
    """
    Fetches season F5 stats from MLB Stats API using Render's unblocked IP.
    Called by the PebbleHost bot instead of hitting MLB's API directly.
    Returns scored_home, scored_away, allowed_home, allowed_away as JSON dicts.
    """
    import requests as _req
    from collections import defaultdict as _dd
    from datetime import datetime as _dt, timedelta as _td

    SEASON_START = "2026-03-20"
    yesterday    = (_dt.today() - _td(days=1)).strftime("%Y-%m-%d")
    headers      = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept":     "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    scored_home  = _dd(list)
    scored_away  = _dd(list)
    allowed_home = _dd(list)
    allowed_away = _dd(list)

    current = _dt.strptime(SEASON_START, "%Y-%m-%d")
    end     = _dt.strptime(yesterday,    "%Y-%m-%d")
    errors  = []

    while current <= end:
        month_end = min(
            (current.replace(day=1) + _td(days=32)).replace(day=1) - _td(days=1),
            end
        )
        url = (
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&startDate={current.strftime('%Y-%m-%d')}"
            f"&endDate={month_end.strftime('%Y-%m-%d')}"
            f"&hydrate=linescore&gameType=R"
        )
        try:
            resp = _req.get(url, headers=headers, timeout=25)
            resp.raise_for_status()
            for date_entry in resp.json().get("dates", []):
                for game in date_entry.get("games", []):
                    if game.get("status", {}).get("abstractGameState") != "Final":
                        continue
                    innings = game.get("linescore", {}).get("innings", [])
                    if len(innings) < 5:
                        continue
                    away   = game["teams"]["away"]["team"]["name"]
                    home   = game["teams"]["home"]["team"]["name"]
                    away_f5 = sum(i.get("away", {}).get("runs", 0) for i in innings[:5])
                    home_f5 = sum(i.get("home", {}).get("runs", 0) for i in innings[:5])
                    scored_away[away].append(away_f5)
                    scored_home[home].append(home_f5)
                    allowed_away[away].append(home_f5)
                    allowed_home[home].append(away_f5)
        except Exception as e:
            errors.append(f"{current.strftime('%Y-%m')}: {e}")

        current = month_end + _td(days=1)

    return jsonify({
        "ok":          True,
        "scored_home":  dict(scored_home),
        "scored_away":  dict(scored_away),
        "allowed_home": dict(allowed_home),
        "allowed_away": dict(allowed_away),
        "errors":       errors,
    })


# ── Team stats (F5 averages + last 5 form) ───────────────────────────────────────
_team_stats_cache = {"data": None, "ts": 0}

@app.route("/api/team_stats", methods=["GET"])
@require_api_secret
def api_team_stats():
    """
    Returns per-team F5 run averages and last-5-game form.
    Results cached for 1 hour to keep response fast.
    """
    import requests as _req
    from collections import defaultdict as _dd
    from datetime import datetime as _dt, timedelta as _td

    now = time.time()
    if _team_stats_cache["data"] and now - _team_stats_cache["ts"] < 3600:
        return jsonify(_team_stats_cache["data"])

    SEASON_START = "2026-03-20"
    yesterday    = (_dt.today() - _td(days=1)).strftime("%Y-%m-%d")
    two_weeks    = (_dt.today() - _td(days=16)).strftime("%Y-%m-%d")
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # ── 1. Season F5 stats (month by month) ──────────────────────────────────
    scored_home  = _dd(list); scored_away  = _dd(list)
    allowed_home = _dd(list); allowed_away = _dd(list)

    current = _dt.strptime(SEASON_START, "%Y-%m-%d")
    end     = _dt.strptime(yesterday,    "%Y-%m-%d")
    while current <= end:
        month_end = min(
            (current.replace(day=1) + _td(days=32)).replace(day=1) - _td(days=1), end
        )
        url = (f"https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&startDate={current.strftime('%Y-%m-%d')}"
               f"&endDate={month_end.strftime('%Y-%m-%d')}"
               f"&hydrate=linescore&gameType=R")
        try:
            resp = _req.get(url, headers=hdrs, timeout=25)
            resp.raise_for_status()
            for de in resp.json().get("dates", []):
                for game in de.get("games", []):
                    if game.get("status", {}).get("abstractGameState") != "Final":
                        continue
                    innings = game.get("linescore", {}).get("innings", [])
                    if len(innings) < 5:
                        continue
                    away = game["teams"]["away"]["team"]["name"]
                    home = game["teams"]["home"]["team"]["name"]
                    af5  = sum(i.get("away", {}).get("runs", 0) for i in innings[:5])
                    hf5  = sum(i.get("home", {}).get("runs", 0) for i in innings[:5])
                    scored_away[away].append(af5);  scored_home[home].append(hf5)
                    allowed_away[away].append(hf5); allowed_home[home].append(af5)
        except Exception:
            pass
        current = month_end + _td(days=1)

    # ── 2. Last 5 game results per team ──────────────────────────────────────
    last5 = _dd(list)   # team -> list of "W"/"L" (chronological)
    try:
        url = (f"https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&startDate={two_weeks}&endDate={yesterday}"
               f"&hydrate=linescore&gameType=R")
        resp = _req.get(url, headers=hdrs, timeout=25)
        resp.raise_for_status()
        for de in resp.json().get("dates", []):
            for game in de.get("games", []):
                if game.get("status", {}).get("abstractGameState") != "Final":
                    continue
                innings = game.get("linescore", {}).get("innings", [])
                if not innings:
                    continue
                away = game["teams"]["away"]["team"]["name"]
                home = game["teams"]["home"]["team"]["name"]
                ar   = sum(i.get("away", {}).get("runs", 0) for i in innings)
                hr   = sum(i.get("home", {}).get("runs", 0) for i in innings)
                if ar > hr:
                    last5[away].append("W"); last5[home].append("L")
                elif hr > ar:
                    last5[home].append("W"); last5[away].append("L")
    except Exception:
        pass

    # ── 3. Build per-team output ──────────────────────────────────────────────
    all_teams = set(list(scored_home)+list(scored_away))
    teams = {}
    for team in all_teams:
        scored  = scored_home.get(team, []) + scored_away.get(team, [])
        allowed = allowed_home.get(team, []) + allowed_away.get(team, [])
        form    = last5.get(team, [])[-5:]
        wins5   = form.count("W")
        teams[team] = {
            "f5_scored_avg":  round(sum(scored)/len(scored),   1) if scored  else None,
            "f5_allowed_avg": round(sum(allowed)/len(allowed), 1) if allowed else None,
            "last5":          form,
            "last5_w":        wins5,
            "last5_l":        len(form) - wins5,
        }

    result = {"ok": True, "teams": teams, "cached_at": int(now)}
    _team_stats_cache["data"] = result
    _team_stats_cache["ts"]   = now
    return jsonify(result)


# ── Live odds (multi-book line comparison) ───────────────────────────────────────
_odds_cache = {"data": None, "ts": 0}

@app.route("/api/odds", methods=["GET"])
def api_odds():
    import requests as _req
    now = time.time()
    if _odds_cache["data"] and now - _odds_cache["ts"] < 900:   # 15-min cache
        return jsonify(_odds_cache["data"])

    ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "999d33b533c90c3b35e6dfeece550932")
    BOOKS        = "pinnacle,betway,betmgm,draftkings,fanduel"
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
           f"?apiKey={ODDS_API_KEY}&regions=us,eu&markets=h2h"
           f"&oddsFormat=decimal&bookmakers={BOOKS}")
    try:
        resp = _req.get(url, timeout=15)
        resp.raise_for_status()
        games = resp.json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    result = {}
    for game in games:
        away = game.get("away_team", "")
        home = game.get("home_team", "")
        key  = f"{away}|{home}"
        books_data = {}
        for bm in game.get("bookmakers", []):
            title = bm["title"]
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for outcome in market["outcomes"]:
                    team  = outcome["name"]
                    price = outcome["price"]
                    if team not in books_data:
                        books_data[team] = {}
                    books_data[team][title] = price
        result[key] = {"away": away, "home": home, "books": books_data}

    data = {"ok": True, "games": result, "cached_at": int(now)}
    _odds_cache["data"] = data
    _odds_cache["ts"]   = now
    return jsonify(data)


# ── Yesterday results proxy ───────────────────────────────────────────────────────
@app.route("/api/yesterday_results", methods=["GET"])
@require_api_secret
def api_yesterday_results():
    """
    Proxy for fetching and saving yesterday's results via Render's unblocked IP.
    Called by the bot instead of hitting MLB's API directly from PebbleHost.
    """
    import requests as _req

    target_date = request.args.get("date") or (date.today() - timedelta(days=1)).isoformat()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = (
        f"https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&startDate={target_date}&endDate={target_date}"
        f"&hydrate=linescore&gameType=R"
    )
    try:
        resp = _req.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
        mlb_data = resp.json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Fetch picks for that date from Supabase
    picks_resp = supabase.table("picks").select("*").eq("date", target_date).execute()
    picks = [p for p in (picks_resp.data or []) if not (p.get("result") or "").strip()]

    # Build final scores
    final_games = {}
    for date_entry in mlb_data.get("dates", []):
        for game in date_entry.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            innings = game.get("linescore", {}).get("innings", [])
            if len(innings) < 5:
                continue
            away = game["teams"]["away"]["team"]["name"]
            home = game["teams"]["home"]["team"]["name"]
            away_runs = sum(i.get("away", {}).get("runs", 0) for i in innings)
            home_runs = sum(i.get("home", {}).get("runs", 0) for i in innings)
            final_games[(away, home)] = away if away_runs > home_runs else (home if home_runs > away_runs else "TIE")

    # Update results in Supabase
    updated = 0
    details = []
    for pick in picks:
        winner = final_games.get((pick["away_team"], pick["home_team"]))
        if winner is None:
            continue
        result = "P" if winner == "TIE" else ("W" if winner == pick["pick"] else "L")
        supabase.table("picks").update({"result": result}).eq("id", pick["id"]).execute()
        updated += 1
        details.append({"game": f"{pick['away_team']} @ {pick['home_team']}", "result": result})

    return jsonify({"ok": True, "date": target_date, "updated": updated, "details": details})


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

    # Push notifications for settled results
    for item in details:
        emoji  = "✅" if item["result"] == "W" else ("❌" if item["result"] == "L" else "➖")
        label  = "WIN" if item["result"] == "W" else ("LOSS" if item["result"] == "L" else "PUSH")
        _send_push_to_all(
            title=f"{emoji} {item['pick'].split()[-1]} — {label}",
            body=f"{item['game']} · Result recorded",
            url="/"
        )

    return jsonify({
        "ok":      True,
        "date":    today,
        "updated": updated,
        "details": details,
        "finals":  len(final_games),
        "pending": len(pending),
    })


# ── Void a pick ──────────────────────────────────────────────────────────────────
@app.route("/api/void_pick", methods=["POST"])
@require_api_secret
def api_void_pick():
    data      = request.get_json(force=True, silent=True) or {}
    away_team = data.get("away_team", "")
    home_team = data.get("home_team", "")
    today     = date.today().isoformat()
    if not away_team or not home_team:
        return jsonify({"ok": False, "error": "Missing teams"}), 400
    supabase.table("picks").update({"result": "V"}).eq("date", today).eq("away_team", away_team).eq("home_team", home_team).execute()
    return jsonify({"ok": True})


# ── Push notifications ────────────────────────────────────────────────────────────
@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    data   = request.get_json(force=True, silent=True) or {}
    sub    = data.get("subscription", {})
    name   = data.get("name", "")
    endpoint = sub.get("endpoint", "")
    keys     = sub.get("keys", {})
    p256dh   = keys.get("p256dh", "")
    auth     = keys.get("auth", "")
    if not endpoint or not p256dh or not auth:
        return jsonify({"ok": False, "error": "Missing subscription fields"}), 400
    supabase.table("push_subscriptions").upsert({
        "endpoint": endpoint, "p256dh": p256dh,
        "auth": auth, "user_name": name,
    }, on_conflict="endpoint").execute()
    return jsonify({"ok": True})


def _send_push_to_all(title, body, url="/"):
    """Send a push notification to all active subscribers."""
    try:
        from pywebpush import webpush, WebPushException
        subs = supabase.table("push_subscriptions").select("*").execute().data or []
        dead = []
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub["endpoint"],
                        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                    },
                    data=json.dumps({"title": title, "body": body, "url": url}),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS,
                )
            except Exception as e:
                if "410" in str(e) or "404" in str(e):
                    dead.append(sub["endpoint"])
        for ep in dead:
            supabase.table("push_subscriptions").delete().eq("endpoint", ep).execute()
    except Exception:
        pass   # never crash the caller


@app.route("/api/push/send", methods=["POST"])
@require_api_secret
def api_push_send():
    data  = request.get_json(force=True, silent=True) or {}
    title = data.get("title", "⚾ MLB Picks Bot")
    body  = data.get("body", "")
    url   = data.get("url", "/")
    _send_push_to_all(title, body, url)
    return jsonify({"ok": True})


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
