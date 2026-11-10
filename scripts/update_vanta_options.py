#!/usr/bin/env python3
"""VANTA free Yahoo options snapshot: attempts the entire published Russell 3000 proxy.
Partial coverage is explicitly reported; no premium data, dealer direction, or licensed flow.
"""
import concurrent.futures as futures
import datetime as dt
import json
import math
import os
import re
import time
from pathlib import Path
import yfinance as yf
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"data"/"options"
SOURCE=ROOT/"data"/"universe.json"
WORKERS=max(1,min(8,int(os.getenv("VANTA_WORKERS","4"))))
MAX_MINUTES=max(5,int(os.getenv("VANTA_MAX_MINUTES","120")))
EXPS=max(1,min(3,int(os.getenv("VANTA_EXPIRIES","2"))))
VALID=re.compile(r"^[A-Z0-9.^_-]{1,24}$")
def finite(x):
    try:
        f=float(x)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError,OverflowError): return None
def iso(): return dt.datetime.now(dt.timezone.utc).isoformat()
def ticker_name(s): return s.replace(".","-").replace("/","-")
def per_contract(row,kind,expiry,spot):
    strike=finite(row.get("strike"));vol=finite(row.get("volume")) or 0
    oi=finite(row.get("openInterest")) or 0
    bid=finite(row.get("bid"));ask=finite(row.get("ask"))
    iv=finite(row.get("impliedVolatility"))
    good=bool(strike and strike>0 and bid is not None and ask is not None and bid>0 and ask>=bid and iv and .01<iv<4)
    mid=(bid+ask)/2 if good else None
    spread=(ask-bid)/mid if good and mid else None
    return dict(type=kind,expiration=expiry,strike=strike,volume=int(vol),openInterest=int(oi),
        bid=bid,ask=ask,impliedVolatility=iv,quote_ok=good,rel_spread=spread,
        vol_oi=vol/oi if oi>0 else 0,
        premium_notional_proxy=mid*vol*100 if mid else 0,
        signal="Volume / OI anomaly" if oi>=25 and vol/oi>=2 and vol>=100 else "No screen flag")
def scan(sym):
    fetched=iso()
    try:
        t=yf.Ticker(ticker_name(sym))
        expiry_list=list(t.options or [])[:EXPS]
        if not expiry_list: return sym,{"ticker":sym,"status":"no_listed_options","fetched_utc":fetched},None
        spot=None
        try:
            hist=t.history(period="5d",auto_adjust=True)
            if not hist.empty: spot=finite(hist["Close"].iloc[-1])
        except Exception: pass
        contracts=[];summary=[];bad_exp=[]
        for expiry in expiry_list:
            try:
                chain=t.option_chain(expiry)
                local=[]
                for typ,df in (("Call",chain.calls),("Put",chain.puts)):
                    if df is None or df.empty: continue
                    for _,r in df.iterrows():
                        strike=finite(r.get("strike"))
                        if spot and strike and not (.55*spot<=strike<=1.45*spot): continue
                        local.append(per_contract(r,typ,expiry,spot))
                contracts.extend(local)
                calls=[x for x in local if x["type"]=="Call"];puts=[x for x in local if x["type"]=="Put"]
                atm=min(calls,key=lambda r:abs(r["strike"]-spot),default=None) if spot else None
                atmp=min(puts,key=lambda r:abs(r["strike"]-spot),default=None) if spot else None
                straddle=None
                if atm and atmp and atm["quote_ok"] and atmp["quote_ok"]:
                    straddle=(atm["bid"]+atm["ask"]+atmp["bid"]+atmp["ask"])/2
                try: dte=max(1,(dt.date.fromisoformat(expiry)-dt.datetime.now(dt.timezone.utc).date()).days)
                except ValueError: dte=None
                oi_calls=sum(r["openInterest"] for r in calls);oi_puts=sum(r["openInterest"] for r in puts)
                vol_calls=sum(r["volume"] for r in calls);vol_puts=sum(r["volume"] for r in puts)
                summary.append(dict(expiration=expiry,dte=dte,atm_strike=atm["strike"] if atm else None,
                    straddle_mid=straddle,approx_1sigma_move_dollars=(straddle/0.8 if straddle is not None else None),
                    approx_1sigma_move_pct=(100*straddle/0.8/spot if straddle is not None and spot else None),
                    rv20_1sigma_move_dollars=None,implied_vs_rv20_move_ratio=None,max_pain=None,
                    put_call_volume_ratio=vol_puts/vol_calls if vol_calls else None,
                    put_call_oi_ratio=oi_puts/oi_calls if oi_calls else None))
            except Exception as exc: bad_exp.append(expiry+":"+str(exc)[:90])
        if not contracts: return sym,{"ticker":sym,"status":"no_usable_chains","fetched_utc":fetched,"errors":bad_exp},None
        alerts=[r for r in contracts if r["quote_ok"] and r["openInterest"]>=25 and r["volume"]>=100 and r["vol_oi"]>=2]
        alerts.sort(key=lambda r:r["vol_oi"],reverse=True)
        payload={"metadata":{"ticker":sym,"run_utc":fetched,"spot_daily_close":spot,
           "source":"yfinance/Yahoo public options snapshots; no licensed feed",
           "coverage":"first "+str(EXPS)+" listed expirations; strikes within 55%-145% of spot",
           "limitations":"Volume/OI is a screening proxy, not proof of opening trades, direction, unusual institutional flow, or observed dealer inventory."},
          "contracts":contracts,"alerts":alerts[:150],"summary":summary,"gamma":[]}
        return sym,{"ticker":sym,"status":"covered","fetched_utc":fetched,"expiries":len(summary),
          "contracts":len(contracts),"alerts":len(alerts),"spot":spot,"errors":bad_exp},payload
    except Exception as exc: return sym,{"ticker":sym,"status":"fetch_error","fetched_utc":fetched,"error":str(exc)[:160]},None

def main():
    source=json.loads(SOURCE.read_text())
    members=source.get("members",[])
    symbols=sorted({str(x.get("symbol","")).strip().upper() for x in members if isinstance(x,dict)})
    symbols=[x for x in symbols if VALID.fullmatch(x)]
    if len(symbols)<2000: raise RuntimeError("Refusing to substitute a small ticker list for full Russell 3000 proxy.")
    DATA.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();deadline=start+MAX_MINUTES*60
    results={};covered=0
    # Every member is queued; requests are bounded and a missed deadline is explicitly reported.
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending={}
        it=iter(symbols)
        def fill():
            while len(pending)<WORKERS*2 and time.monotonic()<deadline:
                sym=next(it,None)
                if sym is None: break
                pending[pool.submit(scan,sym)]=sym
        fill()
        while pending:
            done,_=futures.wait(pending,timeout=20,return_when=futures.FIRST_COMPLETED)
            if not done and time.monotonic()>=deadline: break
            for task in done:
                sym=pending.pop(task)
                try: _,meta,payload=task.result()
                except Exception as exc: meta={"ticker":sym,"status":"worker_error","error":str(exc)[:160]};payload=None
                results[sym]=meta
                if payload:
                    covered+=1
                    (DATA/(re.sub("[^A-Z0-9_-]","_",sym)+".json")).write_text(json.dumps(payload,separators=(",",":"),allow_nan=False))
            fill()
            if len(results)%100==0 and results: print("VANTA progress",len(results),"/",len(symbols),"covered",covered,flush=True)
            if time.monotonic()>=deadline: break
        for f in pending: f.cancel()
    for s in symbols:
        if s not in results: results[s]={"ticker":s,"status":"not_scanned_time_budget"}
    statuses={}
    for r in results.values(): statuses[r["status"]]=statuses.get(r["status"],0)+1
    generated=iso()
    index={"schema":"vanta-free-options-universe-v1","generated_at_utc":generated,
      "universe_source":source.get("source"),"holdings_as_of":source.get("holdings_as_of"),
      "universe_size":len(symbols),"attempted":len(symbols)-statuses.get("not_scanned_time_budget",0),
      "covered":covered,"statuses":statuses,
      "complete":len(results)==len(symbols) and statuses.get("not_scanned_time_budget",0)==0 and statuses.get("fetch_error",0)==0,
      "note":"Public Yahoo option-chain snapshots are incomplete and rate-limited. Each covered ticker contains only selected expirations and near-money strikes. No dealer gamma sign or trade-side inference.",
      "tickers":[results[s] for s in symbols]}
    (DATA/"universe.json").write_text(json.dumps(index,separators=(",",":"),allow_nan=False))
    print("VANTA FREE OPTIONS COVERAGE",json.dumps({k:index[k] for k in ["universe_size","attempted","covered","complete","statuses"]}),flush=True)
    if not covered: print("WARNING: Zero valid chains; publishing manifest with honest zero coverage.",flush=True)
if __name__=="__main__":main()
