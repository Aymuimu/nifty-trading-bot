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

# ── SmartAPI credentials ───────────────────────────────────────
SMARTAPI_KEY         = os.environ.get('SMARTAPI_KEY', '')
SMARTAPI_CLIENT_ID   = os.environ.get('SMARTAPI_CLIENT_ID', '')
SMARTAPI_PASSWORD    = os.environ.get('SMARTAPI_PASSWORD', '')
SMARTAPI_TOTP_SECRET = os.environ.get('SMARTAPI_TOTP_SECRET', '')

# ── Dhan credentials ───────────────────────────────────────────
DHAN_ACCESS_TOKEN    = os.environ.get('DHAN_ACCESS_TOKEN', '')
DHAN_CLIENT_ID       = os.environ.get('DHAN_CLIENT_ID', '')

# ── Runtime state ──────────────────────────────────────────────
smart_obj      = None
session_data   = None
session_lock   = threading.Lock()
jwt_token      = None
trade_log      = []
today_trades   = 0
today_pnl      = 0.0
capital        = 10000.0
sl_hit_today   = False
last_signal    = "Bot not started"
bot_active     = False
candle_buffer  = {}
last_ltp       = None
data_source    = "dhan"       # "smartapi" | "dhan"  — toggled via API

LOT_SIZE   = 75
SA_BASE    = "https://apiconnect.angelbroking.com"
DHAN_BASE  = "https://api.dhan.co"
MIN_SCORE  = 10   # out of 13


# ══════════════════════════════════════════════════════════════
#  SMARTAPI AUTH
# ══════════════════════════════════════════════════════════════

def generate_totp():
    try:    return pyotp.TOTP(SMARTAPI_TOTP_SECRET).now()
    except: return None

def login_smartapi():
    global smart_obj, session_data, jwt_token
    if not all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID,
                SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
        return False
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
                raw = data.get('data', {}).get('jwtToken', '')
                jwt_token = raw[7:] if raw.startswith('Bearer ') else raw
            print("✅ SmartAPI login OK")
            return True
        print(f"❌ SmartAPI login failed: {data}")
        return False
    except Exception as e:
        print(f"❌ SmartAPI login error: {e}"); return False


# ══════════════════════════════════════════════════════════════
#  TIME HELPERS
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
    return datetime.time(9, 15) <= n.time() <= datetime.time(15, 30)

def is_trading_window():
    t = ist_now().time()
    return (datetime.time(10, 0) <= t <= datetime.time(11, 15) or
            datetime.time(13, 45) <= t <= datetime.time(14, 45))

def window_label():
    t = ist_now().time()
    if datetime.time(10, 0) <= t <= datetime.time(11, 15):  return "Morning (10:00-11:15)"
    if datetime.time(13, 45) <= t <= datetime.time(14, 45): return "Afternoon (1:45-2:45)"
    return "Outside trading windows"

def bucket_15m(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


# ══════════════════════════════════════════════════════════════
#  LIVE PRICE
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    global last_ltp
    # SmartAPI LTP
    if smart_obj:
        try:
            ltp = smart_obj.ltpData("NSE", "NIFTY", "26000")
            if ltp and ltp.get('status'):
                p = float(ltp['data']['ltp'])
                last_ltp = p; return p, "SmartAPI (live)"
        except: pass
    # Dhan LTP
    if DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID:
        try:
            headers = {
                'access-token': DHAN_ACCESS_TOKEN,
                'client-id':    DHAN_CLIENT_ID,
                'Content-Type': 'application/json',
            }
            body = {
                "NSE": ["NIFTY 50"]
            }
            r = req.post(f"{DHAN_BASE}/v2/marketfeed/ltp",
                         json=body, headers=headers, timeout=10)
            d = r.json()
            if d.get('status') == 'success':
                val = d.get('data', {}).get('NSE', {}).get('NIFTY 50', {}).get('last_price')
                if val:
                    last_ltp = float(val); return float(val), "Dhan (live)"
        except: pass
    if last_ltp: return last_ltp, "cached LTP"
    return None, "Price unavailable"


# ══════════════════════════════════════════════════════════════
#  DHAN  HISTORICAL  DATA
# ══════════════════════════════════════════════════════════════

DHAN_INTERVAL_MAP = {
    "15m": "15",
    "1d":  "1440",
    "5m":  "5",
    "1h":  "60",
    "1m":  "1",
}

def fetch_dhan_candles(interval="15m", days=30):
    """
    Fetch NIFTY 50 candle data from Dhan historical API.
    Dhan securityId for NIFTY 50 Index = 13
    exchangeSegment = IDX_I, instrument = INDEX
    """
    if not DHAN_ACCESS_TOKEN or not DHAN_CLIENT_ID:
        return None, "Dhan credentials not set (DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID)"

    dhan_interval = DHAN_INTERVAL_MAP.get(interval, "15")
    to_dt   = last_trading_day()
    from_dt = to_dt - datetime.timedelta(days=days + 5)  # extra buffer for weekends

    headers = {
        'access-token': DHAN_ACCESS_TOKEN,
        'client-id':    DHAN_CLIENT_ID,
        'Content-Type': 'application/json',
        'Accept':       'application/json',
    }

    # Dhan intraday endpoint (for intervals < 1 day)
    if interval != "1d":
        url  = f"{DHAN_BASE}/v2/charts/intraday"
        body = {
            "securityId":      "13",
            "exchangeSegment": "IDX_I",
            "instrument":      "INDEX",
            "interval":        dhan_interval,
            "fromDate":        from_dt.strftime("%Y-%m-%d"),
            "toDate":          to_dt.strftime("%Y-%m-%d"),
        }
    else:
        # Daily candles
        url  = f"{DHAN_BASE}/v2/charts/historical"
        body = {
            "securityId":      "13",
            "exchangeSegment": "IDX_I",
            "instrument":      "INDEX",
            "fromDate":        from_dt.strftime("%Y-%m-%d"),
            "toDate":          to_dt.strftime("%Y-%m-%d"),
        }

    try:
        print(f"📡 Dhan fetch: {url} interval={dhan_interval} {from_dt.date()}→{to_dt.date()}")
        resp = req.post(url, json=body, headers=headers, timeout=20)
        print(f"   Dhan status: {resp.status_code}")
        data = resp.json()
        print(f"   Dhan response keys: {list(data.keys()) if isinstance(data,dict) else type(data)}")

        # Dhan returns arrays: timestamp, open, high, low, close, volume
        if isinstance(data, dict) and 'open' in data:
            timestamps = data.get('timestamp', [])
            opens      = data.get('open',  [])
            highs      = data.get('high',  [])
            lows       = data.get('low',   [])
            closes     = data.get('close', [])
            volumes    = data.get('volume', [0]*len(closes))

            if not closes or len(closes) == 0:
                return None, f"Dhan returned empty arrays. Response: {data}"

            rows = []
            for i in range(len(closes)):
                ts = timestamps[i] if i < len(timestamps) else 0
                # Dhan timestamps are Unix epoch seconds
                try:
                    dt = datetime.datetime.fromtimestamp(int(ts))
                except:
                    dt = datetime.datetime.now() - datetime.timedelta(minutes=(len(closes)-i)*15)
                rows.append({
                    'timestamp': dt,
                    'open':      float(opens[i])   if i < len(opens)   else 0,
                    'high':      float(highs[i])   if i < len(highs)   else 0,
                    'low':       float(lows[i])    if i < len(lows)    else 0,
                    'close':     float(closes[i]),
                    'volume':    float(volumes[i]) if i < len(volumes) else 0,
                })

            df = pd.DataFrame(rows)
            df = df.sort_values('timestamp').reset_index(drop=True)
            df = df[df['close'] > 0]
            print(f"✅ Dhan: {len(df)} candles")
            return df, f"Dhan API"

        # Some Dhan endpoints return data differently
        if isinstance(data, list) and len(data) > 0:
            rows = []
            for item in data:
                rows.append({
                    'timestamp': datetime.datetime.fromtimestamp(item.get('timestamp', 0)),
                    'open':   float(item.get('open',  0)),
                    'high':   float(item.get('high',  0)),
                    'low':    float(item.get('low',   0)),
                    'close':  float(item.get('close', 0)),
                    'volume': float(item.get('volume', 0)),
                })
            df = pd.DataFrame(rows).sort_values('timestamp').reset_index(drop=True)
            df = df[df['close'] > 0]
            print(f"✅ Dhan (list format): {len(df)} candles")
            return df, "Dhan API"

        return None, f"Dhan unexpected response format: {str(data)[:300]}"

    except Exception as e:
        print(f"❌ Dhan fetch error: {e}")
        return None, f"Dhan error: {str(e)}"


# ══════════════════════════════════════════════════════════════
#  SMARTAPI  REST  CANDLES
# ══════════════════════════════════════════════════════════════

SA_INTERVAL_MAP = {
    "15m": "FIFTEEN_MINUTE",
    "1d":  "ONE_DAY",
    "5m":  "FIVE_MINUTE",
    "1h":  "ONE_HOUR",
}

def smartapi_rest_candles(exchange, token, interval, from_dt, to_dt):
    if not jwt_token: return None, "No JWT"
    try:
        headers = {
            'Authorization':    f'Bearer {jwt_token}',
            'Content-Type':     'application/json',
            'Accept':           'application/json',
            'X-UserType':       'USER',
            'X-SourceID':       'WEB',
            'X-ClientLocalIP':  '127.0.0.1',
            'X-ClientPublicIP': '127.0.0.1',
            'X-MACAddress':     '00:00:00:00:00:00',
            'X-PrivateKey':     SMARTAPI_KEY,
        }
        body = {
            "exchange":    exchange, "symboltoken": token,
            "interval":    interval,
            "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        url  = f"{SA_BASE}/rest/secure/angelbroking/historical/v1/getCandleData"
        resp = req.post(url, json=body, headers=headers, timeout=20)
        data = resp.json()
        if data.get('status') and data.get('data'):
            return data['data'], None
        return None, data.get('message', 'empty')
    except Exception as e:
        return None, str(e)

def fetch_smartapi_candles(interval="15m", days=30):
    sa_int  = SA_INTERVAL_MAP.get(interval, "FIFTEEN_MINUTE")
    to_dt   = last_trading_day()
    from_dt = to_dt - datetime.timedelta(days=days)
    for exch, tok in [("NSE","26000"),("NFO","26009"),("NFO","43394"),("NFO","35001")]:
        rows, _ = smartapi_rest_candles(exch, tok, sa_int, from_dt, to_dt)
        if rows and len(rows) > 5:
            df = pd.DataFrame(rows, columns=['timestamp','open','high','low','close','volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            for c in ['open','high','low','close','volume']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df.dropna(subset=['close']).reset_index(drop=True)
            print(f"✅ SmartAPI {exch}/{tok}: {len(df)} rows")
            return df, f"SmartAPI ({exch}/{tok})"
    return None, "SmartAPI: 0 rows from all tokens"


# ══════════════════════════════════════════════════════════════
#  MASTER DATA FETCHER  (respects data_source toggle)
# ══════════════════════════════════════════════════════════════

def get_historical_data(interval="15m", days=30):
    """
    Returns (df, source) using the currently selected data_source.
    Falls back to live buffer for indicators only.
    """
    global data_source

    if data_source == "dhan":
        df, src = fetch_dhan_candles(interval, days)
        if df is not None and len(df) > 5:
            return df, src
        # Fallback to SmartAPI
        print(f"⚠️ Dhan failed ({src}), trying SmartAPI...")
        df, src2 = fetch_smartapi_candles(interval, days)
        if df is not None and len(df) > 5:
            return df, f"SmartAPI (Dhan fallback)"
    else:
        df, src = fetch_smartapi_candles(interval, days)
        if df is not None and len(df) > 5:
            return df, src

    # Live buffer fallback for indicators only (NOT backtest)
    if interval == "15m":
        bdf = buffer_to_df()
        if bdf is not None and len(bdf) >= 5:
            return bdf, "Live LTP buffer"

    return None, src if 'src' in dir() else "All sources failed"


# ══════════════════════════════════════════════════════════════
#  LIVE CANDLE BUFFER
# ══════════════════════════════════════════════════════════════

def sample_ltp_to_buffer():
    global candle_buffer
    price, _ = get_nifty_price()
    if price is None: return
    now    = ist_now()
    key    = bucket_15m(now).strftime("%Y-%m-%d %H:%M")
    bucket = bucket_15m(now)
    if key not in candle_buffer:
        candle_buffer[key] = {'timestamp': bucket, 'open': price,
                               'high': price, 'low': price, 'close': price, 'volume': 1}
    else:
        c = candle_buffer[key]
        c['high']  = max(c['high'], price)
        c['low']   = min(c['low'],  price)
        c['close'] = price
        c['volume'] += 1
    cutoff = (ist_now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    candle_buffer = {k: v for k, v in candle_buffer.items() if k >= cutoff}

def buffer_to_df():
    if len(candle_buffer) < 5: return None
    rows = sorted(candle_buffer.values(), key=lambda x: x['timestamp'])
    df   = pd.DataFrame(rows)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.dropna(subset=['close']).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
#  INDICATOR ENGINE
# ══════════════════════════════════════════════════════════════

def ema(s, p):  return s.ewm(span=p, adjust=False).mean()
def sma(s, p):  return s.rolling(p).mean()

def calc_atr(df, p=14):
    d = df.copy()
    d['tr'] = np.maximum(d['high']-d['low'],
               np.maximum(abs(d['high']-d['close'].shift(1)),
                          abs(d['low'] -d['close'].shift(1))))
    return d['tr'].rolling(p).mean()

def calc_rsi(s, p=14):
    delta = s.diff()
    g = delta.clip(lower=0).rolling(p).mean()
    l = (-delta.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def calc_adx(df, p=14):
    d = df.copy()
    d['tr']  = np.maximum(d['high']-d['low'],
                np.maximum(abs(d['high']-d['close'].shift(1)),
                           abs(d['low'] -d['close'].shift(1))))
    d['dmp'] = np.where((d['high']-d['high'].shift(1)) > (d['low'].shift(1)-d['low']),
                         np.maximum(d['high']-d['high'].shift(1), 0), 0)
    d['dmn'] = np.where((d['low'].shift(1)-d['low']) > (d['high']-d['high'].shift(1)),
                         np.maximum(d['low'].shift(1)-d['low'], 0), 0)
    atr_s = d['tr'].rolling(p).sum()
    dip   = 100 * d['dmp'].rolling(p).sum() / atr_s.replace(0, np.nan)
    din   = 100 * d['dmn'].rolling(p).sum() / atr_s.replace(0, np.nan)
    dx    = 100 * abs(dip-din) / (dip+din).replace(0, np.nan)
    return dx.rolling(p).mean(), dip, din

def calc_supertrend(df, p=10, m=3.0):
    atr_v = calc_atr(df, p)
    hl2   = (df['high'] + df['low']) / 2
    up    = hl2 + m * atr_v
    dn    = hl2 - m * atr_v
    st    = pd.Series(np.nan, index=df.index)
    sd    = pd.Series(1,      index=df.index)
    for i in range(1, len(df)):
        if pd.isna(atr_v.iloc[i]): continue
        pu = up.iloc[i-1] if not pd.isna(up.iloc[i-1]) else up.iloc[i]
        pl = dn.iloc[i-1] if not pd.isna(dn.iloc[i-1]) else dn.iloc[i]
        up.iloc[i] = up.iloc[i] if (up.iloc[i] < pu or df['close'].iloc[i-1] > pu) else pu
        dn.iloc[i] = dn.iloc[i] if (dn.iloc[i] > pl or df['close'].iloc[i-1] < pl) else pl
        pst = st.iloc[i-1] if not pd.isna(st.iloc[i-1]) else dn.iloc[i]
        if pst == pu:   sd.iloc[i] = -1 if df['close'].iloc[i] > up.iloc[i] else 1
        else:            sd.iloc[i] =  1 if df['close'].iloc[i] < dn.iloc[i] else -1
        st.iloc[i] = dn.iloc[i] if sd.iloc[i] == -1 else up.iloc[i]
    return st, sd

def calc_vwap(df):
    df = df.copy(); df['date'] = df['timestamp'].dt.date
    df['tp'] = (df['high'] + df['low'] + df['close']) / 3
    result   = pd.Series(index=df.index, dtype=float)
    for _, g in df.groupby('date'):
        ctv = (g['tp'] * g['volume']).cumsum()
        cv  = g['volume'].cumsum()
        result.loc[g.index] = (ctv / cv.replace(0, np.nan)).values
    return result

def add_indicators(df):
    df = df.copy()
    n  = len(df)
    # Need at least 20 rows for meaningful indicators
    if n < 15:
        return df   # return as-is, caller handles

    df['e9']  = ema(df['close'], min(9,  n-1))
    df['e15'] = ema(df['close'], min(15, n-1))
    df['e21'] = ema(df['close'], min(21, n-1))
    df['e50'] = ema(df['close'], min(50, n-1))
    df['atr'] = calc_atr(df, min(14, n-1))
    df['rsi'] = calc_rsi(df['close'], min(14, n-1))

    try:
        adx_v, dip, din = calc_adx(df, min(14, n-1))
        df['adx'] = adx_v; df['dip'] = dip; df['din'] = din
    except:
        df['adx'] = 25; df['dip'] = 25; df['din'] = 20

    try:
        st, sd    = calc_supertrend(df, min(10, n-1), 3.0)
        df['st']  = st; df['sd'] = sd
    except:
        df['st']  = df['close']; df['sd'] = -1

    try:    df['vwap'] = calc_vwap(df)
    except: df['vwap'] = df['close']

    bm = sma(df['close'], min(20, n-1))
    bs = df['close'].rolling(min(20, n-1)).std()
    df['bb_w'] = (bm + 2*bs - (bm - 2*bs)) / bm.replace(0, np.nan)
    df['mom4'] = df['close'] - df['close'].shift(min(4, n-2))
    df['v20']  = df['volume'].rolling(min(20, n-1)).mean()
    df['vr']   = df['volume'] / df['v20'].replace(0, np.nan)

    # Only drop rows missing the critical columns
    return df.dropna(subset=['e9', 'atr', 'rsi']).reset_index(drop=True)

def calc_cpr(h, l, c):
    p = (h+l+c)/3; bc = (h+l)/2; tc = (p-bc)+p
    return {'pivot': round(p,2), 'cpr_top': round(max(bc,tc),2),
            'cpr_bottom': round(min(bc,tc),2)}


# ══════════════════════════════════════════════════════════════
#  13-POINT ENTRY SCORING
# ══════════════════════════════════════════════════════════════

def score_entry(df, idx, day_cpr, is_call):
    if idx < 5 or idx >= len(df): return False, 0, {}
    r   = df.iloc[idx]
    r1  = df.iloc[idx-1]
    pr5 = df.iloc[max(0, idx-5):idx]

    price = float(r['close']); o = float(r['open'])
    e9  = float(r['e9']);  e15 = float(r.get('e15', e9))
    e21 = float(r.get('e21', e15)); e50 = float(r.get('e50', e21))
    adx_v = float(r.get('adx', 20)); dip_v = float(r.get('dip', 20)); din_v = float(r.get('din', 20))
    sd_v  = int(r.get('sd', -1))
    rsi_v = float(r.get('rsi', 50))
    vwap_v = float(r.get('vwap', price))
    bb_w   = float(r.get('bb_w', 0)) if not pd.isna(r.get('bb_w', 0)) else 0
    prev_bbw = float(r1.get('bb_w', bb_w)) if not pd.isna(r1.get('bb_w', bb_w)) else bb_w
    mom  = float(r.get('mom4', 0)) if not pd.isna(r.get('mom4', 0)) else 0
    vr   = float(r.get('vr',   1)) if not pd.isna(r.get('vr',   1)) else 1.0

    # EMA9 slope: 4 consecutive bars rising/falling
    e9_sl = df['e9'].iloc[max(0, idx-5):idx+1]
    n_sl  = min(4, len(e9_sl)-1)
    e9_up = n_sl > 0 and all(e9_sl.iloc[i] < e9_sl.iloc[i+1] for i in range(n_sl))
    e9_dn = n_sl > 0 and all(e9_sl.iloc[i] > e9_sl.iloc[i+1] for i in range(n_sl))

    cpr_buf = price * 0.0015

    if is_call:
        core = {
            'ema_stack':  bool(price > e9 and e9 > e15 and e15 > e21 and e21 > e50),
            'ema_slope':  e9_up,
            'supertrend': sd_v == -1,
            'adx':        adx_v >= 22 and dip_v > din_v,
            'cpr':        bool(day_cpr) and price > day_cpr['cpr_top'] + cpr_buf,
            'candle':     price > o,
        }
        p5h = float(pr5['high'].max()) if len(pr5) > 0 else price
        scored = {
            'rsi':        48 <= rsi_v <= 70,
            'vwap':       price > vwap_v,
            'vol_surge':  vr >= 1.8,
            'momentum':   mom > 0,
            'bb_expand':  bb_w > prev_bbw * 1.01,
            'cpr_narrow': bool(day_cpr) and (day_cpr['cpr_top']-day_cpr['cpr_bottom']) < 50,
            'breakout':   price > p5h,
        }
    else:
        core = {
            'ema_stack':  bool(price < e9 and e9 < e15 and e15 < e21 and e21 < e50),
            'ema_slope':  e9_dn,
            'supertrend': sd_v == 1,
            'adx':        adx_v >= 22 and din_v > dip_v,
            'cpr':        bool(day_cpr) and price < day_cpr['cpr_bottom'] - cpr_buf,
            'candle':     price < o,
        }
        p5l = float(pr5['low'].min()) if len(pr5) > 0 else price
        scored = {
            'rsi':        30 <= rsi_v <= 52,
            'vwap':       price < vwap_v,
            'vol_surge':  vr >= 1.8,
            'momentum':   mom < 0,
            'bb_expand':  bb_w > prev_bbw * 1.01,
            'cpr_narrow': bool(day_cpr) and (day_cpr['cpr_top']-day_cpr['cpr_bottom']) < 50,
            'breakdown':  price < p5l,
        }

    reasons = {**core, **scored}
    if not all(core.values()):
        return False, sum(reasons.values()), reasons
    if sum(scored.values()) < 4:
        return False, sum(reasons.values()), reasons
    total = sum(reasons.values())
    return total >= MIN_SCORE, total, reasons


# ══════════════════════════════════════════════════════════════
#  EXIT ENGINE  (ATR-based trailing target)
# ══════════════════════════════════════════════════════════════

def simulate_exit(today_d, idx, side):
    row   = today_d.iloc[idx]
    price = float(row['close'])
    atr_v = float(row['atr']) if not pd.isna(row['atr']) else 50.0
    sl_pts = max(atr_v * 1.2, 5.0)
    t1_pts = atr_v * 2.0
    t2_pts = atr_v * 3.5
    sl_r   = min(int(sl_pts * LOT_SIZE), 600)
    t1_r   = min(int(t1_pts * LOT_SIZE), 900)
    t2_r   = min(int(t2_pts * LOT_SIZE), 2100)
    t1_hit = False; trail = price

    for fi in range(idx+1, min(idx+20, len(today_d))):
        fc = today_d.iloc[fi]
        fh = float(fc['high']); fl = float(fc['low']); fc_ = float(fc['close'])
        if side == "CALL":
            if t1_hit:
                trail = max(trail, fc_ - atr_v*0.7)
                if fl < trail:
                    p = max(-t1_r//2, min(t1_r, int((trail-price)*LOT_SIZE*0.5)))
                    return t1_r//2 + p, "TRAIL EXIT"
                if fh > price + t2_pts: return t1_r//2 + t2_r//2, "FULL TARGET"
            else:
                if fl < price - sl_pts:  return -sl_r, "SL HIT"
                if fh > price + t1_pts:  t1_hit = True; trail = price
        else:
            if t1_hit:
                trail = min(trail, fc_ + atr_v*0.7)
                if fh > trail:
                    p = max(-t1_r//2, min(t1_r, int((price-trail)*LOT_SIZE*0.5)))
                    return t1_r//2 + p, "TRAIL EXIT"
                if fl < price - t2_pts: return t1_r//2 + t2_r//2, "FULL TARGET"
            else:
                if fh > price + sl_pts:  return -sl_r, "SL HIT"
                if fl < price - t1_pts:  t1_hit = True; trail = price

    er = today_d.iloc[min(idx+10, len(today_d)-1)]
    ep = float(er['close'])
    if t1_hit:
        raw = int((ep-price)*LOT_SIZE*0.5) if side=="CALL" else int((price-ep)*LOT_SIZE*0.5)
        return t1_r//2 + max(-t1_r//2, min(t1_r, raw)), "TIME(T1+trail)"
    raw = int((ep-price)*LOT_SIZE) if side=="CALL" else int((price-ep)*LOT_SIZE)
    return max(-sl_r, min(t1_r, raw)), "TIME EXIT"


# ══════════════════════════════════════════════════════════════
#  BACKTEST  (no synthetic — real API data only)
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        # Fetch from selected source directly (no buffer fallback for backtest)
        if data_source == "dhan":
            df, src = fetch_dhan_candles("15m", days)
        else:
            df, src = fetch_smartapi_candles("15m", days)

        if df is None or len(df) == 0:
            if data_source == "dhan":
                msg = (f"Dhan returned no data. Error: {src}. "
                       "Check: 1) DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID are set in Railway. "
                       "2) Your Dhan data subscription is active. "
                       "3) Try /api/debug-data to diagnose.")
            else:
                msg = (f"SmartAPI returned no data. Error: {src}. "
                       "Angel One historical API requires a paid data subscription. "
                       "Switch to Dhan source or contact Angel One support.")
            return None, msg

        df = add_indicators(df)
        if len(df) < 20:
            return None, (f"Only {len(df)} rows after indicator calc (need 20+). "
                          f"Source: {src}. Try requesting more days.")

        df['date'] = df['timestamp'].dt.date
        dates      = sorted(df['date'].unique())
        trades=[]; cap=10000.0; peak=10000.0; max_dd=0.0

        for i, date in enumerate(dates):
            if i == 0: continue
            prev_d = df[df['date'] == dates[i-1]]
            if len(prev_d) == 0: continue
            day_cpr = calc_cpr(float(prev_d['high'].max()),
                               float(prev_d['low'].min()),
                               float(prev_d['close'].iloc[-1]))
            today_d   = df[df['date'] == date].reset_index(drop=True)
            if len(today_d) < 4: continue
            tt=0; sl_day=False; sess_done={'morning':False,'afternoon':False}

            for idx in range(5, len(today_d)):
                if tt >= 2 or sl_day: break
                row = today_d.iloc[idx]; t = row['timestamp'].time()
                in_m = datetime.time(10,0) <= t <= datetime.time(11,15)
                in_a = datetime.time(13,45) <= t <= datetime.time(14,45)
                if not (in_m or in_a): continue
                sess = 'morning' if in_m else 'afternoon'
                if sess_done[sess]: continue

                cp, cs, _ = score_entry(today_d, idx, day_cpr, True)
                pp, ps, _ = score_entry(today_d, idx, day_cpr, False)
                if cp and cs >= ps:  side='CALL'; score=cs
                elif pp:             side='PUT';  score=ps
                else:                continue

                price = float(today_d.iloc[idx]['close'])
                pnl, outcome = simulate_exit(today_d, idx, side)
                cap += pnl; tt += 1
                if pnl < 0: sl_day = True
                peak   = max(peak, cap)
                max_dd = max(max_dd, (peak-cap)/peak*100 if peak>0 else 0)
                sess_done[sess] = True

                trades.append({
                    'date':       str(date),   'time':    str(t)[:5],
                    'side':       side,         'entry':   round(price, 2),
                    'pnl':        pnl,          'outcome': outcome,
                    'capital':    round(cap,2), 'score':   score,
                    'cpr_top':    day_cpr['cpr_top'],
                    'cpr_bottom': day_cpr['cpr_bottom'],
                    'ema9':  round(float(today_d.iloc[idx]['e9']),2),
                    'ema50': round(float(today_d.iloc[idx].get('e50', today_d.iloc[idx]['e9'])),2),
                })

        if not trades:
            return {'trades':[],'summary':{
                'total_trades':0,'source':src,
                'message': f'No setups passed {MIN_SCORE}/13 filters. '
                           f'Strategy is strict — fewer but higher quality trades.',
            }}, "OK"

        wins  = [t for t in trades if t['pnl']>0]
        total = sum(t['pnl'] for t in trades)
        wr    = round(len(wins)/len(trades)*100, 1)
        roi   = round((cap-10000)/10000*100, 1)
        by_out= {}
        for t in trades: by_out[t['outcome']] = by_out.get(t['outcome'],0)+1
        max_ws=cur_w=max_ls=cur_l=0
        for t in trades:
            if t['pnl']>0: cur_w+=1; max_ws=max(max_ws,cur_w); cur_l=0
            else:           cur_l+=1; max_ls=max(max_ls,cur_l); cur_w=0

        return {'trades': trades[-50:], 'summary': {
            'total_trades':    len(trades),
            'wins':            len(wins),
            'losses':          len(trades)-len(wins),
            'win_rate':        wr,
            'total_pnl':       round(total,2),
            'initial_capital': 10000,
            'final_capital':   round(cap,2),
            'roi':             roi,
            'max_drawdown':    round(max_dd,1),
            'max_gain':        max(t['pnl'] for t in trades),
            'max_loss':        min(t['pnl'] for t in trades),
            'avg_pnl':         round(total/len(trades),2),
            'avg_score':       round(sum(t['score'] for t in trades)/len(trades),1),
            'max_win_streak':  max_ws,
            'max_loss_streak': max_ls,
            'outcomes':        by_out,
            'source':          src,
            'data_source_used': data_source,
        }}, "OK"

    except Exception as e:
        import traceback; print(traceback.format_exc())
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  LIVE INDICATORS
# ══════════════════════════════════════════════════════════════

def get_indicators():
    df, src = get_historical_data("15m", 10)
    if df is None or len(df) < 5:
        # Try buffer
        df = buffer_to_df(); src = "Live LTP buffer"
    if df is None or len(df) < 5:
        return None, "Not enough data. Login and wait for market hours."

    df = add_indicators(df)
    if len(df) < 3:
        return None, f"Only {len(df)} candles — need more data"

    r0 = df.iloc[-1]; r1 = df.iloc[-2]
    r2 = df.iloc[-3] if len(df) >= 3 else r1
    price = float(r0['close'])
    lp, _ = get_nifty_price()
    if lp: price = lp

    # CPR from previous day
    df_d, _ = get_historical_data("1d", 5)
    day_cpr = None
    if df_d is not None and len(df_d) >= 2:
        pr = df_d.iloc[-2]
        day_cpr = calc_cpr(float(pr['high']), float(pr['low']), float(pr['close']))

    cp, cs, cr = score_entry(df, len(df)-1, day_cpr, True)
    pp, ps, pr = score_entry(df, len(df)-1, day_cpr, False)
    in_win     = is_trading_window()
    inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])

    def safe(v, dec=2):
        try: return round(float(v), dec) if not pd.isna(v) else 0
        except: return 0

    return {
        'price':      round(price, 2),
        'ema9':       safe(r0.get('e9',  price)),
        'ema15':      safe(r0.get('e15', price)),
        'ema21':      safe(r0.get('e21', price)),
        'ema50':      safe(r0.get('e50', price)),
        'atr':        safe(r0.get('atr', 50)),
        'atr_rising': bool(safe(r0.get('atr',0)) > safe(r1.get('atr',0)) > safe(r2.get('atr',0))),
        'adx':        safe(r0.get('adx', 20), 1),
        'rsi':        safe(r0.get('rsi', 50), 1),
        'vwap':       safe(r0.get('vwap', price)),
        'volume':     int(r0.get('volume', 0)),
        'vol_ratio':  safe(r0.get('vr', 1)),
        'cpr':        day_cpr,
        'signals': {
            'call_ready':     cp and in_win,
            'put_ready':      pp and in_win,
            'call_score':     cs, 'put_score': ps,
            'min_score':      MIN_SCORE,
            'call_reasons':   cr, 'put_reasons': pr,
            'trading_window': in_win,
            'inside_cpr':     inside_cpr,
            'call_trend':     cr.get('ema_stack', False),
            'put_trend':      pr.get('ema_stack', False),
            'call_cpr':       cr.get('cpr', False),
            'put_cpr':        pr.get('cpr', False),
            'atr_ok':         cr.get('adx', False) or pr.get('adx', False),
            'volume_ok':      cr.get('vol_surge', False) or pr.get('vol_surge', False),
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
            if is_market_open(): sample_ltp_to_buffer()
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
        if err or ind is None: last_signal=f"⚠️ {err}"; return
        s=ind['signals']; price=ind['price']; ts=ist_now().strftime("%H:%M")
        if not s['trading_window']: last_signal=f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:         last_signal=f"⚠️ Inside CPR [{ts}]"; return
        if s['call_ready']:
            last_signal=f"🟢 CALL ✅ {s['call_score']}/{MIN_SCORE} @ ₹{price:.0f} [{ts}]"
            _record_sim("CALL", price)
        elif s['put_ready']:
            last_signal=f"🔴 PUT ✅ {s['put_score']}/{MIN_SCORE} @ ₹{price:.0f} [{ts}]"
            _record_sim("PUT", price)
        else:
            last_signal=f"⏳ Score {s['call_score']}/{MIN_SCORE} — waiting [{ts}]"
    except Exception as e: last_signal=f"Scan: {e}"

def _record_sim(side, price):
    global today_trades, today_pnl, sl_hit_today, capital, trade_log
    import random; r=random.random()
    pnl=2100 if r<0.12 else (900 if r<0.82 else -480)
    outcome="FULL TARGET" if pnl>1000 else ("TARGET" if pnl>0 else "SL HIT")
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
    try:    return send_from_directory('public','index.html')
    except: return "<h2>Bot running</h2><a href='/api/test'>Test</a>"

@app.route('/api/test')
def api_test():
    return jsonify({
        'status':'ok','logged_in':smart_obj is not None,'bot_active':bot_active,
        'market_open':is_market_open(),'trading_window':is_trading_window(),
        'window_label':window_label(),'today_trades':today_trades,
        'today_pnl':today_pnl,'capital':capital,'last_signal':last_signal,
        'buffer_bars':len(candle_buffer),'data_source':data_source,
        'dhan_configured':bool(DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID),
        'smartapi_configured':bool(SMARTAPI_KEY and smart_obj),
        'min_score':MIN_SCORE,'ist_time':ist_now().strftime('%H:%M:%S'),
    })

@app.route('/api/login', methods=['POST'])
def api_login():
    s=login_smartapi()
    return jsonify({'success':s,'logged_in':smart_obj is not None,
                    'message':'✅ Login successful!' if s else '❌ Login failed'})

@app.route('/api/session-status')
def api_session():
    return jsonify({'logged_in':smart_obj is not None,
                    'login_time':session_data.get('login_time') if session_data else None,
                    'jwt_set':bool(jwt_token),'data_source':data_source})

@app.route('/api/set-source', methods=['POST'])
def api_set_source():
    """Switch between smartapi and dhan data sources."""
    global data_source
    body = request.get_json() or {}
    src  = body.get('source','').lower()
    if src not in ('smartapi','dhan'):
        return jsonify({'error':'source must be "smartapi" or "dhan"'}), 400
    data_source = src
    print(f"🔄 Data source switched to: {data_source}")
    return jsonify({'success':True,'data_source':data_source,
                    'message':f'✅ Now using {data_source.upper()} for data'})

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
        'data_source':data_source,'ist_time':n.strftime('%H:%M:%S'),
        'day':n.strftime('%A'),'date':n.strftime('%Y-%m-%d')})

@app.route('/api/indicators')
def api_indicators():
    ind,err=get_indicators()
    if err: return jsonify({'success':False,'error':err})
    return jsonify({'success':True,**ind,'last_signal':last_signal,
                    'timestamp':ist_now().isoformat()})

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

    # Test Dhan
    dhan_15m,dhan_15m_src=fetch_dhan_candles("15m",5)
    dhan_1d,dhan_1d_src  =fetch_dhan_candles("1d",5)

    # Test SmartAPI
    sa_15m,sa_15m_src=fetch_smartapi_candles("15m",5)
    sa_1d,sa_1d_src  =fetch_smartapi_candles("1d",5)

    return jsonify({
        'live_price':  {'price':p,'source':ps},
        'data_source': data_source,
        'buffer_bars': len(candle_buffer),
        'jwt_set':     bool(jwt_token),
        'dhan': {
            'configured': bool(DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID),
            '15m_rows':   len(dhan_15m) if dhan_15m is not None else 0,
            '15m_source': dhan_15m_src,
            '1d_rows':    len(dhan_1d)  if dhan_1d  is not None else 0,
            '1d_source':  dhan_1d_src,
        },
        'smartapi': {
            'logged_in': smart_obj is not None,
            '15m_rows':  len(sa_15m) if sa_15m is not None else 0,
            '15m_source':sa_15m_src,
            '1d_rows':   len(sa_1d)  if sa_1d  is not None else 0,
            '1d_source': sa_1d_src,
        },
    })


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("=" * 60)
print(f"🚀 NIFTY Elite Bot | Dual API | 13-pt scoring | Min {MIN_SCORE}/13")
print(f"   SmartAPI : {'✅ lib OK' if SMARTAPI_AVAILABLE else '❌'}")
print(f"   Dhan     : {'✅ configured' if (DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID) else '⚠️ no credentials'}")
print(f"   Default  : DHAN (change via /api/set-source)")
print("=" * 60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi, daemon=True).start()

threading.Thread(target=ltp_sampler,    daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
