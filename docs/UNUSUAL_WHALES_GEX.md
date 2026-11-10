# MARKET-DEX × Unusual Whales GEX

This integration is additive. It does **not** replace or alter the existing Yahoo Finance / Russell 3000 price-history feed.

## Existing Market Dex feed remains the source of daily prices

The current updater continues to:
- Discover current iShares IWV equity holdings as a Russell 3000 proxy.
- Download adjusted daily OHLCV history.
- Publish `data/latest.zip`, `data/universe.json`, and `data/tickers/*.zip`.
- Deploy those files to GitHub Pages on the existing weekday 5:00 PM America/New_York schedule.

The Unusual Whales integration writes only to `data/gex/` and `.marketdex_gex_cache/`.

## What is published

After a successful UW refresh, GitHub Pages exposes:

| Path | Contents |
|---|---|
| `data/gex/universe.json` | Broad stock-screener snapshot for optionable IWV holdings, including aggregate OI/volume GEX, GEX ratio, IV, open interest and volume |
| `data/gex/tickers/SYMBOL.json` | Detailed OI-based strike × expiry exposure map and both OI/volume GEX levels for a selected ticker |
| `data/gex/manifest.json` | Coverage, refresh and detailed-cache metadata |
| `data/gex/status.json` | Last refresh status and provider date |

The stock screener is paginated at 50 rows per request until a short page is returned. The result is the **optionable/covered subset** of IWV constituents, not a claim that all 3,000 constituents have listed options or GEX data. The existing Market Dex OHLCV universe remains the broader full IWV proxy universe.

## Detailed maps and API usage

To avoid thousands of per-symbol requests on every scheduled run:
- The daily job refreshes the broad GEX/IV/options summary for the IWV universe.
- It refreshes detailed strike × expiry maps for the 12 highest-open-interest names by default.
- A manual workflow dispatch can request any additional ticker(s) using the `gex_detail_tickers` input, e.g. `AAPL,MSFT,NVDA`.
- The number of detailed tickers per run is capped at 40 by default. The cap is configurable in the workflow/script.

The detailed strike × expiry surface uses the UW endpoint's **open-interest GEX** fields. The separate `levels.oi` and `levels.vol` objects provide OI-based and directionalized-volume call wall, put wall, gamma flip and gamma magnet. The pipeline deliberately does not relabel an OI surface as a volume surface.

Unusual Whales describes GEX as an assumed market-maker exposure estimate. It is not observed dealer inventory. Data availability, latency and request limits depend on the user's UW API plan. The public docs used for this integration do not provide a universal request quota, so the job uses conservative pauses and retries 429/503 responses.

## One-time GitHub setup

1. Open the repository: **Settings → Secrets and variables → Actions**.
2. Choose **New repository secret**.
3. Name it exactly `UW_TOKEN`.
4. Paste your Unusual Whales Public API token as the value.
5. Save. The token is only read by GitHub Actions and is never placed in the HTML or published JSON.

If `UW_TOKEN` is absent, the GEX step publishes its last restored cache if available; otherwise it writes a `not_configured` status and exits successfully. The original OHLCV job continues.

## HTML behavior

The updated standalone Market Dex HTML reads the GEX summary from:

`https://denism2013.github.io/market-dex-data/data/gex/universe.json`

It automatically looks up the selected ticker in that broad snapshot and attempts to load:

`https://denism2013.github.io/market-dex-data/data/gex/tickers/SYMBOL.json`

Therefore, a ticker's daily OHLCV can be available even when its detailed GEX map is not cached. The Gamma Exposure page makes this distinction explicit and links to the Actions workflow for requesting a detailed map.

## Manual detailed-map refresh

1. Open **Actions → MARKET-DEX Daily Market Snapshot → Run workflow**.
2. Enter the desired comma-separated symbols in `gex_detail_tickers`.
3. Run the workflow. It performs the normal market-history update and then refreshes the requested GEX detail maps.
4. After GitHub Pages deployment completes, reopen or refresh the Market Dex HTML. It will fetch the new GEX files on load.

The existing data feed paths and ZIP schemas are preserved. No existing data file is overwritten by the GEX script.
