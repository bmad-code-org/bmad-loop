#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Register a module's help entries into a project's _bmad/ tree.

BMAD ships two help layouts and this script writes whichever one the project
actually uses. --bmad-dir points at _bmad/ and decides which (see detect_layout):

TOML layout (BMAD v6.10+, detected by the presence of _bmad/config.toml)
    /bmad-help reads exactly one catalog: _bmad/_config/bmad-help.csv, the
    installer's assembled concatenation of the per-module _bmad/<module>/module-help.csv
    files. A root _bmad/module-help.csv is read by nothing and is not tracked in
    _bmad/_config/files-manifest.csv, so on this layout two files are written:

      _bmad/<module-code>/module-help.csv — the per-module CSV, entirely ours, so
        it is overwritten outright (source header + source rows).
      _bmad/_config/bmad-help.csv — shared with every other module, so our rows are
        merged in anti-zombie (every row whose module column matches a source module
        code is dropped first) and the catalog's own header is preserved. Created
        from the source header when absent. Because the catalog owns its header, our
        rows are re-keyed into its column order by name (see align_rows) rather than
        appended positionally.

    Both files are rendered in full before either is written, and each is committed
    through a temp file and one atomic rename: the catalog is shared state that is
    not hash-tracked, so it must never be left truncated by a failed run.

    Caveat worth reporting to the user: the catalog is generated, so a BMAD
    installer re-run regenerates it and drops our rows. Re-running the setup skill
    restores them.

Legacy YAML layout (pre-6.10, no _bmad/config.toml)
    Today's behavior, unchanged: the same anti-zombie merge into the single shared
    CSV named by --target (conventionally _bmad/module-help.csv). Requires --target.

Inert leftovers (a root config.yaml / config.user.yaml / module-help.csv on a project
that has moved to the TOML layout) are REPORTED as orphans_detected and never deleted.

Legacy cleanup: DISABLED. Old per-module module-help.csv files (including
_bmad/core/module-help.csv) are never deleted — on BMAD v6 they are live,
manifest-tracked files. The --legacy-dir / --module-code flags are accepted for
backward compatibility but no longer remove anything.

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
from io import StringIO
from pathlib import Path

# Fallback CSV header, used only when neither the target nor the source carries one.
# Matches the columns every real BMAD CSV ships — _bmad/_config/bmad-help.csv,
# _bmad/<module>/module-help.csv and this skill's own assets/module-help.csv.
HEADER = [
    "module",
    "skill",
    "display-name",
    "menu-code",
    "description",
    "action",
    "args",
    "phase",
    "preceded-by",
    "followed-by",
    "required",
    "output-location",
    "outputs",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Register module help entries into a project's _bmad/ tree with an "
        "anti-zombie merge (BMAD v6.10+ TOML layout, or the legacy shared CSV otherwise)."
    )
    parser.add_argument(
        "--bmad-dir",
        required=True,
        help="Path to the project's _bmad/ directory. Selects the layout: TOML when "
        "_bmad/config.toml exists (rows go to _bmad/<module-code>/module-help.csv and are "
        "merged into _bmad/_config/bmad-help.csv), otherwise the legacy --target CSV.",
    )
    parser.add_argument(
        "--target",
        help="Path to the target shared _bmad/module-help.csv file (legacy YAML layout only)",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the source module-help.csv with entries to merge",
    )
    parser.add_argument(
        "--legacy-dir",
        help="Path to _bmad/ directory to check for legacy per-module CSV files.",
    )
    parser.add_argument(
        "--module-code",
        help="Module code. Required on the TOML layout (it names the per-module CSV "
        "directory) and with --legacy-dir (for scoping cleanup).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress to stderr",
    )
    return parser.parse_args()


def read_csv_rows(path: str) -> tuple[list[str], list[list[str]]]:
    """Read CSV file returning (header, data_rows).

    Returns empty header and rows if file doesn't exist.
    """
    file_path = Path(path)
    if not file_path.exists():
        return [], []

    with open(file_path, "r", encoding="utf-8", newline="") as f:
        content = f.read()

    reader = csv.reader(StringIO(content))
    rows = list(reader)

    if not rows:
        return [], []

    return rows[0], rows[1:]


def extract_module_codes(rows: list[list[str]]) -> set[str]:
    """Extract unique module codes from data rows."""
    codes = set()
    for row in rows:
        if row and row[0].strip():
            codes.add(row[0].strip())
    return codes


def filter_rows(rows: list[list[str]], module_code: str) -> list[list[str]]:
    """Remove all rows matching the given module code."""
    return [row for row in rows if not row or row[0].strip() != module_code]


def merge_rows(
    existing_rows: list[list[str]],
    source_rows: list[list[str]],
    source_codes: set,
    verbose: bool = False,
) -> tuple[list[list[str]], int]:
    """Anti-zombie merge: drop every existing row for a source module code, then append.

    Keyed on the module column of the *source* rows, so rows a previous run wrote
    under a different module name (e.g. the pre-rename "BMAD Automator Skills") are
    not matched and survive — removing those is a documented manual step.

    Returns:
        (merged_rows, removed_count)
    """
    filtered_rows = existing_rows
    removed_count = 0
    for code in sorted(source_codes):
        before_count = len(filtered_rows)
        filtered_rows = filter_rows(filtered_rows, code)
        removed_count += before_count - len(filtered_rows)

    if verbose and removed_count > 0:
        print(f"Removed {removed_count} existing rows (anti-zombie)", file=sys.stderr)

    return filtered_rows + source_rows, removed_count


def align_rows(
    source_header: list[str], rows: list[list[str]], target_header: list[str]
) -> tuple[list[list[str]], list[str]]:
    """Re-key ``rows`` from the source column order into the target's.

    The catalog keeps its own header, so appending source rows positionally is only
    correct while the two headers happen to agree. They are not the same file and
    not the same schema owner — BMAD reorders or extends the help columns and every
    one of our rows silently shifts a field (a description landing under `skill`),
    with nothing to notice it. Mapping by column *name* makes the merge independent
    of column order; columns the target does not have are dropped and reported.

    Returns:
        (aligned_rows, dropped_columns) — dropped lists only source columns that
        have no home in the target *and* carry a value somewhere.
    """
    if not target_header or source_header == target_header:
        # Fast path: identical schemas (every current BMAD tree). Still normalise
        # width so a short or long row can never shift the columns after it.
        width = len(target_header or source_header)
        if not width:
            return [list(row) for row in rows], []
        return [list(row[:width]) + [""] * (width - len(row)) for row in rows], []

    index = {name: n for n, name in enumerate(source_header)}
    aligned = []
    for row in rows:
        aligned.append(
            [
                row[index[col]] if col in index and index[col] < len(row) else ""
                for col in target_header
            ]
        )

    dropped = [
        col
        for col in source_header
        if col not in target_header
        and any(index[col] < len(row) and row[index[col]].strip() for row in rows)
    ]
    return aligned, dropped


def render_csv(header: list[str], rows: list[list[str]]) -> str:
    """Serialise header + rows to CSV text (excel dialect, CRLF terminators).

    Split out from the write so both outputs can be built in full before either is
    committed — see run_toml_layout.
    """
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


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


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a sibling temp file and one atomic rename.

    _bmad/_config/bmad-help.csv is shared with every other BMAD module and is not
    hash-tracked in files-manifest.csv, so a truncated write there loses every
    module's help rows with nothing to detect it. Never truncate it in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.is_file() else None
    tmp = path.with_name(path.name + ".bmad-loop-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        _atomic_replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_csv(path: str, header: list[str], rows: list[list[str]], verbose: bool = False) -> None:
    """Write header + rows to CSV file, creating parent dirs as needed."""
    if verbose:
        print(f"Writing {len(rows)} data rows to {path}", file=sys.stderr)

    atomic_write_text(Path(path), render_csv(header, rows))


def cleanup_legacy_csvs(legacy_dir: str, module_code: str, verbose: bool = False) -> list:
    """Intentionally does NOT delete any legacy CSV files (returns an empty list).

    Old per-module module-help.csv files — including _bmad/core/module-help.csv — are
    live, manifest-tracked config on BMAD v6, so removing them destroys shared BMAD
    state or desyncs _bmad/_config/files-manifest.csv. The anti-zombie merge into the
    shared _bmad/module-help.csv already supersedes their entries, so leaving them in
    place is harmless. Kept as a function so callers and the result JSON stay stable.
    """
    if verbose:
        print(
            "Preserving legacy module-help.csv files (live BMAD v6 config is never deleted)",
            file=sys.stderr,
        )
    return []


def detect_layout(bmad_dir: str) -> str:
    """Return "toml" when the project uses the BMAD v6.10+ consolidated layout.

    The tell is a single file: _bmad/config.toml. Deliberately a pure existence
    check — no parsing, no imports, so it behaves identically on every Python this
    script may be run with. Duplicated in merge-config.py on purpose: both scripts
    ship standalone into user projects and must not import each other.
    """
    return "toml" if (Path(bmad_dir) / "config.toml").exists() else "yaml"


# ---------------------------------------------------------------- orphan report

# Files that a pre-6.10 bmad-loop-setup used to write directly under _bmad/. On the
# TOML layout nothing reads them and the installer manifest does not track them.
_ORPHAN_FILES = ("config.yaml", "config.user.yaml", "module-help.csv")

# Module display names used in help CSV column 1, current and pre-rename.
_HELP_MODULE_NAMES = ("BMAD Loop Skills", "BMAD Automator Skills")

# An unindented `key:` line. This script declares no dependencies (merge-config.py
# has pyyaml; this one must stay pure stdlib), so the orphan YAML check is a
# conservative top-level key scan rather than a parse. It only ever answers "does
# this leftover mention our module", which a line scan settles.
_TOP_LEVEL_KEY = re.compile(r"^(?P<key>[^\s#][^:#]*?)[ \t]*:(?:[ \t].*)?$")


def _yaml_top_level_keys(path: Path) -> set:
    """Best-effort set of top-level keys in a YAML file. Never raises."""
    keys: set = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys
    for line in text.splitlines():
        if not line or line[0] in " \t#":
            continue
        match = _TOP_LEVEL_KEY.match(line)
        if match:
            keys.add(match.group("key").strip().strip("\"'"))
    return keys


def _yaml_has_module_entries(path: Path, module_code: str) -> bool:
    """True when a legacy YAML config carries our module's section."""
    return bool(_yaml_top_level_keys(path) & {module_code, "bauto"})


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
            _fail(
                f"Unresolved '{{project-root}}' token in {name} path: {value!r}. "
                "Resolve '{project-root}' to the actual project root before running "
                "this script — it is a filesystem path, not a config value."
            )


def _fail(message: str) -> None:
    """Emit the standard error JSON and exit 1."""
    print(json.dumps({"status": "error", "error": message}, indent=2))
    sys.exit(1)


def run_toml_layout(
    args, source_header: list[str], source_rows: list[list[str]], source_codes: set
) -> dict:
    """Register help rows where BMAD v6.10+ actually reads them.

    Writes the per-module CSV the installer assembles from, then merges the same
    rows into the assembled catalog /bmad-help reads. The per-module file is
    entirely ours, so it is overwritten; the catalog is shared, so it is merged.
    """
    if not args.module_code:
        _fail(
            "The TOML layout requires --module-code: it names the per-module CSV "
            f"directory ({Path(args.bmad_dir) / '<module-code>' / 'module-help.csv'}). "
            f"This project is on the BMAD v6.10+ TOML layout ({Path(args.bmad_dir) / 'config.toml'} exists)."
        )

    if len(source_codes) > 1:
        _fail(
            "The source CSV carries rows for more than one module "
            f"({', '.join(sorted(source_codes))}), but _bmad/{args.module_code}/module-help.csv "
            "is by definition one module's file — the installer attributes every row in it to "
            f"'{args.module_code}'. Split the source, or run the legacy --target merge instead."
        )

    header = source_header if source_header else HEADER

    # Both outputs are rendered in full before either is committed, and each is
    # committed atomically: a failure part-way must never truncate the shared
    # catalog, and must never leave it half-merged.

    # 1. The per-module CSV: wholly ours, so a full overwrite. Creating it also makes
    #    _bmad/<module-code>/ config-bearing, which cleanup-legacy.py then protects.
    module_help_path = Path(args.bmad_dir) / args.module_code / "module-help.csv"

    # 2. The assembled catalog: shared with every other module, so merge anti-zombie
    #    and keep its own header — remapping our rows into its column order.
    catalog_path = Path(args.bmad_dir) / "_config" / "bmad-help.csv"
    catalog_existed = catalog_path.is_file()
    catalog_header, catalog_rows = read_csv_rows(str(catalog_path))
    target_header = catalog_header if catalog_header else header

    if args.verbose:
        print(f"Catalog exists: {catalog_existed}", file=sys.stderr)
        if catalog_existed:
            print(f"Existing catalog rows: {len(catalog_rows)}", file=sys.stderr)

    catalog_source_rows, columns_dropped = align_rows(header, source_rows, target_header)
    if columns_dropped:
        print(
            "Warning: the catalog header has no column for "
            f"{', '.join(columns_dropped)} — those values were dropped from the merged rows.",
            file=sys.stderr,
        )

    merged_rows, removed_count = merge_rows(
        catalog_rows, catalog_source_rows, source_codes, args.verbose
    )

    module_help_text = render_csv(header, source_rows)
    catalog_text = render_csv(target_header, merged_rows)

    if args.verbose:
        print(f"TOML layout: writing per-module CSV {module_help_path}", file=sys.stderr)
    atomic_write_text(module_help_path, module_help_text)
    atomic_write_text(catalog_path, catalog_text)

    legacy_deleted = []
    if args.legacy_dir:
        legacy_deleted = cleanup_legacy_csvs(args.legacy_dir, args.module_code, args.verbose)

    return {
        "status": "success",
        "layout": "toml",
        "module_help_path": str(module_help_path.resolve()),
        "catalog_path": str(catalog_path.resolve()),
        "catalog_existed": catalog_existed,
        "module_codes": sorted(source_codes),
        "rows_removed": removed_count,
        "rows_added": len(source_rows),
        "total_rows": len(merged_rows),
        "columns_dropped": columns_dropped,
        "legacy_csvs_deleted": legacy_deleted,
        "orphans_detected": detect_orphans(args.bmad_dir, args.module_code),
    }


def run_yaml_layout(
    args, source_header: list[str], source_rows: list[list[str]], source_codes: set
) -> dict:
    """Merge help rows into the single shared CSV of the legacy layout (pre-6.10)."""
    if not args.target:
        _fail(
            "The legacy YAML layout requires --target (conventionally "
            f"{Path(args.bmad_dir) / 'module-help.csv'}). No {Path(args.bmad_dir) / 'config.toml'} "
            "was found, so this project is not on the BMAD v6.10+ TOML layout."
        )

    # Read existing target (may not exist)
    target_header, target_rows = read_csv_rows(args.target)
    target_existed = Path(args.target).exists()

    if args.verbose:
        print(f"Target exists: {target_existed}", file=sys.stderr)
        if target_existed:
            print(f"Existing target rows: {len(target_rows)}", file=sys.stderr)

    # Use source header if target doesn't exist or has no header
    header = target_header if target_header else (source_header if source_header else HEADER)

    # Same name-keyed remap as the TOML branch: the target keeps its own header, so
    # appending positionally is only correct while the two schemas agree.
    aligned_source, columns_dropped = align_rows(
        source_header if source_header else HEADER, source_rows, header
    )
    if columns_dropped:
        print(
            "Warning: the target header has no column for "
            f"{', '.join(columns_dropped)} — those values were dropped from the merged rows.",
            file=sys.stderr,
        )

    merged_rows, removed_count = merge_rows(target_rows, aligned_source, source_codes, args.verbose)

    write_csv(args.target, header, merged_rows, args.verbose)

    # Legacy cleanup: delete old per-module CSV files
    legacy_deleted = []
    if args.legacy_dir:
        if not args.module_code:
            print(
                "Error: --module-code is required when --legacy-dir is provided",
                file=sys.stderr,
            )
            sys.exit(1)
        legacy_deleted = cleanup_legacy_csvs(args.legacy_dir, args.module_code, args.verbose)

    return {
        "status": "success",
        "layout": "yaml",
        "target_path": str(Path(args.target).resolve()),
        "target_existed": target_existed,
        "module_codes": sorted(source_codes),
        "rows_removed": removed_count,
        "rows_added": len(source_rows),
        "total_rows": len(merged_rows),
        "columns_dropped": columns_dropped,
        "legacy_csvs_deleted": legacy_deleted,
        "orphans_detected": [],
    }


def main():
    args = parse_args()

    reject_unresolved_paths(
        [
            ("--bmad-dir", args.bmad_dir),
            ("--target", args.target),
            ("--legacy-dir", args.legacy_dir),
        ]
    )

    # Read source entries
    source_header, source_rows = read_csv_rows(args.source)
    if not source_rows:
        print(f"Error: No data rows found in source {args.source}", file=sys.stderr)
        sys.exit(1)

    # Determine module codes being merged
    source_codes = extract_module_codes(source_rows)
    if not source_codes:
        print("Error: Could not determine module code from source rows", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Source module codes: {source_codes}", file=sys.stderr)
        print(f"Source rows: {len(source_rows)}", file=sys.stderr)

    layout = detect_layout(args.bmad_dir)
    if args.verbose:
        print(f"Detected {layout} help layout under {args.bmad_dir}", file=sys.stderr)

    try:
        if layout == "toml":
            result = run_toml_layout(args, source_header, source_rows, source_codes)
        else:
            result = run_yaml_layout(args, source_header, source_rows, source_codes)
    except OSError as exc:
        # Each write is atomic, so whatever failed here was left exactly as it was —
        # never half-written. An earlier output may already be committed, which is
        # harmless: the merge is idempotent, so re-running finishes the job.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": f"Could not write help entries: {exc}. Every write is atomic, so "
                    "no CSV was left truncated; any output not yet committed is unchanged. "
                    "Fix the cause and re-run — the merge is idempotent.",
                },
                indent=2,
            )
        )
        sys.exit(2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
