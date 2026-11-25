"""
Every page renders, auth gates them, and no internal link is dead.

Pages that fill themselves by fetch() after load only have their shell
tested here; their JSON endpoints get tests as they are touched.
"""
import re

import pytest

PAGES = [
    "/", "/scheduler", "/torrents", "/quarantine",
    "/indexers", "/indexers/stats", "/indexers/ai-scoring",
    "/config",
    "/system/status", "/system/tasks", "/system/notifications",
    "/system/updates", "/system/events",
    "/logs",
]

# Old URLs kept alive as permanent redirects (folded into Indexers in v1.6.0).
REDIRECTS = {
    "/stats": "/indexers/stats",
    "/system/llm-logs": "/indexers/ai-scoring",
}


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    assert b"Traceback" not in resp.data


@pytest.mark.parametrize("old,new", REDIRECTS.items())
def test_old_urls_redirect(client, old, new):
    resp = client.get(old)
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith(new)
    assert client.get(new).status_code == 200


def test_health_is_open(client):
    resp = client.get("/api/health")
    assert resp.status_code in (200, 503)


@pytest.mark.parametrize("path", PAGES)
def test_auth_required_when_enabled(app, auth_header, path):
    c = app.test_client()
    assert c.get(path).status_code == 401
    assert c.get(path, headers=auth_header).status_code == 200


def test_health_open_with_auth_on(app, auth_header):
    assert app.test_client().get("/api/health").status_code in (200, 503)


# Whole attribute value; template expressions (JS `${...}` or Jinja) are
# skipped below rather than truncated into a partial, wrong path.
_HREF = re.compile(r'href="(/[^"]*)"')


def test_every_internal_link_resolves(app, client):
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    adapter = app.url_map.bind("localhost")
    dead = []
    for page in PAGES:
        html = client.get(page).get_data(as_text=True)
        for href in set(_HREF.findall(html)):
            if href.startswith("/static/") or "${" in href or "{{" in href:
                continue
            path = href.split("#", 1)[0].split("?", 1)[0]
            try:
                adapter.match(path, method="GET")
            except NotFound:
                dead.append(f"{page} -> {href}")
            except MethodNotAllowed:
                dead.append(f"{page} -> {href} (not a GET route)")
    assert not dead, "dead links:\n" + "\n".join(sorted(dead))
