"""
ui/nav.py — the navigation, as data.

One list drives the sidebar, the active highlight, the per-section tab bar
and the link-check tests. A page never decides on its own whether a nav item
is active, which is how links that never highlighted crept in before.

Keys:
  match   URL prefixes that belong to the item. "/" is special-cased: it only
          matches the root, otherwise every page would belong to Overview.
  subnav  optional tab bar shown under the topbar on every page of the
          section. Each tab has its own match prefixes for the same reason.
"""

NAV = [
    {"key": "overview", "label": "Overview", "href": "/", "icon": "overview",
     "match": ("/",)},
    {"key": "activity", "label": "Activity", "href": "/activity", "icon": "activity",
     "match": ("/activity", "/torrents", "/quarantine"),
     "subnav": [
         {"key": "review", "label": "Needs review", "href": "/activity/review",
          "match": ("/activity/review", "/quarantine"), "badge": "held"},
         {"key": "history", "label": "History", "href": "/activity/history",
          "match": ("/activity/history",)},
         {"key": "torrents", "label": "Torrents", "href": "/torrents",
          "match": ("/torrents",)},
     ]},
    {"key": "indexers", "label": "Indexers", "href": "/indexers", "icon": "indexers",
     "match": ("/indexers", "/stats")},
    {"key": "settings", "label": "Settings", "href": "/config", "icon": "settings",
     "match": ("/config",)},
    {"key": "system", "label": "System", "href": "/system/status", "icon": "system",
     "match": ("/system", "/logs", "/scheduler"),
     "subnav": [
         {"key": "status", "label": "Status", "href": "/system/status",
          "match": ("/system/status",)},
         {"key": "events", "label": "Events", "href": "/system/events",
          "match": ("/system/events", "/logs")},
         {"key": "notifications", "label": "Notifications", "href": "/system/notifications",
          "match": ("/system/notifications",)},
         {"key": "tasks", "label": "Tasks", "href": "/system/tasks",
          "match": ("/system/tasks", "/scheduler")},
         {"key": "updates", "label": "Updates", "href": "/system/updates",
          "match": ("/system/updates",)},
     ]},
]


def _matches(path: str, prefixes) -> bool:
    for p in prefixes:
        if p == "/":
            if path == "/":
                return True
        elif path == p or path.startswith(p.rstrip("/") + "/"):
            return True
    return False


def active_item(path: str) -> dict | None:
    """The top-level nav item this path belongs to, or None."""
    for item in NAV:
        if _matches(path, item["match"]):
            return item
    return None


def active_key(path: str) -> str:
    item = active_item(path)
    return item["key"] if item else ""


def active_subnav(path: str) -> tuple[list[dict], str]:
    """(tabs, active tab key) for the section, or ([], "") when it has none."""
    item = active_item(path)
    if not item or not item.get("subnav"):
        return [], ""
    for tab in item["subnav"]:
        if _matches(path, tab["match"]):
            return item["subnav"], tab["key"]
    return item["subnav"], ""
