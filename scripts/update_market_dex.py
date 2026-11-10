#!/usr/bin/env python3
"""Create the ZIP consumed by MARKET-DEX's existing HTML importer.
Daily OHLCV, benchmark, and optional market context. No invented tick/order flow.
"""
from __future__ import annotations
import datetime as dt
import io
import json
import os
import re
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

NY = ZoneInfo('America/New_York')
BASE = Path(__file__).resolve().parents[1]
DATA = BASE / 'data'
TICKER = os.environ.get('MARKETDEX_TICKER', 'SPY').upper().strip()
BENCHMARK = os.environ.get('MARKETDEX_BENCHMARK', 'QQQ').upper().strip()
PERIOD = os.environ.get('MARKETDEX_PERIOD', '10y')
CONTEXT = [
    'SPY', 'QQQ', 'DIA', 'IWM', 'RSP', 'HYG', 'TLT', 'GLD', '^VIX', '^TNX',
    'XLK', 'XLF', 'XLE', 'XLY', 'XLP', 'XLV', 'XLI', 'XLU',
    'XLB', 'XLRE', 'XLC',
]
VALID = re.compile(r'^[A-Z0-9.^=_-]{1,24}$')
for symbol in (TICKER, BENCHMARK):
    if not VALID.fullmatch(symbol):
        raise ValueError('Invalid Yahoo Finance ticker: ' + symbol)


def get_daily(symbol: str, period: str) -> pd.DataFrame:
    frame = yf.download(symbol, period=period, interval='1d', auto_adjust=True,
                        progress=False, actions=False, threads=False)
    if frame is None or frame.empty:
        raise RuntimeError(f'No yfinance data for {symbol}')
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index().rename(columns={'Datetime': 'Date'})
    columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
    frame = frame[columns].copy()
    frame['Date'] = pd.to_datetime(frame['Date'], utc=True).dt.strftime('%Y-%m-%d')
    for k in columns[1:]:
        frame[k] = pd.to_numeric(frame[k], errors='coerce')
    frame = frame.dropna(subset=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
    frame = frame[(frame['Close'] > 0) & (frame['High'] >= frame['Low']) & (frame['Volume'] >= 0)]
    frame = frame.drop_duplicates('Date', keep='last').sort_values('Date')
    if len(frame) < 240:
        raise RuntimeError(f'{symbol} has fewer than 240 valid daily bars')
    return frame


def main() -> None:
    DATA.mkdir(exist_ok=True, parents=True)
    price = get_daily(TICKER, PERIOD)
    benchmark = get_daily(BENCHMARK, PERIOD)
    context_records: list[dict] = []
    context_ok = []
    for symbol in CONTEXT:
        try:
            fr = get_daily(symbol, '2y')
            context_ok.append(symbol)
            context_records.extend({
                'Date': row.Date, 'Symbol': symbol,
                'Close': row.Close, 'Volume': row.Volume
            } for row in fr.itertuples(index=False))
        except Exception as exc:
            print(f'Optional context {symbol} skipped: {str(exc)[:150]}')
    if not context_records:
        print('Market context unavailable. Core ticker and benchmark ZIP will still work.')
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    zip_path = DATA / 'latest.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=7) as z:
        clean = lambda sym: re.sub(r'[^A-Za-z0-9_-]', '_', sym)
        z.writestr(f'ticker_{clean(TICKER)}.csv', price.to_csv(index=False))
        z.writestr(f'benchmark_{clean(BENCHMARK)}.csv', benchmark.to_csv(index=False))
        if context_records:
            context_frame = pd.DataFrame(context_records)
            z.writestr('context_market.csv', context_frame.to_csv(index=False))
    manifest = {'generated_at_utc': stamp, 'primary': TICKER,
                'benchmark': BENCHMARK, 'latest_bar_date': price['Date'].iloc[-1],
                'last_benchmark_date': benchmark['Date'].iloc[-1],
                'market_context_symbols': context_ok,
                'note': 'Daily adjusted yfinance OHLCV, not live quotes or true order flow.'}
    (DATA / 'latest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))
    print(f'Wrote {zip_path} ({zip_path.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
