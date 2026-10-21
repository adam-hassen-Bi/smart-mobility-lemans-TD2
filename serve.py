"""Open the dashboard locally: python3 serve.py

Serves the docs/ folder on http://localhost:8000 and opens it in the browser.
Add --refresh to take a fresh real-time snapshot first
(needs: pip3 install requests pandas gtfs-realtime-bindings).
"""
import functools
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8000

if "--refresh" in sys.argv:
    subprocess.run([sys.executable, os.path.join("scripts", "collect_snapshot.py")], cwd=ROOT, check=True)

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=os.path.join(ROOT, "docs"))
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", PORT), handler) as httpd:
    url = f"http://localhost:{PORT}"
    print(f"Dashboard running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
