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
    installer — through a surgical text edit that leaves every other byte of that
    human-authored, comment-bearing file alone.

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
import re
import sys
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
    """Write config dict to YAML file, creating parent dirs as needed."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"Writing config to {path}", file=sys.stderr)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            config,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
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


def emit_module_table(module_code: str, values: dict) -> str:
    """Emit a `[modules.<code>]` table with one `key = value` line per entry.

    None values are skipped — module.yaml leaves optional metadata unset rather
    than nulled, and TOML has no null.
    """
    lines = ["[modules.%s]" % _toml_key(module_code)]
    for key, value in values.items():
        if value is None:
            continue
        lines.append("%s = %s" % (_toml_key(key), _toml_value(value)))
    return "\n".join(lines) + "\n"


_NEXT_TABLE = re.compile(r"^[ \t]*\[", re.MULTILINE)


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _module_header_pattern(module_code: str) -> "re.Pattern":
    """Match the `[modules.<code>]` header line, bare or quoted, any spacing."""
    forms = "|".join(
        (
            re.escape(module_code),
            re.escape('"%s"' % _toml_escape(module_code)),
        )
    )
    return re.compile(
        r"^[ \t]*\[[ \t]*modules[ \t]*\.[ \t]*(?:%s)[ \t]*\][ \t]*$" % forms,
        re.MULTILINE,
    )


def upsert_module_table(text: str, module_code: str, block: str) -> tuple[str, bool]:
    """Replace (or append) the `[modules.<code>]` table in TOML source text.

    A surgical text edit, not a parse-and-re-emit: _bmad/custom/config.toml is a
    human-authored, comment-bearing file and round-tripping it through a parser
    would destroy that. Every byte outside the replaced table comes back identical.

    The table's span runs from its header line to the next top-level `[` line (or
    EOF). Blank and comment lines immediately above that next header are the *next*
    table's preamble in any hand-written file, so they are left in place.

    Returns:
        (new_text, replaced) — replaced is True when an existing table was
        overwritten, False when the block was appended.
    """
    match = _module_header_pattern(module_code).search(text)

    if match is None:
        if not text.strip():
            return block, False
        new_text = text if text.endswith("\n") else text + "\n"
        if not new_text.endswith("\n\n"):
            new_text += "\n"
        return new_text + block, False

    header_end = match.end()
    following = _NEXT_TABLE.search(text, header_end)
    span_end = following.start() if following else len(text)

    if following is not None:
        body = text[header_end:span_end].splitlines(keepends=True)
        trailing = 0
        while body and _is_blank_or_comment(body[-1]):
            trailing += len(body.pop())
        span_end -= trailing

    return text[: match.start()] + block + text[span_end:], True


def validate_toml(text: str, verbose: bool = False) -> bool:
    """Parse the emitted TOML back with tomllib, if it is available.

    tomllib is stdlib only on Python 3.11+; this script declares >=3.9, so on
    older interpreters validation is skipped rather than failing the run.
    """
    try:
        import tomllib
    except ImportError:
        print(
            "Note: tomllib unavailable (Python < 3.11) — skipping TOML validation",
            file=sys.stderr,
        )
        return False

    try:
        tomllib.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError, but stay defensive
        print(f"Warning: written TOML did not parse cleanly: {exc}", file=sys.stderr)
        return False

    if verbose:
        print("Validated written TOML with tomllib", file=sys.stderr)
    return True


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
    values = extract_module_metadata(module_yaml)
    block = emit_module_table(module_code, values)

    config_path = Path(args.bmad_dir) / "custom" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""

    if args.verbose:
        print(f"TOML layout: registering [modules.{module_code}] in {config_path}", file=sys.stderr)

    new_text, table_replaced = upsert_module_table(existing, module_code, block)
    config_path.write_text(new_text, encoding="utf-8")

    toml_validated = validate_toml(new_text, args.verbose)

    # --legacy-dir stays read-only here: report what exists, delete nothing.
    legacy_files_found: list = []
    legacy_deleted: list = []
    if args.legacy_dir:
        _, _, legacy_files_found = load_legacy_values(
            args.legacy_dir, module_code, module_yaml, args.verbose
        )
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
        "toml_validated": toml_validated,
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

    if layout == "toml":
        result = run_toml_layout(args, module_yaml, module_code)
    else:
        result = run_yaml_layout(args, module_yaml, module_code)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
