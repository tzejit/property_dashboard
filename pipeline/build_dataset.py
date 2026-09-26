from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm


# ============================================================
# Configuration
# ============================================================

MIN_HOLDING_YEARS = 1.0
MAX_HOLDING_YEARS = 30.0
MAX_AREA_CHANGE = 0.05
GFA_HARMONIZATION_CUTOFF = datetime.date(2023, 6, 1)
GFA_HARMONIZATION_DISCOUNT = 0.05
REFERENCE_YEAR = datetime.date.today().year
RECENT_CAGR_YEARS = 3  # look-back window for recent_cagr metric

# Expected raw transaction columns
TRANSACTION_COLUMNS = [
    "Project Name",
    "Transacted Price ($)",
    "Area (SQFT)",
    "Unit Price ($ PSF)",
    "Sale Date",
    "Address",
    "Type of Sale",
    "Type of Area",
    "Area (SQM)",
    "Unit Price ($ PSM)",
    "Nett Price($)",
    "Property Type",
    "Number of Units",
    "Tenure",
    "Completion Date",
    "Purchaser Address Indicator",
    "Postal Code",
    "Postal District",
    "Postal Sector",
    "Planning Region",
    "Planning Area",
    "detail_token",
]

LOCATION_COLUMNS = [
    "Postal Code",
    "Query Used",
    "Matched Address",
    "Latitude",
    "Longitude",
    "Nearest MRT Name",
    "Nearest MRT Distance (m)",
    "Nearest MRT Walk Time (min)",
    "Geocoded At",
]


# ============================================================
# Helpers
# ============================================================

def clean_money(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("$", "", regex=False)
        .str.strip()
        .replace({
            "": np.nan,
            "-": np.nan,
            "nan": np.nan,
            "None": np.nan,
        }),
        errors="coerce",
    )


def clean_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.strip()
        .replace({
            "": np.nan,
            "-": np.nan,
            "nan": np.nan,
            "None": np.nan,
        }),
        errors="coerce",
    )


def clean_postal(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return (
        numeric.round()
        .astype("Int64")
        .astype(str)
        .replace("<NA>", "")
        .str.zfill(6)
    )


def normalize_text(value) -> str:
    if pd.isna(value):
        return ""
    value = str(value).upper().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def normalize_address(value) -> str:
    return normalize_text(value)


def safe_pct(value):
    if pd.isna(value):
        return None
    return round(float(value), 4)


def safe_int(v):
    try:
        if v is None:
            return None
        if isinstance(v, float) and np.isnan(v):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_tenure(tenure_str: str) -> dict:
    """
    Parse URA tenure strings into a structured dict.

    Examples:
        "Freehold"
        "99 yrs lease commencing from 2010"
        "999 yrs lease commencing from 1985"
        "103 yrs lease commencing from 1987"
    """
    s = str(tenure_str).upper().strip()
    if not s or s in ("", "NAN", "NONE", "-"):
        return {"type": None, "duration": None, "start_year": None}
    if "FREEHOLD" in s:
        return {"type": "freehold", "duration": None, "start_year": None}
    m = re.match(r"(\d+)\s+YRS?\s+LEASE\s+COMMENCING\s+FROM\s+(\d{4})", s)
    if m:
        return {
            "type": "leasehold",
            "duration": int(m.group(1)),
            "start_year": int(m.group(2)),
        }
    m = re.match(r"(\d+)\s+YRS?", s)
    if m:
        return {"type": "leasehold", "duration": int(m.group(1)), "start_year": None}
    return {"type": "other", "duration": None, "start_year": None}


def compute_lease_remaining(
    duration: int | None,
    start_year: int | None,
    ref_year: int = REFERENCE_YEAR,
) -> int | None:
    if duration is None or start_year is None:
        return None
    return max(0, duration - (ref_year - start_year))


# ============================================================
# Load transaction data
# ============================================================

def load_transactions(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)

    missing = [c for c in TRANSACTION_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Transaction CSV is missing expected columns:\n"
            + "\n".join(f"  - {c}" for c in missing)
        )

    out = pd.DataFrame()

    out["project_name"] = df["Project Name"].map(normalize_text)
    out["address"] = df["Address"].map(normalize_address)
    out["transaction_date"] = pd.to_datetime(
        df["Sale Date"], errors="coerce", dayfirst=True,
    )
    out["price"] = clean_money(df["Transacted Price ($)"])
    out["floor_area_sqft"] = clean_numeric(df["Area (SQFT)"])
    out["psf"] = clean_numeric(df["Unit Price ($ PSF)"])
    out["sale_type"] = df["Type of Sale"].fillna("").astype(str).str.strip()
    out["area_type"] = df["Type of Area"].fillna("").astype(str).str.strip()
    out["floor_area_sqm"] = clean_numeric(df["Area (SQM)"])
    out["psm"] = clean_numeric(df["Unit Price ($ PSM)"])
    out["nett_price"] = clean_money(df["Nett Price($)"])
    out["property_type"] = df["Property Type"].fillna("").astype(str).str.strip()
    out["number_of_units"] = clean_numeric(df["Number of Units"])
    out["tenure"] = df["Tenure"].fillna("").astype(str).str.strip()
    out["completion_date"] = pd.to_datetime(
        df["Completion Date"], errors="coerce", dayfirst=True,
    )
    out["purchaser_address_indicator"] = (
        df["Purchaser Address Indicator"].fillna("").astype(str).str.strip()
    )
    out["postal_code"] = clean_postal(df["Postal Code"])
    out["district"] = df["Postal District"].map(normalize_text)
    out["postal_sector"] = df["Postal Sector"].map(normalize_text)
    out["region"] = df["Planning Region"].map(normalize_text)
    out["planning_area"] = df["Planning Area"].map(normalize_text)
    out["detail_token"] = df["detail_token"].fillna("").astype(str)

    out = out.dropna(subset=["transaction_date", "price", "floor_area_sqft"])
    out = out[
        (out["price"] > 0)
        & (out["floor_area_sqft"] > 0)
    ].copy()

    out["transaction_year"] = out["transaction_date"].dt.year
    out["_unit_key"] = out["project_name"] + " | " + out["address"]
    out["asset_class"] = "Private"

    return out.reset_index(drop=True)


# ============================================================
# Load HDB resale transaction data
# ============================================================

def load_hdb_transactions(path: str) -> pd.DataFrame:
    """
    Load HDB resale flat prices CSV (data.gov.sg dataset
    d_8b84c4ee58e3cfc0ece0d773c8ca6abc) and normalise to the
    internal transaction schema.

    Key differences from URA condo data:
    - No unit number. Use storey_range + flat_type as unit proxy.
    - Floor area is in sqm. Convert to sqft.
    - All transactions are Resale type.
    - Tenure is always 99-year leasehold from lease_commence_date.
    - No postal code → no lat/lon from postal_locations.
    - GFA harmonization does not apply to HDB.
    """
    df = pd.read_csv(path, dtype=str, low_memory=False)

    required = {
        "month", "town", "flat_type", "block", "street_name",
        "storey_range", "floor_area_sqm", "lease_commence_date", "resale_price",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"HDB CSV missing columns: {missing}")

    out = pd.DataFrame()

    # project_name = block + street_name (the HDB block address).
    out["project_name"] = (
        df["block"].str.strip() + " " + df["street_name"].str.strip()
    ).map(normalize_text)

    # address = project_name + storey range + flat type.
    # This is the best available unit proxy (no unit numbers in HDB data).
    out["address"] = (
        out["project_name"]
        + " "
        + df["storey_range"].str.strip()
        + " "
        + df["flat_type"].str.strip()
    ).map(normalize_address)

    # transaction_date: HDB data provides month (YYYY-MM). Use the 1st.
    out["transaction_date"] = pd.to_datetime(
        df["month"].str.strip() + "-01", format="%Y-%m-%d", errors="coerce"
    )

    out["price"] = pd.to_numeric(df["resale_price"], errors="coerce")

    sqm = pd.to_numeric(df["floor_area_sqm"], errors="coerce")
    out["floor_area_sqm"]  = sqm
    out["floor_area_sqft"] = (sqm * 10.7639).round(1)

    out["psf"] = (out["price"] / out["floor_area_sqft"]).round(2)
    out["psm"] = (out["price"] / sqm).round(2)

    out["sale_type"] = "Resale"
    # area_type stores the flat type so PSF series are labelled correctly.
    out["area_type"] = df["flat_type"].str.strip().str.title()

    # Tenure in URA format so parse_tenure() handles it correctly.
    out["tenure"] = (
        "99 yrs lease commencing from " + df["lease_commence_date"].str.strip()
    )

    # completion_date: use lease_commence_date as year proxy.
    lease_year = pd.to_numeric(df["lease_commence_date"], errors="coerce")
    out["completion_date"] = pd.to_datetime(
        lease_year.astype("Int64").astype(str) + "-01-01",
        format="%Y-%m-%d",
        errors="coerce",
    )

    # HDB uses town as planning area. No postal code, district, region.
    out["planning_area"]             = df["town"].str.strip().map(normalize_text)
    out["region"]                    = None
    out["district"]                  = None
    out["postal_sector"]             = None
    out["postal_code"]               = None

    # Fields present in condo schema but not in HDB data.
    out["nett_price"]                = out["price"]
    out["property_type"]             = "HDB Resale"
    out["number_of_units"]           = None
    out["purchaser_address_indicator"] = None
    out["detail_token"]              = ""

    out["asset_class"] = "HDB"

    # Drop rows with null price, area, or date.
    out = out.dropna(subset=["transaction_date", "price", "floor_area_sqft"])
    out = out[(out["price"] > 0) & (out["floor_area_sqft"] > 0)].copy()

    out["transaction_year"] = out["transaction_date"].dt.year
    out["_unit_key"] = out["project_name"] + " | " + out["address"]

    return out.reset_index(drop=True)


# ============================================================
# Load location data
# ============================================================

def load_locations(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)

    missing = [c for c in LOCATION_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Location CSV is missing expected columns:\n"
            + "\n".join(f"  - {c}" for c in missing)
        )

    out = pd.DataFrame()
    out["postal_code"] = clean_postal(df["Postal Code"])
    out["latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    out["longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")
    out["nearest_mrt"] = (
        df["Nearest MRT Name"].fillna("").astype(str).replace("nan", "")
    )
    out["nearest_mrt_distance_m"] = pd.to_numeric(
        df["Nearest MRT Distance (m)"], errors="coerce",
    )
    out["nearest_mrt_walk_min"] = pd.to_numeric(
        df["Nearest MRT Walk Time (min)"], errors="coerce",
    )
    out["matched_address"] = df["Matched Address"].fillna("").astype(str)

    return out.drop_duplicates("postal_code").reset_index(drop=True)


def load_hdb_locations(path: str) -> pd.DataFrame:
    """
    Load HDB block geocoding results written by pipeline/geocode_hdb.py.

    Columns: project_name, latitude, longitude,
             nearest_mrt, nearest_mrt_distance_m.
    """
    df = pd.read_csv(path, dtype=str, low_memory=False)
    out = pd.DataFrame()
    out["project_name"]           = df["project_name"].str.strip().str.upper()
    out["latitude"]               = pd.to_numeric(df["latitude"],               errors="coerce")
    out["longitude"]              = pd.to_numeric(df["longitude"],               errors="coerce")
    out["nearest_mrt"]            = df["nearest_mrt"].fillna("").str.strip()
    out["nearest_mrt_distance_m"] = pd.to_numeric(df["nearest_mrt_distance_m"], errors="coerce")
    return out.drop_duplicates("project_name").reset_index(drop=True)


# ============================================================
# Repeat-sale construction
# ============================================================

def build_repeat_sales(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build consecutive repeat-sale pairs using vectorised pandas operations.

    The logic is equivalent to the previous per-unit Python loop:
    - Sort all rows by (_unit_key, transaction_date) once.
    - A pair is any two adjacent rows with the same _unit_key (shift-based).
    - Apply all holding-year, area-change, price, and CAGR filters in bulk.
    - Build the result DataFrame from aligned column arrays (no per-row dicts).
    """
    # Sort once — O(n log n).
    df_s = (
        df.sort_values(["_unit_key", "transaction_date"])
        .reset_index(drop=True)
    )

    # Identify consecutive same-group pairs.
    # same_group[i] is True when row i and row i-1 share the same _unit_key.
    same_group = (df_s["_unit_key"] == df_s["_unit_key"].shift(1)).to_numpy()

    sale_idx     = np.where(same_group)[0]
    purchase_idx = sale_idx - 1

    if len(sale_idx) == 0:
        return pd.DataFrame()

    sale     = df_s.iloc[sale_idx].reset_index(drop=True)
    purchase = df_s.iloc[purchase_idx].reset_index(drop=True)

    # ---- Vectorised filters ----
    holding_days  = (
        sale["transaction_date"].values - purchase["transaction_date"].values
    ).astype("timedelta64[D]").astype(float)
    holding_years = holding_days / 365.2425

    # Clip purchase area to avoid division by zero before area_change filter.
    purchase_area = purchase["floor_area_sqft"].to_numpy()
    sale_area     = sale["floor_area_sqft"].to_numpy()
    area_change   = np.abs(sale_area - purchase_area) / np.where(
        purchase_area > 0, purchase_area, 1.0
    )

    keep = (
        (holding_years >= MIN_HOLDING_YEARS)
        & (holding_years <= MAX_HOLDING_YEARS)
        & (purchase_area > 0)
        & (sale_area > 0)
        & (purchase["price"].to_numpy() > 0)
        & (sale["price"].to_numpy() > 0)
        & (area_change <= MAX_AREA_CHANGE)
    )

    sale          = sale[keep].reset_index(drop=True)
    purchase      = purchase[keep].reset_index(drop=True)
    holding_years = holding_years[keep]
    area_change   = area_change[keep]

    cagr = (
        (sale["price"].to_numpy() / purchase["price"].to_numpy())
        ** (1.0 / holding_years)
        - 1.0
    )

    cagr_keep     = (cagr > -0.95) & (cagr <= 2.0)
    sale          = sale[cagr_keep].reset_index(drop=True)
    purchase      = purchase[cagr_keep].reset_index(drop=True)
    holding_years = holding_years[cagr_keep]
    area_change   = area_change[cagr_keep]
    cagr          = cagr[cagr_keep]

    if sale.empty:
        return pd.DataFrame()

    # Format dates as ISO strings (matches the original isoformat() output).
    purchase_dates = purchase["transaction_date"].dt.strftime("%Y-%m-%d")
    sale_dates     = sale["transaction_date"].dt.strftime("%Y-%m-%d")
    comp_dt        = sale["completion_date"]
    completion_dates = np.where(
        comp_dt.isna().to_numpy(),
        None,
        comp_dt.dt.strftime("%Y-%m-%d").to_numpy(),
    )

    return pd.DataFrame({
        "unit_key":           sale["_unit_key"].to_numpy(),
        "project_name":       sale["project_name"].to_numpy(),
        "address":            sale["address"].to_numpy(),
        "purchase_date":      purchase_dates.to_numpy(),
        "sale_date":          sale_dates.to_numpy(),
        "purchase_year":      purchase["transaction_date"].dt.year.to_numpy(),
        "sale_year":          sale["transaction_date"].dt.year.to_numpy(),
        "holding_years":      holding_years,
        "purchase_price":     purchase["price"].to_numpy(),
        "sale_price":         sale["price"].to_numpy(),
        "purchase_area_sqft": purchase_area[keep][cagr_keep],
        "sale_area_sqft":     sale_area[keep][cagr_keep],
        "area_change_pct":    area_change * 100.0,
        "cagr":               cagr,
        "cagr_pct":           cagr * 100.0,
        "profitable":         (sale["price"].to_numpy() > purchase["price"].to_numpy()),
        "purchase_type":      purchase["sale_type"].to_numpy(),
        "sale_type":          sale["sale_type"].to_numpy(),
        "property_type":      sale["property_type"].to_numpy(),
        "tenure":             sale["tenure"].to_numpy(),
        "completion_date":    completion_dates,
        "postal_code":        sale["postal_code"].to_numpy(),
        "district":           sale["district"].to_numpy(),
        "postal_sector":      sale["postal_sector"].to_numpy(),
        "region":             sale["region"].to_numpy(),
        "planning_area":      sale["planning_area"].to_numpy(),
        "asset_class": (
            sale["asset_class"].to_numpy()
            if "asset_class" in sale.columns
            else np.full(len(sale), "Private", dtype=object)
        ),
    })


# ============================================================
# Project-level stats from full transactions
# ============================================================

def compute_project_stats(
    transactions: pd.DataFrame,
    ref_year: int = REFERENCE_YEAR,
) -> pd.DataFrame:
    """
    Compute per-project statistics that require the full transactions dataset:
    total unique unit addresses, build year, house age, GFA harmonization flag,
    and parsed tenure info with remaining lease.

    Heavy aggregations run once via groupby (vectorised). Per-project tenure
    parsing still runs in a loop, but only processes one scalar string per
    project instead of iterating over the whole DataFrame.
    """
    gfa_cutoff = pd.Timestamp(
        datetime.datetime.combine(GFA_HARMONIZATION_CUTOFF, datetime.time())
    )

    # ---- Vectorised aggregations ----

    # Distinct unit count per project.
    total_units = (
        transactions.groupby("project_name", observed=True)["address"]
        .nunique()
    )

    # Build year: mode of completion_date year per project.
    has_comp = transactions.dropna(subset=["completion_date"]).copy()
    has_comp["_comp_year"] = has_comp["completion_date"].dt.year
    build_year = (
        has_comp.groupby("project_name", observed=True)["_comp_year"]
        .agg(lambda s: int(s.mode().iloc[0]) if len(s) else None)
    )

    # GFA harmonization: earliest New Sale date per project.
    new_sales = transactions[
        transactions["sale_type"].str.upper().str.strip() == "NEW SALE"
    ]
    earliest_new_sale = (
        new_sales.groupby("project_name", observed=True)["transaction_date"]
        .min()
    )

    # Tenure mode per project (exclude blank/NaN).
    tenures_valid = transactions.copy()
    tenures_valid["tenure"] = tenures_valid["tenure"].replace("", np.nan)
    tenure_mode = (
        tenures_valid.dropna(subset=["tenure"])
        .groupby("project_name", observed=True)["tenure"]
        .agg(lambda s: s.mode().iloc[0] if len(s) else "")
    )

    # ---- Per-project loop — only scalar operations remain ----
    records = []
    for project_name in total_units.index:
        build_yr    = build_year.get(project_name)
        # build_year agg returns None for projects with no completion data;
        # NaN-safe check handles the case where pandas returns NaN instead.
        if build_yr is not None and (
            isinstance(build_yr, float) and np.isnan(build_yr)
        ):
            build_yr = None
        house_age   = ref_year - build_yr if build_yr is not None else None

        # GFA harmonization.
        earliest = earliest_new_sale.get(project_name)
        gfa_harmonized = bool(
            earliest is not None
            and pd.notna(earliest)
            and earliest >= gfa_cutoff
        )

        # Tenure parsing — still per-project, but only one string each.
        tenure_str  = str(tenure_mode.get(project_name, ""))
        tenure_info = parse_tenure(tenure_str)
        # Use build_year as fallback when the tenure string has no start year
        # (e.g. "99 yrs" without "commencing from YYYY"). Singapore leasehold
        # periods typically start from the year of TOP/completion.
        effective_start_year = tenure_info["start_year"] or build_yr
        lease_remaining = compute_lease_remaining(
            tenure_info["duration"],
            effective_start_year,
            ref_year,
        )

        records.append({
            "project_name":   project_name,
            "total_units":    int(total_units[project_name]),
            "build_year":     build_yr,
            "house_age":      house_age,
            "gfa_harmonized": gfa_harmonized,
            "tenure_type":    tenure_info["type"],
            "lease_duration": tenure_info["duration"],
            "lease_start_year": effective_start_year,
            "lease_remaining":  lease_remaining,
        })

    return pd.DataFrame(records).set_index("project_name")


# ============================================================
# PSF and CAGR monthly time series per project
# ============================================================

def build_project_time_series(
    transactions: pd.DataFrame,
    repeat_sales: pd.DataFrame,
    project_filter: set | None = None,
) -> dict:
    """
    Returns compact time series for projects that have repeat-sale data.

    Output format (space-efficient arrays):
        {project_name: {
            "p":  [["YYYY-MM", psf_int, n], ...],   PSF monthly (all transactions)
            "c":  [["YYYY-MM", cagr_2dp, n], ...],  CAGR all purchase types
            "cn": [...],   CAGR — New Sale purchases only (omitted if empty)
            "cs": [...],   CAGR — Sub Sale purchases only (omitted if empty)
            "cr": [...],   CAGR — Resale purchases only   (omitted if empty)
        }}

    CAGR is always from raw prices. No lease or GFA adjustment is applied here.
    Only projects in project_filter are included; pass None to include all.
    """
    tx = transactions.dropna(subset=["psf", "transaction_date"]).copy()
    tx = tx[tx["psf"] > 0]
    tx["month"] = tx["transaction_date"].dt.to_period("M").astype(str)

    rs = repeat_sales.copy()
    rs["_sale_dt"]     = pd.to_datetime(rs["sale_date"],     errors="coerce")
    rs["_purchase_dt"] = pd.to_datetime(rs["purchase_date"], errors="coerce")
    rs = rs.dropna(subset=["_sale_dt"])
    rs["month"]          = rs["_sale_dt"].dt.to_period("M").astype(str)
    rs["purchase_month"] = rs["_purchase_dt"].dt.to_period("M").astype(str)
    # Purchase PSF: price paid at buy-in divided by unit area ($/sqft)
    rs["purchase_psf"] = rs["purchase_price"] / rs["purchase_area_sqft"]

    # Restrict to the requested project set
    if project_filter is not None:
        tx = tx[tx["project_name"].isin(project_filter)]
        rs = rs[rs["project_name"].isin(project_filter)]

    projects = (
        set(tx["project_name"].unique())
        | set(rs["project_name"].unique())
    )

    # Slice by purchase type once, then pre-group each slice by project.
    # This turns every per-project lookup from an O(n_pairs) scan into an
    # O(1) dict get followed by groupby on only that project's rows.
    rs_by_type = {
        "cn": rs[rs["purchase_type"] == "New Sale"],
        "cs": rs[rs["purchase_type"] == "Sub Sale"],
        "cr": rs[rs["purchase_type"] == "Resale"],
    }

    def _group_by_project(subset: pd.DataFrame) -> dict[str, pd.DataFrame]:
        return {
            proj: sub
            for proj, sub in subset.groupby("project_name", observed=True)
        }

    rs_by_project          = _group_by_project(rs)
    rs_by_type_by_project  = {k: _group_by_project(v) for k, v in rs_by_type.items()}
    tx_by_project          = _group_by_project(tx)

    _EMPTY_RS = rs.iloc[:0]
    _EMPTY_TX = tx.iloc[:0]

    def _cagr_series(proj_rs: pd.DataFrame) -> list:
        """Median CAGR by sale month for one project's repeat-sale rows."""
        if proj_rs.empty:
            return []
        grp = (
            proj_rs.groupby("month")
            .agg(cagr=("cagr_pct", "median"), n=("cagr_pct", "count"))
            .reset_index()
            .sort_values("month")
        )
        return [
            [m, round(c, 2), int(n)]
            for m, c, n in zip(grp["month"], grp["cagr"], grp["n"])
        ]

    def _purchase_psf_series(proj_rs: pd.DataFrame) -> list:
        """Median purchase PSF by PURCHASE month — shows buy-in price over time."""
        sub = proj_rs[proj_rs["purchase_psf"].notna() & (proj_rs["purchase_psf"] > 0)]
        if sub.empty:
            return []
        grp = (
            sub.groupby("purchase_month")
            .agg(psf=("purchase_psf", "median"), n=("purchase_psf", "count"))
            .reset_index()
            .sort_values("purchase_month")
        )
        return [
            [m, int(round(p)), int(n)]
            for m, p, n in zip(grp["purchase_month"], grp["psf"], grp["n"])
        ]

    def _purchase_base_series(proj_rs: pd.DataFrame) -> list:
        """
        Median purchase PSF grouped by SALE month — the base entry PSF for
        each CAGR data point. For a pair sold in May-2022, this is what the
        buyer paid at the original purchase leg.
        """
        sub = proj_rs[proj_rs["purchase_psf"].notna() & (proj_rs["purchase_psf"] > 0)]
        if sub.empty:
            return []
        grp = (
            sub.groupby("month")        # sale month — same grouping as CAGR
            .agg(psf=("purchase_psf", "median"), n=("purchase_psf", "count"))
            .reset_index()
            .sort_values("month")
        )
        return [
            [m, int(round(p)), int(n)]
            for m, p, n in zip(grp["month"], grp["psf"], grp["n"])
        ]

    def _cagr_by_purchase_series(proj_rs: pd.DataFrame) -> list:
        """
        Median CAGR grouped by PURCHASE month — purchase-cohort view.
        "Buyers who entered in May-2022 achieved median CAGR of X%"
        across all their eventual sale dates.
        """
        if proj_rs.empty:
            return []
        grp = (
            proj_rs.groupby("purchase_month")
            .agg(cagr=("cagr_pct", "median"), n=("cagr_pct", "count"))
            .reset_index()
            .sort_values("purchase_month")
        )
        return [
            [m, round(c, 2), int(n)]
            for m, c, n in zip(grp["purchase_month"], grp["cagr"], grp["n"])
        ]

    result = {}

    for project in projects:
        proj_tx = tx_by_project.get(project, _EMPTY_TX)
        if not proj_tx.empty:
            grp = (
                proj_tx.groupby("month")
                .agg(psf=("psf", "median"), n=("psf", "count"))
                .reset_index()
                .sort_values("month")
            )
            psf_series = [
                [m, int(round(p)), int(n)]
                for m, p, n in zip(grp["month"], grp["psf"], grp["n"])
            ]
        else:
            psf_series = []

        proj_rs = rs_by_project.get(project, _EMPTY_RS)
        pp = _purchase_psf_series(proj_rs)
        pb = _purchase_base_series(proj_rs)
        cp = _cagr_by_purchase_series(proj_rs)
        entry: dict = {
            "p": psf_series,
            "c": _cagr_series(proj_rs),
        }
        if pp:
            entry["pp"] = pp  # purchase PSF by purchase month (buy-in trend)
        if pb:
            entry["pb"] = pb  # purchase PSF by sale month (base used in CAGR)
        if cp:
            entry["cp"] = cp  # CAGR by purchase month (cohort view)

        # Per-purchase-type CAGR series — omit key if empty to save space
        for key, proj_rs_by_type in rs_by_type_by_project.items():
            s = _cagr_series(proj_rs_by_type.get(project, _EMPTY_RS))
            if s:
                entry[key] = s

        result[project] = entry

    return result


def build_size_time_series(
    transactions: pd.DataFrame,
    repeat_sales: pd.DataFrame,
    project_filter: set | None = None,
    min_pairs: int = 3,
    psf_area_tol: int = 50,
) -> dict:
    """
    Per (project, sqft) time series. Key format: "project_name|sqft".

    PSF series is included for every size bucket that has any transactions within
    ±psf_area_tol sqft of the bucket area. This brings in more transactions per
    bucket so that projects with few exact-area matches still show PSF data.
    CAGR series is included only for buckets with at least min_pairs repeat-sale pairs.
    CAGR pairs always use exact area matching (unchanged).

    Output format: same compact arrays as build_project_time_series.
    """
    tx = transactions.dropna(subset=["psf", "transaction_date"]).copy()
    tx = tx[tx["psf"] > 0]
    tx["month"] = tx["transaction_date"].dt.to_period("M").astype(str)
    tx["_area"] = tx["floor_area_sqft"].round(0).astype(int)

    rs = repeat_sales.copy()
    rs["_sale_dt"]     = pd.to_datetime(rs["sale_date"],     errors="coerce")
    rs["_purchase_dt"] = pd.to_datetime(rs["purchase_date"], errors="coerce")
    rs = rs.dropna(subset=["_sale_dt"])
    rs["month"]          = rs["_sale_dt"].dt.to_period("M").astype(str)
    rs["purchase_month"] = rs["_purchase_dt"].dt.to_period("M").astype(str)
    rs["purchase_psf"]   = rs["purchase_price"] / rs["purchase_area_sqft"]
    rs["_area"] = rs["purchase_area_sqft"].round(0).astype(int)

    if project_filter is not None:
        tx = tx[tx["project_name"].isin(project_filter)]
        rs = rs[rs["project_name"].isin(project_filter)]

    rs_by_type = {
        "cn": rs[rs["purchase_type"] == "New Sale"],
        "cs": rs[rs["purchase_type"] == "Sub Sale"],
        "cr": rs[rs["purchase_type"] == "Resale"],
    }

    def _cagr_series_sz(project_sub: pd.DataFrame, area: int) -> list:
        """Median CAGR by sale month for one (project, area) bucket.
        `project_sub` is already filtered to the project; only area is applied here."""
        sub = project_sub[project_sub["_area"] == area]
        if sub.empty:
            return []
        grp = (
            sub.groupby("month")
            .agg(cagr=("cagr_pct", "median"), n=("cagr_pct", "count"))
            .reset_index()
            .sort_values("month")
        )
        return [
            [m, round(c, 2), int(n)]
            for m, c, n in zip(grp["month"], grp["cagr"], grp["n"])
        ]

    def _purchase_psf_sz(project_sub: pd.DataFrame, area: int) -> list:
        """Median purchase PSF by purchase month for one (project, area) bucket."""
        sub = project_sub[
            (project_sub["_area"] == area)
            & project_sub["purchase_psf"].notna()
            & (project_sub["purchase_psf"] > 0)
        ]
        if sub.empty:
            return []
        grp = (
            sub.groupby("purchase_month")
            .agg(psf=("purchase_psf", "median"), n=("purchase_psf", "count"))
            .reset_index()
            .sort_values("purchase_month")
        )
        return [
            [m, int(round(p)), int(n)]
            for m, p, n in zip(grp["purchase_month"], grp["psf"], grp["n"])
        ]

    def _purchase_base_sz(project_sub: pd.DataFrame, area: int) -> list:
        """Median purchase PSF by SALE month — base entry PSF for each CAGR point."""
        sub = project_sub[
            (project_sub["_area"] == area)
            & project_sub["purchase_psf"].notna()
            & (project_sub["purchase_psf"] > 0)
        ]
        if sub.empty:
            return []
        grp = (
            sub.groupby("month")  # sale month
            .agg(psf=("purchase_psf", "median"), n=("purchase_psf", "count"))
            .reset_index()
            .sort_values("month")
        )
        return [
            [m, int(round(p)), int(n)]
            for m, p, n in zip(grp["month"], grp["psf"], grp["n"])
        ]

    def _cagr_by_purchase_sz(project_sub: pd.DataFrame, area: int) -> list:
        """CAGR by purchase month — cohort view for a specific size bucket."""
        sub = project_sub[project_sub["_area"] == area]
        if sub.empty:
            return []
        grp = (
            sub.groupby("purchase_month")
            .agg(cagr=("cagr_pct", "median"), n=("cagr_pct", "count"))
            .reset_index()
            .sort_values("purchase_month")
        )
        return [
            [m, round(c, 2), int(n)]
            for m, c, n in zip(grp["purchase_month"], grp["cagr"], grp["n"])
        ]

    # All (project, area) buckets that have any transactions — drives PSF series.
    tx_buckets = (
        tx.groupby(["project_name", "_area"])
        .size()
        .reset_index(name="_tx_n")
    )
    tx_buckets = tx_buckets[tx_buckets["_tx_n"] > 0]
    tx_bucket_keys = set(zip(tx_buckets["project_name"], tx_buckets["_area"]))

    # All (project, area) buckets from repeat-sale pairs.
    all_pair_buckets = (
        rs.groupby(["project_name", "_area"])
        .size()
        .reset_index(name="_pair_n")
    )
    all_pair_bucket_keys = set(
        zip(all_pair_buckets["project_name"], all_pair_buckets["_area"])
    )
    # Buckets that have enough repeat-sale pairs — for CAGR series.
    pair_counts = all_pair_buckets[all_pair_buckets["_pair_n"] >= min_pairs]
    cagr_keys = set(
        zip(pair_counts["project_name"], pair_counts["_area"])
    )

    # Iterate the union: tx-defined buckets PLUS pair-defined buckets.
    # Pair-defined buckets without exact tx matches still need a CAGR entry.
    all_bucket_keys = tx_bucket_keys | all_pair_bucket_keys

    # Pre-group transactions by project so each bucket only scans its own project rows.
    # This avoids a full-table scan per bucket (critical: 33k+ buckets × 510k rows).
    tx_by_project: dict[str, pd.DataFrame] = {
        proj: sub for proj, sub in tx.groupby("project_name", observed=True)
    }
    rs_by_project: dict[str, pd.DataFrame] = {
        proj: sub for proj, sub in rs.groupby("project_name", observed=True)
    }
    rs_by_type_by_project: dict[str, dict[str, pd.DataFrame]] = {
        ts_key: {
            proj: sub for proj, sub in rs_sub.groupby("project_name", observed=True)
        }
        for ts_key, rs_sub in rs_by_type.items()
    }

    _EMPTY_TX_SZ = tx.iloc[:0]  # reused empty frame — avoids repeated allocation
    _EMPTY_RS_SZ = rs.iloc[:0]

    result = {}

    for (project, area) in sorted(all_bucket_keys):
        key = f"{project}|{area}"

        # PSF: use project-filtered rows, then range-filter by area.
        proj_tx_all = tx_by_project.get(project)
        if proj_tx_all is None or proj_tx_all.empty:
            proj_tx = _EMPTY_TX_SZ
        else:
            proj_tx = proj_tx_all[
                (proj_tx_all["floor_area_sqft"] >= area - psf_area_tol)
                & (proj_tx_all["floor_area_sqft"] <= area + psf_area_tol)
            ]
        grp = (
            proj_tx.groupby("month")
            .agg(
                psf=("psf", "median"),
                n=("psf", "count"),
                median_area=("floor_area_sqft", "median"),
            )
            .reset_index()
            .sort_values("month")
        )
        # Format: [YYYY-MM, psf_int, n, median_area_sqft_int]
        # median_area shows the typical unit size transacted that month.
        psf_series = [
            [m, int(round(p)), int(n), int(round(a))]
            for m, p, n, a in zip(
                grp["month"], grp["psf"], grp["n"], grp["median_area"]
            )
        ]

        entry: dict = {"p": psf_series}

        # Per-purchase-type PSF series. Used by the frontend to select the base
        # price for CAGR calculation by purchase type.
        # pn = New Sale, ps = Sub Sale, pr = Resale.
        if "sale_type" in proj_tx.columns and not proj_tx.empty:
            for ts_psf_key, sale_type_str in [
                ("pn", "New Sale"), ("ps", "Sub Sale"), ("pr", "Resale")
            ]:
                type_tx = proj_tx[proj_tx["sale_type"] == sale_type_str]
                if type_tx.empty:
                    continue
                type_grp = (
                    type_tx.groupby("month")
                    .agg(psf=("psf", "median"), n=("psf", "count"))
                    .reset_index()
                    .sort_values("month")
                )
                if type_grp.empty:
                    continue
                entry[ts_psf_key] = [
                    [m, int(round(p)), int(n)]
                    for m, p, n in zip(
                        type_grp["month"], type_grp["psf"], type_grp["n"]
                    )
                ]

        # Only add pair-based CAGR series for buckets with enough pairs.
        if (project, area) in cagr_keys:
            # Use pre-grouped project subsets for fast area-level filtering.
            proj_rs = rs_by_project.get(project, _EMPTY_RS_SZ)
            c = _cagr_series_sz(proj_rs, area)
            if c:
                entry["c"] = c
            pp = _purchase_psf_sz(proj_rs, area)
            pb = _purchase_base_sz(proj_rs, area)
            cp = _cagr_by_purchase_sz(proj_rs, area)
            if pp:
                entry["pp"] = pp
            if pb:
                entry["pb"] = pb
            if cp:
                entry["cp"] = cp
            for ts_key, proj_rs_by_type in rs_by_type_by_project.items():
                proj_rs_sub = proj_rs_by_type.get(project, _EMPTY_RS_SZ)
                s = _cagr_series_sz(proj_rs_sub, area)
                if s:
                    entry[ts_key] = s

        result[key] = entry

    return result


# ============================================================
# Statistical summaries
# ============================================================

def summarize_group(repeat_sales: pd.DataFrame, column: str) -> list:

    records = []

    for value, group in repeat_sales.groupby(column, dropna=True):

        if len(group) == 0:
            continue

        records.append({
            column: value,
            "n": int(len(group)),
            "median_cagr": safe_pct(group["cagr_pct"].median()),
            "p25_cagr": safe_pct(group["cagr_pct"].quantile(.25)),
            "p75_cagr": safe_pct(group["cagr_pct"].quantile(.75)),
            "profit_rate": safe_pct(group["profitable"].mean() * 100),
            "median_holding_years": safe_pct(group["holding_years"].median()),
        })

    return records


def make_size_buckets(df):

    bins = [
        0, 400, 600, 800, 900, 1100, 1200, 1400,
        1600, 1800, 2000, 2200, 2400, 2600, np.inf,
    ]
    labels = [
        "<400", "400–600", "600–800", "800–900", "900–1,100",
        "1,100–1,200", "1,200–1,400", "1,400–1,600", "1,600–1,800",
        "1,800–2,000", "2,000–2,200", "2,200–2,400", "2,400–2,600", "2,600+",
    ]

    temp = df.copy()
    temp["size_bucket"] = pd.cut(
        temp["purchase_area_sqft"], bins=bins, labels=labels, right=False,
    )

    return summarize_group(temp, "size_bucket")


# ============================================================
# Controlled statistical model
# ============================================================

def fit_controlled_model(repeat_sales):

    if len(repeat_sales) < 100:
        return {
            "available": False,
            "reason": "Insufficient repeat-sale observations.",
        }

    df = repeat_sales.copy()
    df["log_area"] = np.log(df["purchase_area_sqft"])
    df["holding_sq"] = df["holding_years"] ** 2

    X = pd.DataFrame({
        "log_area": df["log_area"],
        "holding_years": df["holding_years"],
        "holding_sq": df["holding_sq"],
    })

    year_dummies = pd.get_dummies(
        df["purchase_year"], prefix="year", drop_first=True, dtype=float,
    )
    region_dummies = pd.get_dummies(
        df["region"], prefix="region", drop_first=True, dtype=float,
    )
    type_dummies = pd.get_dummies(
        df["purchase_type"], prefix="type", drop_first=True, dtype=float,
    )

    X = pd.concat([X, year_dummies, region_dummies, type_dummies], axis=1)
    X = sm.add_constant(X)
    y = df["cagr_pct"].astype(float)
    X = X.astype(float)

    model = sm.OLS(y, X).fit(cov_type="HC3")
    coefficient = model.params.get("log_area", np.nan)
    ci = model.conf_int()

    if "log_area" in ci.index:
        ci_low = ci.loc["log_area", 0]
        ci_high = ci.loc["log_area", 1]
    else:
        ci_low = np.nan
        ci_high = np.nan

    return {
        "available": True,
        "n": int(len(df)),
        "r_squared": safe_pct(model.rsquared),
        "log_area_coefficient": safe_pct(coefficient),
        "log_area_ci_low": safe_pct(ci_low),
        "log_area_ci_high": safe_pct(ci_high),
        "log_area_p_value": safe_pct(model.pvalues.get("log_area", np.nan)),
        "note": "Association, not causation. HC3 robust standard errors.",
    }


# ============================================================
# School proximity
# ============================================================

def load_schools(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    out = pd.DataFrame()
    out["name"] = df["name"].fillna("").astype(str).str.strip()
    out["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    out["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    return out.dropna(subset=["latitude", "longitude"]).reset_index(drop=True)


def compute_school_proximity(
    repeat_sales: pd.DataFrame,
    schools: pd.DataFrame,
    max_radius_m: float = 2000.0,
) -> dict:
    """
    For each project, find schools within max_radius_m.
    Uses one representative location per project (first non-null lat/lon).
    Returns {project_name: [{n: name, d: dist_m}, ...]} sorted by distance.
    """
    proj_locs = (
        repeat_sales[["project_name", "latitude", "longitude"]]
        .dropna(subset=["latitude", "longitude"])
        .drop_duplicates("project_name")
        .reset_index(drop=True)
    )

    if proj_locs.empty or schools.empty:
        return {}

    sch_lat = np.radians(schools["latitude"].values)
    sch_lon = np.radians(schools["longitude"].values)
    sch_names = schools["name"].tolist()
    R = 6_371_000.0

    result = {}
    for _, row in proj_locs.iterrows():
        lat1 = np.radians(float(row["latitude"]))
        lon1 = np.radians(float(row["longitude"]))
        dlat = sch_lat - lat1
        dlon = sch_lon - lon1
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(sch_lat) * np.sin(dlon / 2) ** 2
        dist = R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
        mask = dist <= max_radius_m
        if not mask.any():
            continue
        idx = np.argsort(dist[mask])
        near_dist = dist[mask][idx]
        near_names = [sch_names[i] for i in np.where(mask)[0][idx]]
        result[row["project_name"]] = [
            {"n": nm, "d": int(round(d))}
            for nm, d in zip(near_names, near_dist)
        ]

    return result


# ============================================================
# Project records
# ============================================================

def make_project_records(
    repeat_sales: pd.DataFrame,
    project_stats: pd.DataFrame,
    transactions: pd.DataFrame | None = None,
    psf_area_tol: int = 50,
) -> list:
    """
    Build one record per (project_name, purchase_area_sqft) from repeat-sale pairs.

    `median_psf` is included when `transactions` is supplied. It is the median
    PSF of transactions within ±psf_area_tol sqft of the bucket area that fall
    in the latest calendar quarter (Q1=Jan–Mar, Q2=Apr–Jun, Q3=Jul–Sep,
    Q4=Oct–Dec) that has data for that bucket. When the latest quarter is empty,
    it falls back to the single most recent transaction PSF.
    """

    if repeat_sales.empty:
        return []

    # Pre-filter transactions for PSF lookup.
    if transactions is not None:
        tx_psf = transactions.dropna(subset=["psf"]).copy()
        tx_psf = tx_psf[tx_psf["psf"] > 0]
        # Pre-group by project — turns O(n_all_tx) PSF scan per bucket into
        # O(n_project_tx) by looking up only that project's rows.
        tx_psf_by_project: dict[str, pd.DataFrame] = {
            proj: sub
            for proj, sub in tx_psf.groupby("project_name", observed=True)
        }

        # Pre-compute PSF CAGR and last-transacted per (project, area_bucket).
        # Doing this once on the full table is far faster than a per-bucket groupby
        # inside the loop (45,000+ iterations → one vectorised pass).
        _pre = tx_psf.copy()
        _pre["_ab"]      = _pre["floor_area_sqft"].round(0).astype(int)
        _pre["_q_start"] = _pre["transaction_date"].dt.to_period("Q").dt.to_timestamp()

        # Median PSF per (project, area_bucket, quarter).
        _q_agg = (
            _pre.groupby(["project_name", "_ab", "_q_start"])["psf"]
            .median()
            .reset_index()
            .sort_values("_q_start")
        )

        _psf_cagr_map:     dict[tuple, float | None] = {}
        _psf_momentum_map: dict[tuple, float | None] = {}
        for (proj, ab), grp in _q_agg.groupby(["project_name", "_ab"]):
            if len(grp) < 2:
                _psf_cagr_map[(proj, ab)]     = None
                _psf_momentum_map[(proj, ab)] = None
                continue
            first_q = grp["_q_start"].iloc[0]
            last_q  = grp["_q_start"].iloc[-1]
            years   = (last_q - first_q).days / 365.25
            if years < 1.0 or grp["psf"].iloc[0] <= 0:
                _psf_cagr_map[(proj, ab)] = None
            else:
                raw = (grp["psf"].iloc[-1] / grp["psf"].iloc[0]) ** (1 / years) - 1
                _psf_cagr_map[(proj, ab)] = round(raw * 100, 4)
            # 1-year PSF momentum: last 4 quarters vs prior 4 quarters.
            # Requires at least 8 quarters of data.
            if len(grp) >= 8:
                recent_psf = grp["psf"].iloc[-4:].median()
                prior_psf  = grp["psf"].iloc[-8:-4].median()
                if prior_psf > 0:
                    _psf_momentum_map[(proj, ab)] = round(
                        (recent_psf / prior_psf - 1) * 100, 4
                    )
                else:
                    _psf_momentum_map[(proj, ab)] = None
            else:
                _psf_momentum_map[(proj, ab)] = None
    else:
        tx_psf = None
        tx_psf_by_project = {}
        _psf_cagr_map     = {}
        _psf_momentum_map = {}

    # Round areas to the nearest whole sqft before grouping.
    # Raw transaction data reports areas to 2 d.p., so the same unit type
    # can appear as 900.00 / 900.25 / 900.50 — all meaning "900 sqft 2-bed".
    # Rounding merges these near-duplicates without changing the analysis intent.
    rs = repeat_sales.copy()
    rs["purchase_area_sqft"] = rs["purchase_area_sqft"].round(0)

    def _first_valid(grp: pd.DataFrame, col: str):
        """Return the first non-null, non-empty value in `col`, or None."""
        if col not in grp.columns:
            return None
        vals = grp[col].dropna()
        # For string columns, also skip empty strings so that a later row
        # with a real value (e.g. a real MRT name) wins over an earlier "".
        if vals.dtype == object:
            vals = vals[vals.astype(str).str.strip() != ""]
        return vals.iloc[0] if not vals.empty else None

    def _mode_str(grp: pd.DataFrame, col: str) -> str:
        """Return the modal non-null value of `col` as a str, or '' if all null."""
        if col not in grp.columns:
            return ""
        m = grp[col].dropna().mode()
        return str(m.iloc[0]) if not m.empty else ""

    records = []

    for (project, area), group in rs.groupby(
        ["project_name", "purchase_area_sqft"]
    ):
        n = len(group)

        postal = group["postal_code"].dropna().mode()
        postal_code = str(postal.iloc[0]) if not postal.empty else ""

        # Restrict location lookups to rows matching the representative postal
        # code. A large project can span multiple postal codes (different
        # blocks), each with its own lat/lon/nearest MRT. Without this filter,
        # `postal_code` (the group's modal postal code) and the location
        # fields (previously first-valid over the WHOLE group) could come
        # from different blocks, showing a postal code paired with another
        # block's MRT/distance.
        loc_group = group[group["postal_code"] == postal_code] if postal_code else group

        lat      = _first_valid(loc_group, "latitude")
        lon      = _first_valid(loc_group, "longitude")
        mrt      = _first_valid(loc_group, "nearest_mrt")
        mrt_dist = _first_valid(loc_group, "nearest_mrt_distance_m")

        g_new = group[group["purchase_type"] == "New Sale"]
        g_sub = group[group["purchase_type"] == "Sub Sale"]
        g_res = group[group["purchase_type"] == "Resale"]

        # Compute PSF from transactions within ±psf_area_tol sqft.
        # Use median of the latest calendar quarter (Q1=Jan–Mar, Q2=Apr–Jun,
        # Q3=Jul–Sep, Q4=Oct–Dec) that has data for this bucket.
        # Fall back to the single most recent transaction when the latest
        # quarter is empty (should not occur, but defensive).
        median_psf_val   = None
        psf_cagr_val     = None
        psf_momentum_val = None
        tx_per_year_val  = None
        bucket_tx        = pd.DataFrame()
        if transactions is not None:
            area_int = int(round(area))
            proj_tx_psf = tx_psf_by_project.get(project)
            if proj_tx_psf is not None:
                mask = (
                    (proj_tx_psf["floor_area_sqft"] >= area_int - psf_area_tol)
                    & (proj_tx_psf["floor_area_sqft"] <= area_int + psf_area_tol)
                )
                bucket_tx = proj_tx_psf.loc[mask]
            else:
                bucket_tx = pd.DataFrame()
            if not bucket_tx.empty:
                latest_date = bucket_tx["transaction_date"].max()
                # Start of the calendar quarter that contains latest_date.
                q_start_month = ((latest_date.month - 1) // 3) * 3 + 1
                q_start = pd.Timestamp(latest_date.year, q_start_month, 1)
                recent = bucket_tx[bucket_tx["transaction_date"] >= q_start]
                if not recent.empty:
                    median_psf_val = round(float(recent["psf"].median()), 0)
                else:
                    # Fall back: PSF of the single most recent transaction.
                    last_row = bucket_tx.loc[bucket_tx["transaction_date"].idxmax()]
                    median_psf_val = round(float(last_row["psf"]), 0)

                # PSF CAGR and 1-year momentum — pre-computed lookups.
                psf_cagr_val     = _psf_cagr_map.get((project, area_int))
                psf_momentum_val = _psf_momentum_map.get((project, area_int))

                # Transaction velocity: avg number of transactions per year.
                tx_span_days = (
                    bucket_tx["transaction_date"].max()
                    - bucket_tx["transaction_date"].min()
                ).days
                tx_per_year_val = (
                    round(len(bucket_tx) / (tx_span_days / 365.25), 1)
                    if tx_span_days >= 180  # need at least 6 months of history
                    else None
                )

        # Recent CAGR: pairs with sale date within the last RECENT_CAGR_YEARS years.
        _recent_cutoff = (
            datetime.date.today()
            - datetime.timedelta(days=RECENT_CAGR_YEARS * 365)
        ).strftime("%Y-%m-%d")
        g_recent    = group[group["sale_date"] >= _recent_cutoff]
        recent_cagr = safe_pct(g_recent["cagr_pct"].median()) if len(g_recent) >= 3 else None
        n_recent    = int(len(g_recent))

        # CAGR spread (p75 − p25): narrower = more consistent across pairs.
        p25 = safe_pct(group["cagr_pct"].quantile(.25))
        p75 = safe_pct(group["cagr_pct"].quantile(.75))
        cagr_spread = round(p75 - p25, 4) if p25 is not None and p75 is not None else None

        ac_vals = group["asset_class"] if "asset_class" in group.columns else pd.Series(["Private"])
        ac_mode = ac_vals.mode()
        asset_class_val = ac_mode.iloc[0] if not ac_mode.empty else "Private"

        rec = {
            "project_name": project,
            "purchase_area_sqft": area,
            "asset_class": str(asset_class_val),
            "n": int(n),
            "median_cagr": safe_pct(group["cagr_pct"].median()),
            "p25_cagr": p25,
            "p75_cagr": p75,
            "cagr_spread":  cagr_spread,
            "recent_cagr":  recent_cagr,
            "n_recent":     n_recent,
            "profit_rate": safe_pct(group["profitable"].mean() * 100),
            "median_holding_years": safe_pct(group["holding_years"].median()),
            "cagr_new": safe_pct(g_new["cagr_pct"].median()) if len(g_new) >= 3 else None,
            "n_new": int(len(g_new)),
            "cagr_sub": safe_pct(g_sub["cagr_pct"].median()) if len(g_sub) >= 3 else None,
            "n_sub": int(len(g_sub)),
            "cagr_res": safe_pct(g_res["cagr_pct"].median()) if len(g_res) >= 3 else None,
            "n_res": int(len(g_res)),
            "median_psf":      int(median_psf_val) if median_psf_val is not None else None,
            "psf_cagr":        psf_cagr_val,
            "psf_momentum":    psf_momentum_val,
            "tx_per_year":     tx_per_year_val if transactions is not None else None,
            "last_transacted": (
                bucket_tx["transaction_date"].max().strftime("%Y-%m")
                if transactions is not None and not bucket_tx.empty
                else None
            ),
            "region":       _mode_str(group, "region"),
            "planning_area": _mode_str(group, "planning_area"),
            "district":     _mode_str(group, "district"),
            "postal_code": postal_code,
            "latitude": round(float(lat), 6) if lat is not None else None,
            "longitude": round(float(lon), 6) if lon is not None else None,
            "nearest_mrt": (
                str(mrt) if mrt and str(mrt) not in ("", "nan") else ""
            ),
            "nearest_mrt_distance_m": (
                round(float(mrt_dist), 1) if mrt_dist is not None else None
            ),
        }

        if project in project_stats.index:
            s = project_stats.loc[project]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[0]
            rec.update({
                "build_year": safe_int(s.get("build_year")),
                "house_age": safe_int(s.get("house_age")),
                "total_units": safe_int(s.get("total_units")),
                "gfa_harmonized": bool(s.get("gfa_harmonized", False)),
                "tenure_type": str(s.get("tenure_type") or ""),
                "lease_duration": safe_int(s.get("lease_duration")),
                "lease_start_year": safe_int(s.get("lease_start_year")),
                "lease_remaining": safe_int(s.get("lease_remaining")),
            })
        else:
            rec.update({
                "build_year": None,
                "house_age": None,
                "total_units": None,
                "gfa_harmonized": False,
                "tenure_type": "",
                "lease_duration": None,
                "lease_start_year": None,
                "lease_remaining": None,
            })

        records.append(rec)

    return sorted(
        records,
        key=lambda x: x["n"] if x["n"] is not None else -999,
        reverse=True,
    )


# ============================================================
# Main pipeline
# ============================================================

def build_dataset(
    transactions_path,
    locations_path,
    output_path,
    schools_path=None,
    hdb_path=None,
    hdb_locations_path=None,
):
    print("Loading transactions...")
    transactions = load_transactions(transactions_path)
    print(f"Loaded {len(transactions):,} valid transactions.")

    if hdb_path:
        print("Loading HDB resale transactions...")
        hdb_tx = load_hdb_transactions(hdb_path)
        transactions = pd.concat([transactions, hdb_tx], ignore_index=True)
        print(f"Total after HDB merge: {len(transactions):,} transactions.")

    print("Computing project stats...")
    project_stats = compute_project_stats(transactions)
    print(f"Computed stats for {len(project_stats):,} projects.")

    print("Building repeat-sale pairs...")
    repeat_sales = build_repeat_sales(transactions)
    print(f"Built {len(repeat_sales):,} repeat-sale pairs.")

    if repeat_sales.empty:
        raise ValueError(
            "No valid repeat-sale observations were created."
        )

    print("Loading postal locations...")
    locations = load_locations(locations_path)

    repeat_sales = repeat_sales.merge(
        locations,
        on="postal_code",
        how="left",
    )

    # Fill lat/lon/mrt for HDB rows using block-address geocoding.
    # HDB rows have no postal code, so the postal merge leaves them null.
    if hdb_locations_path and Path(hdb_locations_path).exists():
        print("Loading HDB block locations...")
        hdb_locs = load_hdb_locations(hdb_locations_path)
        hdb_loc_idx = hdb_locs.set_index("project_name")
        null_lat = repeat_sales["latitude"].isna()
        hdb_mask = null_lat & repeat_sales["project_name"].str.upper().isin(hdb_loc_idx.index)
        if hdb_mask.any():
            pnames = repeat_sales.loc[hdb_mask, "project_name"].str.upper()
            for col in ["latitude", "longitude", "nearest_mrt", "nearest_mrt_distance_m"]:
                if col in hdb_loc_idx.columns:
                    repeat_sales.loc[hdb_mask, col] = pnames.map(hdb_loc_idx[col]).values
            print(f"  Filled location data for {hdb_mask.sum():,} HDB repeat-sale rows.")

    repeat_sale_projects = set(repeat_sales["project_name"].unique())
    print("Building project time series...")
    time_series = build_project_time_series(
        transactions, repeat_sales, project_filter=repeat_sale_projects,
    )
    print(f"Built time series for {len(time_series):,} projects.")

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = {
        "transactions": int(len(transactions)),
        "repeat_sales": int(len(repeat_sales)),
        "projects": int(repeat_sales["project_name"].nunique()),
        "median_cagr": safe_pct(repeat_sales["cagr_pct"].median()),
        "mean_cagr": safe_pct(repeat_sales["cagr_pct"].mean()),
        "p25_cagr": safe_pct(repeat_sales["cagr_pct"].quantile(.25)),
        "p75_cagr": safe_pct(repeat_sales["cagr_pct"].quantile(.75)),
        "p95_cagr": safe_pct(repeat_sales["cagr_pct"].quantile(.95)),
        "profit_rate": safe_pct(repeat_sales["profitable"].mean() * 100),
        "median_holding_years": safe_pct(repeat_sales["holding_years"].median()),
    }

    # --------------------------------------------------------
    # Website payload
    # --------------------------------------------------------

    payload = {
        "meta": {
            "schema": "Singapore Condo Analytics v3",
            "source_schema": "URA-style raw transaction data",
            "repeat_sale_definition":
                "Same project + exact address including unit number",
            "min_holding_years": MIN_HOLDING_YEARS,
            "max_holding_years": MAX_HOLDING_YEARS,
            "max_area_change_pct": MAX_AREA_CHANGE * 100,
            "gfa_harmonization_cutoff":
                GFA_HARMONIZATION_CUTOFF.isoformat(),
            "gfa_harmonization_discount": GFA_HARMONIZATION_DISCOUNT,
            "reference_year": REFERENCE_YEAR,
        },

        "summary": summary,

        "projects": make_project_records(repeat_sales, project_stats, transactions),

        "regions": summarize_group(repeat_sales, "region"),

        "planning_areas": summarize_group(repeat_sales, "planning_area"),

        "districts": summarize_group(repeat_sales, "district"),

        "purchase_cohorts": [],

        "size_buckets": make_size_buckets(repeat_sales),
    }

    # Purchase cohorts
    cohort = repeat_sales.copy()
    cohort["cohort"] = pd.cut(
        cohort["purchase_year"],
        bins=[1999, 2004, 2009, 2014, 2019, 2026],
        labels=["2000–04", "2005–09", "2010–14", "2015–19", "2020–26"],
    )
    payload["purchase_cohorts"] = summarize_group(cohort, "cohort")

    # --------------------------------------------------------
    # Write JSON
    # --------------------------------------------------------

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, allow_nan=False)

    print(f"Dataset written to: {output_path}")

    # Write compact time series to a separate file (loaded lazily by the site).
    ts_path = output_path.parent / "ts.json"
    with ts_path.open("w", encoding="utf-8") as f:
        json.dump(time_series, f, separators=(",", ":"), allow_nan=False)

    ts_kb = ts_path.stat().st_size / 1024
    print(f"Time series written to: {ts_path} ({ts_kb:,.0f} KB)")

    # Per-size time series (for modal chart bucketed by floor area)
    print("Building size-specific time series...")
    ts_size = build_size_time_series(
        transactions, repeat_sales, project_filter=repeat_sale_projects,
    )
    ts_size_path = output_path.parent / "ts_size.json"
    with ts_size_path.open("w", encoding="utf-8") as f:
        json.dump(ts_size, f, separators=(",", ":"), allow_nan=False)
    ts_size_kb = ts_size_path.stat().st_size / 1024
    print(
        f"Size time series written to: {ts_size_path} "
        f"({ts_size_kb:,.0f} KB, {len(ts_size):,} size buckets)"
    )

    # School proximity (optional — only written if --schools is supplied)
    if schools_path:
        print("Loading schools...")
        schools_df = load_schools(schools_path)
        print(f"Loaded {len(schools_df):,} schools.")
        print("Computing school proximity...")
        school_proximity = compute_school_proximity(repeat_sales, schools_df)
        print(f"Computed proximity for {len(school_proximity):,} projects.")
        sch_path = output_path.parent / "schools.json"
        with sch_path.open("w", encoding="utf-8") as f:
            json.dump(school_proximity, f, separators=(",", ":"), allow_nan=False)
        sch_kb = sch_path.stat().st_size / 1024
        print(f"Schools written to: {sch_path} ({sch_kb:,.0f} KB)")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Build Singapore condominium repeat-sales analytics dataset."
    )

    parser.add_argument(
        "--input",
        default="data/combined_transactions.csv",
        help="Raw transaction CSV",
    )

    parser.add_argument(
        "--locations",
        default="data/postal_locations.csv",
        help="Postal/location CSV",
    )

    parser.add_argument(
        "--output",
        default="site/data.json",
        help="Output site/data.json",
    )

    parser.add_argument(
        "--schools",
        default=None,
        help="MOE primary schools geocoded CSV (optional). Writes site/schools.json.",
    )

    parser.add_argument(
        "--hdb",
        default=None,
        help=(
            "HDB resale flat prices CSV from data.gov.sg "
            "(dataset d_8b84c4ee58e3cfc0ece0d773c8ca6abc). "
            "When supplied, HDB transactions are merged with the URA "
            "condo data before all pipeline stages run."
        ),
    )

    parser.add_argument(
        "--hdb-locations",
        default=None,
        dest="hdb_locations",
        help=(
            "HDB block locations CSV produced by pipeline/geocode_hdb.py. "
            "When supplied, lat/lon/MRT data from this file is merged onto "
            "HDB repeat-sale rows that have no postal-code location."
        ),
    )

    args = parser.parse_args()

    build_dataset(
        transactions_path=args.input,
        locations_path=args.locations,
        output_path=args.output,
        schools_path=args.schools,
        hdb_path=args.hdb,
        hdb_locations_path=args.hdb_locations,
    )
