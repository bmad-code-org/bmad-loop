#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""Register a module's configuration into a project's _bmad/ tree.

BMAD ships two config layouts and this script writes whichever one the project
actually uses. --bmad-dir points at _bmad/ and decides which (see detect_layout):

TOML layout (BMAD v6.10+, detected by the presence of _bmad/config.toml)
    The consolidated resolver (_bmad/scripts/resolve_config.py) reads exactly four
    files: config.toml, config.user.toml, custom/config.toml, custom/config.user.toml.
    It never reads config.yaml. So the [modules.<code>] table is written into
    _bmad/custom/config.toml — the layer BMAD documents as never touched by the
    installer — through a surgical text edit that leaves every byte outside that
    table, and the file's own line endings and mode, exactly as they were. (The
    table's own body is ours and is rewritten wholesale: that is the anti-zombie
    guarantee, so comments inside it do not survive.)

    The edit is verified BEFORE anything is written: the candidate must parse and
    must differ from the original only in modules.<code> (see verify_candidate),
    and it is then committed atomically. A refused edit writes nothing, leaves the
    file byte-identical and exits non-zero — bmad-loop never leaves a config it
    cannot vouch for, because BMAD's resolver treats an unparseable custom layer as
    an empty one and would silently drop the user's own overrides along with ours.
    Verification needs tomllib, so this branch requires Python 3.11+ — as BMAD's
    own resolve_config.py already does.

    Core values (user_name, output_folder, ...) are deliberately NOT written on this
    layout: the custom layer WINS over the installer layer, so pinning them here
    would override every future installer answer. Core config stays installer-owned.
    Nothing else is written — no config.yaml, no config.user.yaml.

Legacy YAML layout (pre-6.10, no _bmad/config.toml)
    Today's behavior, unchanged. Reads a module.yaml definition and a JSON answers
    file, then writes or updates the shared config.yaml (core values at root + module
    section, anti-zombie) and config.user.yaml (user_name, communication_language,
    plus any module variable with user_setting: true). Requires --config-path and
    --user-config-path.

Inert leftovers (a root config.yaml / config.user.yaml / module-help.csv on a project
that has moved to the TOML layout) are REPORTED as orphans_detected and never deleted.

Legacy migration: when --legacy-dir is provided, reads old per-module config files
from {legacy-dir}/{module-code}/config.yaml and {legacy-dir}/core/config.yaml.
Matching values serve as fallback defaults (answers override them). These legacy
files are READ ONLY and never deleted — on BMAD v6 the per-module and core
config.yaml are live, manifest-tracked files, and the consolidated _bmad/config.yaml
wins on read regardless. Other modules' config is never touched.

Exit codes: 0=success, 1=validation error, 2=runtime error
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required (PEP 723 dependency)", file=sys.stderr)
    sys.exit(2)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Register module config into a project's _bmad/ tree "
        "(TOML layout on BMAD v6.10+, legacy per-file YAML otherwise)."
    )
    parser.add_argument(
        "--bmad-dir",
        required=True,
        help="Path to the project's _bmad/ directory. Selects the layout: TOML when "
        "_bmad/config.toml exists (module table goes to _bmad/custom/config.toml), "
        "otherwise the legacy per-file YAML layout.",
    )
    parser.add_argument(
        "--config-path",
        help="Path to the target _bmad/config.yaml file (legacy YAML layout only)",
    )
    parser.add_argument(
        "--module-yaml",
        required=True,
        help="Path to the module.yaml definition file",
    )
    parser.add_argument(
        "--answers",
        required=True,
        help="Path to JSON file with collected answers",
    )
    parser.add_argument(
        "--user-config-path",
        help="Path to the target _bmad/config.user.yaml file (legacy YAML layout only)",
    )
    parser.add_argument(
        "--legacy-dir",
        help="Path to _bmad/ directory to check for legacy per-module config files. "
        "Matching values are used as fallback defaults; the legacy files are read "
        "only and never deleted (they are live, manifest-tracked config on BMAD v6).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress to stderr",
    )
    return parser.parse_args()


def load_yaml_file(path: str) -> dict:
    """Load a YAML file, returning empty dict if file doesn't exist."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)
    return content if content else {}


def load_json_file(path: str) -> dict:
    """Load a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Keys that live at config root (shared across all modules)
_CORE_KEYS = frozenset(
    {"user_name", "communication_language", "document_output_language", "output_folder"}
)


def load_legacy_values(
    legacy_dir: str, module_code: str, module_yaml: dict, verbose: bool = False
) -> tuple[dict, dict, list]:
    """Read legacy per-module config files and return core/module value dicts.

    Reads {legacy_dir}/core/config.yaml and {legacy_dir}/{module_code}/config.yaml.
    Only returns values whose keys match the current schema (core keys or module.yaml
    variable definitions). Other modules' directories are not touched.

    Returns:
        (legacy_core, legacy_module, files_found) where files_found lists paths read.
    """
    legacy_core: dict = {}
    legacy_module: dict = {}
    files_found: list = []

    # Read core legacy config
    core_path = Path(legacy_dir) / "core" / "config.yaml"
    if core_path.exists():
        core_data = load_yaml_file(str(core_path))
        files_found.append(str(core_path))
        for k, v in core_data.items():
            if k in _CORE_KEYS:
                legacy_core[k] = v
        if verbose:
            print(f"Legacy core config: {list(legacy_core.keys())}", file=sys.stderr)

    # Read module legacy config
    mod_path = Path(legacy_dir) / module_code / "config.yaml"
    if mod_path.exists():
        mod_data = load_yaml_file(str(mod_path))
        files_found.append(str(mod_path))
        for k, v in mod_data.items():
            if k in _CORE_KEYS:
                # Core keys duplicated in module config — only use if not already set
                if k not in legacy_core:
                    legacy_core[k] = v
            elif k in module_yaml and isinstance(module_yaml[k], dict):
                # Module-specific key that matches a current variable definition
                legacy_module[k] = v
        if verbose:
            print(f"Legacy module config: {list(legacy_module.keys())}", file=sys.stderr)

    return legacy_core, legacy_module, files_found


def legacy_config_paths(legacy_dir: str, module_code: str) -> list:
    """The legacy per-module/core config paths that exist, without reading them.

    load_legacy_values parses these for their *values*; the TOML branch has no use
    for the values (core config is installer-owned there) and only reports the
    paths, so it uses this instead — a pure existence check cannot raise on a
    malformed leftover.
    """
    base = Path(legacy_dir)
    return [
        str(path)
        for path in (base / "core" / "config.yaml", base / module_code / "config.yaml")
        if path.exists()
    ]


def apply_legacy_defaults(answers: dict, legacy_core: dict, legacy_module: dict) -> dict:
    """Apply legacy values as fallback defaults under the answers.

    Legacy values fill in any key not already present in answers.
    Explicit answers always win.
    """
    merged = dict(answers)

    if legacy_core:
        core = merged.get("core", {})
        filled_core = dict(legacy_core)  # legacy as base
        filled_core.update(core)  # answers override
        merged["core"] = filled_core

    if legacy_module:
        mod = merged.get("module", {})
        filled_mod = dict(legacy_module)  # legacy as base
        filled_mod.update(mod)  # answers override
        merged["module"] = filled_mod

    return merged


def cleanup_legacy_configs(legacy_dir: str, module_code: str, verbose: bool = False) -> list:
    """Intentionally does NOT delete any legacy config files (returns an empty list).

    Legacy per-module (_bmad/<module>/config.yaml) and core (_bmad/core/config.yaml)
    configs are read as fallback defaults (see load_legacy_values) but never deleted:
    on BMAD v6 both are LIVE, manifest-tracked files, so removing core/config.yaml
    destroys shared core config and removing <module>/config.yaml desyncs
    _bmad/_config/files-manifest.csv. The consolidated _bmad/config.yaml always wins
    on read, so leaving the legacy files in place is harmless. Kept as a function so
    callers and the result JSON stay stable.
    """
    if verbose:
        print(
            "Preserving legacy config files (live BMAD v6 config is never deleted)",
            file=sys.stderr,
        )
    return []


def extract_module_metadata(module_yaml: dict) -> dict:
    """Extract non-variable metadata fields from module.yaml."""
    meta = {}
    for k in ("name", "description"):
        if k in module_yaml:
            meta[k] = module_yaml[k]
    meta["version"] = module_yaml.get("module_version")  # null if absent
    if "default_selected" in module_yaml:
        meta["default_selected"] = module_yaml["default_selected"]
    return meta


def apply_result_templates(module_yaml: dict, module_answers: dict, verbose: bool = False) -> dict:
    """Apply result templates from module.yaml to transform raw answer values.

    For each answer, if the corresponding variable definition in module.yaml has
    a 'result' field, replaces {value} in that template with the answer. Skips
    the template if the answer already contains '{project-root}' to prevent
    double-prefixing.
    """
    transformed = {}
    for key, value in module_answers.items():
        var_def = module_yaml.get(key)
        if isinstance(var_def, dict) and "result" in var_def and "{project-root}" not in str(value):
            template = var_def["result"]
            transformed[key] = template.replace("{value}", str(value))
            if verbose:
                print(
                    f"Applied result template for '{key}': {value} → {transformed[key]}",
                    file=sys.stderr,
                )
        else:
            transformed[key] = value
    return transformed


def merge_config(
    existing_config: dict,
    module_yaml: dict,
    answers: dict,
    verbose: bool = False,
) -> dict:
    """Merge answers into config, applying anti-zombie pattern.

    Args:
        existing_config: Current config.yaml contents (may be empty)
        module_yaml: The module definition
        answers: JSON with 'core' and/or 'module' keys
        verbose: Print progress to stderr

    Returns:
        Updated config dict ready to write
    """
    config = dict(existing_config)
    module_code = module_yaml.get("code")

    if not module_code:
        print("Error: module.yaml must have a 'code' field", file=sys.stderr)
        sys.exit(1)

    # Migrate legacy core: section to root
    if "core" in config and isinstance(config["core"], dict):
        if verbose:
            print("Migrating legacy 'core' section to root", file=sys.stderr)
        config.update(config.pop("core"))

    # Strip user-only keys from config — they belong exclusively in config.user.yaml
    for key in _CORE_USER_KEYS:
        if key in config:
            if verbose:
                print(
                    f"Removing user-only key '{key}' from config (belongs in config.user.yaml)",
                    file=sys.stderr,
                )
            del config[key]

    # Write core values at root (global properties, not nested under "core")
    # Exclude user-only keys — those belong exclusively in config.user.yaml
    core_answers = answers.get("core")
    if core_answers:
        shared_core = {k: v for k, v in core_answers.items() if k not in _CORE_USER_KEYS}
        if shared_core:
            if verbose:
                print(
                    f"Writing core config at root: {list(shared_core.keys())}",
                    file=sys.stderr,
                )
            config.update(shared_core)

    # Anti-zombie: remove existing module section
    if module_code in config:
        if verbose:
            print(
                f"Removing existing '{module_code}' section (anti-zombie)",
                file=sys.stderr,
            )
        del config[module_code]

    # Build module section: metadata + variable values
    module_section = extract_module_metadata(module_yaml)
    module_answers = apply_result_templates(module_yaml, answers.get("module", {}), verbose)
    module_section.update(module_answers)

    if verbose:
        print(
            f"Writing '{module_code}' section with keys: {list(module_section.keys())}",
            file=sys.stderr,
        )

    config[module_code] = module_section

    return config


# Core keys that are always written to config.user.yaml
_CORE_USER_KEYS = ("user_name", "communication_language")


def extract_user_settings(module_yaml: dict, answers: dict) -> dict:
    """Collect settings that belong in config.user.yaml.

    Includes user_name and communication_language from core answers, plus any
    module variable whose definition contains user_setting: true.
    """
    user_settings = {}

    core_answers = answers.get("core", {})
    for key in _CORE_USER_KEYS:
        if key in core_answers:
            user_settings[key] = core_answers[key]

    module_answers = answers.get("module", {})
    for var_name, var_def in module_yaml.items():
        if isinstance(var_def, dict) and var_def.get("user_setting") is True:
            if var_name in module_answers:
                user_settings[var_name] = module_answers[var_name]

    return user_settings


def write_config(config: dict, config_path: str, verbose: bool = False) -> None:
    """Write config dict to YAML file, creating parent dirs as needed.

    Committed through atomic_write_text so a failed or interrupted run cannot
    leave a half-written config behind. newline=None keeps the platform line
    endings the plain `open(..., "w")` this replaced produced.
    """
    path = Path(config_path)

    if verbose:
        print(f"Writing config to {path}", file=sys.stderr)

    atomic_write_text(
        path,
        yaml.dump(
            config,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ),
        newline=None,
    )


def detect_layout(bmad_dir: str) -> str:
    """Return "toml" when the project uses the BMAD v6.10+ consolidated layout.

    The tell is a single file: _bmad/config.toml. Deliberately a pure existence
    check — no parsing, no imports, so it behaves identically on every Python this
    script may be run with.
    """
    return "toml" if (Path(bmad_dir) / "config.toml").exists() else "yaml"


# --------------------------------------------------------------- TOML emission
#
# There is no TOML *writer* in the standard library (tomllib is read-only, 3.11+)
# and this script must not grow a dependency, so the module table is hand-emitted.
# The payload is a flat table of scalars, which is the trivial corner of TOML.

_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_escape(text: str) -> str:
    """Escape a string for a TOML basic ("...") string."""
    out = []
    for ch in text:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _toml_key(key: str) -> str:
    """Render a table/key name, quoting it only when it is not a bare key."""
    return key if _BARE_KEY.match(key) else '"%s"' % _toml_escape(key)


def _toml_value(value) -> str:
    """Render a scalar as TOML. Anything exotic is stringified defensively."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return '"%s"' % _toml_escape(str(value))


def emit_module_table(module_code: str, values: dict, newline: str = "\n") -> str:
    """Emit a `[modules.<code>]` table with one `key = value` line per entry.

    None values are skipped — module.yaml leaves optional metadata unset rather
    than nulled, and TOML has no null. ``newline`` is the target file's own line
    ending, so a CRLF config.toml stays CRLF (see read_text_preserving_newlines).
    """
    lines = ["[modules.%s]" % _toml_key(module_code)]
    for key, value in values.items():
        if value is None:
            continue
        lines.append("%s = %s" % (_toml_key(key), _toml_value(value)))
    return newline.join(lines) + newline


# ------------------------------------------------------------- TOML table scanner
#
# Locating a table's span with a regex is not safe: `^\s*\[` also matches a line
# inside a multi-line string or a multi-line array, and a header line may carry a
# trailing comment (`[modules.bmad-loop] # keep`) or quoted components
# (`["modules"."bmad-loop"]`) that a fixed pattern misses. Either mistake lets the
# surgical edit truncate a neighbour's value or append a duplicate table — both
# produce invalid TOML from valid input. So the scanner below tracks string and
# bracket state properly, and upsert_module_table works off its offsets.


def _skip_string(text: str, i: int) -> int:
    """Return the offset just past the string literal starting at ``text[i]``.

    Handles all four TOML string forms. An unterminated literal consumes the rest
    of the document — the candidate then fails to parse and verify_candidate
    refuses the write, which is the correct outcome for malformed input.
    """
    quote = text[i]
    triple = text[i : i + 3]
    if triple in ('"""', "'''"):
        delim, escaped = triple, triple == '"""'
        j = i + 3
        while j < len(text):
            if escaped and text[j] == "\\":
                j += 2
                continue
            if text.startswith(delim, j):
                j += 3
                # TOML lets one or two extra quotes abut the closing delimiter, so
                # `"""ends with ""'''` closes at the *last* of them, not the first.
                extra = 0
                while j < len(text) and text[j] == quote and extra < 2:
                    j += 1
                    extra += 1
                return j
            j += 1
        return len(text)

    escaped = quote == '"'
    j = i + 1
    while j < len(text):
        ch = text[j]
        if escaped and ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        if ch == "\n":  # a single-line string never spans a newline
            return j
        j += 1
    return len(text)


def _parse_header_keys(text: str, start: int) -> "tuple[tuple, int] | None":
    """Parse the table header whose `[` sits at ``text[start]``.

    Returns ``(key_path, end_offset)`` — the normalised dotted key as a tuple and
    the offset just past the closing bracket — or None when this is not a
    well-formed header (a value array, say).
    """
    i = start + 1
    if text.startswith("[", i):  # [[array.of.tables]]
        i += 1
        closing = "]]"
    else:
        closing = "]"

    keys: list = []
    expect_key = True
    while i < len(text):
        ch = text[i]
        if ch in " \t":
            i += 1
            continue
        if ch == "\n":
            return None  # a header never spans a line
        if text.startswith(closing, i):
            return (tuple(keys), i + len(closing)) if keys and not expect_key else None
        if ch == "." and not expect_key:
            expect_key = True
            i += 1
            continue
        if not expect_key:
            return None
        if ch in "\"'":
            end = _skip_string(text, i)
            raw = text[i:end]
            if len(raw) < 2 or raw[0] != raw[-1]:
                return None
            keys.append(_unescape_basic(raw[1:-1]) if ch == '"' else raw[1:-1])
            i = end
        else:
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            if j == i:
                return None
            keys.append(text[i:j])
            i = j
        expect_key = False
    return None


_BASIC_UNESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _unescape_basic(raw: str) -> str:
    """Decode a TOML basic-string body so `"bmad-loop"` compares equal to a bare key."""
    if "\\" not in raw:
        return raw
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\" or i + 1 >= len(raw):
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _BASIC_UNESCAPES:
            out.append(_BASIC_UNESCAPES[nxt])
            i += 2
        elif nxt in "uU":
            width = 4 if nxt == "u" else 8
            try:
                out.append(chr(int(raw[i + 2 : i + 2 + width], 16)))
            except ValueError:
                out.append(nxt)
            i += 2 + width
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def scan_table_headers(text: str) -> list:
    """Return ``[(line_start, header_end, key_path)]`` for every real table header.

    A `[` opens a table only when it is the first significant character on its
    line *and* no array/inline-table value is still open — everything else (a `[`
    inside a string, inside a multi-line array, or after a key) is skipped along
    with the construct that contains it.
    """
    headers: list = []
    i, line_start, depth = 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            i += 1
            line_start = i
            continue
        if ch in " \t\r":
            i += 1
            continue
        if ch == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch == "[":
            if depth == 0 and not text[line_start:i].strip():
                parsed = _parse_header_keys(text, i)
                if parsed is not None:
                    keys, end = parsed
                    headers.append((line_start, end, keys))
                    i = end
                    continue
            depth += 1
        elif ch == "{":
            depth += 1
        elif ch in "]}":
            depth = max(0, depth - 1)
        i += 1
    return headers


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def upsert_module_table(
    text: str, module_code: str, block: str, newline: str = "\n"
) -> tuple[str, bool]:
    """Replace (or append) the `[modules.<code>]` table in TOML source text.

    A surgical text edit, not a parse-and-re-emit: _bmad/custom/config.toml is a
    human-authored, comment-bearing file and round-tripping it through a parser
    would destroy that. Every byte *outside* the `[modules.<code>]` table comes
    back identical, and so does the file's line ending. The table's own body is
    ours and is rewritten wholesale — that is the anti-zombie guarantee.

    The span runs from the header line to the next table header found by
    scan_table_headers (or EOF). Trailing blank and comment lines are backed off
    the span in *both* cases: before a following table they are its preamble, and
    at EOF they are the file's footer. Neither is ours to delete.

    Returns:
        (new_text, replaced) — replaced is True when an existing table was
        overwritten, False when the block was appended.
    """
    headers = scan_table_headers(text)
    target = ("modules", module_code)
    index = next((n for n, h in enumerate(headers) if h[2] == target), None)

    if index is None:
        if not text.strip():
            return block, False
        new_text = text if text.endswith(("\n", "\r")) else text + newline
        if not new_text.endswith(newline * 2):
            new_text += newline
        return new_text + block, False

    start, header_end, _ = headers[index]
    span_end = headers[index + 1][0] if index + 1 < len(headers) else len(text)

    body = text[header_end:span_end].splitlines(keepends=True)
    trailing = 0
    while body and _is_blank_or_comment(body[-1]):
        trailing += len(body.pop())
    span_end -= trailing

    return text[:start] + block + text[span_end:], True


def verify_candidate(original: str, candidate: str, module_code: str, block: str) -> "str | None":
    """Return an error message when ``candidate`` is not a safe edit of ``original``.

    The gate that makes the surgical edit trustworthy. Rather than trusting the
    scanner, it checks the *result*: the candidate must parse, and must be
    semantically identical to the original except for `modules.<code>`, which must
    equal exactly what we emitted. That single comparison catches every way the
    edit can go wrong — a duplicated table (tomllib raises), a truncated multi-line
    value (parse error), a clobbered neighbour (diff mismatch) — so a scanner bug
    fails the run instead of corrupting a config. Returns None when the edit is safe.

    BMAD's own resolve_config.py requires Python 3.11+ for tomllib and exits
    without it, so a v6.10 TOML project cannot function on an older interpreter:
    refusing to write unverified there costs nothing real.
    """
    try:
        import tomllib
    except ImportError:
        return (
            "Cannot verify the edit: tomllib requires Python 3.11+ and this "
            f"interpreter is {sys.version_info[0]}.{sys.version_info[1]}. BMAD v6.10's own "
            "resolve_config.py requires 3.11+ for the same reason, so this project already "
            "needs it. Nothing was written — re-run under Python 3.11 or newer."
        )

    try:
        before = tomllib.loads(original) if original.strip() else {}
    except Exception as exc:  # tomllib.TOMLDecodeError, but stay defensive
        return (
            f"The existing custom/config.toml is not valid TOML ({exc}). Nothing was "
            "written — fix that file by hand, then re-run setup. bmad-loop will not edit "
            "a config it cannot parse."
        )

    try:
        after = tomllib.loads(candidate)
        expected_table = tomllib.loads(block)["modules"][module_code]
    except Exception as exc:
        return (
            f"The edited custom/config.toml would not parse ({exc}). Nothing was written "
            "and the file is unchanged. Please report this with a copy of the file."
        )

    expected = dict(before)
    modules = (
        dict(expected.get("modules") or {}) if isinstance(expected.get("modules"), dict) else {}
    )
    modules[module_code] = expected_table
    expected["modules"] = modules

    if after != expected:
        return (
            "The edit would have changed more than the [modules.%s] table, so it was "
            "refused. Nothing was written and the file is unchanged. Please report this "
            "with a copy of the file." % module_code
        )
    return None


# --------------------------------------------------------------- durable writes

_REPLACE_ATTEMPTS = 6
_REPLACE_BASE_S = 0.05
_REPLACE_CAP_S = 1.5


def _atomic_replace(tmp: Path, target: Path) -> None:
    """``os.replace``, retried on the transient Windows sharing violation a
    concurrent handle on ``target`` triggers (WinError 5/32). Mirrors
    bmad_loop.platform_util.atomic_replace — duplicated, not imported, because
    this script ships standalone into user projects.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            last = attempt == _REPLACE_ATTEMPTS - 1
            winerror = getattr(exc, "winerror", None)
            retryable = isinstance(exc, PermissionError) or winerror in (5, 32)
            if sys.platform != "win32" or last or not retryable:
                raise
            time.sleep(
                min(_REPLACE_CAP_S, _REPLACE_BASE_S * 2**attempt)
                + random.uniform(0, _REPLACE_BASE_S)  # nosec B311 - retry jitter
            )


def atomic_write_text(path: Path, text: str, newline: "str | None" = "") -> None:
    """Write ``text`` to ``path`` through a sibling temp file and one atomic rename.

    An interrupted or failed write must never leave a truncated config behind —
    these files are shared with every other BMAD module. The existing file's mode
    is carried over, which a plain create would otherwise drop. ``newline=""``
    writes verbatim (the caller has already chosen the line ending); ``None`` keeps
    Python's default os.linesep translation, which the legacy YAML branch relies on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.is_file() else None
    tmp = path.with_name(path.name + ".bmad-loop-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        _atomic_replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_text_preserving_newlines(path: Path) -> "tuple[str, str]":
    """Return ``(text, newline)`` with the file's own line endings left intact.

    Path.read_text translates CRLF to LF on the way in and back to os.linesep on
    the way out, so a round-trip rewrites every line of a CRLF file on POSIX. This
    reads verbatim and reports the convention so the emitted block can match it.
    """
    if not path.is_file():
        return "", "\n"
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    return text, "\r\n" if "\r\n" in text else "\n"


# ---------------------------------------------------------------- orphan report

# Files that a pre-6.10 bmad-loop-setup used to write directly under _bmad/. On the
# TOML layout nothing reads them and the installer manifest does not track them.
_ORPHAN_FILES = ("config.yaml", "config.user.yaml", "module-help.csv")

# Module display names used in help CSV column 1, current and pre-rename.
_HELP_MODULE_NAMES = ("BMAD Loop Skills", "BMAD Automator Skills")


def _yaml_has_module_entries(path: Path, module_code: str) -> bool:
    """True when a legacy YAML config carries our module's section."""
    try:
        data = load_yaml_file(str(path))
    except Exception:
        return False  # malformed leftovers are still reportable orphans
    if not isinstance(data, dict):
        return False
    return any(key in data for key in (module_code, "bauto"))


def _csv_has_module_entries(path: Path) -> bool:
    """True when a legacy help CSV carries rows for our module."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if row and row[0].strip() in _HELP_MODULE_NAMES:
                    return True
    except Exception:
        return False
    return False


def detect_orphans(bmad_dir: str, module_code: str) -> list:
    """Report inert pre-6.10 files left directly under _bmad/.

    Reported, never deleted (bmad-loop#64: never delete live BMAD config, and a
    file we do not read is not ours to judge). Reading is fully defensive — a
    malformed orphan is still listed, just with has_module_entries: false.
    """
    orphans = []
    base = Path(bmad_dir)
    for name in _ORPHAN_FILES:
        path = base / name
        if not path.is_file():
            continue
        if name.endswith(".csv"):
            has_entries = _csv_has_module_entries(path)
        else:
            has_entries = _yaml_has_module_entries(path, module_code)
        orphans.append({"path": str(path.resolve()), "has_module_entries": has_entries})
    return orphans


def reject_unresolved_paths(named_paths: list[tuple[str, str]]) -> None:
    """Exit with a clear error if any path argument still contains the literal
    ``{project-root}`` token. That token is meaningful only inside config
    values; filesystem path arguments must be resolved by the caller. Failing
    loudly here prevents silently creating a junk ``{project-root}/`` directory.
    """
    for name, value in named_paths:
        if value and "{project-root}" in value:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": (
                            f"Unresolved '{{project-root}}' token in {name} path: {value!r}. "
                            "Resolve '{project-root}' to the actual project root before running "
                            "this script — it is a filesystem path, not a config value."
                        ),
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)


def _fail(message: str) -> None:
    """Emit the standard error JSON on stderr and exit 1."""
    print(
        json.dumps({"status": "error", "error": message}, indent=2),
        file=sys.stderr,
    )
    sys.exit(1)


def run_toml_layout(args, module_yaml: dict, module_code: str) -> dict:
    """Register the module into _bmad/custom/config.toml (BMAD v6.10+).

    Writes exactly one thing: the [modules.<code>] table. No core values — the
    custom layer wins over the installer layer, so anything pinned here would
    override every future installer answer.
    """
    config_path = Path(args.bmad_dir) / "custom" / "config.toml"
    existing, newline = read_text_preserving_newlines(config_path)

    values = extract_module_metadata(module_yaml)
    block = emit_module_table(module_code, values, newline)

    if args.verbose:
        print(f"TOML layout: registering [modules.{module_code}] in {config_path}", file=sys.stderr)

    new_text, table_replaced = upsert_module_table(existing, module_code, block, newline)

    # Verify BEFORE touching disk: a refused edit must leave the file untouched.
    error = verify_candidate(existing, new_text, module_code, block)
    if error:
        _fail(f"{config_path}: {error}")

    atomic_write_text(config_path, new_text)

    # --legacy-dir stays read-only here, and unparsed: the TOML branch discards
    # legacy *values* (core config is installer-owned), so only the paths matter.
    # Parsing them would let a malformed leftover raise — and it used to raise
    # after the write, reporting failure for a registration that had succeeded.
    legacy_files_found: list = []
    legacy_deleted: list = []
    if args.legacy_dir:
        legacy_files_found = legacy_config_paths(args.legacy_dir, module_code)
        legacy_deleted = cleanup_legacy_configs(args.legacy_dir, module_code, args.verbose)

    return {
        "status": "success",
        "layout": "toml",
        "config_path": str(config_path.resolve()),
        "user_config_path": None,
        "module_code": module_code,
        "core_updated": False,
        "module_keys": [k for k, v in values.items() if v is not None],
        "user_keys": [],
        "table_replaced": table_replaced,
        # Always true on success: the edit is verified before anything is written,
        # so an unverified file is never committed (a failure exits non-zero above).
        "toml_validated": True,
        "legacy_configs_found": legacy_files_found,
        "legacy_configs_deleted": legacy_deleted,
        "orphans_detected": detect_orphans(args.bmad_dir, module_code),
    }


def run_yaml_layout(args, module_yaml: dict, module_code: str) -> dict:
    """Register the module into the legacy per-file YAML layout (pre-6.10)."""
    if not args.config_path or not args.user_config_path:
        _fail(
            "The legacy YAML layout requires --config-path and --user-config-path. "
            f"No {Path(args.bmad_dir) / 'config.toml'} was found, so this project is "
            "not on the BMAD v6.10+ TOML layout."
        )

    answers = load_json_file(args.answers)
    existing_config = load_yaml_file(args.config_path)

    if args.verbose:
        exists = Path(args.config_path).exists()
        print(f"Config file exists: {exists}", file=sys.stderr)
        if exists:
            print(f"Existing sections: {list(existing_config.keys())}", file=sys.stderr)

    # Legacy migration: read old per-module configs as fallback defaults
    legacy_files_found = []
    if args.legacy_dir:
        legacy_core, legacy_module, legacy_files_found = load_legacy_values(
            args.legacy_dir, module_code, module_yaml, args.verbose
        )
        if legacy_core or legacy_module:
            answers = apply_legacy_defaults(answers, legacy_core, legacy_module)
            if args.verbose:
                print("Applied legacy values as fallback defaults", file=sys.stderr)

    # Merge and write config.yaml
    updated_config = merge_config(existing_config, module_yaml, answers, args.verbose)
    write_config(updated_config, args.config_path, args.verbose)

    # Merge and write config.user.yaml
    user_settings = extract_user_settings(module_yaml, answers)
    existing_user_config = load_yaml_file(args.user_config_path)
    updated_user_config = dict(existing_user_config)
    updated_user_config.update(user_settings)
    if user_settings:
        write_config(updated_user_config, args.user_config_path, args.verbose)

    # Legacy cleanup: delete old per-module config files
    legacy_deleted = []
    if args.legacy_dir:
        legacy_deleted = cleanup_legacy_configs(args.legacy_dir, module_code, args.verbose)

    return {
        "status": "success",
        "layout": "yaml",
        "config_path": str(Path(args.config_path).resolve()),
        "user_config_path": str(Path(args.user_config_path).resolve()),
        "module_code": module_code,
        "core_updated": bool(answers.get("core")),
        "module_keys": list(updated_config.get(module_code, {}).keys()),
        "user_keys": list(user_settings.keys()),
        "legacy_configs_found": legacy_files_found,
        "legacy_configs_deleted": legacy_deleted,
        "orphans_detected": [],
    }


def main():
    args = parse_args()

    reject_unresolved_paths(
        [
            ("--bmad-dir", args.bmad_dir),
            ("--config-path", args.config_path),
            ("--user-config-path", args.user_config_path),
            ("--legacy-dir", args.legacy_dir),
        ]
    )

    # Load inputs
    module_yaml = load_yaml_file(args.module_yaml)
    if not module_yaml:
        print(
            f"Error: Could not load module.yaml from {args.module_yaml}",
            file=sys.stderr,
        )
        sys.exit(1)

    module_code = module_yaml.get("code")
    if not module_code:
        print("Error: module.yaml must have a 'code' field", file=sys.stderr)
        sys.exit(1)

    layout = detect_layout(args.bmad_dir)
    if args.verbose:
        print(f"Detected {layout} config layout under {args.bmad_dir}", file=sys.stderr)

    try:
        if layout == "toml":
            result = run_toml_layout(args, module_yaml, module_code)
        else:
            result = run_yaml_layout(args, module_yaml, module_code)
    except OSError as exc:
        # Each write is atomic, so whatever failed here was left exactly as it was —
        # never half-written. An earlier output may already be committed, which is
        # harmless: the merge is idempotent, so re-running finishes the job.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": f"Could not write config: {exc}. Every write is atomic, so no "
                    "config was left truncated; any output not yet committed is unchanged. "
                    "Fix the cause and re-run — the merge is idempotent.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
