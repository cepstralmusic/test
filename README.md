# Edinburgh Hogmanay monitor: 4★+ hotels and Underworld tickets

Two monitors share one database:

- **`monitor.py`**: hotel prices. See [REPORT.md](REPORT.md).
- **`tickets.py`**: *Hogmanay in the Gardens with Underworld* tickets from the official box office. See [TICKETS.md](TICKETS.md).

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

## Underworld tickets (`tickets.py`)

Reads the official box office feed for edwinterfest.com, the Edinburgh's Hogmanay ticket site. This is the same
public data the booking page loads. Regular prices are set in `tickets.json`. They include fees and the £1 donation.

| Tier | Regular price | Status at first check (20 Nov) |
|---|---:|---|
| Gardens | £79 | On sale, 25% left |
| Enclosure | £89 | Sold out |
| Premium | £180 | Sold out |

| Alert | Fires when |
|---|---|
| `BELOW REGULAR PRICE` | A tier is on sale for less than its regular price |
| `BACK ON SALE` | A sold-out tier becomes available again, for example from returns or a new release |
| `SELLING OUT` | A tier on sale drops to `low_stock_percent` (default 10%) or less |
| `SOLD OUT` / `PRICE CHANGE` | Status or price changes |

Resale sites (Twickets, StubHub, viagogo) are **not** checked.

```bash
python3 tickets.py                # check and write TICKETS.md
python3 tickets.py --report-only
```

## Scheduled runs

`.github/workflows/price-monitor.yml` checks tickets and hotel prices once a day at 07:17 UTC.
It commits the updated database and reports, and shows alerts as warnings on the run. You can also start it from the Actions tab
with "Run workflow". GitHub only runs scheduled workflows from the repository's **default branch**.

Useful queries:

```sql
-- price history for one hotel and stay
SELECT r.run_at, MIN(q.rate) FROM quotes q JOIN runs r ON r.id = q.run_id
WHERE q.hotel LIKE 'The Caledonian%' AND q.stay_label LIKE 'NYE 2%' GROUP BY r.id;
```
