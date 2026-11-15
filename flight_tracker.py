#!/usr/bin/env python3
"""
Single-trip flight price tracker.

Checks one round trip on Google Flights (through SerpApi), saves every result
to data/history.csv, and sends a phone notification (ntfy.sh) when:
  * it's the first check (so you know it's working),
  * the price hits a new low (by at least min_drop_to_alert),
  * the price drops to or below your target_price,
  * Google starts calling the price "low" for this route.

Standard library only - nothing to pip install.

Environment variables:
  SERPAPI_KEY   your SerpApi API key (required)
  NTFY_TOPIC    your ntfy.sh topic name (optional - without it, results only print)

Usage:
  python flight_tracker.py              normal check (uses 1 search, 2 on some alerts)
  python flight_tracker.py --test-alert send a test notification, uses no searches
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
DATA_DIR = ROOT / "data"
HISTORY_FILE = DATA_DIR / "history.csv"
STATE_FILE = DATA_DIR / "state.json"

SERPAPI_URL = "https://serpapi.com/search.json"
NTFY_URL = "https://ntfy.sh/"

HISTORY_FIELDS = [
    "checked_at_utc", "price", "airline", "flight_numbers", "depart_time",
    "arrive_time", "stops", "google_price_level", "google_typical_low",
    "google_typical_high",
]


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def http_get_json(url, params, timeout=60):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "flight-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            msg = body
        raise RuntimeError(f"SerpApi HTTP {e.code}: {msg}") from None


def notify(title, message, click_url=None, priority="default", tags="airplane"):
    """Push a notification to your phone through ntfy.sh (free, no account)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    print(f"\n=== {title} ===\n{message}\n")
    if not topic:
        print("(NTFY_TOPIC not set - notification printed only)")
        return
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        NTFY_URL + urllib.parse.quote(topic),
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:  # a failed push shouldn't kill the price check
        print(f"WARNING: notification failed: {e}")


def money(value, currency="USD"):
    if value is None:
        return "n/a"
    return f"${value:,.0f}" if currency == "USD" else f"{value:,.0f} {currency}"


# ---------------------------------------------------------------- SerpApi

def search_flights(cfg, api_key, departure_token=None):
    params = {
        "engine": "google_flights",
        "departure_id": cfg["departure_id"],
        "arrival_id": cfg["arrival_id"],
        "outbound_date": cfg["outbound_date"],
        "return_date": cfg["return_date"],
        "type": "1",                       # 1 = round trip
        "adults": str(cfg.get("adults", 1)),
        "stops": str(cfg.get("stops", 0)),  # 0 any, 1 nonstop, 2 <=1 stop, 3 <=2 stops
        "currency": cfg.get("currency", "USD"),
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    if cfg.get("include_airlines"):
        params["include_airlines"] = cfg["include_airlines"]
    if cfg.get("exclude_basic"):
        params["exclude_basic"] = "true"   # only fares with seat selection + carry-on
        params["travel_class"] = "1"       # economy (required for exclude_basic)
    if departure_token:
        params["departure_token"] = departure_token
    data = http_get_json(SERPAPI_URL, params)
    if data.get("error"):
        raise RuntimeError(f"SerpApi error: {data['error']}")
    return data


def all_options(data):
    return (data.get("best_flights") or []) + (data.get("other_flights") or [])


def cheapest(options):
    priced = [o for o in options if isinstance(o.get("price"), (int, float))]
    return min(priced, key=lambda o: o["price"]) if priced else None


def describe(option):
    """Pull the readable bits out of one itinerary."""
    legs = option.get("flights") or []
    airlines = []
    for leg in legs:
        name = leg.get("airline")
        if name and name not in airlines:
            airlines.append(name)
    return {
        "airline": " / ".join(airlines) or "?",
        "flight_numbers": ", ".join(l.get("flight_number", "?") for l in legs),
        "depart_time": (legs[0].get("departure_airport") or {}).get("time", "") if legs else "",
        "arrive_time": (legs[-1].get("arrival_airport") or {}).get("time", "") if legs else "",
        "stops": max(len(legs) - 1, 0),
    }


def fmt_leg(d):
    stops = "nonstop" if d["stops"] == 0 else f"{d['stops']} stop(s)"
    return f"{d['airline']} {d['flight_numbers']}, {d['depart_time']} -> {d['arrive_time']} ({stops})"


# ---------------------------------------------------------------- budget

def budget_ok(state, cfg, needed=1):
    """Local monthly counter so we never go past the free tier."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("month") != month:
        state["month"] = month
        state["requests_this_month"] = 0
    cap = int(cfg.get("monthly_request_cap", 200))
    return state["requests_this_month"] + needed <= cap


def count_request(state):
    state["requests_this_month"] = state.get("requests_this_month", 0) + 1


# ---------------------------------------------------------------- main

def main():
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit(f"Could not read {CONFIG_FILE}")
    currency = cfg.get("currency", "USD")
    trip = (f"{cfg['departure_id']} -> {cfg['arrival_id']}, "
            f"{cfg['outbound_date']} to {cfg['return_date']}")

    if "--test-alert" in sys.argv:
        notify(f"Test: {cfg.get('trip_name', 'Flight tracker')}",
               f"Notifications are working for {trip}.")
        return

    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not api_key:
        sys.exit("SERPAPI_KEY is not set.")

    state = load_json(STATE_FILE, {})
    if not budget_ok(state, cfg):
        print(f"Monthly cap of {cfg.get('monthly_request_cap')} searches reached; "
              "skipping this check to stay on the free plan.")
        save_json(STATE_FILE, state)
        return

    # --- search
    data = search_flights(cfg, api_key)
    count_request(state)

    best = cheapest(all_options(data))
    insights = data.get("price_insights") or {}
    level = insights.get("price_level")
    typical = insights.get("typical_price_range") or [None, None]
    google_url = (data.get("search_metadata") or {}).get("google_flights_url")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    if best is None:
        print("No priced flights returned (schedule may not be loaded yet).")
        state["last_checked_utc"] = now
        save_json(STATE_FILE, state)
        return

    price = best["price"]
    info = describe(best)

    # --- history
    new_file = not HISTORY_FILE.exists()
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({"checked_at_utc": now, "price": price, **info,
                    "google_price_level": level or "",
                    "google_typical_low": typical[0] if len(typical) > 0 else "",
                    "google_typical_high": typical[1] if len(typical) > 1 else ""})

    print(f"{now} UTC  cheapest {money(price, currency)}  {fmt_leg(info)}  "
          f"[Google: {level or 'n/a'}]")

    # --- decide whether to alert
    prev_low = state.get("lowest_price")
    prev_price = state.get("last_price")
    prev_level = state.get("last_price_level")
    target = cfg.get("target_price")
    min_drop = cfg.get("min_drop_to_alert", 5)

    reasons = []
    if prev_low is None:
        reasons.append("Tracking started")
    elif price <= prev_low - min_drop:
        reasons.append(f"New low (was {money(prev_low, currency)})")
    if target is not None and price <= target and (prev_price is None or prev_price > target):
        reasons.append(f"At or below your target of {money(target, currency)}")
    near = cfg.get("near_target_margin")
    if target is not None and near and target < price <= target + near and \
            (prev_price is None or prev_price > target + near):
        reasons.append(f"Getting close: within {money(near, currency)} of your target")
    if level == "low" and prev_level not in (None, "low"):
        reasons.append("Google now rates this price as LOW")

    # --- update state
    if prev_low is None or price < prev_low:
        state["lowest_price"] = price
        state["lowest_seen_utc"] = now
    state["last_price"] = price
    state["last_price_level"] = level
    state["last_checked_utc"] = now

    if reasons:
        lines = [
            f"{money(price, currency)} round trip "
            f"(total for {cfg.get('adults', 1)} traveler(s))",
            f"Out: {fmt_leg(info)}",
        ]

        # One extra search to show the matching return flight - only on alerts.
        token = best.get("departure_token")
        if (cfg.get("fetch_return_details_on_alert", True) and token
                and budget_ok(state, cfg)):
            try:
                ret = cheapest(all_options(search_flights(cfg, api_key, token)))
                count_request(state)
                if ret:
                    lines.append(f"Back: {fmt_leg(describe(ret))}")
            except Exception as e:
                print(f"WARNING: return-flight lookup failed: {e}")

        if typical[0] is not None:
            lines.append(f"Google's typical range: {money(typical[0], currency)}"
                         f"-{money(typical[1], currency)} ({level or 'n/a'})")
        lines.append(f"Lowest seen: {money(state['lowest_price'], currency)}")
        lines.append(f"Searches used this month: {state['requests_this_month']}")

        if target is not None:
            lines.append(f"Your target: {money(target, currency)} "
                         f"(about {money(target / cfg.get('adults', 1), currency)} each)")
        is_good = any(r.startswith(("New low", "At or below", "Google now", "Getting close"))
                      for r in reasons)
        notify(f"{cfg.get('trip_name', 'Flights')}: " + "; ".join(reasons),
               "\n".join(lines), click_url=google_url,
               priority="high" if is_good else "default",
               tags="money_with_wings" if is_good else "airplane")

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()