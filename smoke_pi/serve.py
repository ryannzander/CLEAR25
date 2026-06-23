#!/usr/bin/env python3
"""Tiny static server for the smoke dashboard (Raspberry Pi).

Serves smoke_pi/web/ on 0.0.0.0:PORT so you can open the dashboard from any
device on the LAN. JSON is sent no-store so the dashboard always reads the
freshest status the collector wrote. Pure stdlib — no Flask/Django needed.
"""
import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(WEB), **k)

    def end_headers(self):
        if self.path.endswith(".json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):  # keep the journal quiet
        pass


def main():
    ap = argparse.ArgumentParser(description="Serve the CLEAR smoke dashboard.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SMOKE_PORT", "8077")))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    (WEB / "data").mkdir(parents=True, exist_ok=True)
    print(f"Smoke dashboard -> http://{args.host}:{args.port}/  (serving {WEB})")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
