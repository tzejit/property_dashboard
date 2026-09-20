# Singapore Condo Analytics

A placeholder-ready analytics website and Python data pipeline for Singapore condominium
transaction data.

## Project structure

- `pipeline/build_dataset.py` — cleans transactions, constructs exact-unit repeat-sale pairs,
  produces statistical summaries and JSON used by the website.
- `site/index.html` — single-page dashboard.
- `site/app.js` — charts, filters and project explorer.
- `site/styles.css` — UI styling.
- `data/transactions.csv` — placeholder input schema.
- `data/postal_locations.csv` — placeholder input schema.
- `requirements.txt` — Python dependencies.

## Expected transaction columns

The pipeline accepts common variants, but the canonical schema is:

`transaction_date, project_name, address, postal_code, floor_area_sqft, price, psf, sale_type, region, planning_area, district`

For exact repeat-sales matching, `address` should identify the same physical unit.
If the source has a unit number column, put it into `address` or modify `UNIT_KEYS`.

## Run

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
python pipeline/build_dataset.py --input data/transactions.csv --locations data/postal_locations.csv --output site/data.json
```

Then serve the website:

```bash
python -m http.server 8000 --directory site
```

Open http://localhost:8000

## Statistical design

The pipeline uses exact-unit repeat sales as the primary appreciation measure:

`CAGR = (resale_price / purchase_price) ** (1 / holding_years) - 1`

Default cleaning rules:

- same exact unit/address
- resale occurs after purchase
- minimum holding period: 1 year
- maximum holding period: 30 years
- price and floor area must be valid
- repeat-sale pair is rejected when floor area changes by more than 5%

For descriptive project statistics, median CAGR and percentile ranges are reported.

For controlled analysis, the pipeline fits a robust OLS model with:

- log unit size
- purchase-year fixed effects
- region fixed effects
- sale-type indicators
- holding period
- size × purchase-year interaction
- heteroskedasticity-consistent (HC3) standard errors

This is an association model, not a causal model.

The website deliberately labels forward-looking scores as *signals*, not predictions.
