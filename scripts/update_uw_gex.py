#!/usr/bin/env python3
"""Optional Unusual Whales GEX snapshots for MARKET-DEX.

This is deliberately additive: it writes only data/gex/* and a separate cache.
It never edits data/latest.zip, data/universe.json, data/tickers/*, or the
existing yfinance updater outputs.

Scheduled mode:
  * Pulls the IWV constituent stock screener in 50-row pages. This gives a
    broad GEX/IV/options summary for optionable IWV holdings.
  * Builds detailed strike x expiry maps for the highest-open-interest names
    (conservative default: 12) plus any explicitly requested tickers.

Manual mode:
  * Set MARKETDEX_UW_GEX_DETAIL_TICKERS="AAPL,MSFT" in workflow_dispatch to
    request full maps for additional symbols.

Requires UW_TOKEN as a GitHub Actions secret. If absent, cached snapshots are
published when available; otherwise the step writes a status file and exits
successfully so the existing MARKET-DEX price-data workflow is unaffected.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "gex"
CACHE_DIR = Path(os.getenv("MARKETDEX_UW_GEX_CACHE_DIR", str(ROOT / ".marketdex_gex_cache")))
CACHE_TICKERS = CACHE_DIR / "tickers"
DATA_TICKERS = DATA_DIR / "tickers"
BASE_URL = "https://api.unusualwhales.com/api"
TOKEN = os.getenv("UW_TOKEN", "").strip()
PAGE_SIZE = 50  # Stock screener API caps this endpoint at 50 rows.
MAX_UNIVERSE_PAGES = max(1, min(200, int(os.getenv("MARKETDEX_UW_GEX_MAX_PAGES", "100"))))
DETAIL_LIMIT = max(0, min(50, int(os.getenv("MARKETDEX_UW_GEX_DETAIL_LIMIT", "12"))))
DETAIL_MAX_TICKERS = max(1, min(50, int(os.getenv("MARKETDEX_UW_GEX_DETAIL_MAX_TICKERS", "40"))))
DETAIL_MAX_DTE = max(1, min(180, int(os.getenv("MARKETDEX_UW_GEX_DETAIL_MAX_DTE", "60"))))
DETAIL_STRIKE_RANGE = max(0.05, min(0.75, float(os.getenv("MARKETDEX_UW_GEX_STRIKE_RANGE", "0.20"))))
DETAIL_MAX_PAGES = max(1, min(50, int(os.getenv("MARKETDEX_UW_GEX_DETAIL_MAX_PAGES", "12"))))
PAUSE_SECONDS = max(0.0, min(3.0, float(os.getenv("MARKETDEX_UW_GEX_PAUSE", "0.20"))))
MAX_RETRIES = max(1, min(8, int(os.getenv("MARKETDEX_UW_GEX_RETRIES", "4"))))
EXPLICIT_TICKERS = os.getenv("MARKETDEX_UW_GEX_DETAIL_TICKERS", "")
VALID = re.compile(r"^[A-Z0-9.^=_-]{1,24}$")

# Keep the universe payload compact and stable. Missing fields remain null.
SUMMARY_FIELDS = (
    "ticker", "date", "full_name", "sector", "industry_type", "issue_type",
    "close", "prev_close", "change", "marketcap", "total_open_interest",
    "call_open_interest", "put_open_interest", "call_volume", "put_volume",
    "call_premium", "put_premium", "net_premium", "iv30d", "iv_rank",
    "iv_percentile_1y", "realized_volatility", "implied_move_perc_7",
    "gex_gamma_per_one_percent_move_oi",
    "gex_gamma_per_one_percent_move_vol",
    "gex_gamma_per_one_percent_move_dir",
    "gex_charm_per_one_percent_move_oi",
    "gex_vanna_per_one_percent_move_oi",
    "gex_ratio", "gex_net_change", "gex_perc_change",
    "gex_daily_net_gex", "gex_daily_call_gex", "gex_daily_put_gex",
    "gex_daily_net_delta", "has_options",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_symbol(raw: Any) -> str:
    symbol = re.sub(r"\s+", "-", str(raw or "").strip().upper()).replace("/", "-")
    return symbol if VALID.fullmatch(symbol) else ""


def file_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "_", symbol)


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def unwrap_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("result", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def unwrap_object(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get("data")
        return value if isinstance(value, dict) else payload
    return {}


def request_json(session: requests.Session, path: str,
                 params: list[tuple[str, Any]] | dict[str, Any] | None = None) -> Any:
    url = BASE_URL + path
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, params=params, timeout=(10, 40))
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES - 1:
                    response.raise_for_status()
                retry_after = as_float(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else min(20, 1.5 ** (attempt + 1))
                print(f"UW {response.status_code}; retrying in {delay:.1f}s: {path}", flush=True)
                time.sleep(max(1.0, delay))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES - 1:
                break
            delay = min(15, 1.5 ** (attempt + 1))
            print(f"UW request retry {attempt + 1}/{MAX_RETRIES}: {str(exc)[:180]}", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Unusual Whales request failed: {path}: {last_error}")


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": "Bearer " + TOKEN.removeprefix("Bearer ").strip(),
        "Accept": "application/json",
        "User-Agent": "MARKET-DEX/Unusual-Whales-GEX/1.0",
    })
    return session


def load_marketdex_universe_count() -> int | None:
    path = ROOT / "data" / "universe.json"
    try:
        obj = json.loads(path.read_text())
        value = int(obj.get("requested_equities") or obj.get("available_equities") or 0)
        return value or None
    except Exception:
        return None


def fetch_iwv_gex_universe(session: requests.Session) -> tuple[list[dict[str, Any]], int]:
    """Fetch all pages of the UW screener for IWV holdings (optionable subset)."""
    by_symbol: dict[str, dict[str, Any]] = {}
    pages = 0
    for page in range(MAX_UNIVERSE_PAGES):
        params = [
            ("etfs[]", "IWV"),
            ("limit", PAGE_SIZE),
            ("offset", page),
            ("order", "marketcap"),
            ("direction", "desc"),
        ]
        payload = request_json(session, "/screener/stocks", params)
        batch = unwrap_rows(payload)
        pages += 1
        for raw in batch:
            symbol = safe_symbol(raw.get("ticker") or raw.get("symbol"))
            if not symbol:
                continue
            row = {field: raw.get(field) for field in SUMMARY_FIELDS}
            row["ticker"] = symbol
            by_symbol[symbol] = row
        print(f"IWV GEX screener page {page + 1}: {len(batch)} rows · {len(by_symbol)} unique tickers", flush=True)
        if len(batch) < PAGE_SIZE:
            return list(by_symbol.values()), pages
        if PAUSE_SECONDS:
            time.sleep(PAUSE_SECONDS)
    raise RuntimeError(
        f"IWV screener exceeded {MAX_UNIVERSE_PAGES} pages; refusing to publish a truncated universe."
    )


def detail_ticker_list(rows: list[dict[str, Any]]) -> list[str]:
    requested = []
    for raw in re.split(r"[,;\s]+", EXPLICIT_TICKERS.strip()):
        symbol = safe_symbol(raw)
        if symbol and symbol not in requested:
            requested.append(symbol)

    # The daily detailed cache is deliberately bounded. The summary feed above
    # covers the broad optionable universe; manual workflow input adds any name.
    ranked = sorted(
        rows,
        key=lambda row: (
            as_float(row.get("total_open_interest")) or 0,
            (as_float(row.get("call_volume")) or 0) + (as_float(row.get("put_volume")) or 0),
            as_float(row.get("marketcap")) or 0,
        ),
        reverse=True,
    )
    for row in ranked[:DETAIL_LIMIT]:
        symbol = safe_symbol(row.get("ticker"))
        if symbol and symbol not in requested:
            requested.append(symbol)
    if len(requested) > DETAIL_MAX_TICKERS:
        print(f"Detailed GEX request capped at {DETAIL_MAX_TICKERS} tickers (requested {len(requested)}).", flush=True)
    return requested[:DETAIL_MAX_TICKERS]


def fetch_detail(session: requests.Session, symbol: str,
                 summary: dict[str, Any] | None) -> dict[str, Any]:
    spot = as_float((summary or {}).get("close"))
    params: dict[str, Any] = {
        "page": 0,
        "limit": 500,
        "min_dte": 0,
        "max_dte": DETAIL_MAX_DTE,
    }
    if spot and spot > 0:
        params["min_strike"] = round(spot * (1 - DETAIL_STRIKE_RANGE), 4)
        params["max_strike"] = round(spot * (1 + DETAIL_STRIKE_RANGE), 4)

    path = f"/stock/{symbol}/spot-exposures/expiry-strike"
    rows: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    for page in range(DETAIL_MAX_PAGES):
        params["page"] = page
        batch = unwrap_rows(request_json(session, path, params))
        pages += 1
        rows.extend(batch)
        if spot is None:
            for item in batch:
                spot = as_float(item.get("underlying_price") or item.get("price"))
                if spot:
                    break
        if len(batch) < 500:
            break
        if PAUSE_SECONDS:
            time.sleep(PAUSE_SECONDS)
    else:
        truncated = True

    normalized = []
    expiries = set()
    for raw in rows:
        expiry = str(raw.get("expiry") or raw.get("expiration") or "")[:10]
        strike = as_float(raw.get("strike") or raw.get("strike_price"))
        if not expiry or strike is None:
            continue
        expiries.add(expiry)
        call_gex = as_float(raw.get("call_gex"))
        put_gex = as_float(raw.get("put_gex"))
        # The expiry/strike endpoint is the OI GEX surface. Do not fabricate a
        # volume-by-expiry surface by relabelling the OI fields.
        if call_gex is None:
            call_gex = as_float(raw.get("call_gamma_oi") or raw.get("call_gamma"))
        if put_gex is None:
            put_gex = as_float(raw.get("put_gamma_oi") or raw.get("put_gamma"))
        normalized.append({
            "ticker": symbol,
            "expiry": expiry,
            "strike": strike,
            "spot": as_float(raw.get("underlying_price") or raw.get("price")) or spot,
            "call_gex_oi": call_gex,
            "put_gex_oi": put_gex,
            "net_gex_oi": (call_gex or 0.0) + (put_gex or 0.0),
            "call_delta": as_float(raw.get("call_delta")),
            "put_delta": as_float(raw.get("put_delta")),
            "call_charm": as_float(raw.get("call_charm")),
            "put_charm": as_float(raw.get("put_charm")),
            "call_vanna": as_float(raw.get("call_vanna")),
            "put_vanna": as_float(raw.get("put_vanna")),
            "dte": as_float(raw.get("dte")),
            "date": str(raw.get("date") or "")[:10],
        })

    if not normalized:
        raise RuntimeError(f"{symbol}: UW returned no usable strike/expiry GEX rows.")
    levels: dict[str, Any] = {}
    for source in ("oi", "vol"):
        try:
            levels[source] = unwrap_object(request_json(
                session, f"/stock/{symbol}/gex-levels", {"source": source}
            ))
        except Exception as exc:
            levels[source] = {"error": str(exc)[:220]}

    dates = sorted({row["date"] for row in normalized if row["date"]})
    expiries_sorted = sorted(expiries)
    detail = {
        "schema": "marketdex-uw-gex-detail-v1",
        "metadata": {
            "ticker": symbol,
            "provider": "Unusual Whales Public API",
            "market_date": dates[-1] if dates else None,
            "fetched_at_utc": utc_now(),
            "spot": spot,
            "rows": len(normalized),
            "pages_fetched": pages,
            "pages_truncated": truncated,
            "expiries": expiries_sorted,
            "max_dte": DETAIL_MAX_DTE,
            "strike_range_pct": DETAIL_STRIKE_RANGE,
            "surface_basis": "open_interest",
            "note": (
                "Strike x expiry surface uses OI-based GEX from the UW expiry-strike endpoint. "
                "Directionalized-volume call wall, put wall, gamma flip and magnet are supplied "
                "separately in levels.vol; they are not a volume-by-expiry surface."
            ),
        },
        "levels": levels,
        "rows": normalized,
    }
    return detail


def load_cached_summary() -> dict[str, Any] | None:
    path = CACHE_DIR / "universe.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def publish_cached_files() -> bool:
    DATA_TICKERS.mkdir(parents=True, exist_ok=True)
    summary = load_cached_summary()
    if summary:
        (DATA_DIR / "universe.json").write_text(json.dumps(summary, separators=(",", ":"), allow_nan=False) + "\n")
    if CACHE_TICKERS.exists():
        for source in CACHE_TICKERS.glob("*.json"):
            shutil.copy2(source, DATA_TICKERS / source.name)
    return bool(summary)


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n")
    tmp.replace(path)


def run() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_TICKERS.mkdir(parents=True, exist_ok=True)
    CACHE_TICKERS.mkdir(parents=True, exist_ok=True)

    if not TOKEN:
        restored = publish_cached_files()
        status = {
            "schema": "marketdex-uw-gex-status-v1",
            "provider": "Unusual Whales Public API",
            "status": "cached_only" if restored else "not_configured",
            "generated_at_utc": utc_now(),
            "message": (
                "UW_TOKEN is not configured. Publishing the last cached GEX snapshot."
                if restored else
                "UW_TOKEN is not configured. The original MARKET-DEX price-history pipeline is unaffected. "
                "Add UW_TOKEN as a GitHub Actions repository secret to enable GEX snapshots."
            ),
        }
        save_json(DATA_DIR / "status.json", status)
        print(status["message"], flush=True)
        return 0

    session = new_session()
    try:
        rows, pages = fetch_iwv_gex_universe(session)
        if not rows:
            raise RuntimeError("UW IWV screener returned no optionable constituents.")
        dates = sorted({str(row.get("date") or "")[:10] for row in rows if row.get("date")})
        marketdex_count = load_marketdex_universe_count()
        payload = {
            "schema": "marketdex-uw-gex-universe-v1",
            "metadata": {
                "provider": "Unusual Whales Public API",
                "source": "UW stock screener filtered to IWV ETF holdings",
                "generated_at_utc": utc_now(),
                "market_date": dates[-1] if dates else None,
                "optionable_iwv_tickers": len(rows),
                "marketdex_iwv_equity_count": marketdex_count,
                "coverage_pct_of_marketdex_universe": (
                    round(100 * len(rows) / marketdex_count, 1)
                    if marketdex_count else None
                ),
                "pages_fetched": pages,
                "page_size": PAGE_SIZE,
                "complete": True,
                "note": (
                    "This is the optionable/covered subset of current IWV holdings with UW screener data, "
                    "not a claim that every Russell 3000 constituent has listed options or GEX. "
                    "Market Dex daily OHLCV remains the separate full IWV proxy universe."
                ),
            },
            "rows": rows,
        }
        save_json(CACHE_DIR / "universe.json", payload)
        save_json(DATA_DIR / "universe.json", payload)

        details = detail_ticker_list(rows)
        successes, failures = [], []
        summary_by_symbol = {row["ticker"]: row for row in rows}
        for index, symbol in enumerate(details, 1):
            try:
                detail = fetch_detail(session, symbol, summary_by_symbol.get(symbol))
                path = CACHE_TICKERS / f"{file_symbol(symbol)}.json"
                save_json(path, detail)
                shutil.copy2(path, DATA_TICKERS / path.name)
                successes.append(symbol)
                print(f"Detailed GEX {index}/{len(details)}: {symbol} · {len(detail['rows'])} strike/expiry rows", flush=True)
            except Exception as exc:
                failures.append({"ticker": symbol, "error": str(exc)[:240]})
                print(f"Detailed GEX failed for {symbol}: {str(exc)[:180]}", flush=True)
            if PAUSE_SECONDS:
                time.sleep(PAUSE_SECONDS)

        # Re-publish cached detail maps so a symbol requested on an earlier run
        # remains available to the static HTML.
        for path in CACHE_TICKERS.glob("*.json"):
            target = DATA_TICKERS / path.name
            if not target.exists():
                shutil.copy2(path, target)

        manifest = {
            "schema": "marketdex-uw-gex-manifest-v1",
            "generated_at_utc": utc_now(),
            "market_date": dates[-1] if dates else None,
            "summary_tickers": len(rows),
            "detail_cache_count": len(list(CACHE_TICKERS.glob("*.json"))),
            "details_refreshed": successes,
            "detail_failures": failures,
            "default_detail_limit": DETAIL_LIMIT,
            "max_detail_tickers_per_run": DETAIL_MAX_TICKERS,
            "explicit_detail_tickers": [safe_symbol(x) for x in re.split(r"[,;\s]+", EXPLICIT_TICKERS) if safe_symbol(x)],
            "detail_note": "Detailed strike-by-expiry surfaces are cached selectively to control API usage. Use workflow_dispatch input gex_detail_tickers for any additional ticker.",
        }
        save_json(DATA_DIR / "manifest.json", manifest)
        save_json(CACHE_DIR / "manifest.json", manifest)
        status = {
            "schema": "marketdex-uw-gex-status-v1",
            "provider": "Unusual Whales Public API",
            "status": "updated",
            "generated_at_utc": utc_now(),
            "market_date": dates[-1] if dates else None,
            "summary_tickers": len(rows),
            "detail_maps_refreshed": len(successes),
            "detail_failures": len(failures),
        }
        save_json(DATA_DIR / "status.json", status)
        print(json.dumps(status, indent=2), flush=True)
        return 0
    except Exception as exc:
        restored = publish_cached_files()
        status = {
            "schema": "marketdex-uw-gex-status-v1",
            "provider": "Unusual Whales Public API",
            "status": "stale_cache" if restored else "refresh_failed",
            "generated_at_utc": utc_now(),
            "message": str(exc)[:500],
        }
        save_json(DATA_DIR / "status.json", status)
        print("UW GEX refresh failed; original Market Dex pipeline will continue:", str(exc), flush=True)
        return 0  # GEX is optional; never fail the existing OHLCV deployment.


if __name__ == "__main__":
    raise SystemExit(run())
