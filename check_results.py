#!/usr/bin/env python3
"""
check_results.py — MLB Picks Bot: Live Result Checker
─────────────────────────────────────────────────────
Runs every 30 mins via PebbleHost cron (noon–1am EST).
  1. Fetches today's pending picks from the dashboard API
  2. Checks the MLB Stats API for any Final games today
  3. Determines W / L / P for each completed pick
  4. Posts results back to the dashboard (which updates Supabase)

No Supabase credentials needed — uses the dashboard's public
/api/today endpoint + authenticated /api/update_results endpoint.
"""

import os
import sys
import logging
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://mlb-dashboard-htn3.onrender.com")
API_SECRET    = os.environ.get("API_SECRET",    "mlbpicks2026")

MLB_API_BASE  = "https://statsapi.mlb.com/api/v1"
HEADERS       = {"User-Agent": "Mozilla/5.0 (compatible; mlb-result-checker/1.0)"}
TIMEOUT       = 20

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def today_est() -> str:
    """Return today's date in EST as YYYY-MM-DD."""
    est = timezone(timedelta(hours=-5))
    return datetime.now(est).strftime("%Y-%m-%d")


def fetch_pending_picks(date: str) -> list:
    """GET /api/today and return picks that still have no result."""
    url = f"{DASHBOARD_URL.rstrip('/')}/api/today"
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        picks = resp.json()
    except Exception as e:
        log.error("Could not fetch today's picks: %s", e)
        return []

    pending = [p for p in picks if not (p.get("result") or "").strip()]
    log.info("Today has %d pick(s), %d still pending.", len(picks), len(pending))
    return pending


def fetch_final_games(date: str) -> dict:
    """
    Query the MLB Stats API for today's completed games.
    Returns a dict keyed by (away_team, home_team) → winning_team | "TIE"
    Only includes games whose status is 'Final'.
    """
    url = (
        f"{MLB_API_BASE}/schedule"
        f"?sportId=1&startDate={date}&endDate={date}"
        f"&hydrate=linescore,game&gameType=R"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("MLB API error: %s", e)
        return {}

    results = {}
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            status = game.get("status", {})
            if status.get("abstractGameState") != "Final":
                continue

            innings = game.get("linescore", {}).get("innings", [])
            if len(innings) < 5:
                continue   # suspended / incomplete

            away = game["teams"]["away"]["team"]["name"]
            home = game["teams"]["home"]["team"]["name"]

            away_runs = sum(i.get("away", {}).get("runs", 0) for i in innings)
            home_runs = sum(i.get("home", {}).get("runs", 0) for i in innings)

            if away_runs > home_runs:
                winner = away
            elif home_runs > away_runs:
                winner = home
            else:
                winner = "TIE"

            results[(away, home)] = winner
            log.info("Final: %s %d – %s %d → %s",
                     away, away_runs, home, home_runs, winner)

    log.info("Found %d final game(s) so far today.", len(results))
    return results


def determine_result(pick: dict, final_games: dict) -> str | None:
    """
    Returns 'W', 'L', 'P', or None (game not finished yet).
    """
    away   = pick.get("away_team", "")
    home   = pick.get("home_team", "")
    chosen = pick.get("pick", "")

    winner = final_games.get((away, home))
    if winner is None:
        return None          # game not final yet

    if winner == "TIE":
        return "P"
    elif winner == chosen:
        return "W"
    else:
        return "L"


def post_results(date: str, results: list) -> bool:
    """POST completed results to the dashboard /api/update_results endpoint."""
    if not results:
        return True

    url     = f"{DASHBOARD_URL.rstrip('/')}/api/update_results"
    payload = {"date": date, "results": results}
    headers = {
        "X-API-Secret":  API_SECRET,
        "Content-Type":  "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        log.info("Dashboard updated: %s", data)
        return True
    except Exception as e:
        log.error("Failed to post results to dashboard: %s", e)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    date = today_est()
    log.info("=== Result checker running for %s ===", date)

    pending = fetch_pending_picks(date)
    if not pending:
        log.info("No pending picks — nothing to do.")
        return

    final_games = fetch_final_games(date)
    if not final_games:
        log.info("No final games yet — will try again next run.")
        return

    completed = []
    for pick in pending:
        result = determine_result(pick, final_games)
        if result is None:
            log.info("  %-25s vs %-25s — still in progress",
                     pick.get("away_team"), pick.get("home_team"))
            continue

        log.info("  %-25s vs %-25s → %s (picked: %s)",
                 pick.get("away_team"), pick.get("home_team"),
                 result, pick.get("pick"))
        completed.append({
            "away_team": pick["away_team"],
            "home_team": pick["home_team"],
            "result":    result,
        })

    if completed:
        log.info("Posting %d result(s) to dashboard...", len(completed))
        post_results(date, completed)
    else:
        log.info("No new results to post yet.")

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
