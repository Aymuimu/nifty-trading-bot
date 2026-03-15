from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os
import threading
import time
import datetime
import pyotp
import pandas as pd
import numpy as np

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

# ─── Global session ────────────────────────────────────────────
smart_obj    = None
session_data = None
session_lock = threading.Lock()

# ─── Strategy constants ────────────────────────────────────────
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN  = "26000"
EXCHANGE     = "NSE"
LOT_SIZE     = 75
STOP_LOSS    = 500
BASE_TARGET  = 1500
EXT_TARGET   = 3000


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
        print("⚠️ SmartAPI credentials not fully configured")
        return False
    if not SMARTAPI_AVAILABLE:
        print("⚠️ SmartAPI library not installed")
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
        print(f"❌ SmartAPI login failed: {data}")
        return False
    except Exception as e:
        print(f"❌ SmartAPI login error: {e}")
        return False


def auto_refresh_session():
    while True:
        time.sleep(6 * 3600)
        print("🔄 Auto-refreshing SmartAPI session...")
        login_smartapi()


# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    global smart_obj
    try:
        if smart_obj is None:
            return None, "Not logged in"
        ltp = smart_obj.ltpData(EXCHANGE, NIFTY_SYMBOL, NIFTY_TOKEN)
        if ltp and ltp.get('status'):
            return float(ltp['data']['ltp']), "SmartAPI (live)"
        return None, "LTP fetch failed"
    except Exception as e:
        return None, str(e)


def get_historical_data(interval="FIFTEEN_MINUTE", days=30):
    global smart_obj
    try:
        if smart_obj is None:
            return None, "Not logged in"
        to_dt   = datetime.datetime.now()
        from_dt = to_dt - datetime.timedelta(days=days)
        param   = {
            "exchange":    "NSE",
            "symboltoken": "26000",
            "interval":    interval,
            "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        print(f"📡 Historical request: {param}")
        data = smart_obj.getCandleData(param)
        print(f"📡 Historical response: {data}")
        if data and data.get('status') and data.get('data'):
            df = pd.DataFrame(
                data['data'],
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df, "SmartAPI"
        error_msg  = data.get('message',   'Unknown error') if data else 'No response'
        error_code = data.get('errorcode', '')              if data else ''
        return None, f"SmartAPI error {error_code}: {error_msg}"
    except Exception as e:
        return None, f"Exception: {str(e)}"


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def atr(df, period=14):
    d = df.copy()
    d['tr'] = np.maximum(
        d['high'] - d['low'],
        np.maximum(
            abs(d['high'] - d['close'].shift(1)),
            abs(d['low']  - d['close'].shift(1))
        )
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
#  TIME HELPERS
# ══════════════════════════════════════════════════════════════

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def is_market_open():
    n = ist_now()
    if n.weekday() >= 5:
        return False
    t = n.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 30)


def is_trading_window():
    t = ist_now().time()
    morning   = datetime.time(10,  0) <= t <= datetime.time(11, 15)
    afternoon = datetime.time(13, 45) <= t <= datetime.time(14, 45)
    return morning or afternoon


def window_label():
    t = ist_now().time()
    if datetime.time(10, 0) <= t <= datetime.time(11, 15):
        return "Morning Window (10:00-11:15)"
    if datetime.time(13, 45) <= t <= datetime.time(14, 45):
        return "Afternoon Window (1:45-2:45)"
    return "Outside trading windows"


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
        capital = 10000.0

        for i, date in enumerate(dates):
            if i == 0:
                continue
            prev_d = df15[df15['date'] == dates[i - 1]]
            if len(prev_d) == 0:
                continue
            day_cpr = cpr(prev_d['high'].max(), prev_d['low'].min(), prev_d['close'].iloc[-1])

            today_d      = df15[df15['date'] == date].reset_index(drop=True)
            trades_today = 0
            sl_today     = False

            for idx in range(3, len(today_d)):
                if trades_today >= 2 or sl_today:
                    break

                row  = today_d.iloc[idx]
                t    = row['timestamp'].time()
                morn = datetime.time(10, 0) <= t <= datetime.time(11, 15)
                aft  = datetime.time(13, 45) <= t <= datetime.time(14, 45)
                if not (morn or aft):
                    continue

                price   = row['close']
                e9      = row['ema9']
                e15     = row['ema15']
                e50     = row['ema50']
                atr_now = row['atr']

                atr_slice = today_d['atr'].iloc[max(0, idx - 3):idx + 1]
                atr_up    = bool(atr_slice.is_monotonic_increasing) if len(atr_slice) >= 3 else False

                vol_slice = today_d['volume'].iloc[max(0, idx - 3):idx + 1]
                vol_up    = bool(vol_slice.iloc[-1] > vol_slice.mean()) if len(vol_slice) >= 2 else False

                call_ok = (
                    price > e9 > e15 > e50
                    and price > day_cpr['cpr_top']
                    and row['close'] > row['open']
                    and atr_up and vol_up
                )
                put_ok = (
                    price < e9 < e15 < e50
                    and price < day_cpr['cpr_bottom']
                    and row['close'] < row['open']
                    and atr_up and vol_up
                )

                side = "CALL" if call_ok else ("PUT" if put_ok else None)
                if side is None:
                    continue

                pnl     = 0
                outcome = "TIME EXIT"

                for fi in range(idx + 1, min(idx + 12, len(today_d))):
                    fc = today_d.iloc[fi]
                    if side == "CALL":
                        if fc['low'] < price - STOP_LOSS / LOT_SIZE:
                            pnl = -STOP_LOSS; outcome = "SL HIT"; sl_today = True; break
                        if fc['high'] > price + BASE_TARGET / LOT_SIZE:
                            pnl = BASE_TARGET; outcome = "TARGET"; break
                    else:
                        if fc['high'] > price + STOP_LOSS / LOT_SIZE:
                            pnl = -STOP_LOSS; outcome = "SL HIT"; sl_today = True; break
                        if fc['low'] < price - BASE_TARGET / LOT_SIZE:
                            pnl = BASE_TARGET; outcome = "TARGET"; break

                if pnl == 0:
                    exit_row = today_d.iloc[min(idx + 6, len(today_d) - 1)]
                    raw_pnl  = (exit_row['close'] - price) * LOT_SIZE
                    pnl      = int(raw_pnl if side == "CALL" else -raw_pnl)

                capital      += pnl
                trades_today += 1
                trades.append({
                    'date':       str(date),
                    'time':       str(t)[:5],
                    'side':       side,
                    'entry':      round(price, 2),
                    'pnl':        pnl,
                    'outcome':    outcome,
                    'capital':    round(capital, 2),
                    'cpr_top':    day_cpr['cpr_top'],
                    'cpr_bottom': day_cpr['cpr_bottom'],
                    'ema9':       round(e9, 2),
                    'ema50':      round(e50, 2),
                })

        if not trades:
            return {'trades': [], 'summary': {'total_trades': 0, 'message': 'No setups found'}}, "OK"

        wins  = [t for t in trades if t['pnl'] > 0]
        total = sum(t['pnl'] for t in trades)

        summary = {
            'total_trades':    len(trades),
            'wins':            len(wins),
            'losses':          len(trades) - len(wins),
            'win_rate':        round(len(wins) / len(trades) * 100, 1),
            'total_pnl':       round(total, 2),
            'initial_capital': 10000,
            'final_capital':   round(capital, 2),
            'roi':             round((capital - 10000) / 10000 * 100, 1),
            'max_loss':        min(t['pnl'] for t in trades),
            'max_gain':        max(t['pnl'] for t in trades),
            'avg_pnl':         round(total / len(trades), 2),
            'source':          src,
        }
        return {'trades': trades[-30:], 'summary': summary}, "OK"

    except Exception as e:
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    try:
        return send_from_directory('public', 'index.html')
    except Exception as e:
        return f"<h2>Bot is running</h2><p>UI error: {e}</p><a href='/api/test'>Test API</a>"


@app.route('/api/test')
def api_test():
    return jsonify({
        'status':         'ok',
        'message':        'NIFTY Options Bot - SmartAPI',
        'configured':     all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]),
        'logged_in':      smart_obj is not None,
        'market_open':    is_market_open(),
        'trading_window': is_trading_window(),
        'window_label':   window_label(),
        'timestamp':      datetime.datetime.utcnow().isoformat(),
    })


@app.route('/api/login', methods=['POST'])
def api_login():
    success = login_smartapi()
    return jsonify({
        'success':   success,
        'message':   '✅ Login successful!' if success else '❌ Login failed – check credentials',
        'logged_in': smart_obj is not None,
    })


@app.route('/api/debug-login')
def api_debug_login():
    try:
        if not SMARTAPI_AVAILABLE:
            return jsonify({'error': 'smartapi-python not installed'})

        creds = {
            'SMARTAPI_KEY':         '✅ Set' if SMARTAPI_KEY         else '❌ MISSING',
            'SMARTAPI_CLIENT_ID':   '✅ Set' if SMARTAPI_CLIENT_ID   else '❌ MISSING',
            'SMARTAPI_PASSWORD':    '✅ Set' if SMARTAPI_PASSWORD     else '❌ MISSING',
            'SMARTAPI_TOTP_SECRET': '✅ Set' if SMARTAPI_TOTP_SECRET  else '❌ MISSING',
        }

        if not all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
            return jsonify({'error': 'Missing credentials', 'credentials': creds})

        totp_code = generate_totp()
        obj       = SmartConnect(api_key=SMARTAPI_KEY)
        data      = obj.generateSession(SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, totp_code)

        return jsonify({
            'credentials':        creds,
            'totp_generated':     totp_code,
            'api_key_length':     len(SMARTAPI_KEY),
            'password_length':    len(SMARTAPI_PASSWORD),
            'totp_secret_length': len(SMARTAPI_TOTP_SECRET),
            'smartapi_response':  data,
        })
    except Exception as e:
        return jsonify({'exception': str(e), 'type': type(e).__name__})


@app.route('/api/debug-historical')
def api_debug_historical():
    try:
        if smart_obj is None:
            return jsonify({'error': 'Not logged in – click Login first'})
        to_dt   = datetime.datetime.now()
        from_dt = to_dt - datetime.timedelta(days=5)
        param   = {
            "exchange":    "NSE",
            "symboltoken": "26000",
            "interval":    "FIFTEEN_MINUTE",
            "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        data = smart_obj.getCandleData(param)
        rows = len(data.get('data', [])) if data and data.get('data') else 0
        return jsonify({
            'logged_in':  True,
            'params':     param,
            'status':     data.get('status')    if data else None,
            'message':    data.get('message')   if data else None,
            'errorcode':  data.get('errorcode') if data else None,
            'rows_returned': rows,
            'sample':     data.get('data', [])[:3] if data else None,
        })
    except Exception as e:
        return jsonify({'exception': str(e), 'type': type(e).__name__})


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
                        'timestamp': datetime.datetime.utcnow().isoformat()})
    return jsonify({'success': False, 'error': source})


@app.route('/api/market-status')
def api_market():
    n = ist_now()
    return jsonify({
        'is_open':        is_market_open(),
        'trading_window': is_trading_window(),
        'window_label':   window_label(),
        'ist_time':       n.strftime('%H:%M:%S'),
        'day':            n.strftime('%A'),
        'date':           n.strftime('%Y-%m-%d'),
    })


@app.route('/api/cpr')
def api_cpr():
    try:
        df, src = get_historical_data("ONE_DAY", 5)
        if df is None or len(df) < 2:
            return jsonify({'success': False, 'error': src or 'Not enough data'})
        pd_row = df.iloc[-2]
        c      = cpr(pd_row['high'], pd_row['low'], pd_row['close'])
        return jsonify({'success': True, **c, 'source': src,
                        'prev_high':  round(pd_row['high'],  2),
                        'prev_low':   round(pd_row['low'],   2),
                        'prev_close': round(pd_row['close'], 2)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


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
            return jsonify({'success': False, 'error': 'Not enough data for indicators'})

        r0, r1, r2   = df.iloc[-1], df.iloc[-2], df.iloc[-3]
        price        = r0['close']
        e9, e15, e50 = r0['ema9'], r0['ema15'], r0['ema50']
        atr_up       = bool(r0['atr'] > r1['atr'] > r2['atr'])
        vol_up       = bool(r0['volume'] > r1['volume'])

        df_d, _ = get_historical_data("ONE_DAY", 5)
        day_cpr = None
        if df_d is not None and len(df_d) >= 2:
            pd_row  = df_d.iloc[-2]
            day_cpr = cpr(pd_row['high'], pd_row['low'], pd_row['close'])

        call_trend = bool(price > e9 > e15 > e50)
        put_trend  = bool(price < e9 < e15 < e50)
        call_cpr   = bool(day_cpr and price > day_cpr['cpr_top'])
        put_cpr    = bool(day_cpr and price < day_cpr['cpr_bottom'])
        inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])

        call_ready = call_trend and call_cpr and atr_up and vol_up and is_trading_window()
        put_ready  = put_trend  and put_cpr  and atr_up and vol_up and is_trading_window()

        return jsonify({
            'success':    True,
            'price':      round(price, 2),
            'ema9':       round(e9,    2),
            'ema15':      round(e15,   2),
            'ema50':      round(e50,   2),
            'atr':        round(r0['atr'], 2),
            'atr_rising': atr_up,
            'volume':     int(r0['volume']),
            'vol_rising': vol_up,
            'cpr':        day_cpr,
            'signals': {
                'call_trend':     call_trend,
                'put_trend':      put_trend,
                'call_cpr':       call_cpr,
                'put_cpr':        put_cpr,
                'inside_cpr':     inside_cpr,
                'atr_ok':         atr_up,
                'volume_ok':      vol_up,
                'trading_window': is_trading_window(),
                'call_ready':     call_ready,
                'put_ready':      put_ready,
            },
            'source':    src,
            'timestamp': datetime.datetime.utcnow().isoformat(),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


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

if all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
    print("🔄 Auto-login starting...")
    threading.Thread(target=login_smartapi,       daemon=True).start()
    threading.Thread(target=auto_refresh_session, daemon=True).start()
else:
    print("⚠️  Set all 4 Railway variables, then redeploy.")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
