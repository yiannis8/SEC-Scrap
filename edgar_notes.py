#!/usr/bin/env python3
"""
edgar_notes.py — market-wide extract of SEC-registered note issuance.

Enumerates every 424(b)(2) pricing supplement filed by any issuer from a start
date onward, parses each cover page for identifiers, size, dates and payoff
type, and writes a note-level table plus an issuer league table.

    pip install requests pandas openpyxl lxml
    export EDGAR_UA="Joost Burgerhout joost.burgerhout@marex.com"

    # 1. size the job first — index only, no document fetching
    python edgar_notes.py --index-only

    # 2. run it (resumable; re-run after a stop and it picks up where it left off)
    python edgar_notes.py --workers 5

    # narrower cuts
    python edgar_notes.py --issuer "Marex|Leonteq|UBS"
    python edgar_notes.py --start 2026-07-01 --end 2026-09-30 --structured-only

Design notes
  * Enumeration uses the quarterly master index (one request per quarter),
    not full-text search, so the population is complete rather than ranked.
  * Primary document names come from the submissions API (one request per
    filer) rather than one index.json per filing — roughly 200 requests
    instead of 30,000.
  * Every fetch and every parse result is cached in SQLite. Killing the
    process loses nothing; re-running re-parses from cache without re-fetching.
  * Size is taken from the cover-page total, falling back to the EX-FILING FEES
    exhibit, which is structured and consistent across issuers where the cover
    table is not.
  * Each issuer words its cover differently. Parsers below hold alternates for
    every field; unmatched fields come back empty rather than guessed, and
    `parse_flags` records what was missed so you can see where to improve it.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

SEC = "https://www.sec.gov"
DATA_SEC = "https://data.sec.gov"
DB_PATH = "edgar_notes.sqlite"
RATE = 8.0            # requests/sec, under EDGAR's stated 10/s ceiling
UA = os.environ.get("EDGAR_UA", "")


# --------------------------------------------------------------------------- #
# HTTP: one global rate limiter shared by all worker threads
# --------------------------------------------------------------------------- #

class Throttle:
    def __init__(self, per_sec: float):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next:
                time.sleep(self.next - now)
                now = time.monotonic()
            self.next = max(now, self.next) + self.gap


THROTTLE = Throttle(RATE)
_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        _local.s = s
    return s


def fetch(url: str, tries: int = 4, timeout: int = 45) -> str | None:
    """GET with throttling and backoff. None on a permanent failure."""
    for attempt in range(tries):
        THROTTLE.wait()
        try:
            r = session().get(url, timeout=timeout)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429, 500, 502, 503, 504):
            time.sleep(2 ** attempt + 1)
            continue
        return None
    return None


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def db_connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS filings (
        accession TEXT PRIMARY KEY, cik TEXT, issuer TEXT, form TEXT,
        filed TEXT, primary_doc TEXT, doc_url TEXT);
    CREATE TABLE IF NOT EXISTS parsed (
        accession TEXT PRIMARY KEY, payload TEXT, parsed_at TEXT);
    CREATE TABLE IF NOT EXISTS failed (
        accession TEXT PRIMARY KEY, reason TEXT, at TEXT);
    CREATE INDEX IF NOT EXISTS ix_filed ON filings(filed);
    """)
    return con


# --------------------------------------------------------------------------- #
# Step 1 — enumerate filings from the quarterly master index
# --------------------------------------------------------------------------- #

def quarters(start: date, end: date):
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        yield y, q
        q += 1
        if q > 4:
            q, y = 1, y + 1


def enumerate_filings(con, start: date, end: date, forms: set[str],
                      issuer_re: re.Pattern | None) -> int:
    """Populate the filings table. Returns rows added."""
    added = 0
    for y, q in quarters(start, end):
        url = f"{SEC}/Archives/edgar/full-index/{y}/QTR{q}/master.idx"
        print(f"  index {y} QTR{q} …", end="", flush=True)
        txt = fetch(url)
        if not txt:
            print(" unavailable")
            continue
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
                continue
            if not (start <= fdate <= end):
                continue
            if issuer_re and not issuer_re.search(name):
                continue
            m = re.search(r"(\d{10}-\d{2}-\d{6})", fname)
            if not m:
                continue
            rows.append((m.group(1), cik, name, form, filed, None, None))
        with con:
            cur = con.executemany(
                "INSERT OR IGNORE INTO filings "
                "(accession,cik,issuer,form,filed,primary_doc,doc_url) "
                "VALUES (?,?,?,?,?,?,?)", rows)
        added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        print(f" {len(rows)} filings")
    return added


# --------------------------------------------------------------------------- #
# Step 2 — resolve primary document names, one submissions call per filer
# --------------------------------------------------------------------------- #

def resolve_docs(con) -> None:
    ciks = [r[0] for r in con.execute(
        "SELECT DISTINCT cik FROM filings WHERE primary_doc IS NULL")]
    print(f"resolving document names for {len(ciks)} filers")
    for n, cik in enumerate(ciks, 1):
        mapping = {}
        url = f"{DATA_SEC}/submissions/CIK{int(cik):010d}.json"
        txt = fetch(url)
        if txt:
            try:
                sub = json.loads(txt)
            except json.JSONDecodeError:
                sub = None
            if sub:
                blocks = [sub["filings"]["recent"]]
                for extra in sub["filings"].get("files", []):
                    t = fetch(f"{DATA_SEC}/submissions/{extra['name']}")
                    if t:
                        try:
                            blocks.append(json.loads(t))
                        except json.JSONDecodeError:
                            pass
                for b in blocks:
                    accs = b.get("accessionNumber", [])
                    docs = b.get("primaryDocument", [])
                    for a, dcm in zip(accs, docs):
                        if dcm:
                            mapping[a] = dcm
        rows = []
        for acc, in con.execute(
                "SELECT accession FROM filings WHERE cik=? AND primary_doc IS NULL",
                (cik,)):
            doc = mapping.get(acc)
            if not doc:                      # fall back to the filing's own index
                nod = acc.replace("-", "")
                j = fetch(f"{SEC}/Archives/edgar/data/{int(cik)}/{nod}/index.json")
                if j:
                    try:
                        items = json.loads(j)["directory"]["item"]
                        cands = [i["name"] for i in items
                                 if i["name"].lower().endswith((".htm", ".html"))
                                 and "ex-filingfees" not in i["name"].lower()]
                        doc = cands[0] if cands else None
                    except Exception:
                        doc = None
            if doc:
                nod = acc.replace("-", "")
                rows.append((doc,
                             f"{SEC}/Archives/edgar/data/{int(cik)}/{nod}/{doc}",
                             acc))
        if rows:
            with con:
                con.executemany(
                    "UPDATE filings SET primary_doc=?, doc_url=? WHERE accession=?",
                    rows)
        if n % 25 == 0 or n == len(ciks):
            print(f"  {n}/{len(ciks)} filers")


# --------------------------------------------------------------------------- #
# Step 3 — parse a cover page
# --------------------------------------------------------------------------- #

TAGS = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")
ELEM = re.compile(r"(?s)<[^>]+>")
SPACE = re.compile(r"[\u00a0\u2007\u2009\u202f\u200b\t ]+")


def to_text(raw: str, cap: int = 400_000) -> str:
    raw = raw[:cap]
    t = ELEM.sub(" ", TAGS.sub(" ", raw))
    t = html.unescape(t)
    for ch in ("\u00ae", "\u2122", "\u2120", "\u200b"):
        t = t.replace(ch, "")
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    return SPACE.sub(" ", t).strip()


DATE_RE = r"[A-Z][a-z]{2,9}\.?\s+\d{1,2},?\s+20\d{2}"

# Every label variant seen across the main shelf issuers. Order matters:
# the first alternate that matches wins.
DATE_FIELDS = {
    "trade_date": ["Trade Date", "Pricing Date", "Strike Date", "Initial Valuation Date",
                   "Determination Date"],
    "issue_date": ["Original Issue Date", "Issue Date", "Settlement Date",
                   "Original Offering Date"],
    "final_val_date": ["Final Valuation Date", "Valuation Date", "Final Observation Date",
                       "Observation Date", "Final Determination Date", "Final Averaging Date",
                       "Final Review Date"],
    "maturity_date": ["Maturity Date", "Stated Maturity Date", "Scheduled Maturity Date",
                      "Maturity"],
    "first_call_date": ["First Call Date", "First Redemption Date",
                        "First Optional Redemption Date", "Initial Call Date"],
}

SIZE_LABELS = [
    r"Total[^A-Za-z0-9]{0,40}\$\s*([\d,]{7,})",
    r"Aggregate\s+[Pp]rincipal\s+[Aa]mount[^$]{0,80}\$\s*([\d,]{7,})",
    r"Aggregate\s+[Ff]ace\s+[Aa]mount[^$]{0,80}\$\s*([\d,]{7,})",
    r"Principal\s+Amount\s+of\s+Notes[^$]{0,80}\$\s*([\d,]{7,})",
    r"Total\s+[Aa]ggregate[^$]{0,80}\$\s*([\d,]{7,})",
]

# Payoff classification. First match wins, so specific patterns precede general.
AC = r"auto[ -]?call(?:able|ing)?"
CI = r"contingent (?:income|coupon|interest|yield)"
FAMILIES = [
    ("Autocallable contingent income", rf"{AC}.{{0,40}}{CI}|trigger {AC}.{{0,40}}{CI}"),
    ("Issuer callable contingent income", rf"(?:issuer )?callable.{{0,40}}{CI}"),
    ("Contingent income / income barrier", rf"{CI}|income barrier"),
    ("Autocallable buffered", rf"{AC}.{{0,30}}buffer"),
    ("Autocallable", rf"{AC}|trigger \w+ securit"),
    ("Capped leveraged buffered", r"capped.{0,20}leverag\w*.{0,20}buffer"),
    ("Leveraged buffered / participation", r"leverag\w*.{0,30}(buffer|participat)|dual directional"),
    ("Buffered / barrier return", r"buffer\w*|barrier"),
    ("Digital / absolute return", r"digital|absolute return|range accrual"),
    ("Market linked / uncapped growth", r"market linked|uncapped|growth securit"),
    ("Fixed-to-floating / floater", r"floating rate|fixed[- ]to[- ]floating|steepener|cms"),
    ("Fixed-rate callable", r"callable.{0,25}(fixed|notes due)|fixed rate callable"),
    ("Fixed rate senior", r"fixed rate|senior notes due"),
]

STRUCTURED_HINTS = re.compile(
    r"(?i)linked to|autocall|auto-call|contingent coupon|contingent income|buffer|"
    r"barrier|participation rate|digital|worst performing|least performing|"
    r"market linked|trigger|knock-?in|knock-?out|upside participation|"
    r"reference asset|underlying (index|stock|share|asset)|basket of")

ASSET_CLASSES = [
    ("Worst-of basket", r"worst[- ]performing|least performing|lowest performing"),
    ("Equity index", r"s&p 500|russell 2000|nasdaq-?100|dow jones|euro stoxx|nikkei|"
                     r"msci|ftse|hang seng|topix|index"),
    ("Single stock", r"common stock of|ordinary shares of|class a (common )?stock"),
    ("ETF", r"\betf\b|shares of the|trust, series|spdr|ishares|invesco qqq"),
    ("Commodity", r"gold|silver|crude|wti|brent|copper|natural gas|commodity"),
    ("Rates", r"cms|swap rate|treasury|sofr|constant maturity"),
    ("FX", r"exchange rate|usd/|eur/|currency"),
    ("Credit", r"credit linked|reference entity"),
]


def cusip_to_isin(cusip: str) -> str | None:
    """US CUSIP -> ISIN. Most US issuers quote only the CUSIP; the ISIN is the
    same identifier with a country prefix and a Luhn check digit, so deriving
    it lets the whole market key on one column."""
    if not cusip or len(cusip) != 9:
        return None
    body = "US" + cusip
    digits = ""
    for ch in body:
        digits += str(ord(ch) - 55) if ch.isalpha() else ch
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


def first_date(text: str, labels: list[str]) -> str | None:
    for lab in labels:
        m = re.search(rf"{lab}\s*:?\s*(?:is|will be)?\s*({DATE_RE})", text, re.I)
        if m:
            return norm_date(m.group(1))
    return None


def norm_date(s: str) -> str | None:
    s = re.sub(r"\s+", " ", s.replace(".", "")).replace(" ,", ",")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def classify(patterns, text: str, default: str) -> str:
    low = text.lower()
    for name, pat in patterns:
        if re.search(pat, low):
            return name
    return default


def headline(text: str) -> str | None:
    """The cover-page product title: '<$size> <product name> due <date>'."""
    window = text[:12000]
    m = re.search(rf"\$\s*[\d,]*\s*([A-Z][^$]{{15,260}}?due\s+{DATE_RE})", window)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip(" -,")
    m = re.search(rf"([A-Z][A-Za-z][^$]{{15,260}}?due\s+{DATE_RE})", window)
    return re.sub(r"\s+", " ", m.group(1)).strip(" -,") if m else None


def parse_doc(text: str) -> dict:
    out: dict[str, object] = {}
    flags = []

    m = re.search(r"\b([0-9A-Z]{8}\d)\s*/\s*(US[0-9A-Z]{9}\d)\b", text)
    if m:
        out["cusip"], out["isin"] = m.group(1), m.group(2)
    else:
        mi = re.search(r"\b(US[0-9A-Z]{9}\d)\b", text)
        mc = re.search(r"CUSIP(?:\s*(?:No\.?|Number))?\s*:?\s*([0-9A-Z]{8}\d)\b", text, re.I)
        out["isin"] = mi.group(1) if mi else None
        out["cusip"] = mc.group(1) if mc else None
    if not out.get("isin") and out.get("cusip"):
        out["isin"] = cusip_to_isin(out["cusip"])
        if out["isin"]:
            flags.append("isin_derived")
    if not out.get("isin") and not out.get("cusip"):
        flags.append("no_identifier")

    for field, labels in DATE_FIELDS.items():
        out[field] = first_date(text, labels)
        if out[field] is None:
            flags.append(f"no_{field}")

    size = None
    for pat in SIZE_LABELS:
        m = re.search(pat, text)
        if m:
            v = int(m.group(1).replace(",", ""))
            if v >= 50_000:                 # below this it is a per-note figure
                size = v
                break
    title = headline(text)
    if size is None:
        # Many issuers state the offering amount only in the cover title, and
        # preliminary supplements often have no "Total" row at all.
        m = re.search(rf"\$\s*([\d,]{{7,}})\s+(?=[A-Z])[^$]{{10,260}}?due\s+{DATE_RE}",
                      text[:12000])
        if m:
            v = int(m.group(1).replace(",", ""))
            if v >= 50_000:
                size = v
                flags.append("size_from_title")
    out["size_usd"] = size
    if size is None:
        flags.append("no_size")

    out["product"] = title
    out["family"] = classify(FAMILIES, title or text[:6000], "Unclassified")
    out["asset_class"] = classify(ASSET_CLASSES, title or text[:6000], "Unclassified")
    out["structured"] = bool(STRUCTURED_HINTS.search(text[:20000]))
    out["preliminary"] = bool(re.search(r"Subject to Completion|Preliminary Pricing", text[:8000]))

    m = re.search(r"Estimated (?:Initial )?[Vv]alue[^$]{0,160}(\$\s?[\d,.]+"
                  r"(?:\s*(?:and|to|-)\s*\$\s?[\d,.]+)?)", text)
    out["estimated_initial_value"] = (
        re.sub(r"\s+", " ", m.group(1)).replace("$ ", "$").strip() if m else None)

    m = re.search(r"Registration (?:Statement )?(?:No|Number)\.?\s*:?\s*(333-\d{5,6})", text)
    out["registration_no"] = m.group(1) if m else None

    m = re.search(r"(?:(?:fully and unconditionally |unconditionally |irrevocably )?"
                  r"guarantee(?:d|s)?\s+(?:fully and unconditionally\s+)?by"
                  r"|Guarantor)\s*:?\s*"
                  r"([A-Z][A-Za-z.,&' ]{4,60}?)(?=\s+(?:Pricing|Trade|Strike|CUSIP|The|Investing|\$|due)|[.\n]|$)",
                  text, re.I)
    out["guarantor"] = re.sub(r"\s+", " ", m.group(1)).strip(" .,") if m else None

    if out["trade_date"] and out["maturity_date"]:
        try:
            t = date.fromisoformat(out["trade_date"])
            mt = date.fromisoformat(out["maturity_date"])
            out["tenor_years"] = round((mt - t).days / 365.25, 2)
        except ValueError:
            out["tenor_years"] = None
    else:
        out["tenor_years"] = None

    out["parse_flags"] = ",".join(flags)
    return out


FEE_TAGS = re.compile(r"(?i)(AggtSalesPrc|MaxAggtOfferg|AmtRegistered|AmtRgstd|"
                      r"AggregateOffering|ProposedMaximumAggregate)")


def fee_exhibit_size(cik: str, accession: str) -> int | None:
    """Fall back to the EX-FILING FEES exhibit, which is structured XBRL."""
    nod = accession.replace("-", "")
    j = fetch(f"{SEC}/Archives/edgar/data/{int(cik)}/{nod}/index.json")
    if not j:
        return None
    try:
        items = json.loads(j)["directory"]["item"]
    except Exception:
        return None
    names = [i["name"] for i in items
             if "filingfee" in i["name"].lower().replace("-", "").replace("_", "")]
    xmls = [n for n in names if n.lower().endswith(".xml")]
    for name in xmls + names:
        body = fetch(f"{SEC}/Archives/edgar/data/{int(cik)}/{nod}/{name}")
        if not body:
            continue
        best = None
        for m in re.finditer(r"<([\w:]+)[^>]*>\s*([\d,.]+)\s*</\1>", body):
            if FEE_TAGS.search(m.group(1)):
                try:
                    v = float(m.group(2).replace(",", ""))
                except ValueError:
                    continue
                if v >= 50_000 and (best is None or v > best):
                    best = v
        if best:
            return int(best)
    return None


# --------------------------------------------------------------------------- #
# Step 4 — run the crawl
# --------------------------------------------------------------------------- #

def process(con, accession, cik, issuer, form, filed, doc_url, use_fee):
    raw = fetch(doc_url)
    if not raw:
        return accession, None, "fetch_failed"
    rec = parse_doc(to_text(raw))
    if rec["size_usd"] is None and use_fee and not rec["preliminary"]:
        rec["size_usd"] = fee_exhibit_size(cik, accession)
        if rec["size_usd"]:
            rec["parse_flags"] = rec["parse_flags"].replace("no_size", "size_from_fee_exhibit")
    rec.update(accession=accession, cik=cik, issuer=issuer, form=form,
               filed=filed, url=doc_url)
    return accession, rec, None


def crawl(con, workers: int, limit: int | None, use_fee: bool):
    todo = list(con.execute("""
        SELECT f.accession, f.cik, f.issuer, f.form, f.filed, f.doc_url
        FROM filings f
        LEFT JOIN parsed p ON p.accession = f.accession
        LEFT JOIN failed x ON x.accession = f.accession
        WHERE f.doc_url IS NOT NULL AND p.accession IS NULL AND x.accession IS NULL
        ORDER BY f.filed DESC"""))
    if limit:
        todo = todo[:limit]
    print(f"parsing {len(todo)} documents with {workers} workers")
    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(process, con, *row, use_fee) for row in todo]
        for fut in cf.as_completed(futures):
            acc, rec, err = fut.result()
            with con:
                if rec:
                    con.execute("INSERT OR REPLACE INTO parsed VALUES (?,?,?)",
                                (acc, json.dumps(rec), datetime.now().isoformat()))
                else:
                    con.execute("INSERT OR REPLACE INTO failed VALUES (?,?,?)",
                                (acc, err, datetime.now().isoformat()))
            done += 1
            if done % 250 == 0:
                rate = done / max(1e-9, time.time() - t0)
                left = (len(todo) - done) / max(rate, 1e-9) / 60
                print(f"  {done}/{len(todo)}  {rate:.1f} doc/s  ~{left:.0f} min left")


# --------------------------------------------------------------------------- #
# Step 5 — output
# --------------------------------------------------------------------------- #

COLS = ["isin", "cusip", "issuer", "guarantor", "family", "asset_class", "product",
        "size_usd", "trade_date", "issue_date", "final_val_date", "maturity_date",
        "first_call_date", "tenor_years", "estimated_initial_value", "structured",
        "preliminary", "registration_no", "form", "filed", "accession", "cik",
        "parse_flags", "url"]


def export(con, out_stem: str, structured_only: bool, dedupe: bool):
    rows = [json.loads(p) for (p,) in con.execute("SELECT payload FROM parsed")]
    if structured_only:
        rows = [r for r in rows if r.get("structured")]

    if dedupe:
        # One row per ISIN: a note filed preliminary then final appears twice.
        # Merge field-wise, letting the final (priced) filing win.
        merged: dict[str, dict] = {}
        loose = []
        for r in rows:
            key = r.get("isin") or r.get("cusip")
            if not key:
                loose.append(r)
                continue
            m = merged.setdefault(key, {})
            final = not r.get("preliminary")
            for k, v in r.items():
                if v in (None, "", False):
                    continue
                if k not in m or m[k] in (None, "", False) or (final and k in
                        ("size_usd", "preliminary", "url", "filed", "accession",
                         "estimated_initial_value")):
                    m[k] = v
            m["filings"] = m.get("filings", 0) + 1
        rows = list(merged.values()) + loose

    csv_path = f"{out_stem}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS + ["filings"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {csv_path} — {len(rows)} rows")

    try:
        import pandas as pd
    except ImportError:
        return
    df = pd.DataFrame(rows)
    for c in COLS + ["filings"]:
        if c not in df:
            df[c] = None
    df = df[COLS + ["filings"]]

    final = df[df["preliminary"] != True]
    league = (final.groupby("issuer")
              .agg(notes=("isin", "count"),
                   notional=("size_usd", "sum"),
                   median_ticket=("size_usd", "median"),
                   median_tenor=("tenor_years", "median"))
              .sort_values("notional", ascending=False))
    league["share_pct"] = (league["notional"] / league["notional"].sum() * 100).round(1)

    mix = (final.pivot_table(index="family", values="size_usd",
                             aggfunc=["count", "sum"])
           .droplevel(0, axis=1))
    mix.columns = ["notes", "notional"]
    mix = mix.sort_values("notional", ascending=False)

    with pd.ExcelWriter(f"{out_stem}.xlsx", engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name="notes", index=False)
        league.to_excel(xl, sheet_name="issuer league table")
        mix.to_excel(xl, sheet_name="payoff mix")
        (final.groupby([final["trade_date"].str[:7], "issuer"])["size_usd"]
         .sum().unstack(fill_value=0)
         .to_excel(xl, sheet_name="monthly by issuer"))
    print(f"wrote {out_stem}.xlsx")

    print("\nTop issuers by priced notional")
    print(league.head(20).to_string(
        formatters={"notional": lambda v: f"${v/1e6:,.0f}m",
                    "median_ticket": lambda v: f"${v/1e6:,.2f}m"}))
    miss = df["parse_flags"].fillna("").str.contains("no_size").sum()
    print(f"\n{len(df)} rows · {miss} without a size · "
          f"{int(df['preliminary'].sum() or 0)} preliminary")


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--forms", default="424B2",
                    help="comma separated, e.g. 424B2,424B3,FWP")
    ap.add_argument("--issuer", default=None,
                    help="regex filter on the filer name in the index")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N documents (for a trial run)")
    ap.add_argument("--index-only", action="store_true",
                    help="enumerate and report the population, fetch nothing")
    ap.add_argument("--structured-only", action="store_true",
                    help="drop vanilla debt takedowns from the output")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="keep preliminary and final as separate rows")
    ap.add_argument("--fee-fallback", action="store_true", default=True,
                    help="use the EX-FILING FEES exhibit when the cover has no total")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default="edgar_notes")
    ap.add_argument("--export-only", action="store_true",
                    help="re-export from cache without fetching")
    args = ap.parse_args()

    if not UA:
        sys.exit("Set EDGAR_UA to 'Your Name your@email' — EDGAR rejects generic agents.")

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    con = db_connect(args.db)

    if args.export_only:
        export(con, args.out, args.structured_only, not args.no_dedupe)
        return

    forms = {f.strip().upper() for f in args.forms.split(",")}
    issuer_re = re.compile(args.issuer, re.I) if args.issuer else None

    print(f"enumerating {sorted(forms)} from {start} to {end}")
    enumerate_filings(con, start, end, forms, issuer_re)

    n, = con.execute("SELECT COUNT(*) FROM filings").fetchone()
    filers, = con.execute("SELECT COUNT(DISTINCT cik) FROM filings").fetchone()
    print(f"\npopulation: {n} filings from {filers} filers")
    if args.index_only:
        print("\nBiggest filers in the window:")
        for issuer, c in con.execute(
                "SELECT issuer, COUNT(*) c FROM filings GROUP BY cik "
                "ORDER BY c DESC LIMIT 25"):
            print(f"  {c:6d}  {issuer}")
        est = n * 2 / RATE / 3600
        print(f"\nA full parse is roughly {est:.1f} hours of requests at {RATE}/s.")
        print("Narrow it with --issuer or --start, or trial it with --limit 200.")
        return

    resolve_docs(con)
    crawl(con, args.workers, args.limit, args.fee_fallback)
    export(con, args.out, args.structured_only, not args.no_dedupe)


if __name__ == "__main__":
    main()
