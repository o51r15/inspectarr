"""
core/notify_targets.py -- the places Inspectarr sends messages to.

Adapted from DockLlama's notifications. A person picks a service and fills in
one or two plain fields; Inspectarr builds the Apprise address from them.
Each place has its own list of events, so removals can go to a phone while
the daily summary goes to email.

Stored in config.yaml under notifications.apprise.targets:

    targets:
      - id: 1
        service: discord
        fields: {webhook: https://discord.com/api/webhooks/...}
        events: [action, quarantine, error, startup, summary]
        enabled: true

The Apprise address is never stored: it is rebuilt from `fields` whenever it
is needed, so there is one copy of each secret, not two.

Before this, a config held a flat list (apprise.urls) and one event list for
all of them (apprise.notify_on). Such a config is read as one "Other (Apprise
URL)" place per address, each with those events (from_legacy); the web app
then rewrites the file once (migrate_config).
"""
from __future__ import annotations

import logging
import re
import secrets
from urllib.parse import quote, urlparse

log = logging.getLogger("inspectarr")

# --- what can be sent ----------------------------------------------------------
# (key, label). Keys are what config.yaml stores; "action", "quarantine",
# "error", "dry_run" and "startup" are the same keys notify_on always used.
EVENTS = (
    ("action", "A bad release was removed or blocked"),
    ("quarantine", "A release is waiting for my review"),
    ("error", "Something went wrong (a service is unreachable, a removal failed)"),
    ("dry_run", "A release would have been removed (while watching only)"),
    ("startup", "Inspectarr started, stopped, or stopped unexpectedly"),
    ("summary", "The daily or weekly summary"),
)
EVENT_KEYS = tuple(k for k, _ in EVENTS)
DEFAULT_EVENTS = ["action", "quarantine", "error", "dry_run", "startup", "summary"]
LEGACY_DEFAULT_EVENTS = ["action", "error"]      # old notify_on when absent

# --- services --------------------------------------------------------------------
# Field kinds: "secret" values are masked wherever the config is shown.
SERVICES = {
    "ntfy": {
        "label": "ntfy (recommended)",
        "blurb": "Free phone app, nothing to sign up for. Inspectarr makes a private topic for you.",
        "fields": [
            {"key": "topic", "label": "Topic",
             "hint": "Made up for you. Anyone who knows it can read the messages, so keep it hard to guess."},
            {"key": "server", "label": "Server", "placeholder": "https://ntfy.sh",
             "hint": "Leave as ntfy.sh unless you run your own.", "optional": True},
            {"key": "token", "label": "Access token (only for a protected topic)", "secret": True,
             "optional": True},
        ]},
    "discord": {
        "label": "Discord",
        "blurb": "Messages to a channel through a webhook.",
        "fields": [
            {"key": "webhook", "label": "Webhook URL", "secret": True,
             "hint": "Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL."},
        ]},
    "telegram": {
        "label": "Telegram",
        "blurb": "Messages from a bot you create with @BotFather.",
        "fields": [
            {"key": "bot_token", "label": "Bot token", "secret": True,
             "hint": "Message @BotFather, send /newbot, copy the token."},
            {"key": "chat_id", "label": "Your chat ID",
             "hint": "Message @userinfobot to see it. Then send your new bot any message once."},
        ]},
    "gotify": {
        "label": "Gotify",
        "blurb": "Your own Gotify server.",
        "fields": [
            {"key": "server", "label": "Gotify server", "placeholder": "https://gotify.example.com"},
            {"key": "app_token", "label": "App token", "secret": True,
             "hint": "In Gotify: Apps → Create application → copy its token."},
        ]},
    "pushover": {
        "label": "Pushover",
        "blurb": "Pushover app (paid once).",
        "fields": [
            {"key": "user_key", "label": "Your user key", "secret": True,
             "hint": "On your Pushover dashboard."},
            {"key": "app_token", "label": "App token", "secret": True,
             "hint": "Create an application on pushover.net for Inspectarr."},
        ]},
    "email": {
        "label": "Email",
        "blurb": "Gmail, Outlook, Yahoo and most others. Use an app password, not your normal one.",
        "fields": [
            {"key": "address", "label": "Your email address"},
            {"key": "password", "label": "App password", "secret": True, "type": "password",
             "hint": "Gmail: Google account → Security → App passwords. Outlook and Yahoo have the same."},
            {"key": "smtp", "label": "SMTP server (only if yours isn't found)",
             "placeholder": "smtp.example.com", "optional": True},
        ]},
    "apprise": {
        "label": "Other (Apprise URL)",
        "blurb": "Any of the 100+ services Apprise supports, if you know its URL format.",
        "fields": [
            {"key": "url", "label": "Apprise URL", "secret": True, "placeholder": "service://…",
             "hint": "Each service's page on the Apprise wiki shows the format."},
        ]},
}


class NotifyError(ValueError):
    """A problem with what the person typed, worded for them."""


def secret_keys(service: str) -> tuple[str, ...]:
    return tuple(f["key"] for f in SERVICES.get(service, {}).get("fields", []) if f.get("secret"))


def new_ntfy_topic() -> str:
    """Hard to guess: anyone who knows an ntfy.sh topic can read it."""
    return "inspectarr-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y").lower()


def _need(fields: dict, key: str, label: str) -> str:
    value = str(fields.get(key) or "").strip()
    if not value:
        raise NotifyError(f"Fill in {label}.")
    return value


def _host(url: str, label: str) -> tuple[str, str]:
    """('s' for https or '', host[:port][/path]) from what the person typed."""
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    p = urlparse(text)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise NotifyError(f"{label} should look like https://example.com")
    return ("s" if p.scheme == "https" else ""), (p.netloc + p.path.rstrip("/"))


def build_url(service: str, fields: dict) -> str:
    """The Apprise address for these fields. Raises NotifyError, worded for people."""
    fields = fields or {}
    if service == "ntfy":
        topic = _need(fields, "topic", "the topic")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise NotifyError("A topic can only use letters, numbers, - and _.")
        secure, host = _host(fields.get("server") or "https://ntfy.sh", "The ntfy server")
        url = f"ntfy{secure}://{host}/{topic}"
        if str(fields.get("token") or "").strip():
            url += "?token=" + quote(str(fields["token"]).strip(), safe="")
        return url
    if service == "discord":
        hook = _need(fields, "webhook", "the webhook URL")
        if not re.match(r"https://(\w+\.)?(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+", hook):
            raise NotifyError("That isn't a Discord webhook URL. In Discord: channel settings → "
                              "Integrations → Webhooks → Copy Webhook URL.")
        return hook
    if service == "telegram":
        token = _need(fields, "bot_token", "the bot token")
        chat = _need(fields, "chat_id", "the chat ID")
        if not re.fullmatch(r"\d+:[\w-]+", token):
            raise NotifyError("A bot token looks like 123456789:ABCdef… (from @BotFather).")
        if not re.fullmatch(r"-?\d+|@\w+", chat):
            raise NotifyError("The chat ID is a number (from @userinfobot), or @channelname.")
        return f"tgram://{token}/{chat}"
    if service == "gotify":
        secure, host = _host(_need(fields, "server", "the Gotify server"), "The Gotify server")
        return f"gotify{secure}://{host}/{_need(fields, 'app_token', 'the app token')}"
    if service == "pushover":
        return f"pover://{_need(fields, 'user_key', 'your user key')}@{_need(fields, 'app_token', 'the app token')}"
    if service == "email":
        address = _need(fields, "address", "your email address")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            raise NotifyError("That doesn't look like an email address.")
        password = _need(fields, "password", "the app password")
        user, domain = address.split("@", 1)
        url = f"mailtos://{quote(user, safe='')}:{quote(password, safe='')}@{domain}?to={quote(address, safe='@')}"
        if str(fields.get("smtp") or "").strip():
            url += "&smtp=" + quote(str(fields["smtp"]).strip(), safe="")
        return url
    if service == "apprise":
        url = _need(fields, "url", "the Apprise URL")
        if "://" not in url:
            raise NotifyError("An Apprise URL looks like service://… (see the Apprise wiki).")
        return url
    raise NotifyError("Pick one of the listed services.")


def valid_url(url: str) -> bool:
    """Does Apprise accept this address at all? (No message is sent.)"""
    import apprise
    return bool(apprise.Apprise().add(url))


def describe(service: str, fields: dict) -> str:
    """A safe one-line description: never a token, password or full webhook."""
    fields = fields or {}
    if service == "ntfy":
        server = str(fields.get("server") or "ntfy.sh").replace("https://", "").replace("http://", "")
        return f"ntfy topic {fields.get('topic')} on {server}"
    if service == "discord":
        return "Discord webhook …" + str(fields.get("webhook", ""))[-6:]
    if service == "telegram":
        return f"Telegram chat {fields.get('chat_id')}"
    if service == "gotify":
        return f"Gotify at {fields.get('server')}"
    if service == "pushover":
        return "Pushover user …" + str(fields.get("user_key", ""))[-4:]
    if service == "email":
        return f"Email to {fields.get('address')}"
    url = str(fields.get("url") or "")
    scheme = url.split("://", 1)[0] if "://" in url else ""
    return f"Apprise address ({scheme}://…)" if scheme else "Apprise address"


def subscribe_link(service: str, fields: dict) -> str | None:
    """For ntfy: the link the phone app (or a browser) opens to subscribe."""
    if service != "ntfy":
        return None
    try:
        secure, host = _host((fields or {}).get("server") or "https://ntfy.sh", "The ntfy server")
    except NotifyError:
        return None
    return f"http{secure}://{host}/{(fields or {}).get('topic', '')}"


def qr_svg(text: str) -> str:
    """A standalone SVG (works as an <img>). Dark on light scans most reliably."""
    import io

    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, dark="#000000", light="#ffffff",
                                     border=2, xmldecl=False)
    return buf.getvalue().decode()


# --- sending -----------------------------------------------------------------------
SERVICE_NAMES = {"ntfy": "ntfy", "ntfys": "ntfy", "discord": "Discord", "https": "Discord",
                 "tgram": "Telegram", "gotify": "Gotify", "gotifys": "Gotify", "pover": "Pushover",
                 "mailto": "the email server", "mailtos": "the email server"}
GENERIC_FAILURE = "The service didn't accept the message. Check the details and that it can be reached."


def explain_failure(captured_log: str, url: str) -> str:
    """Turn what Apprise logged about a failed send into plain words. Apprise
    itself only answers yes/no; the reason is in its log."""
    service = SERVICE_NAMES.get(url.split("://", 1)[0].lower(), "The service")
    host_m = re.search(r"host='([^']+)'", captured_log)
    host = host_m.group(1) if host_m else service
    low = captured_log.lower()
    if "socket exception" in low or "connection error" in low or "connectionerror" in low:
        if ("nameresolution" in low or "name or service not known" in low
                or "nodename nor servname" in low or "temporary failure in name resolution" in low
                or "getaddrinfo failed" in low):
            return f"Couldn't find {host}: the name doesn't resolve. Check the address, and that Inspectarr's machine has DNS."
        if "timed out" in low or "timeout" in low:
            return (f"No answer from {host} in time. If it works from your browser, the network between "
                    "Inspectarr and the internet may be the problem (for example a VPN).")
        if "refused" in low or "newconnectionerror" in low:
            return f"{host} refused the connection. Check the address and port, and that the service is running."
        if "ssl" in low or "certificate" in low:
            return f"The secure connection to {host} failed (a certificate problem)."
        return f"Couldn't connect to {host}."
    code_m = re.search(r"error=(\d{3})", captured_log)
    if code_m:
        code = int(code_m.group(1))
        name = "The email server" if service == "the email server" else service
        if code in (401, 403):
            return (f"{name} turned the message down (HTTP {code}): the token or password is probably "
                    "wrong, or the topic is protected.")
        if code == 404:
            return f"{name} answered \"not found\" (HTTP 404). Check the address, topic or webhook."
        if code == 429:
            return f"{name} is limiting how many messages it takes (HTTP 429). Try again in a little while."
        if code >= 500:
            return f"{name} had a problem on its side (HTTP {code}). Try again later."
        return f"{name} turned the message down (HTTP {code}). Check the details."
    return GENERIC_FAILURE


def send_now(url: str, title: str, body: str) -> tuple[bool, str]:
    """Blocking send through Apprise. Returns (ok, problem in plain words). Never raises."""
    import apprise
    a = apprise.Apprise()
    if not a.add(url):
        return False, "Inspectarr couldn't use those details. Check them and try again."
    try:
        with apprise.LogCapture(level=apprise.logging.DEBUG) as captured:
            ok = a.notify(title=title, body=body)
    except Exception as exc:            # any delivery failure is reported, never raised
        return False, f"Sending failed: {exc}"
    if ok:
        return True, ""
    return False, explain_failure(captured.getvalue(), url)


# --- reading config.yaml --------------------------------------------------------------

def _events(value, fallback) -> list[str]:
    if value is None:
        value = fallback
    if isinstance(value, str):
        value = [value]
    return [e for e in (value or []) if isinstance(e, str) and e in EVENT_KEYS]


def parse_targets(apprise_raw: dict, summary_enabled: bool = False) -> list[dict]:
    """The places to send to, from notifications.apprise in config.yaml.

    Returns plain dicts: id, service, fields, events, enabled, url (None when
    the fields are incomplete -- such a place is shown, but sent nothing).
    A config still in the old shape is read through from_legacy.
    """
    raw = apprise_raw or {}
    if "targets" not in raw:
        return from_legacy(raw, summary_enabled)
    out = []
    for i, t in enumerate(raw.get("targets") or []):
        if not isinstance(t, dict):
            continue
        service = str(t.get("service") or "apprise")
        fields = dict(t.get("fields") or {})
        try:
            url = build_url(service, fields)
        except NotifyError as exc:
            log.warning(f"Notifications: place {t.get('id', i + 1)} can't be used: {exc}")
            url = None
        out.append({"id": int(t.get("id") or i + 1), "service": service, "fields": fields,
                    "events": _events(t.get("events"), DEFAULT_EVENTS),
                    "enabled": bool(t.get("enabled", True)), "url": url})
    return out


def from_legacy(apprise_raw: dict, summary_enabled: bool = False) -> list[dict]:
    """Old config: urls + one notify_on for all of them + a master switch."""
    urls = apprise_raw.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    events = _events(apprise_raw.get("notify_on"), LEGACY_DEFAULT_EVENTS)
    if summary_enabled and "summary" not in events:
        events.append("summary")             # the summary used to go to every URL
    enabled = bool(apprise_raw.get("enabled", False))
    out = []
    for i, url in enumerate(u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()):
        out.append({"id": i + 1, "service": "apprise", "fields": {"url": url},
                    "events": list(events), "enabled": enabled, "url": url})
    return out


def wants(target: dict, event: str) -> bool:
    """Does this place want this event? A hold for review also goes to places
    that only asked for removals: configs from before "quarantine" existed
    would otherwise never hear about a release that is waiting for an answer."""
    if not target.get("enabled") or not target.get("url"):
        return False
    events = target.get("events") or []
    if event in events:
        return True
    if event == "quarantine" and "action" in events:
        return True
    if event == "retry_exhausted" and "error" in events:
        return True
    if event in ("shutdown", "unexpected_stop") and "startup" in events:
        return True
    return False


def convert_legacy_node(notif) -> int:
    """In a loaded config's `notifications` node (dict or ruamel map), turn an
    old-shape apprise section into targets, in place. Returns how many places
    it made. The old keys go, so the file has one way of saying it."""
    from .config_store import to_plain
    app = notif.get("apprise")
    summary = notif.get("summary") or {}
    targets = from_legacy(to_plain(app) or {}, bool(summary.get("enabled", False)))
    app["targets"] = [{"id": t["id"], "service": t["service"], "fields": t["fields"],
                       "events": t["events"], "enabled": t["enabled"]} for t in targets]
    for key in ("urls", "notify_on", "enabled"):
        if key in app:
            del app[key]
    if "enabled" in summary:
        del summary["enabled"]
    return len(targets)


def migrate_config(config_path: str) -> str | None:
    """Rewrite an old-shape notifications.apprise into targets, once. Never raises.

    The old urls/notify_on/enabled keys and summary.enabled are removed after
    the targets are written, so the file has one way of saying it.
    """
    try:
        from . import config_store
        doc = config_store.load_document(config_path)
        notif = (doc.data or {}).get("notifications")
        if not notif:
            return None
        app = notif.get("apprise")
        if app is None or "targets" in app:
            return None
        count = convert_legacy_node(notif)
        config_store.save_document(config_path, doc)
        outcome = f"moved {count} notification address(es) into places to send"
        log.info(f"Notifications: {outcome}")
        return outcome
    except Exception as exc:
        log.warning(f"Notifications: could not move the old settings: {exc}")
        return None
