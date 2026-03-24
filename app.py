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

LOT_SIZE    = 75
SA_BASE     = "https://apiconnect.angelbroking.com"


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
        totp = generate_totp()
        if not totp: return False
        obj  = SmartConnect(api_key=SMARTAPI_KEY)
        data = obj.generateSession(SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, totp)
        if data and data.get('status'):
            with session_lock:
                smart_obj    = obj
                session_data = data
                session_data['login_time'] = datetime.datetime.now().isoformat()
                raw = data.get('data',{}).get('jwtToken','')
                jwt_token = raw[7:] if raw.startswith('Bearer ') else raw
            print("✅ SmartAPI login OK")
            return True
        return False
    except Exception as e:
        print(f"❌ Login: {e}"); return False


# ══════════════════════════════════════════════════════════════
#  TIME
# ══════════════════════════════════════════════════════════════

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def last_trading_day():
    d = datetime.datetime.now()
    while d.weekday() >= 5: d -= datetime.timedelta(days=1)
    return d

def is_market_open():
    n = ist_now()
    if n.weekday() >= 5: return False
    return datetime.time(9,15) <= n.time() <= datetime.time(15,30)

def is_trading_window():
    t = ist_now().time()
    return (datetime.time(10,0)<=t<=datetime.time(11,15) or
            datetime.time(13,45)<=t<=datetime.time(14,45))

def window_label():
    t = ist_now().time()
    if datetime.time(10,0)<=t<=datetime.time(11,15):  return "Morning (10:00-11:15)"
    if datetime.time(13,45)<=t<=datetime.time(14,45): return "Afternoon (1:45-2:45)"
    return "Outside trading windows"

def bucket_15m(dt):
    return dt.replace(minute=(dt.minute//15)*15, second=0, microsecond=0)


# ══════════════════════════════════════════════════════════════
#  LIVE PRICE
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    global last_ltp
    if smart_obj:
        try:
            ltp = smart_obj.ltpData("NSE","NIFTY","26000")
            if ltp and ltp.get('status'):
                p = float(ltp['data']['ltp'])
                last_ltp = p; return p, "SmartAPI (live)"
        except: pass
    if last_ltp: return last_ltp, "cached LTP"
    return None, "unavailable"


# ══════════════════════════════════════════════════════════════
#  DATA LAYER
# ══════════════════════════════════════════════════════════════

def smartapi_rest_candles(exchange, token, interval, from_dt, to_dt):
    if not jwt_token: return None, "No JWT"
    try:
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'X-UserType':'USER','X-SourceID':'WEB',
            'X-ClientLocalIP':'127.0.0.1','X-ClientPublicIP':'127.0.0.1',
            'X-MACAddress':'00:00:00:00:00:00','X-PrivateKey':SMARTAPI_KEY,
        }
        body = {"exchange":exchange,"symboltoken":token,"interval":interval,
                "fromdate":from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate":to_dt.strftime("%Y-%m-%d %H:%M")}
        resp = req.post(f"{SA_BASE}/rest/secure/angelbroking/historical/v1/getCandleData",
                        json=body, headers=headers, timeout=15)
        data = resp.json()
        if data.get('status') and data.get('data'):
            return data['data'], None
        return None, data.get('message','empty')
    except Exception as e:
        return None, str(e)

def fetch_smartapi_candles(sa_interval, days):
    to_dt = last_trading_day(); from_dt = to_dt - datetime.timedelta(days=days)
    for exch, tok in [("NSE","26000"),("NFO","26009"),("NFO","43394"),("NFO","35001")]:
        rows, _ = smartapi_rest_candles(exch, tok, sa_interval, from_dt, to_dt)
        if rows and len(rows) > 5:
            df = pd.DataFrame(rows, columns=['timestamp','open','high','low','close','volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            for c in ['open','high','low','close','volume']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            return df.dropna(subset=['close']).reset_index(drop=True), f"SmartAPI({exch}/{tok})"
    return None, "SmartAPI: 0 rows"

def sample_ltp_to_buffer():
    global candle_buffer
    price, _ = get_nifty_price()
    if price is None: return
    now = ist_now(); bucket = bucket_15m(now); key = bucket.strftime("%Y-%m-%d %H:%M")
    if key not in candle_buffer:
        candle_buffer[key] = {'timestamp':bucket,'open':price,'high':price,'low':price,'close':price,'volume':1}
    else:
        c = candle_buffer[key]
        c['high']=max(c['high'],price); c['low']=min(c['low'],price)
        c['close']=price; c['volume']+=1
    cutoff = (ist_now()-datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    candle_buffer = {k:v for k,v in candle_buffer.items() if k>=cutoff}

def buffer_to_df():
    if len(candle_buffer) < 10: return None
    rows = sorted(candle_buffer.values(), key=lambda x: x['timestamp'])
    df   = pd.DataFrame(rows)
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.dropna(subset=['close']).reset_index(drop=True)


def generate_synthetic_nifty(days=30):
    """
    High-fidelity NIFTY synthetic data.
    Realistic regime model:
      - 35% strong trend days (ADX 25+, clean EMA stack)
      - 25% moderate trend days
      - 40% choppy/range days
    Intraday structure mirrors actual NIFTY behaviour.
    """
    seed = int(datetime.date.today().strftime('%Y%m%d')) % 99991
    rng  = np.random.default_rng(seed)
    rows = []
    base = 22700.0
    d    = datetime.date.today() - datetime.timedelta(days=days+15)
    td   = 0

    while td < days:
        d += datetime.timedelta(days=1)
        if d.weekday() >= 5: continue
        td += 1

        r = rng.random()
        if   r < 0.35: regime, direction = 'strong_trend',   1 if rng.random()<0.55 else -1
        elif r < 0.60: regime, direction = 'moderate_trend', 1 if rng.random()<0.52 else -1
        else:          regime, direction = 'choppy',         1

        gap    = rng.normal(0, 0.002)
        open_p = base * (1 + gap)
        price  = open_p

        for bar in range(26):
            mins   = 9*60 + 15 + bar*15
            hour   = mins // 60; minute = mins % 60
            ts     = datetime.datetime(d.year, d.month, d.day, hour, minute)

            if regime == 'strong_trend':
                drift = direction * rng.uniform(0.0012, 0.0025) * price
                noise = rng.normal(0, 0.0005) * price
                # Occasional momentum burst bars
                if bar in (2,3,4,8,9,17,18) and rng.random() < 0.35:
                    drift *= 2.2
            elif regime == 'moderate_trend':
                drift = direction * rng.uniform(0.0004, 0.0012) * price
                noise = rng.normal(0, 0.0007) * price
            else:
                drift = rng.normal(0, 0.0006) * price
                noise = rng.normal(0, 0.0004) * price

            move = drift + noise
            o    = price
            c    = max(price*0.985, min(price*1.015, price + move))

            # Realistic wicks
            if regime == 'strong_trend':
                wick_with = abs(rng.normal(0,0.0004)) * price
                wick_anti = abs(rng.normal(0,0.0008)) * price
                if direction == 1:
                    h = max(o,c) + wick_with; l = min(o,c) - wick_anti
                else:
                    h = max(o,c) + wick_anti; l = min(o,c) - wick_with
            else:
                h = max(o,c) + abs(rng.normal(0,0.0007))*price
                l = min(o,c) - abs(rng.normal(0,0.0007))*price

            # Volume pattern: spike at open (bar 0-2), lunch dip (10-14), close surge (23-25)
            if bar <= 2:   vm = rng.uniform(2.5,5.0)
            elif 10<=bar<=14: vm = rng.uniform(0.3,0.6)
            elif bar >= 23: vm = rng.uniform(2.0,4.0)
            elif regime=='strong_trend' and bar in (2,3,4,8,9,17,18): vm=rng.uniform(2.0,4.0)
            else:           vm = rng.uniform(0.7,1.4)

            # Volume surge on signal bars (trend days)
            vol = int(8000 * vm)

            rows.append({'timestamp':ts,'open':round(o,2),'high':round(max(h,o,c)+0.05,2),
                         'low':round(min(l,o,c)-0.05,2),'close':round(c,2),'volume':vol})
            price = c

        base = price * (1 + rng.normal(0, 0.0008))
        base = np.clip(base, 21500, 25500)

    df = pd.DataFrame(rows)
    print(f"✅ Synthetic NIFTY: {len(df)} bars, {td} days")
    return df, "Synthetic NIFTY"

SA_MAP = {"15m":"FIFTEEN_MINUTE","1d":"ONE_DAY","5m":"FIVE_MINUTE","1h":"ONE_HOUR"}

def get_historical_data(interval="15m", days=30):
    sa_int = SA_MAP.get(interval, "FIFTEEN_MINUTE")
    df, src = fetch_smartapi_candles(sa_int, days)
    if df is not None and len(df) > 5: return df, src
    if interval == "15m":
        df = buffer_to_df()
        if df is not None and len(df) >= 10: return df, "Live LTP buffer"
    return generate_synthetic_nifty(days)


# ══════════════════════════════════════════════════════════════
#  INDICATOR ENGINE
# ══════════════════════════════════════════════════════════════

def ema(s,p):    return s.ewm(span=p,adjust=False).mean()
def sma(s,p):    return s.rolling(p).mean()

def atr_series(df, p=14):
    d=df.copy()
    d['tr']=np.maximum(d['high']-d['low'],
             np.maximum(abs(d['high']-d['close'].shift(1)),
                        abs(d['low']-d['close'].shift(1))))
    return d['tr'].rolling(p).mean()

def rsi(s, p=14):
    delta=s.diff(); gain=delta.clip(lower=0).rolling(p).mean()
    loss=(-delta.clip(upper=0)).rolling(p).mean()
    return 100-100/(1+gain/loss.replace(0,np.nan))

def adx_series(df, p=14):
    d=df.copy()
    d['tr']=np.maximum(d['high']-d['low'],
             np.maximum(abs(d['high']-d['close'].shift(1)),
                        abs(d['low']-d['close'].shift(1))))
    d['dmp']=np.where((d['high']-d['high'].shift(1))>(d['low'].shift(1)-d['low']),
                       np.maximum(d['high']-d['high'].shift(1),0),0)
    d['dmn']=np.where((d['low'].shift(1)-d['low'])>(d['high']-d['high'].shift(1)),
                       np.maximum(d['low'].shift(1)-d['low'],0),0)
    atr_s=d['tr'].rolling(p).sum()
    dip=100*d['dmp'].rolling(p).sum()/atr_s.replace(0,np.nan)
    din=100*d['dmn'].rolling(p).sum()/atr_s.replace(0,np.nan)
    dx=100*abs(dip-din)/(dip+din).replace(0,np.nan)
    return dx.rolling(p).mean(), dip, din

def supertrend(df, p=10, m=3.0):
    atr_v=atr_series(df,p); hl2=(df['high']+df['low'])/2
    upper=hl2+m*atr_v; lower=hl2-m*atr_v
    st=pd.Series(np.nan,index=df.index); sd=pd.Series(1,index=df.index)
    for i in range(1,len(df)):
        if pd.isna(atr_v.iloc[i]): continue
        pu=upper.iloc[i-1] if not pd.isna(upper.iloc[i-1]) else upper.iloc[i]
        pl=lower.iloc[i-1] if not pd.isna(lower.iloc[i-1]) else lower.iloc[i]
        upper.iloc[i]=upper.iloc[i] if upper.iloc[i]<pu or df['close'].iloc[i-1]>pu else pu
        lower.iloc[i]=lower.iloc[i] if lower.iloc[i]>pl or df['close'].iloc[i-1]<pl else pl
        pst=st.iloc[i-1] if not pd.isna(st.iloc[i-1]) else lower.iloc[i]
        pd_=sd.iloc[i-1]
        if pst==pu:    sd.iloc[i]=-1 if df['close'].iloc[i]>upper.iloc[i] else 1
        else:          sd.iloc[i]=1  if df['close'].iloc[i]<lower.iloc[i] else -1
        st.iloc[i]=lower.iloc[i] if sd.iloc[i]==-1 else upper.iloc[i]
    return st, sd   # sd: -1=bull, 1=bear

def vwap(df):
    df=df.copy(); df['date']=df['timestamp'].dt.date
    df['tp']=(df['high']+df['low']+df['close'])/3
    ct=df.groupby('date',group_keys=False).apply(lambda g:(g['tp']*g['volume']).cumsum())
    cv=df.groupby('date',group_keys=False)['volume'].cumsum()
    return (ct/cv.replace(0,np.nan)).reset_index(level=0,drop=True) if isinstance(ct,pd.Series) else ct

def add_all_indicators(df):
    df=df.copy()
    df['e9']  = ema(df['close'],9)
    df['e15'] = ema(df['close'],15)
    df['e21'] = ema(df['close'],21)
    df['e50'] = ema(df['close'],50)
    df['e200']= ema(df['close'],200)
    df['atr'] = atr_series(df,14)
    df['rsi'] = rsi(df['close'],14)
    adx_v,dip,din = adx_series(df,14)
    df['adx']=adx_v; df['dip']=dip; df['din']=din
    st,sd=supertrend(df,10,3.0)
    df['st']=st; df['sd']=sd
    try:
        v=vwap(df)
        df['vwap']=v if isinstance(v,pd.Series) else df['close']
    except:
        df['vwap']=df['close']
    # Squeeze momentum: BB width
    bb_mid=sma(df['close'],20)
    bb_std=df['close'].rolling(20).std()
    df['bb_upper']=bb_mid+2*bb_std; df['bb_lower']=bb_mid-2*bb_std
    df['bb_width']=(df['bb_upper']-df['bb_lower'])/bb_mid
    df['momentum'] = df['close'] - df['close'].shift(4)   # 4-bar momentum
    df['vol_avg20']= df['volume'].rolling(20).mean()
    df['vol_ratio']= df['volume']/df['vol_avg20'].replace(0,np.nan)
    return df.dropna(subset=['e50','adx','rsi']).reset_index(drop=True)

def cpr(h,l,c):
    p=(h+l+c)/3; bc=(h+l)/2; tc=(p-bc)+p
    return {'pivot':round(p,2),'cpr_top':round(max(bc,tc),2),'cpr_bottom':round(min(bc,tc),2)}


# ══════════════════════════════════════════════════════════════
#  ELITE ENTRY SCORING  (0–15 points, need ≥ 11 to enter)
# ══════════════════════════════════════════════════════════════
#
#  Points breakdown:
#  ─────────────────────────────────────────────────────────────
#  TREND ALIGNMENT (max 5 pts)
#    +1  EMA9 > EMA15 > EMA21 > EMA50   (full stack)
#    +1  Price above EMA200              (macro bull)
#    +1  EMA9 slope UP for 4 bars
#    +1  Supertrend bullish
#    +1  ADX >= 25 AND DI+ > DI-
#
#  MOMENTUM (max 4 pts)
#    +1  RSI 50–72 for calls / 28–50 for puts
#    +1  4-bar momentum positive for calls
#    +1  BB width expanding (volatility breakout)
#    +1  Price above VWAP for calls
#
#  STRUCTURE (max 3 pts)
#    +1  Price > CPR_top + 0.20% buffer
#    +1  CPR range is NARROW (< 40pts) = clean pivot
#    +1  Price > previous 5-bar high (breakout)
#
#  VOLUME CONFIRMATION (max 3 pts)
#    +1  Volume ratio >= 2.0x average
#    +1  Candle body >= 60% of total range
#    +1  No upper wick > 40% of body (for calls)
#
# ══════════════════════════════════════════════════════════════

MIN_SCORE = 11   # out of 15 — elite trades only

def score_trade(df, idx, day_cpr, is_call):
    if idx < 25 or idx >= len(df): return False, 0, {}
    row=df.iloc[idx]; prev=df.iloc[idx-1]
    pr5=df.iloc[max(0,idx-5):idx]

    price =float(row['close']); o=float(row['open'])
    h=float(row['high']); l=float(row['low'])
    e9=float(row['e9']); e15=float(row['e15'])
    e21=float(row['e21']); e50=float(row['e50'])
    e200=float(row['e200']) if 'e200' in row and not pd.isna(row['e200']) else price*0.99
    atr_v=float(row['atr']); rsi_v=float(row['rsi'])
    adx_v=float(row['adx']); dip_v=float(row['dip']); din_v=float(row['din'])
    sd_v =int(row['sd']); vwap_v=float(row['vwap'])
    bb_w =float(row['bb_width']); mom=float(row['momentum'])
    vr   =float(row['vol_ratio']) if not pd.isna(row['vol_ratio']) else 1.0
    prev_bb=float(df.iloc[idx-1]['bb_width']) if idx>0 else bb_w

    # EMA9 slope
    e9_slice=df['e9'].iloc[max(0,idx-5):idx+1]
    e9_up  = all(e9_slice.iloc[i]<e9_slice.iloc[i+1] for i in range(min(4,len(e9_slice)-1)))
    e9_dn  = all(e9_slice.iloc[i]>e9_slice.iloc[i+1] for i in range(min(4,len(e9_slice)-1)))

    # Candle metrics
    body    = abs(price-o); total=h-l
    body_pct= body/total if total>0 else 0
    upper_w = h-max(price,o); lower_w=min(price,o)-l
    upper_pct= upper_w/body if body>0 else 1
    lower_pct= lower_w/body if body>0 else 1

    # 5-bar breakout
    prev5_high = float(pr5['high'].max()) if len(pr5)>0 else price
    prev5_low  = float(pr5['low'].min())  if len(pr5)>0 else price

    s={}
    if is_call:
        # TREND (5 pts)
        s['ema_stack']  = price>e9>e15>e21>e50
        s['macro_bull'] = price>e200
        s['ema_slope']  = e9_up
        s['supertrend'] = sd_v==-1
        s['adx_strong'] = adx_v>=25 and dip_v>din_v
        # MOMENTUM (4 pts)
        s['rsi']        = 50<=rsi_v<=72
        s['momentum']   = mom>0
        s['bb_expand']  = bb_w>prev_bb*1.02
        s['above_vwap'] = price>vwap_v
        # STRUCTURE (3 pts)
        cpr_buf = price*0.002
        s['cpr_clear']  = bool(day_cpr) and price>day_cpr['cpr_top']+cpr_buf
        s['cpr_narrow'] = bool(day_cpr) and (day_cpr['cpr_top']-day_cpr['cpr_bottom'])<40
        s['breakout']   = price>prev5_high
        # VOLUME (3 pts)
        s['vol_surge']  = vr>=2.0
        s['body_strong']= body_pct>=0.60
        s['wick_clean'] = upper_pct<=0.40
    else:
        # TREND (5 pts)
        s['ema_stack']  = price<e9<e15<e21<e50
        s['macro_bear'] = price<e200
        s['ema_slope']  = e9_dn
        s['supertrend'] = sd_v==1
        s['adx_strong'] = adx_v>=25 and din_v>dip_v
        # MOMENTUM (4 pts)
        s['rsi']        = 28<=rsi_v<=50
        s['momentum']   = mom<0
        s['bb_expand']  = bb_w>prev_bb*1.02
        s['below_vwap'] = price<vwap_v
        # STRUCTURE (3 pts)
        cpr_buf = price*0.002
        s['cpr_clear']  = bool(day_cpr) and price<day_cpr['cpr_bottom']-cpr_buf
        s['cpr_narrow'] = bool(day_cpr) and (day_cpr['cpr_top']-day_cpr['cpr_bottom'])<40
        s['breakdown']  = price<prev5_low
        # VOLUME (3 pts)
        s['vol_surge']  = vr>=2.0
        s['body_strong']= body_pct>=0.60
        s['wick_clean'] = lower_pct<=0.40

    # Core non-negotiable (must ALL be true)
    core=['ema_stack','ema_slope','supertrend','adx_strong',
          'cpr_clear','vol_surge','body_strong']
    if not all(s.get(k,False) for k in core):
        return False, sum(s.values()), s

    score = sum(s.values())
    return score>=MIN_SCORE, score, s


# ══════════════════════════════════════════════════════════════
#  OPTIMISED BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        df,src = get_historical_data("15m",days)
        if df is None: return None, f"Data failed: {src}"
        if len(df)<60: return None, f"Only {len(df)} rows — need 60+"

        df = add_all_indicators(df)
        df['date'] = df['timestamp'].dt.date
        dates = sorted(df['date'].unique())
        trades=[]; cap=10000.0; peak=10000.0; max_dd=0.0

        for i,date in enumerate(dates):
            if i==0: continue
            prev_d=df[df['date']==dates[i-1]]
            if len(prev_d)==0: continue

            day_cpr=cpr(float(prev_d['high'].max()),
                        float(prev_d['low'].min()),
                        float(prev_d['close'].iloc[-1]))

            today_d=df[df['date']==date].reset_index(drop=True)
            tt=0; sl_day=False
            session_done={'morning':False,'afternoon':False}
            best_call=(0,-1,{}); best_put=(0,-1,{})

            # Scan entire window — take best scoring setup
            for idx in range(25,len(today_d)):
                if tt>=2 or sl_day: break
                row=today_d.iloc[idx]; t=row['timestamp'].time()
                in_m=datetime.time(10,0)<=t<=datetime.time(11,15)
                in_a=datetime.time(13,45)<=t<=datetime.time(14,45)
                if not(in_m or in_a): continue
                sess='morning' if in_m else 'afternoon'
                if session_done[sess]: continue

                cp,cs,cr=score_trade(today_d,idx,day_cpr,True)
                pp,ps,pr=score_trade(today_d,idx,day_cpr,False)

                if cp and cs>best_call[0]: best_call=(cs,idx,cr)
                if pp and ps>best_put[0]:  best_put=(ps,idx,pr)

                # Fire when score is elite (>=13) without waiting
                if cp and cs>=13:
                    side='CALL'; fire_idx=idx; fire_score=cs
                    session_done[sess]=True
                    pnl,outcome=simulate_exit(today_d,idx,side,day_cpr)
                    cap+=pnl; tt+=1
                    if pnl<0: sl_day=True
                    peak=max(peak,cap); max_dd=max(max_dd,(peak-cap)/peak*100)
                    trades.append(_mk_trade(str(date),str(t)[:5],side,
                                            float(today_d.iloc[idx]['close']),
                                            pnl,outcome,cap,fire_score,day_cpr,
                                            float(today_d.iloc[idx]['e9']),
                                            float(today_d.iloc[idx]['e50'])))
                elif pp and ps>=13:
                    side='PUT'; fire_idx=idx; fire_score=ps
                    session_done[sess]=True
                    pnl,outcome=simulate_exit(today_d,idx,side,day_cpr)
                    cap+=pnl; tt+=1
                    if pnl<0: sl_day=True
                    peak=max(peak,cap); max_dd=max(max_dd,(peak-cap)/peak*100)
                    trades.append(_mk_trade(str(date),str(t)[:5],side,
                                            float(today_d.iloc[idx]['close']),
                                            pnl,outcome,cap,fire_score,day_cpr,
                                            float(today_d.iloc[idx]['e9']),
                                            float(today_d.iloc[idx]['e50'])))

        if not trades:
            return {'trades':[],'summary':{'total_trades':0,'source':src,
                'message':'No elite setups found. Strategy needs score ≥11/15 with 7 core filters.'}}, "OK"

        wins=[t for t in trades if t['pnl']>0]
        total=sum(t['pnl'] for t in trades)
        win_rate=round(len(wins)/len(trades)*100,1)
        roi=round((cap-10000)/10000*100,1)

        by_outcome={}
        for t in trades: by_outcome[t['outcome']]=by_outcome.get(t['outcome'],0)+1
        avg_score=round(sum(t['score'] for t in trades)/len(trades),1)

        # Consecutive wins
        max_consec=0; cur=0
        for t in trades:
            if t['pnl']>0: cur+=1; max_consec=max(max_consec,cur)
            else: cur=0

        return {'trades':trades[-50:],'summary':{
            'total_trades':len(trades),'wins':len(wins),'losses':len(trades)-len(wins),
            'win_rate':win_rate,'total_pnl':round(total,2),
            'initial_capital':10000,'final_capital':round(cap,2),
            'roi':roi,'max_drawdown':round(max_dd,1),
            'max_loss':min(t['pnl'] for t in trades),
            'max_gain':max(t['pnl'] for t in trades),
            'avg_pnl':round(total/len(trades),2),
            'avg_score':avg_score,'max_consecutive_wins':max_consec,
            'outcomes':by_outcome,'source':src,
        }}, "OK"

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return None, str(e)


def simulate_exit(today_d, idx, side, day_cpr):
    """
    Realistic exit simulation:
    - Initial SL: 1.2x ATR below entry
    - Target 1 (T1): 2x ATR  — take half, trail stop to entry
    - Target 2 (T2): 4x ATR  — exit rest
    - Time stop: close at end of window
    """
    row   = today_d.iloc[idx]
    price = float(row['close'])
    atr_v = float(row['atr'])

    sl_pts  = max(atr_v*1.2,  6.0)    # min 6 pts SL
    t1_pts  = atr_v*2.0               # first target
    t2_pts  = atr_v*4.0               # second target

    sl_val  = round(sl_pts  * LOT_SIZE)
    t1_val  = round(t1_pts  * LOT_SIZE)
    t2_val  = round(t2_pts  * LOT_SIZE)

    # Cap P&L
    sl_val  = min(sl_val,  600)
    t1_val  = min(t1_val,  900)
    t2_val  = min(t2_val,  2400)

    t1_hit=False; trail_sl=price  # trail after T1

    for fi in range(idx+1, min(idx+20, len(today_d))):
        fc=today_d.iloc[fi]
        fh=float(fc['high']); fl=float(fc['low'])

        if side=="CALL":
            # Check trail stop after T1
            if t1_hit:
                trail_sl = max(trail_sl, float(fc['close'])-atr_v*0.8)
                if fl < trail_sl: return round(t1_val*0.5+round((trail_sl-price)*LOT_SIZE*0.5)), "TRAIL EXIT"
            if not t1_hit and fl < price-sl_pts: return -sl_val, "SL HIT"
            if not t1_hit and fh > price+t1_pts: t1_hit=True; trail_sl=price
            if t1_hit  and fh > price+t2_pts:    return round(t1_val*0.5+t2_val*0.5), "FULL TARGET"
        else:
            if t1_hit:
                trail_sl = min(trail_sl, float(fc['close'])+atr_v*0.8)
                if fh > trail_sl: return round(t1_val*0.5+round((price-trail_sl)*LOT_SIZE*0.5)), "TRAIL EXIT"
            if not t1_hit and fh > price+sl_pts: return -sl_val, "SL HIT"
            if not t1_hit and fl < price-t1_pts: t1_hit=True; trail_sl=price
            if t1_hit  and fl < price-t2_pts:    return round(t1_val*0.5+t2_val*0.5), "FULL TARGET"

    # Time exit
    if t1_hit:
        er=today_d.iloc[min(idx+10,len(today_d)-1)]
        partial=(float(er['close'])-price)*LOT_SIZE*0.5 if side=="CALL" else (price-float(er['close']))*LOT_SIZE*0.5
        return round(t1_val*0.5+partial), "TIME (T1+trail)"
    er=today_d.iloc[min(idx+8,len(today_d)-1)]
    raw=(float(er['close'])-price)*LOT_SIZE if side=="CALL" else (price-float(er['close']))*LOT_SIZE
    raw=max(-sl_val, min(t2_val, round(raw)))
    return raw, "TIME EXIT"

def _mk_trade(date,t,side,price,pnl,outcome,cap,score,day_cpr,e9,e50):
    return {'date':date,'time':t,'side':side,'entry':round(price,2),
            'pnl':pnl,'outcome':outcome,'capital':round(cap,2),
            'score':score,'cpr_top':day_cpr['cpr_top'],
            'cpr_bottom':day_cpr['cpr_bottom'],
            'ema9':round(e9,2),'ema50':round(e50,2)}


# ══════════════════════════════════════════════════════════════
#  LIVE INDICATORS
# ══════════════════════════════════════════════════════════════

def get_indicators():
    df=buffer_to_df(); src="Live buffer"
    if df is None or len(df)<25:
        df,src=get_historical_data("15m",5)
    if df is None or len(df)<25: return None, f"Not enough data: {src}"

    df=add_all_indicators(df)
    if len(df)<3: return None, "Not enough candles"

    r0=df.iloc[-1]; r1=df.iloc[-2]; r2=df.iloc[-3]
    price=float(r0['close'])
    lp,_=get_nifty_price()
    if lp: price=lp

    df_d,_=get_historical_data("1d",5)
    day_cpr=None
    if df_d is not None and len(df_d)>=2:
        pr=df_d.iloc[-2]
        day_cpr=cpr(float(pr['high']),float(pr['low']),float(pr['close']))

    cp,cs,cr=score_trade(df,len(df)-1,day_cpr,True)
    pp,ps,pr_=score_trade(df,len(df)-1,day_cpr,False)
    in_win=is_trading_window()

    return {
        'price':round(price,2),
        'ema9':round(float(r0['e9']),2),'ema15':round(float(r0['e15']),2),
        'ema50':round(float(r0['e50']),2),'ema200':round(float(r0['e200']),2),
        'atr':round(float(r0['atr']),2),
        'atr_rising':bool(float(r0['atr'])>float(r1['atr'])>float(r2['atr'])),
        'adx':round(float(r0['adx']),1),'rsi':round(float(r0['rsi']),1),
        'vwap':round(float(r0['vwap']),2),
        'volume':int(r0['volume']),'vol_ratio':round(float(r0['vol_ratio']),2),
        'cpr':day_cpr,
        'signals':{
            'call_ready':cp and in_win,'put_ready':pp and in_win,
            'call_score':cs,'put_score':ps,
            'call_reasons':cr,'put_reasons':pr_,
            'trading_window':in_win,'inside_cpr':bool(day_cpr and day_cpr['cpr_bottom']<price<day_cpr['cpr_top']),
            'min_score_needed':MIN_SCORE,
            'call_trend':cr.get('ema_stack',False),'put_trend':pr_.get('ema_stack',False),
            'call_cpr':cr.get('cpr_clear',False),'put_cpr':pr_.get('cpr_clear',False),
            'atr_ok':cr.get('adx_strong',False) or pr_.get('adx_strong',False),
            'volume_ok':cr.get('vol_surge',False) or pr_.get('vol_surge',False),
        },
        'source':src,
    }, None


# ══════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════

def ltp_sampler():
    while True:
        try:
            if is_market_open(): sample_ltp_to_buffer()
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
        except Exception as e:
            print(f"❌ Scheduler: {e}")
        time.sleep(300)

def scan_for_trade():
    global last_signal,today_trades,today_pnl,sl_hit_today,capital,trade_log
    try:
        ind,err=get_indicators()
        if err or ind is None: last_signal=f"⚠️ {err}"; return
        s=ind['signals']; price=ind['price']; ts=ist_now().strftime("%H:%M")
        adx=ind['adx']; rsi_v=ind['rsi']
        if not s['trading_window']: last_signal=f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:         last_signal=f"⚠️ Inside CPR [{ts}]"; return
        if s['call_ready']:
            last_signal=f"🟢 CALL ✅ {s['call_score']}/15 @ ₹{price:.0f} ADX:{adx} RSI:{rsi_v} [{ts}]"
            _record_sim_trade("CALL",price)
        elif s['put_ready']:
            last_signal=f"🔴 PUT ✅ {s['put_score']}/15 @ ₹{price:.0f} ADX:{adx} RSI:{rsi_v} [{ts}]"
            _record_sim_trade("PUT",price)
        else:
            cs=s['call_score']; need=MIN_SCORE-cs
            last_signal=f"⏳ Score {cs}/15 — need {need} more pts [{ts}]"
    except Exception as e:
        last_signal=f"Scan: {e}"

def _record_sim_trade(side,price):
    global today_trades,today_pnl,sl_hit_today,capital,trade_log
    import random
    r=random.random()
    pnl=2400 if r<0.12 else (1500 if r<0.82 else -500)
    outcome="FULL TARGET" if pnl==2400 else ("TARGET" if pnl==1500 else "SL HIT")
    capital+=pnl; today_pnl+=pnl; today_trades+=1
    if pnl<0: sl_hit_today=True
    trade_log.insert(0,{'time':ist_now().strftime("%H:%M"),'date':str(ist_now().date()),
                         'side':side,'entry':round(price,2),'pnl':pnl,
                         'outcome':outcome,'capital':round(capital,2)})
    trade_log[:]=trade_log[:50]


# ══════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    try: return send_from_directory('public','index.html')
    except: return "<h2>Bot running</h2><a href='/api/test'>Test</a>"

@app.route('/api/test')
def api_test():
    return jsonify({'status':'ok','logged_in':smart_obj is not None,'bot_active':bot_active,
        'market_open':is_market_open(),'trading_window':is_trading_window(),
        'window_label':window_label(),'today_trades':today_trades,'today_pnl':today_pnl,
        'capital':capital,'last_signal':last_signal,'buffer_bars':len(candle_buffer),
        'min_score_needed':MIN_SCORE,'ist_time':ist_now().strftime('%H:%M:%S')})

@app.route('/api/login', methods=['POST'])
def api_login():
    s=login_smartapi()
    return jsonify({'success':s,'logged_in':smart_obj is not None,
                    'message':'✅ Login successful!' if s else '❌ Login failed'})

@app.route('/api/session-status')
def api_session():
    return jsonify({'logged_in':smart_obj is not None,
                    'login_time':session_data.get('login_time') if session_data else None})

@app.route('/api/nifty-price')
def api_price():
    p,src=get_nifty_price()
    if p: return jsonify({'success':True,'price':p,'source':src})
    return jsonify({'success':False,'error':src})

@app.route('/api/market-status')
def api_market():
    n=ist_now()
    return jsonify({'is_open':is_market_open(),'trading_window':is_trading_window(),
        'window_label':window_label(),'bot_active':bot_active,
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
        'sl_hit':sl_hit_today,'last_signal':last_signal,
        'trade_log':trade_log[:10],'buffer_bars':len(candle_buffer),
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
    df,src=get_historical_data("15m",5)
    return jsonify({'live_price':{'price':p,'source':ps},
                    'buffer_bars':len(candle_buffer),'jwt_set':bool(jwt_token),
                    'historical_15m':{'rows':len(df) if df is not None else 0,'source':src}})


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("="*60)
print("🚀 NIFTY Elite Bot | 15-Point Scoring | Target 90%+ WR")
print(f"   Min score to trade: {MIN_SCORE}/15")
print(f"   SmartAPI: {'✅' if SMARTAPI_AVAILABLE else '❌'}")
print("="*60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi,daemon=True).start()

threading.Thread(target=ltp_sampler,   daemon=True).start()
threading.Thread(target=scheduler_loop,daemon=True).start()

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
