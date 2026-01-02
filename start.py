import os
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")
    
    def log_message(self, format, *args):
        pass

def run_health():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Health check server running on port {port}")
    server.serve_forever()

def run_bot():
    from bot import AdBot
    bot = AdBot()
    asyncio.run(bot.start())

if __name__ == "__main__":
    t = threading.Thread(target=run_health, daemon=True)
    t.start()
    run_bot()
