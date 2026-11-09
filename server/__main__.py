"""Run with:  .venv\\Scripts\\python -m server [--port 8765] [--no-browser]"""
import argparse
import socket
import threading
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

p = argparse.ArgumentParser(prog="openresense")
p.add_argument("--port", type=int, default=8765)
p.add_argument("--no-browser", action="store_true")
p.add_argument("--reload", action="store_true")
a = p.parse_args()
url = f"http://127.0.0.1:{a.port}/"

with socket.socket() as s:
    s.settimeout(0.3)  # a listener on this PC accepts at once; Windows takes ~2 s to report a refused connection
    busy = s.connect_ex(("127.0.0.1", a.port)) == 0
if busy:  # usually OpenReSense started a second time: show the running one instead of a bind error
    try:
        ours = b"<title>OpenReSense</title>" in urllib.request.urlopen(url, timeout=5).read(4096)
    except OSError:
        ours = False
    if not ours:
        raise SystemExit(f"Port {a.port} is used by another program: start with --port 9000 (or another free port).")
    print(f"OpenReSense is already running at {url}")
    if not a.no_browser:
        webbrowser.open(url)
    raise SystemExit(0)
root = Path(__file__).resolve().parent.parent
if len(str(root)) > 150:  # the deepest DLLs sit ~95 characters below; Windows cannot load DLLs from paths over 260
    print(f"Note: this folder's path is {len(str(root))} characters long. If the app fails with 'DLL load failed' or "
          "'filename or extension is too long', move the folder to a short path such as C:\\OpenReSense.")

if not a.no_browser:
    threading.Timer(1.5, webbrowser.open, [url]).start()
# Bind to localhost only: the API can read local files.
uvicorn.run("server.app:app", host="127.0.0.1", port=a.port, reload=a.reload)
