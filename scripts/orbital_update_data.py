#!/usr/bin/env python3
"""ORBITAL: source-traceable market universe snapshot from public iShares holdings + optional yfinance.

Usage: python scripts/orbital_update_data.py
Writes data/orbital-market-universe.json and data/orbital-company-metadata.json.
Fund holdings are NOT official index member lists. Do not redistribute Yahoo data in violation of its terms.
"""
import csv, io, json, logging, math, os, random, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError as e:
    raise SystemExit('Install requests and yfinance: pip install requests yfinance') from e

BASE = Path.cwd()  # Colab's /content, or current working directory
OUT = BASE / 'data' / 'orbital-market-universe.json'
CACHE = BASE / 'data' / 'orbital-company-metadata.json'
# Explicit official iShares URLs. Holdings may be sampled and differ from benchmark membership.
FUNDS = {
    'IWV': ('239714', 'ishares-russell-3000-etf'),
    'IWB': ('239707', 'ishares-russell-1000-etf'),
    'IWM': ('239710', 'ishares-russell-2000-etf'),
    'IEFA': ('244049', 'ishares-core-msci-eafe-etf'),
    'IEMG': ('244050', 'ishares-core-msci-emerging-markets-etf'),
    'EWC': ('239615', 'ishares-msci-canada-etf'),
}
LOG = logging.getLogger('orbital')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def fnum(value, default=None):
    try:
        x = float(str(value).replace(',', '').replace('$','').replace('%','').strip())
        return round(x, 8) if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def clean_ticker(raw):
    s = str(raw or '').strip().upper()
    if not s or s in {'-', 'N/A', 'CASH', 'USD'}: return ''
    # BlackRock represents class symbols with whitespace; Yahoo uses a dash.
    if s.count(' ') == 1 and len(s.split(' ')[1]) <= 2: s = s.replace(' ', '-')
    return s


def load_fund(session, fund):
    pid, slug = FUNDS[fund]
    hosts = ['https://www.blackrock.com/us/individual/products', 'https://www.ishares.com/us/products']
    errors = []
    for host in hosts:
        url = f'{host}/{pid}/{slug}/latest-holdings.csv'
        for attempt in range(3):
            try:
                res = session.get(url, timeout=55, headers={'Accept': 'text/csv, text/plain, */*'})
                res.raise_for_status()
                body = res.content.decode('utf-8-sig', 'replace')
                lines = body.splitlines()
                start = next((i for i, line in enumerate(lines) if line.startswith('Ticker,Name,') and ',Sector,Asset Class,' in line), None)
                if start is None: raise ValueError('Holdings header not found; possible remote format change or HTML response')
                asof = ''
                for line in lines[:start]:
                    if line.startswith('Fund Holdings as of,'):
                        fields = next(csv.reader([line])); asof = fields[1] if len(fields) > 1 else ''
                records = []
                for row in csv.DictReader(io.StringIO('\n'.join(lines[start:]))):
                    if row.get('Asset Class', '').strip() != 'Equity': continue
                    symbol = clean_ticker(row.get('Ticker'))
                    if not symbol or len(symbol) > 22: continue
                    w = fnum(row.get('Weight (%)'))
                    mv = fnum(row.get('Market Value'))
                    if mv is None or mv <= 0: continue
                    records.append({
                        'ticker':symbol,
                        'name':str(row.get('Name') or symbol).strip().title(),
                        'sector':str(row.get('Sector') or 'Unclassified').strip(),
                        'country':str(row.get('Location') or 'Unspecified').strip(),
                        'weight':w,
                        'holding_value':round(mv,2),
                        'exchange':str(row.get('Exchange') or '').strip(),
                    })
                if len(records) < (60 if fund == 'EWC' else 500):
                    raise ValueError(f'Suspicious count {len(records)}; refusing to publish incomplete holdings')
                LOG.info('%s: %s equity securities as of %s', fund, len(records), asof)
                return {'fund':fund,'asof':asof,'source_url':url,'records':records}
            except Exception as exc:
                errors.append(f'{url} attempt {attempt+1}: {exc}')
                time.sleep(min(2**attempt,4))
    raise RuntimeError('Failed to fetch '+fund+'; '+ ' | '.join(errors[-5:]))


def enrich_yfinance(rows, cache, max_queries=85):
    """Rate-limited, rotating metadata refresh. No invented data when unavailable.
    Quotes for every security are not practical via free, unauthenticated Yahoo APIs.
    Use the ticker cache to spread coverage over many daily runs.
    """
    if os.environ.get('ORBITAL_NO_YFINANCE') == '1': return cache, 0
    try:
        import yfinance as yf
    except ImportError:
        LOG.warning('yfinance unavailable; publishing ETF holdings only')
        return cache, 0
    tickers = list({r['ticker'] for r in rows if r['country'] == 'United States'})
    priority = sorted(rows, key=lambda r: r.get('holding_value') or 0, reverse=True)
    priority = [r['ticker'] for r in priority if r['country'] == 'United States']
    uncached = [t for t in dict.fromkeys(priority) if t not in cache or not cache[t].get('industry')]
    # Refresh a small fraction of cached records each day without busting rate limits.
    day = datetime.now(timezone.utc).timetuple().tm_yday
    dated = [t for t in tickers if t in cache and t not in uncached]
    rng = random.Random(day)
    rng.shuffle(dated)
    # Use the entire quota for as-yet unclassified companies first; reserve only
    # 10% for stale-cache refreshing once the cache is well populated.
    refresh_quota = min(len(dated), max(1, max_queries // 10)) if len(uncached) < max_queries else 0
    selection = (uncached[:max_queries-refresh_quota] + dated[:refresh_quota])[:max_queries]
    updated = 0
    for idx, ticker in enumerate(selection):
        try:
            info = yf.Ticker(ticker).get_info()
            if not isinstance(info, dict) or not info.get('symbol'): continue
            cache[ticker] = {
                'industry':str(info.get('industry') or '').strip()[:80],
                'sector':str(info.get('sector') or '').strip()[:80],
                'market_cap':fnum(info.get('marketCap')),
                'trailing_pe':fnum(info.get('trailingPE')),
                'forward_pe':fnum(info.get('forwardPE')),
                'currency':str(info.get('currency') or '')[:8],
                'updated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            }
            updated += 1
        except Exception as exc:
            if idx < 3: LOG.warning('Yahoo metadata unavailable for %s: %s', ticker, str(exc)[:150])
        if idx % 20 == 0: LOG.info('Yahoo enrichment: %s/%s processed', idx+1, len(selection))
        time.sleep(0.3)
    return cache, updated


def benchmark_returns():
    if os.environ.get('ORBITAL_NO_YFINANCE') == '1': return {}
    try:
        import yfinance as yf
        frames = yf.download(' '.join(FUNDS), period='15mo', interval='1d', auto_adjust=True, progress=False,
                             group_by='ticker', threads=2, timeout=20)
        result={}
        for fund in FUNDS:
            try:
                close = frames[fund]['Close'].dropna()
                if len(close)<3: continue
                now = float(close.iloc[-1]); year = int(close.index[-1].year)
                yseries = close[close.index.year < year]
                start_y = float(yseries.iloc[-1]) if len(yseries) else None
                result[fund]={
                    '1d':round(100*(now/float(close.iloc[-2])-1),3),
                    '1m':round(100*(now/float(close.iloc[-22 if len(close)>=22 else 0])-1),3) if len(close)>2 else None,
                    'ytd':round(100*(now/start_y-1),3) if start_y else None,
                    'asof':str(close.index[-1].date()),
                }
            except Exception: pass
        return result
    except Exception as exc:
        LOG.warning('Benchmark quotes failed (holdings still valid): %s', str(exc)[:200]); return {}


def leaf(row, ticker_cache, fund, returns):
    meta=ticker_cache.get(row['ticker'],{})
    return {
        'id':f"{fund}-{row['ticker']}",'name':row['name'], 'kind':'stock','ticker':row['ticker'],
        'sector':row['sector'],'industry':meta.get('industry') or None,
        'weight':row['weight'],'marketCap':meta.get('market_cap'),
        'pe':meta.get('trailing_pe'), 'country':row['country'],
        'exchange':row['exchange'], 'returns':None,
        'source':fund,
    }


def branch(id, name, kind, children, source=None, subtitle=None, returns=None):
    # Parent weights are source-local holdings weights; never add across ETF sources.
    weights=[x.get('weight') for x in children]
    total = round(sum(x for x in weights if isinstance(x,(float,int))),4) if source and all(x is not None for x in weights) else None
    return {'id':id, 'name':name, 'kind':kind, 'subtitle':subtitle, 'source':source,
            'weight':total, 'returns':returns, 'children':children}


def by_sector(rows, cache, source, idprefix):
    sectors=defaultdict(list)
    for r in rows: sectors[r['sector']].append(r)
    out=[]
    for sector, members in sorted(sectors.items(), key=lambda t:-sum(x.get('weight') or 0 for x in t[1])):
        industries=defaultdict(list)
        for r in members:
            industry=(cache.get(r['ticker']) or {}).get('industry') or 'Industry unclassified'
            industries[industry].append(r)
        indnodes=[]
        for ind, records in sorted(industries.items(),key=lambda t:-sum(x.get('weight') or 0 for x in t[1])):
            ls=[leaf(r, cache, source, None) for r in sorted(records,key=lambda x:-(x.get('weight') or 0))]
            indnodes.append(branch(f'{idprefix}/{sector}/{ind}',ind,'industry',ls,source=source))
        out.append(branch(f'{idprefix}/{sector}',sector,'sector',indnodes,source=source))
    return out


def build(holdings, cache, performance):
    iwv = holdings['IWV']['records']; iwb = holdings['IWB']['records']; iwm = holdings['IWM']['records']
    # Combine *distinct* Russell 1000 / Russell 2000 ETF security holdings to
    # broaden market coverage beyond IWV's sampling. Securities from each ETF
    # retain their own source-specific holding weights; never add these weights
    # across IWB, IWM and IWV as though they shared a denominator.
    big_tickers={r['ticker'] for r in iwb}
    small_tickers={r['ticker'] for r in iwm}
    russell_big=list(iwb)
    russell_small=[r for r in iwm if r['ticker'] not in big_tickers]
    union_tickers=big_tickers | small_tickers
    remainder=[r for r in iwv if r['ticker'] not in union_tickers]
    # Top-200 partition is ranked by ETF position market value only; this is
    # approximate and is NOT official FTSE Russell Top 200 membership.
    sorted_big=sorted(russell_big,key=lambda r:-(r.get('holding_value') or 0))
    top200, midrest=sorted_big[:200], sorted_big[200:]
    r1000=branch('r1000','Russell 1000 · IWB holdings proxy','segment',[
        branch('top200','Largest 200 · estimated','segment',by_sector(top200,cache,'IWB','top200'),source='IWB',subtitle='Holdings rank, not official Top 200 index membership'),
        branch('midcap','Remaining Russell 1000 proxy','segment',by_sector(midrest,cache,'IWB','midcap'),source='IWB',subtitle='Not official Russell Midcap membership'),
    ],source='IWB',returns=performance.get('IWB'),subtitle='IWB ETF holdings, not licensed Russell membership')
    r2000=branch('r2000','Russell 2000 · IWM holdings proxy','segment',by_sector(russell_small,cache,'IWM','r2000'),source='IWM',returns=performance.get('IWM'),subtitle='IWM ETF holdings; IWB overlap removed')
    ussegments=[r1000,r2000]
    if remainder:
        ussegments.append(branch('unmapped','Additional IWV-only holdings','segment',by_sector(remainder,cache,'IWV','unmapped'),source='IWV',subtitle='Not in IWB/IWM ETF overlap; not an extra Russell index'))
    r3000=branch('r3000','Russell 3000 · ETF coverage proxy','benchmark',ussegments,returns=performance.get('IWV'),subtitle='Unique IWB + IWM positions plus IWV-only extras; no official Russell membership or additive ETF weights')
    us=branch('us','United States','region',[r3000],returns=performance.get('IWV'))
    iefa=holdings['IEFA']['records']; ewc=holdings['EWC']['records']
    def countries(rows, fund):
        groups=defaultdict(list)
        for r in rows: groups[r['country']].append(r)
        return [branch(f'{fund}/{country}',country,'country',by_sector(rr,cache,fund,f'{fund}/{country}'),source=fund)
                for country,rr in sorted(groups.items(),key=lambda pair:-sum(z.get('weight') or 0 for z in pair[1]))]
    developed=branch('developed','Developed ex-U.S.','region',[
        branch('iefa','MSCI EAFE IMI · IEFA proxy','benchmark',countries(iefa,'IEFA'),source='IEFA',returns=performance.get('IEFA'),subtitle='Developed excluding U.S. and Canada'),
        branch('canada','Canada · EWC proxy','benchmark',countries(ewc,'EWC'),source='EWC',returns=performance.get('EWC'),subtitle='Country ETF, not full Canadian market'),
    ],subtitle='Developed exposures drawn from distinct ETF sources; weights cannot be summed')
    iemg=holdings['IEMG']['records']; emerging=branch('emerging','Emerging markets','region',[
        branch('iemg','MSCI EM IMI · IEMG proxy','benchmark',countries(iemg,'IEMG'),source='IEMG',returns=performance.get('IEMG'),subtitle='Broad EM ETF holdings; not an exhaustive EM market list')
    ],returns=performance.get('IEMG'))
    root=branch('global','Global equity universe','universe',[us,developed,emerging],subtitle='Three geographic realms. Sources not globally cap-weighted or directly additive.')
    # Report the actual classification coverage. A full holdings universe is NOT
    # the same as complete industry/valuation coverage.
    def collect(node):
        if node.get('kind')=='stock': return [node]
        result=[]
        for child in node.get('children',[]): result.extend(collect(child))
        return result
    stocks=collect(root)
    nindustry=sum(bool(x.get('industry')) for x in stocks)
    ncap=sum(isinstance(x.get('marketCap'),(int,float)) for x in stocks)
    nquote=sum(bool(x.get('returns')) for x in stocks)
    bysource={f:len(h['records']) for f,h in holdings.items()}
    quality={
        'mapped_security_nodes':len(stocks),
        'unique_security_ids':len({(x.get('source'),x.get('ticker')) for x in stocks}),
        'industry_classified_nodes':nindustry,
        'industry_coverage_pct':round(100*nindustry/max(1,len(stocks)),1),
        'market_cap_nodes':ncap,
        'market_cap_coverage_pct':round(100*ncap/max(1,len(stocks)),1),
        'stock_return_nodes':nquote,
        'etf_source_holdings':bysource,
        'official_index_membership':False,
        'warning':'Market holdings coverage does not imply complete industry, stock returns, fundamentals, or licensed index membership.',
    }
    return {
        'quality':quality,
        'schema':'orbital-v1','generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'mode':'holdings-proxy', 'root':root,
        'sources':{f:{'holdings':len(h['records']),'holdings_asof':h['asof'],'url':h['source_url']} for f,h in holdings.items()},
        'methodology':{
            'universe':'ETF portfolio holdings as proxies, not complete or licensed index membership.',
            'weights':'Percent of each source ETF. IWB, IWM and IWV have separate denominators. Do not combine across ETFs.',
            'size':'U.S. proxy built from unique IWB and IWM holdings plus IWV-only residual. Top 200 is estimated from IWB position-size rank.',
            'industry':'Yahoo Finance metadata for companies with successful, cached enrichment; otherwise Unclassified.',
            'returns':'Yahoo Finance adjusted ETF close returns; benchmarks reflect ETF proxies, not stock-level performance.',
            'coverage':'ETF holdings proxy with union/deduplication; funds may sample, have overlap, or differ from benchmarks. Not exhaustive.',
        }
    }


def main():
    OUT.parent.mkdir(parents=True,exist_ok=True)
    session=requests.Session()
    session.headers.update({'User-Agent':'Mozilla/5.0 (compatible; ORBITAL-Research/1.0; holdings analysis)'})
    holdings={}
    for fund in FUNDS:
        holdings[fund]=load_fund(session,fund)
    cache={}
    if CACHE.exists():
        try: cache=json.loads(CACHE.read_text(encoding='utf-8'))
        except Exception: LOG.warning('Ignoring damaged metadata cache')
    # A fresh Colab runtime otherwise starts from zero industry mappings every run.
    # Reuse previously published, non-secret Yahoo metadata when no local cache exists.
    # Failure is harmless; the stock universe still builds from the current holdings.
    if not cache:
        try:
            url='https://raw.githubusercontent.com/denism2013/market-dex-data/main/data/orbital-company-metadata.json'
            response=session.get(url, timeout=12)
            response.raise_for_status()
            published=response.json()
            if isinstance(published,dict):
                cache={str(k):v for k,v in published.items() if isinstance(v,dict)}
                LOG.info('Reused %d previously published company metadata records',len(cache))
        except Exception as exc:
            LOG.warning('Published metadata unavailable; rebuilding gradually (%s)',str(exc)[:120])
    us_enrichment = holdings['IWB']['records'] + holdings['IWM']['records'] + holdings['IWV']['records']
    cache, n=enrich_yfinance(us_enrichment,cache,int(os.environ.get('ORBITAL_YF_MAX','85')))
    performance=benchmark_returns()
    data=build(holdings,cache,performance)
    count=sum(len(holdings[t]['records']) for t in ('IWV','IEFA','IEMG','EWC'))
    if count<4000: raise RuntimeError(f'Refusing incomplete combined dataset ({count} holdings)')
    OUT.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    CACHE.write_text(json.dumps(cache,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    LOG.info('Wrote %s (%s bytes), approx %s holdings across non-overlapping sources, %s Yahoo updates',OUT,OUT.stat().st_size,count,n)


if __name__ == "__main__":
    main()
