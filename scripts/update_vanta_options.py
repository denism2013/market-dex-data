#!/usr/bin/env python3
"""VANTA optional 5 PM options-snapshot publisher.

Only publishes a separately licensed/redistributable options feed supplied by the
owner. Does NOT scrape Yahoo, proxy the ChatGPT UW connector, or expose API tokens.
Reads existing MARKET-DEX IWV catalog; never mutates the OHLCV or CFTC datasets.

Source contract: VANTA_OPTIONS_SOURCE_URL_TEMPLATE is an HTTPS URL with {ticker}
substitution, serving a VANTA dashboard JSON: metadata, contracts, and summary.
The source itself must be licensed for third-party public redistribution.
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
from urllib.parse import quote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'options'
CACHE = Path(os.getenv('VANTA_OPTIONS_CACHE_DIR', str(ROOT / '.marketdex_options_cache')))
UNIVERSE = ROOT / 'data' / 'universe.json'
VALID = re.compile(r'^[A-Z0-9.^_-]{1,24}$')
SCHEMA = 'vanta-options-feed-v1'
BASELINE = ['SPY', 'QQQ', 'IWM', 'DIA', 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AMD', 'AVGO', 'JPM', 'XLF', 'XLK']
MAX_SYMBOLS = min(300, max(1, int(os.getenv('VANTA_OPTIONS_MAX_SYMBOLS', '60'))))
MAX_EXPIRIES = min(14, max(1, int(os.getenv('VANTA_OPTIONS_MAX_EXPIRIES', '6'))))
MAX_CONTRACTS = 18000


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def safe_number(raw, *, nonnegative=False):
    if raw is None or raw == '':
        return None
    try:
        val = float(raw)
        if math.isfinite(val) and (not nonnegative or val >= 0):
            return val
    except (TypeError, ValueError):
        pass
    return None


def symbol(raw) -> str:
    value = str(raw or '').strip().upper()
    return value if VALID.fullmatch(value) else ''


def ordered_symbols():
    selected = list(BASELINE)
    if UNIVERSE.is_file():
        try:
            catalog = json.loads(UNIVERSE.read_text())
            members = catalog.get('members') or []
            if catalog.get('schema') == 'marketdex-universe-v1':
                # IWV weight is indicative holding weight; not proof of options coverage.
                def weight(x):
                    return safe_number(x.get('weight_pct'), nonnegative=True) or 0
                selected += [x.get('symbol') for x in sorted(members, key=weight, reverse=True)]
        except (OSError, ValueError, TypeError):
            pass
    extra = re.split(r'[,\s;]+', os.getenv('VANTA_OPTIONS_EXTRA_TICKERS', '').strip())
    selected = [symbol(x) for x in extra if symbol(x)] + selected
    return list(dict.fromkeys(x for x in map(symbol, selected) if x))[:MAX_SYMBOLS]


def sanitize(raw, expected_ticker):
    if not isinstance(raw, dict) or not isinstance(raw.get('metadata'), dict) or not isinstance(raw.get('contracts'), list):
        raise ValueError('Expected JSON with metadata and contracts array')
    meta = raw['metadata']
    if symbol(meta.get('ticker')) != expected_ticker:
        raise ValueError('Source ticker mismatch')
    if len(raw['contracts']) > MAX_CONTRACTS:
        raise ValueError('Source chain exceeds bounded contract limit')
    entries = []
    expiries = set()
    for item in raw['contracts']:
        if not isinstance(item, dict):
            continue
        typ = {'call': 'Call', 'put': 'Put'}.get(str(item.get('type', '')).strip().lower())
        exp = str(item.get('expiration', ''))[:10]
        strike = safe_number(item.get('strike'), nonnegative=True)
        if not typ or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', exp) or strike is None or strike <= 0:
            continue
        try:
            dt.date.fromisoformat(exp)
        except ValueError:
            continue
        if exp not in expiries and len(expiries) >= MAX_EXPIRIES:
            continue
        expiries.add(exp)
        bid = safe_number(item.get('bid'), nonnegative=True)
        ask = safe_number(item.get('ask'), nonnegative=True)
        iv = safe_number(item.get('impliedVolatility'), nonnegative=True)
        volume = safe_number(item.get('volume'), nonnegative=True)
        oi = safe_number(item.get('openInterest'), nonnegative=True)
        mid = (bid + ask) / 2 if bid is not None and ask is not None and bid > 0 and ask >= bid else None
        spread = (ask - bid) / mid if mid and bid is not None and ask is not None else None
        good_quote = bool(mid and iv is not None and .01 < iv < 5 and spread is not None and spread <= .35)
        rec = {
            'expiration': exp, 'type': typ, 'strike': strike,
            'volume': volume, 'openInterest': oi, 'bid': bid, 'ask': ask,
            'mid': mid, 'rel_spread': spread,
            'impliedVolatility': iv, 'quote_ok': good_quote,
            'vol_oi': (volume / oi) if volume is not None and oi and oi > 0 else None,
            'premium_notional_proxy': volume * mid * 100 if volume is not None and mid else None,
            'dte': max(0, (dt.date.fromisoformat(exp) - dt.datetime.now(dt.timezone.utc).date()).days),
            'trade_direction': 'UNKNOWN (no aggressor-side data)',
        }
        rec['moneyness_pct'] = safe_number(item.get('moneyness_pct'))
        rec['signal'] = str(item.get('signal') or 'UNVERIFIED')[:65]
        entries.append(rec)
    if not entries:
        raise ValueError('No valid option contracts; refusing an empty publication')
    dates = sorted({r['expiration'] for r in entries})
    spot = safe_number(meta.get('spot_daily_close'), nonnegative=True)
    stamp = str(meta.get('run_utc') or meta.get('source_timestamp_utc') or '')
    try:
        if stamp:
            dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        stamp = ''
    if not stamp:
        raise ValueError('Snapshot must have a valid provider timestamp')
    # Summary is descriptive call/put volume and open interest. Never fabricate ATM quotes.
    summary = []
    for expiry in dates:
        group = [r for r in entries if r['expiration'] == expiry]
        calls = [r for r in group if r['type'] == 'Call']
        puts = [r for r in group if r['type'] == 'Put']
        cv = sum(r['volume'] or 0 for r in calls)
        pv = sum(r['volume'] or 0 for r in puts)
        co = sum(r['openInterest'] or 0 for r in calls)
        po = sum(r['openInterest'] or 0 for r in puts)
        ivs = [r['impliedVolatility'] for r in group if r['quote_ok']]
        summary.append({'expiration': expiry,
                        'dte': max(0, (dt.date.fromisoformat(expiry) - dt.datetime.now(dt.timezone.utc).date()).days),
                        'call_volume': cv, 'put_volume': pv, 'call_oi': co, 'put_oi': po,
                        'put_call_volume_ratio': pv / cv if cv else None,
                        'atm_iv': None,
                        'valid_quotes': len(ivs),
                        'straddle_mid': None, 'max_pain': None,
                        'approx_1sigma_move_pct': None})
    # Preserves *verified* upstream summary fields if explicitly furnished with validity constraints.
    raw_summaries = {str(s.get('expiration', '')): s for s in raw.get('summary', []) if isinstance(s, dict)}
    optional = ('atm_strike', 'straddle_mid', 'straddle_cost_pct', 'straddle_breakeven_low',
                'straddle_breakeven_high', 'approx_1sigma_move_pct', 'rv20_1sigma_move_dollars',
                'implied_vs_rv20_move_ratio', 'atm_iv', 'max_pain')
    for rec in summary:
        original = raw_summaries.get(rec['expiration'], {})
        for key in optional:
            val = safe_number(original.get(key), nonnegative=True)
            if val is not None:
                rec[key] = val
    # No actual dealer sign can be inferred from OI; never publish a fabricated gamma wall.
    result = {
        'schema': 'vanta-options-snapshot-v1',
        'metadata': {'ticker': expected_ticker, 'run_utc': stamp,
                     'spot_daily_close': spot, 'spot_bar': str(meta.get('spot_bar') or ''),
                     'provider': str(meta.get('provider') or 'licensed external options source')[:100],
                     'provenance': 'Authorized third-party options snapshot; not consolidated prints',
                     'limitations': 'Bid/ask may be delayed; call/put OI does not identify actual dealer positions. No buyer/seller side.'},
        'contracts': entries, 'summary': summary, 'gamma': [], 'alerts': [], 'iv_alerts': [],
    }
    return result


def fetch_source(session, template, ticker, token):
    url = template.replace('{ticker}', quote(ticker, safe=''))
    headers = {'Accept': 'application/json', 'User-Agent': 'VANTA-Authorized-Feed/1.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    error = None
    for attempt in range(3):
        try:
            r = session.get(url, timeout=28, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(15, 2 ** attempt))
                continue
            r.raise_for_status()
            if len(r.content) > 12 * 1024 * 1024:
                raise ValueError('Unreasonably large options snapshot')
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            time.sleep(min(3, attempt + 1))
    raise RuntimeError(f'Source request failed ({type(error).__name__ if error else "retry limit"})')


def validated_cache(ticker):
    """Only reuse recent, source-dated snapshots; never label cache as fresh."""
    path = CACHE / 'tickers' / (re.sub('[^A-Z0-9_-]', '_', ticker) + '.json')
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
        if str(raw.get('metadata', {}).get('ticker', '')).upper() != ticker:
            return None
        stamp = str(raw['metadata']['run_utc'])
        asof = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if asof.tzinfo is None:
            return None
        age = (dt.datetime.now(dt.timezone.utc) - asof.astimezone(dt.timezone.utc)).total_seconds() / 86400
        if age < -.25 or age > min(7, max(1, int(os.getenv('VANTA_OPTIONS_CACHE_MAX_DAYS', '3')))):
            return None
        kept = [r for r in raw.get('contracts', []) if str(r.get('expiration', '')) >= dt.datetime.now(dt.timezone.utc).date().isoformat()]
        if not kept:
            return None
        raw['contracts'] = kept
        raw['summary'] = [r for r in raw.get('summary', []) if str(r.get('expiration', '')) >= dt.datetime.now(dt.timezone.utc).date().isoformat()]
        return raw
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remember(ticker, payload):
    folder = CACHE / 'tickers'
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / (re.sub('[^A-Z0-9_-]', '_', ticker) + '.json')
    file.write_text(json.dumps(payload, separators=(',', ':'), allow_nan=False) + '\n')


def record_status(state, reason, **extra):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    payload = {'schema': 'vanta-options-status-v1', 'generated_at_utc': now(),
               'state': state, 'message': reason, **extra}
    (OUTPUT / 'status.json').write_text(json.dumps(payload, indent=2) + '\n')
    return payload


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    allowed = os.getenv('VANTA_OPTIONS_PUBLICATION_APPROVED', '').lower() == 'true'
    template = os.getenv('VANTA_OPTIONS_SOURCE_URL_TEMPLATE', '').strip()
    if not allowed:
        shutil.rmtree(OUTPUT / 'tickers', ignore_errors=True)
        (OUTPUT / 'manifest.json').unlink(missing_ok=True)
        record_status('permission_required', 'Public options redistribution is off. Supply written publication rights before enabling this feed.')
        print('VANTA options: public publication disabled; not fetching any provider')
        return
    if ('{ticker}' not in template or urlparse(template).scheme != 'https'
            or not urlparse(template).netloc):
        shutil.rmtree(OUTPUT / 'tickers', ignore_errors=True)
        (OUTPUT / 'manifest.json').unlink(missing_ok=True)
        record_status('source_not_configured', 'Set an HTTPS licensed source URL template with a {ticker} placeholder.')
        return
    entries, misses = [], []
    folder = OUTPUT / 'tickers'
    folder.mkdir(parents=True, exist_ok=True)
    token = os.getenv('VANTA_OPTIONS_SOURCE_TOKEN', '')
    with requests.Session() as session:
        for i, tick in enumerate(ordered_symbols(), 1):
            cached = False
            try:
                payload = sanitize(fetch_source(session, template, tick, token), tick)
                remember(tick, payload)
            except Exception as exc:
                payload = validated_cache(tick)
                cached = bool(payload)
                misses.append({'ticker': tick, 'error_class': type(exc).__name__, 'cached_snapshot_available': cached})
                if not payload:
                    print(f'VANTA {i}: {tick} unavailable ({type(exc).__name__})', flush=True)
                    continue
            file = folder / (re.sub('[^A-Z0-9_-]', '_', tick) + '.json')
            file.write_text(json.dumps(payload, separators=(',', ':'), allow_nan=False) + '\n')
            entries.append({'ticker': tick,
                            'path': f'data/options/tickers/{file.name}',
                            'contracts': len(payload['contracts']),
                            'expiries': len(payload['summary']),
                            'provider_snapshot_utc': payload['metadata']['run_utc'],
                            'cached': cached})
            print(f'VANTA {i}: {tick} {len(payload["contracts"])} contracts'+(' [STALE CACHED]' if cached else ' [NEW]'), flush=True)
            time.sleep(max(0, min(2, float(os.getenv('VANTA_OPTIONS_PAUSE', '0.2')))))
    if not entries:
        record_status('source_unavailable', 'No valid licensed snapshots fetched. Previously verified files were not modified.', failures=len(misses))
        return
    manifest = {'schema': SCHEMA, 'generated_at_utc': now(),
                'source': 'User-authorized, licensed external options snapshot endpoint',
                'coverage': len(entries), 'attempted': len({x['ticker'] for x in entries} | {x['ticker'] for x in misses}),
                'published_tickers': entries, 'failures': misses[:250],
                'notes': 'Current snapshot, not historical dealer inventory. Time may differ from daily OHLCV. Missing tickers are not filled synthetically.'}
    (OUTPUT / 'manifest.json').write_text(json.dumps(manifest, separators=(',', ':')) + '\n')
    record_status('ready', 'Verified authorized options snapshots published.', coverage=len(entries), failures=len(misses))


if __name__ == '__main__':
    main()
