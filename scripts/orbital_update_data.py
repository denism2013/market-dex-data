#!/usr/bin/env python3
"""ORBITAL GLOBAL ATLAS: source-traceable world equity market mapping from ETF holdings + optional Yahoo metadata.

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
                        'fund_industry':str(row.get('Industry') or row.get('Sub-Industry') or '').strip()[:90],
                    })
                if len(records) < (60 if fund == 'EWC' else 500):
                    raise ValueError(f'Suspicious count {len(records)}; refusing to publish incomplete holdings')
                LOG.info('%s: %s equity securities as of %s', fund, len(records), asof)
                return {'fund':fund,'asof':asof,'source_url':url,'records':records}
            except Exception as exc:
                errors.append(f'{url} attempt {attempt+1}: {exc}')
                time.sleep(min(2**attempt,4))
    raise RuntimeError('Failed to fetch '+fund+'; '+ ' | '.join(errors[-5:]))


def cache_key(row):
    """Avoid false metadata joins when international tickers coincide with US tickers."""
    return row['ticker'] if row['country']=='United States' else row['country']+'|'+row['ticker']


def yahoo_symbol(row):
    """Exchange-qualified symbols only where exchange notation is unambiguous.

    Reject unknown international symbols instead of silently returning a US namesake.
    Yahoo symbols and sector labels are not a licensed global security master.
    """
    symbol=row['ticker']
    if row['country']=='United States': return symbol
    exchange=(row.get('exchange') or '').lower()
    pairs=[
      (('tokyo','japan'),'.T'),(('london','united kingdom'),'.L'),
      (('xetra','germany'),'.DE'),(('frankfurt',),'.F'),
      (('paris',),'.PA'),(('amsterdam',),'.AS'),(('milan','borsa italiana'),'.MI'),
      (('madrid',),'.MC'),(('zurich','six swiss'),'.SW'),
      (('hong kong',),'.HK'),(('shanghai',),'.SS'),(('shenzhen',),'.SZ'),
      (('national stock exchange of india',),'.NS'),(('bombay',),'.BO'),
      (('taiwan stock',),'.TW'),(('taipei',),'.TWO'),
      (('korea exchange',),'.KS'),(('kosdaq',),'.KQ'),
      (('australian',),'.AX'),(('toronto',),'.TO'),(('tsx venture',),'.V'),
      (('bovespa','sao paulo'),'.SA'),(('johannesburg',),'.JO'),
      (('singapore',),'.SI'),(('oslo',),'.OL'),(('stockholm',),'.ST'),
      (('copenhagen',),'.CO'),(('helsinki',),'.HE'),(('brussels',),'.BR'),
      (('mexico',),'.MX'),(('new zealand',),'.NZ'),(('tel aviv',),'.TA'),
      (('istanbul',),'.IS'),(('warsaw',),'.WA'),(('vienna',),'.VI'),
    ]
    for labels,suffix in pairs:
        if any(token in exchange for token in labels):return symbol+suffix
    return None


def enrich_yfinance(rows, cache, max_queries=85):
    """Across-region, cached best-effort metadata; no assumption of full coverage."""
    if os.environ.get('ORBITAL_NO_YFINANCE') == '1': return cache,0
    try: import yfinance as yf
    except ImportError:
        LOG.warning('yfinance unavailable; ETF holdings only');return cache,0
    by_realm={'us':[], 'developed':[], 'emerging':[]}
    seen=set()
    for row in sorted(rows,key=lambda r:-(r.get('holding_value') or 0)):
        key=cache_key(row)
        if key in seen or (cache.get(key) or {}).get('industry'):continue
        seen.add(key)
        yahoo=yahoo_symbol(row)
        if not yahoo:continue
        bucket='us' if row['country']=='United States' else ('emerging' if row.get('_fund')=='IEMG' else 'developed')
        by_realm[bucket].append((key,yahoo,row['country']))
    selected=[]
    # Interleave market realms so a US mega-cap never consumes the global query budget.
    while len(selected)<max_queries and any(by_realm.values()):
        for realm in ('us','developed','emerging'):
            if len(selected)>=max_queries:break
            if by_realm[realm]:selected.append(by_realm[realm].pop(0))
    updated=0
    for idx,(key,symbol,country) in enumerate(selected):
        try:
            info=yf.Ticker(symbol).get_info()
            if not isinstance(info,dict) or not info.get('symbol'):continue
            cache[key]={
                'symbol':symbol,'country':country,
                'industry':str(info.get('industry') or '').strip()[:90],
                'sector':str(info.get('sector') or '').strip()[:90],
                'market_cap':fnum(info.get('marketCap')),
                'trailing_pe':fnum(info.get('trailingPE')),
                'forward_pe':fnum(info.get('forwardPE')),
                'currency':str(info.get('currency') or '')[:8],
                'updated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            }
            updated+=1
        except Exception as exc:
            if idx<3:LOG.warning('Yahoo unavailable for %s: %s',symbol,str(exc)[:130])
        if idx%20==0:LOG.info('Global Yahoo enrichment %s/%s processed',idx+1,len(selected))
        time.sleep(.25)
    return cache,updated


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
    meta=ticker_cache.get(cache_key(row),{})
    return {
        'id':f"{fund}-{row['ticker']}",'name':row['name'], 'kind':'stock','ticker':row['ticker'],
        'sector':row['sector'],'industry':meta.get('industry') or row.get('fund_industry') or None,
        'weight':row['weight'],'marketCap':meta.get('market_cap'),
        'pe':meta.get('trailing_pe'), 'marketCapCurrency':meta.get('currency'), 'country':row['country'],
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
            industry=(cache.get(cache_key(r)) or {}).get('industry') or r.get('fund_industry') or 'Industry unclassified'
            industries[industry].append(r)
        indnodes=[]
        for ind, records in sorted(industries.items(),key=lambda t:-sum(x.get('weight') or 0 for x in t[1])):
            ls=[leaf(r, cache, source, None) for r in sorted(records,key=lambda x:-(x.get('weight') or 0))]
            indnodes.append(branch(f'{idprefix}/{sector}/{ind}',ind,'industry',ls,source=source))
        out.append(branch(f'{idprefix}/{sector}',sector,'sector',indnodes,source=source))
    return out


# Geographic groupings represent navigation only, not index provider market classifications.
# Developed/emerging realm memberships come directly from IEFA/EWC/IEMG fund mandates.
REGION_COUNTRIES = {
  'Europe': {'United Kingdom','UK','France','Germany','Italy','Spain','Netherlands','Switzerland','Sweden','Denmark','Norway','Finland','Belgium','Austria','Ireland','Portugal','Poland','Greece','Czech Republic','Czechia','Hungary','Romania','Turkey','Türkiye','Iceland','Luxembourg'},
  'Asia-Pacific': {'Japan','Australia','New Zealand','Hong Kong','Singapore','South Korea','Korea (South)','Taiwan','China','India','Indonesia','Malaysia','Philippines','Thailand','Vietnam','Pakistan','Sri Lanka'},
  'North America': {'Canada','United States','Mexico'},
  'Latin America': {'Brazil','Chile','Colombia','Peru','Argentina'},
  'Middle East & Africa': {'Saudi Arabia','South Africa','United Arab Emirates','UAE','Qatar','Kuwait','Israel','Egypt','Morocco','Nigeria','Bahrain','Oman','Jordan'},
}
COUNTRY_INDEX_CONTEXT={
 'Japan':'TOPIX / Nikkei 225','United Kingdom':'FTSE 100 / FTSE All-Share',
 'Germany':'DAX / CDAX','France':'CAC 40 / SBF 120','Canada':'S&P/TSX Composite',
 'Australia':'S&P/ASX 200','India':'Nifty 50 / Nifty 500','China':'CSI 300 / MSCI China',
 'Taiwan':'TAIEX','South Korea':'KOSPI / KOSDAQ','Korea (South)':'KOSPI / KOSDAQ',
 'Brazil':'Ibovespa','Mexico':'S&P/BMV IPC','Switzerland':'SMI / SPI',
 'Netherlands':'AEX','Spain':'IBEX 35','Italy':'FTSE MIB',
 'South Africa':'FTSE/JSE All Share','Saudi Arabia':'Tadawul All Share',
 'Singapore':'Straits Times','Hong Kong':'Hang Seng',
}

def geography(country):
    for label,countries in REGION_COUNTRIES.items():
        if country in countries:return label
    return 'Other / not mapped'


def weight_tiers(rows,cache,source,idprefix):
    """Country-relative ETF portfolio weight ranks; NEVER label as actual cap sizes."""
    rows=sorted(rows,key=lambda r:-(r.get('weight') or 0))
    n=len(rows)
    if n<=5:
        specs=[('constituents','Country constituents · ETF proxy',rows)]
    else:
        h=max(1,int(math.ceil(n*.15)))
        m=max(h+1,int(math.ceil(n*.50)))
        specs=[('leaders','Largest allocations · top 15%',rows[:h]),
               ('core','Core allocations · next 35%',rows[h:m]),
               ('tail','Smaller allocations · remaining 50%',rows[m:])]
    return [branch(idprefix+'/'+key,title,'segment',by_sector(members,cache,source,idprefix+'/'+key),
                   source=source,subtitle='Relative position ranks WITHIN '+source+' ETF holdings for this country; NOT company market-cap classifications')
            for key,title,members in specs if members]


def build(holdings, cache, performance):
    # One authoritative place for each stock within a realm. Never sum fund-level
    # portfolio weights from distinct source ETFs into a fictional global weight.
    iwv=holdings['IWV']['records'];iwb=holdings['IWB']['records'];iwm=holdings['IWM']['records']
    big={r['ticker'] for r in iwb};small=[r for r in iwm if r['ticker'] not in big]
    union=big|{r['ticker'] for r in small}
    extra=[r for r in iwv if r['ticker'] not in union]
    large=branch('us/r1000','Russell 1000 · IWB proxy','benchmark',by_sector(iwb,cache,'IWB','us/r1000'),
                 source='IWB',returns=performance.get('IWB'),subtitle='IWB ETF portfolio proxy for the Russell 1000; exact index membership is not claimed')
    smallnode=branch('us/r2000','Russell 2000 · IWM proxy','benchmark',by_sector(small,cache,'IWM','us/r2000'),
                 source='IWM',returns=performance.get('IWM'),subtitle='IWM ETF holdings excluding tickers also in IWB; not the official Russell membership list')
    uchildren=[large,smallnode]
    if extra:
        uchildren.append(branch('us/iwv-only','Additional IWV-only names','benchmark',by_sector(extra,cache,'IWV','us/iwv-only'),source='IWV',subtitle='IWV positions absent from IWB and IWM; excludes duplicates'))
    us=branch('us','United States','galaxy',[
        branch('us/r3000','Russell 3000 · ETF coverage','market',uchildren,returns=performance.get('IWV'),
               subtitle='U.S. broad market: IWB + IWM unique holdings, with additional IWV-only names; source weights not additive')
    ],returns=performance.get('IWV'),subtitle='One U.S. equity galaxy within the global universe, not the global organizing framework')

    # Developed & emerging realms are assembled from the market-specific holdings
    # of each fund. Region grouping is geography, while fund classification is MSCI.
    def make_regions(items,realm):
        # items: (source,record); each country sourced from one canonical ETF here.
        buckets=defaultdict(lambda:defaultdict(list))
        for fund,row in items:buckets[geography(row['country'])][(row['country'],fund)].append(row)
        regions=[]
        for rname,group in buckets.items():
            countries=[]
            for (country,fund),rows in sorted(group.items(),key=lambda a:-len(a[1])):
                index_ref=COUNTRY_INDEX_CONTEXT.get(country)
                subtitle=f'{fund} ETF constituents; market benchmark'+(' references (context only): '+index_ref if index_ref else ' unavailable')+'. No claim of national-index membership.'
                country_id=realm+'/'+rname+'/'+country
                countries.append(branch(country_id,country,'country',weight_tiers(rows,cache,fund,country_id),
                                        source=fund,subtitle=subtitle))
            regions.append(branch(realm+'/'+rname,rname,'continent',countries,
                       subtitle='Geographic grouping of countries in the '+realm+' ETF proxy universe; weights across fund sources are not additive'))
        return sorted(regions,key=lambda x:-sum(sum(1 for z in c.get('children',[])) for c in x['children']))

    dmrows=[('IEFA',row) for row in holdings['IEFA']['records']]+[('EWC',row) for row in holdings['EWC']['records']]
    dev=branch('developed','Developed international','galaxy',make_regions(dmrows,'developed'),
               subtitle='MSCI EAFE IMI (IEFA) + Canada (EWC) country exposures; excludes the United States. These are ETF holdings proxies, not globally additive weights.')
    emrows=[('IEMG',row) for row in holdings['IEMG']['records']]
    emerging=branch('emerging','Emerging markets','galaxy',make_regions(emrows,'emerging'),
                    returns=performance.get('IEMG'),subtitle='MSCI EM IMI exposures in IEMG; MSCI market categories (not FTSE Russell classifications)')
    frontier=branch('frontier','Frontier & uncovered markets','gap',[],
           subtitle='Not mapped: current free ETF inputs do not provide trustworthy frontier-market constituent coverage. No fictitious securities are displayed.')
    root=branch('global','Global Equity Universe','universe',[us,dev,emerging,frontier],
                subtitle='Worldwide equity map: U.S., developed international, emerging, plus frontier coverage gaps. Source-specific holdings are NOT worldwide market-cap weights.')
    def collect(node):
        if node.get('kind')=='stock':return [node]
        out=[]
        for child in node.get('children',[]):out.extend(collect(child))
        return out
    stocks=collect(root)
    n_industry=sum(bool(x.get('industry')) for x in stocks)
    n_cap=sum(isinstance(x.get('marketCap'),(int,float)) for x in stocks)
    countries={x.get('country') for x in stocks if x.get('country')}
    realm_counts={n['id']:len(collect(n)) for n in root['children']}
    quality={
      'mapped_security_nodes':len(stocks),
      'unique_security_ids':len({(x.get('country'),x.get('ticker')) for x in stocks}),
      'country_count':len(countries),
      'industry_classified_nodes':n_industry,
      'industry_coverage_pct':round(100*n_industry/max(1,len(stocks)),1),
      'market_cap_nodes':n_cap,
      'market_cap_coverage_pct':round(100*n_cap/max(1,len(stocks)),1),
      'stock_return_nodes':0,
      'realm_security_counts':realm_counts,
      'etf_source_holdings':{f:len(h['records']) for f,h in holdings.items()},
      'official_index_membership':False,
      'frontier_constituent_coverage':False,
      'warning':'ETF holdings are a sampled investable proxy, not the total worldwide listed-equity universe. Country tiers are ETF allocation ranks, not issuer market-cap segments.',
    }
    return {
      'schema':'orbital-v1','mode':'holdings-proxy','generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
      'root':root,'quality':quality,
      'sources':{f:{'holdings':len(h['records']),'holdings_asof':h['asof'],'url':h['source_url']} for f,h in holdings.items()},
      'methodology':{
       'world':'The three investable realms are derived from IWB, IWM, IWV, IEFA, EWC, IEMG ETF portfolio holdings. Frontier is an explicit data gap. Not the universe of all listed companies.',
       'classification':'Developed versus emerging uses the ETF/MSCI mandates; geographic region is a display grouping. Classification can differ from FTSE Russell.',
       'country':'Countries use holdings Location; not country of revenue, listing or issuer domicile necessarily.',
       'size':'Country holding tiers are rank percentiles of allocation within one ETF and one country; NOT large/mid/small capitalizations. Where Yahoo market capitalization exists it is displayed on the security only.',
       'weights':'Source ETF weight denominator only. No composite global weighting or cross-fund sum.',
       'sector':'Sector reported by iShares holdings, not necessarily full current GICS industry hierarchy.',
       'industry':'Optional Yahoo industry (not official GICS subindustry) or issuer-provided industry; unclassified names retained.',
       'returns':'ETF adjusted-close returns where Yahoo Finance data succeeds; not individual-stock returns.',
       'coverage':'Broad but incomplete global coverage. Some frontier markets and uninvestable/micro-cap listings excluded; Yahoo access may be rate limited.',
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
    global_enrichment=[]
    for fund in ('IWB','IWM','IWV','IEFA','IEMG','EWC'):
        for record in holdings[fund]['records']:
            global_enrichment.append({**record,'_fund':fund})
    cache, n=enrich_yfinance(global_enrichment,cache,int(os.environ.get('ORBITAL_YF_MAX','85')))
    performance=benchmark_returns()
    data=build(holdings,cache,performance)
    count=data['quality']['mapped_security_nodes']
    if count<4000 or data['quality']['country_count']<25: raise RuntimeError(f'Refusing incomplete global snapshot ({count} positions, {data["quality"]["country_count"]} countries)')
    OUT.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    CACHE.write_text(json.dumps(cache,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    LOG.info('Wrote %s (%s bytes), %s mapped equity positions across global realms, %s Yahoo updates',OUT,OUT.stat().st_size,count,n)





if __name__ == "__main__":
    main()
