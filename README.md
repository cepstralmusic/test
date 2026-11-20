# Edinburgh Hogmanay hotel price monitor (4★+)

Tracks live prices at 47 four- and five-star Edinburgh hotels for New Year's Eve 2026.
Each run stores a snapshot and rewrites **[REPORT.md](REPORT.md)** with the current prices.

## How it works

- **Data source:** the free [Xotelo](https://xotelo.com) API, which returns the rates TripAdvisor
  shows from Booking.com, Agoda, Expedia, Trip.com, Vio.com and hotel-direct sites. No API key needed.
- **Prices** are the cheapest **average rate per night** across those sites, in GBP.
  Taxes and fees can vary by site.
- **Stays checked** (edit `stays` in `hotels.json`):
  - 31 Dec → 1 Jan (1 night)
  - 30 Dec → 1 Jan (2 nights)
  - 30 Dec → 2 Jan (3 nights)
- **"No rates"** means the hotel is sold out for those dates or has a minimum stay.
  Many top hotels require 2–3 nights over Hogmanay.
- **Star ratings** in `hotels.json` were set by hand from published classifications.
  The API does not provide star ratings.
- History is kept in `data/prices.db` (SQLite). The `quotes` table has one row per hotel, stay, site and run.

## Alerts

Alerts start from the second run. They are printed as `ALERT:` lines and listed at the top of the report.

| Alert | Fires when |
|---|---|
| `PRICE DROP` | A hotel's cheapest rate falls by at least `drop_percent` (default 5%) |
| `NOW UNDER £X` | A rate falls below `max_price_per_night` (default £400) |
| `NOW AVAILABLE` | A sold-out hotel or stay shows prices again |

Rates under 40% of the median for that stay are marked ⚠️. These are usually restricted room types. Check them before booking.

## Usage

```bash
python3 monitor.py                # fetch prices, store a snapshot, write REPORT.md
python3 monitor.py --stars 5      # 5-star hotels only
python3 monitor.py --report-only  # rebuild the report from stored data
```

Python 3.9+ and the standard library only.

## Scheduled runs

`.github/workflows/price-monitor.yml` runs twice a day (06:17 and 18:17 UTC). It commits the updated
database and report, and shows alerts as warnings on the run. You can also start it from the Actions tab
with "Run workflow". GitHub only runs scheduled workflows from the repository's **default branch**.

Useful queries:

```sql
-- price history for one hotel and stay
SELECT r.run_at, MIN(q.rate) FROM quotes q JOIN runs r ON r.id = q.run_id
WHERE q.hotel LIKE 'The Caledonian%' AND q.stay_label LIKE 'NYE 2%' GROUP BY r.id;
```
