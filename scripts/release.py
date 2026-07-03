#!/usr/bin/env python3
"""Standardized release driver for ``bmad-loop``.

This is the deterministic engine behind the release flow. It does *not* own the
version regexes — :mod:`sync_version` is the single source of truth for the
version and stamps every file; ``release.py`` orchestrates it together with the
asset generators, the CHANGELOG, git, and ``gh``.

The flow is two-phase:

* ``prepare X.Y.Z`` runs on a feature/release branch. It validates the CHANGELOG
  section for the target version (the human curates it *before* calling this),
  stamps the version everywhere via ``sync_version.py``,
  regenerates screenshots + demo *only* when ``src/bmad_loop/tui`` changed since
  the last tag, and commits the result — leaving the branch ready for a PR.
* ``publish`` runs on ``main`` after the PR merges (driven by
  ``.github/workflows/release.yml``). It is idempotent: if the ``vX.Y.Z`` tag
  already exists it is a no-op, otherwise it creates the tag + GitHub release with
  notes extracted from the CHANGELOG.

Usage::

    python scripts/release.py prepare 0.5.0           # bump + changelog + assets + commit
    python scripts/release.py prepare 0.5.0 --dry-run  # report only, no mutations
    python scripts/release.py commits                  # commits since last tag, grouped
    python scripts/release.py publish                  # tag + gh release (idempotent)
    python scripts/release.py publish --dry-run        # show the tag + notes it would create
    python scripts/release.py check                    # local mirror of the CI guards
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# sync_version is the canonical owner of the version value + format. Import it
# rather than re-deriving any version regex here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_version  # noqa: E402

REPO = sync_version.ROOT
CHANGELOG = REPO / "CHANGELOG.md"
SYNC_VERSION = REPO / "scripts" / "sync_version.py"
SEED_SKILLS = REPO / "scripts" / "seed_skills.py"
GEN_SCREENSHOTS = REPO / "scripts" / "gen_screenshots.py"
GEN_DEMO = REPO / "scripts" / "gen_demo.py"
TUI_PATH = "src/bmad_loop/tui"

# Files the release commit touches. Staged explicitly (not `git add -A`) so the
# commit can never sweep in unrelated working-tree changes.
STAMPED_PATHS = [
    "src/bmad_loop/__init__.py",
    "pyproject.toml",
    "module.yaml",
    "src/bmad_loop/data/skills/bmad-loop-setup/assets/module.yaml",
    ".claude-plugin/marketplace.json",
    "uv.lock",
    "CHANGELOG.md",
]
ASSET_PATH = "docs/images"

DEFAULT_REPO_URL = "https://github.com/bmad-code-org/bmad-loop"


# --------------------------------------------------------------------------- #
# small shells
# --------------------------------------------------------------------------- #
def _run(
    cmd: list[str], *, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess:
    if not capture:
        sys.stdout.flush()  # keep our prints ahead of the child's direct fd writes
    return subprocess.run(cmd, cwd=REPO, check=check, text=True, capture_output=capture)


def _git_out(*args: str) -> str:
    return _run(["git", *args], capture=True).stdout.strip()


def _die(msg: str) -> None:
    sys.exit(f"release: {msg}")


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested in tests/test_release.py)
# --------------------------------------------------------------------------- #
def parse_version(v: str) -> tuple[tuple[int, int, int], str]:
    """Return ((major, minor, patch), suffix). ``suffix`` is '' for a final
    release or e.g. 'rc1' for a pre-release. Raises ValueError on bad input."""
    if not sync_version._VERSION_RE.match(v):
        raise ValueError(f"{v!r} is not a valid X.Y.Z version")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[.-]([0-9A-Za-z.-]+))?$", v)
    assert m  # guaranteed by the regex above
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    suffix = m.group(4) or ""
    return (major, minor, patch), suffix


def version_gt(new: str, old: str) -> bool:
    """True if ``new`` is strictly greater than ``old``. A pre-release sorts
    below the same final version (1.2.0-rc1 < 1.2.0)."""
    (nc, ns), (oc, os_) = parse_version(new), parse_version(old)
    if nc != oc:
        return nc > oc
    # same numeric core: '' (final) outranks any suffix; otherwise lexical.
    if ns == os_:
        return False
    if ns == "":
        return True
    if os_ == "":
        return False
    return ns > os_


def section_re(version: str) -> re.Pattern[str]:
    # Body runs until the next `## ` heading, the trailing link-reference block
    # (`[x]: url` lines), or end-of-file — so the last section never swallows refs.
    return re.compile(
        rf"(?m)^##\s+\[{re.escape(version)}\][^\n]*\n(?P<body>.*?)(?=^##\s|^\[[^\]]+\]:\s|\Z)",
        re.DOTALL,
    )


def extract_section(text: str, version: str) -> str | None:
    """Return the trimmed body of the ``## [version]`` CHANGELOG section, or
    ``None`` if there is no such heading."""
    m = section_re(version).search(text)
    if not m:
        return None
    return m.group("body").strip()


def has_curated_section(text: str, version: str) -> bool:
    body = extract_section(text, version)
    return bool(body)


def ensure_link_ref(text: str, version: str, repo_url: str) -> str:
    """Insert ``[version]: <repo_url>/releases/tag/vversion`` into the trailing
    link-reference block if it is absent. Newest refs sit on top, matching the
    existing descending order. Returns the (possibly unchanged) text."""
    ref_line = f"[{version}]: {repo_url}/releases/tag/v{version}"
    if re.search(rf"(?m)^\[{re.escape(version)}\]:\s", text):
        return text
    ref_pat = re.compile(r"(?m)^\[\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.-]+)?\]:\s")
    m = ref_pat.search(text)
    if m:
        return text[: m.start()] + ref_line + "\n" + text[m.start() :]
    # No link-ref block yet: append one.
    sep = "" if text.endswith("\n") else "\n"
    return text + sep + "\n" + ref_line + "\n"


def group_commits(lines: list[str]) -> dict[str, list[str]]:
    """Group ``subject<NUL>hash`` lines by conventional-commit type."""
    groups: dict[str, list[str]] = {}
    for line in lines:
        if not line.strip():
            continue
        subject, _, _hash = line.partition("\x00")
        m = re.match(r"^(\w+)(?:\([^)]*\))?!?:", subject)
        kind = m.group(1) if m else "other"
        groups.setdefault(kind, []).append(subject)
    return groups


# --------------------------------------------------------------------------- #
# git / repo state
# --------------------------------------------------------------------------- #
def repo_url() -> str:
    try:
        origin = _git_out("remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return DEFAULT_REPO_URL
    # git@github.com:bmad-code-org/bmad-loop.git  ->  https://github.com/bmad-code-org/bmad-loop
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", origin)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git$", "", origin) or DEFAULT_REPO_URL


def current_branch() -> str:
    return _git_out("rev-parse", "--abbrev-ref", "HEAD")


def tag_exists(tag: str) -> bool:
    return (
        _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], check=False).returncode
        == 0
    )


def last_release_tag() -> str | None:
    """Highest ``vX.Y.Z`` tag by version order, or ``None`` for a first release."""
    out = _git_out("tag", "--list", "v*")
    versions: list[tuple[tuple[int, int, int], str, str]] = []
    for tag in out.splitlines():
        try:
            core, suffix = parse_version(tag.lstrip("v"))
        except ValueError:
            continue
        versions.append((core, suffix, tag))
    if not versions:
        return None
    # final > pre-release at the same core; '' must sort high, so map to '~'.
    versions.sort(key=lambda t: (t[0], t[1] or "~"))
    return versions[-1][2]


def dirty_paths() -> list[str]:
    # NUL-delimited and *unstripped*: porcelain status codes lead with a space for
    # worktree-only changes (e.g. " M CHANGELOG.md"), which `.strip()` would corrupt
    # into "M CHANGELOG.md" — dropping the real path's first character.
    out = _run(["git", "status", "--porcelain", "-z"], capture=True).stdout
    return [entry[3:] for entry in out.split("\0") if entry]


def tui_changed_since(tag: str | None) -> bool:
    if tag is None:
        return True  # first release: nothing to diff against, regenerate.
    rng = f"{tag}..HEAD"
    return _run(["git", "diff", "--quiet", rng, "--", TUI_PATH], check=False).returncode != 0


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_commits(args: argparse.Namespace) -> int:
    tag = args.since or last_release_tag()
    rng = f"{tag}..HEAD" if tag else "HEAD"
    out = _git_out("log", "--pretty=format:%s%x00%h", rng)
    groups = group_commits(out.splitlines())
    if not any(groups.values()):
        print(f"(no commits since {tag or 'repo start'})")
        return 0
    order = ["feat", "fix", "perf", "refactor", "docs", "test", "build", "ci", "chore", "other"]
    print(f"commits since {tag or 'repo start'}:")
    for kind in [*order, *(k for k in groups if k not in order)]:
        for subject in groups.get(kind, []):
            print(f"  {subject}")
    return 0


def _regen_assets(dry_run: bool) -> None:
    cmds = [
        ["uv", "run", "--extra", "tui", "python", str(GEN_SCREENSHOTS)],
        ["uv", "run", "--extra", "tui", "python", str(GEN_DEMO)],
    ]
    for cmd in cmds:
        if dry_run:
            print(f"  would run: {' '.join(cmd)}")
        else:
            print(f"  running: {' '.join(cmd)}")
            _run(cmd)


def _reseed_skills(dry_run: bool) -> None:
    """Re-copy the canonical bmad-loop skills into the gitignored dev-workspace
    forks (.claude/skills, .agents/skills) so they pick up the just-stamped
    module.yaml version. Without this the version bump drifts the forks and the
    local `tests/test_module_skills_sync.py` fails until reseeded by hand. The
    forks are gitignored, so nothing here is staged or committed."""
    cmd = ["uv", "run", "python", str(SEED_SKILLS)]
    if dry_run:
        print(f"  would run: {' '.join(cmd)}")
        return
    print("reseeding dev-workspace skill forks ...")
    _run(cmd)


def _run_trunk_fmt(dry_run: bool) -> None:
    if not shutil.which("trunk"):
        print("  trunk not on PATH — skipping fmt (run `trunk check` before pushing)")
        return
    cmd = ["trunk", "fmt", "--no-progress"]
    if dry_run:
        print(f"  would run: {' '.join(cmd)}")
        return
    print(f"  running: {' '.join(cmd)}")
    _run(cmd, check=False)  # formatting is best-effort; don't abort a release on it.


def cmd_prepare(args: argparse.Namespace) -> int:
    version = args.version
    try:
        parse_version(version)
    except ValueError as e:
        _die(str(e))

    tag = f"v{version}"
    canonical = sync_version.read_canonical()
    url = repo_url()
    branch = current_branch()
    last_tag = last_release_tag()

    # --- preconditions ----------------------------------------------------- #
    problems: list[str] = []
    if branch == "main":
        problems.append("on `main`; run prepare from a release/feature branch")
    if tag_exists(tag):
        problems.append(f"tag {tag} already exists")
    if not version_gt(version, canonical):
        problems.append(f"{version} is not greater than the current version {canonical}")
    if not has_curated_section(CHANGELOG.read_text(), version):
        problems.append(
            f"CHANGELOG.md has no non-empty `## [{version}]` section — "
            "curate the release notes there first"
        )
    # Only CHANGELOG.md + regenerated assets are expected to be dirty pre-prepare.
    expected_dirty = {"CHANGELOG.md"}
    unexpected = [
        p for p in dirty_paths() if p not in expected_dirty and not p.startswith(ASSET_PATH)
    ]
    if unexpected and not args.allow_dirty:
        problems.append(
            "unexpected uncommitted changes (commit/stash them, or pass --allow-dirty):\n    "
            + "\n    ".join(unexpected)
        )
    if problems:
        _die("cannot prepare release:\n  - " + "\n  - ".join(problems))

    regen = False if args.no_assets else True if args.force_assets else tui_changed_since(last_tag)
    reason = (
        "disabled (--no-assets)"
        if args.no_assets
        else (
            "forced (--force-assets)"
            if args.force_assets
            else (
                f"{TUI_PATH} changed since {last_tag}"
                if regen
                else f"{TUI_PATH} unchanged since {last_tag} — skipping"
            )
        )
    )

    print(f"prepare {tag} on branch '{branch}' (was {canonical}, last tag {last_tag or 'none'})")
    print(f"assets: {reason}")

    if args.dry_run:
        print("\n[dry-run] planned actions:")
        print(f"  ensure CHANGELOG link ref: [{version}]: {url}/releases/tag/{tag}")
        print(f"  run: python {SYNC_VERSION.relative_to(REPO)} {version}  (+ uv lock)")
        _reseed_skills(dry_run=True)
        if regen:
            _regen_assets(dry_run=True)
        _run_trunk_fmt(dry_run=True)
        print(f"  git add {' '.join(STAMPED_PATHS)}" + (f" {ASSET_PATH}" if regen else ""))
        print(f"  git commit -m 'chore(release): {version} — {_commit_summary(version)}'")
        return 0

    # --- mutate ------------------------------------------------------------ #
    CHANGELOG.write_text(ensure_link_ref(CHANGELOG.read_text(), version, url))

    print(f"stamping version via {SYNC_VERSION.name} ...")
    _run(["uv", "run", "python", str(SYNC_VERSION), version])

    _reseed_skills(dry_run=False)

    if regen:
        print("regenerating screenshots + demo ...")
        _regen_assets(dry_run=False)

    _run_trunk_fmt(dry_run=False)

    to_add = [*STAMPED_PATHS, *([ASSET_PATH] if regen else [])]
    _run(["git", "add", *to_add])
    summary = _commit_summary(version)
    _run(["git", "commit", "-m", f"chore(release): {version} — {summary}"])

    print(f"\ncommitted release prep for {tag}. Next:")
    print(f"  git push -u origin {branch}")
    print("  gh pr create   # then wait for green CI and merge — publish runs automatically")
    return 0


def _commit_summary(version: str) -> str:
    """A short subject tail derived from the first content line of the section."""
    body = extract_section(CHANGELOG.read_text(), version) or ""
    for line in body.splitlines():
        stripped = re.sub(r"^\s*[-*]\s+", "", line)  # drop the bullet marker only
        stripped = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped).strip()  # drop bold markers
        if stripped and not stripped.startswith("#"):
            return (stripped[:60].rstrip() + "…") if len(stripped) > 60 else stripped
    return "version bump + changelog"


def cmd_publish(args: argparse.Namespace) -> int:
    version = sync_version.read_canonical()
    tag = f"v{version}"
    if tag_exists(tag):
        print(f"{tag} already exists — nothing to publish")
        return 0

    notes = extract_section(CHANGELOG.read_text(), version)
    if not notes:
        _die(f"no CHANGELOG `## [{version}]` section — cannot publish release notes")

    sha = _git_out("rev-parse", "HEAD")
    if args.dry_run:
        print(f"[dry-run] would create release {tag} at {sha[:12]} with notes:\n")
        print(notes)
        return 0

    if not shutil.which("gh"):
        _die("`gh` CLI not found — required to create the GitHub release")
    print(f"creating release {tag} at {sha[:12]} ...")
    subprocess.run(
        ["gh", "release", "create", tag, "--target", sha, "--title", tag, "--notes-file", "-"],
        cwd=REPO,
        check=True,
        text=True,
        input=notes,
    )
    print(f"published {tag}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    rc = 0
    print("version-sync:")
    if _run(["uv", "run", "python", str(SYNC_VERSION), "--check"], check=False).returncode != 0:
        rc = 1
    version = sync_version.read_canonical()
    if has_curated_section(CHANGELOG.read_text(), version):
        print(f"changelog: `## [{version}]` section present")
    else:
        print(f"changelog: MISSING `## [{version}]` section", file=sys.stderr)
        rc = 1
    return rc


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="release.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prepare", help="bump + changelog + assets + commit on a branch")
    sp.add_argument("version", help="target version, e.g. 0.5.0")
    sp.add_argument("--dry-run", action="store_true", help="report planned actions, mutate nothing")
    sp.add_argument(
        "--force-assets", action="store_true", help="regenerate assets regardless of TUI diff"
    )
    sp.add_argument("--no-assets", action="store_true", help="never regenerate assets")
    sp.add_argument(
        "--allow-dirty", action="store_true", help="proceed despite unexpected dirty files"
    )
    sp.set_defaults(func=cmd_prepare)

    sc = sub.add_parser("commits", help="list commits since the last tag, grouped by type")
    sc.add_argument("--since", help="base tag/ref (default: last vX.Y.Z tag)")
    sc.set_defaults(func=cmd_commits)

    pp = sub.add_parser("publish", help="create the tag + GitHub release (idempotent)")
    pp.add_argument("--dry-run", action="store_true", help="show the tag + notes, create nothing")
    pp.set_defaults(func=cmd_publish)

    cc = sub.add_parser("check", help="local mirror of the CI release guards")
    cc.set_defaults(func=cmd_check)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
