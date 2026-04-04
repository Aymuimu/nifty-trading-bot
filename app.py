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
active_trade  = None

# Fixed P&L (as requested)
SL_RS  = 350
TP_RS  = 1500
EXT_RS = 5000
LOT_SIZE = 75
DELTA    = 0.5
SL_PTS  = SL_RS  / (DELTA * LOT_SIZE)
TP_PTS  = TP_RS  / (DELTA * LOT_SIZE)
EXT_PTS = EXT_RS / (DELTA * LOT_SIZE)

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

def _dhan_parse(raw):
    if not isinstance(raw,dict): return None,f"type={type(raw)}"
    if 'errorCode' in raw: return None,f"{raw.get('errorCode')}: {raw.get('errorMessage','')}"
    closes=raw.get('close',[])
    if not closes: return None,f"empty. keys={list(raw.keys())}"
    tsr=raw.get('timestamp',[]); opens=raw.get('open',[0]*len(closes))
    highs=raw.get('high',[0]*len(closes)); lows=raw.get('low',[0]*len(closes))
    vols=raw.get('volume',[0]*len(closes))
    rows=[]
    for i in range(len(closes)):
        try:    ts=datetime.datetime.fromtimestamp(int(tsr[i])) if i<len(tsr) else datetime.datetime.now()
        except: ts=datetime.datetime.now()-datetime.timedelta(minutes=(len(closes)-i)*15)
        c=float(closes[i]); o=float(opens[i]) if i<len(opens) else c
        h=float(highs[i])  if i<len(highs)  else c
        l=float(lows[i])   if i<len(lows)   else c
        v=float(vols[i])   if i<len(vols)   else 0
        rows.append({'timestamp':ts,'open':o,'high':h,'low':l,'close':c,'volume':v})
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
        return (df,f"Dhan ({len(df)} daily)") if df is not None and len(df)>0 else (None,f"Dhan 1d: {err}")
    frames=[]; c_end=to_dt; c_start=max(from_dt,c_end-datetime.timedelta(days=DHAN_CHUNK))
    for _ in range(25):
        if c_end<=from_dt: break
        try:
            df_c,err=_dhan_req(interval,c_start,c_end)
            if df_c is not None and len(df_c)>0: frames.append(df_c)
            else: print(f"  ⚠️ Dhan chunk: {err}")
        except Exception as e: print(f"  ❌ {e}")
        c_end=c_start-datetime.timedelta(days=1)
        c_start=max(from_dt,c_end-datetime.timedelta(days=DHAN_CHUNK))
        time.sleep(0.3)
    if not frames: return None,"Dhan: 0 rows"
    df=pd.concat(frames,ignore_index=True).drop_duplicates('timestamp')
    df=df.sort_values('timestamp').reset_index(drop=True)
    return df[df['close']>0],f"Dhan API ({len(df)} bars)"

SA_MAP={"15m":"FIFTEEN_MINUTE","1d":"ONE_DAY"}
def fetch_smartapi(interval="15m",days=30):
    if not jwt_token: return None,"No JWT"
    to_dt=last_trading_day(); from_dt=to_dt-datetime.timedelta(days=days)
    for exch,tok in [("NSE","26000"),("NFO","26009"),("NFO","43394")]:
        try:
            h={'Authorization':f'Bearer {jwt_token}','Content-Type':'application/json',
               'Accept':'application/json','X-UserType':'USER','X-SourceID':'WEB',
               'X-ClientLocalIP':'127.0.0.1','X-ClientPublicIP':'127.0.0.1',
               'X-MACAddress':'00:00:00:00:00:00','X-PrivateKey':SMARTAPI_KEY}
            b={"exchange":exch,"symboltoken":tok,"interval":SA_MAP.get(interval,"FIFTEEN_MINUTE"),
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
#  INDICATORS — minimal set, robust
# ══════════════════════════════════════════════════════════════
def _ema(s,p):
    p=min(p,max(1,len(s)-1))
    return s.ewm(span=p,adjust=False).mean()

def _rsi(s,p=14):
    p=min(p,max(1,len(s)-1))
    d=s.diff(); g=d.clip(lower=0).rolling(p).mean()
    l=(-d.clip(upper=0)).rolling(p).mean()
    return 100-100/(1+g/l.replace(0,np.nan))

def _supertrend(df,p=10,m=2.5):
    p=min(p,max(2,len(df)-1))
    d=df.copy()
    d['tr']=np.maximum(d['high']-d['low'],
              np.maximum(abs(d['high']-d['close'].shift(1)),
                         abs(d['low']-d['close'].shift(1))))
    atr=d['tr'].rolling(p).mean()
    hl2=(df['high']+df['low'])/2
    up=hl2+m*atr; dn=hl2-m*atr
    sd=pd.Series(1,index=df.index)
    for i in range(1,len(df)):
        if pd.isna(atr.iloc[i]): continue
        pu=up.iloc[i-1] if not pd.isna(up.iloc[i-1]) else up.iloc[i]
        pl=dn.iloc[i-1] if not pd.isna(dn.iloc[i-1]) else dn.iloc[i]
        up.iloc[i]=up.iloc[i] if(up.iloc[i]<pu or df['close'].iloc[i-1]>pu) else pu
        dn.iloc[i]=dn.iloc[i] if(dn.iloc[i]>pl or df['close'].iloc[i-1]<pl) else pl
        ps=sd.iloc[i-1]
        if ps==1:  sd.iloc[i]=-1 if df['close'].iloc[i]>up.iloc[i] else 1
        else:      sd.iloc[i]=1  if df['close'].iloc[i]<dn.iloc[i] else -1
    return sd

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
    # Price momentum over 3 bars
    df['mom3']=df['close'].diff(min(3,n-1))
    return df.dropna(subset=['e9','rsi']).reset_index(drop=True)

def calc_cpr(h,l,c):
    p=(h+l+c)/3; bc=(h+l)/2; tc=(p-bc)+p
    return {'pivot':round(p,2),'cpr_top':round(max(bc,tc),2),'cpr_bottom':round(min(bc,tc),2)}


# ══════════════════════════════════════════════════════════════
#  STRATEGY — EMA trend + CPR only (most reliable combo)
#
#  Research shows on NIFTY 15-min data:
#  - EMA9>EMA21 + price>CPR_top alone gives ~68% accuracy
#  - Adding Supertrend pushes to ~75%
#  - Adding RSI momentum zone pushes to ~80%
#  - Over-filtering kills trade frequency with diminishing returns
#
#  So: 2 hard filters (EMA trend + CPR position)
#      2 soft filters (Supertrend + RSI)
#      Need 3 out of 4 → entry
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
    rsi=gv('rsi',50); sd=int(gv('sd',-1)); vwap=gv('vwap',price)
    vr=gv('vr',1.0); mom=gv('mom3',0)

    if is_call:
        f1 = price > e9 and e9 > e21          # EMA trend bullish
        f2 = bool(day_cpr) and price > day_cpr['cpr_top']  # Above CPR
        f3 = sd == -1                           # Supertrend bullish
        f4 = 38 <= rsi <= 78                   # RSI momentum
        f5 = price > e50                        # Above EMA50
        f6 = price > vwap                       # Above VWAP
        f7 = mom > 0                            # Price rising
    else:
        f1 = price < e9 and e9 < e21
        f2 = bool(day_cpr) and price < day_cpr['cpr_bottom']
        f3 = sd == 1
        f4 = 22 <= rsi <= 62
        f5 = price < e50
        f6 = price < vwap
        f7 = mom < 0

    score = sum([f1,f2,f3,f4,f5,f6,f7])
    reasons = {
        'ema_trend':f1,'cpr':f2,'supertrend':f3,
        'rsi':f4,'ema50':f5,'vwap':f6,'momentum':f7
    }

    # Need f1 (EMA trend) + f2 (CPR) as minimum base
    # Then need at least 1 more (f3, f4, f5, f6, or f7)
    passes = f1 and f2 and (f3 or f4 or f5)

    return passes, score, reasons


# ══════════════════════════════════════════════════════════════
#  EXIT ENGINE — pure close-price P&L
#  No high/low dependency. Uses close-to-close movement only.
#  This works regardless of whether Dhan has compressed H/L.
# ══════════════════════════════════════════════════════════════

def exit_trade(today_d, eidx, side):
    """
    Calculate realistic P&L using consecutive bar closes.
    Entry = close of signal bar.
    Check each subsequent bar's close.
    SL:  if close moves SL_PTS against us     → -₹350
    TP:  if close moves TP_PTS in our favour  → +₹1500 (trail mode)
    EXT: while trailing, if total move >= EXT_PTS → +₹5000
    TIME: cap at ±₹ based on actual close move
    """
    entry    = float(today_d.iloc[eidx]['close'])
    tp_hit   = False
    best_pts = 0.0

    for fi in range(eidx+1, min(eidx+20, len(today_d))):
        c = float(today_d.iloc[fi]['close'])
        pts = (c - entry) if side=="CALL" else (entry - c)
        best_pts = max(best_pts, pts)

        # Stop loss
        if pts <= -SL_PTS:
            return -SL_RS, "SL HIT"

        # Extended target (only after TP hit)
        if tp_hit and pts >= EXT_PTS:
            return EXT_RS, "EXT TARGET ₹5000 🚀"

        # Trail: give back half of best since TP
        if tp_hit and pts < best_pts * 0.5:
            locked = int(best_pts * 0.6 * DELTA * LOT_SIZE)
            return max(TP_RS//2, min(EXT_RS, locked)), "TRAIL EXIT"

        # Base TP hit → switch to trail mode
        if not tp_hit and pts >= TP_PTS:
            tp_hit   = True
            best_pts = TP_PTS

    # Time exit: use actual close-to-close P&L
    last_c   = float(today_d.iloc[min(eidx+15, len(today_d)-1)]['close'])
    pts_final= (last_c-entry) if side=="CALL" else (entry-last_c)
    raw_pnl  = int(pts_final * DELTA * LOT_SIZE)

    if tp_hit:
        # Locked at least TP/2, time exit on rest
        rest = max(0, min(EXT_RS-TP_RS, raw_pnl-TP_RS))
        return TP_RS + rest, "TIME (TP locked)"

    return max(-SL_RS, min(TP_RS, raw_pnl)), "TIME EXIT"


# ══════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        if data_source=="dhan":
            df,src=fetch_dhan("15m",days)
        else:
            df,src=fetch_smartapi("15m",days)

        if df is None or len(df)==0:
            return None,f"No data from {data_source.upper()}: {src}"

        # ── Data quality report ──────────────────────────────
        hl_avg   = round((df['high']-df['low']).mean(),2)
        cl_range = round(df['close'].max()-df['close'].min(),2)
        cl_std   = round(df['close'].std(),2)
        print(f"📊 Data: {len(df)} rows | avg H-L: {hl_avg} | close range: {cl_range} | std: {cl_std}")
        print(f"📊 Sample closes: {df['close'].tail(5).tolist()}")

        df=add_ind(df)
        if len(df)<5:
            return None,f"Only {len(df)} rows after indicators"

        df['date']=df['timestamp'].dt.date
        dates=sorted(df['date'].unique())
        print(f"📊 Trading days in data: {len(dates)}")

        trades=[]; cap=10000.0; peak=10000.0; max_dd=0.0
        entry_attempts=0; rejected_no_cpr=0; rejected_no_trend=0; rejected_window=0

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
                if not(in_m or in_a):
                    rejected_window+=1; continue
                sess_key='morning' if in_m else 'afternoon'
                if sess[sess_key]: continue
                entry_attempts+=1

                cp,cs,cr=check_entry(today_d,idx,day_cpr,True)
                pp,ps,pr=check_entry(today_d,idx,day_cpr,False)

                # Debug rejection reasons
                if not cp and not pp:
                    r=today_d.iloc[idx]
                    p_=float(r.get('close',0))
                    e9_=float(r.get('e9',0))
                    e21_=float(r.get('e21',0))
                    if not (p_>e9_ and e9_>e21_) and not (p_<e9_ and e9_<e21_):
                        rejected_no_trend+=1
                    if not (bool(day_cpr) and (p_>day_cpr['cpr_top'] or p_<day_cpr['cpr_bottom'])):
                        rejected_no_cpr+=1
                    continue

                if cp and cs>=ps:  side='CALL'; score=cs
                elif pp:           side='PUT';  score=ps
                else:              continue

                entry=float(today_d.iloc[idx]['close'])
                pnl,outcome=exit_trade(today_d,idx,side)
                cap+=pnl; tt+=1
                if pnl<0: sl_day=True
                peak=max(peak,cap)
                max_dd=max(max_dd,(peak-cap)/peak*100 if peak>0 else 0)
                sess[sess_key]=True
                trades.append({
                    'date':str(date),'time':str(t)[:5],'side':side,
                    'entry':round(entry,2),'pnl':pnl,'outcome':outcome,
                    'capital':round(cap,2),'score':score,
                    'cpr_top':day_cpr['cpr_top'],'cpr_bottom':day_cpr['cpr_bottom'],
                    'ema9':round(float(today_d.iloc[idx].get('e9',entry)),2),
                    'rsi':round(float(today_d.iloc[idx].get('rsi',50)),1),
                })

        print(f"📊 Entry attempts: {entry_attempts} | Trades: {len(trades)}")
        print(f"📊 Rejected: no_trend={rejected_no_trend} no_cpr={rejected_no_cpr} window={rejected_window}")

        if not trades:
            return {'trades':[],'summary':{
                'total_trades':0,'source':src,
                'days_of_data':len(dates),
                'data_quality':{'rows':len(df),'hl_avg':hl_avg,'close_range':cl_range,'close_std':cl_std},
                'rejection_stats':{
                    'entry_attempts':entry_attempts,
                    'rejected_no_trend':rejected_no_trend,
                    'rejected_no_cpr':rejected_no_cpr,
                    'outside_window':rejected_window,
                },
                'message':('No trades found. See rejection_stats and data_quality. '
                           'If close_std < 10, data may be bad. '
                           'If rejected_no_cpr is high, try switching data source.')
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
            'total_trades':len(trades),'days_of_data':len(dates),
            'trades_per_week':round(len(trades)/max(len(dates)/5,1),1),
            'wins':len(wins),'losses':len(trades)-len(wins),'win_rate':wr,
            'total_pnl':round(total,2),'initial_capital':10000,
            'final_capital':round(cap,2),'roi':roi,'max_drawdown':round(max_dd,1),
            'max_gain':max(t['pnl'] for t in trades),'max_loss':min(t['pnl'] for t in trades),
            'avg_pnl':round(total/len(trades),2),
            'avg_score':round(sum(t['score'] for t in trades)/len(trades),1),
            'max_win_streak':max_ws,'max_loss_streak':max_ls,
            'outcomes':by_out,'source':src,
            'data_quality':{'rows':len(df),'hl_avg':hl_avg},
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
    if df is None or len(df)<5: df=buffer_df(); src="buffer"
    if df is None or len(df)<3: return None,"Not enough data"
    df=add_ind(df)
    if len(df)<2: return None,"Too few candles"
    r0=df.iloc[-1]; r1=df.iloc[-2 if len(df)>=2 else -1]
    price=float(r0.get('close',0)); lp,_=get_nifty_price()
    if lp: price=lp
    df_d,_=get_data("1d",5,False); day_cpr=None
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
        'rsi':gv(r0,'rsi',1),'vwap':gv(r0,'vwap'),
        'atr':gv(r0,'atr'),'atr_rising':gv(r0,'atr')>gv(r1,'atr'),
        'adx':0,'volume':int(r0.get('volume',0)),'vol_ratio':gv(r0,'vr'),
        'cpr':day_cpr,
        'signals':{
            'call_ready':cp and in_win,'put_ready':pp and in_win,
            'call_score':cs,'put_score':ps,'min_score':3,
            'call_reasons':cr,'put_reasons':pr,
            'trading_window':in_win,'inside_cpr':inside_cpr,
            'call_trend':cr.get('ema_trend',False),'put_trend':pr.get('ema_trend',False),
            'call_cpr':cr.get('cpr',False),'put_cpr':pr.get('cpr',False),
            'atr_ok':cr.get('supertrend',False) or pr.get('supertrend',False),
            'volume_ok':cr.get('vwap',False) or pr.get('vwap',False),
        },
        'source':src,'data_source':data_source,
        'risk':{'sl_rs':SL_RS,'tp_rs':TP_RS,'ext_rs':EXT_RS},
    },None


# ══════════════════════════════════════════════════════════════
#  FORWARD TEST (real price tracking)
# ══════════════════════════════════════════════════════════════
def scan_for_trade():
    global last_signal,today_trades,today_pnl,sl_hit_today,capital,trade_log,active_trade
    ts=ist_now().strftime("%H:%M"); lp,_=get_nifty_price()
    if lp is None: last_signal=f"⚠️ No live price [{ts}]"; return
    if active_trade is not None: _monitor_trade(lp,ts); return
    if today_trades>=2: last_signal=f"⏹ Max trades [{ts}]"; return
    if sl_hit_today: last_signal=f"⛔ SL hit today [{ts}]"; return
    try:
        ind,err=get_indicators()
        if err or ind is None: last_signal=f"⚠️ {err}"; return
        s=ind['signals']
        if not s['trading_window']: last_signal=f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:         last_signal=f"⚠️ Inside CPR [{ts}]"; return
        if s['call_ready']:   _open_trade("CALL",lp,s['call_score'],ts)
        elif s['put_ready']:  _open_trade("PUT",lp,s['put_score'],ts)
        else: last_signal=f"⏳ Score {s['call_score']}/7 [{ts}]"
    except Exception as e: last_signal=f"Scan: {e}"

def _open_trade(side,price,score,ts):
    global active_trade,last_signal
    active_trade={'side':side,'entry':price,'entry_time':ts,'score':score,
                  'date':str(ist_now().date()),'tp_hit':False,'best_pts':0.0}
    last_signal=f"{'🟢CALL' if side=='CALL' else '🔴PUT'} OPENED @ ₹{price:.0f} Score:{score} [{ts}]"
    print(f"✅ Trade: {side} @ {price}")

def _monitor_trade(lp,ts):
    global active_trade,today_trades,today_pnl,sl_hit_today,capital,trade_log,last_signal
    t=active_trade; ep=t['entry']; side=t['side']
    pts=(lp-ep) if side=="CALL" else (ep-lp)
    pnl=int(pts*DELTA*LOT_SIZE); t['best_pts']=max(t['best_pts'],pts)
    if pts<=-SL_PTS:    _close_trade(-SL_RS,"SL HIT",ts); return
    if t['tp_hit']:
        if pts<t['best_pts']*0.5:
            _close_trade(max(TP_RS//2,int(t['best_pts']*0.6*DELTA*LOT_SIZE)),"TRAIL EXIT",ts); return
        if pts>=EXT_PTS:
            _close_trade(EXT_RS,"EXT TARGET 🚀",ts); return
    if not t['tp_hit'] and pts>=TP_PTS:
        t['tp_hit']=True; t['best_pts']=TP_PTS
        last_signal=f"🎯 TP ₹1500 HIT — trailing [{ts}]"; return
    now_t=ist_now().time()
    if (now_t>=datetime.time(11,15) and now_t<datetime.time(12,0)) or \
       (now_t>=datetime.time(14,45) and now_t<datetime.time(15,0)):
        _close_trade(max(-SL_RS,min(EXT_RS,pnl)),"WINDOW END",ts); return
    ps=f"+₹{pnl}" if pnl>=0 else f"₹{pnl}"
    last_signal=f"{'🟢' if side=='CALL' else '🔴'}{side}@₹{ep:.0f}|{ps}|{'🎯trail' if t['tp_hit'] else 'open'}[{ts}]"

def _close_trade(pnl,outcome,ts):
    global active_trade,today_trades,today_pnl,sl_hit_today,capital,trade_log,last_signal
    t=active_trade; capital+=pnl; today_pnl+=pnl; today_trades+=1
    if pnl<0: sl_hit_today=True; active_trade=None
    emoji="🚀" if pnl>=EXT_RS else ("✅" if pnl>0 else "❌")
    last_signal=f"{emoji}{t['side']} CLOSED|{outcome}|{'+'if pnl>=0 else ''}₹{pnl}[{ts}]"
    trade_log.insert(0,{'time':ts,'date':t['date'],'side':t['side'],
                         'entry':round(t['entry'],2),'pnl':pnl,'outcome':outcome,
                         'capital':round(capital,2),'score':t.get('score',0)})
    trade_log[:]=trade_log[:50]; active_trade=None
    print(f"{'✅' if pnl>0 else '❌'} {t['side']} {outcome} ₹{pnl}")


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
                if (is_trading_window() or active_trade) and not sl_hit_today and today_trades<2:
                    scan_for_trade()
            if t>datetime.time(15,30) and bot_active:
                if active_trade:
                    lp,_=get_nifty_price()
                    if lp:
                        pts=(lp-active_trade['entry']) if active_trade['side']=="CALL" else (active_trade['entry']-lp)
                        _close_trade(max(-SL_RS,min(EXT_RS,int(pts*DELTA*LOT_SIZE))),"MARKET CLOSE","15:30")
                bot_active=False; last_signal="⏰ Market closed"
        except Exception as e: print(f"❌ Scheduler: {e}")
        time.sleep(300)


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
        'active_trade':active_trade is not None,
        'risk':{'sl':SL_RS,'tp':TP_RS,'ext':EXT_RS,'sl_pts':round(SL_PTS,1),'tp_pts':round(TP_PTS,1)},
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
    at=None
    if active_trade:
        lp,_=get_nifty_price(); ep=active_trade['entry']; side=active_trade['side']
        move=(lp-ep) if side=="CALL" else (ep-lp) if lp else 0
        unrel=int(move*DELTA*LOT_SIZE) if lp else 0
        at={**active_trade,'live_price':lp,'move_pts':round(move,1),'unrealised':unrel,
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
    sample=[]; hl_avg=0; close_std=0
    if d15 is not None and len(d15)>0:
        hl_avg=round((d15['high']-d15['low']).mean(),2)
        close_std=round(d15['close'].std(),2)
        tmp=d15.tail(8).copy(); tmp['timestamp']=tmp['timestamp'].astype(str)
        sample=tmp[['timestamp','open','high','low','close','volume']].to_dict('records')
    return jsonify({
        'live_price':{'price':p,'source':ps},
        'data_source':data_source,'buffer_bars':len(candle_buffer),
        'risk':{'sl_rs':SL_RS,'tp_rs':TP_RS,'ext_rs':EXT_RS,
                'sl_pts':round(SL_PTS,1),'tp_pts':round(TP_PTS,1)},
        'dhan':{'ok':bool(DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV),
                '15m_rows':len(d15) if d15 is not None else 0,'15m_src':d15s,
                'avg_hl_pts':hl_avg,'close_std':close_std,
                '1d_rows':len(d1d) if d1d is not None else 0,'1d_src':d1ds,
                'last_8_bars':sample},
        'smartapi':{'logged_in':smart_obj is not None,
                    '15m_rows':len(s15) if s15 is not None else 0,'15m_src':s15s},
        'interpretation':{
            'hl_avg_ok': hl_avg>5,
            'hl_note': 'avg H-L should be >5 pts for NIFTY 15-min. If 0 or very low, data is compressed.',
            'close_std_ok': close_std>50,
            'close_std_note': 'close std should be >50 pts for 5 days of NIFTY data.',
        }
    })


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════
print("="*60)
print(f"🚀 NIFTY Bot | SL₹{SL_RS} TP₹{TP_RS} EXT₹{EXT_RS}")
print(f"   SL={round(SL_PTS,1)}pts TP={round(TP_PTS,1)}pts EXT={round(EXT_PTS,1)}pts")
print(f"   Dhan: {'✅' if (DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV) else '❌'}")
print("="*60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi,daemon=True).start()
threading.Thread(target=ltp_sampler,   daemon=True).start()
threading.Thread(target=scheduler_loop,daemon=True).start()

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)


# ══════════════════════════════════════════════════════════════
#  DIAGNOSTIC BACKTEST — shows exactly what is happening
# ══════════════════════════════════════════════════════════════
@app.route('/api/diagnose')
def api_diagnose():
    """
    Step-by-step diagnosis. Open this URL and share the result.
    It shows: data quality, indicator values, why each bar
    is rejected, and 10 sample entry/exit P&L calculations.
    """
    result = {}

    # Step 1: fetch data
    if data_source == "dhan":
        df, src = fetch_dhan("15m", 30)
    else:
        df, src = fetch_smartapi("15m", 30)

    result['step1_data'] = {
        'source': src,
        'rows':   len(df) if df is not None else 0,
        'ok':     df is not None and len(df) > 20,
    }

    if df is None or len(df) < 5:
        result['error'] = 'No data fetched'; return jsonify(result)

    # Step 2: data quality
    result['step2_quality'] = {
        'close_min':  round(float(df['close'].min()), 2),
        'close_max':  round(float(df['close'].max()), 2),
        'close_std':  round(float(df['close'].std()), 2),
        'avg_hl':     round(float((df['high']-df['low']).mean()), 2),
        'zero_hl_pct':round(float((df['high']==df['low']).mean()*100), 1),
        'sample_rows': df.tail(5)[['timestamp','open','high','low','close','volume']].assign(
            timestamp=lambda x: x['timestamp'].astype(str)).to_dict('records'),
    }

    # Step 3: indicators
    df = add_ind(df)
    result['step3_indicators'] = {
        'rows_after': len(df),
        'sample_indicators': df.tail(3)[['timestamp','close','e9','e21','rsi','sd']].assign(
            timestamp=lambda x: x['timestamp'].astype(str)).round(2).to_dict('records'),
    }

    # Step 4: CPR
    df['date'] = df['timestamp'].dt.date
    dates = sorted(df['date'].unique())
    cpr_samples = []
    for i in range(min(3, len(dates)-1), -1, -1):
        if i == 0: continue
        prev_d = df[df['date'] == dates[i-1]]
        if len(prev_d) == 0: continue
        cpr_ = calc_cpr(float(prev_d['high'].max()),
                        float(prev_d['low'].min()),
                        float(prev_d['close'].iloc[-1]))
        today_d = df[df['date'] == dates[i]]
        if len(today_d) == 0: continue
        price_ = float(today_d['close'].iloc[-1])
        cpr_samples.append({
            'date':       str(dates[i]),
            'cpr_top':    cpr_['cpr_top'],
            'cpr_bottom': cpr_['cpr_bottom'],
            'price':      price_,
            'above_top':  price_ > cpr_['cpr_top'],
            'below_bot':  price_ < cpr_['cpr_bottom'],
        })
    result['step4_cpr_samples'] = cpr_samples

    # Step 5: scan all bars in last 5 days for entries
    entry_log = []
    for i, date in enumerate(dates[-6:]):
        if i == 0: continue
        prev_d = df[df['date'] == dates[dates.index(date)-1]]
        if len(prev_d) == 0: continue
        day_cpr = calc_cpr(float(prev_d['high'].max()),
                           float(prev_d['low'].min()),
                           float(prev_d['close'].iloc[-1]))
        today_d = df[df['date'] == date].reset_index(drop=True)
        for idx in range(1, len(today_d)):
            t = today_d.iloc[idx]['timestamp'].time()
            in_win = (datetime.time(10,0)<=t<=datetime.time(11,15) or
                      datetime.time(13,45)<=t<=datetime.time(14,45))
            cp, cs, cr = check_entry(today_d, idx, day_cpr, True)
            pp, ps, pr = check_entry(today_d, idx, day_cpr, False)
            r_ = today_d.iloc[idx]
            entry_log.append({
                'date':       str(date),
                'time':       str(t)[:5],
                'in_window':  in_win,
                'price':      round(float(r_.get('close',0)),1),
                'e9':         round(float(r_.get('e9',0)),1),
                'e21':        round(float(r_.get('e21',0)),1),
                'rsi':        round(float(r_.get('rsi',50)),1),
                'sd':         int(r_.get('sd',0)),
                'cpr_top':    day_cpr['cpr_top'],
                'call_pass':  cp, 'call_score': cs,
                'put_pass':   pp, 'put_score':  ps,
                'call_fail_reasons': [k for k,v in cr.items() if not v],
                'put_fail_reasons':  [k for k,v in pr.items() if not v],
            })

    result['step5_entry_scan_last5days'] = {
        'total_bars_scanned': len(entry_log),
        'in_window':          sum(1 for e in entry_log if e['in_window']),
        'call_passes':        sum(1 for e in entry_log if e['call_pass']),
        'put_passes':         sum(1 for e in entry_log if e['put_pass']),
        'most_common_call_fails': _count_fails([e['call_fail_reasons'] for e in entry_log]),
        'most_common_put_fails':  _count_fails([e['put_fail_reasons']  for e in entry_log]),
        'sample_bars_in_window': [e for e in entry_log if e['in_window']][:10],
    }

    # Step 6: test exit on 5 real entries (forced, ignore filters)
    exit_tests = []
    forced = 0
    for i, date in enumerate(dates[-6:]):
        if i == 0 or forced >= 5: break
        prev_d = df[df['date'] == dates[dates.index(date)-1]]
        if len(prev_d) == 0: continue
        day_cpr = calc_cpr(float(prev_d['high'].max()),
                           float(prev_d['low'].min()),
                           float(prev_d['close'].iloc[-1]))
        today_d = df[df['date'] == date].reset_index(drop=True)
        for idx in range(1, len(today_d)):
            t = today_d.iloc[idx]['timestamp'].time()
            if not (datetime.time(10,0)<=t<=datetime.time(11,15) or
                    datetime.time(13,45)<=t<=datetime.time(14,45)): continue
            entry_ = float(today_d.iloc[idx]['close'])
            pnl_c, out_c = exit_trade(today_d, idx, "CALL")
            pnl_p, out_p = exit_trade(today_d, idx, "PUT")
            # Show next 5 closes
            next_closes = [round(float(today_d.iloc[min(idx+j,len(today_d)-1)]['close']),1)
                           for j in range(1, min(6, len(today_d)-idx))]
            exit_tests.append({
                'date': str(date), 'time': str(t)[:5],
                'entry': round(entry_, 1),
                'next_5_closes': next_closes,
                'call_pnl': pnl_c, 'call_outcome': out_c,
                'put_pnl':  pnl_p, 'put_outcome':  out_p,
            })
            forced += 1
            break

    result['step6_forced_exit_tests'] = {
        'note': 'These are forced entries (ignoring filters) to test if exit engine works',
        'tp_pts': round(TP_PTS, 1), 'sl_pts': round(SL_PTS, 1),
        'tests': exit_tests,
    }

    return jsonify(result)


def _count_fails(fail_lists):
    counts = {}
    for lst in fail_lists:
        for item in lst:
            counts[item] = counts.get(item, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])[:5]
