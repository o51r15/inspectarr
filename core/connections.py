"""
core/connections.py -- live connection checks for every configured service.

Extracted from ui/routes/system.py so the CLI can reach them without
importing a Flask blueprint (ROADMAP item 13). The checks never contained any
Flask; they were simply written where they were first needed.

Two callers, one implementation: System -> Status renders these, and
`inspectarr.py --test` prints them.
"""
from concurrent.futures import ThreadPoolExecutor

# The services checked, in the order they are reported.
SERVICE_NAMES = ["Torrent Client", "Sonarr", "Radarr", "Lidarr",
                 "Prowlarr", "Ollama"]


def check_connection(name: str, cfg) -> dict:
    """Run a single connection test. Returns {name, configured, ok}."""
    try:
        if name == "Torrent Client":
            from core.torrent_client import build_torrent_client
            c = build_torrent_client(cfg)
            return {"name": f"{name} ({cfg.torrent_client})", "configured": True, "ok": c.test_connection()}
        if name == "Sonarr":
            if not cfg.arrs.sonarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.arrs.sonarr import SonarrClient
            c = SonarrClient(cfg.arrs.sonarr.url, cfg.arrs.sonarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Radarr":
            if not cfg.arrs.radarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.arrs.radarr import RadarrClient
            c = RadarrClient(cfg.arrs.radarr.url, cfg.arrs.radarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Lidarr":
            if not cfg.arrs.lidarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.arrs.lidarr import LidarrClient
            c = LidarrClient(cfg.arrs.lidarr.url, cfg.arrs.lidarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Prowlarr":
            if not cfg.prowlarr.enabled:
                return {"name": name, "configured": False, "ok": False}
            from core.prowlarr import ProwlarrClient
            c = ProwlarrClient(cfg.prowlarr.url, cfg.prowlarr.api_key)
            return {"name": name, "configured": True, "ok": c.test_connection()}
        if name == "Ollama":
            ocfg = cfg.prowlarr.ollama
            url, model = ocfg.url, ocfg.model
            if not ocfg.enabled:
                # Distinct from unconfigured: the user switched AI off.
                return {"name": f"{name} (disabled)", "configured": False,
                        "ok": False}
            if not url or not model:
                return {"name": name, "configured": False, "ok": False}
            import requests
            resp = requests.get(f"{url}/api/tags", timeout=10)
            return {"name": f"{name} ({model})", "configured": True, "ok": resp.status_code == 200}
    except Exception:
        return {"name": name, "configured": True, "ok": False}
    return {"name": name, "configured": False, "ok": False}


def check_all(cfg, names=None) -> list[dict]:
    """
    Every check, run concurrently.

    Concurrency keeps the total near a single timeout rather than the sum of
    six sequential ones when services are unreachable -- the difference
    between a status page that renders and one that appears to hang.
    """
    names = names or SERVICE_NAMES
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return list(pool.map(lambda n: check_connection(n, cfg), names))

