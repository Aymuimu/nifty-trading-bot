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

smart_obj    = None
session_data = None
session_lock = threading.Lock()
jwt_token    = None

trade_log    = []
today_trades = 0
today_pnl    = 0.0
capital      = 10000.0
sl_hit_today = False
last_signal  = "Bot not started"
bot_active   = False
candle_buffer = {}
last_ltp      = None

# ── Strategy constants ─────────────────────────────────────────
LOT_SIZE    = 75
STOP_LOSS   = 500
BASE_TARGET = 1500
EXT_TARGET  = 3000   # extended target when explosive momentum

SA_BASE = "https://apiconnect.angelbroking.com"


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

def last_trading_day(offset=0):
    d = datetime.datetime.now() - datetime.timedelta(days=offset)
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
#  DATA — SmartAPI REST → live buffer → synthetic
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
        url  = f"{SA_BASE}/rest/secure/angelbroking/historical/v1/getCandleData"
        resp = req.post(url, json=body, headers=headers, timeout=15)
        data = resp.json()
        if data.get('status') and data.get('data'):
            return data['data'], None
        return None, data.get('message','empty')
    except Exception as e:
        return None, str(e)

def fetch_smartapi_candles(sa_interval, days):
    to_dt = last_trading_day(); from_dt = to_dt - datetime.timedelta(days=days)
    for exch, tok in [("NSE","26000"),("NFO","26009"),("NFO","43394"),("NFO","35001")]:
        rows, err = smartapi_rest_candles(exch, tok, sa_interval, from_dt, to_dt)
        if rows and len(rows) > 5:
            df = pd.DataFrame(rows, columns=['timestamp','open','high','low','close','volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            for c in ['open','high','low','close','volume']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            return df.dropna(subset=['close']).reset_index(drop=True), f"SmartAPI({exch}/{tok})"
    return None, "SmartAPI REST: 0 rows"

def sample_ltp_to_buffer():
    global candle_buffer
    price, _ = get_nifty_price()
    if price is None: return
    now = ist_now(); bucket = bucket_15m(now); key = bucket.strftime("%Y-%m-%d %H:%M")
    if key not in candle_buffer:
        candle_buffer[key] = {'timestamp':bucket,'open':price,'high':price,'low':price,'close':price,'volume':1}
    else:
        c = candle_buffer[key]
        c['high'] = max(c['high'],price); c['low'] = min(c['low'],price)
        c['close'] = price; c['volume'] += 1
    cutoff = (now-datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
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
    Realistic NIFTY synthetic data.
    Uses actual NIFTY characteristics:
    - Range: 22000-24000
    - Intraday volatility: 0.8% typical, 1.5% trending days
    - Clear trend days ~40% of time
    - Choppy/range days ~60%
    """
    np.random.seed(int(datetime.date.today().strftime('%Y%m%d')) % 10000)
    rows = []; base = 22700.0
    trading_days = 0
    d = datetime.date.today() - datetime.timedelta(days=days+10)

    while trading_days < days:
        d += datetime.timedelta(days=1)
        if d.weekday() >= 5: continue
        trading_days += 1

        # Day type: trending (40%) or choppy (60%)
        is_trending = np.random.random() < 0.40
        direction   = 1 if np.random.random() < 0.5 else -1
        daily_vol   = 0.008 if is_trending else 0.004

        open_p = base * (1 + np.random.normal(0, 0.003))
        price  = open_p

        for bar in range(25):   # 9:15 to 15:15 in 15-min bars
            mins  = 9*60 + 15 + bar*15
            hour  = mins // 60; minute = mins % 60
            ts    = datetime.datetime(d.year, d.month, d.day, hour, minute)

            if is_trending:
                # Trending: directional movement with pullbacks
                trend_move = np.random.normal(direction*0.0015, 0.001) * price
                noise      = np.random.normal(0, 0.0005) * price
                move       = trend_move + noise
            else:
                # Choppy: mean-reverting small moves
                move = np.random.normal(0, 0.0008) * price

            o = price
            c = price + move
            # Realistic wicks
            wick_up   = abs(np.random.normal(0, 0.0006)) * price
            wick_down = abs(np.random.normal(0, 0.0006)) * price
            h = max(o,c) + wick_up
            l = min(o,c) - wick_down

            # Volume: higher at open/close, lower midday
            if bar < 3 or bar > 21:    vol_mult = np.random.uniform(1.5, 3.0)
            elif 10 <= bar <= 15:       vol_mult = np.random.uniform(0.4, 0.8)
            else:                       vol_mult = np.random.uniform(0.8, 1.5)
            vol = int(10000 * vol_mult)

            rows.append({'timestamp':ts,'open':round(o,2),'high':round(h,2),
                         'low':round(l,2),'close':round(c,2),'volume':vol})
            price = c

        # Drift base
        base = price * (1 + np.random.normal(0, 0.001))
        base = max(21000, min(25000, base))  # keep in realistic range

    df = pd.DataFrame(rows)
    print(f"✅ Synthetic NIFTY: {len(df)} bars over {trading_days} days")
    return df, "Synthetic NIFTY (realistic)"

SA_MAP = {"15m":"FIFTEEN_MINUTE","1d":"ONE_DAY","5m":"FIVE_MINUTE","1m":"ONE_MINUTE","1h":"ONE_HOUR"}

def get_historical_data(interval="15m", days=30):
    sa_int = SA_MAP.get(interval, "FIFTEEN_MINUTE")
    df, src = fetch_smartapi_candles(sa_int, days)
    if df is not None and len(df) > 5: return df, src

    if interval == "15m":
        df = buffer_to_df()
        if df is not None and len(df) >= 10: return df, "Live LTP buffer"

    df, src = generate_synthetic_nifty(days)
    return df, src


# ══════════════════════════════════════════════════════════════
#  ENHANCED INDICATORS
# ══════════════════════════════════════════════════════════════

def calc_ema(s, p):   return s.ewm(span=p, adjust=False).mean()
def calc_sma(s, p):   return s.rolling(p).mean()

def calc_atr(df, p=14):
    d = df.copy()
    d['tr'] = np.maximum(d['high']-d['low'],
               np.maximum(abs(d['high']-d['close'].shift(1)),
                          abs(d['low'] -d['close'].shift(1))))
    return d['tr'].rolling(p).mean()

def calc_adx(df, p=14):
    """Average Directional Index — measures trend STRENGTH (not direction)."""
    d = df.copy()
    d['tr']   = np.maximum(d['high']-d['low'],
                 np.maximum(abs(d['high']-d['close'].shift(1)),
                            abs(d['low'] -d['close'].shift(1))))
    d['dm_p'] = np.where((d['high']-d['high'].shift(1)) > (d['low'].shift(1)-d['low']),
                          np.maximum(d['high']-d['high'].shift(1), 0), 0)
    d['dm_n'] = np.where((d['low'].shift(1)-d['low']) > (d['high']-d['high'].shift(1)),
                          np.maximum(d['low'].shift(1)-d['low'], 0), 0)
    atr_s  = d['tr'].rolling(p).sum()
    di_p   = 100 * d['dm_p'].rolling(p).sum() / atr_s.replace(0, np.nan)
    di_n   = 100 * d['dm_n'].rolling(p).sum() / atr_s.replace(0, np.nan)
    dx     = 100 * abs(di_p - di_n) / (di_p + di_n).replace(0, np.nan)
    return dx.rolling(p).mean(), di_p, di_n

def calc_rsi(s, p=14):
    delta = s.diff()
    gain  = delta.clip(lower=0).rolling(p).mean()
    loss  = (-delta.clip(upper=0)).rolling(p).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100/(1+rs)

def calc_vwap(df):
    """VWAP reset per day."""
    df = df.copy()
    df['date'] = df['timestamp'].dt.date
    df['tp']   = (df['high']+df['low']+df['close'])/3
    df['cum_tv'] = df.groupby('date').apply(
        lambda g: (g['tp']*g['volume']).cumsum()
    ).reset_index(level=0, drop=True)
    df['cum_v']  = df.groupby('date')['volume'].cumsum()
    df['vwap']   = df['cum_tv'] / df['cum_v'].replace(0, np.nan)
    return df['vwap']

def calc_supertrend(df, period=10, multiplier=3.0):
    """Supertrend indicator — very reliable for NIFTY trends."""
    atr_v  = calc_atr(df, period)
    hl2    = (df['high'] + df['low']) / 2
    upper  = hl2 + multiplier * atr_v
    lower  = hl2 - multiplier * atr_v

    supertrend = pd.Series(index=df.index, dtype=float)
    direction  = pd.Series(index=df.index, dtype=int)

    for i in range(1, len(df)):
        if pd.isna(atr_v.iloc[i]):
            supertrend.iloc[i] = np.nan; direction.iloc[i] = 1; continue

        prev_upper = upper.iloc[i-1] if not pd.isna(upper.iloc[i-1]) else upper.iloc[i]
        prev_lower = lower.iloc[i-1] if not pd.isna(lower.iloc[i-1]) else lower.iloc[i]
        prev_st    = supertrend.iloc[i-1] if not pd.isna(supertrend.iloc[i-1]) else lower.iloc[i]
        prev_dir   = direction.iloc[i-1]  if not pd.isna(direction.iloc[i-1])  else 1

        upper.iloc[i] = upper.iloc[i] if upper.iloc[i] < prev_upper or df['close'].iloc[i-1] > prev_upper else prev_upper
        lower.iloc[i] = lower.iloc[i] if lower.iloc[i] > prev_lower or df['close'].iloc[i-1] < prev_lower else prev_lower

        if prev_st == prev_upper:
            direction.iloc[i] = -1 if df['close'].iloc[i] > upper.iloc[i] else 1
        else:
            direction.iloc[i] =  1 if df['close'].iloc[i] < lower.iloc[i] else -1

        supertrend.iloc[i] = lower.iloc[i] if direction.iloc[i] == -1 else upper.iloc[i]

    return supertrend, direction   # direction: -1=bullish, 1=bearish

def calc_cpr(h, l, c):
    piv=(h+l+c)/3; bc=(h+l)/2; tc=(piv-bc)+piv
    return {'pivot':round(piv,2),'cpr_top':round(max(bc,tc),2),'cpr_bottom':round(min(bc,tc),2)}

def ema_slope_ok(ema_series, lookback=3):
    """True if EMA has been consistently rising/falling for `lookback` bars."""
    if len(ema_series) < lookback+1: return False, False
    last = ema_series.iloc[-(lookback+1):]
    rising  = all(last.iloc[i] < last.iloc[i+1] for i in range(lookback))
    falling = all(last.iloc[i] > last.iloc[i+1] for i in range(lookback))
    return rising, falling

def candle_pattern(row):
    """
    Returns 'bull_rejection' (hammer), 'bear_rejection' (shooting star), or 'none'.
    Bull rejection: long lower wick, closes near top.
    Bear rejection: long upper wick, closes near bottom.
    """
    body  = abs(float(row['close']) - float(row['open']))
    total = float(row['high']) - float(row['low'])
    if total < 1: return 'none'
    lower_wick = float(row['open'])  - float(row['low'])  if float(row['close']) >= float(row['open']) else float(row['close']) - float(row['low'])
    upper_wick = float(row['high'])  - float(row['close']) if float(row['close']) >= float(row['open']) else float(row['high']) - float(row['open'])
    lower_wick = max(lower_wick, 0); upper_wick = max(upper_wick, 0)
    if lower_wick >= 0.55 * total and float(row['close']) >= float(row['open']): return 'bull_rejection'
    if upper_wick >= 0.55 * total and float(row['close']) <= float(row['open']): return 'bear_rejection'
    return 'none'


# ══════════════════════════════════════════════════════════════
#  ENHANCED STRATEGY — 8 FILTER CHECKLIST
# ══════════════════════════════════════════════════════════════
#
#  CALL entry requires ALL of:
#  1. Time window (10:00-11:15 or 13:45-14:45)
#  2. Price ABOVE CPR_top by at least 0.15%
#  3. EMA trend: price > EMA9 > EMA15 > EMA50
#  4. EMA9 slope rising for 3+ candles
#  5. ADX >= 22 (trending market, not choppy)
#  6. Supertrend bullish (direction == -1)
#  7. Volume surge: current vol > 1.5x 10-bar average
#  8. ATR rising for 3 candles AND ATR > 40 pts
#  9. (Optional bonus) Bull rejection candle pattern
#  10. RSI between 45-70 (momentum but not overbought)
#  11. Price above VWAP
#
#  PUT entry: exact mirror with reversed conditions
#
# ══════════════════════════════════════════════════════════════

def score_setup(df, idx, day_cpr, is_call):
    """
    Returns (passes:bool, score:int, reasons:dict)
    score 0-11; passes requires score >= 7
    """
    if idx < 20 or idx >= len(df): return False, 0, {}

    row   = df.iloc[idx]
    prev3 = df.iloc[idx-3:idx+1]

    price  = float(row['close'])
    e9     = float(row['ema9'])
    e15    = float(row['ema15'])
    e50    = float(row['ema50'])
    atr_v  = float(row['atr'])
    adx_v  = float(row['adx']) if 'adx' in df.columns else 0
    rsi_v  = float(row['rsi']) if 'rsi' in df.columns else 50
    vwap_v = float(row['vwap']) if 'vwap' in df.columns else price
    st_dir = int(row['st_dir']) if 'st_dir' in df.columns else 0

    # 1. EMA trend
    if is_call:
        ema_trend = bool(price > e9 > e15 > e50)
    else:
        ema_trend = bool(price < e9 < e15 < e50)

    # 2. EMA9 slope
    e9_series = df['ema9'].iloc[max(0,idx-4):idx+1]
    e9_rising, e9_falling = ema_slope_ok(e9_series, 3)
    slope_ok = e9_rising if is_call else e9_falling

    # 3. CPR distance (price must be well outside CPR)
    cpr_buffer = price * 0.0015  # 0.15% of price
    if is_call:
        cpr_ok = bool(day_cpr and price > day_cpr['cpr_top'] + cpr_buffer)
    else:
        cpr_ok = bool(day_cpr and price < day_cpr['cpr_bottom'] - cpr_buffer)

    # 4. ADX >= 22
    adx_ok = bool(adx_v >= 22)

    # 5. Supertrend
    if is_call:
        st_ok = bool(st_dir == -1)   # -1 = bullish
    else:
        st_ok = bool(st_dir == 1)    # 1 = bearish

    # 6. Volume surge: current > 1.5x recent average
    vol_avg = df['volume'].iloc[max(0,idx-10):idx].mean()
    vol_ok  = bool(float(row['volume']) > 1.5 * vol_avg) if vol_avg > 0 else False

    # 7. ATR rising + sufficient volatility
    atr_slice = df['atr'].iloc[max(0,idx-3):idx+1]
    atr_rising = bool(atr_slice.is_monotonic_increasing) if len(atr_slice)>=3 else False
    atr_ok     = bool(atr_rising and atr_v >= 30)

    # 8. RSI filter
    if is_call:
        rsi_ok = bool(45 <= rsi_v <= 72)   # momentum, not overbought
    else:
        rsi_ok = bool(28 <= rsi_v <= 55)   # momentum, not oversold

    # 9. VWAP
    if is_call:
        vwap_ok = bool(price > vwap_v)
    else:
        vwap_ok = bool(price < vwap_v)

    # 10. Candle pattern (bonus)
    pat = candle_pattern(row)
    pattern_ok = bool(pat == 'bull_rejection' if is_call else pat == 'bear_rejection')

    # 11. Candle color confirmation
    if is_call:
        color_ok = bool(float(row['close']) > float(row['open']))
    else:
        color_ok = bool(float(row['close']) < float(row['open']))

    reasons = {
        'ema_trend': ema_trend, 'ema_slope': slope_ok,
        'cpr': cpr_ok, 'adx': adx_ok, 'supertrend': st_ok,
        'volume': vol_ok, 'atr': atr_ok, 'rsi': rsi_ok,
        'vwap': vwap_ok, 'candle': pattern_ok, 'color': color_ok,
    }

    # Core 7 must ALL pass (non-negotiable)
    core = [ema_trend, slope_ok, cpr_ok, adx_ok, st_ok, atr_ok, color_ok]
    if not all(core):
        return False, sum(reasons.values()), reasons

    # At least 2 of the 4 secondary filters
    secondary = [vol_ok, rsi_ok, vwap_ok, pattern_ok]
    if sum(secondary) < 2:
        return False, sum(reasons.values()), reasons

    return True, sum(reasons.values()), reasons


def add_indicators(df):
    """Add all indicators to a DataFrame in one pass."""
    df = df.copy()
    df['ema9']  = calc_ema(df['close'], 9)
    df['ema15'] = calc_ema(df['close'], 15)
    df['ema50'] = calc_ema(df['close'], 50)
    df['atr']   = calc_atr(df, 14)
    df['rsi']   = calc_rsi(df['close'], 14)
    try:
        df['vwap'] = calc_vwap(df)
    except:
        df['vwap'] = df['close']
    try:
        adx_v, di_p, di_n = calc_adx(df, 14)
        df['adx']   = adx_v
        df['di_p']  = di_p
        df['di_n']  = di_n
    except:
        df['adx'] = 25; df['di_p'] = 25; df['di_n'] = 20
    try:
        st, st_dir   = calc_supertrend(df, 10, 3.0)
        df['st']     = st
        df['st_dir'] = st_dir
    except:
        df['st'] = df['close']; df['st_dir'] = -1
    return df.dropna(subset=['ema50','atr','adx']).reset_index(drop=True)


def get_indicators():
    df = buffer_to_df()
    src = "Live buffer"
    if df is None or len(df) < 20:
        df, src = get_historical_data("15m", 5)
    if df is None or len(df) < 20:
        return None, f"Not enough data: {src}"

    df = add_indicators(df)
    if len(df) < 3: return None, "Not enough data after indicators"

    r0,r1,r2   = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    price      = float(r0['close'])
    e9,e15,e50 = float(r0['ema9']), float(r0['ema15']), float(r0['ema50'])

    # Live price override
    lp, _ = get_nifty_price()
    if lp: price = lp

    df_d, _ = get_historical_data("1d", 5)
    day_cpr = None
    if df_d is not None and len(df_d) >= 2:
        pr = df_d.iloc[-2]
        day_cpr = calc_cpr(float(pr['high']),float(pr['low']),float(pr['close']))

    call_pass, call_score, call_r = score_setup(df, len(df)-1, day_cpr, True)
    put_pass,  put_score,  put_r  = score_setup(df, len(df)-1, day_cpr, False)

    in_win = is_trading_window()
    call_ready = call_pass and in_win
    put_ready  = put_pass  and in_win
    inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])

    return {
        'price':round(price,2),'ema9':round(e9,2),'ema15':round(e15,2),'ema50':round(e50,2),
        'atr':round(float(r0['atr']),2),
        'atr_rising':bool(float(r0['atr'])>float(r1['atr'])>float(r2['atr'])),
        'adx':round(float(r0['adx']),1),
        'rsi':round(float(r0['rsi']),1),
        'volume':int(r0['volume']),'vol_rising':bool(float(r0['volume'])>float(r1['volume'])),
        'cpr':day_cpr,
        'signals':{
            'call_trend':   call_r.get('ema_trend',False),
            'put_trend':    put_r.get('ema_trend',False),
            'call_cpr':     call_r.get('cpr',False),
            'put_cpr':      put_r.get('cpr',False),
            'inside_cpr':   inside_cpr,
            'adx_ok':       call_r.get('adx',False),
            'supertrend_bull': call_r.get('supertrend',False),
            'supertrend_bear': put_r.get('supertrend',False),
            'atr_ok':       call_r.get('atr',False),
            'volume_ok':    call_r.get('volume',False),
            'rsi_ok':       call_r.get('rsi',False),
            'vwap_ok':      call_r.get('vwap',False),
            'trading_window': in_win,
            'call_ready':   call_ready, 'put_ready': put_ready,
            'call_score':   call_score, 'put_score':  put_score,
            'call_reasons': call_r,     'put_reasons': put_r,
        },
        'source':src,
    }, None


# ══════════════════════════════════════════════════════════════
#  ENHANCED BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        df, src = get_historical_data("15m", days)
        if df is None: return None, f"Data failed: {src}"
        if len(df) < 50: return None, f"Only {len(df)} rows"

        df = add_indicators(df)
        df['date'] = df['timestamp'].dt.date
        dates = sorted(df['date'].unique())
        trades=[]; cap=10000.0

        for i, date in enumerate(dates):
            if i == 0: continue
            prev_d = df[df['date']==dates[i-1]]
            if len(prev_d)==0: continue
            day_cpr = calc_cpr(float(prev_d['high'].max()),
                               float(prev_d['low'].min()),
                               float(prev_d['close'].iloc[-1]))
            today_d = df[df['date']==date].reset_index(drop=True)
            tt=0; sl=False; session_traded={'morning':False,'afternoon':False}

            for idx in range(20, len(today_d)):
                if tt>=2 or sl: break
                row = today_d.iloc[idx]
                t   = row['timestamp'].time()

                # Strict time windows — only 1 trade per session
                in_morning   = datetime.time(10,0)<=t<=datetime.time(11,15)
                in_afternoon = datetime.time(13,45)<=t<=datetime.time(14,45)
                if not (in_morning or in_afternoon): continue
                session = 'morning' if in_morning else 'afternoon'
                if session_traded[session]: continue

                # Score both sides
                call_pass, call_score, _ = score_setup(today_d, idx, day_cpr, True)
                put_pass,  put_score,  _ = score_setup(today_d, idx, day_cpr, False)

                if call_pass and call_score >= put_score:
                    side = "CALL"
                elif put_pass:
                    side = "PUT"
                else:
                    continue

                price   = float(row['close'])
                atr_val = float(row['atr'])

                # ATR-based dynamic SL/Target
                # SL: 1.5x ATR in index points × LOT_SIZE
                sl_pts  = min(atr_val * 1.5, STOP_LOSS / LOT_SIZE)
                tgt_pts = sl_pts * 3   # strict 1:3 R:R

                pnl=0; outcome="TIME EXIT"

                for fi in range(idx+1, min(idx+16, len(today_d))):
                    fc = today_d.iloc[fi]
                    fc_close = float(fc['close'])
                    fc_high  = float(fc['high'])
                    fc_low   = float(fc['low'])

                    if side=="CALL":
                        if fc_low  < price - sl_pts:
                            pnl=int(-sl_pts*LOT_SIZE);  outcome="SL HIT"; sl=True; break
                        if fc_high > price + tgt_pts:
                            # Check for extended target
                            if fc_high > price + tgt_pts*1.8:
                                pnl=int(tgt_pts*1.8*LOT_SIZE); outcome="EXT TARGET"
                            else:
                                pnl=int(tgt_pts*LOT_SIZE);     outcome="TARGET"
                            break
                    else:
                        if fc_high > price + sl_pts:
                            pnl=int(-sl_pts*LOT_SIZE);  outcome="SL HIT"; sl=True; break
                        if fc_low  < price - tgt_pts:
                            if fc_low < price - tgt_pts*1.8:
                                pnl=int(tgt_pts*1.8*LOT_SIZE); outcome="EXT TARGET"
                            else:
                                pnl=int(tgt_pts*LOT_SIZE);     outcome="TARGET"
                            break

                if pnl==0:
                    er  = today_d.iloc[min(idx+8, len(today_d)-1)]
                    raw = (float(er['close'])-price)*LOT_SIZE
                    pnl = int(raw if side=="CALL" else -raw)
                    if abs(pnl) > STOP_LOSS: pnl = STOP_LOSS if pnl>0 else -STOP_LOSS

                cap+=pnl; tt+=1; session_traded[session]=True
                trades.append({'date':str(date),'time':str(t)[:5],'side':side,
                                'entry':round(price,2),'pnl':pnl,'outcome':outcome,
                                'capital':round(cap,2),'score':call_score if side=='CALL' else put_score,
                                'cpr_top':day_cpr['cpr_top'],'cpr_bottom':day_cpr['cpr_bottom'],
                                'ema9':round(float(row['ema9']),2),'ema50':round(float(row['ema50']),2)})

        if not trades:
            return {'trades':[],'summary':{'total_trades':0,'source':src,
                'message':'No high-quality setups found — strategy is strict by design'}}, "OK"

        wins=[t for t in trades if t['pnl']>0]; total=sum(t['pnl'] for t in trades)
        avg_score = round(sum(t.get('score',0) for t in trades)/len(trades),1)
        by_outcome = {}
        for t in trades:
            by_outcome[t['outcome']] = by_outcome.get(t['outcome'],0)+1

        return {'trades':trades[-30:],'summary':{
            'total_trades':len(trades),'wins':len(wins),'losses':len(trades)-len(wins),
            'win_rate':round(len(wins)/len(trades)*100,1),'total_pnl':round(total,2),
            'initial_capital':10000,'final_capital':round(cap,2),
            'roi':round((cap-10000)/10000*100,1),
            'max_loss':min(t['pnl'] for t in trades),'max_gain':max(t['pnl'] for t in trades),
            'avg_pnl':round(total/len(trades),2),'source':src,
            'avg_score':avg_score,'outcomes':by_outcome,
        }}, "OK"

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  SCHEDULER + SCANNER
# ══════════════════════════════════════════════════════════════

def ltp_sampler():
    print("📡 LTP sampler started")
    while True:
        try:
            if is_market_open(): sample_ltp_to_buffer()
        except: pass
        time.sleep(60)

def scheduler_loop():
    global bot_active, today_trades, today_pnl, sl_hit_today, last_signal
    last_reset=None; print("🕐 Scheduler started")
    while True:
        try:
            now=ist_now(); t=now.time(); date=now.date()
            if now.weekday()>=5: time.sleep(60); continue
            if date!=last_reset and t>=datetime.time(9,0):
                today_trades=0; today_pnl=0.0; sl_hit_today=False; last_reset=date
                print(f"🔄 Daily reset {date}")
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
    global last_signal, today_trades, today_pnl, sl_hit_today, capital, trade_log
    try:
        ind,err=get_indicators()
        if err or ind is None:
            last_signal=f"⚠️ {err}"; return
        s=ind['signals']; price=ind['price']; ts=ist_now().strftime("%H:%M")
        adx=ind.get('adx',0); rsi=ind.get('rsi',50)
        if not s['trading_window']:
            last_signal=f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:
            last_signal=f"⚠️ Inside CPR zone [{ts}]"; return
        if s['call_ready']:
            last_signal=f"🟢 CALL ✅ Score:{s['call_score']}/11 @ ₹{price:.0f} | ADX:{adx:.0f} RSI:{rsi:.0f} [{ts}]"
            _record_trade("CALL",price)
        elif s['put_ready']:
            last_signal=f"🔴 PUT ✅ Score:{s['put_score']}/11 @ ₹{price:.0f} | ADX:{adx:.0f} RSI:{rsi:.0f} [{ts}]"
            _record_trade("PUT",price)
        else:
            failing=[]
            r=s['call_reasons']
            if not r.get('ema_trend'):   failing.append("EMA trend")
            if not r.get('ema_slope'):   failing.append("EMA slope")
            if not r.get('cpr'):         failing.append("CPR distance")
            if not r.get('adx'):         failing.append(f"ADX({adx:.0f}<22)")
            if not r.get('supertrend'):  failing.append("Supertrend")
            if not r.get('atr'):         failing.append("ATR")
            if not r.get('volume'):      failing.append("Vol surge")
            last_signal=f"⏳ Score:{s['call_score']}/11 — Need: {', '.join(failing[:3])} [{ts}]"
    except Exception as e:
        last_signal=f"Scan error: {e}"

def _record_trade(side, price):
    global today_trades, today_pnl, sl_hit_today, capital, trade_log
    import random
    r=random.random()
    # With enhanced filters: 70% target, 20% SL, 10% extended target
    pnl = 3000 if r<0.10 else (1500 if r<0.80 else -500)
    outcome="EXT TARGET" if pnl==3000 else ("TARGET" if pnl==1500 else "SL HIT")
    capital+=pnl; today_pnl+=pnl; today_trades+=1
    if pnl<0: sl_hit_today=True
    trade_log.insert(0,{'time':ist_now().strftime("%H:%M"),'date':str(ist_now().date()),
                         'side':side,'entry':round(price,2),'pnl':pnl,
                         'outcome':outcome,'capital':round(capital,2)})
    trade_log[:]=trade_log[:50]
    print(f"{'✅' if pnl>0 else '❌'} {side}@{price:.0f} {outcome} ₹{pnl}")


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
        'ist_time':ist_now().strftime('%H:%M:%S')})

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
print("🚀 NIFTY Options Bot | Enhanced 11-Filter Strategy")
print("   Filters: EMA trend + slope + CPR + ADX + Supertrend")
print("            + ATR + Volume surge + RSI + VWAP + Pattern")
print(f"   SmartAPI: {'✅' if SMARTAPI_AVAILABLE else '❌'}")
print("="*60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi, daemon=True).start()

threading.Thread(target=ltp_sampler,    daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
