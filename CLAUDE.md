# CLAUDE.md

This file gives guidance to Claude Code (claude.ai/code) when working in this repository.

## Instructions

Follow these rules in all responses and documentation in this project:

1. **Use ASD-STE100 Simplified Technical English.** Write short sentences. Use active voice. Use common words. Use one instruction per sentence. Use the same word for the same thing throughout.
2. **Update this file** when you change the architecture or data contracts.
3. **Do not read large data files fully.** Sample them instead. This saves tokens. See the "Sampling" section below.

## What this project does

This project has two parts:

- A Python data pipeline that reads Singapore condo transaction data and HDB resale data. It builds repeat-sale pairs. It writes a JSON file for the website.
- A static single-page website that reads the JSON file. It shows charts and a searchable project table. A "Type" filter switches between Private Condo and HDB Resale rows.

## Commands

**Run the pipeline:**
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python pipeline/build_dataset.py --input data/all_transactions.csv --locations data/postal_locations.csv --output site/data.json --schools data/moe_primary_schools_geocoded.csv --hdb data/hdb_resale.csv --hdb-locations data/hdb_locations.csv
```

**Serve the site:**
```bash
python -m http.server 8000 --directory site
# Open http://localhost:8000
```

The default `--input` value is `data/combined_transactions.csv`. The actual file is `data/all_transactions.csv`. Always pass `--input` when you run the pipeline. `--schools` is optional. It writes `site/schools.json` when provided. `--hdb` is optional. Pass `data/hdb_resale.csv` to include HDB resale transactions. `--hdb-locations` is optional. Pass `data/hdb_locations.csv` (produced by `pipeline/geocode_hdb.py`) to add lat/lon/MRT data to HDB rows.

**Refresh HDB data:**
```bash
# Re-download from data.gov.sg (dataset d_8b84c4ee58e3cfc0ece0d773c8ca6abc)
URL=$(curl -s "https://api-open.data.gov.sg/v1/public/api/datasets/d_8b84c4ee58e3cfc0ece0d773c8ca6abc/initiate-download" | python -c "import sys,json; print(json.load(sys.stdin)['data']['url'])")
curl -o data/hdb_resale.csv "$URL"
```

## Sampling large files

Do not read the full CSV or JSON files. Sample them.

For CSV files:
```python
pd.read_csv("data/all_transactions.csv", nrows=5)
```

For JSON files, use the `Read` tool with a `limit` parameter. Or run a short Python script that loads only the keys you need.

## Architecture

### Pipeline stages

The pipeline file is `pipeline/build_dataset.py`. It runs these steps in order:

1. **`load_transactions()`** — Reads the raw URA CSV. It checks that `TRANSACTION_COLUMNS` are present. It converts all fields to the internal schema. It creates `_unit_key = project_name | address`. The address includes the unit number (for example, `#15-07`). This key is used to match repeat sales.

2. **`compute_project_stats(transactions)`** — Computes one row per project. Returns a DataFrame indexed by `project_name`. It computes these fields: `total_units` (count of distinct addresses), `build_year` (mode of `completion_date` year), `house_age`, `gfa_harmonized` (true if earliest new sale is on or after 2023-06-01), `tenure_type`, `lease_duration`, `lease_start_year`, `lease_remaining`.

3. **`build_repeat_sales()`** — Groups transactions by `_unit_key`. For each group, it iterates consecutive pairs. It applies these filters: `MIN_HOLDING_YEARS`, `MAX_HOLDING_YEARS`, `MAX_AREA_CHANGE`. It computes CAGR from raw prices. CAGR is never adjusted for lease or GFA.

4. **`load_locations()`** — Reads postal code data. It deduplicates on postal code. It returns lat/lon and MRT distance per postal code.

5. Merge step — Merges location data onto `repeat_sales` on `postal_code`. This adds `latitude`, `longitude`, `nearest_mrt`, and `nearest_mrt_distance_m` to each row.

6. **`build_project_time_series(transactions, repeat_sales, project_filter)`** — Builds monthly series for each project that has repeat sales. PSF series: monthly median PSF from all transactions. CAGR series: monthly median CAGR from repeat-sale pairs, by sale date. Writes compact arrays for all purchase types (`c`, `cn`, `cs`, `cr`). Per-type keys are omitted when empty.

7. **`make_project_records(repeat_sales, project_stats)`** — Groups by `(project_name, purchase_area_sqft)`. Computes CAGR stats for all pairs and for each purchase type (New Sale, Sub Sale, Resale). Joins location fields from merged `repeat_sales`. Joins per-project fields from `project_stats`.

8. Write step — Writes `site/data.json` (main data, no time series). Writes `site/ts.json` (compact time series).

9. **`load_schools(path)` + `compute_school_proximity(repeat_sales, schools)`** (optional) — Loads MOE primary schools from CSV. Computes haversine distance from each project to all schools within 2,000 m. Uses NumPy vectorised haversine. Writes `site/schools.json`.

The OLS controlled model (`fit_controlled_model`) is coded but not included in the output. To enable it, uncomment the `"model"` key in `build_dataset()`.

### Frontend files

The frontend is a static single-page application. No build step is needed. Edit the files and reload the browser.

- **`site/index.html`** — Layout. Loads Chart.js 4.4.4 from CDN. Loads `app.js`. PSF adjustment controls: `#adj-gfa`, `#adj-gfa-pct`, `#adj-lease` (in the explorer card, applied to the modal PSF chart). Purchase type radios: `input[name="purchaseType"]` (values: all/new/sub/res). School filter: `#school-search`, `#school-dist-min`, `#school-dist-max`, `#school-list` (checkbox list). Modal sections: PSF+CAGR chart, spatial neighbours, MRT neighbours, nearby schools.
- **`site/app.js`** — All interaction logic. Key helpers: `activeCagrField()` maps purchase type to `median_cagr`/`cagr_new`/`cagr_sub`/`cagr_res` (used for the explorer table). `activeTsKey()` maps to the pipeline's matched-pair CAGR keys `c`/`cn`/`cs`/`cr` but is currently unused — the modal chart does not read these keys; see "PSF adjustments" below and `data/calculations.md` section 14 for what the chart actually plots. Lazy loaders: `ensureTimeSeries()`, `ensureSchools()`. School filter: `populateSchoolList()`, school checkboxes in `readFilters`/`applyFilters`. `buildProjectIndex()` deduplicates to one row per project (highest-n).
- **`site/styles.css`** — All styling.

## Data contracts

### Input: transaction CSV

The pipeline checks that all columns in `TRANSACTION_COLUMNS` are present. If the source schema changes, update `TRANSACTION_COLUMNS` and the field mapping in `load_transactions()`. The same applies to `LOCATION_COLUMNS` and `load_locations()`.

### Output: `site/data.json` (schema v3)

Top-level keys: `meta`, `summary`, `projects`, `regions`, `planning_areas`, `districts`, `purchase_cohorts`, `size_buckets`.

The `projects` array has one entry per `(project_name, purchase_area_sqft)` pair. It includes both private condo and HDB resale records when `--hdb` is used. The pipeline rounds `purchase_area_sqft` to the nearest whole sqft before grouping. This merges near-duplicate unit types (for example, 900.0 sqft and 900.25 sqft become one "900 sqft" group). Fields:

| Field | Type | Notes |
|---|---|---|
| `asset_class` | string | `"Private"` for URA condo data; `"HDB"` for HDB resale |
| `latitude`, `longitude` | float or null | From postal_locations join |
| `nearest_mrt` | string | MRT station name |
| `nearest_mrt_distance_m` | float or null | Walking distance in metres |
| `build_year` | int or null | From completion_date |
| `house_age` | int or null | Reference year minus build year |
| `total_units` | int or null | Distinct address count |
| `gfa_harmonized` | bool | True if earliest new sale >= 2023-06-01 |
| `tenure_type` | string or null | `"freehold"`, `"leasehold"`, `"other"`, or null |
| `lease_duration` | int or null | For example, 99 or 999 |
| `lease_start_year` | int or null | Year lease commenced |
| `lease_remaining` | int or null | Years left as of reference year |
| `cagr_new` | float or null | Median CAGR for New Sale purchases (null if <3 pairs) |
| `n_new` | int | Pair count for New Sale purchases |
| `cagr_sub` | float or null | Median CAGR for Sub Sale purchases |
| `n_sub` | int | Pair count for Sub Sale purchases |
| `cagr_res` | float or null | Median CAGR for Resale purchases |
| `n_res` | int | Pair count for Resale purchases |

### Output: `site/ts.json` (compact time series)

This file is separate from `data.json`. The site loads it on the first modal open. It includes only projects that have repeat sales.

Format:
```json
{
  "PROJECT NAME": {
    "p":  [["YYYY-MM", psf_int, n, median_area_sqft], ...],
    "c":  [["YYYY-MM", cagr_2dp, n], ...],
    "cn": [["YYYY-MM", cagr_2dp, n], ...],
    "cs": [["YYYY-MM", cagr_2dp, n], ...],
    "cr": [["YYYY-MM", cagr_2dp, n], ...]
  }
}
```

`p` = PSF monthly (all transactions). `c` = CAGR all purchase types. `cn` = New Sale purchases only. `cs` = Sub Sale. `cr` = Resale. `pb` = purchase-side PSF grouped by sale month (base used in tooltip). `cp` = CAGR grouped by purchase month (cohort view). `pp` = purchase-side PSF grouped by purchase month (reference trend line). Per-type keys are omitted when empty. CAGR is always from raw prices — no lease or GFA adjustment.

In `app.js`, `ensureTimeSeries()` loads the file once and returns a shared Promise. `activeTsKey()` returns the right key (`c`/`cn`/`cs`/`cr`) for the active purchase type radio. `getChartAxis()` returns `"sale"` or `"purchase"` for the X-axis toggle. `getModalPurchaseType()` returns the chart-only purchase type (independent of the explorer radio).

See `data/calculations.md` for all formulas.

### Output: `site/schools.json` (school proximity, optional)

Written only when `--schools` is supplied. Lazy-loaded in the frontend.

Format:
```json
{
  "PROJECT NAME": [{"n": "School Name", "d": 456}, ...]
}
```

Includes only schools within 2,000 m. Sorted by distance. `d` is in metres. `ensureSchools()` in `app.js` loads this file on first use (focus on school search box or first modal open).

### PSF adjustments (frontend only)

These adjustments apply only to the PSF chart in the modal. CAGR is always computed from raw prices. CAGR is never adjusted.

**GFA adjustment** — If `#adj-gfa` is checked and `p.gfa_harmonized` is true, multiply PSF by `(1 - gfaPct/100)`. This reduces the PSF of post-harmonization units for fair comparison.

**Lease normalisation** — Apply this per chart month. Use `leaseRemainingAt(p, year)` to get the historical remaining lease (not the current remaining lease). Two modes:
- `"proportionate"`: multiply by `99 / remaining`
- `"bala"`: multiply by `1 / balaValue(remaining)`

Do not apply adjustment when tenure is freehold or when remaining lease is 99 or more years.

**Bala's curve** — Defined by `BALA_TABLE` in `app.js`. Use linear interpolation between table breakpoints.

**Spatial neighbours** — Use `haversineM()` in `app.js`. Deduplicate to one representative per `project_name` (the entry with the highest `n`) using `buildProjectIndex()`.

**MRT neighbours** — Match projects that have the same `nearest_mrt` value and `nearest_mrt_distance_m` at or below the radius.
