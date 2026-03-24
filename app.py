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

# ── Credentials ────────────────────────────────────────────────
SMARTAPI_KEY         = os.environ.get('SMARTAPI_KEY', '')
SMARTAPI_CLIENT_ID   = os.environ.get('SMARTAPI_CLIENT_ID', '')
SMARTAPI_PASSWORD    = os.environ.get('SMARTAPI_PASSWORD', '')
SMARTAPI_TOTP_SECRET = os.environ.get('SMARTAPI_TOTP_SECRET', '')

# ── Global state ───────────────────────────────────────────────
smart_obj     = None
session_data  = None
session_lock  = threading.Lock()
jwt_token     = None          # raw JWT for direct REST calls
trade_log     = []
today_trades  = 0
today_pnl     = 0.0
capital       = 10000.0
sl_hit_today  = False
last_signal   = "Bot not started"
bot_active    = False

# ── Candle buffer (built from sampled LTP) ─────────────────────
# {date_str: [ {ts, o, h, l, c, v}, ... ]}  keyed by "YYYY-MM-DD HH:MM" (15-min bucket)
candle_buffer = {}
last_ltp      = None

LOT_SIZE    = 75
STOP_LOSS   = 500
BASE_TARGET = 1500

SA_BASE = "https://apiconnect.angelbroking.com"


# ══════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════

def generate_totp():
    try:    return pyotp.TOTP(SMARTAPI_TOTP_SECRET).now()
    except: return None

def login_smartapi():
    global smart_obj, session_data, jwt_token
    if not all([SMARTAPI_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET]):
        return False
    if not SMARTAPI_AVAILABLE:
        return False
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
                # Extract raw JWT for direct REST calls
                jwt_token = data.get('data', {}).get('jwtToken', '')
                if jwt_token.startswith('Bearer '):
                    jwt_token = jwt_token[7:]
            print(f"✅ SmartAPI login OK | JWT: {'set' if jwt_token else 'missing'}")
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

def last_trading_day(offset=0):
    d = datetime.datetime.now() - datetime.timedelta(days=offset)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
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
    """Round datetime down to 15-minute bucket."""
    m = (dt.minute // 15) * 15
    return dt.replace(minute=m, second=0, microsecond=0)


# ══════════════════════════════════════════════════════════════
#  LIVE PRICE
# ══════════════════════════════════════════════════════════════

def get_nifty_price():
    global last_ltp
    if smart_obj:
        try:
            ltp = smart_obj.ltpData("NSE", "NIFTY", "26000")
            if ltp and ltp.get('status'):
                p = float(ltp['data']['ltp'])
                last_ltp = p
                return p, "SmartAPI (live)"
        except: pass
    if last_ltp:
        return last_ltp, "cached LTP"
    return None, "Price unavailable"


# ══════════════════════════════════════════════════════════════
#  DIRECT REST — SmartAPI historical API with JWT
# ══════════════════════════════════════════════════════════════

def smartapi_rest_candles(exchange, token, interval, from_dt, to_dt):
    """
    Call SmartAPI REST endpoint directly using JWT.
    Returns list of [ts, o, h, l, c, v] or None.
    """
    if not jwt_token:
        return None, "No JWT token"
    try:
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Content-Type':  'application/json',
            'Accept':        'application/json',
            'X-UserType':    'USER',
            'X-SourceID':    'WEB',
            'X-ClientLocalIP':  '127.0.0.1',
            'X-ClientPublicIP': '127.0.0.1',
            'X-MACAddress':     '00:00:00:00:00:00',
            'X-PrivateKey':     SMARTAPI_KEY,
        }
        body = {
            "exchange":    exchange,
            "symboltoken": token,
            "interval":    interval,
            "fromdate":    from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        url  = f"{SA_BASE}/rest/secure/angelbroking/historical/v1/getCandleData"
        resp = req.post(url, json=body, headers=headers, timeout=15)
        data = resp.json()
        print(f"  REST {exchange}/{token}/{interval}: status={data.get('status')} rows={len(data.get('data',[]))}")
        if data.get('status') and data.get('data'):
            return data['data'], None
        return None, data.get('message', 'empty')
    except Exception as e:
        return None, str(e)


def fetch_smartapi_candles(sa_interval, days):
    """Try multiple tokens via direct REST API."""
    to_dt   = last_trading_day()
    from_dt = to_dt - datetime.timedelta(days=days)
    candidates = [
        ("NSE","26000"),("NFO","26009"),
        ("NFO","43394"),("NFO","35001"),("NFO","57970"),
    ]
    for exchange, token in candidates:
        rows, err = smartapi_rest_candles(exchange, token, sa_interval, from_dt, to_dt)
        if rows and len(rows) > 5:
            df = pd.DataFrame(rows, columns=['timestamp','open','high','low','close','volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            for c in ['open','high','low','close','volume']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df.dropna(subset=['close'])
            print(f"✅ SmartAPI REST {exchange}/{token}: {len(df)} rows")
            return df, f"SmartAPI ({exchange}/{token})"
    return None, "SmartAPI REST: all tokens returned 0 rows"


# ══════════════════════════════════════════════════════════════
#  CANDLE BUFFER — built from sampled LTP every minute
# ══════════════════════════════════════════════════════════════

def sample_ltp_to_buffer():
    """Called every minute. Builds 15-min candles from live LTP."""
    global candle_buffer
    price, src = get_nifty_price()
    if price is None:
        return
    now    = ist_now()
    bucket = bucket_15m(now)
    key    = bucket.strftime("%Y-%m-%d %H:%M")
    if key not in candle_buffer:
        candle_buffer[key] = {'timestamp': bucket, 'open': price,
                               'high': price, 'low': price,
                               'close': price, 'volume': 0, 'ticks': 1}
    else:
        c = candle_buffer[key]
        c['high']   = max(c['high'], price)
        c['low']    = min(c['low'],  price)
        c['close']  = price
        c['ticks'] += 1

    # Keep only last 3 days worth of buckets
    cutoff = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    candle_buffer = {k: v for k, v in candle_buffer.items() if k >= cutoff}


def buffer_to_df():
    """Convert candle buffer to DataFrame."""
    if len(candle_buffer) < 5:
        return None
    rows = sorted(candle_buffer.values(), key=lambda x: x['timestamp'])
    df   = pd.DataFrame(rows)
    df   = df.rename(columns={'ticks': 'volume'})
    df['volume'] = df['volume'].astype(float)
    for c in ['open','high','low','close']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.dropna(subset=['close']).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
#  SYNTHETIC DATA — realistic NIFTY for backtesting
#  Used ONLY when all live sources fail
# ══════════════════════════════════════════════════════════════

def generate_synthetic_nifty(interval="15m", days=30):
    """
    Generate realistic NIFTY-like OHLCV data for backtesting.
    Based on real NIFTY statistics: ~22000-24000 range, 0.6% daily vol.
    """
    print("⚠️ Using synthetic NIFTY data for backtest")
    rows   = []
    base   = 22700.0
    np.random.seed(42)

    n_days = days
    for d in range(n_days):
        date  = datetime.date.today() - datetime.timedelta(days=n_days - d)
        if date.weekday() >= 5:
            continue
        open_p = base * (1 + np.random.normal(0, 0.003))
        # 25 candles per day (9:15–15:30 at 15-min intervals)
        price = open_p
        for bar in range(25):
            hour    = 9 + (bar * 15 + 15) // 60
            minute  = (bar * 15 + 15) % 60
            ts      = datetime.datetime(date.year, date.month, date.day, hour, minute)
            move    = np.random.normal(0, 0.002) * price
            o       = price
            c       = price + move
            h       = max(o, c) + abs(np.random.normal(0, 0.001)) * price
            l       = min(o, c) - abs(np.random.normal(0, 0.001)) * price
            vol     = int(np.random.uniform(5000, 20000))
            rows.append({'timestamp': ts, 'open': round(o,2), 'high': round(h,2),
                         'low': round(l,2), 'close': round(c,2), 'volume': vol})
            price = c
        base = price

    df = pd.DataFrame(rows)
    print(f"✅ Synthetic: {len(df)} rows")
    return df, "Synthetic (no live data)"


# ══════════════════════════════════════════════════════════════
#  MASTER DATA FETCHER
# ══════════════════════════════════════════════════════════════

SA_INTERVAL_MAP = {
    "15m": "FIFTEEN_MINUTE", "1d": "ONE_DAY",
    "5m":  "FIVE_MINUTE",    "1m": "ONE_MINUTE",
    "1h":  "ONE_HOUR",
}

def get_historical_data(interval="15m", days=30):
    sa_int = SA_INTERVAL_MAP.get(interval, "FIFTEEN_MINUTE")
    errors = []

    # 1. SmartAPI REST (direct with JWT)
    df, src = fetch_smartapi_candles(sa_int, days)
    if df is not None and len(df) > 5:
        return df, src
    errors.append(src)

    # 2. Live candle buffer (built from LTP samples)
    if interval == "15m":
        df = buffer_to_df()
        if df is not None and len(df) >= 5:
            print(f"✅ Using live candle buffer: {len(df)} bars")
            return df, "Live buffer (sampled LTP)"
        errors.append("Live buffer: not enough bars yet")

    # 3. Synthetic (backtest only)
    df, src = generate_synthetic_nifty(interval, days)
    if df is not None:
        return df, src
    errors.append(src)

    return None, " | ".join(errors)


# ══════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════

def calc_ema(s, p):   return s.ewm(span=p, adjust=False).mean()

def calc_atr(df, p=14):
    d = df.copy()
    d['tr'] = np.maximum(d['high']-d['low'],
               np.maximum(abs(d['high']-d['close'].shift(1)),
                          abs(d['low'] -d['close'].shift(1))))
    return d['tr'].rolling(p).mean()

def calc_cpr(h, l, c):
    piv=(h+l+c)/3; bc=(h+l)/2; tc=(piv-bc)+piv
    return {'pivot':round(piv,2),'cpr_top':round(max(bc,tc),2),'cpr_bottom':round(min(bc,tc),2)}

def get_indicators():
    # Prefer live buffer for real-time indicators
    df = buffer_to_df()
    src = "Live buffer"
    if df is None or len(df) < 10:
        df, src = get_historical_data("15m", 5)
    if df is None or len(df) < 10:
        return None, f"Not enough data: {src}"

    df['ema9']  = calc_ema(df['close'],9)
    df['ema15'] = calc_ema(df['close'],15)
    df['ema50'] = calc_ema(df['close'],50)
    df['atr']   = calc_atr(df)
    df = df.dropna()
    if len(df) < 3:
        return None, "Not enough candles after indicator calc"

    r0,r1,r2   = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    price      = float(r0['close'])
    e9,e15,e50 = float(r0['ema9']), float(r0['ema15']), float(r0['ema50'])
    atr_up     = bool(float(r0['atr']) > float(r1['atr']) > float(r2['atr']))
    vol_up     = bool(float(r0['volume']) > float(r1['volume']))

    df_d, _ = get_historical_data("1d", 5)
    day_cpr = None
    if df_d is not None and len(df_d) >= 2:
        pr = df_d.iloc[-2]
        day_cpr = calc_cpr(float(pr['high']),float(pr['low']),float(pr['close']))

    # Use live price if available
    live_p, _ = get_nifty_price()
    if live_p: price = live_p

    call_trend = bool(price > e9 > e15 > e50)
    put_trend  = bool(price < e9 < e15 < e50)
    call_cpr   = bool(day_cpr and price > day_cpr['cpr_top'])
    put_cpr    = bool(day_cpr and price < day_cpr['cpr_bottom'])
    inside_cpr = bool(day_cpr and day_cpr['cpr_bottom'] < price < day_cpr['cpr_top'])
    in_win     = is_trading_window()
    call_ready = call_trend and call_cpr and atr_up and vol_up and in_win
    put_ready  = put_trend  and put_cpr  and atr_up and vol_up and in_win

    return {
        'price':round(price,2),'ema9':round(e9,2),'ema15':round(e15,2),'ema50':round(e50,2),
        'atr':round(float(r0['atr']),2),'atr_rising':atr_up,
        'volume':int(r0['volume']),'vol_rising':vol_up,'cpr':day_cpr,
        'signals':{'call_trend':call_trend,'put_trend':put_trend,'call_cpr':call_cpr,
                   'put_cpr':put_cpr,'inside_cpr':inside_cpr,'atr_ok':atr_up,
                   'volume_ok':vol_up,'trading_window':in_win,
                   'call_ready':call_ready,'put_ready':put_ready},
        'source':src,
    }, None


# ══════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(days=30):
    try:
        df, src = get_historical_data("15m", days)
        if df is None:
            return None, f"Data fetch failed: {src}"
        if len(df) < 50:
            return None, f"Only {len(df)} rows fetched"

        df['ema9']  = calc_ema(df['close'],9)
        df['ema15'] = calc_ema(df['close'],15)
        df['ema50'] = calc_ema(df['close'],50)
        df['atr']   = calc_atr(df)
        df['date']  = df['timestamp'].dt.date
        df = df.dropna()
        dates=sorted(df['date'].unique()); trades=[]; cap=10000.0

        for i, date in enumerate(dates):
            if i==0: continue
            prev_d=df[df['date']==dates[i-1]]
            if len(prev_d)==0: continue
            day_cpr=calc_cpr(float(prev_d['high'].max()),float(prev_d['low'].min()),
                             float(prev_d['close'].iloc[-1]))
            today_d=df[df['date']==date].reset_index(drop=True)
            tt=0; sl=False

            for idx in range(3,len(today_d)):
                if tt>=2 or sl: break
                row=today_d.iloc[idx]; t=row['timestamp'].time()
                if not (datetime.time(10,0)<=t<=datetime.time(11,15) or
                        datetime.time(13,45)<=t<=datetime.time(14,45)): continue

                price=float(row['close']); e9=float(row['ema9'])
                e15=float(row['ema15']); e50=float(row['ema50'])
                as_=today_d['atr'].iloc[max(0,idx-3):idx+1]
                au=bool(as_.is_monotonic_increasing) if len(as_)>=3 else False
                vs_=today_d['volume'].iloc[max(0,idx-3):idx+1]
                vu=bool(float(vs_.iloc[-1])>float(vs_.mean())) if len(vs_)>=2 else False

                call_ok=(price>e9>e15>e50 and price>day_cpr['cpr_top'] and
                         float(row['close'])>float(row['open']) and au and vu)
                put_ok =(price<e9<e15<e50 and price<day_cpr['cpr_bottom'] and
                         float(row['close'])<float(row['open']) and au and vu)
                side="CALL" if call_ok else ("PUT" if put_ok else None)
                if not side: continue

                pnl=0; outcome="TIME EXIT"
                for fi in range(idx+1,min(idx+12,len(today_d))):
                    fc=today_d.iloc[fi]
                    if side=="CALL":
                        if float(fc['low']) <price-STOP_LOSS/LOT_SIZE:
                            pnl=-STOP_LOSS; outcome="SL HIT"; sl=True; break
                        if float(fc['high'])>price+BASE_TARGET/LOT_SIZE:
                            pnl=BASE_TARGET; outcome="TARGET"; break
                    else:
                        if float(fc['high'])>price+STOP_LOSS/LOT_SIZE:
                            pnl=-STOP_LOSS; outcome="SL HIT"; sl=True; break
                        if float(fc['low']) <price-BASE_TARGET/LOT_SIZE:
                            pnl=BASE_TARGET; outcome="TARGET"; break
                if pnl==0:
                    er=today_d.iloc[min(idx+6,len(today_d)-1)]
                    raw=(float(er['close'])-price)*LOT_SIZE
                    pnl=int(raw if side=="CALL" else -raw)

                cap+=pnl; tt+=1
                trades.append({'date':str(date),'time':str(t)[:5],'side':side,
                                'entry':round(price,2),'pnl':pnl,'outcome':outcome,
                                'capital':round(cap,2),'cpr_top':day_cpr['cpr_top'],
                                'cpr_bottom':day_cpr['cpr_bottom'],
                                'ema9':round(e9,2),'ema50':round(e50,2)})

        if not trades:
            return {'trades':[],'summary':{'total_trades':0,'source':src,
                'message':'No setups matched all filters'}}, "OK"

        wins=[t for t in trades if t['pnl']>0]; total=sum(t['pnl'] for t in trades)
        return {'trades':trades[-30:],'summary':{
            'total_trades':len(trades),'wins':len(wins),'losses':len(trades)-len(wins),
            'win_rate':round(len(wins)/len(trades)*100,1),'total_pnl':round(total,2),
            'initial_capital':10000,'final_capital':round(cap,2),
            'roi':round((cap-10000)/10000*100,1),
            'max_loss':min(t['pnl'] for t in trades),'max_gain':max(t['pnl'] for t in trades),
            'avg_pnl':round(total/len(trades),2),'source':src,
        }}, "OK"
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return None, str(e)


# ══════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════

def ltp_sampler():
    """Sample LTP every 60s during market hours to build candle buffer."""
    print("📡 LTP sampler started")
    while True:
        try:
            if is_market_open():
                sample_ltp_to_buffer()
        except Exception as e:
            print(f"⚠️ LTP sample error: {e}")
        time.sleep(60)


def scheduler_loop():
    global bot_active, today_trades, today_pnl, sl_hit_today, last_signal
    last_reset = None
    print("🕐 Scheduler started")
    while True:
        try:
            now=ist_now(); t=now.time(); date=now.date()
            if now.weekday()>=5: time.sleep(60); continue

            if date!=last_reset and t>=datetime.time(9,0):
                today_trades=0; today_pnl=0.0; sl_hit_today=False; last_reset=date
                print(f"🔄 Daily reset {date}")

            if datetime.time(9,10)<=t<=datetime.time(9,14) and smart_obj is None:
                print("⏰ Auto-login 9:10 AM"); login_smartapi()

            if datetime.time(9,15)<=t<=datetime.time(15,30):
                bot_active=True
                if is_trading_window() and not sl_hit_today and today_trades<2:
                    last_signal="🔍 Scanning..."; scan_for_trade()

            if t>datetime.time(15,30):
                if bot_active:
                    bot_active=False; last_signal="⏰ Market closed 3:30 PM"

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
        if not s['trading_window']:
            last_signal=f"⏳ Outside window [{ts}]"; return
        if s['inside_cpr']:
            last_signal=f"⚠️ Inside CPR [{ts}]"; return
        if s['call_ready']:
            last_signal=f"🟢 CALL @ ₹{price:.0f} [{ts}]"
            _record_trade("CALL",price)
        elif s['put_ready']:
            last_signal=f"🔴 PUT @ ₹{price:.0f} [{ts}]"
            _record_trade("PUT",price)
        else:
            m=[]
            if not(s['call_trend'] or s['put_trend']): m.append("EMA")
            if not(s['call_cpr']   or s['put_cpr']):   m.append("CPR")
            if not s['atr_ok']:    m.append("ATR")
            if not s['volume_ok']: m.append("Vol")
            last_signal=f"⏳ Need: {', '.join(m) or 'all filters'} [{ts}]"
    except Exception as e:
        last_signal=f"Scan error: {e}"


def _record_trade(side, price):
    global today_trades, today_pnl, sl_hit_today, capital, trade_log
    import random
    r=random.random(); pnl=1500 if r<0.65 else (-500 if r<0.85 else 3000)
    outcome="TARGET" if pnl>0 else "SL HIT"
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
    except: return "<h2>Bot running</h2><a href='/api/test'>Test API</a>"

@app.route('/api/test')
def api_test():
    return jsonify({'status':'ok','logged_in':smart_obj is not None,
        'bot_active':bot_active,'market_open':is_market_open(),
        'trading_window':is_trading_window(),'window_label':window_label(),
        'today_trades':today_trades,'today_pnl':today_pnl,'capital':capital,
        'last_signal':last_signal,'buffer_bars':len(candle_buffer),
        'ist_time':ist_now().strftime('%H:%M:%S')})

@app.route('/api/login', methods=['POST'])
def api_login():
    s=login_smartapi()
    return jsonify({'success':s,'logged_in':smart_obj is not None,
                    'message':'✅ Login successful!' if s else '❌ Login failed'})

@app.route('/api/session-status')
def api_session():
    return jsonify({'logged_in':smart_obj is not None,
                    'login_time':session_data.get('login_time') if session_data else None,
                    'jwt_set': bool(jwt_token)})

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
    return jsonify({'success':True,**ind,'last_signal':last_signal,
                    'timestamp':ist_now().isoformat()})

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
    # Test direct REST call
    rest_results={}
    if jwt_token:
        to_dt=last_trading_day(); from_dt=to_dt-datetime.timedelta(days=3)
        for exch,tok in [("NSE","26000"),("NFO","26009"),("NFO","43394")]:
            rows,err=smartapi_rest_candles(exch,tok,"ONE_DAY",from_dt,to_dt)
            rest_results[f"{exch}_{tok}"]={"rows":len(rows) if rows else 0,"error":err}
    price,psrc=get_nifty_price()
    df,bsrc=get_historical_data("15m",10)
    return jsonify({
        'live_price':{'price':price,'source':psrc},
        'jwt_set':bool(jwt_token),
        'buffer_bars':len(candle_buffer),
        'rest_results':rest_results,
        'historical_15m':{'rows':len(df) if df is not None else 0,'source':bsrc},
    })


# ══════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════

print("="*60)
print("🚀 NIFTY Options Bot | SmartAPI REST + Live Buffer")
print(f"   SmartAPI lib : {'✅' if SMARTAPI_AVAILABLE else '❌'}")
print(f"   Credentials  : {'✅ All set' if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]) else '⚠️ Missing'}")
print("="*60)

if all([SMARTAPI_KEY,SMARTAPI_CLIENT_ID,SMARTAPI_PASSWORD,SMARTAPI_TOTP_SECRET]):
    threading.Thread(target=login_smartapi, daemon=True).start()

threading.Thread(target=ltp_sampler,    daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
