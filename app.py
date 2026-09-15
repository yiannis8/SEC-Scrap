"""
app.py — SEC structured-note issuance dashboard (Marex design system).

    pip install -r requirements.txt
    export EDGAR_UA="Your Name your@marex.com"
    streamlit run app.py

Reads the SQLite cache written by edgar_notes.py. Pick a single date or a
range, get a per-issuer breakdown, a pie of the mix, and drill into any issuer.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import edgar_data as ed

# --------------------------------------------------------------------------- #
# Marex design tokens
# --------------------------------------------------------------------------- #

M = {
    "purple": "#793690", "purple_deep": "#8123a3", "ink": "#1C1B4A",
    "ink_deep": "#0E0D24", "violet": "#843FDC", "white": "#FFFFFF",
    "off_white": "#FAFAFC", "cloud": "#F5F5F8", "mist": "#ECECF2",
    "light_grey": "#DADAE7", "light_grey_2": "#CBCBD2", "mid_grey": "#858596",
    "mid_grey_2": "#A7A7B3", "slate": "#505068",
    # sector palette — categorical data colours
    "amber": "#FFB443", "coral": "#FF846A", "cyan": "#05A0BF",
    "green": "#2D8D79", "stone": "#9E9188",
}

# Data colours: purple is reserved for accents, so charts lead with the sector
# palette and dark blue; the "Other" bucket is always light grey.
CHART_COLOURS = [M["ink"], M["violet"], M["cyan"], M["coral"], M["amber"],
                 M["green"], M["stone"], M["slate"], M["purple_deep"],
                 M["mid_grey"], "#4C6EF5", "#C2185B", "#00897B", "#EF6C00"]
OTHER_COLOUR = M["light_grey_2"]
FONT = "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

CSS = f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
  html, body, [class*="css"], .stApp, .stMarkdown, .stDataFrame, .stSelectbox,
  .stMultiSelect, .stRadio, .stDateInput, .stButton, .stMetric {{
      font-family: {FONT} !important;
      letter-spacing: -0.015em;
      color: {M['ink']};
  }}
  .stApp {{ background: {M['cloud']}; }}
  .block-container {{ padding-top: 2.5rem; padding-bottom: 4rem; max-width: 1280px; }}

  /* Sidebar as a white surface card */
  section[data-testid="stSidebar"] {{
      background: {M['white']}; border-right: 1px solid {M['light_grey']};
  }}
  section[data-testid="stSidebar"] .block-container {{ padding-top: 2rem; }}

  /* Headings: light weight, tight tracking (Marex display style) */
  h1 {{ font-weight: 300 !important; font-size: 40px !important; line-height: 1.12 !important;
        letter-spacing: -0.035em !important; color: {M['ink']}; margin: 0 0 4px 0 !important; }}
  h2 {{ font-weight: 400 !important; font-size: 22px !important; line-height: 1.3 !important;
        letter-spacing: -0.03em !important; color: {M['ink']}; margin: 0 0 4px 0 !important; }}
  h3 {{ font-weight: 500 !important; font-size: 16px !important; letter-spacing: -0.02em !important; }}

  .mx-wordmark {{ font-weight: 800; font-size: 20px; line-height: 1; letter-spacing: -0.02em; color: {M['ink']}; }}
  .mx-eyebrow  {{ font-weight: 500; font-size: 12px; line-height: 1.2; letter-spacing: 0.08em;
                  text-transform: uppercase; color: {M['mid_grey']}; }}
  .mx-caption  {{ font-size: 12px; line-height: 1.4; color: {M['mid_grey']}; }}
  .mx-lead     {{ font-weight: 300; font-size: 18px; line-height: 1.45; letter-spacing: -0.02em; color: {M['slate']}; }}

  /* Cards — Marex standard 15px radius, hairline border, no heavy shadow */
  .mx-card {{ background: {M['white']}; border: 1px solid {M['light_grey']}; border-radius: 15px;
              padding: 20px 24px; height: 100%; }}
  .mx-metric {{ font-weight: 300; font-size: 34px; line-height: 1.05; letter-spacing: -0.035em; color: {M['ink']}; margin-top: 8px; }}
  .mx-metric-sub {{ font-size: 13px; color: {M['slate']}; margin-top: 6px; }}
  .mx-header {{ display: flex; align-items: center; justify-content: space-between;
                padding-bottom: 20px; margin-bottom: 28px; border-bottom: 1px solid {M['light_grey']}; }}
  .mx-pill {{ display: inline-block; padding: 4px 12px; border-radius: 999px; background: {M['mist']};
              color: {M['slate']}; font-size: 12px; font-weight: 500; }}

  /* Widgets */
  div[data-baseweb="select"] > div, .stDateInput input, .stTextInput input {{
      border-radius: 8px !important; border-color: {M['light_grey']} !important; background: {M['white']};
  }}
  .stButton > button {{
      background: {M['purple']}; color: {M['white']}; border: none; border-radius: 999px;
      padding: 0.5rem 1.25rem; font-weight: 500; letter-spacing: -0.01em;
  }}
  .stButton > button:hover {{ background: {M['purple_deep']}; color: {M['white']}; }}
  .stDownloadButton > button {{
      background: {M['white']}; color: {M['ink']}; border: 1px solid {M['light_grey_2']};
      border-radius: 999px; font-weight: 500;
  }}
  .stDownloadButton > button:hover {{ border-color: {M['purple']}; color: {M['purple']}; }}
  div[role="radiogroup"] label p {{ font-size: 14px; }}
  .stDataFrame {{ border: 1px solid {M['light_grey']}; border-radius: 12px; overflow: hidden; }}
  a {{ color: {M['purple']}; text-decoration: none; }}
  a:hover {{ color: {M['purple_deep']}; }}
  hr {{ border-color: {M['light_grey']}; }}
  #MainMenu, footer {{ visibility: hidden; }}
</style>
"""

DB_PATH = os.environ.get("EDGAR_DB", "edgar_notes.sqlite")


def default_ua() -> str:
    """EDGAR_UA from the environment, else from Streamlit secrets."""
    ua = os.environ.get("EDGAR_UA", "")
    if not ua:
        try:
            ua = st.secrets.get("EDGAR_UA", "")
        except Exception:      # no secrets.toml locally
            ua = ""
    return ua


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def usd(v: float | None, decimals: int = 1) -> str:
    if v is None or pd.isna(v):
        return "—"
    if v >= 1e9:
        return f"${v / 1e9:,.{decimals}f}bn"
    if v >= 1e6:
        return f"${v / 1e6:,.{decimals}f}m"
    return f"${v / 1e3:,.0f}k"


def html(markup: str) -> None:
    """Render raw HTML. st.html skips markdown parsing (which otherwise ends an
    HTML block at the first blank line and prints the rest as text)."""
    if hasattr(st, "html"):
        st.html(markup)
    else:
        st.markdown("\n".join(l for l in markup.splitlines() if l.strip()),
                    unsafe_allow_html=True)


def eyebrow(text: str) -> None:
    html(f'<div class="mx-eyebrow">{text}</div>')


def metric_card(label: str, value: str, sub: str = "") -> None:
    html(f'<div class="mx-card"><div class="mx-eyebrow">{label}</div>'
         f'<div class="mx-metric">{value}</div>'
         f'<div class="mx-metric-sub">{sub}</div></div>')


def plotly_layout(fig: go.Figure, height: int = 420) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=M["ink"], size=13),
        legend=dict(orientation="v", x=1.02, y=0.5, font=dict(size=12, color=M["slate"]),
                    bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor=M["ink"], font=dict(family=FONT, color=M["white"], size=12),
                        bordercolor=M["ink"]),
    )
    return fig


def donut(labels: list[str], values: list[float], centre_top: str, centre_bottom: str,
          value_fmt, colours: list[str]) -> go.Figure:
    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=0.62, sort=False, direction="clockwise",
        marker=dict(colors=colours, line=dict(color=M["white"], width=2)),
        textinfo="percent", textposition="inside", insidetextorientation="horizontal",
        textfont=dict(size=12, color=M["white"]),
        customdata=[value_fmt(v) for v in values],
        hovertemplate="<b>%{label}</b><br>%{customdata}<br>%{percent}<extra></extra>",
    ))
    fig.add_annotation(text=f"<span style='font-size:12px;color:{M['mid_grey']};"
                            f"letter-spacing:0.08em'>{centre_top.upper()}</span>",
                       x=0.5, y=0.56, showarrow=False, font=dict(family=FONT))
    fig.add_annotation(text=f"<span style='font-size:28px;font-weight:300;color:{M['ink']}'>"
                            f"{centre_bottom}</span>",
                       x=0.5, y=0.44, showarrow=False, font=dict(family=FONT))
    return plotly_layout(fig)


def group_other(s: pd.Series, min_share: float, max_slices: int = 12) -> pd.Series:
    """Collapse tail issuers into 'Other' so the pie stays legible."""
    s = s.sort_values(ascending=False)
    share = s / s.sum()
    keep = s[(share >= min_share)].head(max_slices)
    other = s.sum() - keep.sum()
    if other > 0 and len(s) > len(keep):
        keep = pd.concat([keep, pd.Series({f"Other ({len(s) - len(keep)})": other})])
    return keep


def slice_colours(labels: list[str]) -> list[str]:
    return [OTHER_COLOUR if str(l).startswith("Other") else CHART_COLOURS[i % len(CHART_COLOURS)]
            for i, l in enumerate(labels)]


@st.cache_data(show_spinner=False)
def load(db_path: str, mtime: float) -> pd.DataFrame:
    # mtime is part of the cache key so a refresh invalidates it
    return ed.load_notes(db_path)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="Marex · SEC note issuance", page_icon="◆",
                   layout="wide", initial_sidebar_state="expanded")
html(CSS)

status = ed.db_status(DB_PATH)
df_all = load(DB_PATH, status.get("mtime", 0.0))

# ---- Sidebar: filters ----------------------------------------------------- #
with st.sidebar:
    html('<div class="mx-wordmark">MAREX</div>'
         '<div class="mx-caption" style="margin:4px 0 24px">Structured Products · EDGAR monitor</div>')

    eyebrow("Period")
    mode = st.radio("Period", ["Single date", "Date range"], horizontal=True,
                    label_visibility="collapsed")
    today = date.today()
    if mode == "Single date":
        d = st.date_input("Date", value=today - timedelta(days=1), max_value=today,
                          label_visibility="collapsed")
        start, end = d, d
    else:
        picked = st.date_input("Range", value=(today - timedelta(days=30), today),
                               max_value=today, label_visibility="collapsed")
        if isinstance(picked, tuple) and len(picked) == 2:
            start, end = picked
        elif isinstance(picked, tuple) and len(picked) == 1:
            start = end = picked[0]
        else:
            start = end = picked

    html("<div style='height:16px'></div>")
    eyebrow("Date basis")
    basis_label = st.selectbox("Date basis", ["Filing date", "Trade / pricing date", "Issue date"],
                               label_visibility="collapsed",
                               help="Filing date is always present. Trade and issue dates are parsed "
                                    "from the cover page and are missing on some supplements.")
    basis = {"Filing date": "filed", "Trade / pricing date": "trade_date",
             "Issue date": "issue_date"}[basis_label]

    html("<div style='height:16px'></div>")
    eyebrow("Measure")
    measure_label = st.radio("Measure", ["Notional", "Number of notes"], horizontal=True,
                             label_visibility="collapsed")
    by_notional = measure_label == "Notional"

    html("<div style='height:16px'></div>")
    eyebrow("Scope")
    structured_only = st.toggle("Structured notes only", value=True,
                                help="Drop vanilla fixed-rate and floating-rate takedowns.")
    exclude_prelim = st.toggle("Exclude preliminary supplements", value=True)
    min_share = st.slider("Group issuers below", 0.0, 10.0, 2.0, 0.5, format="%.1f%%",
                          help="Issuers under this share are collapsed into 'Other' in the pie.") / 100

    issuers_all = sorted(df_all["issuer"].dropna().unique()) if len(df_all) else []
    issuer_pick = st.multiselect("Issuers", issuers_all, placeholder="All issuers")

    # ---- Sidebar: refresh -------------------------------------------------- #
    html("<div style='height:24px'></div>")
    eyebrow("Data")
    if status.get("exists"):
        html(f'<div class="mx-caption">Cache covers <b>{status["first"]}</b> → <b>{status["last"]}</b><br>'
             f'{status["parsed"]:,} parsed · {status["filings"]:,} filings · {status["failed"]:,} failed</div>')
    else:
        html('<div class="mx-caption">No cache yet — pick a date and the app will fetch it.</div>')

    with st.expander("Refresh from EDGAR"):
        ua = st.text_input("EDGAR user agent", value=default_ua(),
                           placeholder="Name name@marex.com",
                           help="EDGAR requires a real name and email in the User-Agent.")
        workers = st.slider("Workers", 1, 8, 4)
        st.caption(f"Fetches {start} → {end}. Already-cached filings are skipped; "
                   "a single day usually takes a few minutes.")
        if st.button("Refresh selected period", use_container_width=True, disabled=not ua):
            with st.status("Refreshing from EDGAR…", expanded=True) as box:
                result = ed.refresh(DB_PATH, start, end, ua, workers=workers,
                                    progress=lambda msg: box.write(msg))
                box.update(label=f"Done — {result['parsed']} new supplements parsed",
                           state="complete", expanded=False)
            st.cache_data.clear()
            st.rerun()

# ---- Auto-fetch: if nothing has been enumerated for this window, get it --- #
fetched_now = False
auto_key = f"fetched:{start}:{end}"
if not ed.window_cached(DB_PATH, start, end) and not st.session_state.get(auto_key):
    st.session_state[auto_key] = True          # one attempt per window per session
    if ua:
        with st.status(f"Fetching {start} → {end} from EDGAR…", expanded=True) as box:
            try:
                result = ed.refresh(DB_PATH, start, end, ua, workers=workers,
                                    progress=lambda msg: box.write(msg))
                box.update(label=f"Fetched — {result['parsed']} supplements parsed",
                           state="complete", expanded=False)
                fetched_now = True
            except Exception as exc:        # surface, don't crash the page
                box.update(label="EDGAR fetch failed", state="error")
                st.error(f"Could not fetch from EDGAR: {exc}")
        if fetched_now:
            st.cache_data.clear()
            status = ed.db_status(DB_PATH)
            df_all = load(DB_PATH, status.get("mtime", 0.0))
    else:
        st.warning("No EDGAR user agent configured. Set EDGAR_UA in Streamlit secrets "
                   "(or in the sidebar under *Refresh from EDGAR*) to fetch data.")

# ---- Filter --------------------------------------------------------------- #
df = df_all.copy()
df = df[df[basis].notna()]
df = df[(df[basis] >= start) & (df[basis] <= end)]
if structured_only:
    df = df[df["structured"]]
if exclude_prelim:
    df = df[~df["preliminary"]]
if issuer_pick:
    df = df[df["issuer"].isin(issuer_pick)]

# ---- Header --------------------------------------------------------------- #
def fmt_day(d: date, year: bool = True) -> str:
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


period_txt = fmt_day(start) if start == end else f"{fmt_day(start, False)} – {fmt_day(end)}"
html(f'<div class="mx-header"><div>'
     f'<div class="mx-eyebrow">SEC 424(b)(2) pricing supplements · {basis_label.lower()}</div>'
     f'<h1>Note issuance, {period_txt}</h1>'
     f'<div class="mx-lead">{"Structured notes" if structured_only else "All registered notes"}'
     f'{" · priced only" if exclude_prelim else " · incl. preliminary"}</div>'
     f'</div><div class="mx-wordmark">MAREX</div></div>')

if df.empty:
    reason = ("No 424(b)(2) pricing supplements were filed in this window." if fetched_now
              else "No parsed supplements match these filters. Widen the period, switch the date basis, "
                   "or refresh the cache from the sidebar.")
    html('<div class="mx-card" style="text-align:center;padding:64px 24px">'
         '<div class="mx-eyebrow">Nothing in this window</div>'
         f'<div class="mx-lead" style="margin-top:12px">{reason}</div></div>')
    st.stop()

# ---- Headline metrics ----------------------------------------------------- #
n_notes = len(df)
notional = df["size_usd"].sum()
n_iss = df["issuer"].nunique()
no_size = df["size_usd"].isna().sum()
median_ticket = df["size_usd"].median()

c1, c2, c3, c4 = st.columns(4)
with c1:
    metric_card("Notes", f"{n_notes:,}", f"{df['filings'].sum():,.0f} filings incl. preliminary")
with c2:
    metric_card("Notional", usd(notional), f"{no_size:,} note{'s' if no_size != 1 else ''} without a stated size")
with c3:
    metric_card("Issuers", f"{n_iss}", f"median ticket {usd(median_ticket, 2)}")
with c4:
    top = df.groupby("issuer")["size_usd" if by_notional else "isin"].agg("sum" if by_notional else "count")
    if top.sum() > 0:
        metric_card("Largest issuer", top.idxmax(), f"{top.max() / top.sum():.0%} of {measure_label.lower()}")
    else:
        metric_card("Largest issuer", "—", "no sizes stated in this window")

html("<div style='height:28px'></div>")

# ---- Issuer breakdown: pie + league table --------------------------------- #
league = (df.groupby("issuer")
          .agg(notes=("accession", "count"), notional=("size_usd", "sum"),
               median_ticket=("size_usd", "median"), median_tenor=("tenor_years", "median"))
          .sort_values("notional" if by_notional else "notes", ascending=False))
league["share"] = league["notional" if by_notional else "notes"] / \
                  league["notional" if by_notional else "notes"].sum()

left, right = st.columns([5, 6], gap="large")
with left:
    eyebrow(f"{measure_label} by issuer")
    html("<h2>Issuer mix</h2>")
    series = league["notional" if by_notional else "notes"]
    series = group_other(series[series > 0], min_share)
    fig = donut(list(series.index), list(series.values),
                centre_top=measure_label,
                centre_bottom=usd(notional) if by_notional else f"{n_notes:,}",
                value_fmt=(lambda v: usd(v)) if by_notional else (lambda v: f"{int(v):,} notes"),
                colours=slice_colours(list(series.index)))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

with right:
    eyebrow("League table")
    html("<h2>Breakdown per issuer</h2>")
    table = league.reset_index().rename(columns={
        "issuer": "Issuer", "notes": "Notes", "notional": "Notional",
        "median_ticket": "Median ticket", "median_tenor": "Median tenor (y)", "share": "Share"})
    st.dataframe(
        table, hide_index=True, use_container_width=True, height=420,
        column_config={
            "Notes": st.column_config.NumberColumn(format="%d"),
            "Notional": st.column_config.NumberColumn(format="$%,.0f"),
            "Median ticket": st.column_config.NumberColumn(format="$%,.0f"),
            "Median tenor (y)": st.column_config.NumberColumn(format="%.1f"),
            "Share": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=1),
        })

html("<div style='height:36px'></div>")

# ---- Payoff / asset-class mix -------------------------------------------- #
eyebrow("Product mix")
html("<h2>What was issued</h2>")
m1, m2 = st.columns(2, gap="large")
for col, field, title in ((m1, "family", "Payoff family"), (m2, "asset_class", "Underlying asset class")):
    with col:
        s = df.groupby(field)["size_usd" if by_notional else "accession"].agg("sum" if by_notional else "count")
        s = group_other(s[s > 0], 0.0, max_slices=9)
        fig = donut(list(s.index), list(s.values), centre_top=title,
                    centre_bottom=f"{len(s)} types",
                    value_fmt=(lambda v: usd(v)) if by_notional else (lambda v: f"{int(v):,} notes"),
                    colours=slice_colours(list(s.index)))
        st.plotly_chart(plotly_layout(fig, height=340), use_container_width=True,
                        config={"displayModeBar": False})

html("<div style='height:36px'></div>")

# ---- Drill-down ----------------------------------------------------------- #
eyebrow("Drill-down")
html("<h2>Products issued</h2>")
sel = st.selectbox("Issuer", ["All issuers"] + list(league.index), label_visibility="collapsed")
detail = df if sel == "All issuers" else df[df["issuer"] == sel]

if sel != "All issuers":
    row = league.loc[sel]
    pills = [f"{int(row['notes'])} notes", f"{usd(row['notional'])} notional", f"{row['share']:.1%} share"]
    if pd.notna(row["median_tenor"]):
        pills.append(f"median tenor {row['median_tenor']:.1f}y")
    html(" &nbsp;".join(f'<span class="mx-pill">{p}</span>' for p in pills)
         + "<div style='height:12px'></div>")

show_cols = ["isin", "issuer", "product", "family", "asset_class", "size_usd",
             "trade_date", "issue_date", "maturity_date", "tenor_years",
             "estimated_initial_value", "preliminary", "filed", "url"]
view = (detail[show_cols].sort_values("size_usd", ascending=False, na_position="last")
        .rename(columns={
            "isin": "ISIN", "issuer": "Issuer", "product": "Product", "family": "Payoff",
            "asset_class": "Asset class", "size_usd": "Size", "trade_date": "Trade",
            "issue_date": "Issue", "maturity_date": "Maturity", "tenor_years": "Tenor (y)",
            "estimated_initial_value": "Est. initial value", "preliminary": "Prelim",
            "filed": "Filed", "url": "EDGAR"}))
st.dataframe(
    view, hide_index=True, use_container_width=True, height=min(60 + 35 * len(view), 640),
    column_config={
        "Size": st.column_config.NumberColumn(format="$%,.0f"),
        "Tenor (y)": st.column_config.NumberColumn(format="%.1f"),
        "Product": st.column_config.TextColumn(width="large"),
        "EDGAR": st.column_config.LinkColumn(display_text="Open"),
        "Prelim": st.column_config.CheckboxColumn(),
    })

# ---- Export --------------------------------------------------------------- #
html("<div style='height:16px'></div>")
e1, e2, _ = st.columns([2, 2, 8])
stem = f"issuance_{start.isoformat()}" + ("" if start == end else f"_{end.isoformat()}")
with e1:
    st.download_button("Download notes (CSV)", detail.to_csv(index=False).encode(),
                       f"{stem}.csv", "text/csv", use_container_width=True)
with e2:
    st.download_button("Download league table (CSV)", table.to_csv(index=False).encode(),
                       f"{stem}_issuers.csv", "text/csv", use_container_width=True)

html(f'<div class="mx-caption" style="margin-top:40px;padding-top:20px;border-top:1px solid {M["light_grey"]}">'
     'Source: SEC EDGAR 424(b)(2) pricing supplements, parsed from cover pages. Sizes reflect the stated '
     'aggregate principal amount or the filing-fee exhibit where the cover has no total; some supplements '
     'carry no size and are counted but not summed. Preliminary and final supplements for the same ISIN are merged.'
     '</div>')
