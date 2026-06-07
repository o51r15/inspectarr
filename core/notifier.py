import requests
from typing import Optional

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


class Notifier:
    """
    Dispatches Pushover notifications.
    Notification failures are swallowed — they never crash the main flow.
    """

    def __init__(self, config):
        self.cfg = config.notifications.pushover

    def _should(self, event_type: str) -> bool:
        return self.cfg.enabled and event_type in self.cfg.notify_on

    def _send(self, title: str, message: str, priority: Optional[int] = None):
        if not self.cfg.enabled:
            return
        p = priority if priority is not None else self.cfg.priority
        payload = {
            "token": self.cfg.app_token,
            "user":  self.cfg.user_key,
            "title": title,
            "message": message,
            "priority": p,
        }
        if p == 2:
            # Emergency priority requires retry + expire
            payload["retry"]  = 60
            payload["expire"] = 3600
        try:
            requests.post(PUSHOVER_API, data=payload, timeout=10)
        except Exception:
            pass

    def notify_startup(self, rules_count: int, dry_run: bool):
        if not self._should("startup"):
            return
        tag = " [DRY RUN]" if dry_run else ""
        self._send(
            f"inspectarr started{tag}",
            f"{rules_count} rule(s) loaded and active.",
        )

    def notify_dry_run(self, torrent_name: str, bad_files: list[str]):
        if not self._should("dry_run"):
            return
        files = ", ".join(bad_files[:3])
        if len(bad_files) > 3:
            files += f" (+{len(bad_files) - 3} more)"
        self._send("[DRY RUN] Would remove", f"{torrent_name}\nBad files: {files}")


    def notify_action(
        self, torrent_name: str, bad_files: list[str],
        arr_blocklisted: bool, qbit_deleted: bool
    ):
        if not self._should("action"):
            return
        arr_status  = "blocklisted" if arr_blocklisted else "arr FAILED"
        qbit_status = "deleted"     if qbit_deleted    else "qbit FAILED"
        files = ", ".join(bad_files[:3])
        if len(bad_files) > 3:
            files += f" (+{len(bad_files) - 3} more)"
        self._send(
            "inspectarr: Torrent removed",
            f"{torrent_name}\nFiles: {files}\n"
            f"Sonarr: {arr_status}  |  qBit: {qbit_status}",
        )

    def notify_error(self, context: str, reason: str):
        if not self._should("error"):
            return
        # Errors are sent at least at normal priority regardless of config
        p = max(self.cfg.priority, 0)
        self._send("inspectarr: Error", f"{context}\n{reason}", priority=p)

    def notify_retry_exhausted(self, torrent_name: str, hash: str, attempts: int):
        if not self._should("error"):
            return
        p = max(self.cfg.priority, 0)
        self._send(
            "inspectarr: Retry exhausted",
            f"{torrent_name}\nHash: {hash[:12]}...\nFailed after {attempts} attempts.",
            priority=p,
        )
