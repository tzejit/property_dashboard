# Data Calculations

This document lists every formula and rule used in the pipeline and frontend.
All CAGR values use raw transaction prices. No lease adjustment and no GFA adjustment is ever applied to CAGR.

---

## 1. Input Data Source

The pipeline reads the raw URA REALIS transaction CSV. Key columns used:

| CSV Column | Internal Field | Notes |
|---|---|---|
| `Transacted Price ($)` | `price` | Total transacted price in SGD |
| `Area (SQFT)` | `floor_area_sqft` | Strata floor area in square feet |
| `Unit Price ($ PSF)` | `psf` | Read directly from URA; not recalculated |
| `Sale Date` | `transaction_date` | Parsed with `dayfirst=True` |
| `Type of Sale` | `sale_type` | "New Sale", "Sub Sale", or "Resale" |
| `Completion Date` | `completion_date` | Used for `build_year` |
| `Tenure` | `tenure` | Parsed for lease type and duration |

Rows are dropped when `transaction_date`, `price`, or `floor_area_sqft` is null, or when `price <= 0` or `floor_area_sqft <= 0`.

---

## 2. Unit Key (Repeat-Sale Matching)

Each transaction is assigned a `_unit_key`:

```
_unit_key = project_name + " | " + address
```

`address` includes the unit number (for example, `#15-07`). Two transactions match as a repeat-sale pair only when their `_unit_key` is identical. This ensures the same physical unit is tracked.

---

## 3. Repeat-Sale Pair Construction

Transactions are sorted by `(_unit_key, transaction_date)`. For each unit with multiple transactions, consecutive pairs are formed. For each pair:

- `purchase` = earlier transaction
- `sale` = later transaction

**Filters applied (all must pass):**

| Filter | Rule | Constant |
|---|---|---|
| Minimum holding | `holding_years >= 1.0` | `MIN_HOLDING_YEARS = 1.0` |
| Maximum holding | `holding_years <= 30.0` | `MAX_HOLDING_YEARS = 30.0` |
| Non-zero areas | `purchase_area > 0` and `sale_area > 0` | — |
| Area consistency | `|sale_area - purchase_area| / purchase_area <= 0.05` | `MAX_AREA_CHANGE = 0.05` (5%) |
| Non-zero prices | `purchase_price > 0` and `sale_price > 0` | — |
| CAGR plausibility | `-0.95 < CAGR <= 2.0` | Hard-coded outlier cutoff |

The area consistency filter removes pairs where the recorded area changed by more than 5% between transactions. This catches data errors or genuinely different unit listings filed under the same address.

The CAGR plausibility filter removes pairs with CAGR below −95% (near-total loss, likely data error) or above 200% annualised (implausible for real estate).

**Only consecutive pairs are used** (transaction i−1 and i for each unit). All pairwise combinations are not used. This avoids double-counting the same capital gain.

---

## 4. Holding Period

```
holding_days  = (sale_date − purchase_date).days
holding_years = holding_days / 365.2425
```

`365.2425` is the mean Gregorian year length. It accounts for leap years more accurately than 365 or 365.25.

---

## 5. CAGR (Compound Annual Growth Rate)

```
CAGR = (sale_price / purchase_price) ^ (1 / holding_years) − 1
```

Stored as `cagr` (decimal) and `cagr_pct = cagr × 100` (percentage).

CAGR is computed from **raw prices only**. No adjustment for remaining lease and no adjustment for GFA harmonisation is ever applied.

---

## 6. Purchase Type Assignment

Each repeat-sale pair is classified by the **purchase leg's** `sale_type`:

| Value | Meaning |
|---|---|
| `"New Sale"` | Unit was originally bought directly from developer |
| `"Sub Sale"` | Unit was on-sold before completion |
| `"Resale"` | Unit was bought in the secondary market |

This classifies **how the owner entered the market**, not how they exited. A unit originally bought as a New Sale and later resold is counted as a "New Sale" pair.

---

## 7. PSF (Price Per Square Foot)

The **resale PSF** for each transaction is read directly from the URA column `Unit Price ($ PSF)`. It is not recalculated.

The **purchase PSF** used in time-series tooltips is computed from the repeat-sale pair:

```
purchase_psf = purchase_price / purchase_area_sqft
```

This is necessary because the URA source provides PSF for each individual transaction, but the repeat-sale dataset links two transactions. The purchase-leg PSF is recalculated from its price and area.

---

## 8. Median PSF (Monthly)

For each `(project, YYYY-MM)` group, the pipeline computes:

```
monthly_psf = median(psf) across all transactions in that month
n           = count of transactions
```

This uses **all transactions** (all purchase types, all unit sizes), not only those in repeat-sale pairs. It represents the market price of the project in that month.

For per-size PSF (`ts_size.json`), transactions whose `floor_area_sqft` is within ±50 sqft of the bucket area are included. Each monthly entry also records the median transacted area that month (`median_area`), so the chart tooltip can show how the typical unit size varies over time. This tolerance brings in more transactions per bucket so that projects with few exact-area matches still show meaningful PSF data. The 50 sqft window is consistent with the repeat-sale pair area-change limit (5% of 900 sqft ≈ 45 sqft).

---

## 9. Median CAGR (Monthly — Sale-Date View)

For each `(project, sale_YYYY-MM)` group:

```
monthly_cagr = median(cagr_pct) across all pairs whose sale_date falls in that month
n            = count of pairs
```

Per-purchase-type variants (`cn`, `cs`, `cr`) apply an additional filter:

```
cn: purchase_type == "New Sale"
cs: purchase_type == "Sub Sale"
cr: purchase_type == "Resale"
```

A per-type key is omitted from the output when the filtered group is empty.

**Why median?** The median is robust to outliers. A few high-CAGR pairs in one month do not distort the headline figure the way a mean would.

---

## 10. Purchase-Date Cohort CAGR (`cp` key)

For each `(project, purchase_YYYY-MM)` group:

```
cohort_cagr = median(cagr_pct) across all pairs whose purchase_date falls in that month
n           = count of pairs
```

This answers: "Buyers who entered in May 2022 eventually achieved a median CAGR of X%."
The sale date of those pairs is irrelevant — only when they bought is grouped.

---

## 11. Purchase Base PSF (`pb` key)

For each `(project, sale_YYYY-MM)` group:

```
pb = median(purchase_psf) across all pairs whose sale_date falls in that month
```

This is the **median buy-in price** of sellers who sold in a given month. It is shown in the chart tooltip as "bought @ $X psf" alongside the CAGR value.

`pb` groups by **sale month** (same grouping as CAGR) so each tooltip can show both the return and the entry price for the same cohort.

---

## 12. Purchase PSF by Purchase Month (`pp` key)

For each `(project, purchase_YYYY-MM)` group:

```
pp = median(purchase_psf) across all pairs whose purchase_date falls in that month
```

This shows the reference buy-in price trend over time, plotted on the chart as an orange dashed line.

---

## 13. Per-Size vs. Project-Wide Series

`ts.json` contains one entry per project. PSF and CAGR use all transactions and all pairs for that project regardless of unit size.

`ts_size.json` contains one entry per `(project, sqft)` bucket where `sqft = round(floor_area_sqft)`. PSF is included for every size bucket with any transactions. CAGR is included only when the bucket has at least **3 repeat-sale pairs**. The chart uses `ts_size.json` exclusively — it does not fall back to project-wide data.

---

## 14. Modal PSF+CAGR Chart CAGR (Frontend, Market-Index Based)

The pipeline computes matched repeat-sale-pair CAGR series (`c`, `cn`, `cs`, `cr`, `cp`, `pb` — see sections 9–12) into `ts.json` and `ts_size.json`. **The modal chart does not use these keys.** `renderPsfCagrChart()` in `app.js` computes its own CAGR client-side from the quarterly PSF index instead, aggregated from `raw.p`/`raw.pn`/`raw.ps`/`raw.pr` (per selected purchase type) in `ts_size.json`. This is a market-index CAGR (how the average price of similar-sized units moved), not a matched-pair CAGR — it can differ from the project's `median_cagr` table column for the same project/bucket, because it compares different units transacted at different times rather than tracking one physical unit's two transactions.

**Sale-date view** (`getChartAxis() === "sale"`, default):

```
base_month = earliest monthly entry of the purchase-type-specific PSF series (raw.p / pn / ps / pr)
             that is on or after the date-filter start month (#chart-date-min);
             the series' first entry when no start-date filter is set
base_psf   = PSF value at base_month
for each chart quarter Q (after base_month's quarter):
  months_elapsed = months from base_month to the start of Q
  CAGR = (PSF[Q] / base_psf) ^ (12 / months_elapsed) − 1
```

Moving the start-date filter moves the CAGR base with it — the displayed return is always measured from the filtered starting point forward, not from the project's earliest recorded transaction.

**Purchase-date view** (`getChartAxis() === "purchase"`):

```
latest_Q = most recent quarter with market PSF data (raw.p, all purchase types)
for each purchase quarter Q (before latest_Q):
  months_held = months from Q to latest_Q
  CAGR = (latest_psf / buy_in_psf_at_Q) ^ (12 / months_held) − 1
```

Both views skip quarters with no PSF data (no interpolation across gaps). CAGR is computed from raw PSF only — GFA/lease adjustments apply to the displayed PSF bars, never to the CAGR line.

There is no rolling fixed-lookback (6/12/24/36 month) selector in the current frontend. `subtractMonths()` in `app.js` is unused.

---

## 14b. Recent PSF per Size Bucket

Each project record in `data.json` includes a `median_psf` field:

```
bucket_tx = transactions where |floor_area_sqft − area| ≤ 50 sqft
latest_date = max(transaction_date in bucket_tx)
q_start     = start of the calendar quarter containing latest_date
              (Q1=Jan–Mar, Q2=Apr–Jun, Q3=Jul–Sep, Q4=Oct–Dec)
recent_tx   = bucket_tx where transaction_date >= q_start

if recent_tx is not empty:
    median_psf = median(psf) across recent_tx
else:
    median_psf = psf of the single most recent transaction in bucket_tx
```

The ±50 sqft tolerance is the same as the PSF series tolerance in `ts_size.json`. The window is the **current calendar quarter of the latest transaction**, not a rolling 3-month lookback — if the latest transaction fell only days into a new quarter, `recent_tx` can cover just those few days rather than a full 3 months. The fallback (single most recent transaction) is defensive and should not normally trigger, since the latest transaction is always inside its own quarter. The PSF filter in the explorer uses this value.

---

## 15. Project-Level CAGR Summary Fields

For each `(project_name, purchase_area_sqft)` group in `data.json`:

| Field | Formula |
|---|---|
| `median_cagr` | `median(cagr_pct)` across all pairs in the group |
| `p25_cagr` | 25th percentile of `cagr_pct` |
| `p75_cagr` | 75th percentile of `cagr_pct` |
| `cagr_new` | `median(cagr_pct)` for New Sale pairs; `null` when fewer than 3 pairs |
| `cagr_sub` | `median(cagr_pct)` for Sub Sale pairs; `null` when fewer than 3 pairs |
| `cagr_res` | `median(cagr_pct)` for Resale pairs; `null` when fewer than 3 pairs |
| `profit_rate` | `mean(profitable) × 100` where `profitable = sale_price > purchase_price` |
| `median_holding_years` | `median(holding_years)` |

`purchase_area_sqft` is rounded to the nearest whole sqft before grouping. This merges near-duplicate records (e.g. 900.00 and 900.25 sqft) that refer to the same unit type.

The minimum pair count for `cagr_new/sub/res` is 3. Below 3 pairs, the per-type median is too sensitive to individual outliers.

---

## 16. Profit Rate

```
profit_rate = count(sale_price > purchase_price) / total_pairs × 100
```

Expressed as a percentage. A pair is profitable when the nominal sale price exceeds the nominal purchase price. No adjustment for inflation.

---

## 17. Holding Period Summary

```
median_holding_years = median((sale_date − purchase_date).days / 365.2425)
                       across all qualifying pairs for the group
```

---

## 18. Build Year and House Age

```
build_year = mode(completion_date.year) across all transactions for the project
house_age  = REFERENCE_YEAR − build_year
```

`REFERENCE_YEAR = current calendar year`.

---

## 19. Total Units

```
total_units = count of distinct address strings for the project
```

Address includes the unit number (e.g. `#15-07`). This counts the number of distinct units that have appeared in any transaction, not the developer-declared total number of units in the building.

---

## 20. GFA Harmonisation Flag

```
gfa_harmonized = True  when the earliest New Sale transaction date >= 2023-06-01
               = False otherwise (or when there are no New Sale records)
```

The Singapore government changed how Gross Floor Area is measured on 2023-06-01. This flag marks projects whose developer sales started under the new measurement rules. These projects will show a lower PSF than comparable older projects when comparing strata area. The frontend offers an optional PSF adjustment of `PSF × (1 − gfa_pct / 100)` (default 5%) to compensate.

---

## 21. Tenure Parsing

The pipeline parses URA tenure strings into structured fields.

| Tenure string | `tenure_type` | `lease_duration` | `lease_start_year` |
|---|---|---|---|
| `"Freehold"` | `"freehold"` | `null` | `null` |
| `"99 yrs lease commencing from 2010"` | `"leasehold"` | 99 | 2010 |
| `"999 yrs lease commencing from 1985"` | `"leasehold"` | 999 | 1985 |
| `"99 yrs"` (no start year) | `"leasehold"` | 99 | `null` |
| Other / blank | `"other"` | `null` | `null` |

When the start year is absent from the tenure string, `build_year` is used as a fallback. This can underestimate remaining lease if the lease commenced before the building was completed.

---

## 22. Lease Remaining

```
effective_start_year = tenure_info["start_year"]  (or build_year if absent)
lease_remaining      = max(0, lease_duration − (REFERENCE_YEAR − effective_start_year))
```

`lease_remaining` is `null` when `lease_duration` or `effective_start_year` is unavailable.

---

## 23. PSF Adjustments (Frontend Only — Chart Display Only)

These adjustments apply **only** to PSF bars in the modal chart. CAGR values are **never** adjusted.

### GFA Adjustment

Applied when the user enables GFA adjustment and `p.gfa_harmonized` is `true`:

```
adjusted_PSF = raw_PSF × (1 − gfa_pct / 100)
```

Default `gfa_pct` = 5%.

### Lease Normalisation — Proportionate

```
adjusted_PSF = raw_PSF × (99 / lease_remaining_at_sale_year)
```

Applied only to leasehold units where `lease_remaining < 99`.

### Lease Normalisation — Bala's Curve

```
adjusted_PSF = raw_PSF × (1 / bala_value(lease_remaining_at_sale_year))
```

`bala_value` is the fraction of a 99-year leasehold value at a given remaining term. Linear interpolation is used between table breakpoints.

Bala's table:

| Remaining lease (yrs) | Fraction of 99-yr value |
|---|---|
| 99 | 1.0000 |
| 90 | 0.9625 |
| 80 | 0.9000 |
| 70 | 0.8250 |
| 60 | 0.7375 |
| 50 | 0.6375 |
| 40 | 0.5250 |
| 30 | 0.4000 |
| 20 | 0.2625 |
| 10 | 0.1250 |
| 5  | 0.0563 |
| 0  | 0.0000 |

### Historical Lease Remaining for PSF Adjustment

```
lease_remaining_at_sale_year = lease_duration − (sale_year − lease_start_year)
```

The lease remaining is computed at the **sale year** of each chart data point. This gives the historically correct lease value, not the current remaining lease.

---

## 24. Spatial Neighbour Distance

Uses the Haversine formula to compute great-circle distance in metres between two lat/lon points:

```
dLat = (lat2 − lat1) in radians
dLon = (lon2 − lon1) in radians
a    = sin(dLat/2)² + cos(lat1) × cos(lat2) × sin(dLon/2)²
dist = 6,371,000 × 2 × atan2(√a, √(1−a))
```

The pipeline uses the same formula (vectorised with NumPy) to compute school proximity.

---

## 25. Size-Bucket Tolerance Merging (Explorer Table)

When `bucketTol > 0`, rows for the same project are clustered by sqft:

1. Sort rows by `purchase_area_sqft` ascending.
2. A new cluster starts when the gap between consecutive sqft values exceeds `bucketTol`.
3. Each cluster is represented by the **highest-n** row (most data).
4. The pair count shown is the **sum** of all rows in the cluster.
5. The sqft column shows the range (e.g. `800–820 sqft`) when multiple sizes are merged.

---

## 26. Summary Statistics (Overview Cards)

For each grouping variable (region, planning area, district, size bucket, purchase cohort):

```
n                    = count of repeat-sale pairs in group
median_cagr          = median(cagr_pct)
p25_cagr             = 25th percentile of cagr_pct
p75_cagr             = 75th percentile of cagr_pct
profit_rate          = mean(profitable) × 100
median_holding_years = median(holding_years)
```

---

## Data Sources

| File | Source | Notes |
|---|---|---|
| `data/all_transactions.csv` | URA REALIS export | All condo transactions |
| `data/postal_locations.csv` | OneMap API (geocoded) | Lat/lon + MRT distance per postal code |
| `data/moe_primary_schools_geocoded.csv` | MOE school list + geocoded | School names and coordinates |
