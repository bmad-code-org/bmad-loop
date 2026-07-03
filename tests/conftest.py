"""Shared fixtures: a sandbox BMAD project with a real git repo, and helpers
that simulate the side effects skill sessions would have on disk."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from automator.adapters.base import SessionResult, SessionSpec
from automator.bmadconfig import ProjectPaths
from automator.verify import rev_parse_head

# The suite reads/writes UTF-8 files (specs, journals, JSON, reports). Windows'
# default text encoding is cp1252, so a plain read_text()/open() throws
# UnicodeDecodeError on any non-ASCII byte — a whole class of "passes on Linux,
# dies on Windows" failures. Require UTF-8 mode on win32 so local runs match CI
# (whose windows job sets PYTHONUTF8=1) instead of failing with cryptic charmap
# errors deep in an unrelated test.
if sys.platform == "win32" and not sys.flags.utf8_mode:
    raise pytest.UsageError(
        "Windows test runs must use UTF-8 mode: set PYTHONUTF8=1 or pass -X utf8 "
        "(e.g. `set PYTHONUTF8=1 && uv run pytest`). The suite assumes UTF-8 to "
        "match the files under test; CI's windows job sets this automatically."
    )


def write_script_launcher(directory: Path, name: str, body: str) -> Path:
    """Write a fake CLI launcher for the host OS."""
    directory = Path(directory)
    sidecar = directory / f"{name}.py"
    sidecar.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = directory / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{sidecar}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{sidecar}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return launcher


# ---- host-shell verify/lifecycle stub commands (single platform-detection spot) ----
# The engine runs verify/plugin-lifecycle commands via the host shell (`sh -c` on
# POSIX, `cmd /c` on Windows), so tests that assert on that machinery need commands
# both shells honor. These build them per-OS in one place instead of each test file
# re-deriving the win32 branch.

_OK = "exit 0"  # cross-platform always-success verb (both `cmd /c` and `sh -c` honor it)
_RUN = "%BMAD_AUTO_RUN_DIR%" if sys.platform == "win32" else "$BMAD_AUTO_RUN_DIR"


def _file_exists_cmd(path) -> str:
    """Shell verify command (run via shell=True) exiting 0 iff `path` exists, on the
    host's shell — `test -f` (POSIX) / `if exist` (Windows cmd) — so the verify-gate
    tests drive the real machinery on either OS, not a POSIX-only `test` that cmd
    rejects with "'test' is not recognized"."""
    if sys.platform == "win32":
        return f'if exist "{path}\\NUL" (exit 1) else if exist "{path}" (exit 0) else (exit 1)'
    return f'test -f "{path}"'


def _touch_run(marker: str) -> str:
    if sys.platform == "win32":
        return f'type nul > "{_RUN}\\{marker}"'
    return f'touch "{_RUN}/{marker}"'


def _exists_run(marker: str) -> str:
    if sys.platform == "win32":
        return (
            f'if exist "{_RUN}\\{marker}\\NUL" (exit 1) '
            f'else if exist "{_RUN}\\{marker}" (exit 0) else (exit 1)'
        )
    return f'test -f "{_RUN}/{marker}"'


def _seeded_then_touch(rel: str, marker: str) -> str:
    if sys.platform == "win32":
        norm_rel = rel.replace("/", "\\")
        return (
            f'if exist "{norm_rel}\\NUL" (exit 1) '
            f'else if exist "{norm_rel}" (type nul > "{_RUN}\\{marker}") else (exit 1)'
        )
    return f'test -f "{rel}" && touch "{_RUN}/{marker}"'


SPRINT_TEMPLATE = {
    "generated": "01-06-2026 10:00",
    "last_updated": "01-06-2026 10:00",
    "project": "sandbox",
    "project_key": "NOKEY",
    "tracking_system": "file-system",
    "development_status": {},
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def _project_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Master sandbox repo, built once per xdist worker. NEVER hand this path to
    a test — a mutation would poison every later test in the worker; tests get
    disposable copies via `project`. (Do not chmod it read-only either: copytree
    preserves modes, so the copies would inherit it and break every write.)"""
    root = tmp_path_factory.mktemp("project-template") / "sandbox"
    impl = root / "_bmad-output" / "implementation-artifacts"
    plan = root / "_bmad-output" / "planning-artifacts"
    impl.mkdir(parents=True)
    plan.mkdir(parents=True)
    (root / "src.txt").write_text("original\n")
    (root / ".gitignore").write_text(".automator/runs/\n")  # as `bmad-auto init` would
    git(root, "init", "-q", "-b", "main")
    # Local config: copies (and their worktrees) inherit it via the copied .git/config.
    git(root, "config", "user.email", "test@test")
    git(root, "config", "user.name", "test")
    git(root, "config", "core.fsync", "none")  # cheapen commits; old git ignores unknown keys
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def project(tmp_path: Path, _project_template: Path) -> ProjectPaths:
    """Git repo with BMAD-shaped artifact dirs and an initial commit — a copytree
    clone of the per-worker template, so no git subprocesses per test (git spawn
    plus fsync made this fixture ~3s per test on Windows CI)."""
    root = tmp_path / "sandbox"
    shutil.copytree(_project_template, root)
    return ProjectPaths(
        project=root,
        implementation_artifacts=root / "_bmad-output" / "implementation-artifacts",
        planning_artifacts=root / "_bmad-output" / "planning-artifacts",
    )


def install_bmad_config(paths: ProjectPaths) -> None:
    """Write the _bmad/bmm/config.yaml that bmadconfig.load_paths resolves."""
    cfg = paths.project / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n"
    )


def install_base_skills(paths: ProjectPaths, trees=(".claude/skills", ".agents/skills")) -> None:
    """Lay down stubs of the non-bundled upstream skills the orchestrator drives
    (bmad-dev-auto + the review hunters) so the run-start preflight
    (`install.missing_base_skills`) passes."""
    from automator.install import BASE_SKILLS

    for tree in trees:
        for skill, markers in BASE_SKILLS.items():
            d = paths.project / tree / skill
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            for marker in markers:
                (d / marker).write_text("x\n", encoding="utf-8")


def write_sprint(paths: ProjectPaths, statuses: dict[str, str]) -> None:
    doc = dict(SPRINT_TEMPLATE)
    doc["development_status"] = dict(statuses)
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def set_sprint(paths: ProjectPaths, key: str, status: str) -> None:
    doc = yaml.safe_load(paths.sprint_status.read_text())
    doc["development_status"][key] = status
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def write_spec(path: Path, status: str, baseline: str, *, prose_status: str | None = None) -> None:
    body = (
        f"---\ntitle: 'test'\ntype: 'feature'\nstatus: '{status}'\n"
        f"baseline_commit: '{baseline}'\n---\n\n## Intent\n\ntest spec\n"
    )
    if prose_status is not None:
        # mirror bmad-dev-auto's terminal finalize: it appends a `## Auto Run
        # Result` prose block (carrying a `Status:` line) but can leave the
        # frontmatter `status` short of the success value — the exact draft-vs-done
        # split that the orchestrator's reconcile repairs.
        body += f"\n## Auto Run Result\n\n- Status: {prose_status}\n\nSummary: test.\n"
    path.write_text(body)


def spec_path(paths: ProjectPaths, story_key: str) -> Path:
    return paths.implementation_artifacts / f"spec-{story_key}.md"


def dev_effect(
    paths: ProjectPaths,
    story_key: str,
    *,
    final_status: str = "done",
    followup_review: bool = True,
    prose_status: str | None = None,
):
    """Simulate a successful bmad-dev-auto session: it self-finalizes the spec
    (no in-review handoff — always straight to ``done``) but never touches the
    automator's sprint board (the orchestrator is the single sprint-status
    writer). ``final_status`` lets a test leave the spec short of the success
    status to exercise the dev-verify gating. ``followup_review`` mirrors the
    skill's `followup_review_recommended` signal (PR #2505) — defaults True so
    the review-flow tests still run the review under the default
    ``review.trigger = "recommended"``; set False to exercise the skip path.
    ``prose_status`` appends a terminal ``## Auto Run Result`` block with that
    Status line — pair it with a non-terminal ``final_status`` to reproduce the
    skill leaving frontmatter behind its prose (the reconcile path)."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        source.write_text(source.read_text() + f"change for {story_key}\n")
        sp = spec_path(paths, story_key)
        write_spec(sp, final_status, baseline, prose_status=prose_status)
        # deliberately NO set_sprint: the dev skill does not write sprint-status
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [],
                "escalations": [],
                "followup_review_recommended": followup_review,
            },
        )

    return effect


# bmad-dev-auto is the sole dev skill, so the generic effect IS the dev effect.
# Alias kept so existing call sites that spell out the decoupled path still read.
generic_dev_effect = dev_effect


def review_effect(
    paths: ProjectPaths, story_key: str, clean: bool, patched: int = 0, finalized: bool = True
):
    """Simulate a follow-up review pass — a bmad-dev-auto re-invocation on the
    done spec (BMAD-METHOD #2508). A review pass always finalizes the spec to
    ``done`` and re-sets `followup_review_recommended`; the orchestrator
    synthesizes the result the same way it does for a dev pass. ``clean=True``
    means the pass no longer recommends a follow-up (the loop converges);
    ``clean=False`` means it still does (the orchestrator loops). ``patched`` is
    accepted for call-site compatibility and otherwise unused.

    ``finalized=False`` leaves the spec at a non-terminal ``in-progress`` status
    (and does not advance the sprint), so when the review budget is exhausted the
    post-loop ``_verify_review`` gate fails — the genuine-non-convergence path
    that defers + rolls back, as opposed to a finalized story that merely keeps
    recommending a follow-up (which the orchestrator now commits)."""

    def effect(spec: SessionSpec) -> SessionResult:
        sp = spec_path(paths, story_key)
        baseline = _spec_baseline(sp)
        status = "done" if finalized else "in-progress"
        write_spec(sp, status, baseline)
        if finalized:
            set_sprint(paths, story_key, "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": status,
                "followup_review_recommended": not clean,
                "escalations": [],
            },
        )

    return effect


def _spec_baseline(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("baseline_commit:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


# ----------------------------------------------------------- sweep helpers


def write_ledger(paths: ProjectPaths, statuses: dict[str, str], commit: bool = True) -> None:
    """Write a DW-format deferred-work ledger; statuses maps id -> status
    value. Committed by default — sweeps start from a clean tree."""
    parts = ["# Deferred Work\n"]
    for dw_id, status in statuses.items():
        parts.append(
            f"### {dw_id}: item {dw_id}\n\norigin: test, 2026-06-01\n"
            f"location: src.txt:1\nreason: test entry.\nstatus: {status}\n"
        )
    paths.deferred_work.write_text("\n".join(parts), encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "ledger")


def mark_ledger_done(paths: ProjectPaths, dw_ids, date: str = "2026-06-11") -> None:
    from automator import deferredwork

    for dw_id in dw_ids:
        deferredwork.mark_done(paths.deferred_work, dw_id, date, "built in test")


def write_legacy_ledger(paths: ProjectPaths, text: str, commit: bool = True) -> None:
    """Write a freeform (pre-DW-format) deferred-work ledger verbatim."""
    paths.deferred_work.write_text(text, encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "legacy ledger")


def migrate_effect(paths: ProjectPaths, new_ledger_text: str, mapping):
    """Simulate a /bmad-auto-sweep --migrate session: rewrites the ledger to
    canonical DW format and reports the manifest-key -> dw_id mapping."""

    def effect(spec: SessionSpec) -> SessionResult:
        paths.deferred_work.write_text(new_ledger_text, encoding="utf-8")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "deferred-sweep-migrate",
                "mapping": list(mapping),
                "escalations": [],
            },
        )

    return effect


def bundle_spec_path(paths: ProjectPaths, name: str) -> Path:
    return paths.implementation_artifacts / f"spec-dw-{name}.md"


def triage_effect(result_json: dict):
    """Simulate a deferred-sweep triage session returning the given result."""

    def effect(spec: SessionSpec) -> SessionResult:
        return SessionResult(status="completed", result_json=result_json)

    return effect


def bundle_dev_effect(
    paths: ProjectPaths,
    name: str,
    dw_ids,
    mark_ledger: bool = False,
    followup_review: bool = True,
    final_status: str = "done",
    prose_status: str | None = None,
):
    """Simulate a bmad-dev-auto bundle dev session: edits code and self-finalizes
    the bundle spec to ``done`` (no in-review handoff). On the decoupled path the
    orchestrator owns the ledger, so by default the session does NOT touch it;
    ``mark_ledger=True`` is kept only for the legacy-marking path in older tests.
    ``followup_review`` mirrors `followup_review_recommended` — defaults True so
    the bundle review runs under the default trigger = "recommended". ``final_status``
    / ``prose_status`` mirror ``dev_effect``: pair a non-terminal ``final_status``
    with ``prose_status="done"`` to reproduce the skill finalizing in prose only."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        source.write_text(source.read_text() + f"change for dw-{name}\n")
        sp = bundle_spec_path(paths, name)
        # mirror the skill: always self-finalize the bundle spec straight to done
        write_spec(sp, final_status, baseline, prose_status=prose_status)
        if mark_ledger:
            mark_ledger_done(paths, dw_ids)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
                "dw_ids": list(dw_ids),
                "followup_review_recommended": followup_review,
            },
        )

    return effect


def bundle_review_effect(paths: ProjectPaths, name: str, clean: bool = True):
    """Simulate a follow-up review pass over a bundle spec — a bmad-dev-auto
    re-invocation on the done bundle spec (no sprint-status entry for bundles).
    ``clean=True`` converges; ``clean=False`` keeps recommending a follow-up."""

    def effect(spec: SessionSpec) -> SessionResult:
        sp = bundle_spec_path(paths, name)
        baseline = _spec_baseline(sp)
        write_spec(sp, "done", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": not clean,
                "escalations": [],
            },
        )

    return effect
