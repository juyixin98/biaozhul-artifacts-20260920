#!/usr/bin/env python3
"""Block until MySQL accepts TCP connections. Uses only stdlib + Django env."""
import os
import socket
import sys
import time

host = os.environ.get("MYSQL_HOST", "db")
port = int(os.environ.get("MYSQL_PORT", "3306"))

deadline = time.monotonic() + 120
while True:
    try:
        with socket.create_connection((host, port), timeout=3):
            sys.exit(0)
    except OSError:
        if time.monotonic() > deadline:
            print(f"MySQL at {host}:{port} unreachable after 120s", file=sys.stderr)
            sys.exit(1)
        time.sleep(1)
