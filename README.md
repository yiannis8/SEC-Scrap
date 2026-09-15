# Marex · SEC note issuance monitor

Streamlit front end over `edgar_notes.py`. Pick a single date or a date range and get
the per-issuer breakdown, an issuer-mix pie, payoff/asset-class mix, and a drill-down
into the individual products — styled to the Marex design system.

## Run

```bash
pip install -r requirements.txt
export EDGAR_UA="Your Name your@marex.com"     # EDGAR rejects generic user agents
streamlit run app.py
```

## Getting data in

The app reads `edgar_notes.sqlite` (override with `EDGAR_DB=...`). Populate it either way:

* **CLI** (best for a backfill): `python edgar_notes.py --start 2026-07-01 --end 2026-09-15 --workers 5`
* **In-app**: sidebar → *Refresh from EDGAR* → *Refresh selected period*. Only fetches
  filings not already cached, so a single day is a few minutes; a quarter is hours.

Both write to the same cache and are resumable.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI |
| `edgar_data.py` | Loads/merges the SQLite cache; windowed refresh with progress callback |
| `edgar_notes.py` | Your scraper, unchanged |
| `.streamlit/config.toml` | Marex theme tokens |

## Design notes

* Purple (`#793690`) is used only for buttons, links and focus states per the brand rule
  ("never a background or large fill"). Chart slices use the sector palette + dark blue;
  the "Other" bucket is always light grey.
* Filing date is the default date basis because it is always present; trade/pricing and
  issue dates are parsed from cover pages and can be missing.
* Preliminary and final supplements for the same ISIN are merged (final wins on size).
