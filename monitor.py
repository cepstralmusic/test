#!/usr/bin/env python3
"""Edinburgh New Year's Eve hotel price monitor (4-star and above).

Fetches live rates from the free Xotelo API (which aggregates the OTA prices
TripAdvisor shows: Booking.com, Agoda, Expedia, Trip.com, hotel direct, ...)
for every hotel and stay window in hotels.json, stores each run in SQLite,
and writes a Markdown report comparing against the previous run.

Standard library only. Usage:
    python3 monitor.py                 # fetch, store, report
    python3 monitor.py --report-only   # rebuild report from stored data
    python3 monitor.py --stars 5       # only 5-star hotels
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

API = "https://data.xotelo.com/api/rates"
ROOT = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    hotel_key TEXT NOT NULL,
    hotel TEXT NOT NULL,
    stars INTEGER NOT NULL,
    stay_label TEXT NOT NULL,
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL,
    provider TEXT,          -- NULL row = queried, no availability
    rate REAL,              -- average price per night
    currency TEXT NOT NULL,
    status TEXT NOT NULL    -- ok | unavailable | error
);
CREATE INDEX IF NOT EXISTS quotes_lookup ON quotes(hotel_key, stay_label, run_id);
"""


def fetch_rates(hotel_key: str, check_in: str, check_out: str, currency: str,
                retries: int = 3) -> tuple[str, list[dict]]:
    qs = urllib.parse.urlencode({"hotel_key": hotel_key, "chk_in": check_in,
                                 "chk_out": check_out, "currency": currency})
    req = urllib.request.Request(f"{API}?{qs}", headers={"User-Agent": "hotel-price-monitor/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.load(resp)
            if body.get("error"):
                raise RuntimeError(body["error"])
            rates = (body.get("result") or {}).get("rates") or []
            return ("ok" if rates else "unavailable"), rates
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                print(f"  ! {hotel_key} {check_in}->{check_out}: {exc}", file=sys.stderr)
                return "error", []
            time.sleep(2 * (attempt + 1))
    return "error", []


def run_fetch(cfg: dict, hotels: list[dict], db: sqlite3.Connection) -> int:
    currency = cfg.get("currency", "GBP")
    jobs = [(h, s) for h in hotels for s in cfg["stays"]]
    print(f"Fetching {len(jobs)} quotes ({len(hotels)} hotels x {len(cfg['stays'])} stays)...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda j: fetch_rates(j[0]["key"], j[1]["check_in"], j[1]["check_out"], currency), jobs))

    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cur = db.execute("INSERT INTO runs(run_at) VALUES (?)", (run_at,))
    run_id = cur.lastrowid
    rows = []
    for (h, s), (status, rates) in zip(jobs, results):
        base = (run_id, h["key"], h["name"], h["stars"], s["label"], s["check_in"], s["check_out"])
        if rates:
            rows += [base + (r.get("name") or r.get("code"), float(r["rate"]), currency, status)
                     for r in rates if r.get("rate")]
        else:
            rows.append(base + (None, None, currency, status))
    db.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    ok = sum(1 for st, _ in results if st == "ok")
    err = sum(1 for st, _ in results if st == "error")
    print(f"Run {run_id} stored: {ok} priced, {len(results) - ok - err} unavailable, {err} errors.")
    return run_id


def best_quotes(db: sqlite3.Connection, run_id: int) -> dict:
    """(hotel_key, stay_label) -> {hotel, stars, rate, provider, status, n_providers}"""
    out = {}
    for key, hotel, stars, label, provider, rate, status in db.execute(
            "SELECT hotel_key, hotel, stars, stay_label, provider, rate, status "
            "FROM quotes WHERE run_id=?", (run_id,)):
        cur = out.setdefault((key, label), {"hotel": hotel, "stars": stars, "rate": None,
                                            "provider": None, "status": status, "n": 0})
        if rate is not None:
            cur["n"] += 1
            if cur["rate"] is None or rate < cur["rate"]:
                cur["rate"], cur["provider"] = rate, provider
    return out


def nights(stay: dict) -> int:
    return (date.fromisoformat(stay["check_out"]) - date.fromisoformat(stay["check_in"])).days


def build_report(cfg: dict, db: sqlite3.Connection, stars_filter: int | None) -> tuple[str, list[str]]:
    runs = db.execute("SELECT id, run_at FROM runs ORDER BY id DESC LIMIT 2").fetchall()
    if not runs:
        return "No data yet. Run `python3 monitor.py` first.\n", []
    (run_id, run_at), prev = runs[0], (runs[1] if len(runs) > 1 else None)
    now = best_quotes(db, run_id)
    before = best_quotes(db, prev[0]) if prev else {}
    first_seen = dict(db.execute(
        "SELECT hotel_key || '|' || stay_label, MIN(rate) FROM quotes GROUP BY hotel_key, stay_label"))
    sym = {"GBP": "£", "EUR": "€", "USD": "$"}.get(cfg.get("currency", "GBP"), "")
    cap = cfg.get("alerts", {}).get("max_price_per_night")
    drop_pct = cfg.get("alerts", {}).get("drop_percent", 10)
    alerts: list[str] = []

    lines = [f"# Edinburgh Hogmanay hotel prices — 4★ and above", "",
             f"Last checked **{run_at}**"
             + (f" (compared with {prev[1]})" if prev else " (first run)") + ".  ",
             "Prices are the cheapest rate per night across booking sites, "
             f"in {cfg.get('currency', 'GBP')}. Source: Xotelo / TripAdvisor rate feed.", ""]

    for stay in cfg["stays"]:
        n = nights(stay)
        entries = [(k, v) for (k, label), v in now.items() if label == stay["label"]
                   and (stars_filter is None or v["stars"] >= stars_filter)]
        priced = sorted((e for e in entries if e[1]["rate"] is not None), key=lambda e: e[1]["rate"])
        unpriced = sorted((e for e in entries if e[1]["rate"] is None), key=lambda e: (-e[1]["stars"], e[1]["hotel"]))

        lines += [f"## {stay['label']}: {stay['check_in']} → {stay['check_out']} ({n} night{'s' * (n > 1)})", ""]
        if priced:
            lines += ["| # | Hotel | ★ | Per night | Total | Cheapest via | Change | Lowest seen |",
                      "|---|---|---|---:|---:|---|---|---:|"]
        rates = sorted(v["rate"] for _, v in priced)
        median = rates[len(rates) // 2] if rates else 0
        for i, (key, v) in enumerate(priced, 1):
            prev_q = before.get((key, stay["label"]))
            old = prev_q.get("rate") if prev_q else None
            change = "new" if prev and old is None else "–"
            if old is not None:
                diff = v["rate"] - old
                change = "–" if abs(diff) < 0.5 else f"{'▼' if diff < 0 else '▲'} {sym}{abs(diff):.0f} ({diff / old:+.0%})"
                if diff < 0 and -diff / old * 100 >= drop_pct:
                    alerts.append(f"PRICE DROP {v['hotel']} [{stay['label']}]: {sym}{old:.0f} → {sym}{v['rate']:.0f}/night via {v['provider']}")
            low = first_seen.get(f"{key}|{stay['label']}")
            if prev_q and old is None:
                alerts.append(f"NOW AVAILABLE {v['hotel']} ({v['stars']}★) [{stay['label']}]: {sym}{v['rate']:.0f}/night via {v['provider']}")
            elif cap and v["rate"] <= cap and (old is None or old > cap) and prev:
                alerts.append(f"NOW UNDER {sym}{cap} {v['hotel']} ({v['stars']}★) [{stay['label']}]: {sym}{v['rate']:.0f}/night via {v['provider']}")
            odd = " ⚠️" if v["rate"] < 0.4 * median else ""
            lines.append(f"| {i} | {v['hotel']}{odd} | {v['stars']} | {sym}{v['rate']:.0f} | {sym}{v['rate'] * n:,.0f} | "
                         f"{v['provider']} | {change} | {sym}{low:.0f} |")
        if unpriced:
            names = ", ".join(f"{v['hotel']} ({v['stars']}★)" + (" — fetch error" if v["status"] == "error" else "")
                              for _, v in unpriced)
            lines += ["", f"**No rates (sold out or minimum-stay rule):** {names}"]
        if any(v["rate"] < 0.4 * median for _, v in priced):
            lines += ["", "⚠️ = under 40% of the median for this stay; likely a restricted room type "
                      "(non-refundable, no window, etc.). Check before booking."]
        lines.append("")

    if not prev:
        alerts = []  # first run is the baseline; alerts start from the next run
    if alerts:
        lines[5:5] = ["## Alerts", ""] + [f"- {a}" for a in alerts] + [""]
    return "\n".join(lines), alerts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=ROOT / "hotels.json", type=Path)
    ap.add_argument("--db", default=ROOT / "data" / "prices.db", type=Path)
    ap.add_argument("--report", default=ROOT / "REPORT.md", type=Path)
    ap.add_argument("--stars", type=int, choices=[4, 5], help="minimum star rating (default 4)")
    ap.add_argument("--report-only", action="store_true", help="skip fetching; rebuild report")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    hotels = [h for h in cfg["hotels"] if h["stars"] >= (args.stars or 4)]
    args.db.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)

    if not args.report_only:
        run_fetch(cfg, hotels, db)
    report, alerts = build_report(cfg, db, args.stars)
    args.report.write_text(report)
    print(f"Report written to {args.report}")
    for a in alerts:
        print("ALERT:", a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
