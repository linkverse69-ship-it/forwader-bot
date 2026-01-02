import os
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple health check endpoint for Clever Cloud"""
    
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is running")
    
    def log_message(self, format, *args):
        # Suppress HTTP request logs to keep console clean
        pass


def run_health_check_server():
    """Run health check HTTP server in background thread"""
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"✅ Health check server running on port {port}")
    server.serve_forever()


def run_telegram_bot():
    """Run the main Telegram bot"""
    try:
        # Import main function from your bot file
        from bot import main
        
        print("🤖 Starting Telegram Media Forwarder Bot...")
        main()
        
    except ImportError as e:
        print(f"❌ Error importing bot: {e}")
        print("Make sure 'bot.py' exists in the same directory")
        raise
    except Exception as e:
        print(f"❌ Bot error: {e}")
        raise


if __name__ == "__main__":
    # Start health check server in daemon thread
    health_thread = threading.Thread(target=run_health_check_server, daemon=True)
    health_thread.start()
    
    # Run main bot in main thread
    run_telegram_bot()
