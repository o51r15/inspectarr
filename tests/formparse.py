"""
tests/formparse.py -- what a browser would submit for a given <form>.

Used by the round-trip test: render Settings, collect exactly the fields a
browser would send if the user pressed Save without touching anything, post
them back. Stdlib only.

Browser rules reproduced:
  * <input>: sent unless disabled; checkbox/radio only when checked;
    type=submit/button/reset/file/image are not sent.
  * <select>: the selected option's value, or the first option if none is
    marked selected (a browser's default). Option value falls back to text.
  * <textarea>: its text content.
Fields outside the form with the given id are ignored.
"""
from html.parser import HTMLParser

from werkzeug.datastructures import MultiDict

_SKIP_TYPES = {"submit", "button", "reset", "file", "image"}


class _FormParser(HTMLParser):
    def __init__(self, form_id: str):
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.depth = 0            # >0 while inside the target form
        self.fields: list[tuple[str, str]] = []
        self._select = None       # {"name", "options": [(value, selected)], "disabled"}
        self._option = None       # [value_or_None, text, selected]
        self._textarea = None     # [name, text, disabled]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            if self.depth:
                self.depth += 1
            elif a.get("id") == self.form_id:
                self.depth = 1
            return
        if not self.depth:
            return
        disabled = "disabled" in a
        if tag == "input":
            name = a.get("name")
            itype = (a.get("type") or "text").lower()
            if not name or disabled or itype in _SKIP_TYPES:
                return
            if itype in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((name, a.get("value", "on")))
                return
            self.fields.append((name, a.get("value", "")))
        elif tag == "select":
            self._select = {"name": a.get("name"), "options": [], "disabled": disabled}
        elif tag == "option" and self._select is not None:
            self._option = [a.get("value"), "", "selected" in a]
        elif tag == "textarea":
            self._textarea = [a.get("name"), "", disabled]

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data
        elif self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form" and self.depth:
            self.depth -= 1
            return
        if not self.depth:
            return
        if tag == "option" and self._option is not None and self._select is not None:
            value, text, selected = self._option
            self._select["options"].append(
                (value if value is not None else text.strip(), selected))
            self._option = None
        elif tag == "select" and self._select is not None:
            sel = self._select
            self._select = None
            if not sel["name"] or sel["disabled"] or not sel["options"]:
                return
            chosen = [v for v, s in sel["options"] if s]
            self.fields.append((sel["name"], chosen[-1] if chosen else sel["options"][0][0]))
        elif tag == "textarea" and self._textarea is not None:
            name, text, disabled = self._textarea
            self._textarea = None
            if name and not disabled:
                # A browser drops one leading newline in a textarea.
                self.fields.append((name, text[1:] if text.startswith("\n") else text))


def form_fields(html: str, form_id: str) -> MultiDict:
    parser = _FormParser(form_id)
    parser.feed(html)
    parser.close()
    if not parser.fields:
        raise AssertionError(f"form #{form_id} not found or has no fields")
    return MultiDict(parser.fields)
