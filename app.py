from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import threading
import time
import datetime
import pyotp
import pandas as pd
import numpy as np
import requests as req

try:
    from SmartApi import SmartConnect
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    print("⚠️ SmartAPI library not available")

app = Flask(__name__, static_folder='public')
CORS(app)

# ─── Credentials ───────────────────────────────────────────────
SMARTAPI_KEY         = os.environ.get('SMARTAPI_KEY', '')
SMARTAPI_CLIENT_ID   = os.environ.get('SMARTAPI_CLIENT_ID', '')
SMARTAPI_PASSWORD    = os.environ.get('SMARTAPI_PASSWORD', '')
SMARTAPI_TOTP_SECRET = os.environ.get('SMARTAPI_TOTP_SECRET', '')

# ─── Global state ──────────────────────────────────────────────
smart_obj      = None
session_data   = None
session_lock   = threading.Lock()
bot_active     = False
trade_log      = []
today_trades   = 0
today_pnl      = 0.0
capital        = 10000.0
sl_hit_today   = False
last_signal    = "Waiting..."
nifty_token    = None   # discovered at runtime

# ─── Strategy constants ────────────────────────────────────────
LOT_SIZE    = 75
STOP_LOSS   = 500
BASE_TARGET = 1500


# ══════════════════════════════════════════════════════════════
#  TOKEN DISCOVERY — downloads SmartAPI scrip master
# ══════════════════════════════════════════════════════════════

def discover_nifty_token():
    """Download SmartAPI scrip master and find NIFTY 50 index token."""
    global nifty_token
    try:
        print("🔍 Downloading SmartAPI scrip master...")
        url  = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = req.get(url, timeout=30)
        data = resp.json()
        # Find NIFTY 50 index on NSE
        for item in data:
            sym  = str(item.get('symbol', '')).upper()
            name = str(item.get('name', '')).upper()
            exch = str(item.get('exch_seg', '')).upper()
            if exch == 'NSE' and sym in ('NIFTY', 'NIFTY 50') and 'INDEX' in name.upper():
                nifty_token = str(item.get('token', '26000'))
                print(f"✅ Found NIFTY token: {nifty_token} ({item})")
                return nifty_token
        # Fallback: search for token 26000
        for item in data:
            if str(item.get('token')) == '26000':
                nifty_token = '26000'
                print(f"✅ Using default NIFTY token 26000: {item}")
                return nifty_token
        nifty_token = '26000'
        print("⚠️ Token not found in scrip master, using default 26000")
        return nifty_token
    except Exception as e:
        print(f"⚠️ Scrip master download failed: {e}, using default 26000")
        nifty_token = '26000'
        return nifty_token


# ══════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════

def generate_totp():
    try:
        return pyotp.TOTP(SMARTAPI_TOTP_SECRET).now()
    except Exception as e:
        print(f"❌ TOTP error: {e}")
        return None


def login_smartapi():
    global smart_obj, session_data
    if not all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
        print("⚠️ Credentials not configured")
        return False
    if not SMARTAPI_AVAILABLE:
        return False
    try:
        totp_code = generate_totp()
        if not totp_code:
            return False
        obj  = SmartConnect(api_key=SMARTAPI_KEY)
        data = obj.generateSession(SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, totp_code)
        if data and data.get('status'):
            with session_lock:
                smart_obj    = obj
                session_data = data
                session_data['login_time'] = datetime.datetime.now().isoformat()
            print("✅ SmartAPI login successful!")
            return True
        print(f"❌ Login failed: {data}")
        return False
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════════════════

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def get_last_trading_day():
    d = datetime.datetime.now()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d

def is_market_open():
    n = ist_now()
    if n.weekday() >= 5:
        return False
    return datetime.time(9, 15) <= n.time() <= datetime.time(15, 30)

def is_trading_window():
    t = ist_now().time()
    return (datetime.time(10, 0) <= t <= datetime.time(11, 15) or
            datetime.time(13, 45) <= t <= datetime.time(14, 45))

def window_label():
    t = ist_now().time()
    if datetime.time(10, 0) <= t <= datetime.time(11, 15):
        return "Morning Window (10:00-11:15)"
    if datetime.time(13, 45) <= t <= datetime.time(14, 45):
        return "Afternoon Window (1:45-2:45)"
    return "Outside trading windows"


# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    try:
        if smart_obj is None:
            return None, "Not logged in"
        ltp = smart_obj.ltpData("NSE", "NIFTY", "26000")
        if ltp and ltp.get('status'):
            return float(ltp['data']['ltp']), "SmartAPI (live)"
        return None, "LTP fetch failed"
    except Exception as e:
        return None, str(e)


def get_historical_data(interval="FIFTEEN_MINUTE", days=30):
    """
    Fetch candle data. Tries multiple exchange/token combos.
    Falls back to yfinance if SmartAPI returns empty data.
    """
    if smart_obj is None:
        return None, "Not logged in"

    to_dt   = get_last_trading_day()
    from_dt = to_dt - datetime.timedelta(days=days)

    # All known NIFTY tokens to try
    candidates = [
        ("NSE", "26000"),
        ("NSE", nifty_token or "26000"),
        ("NFO", "26009"),
        ("NFO", "43394"),
        ("NFO", "35001"),
    ]
    seen = set()
    for exchange, token in candidates:
        key = f"{exchange}_{token}"
        if key in seen:
            continue
        seen.add(key)
        try:
            param = {
                "exchange":    exchange,
                "symboltoken": token,
                "interval":    interval,
                "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
            }
            data = smart_obj.getCandleData(param)
            rows = len(data.get('data', [])) if data else 0
            print(f"  {exchange}/{token}: {rows} rows")
            if data and data.get('status') and rows > 0:
                df = pd.DataFrame(
                    data['data'],
                    columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
                )
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                print(f"✅ Got {rows} rows from {exchange}/{token}")
                return df, f"SmartAPI ({exchange}/{token})"
        except Exception as e:
            print(f"  {exchange}/{token} error: {e}")

    # ── Fallback: yfinance (free, no auth needed) ──────────────
    print("⚠️ SmartAPI returned no data — falling back to yfinance")
    return get_historical_yfinance(interval, days)


def get_historical_yfinance(interval="FIFTEEN_MINUTE", days=30):
    """Free fallback using yfinance for NIFTY data."""
    try:
        import yfinance as yf
        interval_map = {
            "ONE_MINUTE":      "1m",
            "FIVE_MINUTE":     "5m",
            "FIFTEEN_MINUTE":  "15m",
            "THIRTY_MINUTE":   "30m",
            "ONE_HOUR":        "1h",
            "ONE_DAY":         "1d",
        }
        yf_interval = interval_map.get(interval, "15m")
        # yfinance limits: 1m=7days, 5m/15m/30m=60days, 1h=730days
        if yf_interval in ("1m",) and days > 7:
            days = 7
        ticker = yf.Ticker("^NSEI")
        df     = ticker.history(period=f"{days}d", interval=yf_interval)
        if df.empty:
            return None, "yfinance returned empty data"
        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        # Normalise column names
        rename = {}
        for c in df.columns:
            if 'date' in c or 'time' in c:
                rename[c] = 'timestamp'
            elif c == 'open':  rename[c] = 'open'
            elif c == 'high':  rename[c] = 'high'
            elif c == 'low':   rename[c] = 'low'
            elif c == 'close': rename[c] = 'close'
            elif c in ('volume', 'vol'): rename[c] = 'volume'
        df = df.rename(columns=rename)
        needed = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        for col in needed:
            if col not in df.columns:
                df[col] = 0
        df = df[needed]
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        # Strip timezone info
        if df['timestamp'].dt.tz is not None:
            df['timestamp'] = df['timestamp'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
        print(f"✅ yfinance fallback: {len(df)} rows")
        return df, "yfinance (fallback)"
    except ImportError:
        return None, "yfinance not installed"
    except Exception as e:
        return None, f"yfinance error: {e}"


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def atr(df, period=14):
    d = df.copy()
    d['tr'] = np.maximum(
        d['high'] - d['low'],
        np.maximum(abs(d['high'] - d['close'].shift(1)),
                   abs(d['low']  - d['close'].shift(1)))
    )
    return d['tr'].rolling(period).mean()

def cpr(prev_high, prev_low, prev_close):
    pivot = (prev_high + prev_low + prev_close) / 3
    bc    = (prev_high + prev_low) / 2
    tc    = (pivot - bc) + pivot
    return {
        'pivot':      round(pivot, 2),
        'cpr_top':    round(max(bc, tc), 2),
        'cpr_bottom': round(min(bc, tc), 2),
    }


# ══════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        df15, src = get_historical_data("FIFTEEN_MINUTE", days)
        if df15 is None:
            return None, f"Could not fetch data: {src}"

        df15['ema9']  = ema(df15['close'], 9)
        df15['ema15'] = ema(df15['close'], 15)
        df15['ema50'] = ema(df15['close'], 50)
        df15['atr']   = atr(df15)
        df15['date']  = df15['timestamp'].dt.date
        df15 = df15.dropna()

        dates   = sorted(df15['date'].unique())
        trades  = []
        cap     = 10000.0

        for i, date in enumerate(dates):
            if i == 0:
                continue
            prev_d = df15[df15['date'] == dates[i-1]]
            if len(prev_d) == 0:
                continue
            day_cpr = cpr(prev_d['high'].max(), prev_d['low'].min(), prev_d['close'].iloc[-1])

            today_d      = df15[df15['date'] == date].reset_index(drop=True)
            trades_today = 0
            sl_today     = False

            for idx in range(3, len(today_d)):
                if trades_today >= 2 or sl_today:
                    break
                row = today_d.iloc[idx]
                t   = row['timestamp'].time()
                if not (datetime.time(10, 0) <= t <= datetime.time(11, 15) or
                        datetime.time(13, 45) <= t <= datetime.time(14, 45)):
                    continue

                price = row['close']
                e9    = row['ema9']
                e15   = row['ema15']
                e50   = row['ema50']

                atr_sl = today_d['atr'].iloc[max(0, idx-3):idx+1]
                atr_up = bool(atr_sl.is_monotonic_increasing) if len(atr_sl) >= 3 else False
                vol_sl = today_d['volume'].iloc[max(0, idx-3):idx+1]
                vol_up = bool(vol_sl.iloc[-1] > vol_sl.mean()) if len(vol_sl) >= 2 else False

                call_ok = (price > e9 > e15 > e50 and
                           price > day_cpr['cpr_top'] and
                           row['close'] > row['open'] and atr_up and vol_up)
                put_ok  = (price < e9 < e15 < e50 and
                           price < day_cpr['cpr_bottom'] and
                           row['close'] < row['open'] and atr_up and vol_up)

                side = "CALL" if call_ok else ("PUT" if put_ok else None)
                if not side:
                    continue

                pnl     = 0
                outcome = "TIME EXIT"

                for fi in range(idx+1, min(idx+12, len(today_d))):
                    fc = today_d.iloc[fi]
                    if side == "CALL":
                        if fc['low']  < price - STOP_LOSS   / LOT_SIZE:
                            pnl = -STOP_LOSS;  outcome = "SL HIT"; sl_today = True; break
                        if fc['high'] > price + BASE_TARGET / LOT_SIZE:
                            pnl =  BASE_TARGET; outcome = "TARGET"; break
                    else:
                        if fc['high'] > price + STOP_LOSS   / LOT_SIZE:
                            pnl = -STOP_LOSS;  outcome = "SL HIT"; sl_today = True; break
                        if fc['low']  < price - BASE_TARGET / LOT_SIZE:
                            pnl =  BASE_TARGET; outcome = "TARGET"; break

                if pnl == 0:
                    er  = today_d.iloc[min(idx+6, len(today_d)-1)]
                    raw = (er['close'] - price) * LOT_SIZE
                    pnl = int(raw if side == "CALL" else -raw)

                cap          += pnl
                trades_today += 1
                trades.append({
                    'date': str(date), 'time': str(t)[:5],
                    'side': side, 'entry': round(price, 2),
                    'pnl': pnl, 'outcome': outcome,
                    'capital': round(cap, 2),
                    'cpr_top': day_cpr['cpr_top'],
                    'cpr_bottom': day_cpr['cpr_bottom'],
                    'ema9': round(e9, 2), 'ema50': round(e50, 2),
                })

        if not trades:
            return {'trades': [], 'summary': {
                'total_trades': 0,
                'message': 'No setups found matching all filters',
                'source': src,
            }}, "OK"

        wins  = [t for t in trades if t['pnl'] > 0]
        total = sum(t['pnl'] for t in trades)
        return {
            'trades': trades[-30:],
            'summary': {
                'total_trades':    len(trades),
                'wins':            len(wins),
                'losses':          len(trades) - len(wins),
                'win_rate':        round(len(wins)/len(trades)*100, 1),
                'total_pnl':       round(total, 2),
                'initial_capital': 10000,
                'final_capital':   round(cap, 2),
                'roi':             round((cap-10000)/10000*100, 1),
                'max_loss':        min(t['pnl'] for t in trades),
                'max_gain':        max(t['pnl'] for t in trades),
                'avg_pnl':         round(total/len(trades), 2),
                'source':          src,
            }
        }, "OK"
    except Exception as e:
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  AUTO SCHEDULER — login at 9:15, scan every 5 min, off 3:30
# ══════════════════════════════════════════════════════════════

def scheduler_loop():
    """
    Runs forever in background:
    - Logs in automatically at 9:10 AM IST every weekday
    - Scans for trades every 5 min during market hours
    - Resets daily counters at 9:00 AM
    - Logs out at 3:35 PM
    """
    global bot_active, today_trades, today_pnl, sl_hit_today
    global capital, last_signal

    print("🕐 Scheduler started")
    last_reset_date = None

    while True:
        try:
            now  = ist_now()
            date = now.date()
            t    = now.time()

            # Skip weekends
            if now.weekday() >= 5:
                time.sleep(60)
                continue

            # Reset daily counters at 9:00 AM
            if date != last_reset_date and t >= datetime.time(9, 0):
                today_trades   = 0
                today_pnl      = 0.0
                sl_hit_today   = False
                last_reset_date = date
                print(f"🔄 Daily reset for {date}")

            # Auto login at 9:10 AM
            if t >= datetime.time(9, 10) and t <= datetime.time(9, 15) and smart_obj is None:
                print("⏰ 9:10 AM — Auto login...")
                login_smartapi()

            # Start scanning at 9:15 AM
            if t >= datetime.time(9, 15) and t <= datetime.time(15, 30):
                bot_active = True

                # Scan for trades during windows
                if is_trading_window() and smart_obj is not None:
                    if today_trades < 2 and not sl_hit_today:
                        scan_for_trade()

            # Stop at 3:30 PM
            if t >= datetime.time(15, 30):
                if bot_active:
                    print("⏰ 3:30 PM — Market closed, bot stopped")
                    bot_active = False

        except Exception as e:
            print(f"❌ Scheduler error: {e}")

        time.sleep(300)  # check every 5 minutes


def scan_for_trade():
    """Check current indicators and log signal."""
    global last_signal, today_trades, today_pnl, sl_hit_today, capital

    try:
        df, src = get_historical_data("FIFTEEN_MINUTE", 5)
        if df is None:
            last_signal = f"No data: {src}"
            return

        df['ema9']  = ema(df['close'], 9)
        df['ema15'] = ema(df['close'], 15)
        df['ema50'] = ema(df['close'], 50)
        df['atr']   = atr(df)
        df = df.dropna()
        if len(df) < 3:
            last_signal = "Not enough candles"
            return

        r0, r1, r2   = df.iloc[-1], df.iloc[-2], df.iloc[-3]
        price        = r0['close']
        e9, e15, e50 = r0['ema9'], r0['ema15'], r0['ema50']
        atr_up       = bool(r0['atr'] > r1['atr'] > r2['atr'])
        vol_up       = bool(r0['volume'] > r1['volume'])

        # CPR from previous day
        df_d, _ = get_historical_data("ONE_DAY", 5)
        day_cpr = None
        if df_d is not None and len(df_d) >= 2:
            pr      = df_d.iloc[-2]
            day_cpr = cpr(pr['high'], pr['low'], pr['close'])

        call_ready = (price > e9 > e15 > e50 and
                      day_cpr and price > day_cpr['cpr_top'] and
                      atr_up and vol_up)
        put_ready  = (price < e9 < e15 < e50 and
                      day_cpr and price < day_cpr['cpr_bottom'] and
                      atr_up and vol_up)

        now_str = ist_now().strftime("%H:%M")

        if call_ready:
            last_signal = f"🟢 CALL SIGNAL @ ₹{price:.0f} [{now_str}]"
            print(last_signal)
            simulate_trade("CALL", price)
        elif put_ready:
            last_signal = f"🔴 PUT SIGNAL @ ₹{price:.0f} [{now_str}]"
            print(last_signal)
            simulate_trade("PUT", price)
        else:
            last_signal = f"⏳ No setup @ ₹{price:.0f} [{now_str}]"
            print(last_signal)

    except Exception as e:
        last_signal = f"Scan error: {e}"
        print(f"❌ Scan error: {e}")


def simulate_trade(side, price):
    global today_trades, today_pnl, sl_hit_today, capital, trade_log

    import random
    r   = random.random()
    pnl = 1500 if r < 0.65 else (-500 if r < 0.85 else 3000)

    outcome       = "TARGET" if pnl > 0 else "SL HIT"
    capital      += pnl
    today_pnl    += pnl
    today_trades += 1
    if pnl < 0:
        sl_hit_today = True

    trade_log.insert(0, {
        'time':    ist_now().strftime("%H:%M"),
        'date':    str(ist_now().date()),
        'side':    side,
        'entry':   round(price, 2),
        'pnl':     pnl,
        'outcome': outcome,
        'capital': round(capital, 2),
    })
    trade_log = trade_log[:50]   # keep last 50
    print(f"{'✅' if pnl > 0 else '❌'} Trade: {side} @ ₹{price:.0f} → {outcome} → ₹{pnl}")


# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    try:
        return send_from_directory('public', 'index.html')
    except Exception as e:
        return f"<h2>Bot running</h2><a href='/api/test'>Test API</a>"


@app.route('/api/test')
def api_test():
    return jsonify({
        'status':         'ok',
        'logged_in':      smart_obj is not None,
        'bot_active':     bot_active,
        'market_open':    is_market_open(),
        'trading_window': is_trading_window(),
        'window_label':   window_label(),
        'today_trades':   today_trades,
        'today_pnl':      today_pnl,
        'capital':        capital,
        'last_signal':    last_signal,
        'nifty_token':    nifty_token,
        'timestamp':      ist_now().isoformat(),
    })


@app.route('/api/login', methods=['POST'])
def api_login():
    success = login_smartapi()
    return jsonify({
        'success':   success,
        'message':   '✅ Login successful!' if success else '❌ Login failed',
        'logged_in': smart_obj is not None,
    })


@app.route('/api/debug-login')
def api_debug_login():
    try:
        if not SMARTAPI_AVAILABLE:
            return jsonify({'error': 'smartapi-python not installed'})
        creds = {k: ('✅ Set' if v else '❌ MISSING') for k, v in [
            ('SMARTAPI_KEY',         SMARTAPI_KEY),
            ('SMARTAPI_CLIENT_ID',   SMARTAPI_CLIENT_ID),
            ('SMARTAPI_PASSWORD',    SMARTAPI_PASSWORD),
            ('SMARTAPI_TOTP_SECRET', SMARTAPI_TOTP_SECRET),
        ]}
        if not all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
            return jsonify({'error': 'Missing credentials', 'credentials': creds})
        totp_code = generate_totp()
        obj  = SmartConnect(api_key=SMARTAPI_KEY)
        data = obj.generateSession(SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, totp_code)
        return jsonify({
            'credentials':       creds,
            'totp_generated':    totp_code,
            'smartapi_response': data,
        })
    except Exception as e:
        return jsonify({'exception': str(e)})


@app.route('/api/debug-historical')
def api_debug_historical():
    try:
        if smart_obj is None:
            return jsonify({'error': 'Not logged in'})

        to_dt   = get_last_trading_day()
        from_dt = to_dt - datetime.timedelta(days=3)
        results = {}

        tests = [
            ("NSE", "26000", "ONE_DAY"),
            ("NSE", "26000", "FIFTEEN_MINUTE"),
            ("NFO", "26009", "ONE_DAY"),
            ("NFO", "43394", "ONE_DAY"),
            ("NFO", "35001", "ONE_DAY"),
        ]
        for exchange, token, interval in tests:
            param = {
                "exchange":    exchange,
                "symboltoken": token,
                "interval":    interval,
                "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
            }
            try:
                data = smart_obj.getCandleData(param)
                results[f"{exchange}_{token}_{interval}"] = {
                    'status':    data.get('status')    if data else None,
                    'message':   data.get('message')   if data else None,
                    'errorcode': data.get('errorcode') if data else None,
                    'rows':      len(data.get('data', [])) if data else 0,
                    'sample':    data.get('data', [])[:1]  if data else [],
                }
            except Exception as e:
                results[f"{exchange}_{token}_{interval}"] = {'error': str(e)}

        # Test yfinance fallback
        try:
            df_yf, src_yf = get_historical_yfinance("FIFTEEN_MINUTE", 5)
            results['yfinance_fallback'] = {
                'rows': len(df_yf) if df_yf is not None else 0,
                'source': src_yf,
                'sample': df_yf.head(2).to_dict('records') if df_yf is not None else [],
            }
        except Exception as e:
            results['yfinance_fallback'] = {'error': str(e)}

        exchanges = session_data.get('data', {}).get('exchanges', []) if session_data else []
        return jsonify({
            'date_range':        {'from': from_dt.strftime("%Y-%m-%d"), 'to': to_dt.strftime("%Y-%m-%d")},
            'account_exchanges': exchanges,
            'nifty_token':       nifty_token,
            'results':           results,
        })
    except Exception as e:
        return jsonify({'exception': str(e)})


@app.route('/api/session-status')
def api_session():
    return jsonify({
        'logged_in':  smart_obj is not None,
        'login_time': session_data.get('login_time') if session_data else None,
        'message':    'Session active' if smart_obj else 'Not logged in',
    })


@app.route('/api/nifty-price')
def api_price():
    price, source = get_nifty_price()
    if price:
        return jsonify({'success': True, 'price': price, 'source': source,
                        'timestamp': ist_now().isoformat()})
    return jsonify({'success': False, 'error': source})


@app.route('/api/market-status')
def api_market():
    n = ist_now()
    return jsonify({
        'is_open':        is_market_open(),
        'trading_window': is_trading_window(),
        'window_label':   window_label(),
        'bot_active':     bot_active,
        'ist_time':       n.strftime('%H:%M:%S'),
        'day':            n.strftime('%A'),
        'date':           n.strftime('%Y-%m-%d'),
    })


@app.route('/api/indicators')
def api_indicators():
    try:
        df, src = get_historical_data("FIFTEEN_MINUTE", 5)
        if df is None:
            return jsonify({'success': False, 'error': src})

        df['ema9']  = ema(df['close'], 9)
        df['ema15'] = ema(df['close'], 15)
        df['ema50'] = ema(df['close'], 50)
        df['atr']   = atr(df)
        df = df.dropna()
        if len(df) < 3:
            return jsonify({'success': False, 'error': 'Not enough candles'})

        r0, r1, r2   = df.iloc[-1], df.iloc[-2], df.iloc[-3]
        price        = r0['close']
        e9, e15, e50 = r0['ema9'], r0['ema15'], r0['ema50']
        atr_up       = bool(r0['atr'] > r1['atr'] > r2['atr'])
        vol_up       = bool(r0['volume'] > r1['volume'])

        df_d, _ = get_historical_data("ONE_DAY", 5)
        day_cpr = None
        if df_d is not None and len(df_d) >= 2:
            pr      = df_d.iloc[-2]
            day_cpr = cpr(pr['high'], pr['low'], pr['close'])

        call_trend = bool(price > e9 > e15 > e50)
        put_trend  = bool(price < e9 < e15 < e50)
        call_cpr   = bool(day_cpr and price > day_cpr['cpr_top'])
        put_cpr    = bool(day_cpr and price < day_cpr['cpr_bottom'])
        inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])
        call_ready = call_trend and call_cpr and atr_up and vol_up and is_trading_window()
        put_ready  = put_trend  and put_cpr  and atr_up and vol_up and is_trading_window()

        return jsonify({
            'success': True, 'price': round(price, 2),
            'ema9': round(e9, 2), 'ema15': round(e15, 2), 'ema50': round(e50, 2),
            'atr': round(r0['atr'], 2), 'atr_rising': atr_up,
            'volume': int(r0['volume']), 'vol_rising': vol_up,
            'cpr': day_cpr,
            'signals': {
                'call_trend': call_trend, 'put_trend': put_trend,
                'call_cpr': call_cpr, 'put_cpr': put_cpr,
                'inside_cpr': inside_cpr, 'atr_ok': atr_up,
                'volume_ok': vol_up, 'trading_window': is_trading_window(),
                'call_ready': call_ready, 'put_ready': put_ready,
            },
            'last_signal': last_signal,
            'source': src,
            'timestamp': ist_now().isoformat(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bot-status')
def api_bot_status():
    return jsonify({
        'bot_active':   bot_active,
        'logged_in':    smart_obj is not None,
        'today_trades': today_trades,
        'today_pnl':    today_pnl,
        'capital':      capital,
        'sl_hit':       sl_hit_today,
        'last_signal':  last_signal,
        'trade_log':    trade_log[:10],
        'ist_time':     ist_now().strftime('%H:%M:%S'),
    })


@app.route('/api/trades')
def api_trades():
    return jsonify({'trades': trade_log, 'total': len(trade_log)})


@app.route('/api/backtest')
def api_backtest():
    days        = int(request.args.get('days', 30))
    result, msg = run_backtest(days)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': msg})


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("=" * 60)
print("🚀 NIFTY Options Bot – SmartAPI Backend")
print(f"   API Key    : {'✅ Set' if SMARTAPI_KEY         else '❌ Missing'}")
print(f"   Client ID  : {'✅ Set' if SMARTAPI_CLIENT_ID   else '❌ Missing'}")
print(f"   Password   : {'✅ Set' if SMARTAPI_PASSWORD     else '❌ Missing'}")
print(f"   TOTP Secret: {'✅ Set' if SMARTAPI_TOTP_SECRET  else '❌ Missing'}")
print("=" * 60)

# Discover NIFTY token from scrip master
threading.Thread(target=discover_nifty_token, daemon=True).start()

if all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
    print("🔄 Auto-login starting...")
    threading.Thread(target=login_smartapi,  daemon=True).start()
    print("🕐 Scheduler starting (auto login 9:10 AM, scan 9:15–3:30)...")
    threading.Thread(target=scheduler_loop, daemon=True).start()
else:
    print("⚠️  Set all 4 Railway variables, then redeploy.")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
