"""Rate file dashboard (C7). Run: uv run streamlit run dashboard.py"""

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DEFAULT_FILE = Path(__file__).parent / "output" / "1_ppo_rate_file.xlsx"
TEXT_COLUMNS = ["NPI", "TIN", "Billing Code", "Address", "Specialty"]
BELOW, AT, ABOVE, SINGLE = "Below market", "At market", "Above market", "Only rate for code"
TIERS = [BELOW, AT, ABOVE, SINGLE]
BAND = 0.10
MIN_CODES = 3
BAR = "#2a78d6"

st.set_page_config(page_title="PPO Rate Explorer", page_icon="📊", layout="wide")


@st.cache_data(show_spinner="Reading the rate file…")
def load(path: str, mtime: float) -> pd.DataFrame:
    df = pd.read_excel(path, dtype={c: str for c in TEXT_COLUMNS})
    df[TEXT_COLUMNS] = df[TEXT_COLUMNS].fillna("")
    df["Rate"] = pd.to_numeric(df["Rate"], errors="coerce")
    df = df.dropna(subset=["Rate"])

    parts = df["Address"].str.rsplit(", ", n=2)
    df["City"] = parts.str[-2].where(parts.str.len() >= 3, "").str.title()
    df["ZIP"] = parts.str[-1].str.extract(r"(\d{5})", expand=False).fillna("")

    by_code = df.groupby("Billing Code")["Rate"]
    df["Code Median"] = by_code.transform("median")
    df["Rate Index"] = (df["Rate"] / df["Code Median"]).where(df["Code Median"] > 0)
    df["Over Median"] = (df["Rate"] - df["Code Median"]).clip(lower=0)
    rates_for_code = by_code.transform("nunique")
    df["Cost Tier"] = pd.Series(AT, index=df.index)
    df.loc[df["Rate Index"] < 1 - BAND, "Cost Tier"] = BELOW
    df.loc[df["Rate Index"] > 1 + BAND, "Cost Tier"] = ABOVE
    df.loc[rates_for_code == 1, "Cost Tier"] = SINGLE
    df["Cost Tier"] = pd.Categorical(df["Cost Tier"], TIERS)

    df["_search"] = df[TEXT_COLUMNS + ["City"]].agg(" ".join, axis=1).str.lower()
    return df


def bar_chart(data: pd.DataFrame, category: str, value: str, title: str, fmt: str = ",.0f"):
    chart = (
        alt.Chart(data, title=title)
        .mark_bar(color=BAR, cornerRadiusEnd=4, height={"band": 0.7})
        .encode(
            x=alt.X(value, title=None, axis=alt.Axis(format=fmt, grid=True, gridOpacity=0.3)),
            y=alt.Y(category, sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            tooltip=[alt.Tooltip(category), alt.Tooltip(value, format=fmt)],
        )
        .properties(height=max(160, 28 * len(data)))
    )
    st.altair_chart(chart, width="stretch")


def options(df: pd.DataFrame, column: str) -> list:
    return sorted(v for v in df[column].unique() if v)



args = [a for a in sys.argv[1:] if not a.startswith("-")]
path = Path(args[0]) if args else DEFAULT_FILE
if not path.exists():
    st.title("PPO Rate Explorer")
    st.error(f"No rate file at `{path}`. Run the pipeline first: `uv run main.py`.")
    st.stop()

mtime = path.stat().st_mtime
data = load(str(path), mtime)


with st.sidebar:
    st.header("Filters")
    query = st.text_input("Search", placeholder="NPI, TIN, code, address, specialty…",
                          help="Matches any part of any column; separate words must all match.")
    specialties = st.multiselect("Specialty", options(data, "Specialty"))
    codes = st.multiselect("Billing code", options(data, "Billing Code"))
    cities = st.multiselect("City", options(data, "City"))

    view = data
    for word in query.lower().split():
        view = view[view["_search"].str.contains(word, regex=False)]
    for column, chosen in [("Specialty", specialties), ("Billing Code", codes), ("City", cities)]:
        if chosen:
            view = view[view[column].isin(chosen)]
    st.divider()
    st.caption(f"**File:** `{path.name}`  \n**Updated:** {pd.Timestamp(mtime, unit='s'):%d %b %Y, %H:%M}  \n"
               f"**Rows:** {len(data):,}")

st.title("PPO Rate Explorer")
filtered = len(view) < len(data)
st.caption(f"Showing **{len(view):,}** of {len(data):,} rates" + (" (filtered)" if filtered else ""))
if view.empty:
    st.warning("No rates match these filters.")
    st.stop()

executive, analyst, cost = st.tabs(["Executive overview", "Analyst explorer", "Cost effectiveness"])


with executive:
    k = st.columns(6)
    k[0].metric("Rates", f"{len(view):,}")
    k[1].metric("Providers (NPI)", f"{view['NPI'].nunique():,}")
    k[2].metric("Organizations (TIN)", f"{view['TIN'].nunique():,}")
    k[3].metric("Billing codes", f"{view['Billing Code'].nunique():,}")
    k[4].metric("Specialties", f"{view['Specialty'].replace('', pd.NA).nunique():,}")
    k[5].metric("Median rate", f"${view['Rate'].median():,.2f}")

    left, right = st.columns(2)
    with left:
        top = (view[view["Specialty"] != ""].groupby("Specialty")["NPI"].nunique()
               .nlargest(10).rename("Providers").reset_index())
        bar_chart(top, "Specialty", "Providers", "Top specialties by providers")
    with right:
        top = (view[view["City"] != ""].groupby("City")["NPI"].nunique()
               .nlargest(10).rename("Providers").reset_index())
        bar_chart(top, "City", "Providers", "Top cities by providers")

    left, right = st.columns(2)
    with left:
        top = (view.groupby("Billing Code")["NPI"].nunique().nlargest(10).rename("Providers").reset_index())
        bar_chart(top, "Billing Code", "Providers", "Most widely priced billing codes")
    with right:
        tier_counts = view["Cost Tier"].value_counts().reindex(TIERS).rename("Rates").reset_index()
        bar_chart(tier_counts, "Cost Tier", "Rates", "Rates by cost tier")


with analyst:
    st.caption("Click a column header to sort. Narrow the rows with the search box and filters on the left.")
    columns = ["NPI", "TIN", "Billing Code", "Specialty", "Address", "City", "ZIP",
               "Rate", "Code Median", "Rate Index", "Cost Tier"]
    st.dataframe(
        view[columns],
        hide_index=True,
        width="stretch",
        height=560,
        column_config={
            "Rate": st.column_config.NumberColumn(format="$%.2f"),
            "Code Median": st.column_config.NumberColumn(format="$%.2f", help="Median rate for this billing code"),
            "Rate Index": st.column_config.NumberColumn(format="%.2f", help="Rate ÷ code median (1.00 = market)"),
            "Address": st.column_config.TextColumn(width="large"),
        },
    )
    st.download_button("Download these rows (CSV)", view[columns].to_csv(index=False).encode(),
                       file_name="ppo_rates_filtered.csv", mime="text/csv")


with cost:
    st.caption(f"Each rate is compared with the median rate for the same billing code across the whole file. "
               f"Rate Index = rate ÷ code median; within ±{BAND:.0%} counts as at market. "
               f"Codes with only one rate in the file have no market to compare with and are left out.")
    priced = view[view["Cost Tier"] != SINGLE]
    if priced.empty:
        st.info("None of these rates has a market to compare with.")
        st.stop()

    k = st.columns(4)
    k[0].metric("Below market", f"{(priced['Cost Tier'] == BELOW).mean():.1%}")
    k[1].metric("Above market", f"{(priced['Cost Tier'] == ABOVE).mean():.1%}")
    k[2].metric("Median rate index", f"{priced['Rate Index'].median():.2f}")
    k[3].metric("Savings at median", f"${priced['Over Median'].sum():,.0f}",
                help="Sum over above-median rates of (rate − code median): the saving per one unit of every "
                     "service if they were paid the market rate. No utilization is in the file, so this is "
                     "per unit, not a total spend.")

    st.subheader("Specialties")
    spec = (priced[priced["Specialty"] != ""].groupby("Specialty")
            .agg(Providers=("NPI", "nunique"), Rates=("Rate", "size"),
                 **{"Median Rate Index": ("Rate Index", "median")},
                 **{"Above Market": ("Cost Tier", lambda t: (t == ABOVE).mean())},
                 **{"Savings at Median": ("Over Median", "sum")})
            .sort_values("Median Rate Index").reset_index())
    st.dataframe(spec, hide_index=True, width="stretch", height=360, column_config={
        "Median Rate Index": st.column_config.ProgressColumn(format="%.2f", min_value=0,
                                                             max_value=max(2.0, spec["Median Rate Index"].max())),
        "Above Market": st.column_config.NumberColumn(format="percent"),
        "Savings at Median": st.column_config.NumberColumn(format="$%.0f"),
    })

    st.subheader("Providers")
    st.caption(f"Providers (NPI + TIN) with at least {MIN_CODES} priced billing codes, ranked by their median rate index.")
    prov = (priced.groupby(["NPI", "TIN"])
            .agg(Specialty=("Specialty", "first"), City=("City", "first"),
                 Codes=("Billing Code", "nunique"),
                 **{"Median Rate Index": ("Rate Index", "median")},
                 **{"Savings at Median": ("Over Median", "sum")})
            .query("Codes >= @MIN_CODES").reset_index())
    provider_config = {
        "Median Rate Index": st.column_config.NumberColumn(format="%.2f"),
        "Savings at Median": st.column_config.NumberColumn(format="$%.0f"),
    }
    left, right = st.columns(2)
    with left:
        st.markdown("**Most cost-effective**")
        st.dataframe(prov.nsmallest(25, "Median Rate Index"), hide_index=True,
                     width="stretch", column_config=provider_config)
    with right:
        st.markdown("**Highest cost**")
        st.dataframe(prov.nlargest(25, "Median Rate Index"), hide_index=True,
                     width="stretch", column_config=provider_config)

    st.subheader("Rate spread by billing code")
    st.caption("How far apart the rates for one service are. A wide spread is where steering patients saves the most.")
    spread = (priced.groupby("Billing Code")["Rate"]
              .agg(Providers="size", Lowest="min", Median="median", Highest="max").reset_index())
    spread["Highest ÷ Lowest"] = spread["Highest"] / spread["Lowest"]
    spread = spread.sort_values("Highest ÷ Lowest", ascending=False)
    money = st.column_config.NumberColumn(format="$%.2f")
    st.dataframe(spread, hide_index=True, width="stretch", height=360, column_config={
        "Lowest": money, "Median": money, "Highest": money,
        "Highest ÷ Lowest": st.column_config.NumberColumn(format="%.1f×"),
    })
