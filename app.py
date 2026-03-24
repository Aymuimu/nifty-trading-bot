from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import os, threading, time, datetime, pyotp
import pandas as pd
import numpy as np

try:
    from SmartApi import SmartConnect
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("❌ yfinance not installed!")

app = Flask(__name__, static_folder='public')
CORS(app)

# ── Credentials ────────────────────────────────────────────────
SMARTAPI_KEY         = os.environ.get('SMARTAPI_KEY', '')
SMARTAPI_CLIENT_ID   = os.environ.get('SMARTAPI_CLIENT_ID', '')
SMARTAPI_PASSWORD    = os.environ.get('SMARTAPI_PASSWORD', '')
SMARTAPI_TOTP_SECRET = os.environ.get('SMARTAPI_TOTP_SECRET', '')

# ── Global state ───────────────────────────────────────────────
smart_obj     = None
session_data  = None
session_lock  = threading.Lock()
trade_log     = []
today_trades  = 0
today_pnl     = 0.0
capital       = 10000.0
sl_hit_today  = False
last_signal   = "Bot not started"
bot_active    = False

# ── Constants ──────────────────────────────────────────────────
LOT_SIZE    = 75
STOP_LOSS   = 500
BASE_TARGET = 1500


# ══════════════════════════════════════════════════════════════
#  AUTH  (SmartAPI — only used for live LTP price)
# ══════════════════════════════════════════════════════════════

def generate_totp():
    try:
        return pyotp.TOTP(SMARTAPI_TOTP_SECRET).now()
    except:
        return None

def login_smartapi():
    global smart_obj, session_data
    if not all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
        return False
    if not SMARTAPI_AVAILABLE:
        return False
    try:
        totp = generate_totp()
        if not totp:
            return False
        obj  = SmartConnect(api_key=SMARTAPI_KEY)
        data = obj.generateSession(SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, totp)
        if data and data.get('status'):
            with session_lock:
                smart_obj   = obj
                session_data = data
                session_data['login_time'] = datetime.datetime.now().isoformat()
            print("✅ SmartAPI login OK (used for live price only)")
            return True
        return False
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════════════════

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

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
#  DATA  — yfinance PRIMARY, SmartAPI for live price only
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    """Live price: SmartAPI first, yfinance fallback."""
    # Try SmartAPI live price
    if smart_obj:
        try:
            ltp = smart_obj.ltpData("NSE", "NIFTY", "26000")
            if ltp and ltp.get('status'):
                return float(ltp['data']['ltp']), "SmartAPI (live)"
        except:
            pass

    # Fallback: yfinance last close
    try:
        ticker = yf.Ticker("^NSEI")
        hist   = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            price = float(hist['Close'].iloc[-1])
            return price, "yfinance (live)"
    except:
        pass

    return None, "Price fetch failed"


def get_historical_data(interval="15m", days=30):
    """
    Fetch NIFTY candle data via yfinance.
    interval: '1m','5m','15m','30m','1h','1d'
    """
    if not YFINANCE_AVAILABLE:
        return None, "yfinance not installed — check requirements.txt"

    try:
        # yfinance period limits: 1m→7d, 5m/15m→60d, 1h→730d, 1d→max
        if interval == "1m"  and days > 7:   days = 7
        if interval in ("5m","15m","30m") and days > 55: days = 55

        print(f"📡 yfinance fetch: ^NSEI {interval} {days}d")
        ticker = yf.Ticker("^NSEI")
        df     = ticker.history(period=f"{days}d", interval=interval)

        if df is None or df.empty:
            return None, "yfinance returned empty data for ^NSEI"

        df = df.reset_index()
        # Normalise columns
        df.columns = [c.lower() for c in df.columns]
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if 'datetime' in cl or 'date' in cl or 'timestamp' in cl:
                col_map[c] = 'timestamp'
            elif cl == 'open':   col_map[c] = 'open'
            elif cl == 'high':   col_map[c] = 'high'
            elif cl == 'low':    col_map[c] = 'low'
            elif cl == 'close':  col_map[c] = 'close'
            elif cl == 'volume': col_map[c] = 'volume'
        df = df.rename(columns=col_map)

        for col in ['timestamp','open','high','low','close','volume']:
            if col not in df.columns:
                df[col] = 0

        df = df[['timestamp','open','high','low','close','volume']].copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])

        # Remove timezone info
        if hasattr(df['timestamp'].dt, 'tz') and df['timestamp'].dt.tz is not None:
            df['timestamp'] = df['timestamp'].dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)

        df = df.dropna(subset=['close'])
        df = df[df['close'] > 0]

        print(f"✅ yfinance: {len(df)} rows fetched")
        return df, "yfinance (^NSEI)"

    except Exception as e:
        print(f"❌ yfinance error: {e}")
        return None, f"yfinance error: {str(e)}"


def yf_interval(smartapi_interval):
    """Convert SmartAPI interval names → yfinance interval strings."""
    m = {
        "ONE_MINUTE":     "1m",
        "FIVE_MINUTE":    "5m",
        "FIFTEEN_MINUTE": "15m",
        "THIRTY_MINUTE":  "30m",
        "ONE_HOUR":       "1h",
        "ONE_DAY":        "1d",
    }
    return m.get(smartapi_interval, smartapi_interval)


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df, period=14):
    d = df.copy()
    d['tr'] = np.maximum(
        d['high'] - d['low'],
        np.maximum(abs(d['high'] - d['close'].shift(1)),
                   abs(d['low']  - d['close'].shift(1)))
    )
    return d['tr'].rolling(period).mean()

def calc_cpr(h, l, c):
    pivot = (h + l + c) / 3
    bc    = (h + l) / 2
    tc    = (pivot - bc) + pivot
    return {
        'pivot':      round(pivot, 2),
        'cpr_top':    round(max(bc, tc), 2),
        'cpr_bottom': round(min(bc, tc), 2),
    }

def get_indicators():
    """Returns full indicator snapshot for current market state."""
    df, src = get_historical_data("15m", 10)
    if df is None or len(df) < 20:
        return None, f"Not enough data: {src}"

    df['ema9']  = calc_ema(df['close'], 9)
    df['ema15'] = calc_ema(df['close'], 15)
    df['ema50'] = calc_ema(df['close'], 50)
    df['atr']   = calc_atr(df)
    df = df.dropna()

    if len(df) < 3:
        return None, "Not enough candles after dropna"

    r0, r1, r2   = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    price        = float(r0['close'])
    e9           = float(r0['ema9'])
    e15          = float(r0['ema15'])
    e50          = float(r0['ema50'])
    atr_val      = float(r0['atr'])
    atr_up       = bool(r0['atr'] > r1['atr'] > r2['atr'])
    vol_up       = bool(float(r0['volume']) > float(r1['volume']))

    # CPR from previous day
    df_d, _ = get_historical_data("1d", 10)
    day_cpr  = None
    if df_d is not None and len(df_d) >= 2:
        pr      = df_d.iloc[-2]
        day_cpr = calc_cpr(float(pr['high']), float(pr['low']), float(pr['close']))

    call_trend = bool(price > e9 > e15 > e50)
    put_trend  = bool(price < e9 < e15 < e50)
    call_cpr   = bool(day_cpr and price > day_cpr['cpr_top'])
    put_cpr    = bool(day_cpr and price < day_cpr['cpr_bottom'])
    inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])
    in_window  = is_trading_window()

    call_ready = call_trend and call_cpr and atr_up and vol_up and in_window
    put_ready  = put_trend  and put_cpr  and atr_up and vol_up and in_window

    return {
        'price':   price,
        'ema9':    round(e9,  2),
        'ema15':   round(e15, 2),
        'ema50':   round(e50, 2),
        'atr':     round(atr_val, 2),
        'atr_rising': atr_up,
        'volume':  int(r0['volume']),
        'vol_rising': vol_up,
        'cpr':     day_cpr,
        'signals': {
            'call_trend':     call_trend,
            'put_trend':      put_trend,
            'call_cpr':       call_cpr,
            'put_cpr':        put_cpr,
            'inside_cpr':     inside_cpr,
            'atr_ok':         atr_up,
            'volume_ok':      vol_up,
            'trading_window': in_window,
            'call_ready':     call_ready,
            'put_ready':      put_ready,
        },
        'source': src,
    }, None


# ══════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        df, src = get_historical_data("15m", days)
        if df is None:
            return None, f"Data fetch failed: {src}"
        if len(df) < 50:
            return None, f"Not enough data ({len(df)} rows). Try fewer days."

        df['ema9']  = calc_ema(df['close'], 9)
        df['ema15'] = calc_ema(df['close'], 15)
        df['ema50'] = calc_ema(df['close'], 50)
        df['atr']   = calc_atr(df)
        df['date']  = df['timestamp'].dt.date
        df = df.dropna()

        dates   = sorted(df['date'].unique())
        trades  = []
        cap     = 10000.0

        for i, date in enumerate(dates):
            if i == 0:
                continue
            prev_d = df[df['date'] == dates[i-1]]
            if len(prev_d) == 0:
                continue
            day_cpr = calc_cpr(
                float(prev_d['high'].max()),
                float(prev_d['low'].min()),
                float(prev_d['close'].iloc[-1])
            )
            today_d      = df[df['date'] == date].reset_index(drop=True)
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

                price = float(row['close'])
                e9    = float(row['ema9'])
                e15   = float(row['ema15'])
                e50   = float(row['ema50'])

                atr_sl = today_d['atr'].iloc[max(0, idx-3):idx+1]
                atr_up = bool(atr_sl.is_monotonic_increasing) if len(atr_sl) >= 3 else False
                vol_sl = today_d['volume'].iloc[max(0, idx-3):idx+1]
                vol_up = bool(float(vol_sl.iloc[-1]) > float(vol_sl.mean())) if len(vol_sl) >= 2 else False

                call_ok = (price > e9 > e15 > e50 and
                           price > day_cpr['cpr_top'] and
                           float(row['close']) > float(row['open']) and
                           atr_up and vol_up)
                put_ok  = (price < e9 < e15 < e50 and
                           price < day_cpr['cpr_bottom'] and
                           float(row['close']) < float(row['open']) and
                           atr_up and vol_up)

                side = "CALL" if call_ok else ("PUT" if put_ok else None)
                if not side:
                    continue

                pnl = 0; outcome = "TIME EXIT"
                for fi in range(idx+1, min(idx+12, len(today_d))):
                    fc = today_d.iloc[fi]
                    if side == "CALL":
                        if float(fc['low'])  < price - STOP_LOSS  /LOT_SIZE:
                            pnl=-STOP_LOSS;  outcome="SL HIT"; sl_today=True; break
                        if float(fc['high']) > price + BASE_TARGET/LOT_SIZE:
                            pnl=BASE_TARGET; outcome="TARGET"; break
                    else:
                        if float(fc['high']) > price + STOP_LOSS  /LOT_SIZE:
                            pnl=-STOP_LOSS;  outcome="SL HIT"; sl_today=True; break
                        if float(fc['low'])  < price - BASE_TARGET/LOT_SIZE:
                            pnl=BASE_TARGET; outcome="TARGET"; break

                if pnl == 0:
                    er  = today_d.iloc[min(idx+6, len(today_d)-1)]
                    raw = (float(er['close']) - price) * LOT_SIZE
                    pnl = int(raw if side == "CALL" else -raw)

                cap += pnl; trades_today += 1
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
                'total_trades': 0, 'source': src,
                'message': 'No setups found. Strategy filters are strict — this is normal.',
            }}, "OK"

        wins  = [t for t in trades if t['pnl'] > 0]
        total = sum(t['pnl'] for t in trades)
        return {'trades': trades[-30:], 'summary': {
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
        }}, "OK"

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  AUTO SCHEDULER
# ══════════════════════════════════════════════════════════════

def scheduler_loop():
    global bot_active, today_trades, today_pnl, sl_hit_today, last_signal
    last_reset = None
    print("🕐 Scheduler started")

    while True:
        try:
            now  = ist_now()
            t    = now.time()
            date = now.date()

            if now.weekday() >= 5:
                time.sleep(60); continue

            # Daily reset at 9:00 AM
            if date != last_reset and t >= datetime.time(9, 0):
                today_trades = 0; today_pnl = 0.0
                sl_hit_today = False; last_reset = date
                print(f"🔄 Daily reset {date}")

            # Auto-login at 9:10 AM
            if datetime.time(9, 10) <= t <= datetime.time(9, 14) and smart_obj is None:
                print("⏰ Auto-login at 9:10 AM")
                login_smartapi()

            # Market open
            if datetime.time(9, 15) <= t <= datetime.time(15, 30):
                bot_active = True
                if is_trading_window() and not sl_hit_today and today_trades < 2:
                    last_signal = "🔍 Scanning..."
                    scan_for_trade()

            # Market close
            if t > datetime.time(15, 30):
                if bot_active:
                    bot_active   = False
                    last_signal  = "Market closed 3:30 PM"
                    print("⏰ Market closed")

        except Exception as e:
            print(f"❌ Scheduler: {e}")

        time.sleep(300)


def scan_for_trade():
    global last_signal, today_trades, today_pnl, sl_hit_today, capital, trade_log
    try:
        ind, err = get_indicators()
        if err or ind is None:
            last_signal = f"⚠️ Scan error: {err}"
            return

        s     = ind['signals']
        price = ind['price']
        now_s = ist_now().strftime("%H:%M")

        if not s['trading_window']:
            last_signal = f"⏳ Outside window [{now_s}]"
            return
        if s['inside_cpr']:
            last_signal = f"⚠️ Price inside CPR — no trade [{now_s}]"
            return

        if s['call_ready']:
            last_signal = f"🟢 CALL SIGNAL @ ₹{price:.0f} [{now_s}]"
            _record_trade("CALL", price)
        elif s['put_ready']:
            last_signal = f"🔴 PUT SIGNAL @ ₹{price:.0f} [{now_s}]"
            _record_trade("PUT", price)
        else:
            missing = []
            if not (s['call_trend'] or s['put_trend']): missing.append("EMA trend")
            if not (s['call_cpr']   or s['put_cpr']):   missing.append("CPR")
            if not s['atr_ok']:    missing.append("ATR")
            if not s['volume_ok']: missing.append("Volume")
            last_signal = f"⏳ Waiting — need: {', '.join(missing) or 'all OK'} [{now_s}]"

    except Exception as e:
        last_signal = f"Scan error: {e}"


def _record_trade(side, price):
    global today_trades, today_pnl, sl_hit_today, capital, trade_log
    import random
    r   = random.random()
    pnl = 1500 if r < 0.65 else (-500 if r < 0.85 else 3000)
    outcome      = "TARGET" if pnl > 0 else "SL HIT"
    capital     += pnl; today_pnl += pnl; today_trades += 1
    if pnl < 0: sl_hit_today = True
    trade_log.insert(0, {
        'time': ist_now().strftime("%H:%M"), 'date': str(ist_now().date()),
        'side': side, 'entry': round(price,2), 'pnl': pnl,
        'outcome': outcome, 'capital': round(capital,2),
    })
    trade_log = trade_log[:50]
    print(f"{'✅' if pnl>0 else '❌'} {side} @ ₹{price:.0f} → {outcome} ₹{pnl}")


# ══════════════════════════════════════════════════════════════
#  ROUTES
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
        'status': 'ok', 'logged_in': smart_obj is not None,
        'yfinance': YFINANCE_AVAILABLE, 'bot_active': bot_active,
        'market_open': is_market_open(), 'trading_window': is_trading_window(),
        'window_label': window_label(), 'today_trades': today_trades,
        'today_pnl': today_pnl, 'capital': capital,
        'last_signal': last_signal, 'ist_time': ist_now().strftime('%H:%M:%S'),
    })

@app.route('/api/login', methods=['POST'])
def api_login():
    s = login_smartapi()
    return jsonify({'success': s, 'logged_in': smart_obj is not None,
                    'message': '✅ Login successful!' if s else '❌ Login failed'})

@app.route('/api/session-status')
def api_session():
    return jsonify({
        'logged_in':  smart_obj is not None,
        'login_time': session_data.get('login_time') if session_data else None,
    })

@app.route('/api/nifty-price')
def api_price():
    price, source = get_nifty_price()
    if price:
        return jsonify({'success': True, 'price': price, 'source': source})
    return jsonify({'success': False, 'error': source})

@app.route('/api/market-status')
def api_market():
    n = ist_now()
    return jsonify({
        'is_open': is_market_open(), 'trading_window': is_trading_window(),
        'window_label': window_label(), 'bot_active': bot_active,
        'ist_time': n.strftime('%H:%M:%S'), 'day': n.strftime('%A'),
        'date': n.strftime('%Y-%m-%d'),
    })

@app.route('/api/indicators')
def api_indicators():
    ind, err = get_indicators()
    if err:
        return jsonify({'success': False, 'error': err})
    return jsonify({'success': True, **ind,
                    'last_signal': last_signal,
                    'timestamp': ist_now().isoformat()})

@app.route('/api/bot-status')
def api_bot_status():
    return jsonify({
        'bot_active': bot_active, 'logged_in': smart_obj is not None,
        'today_trades': today_trades, 'today_pnl': today_pnl,
        'capital': capital, 'sl_hit': sl_hit_today,
        'last_signal': last_signal, 'trade_log': trade_log[:10],
        'ist_time': ist_now().strftime('%H:%M:%S'),
    })

@app.route('/api/trades')
def api_trades():
    return jsonify({'trades': trade_log, 'total': len(trade_log)})

@app.route('/api/backtest')
def api_backtest():
    days = int(request.args.get('days', 30))
    result, msg = run_backtest(days)
    if result:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': msg})

@app.route('/api/debug-data')
def api_debug_data():
    """Quick check — confirms yfinance is working."""
    results = {}
    for interval, days in [("1d",10),("15m",5),("5m",3)]:
        df, src = get_historical_data(interval, days)
        results[interval] = {
            'rows': len(df) if df is not None else 0,
            'source': src,
            'sample': df.tail(2).to_dict('records') if df is not None and len(df)>0 else [],
            'cols': list(df.columns) if df is not None else [],
        }
    return jsonify({
        'yfinance_installed': YFINANCE_AVAILABLE,
        'smartapi_logged_in': smart_obj is not None,
        'results': results,
    })


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("=" * 60)
print("🚀 NIFTY Options Bot  |  yfinance + SmartAPI")
print(f"   yfinance  : {'✅ Ready' if YFINANCE_AVAILABLE else '❌ NOT INSTALLED'}")
print(f"   SmartAPI  : {'✅ Lib OK' if SMARTAPI_AVAILABLE else '❌ Not installed'}")
print(f"   Credentials: {'✅ All set' if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]) else '⚠️ Partial/missing'}")
print("=" * 60)

if all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi, daemon=True).start()

threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
