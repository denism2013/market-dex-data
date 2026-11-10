# ORBITAL — Global Market Universe

Open the standalone [3D market explorer](./index.html), or deploy GitHub Pages from the main branch and use `/orbital/`.

The application reads `/data/orbital-market-universe.json` from this repository's GitHub Pages origin on page load. If Pages is unavailable, it displays a labeled, non-live structure preview and lets users import a local JSON snapshot through Mission Control.

## Navigation
Click / tap a glowing galaxy, index cluster, sector or issuer to descend. Drag to orbit. Pinch / scroll to zoom. Search by ticker/industry. Backspace ascends one level; Home returns to the global universe; / opens search.

## Update process
The dedicated [Python updater](../scripts/orbital_update_data.py) uses publicly accessible iShares ETF holdings (IWV, IWB, IWM, IEFA, IEMG, EWC) and optional yfinance industry/adjusted-close enrichment. The independent [GitHub workflow](../.github/workflows/orbital-daily.yml) updates snapshots at 22:20 UTC on weekdays and supports manual dispatch.

## Important distinctions
- **ETF holdings are proxies**, not licensed official benchmark constituent lists. Russell 1000/2000 are approximated via ETF overlaps. The "largest 200" partition is estimated by holding size and is NOT the official Russell Top 200.
- Holding weights are **within each source ETF only**, not globally comparable weights.
- Sector/industry labels are from holdings feeds and cached Yahoo metadata; they are not an exhaustive licensed GICS subindustry database. Metadata coverage grows across runs.
- ETF adjusted-close returns are benchmark proxies, not actual official index returns. Some returns may be missing or delayed.
- yfinance / Yahoo Finance data are intended for personal research and subject to applicable redistribution terms; obtain appropriate licenses for commercial distribution.

The app includes a Canvas fallback when WebGL is unsupported. GitHub Actions successfully completed its initial data build on 2026-11-10. GitHub Pages availability has not been independently verified.