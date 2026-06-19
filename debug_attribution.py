#!/usr/bin/env python3
"""
debug_attribution.py — Standalone grab-attribution diagnostic.

Runs a single torrent hash through the full attribution chain and prints each
step's result, without writing to the database or deleting anything. Use this
to confirm the attribution pipeline works end to end after a config or code
change, instead of staging a full bad-torrent scan.

Usage:
    python3 debug_attribution.py <infohash> [--app radarr|sonarr|lidarr]
    python3 debug_attribution.py <infohash>           # tries all enabled arrs

Examples:
    python3 debug_attribution.py 29beae3eed4f7614c0ab777e441eac4167e62120 --app radarr
"""
import sys
import argparse

from core.config import load_config
from core.scanner import _build_arr_client, _normalize_indexer_name
from core.prowlarr import ProwlarrClient


def hr(title: str):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def diagnose(infohash: str, app: str, config) -> bool:
    hr(f"STEP 1 — Query {app} grab history for hash")
    print(f"hash: {infohash}")
    try:
        arr = _build_arr_client(app, config)
    except Exception as exc:
        print(f"  ✗ Could not build {app} client: {exc}")
        return False

    try:
        records = arr.get_history_records_by_hash(infohash)
    except Exception as exc:
        print(f"  ✗ History query failed: {exc}")
        print(f"    Check that the {app} URL in config includes its base path")
        print(f"    (e.g. http://host:7878/radarr).")
        return False

    print(f"  → {len(records)} history record(s) returned")
    for r in records:
        et  = r.get("eventType", "?")
        idx = r.get("data", {}).get("indexer")
        print(f"      eventType={et:<22} indexer={idx!r}")

    if not records:
        print("  ✗ No history records. Either the URL base path is wrong, or")
        print(f"    {app} never grabbed this torrent (manual qBit/Prowlarr add).")
        return False

    hr("STEP 2 — Extract indexer name via get_grab_indexer()")
    indexer_name = arr.get_grab_indexer(infohash)
    print(f"  → indexer_name = {indexer_name!r}")
    if not indexer_name:
        print("  ✗ No record carried a data.indexer field. Can't attribute.")
        return False
    print(f"  → normalized   = {_normalize_indexer_name(indexer_name)!r}")

    hr("STEP 3 — Fetch Prowlarr torrent indexer list")
    try:
        prowlarr = ProwlarrClient(config.prowlarr.url, config.prowlarr.api_key)
        indexers = prowlarr.get_torrent_indexers()
    except Exception as exc:
        print(f"  ✗ Prowlarr query failed: {exc}")
        return False

    print(f"  → {len(indexers)} torrent indexer(s) in Prowlarr:")
    for i in indexers:
        print(f"      id={i['id']:<4} name={i['name']!r:<32} "
              f"normalized={_normalize_indexer_name(i['name'])!r}")

    hr("STEP 4 — Match (with normalization)")
    target = _normalize_indexer_name(indexer_name)
    match = next(
        (i for i in indexers if _normalize_indexer_name(i["name"]) == target),
        None,
    )
    # Also show what the OLD exact-match logic would have done
    old_match = next(
        (i for i in indexers if i["name"].lower() == indexer_name.lower()),
        None,
    )
    print(f"  old exact-match result:  {old_match['name'] if old_match else 'NO MATCH'}")
    print(f"  new normalized result:   {match['name'] if match else 'NO MATCH'}")

    if match:
        print(f"\n  ✓ SUCCESS — would attribute to indexer "
              f"id={match['id']} '{match['name']}'")
        return True
    else:
        print(f"\n  ✗ STILL NO MATCH — '{indexer_name}' (normalized '{target}')")
        print("    does not correspond to any Prowlarr indexer above.")
        print("    The indexer may have been renamed or removed in Prowlarr.")
        return False


def main():
    p = argparse.ArgumentParser(description="Grab-attribution diagnostic")
    p.add_argument("infohash", help="Torrent infohash to trace")
    p.add_argument("--app", choices=["radarr", "sonarr", "lidarr"],
                   help="Which arr to query (default: all enabled)")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()

    config = load_config(args.config)

    if not config.prowlarr.enabled:
        print("Prowlarr is not enabled in config — attribution is inactive.")
        sys.exit(1)

    if args.app:
        apps = [args.app]
    else:
        apps = []
        if config.arrs.sonarr.enabled: apps.append("sonarr")
        if config.arrs.radarr.enabled: apps.append("radarr")
        if getattr(config.arrs, "lidarr", None) and config.arrs.lidarr.enabled:
            apps.append("lidarr")

    print(f"Tracing hash {args.infohash} through: {', '.join(apps)}")
    any_success = False
    for app in apps:
        if diagnose(args.infohash, app, config):
            any_success = True
            break  # found it in this arr, no need to check others

    hr("RESULT")
    print("✓ Attribution chain works for this hash."
          if any_success else
          "✗ Attribution failed — see the failing step above.")
    sys.exit(0 if any_success else 1)


if __name__ == "__main__":
    main()
