#!/usr/bin/env python3
"""MARKET-DEX daily equity universe snapshot, scheduled 5 PM New York weekdays.

Universe: current iShares IWB equity holdings (Russell 1000 proxy, not point-in-time).
Outputs: data/latest.zip; data/universe.json; data/tickers/<SYMBOL>.zip.
Uses adjusted yfinance OHLCV. Neither true order flow nor live quotes.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
CACHE = Path(os.environ.get("MARKETDEX_CACHE_DIR", str(BASE / ".marketdex_cache")))
HOLDINGS_URLS = [
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1395165510754.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/latest-holdings.csv",
]
PUBLISHED_UNIVERSE_URL = "https://denism2013.github.io/market-dex-data/data/universe.json"
VALID = re.compile(r"^[A-Z0-9.^=_-]{1,24}$")
CORE = [
    "SPY", "QQQ", "DIA", "IWM", "RSP", "HYG", "TLT", "GLD", "^VIX", "^TNX",
    "XLK", "XLF", "XLE", "XLY", "XLP", "XLV", "XLI", "XLU", "XLB", "XLRE", "XLC",
]
EXTRAS = ["IWB", "VTI", "SMH", "XBI", "ARKK"]
COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]
SYMBOL_LIMIT = int(os.getenv("MARKETDEX_MAX_SYMBOLS", "0"))
BATCH_SIZE = max(2, min(30, int(os.getenv("MARKETDEX_BATCH_SIZE", "18"))))
MAX_RETRIES = max(0, min(3, int(os.getenv("MARKETDEX_RETRIES", "1"))))
BACKFILL = os.getenv("MARKETDEX_PERIOD", "5y")
TEST = os.getenv("MARKETDEX_TEST_MODE", "0") == "1"


def normalize_symbol(raw: str) -> str:
    ticker = re.sub(r"\s+", "-", str(raw or "").strip().upper())
    ticker = ticker.replace("/", "-")
    return ticker if VALID.fullmatch(ticker) else ""


def parse_holdings(source: str) -> tuple[list[dict], str]:
    lines = source.lstrip("\ufeff").splitlines()
    asof = ""
    for line in lines[:12]:
        if line.startswith("Fund Holdings as of,"):
            bits = next(csv.reader([line]))
            if len(bits) > 1:
                try:
                    asof = dt.datetime.strptime(bits[1], "%b %d, %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("Ticker,Name,Sector,Asset Class,")),
        None,
    )
    if start is None:
        raise RuntimeError("IWB holdings CSV format changed: missing header")
    members: dict[str, dict] = {}
    for item in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if item.get("Asset Class") != "Equity":
            continue
        sym = normalize_symbol(item.get("Ticker", ""))
        if not sym or sym == "-":
            continue
        if item.get("Currency", "").upper() not in ("USD", ""):
            continue
        members[sym] = {
            "symbol": sym,
            "name": item.get("Name", "").strip(),
            "sector": item.get("Sector", "").strip(),
            "weight_pct": item.get("Weight (%)", "").replace(",", "").strip(),
        }
    if len(members) < 900:
        raise RuntimeError(f"IWB holdings returned only {len(members)} equity symbols")
    return list(members.values()), asof


def discover_universe() -> tuple[list[dict], str, str]:
    if TEST:
        return (
            [{"symbol": sym, "name": sym + " demo", "sector": "Technology", "weight_pct": "0"}
             for sym in ("AAPL", "MSFT", "NVDA", "JPM")],
            "2026-11-09",
            "DETERMINISTIC TEST",
        )
    last_error = None
    for holdings_url in HOLDINGS_URLS:
        try:
            r = requests.get(
                holdings_url, timeout=35,
                headers={"User-Agent": "Mozilla/5.0 (compatible; market-research/1.0)",
                         "Accept": "text/csv,text/plain,*/*"},
            )
            r.raise_for_status()
            members, asof = parse_holdings(r.text)
            return members, asof, "iShares IWB published fund holdings (Russell 1000 proxy)"
        except Exception as exc:
            last_error = exc
            print("IWB source variant unavailable:", str(exc)[:160], flush=True)
    try:
        raise RuntimeError("All IWB holdings endpoint variants unavailable") from last_error
    except Exception as exc:
        print("IWB source temporarily unavailable:", str(exc)[:240], flush=True)
        try:
            local = CACHE / "universe.json"
            if local.exists():
                data = json.loads(local.read_text())
            else:
                r = requests.get(PUBLISHED_UNIVERSE_URL, timeout=20)
                r.raise_for_status()
                data = r.json()
            members = data.get("members") or []
            if len(members) >= 900:
                return members, data.get("holdings_as_of", ""), "previously verified IWB holdings (fallback)"
        except Exception as again:
            print("Previous-universe fallback also unavailable:", str(again)[:240], flush=True)
        raise RuntimeError("Cannot verify the Russell 1000 proxy membership; refusing to invent a universe") from exc


def tidy(frame: pd.DataFrame, min_rows: int = 20) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("empty history")
    x = frame.copy()
    if isinstance(x.index, pd.DatetimeIndex):
        x = x.reset_index().rename(
            columns={x.index.name or "index": "Date", "Datetime": "Date"}
        )
    if "Date" not in x:
        raise ValueError("missing Date column")
    x["Date"] = pd.to_datetime(x["Date"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d")
    for col in COLUMNS[1:]:
        if col not in x.columns:
            raise ValueError("missing " + col)
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x[COLUMNS].dropna()
    x = x[
        (x.Close > 0) & (x.Open > 0) & (x.Volume >= 0) &
        (x.High >= x[["Open", "Low", "Close"]].max(axis=1)) &
        (x.Low <= x[["Open", "High", "Close"]].min(axis=1))
    ]
    x = x.drop_duplicates(subset="Date", keep="last").sort_values("Date").reset_index(drop=True)
    if len(x) < min_rows:
        raise ValueError(f"only {len(x)} clean daily bars")
    return x


def from_batch(frame: pd.DataFrame, sym: str, count: int) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None
    d = frame
    if isinstance(d.columns, pd.MultiIndex):
        try:
            if sym in d.columns.get_level_values(0):
                d = d[sym]
            elif sym in d.columns.get_level_values(1):
                d = d.xs(sym, level=1, axis=1)
            else:
                return None
        except (KeyError, IndexError):
            return None
    elif count != 1:
        return None
    try:
        return tidy(d, min_rows=3)
    except Exception as exc:
        print(f"{sym}: invalid downloaded dataset ({str(exc)[:100]})", flush=True)
        return None


def fake_download(symbols: list[str], period: str) -> pd.DataFrame:
    import numpy as np
    periods = 45 if period == "1mo" else 820
    dates = pd.bdate_range("2023-09-01", periods=periods)
    columns = {}
    for n, sym in enumerate(symbols):
        close = 80 + n * 12 + np.arange(periods) * .075 + np.sin(np.arange(periods) / 11) * 2
        columns[sym] = pd.DataFrame({
            "Open": close-0.13, "High": close+0.8, "Low": close-0.6,
            "Close": close, "Volume": np.repeat(2500000+n*4500, periods)
        }, index=dates)
    return pd.concat(columns, axis=1)


def get_download(symbols: list[str], period: str) -> pd.DataFrame:
    if TEST:
        return fake_download(symbols, period)
    if yf is None:
        raise RuntimeError("yfinance required for live daily download")
    return yf.download(
        tickers=" ".join(symbols), period=period, interval="1d",
        auto_adjust=True, progress=False, actions=False,
        group_by="ticker", threads=5, timeout=22, multi_level_index=True,
    )


def symbol_path(sym: str) -> Path:
    return CACHE / "history" / (re.sub(r"[^A-Z0-9_-]", "_", sym) + ".csv")


def get_cached(sym: str) -> pd.DataFrame | None:
    p = symbol_path(sym)
    if not p.exists():
        return None
    try:
        return tidy(pd.read_csv(p))
    except Exception as exc:
        print(f"bad cached {sym}: {exc}", flush=True)
        return None


def store_history(sym: str, frame: pd.DataFrame) -> None:
    path = symbol_path(sym)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def update_history(symbols: list[str]) -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    histories = {sym: get_cached(sym) for sym in symbols}
    groups: dict[str, list[str]] = {"1mo": [], BACKFILL: []}
    for sym in symbols:
        previous = histories[sym]
        if previous is not None and len(previous) > 400:
            groups["1mo"].append(sym)
        else:
            groups[BACKFILL].append(sym)
    failures: list[str] = []
    updated: set[str] = set()
    for period, selected in groups.items():
        for start in range(0, len(selected), BATCH_SIZE):
            batch = selected[start:start+BATCH_SIZE]
            remaining = batch[:]
            for attempt in range(MAX_RETRIES + 1):
                if not remaining:
                    break
                try:
                    result = get_download(remaining, period)
                except Exception as exc:
                    print("Batch download failed:", str(exc)[:180], flush=True)
                    result = pd.DataFrame()
                missing = []
                for sym in remaining:
                    recent = from_batch(result, sym, len(remaining))
                    if recent is None:
                        missing.append(sym)
                        continue
                    previous = histories[sym]
                    try:
                        full = tidy(
                            pd.concat([previous, recent], ignore_index=True)
                            if previous is not None else recent
                        )
                    except Exception as exc:
                        print(f"{sym}: merge failed: {exc}", flush=True)
                        missing.append(sym)
                        continue
                    histories[sym] = full
                    store_history(sym, full)
                    updated.add(sym)
                remaining = missing
                if remaining and attempt < MAX_RETRIES and not TEST:
                    time.sleep(3*(attempt+1))
            failures.extend(remaining)
            if (start // BATCH_SIZE) % 6 == 0 or start+BATCH_SIZE >= len(selected):
                print(
                    f"{period}: processed {min(start+BATCH_SIZE,len(selected))}/{len(selected)}; "
                    f"successes {len(updated)}; failures {len(failures)}",
                    flush=True,
                )
            if not TEST:
                time.sleep(float(os.getenv("MARKETDEX_PAUSE", "0.6")))
    kept = {sym: hist for sym, hist in histories.items() if hist is not None and len(hist) >= 20}
    return kept, sorted(updated), sorted(set(failures))


def zipped_bytes(parts: dict[str, pd.DataFrame | str]) -> bytes:
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, value in parts.items():
            if isinstance(value, pd.DataFrame):
                z.writestr(name, value.to_csv(index=False, float_format="%.8g"))
            else:
                z.writestr(name, str(value))
    return memory.getvalue()


def market_context(histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    out = []
    for sym in CORE:
        if sym not in histories:
            continue
        for row in histories[sym].tail(520).itertuples(index=False):
            out.append({
                "Date": row.Date, "Symbol": sym,
                "Close": row.Close, "Volume": row.Volume,
            })
    return pd.DataFrame(out, columns=["Date", "Symbol", "Close", "Volume"])


def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "tickers").mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    members, holdings_asof, source = discover_universe()
    if SYMBOL_LIMIT:
        members = members[:SYMBOL_LIMIT]
    equities = sorted({member["symbol"] for member in members})
    all_symbols = list(dict.fromkeys(CORE + EXTRAS + equities))
    print(
        f"Holdings source: {source} ({holdings_asof}) | "
        f"equities={len(equities)} | requested={len(all_symbols)}",
        flush=True,
    )
    histories, updated, failed = update_history(all_symbols)
    if "SPY" not in histories or "QQQ" not in histories:
        raise RuntimeError("SPY / QQQ baseline unavailable; refusing to publish invalid baseline")
    context = market_context(histories)
    primary, comparator = histories["SPY"], histories["QQQ"]
    parts = {"ticker_SPY.csv": primary, "benchmark_QQQ.csv": comparator}
    if len(context):
        parts["context_market.csv"] = context
    (DATA / "latest.zip").write_bytes(zipped_bytes(parts))
    (DATA / "latest.json").write_text(json.dumps({
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "primary": "SPY", "benchmark": "QQQ",
        "latest_bar_date": str(primary.Date.iloc[-1]),
        "last_benchmark_date": str(comparator.Date.iloc[-1]),
        "market_context_symbols": [sym for sym in CORE if sym in histories],
        "notes": "Adjusted Yahoo daily OHLCV, not live quotes, true order flow, or historical constituent membership.",
    }, indent=2) + "\n")

    items: list[dict] = []
    for member in members:
        sym = member["symbol"]
        if sym not in histories:
            continue
        hist = histories[sym]
        peer = comparator if sym == "SPY" else primary
        peer_symbol = "QQQ" if sym == "SPY" else "SPY"
        file_name = f"ticker_{re.sub(r'[^A-Z0-9_-]', '_', sym)}.csv"
        package = {file_name: hist, f"benchmark_{peer_symbol}.csv": peer}
        (DATA / "tickers" / f"{sym}.zip").write_bytes(zipped_bytes(package))
        items.append({
            **member, "bars": len(hist), "last_bar_date": str(hist.Date.iloc[-1]),
            "fresh_download": sym in updated, "path": f"data/tickers/{sym}.zip",
        })
    present = {item["symbol"] for item in items}
    for sym in CORE + EXTRAS:
        if sym not in histories or sym in present:
            continue
        hist = histories[sym]
        bench_symbol = "QQQ" if sym == "SPY" else "SPY"
        bench_frame = comparator if sym == "SPY" else primary
        file_name = f"ticker_{re.sub(r'[^A-Z0-9_-]', '_', sym)}.csv"
        (DATA / "tickers" / f"{sym}.zip").write_bytes(zipped_bytes({
            file_name: hist, f"benchmark_{bench_symbol}.csv": bench_frame,
        }))
        items.append({
            "symbol": sym, "name": sym, "sector": "Index / ETF",
            "weight_pct": "", "bars": len(hist),
            "last_bar_date": str(hist.Date.iloc[-1]),
            "fresh_download": sym in updated, "path": f"data/tickers/{sym}.zip",
        })
    universe = {
        "schema": "marketdex-universe-v1",
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "holdings_as_of": holdings_asof,
        "source": source,
        "universe_note": "Current iShares IWB equity holdings, a Russell 1000 proxy. Historical backtests using current members have survivorship bias.",
        "requested_equities": len(equities),
        "available_equities": len([item for item in items if item["symbol"] in equities]),
        "requested_total": len(all_symbols),
        "downloaded_fresh_count": len(updated),
        "failed_downloads": failed[:100],
        "members": items,
    }
    (DATA / "universe.json").write_text(json.dumps(universe, separators=(",", ":")) + "\n")
    (CACHE / "universe.json").write_text(json.dumps({**universe, "members": members}) + "\n")
    total_bytes = sum(p.stat().st_size for p in DATA.rglob("*") if p.is_file())
    print(json.dumps({k: v for k, v in universe.items() if k not in ("members", "failed_downloads")}, indent=2))
    print(
        f"Published {len(items)} symbol ZIPs; SPY latest bar {primary.Date.iloc[-1]}; "
        f"bytes {total_bytes:,}",
        flush=True,
    )
    if len(items) < min(100, max(3, len(equities)//6)):
        raise RuntimeError("Insufficient downloaded history; refusing to advertise sparse universe")


if __name__ == "__main__":
    main()
