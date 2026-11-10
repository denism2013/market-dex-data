#!/usr/bin/env python3
"""Official CFTC COT futures positioning, shared by VANTA & MARKET-DEX.

Read-only source: CFTC public Socrata TFF futures-only (gpe5-46if) and
Disaggregated futures-only (72hh-3qpy). No account or API key required.
COT is futures market positioning, NOT equity-specific short interest,
NOT options market-maker delta hedging and NOT real-time trading data.

Output: data/positioning/index.json, data/positioning/markets/<id>.json.
Never writes to data/latest.zip, data/gex or data/tickers.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'positioning'
CACHE = Path(os.getenv('MARKETDEX_COT_CACHE_DIR', str(ROOT / '.marketdex_cot_cache')))
HOST = 'https://publicreporting.cftc.gov/resource'
DATASETS = {'tff': 'gpe5-46if', 'disaggregated': '72hh-3qpy'}
CATEGORY_FIELDS = {
    'tff': {
        'leveraged_funds': ('lev_money_positions_long', 'lev_money_positions_short'),
        'asset_managers': ('asset_mgr_positions_long', 'asset_mgr_positions_short'),
        'dealers': ('dealer_positions_long', 'dealer_positions_short'),
        'other_reportables': ('other_rept_positions_long', 'other_rept_positions_short'),
    },
    'disaggregated': {
        'managed_money': ('m_money_positions_long', 'm_money_positions_short'),
        'producers_merchants': ('prod_merc_positions_long', 'prod_merc_positions_short'),
        'swap_dealers': ('swap_positions_long', 'swap_positions_short'),
        'other_reportables': ('other_rept_positions_long', 'other_rept_positions_short'),
    },
}
PRIMARY = {'tff': 'leveraged_funds', 'disaggregated': 'managed_money'}
MAX_MARKETS = int(os.environ.get('MARKETDEX_COT_MAX_MARKETS', '240'))
HISTORY_WEEKS = min(104, max(26, int(os.environ.get('MARKETDEX_COT_HISTORY_WEEKS', '70'))))
HTTP_PAGE = 3000
MAX_PAGES = 50
USER_AGENT = 'VANTA-MARKETDEX-COT-Positioning/1.0 (CFTC public data; educational market analysis)'


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def number(raw: Any) -> int | None:
    if raw is None or raw == '':
        return None
    try:
        n = float(raw)
        return round(n) if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def field(row: dict[str, Any], stem: str) -> int | None:
    # Different CFTC report families have both *_all and un-suffixed columns.
    # Never turn a missing position into a zero position.
    for k in (stem + '_all', stem):
        if k in row:
            return number(row[k])
    return None


def date_from(raw: Any) -> str:
    value = str(raw or '')[:10]
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return ''


def id_for(report: str, code: str) -> str:
    return report + '-' + re.sub(r'[^a-z0-9]+', '-', code.lower()).strip('-')


def normalize(row: dict[str, Any], report: str) -> dict[str, Any] | None:
    market = str(row.get('market_and_exchange_names') or '').strip()
    code = str(row.get('cftc_contract_market_code') or '').strip()
    date = date_from(row.get('report_date_as_yyyy_mm_dd'))
    oi = field(row, 'open_interest')
    if not date or not code or not market or oi is None or oi <= 0:
        return None
    categories: dict[str, dict[str, Any]] = {}
    for name, (left, right) in CATEGORY_FIELDS[report].items():
        long, short = field(row, left), field(row, right)
        if long is None or short is None or long < 0 or short < 0:
            continue
        net = long - short
        categories[name] = {'long': long, 'short': short, 'net': net,
                            'net_pct_oi': round(net / oi * 100, 4)}
    if PRIMARY[report] not in categories:
        return None
    return {'id': id_for(report, code), 'report': report, 'contract_code': code,
            'market': market, 'date': date, 'open_interest': oi, 'categories': categories}


def get_json(session: requests.Session, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    last = None
    for retry in range(4):
        try:
            resp = session.get(endpoint, params=params, timeout=(12, 90))
            if resp.status_code in (429, 500, 502, 503, 504):
                if retry < 3:
                    wait = min(25, float(resp.headers.get('Retry-After', 3 * (retry + 1))))
                    time.sleep(wait)
                    continue
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                raise ValueError('CFTC response is not a list')
            return payload
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if retry < 3:
                time.sleep(2 * (retry + 1))
    raise RuntimeError(f'CFTC source unavailable: {last}')


def fetch_report(session: requests.Session, report: str, since: str) -> list[dict[str, Any]]:
    url = f'{HOST}/{DATASETS[report]}.json'
    by_id_date: dict[tuple[str, str], dict[str, Any]] = {}
    for page in range(MAX_PAGES):
        params = {'$where': f"report_date_as_yyyy_mm_dd >= '{since}T00:00:00'",
                  '$order': 'report_date_as_yyyy_mm_dd DESC, id ASC',
                  '$limit': HTTP_PAGE, '$offset': page * HTTP_PAGE}
        batch = get_json(session, url, params)
        for raw in batch:
            item = normalize(raw, report)
            if item:
                key = item['id'], item['date']
                if key in by_id_date and by_id_date[key]['market'] != item['market']:
                    raise RuntimeError('Conflicting CFTC contract codes: ' + item['id'])
                by_id_date[key] = item
        print(report, 'page', page + 1, 'raw', len(batch), 'usable', len(by_id_date), flush=True)
        if len(batch) < HTTP_PAGE:
            break
    else:
        raise RuntimeError(f'CFTC {report} exceeded query page cap. Refusing incomplete output.')
    return list(by_id_date.values())


def build(datasets: dict[str, list[dict[str, Any]]], generated: str | None = None,
          min_markets: int = 40) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    generated = generated or stamp()
    overall: list[dict[str, Any]] = []
    by_market: dict[str, dict[str, Any]] = {}
    latest_dates: dict[str, str] = {}
    for report, rawrows in datasets.items():
        records: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for item in rawrows:
            if item.get('report') == report and PRIMARY[report] in item.get('categories', {}):
                records[item['id']].append(item)
        latest = max((r['date'] for r in rawrows), default='')
        if not latest:
            raise RuntimeError(f'CFTC {report} returned no usable records')
        latest_dates[report] = latest
        # Retain active contracts only, ordered by the latest open interest.
        active = []
        for mid, rows in records.items():
            history = sorted(rows, key=lambda r: r['date'])
            if len(history) < 10 or history[-1]['date'] < (dt.date.fromisoformat(latest) - dt.timedelta(days=28)).isoformat():
                continue
            active.append((mid, history))
        active.sort(key=lambda pair: pair[1][-1]['open_interest'], reverse=True)
        for mid, history in active[:MAX_MARKETS]:
            if len(history) > HISTORY_WEEKS:
                history = history[-HISTORY_WEEKS:]
            latest_row = history[-1]
            category = PRIMARY[report]
            nets = [p['categories'][category]['net'] for p in history if category in p['categories']]
            latest_net = latest_row['categories'][category]['net']
            # Rank of current net among each distinct weekly observation, not a probability.
            percentile = round(100 * sum(v <= latest_net for v in nets) / len(nets), 1) if len(nets) >= 12 else None
            prior = history[-2]['categories'].get(category) if len(history) > 1 else None
            change = latest_net - prior['net'] if prior else None
            base = {'id': mid, 'report': report, 'contract_code': latest_row['contract_code'],
                    'market': latest_row['market'], 'date': latest_row['date'],
                    'open_interest': latest_row['open_interest'], 'primary_category': category,
                    'latest': latest_row['categories'][category], 'change_from_prior_report': change,
                    'net_position_percentile': percentile, 'observations': len(history)}
            overall.append(base)
            by_market[mid] = {'schema': 'vanta-cftc-market-v1', 'metadata': {
                'source': 'U.S. Commodity Futures Trading Commission (CFTC)',
                'dataset': DATASETS[report], 'report': report, 'scope': 'futures_only',
                'generated_at_utc': generated, 'as_of_date': latest_row['date'],
                'contract_code': latest_row['contract_code'], 'market': latest_row['market'],
                'note': 'Positions are historical futures commitments, not stock positions, option delta hedging, or real time.'},
                'summary': base, 'history': history}
    if len(overall) < min_markets:
        raise RuntimeError(f'Only {len(overall)} CFTC markets qualified; refusing sparse publication')
    overall.sort(key=lambda r: (r['report'], -r['open_interest'], r['market']))
    result = {'schema': 'vanta-cftc-index-v1', 'metadata': {
        'provider': 'CFTC', 'scope': 'futures_only', 'source_datasets': DATASETS,
        'source_urls': {k: f'https://publicreporting.cftc.gov/d/{v}' for k, v in DATASETS.items()},
        'generated_at_utc': generated, 'latest_dates': latest_dates,
        'total_markets': len(overall), 'report_lag': 'Publication typically follows position date; verify actual report date.',
        'notes': ['No API key required.', 'No individual-equity positioning in COT.',
                  'A 52-report net-position percentile is descriptive, not predictive.',
                  'CFTC has announced a move to twice-weekly reports; use dates, not fixed weekly assumptions.']},
              'markets': overall}
    return result, by_market


def save(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def publish_cached() -> bool:
    index = CACHE / 'index.json'
    markets = CACHE / 'markets'
    if not index.is_file() or not markets.is_dir():
        return False
    OUTPUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index, OUTPUT / 'index.json')
    (OUTPUT / 'markets').mkdir(parents=True, exist_ok=True)
    for path in markets.glob('*.json'):
        shutil.copy2(path, OUTPUT / 'markets' / path.name)
    return True


def main() -> int:
    # Pull 70 weekly reports plus buffer; actual publication date is read from data.
    since = (dt.date.today() - dt.timedelta(days=7 * (HISTORY_WEEKS + 4))).isoformat()
    session = requests.Session()
    session.headers.update({'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'User-Agent': USER_AGENT})
    try:
        data = {kind: fetch_report(session, kind, since) for kind in DATASETS}
        idx, details = build(data)
        CACHE.mkdir(parents=True, exist_ok=True)
        for mid, record in details.items():
            save(CACHE / 'markets' / f'{mid}.json', record)
        save(CACHE / 'index.json', idx)
        publish_cached()
        save(OUTPUT / 'status.json', {'schema': 'vanta-cftc-status-v1', 'status': 'updated',
              'checked_at_utc': stamp(), 'latest_dates': idx['metadata']['latest_dates']})
        print(f'CFTC published {len(details)} verified markets; position dates {idx["metadata"]["latest_dates"]}')
    except Exception as exc:
        restored = publish_cached()
        save(OUTPUT / 'status.json', {'schema': 'vanta-cftc-status-v1',
              'status': 'cached_only' if restored else 'unavailable',
              'checked_at_utc': stamp(), 'error': str(exc)[:250]})
        print('CFTC update unavailable; cache preserved:', str(exc)[:250], flush=True)
        # Fail the optional step observably without blocking existing market publication.
        return 0 if restored else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
