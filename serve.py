#!/usr/bin/env python3
"""
TrainingHub local server — syncs Garmin data and serves the dashboard.

Usage:
    python3 serve.py              # sync + serve on port 8787
    python3 serve.py --port 9000  # custom port
    python3 serve.py --no-sync    # skip sync, just serve existing data
"""

import argparse
import http.server
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

DIR = Path(__file__).parent
PORT_DEFAULT = 8787


def run_sync():
    """Run garmin_sync.py to pull fresh data."""
    print("Syncing Garmin data...")
    result = subprocess.run(
        [sys.executable, str(DIR / "garmin_sync.py"), "--days", "730"],
        cwd=str(DIR),
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(result.stdout.strip())
    else:
        print(f"Sync warning: {result.stderr.strip() or result.stdout.strip()}")
        print("Continuing with existing data...")


def run_analysis():
    """Run fitness_analysis.py to regenerate the dashboard."""
    print("Generating dashboard...")
    result = subprocess.run(
        [sys.executable, str(DIR / "fitness_analysis.py")],
        cwd=str(DIR),
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(result.stdout.strip())
    else:
        print(f"Analysis error: {result.stderr.strip()}")
        sys.exit(1)


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serve files from garmin_data/, redirect / to the dashboard."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR / "garmin_data"), **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.path = "/fitness_dashboard.html"
        return super().do_GET()

    def log_message(self, format, *args):
        # Suppress noisy request logs
        pass


def main():
    parser = argparse.ArgumentParser(description="TrainingHub local server")
    parser.add_argument("--port", type=int, default=PORT_DEFAULT)
    parser.add_argument("--no-sync", action="store_true", help="Skip Garmin sync")
    args = parser.parse_args()

    if not args.no_sync:
        run_sync()

    run_analysis()

    server = http.server.HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    url = f"http://localhost:{args.port}"
    print(f"\n  TrainingHub running at {url}")
    print("  Press Ctrl+C to stop.\n")

    # Open browser after a short delay
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
