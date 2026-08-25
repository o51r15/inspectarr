#!/usr/bin/env python3
"""
inspectarr — torrent watchdog for *arr ecosystems

Polls qBittorrent categories, evaluates downloads against configurable rules,
blocklists bad releases in Sonarr (and future *arrs), deletes torrents + files,
logs all actions to JSON Lines, and notifies via Apprise (Pushover, Telegram, Discord, and 100+ services).

Usage:
  python inspectarr.py                     # single scan run (default)
  python inspectarr.py --config /path      # alternate config file
  python inspectarr.py --dry-run           # flag matches, take no action
  python inspectarr.py --test              # test every configured connection and exit

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
                   help="Run as a headless daemon — continuous scan loop without the web UI")
    p.add_argument("--retry-now", action="store_true",
                   help="Force flush retry queue — bypasses timing and exhaustion cap, then scans")
    p.add_argument("--test",      action="store_true",
                   help="Test every configured connection and exit "
                        "(exit 1 if any configured service is unreachable)")
    return p.parse_args()


def _run_connection_test(args) -> int:
    """
    Test every configured connection and report. Returns a process exit code.

    Uses the same checks as System -> Status (core/connections.py), so the
    two can never disagree about whether something is reachable -- which is
    the entire point of a CLI test: to answer that question the same way the
    UI would, without needing the UI.

    Exit code is the useful part. 0 only if every CONFIGURED service
    answered; 1 if any did not. Services that are switched off are reported
    as skipped and do not affect it -- a disabled Lidarr is not a failure,
    and treating it as one would make the flag useless in a script.
    """
    from core.config import load_config
    from core.connections import check_all

    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        return 2

    try:
        config = load_config(args.config)
    except Exception as exc:
        # A config that will not load is a different failure from a service
        # that will not answer, and deserves its own exit code.
        print(f"ERROR: Failed to load config: {exc}")
        return 2

    print(f"Testing connections from {args.config}\n")

    results = check_all(config)
    width = max(len(r["name"]) for r in results)
    failures = 0

    for r in results:
        if not r.get("configured"):
            status = "skipped (not configured)"
        elif r.get("ok"):
            status = "OK"
        else:
            status = "FAILED"
            failures += 1
        print(f"  {r['name']:<{width}}  {status}")

    configured = sum(1 for r in results if r.get("configured"))
    print()
    if failures:
        print(f"{failures} of {configured} configured service(s) unreachable.")
        return 1
    if not configured:
        # Nothing to test is not success. A fresh install reaching this
        # would otherwise print a reassuring green message about having
        # verified nothing at all.
        print("No services are configured — nothing was tested.")
        return 1
    print(f"All {configured} configured service(s) reachable.")
    return 0


def _run_periodic_sweeps(scanner) -> None:
    """
    The between-scan housekeeping both entry points owe.

    Kept as one function so the daemon and the one-shot path cannot drift
    apart -- they already had, which is how the one-shot path ended up never
    advancing a replacement watch.
    """
    scanner.process_quarantine_timeouts()
    scanner.process_replacement_watches()


def _run_daemon(args):
    """Headless scan loop — no web UI, just scan → sleep → repeat."""
    import signal
    import time
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

    interval = config.scanning.polling.interval_seconds
    print(f"inspectarr daemon starting — scanning every {interval}s")
    print("Press Ctrl+C to stop")

    _running = True

    def _shutdown(sig, frame):
        nonlocal _running
        print("\nShutdown signal received — finishing current cycle…")
        _running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scanner = Scanner(config)
    scanner.startup()

    while _running:
        try:
            if config.retry.enabled:
                scanner.process_retries()
            _run_periodic_sweeps(scanner)
            scanner.run_scan()
        except Exception as exc:
            print(f"ERROR during scan: {exc}")

        # Sleep in 1s ticks so we can respond to shutdown quickly
        for _ in range(interval):
            if not _running:
                break
            time.sleep(1)

    print("inspectarr daemon stopped")


def main():
    args = parse_args()

    # Checked first: --test must be safe to run at any time, so it must not
    # open the database, start a scheduler or touch a torrent.
    if args.test:
        sys.exit(_run_connection_test(args))

    if args.daemon:
        _run_daemon(args)
        return  # unreachable; _run_daemon loops forever

    # IMP-5: single config-load path for both --retry-now and normal runs
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

    if args.retry_now:
        scanner.prepare()
        print("Force-flushing retry queue (bypassing timing and exhaustion cap)...")
        scanner.process_retries(force=True)
        print("Done. Running scan...")
        _run_periodic_sweeps(scanner)
        scanner.run_scan()
        return

    scanner.startup()

    if config.retry.enabled:
        scanner.process_retries()

    # The daemon loop runs these every cycle; a one-shot run must too. This
    # IS the documented cron deployment, and without them a cron install
    # would open replacement watches it never advanced and never fire a
    # quarantine timeout.
    _run_periodic_sweeps(scanner)

    scanner.run_scan()


if __name__ == "__main__":
    main()
