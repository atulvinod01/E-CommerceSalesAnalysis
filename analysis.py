"""Raw transactions -> who to reward, who to win back, who to let go.

Run `python analysis.py`. On first run it downloads the UCI Online Retail II dataset,
loads it into SQLite (retail.db), and from then on reads from there. It writes:
  outputs/customer_actions.csv   one row per customer: R, F, M, scores, segment, action, priority
  outputs/segment_summary.csv    customers, revenue share and action per segment
  images/*.png                   growth, segment map and retention plots
"""
import io
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

ROOT = Path(__file__).parent
DB = ROOT / "retail.db"
URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"

# Segment map: frequency score (rows, 5 at top) x recency score (columns 1..5).
# Same layout as the map on atulvinod.com.
GRID = {
    5: ["At risk", "At risk", "Loyal", "Champions", "Champions"],
    4: ["Can't lose", "Can't lose", "Loyal", "Champions", "Champions"],
    3: ["Hibernating", "Need attention", "Need attention", "Potential loyalists", "Potential loyalists"],
    2: ["Lost", "Lost", "About to sleep", "Promising", "Promising"],
    1: ["Lost", "Lost", "About to sleep", "New customers", "New customers"],
}
# What to do with each segment. Lower priority number = spend budget there first.
ACTIONS = {
    "Champions": ("Reward", 2, "Loyalty offers, early access, referral asks"),
    "Loyal": ("Reward", 3, "Upsell and loyalty programme"),
    "At risk": ("Win back", 1, "Retention budget first: were frequent, now quiet"),
    "Can't lose": ("Win back", 1, "Retention budget first: personal outreach"),
    "Need attention": ("Win back", 4, "Time-limited offer before they slip"),
    "About to sleep": ("Win back", 5, "Low-cost reminder campaign"),
    "Potential loyalists": ("Nurture", 4, "Membership or second-purchase offer"),
    "Promising": ("Nurture", 5, "Onboarding and product discovery"),
    "New customers": ("Nurture", 5, "Welcome series"),
    "Hibernating": ("Let go", 6, "Generic newsletter only"),
    "Lost": ("Let go", 7, "No paid spend"),
}


def load_db():
    """Raw transactions in SQLite, downloading and importing the dataset on first run."""
    con = sqlite3.connect(DB)
    if con.execute("SELECT name FROM sqlite_master WHERE name='transactions'").fetchone():
        return con
    xlsx = ROOT / "online_retail_II.xlsx"
    csv = ROOT / "online_retail_II.csv"
    if csv.exists():
        raw = pd.read_csv(csv, encoding="latin-1")
    else:
        if not xlsx.exists():
            print("Downloading Online Retail II from UCI (45 MB)…")
            with urllib.request.urlopen(URL) as r, zipfile.ZipFile(io.BytesIO(r.read())) as z:
                xlsx.write_bytes(z.read("online_retail_II.xlsx"))
        print("Reading the spreadsheet (a minute or two, once)…")
        raw = pd.concat(pd.read_excel(xlsx, sheet_name=None, dtype={"Invoice": str, "StockCode": str}).values())
    raw = raw.drop_duplicates()
    raw.to_sql("transactions", con, index=False)
    print(f"Loaded {len(raw):,} rows into {DB.name}")
    return con


def clean(con):
    """Completed sales to known customers, then Z-score outlier removal on line revenue."""
    df = pd.read_sql("""
        SELECT Invoice, InvoiceDate, `Customer ID` AS CustomerID, Country,
               Quantity * Price AS Revenue
        FROM transactions
        WHERE Quantity > 0 AND Price > 0
          AND `Customer ID` IS NOT NULL
          AND Invoice NOT LIKE 'C%'          -- cancellations
    """, con, parse_dates=["InvoiceDate"])
    df["CustomerID"] = df["CustomerID"].astype(int)
    z = (df["Revenue"] - df["Revenue"].mean()) / df["Revenue"].std()
    return df[z.abs() < 3].copy()


def score(series, ascending=True):
    """Quintile score 1..5. Ranks first so heavy ties (e.g. many one-time buyers) still split."""
    s = pd.qcut(series.rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int)
    return s if ascending else 6 - s


def rfm(df):
    snapshot = df["InvoiceDate"].max() + pd.Timedelta(days=1)
    t = df.groupby("CustomerID").agg(
        Recency=("InvoiceDate", lambda x: (snapshot - x.max()).days),
        Frequency=("Invoice", "nunique"),
        Monetary=("Revenue", "sum"),
    )
    t["R"] = score(t["Recency"], ascending=False)  # recent buyers score 5
    t["F"] = score(t["Frequency"])
    t["M"] = score(t["Monetary"])
    t["Segment"] = [GRID[f][r - 1] for r, f in zip(t["R"], t["F"])]
    t["Action"] = t["Segment"].map(lambda s: ACTIONS[s][0])
    t["Priority"] = t["Segment"].map(lambda s: ACTIONS[s][1])
    t["Play"] = t["Segment"].map(lambda s: ACTIONS[s][2])
    # within a segment, the biggest spenders come first
    return t.sort_values(["Priority", "Monetary"], ascending=[True, False])


def summary(t):
    s = t.groupby("Segment").agg(Customers=("R", "size"), Revenue=("Monetary", "sum"),
                                 AvgRecencyDays=("Recency", "mean"), AvgOrders=("Frequency", "mean"))
    s["CustomerShare"] = s["Customers"] / s["Customers"].sum()
    s["RevenueShare"] = s["Revenue"] / s["Revenue"].sum()
    s["Action"] = s.index.map(lambda x: ACTIONS[x][0])
    s["Priority"] = s.index.map(lambda x: ACTIONS[x][1])
    s["Play"] = s.index.map(lambda x: ACTIONS[x][2])
    return s.sort_values(["Priority", "Revenue"], ascending=[True, False]).round(3)


def cohorts(df):
    d = df.assign(InvoiceMonth=df["InvoiceDate"].dt.to_period("M"))
    d["CohortMonth"] = d.groupby("CustomerID")["InvoiceMonth"].transform("min")
    d["CohortIndex"] = (d["InvoiceMonth"] - d["CohortMonth"]).apply(lambda x: x.n) + 1
    counts = d.groupby(["CohortMonth", "CohortIndex"])["CustomerID"].nunique().unstack()
    return counts.divide(counts[1], axis=0)


def plot_growth(df, path):
    m = df.set_index("InvoiceDate").resample("MS").agg({"Revenue": "sum", "CustomerID": "nunique"})
    m = m.iloc[:-1]  # the last month is partial
    fig, ax1 = plt.subplots(figsize=(12, 6))
    labels = m.index.strftime("%Y-%m")
    sns.barplot(x=labels, y=m["Revenue"], ax=ax1, color="skyblue", alpha=0.7)
    ax1.set(ylabel="Monthly revenue (£)", xlabel="")
    ax1.tick_params(axis="x", rotation=45)
    ax2 = ax1.twinx()
    sns.lineplot(x=labels, y=m["CustomerID"].to_numpy(), ax=ax2, color="coral", marker="o", linewidth=2)
    ax2.set_ylabel("Active customers", color="coral")
    ax2.grid(False)
    ax1.set_title("Are we growing sustainably? (Revenue vs active customers)", fontsize=15)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def plot_segments(t, path):
    counts = t.pivot_table(index="F", columns="R", values="M", aggfunc="size").reindex(
        index=[5, 4, 3, 2, 1], columns=[1, 2, 3, 4, 5]).fillna(0)
    rev = t.groupby("Segment")["Monetary"].sum() / t["Monetary"].sum()
    n = t["Segment"].value_counts()
    labels = [[f"{GRID[f][r - 1]}\n{int(counts.loc[f, r]):,} customers" for r in range(1, 6)] for f in counts.index]
    fig, (a, b) = plt.subplots(1, 2, figsize=(17, 6.5), gridspec_kw={"width_ratios": [3, 2]})
    seg_order = list(ACTIONS)
    code = [[seg_order.index(GRID[f][r - 1]) for r in range(1, 6)] for f in counts.index]
    sns.heatmap(code, annot=labels, fmt="", cmap="tab20", cbar=False, ax=a, linewidths=1,
                xticklabels=[1, 2, 3, 4, 5], yticklabels=[5, 4, 3, 2, 1], annot_kws={"fontsize": 8.5})
    a.set(xlabel="Recency score (5 = bought most recently)", ylabel="Frequency score (5 = most orders)",
          title="Who are our best customers? RFM segment map")
    s = pd.DataFrame({"Customers": n / n.sum(), "Revenue": rev}).loc[seg_order].iloc[::-1]
    s.plot.barh(ax=b, color=["#9ecae1", "#f26b1d"])
    b.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1))
    b.set(title="Share of customers vs share of revenue", ylabel="")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def plot_retention(ret, path):
    fig, ax = plt.subplots(figsize=(14, 9))
    sns.heatmap(ret.iloc[:, :13], annot=True, fmt=".0%", cmap="BuGn", ax=ax, cbar=False, annot_kws={"fontsize": 7})
    ax.grid(False)
    ax.set(title="Are we keeping new users? (Monthly retention by first-purchase cohort)",
           xlabel="Months since first purchase (1 = first month)", ylabel="Cohort")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def main():
    sns.set_style("whitegrid")
    con = load_db()
    df = clean(con)
    t = rfm(df)
    s = summary(t)
    t.to_sql("rfm", con, if_exists="replace")  # keep results next to the raw data
    (ROOT / "outputs").mkdir(exist_ok=True)
    (ROOT / "images").mkdir(exist_ok=True)
    t.to_csv(ROOT / "outputs/customer_actions.csv")
    s.to_csv(ROOT / "outputs/segment_summary.csv")
    plot_growth(df, ROOT / "images/plot_a_growth.png")
    plot_segments(t, ROOT / "images/plot_b_rfm.png")
    plot_retention(cohorts(df), ROOT / "images/plot_c_retention.png")
    print(f"{len(df):,} clean lines, {len(t):,} customers, "
          f"{df['InvoiceDate'].min():%d %b %Y} to {df['InvoiceDate'].max():%d %b %Y}")
    print(s[["Customers", "CustomerShare", "RevenueShare", "Action"]].to_string())
    return df, t, s


def _self_check():
    s = score(pd.Series([1, 1, 1, 1, 1, 2, 3, 4, 5, 6]))
    assert sorted(s.unique()) == [1, 2, 3, 4, 5]
    assert score(pd.Series(range(10)), ascending=False).iloc[0] == 5  # lowest recency -> score 5
    assert set(sum(GRID.values(), [])) == set(ACTIONS)
    d = pd.DataFrame({"Invoice": list("abcdefghij"), "CustomerID": range(10), "Revenue": range(1, 11),
                      "InvoiceDate": pd.date_range("2024-01-01", periods=10, freq="D")})
    t = rfm(d)
    assert t.loc[9, "R"] == 5 and t.loc[9, "M"] == 5 and t.loc[0, "R"] == 1


if __name__ == "__main__":
    _self_check()
    main()
