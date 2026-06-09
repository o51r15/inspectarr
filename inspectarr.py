#!/usr/bin/env python3
"""
inspectarr — torrent watchdog for *arr ecosystems

Polls qBittorrent categories, evaluates downloads against configurable rules,
blocklists bad releases in Sonarr (and future *arrs), deletes torrents + files,
logs all actions to JSON Lines, and notifies via Pushover.

Usage:
  python inspectarr.py                     # single scan run (default)
  python inspectarr.py --config /path      # alternate config file
  python inspectarr.py --dry-run           # flag matches, take no action

For the web UI with a built-in scheduler daemon, run web.py instead.
"""
import argparse
import os
import sys


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="inspectarr — torrent watchdog for *arr ecosystems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config",    default="config.yaml",
                   help="Path to config file (default: config.yaml)")
    p.add_argument("--dry-run",   action="store_true",
                   help="Flag matches but take no action (overrides config)")
    p.add_argument("--daemon",    action="store_true",
                   help="Not in the CLI — use web.py for the scheduler daemon")
    p.add_argument("--retry-now", action="store_true",
                   help="Force flush retry queue — bypasses timing and exhaustion cap, then scans")
    return p.parse_args()


def main():
    args = parse_args()

    if args.daemon:
        print("The CLI runs a single scan. For a continuous scheduler daemon,")
        print("run the web UI instead:  python web.py")
        sys.exit(1)

    if args.retry_now:
        from core.config import load_config
        from core.scanner import Scanner

        if not os.path.exists(args.config):
            print(f"ERROR: Config file not found: {args.config}")
            sys.exit(1)
        try:
            config = load_config(args.config)
        except Exception as exc:
            print(f"ERROR: Failed to load config: {exc}")
            sys.exit(1)
        if args.dry_run:
            config.dry_run = True
        scanner = Scanner(config)
        scanner.prepare()
        print("Force-flushing retry queue (bypassing timing and exhaustion cap)...")
        scanner.process_retries(force=True)
        print("Done. Running scan...")
        scanner.run_scan()
        return

    from core.config import load_config
    from core.scanner import Scanner

    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"ERROR: Failed to load config: {exc}")
        sys.exit(1)

    if args.dry_run:
        config.dry_run = True

    scanner = Scanner(config)
    scanner.startup()

    if config.retry.enabled:
        scanner.process_retries()

    scanner.run_scan()


if __name__ == "__main__":
    main()
