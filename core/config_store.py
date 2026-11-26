"""
Read and write config.yaml without losing anything the user put there.

Every in-app write of config.yaml goes through here: the Settings form, the
raw YAML editor, the auth password migration, and (later) the setup wizard.

Three properties, each one a past bug:

  * Merge, never rebuild. Only keys the caller passes are touched, so a key
    the UI does not know about survives a save (S2, BUG-10/11, B-14).
  * Comments, key order and quoting survive, because ruamel's round-trip
    mode keeps them attached to the keys they sit beside.
  * Layout is detected per file, not imposed. A file PyYAML wrote (lists
    flush-left under their key) and the hand-indented example config both
    come back in their own style, so a save produces a small diff.

Writes are atomic where the directory allows it, with a direct-overwrite
fallback for Docker setups that bind-mount config.yaml into a read-only /app.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
from dataclasses import dataclass

import yaml.resolver as pyyaml_resolver
from yaml.emitter import Emitter as _PyYAMLEmitter
from ruamel.yaml import YAML
from ruamel.yaml.emitter import Emitter
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.resolver import VersionedResolver
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarbool import ScalarBoolean

# PyYAML's own layout, used for a file that has no lists to learn from.
_DEFAULT_LAYOUT = (2, 2, 0)  # mapping indent, sequence indent, dash offset


@dataclass
class Document:
    """A parsed config.yaml plus the layout to write it back in."""
    data: CommentedMap
    layout: tuple = _DEFAULT_LAYOUT


class _PyYAMLCompatibleEmitter(Emitter):
    """ruamel's emitter, writing scalar VALUES exactly as PyYAML does.

    Every config.yaml the app has written so far came from PyYAML, and
    ruamel lays out long values differently:

      * plain values: ruamel breaks BEFORE a word that would overflow (and
        moves an over-long token such as a password hash onto its own line
        under the key); PyYAML breaks only after passing the width;
      * double-quoted values: ruamel can split an escape like ``\\`` from
        its line-continuation backslash, which changes the value -- the
        reader then sees an extra space. Found by fuzzing; regex filename
        patterns and AI prompts are exactly the values that get quoted.

    So the three scalar writers are PyYAML's own methods, borrowed rather
    than re-typed. They use only emitter state both libraries share
    (column, best_width, stream, encoding, allow_unicode, write_indent,
    write_indicator, write_line_break). Structure, comments, key order and
    quoting style are still ruamel's, which is the point of using it.
    """

    ESCAPE_REPLACEMENTS = _PyYAMLEmitter.ESCAPE_REPLACEMENTS
    write_single_quoted = _PyYAMLEmitter.write_single_quoted
    write_double_quoted = _PyYAMLEmitter.write_double_quoted

    def write_plain(self, text, split=True):
        if self.root_context:
            # A bare top-level scalar document; never the case for a config.
            return super().write_plain(text, split)
        return _PyYAMLEmitter.write_plain(self, text, split)


# PyYAML's table of "plain text that means something else": which bare
# words are booleans, nulls, ints, floats, timestamps. Keyed by first char.
_PYYAML_IMPLICIT = {
    first: list(pairs)
    for first, pairs in pyyaml_resolver.Resolver.yaml_implicit_resolvers.items()
}


class _PyYAMLResolver(VersionedResolver):
    """Decide what a bare value means exactly as PyYAML does.

    The app reads config.yaml with PyYAML. ruamel's own YAML 1.1 table
    follows the spec more closely (it reads a bare `y` or `n` as a boolean;
    PyYAML reads them as strings), and its 1.2 table differs further. If the
    store and the app disagreed, an unchanged `y` would be rewritten as
    `true`. Using PyYAML's table removes the question.
    """

    @property
    def versioned_resolver(self):
        return _PYYAML_IMPLICIT


class _Representer(RoundTripRepresenter):
    """Write None as `null`, which is how PyYAML (and so the app) always has.

    A subclass, not add_representer on RoundTripRepresenter itself: that
    would change how every ruamel user in the process writes None.
    """


_Representer.add_representer(
    type(None), lambda rep, _: rep.represent_scalar("tag:yaml.org,2002:null", "null"))


def _yaml(layout: tuple = _DEFAULT_LAYOUT) -> YAML:
    mapping, sequence, offset = layout
    y = YAML(typ="rt")              # round-trip: keeps comments, order, quotes
    y.Resolver = _PyYAMLResolver
    y.Representer = _Representer
    y.Emitter = _PyYAMLCompatibleEmitter
    y.preserve_quotes = True
    # PyYAML's own wrap width. Every config the app has ever saved was
    # written by PyYAML, so matching it keeps a long value (a custom AI
    # system prompt, say) wrapped where it already is instead of producing
    # a large diff on the first save.
    y.width = 80
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    # YAML 1.1, the rules PyYAML (and therefore core/config.py) uses. Under
    # ruamel's default 1.2, `on`/`no`/`09_` and friends resolve differently,
    # so a value could mean one thing to the app and another to the store --
    # and be re-written in its 1.2 form. ruamel announces 1.1 with a
    # "%YAML 1.1" header on output; dump_text strips it.
    y.version = (1, 1)
    return y


_VERSION_HEADER = "%YAML 1.1\n---\n"

# A list item directly under a mapping key: "key:" then "<spaces>- ".
_KEY_LINE = re.compile(r"^( *)[^\s#-][^#]*:\s*(#.*)?$")
_DASH_LINE = re.compile(r"^( *)- ")


def _detect_layout(text: str) -> tuple:
    """How this file indents a list under its key.

    PyYAML writes the dash flush with the key ("rules:\\n- name"), the
    example config indents it ("rules:\\n  - name"). The first list that sits
    directly under a key decides; nested lists-in-lists are ignored because
    they say nothing about the file's style. No such list: PyYAML's style.
    """
    previous = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        dash = _DASH_LINE.match(line)
        if dash and previous is not None:
            offset = len(dash.group(1)) - previous
            if offset >= 0:
                return (2, offset + 2, offset)
        key = _KEY_LINE.match(line)
        previous = len(key.group(1)) if key and line.rstrip().endswith(":") else None
    return _DEFAULT_LAYOUT


def parse_text(text: str) -> Document:
    """Parse YAML text. Raises ruamel.yaml.YAMLError on bad syntax."""
    layout = _detect_layout(text)
    data = _yaml(layout).load(text)
    return Document(data if data is not None else CommentedMap(), layout)


def load_document(path: str) -> Document:
    with open(path, encoding="utf-8") as f:
        return parse_text(f.read())


def dump_text(doc: Document) -> str:
    buf = io.StringIO()
    _yaml(doc.layout).dump(doc.data, buf)
    text = buf.getvalue()
    if text.startswith(_VERSION_HEADER):
        text = text[len(_VERSION_HEADER):]
    return text


def merge_into(node, updates: dict) -> None:
    """Apply `updates` onto `node` in place, touching nothing else.

    Nested dicts merge key by key. Any other value (scalar or list) replaces
    the stored one only if it differs, so an unchanged value keeps its
    trailing comment. A list that did change is replaced whole, which drops
    comments inside that list; that is the accepted cost.
    """
    for key, value in updates.items():
        current = node.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merge_into(current, value)
        elif key not in node or _differs(current, value):
            node[key] = value


def _differs(current, value) -> bool:
    """True when writing `value` would change what the file means.

    Compared as plain values (ruamel wraps floats as ScalarFloat). Bools are
    checked separately because Python treats True == 1 and False == 0, and
    `enabled: 1` -> `enabled: true` is a change worth writing.
    """
    a, b = to_plain(current), to_plain(value)
    if _has_bool_mismatch(a, b):
        return True
    return a != b


def _has_bool_mismatch(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and any(_has_bool_mismatch(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return any(_has_bool_mismatch(a[k], b[k]) for k in a.keys() & b.keys())
    return isinstance(a, bool) != isinstance(b, bool)


def to_plain(node):
    """CommentedMap/Seq -> dict/list with plain Python scalars, for validation."""
    if isinstance(node, dict):
        return {str(k): to_plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple, CommentedSeq)):
        return [to_plain(v) for v in node]
    if isinstance(node, (bool, ScalarBoolean)):   # ScalarBoolean subclasses int
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


def save_document(path: str, doc: Document) -> None:
    write_text_atomic(path, dump_text(doc))


def write_text_atomic(path: str, content: str) -> None:
    """Write `content` to `path` as safely as the filesystem allows.

    L-17: temp file in the same directory, fsync, then os.replace, so a crash
    mid-write never leaves a half-written config. If the directory is not
    writable (config.yaml bind-mounted into a read-only /app) fall back to
    overwriting the file in place.
    """
    dir_name = os.path.dirname(os.path.abspath(path))
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except PermissionError:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows / filesystems without POSIX modes

    # ROADMAP item 14: an in-app save must be visible on the very next read
    # even if two writes land inside one filesystem timestamp tick.
    try:
        from core.config import invalidate_config_cache
        invalidate_config_cache(path)
    except Exception:
        pass  # a cache miss is harmless; failing a save over it is not
