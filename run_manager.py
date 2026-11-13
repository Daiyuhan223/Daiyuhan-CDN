#!/usr/bin/env python3
"""
CDN Manager - Central Control Server
Run this on your dedicated control server (Debian 11/12/13).

Usage:
    python run_manager.py
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from manager.config import MANAGER_HOST, MANAGER_PORT


def main():
    parser = argparse.ArgumentParser(description="CDN Manager Server")
    parser.add_argument("--host", default=MANAGER_HOST, help=f"Listen host (default: {MANAGER_HOST})")
    parser.add_argument("--port", type=int, default=MANAGER_PORT, help=f"Listen port (default: {MANAGER_PORT})")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    from manager.app import app, start_cert_autosync

    start_cert_autosync()

    proto = "http"

    print("=" * 55)
    print("  CDN Manager Server")
    print("=" * 55)
    print(f"  Listen:     {proto}://{args.host}:{args.port}")
    print(f"  Database:   {os.path.abspath(os.path.join(os.path.dirname(__file__), 'data', 'cdn.db'))}")
    print(f"  API:        {proto}://{args.host}:{args.port}/api/v1/")
    print(f"  Health:     {proto}://{args.host}:{args.port}/health")
    print(f"  Token CLI:  {sys.executable} manager/cli.py node token --node-name <name>")
    print("=" * 55)
    print()

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
