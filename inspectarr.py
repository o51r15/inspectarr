#!/usr/bin/env python3
"""
inspectarr — torrent watchdog for *arr ecosystems

Polls qBittorrent categories, evaluates downloads against configurable rules,
blocklists bad releases in Sonarr (and future *arrs), deletes torrents + files,
logs all actions to JSON Lines, and notifies via Pushover.

Usage:
  python watchdog.py                     # single scan run (default)
  python watchdog.py --config /path      # alternate config file
  python watchdog.py --dry-run           # flag matches, take no action
  python watchdog.py --daemon            # (v2) continuous loop mode
  python watchdog.py --retry-now         # (v2) force flush retry queue
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
                   help="[Not implemented in v1] Continuous loop mode")
    p.add_argument("--retry-now", action="store_true",
                   help="[Not implemented in v1] Force flush retry queue now")
    return p.parse_args()


def main():
    args = parse_args()

    if args.daemon:
        print("ERROR: --daemon is not implemented in v1.")
        print("       Use a cron job or systemd timer to schedule repeated runs.")
        sys.exit(1)

    if args.retry_now:
        print("ERROR: --retry-now is not implemented in v1.")
        sys.exit(1)

    from core.config import load_config
    from core.scanner import Scanner

    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    try:
        config = load_config(args.config)
    except (ValueError, KeyError, Exception) as exc:
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
