#!/usr/bin/env python3
"""Generate the policy settings tables in ``docs/reference/configuration.md``.

The tables are rendered from the core settings schema
(``bmad_loop.settings_schema.load_core_schema()``, i.e.
``src/bmad_loop/data/settings/core.toml`` with its defaults and options resolved
from ``policy.py``), so the reference can never drift from the runtime model:
``tests/test_docs_hygiene.py`` runs ``--check`` and fails on a stale block.

Only the text between the BEGIN/END markers is owned by this script; everything
else in the page is hand-written. The block sits inside prettier-ignore markers
so ``trunk fmt`` never re-pads the tables out of sync with this output.

Rendering rules: one ``### `[section]` `` heading per schema section in schema
order, its description, then a ``Key | Type | Default | Allowed | Description``
table. Defaults are TOML-style; a field with no default renders as unset (with
its settings-screen placeholder when it has one), except a ``lines`` list, which
reads as ``[]``. ``Allowed`` is the select options or the numeric bounds. The
``[mux] backend`` options are registered at runtime, not in the schema, so that
row links to the multiplexer guide instead. Plugin settings are not rendered —
each plugin's guide owns them.

Usage::

    uv run python scripts/gen_policy_reference.py          # rewrite the block in place
    uv run python scripts/gen_policy_reference.py --check  # exit 1 if the block is stale (CI)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from bmad_loop.settings_schema import SectionSpec, SettingSpec, load_core_schema

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "docs" / "reference" / "configuration.md"

BEGIN = "<!-- BEGIN GENERATED: policy-reference — run scripts/gen_policy_reference.py -->"
END = "<!-- END GENERATED: policy-reference -->"

_TYPES = {
    "select": "string",
    "int": "integer",
    "float": "float",
    "str": "string",
    "switch": "boolean",
    "lines": "array of strings",
    "args": "array of strings",
}

# The one row whose options exist only at runtime (`settings_schema._inject_mux_backends`).
_MUX_BACKENDS = "a registered backend name — see [multiplexer backends](../multiplexer-backends.md)"
_RUNTIME_OPTIONS = {("mux", "backend"): _MUX_BACKENDS}

_CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def _escape(text: str) -> str:
    """Make free text safe inside a table cell: escape ``|`` everywhere, and
    ``*``/``<``/``>`` outside code spans (a bare ``attempt-preserve/*`` pair would
    italicize, a bare ``<id>`` would vanish as an HTML tag)."""
    out: list[str] = []
    pos = 0
    for m in _CODE_SPAN.finditer(text):
        out.append(_escape_prose(text[pos : m.start()]))
        out.append(m.group(0).replace("|", "\\|"))
        pos = m.end()
    out.append(_escape_prose(text[pos:]))
    return "".join(out)


def _escape_prose(text: str) -> str:
    return text.replace("|", "\\|").replace("*", "\\*").replace("<", "&lt;").replace(">", "&gt;")


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return str(value)


def _default(spec: SettingSpec) -> str:
    if spec.default is not None:
        return f"`{_toml(spec.default)}`".replace("|", "\\|")
    if spec.kind == "lines":
        return "`[]`"
    if spec.placeholder:
        return f"unset — {_escape(spec.placeholder)}"
    return "unset"


def _allowed(section: str, spec: SettingSpec) -> str:
    runtime = _RUNTIME_OPTIONS.get((section, spec.key))
    if runtime is not None:
        return runtime
    if spec.options:
        return " / ".join(f"`{o}`" for o in spec.options)
    if spec.minimum is not None and spec.maximum is not None:
        return f"{str(spec.minimum)}–{str(spec.maximum)}"
    if spec.minimum is not None:
        return f"≥ {str(spec.minimum)}"
    if spec.maximum is not None:
        return f"≤ {str(spec.maximum)}"
    return ""


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def render_section(section: SectionSpec) -> list[str]:
    lines = [f"### `[{section.name}]`", ""]
    if section.description:
        lines += [_escape(_sentence(section.description)), ""]
    lines += [
        "| Key | Type | Default | Allowed | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for spec in section.fields:
        cells = [
            f"`{spec.key}`",
            _TYPES.get(spec.kind, spec.kind),
            _default(spec),
            _allowed(section.name, spec),
            _escape(spec.description),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def render_block() -> str:
    """The whole generated block, markers included, newline-terminated."""
    lines = [BEGIN, "<!-- prettier-ignore-start -->", ""]
    for section in load_core_schema():
        lines += render_section(section)
    lines += ["<!-- prettier-ignore-end -->", END]
    return "\n".join(lines) + "\n"


def splice(page: str, block: str) -> str:
    """Replace the marked block in ``page`` with ``block``; exit if the markers are missing."""
    start = page.find(BEGIN)
    end = page.find(END)
    if start == -1 or end == -1 or end < start:
        sys.exit(f"gen_policy_reference: {TARGET.relative_to(REPO)} lacks the BEGIN/END markers")
    stop = end + len(END)
    if page[stop : stop + 1] == "\n":
        stop += 1
    return page[:start] + block + page[stop:]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="gen_policy_reference.py", description=__doc__)
    p.add_argument(
        "--check", action="store_true", help="exit 1 if the committed block is stale; write nothing"
    )
    args = p.parse_args(argv)
    page = TARGET.read_text(encoding="utf-8")
    updated = splice(page, render_block())
    rel = TARGET.relative_to(REPO).as_posix()
    if args.check:
        if updated != page:
            print(
                f"policy-reference: {rel} is STALE against src/bmad_loop/data/settings/core.toml"
                " — run `uv run python scripts/gen_policy_reference.py` and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"policy-reference: {rel} is current")
        return 0
    if updated == page:
        print(f"policy-reference: {rel} already current")
    else:
        TARGET.write_text(updated, encoding="utf-8", newline="\n")
        print(f"policy-reference: rewrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
