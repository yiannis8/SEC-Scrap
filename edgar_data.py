"""
edgar_data.py — data layer between edgar_notes.py (the crawler) and app.py.

Two jobs:
  * load_notes()  — flatten the SQLite `parsed` cache into one tidy DataFrame,
                    applying the same preliminary/final merge as the CLI export.
  * refresh()     — run enumerate → resolve → crawl for a date window, reporting
                    progress through a callback so the app can show a status bar.

edgar_notes.py itself is untouched; everything here calls into it.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd

import edgar_notes as en

DATE_COLS = ["filed", "trade_date", "issue_date", "final_val_date",
             "maturity_date", "first_call_date"]

COLS = en.COLS + ["filings"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _merge_prelim_final(rows: list[dict]) -> list[dict]:
    """One row per ISIN/CUSIP; a note filed preliminary then final appears
    twice in the cache. Field-wise merge, final filing wins on priced fields.
    Mirrors edgar_notes.export(dedupe=True) — with one correction: the
    generic merge below skips any value equal to False (treating it as
    "empty", which is right for text fields but wrong for the `preliminary`
    flag itself). Left as-is, a note that was ever filed preliminary would
    stay flagged preliminary=True forever, even after a final priced filing
    for the same ISIN arrives, silently breaking any "exclude preliminary"
    filter. `has_final` is tracked separately and applied after the loop so
    it can't be skipped by that rule.
    """
    merged: dict[str, dict] = {}
    has_final: dict[str, bool] = {}
    loose: list[dict] = []
    for r in rows:
        key = r.get("isin") or r.get("cusip")
        if not key:
            r["filings"] = 1
            loose.append(r)
            continue
        m = merged.setdefault(key, {})
        final = not r.get("preliminary")
        has_final[key] = has_final.get(key, False) or final
        for k, v in r.items():
            if k == "preliminary" or v in (None, "", False):
                continue
            if k not in m or m[k] in (None, "", False) or (
                final and k in ("size_usd", "url", "filed",
                                "accession", "estimated_initial_value")):
                m[k] = v
        m["filings"] = m.get("filings", 0) + 1
    for key, m in merged.items():
        m["preliminary"] = not has_final[key]
    return list(merged.values()) + loose


def load_notes(db_path: str, dedupe: bool = True) -> pd.DataFrame:
    """All parsed notes as a DataFrame with proper dtypes. Empty frame with the
    right columns and dtypes if the database does not exist yet — this must
    stay on the same code path as the non-empty case below, since an empty
    DataFrame with object-dtype boolean columns silently drops all columns
    (not just rows) under boolean indexing in pandas."""
    rows: list[dict] = []
    if os.path.exists(db_path):
        con = en.db_connect(db_path)
        try:
            rows = [json.loads(p) for (p,) in con.execute("SELECT payload FROM parsed")]
        finally:
            con.close()
    if dedupe:
        rows = _merge_prelim_final(rows)
    df = pd.DataFrame(rows)
    for c in COLS:
        if c not in df:
            df[c] = None
    df = df[COLS].copy()
    for c in DATE_COLS:
        df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    df["size_usd"] = pd.to_numeric(df["size_usd"], errors="coerce")
    df["tenor_years"] = pd.to_numeric(df["tenor_years"], errors="coerce")
    df["preliminary"] = df["preliminary"].fillna(False).astype(bool)
    df["structured"] = df["structured"].fillna(False).astype(bool)
    df["issuer"] = df["issuer"].fillna("Unknown").map(clean_issuer)
    return df


_SUFFIX = re.compile(r"[,/]?\s*\b(INC|CORP|CO|LTD|PLC|AG|SA|NV|LLC|N\.?A\.?|"
                     r"DE|PLC)\b\.?\s*$", re.I)


def clean_issuer(name: str) -> str:
    """EDGAR filer names are shouty ('JPMORGAN CHASE FINANCIAL CO. LLC').
    Title-case them and drop trailing legal suffixes for display."""
    s = str(name).strip()
    for _ in range(2):
        s = _SUFFIX.sub("", s).strip(" ,.")
    words = [w if (w.isupper() and len(w) <= 3) else w.capitalize() for w in s.split()]
    return " ".join(_ACRONYMS.get(w, w) for w in words)


_ACRONYMS = {"Jpmorgan": "JPMorgan", "Ubs": "UBS", "Hsbc": "HSBC", "Bnp": "BNP",
             "Rbc": "RBC", "Td": "TD", "Cibc": "CIBC", "Gs": "GS", "Bmo": "BMO",
             "Ag": "AG", "Usa": "USA", "Nv": "NV", "Sa": "SA"}


def db_status(db_path: str) -> dict:
    """Coverage summary used in the sidebar."""
    if not os.path.exists(db_path):
        return {"exists": False}
    con = en.db_connect(db_path)
    try:
        filings, = con.execute("SELECT COUNT(*) FROM filings").fetchone()
        parsed, = con.execute("SELECT COUNT(*) FROM parsed").fetchone()
        failed, = con.execute("SELECT COUNT(*) FROM failed").fetchone()
        lo, hi = con.execute("SELECT MIN(filed), MAX(filed) FROM filings").fetchone()
    finally:
        con.close()
    return {"exists": True, "filings": filings, "parsed": parsed,
            "failed": failed, "first": lo, "last": hi,
            "mtime": os.path.getmtime(db_path)}


# --------------------------------------------------------------------------- #
# Refreshing
# --------------------------------------------------------------------------- #

def set_user_agent(ua: str) -> None:
    """EDGAR rejects generic agents. Push the UA into the crawler and into any
    session that was already created on this thread."""
    en.UA = ua
    s = getattr(en._local, "s", None)
    if s is not None:
        s.headers["User-Agent"] = ua


DAILY_INDEX_MAX_DAYS = 14   # below this, per-day index files beat the ~50 MB quarterly one


def _index_rows(txt: str, start: date, end: date, forms: set[str],
                issuer_re: re.Pattern | None) -> list[tuple]:
    """Parse a master.idx body (quarterly or daily — same pipe format)."""
    rows = []
    for line in txt.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, name, form, filed, fname = (p.strip() for p in parts)
        if form not in forms:
            continue
        try:
            fdate = datetime.strptime(filed, "%Y-%m-%d").date()
        except ValueError:
            # daily files use YYYYMMDD
            try:
                fdate = datetime.strptime(filed, "%Y%m%d").date()
            except ValueError:
                continue
        if not (start <= fdate <= end):
            continue
        if issuer_re and not issuer_re.search(name):
            continue
        m = re.search(r"(\d{10}-\d{2}-\d{6})", fname)
        if not m:
            continue
        rows.append((m.group(1), cik, name, form, fdate.isoformat(), None, None))
    return rows


def enumerate_daily(con, start: date, end: date, forms: set[str],
                    issuer_re: re.Pattern | None,
                    log: Callable[[str], None]) -> int:
    """Populate `filings` from EDGAR's daily index files, one per business day.
    Weekends/holidays 404 and are skipped."""
    added = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            q = (d.month - 1) // 3 + 1
            url = (f"{en.SEC}/Archives/edgar/daily-index/{d.year}/QTR{q}/"
                   f"master.{d:%Y%m%d}.idx")
            txt = en.fetch(url)
            if txt:
                rows = _index_rows(txt, start, end, forms, issuer_re)
                with con:
                    cur = con.executemany(
                        "INSERT OR IGNORE INTO filings "
                        "(accession,cik,issuer,form,filed,primary_doc,doc_url) "
                        "VALUES (?,?,?,?,?,?,?)", rows)
                n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                added += n
                log(f"{d:%a %d %b}: {len(rows)} pricing supplements filed")
            else:
                log(f"{d:%a %d %b}: no index (holiday or not yet published)")
        d += timedelta(days=1)
    return added


def window_cached(db_path: str, start: date, end: date) -> bool:
    """True if the filings table already has anything filed in this window —
    used by the app to decide whether an automatic fetch is needed."""
    if not os.path.exists(db_path):
        return False
    con = en.db_connect(db_path)
    try:
        n, = con.execute("SELECT COUNT(*) FROM filings WHERE filed BETWEEN ? AND ?",
                         (start.isoformat(), end.isoformat())).fetchone()
    finally:
        con.close()
    return n > 0


def refresh(db_path: str, start: date, end: date, ua: str,
            workers: int = 4, structured_forms: str = "424B2",
            issuer_re: str | None = None,
            progress: Callable[[str], None] | None = None) -> dict:
    """Enumerate, resolve and crawl the window. Resumable — anything already
    cached is skipped. Short windows use the daily index files; long ones the
    quarterly index. Returns counts for the app to display."""
    log = progress or (lambda msg: None)
    set_user_agent(ua)
    con = en.db_connect(db_path)
    try:
        forms = {f.strip().upper() for f in structured_forms.split(",")}
        pat = re.compile(issuer_re, re.I) if issuer_re else None

        if (end - start).days < DAILY_INDEX_MAX_DAYS:
            log(f"Reading EDGAR daily indexes for {start} → {end}")
            added = enumerate_daily(con, start, end, forms, pat, log)
        else:
            log(f"Reading EDGAR quarterly index for {start} → {end}")
            added = en.enumerate_filings(con, start, end, forms, pat)

        log("Resolving document names")
        en.resolve_docs(con)

        todo, = con.execute("""
            SELECT COUNT(*) FROM filings f
            LEFT JOIN parsed p ON p.accession = f.accession
            LEFT JOIN failed x ON x.accession = f.accession
            WHERE f.doc_url IS NOT NULL AND p.accession IS NULL
              AND x.accession IS NULL AND f.filed BETWEEN ? AND ?""",
            (start.isoformat(), end.isoformat())).fetchone()
        log(f"Parsing {todo} new pricing supplements")
        _crawl_window(con, start, end, workers, log)
    finally:
        con.close()
    return {"added": added, "parsed": todo}


def _crawl_window(con, start: date, end: date, workers: int,
                  log: Callable[[str], None]) -> None:
    """Same as edgar_notes.crawl but restricted to the requested window and
    reporting progress via callback rather than stdout."""
    import concurrent.futures as cf
    from datetime import datetime

    todo = list(con.execute("""
        SELECT f.accession, f.cik, f.issuer, f.form, f.filed, f.doc_url
        FROM filings f
        LEFT JOIN parsed p ON p.accession = f.accession
        LEFT JOIN failed x ON x.accession = f.accession
        WHERE f.doc_url IS NOT NULL AND p.accession IS NULL AND x.accession IS NULL
          AND f.filed BETWEEN ? AND ?
        ORDER BY f.filed DESC""", (start.isoformat(), end.isoformat())))
    if not todo:
        return
    done = 0
    lock = threading.Lock()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(en.process, con, *row, True) for row in todo]
        for fut in cf.as_completed(futures):
            acc, rec, err = fut.result()
            with lock, con:
                if rec:
                    con.execute("INSERT OR REPLACE INTO parsed VALUES (?,?,?)",
                                (acc, json.dumps(rec), datetime.now().isoformat()))
                else:
                    con.execute("INSERT OR REPLACE INTO failed VALUES (?,?,?)",
                                (acc, err, datetime.now().isoformat()))
            done += 1
            if done % 20 == 0 or done == len(todo):
                log(f"Parsed {done}/{len(todo)}")
