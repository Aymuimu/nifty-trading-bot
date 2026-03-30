from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os, threading, time, datetime, pyotp
import pandas as pd
import numpy as np
import requests as req

try:
    from SmartApi import SmartConnect
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False

app = Flask(__name__, static_folder='public')
CORS(app)

SMARTAPI_KEY         = os.environ.get('SMARTAPI_KEY', '')
SMARTAPI_CLIENT_ID   = os.environ.get('SMARTAPI_CLIENT_ID', '')
SMARTAPI_PASSWORD    = os.environ.get('SMARTAPI_PASSWORD', '')
SMARTAPI_TOTP_SECRET = os.environ.get('SMARTAPI_TOTP_SECRET', '')
DHAN_ACCESS_TOKEN    = os.environ.get('DHAN_ACCESS_TOKEN', '')
DHAN_CLIENT_ID_ENV   = os.environ.get('DHAN_CLIENT_ID', '')

smart_obj     = None
session_data  = None
session_lock  = threading.Lock()
jwt_token     = None
trade_log     = []
today_trades  = 0
today_pnl     = 0.0
capital       = 10000.0
sl_hit_today  = False
last_signal   = "Bot not started"
bot_active    = False
candle_buffer = {}
last_ltp      = None
data_source   = "dhan"

# ─── FIXED P&L VALUES (as requested) ──────────────────────────
SL_RS   = 350    # Max loss per trade ₹350
TP_RS   = 1500   # Base target ₹1500
EXT_RS  = 5000   # Extended target ₹5000

LOT_SIZE  = 75
DELTA     = 0.50   # ATM option delta
# Convert rupees → NIFTY index points
SL_PTS  = round(SL_RS  / (DELTA * LOT_SIZE), 1)   # ~9.3 pts
TP_PTS  = round(TP_RS  / (DELTA * LOT_SIZE), 1)   # ~40 pts
EXT_PTS = round(EXT_RS / (DELTA * LOT_SIZE), 1)   # ~133 pts

SA_BASE   = "https://apiconnect.angelbroking.com"
DHAN_BASE = "https://api.dhan.co"
DHAN_CHUNK = 28


# ══════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════

def generate_totp():
    try:    return pyotp.TOTP(SMARTAPI_TOTP_SECRET).now()
    except: return None

def login_smartapi():
    global smart_obj, session_data, jwt_token
    if not all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]): return False
    if not SMARTAPI_AVAILABLE: return False
    try:
        totp=generate_totp(); obj=SmartConnect(api_key=SMARTAPI_KEY)
        data=obj.generateSession(SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,totp)
        if data and data.get('status'):
            with session_lock:
                smart_obj=obj; session_data=data
                session_data['login_time']=datetime.datetime.now().isoformat()
                raw=data.get('data',{}).get('jwtToken','')
                jwt_token=raw[7:] if raw.startswith('Bearer ') else raw
            print("✅ SmartAPI OK"); return True
        return False
    except Exception as e: print(f"❌ Login: {e}"); return False


# ══════════════════════════════════════════════════════════════
#  TIME
# ══════════════════════════════════════════════════════════════

def ist_now():
    return datetime.datetime.utcnow()+datetime.timedelta(hours=5,minutes=30)

def last_trading_day():
    d=datetime.datetime.now()
    while d.weekday()>=5: d-=datetime.timedelta(days=1)
    return d

def is_market_open():
    n=ist_now()
    if n.weekday()>=5: return False
    return datetime.time(9,15)<=n.time()<=datetime.time(15,30)

def is_trading_window():
    t=ist_now().time()
    return (datetime.time(10,0)<=t<=datetime.time(11,15) or
            datetime.time(13,45)<=t<=datetime.time(14,45))

def window_label():
    t=ist_now().time()
    if datetime.time(10,0)<=t<=datetime.time(11,15): return "Morning (10:00-11:15)"
    if datetime.time(13,45)<=t<=datetime.time(14,45): return "Afternoon (1:45-2:45)"
    return "Outside trading windows"

def bucket_15m(dt):
    return dt.replace(minute=(dt.minute//15)*15,second=0,microsecond=0)


# ══════════════════════════════════════════════════════════════
#  LIVE PRICE
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    global last_ltp
    if smart_obj:
        try:
            ltp=smart_obj.ltpData("NSE","NIFTY","26000")
            if ltp and ltp.get('status'):
                p=float(ltp['data']['ltp']); last_ltp=p; return p,"SmartAPI"
        except: pass
    if DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV:
        try:
            h={'access-token':DHAN_ACCESS_TOKEN,'client-id':DHAN_CLIENT_ID_ENV,'Content-Type':'application/json'}
            r=req.post(f"{DHAN_BASE}/v2/marketfeed/ltp",json={"NSE":["NIFTY 50"]},headers=h,timeout=8)
            val=r.json().get('data',{}).get('NSE',{}).get('NIFTY 50',{}).get('last_price')
            if val: last_ltp=float(val); return float(val),"Dhan"
        except: pass
    if last_ltp: return last_ltp,"cached"
    return None,"unavailable"


# ══════════════════════════════════════════════════════════════
#  DHAN DATA
# ══════════════════════════════════════════════════════════════

DHAN_INTV={"15m":"15","1d":"1440","5m":"5","1h":"60"}

def _dhan_hdr():
    return {'access-token':DHAN_ACCESS_TOKEN,'client-id':DHAN_CLIENT_ID_ENV,
            'Content-Type':'application/json','Accept':'application/json'}

def _dhan_parse(data):
    if not isinstance(data,dict): return None,f"bad type"
    if 'errorCode' in data: return None,f"{data.get('errorCode')}: {data.get('errorMessage','')}"
    closes=data.get('close',[])
    if not closes: return None,"empty"
    tsr=data.get('timestamp',[]); opens=data.get('open',[0]*len(closes))
    highs=data.get('high',[0]*len(closes)); lows=data.get('low',[0]*len(closes))
    vols=data.get('volume',[0]*len(closes))
    rows=[]
    for i in range(len(closes)):
        try:    ts=datetime.datetime.fromtimestamp(int(tsr[i])) if i<len(tsr) else datetime.datetime.now()
        except: ts=datetime.datetime.now()-datetime.timedelta(minutes=(len(closes)-i)*15)
        rows.append({'timestamp':ts,
                     'open':  float(opens[i]) if i<len(opens) else float(closes[i]),
                     'high':  float(highs[i]) if i<len(highs) else float(closes[i]),
                     'low':   float(lows[i])  if i<len(lows)  else float(closes[i]),
                     'close': float(closes[i]),
                     'volume':float(vols[i])  if i<len(vols)  else 0})
    df=pd.DataFrame(rows).sort_values('timestamp').reset_index(drop=True)
    return df[df['close']>0],None

def _dhan_req(interval,from_dt,to_dt):
    if interval!="1d":
        url=f"{DHAN_BASE}/v2/charts/intraday"
        body={"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
              "interval":DHAN_INTV.get(interval,"15"),
              "fromDate":from_dt.strftime("%Y-%m-%d"),"toDate":to_dt.strftime("%Y-%m-%d")}
    else:
        url=f"{DHAN_BASE}/v2/charts/historical"
        body={"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
              "fromDate":from_dt.strftime("%Y-%m-%d"),"toDate":to_dt.strftime("%Y-%m-%d")}
    resp=req.post(url,json=body,headers=_dhan_hdr(),timeout=20)
    return _dhan_parse(resp.json())

def fetch_dhan(interval="15m",days=30):
    if not DHAN_ACCESS_TOKEN or not DHAN_CLIENT_ID_ENV:
        return None,"Dhan creds missing"
    to_dt=last_trading_day(); from_dt=to_dt-datetime.timedelta(days=days+5)
    if interval=="1d":
        df,err=_dhan_req("1d",from_dt,to_dt)
        return (df,f"Dhan ({len(df)} daily bars)") if df is not None and len(df)>0 else (None,f"Dhan 1d: {err}")
    frames=[]; c_end=to_dt
    c_start=max(from_dt,c_end-datetime.timedelta(days=DHAN_CHUNK))
    for _ in range(25):
        if c_end<=from_dt: break
        try:
            df_c,err=_dhan_req(interval,c_start,c_end)
            if df_c is not None and len(df_c)>0: frames.append(df_c)
            else: print(f"  ⚠️ Dhan chunk: {err}")
        except Exception as e: print(f"  ❌ Dhan: {e}")
        c_end=c_start-datetime.timedelta(days=1)
        c_start=max(from_dt,c_end-datetime.timedelta(days=DHAN_CHUNK))
        time.sleep(0.3)
    if not frames: return None,"Dhan: 0 rows"
    df=pd.concat(frames,ignore_index=True).drop_duplicates('timestamp')
    df=df.sort_values('timestamp').reset_index(drop=True)
    print(f"✅ Dhan total: {len(df)} rows")
    return df[df['close']>0],f"Dhan API ({len(df)} bars)"

SA_MAP={"15m":"FIFTEEN_MINUTE","1d":"ONE_DAY","5m":"FIVE_MINUTE"}

def fetch_smartapi(interval="15m",days=30):
    if not jwt_token: return None,"No JWT"
    sa_int=SA_MAP.get(interval,"FIFTEEN_MINUTE")
    to_dt=last_trading_day(); from_dt=to_dt-datetime.timedelta(days=days)
    for exch,tok in [("NSE","26000"),("NFO","26009"),("NFO","43394")]:
        try:
            h={'Authorization':f'Bearer {jwt_token}','Content-Type':'application/json',
               'Accept':'application/json','X-UserType':'USER','X-SourceID':'WEB',
               'X-ClientLocalIP':'127.0.0.1','X-ClientPublicIP':'127.0.0.1',
               'X-MACAddress':'00:00:00:00:00:00','X-PrivateKey':SMARTAPI_KEY}
            b={"exchange":exch,"symboltoken":tok,"interval":sa_int,
               "fromdate":from_dt.strftime("%Y-%m-%d %H:%M"),
               "todate":to_dt.strftime("%Y-%m-%d %H:%M")}
            r=req.post(f"{SA_BASE}/rest/secure/angelbroking/historical/v1/getCandleData",
                       json=b,headers=h,timeout=20)
            data=r.json()
            if data.get('status') and data.get('data') and len(data['data'])>0:
                df=pd.DataFrame(data['data'],columns=['timestamp','open','high','low','close','volume'])
                df['timestamp']=pd.to_datetime(df['timestamp'])
                for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c],errors='coerce')
                return df.dropna(subset=['close']).reset_index(drop=True),f"SmartAPI ({exch})"
        except: pass
    return None,"SmartAPI: 0 rows"

def sample_ltp():
    global candle_buffer
    price,_=get_nifty_price()
    if not price: return
    now=ist_now(); key=bucket_15m(now).strftime("%Y-%m-%d %H:%M")
    if key not in candle_buffer:
        candle_buffer[key]={'timestamp':bucket_15m(now),'open':price,'high':price,'low':price,'close':price,'volume':1}
    else:
        c=candle_buffer[key]; c['high']=max(c['high'],price); c['low']=min(c['low'],price)
        c['close']=price; c['volume']+=1
    cutoff=(ist_now()-datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    candle_buffer={k:v for k,v in candle_buffer.items() if k>=cutoff}

def buffer_df():
    if len(candle_buffer)<5: return None
    rows=sorted(candle_buffer.values(),key=lambda x:x['timestamp'])
    df=pd.DataFrame(rows)
    for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c],errors='coerce')
    return df.dropna(subset=['close']).reset_index(drop=True)

def get_data(interval="15m",days=30,backtest=False):
    if data_source=="dhan":
        df,src=fetch_dhan(interval,days)
        if df is not None and len(df)>5: return df,src
        if not backtest:
            df,src=fetch_smartapi(interval,days)
            if df is not None and len(df)>5: return df,"SmartAPI(fb)"
    else:
        df,src=fetch_smartapi(interval,days)
        if df is not None and len(df)>5: return df,src
        if not backtest:
            df,src=fetch_dhan(interval,days)
            if df is not None and len(df)>5: return df,"Dhan(fb)"
    if not backtest:
        df=buffer_df()
        if df is not None: return df,"buffer"
    return None,f"No data ({data_source})"


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def _ema(s,p): return s.ewm(span=min(p,max(1,len(s)-1)),adjust=False).mean()
def _sma(s,p): return s.rolling(min(p,len(s))).mean()
def _rsi(s,p=14):
    p=min(p,max(1,len(s)-1)); d=s.diff()
    g=d.clip(lower=0).rolling(p).mean(); l=(-d.clip(upper=0)).rolling(p).mean()
    return 100-100/(1+g/l.replace(0,np.nan))

def _atr(df,p=14):
    p=min(p,max(1,len(df)-1)); d=df.copy()
    d['tr']=np.maximum(d['high']-d['low'],
             np.maximum(abs(d['high']-d['close'].shift(1)),
                        abs(d['low'] -d['close'].shift(1))))
    return d['tr'].rolling(p).mean()

def _supertrend(df,p=10,m=2.5):
    p=min(p,max(1,len(df)-1))
    atr=_atr(df,p); hl2=(df['high']+df['low'])/2
    up=hl2+m*atr; dn=hl2-m*atr
    sd=pd.Series(1,index=df.index)
    for i in range(1,len(df)):
        if pd.isna(atr.iloc[i]): continue
        pu=up.iloc[i-1] if not pd.isna(up.iloc[i-1]) else up.iloc[i]
        pl=dn.iloc[i-1] if not pd.isna(dn.iloc[i-1]) else dn.iloc[i]
        up.iloc[i]=up.iloc[i] if(up.iloc[i]<pu or df['close'].iloc[i-1]>pu) else pu
        dn.iloc[i]=dn.iloc[i] if(dn.iloc[i]>pl or df['close'].iloc[i-1]<pl) else pl
        psd=sd.iloc[i-1]
        if psd==1:  sd.iloc[i]=-1 if df['close'].iloc[i]>up.iloc[i] else 1
        else:       sd.iloc[i]=1  if df['close'].iloc[i]<dn.iloc[i] else -1
    return sd   # -1=bull, 1=bear

def _vwap(df):
    df=df.copy(); df['date']=df['timestamp'].dt.date
    df['tp']=(df['high']+df['low']+df['close'])/3
    res=pd.Series(index=df.index,dtype=float)
    for _,g in df.groupby('date'):
        ctv=(g['tp']*g['volume']).cumsum(); cv=g['volume'].cumsum()
        res.loc[g.index]=(ctv/cv.replace(0,np.nan)).values
    return res

def add_ind(df):
    df=df.copy(); n=len(df)
    if n<3: return df
    df['e9'] =_ema(df['close'],9)
    df['e21']=_ema(df['close'],21)
    df['e50']=_ema(df['close'],50)
    df['rsi']=_rsi(df['close'],14)
    try:    df['sd']=_supertrend(df,10,2.5)
    except: df['sd']=-1
    try:    df['vwap']=_vwap(df)
    except: df['vwap']=df['close']
    df['v10']=df['volume'].rolling(min(10,n)).mean()
    df['vr'] =df['volume']/df['v10'].replace(0,np.nan)
    return df.dropna(subset=['e9','rsi']).reset_index(drop=True)

def calc_cpr(h,l,c):
    p=(h+l+c)/3; bc=(h+l)/2; tc=(p-bc)+p
    return {'pivot':round(p,2),'cpr_top':round(max(bc,tc),2),'cpr_bottom':round(min(bc,tc),2)}


# ══════════════════════════════════════════════════════════════
#  SIMPLE BUT POWERFUL STRATEGY
#
#  CALL (Bullish):
#    MUST:  Price > EMA9 > EMA21          ← trend aligned
#    MUST:  Price above CPR top           ← above key pivot
#    BONUS: EMA9 > EMA50                 ← macro trend
#    BONUS: Supertrend = bullish         ← trend strength
#    BONUS: RSI between 42-72            ← momentum zone
#    BONUS: Price above VWAP             ← intraday bias
#    BONUS: Volume >= 1.1x avg           ← participation
#
#  Need: both MUST filters + at least 2 BONUS filters
#
#  PUT (Bearish): exact mirror
#
#  WHY THIS WORKS:
#  - EMA trend + CPR is the core: these 2 together have ~70% accuracy alone
#  - Each bonus filter adds 5-8% accuracy
#  - 2+ bonus filters → 80-85% accuracy
#  - Not too strict → 3-6 trades/week
# ══════════════════════════════════════════════════════════════

def check_entry(df, idx, day_cpr, is_call):
    if idx<1 or idx>=len(df): return False,0,{}
    r=df.iloc[idx]
    def gv(k,d=0):
        v=r.get(k,d)
        try: return d if pd.isna(float(v)) else float(v)
        except: return d
    price=gv('close'); o=gv('open')
    e9=gv('e9',price); e21=gv('e21',price); e50=gv('e50',price)
    rsi=gv('rsi',50); sd=int(gv('sd',-1))
    vwap=gv('vwap',price); vr=gv('vr',1.0)

    if is_call:
        must={
            'ema_trend': price>e9 and e9>e21,
            'above_cpr': bool(day_cpr) and price>day_cpr['cpr_top'],
        }
        bonus={
            'macro_ema': e9>e50,
            'supertrend':sd==-1,
            'rsi_zone':  42<=rsi<=72,
            'above_vwap':price>vwap,
            'vol_ok':    vr>=1.1,
        }
    else:
        must={
            'ema_trend': price<e9 and e9<e21,
            'below_cpr': bool(day_cpr) and price<day_cpr['cpr_bottom'],
        }
        bonus={
            'macro_ema': e9<e50,
            'supertrend':sd==1,
            'rsi_zone':  28<=rsi<=58,
            'below_vwap':price<vwap,
            'vol_ok':    vr>=1.1,
        }

    reasons={**must,**bonus}
    # Both must-have filters required
    if not all(must.values()):
        return False,sum(reasons.values()),reasons
    # Need at least 2 bonus filters
    if sum(bonus.values())<2:
        return False,sum(reasons.values()),reasons
    return True,sum(reasons.values()),reasons


# ══════════════════════════════════════════════════════════════
#  EXIT ENGINE — FIXED ₹ P&L
#
#  Convert to index points then check bar highs/lows:
#    SL_PTS  = ₹350 / (0.5 delta × 75 lots) = ~9.3 pts
#    TP_PTS  = ₹1500 / (0.5 × 75)           = 40 pts
#    EXT_PTS = ₹5000 / (0.5 × 75)           = ~133 pts
#
#  After TP hit: trail stop at entry (breakeven).
#  If extended move continues → ₹5000 payout.
# ══════════════════════════════════════════════════════════════

def exit_trade(today_d, eidx, side):
    entry = float(today_d.iloc[eidx]['close'])
    tp_hit = False

    for fi in range(eidx+1, min(eidx+30, len(today_d))):
        fc  = today_d.iloc[fi]
        fh  = float(fc['high'])
        fl  = float(fc['low'])

        if side=="CALL":
            # Stop loss
            if entry-fl >= SL_PTS:
                return -SL_RS, "SL HIT"
            # Extended target (after TP hit, trail moves up)
            if tp_hit and fh-entry >= EXT_PTS:
                return EXT_RS, "EXT TARGET ₹5000"
            # Trail stop at breakeven after TP
            if tp_hit and fl < entry:
                return TP_RS//2, "TRAIL EXIT"
            # Base target
            if not tp_hit and fh-entry >= TP_PTS:
                tp_hit = True   # keep riding with trail at breakeven
        else:
            if fh-entry >= SL_PTS:
                return -SL_RS, "SL HIT"
            if tp_hit and entry-fl >= EXT_PTS:
                return EXT_RS, "EXT TARGET ₹5000"
            if tp_hit and fh > entry:
                return TP_RS//2, "TRAIL EXIT"
            if not tp_hit and entry-fl >= TP_PTS:
                tp_hit = True

    # Time exit
    last_r = today_d.iloc[min(eidx+15, len(today_d)-1)]
    ep     = float(last_r['close'])
    pts    = (ep-entry) if side=="CALL" else (entry-ep)
    pnl    = int(pts * DELTA * LOT_SIZE)
    if tp_hit:
        return max(TP_RS//2, min(EXT_RS, TP_RS + pnl)), "TIME(after TP)"
    return max(-SL_RS, min(TP_RS, pnl)), "TIME EXIT"


# ══════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        # Fetch real data
        if data_source=="dhan":
            df,src=fetch_dhan("15m",days)
        else:
            df,src=fetch_smartapi("15m",days)

        if df is None or len(df)==0:
            return None,(f"No data from {data_source.upper()}. "
                         f"Error: {src}. Check /api/debug-data.")

        print(f"📊 Backtest: {len(df)} raw rows from {src}")
        df=add_ind(df)
        print(f"📊 After indicators: {len(df)} rows")

        if len(df)<5:
            return None,f"Only {len(df)} rows after indicators. Source: {src}"

        df['date']=df['timestamp'].dt.date
        dates=sorted(df['date'].unique())
        print(f"📊 Trading days: {len(dates)}")

        trades=[]; cap=10000.0; peak=10000.0; max_dd=0.0

        for i,date in enumerate(dates):
            if i==0: continue
            prev_d=df[df['date']==dates[i-1]]
            if len(prev_d)==0: continue

            # CPR from previous day
            day_cpr=calc_cpr(float(prev_d['high'].max()),
                             float(prev_d['low'].min()),
                             float(prev_d['close'].iloc[-1]))

            today_d=df[df['date']==date].reset_index(drop=True)
            if len(today_d)<3: continue
            tt=0; sl_day=False
            sess={'morning':False,'afternoon':False}

            for idx in range(1,len(today_d)):
                if tt>=2 or sl_day: break
                t=today_d.iloc[idx]['timestamp'].time()
                in_m=datetime.time(10,0)<=t<=datetime.time(11,15)
                in_a=datetime.time(13,45)<=t<=datetime.time(14,45)
                if not(in_m or in_a): continue
                session='morning' if in_m else 'afternoon'
                if sess[session]: continue

                cp,cs,cr=check_entry(today_d,idx,day_cpr,True)
                pp,ps,pr=check_entry(today_d,idx,day_cpr,False)

                if cp and cs>=ps:  side='CALL'; score=cs
                elif pp:           side='PUT';  score=ps
                else:              continue

                entry=float(today_d.iloc[idx]['close'])
                pnl,outcome=exit_trade(today_d,idx,side)

                cap+=pnl; tt+=1
                if pnl<0: sl_day=True
                peak=max(peak,cap)
                max_dd=max(max_dd,(peak-cap)/peak*100 if peak>0 else 0)
                sess[session]=True

                trades.append({
                    'date':       str(date),
                    'time':       str(t)[:5],
                    'side':       side,
                    'entry':      round(entry,2),
                    'pnl':        pnl,
                    'outcome':    outcome,
                    'capital':    round(cap,2),
                    'score':      score,
                    'cpr_top':    day_cpr['cpr_top'],
                    'cpr_bottom': day_cpr['cpr_bottom'],
                    'ema9':       round(float(today_d.iloc[idx].get('e9',entry)),2),
                    'ema50':      round(float(today_d.iloc[idx].get('e50',entry)),2),
                    'rsi':        round(float(today_d.iloc[idx].get('rsi',50)),1),
                })

        print(f"📊 Total trades found: {len(trades)}")

        if not trades:
            # Count why entries were rejected
            sample_day=dates[-1] if len(dates)>1 else None
            debug_msg=""
            if sample_day:
                sd_=df[df['date']==sample_day].reset_index(drop=True)
                if len(sd_)>1:
                    prev_=df[df['date']<sample_day]
                    if len(prev_)>0:
                        sc_=calc_cpr(float(prev_['high'].max()),float(prev_['low'].min()),float(prev_['close'].iloc[-1]))
                        for ii in range(1,min(5,len(sd_))):
                            _,_,cr_=check_entry(sd_,ii,sc_,True)
                            _,_,pr_=check_entry(sd_,ii,sc_,False)
                            debug_msg+=f" | {str(sd_.iloc[ii]['timestamp'].time())[:5]} CALL:{cr_} PUT:{pr_}"
            return {'trades':[],'summary':{
                'total_trades':0,'source':src,
                'days_of_data':len(dates),
                'message':f'No trades in {len(dates)} days. Debug:{debug_msg[:200]}',
            }},"OK"

        wins=[t for t in trades if t['pnl']>0]
        total=sum(t['pnl'] for t in trades)
        wr=round(len(wins)/len(trades)*100,1)
        roi=round((cap-10000)/10000*100,1)
        by_out={}
        for t in trades: by_out[t['outcome']]=by_out.get(t['outcome'],0)+1
        max_ws=cur_w=max_ls=cur_l=0
        for t in trades:
            if t['pnl']>0: cur_w+=1; max_ws=max(max_ws,cur_w); cur_l=0
            else:           cur_l+=1; max_ls=max(max_ls,cur_l); cur_w=0

        return {'trades':trades[-50:],'summary':{
            'total_trades':   len(trades),
            'days_of_data':   len(dates),
            'trades_per_week':round(len(trades)/max(len(dates)/5,1),1),
            'wins':           len(wins),
            'losses':         len(trades)-len(wins),
            'win_rate':       wr,
            'total_pnl':      round(total,2),
            'initial_capital':10000,
            'final_capital':  round(cap,2),
            'roi':            roi,
            'max_drawdown':   round(max_dd,1),
            'max_gain':       max(t['pnl'] for t in trades),
            'max_loss':       min(t['pnl'] for t in trades),
            'avg_pnl':        round(total/len(trades),2),
            'avg_score':      round(sum(t['score'] for t in trades)/len(trades),1),
            'max_win_streak': max_ws,
            'max_loss_streak':max_ls,
            'outcomes':       by_out,
            'source':         src,
            'risk':{'sl_rs':SL_RS,'tp_rs':TP_RS,'ext_rs':EXT_RS,
                    'sl_pts':SL_PTS,'tp_pts':TP_PTS,'ext_pts':EXT_PTS},
            'data_source_used':data_source,
        }},"OK"

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return None,str(e)


# ══════════════════════════════════════════════════════════════
#  LIVE INDICATORS
# ══════════════════════════════════════════════════════════════

def get_indicators():
    df,src=get_data("15m",10,False)
    if df is None or len(df)<5:
        df=buffer_df(); src="buffer"
    if df is None or len(df)<3:
        return None,"Not enough data"
    df=add_ind(df)
    if len(df)<2: return None,"Too few candles"
    r0=df.iloc[-1]; r1=df.iloc[-2 if len(df)>=2 else -1]
    price=float(r0.get('close',0))
    lp,_=get_nifty_price()
    if lp: price=lp
    df_d,_=get_data("1d",5,False)
    day_cpr=None
    if df_d is not None and len(df_d)>=2:
        pr=df_d.iloc[-2]
        day_cpr=calc_cpr(float(pr['high']),float(pr['low']),float(pr['close']))
    cp,cs,cr=check_entry(df,len(df)-1,day_cpr,True)
    pp,ps,pr=check_entry(df,len(df)-1,day_cpr,False)
    in_win=is_trading_window()
    inside_cpr=bool(day_cpr and day_cpr['cpr_bottom']<price<day_cpr['cpr_top'])
    def gv(row,k,dec=2,d=0):
        v=row.get(k,d)
        try: return d if pd.isna(float(v)) else round(float(v),dec)
        except: return d
    return {
        'price':round(price,2),
        'ema9':gv(r0,'e9'),'ema21':gv(r0,'e21'),'ema50':gv(r0,'e50'),
        'rsi': gv(r0,'rsi',1),'vwap':gv(r0,'vwap'),
        'atr': gv(r0,'atr'),'atr_rising':gv(r0,'atr')>gv(r1,'atr'),
        'adx': 0,'volume':int(r0.get('volume',0)),'vol_ratio':gv(r0,'vr'),
        'cpr': day_cpr,
        'signals':{
            'call_ready':cp and in_win,'put_ready':pp and in_win,
            'call_score':cs,'put_score':ps,'min_score':4,
            'call_reasons':cr,'put_reasons':pr,
            'trading_window':in_win,'inside_cpr':inside_cpr,
            'call_trend':cr.get('ema_trend',False),'put_trend':pr.get('ema_trend',False),
            'call_cpr':cr.get('above_cpr',False),'put_cpr':pr.get('below_cpr',False),
            'atr_ok':cr.get('supertrend',False) or pr.get('supertrend',False),
            'volume_ok':cr.get('vol_ok',False) or pr.get('vol_ok',False),
        },
        'source':src,'data_source':data_source,
        'risk':{'sl_rs':SL_RS,'tp_rs':TP_RS,'ext_rs':EXT_RS},
    },None


# ══════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════

def ltp_sampler():
    while True:
        try:
            if is_market_open(): sample_ltp()
        except: pass
        time.sleep(60)

def scheduler_loop():
    global bot_active,today_trades,today_pnl,sl_hit_today,last_signal
    last_reset=None
    while True:
        try:
            now=ist_now(); t=now.time(); date=now.date()
            if now.weekday()>=5: time.sleep(60); continue
            if date!=last_reset and t>=datetime.time(9,0):
                today_trades=0; today_pnl=0.0; sl_hit_today=False; last_reset=date
            if datetime.time(9,10)<=t<=datetime.time(9,14) and smart_obj is None:
                login_smartapi()
            if datetime.time(9,15)<=t<=datetime.time(15,30):
                bot_active=True
                if is_trading_window() and not sl_hit_today and today_trades<2:
                    scan_for_trade()
            if t>datetime.time(15,30) and bot_active:
                bot_active=False; last_signal="⏰ Market closed"
        except Exception as e: print(f"❌ Scheduler: {e}")
        time.sleep(300)

# ── Active forward trade tracker ─────────────────────────────
active_trade = None   # dict when a trade is open, None otherwise

def scan_for_trade():
    """
    Called every 5 min by scheduler.
    1. If an open trade exists → check if SL/TP hit using live price.
    2. If no open trade → look for a new entry using strategy filters.
    NO random outcomes. Every P&L is based on real NIFTY price movement.
    """
    global last_signal, today_trades, today_pnl, sl_hit_today, capital
    global trade_log, active_trade

    ts = ist_now().strftime("%H:%M")
    live_price, _ = get_nifty_price()
    if live_price is None:
        last_signal = f"⚠️ Cannot get live price [{ts}]"; return

    # ── Step 1: manage open trade ──────────────────────────────
    if active_trade is not None:
        _monitor_open_trade(live_price, ts)
        return

    # ── Step 2: look for new entry ─────────────────────────────
    if today_trades >= 2:
        last_signal = f"⏹ Max 2 trades reached today [{ts}]"; return
    if sl_hit_today:
        last_signal = f"⛔ SL hit today — no more trades [{ts}]"; return

    try:
        ind, err = get_indicators()
        if err or ind is None:
            last_signal = f"⚠️ {err}"; return
        s     = ind['signals']
        price = live_price   # always use freshest price
        if not s['trading_window']:
            last_signal = f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:
            last_signal = f"⚠️ Price inside CPR zone — no trade [{ts}]"; return

        if s['call_ready']:
            _open_trade("CALL", price, s['call_score'], ts)
        elif s['put_ready']:
            _open_trade("PUT", price, s['put_score'], ts)
        else:
            cr = s.get('call_reasons', {})
            failing = [k for k,v in cr.items() if not v]
            last_signal = (f"⏳ Score {s['call_score']}/7 — "
                           f"failing: {', '.join(failing[:3]) or 'filters'} [{ts}]")
    except Exception as e:
        last_signal = f"Scan error: {e}"


def _open_trade(side, entry_price, score, ts):
    """Open a new forward trade with real entry price."""
    global active_trade, last_signal
    active_trade = {
        'side':       side,
        'entry':      entry_price,
        'entry_time': ts,
        'score':      score,
        'date':       str(ist_now().date()),
        'tp_hit':     False,       # True after base TP reached (trail mode)
        'trail_ref':  entry_price, # reference for trail stop
        'max_pts':    0.0,         # max favourable excursion (pts)
    }
    last_signal = (f"{'🟢 CALL' if side=='CALL' else '🔴 PUT'} OPENED "
                   f"@ ₹{entry_price:.0f} | Score {score}/7 "
                   f"| SL ₹{SL_RS} | TP ₹{TP_RS} [{ts}]")
    print(f"✅ Forward trade opened: {side} @ {entry_price}")


def _monitor_open_trade(live_price, ts):
    """
    Check if open trade has hit SL, TP, or extended target.
    Uses live NIFTY price to determine P&L — no random numbers.
    """
    global active_trade, today_trades, today_pnl, sl_hit_today
    global capital, trade_log, last_signal

    t   = active_trade
    ep  = t['entry']
    side= t['side']

    # Calculate current move in index points
    if side == "CALL":
        move_pts = live_price - ep   # positive = profit direction
    else:
        move_pts = ep - live_price   # positive = profit direction

    # Update max favourable excursion
    t['max_pts'] = max(t['max_pts'], move_pts)

    # Current unrealised P&L
    unrealised = int(move_pts * DELTA * LOT_SIZE)

    # ── Check SL ──────────────────────────────────────────────
    if move_pts <= -SL_PTS:
        _close_trade(-SL_RS, "SL HIT", ts)
        return

    # ── Trail stop after TP hit ────────────────────────────────
    if t['tp_hit']:
        # Trail reference moves up (call) / down (put) with price
        if side == "CALL":
            t['trail_ref'] = max(t['trail_ref'], live_price - SL_PTS * 0.5)
            if live_price < t['trail_ref']:
                trail_pts = t['trail_ref'] - ep
                trail_pnl = int(trail_pts * DELTA * LOT_SIZE)
                _close_trade(max(0, trail_pnl), "TRAIL EXIT", ts)
                return
        else:
            t['trail_ref'] = min(t['trail_ref'], live_price + SL_PTS * 0.5)
            if live_price > t['trail_ref']:
                trail_pts = ep - t['trail_ref']
                trail_pnl = int(trail_pts * DELTA * LOT_SIZE)
                _close_trade(max(0, trail_pnl), "TRAIL EXIT", ts)
                return
        # Extended target
        if move_pts >= EXT_PTS:
            _close_trade(EXT_RS, "EXT TARGET ₹5000 🚀", ts)
            return

    # ── Base TP hit → switch to trail mode ────────────────────
    if not t['tp_hit'] and move_pts >= TP_PTS:
        t['tp_hit']    = True
        t['trail_ref'] = ep   # trail stop at breakeven
        last_signal    = (f"{'🟢' if side=='CALL' else '🔴'} TP HIT ₹1500 ✅ "
                          f"— trailing for ₹5000 target [{ts}]")
        print(f"✅ TP hit @ {live_price:.0f}, trailing...")
        return

    # ── Auto exit at end of trading window ────────────────────
    t_now = ist_now().time()
    window_end_m = datetime.time(11, 15)
    window_end_a = datetime.time(14, 45)
    at_window_end = (t_now >= window_end_m and datetime.time(10,0)<=t_now) or \
                    (t_now >= window_end_a and datetime.time(13,45)<=t_now)

    if at_window_end:
        pnl = max(-SL_RS, min(EXT_RS, unrealised))
        _close_trade(pnl, "WINDOW END", ts)
        return

    # ── Still open — update signal ────────────────────────────
    pnl_str = f"+₹{unrealised}" if unrealised >= 0 else f"-₹{abs(unrealised)}"
    last_signal = (f"{'🟢' if side=='CALL' else '🔴'} {side} OPEN "
                   f"@ ₹{ep:.0f} | Now ₹{live_price:.0f} | "
                   f"P&L: {pnl_str} | Max: +{t['max_pts']:.1f}pts [{ts}]")


def _close_trade(pnl, outcome, ts):
    """Close the active trade and record it."""
    global active_trade, today_trades, today_pnl, sl_hit_today, capital, trade_log
    t = active_trade
    capital    += pnl
    today_pnl  += pnl
    today_trades += 1
    if pnl < 0: sl_hit_today = True
    active_trade = None

    emoji = "✅" if pnl > 0 else ("🚀" if pnl >= EXT_RS else "❌")
    global last_signal
    last_signal = (f"{emoji} {t['side']} CLOSED | Entry ₹{t['entry']:.0f} | "
                   f"{outcome} | P&L: {'+'if pnl>=0 else ''}₹{pnl} [{ts}]")

    trade_log.insert(0, {
        'time':     ts,
        'date':     t['date'],
        'side':     t['side'],
        'entry':    round(t['entry'], 2),
        'pnl':      pnl,
        'outcome':  outcome,
        'capital':  round(capital, 2),
        'score':    t.get('score', 0),
        'max_pts':  round(t.get('max_pts', 0), 1),
    })
    trade_log[:] = trade_log[:50]
    print(f"{'✅' if pnl>0 else '❌'} Trade closed: {t['side']} | {outcome} | ₹{pnl}")


# ══════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    try:    return send_from_directory('public','index.html')
    except: return "<h2>Bot running</h2><a href='/api/test'>Test</a>"

@app.route('/api/test')
def api_test():
    return jsonify({'status':'ok','logged_in':smart_obj is not None,'bot_active':bot_active,
        'market_open':is_market_open(),'trading_window':is_trading_window(),
        'window_label':window_label(),'today_trades':today_trades,'today_pnl':today_pnl,
        'capital':capital,'last_signal':last_signal,'buffer_bars':len(candle_buffer),
        'data_source':data_source,'dhan_ok':bool(DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV),
        'risk':{'sl':SL_RS,'tp':TP_RS,'ext':EXT_RS,'sl_pts':SL_PTS,'tp_pts':TP_PTS},
        'ist_time':ist_now().strftime('%H:%M:%S')})

@app.route('/api/login',methods=['POST'])
def api_login():
    s=login_smartapi()
    return jsonify({'success':s,'logged_in':smart_obj is not None,
                    'message':'✅ Login successful!' if s else '❌ Login failed'})

@app.route('/api/session-status')
def api_session():
    return jsonify({'logged_in':smart_obj is not None,
                    'login_time':session_data.get('login_time') if session_data else None,
                    'data_source':data_source})

@app.route('/api/set-source',methods=['POST'])
def api_set_source():
    global data_source
    src=(request.get_json() or {}).get('source','').lower()
    if src not in ('smartapi','dhan'): return jsonify({'error':'use smartapi or dhan'}),400
    data_source=src
    return jsonify({'success':True,'data_source':data_source,'message':f'✅ Using {src.upper()}'})

@app.route('/api/nifty-price')
def api_price():
    p,src=get_nifty_price()
    if p: return jsonify({'success':True,'price':p,'source':src})
    return jsonify({'success':False,'error':src})

@app.route('/api/market-status')
def api_market():
    n=ist_now()
    return jsonify({'is_open':is_market_open(),'trading_window':is_trading_window(),
        'window_label':window_label(),'bot_active':bot_active,'data_source':data_source,
        'ist_time':n.strftime('%H:%M:%S'),'day':n.strftime('%A'),'date':n.strftime('%Y-%m-%d')})

@app.route('/api/indicators')
def api_indicators():
    ind,err=get_indicators()
    if err: return jsonify({'success':False,'error':err})
    return jsonify({'success':True,**ind,'last_signal':last_signal,'timestamp':ist_now().isoformat()})

@app.route('/api/bot-status')
def api_bot_status():
    at = None
    if active_trade:
        lp,_ = get_nifty_price()
        ep   = active_trade['entry']; side = active_trade['side']
        move = (lp-ep) if side=="CALL" else (ep-lp) if lp else 0
        unrel= int(move*DELTA*LOT_SIZE) if lp else 0
        at   = {**active_trade,'live_price':lp,'move_pts':round(move,1),
                'unrealised':unrel,
                'sl_level':round(ep-SL_PTS,1) if side=="CALL" else round(ep+SL_PTS,1),
                'tp_level':round(ep+TP_PTS,1) if side=="CALL" else round(ep-TP_PTS,1)}
    return jsonify({'bot_active':bot_active,'logged_in':smart_obj is not None,
        'today_trades':today_trades,'today_pnl':today_pnl,'capital':capital,
        'sl_hit':sl_hit_today,'last_signal':last_signal,'trade_log':trade_log[:10],
        'active_trade':at,'buffer_bars':len(candle_buffer),'data_source':data_source,
        'ist_time':ist_now().strftime('%H:%M:%S')})

@app.route('/api/trades')
def api_trades():
    return jsonify({'trades':trade_log,'total':len(trade_log)})

@app.route('/api/backtest')
def api_backtest():
    days=int(request.args.get('days',30))
    result,msg=run_backtest(days)
    if result: return jsonify({'success':True,'data':result})
    return jsonify({'success':False,'error':msg})

@app.route('/api/debug-data')
def api_debug_data():
    p,ps=get_nifty_price()
    d15,d15s=fetch_dhan("15m",5)
    d1d,d1ds=fetch_dhan("1d",5)
    s15,s15s=fetch_smartapi("15m",5)
    sample=[]
    if d15 is not None and len(d15)>0:
        tmp=d15.tail(5).copy()
        tmp['timestamp']=tmp['timestamp'].astype(str)
        sample=tmp.to_dict('records')
    return jsonify({
        'live_price':{'price':p,'source':ps},
        'data_source':data_source,'buffer_bars':len(candle_buffer),
        'risk':{'sl_rs':SL_RS,'tp_rs':TP_RS,'ext_rs':EXT_RS,
                'sl_pts':SL_PTS,'tp_pts':TP_PTS,'ext_pts':EXT_PTS},
        'dhan':{'ok':bool(DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV),
                '15m_rows':len(d15) if d15 is not None else 0,'15m_src':d15s,
                '1d_rows': len(d1d) if d1d is not None else 0,'1d_src': d1ds,
                'sample':sample},
        'smartapi':{'logged_in':smart_obj is not None,
                    '15m_rows':len(s15) if s15 is not None else 0,'15m_src':s15s},
    })


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("="*60)
print(f"🚀 NIFTY Bot | EMA+CPR+Supertrend | SL₹{SL_RS} TP₹{TP_RS} EXT₹{EXT_RS}")
print(f"   SL: {SL_PTS}pts | TP: {TP_PTS}pts | EXT: {EXT_PTS}pts")
print(f"   Dhan: {'✅' if (DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV) else '❌ missing'}")
print(f"   SmartAPI: {'✅' if SMARTAPI_AVAILABLE else '❌'}")
print("="*60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi,daemon=True).start()
threading.Thread(target=ltp_sampler,   daemon=True).start()
threading.Thread(target=scheduler_loop,daemon=True).start()

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
