# VANTA options auto-feed and MARKET-DEX market-context fix

## Verified behavior and original bug

The original MARKET-DEX `importDataFiles` unconditionally set `marketContextRows=[]`
when importing a ticker's independent OHLCV ZIP without `context_market.csv`.
That erased all index quotes and sector-rotation context whenever the user changed
companies. It also reset `optionsSnapshot=[]` whenever a selected ticker ZIP
had no options CSV. The updated MARKET-DEX standalone HTML now preserves the
last verified sector/index data and loads it independently from the existing
`data/latest.zip`, so company switching cannot wipe the top index strip.

The updated VANTA standalone HTML auto-reads a published options snapshot and
reflects its actual underlying provider snapshot timestamp, not the GitHub
upload time. Both terminals keep manual CSV/JSON import workflows available.

### Existing files that remain unchanged

- `scripts/update_market_dex.py`
- `data/latest.zip`, `data/universe.json`, `data/tickers/*.zip` contracts and filenames
- The primary technical, options, CFTC, and UW GEX calculation modules

The fixes to the standalone HTML files are delivered as downloadable files
separately; this GitHub repository remains a data-only repository.

## How automatic options loading works

The reader checks when you **open the HTML**, change ticker, return to the tab,
or manually press its options refresh button. It requests:

- `data/options/manifest.json` — actual selected ticker coverage and update time
- `data/options/tickers/NVDA.json` — actual chain snapshot for the selected symbol
- `data/options/status.json` — explicit reason when the feed is disabled/unavailable

The nightly published options snapshot uses a VANTA-compatible JSON structure:

```json
{
  "metadata": {
    "ticker": "NVDA",
    "run_utc": "2026-11-09T21:00:00Z",
    "spot_daily_close": 185.5,
    "provider": "AUTHORIZED PROVIDER"
  },
  "contracts": [],
  "summary": []
}
```

That object is **only an illustrative schema**, not market data. Real publications
must have source-timestamped, validated nonempty contracts and actual expiries.
Unknown trade aggressor side, option dealer inventory, and IV surface points are
never filled with invented values. CFTC futures positions are a separate source.

The Options Monitor, IV smile/surface, relative option-volume metrics, strike
concentrations, and MARKET-DEX options OI panels can use the same authorized
snapshot. The market-index strip and sector internals use existing OHLCV
instead, not options data.

## What must be configured for a production options feed

The GitHub worker `scripts/update_vanta_options.py` is scheduled with the
existing **5 PM America/New_York weekday** workflow. GitHub scheduling and
processing are not instantaneous: the workflow starts around 5 PM, but Pages
publishes after the market-data job completes. Do not call the result
real-time options or promise an exact 5:00 PM publication time.

The worker intentionally does **not** scrape Unusual Whales/Yahoo or turn the
ChatGPT UW connector into an unauthenticated public endpoint. For publicly
accessible GitHub Pages, first obtain rights for public/third-party
redistribution of the intended options data and ensure the licensed provider
will supply source-timestamped JSON in the format above.

In GitHub **Settings → Secrets and variables → Actions**:

1. Set **Repository variable** `VANTA_OPTIONS_PUBLICATION_APPROVED=true` only when
   the provider license explicitly authorizes public redistribution including
   derivatives/aggregations. Otherwise leave it disabled. The normal UW
   self-serve API explicitly does **not** grant this right.
2. Set **Repository variable** `VANTA_OPTIONS_SOURCE_URL_TEMPLATE` to an
   HTTPS endpoint you control or are authorized to publish from, for example
   `https://licensed-feed.example.com/vanta/{ticker}.json`.
3. If the source requires private backend authentication, set **Repository
   secret** `VANTA_OPTIONS_SOURCE_TOKEN`. The updater sends it as an HTTP Bearer
   token and never publishes credentials. Do not put tokens in repository
   variables, query strings, HTML, or GitHub Pages.
4. Optional `workflow_dispatch` input `options_detail_tickers` requests up to
   the maximum published symbol count, and can prioritize specific tickers.

The default run attempts **60 liquid symbols**, starting with major ETFs and
large IWV holdings. This is not a claim to cover the entire Russell 3000 with
listed options chains; many members have no option contracts and market-data
licenses/rate limits constrain broad bulk extraction. To cover all optionable
members, arrange an appropriately provisioned bulk data license and adjust
configuration/capacity. `VANTA_OPTIONS_MAX_SYMBOLS` is capped at 300 by this
safe implementation. The UI reports gaps rather than inventing chains.

When a source request fails, the publisher reuses a matching prior verified
snapshot for at most three calendar days **only when it still has unexpired
contracts**, and labels it as `cached` with the original `run_utc` time. When
publication is disabled, it removes the public options snapshots and leaves a
non-sensitive status.json explaining why.

## Required initial run

The new automation is currently staged in a **pull request**. Until merged into
`main`, the live GitHub Pages workflow cannot produce the new options/CFTC
files. After review/merge, run **Actions → MARKET-DEX Daily Market Snapshot →
Run workflow** (or use the scheduled weekday job). Validate:

- `https://denism2013.github.io/market-dex-data/data/latest.json`
- `https://denism2013.github.io/market-dex-data/data/positioning/index.json`
- `https://denism2013.github.io/market-dex-data/data/options/status.json`
- `https://denism2013.github.io/market-dex-data/data/options/manifest.json`
  (exists only if a licensed feed is configured)

Do **not** merge the earlier superseded UW-only PR. Maintain VANTA and
MARKET-DEX as two separate HTML products drawing from the shared data feeds.

## Data rights

- Official CFTC COT futures reports: no API token or options chain license
  required for the public statistical feed.
- Unusual Whales: standard retail/API plans prohibit public redistribution,
  including derived statistics. Obtain written rights or use a private
  authentication-controlled integration. See
  https://unusualwhales.com/terms and
  https://unusualwhales.com/api-terms-of-service.
- Yahoo Finance: the existing OHLCV history pipeline is not changed. Public
  redistribution of third-party Yahoo datasets has its own restrictions;
  this work does not add a new Yahoo options redistribution channel.
