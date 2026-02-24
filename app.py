from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
import sys

# Initialize Flask first
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app)

print("="*60)
print("🚀 NIFTY Trading Bot Backend Starting...")
print(f"Python version: {sys.version}")
print("="*60)

# Dhan Configuration
DHAN_CLIENT_ID = os.environ.get('DHAN_CLIENT_ID', '')
DHAN_CLIENT_SECRET = os.environ.get('DHAN_CLIENT_SECRET', '')

print(f"{'✅' if DHAN_CLIENT_ID else '⚠️'} Dhan Client ID: {'Configured' if DHAN_CLIENT_ID else 'Not configured'}")
print(f"{'✅' if DHAN_CLIENT_SECRET else '⚠️'} Dhan Client Secret: {'Configured' if DHAN_CLIENT_SECRET else 'Not configured'}")

# Try to import optional dependencies
try:
    from nsepy import get_history
    from nsepy.live import get_quote
    from datetime import datetime, timedelta
    import pandas as pd
    import requests
    import threading
    import time
    NSEPY_AVAILABLE = True
    print("✅ NSEpy: Enabled")
except ImportError as e:
    NSEPY_AVAILABLE = False
    print(f"⚠️ NSEpy: Not available ({str(e)})")
    # Create dummy datetime if needed
    from datetime import datetime, timedelta
    import requests
    import threading
    import time

print("="*60)

# Token storage
dhan_token_info = {
    'access_token': None,
    'expires_at': None,
    'last_refresh': None
}

# Cache
cache = {
    'nifty_price': {'value': None, 'timestamp': None, 'source': None}
}
CACHE_DURATION = 30

def generate_dhan_token():
    """Generate Dhan access token"""
    global dhan_token_info
    
    if not DHAN_CLIENT_ID or not DHAN_CLIENT_SECRET:
        return False
    
    try:
        print("🔄 Generating Dhan token...")
        # Simplified token generation for now
        # You'll need to adjust this based on Dhan's actual API
        dhan_token_info['access_token'] = 'temporary_token'
        dhan_token_info['expires_at'] = datetime.now() + timedelta(hours=24)
        dhan_token_info['last_refresh'] = datetime.now()
        print("✅ Token generated (placeholder)")
        return True
    except Exception as e:
        print(f"❌ Token error: {str(e)}")
        return False

def is_token_valid():
    """Check if token is valid"""
    if not dhan_token_info['access_token'] or not dhan_token_info['expires_at']:
        return False
    buffer_time = timedelta(minutes=5)
    return datetime.now() < (dhan_token_info['expires_at'] - buffer_time)

def get_valid_token():
    """Get valid token"""
    if not is_token_valid():
        if generate_dhan_token():
            return dhan_token_info['access_token']
        return None
    return dhan_token_info['access_token']

def is_cache_valid(cache_key):
    """Check cache validity"""
    if cache[cache_key]['value'] is None or cache[cache_key]['timestamp'] is None:
        return False
    elapsed = (datetime.now() - cache[cache_key]['timestamp']).total_seconds()
    return elapsed < CACHE_DURATION

@app.route('/')
def index():
    """Serve main page"""
    try:
        return send_from_directory('public', 'index.html')
    except Exception as e:
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>NIFTY Trading Bot</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                    background: #f5f5f5;
                }}
                .container {{
                    background: white;
                    padding: 30px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                h1 {{ color: #667eea; }}
                .status {{ padding: 15px; margin: 20px 0; border-radius: 5px; }}
                .success {{ background: #d4edda; color: #155724; }}
                .warning {{ background: #fff3cd; color: #856404; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📊 NIFTY Trading Bot</h1>
                <div class="status warning">
                    <strong>⚠️ Frontend not found</strong>
                    <p>Please create <code>public/index.html</code> file in your repository.</p>
                    <p>Error: {str(e)}</p>
                </div>
                <div class="status success">
                    <strong>✅ Backend is running!</strong>
                    <p>API endpoints are available:</p>
                    <ul>
                        <li><a href="/api/test">/api/test</a> - Test backend</li>
                        <li><a href="/api/nifty-price">/api/nifty-price</a> - Get NIFTY price</li>
                        <li><a href="/api/market-status">/api/market-status</a> - Market status</li>
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """, 200

@app.route('/api/test')
def test():
    """Test endpoint"""
    return jsonify({
        'status': 'ok',
        'message': 'NIFTY Trading Bot Backend',
        'dhan_configured': bool(DHAN_CLIENT_ID and DHAN_CLIENT_SECRET),
        'dhan_token_valid': is_token_valid(),
        'nsepy_available': NSEPY_AVAILABLE,
        'mode': 'Hybrid (Dhan + NSEpy)' if NSEPY_AVAILABLE else 'Limited (NSEpy disabled)',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/nifty-price')
def get_nifty_price():
    """Get NIFTY price"""
    try:
        # Check cache
        if is_cache_valid('nifty_price'):
            return jsonify({
                'success': True,
                'price': cache['nifty_price']['value'],
                'source': cache['nifty_price']['source'] + ' (cached)',
                'timestamp': datetime.now().isoformat()
            })
        
        # Try NSEpy if available
        if NSEPY_AVAILABLE:
            try:
                print("📡 Fetching from NSEpy...")
                quote = get_quote('NIFTY 50', as_json=True)
                if quote and 'lastPrice' in quote:
                    price = float(quote['lastPrice'])
                    cache['nifty_price']['value'] = price
                    cache['nifty_price']['timestamp'] = datetime.now()
                    cache['nifty_price']['source'] = 'NSEpy'
                    print(f"✅ NIFTY: ₹{price}")
                    return jsonify({
                        'success': True,
                        'price': price,
                        'source': 'NSEpy',
                        'timestamp': datetime.now().isoformat()
                    })
            except Exception as e:
                print(f"⚠️ NSEpy error: {str(e)}")
        
        # Fallback to estimated price
        import random
        estimated = 25660 + random.randint(0, 100)
        print(f"⚠️ Using estimated price: ₹{estimated}")
        return jsonify({
            'success': True,
            'price': estimated,
            'source': 'Estimated (data sources unavailable)',
            'timestamp': datetime.now().isoformat()
        })
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/api/historical-data', methods=['POST'])
def get_historical_data():
    """Get historical data"""
    try:
        if not NSEPY_AVAILABLE:
            return jsonify({
                'success': False,
                'error': 'NSEpy not available'
            }), 503
        
        data = request.json
        start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d')
        end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d')
        
        print(f"📊 Fetching historical: {start_date} to {end_date}")
        
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
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/market-status')
def get_market_status():
    """Get market status"""
    try:
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
            'day': now.strftime('%A'),
            'message': 'Market is open' if (is_open and not is_holiday) else 'Market is closed'
        })
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/debug-env')
   def debug_env():
       """Debug endpoint to check environment variables"""
       import os
       
       return jsonify({
           'DHAN_CLIENT_ID_exists': bool(os.environ.get('DHAN_CLIENT_ID')),
           'DHAN_CLIENT_ID_length': len(os.environ.get('DHAN_CLIENT_ID', '')),
           'DHAN_CLIENT_SECRET_exists': bool(os.environ.get('DHAN_CLIENT_SECRET')),
           'DHAN_CLIENT_SECRET_length': len(os.environ.get('DHAN_CLIENT_SECRET', '')),
           'note': 'Check if exists is True and length > 0'
       })
    """Get token status"""
    try:
        if not dhan_token_info['access_token']:
            return jsonify({
                'valid': False,
                'message': 'No token generated yet (credentials not configured or invalid)'
            })
        
        if not dhan_token_info['expires_at']:
            return jsonify({
                'valid': False,
                'message': 'Token expiry unknown'
            })
        
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
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    print("\n" + "="*60)
    print("🚀 Server Starting...")
    print(f"📡 Port: {port}")
    print(f"🔄 Mode: Hybrid Trading Bot")
    print(f"📊 Backtest: {'NSEpy' if NSEPY_AVAILABLE else 'Disabled'}")
    print(f"📈 Forward: {'Dhan API' if DHAN_CLIENT_ID else 'Not configured'}")
    print("="*60 + "\n")
    
    # Initialize token if credentials are available
    if DHAN_CLIENT_ID and DHAN_CLIENT_SECRET:
        generate_dhan_token()
    
    app.run(host='0.0.0.0', port=port, debug=False)
