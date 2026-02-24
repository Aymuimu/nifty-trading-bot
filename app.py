from flask import Flask
import os

app = Flask(__name__)

@app.route('/')
def index():
    return """
    <html>
    <head><title>Test</title></head>
    <body style="font-family: Arial; text-align: center; margin-top: 100px;">
        <h1 style="color: green;">✅ FLASK IS WORKING!</h1>
        <p>If you see this, your app is running.</p>
        <p><a href="/api/test">Click here to test API</a></p>
    </body>
    </html>
    """

@app.route('/api/test')
def test():
    return {'status': 'ok', 'message': 'Backend works!'}

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting on port {port}...")
    app.run(host='0.0.0.0', port=port)
