# VANTA × MARKET-DEX — shared, source-verified positioning infrastructure

## Architecture and scope

- **VANTA**: primary options intelligence dashboard. Its standalone HTML now loads the published current IWV/Russell 3000-proxy constituent catalog, uses each **existing** MARKET-DEX ticker ZIP for daily adjusted OHLCV, and checks the optional UW summary and detailed gamma map. It adds official CFTC futures positioning and a dealer gamma-to-hedge sensitivity scenario.
- **MARKET-DEX**: independent technical research dashboard. Its existing indicators, Yahoo OHLCV updater, GitHub data paths and interface are preserved; the updated standalone HTML adds the same **CFTC futures positioning** view.
- **GitHub repository**: `denism2013/market-dex-data`. The daily 5:00 PM America/New_York GitHub Actions job continues to publish the original `data/latest.zip`, `data/universe.json`, `data/tickers/*.zip` unchanged. The additional CFTC updater only writes under `data/positioning/` and uses `.marketdex_cot_cache/`.

### CFTC datasets, genuine provenance

Official CFTC Public Reporting Environment (Socrata API), **futures-only**:

- Traders in Financial Futures (TFF) `gpe5-46if` — financial futures such as equity indexes, Treasuries and currency futures. Positions classified as leveraged funds, asset managers, dealers and other reportables.
- Disaggregated `72hh-3qpy` — agricultural and natural-resources commodity futures. Positions classified as managed money, producers/merchants, swap dealers and other reportables.

The report is historically published with **position date** (usually Tuesday) distinct from its Friday publication date; special holiday shifts and announced future changes may apply. The updater reads dates from the API, not the computer clock or an invented calendar.

`data/positioning/index.json` publishes metadata, source identifiers, actual position dates, current primary-category net position and the descriptive percentile rank across available historical reports. `data/positioning/markets/<report>-<contract_code>.json` publishes contract history, long/short, net and percentage of open interest across trader categories. Missing fields stay absent; the updater never converts missing positions to zero. Incomplete or failed source queries do not create made-up snapshots. An earlier verified cache is used when the source is unavailable and identified as cached.

The first successful scheduled Actions run is needed to populate `data/positioning/` publicly. No API key or subscription is required for official CFTC feeds. CFTC COT is **not** single-name stock positioning, 13F, short interest, true options dealer inventory, market-wide order flow or real-time execution data. It should not be summed with dealer GEX.

### Source limitations and market math

VANTA's `Gamma exposure` lab adds:

- Provider-reported aggregate GEX per 1% price move, with separately selected OI or directionalized-volume assumptions.
- Hypothetical delta-hedge **notional** for a selected underlying move, approximately `−GEX × price change in percentage points`.
- Equivalent shares at the UW reference price (not stock orders). Assumes constant gamma and spot, not modeled dynamic changes in volatility, charm, vanna or transaction direction.
- Strike × expiry heatmap from the published detailed UW snapshot, **OI only** when that's all the backend supplies. The HTML disables unsupported volume/charm/vanna surface selectors and will not invent those numbers.
- When you change ticker, the old ticker's imported gamma is cleared instead of being mislabeled as the new company.
- If GEX summary is unavailable, VANTA still loads daily price data and COT independently. No placeholder UW numbers are shown.

The existing MARKET-DEX ticker ZIPs use Yahoo-adjusted price bars, not live real-time prices. Yahoo's terms prohibit redistribution. The new integration does **not** duplicate or expand public Yahoo publication. Before using publicly distributed market data in a commercial product, arrange licensed data rights; this change intentionally avoids changing the existing MARKET-DEX publishing workflow.

## How to enable CFTC in both files

1. Review the superseding GitHub pull request; merge only when satisfied. Original `scripts/update_market_dex.py` is unchanged.
2. Run Actions → **MARKET-DEX Daily Market Snapshot** → **Run workflow** or allow the scheduled weekday run. The *CFTC refresh* is a separate optional step; the price pipeline continues even if CFTC is temporarily offline.
3. Verify `https://denism2013.github.io/market-dex-data/data/positioning/index.json`. Check `metadata.latest_dates` and source dataset IDs, not just `generated_at_utc`.
4. Open either updated standalone HTML; visit **CFTC Positioning** and select a market plus trader category.

## How to enable Unusual Whales for VANTA

The connected ChatGPT Unusual Whales plugin **cannot** automatically authenticate your standalone HTML, Colab, or GitHub Actions. Options:

**Personal/local workflow (no public redistribution):** Obtain an eligible Unusual Whales API subscription/key (commercial or professional users must verify enterprise eligibility), open VANTA → Data Studio → Unusual Whales GEX, copy the embedded Colab fetcher, set `UW_TOKEN` in **Google Colab Secrets**, run, and import its generated JSON into VANTA. This keeps the API token out of your HTML and outside GitHub Pages. You can use this workflow without merging any UW public-publishing changes.

**Automated public GitHub feed:** Requires **written Unusual Whales enterprise/redistribution authorization**, because the provider's standard API terms prohibit public redistribution **including derived data**. Only after confirmation, add `UW_TOKEN` under GitHub Settings → Secrets and variables → Actions → Secrets, and set Actions *variable* `UW_PUBLIC_REDISTRIBUTION_APPROVED=true`. Then the existing optional UW updater can publish the authorized `data/gex/*` data. The workflow intentionally skips and removes published GEX files when the variable is not true. You are responsible for confirming the written rights before setting the variable. Merely having a standard retail/API plan or being logged into the ChatGPT app is **not** permission for public GitHub Pages distribution.

For an internal/private application, instead deploy the UW worker to a private backend with authenticated browser access and no third-party/public redistribution. A **public static GitHub Pages repository cannot hold secret UW token or access-control-protected payloads**. A private GitHub repository alone does not necessarily protect a public Pages site.

Sources: https://publicreporting.cftc.gov/d/gpe5-46if ; https://publicreporting.cftc.gov/d/72hh-3qpy ; https://unusualwhales.com/api-terms-of-service ; https://unusualwhales.com/terms ; https://unusualwhales.com/public-api ; https://help.yahoo.com/kb/SLN2310.html

## Tests and deployment safeguards

- Five offline Python data-quality tests and browser smoke tests for both HTMLs (mocked source fixtures, not fabricated production datasets).
- Original MARKET-DEX source updater and deployed `main` branch not modified in this development change; source staged in a feature branch / pull request.
- `on.push.branches=main` prevents development-branch workflow from overwriting the published GitHub Pages site; Actions runs on merge or manual trigger.
- GEX publication is permission-gated; CFTC and original OHLCV are not.
- The first live CFTC API fetch and authenticated Unusual Whales fetch have **not been executed** here. Review the live CFTC `index.json` and provider dates after running Actions; any missing data is shown as missing, not guessed.
