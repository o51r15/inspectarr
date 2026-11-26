"""core/config_store: a save changes what it was asked to change, and nothing else."""
import os

import pytest

import yaml

from core.config_store import (
    dump_text, load_document, merge_into, parse_text, save_document, to_plain,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PYYAML_STYLE = """\
qbittorrent:
  url: http://qbit:8080
  username: admin
rules:
- name: TV
  conditions:
    bad_extensions:
    - .exe
    - .zip
retry:
  enabled: true
  max_attempts: 10
"""

COMMENTED = """\
# my homelab config
qbittorrent:
  url: http://qbit:8080   # the seedbox
  username: admin
retry:
  enabled: true           # keep this on
  max_attempts: 10
my_note: kept by hand
"""


def test_pyyaml_layout_round_trips_unchanged():
    assert dump_text(parse_text(PYYAML_STYLE)) == PYYAML_STYLE


def test_example_config_round_trips_to_same_values():
    path = os.path.join(REPO, "config.example.yaml")
    text = open(path, encoding="utf-8").read()
    out = dump_text(parse_text(text))
    assert yaml.safe_load(out) == yaml.safe_load(text)
    in_lines, out_lines = text.splitlines(), out.splitlines()
    assert len(in_lines) == len(out_lines)      # layout kept, not re-indented
    changed = [(a, b) for a, b in zip(in_lines, out_lines) if a != b]
    # The only accepted difference: a null spelled `~` or left empty comes
    # back spelled `null`, the way PyYAML writes it.
    def as_null(line):
        key = line.split("#")[0].rstrip()
        key = key[:-2].rstrip() if key.endswith(" ~") else key
        return key + " null"
    assert all(b == as_null(a) for a, b in changed), changed


def test_long_unbreakable_value_stays_on_its_key_line():
    """ruamel would move a long hash under its key; PyYAML never did."""
    text = "web:\n  auth:\n    password: scrypt:32768:8:1$" + "a" * 150 + "\n"
    assert dump_text(parse_text(text)) == text


def test_long_prose_value_keeps_pyyaml_wrapping():
    words = " ".join(["word"] * 40)
    dumped = yaml.safe_dump({"prompt": words}, sort_keys=False)
    assert "\n  word" in dumped                 # PyYAML wrapped it
    assert dump_text(parse_text(dumped)) == dumped


def test_merge_keeps_comments_and_unknown_keys():
    doc = parse_text(COMMENTED)
    merge_into(doc.data, {"retry": {"max_attempts": 5}, "qbittorrent": {"url": "http://qbit:8080"}})
    out = dump_text(doc)
    assert "# my homelab config" in out
    assert "# the seedbox" in out                 # unchanged value keeps its comment
    assert "# keep this on" in out
    assert "my_note: kept by hand" in out
    assert yaml.safe_load(out)["retry"]["max_attempts"] == 5


def test_unchanged_merge_is_byte_identical():
    doc = parse_text(COMMENTED)
    merge_into(doc.data, to_plain(doc.data))
    assert dump_text(doc) == COMMENTED


def test_bool_replacing_int_is_written():
    doc = parse_text("retry:\n  enabled: 1\n")
    merge_into(doc.data, {"retry": {"enabled": True}})
    assert yaml.safe_load(dump_text(doc))["retry"]["enabled"] is True


def test_new_nested_block_is_added():
    doc = parse_text("a: 1\n")
    merge_into(doc.data, {"scanning": {"webhooks": {"enabled": False}}})
    assert yaml.safe_load(dump_text(doc)) == {"a": 1, "scanning": {"webhooks": {"enabled": False}}}


def test_changed_list_is_replaced_whole():
    doc = parse_text(PYYAML_STYLE)
    merge_into(doc.data, {"rules": [{"name": "Movies"}]})
    assert yaml.safe_load(dump_text(doc))["rules"] == [{"name": "Movies"}]


def test_empty_file_loads_as_empty_mapping(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("", encoding="utf-8")
    doc = load_document(str(p))
    assert dict(doc.data) == {}


def test_save_writes_file_and_survives_reload(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(COMMENTED, encoding="utf-8")
    doc = load_document(str(p))
    merge_into(doc.data, {"retry": {"max_attempts": 3}})
    save_document(str(p), doc)
    text = p.read_text(encoding="utf-8")
    assert "# keep this on" in text and "max_attempts: 3" in text
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def _random_config(rng):
    # Quotes, '#', brackets, backslashes, tabs, newlines and non-ASCII are
    # what push PyYAML into quoting and escaping a value -- the cases where
    # the two libraries' writers used to disagree. Keys stay ordinary
    # identifiers, as in every real config.
    alphabet = "abcxyz09 :/$.-_'\"#{}[],&*!|>%@?\\\téü✓\n"

    def word():
        n = rng.choice([1, 3, 6, 12, 40, 90, 130])
        w = "".join(rng.choice(alphabet) for _ in range(n))
        return w if rng.random() < 0.8 else f" {w} "

    def value(depth):
        kind = rng.choice(["str", "prose", "int", "float", "bool", "null", "list", "map"]
                          [: 8 if depth < 3 else 6])
        if kind == "null":
            return None
        if kind == "str":
            return word()
        if kind == "prose":
            return " ".join(word() for _ in range(rng.randint(2, 30)))
        if kind == "int":
            return rng.randint(-5, 100000)
        if kind == "float":
            return round(rng.uniform(0, 100), 3)
        if kind == "bool":
            return rng.random() < 0.5
        if kind == "list":
            return [value(depth + 1) for _ in range(rng.randint(1, 4))]
        return {f"k{i}": value(depth + 1) for i in range(rng.randint(1, 4))}

    return {f"section{i}": value(0) for i in range(rng.randint(1, 6))}


def test_anything_pyyaml_wrote_round_trips_byte_for_byte():
    """Every existing install's config.yaml was written by PyYAML's dump."""
    import random
    rng = random.Random(20261125)
    for _ in range(500):
        text = yaml.dump(_random_config(rng), default_flow_style=False,
                         allow_unicode=True, sort_keys=False)
        doc = parse_text(text)
        assert to_plain(doc.data) == yaml.safe_load(text), text   # reads as PyYAML does
        assert dump_text(doc) == text, text                       # writes it back unchanged


TRICKY_SCALARS = [
    "yes", "no", "on", "off", "y", "n", "Y", "N", "true", "False", "~", "null", "''",
    "017", "0o17", "0x1F", "1_000", "09_", "190:20:30", "1e3", "1.5e3", ".inf", "-.inf",
    ".nan", "2026-11-25", "0", "-0", "+12", "'yes'", '"on"', "a: b".replace(": ", ""),
]


@pytest.mark.parametrize("scalar", TRICKY_SCALARS)
def test_tricky_scalars_mean_what_pyyaml_says(scalar):
    """The store reads every bare value exactly as core/config.py (PyYAML)
    does, and never changes what one means when writing it back.

    Spelling may change for hand-typed variants PyYAML never writes (`yes`
    -> `true`, `~` -> `null`, `+12` -> `12`). Anything PyYAML itself would
    write -- i.e. every file the app has saved -- must come back exactly.
    """
    text = f"key: {scalar}\nlist:\n- {scalar}\n"
    doc = parse_text(text)
    out = dump_text(doc)
    expected = yaml.safe_load(text)
    if expected["key"] != expected["key"]:      # NaN never equals itself
        assert out == text
        return
    plain = to_plain(doc.data)
    assert plain == expected
    assert type(plain["key"]) is type(expected["key"])
    reread = yaml.safe_load(out)
    assert reread == expected and type(reread["key"]) is type(expected["key"])
    if yaml.dump(expected, default_flow_style=False, sort_keys=False) == text:
        assert out == text


def test_password_migration_changes_only_the_password(tmp_path):
    """H-01 auto-migration used to rewrite the whole file with PyYAML."""
    from werkzeug.security import check_password_hash
    from ui.auth import _migrate_password

    p = tmp_path / "config.yaml"
    p.write_text(
        "# hand-written notes\n"
        "web:\n"
        "  auth:\n"
        "    enabled: true\n"
        "    username: admin   # me\n"
        "    password: hunter2\n"
        "my_note: kept\n", encoding="utf-8")
    _migrate_password(str(p), "hunter2")
    text = p.read_text(encoding="utf-8")
    assert "# hand-written notes" in text and "# me" in text and "my_note: kept" in text
    stored = yaml.safe_load(text)["web"]["auth"]["password"]
    assert stored != "hunter2" and check_password_hash(stored, "hunter2")
