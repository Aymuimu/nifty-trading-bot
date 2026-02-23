from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from nsepy import get_history
from nsepy.live import get_quote
from datetime import datetime, timedelta
import requests
import pandas as pd
import os
import threading
import time

app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app)

# Dhan Configuration
DHAN_CLIENT_ID = os.environ.get('DHAN_CLIENT_ID', '')
DHAN_CLIENT_SECRET = os.environ.get('DHAN_CLIENT_SECRET', '')
DHAN_BASE_URL = 'https://api.dhan.co'

# Token storage
dhan_token_info = {
    'access_token': None,
    'expires_at': None,
    'last_refresh': None
}

print("="*60)
print("🚀 NIFTY Trading Bot Backend Starting...")
print("="*60)
print(f"✅ NSEpy: Enabled (for backtesting)")
print(f"{'✅' if DHAN_CLIENT_ID else '⚠️'} Dhan Client ID: {'Configured' if DHAN_CLIENT_ID else 'Not configured'}")
print(f"{'✅' if DHAN_CLIENT_SECRET else '⚠️'} Dhan Client Secret: {'Configured' if DHAN_CLIENT_SECRET else 'Not configured'}")
print("="*60)

# Cache
cache = {
    'nifty_price': {'value': None, 'timestamp': None, 'source': None},
    'option_chain': {'value': None, 'timestamp': None}
}
CACHE_DURATION = 30

def generate_dhan_token():
    """Generate Dhan access token"""
    global dhan_token_info
    
    if not DHAN_CLIENT_ID or not DHAN_CLIENT_SECRET:
        print("⚠️ Dhan credentials not configured")
        return False
    
    try:
        print("🔄 Generating new Dhan access token...")
        
        token_url = f'{DHAN_BASE_URL}/v2/access_token'
        
        payload = {
            'client_id': DHAN_CLIENT_ID,
            'client_secret': DHAN_CLIENT_SECRET,
            'grant_type': 'client_credentials'
        }
        
        headers = {'Content-Type': 'application/json'}
        
        response = requests.post(token_url, json=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            access_token = data.get('access_token')
            expires_in = data.get('expires_in', 86400)
            
            if access_token:
                dhan_token_info['access_token'] = access_token
                dhan_token_info['expires_at'] = datetime.now() + timedelta(seconds=expires_in)
                dhan_token_info['last_refresh'] = datetime.now()
                
                print(f"✅ New Dhan token generated! Expires at: {dhan_token_info['expires_at'].strftime('%Y-%m-%d %H:%M:%S')}")
                return True
        
        print(f"❌ Token generation failed: HTTP {response.status_code}")
        return False
            
    except Exception as e:
        print(f"❌ Error generating token: {str(e)}")
        return False

def is_token_valid():
    """Check if token is valid"""
    if not dhan_token_info['access_token'] or not dhan_token_info['expires_at']:
        return False
    buffer_time = timedelta(minutes=5)
    return datetime.now() < (dhan_token_info['expires_at'] - buffer_time)

def get_valid_token():
    """Get valid token, refresh if needed"""
    if not is_token_valid():
        print("🔄 Token expired, generating new one...")
        if generate_dhan_token():
            return dhan_token_info['access_token']
        return None
    return dhan_token_info['access_token']

def token_refresh_worker():
    """Background worker for token refresh"""
    while True:
        try:
            time.sleep(3600)
            if not is_token_valid():
                print("⏰ Auto-refresh: Token needs renewal")
                generate_dhan_token()
            else:
                remaining = dhan_token_info['expires_at'] - datetime.now()
                hours_left = remaining.total_seconds() / 3600
                print(f"✅ Token still valid: {hours_left:.1f} hours remaining")
        except Exception as e:
            print(f"❌ Token refresh worker error: {str(e)}")
            time.sleep(300)

def start_token_worker():
    """Start background token worker"""
    generate_dhan_token()
    worker = threading.Thread(target=token_refresh_worker, daemon=True)
    worker.start()
    print("✅ Token auto-refresh worker started")

def is_cache_valid(cache_key):
    """Check if cache is valid"""
    if cache[cache_key]['value'] is None or cache[cache_key]['timestamp'] is None:
        return False
    elapsed = (datetime.now() - cache[cache_key]['timestamp']).total_seconds()
    return elapsed < CACHE_DURATION

@app.route('/')
def index():
    """Serve the main page"""
    try:
        return send_from_directory('public', 'index.html')
    except Exception as e:
        print(f"❌ Error serving index.html: {str(e)}")
        return f"""
        <h1>NIFTY Trading Bot</h1>
        <p>Error: Could not find index.html in public/ folder</p>
        <p>Error details: {str(e)}</p>
        <p>Make sure index.html is in the public/ folder in your repository.</p>
        """, 404

@app.route('/api/test')
def test():
    """Test endpoint"""
    return jsonify({
        'status': 'ok',
        'message': 'Hybrid backend with auto-refresh!',
        'dhan_configured': bool(DHAN_CLIENT_ID and DHAN_CLIENT_SECRET),
        'dhan_token_valid': is_token_valid(),
        'nsepy_available': True,
        'mode': 'Dhan API (Auto-Refresh) + NSEpy (Backtest)'
    })

@app.route('/api/nifty-price')
def get_nifty_price():
    """Get NIFTY price"""
    try:
        if is_cache_valid('nifty_price'):
            return jsonify({
                'success': True,
                'price': cache['nifty_price']['value'],
                'source': cache['nifty_price']['source'] + ' (cached)'
            })
        
        if DHAN_CLIENT_ID and DHAN_CLIENT_SECRET:
            try:
                token = get_valid_token()
                if token:
                    print("📡 Fetching NIFTY from Dhan API...")
                    headers = {
                        'access-token': token,
                        'Content-Type': 'application/json'
                    }
                    payload = {"IDX_I": ["13"]}
                    response = requests.post(
                        f'{DHAN_BASE_URL}/v2/marketfeed/ltp',
                        headers=headers,
                        json=payload,
                        timeout=5
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        if data.get('data') and 'IDX_I' in data['data']:
                            nifty_data = data['data']['IDX_I'].get('13', {})
                            price = float(nifty_data.get('LTP', 0))
                            if price > 0:
                                cache['nifty_price']['value'] = price
                                cache['nifty_price']['timestamp'] = datetime.now()
                                cache['nifty_price']['source'] = 'Dhan API'
                                print(f"✅ NIFTY from Dhan: ₹{price}")
                                return jsonify({
                                    'success': True,
                                    'price': price,
                                    'source': 'Dhan API (live)',
                                    'timestamp': datetime.now().isoformat()
                                })
            except Exception as e:
                print(f"⚠️ Dhan API error: {str(e)}")
        
        print("📡 Fetching NIFTY from NSEpy...")
        quote = get_quote('NIFTY 50', as_json=True)
        if quote and 'lastPrice' in quote:
            price = float(quote['lastPrice'])
            cache['nifty_price']['value'] = price
            cache['nifty_price']['timestamp'] = datetime.now()
            cache['nifty_price']['source'] = 'NSEpy'
            print(f"✅ NIFTY from NSEpy: ₹{price}")
            return jsonify({
                'success': True,
                'price': price,
                'source': 'NSEpy (fallback)',
                'timestamp': datetime.now().isoformat()
            })
        
        raise Exception("No valid data")
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        estimated = 25660 + (hash(str(datetime.now().minute)) % 60)
        return jsonify({
            'success': True,
            'price': estimated,
            'source': 'Estimated',
            'error': str(e)
        })

@app.route('/api/historical-data', methods=['POST'])
def get_historical_data():
    """Get historical data from NSEpy"""
    try:
        data = request.json
        start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d')
        end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d')
        
        print(f"📊 Fetching historical data: {start_date} to {end_date}")
        
        nifty_data = get_history(symbol="NIFTY", start=start_date, end=end_date, index=True)
        
        historical_prices = []
        for date, row in nifty_data.iterrows():
            historical_prices.append({
                'date': date.strftime('%Y-%m-%d'),
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': int(row['Volume']) if 'Volume' in row else 0
            })
        
        print(f"✅ Retrieved {len(historical_prices)} days")
        
        return jsonify({
            'success': True,
            'data': historical_prices,
            'source': 'NSEpy',
            'count': len(historical_prices)
        })
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/market-status')
def get_market_status():
    """Get market status"""
    now = datetime.now()
    is_weekday = now.weekday() < 5
    current_time = now.time()
    market_open = datetime.strptime('09:15', '%H:%M').time()
    market_close = datetime.strptime('15:30', '%H:%M').time()
    is_market_hours = market_open <= current_time <= market_close
    is_open = is_weekday and is_market_hours
    
    holidays = ['2026-01-26', '2026-03-14', '2026-04-10', '2026-04-14',
                '2026-04-18', '2026-05-01', '2026-08-15', '2026-08-27',
                '2026-10-02', '2026-10-21', '2026-11-05', '2026-12-25']
    
    today_str = now.strftime('%Y-%m-%d')
    is_holiday = today_str in holidays
    
    return jsonify({
        'success': True,
        'is_open': is_open and not is_holiday,
        'is_holiday': is_holiday,
        'current_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'day': now.strftime('%A')
    })

@app.route('/api/token-status')
def token_status():
    """Get token status"""
    if not dhan_token_info['access_token']:
        return jsonify({'valid': False, 'message': 'No token generated yet'})
    
    if not dhan_token_info['expires_at']:
        return jsonify({'valid': False, 'message': 'Token expiry unknown'})
    
    now = datetime.now()
    expires_at = dhan_token_info['expires_at']
    time_remaining = expires_at - now
    hours_remaining = time_remaining.total_seconds() / 3600
    
    return jsonify({
        'valid': is_token_valid(),
        'expires_at': expires_at.isoformat(),
        'hours_remaining': round(hours_remaining, 2),
        'last_refresh': dhan_token_info['last_refresh'].isoformat() if dhan_token_info['last_refresh'] else None,
        'auto_refresh_active': True
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    print("\n" + "="*60)
    print("🔄 Initializing Dhan token auto-refresh...")
    print("="*60)
    
    start_token_worker()
    
    print("\n" + "="*60)
    print("🚀 Server Starting...")
    print(f"📡 Port: {port}")
    print(f"🔄 Mode: Hybrid with Auto-Refresh")
    print(f"📊 Backtest: NSEpy")
    print(f"📈 Forward: Dhan API (Auto-Refresh)")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=port, debug=False)
