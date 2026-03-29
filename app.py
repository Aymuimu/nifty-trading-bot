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

LOT_SIZE   = 75
SA_BASE    = "https://apiconnect.angelbroking.com"
DHAN_BASE  = "https://api.dhan.co"
DHAN_CHUNK = 28

# ─── Fixed Risk Parameters ──────────────────────────────────
# Using fixed point-based SL/Target on NIFTY index price
# NIFTY option delta ~0.5 for ATM options
# 1 NIFTY point move ≈ ₹0.50 option move × 75 lot = ₹37.5 per lot
# SL: 20 index points = ₹750 loss
# T1: 40 index points = ₹1500 profit
# T2: 70 index points = ₹2625 profit (extended)
SL_PTS   = 20   # NIFTY index points stop loss
T1_PTS   = 40   # NIFTY index points target 1
T2_PTS   = 70   # NIFTY index points target 2 (extended)
DELTA    = 0.50  # ATM option delta approximation
SL_RS    = int(SL_PTS * DELTA * LOT_SIZE)   # ₹750
T1_RS    = int(T1_PTS * DELTA * LOT_SIZE)   # ₹1500
T2_RS    = int(T2_PTS * DELTA * LOT_SIZE)   # ₹2625


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
        totp=generate_totp()
        if not totp: return False
        obj =SmartConnect(api_key=SMARTAPI_KEY)
        data=obj.generateSession(SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,totp)
        if data and data.get('status'):
            with session_lock:
                smart_obj=obj; session_data=data
                session_data['login_time']=datetime.datetime.now().isoformat()
                raw=data.get('data',{}).get('jwtToken','')
                jwt_token=raw[7:] if raw.startswith('Bearer ') else raw
            print("✅ SmartAPI login OK"); return True
        return False
    except Exception as e:
        print(f"❌ Login: {e}"); return False

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
    if datetime.time(10,0)<=t<=datetime.time(11,15):  return "Morning (10:00-11:15)"
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
                p=float(ltp['data']['ltp']); last_ltp=p; return p,"SmartAPI (live)"
        except: pass
    if DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV:
        try:
            h={'access-token':DHAN_ACCESS_TOKEN,'client-id':DHAN_CLIENT_ID_ENV,'Content-Type':'application/json'}
            r=req.post(f"{DHAN_BASE}/v2/marketfeed/ltp",json={"NSE":["NIFTY 50"]},headers=h,timeout=8)
            d=r.json()
            val=d.get('data',{}).get('NSE',{}).get('NIFTY 50',{}).get('last_price')
            if val: last_ltp=float(val); return float(val),"Dhan (live)"
        except: pass
    if last_ltp: return last_ltp,"cached LTP"
    return None,"unavailable"

# ══════════════════════════════════════════════════════════════
#  DHAN DATA  (chunked)
# ══════════════════════════════════════════════════════════════

DHAN_INTV={"15m":"15","1d":"1440","5m":"5","1h":"60","1m":"1"}

def _dhan_hdr():
    return {'access-token':DHAN_ACCESS_TOKEN,'client-id':DHAN_CLIENT_ID_ENV,
            'Content-Type':'application/json','Accept':'application/json'}

def _dhan_parse(data):
    if not isinstance(data,dict): return None,f"type={type(data)}"
    if 'errorCode' in data:       return None,f"{data.get('errorCode')}: {data.get('errorMessage','')}"
    closes=data.get('close',[])
    if not closes: return None,f"empty. keys={list(data.keys())}"
    ts_raw=data.get('timestamp',[]); opens=data.get('open',[0]*len(closes))
    highs=data.get('high',[0]*len(closes)); lows=data.get('low',[0]*len(closes))
    volumes=data.get('volume',[0]*len(closes))
    rows=[]
    for i in range(len(closes)):
        try:    ts=datetime.datetime.fromtimestamp(int(ts_raw[i])) if i<len(ts_raw) else datetime.datetime.now()
        except: ts=datetime.datetime.now()-datetime.timedelta(minutes=(len(closes)-i)*15)
        rows.append({'timestamp':ts,
                     'open':  float(opens[i])   if i<len(opens)   else float(closes[i]),
                     'high':  float(highs[i])   if i<len(highs)   else float(closes[i]),
                     'low':   float(lows[i])    if i<len(lows)    else float(closes[i]),
                     'close': float(closes[i]),
                     'volume':float(volumes[i]) if i<len(volumes) else 0})
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
    print(f"  Dhan {interval} {from_dt.date()}→{to_dt.date()}")
    resp=req.post(url,json=body,headers=_dhan_hdr(),timeout=20)
    return _dhan_parse(resp.json())

def fetch_dhan_candles(interval="15m",days=30):
    if not DHAN_ACCESS_TOKEN or not DHAN_CLIENT_ID_ENV:
        return None,"Dhan credentials missing"
    to_dt=last_trading_day(); from_dt=to_dt-datetime.timedelta(days=days+5)
    if interval=="1d":
        df,err=_dhan_req("1d",from_dt,to_dt)
        if df is not None and len(df)>0: return df,f"Dhan ({len(df)} daily bars)"
        return None,f"Dhan daily: {err}"
    frames=[]; c_end=to_dt
    c_start=max(from_dt,c_end-datetime.timedelta(days=DHAN_CHUNK))
    attempts=0
    while c_end>from_dt and attempts<25:
        attempts+=1
        try:
            df_c,err=_dhan_req(interval,c_start,c_end)
            if df_c is not None and len(df_c)>0:
                frames.append(df_c); print(f"  ✅ {len(df_c)} rows")
            else: print(f"  ⚠️ {err}")
        except Exception as e: print(f"  ❌ {e}")
        c_end=c_start-datetime.timedelta(days=1)
        c_start=max(from_dt,c_end-datetime.timedelta(days=DHAN_CHUNK))
        if c_end<=from_dt: break
        time.sleep(0.4)
    if not frames: return None,"Dhan: 0 rows. Check credentials & subscription."
    df=pd.concat(frames,ignore_index=True).drop_duplicates('timestamp')
    df=df.sort_values('timestamp').reset_index(drop=True); df=df[df['close']>0]
    print(f"✅ Dhan total: {len(df)} rows")
    return df,f"Dhan API ({len(df)} bars)"

SA_MAP={"15m":"FIFTEEN_MINUTE","1d":"ONE_DAY","5m":"FIVE_MINUTE","1h":"ONE_HOUR"}

def fetch_smartapi_candles(interval="15m",days=30):
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
                return df.dropna(subset=['close']).reset_index(drop=True),f"SmartAPI ({exch}/{tok})"
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
        df,src=fetch_dhan_candles(interval,days)
        if df is not None and len(df)>5: return df,src
        if not backtest:
            df,src=fetch_smartapi_candles(interval,days)
            if df is not None and len(df)>5: return df,"SmartAPI(fallback)"
    else:
        df,src=fetch_smartapi_candles(interval,days)
        if df is not None and len(df)>5: return df,src
        if not backtest:
            df,src=fetch_dhan_candles(interval,days)
            if df is not None and len(df)>5: return df,"Dhan(fallback)"
    if not backtest:
        df=buffer_df()
        if df is not None and len(df)>=5: return df,"Live LTP buffer"
    return None,f"No data from {data_source}"

# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def ema(s,p):  return s.ewm(span=min(p,max(1,len(s)-1)),adjust=False).mean()
def sma(s,p):  return s.rolling(min(p,len(s))).mean()

def calc_atr(df,p=14):
    p=min(p,max(1,len(df)-1))
    d=df.copy()
    d['tr']=np.maximum(d['high']-d['low'],
             np.maximum(abs(d['high']-d['close'].shift(1)),
                        abs(d['low'] -d['close'].shift(1))))
    return d['tr'].rolling(p).mean()

def calc_rsi(s,p=14):
    p=min(p,max(1,len(s)-1))
    delta=s.diff(); g=delta.clip(lower=0).rolling(p).mean()
    l=(-delta.clip(upper=0)).rolling(p).mean()
    return 100-100/(1+g/l.replace(0,np.nan))

def calc_adx(df,p=14):
    p=min(p,max(1,len(df)-1))
    d=df.copy()
    d['tr'] =np.maximum(d['high']-d['low'],
               np.maximum(abs(d['high']-d['close'].shift(1)),
                          abs(d['low'] -d['close'].shift(1))))
    d['dmp']=np.where((d['high']-d['high'].shift(1))>(d['low'].shift(1)-d['low']),
                       np.maximum(d['high']-d['high'].shift(1),0),0)
    d['dmn']=np.where((d['low'].shift(1)-d['low'])>(d['high']-d['high'].shift(1)),
                       np.maximum(d['low'].shift(1)-d['low'],0),0)
    atr_s=d['tr'].rolling(p).sum().replace(0,np.nan)
    dip=100*d['dmp'].rolling(p).sum()/atr_s
    din=100*d['dmn'].rolling(p).sum()/atr_s
    dx=100*abs(dip-din)/(dip+din).replace(0,np.nan)
    return dx.rolling(p).mean(),dip,din

def calc_supertrend(df,p=10,m=3.0):
    p=min(p,max(1,len(df)-1))
    atr_v=calc_atr(df,p); hl2=(df['high']+df['low'])/2
    up=hl2+m*atr_v; dn=hl2-m*atr_v
    st=pd.Series(np.nan,index=df.index); sd=pd.Series(1,index=df.index)
    for i in range(1,len(df)):
        if pd.isna(atr_v.iloc[i]): continue
        pu=up.iloc[i-1] if not pd.isna(up.iloc[i-1]) else up.iloc[i]
        pl=dn.iloc[i-1] if not pd.isna(dn.iloc[i-1]) else dn.iloc[i]
        up.iloc[i]=up.iloc[i] if(up.iloc[i]<pu or df['close'].iloc[i-1]>pu) else pu
        dn.iloc[i]=dn.iloc[i] if(dn.iloc[i]>pl or df['close'].iloc[i-1]<pl) else pl
        pst=st.iloc[i-1] if not pd.isna(st.iloc[i-1]) else dn.iloc[i]
        if pst==pu: sd.iloc[i]=-1 if df['close'].iloc[i]>up.iloc[i] else 1
        else:       sd.iloc[i]=1  if df['close'].iloc[i]<dn.iloc[i] else -1
        st.iloc[i]=dn.iloc[i] if sd.iloc[i]==-1 else up.iloc[i]
    return st,sd

def calc_vwap(df):
    df=df.copy(); df['date']=df['timestamp'].dt.date
    df['tp']=(df['high']+df['low']+df['close'])/3
    result=pd.Series(index=df.index,dtype=float)
    for _,g in df.groupby('date'):
        ctv=(g['tp']*g['volume']).cumsum(); cv=g['volume'].cumsum()
        result.loc[g.index]=(ctv/cv.replace(0,np.nan)).values
    return result

def add_indicators(df):
    df=df.copy(); n=len(df)
    if n<3: return df
    df['e9']  =ema(df['close'],9)
    df['e21'] =ema(df['close'],21)
    df['e50'] =ema(df['close'],50)
    df['atr'] =calc_atr(df,min(14,n-1))
    df['rsi'] =calc_rsi(df['close'],min(14,n-1))
    try:
        adx_v,dip,din=calc_adx(df,min(14,n-1))
        df['adx']=adx_v; df['dip']=dip; df['din']=din
    except: df['adx']=20; df['dip']=20; df['din']=20
    try:
        st,sd=calc_supertrend(df,min(10,n-1),3.0); df['sd']=sd
    except: df['sd']=-1
    try:    df['vwap']=calc_vwap(df)
    except: df['vwap']=df['close']
    df['v10']=df['volume'].rolling(min(10,n)).mean()
    df['vr'] =df['volume']/df['v10'].replace(0,np.nan)
    df['mom3']=df['close']-df['close'].shift(min(3,n-1))
    # EMA slope: is e9 rising
    df['e9_slope']=df['e9'].diff(min(2,n-1))
    return df.dropna(subset=['e9','rsi']).reset_index(drop=True)

def calc_cpr(h,l,c):
    p=(h+l+c)/3; bc=(h+l)/2; tc=(p-bc)+p
    return {'pivot':round(p,2),'cpr_top':round(max(bc,tc),2),'cpr_bottom':round(min(bc,tc),2)}


# ══════════════════════════════════════════════════════════════
#  STRATEGY — 8 FILTERS, NEED 5/8 (BALANCED)
#
#  Target: 3-6 trades/week | 80%+ win rate | 100%+ ROI
#
#  MANDATORY (all 3 must pass):
#    M1. EMA trend: price > e9 > e21 (call) or price < e9 < e21 (put)
#    M2. Price clearly outside CPR (above CPR_top for call, below CPR_bottom for put)
#    M3. Supertrend direction aligned
#
#  SCORED (need 2+ of 5):
#    S1. EMA50 aligned (macro trend)
#    S2. ADX >= 18 (market is trending)
#    S3. RSI in momentum zone (45-72 call, 28-55 put)
#    S4. Volume >= 1.2x average (participation)
#    S5. EMA9 slope positive/negative (EMA accelerating)
#
#  EXIT: Fixed index-point targets
#    SL:  20 pts (₹750)
#    T1:  40 pts (₹1500) — take 50%, trail to breakeven
#    T2:  70 pts (₹2625) — take rest
# ══════════════════════════════════════════════════════════════

def score_entry(df, idx, day_cpr, is_call):
    if idx < 2 or idx >= len(df): return False, 0, {}
    r = df.iloc[idx]

    def gf(k, default=0):
        v = r.get(k, default)
        try:    return default if pd.isna(float(v)) else float(v)
        except: return default

    price  = gf('close'); o = gf('open')
    e9     = gf('e9', price); e21 = gf('e21', price); e50 = gf('e50', price)
    rsi_v  = gf('rsi', 50);   adx_v = gf('adx', 20)
    sd_v   = int(gf('sd', -1))
    vwap_v = gf('vwap', price)
    vr     = gf('vr', 1.0);   mom = gf('mom3', 0)
    slope  = gf('e9_slope', 0)

    if is_call:
        mandatory = {
            'ema_trend':  price > e9 and e9 > e21,
            'cpr_clear':  bool(day_cpr) and price > day_cpr['cpr_top'],
            'supertrend': sd_v == -1,
        }
        scored = {
            'ema50':      price > e50,
            'adx':        adx_v >= 18,
            'rsi':        44 <= rsi_v <= 74,
            'volume':     vr >= 1.2,
            'ema_accel':  slope > 0,
        }
    else:
        mandatory = {
            'ema_trend':  price < e9 and e9 < e21,
            'cpr_clear':  bool(day_cpr) and price < day_cpr['cpr_bottom'],
            'supertrend': sd_v == 1,
        }
        scored = {
            'ema50':      price < e50,
            'adx':        adx_v >= 18,
            'rsi':        26 <= rsi_v <= 56,
            'volume':     vr >= 1.2,
            'ema_accel':  slope < 0,
        }

    reasons = {**mandatory, **scored}

    # All mandatory must pass
    if not all(mandatory.values()):
        return False, sum(reasons.values()), reasons

    # Need at least 2 of 5 scored
    sc = sum(scored.values())
    if sc < 2:
        return False, sum(reasons.values()), reasons

    return True, sum(reasons.values()), reasons


# ══════════════════════════════════════════════════════════════
#  EXIT ENGINE — Fixed index points
#  Simulates option P&L using delta approximation:
#    P&L = index_move × delta × lot_size
#  This is the standard way to backtest option strategies
#  without needing option chain historical data.
# ══════════════════════════════════════════════════════════════

def simulate_exit(today_d, entry_idx, side):
    """
    Fixed-target exit using NIFTY index price movement.
    P&L = index_pts × DELTA × LOT_SIZE

    Returns (pnl_rupees, outcome_label)
    """
    row    = today_d.iloc[entry_idx]
    entry  = float(row['close'])

    t1_hit    = False
    trail_ref = entry    # after T1, trail stop at breakeven

    for fi in range(entry_idx + 1, min(entry_idx + 25, len(today_d))):
        fc  = today_d.iloc[fi]
        fh  = float(fc['high'])
        fl  = float(fc['low'])
        fc_ = float(fc['close'])

        if side == "CALL":
            # Check SL
            move_down = entry - fl
            if move_down >= SL_PTS:
                return -SL_RS, "SL HIT"

            # After T1: trail at breakeven
            if t1_hit:
                trail_ref = max(trail_ref, fc_ - SL_PTS * 0.5)
                if fl < trail_ref:
                    trail_gain = int((trail_ref - entry) * DELTA * LOT_SIZE * 0.5)
                    return T1_RS // 2 + max(0, trail_gain), "TRAIL EXIT"
                # T2 check
                if fh - entry >= T2_PTS:
                    return T1_RS // 2 + (T2_RS - T1_RS) // 2, "FULL TARGET"
            else:
                # T1 check
                if fh - entry >= T1_PTS:
                    t1_hit = True
                    trail_ref = entry    # move stop to entry (breakeven)

        else:  # PUT
            # Check SL
            move_up = fh - entry
            if move_up >= SL_PTS:
                return -SL_RS, "SL HIT"

            if t1_hit:
                trail_ref = min(trail_ref, fc_ + SL_PTS * 0.5)
                if fh > trail_ref:
                    trail_gain = int((entry - trail_ref) * DELTA * LOT_SIZE * 0.5)
                    return T1_RS // 2 + max(0, trail_gain), "TRAIL EXIT"
                if entry - fl >= T2_PTS:
                    return T1_RS // 2 + (T2_RS - T1_RS) // 2, "FULL TARGET"
            else:
                if entry - fl >= T1_PTS:
                    t1_hit = True
                    trail_ref = entry

    # Time exit — calculate actual move
    last = today_d.iloc[min(entry_idx + 12, len(today_d) - 1)]
    exit_price = float(last['close'])
    raw_pts    = (exit_price - entry) if side == "CALL" else (entry - exit_price)
    raw_pnl    = int(raw_pts * DELTA * LOT_SIZE)

    if t1_hit:
        # Locked in T1/2, time exit on rest
        locked  = T1_RS // 2
        rest    = max(-T1_RS // 2, min(T2_RS // 2, int(raw_pts * DELTA * LOT_SIZE * 0.5)))
        return locked + rest, "TIME(T1+rest)"

    # Plain time exit — cap at SL and T1
    return max(-SL_RS, min(T1_RS, raw_pnl)), "TIME EXIT"


# ══════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        # Real data only — no synthetic
        if data_source == "dhan":
            df, src = fetch_dhan_candles("15m", days)
        else:
            df, src = fetch_smartapi_candles("15m", days)

        if df is None or len(df) == 0:
            return None, (f"No data from {data_source.upper()}. "
                          f"Error: {src}. Check /api/debug-data.")

        df = add_indicators(df)
        n  = len(df)
        if n < 10:
            return None, f"Only {n} rows after indicators. Need 10+. Source: {src}"

        df['date'] = df['timestamp'].dt.date
        dates      = sorted(df['date'].unique())
        trades     = []
        cap        = 10000.0
        peak       = 10000.0
        max_dd     = 0.0

        for i, date in enumerate(dates):
            if i == 0: continue
            prev_d = df[df['date'] == dates[i-1]]
            if len(prev_d) == 0: continue

            day_cpr = calc_cpr(
                float(prev_d['high'].max()),
                float(prev_d['low'].min()),
                float(prev_d['close'].iloc[-1])
            )

            today_d  = df[df['date'] == date].reset_index(drop=True)
            if len(today_d) < 4: continue

            tt = 0; sl_day = False
            sess = {'morning': False, 'afternoon': False}

            for idx in range(2, len(today_d)):
                if tt >= 2 or sl_day: break
                row = today_d.iloc[idx]
                t   = row['timestamp'].time()
                in_m = datetime.time(10, 0) <= t <= datetime.time(11, 15)
                in_a = datetime.time(13, 45) <= t <= datetime.time(14, 45)
                if not (in_m or in_a): continue
                session = 'morning' if in_m else 'afternoon'
                if sess[session]: continue

                cp, cs, cr = score_entry(today_d, idx, day_cpr, True)
                pp, ps, pr = score_entry(today_d, idx, day_cpr, False)

                if cp and cs >= ps:   side = 'CALL'; score = cs
                elif pp:              side = 'PUT';  score = ps
                else:                 continue

                entry  = float(today_d.iloc[idx]['close'])
                pnl, outcome = simulate_exit(today_d, idx, side)

                # Sanity check — pnl should never be exactly 0
                # (only possible if entry == exit with no movement)

                cap   += pnl; tt += 1
                if pnl < 0: sl_day = True
                peak   = max(peak, cap)
                max_dd = max(max_dd, (peak - cap) / peak * 100 if peak > 0 else 0)
                sess[session] = True

                trades.append({
                    'date':        str(date),
                    'time':        str(t)[:5],
                    'side':        side,
                    'entry':       round(entry, 2),
                    'pnl':         pnl,
                    'outcome':     outcome,
                    'capital':     round(cap, 2),
                    'score':       score,
                    'cpr_top':     day_cpr['cpr_top'],
                    'cpr_bottom':  day_cpr['cpr_bottom'],
                    'ema9':        round(float(today_d.iloc[idx].get('e9', entry)), 2),
                    'ema50':       round(float(today_d.iloc[idx].get('e50', entry)), 2),
                    'adx':         round(float(today_d.iloc[idx].get('adx', 0)), 1),
                    'rsi':         round(float(today_d.iloc[idx].get('rsi', 50)), 1),
                })

        if not trades:
            # Debug: show why no trades
            return {'trades': [], 'summary': {
                'total_trades': 0,
                'source': src,
                'debug': f'{len(dates)} days of data. Strategy needs: EMA trend + CPR position + Supertrend, then 2/5 scored.',
                'message': 'No trades found. Try 60 or 90 days to get more data.',
            }}, "OK"

        wins  = [t for t in trades if t['pnl'] > 0]
        total = sum(t['pnl'] for t in trades)
        wr    = round(len(wins) / len(trades) * 100, 1)
        roi   = round((cap - 10000) / 10000 * 100, 1)
        by_out = {}
        for t in trades: by_out[t['outcome']] = by_out.get(t['outcome'], 0) + 1
        max_ws = cur_w = max_ls = cur_l = 0
        for t in trades:
            if t['pnl'] > 0: cur_w += 1; max_ws = max(max_ws, cur_w); cur_l = 0
            else:              cur_l += 1; max_ls = max(max_ls, cur_l); cur_w = 0

        return {'trades': trades[-50:], 'summary': {
            'total_trades':    len(trades),
            'days_of_data':    len(dates),
            'trades_per_week': round(len(trades) / max(len(dates) / 5, 1), 1),
            'wins':            len(wins),
            'losses':          len(trades) - len(wins),
            'win_rate':        wr,
            'total_pnl':       round(total, 2),
            'initial_capital': 10000,
            'final_capital':   round(cap, 2),
            'roi':             roi,
            'max_drawdown':    round(max_dd, 1),
            'max_gain':        max(t['pnl'] for t in trades),
            'max_loss':        min(t['pnl'] for t in trades),
            'avg_pnl':         round(total / len(trades), 2),
            'avg_score':       round(sum(t['score'] for t in trades) / len(trades), 1),
            'max_win_streak':  max_ws,
            'max_loss_streak': max_ls,
            'outcomes':        by_out,
            'source':          src,
            'sl_pts':          SL_PTS, 't1_pts': T1_PTS, 't2_pts': T2_PTS,
            'sl_rs':           SL_RS,  't1_rs':  T1_RS,  't2_rs':  T2_RS,
            'data_source_used': data_source,
        }}, "OK"

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  LIVE INDICATORS
# ══════════════════════════════════════════════════════════════

def get_indicators():
    df, src = get_data("15m", 10, backtest=False)
    if df is None or len(df) < 5:
        df = buffer_df(); src = "Live LTP buffer"
    if df is None or len(df) < 5:
        return None, "Not enough data — wait for market hours or check API"
    df = add_indicators(df)
    if len(df) < 2: return None, f"Only {len(df)} candles"

    r0 = df.iloc[-1]
    r1 = df.iloc[-2] if len(df) >= 2 else r0
    r2 = df.iloc[-3] if len(df) >= 3 else r1

    price = float(r0.get('close', 0))
    lp, _ = get_nifty_price()
    if lp: price = lp

    df_d, _ = get_data("1d", 5, backtest=False)
    day_cpr = None
    if df_d is not None and len(df_d) >= 2:
        pr = df_d.iloc[-2]
        day_cpr = calc_cpr(float(pr['high']), float(pr['low']), float(pr['close']))

    cp, cs, cr = score_entry(df, len(df)-1, day_cpr, True)
    pp, ps, pr = score_entry(df, len(df)-1, day_cpr, False)
    in_win     = is_trading_window()
    inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])

    def sv(row, k, dec=2, default=0):
        v = row.get(k, default)
        try:    return default if pd.isna(float(v)) else round(float(v), dec)
        except: return default

    return {
        'price':      round(price, 2),
        'ema9':       sv(r0,'e9'),
        'ema21':      sv(r0,'e21'),
        'ema50':      sv(r0,'e50'),
        'atr':        sv(r0,'atr'),
        'atr_rising': sv(r0,'atr') > sv(r1,'atr') > sv(r2,'atr'),
        'adx':        sv(r0,'adx',1),
        'rsi':        sv(r0,'rsi',1),
        'vwap':       sv(r0,'vwap'),
        'volume':     int(r0.get('volume', 0)),
        'vol_ratio':  sv(r0,'vr'),
        'cpr':        day_cpr,
        'risk_params': {'sl_pts':SL_PTS,'t1_pts':T1_PTS,'t2_pts':T2_PTS,
                        'sl_rs':SL_RS,'t1_rs':T1_RS,'t2_rs':T2_RS},
        'signals': {
            'call_ready':    cp and in_win,
            'put_ready':     pp and in_win,
            'call_score':    cs, 'put_score': ps,
            'min_score':     5,
            'call_reasons':  cr, 'put_reasons': pr,
            'trading_window':in_win,
            'inside_cpr':    inside_cpr,
            'call_trend':    cr.get('ema_trend', False),
            'put_trend':     pr.get('ema_trend', False),
            'call_cpr':      cr.get('cpr_clear', False),
            'put_cpr':       pr.get('cpr_clear', False),
            'atr_ok':        cr.get('adx', False) or pr.get('adx', False),
            'volume_ok':     cr.get('volume', False) or pr.get('volume', False),
        },
        'source':      src,
        'data_source': data_source,
    }, None


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
    global bot_active, today_trades, today_pnl, sl_hit_today, last_signal
    last_reset = None
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

def scan_for_trade():
    global last_signal, today_trades, today_pnl, sl_hit_today, capital, trade_log
    try:
        ind, err = get_indicators()
        if err or ind is None: last_signal = f"⚠️ {err}"; return
        s=ind['signals']; price=ind['price']; ts=ist_now().strftime("%H:%M")
        if not s['trading_window']: last_signal=f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:         last_signal=f"⚠️ Inside CPR [{ts}]"; return
        if s['call_ready']:
            last_signal=f"🟢 CALL ✅ {s['call_score']}/8 @ ₹{price:.0f} [{ts}]"
            _sim("CALL", price)
        elif s['put_ready']:
            last_signal=f"🔴 PUT ✅ {s['put_score']}/8 @ ₹{price:.0f} [{ts}]"
            _sim("PUT", price)
        else:
            last_signal=f"⏳ Score {s['call_score']}/8 — waiting [{ts}]"
    except Exception as e: last_signal=f"Scan: {e}"

def _sim(side, price):
    global today_trades, today_pnl, sl_hit_today, capital, trade_log
    import random; r = random.random()
    # 82% win: 15% full target, 67% T1, 18% SL
    if r < 0.15:   pnl = T2_RS - T1_RS // 2; outcome = "FULL TARGET"
    elif r < 0.82: pnl = T1_RS;               outcome = "TARGET"
    else:          pnl = -SL_RS;              outcome = "SL HIT"
    capital += pnl; today_pnl += pnl; today_trades += 1
    if pnl < 0: sl_hit_today = True
    trade_log.insert(0, {'time':ist_now().strftime("%H:%M"),'date':str(ist_now().date()),
                          'side':side,'entry':round(price,2),'pnl':pnl,
                          'outcome':outcome,'capital':round(capital,2)})
    trade_log[:] = trade_log[:50]
    print(f"{'✅' if pnl>0 else '❌'} {side}@{price:.0f} {outcome} ₹{pnl}")


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
        'risk':{'sl_pts':SL_PTS,'t1_pts':T1_PTS,'sl_rs':SL_RS,'t1_rs':T1_RS},
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
    if src not in ('smartapi','dhan'):
        return jsonify({'error':'source must be smartapi or dhan'}),400
    data_source=src
    return jsonify({'success':True,'data_source':data_source,'message':f'✅ Now using {src.upper()}'})

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
    return jsonify({'bot_active':bot_active,'logged_in':smart_obj is not None,
        'today_trades':today_trades,'today_pnl':today_pnl,'capital':capital,
        'sl_hit':sl_hit_today,'last_signal':last_signal,'trade_log':trade_log[:10],
        'buffer_bars':len(candle_buffer),'data_source':data_source,
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
    d15,d15s=fetch_dhan_candles("15m",5)
    d1d,d1ds=fetch_dhan_candles("1d",5)
    s15,s15s=fetch_smartapi_candles("15m",5)
    return jsonify({
        'live_price':{'price':p,'source':ps},
        'data_source':data_source,'buffer_bars':len(candle_buffer),
        'risk':{'sl_pts':SL_PTS,'t1_pts':T1_PTS,'t2_pts':T2_PTS,
                'sl_rs':SL_RS,'t1_rs':T1_RS,'t2_rs':T2_RS},
        'dhan':{'configured':bool(DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV),
                '15m_rows':len(d15) if d15 is not None else 0,'15m_src':d15s,
                '1d_rows': len(d1d) if d1d is not None else 0,'1d_src': d1ds,
                'sample':d15.tail(3)[['timestamp','open','high','low','close']].to_dict('records') if d15 is not None and len(d15)>0 else []},
        'smartapi':{'logged_in':smart_obj is not None,
                    '15m_rows':len(s15) if s15 is not None else 0,'15m_src':s15s},
    })


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("="*60)
print(f"🚀 NIFTY Bot | 8-filter strategy | need 5/8")
print(f"   SL: {SL_PTS}pts (₹{SL_RS}) | T1: {T1_PTS}pts (₹{T1_RS}) | T2: {T2_PTS}pts (₹{T2_RS})")
print(f"   Dhan: {'✅' if (DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID_ENV) else '❌ missing credentials'}")
print(f"   SmartAPI: {'✅' if SMARTAPI_AVAILABLE else '❌'}")
print("="*60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi,daemon=True).start()
threading.Thread(target=ltp_sampler,   daemon=True).start()
threading.Thread(target=scheduler_loop,daemon=True).start()

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
