"""Shared fixtures: a sandbox BMAD project with a real git repo, and helpers
that simulate the side effects skill sessions would have on disk."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from bmad_loop import cli, documents
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.checks import ValidationReport
from bmad_loop.journal import save_state
from bmad_loop.model import PAUSE_ESCALATION, Phase, RunState, SessionRecord, StoryTask
from bmad_loop.verify import finalize_commit, rev_parse_head

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


@pytest.fixture
def force_tmux_backend(monkeypatch):
    """Pin the tmux transport backend by name, regardless of host platform.

    External backends discovered via the ``bmad_loop.mux_backends`` entry-point
    scan may match any platform — the herdr adapter matches win32, where tmux
    does not — so on a host with such a package installed ``get_multiplexer()``
    would select it and tests that assert tmux-specific argv/behaviour *through
    the seam* would drive the wrong backend. Forcing
    ``BMAD_LOOP_MUX_BACKEND=tmux`` selects tmux by name (the env override
    bypasses the platform predicate and ``available()``), so these tests stay
    environment-independent. On a stock POSIX box this is a no-op — tmux is
    already the default. The cache is cleared on both ends so the forced choice
    takes effect and does not leak to later tests."""
    from bmad_loop.adapters import multiplexer

    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    multiplexer.get_multiplexer.cache_clear()
    yield
    multiplexer.get_multiplexer.cache_clear()


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
_RUN = "%BMAD_LOOP_RUN_DIR%" if sys.platform == "win32" else "$BMAD_LOOP_RUN_DIR"


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


# ------------------------------------------------ machine-readable CLI output


def machine_json(argv, capsys, *, rc: int = 0, err_contains: str | None = None):
    """Run a `--json` CLI command and parse the WHOLE of stdout — parsing the
    full stream (not a substring) is itself the assertion that nothing but the
    document is printed (the machine.py purity contract).

    `rc` is the expected exit code, and it is not always 0: a command may report
    a negative verdict through its exit status while still owing the caller a
    complete document. Only stdout purity is being asserted here, not success.

    `err_contains` guards the other stream. The default — stderr is *empty* — is
    the strict form and the one to reach for; pass a substring only for a command
    that documents chatter there, as `probe-adapter --json` does by routing its
    human `ok:` trailer to stderr so stdout stays the document alone. That is an
    opt-in to a different assertion, never a waiver: the substring must be
    present, so a trailer that silently moves back to stdout still fails.
    """
    assert cli.main(argv) == rc
    out, err = capsys.readouterr()
    if err_contains is None:
        assert err == ""
    else:
        assert err_contains in err
    return json.loads(out)


def make_validate_document(findings, *, stories_on: bool = False, spec_folder: str = ""):
    """Build a REAL `validate --json` document from (check, severity, message,
    detail) tuples, for tests that need to *stub* one rather than run validate.

    A sibling of machine_json, not an extension of it: that helper drives
    cli.main + capsys to assert stdout purity, so it can only ever produce the
    document a real run happens to emit on the host. Callers here need a chosen
    document (a specific severity mix, a specific detail shape) and no
    subprocess.

    It is built by driving the same ValidationReport -> validate_document path
    the CLI drives, so the shape cannot drift from the contract by being
    hand-written. Going through ValidationReport.add also means its assert
    (checks.py) rejects invented check ids: a test cannot quietly pin behaviour
    to a check that does not exist.
    """
    report = ValidationReport()
    for check, severity, message, detail in findings:
        report.add(check, severity, message, detail)
    return documents.validate_document(report, stories_on, spec_folder)


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
    (root / ".gitignore").write_text(".bmad-loop/runs/\n")  # as `bmad-loop init` would
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


def _write_skill_stubs(skills: Path, catalog: dict) -> None:
    """Stub every skill in `catalog` (an install.py {skill: marker_files} map) under
    `skills`. Reading the catalog instead of restating it means a newly required
    skill or marker file fails the scaffolds loudly rather than drifting."""
    for skill, markers in catalog.items():
        d = skills / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        for marker in markers:
            (d / marker).write_text("x\n", encoding="utf-8")


def install_dev_base_skills(root: Path, tree: str = ".claude/skills", *, folder_id: bool) -> Path:
    """Lay down stubs of the upstream skills the orchestrator drives on every dev run
    (`install.DEV_BASE_SKILLS`: bmad-dev-auto + the review hunters) under
    ``root/tree``, so the run-start preflight (`install.missing_base_skills`) passes.

    ``folder_id`` also writes bmad-dev-auto's step-01 carrying the dispatch marker
    `install.missing_stories_support` content-probes for — stories mode needs a newer
    bmad-dev-auto than file existence alone can prove. Returns the skills tree root."""
    from bmad_loop.install import (
        DEV_BASE_SKILLS,
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        STORIES_PROBE_TEXT,
    )

    skills = Path(root) / tree
    _write_skill_stubs(skills, DEV_BASE_SKILLS)
    if folder_id:
        (skills / STORIES_PROBE_SKILL / STORIES_PROBE_FILE).write_text(
            f"This is a **{STORIES_PROBE_TEXT}** router.\n", encoding="utf-8"
        )
    return skills


def install_base_skills(paths: ProjectPaths, trees=(".claude/skills", ".agents/skills")) -> None:
    """Stub every non-bundled upstream skill (`install.BASE_SKILLS` — a superset of
    DEV_BASE_SKILLS that also covers what a worktree mount must copy) in each of a
    sandbox project's active CLI skill trees. Sprint mode drives any dev primitive,
    so no folder+id probe is written.

    BASE_SKILLS names BOTH primitive eras, so this lays down bmad-build-auto AND
    bmad-dev-auto — a state no real bmm install produces, but the point of this
    scaffold is "nothing the orchestrator may copy is absent". Resolution therefore
    picks bmad-build-auto here; use `install_dev_base_skills` for a pre-rename
    project and `install_dev_shim` for a post-rename one."""
    from bmad_loop.install import BASE_SKILLS

    for tree in trees:
        _write_skill_stubs(paths.project / tree, BASE_SKILLS)


BUILD_AUTO_STEPS = (
    "step-01-clarify-and-route.md",
    "step-02-plan.md",
    "step-03-implement.md",
    "step-04-review.md",
)
# What a renderer-stub SKILL.md looks like since BMAD-METHOD #2601: it delegates to
# the project-local render script instead of carrying the prompt inline.
RENDERER_STUB_SKILL_MD = (
    "# bmad-build-auto\n\nRun:\n\n```bash\nuv run _bmad/scripts/render_skill.py "
    "bmad-build-auto\n```\n"
)


def install_build_auto_skill(
    root: Path,
    tree: str = ".claude/skills",
    *,
    folder_id: bool = True,
    renderer_stub: bool = False,
) -> Path:
    """Lay down a realistic POST-rename dev primitive (`bmad-build-auto`) under
    ``root/tree``: SKILL.md, all four step files, customize.toml with prompt-file
    layers, and a review-prompts/ payload.

    Richer than `_write_skill_stubs` on purpose — the marker files alone can't show
    that a worktree mount carries a skill's *subdirectories*, and `renderer_stub`
    needs a SKILL.md whose content is the thing under test. ``folder_id`` writes the
    dispatch marker into step-01 (stories mode's content probe); ``renderer_stub``
    swaps SKILL.md for the #2601 renderer stub. Returns the skill dir."""
    from bmad_loop.install import DEV_PRIMITIVE_NEW, STORIES_PROBE_FILE, STORIES_PROBE_TEXT

    skill = Path(root) / tree / DEV_PRIMITIVE_NEW
    (skill / "review-prompts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        RENDERER_STUB_SKILL_MD if renderer_stub else f"# {DEV_PRIMITIVE_NEW}\n", encoding="utf-8"
    )
    for step in BUILD_AUTO_STEPS:
        body = f"# {step}\n"
        if folder_id and step == STORIES_PROBE_FILE:
            body = f"This is a **{STORIES_PROBE_TEXT}** router.\n"
        (skill / step).write_text(body, encoding="utf-8")
    (skill / "customize.toml").write_text(
        '[[review_layers]]\nid = "adversarial"\nprompt_file = "review-prompts/adversarial.md"\n',
        encoding="utf-8",
    )
    (skill / "review-prompts" / "adversarial.md").write_text("# adversarial\n", encoding="utf-8")
    return skill


def install_dev_shim(root: Path, tree: str = ".claude/skills") -> Path:
    """Lay down the post-rename FORWARDING SHIM: a lone `bmad-dev-auto/SKILL.md`
    with no step files and no customize.toml.

    This is what upstream leaves behind after the bmad-build-auto rename, and its
    migration gate is interactive — an unattended session dispatched to it HALTs
    without writing anything. Writing only SKILL.md is the whole point: the absent
    markers are what the shim detector keys on. Returns the shim dir."""
    from bmad_loop.install import DEV_PRIMITIVE_LEGACY

    shim = Path(root) / tree / DEV_PRIMITIVE_LEGACY
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "SKILL.md").write_text(
        f"# {DEV_PRIMITIVE_LEGACY}\n\nThis skill has been renamed to bmad-build-auto.\n",
        encoding="utf-8",
    )
    return shim


def attach_profile(adapter, name: str = "claude", project: Path | None = None):
    """Give a scripted adapter the ``profile`` a real CLI adapter carries, so the
    seams that read ``adapter.profile.skill_tree`` — chiefly ``Engine._dev_skill``,
    which resolves the invoked dev-primitive NAME off disk — see a real skill tree.

    `MockAdapter` deliberately has no `profile` at all, and that is not an
    oversight to paper over globally: the profile-less shape IS the None-tree
    fallback path (legacy name), so it stays the default and gets pinned by its
    own test. Attach only where the resolved name is what's under test. Returns
    the adapter for chaining."""
    from bmad_loop.adapters.profile import get_profile

    adapter.profile = get_profile(name, project)
    return adapter


def fault_read_text(monkeypatch, target: Path) -> None:
    """Make exactly ``target``'s ``read_text`` raise PermissionError; every other
    path still reads normally. A selective monkeypatch rather than chmod: chmod is a
    no-op for root and carries no read bit on Windows, so the fault would silently
    not fire on half the CI matrix. ``read_bytes`` is untouched, so a test can still
    assert the faulted file's contents are unchanged."""
    real = Path.read_text

    def fake(self, *a, **kw):
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake)


def write_sprint(paths: ProjectPaths, statuses: dict[str, str]) -> None:
    doc = dict(SPRINT_TEMPLATE)
    doc["development_status"] = dict(statuses)
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def set_sprint(paths: ProjectPaths, key: str, status: str) -> None:
    doc = yaml.safe_load(paths.sprint_status.read_text())
    doc["development_status"][key] = status
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def render_deferred(items) -> str:
    """Render a frontmatter `deferred:` block the way bmad-build-auto's step-04
    writes one (BMAD-METHOD #2640): a YAML list whose free-form values are block
    scalars — `>-` folds the single-line summary/location, `|-` keeps evidence
    verbatim — so a `:`, `#`, quote, or line break inside a finding stays data.

    Rendering the real block scalars (rather than plain `key: value`) is the
    point: the harvest reads through `verify.read_frontmatter`, and a fixture
    that quoted its values would prove only that YAML parses quoted strings.

    A dict item renders that shape, omitting keys it does not carry — so
    `{"evidence": ...}` alone is the missing-summary malformed case. A plain
    string item renders as a bare scalar list entry: the not-a-mapping malformed
    case. Both exist so a test can put malformed items NEXT TO good ones."""
    if not items:
        return "deferred: []\n"
    lines = ["deferred:"]
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"  - {item}")
            continue
        rendered = False
        for key in ("summary", "evidence", "location", "severity"):
            if key not in item:
                continue
            lead = "    " if rendered else "  - "
            rendered = True
            text = str(item[key])
            if not text:
                lines.append(f"{lead}{key}: ''")  # a block scalar cannot be empty
                continue
            if key == "severity":
                lines.append(f"{lead}{key}: {text}")  # a plain enum token upstream
                continue
            lines.append(f"{lead}{key}: {'|-' if key == 'evidence' else '>-'}")
            lines += [f"      {ln}" for ln in text.splitlines()]
        if not rendered:
            lines.append("  - {}")
    return "\n".join(lines) + "\n"


def write_spec(
    path: Path,
    status: str,
    baseline: str,
    *,
    prose_status: str | None = None,
    deferred=None,
) -> None:
    """Write a spec the way the real bmad-dev-auto skill does. The skill's step-03
    stamps `baseline_revision` and NEVER `baseline_commit` (that name exists only
    in the orchestrator's synthesized result.json), so this fixture stamps the
    same key — a reader that only knows `baseline_commit` must fail a test here,
    not sail through production (issue #89).

    ``deferred`` adds the post-#2640 frontmatter `deferred:` list (see
    `render_deferred`); ``None`` — the default — omits the field entirely, which
    is both the pre-#2640 spec shape and the overwhelmingly common post-#2640 one
    (nothing was deferred). Pass ``[]`` for an explicitly empty list."""
    body = (
        f"---\ntitle: 'test'\ntype: 'feature'\nstatus: '{status}'\n"
        f"baseline_revision: '{baseline}'\n"
        f"{render_deferred(deferred) if deferred is not None else ''}"
        f"---\n\n## Intent\n\ntest spec\n"
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


def committing_crash_state(paths: ProjectPaths, engine, *, post_squash: bool = False) -> str:
    """Persist the exact state.json shape from issue #115: a task at COMMITTING
    (the save right after advance(COMMITTING), before finalize_commit / the DONE
    save that stamps commit_sha). Fully verified on disk: attempt work committed
    above baseline (only the work file — sweeping the still-untracked sprint
    board into the commit would make a later baseline reset delete it), spec at
    done, sprint synced at DEV time. review_cycle stays 0 — the
    _skip_review_and_commit path reaches COMMITTING with zero review sessions.
    With post_squash, finalize_commit already ran before the death (squashed
    commit at HEAD, clean tree) but commit_sha was never persisted. Returns the
    baseline sha."""
    baseline = rev_parse_head(paths.project)
    src = paths.project / "src.txt"
    src.write_text(src.read_text() + "change for 1-1-a\n")
    git(paths.project, "add", "src.txt")
    git(paths.project, "commit", "-q", "-m", "attempt work for 1-1-a")
    sp = spec_path(paths, "1-1-a")
    write_spec(sp, "done", baseline)
    write_sprint(paths, {"1-1-a": "done"})
    if post_squash:
        finalize_commit(paths.project, baseline, "pre-crash squash")

    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.COMMITTING, attempt=1)
    task.review_cycle = 0
    task.baseline_commit = baseline
    task.baseline_untracked = []
    task.spec_file = str(sp)
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    return baseline


def dev_effect(
    paths: ProjectPaths,
    story_key: str,
    *,
    final_status: str = "done",
    followup_review: bool = True,
    prose_status: str | None = None,
    seen: list[str] | None = None,
    write_src: bool = True,
    deferred=None,
):
    """Simulate a successful bmad-dev-auto session: it self-finalizes the spec
    (no in-review handoff — always straight to ``done``) but never touches the
    bmad_loop's sprint board (the orchestrator is the single sprint-status
    writer). ``final_status`` lets a test leave the spec short of the success
    status to exercise the dev-verify gating. ``followup_review`` mirrors the
    skill's `followup_review_recommended` signal (PR #2505) — defaults True so
    the review-flow tests still run the review under the default
    ``review.trigger = "recommended"``; set False to exercise the skip path.
    ``prose_status`` appends a terminal ``## Auto Run Result`` block with that
    Status line — pair it with a non-terminal ``final_status`` to reproduce the
    skill leaving frontmatter behind its prose (the reconcile path).

    ``seen``, when given, collects `src.txt` as the session found it on entry — the
    patch-restore tests assert the re-driven session ran against the RESTORED diff.
    ``write_src=False`` then keeps the session from appending its own line, so what
    lands in the tree is exactly what the restore laid down (the applied patch is
    the session's proof of work; a second edit would muddy the assertion).

    ``deferred`` records review findings in the spec's frontmatter the way the
    post-#2640 skill does (the orchestrator harvests them into the ledger); the
    session still never touches `deferred-work.md` itself."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        if seen is not None:
            seen.append(source.read_text())
        if write_src:
            source.write_text(source.read_text() + f"change for {story_key}\n")
        sp = spec_path(paths, story_key)
        write_spec(sp, final_status, baseline, prose_status=prose_status, deferred=deferred)
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
    """Read back whichever baseline key a spec carries: `write_spec` stamps
    `baseline_revision` like the real skill, but hand-rolled fixture specs (and
    re-arm's re-stamp) may carry either."""
    for line in path.read_text().splitlines():
        if line.startswith(("baseline_commit:", "baseline_revision:")):
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
    from bmad_loop import deferredwork

    for dw_id in dw_ids:
        deferredwork.mark_done(paths.deferred_work, dw_id, date, "built in test")


def write_legacy_ledger(paths: ProjectPaths, text: str, commit: bool = True) -> None:
    """Write a freeform (pre-DW-format) deferred-work ledger verbatim."""
    paths.deferred_work.write_text(text, encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "legacy ledger")


def migrate_effect(paths: ProjectPaths, new_ledger_text: str, mapping):
    """Simulate a /bmad-loop-sweep --migrate session: rewrites the ledger to
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
    deferred=None,
):
    """Simulate a bmad-dev-auto bundle dev session: edits code and self-finalizes
    the bundle spec to ``done`` (no in-review handoff). On the decoupled path the
    orchestrator owns the ledger, so by default the session does NOT touch it;
    ``mark_ledger=True`` is kept only for the legacy-marking path in older tests.
    ``followup_review`` mirrors `followup_review_recommended` — defaults True so
    the bundle review runs under the default trigger = "recommended". ``final_status``
    / ``prose_status`` / ``deferred`` mirror ``dev_effect``: pair a non-terminal
    ``final_status`` with ``prose_status="done"`` to reproduce the skill finalizing
    in prose only; ``deferred`` records post-#2640 frontmatter findings."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        source.write_text(source.read_text() + f"change for dw-{name}\n")
        sp = bundle_spec_path(paths, name)
        # mirror the skill: always self-finalize the bundle spec straight to done
        write_spec(sp, final_status, baseline, prose_status=prose_status, deferred=deferred)
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


def bundle_dev_escalates(paths: ProjectPaths, name: str, dw_ids, detail: str = "intent gap"):
    """Simulate a bmad-dev-auto bundle session that hits an intent gap during its
    inline review: it reverts its attempt, saves a patch, writes the bundle spec
    ``blocked``, and surfaces a CRITICAL escalation naming the spec — so the run
    pauses for `bmad-loop resolve --restore-patch`. ``spec_file`` in the result lets
    ``_record_dev_spec`` latch ``task.spec_file`` (the restore re-arm's in-review
    target), and the ``blocked`` status keeps the dw ids open (not synced done)."""

    def effect(spec: SessionSpec) -> SessionResult:
        sp = bundle_spec_path(paths, name)
        baseline = rev_parse_head(paths.project)
        write_spec(sp, "blocked", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "dw_ids": list(dw_ids),
                "escalations": [
                    {"type": "bundle-item-blocked", "severity": "CRITICAL", "detail": detail}
                ],
            },
        )

    return effect


# --------------------------------------------------------- escalated-run scaffolds


@dataclass
class EscalatedRun:
    """What `escalated_run` built, so each caller can unpack only what it asserts on."""

    run_dir: Path
    state: RunState
    task: StoryTask


def escalated_run(
    project: Path,
    run_id: str = "r1",
    *,
    story_key: str = "s1",
    epic: int = 1,
    attempt: int = 1,
    review_cycle: int = 0,
    baseline_commit: str | None = None,
    started_at: str = "now",
    paused_reason: str = "CRITICAL escalation",
    source: str = "sprint-status",
    spec_file: str | None = None,
    restore_patch: str | None = None,
    sentinel_kind: str = "",
    worktree_path: str = "",
    with_session: bool = False,
    git_project: bool = False,
) -> EscalatedRun:
    """A saved RunState paused at a CRITICAL escalation, with one ESCALATED task —
    the shared shape behind test_runs / test_resolve / test_cli, whose three local
    copies had drifted into different defaults, different return tuples, and one
    unique kwarg each. Parameterized as a superset rather than lowest-common-
    denominator: every field a caller relied on is still reachable, so no test's
    fixture-specific assertion is weakened by the dedup.

    ``with_session`` appends the completed review SessionRecord the resolve-context
    builder reads. ``git_project`` makes ``state.project`` a REAL repo (spec files
    already written are committed, run state is gitignored) so `rearm_escalation`'s
    baseline snapshot refresh actually runs and `baseline_commit` defaults to HEAD —
    in a bare tmp_path its best-effort `except` swallows every git call and the
    refresh silently no-ops.
    """
    project = Path(project)
    if git_project:
        (project / ".gitignore").write_text(".bmad-loop/\n")  # keep run state out of the snapshot
        git(project, "init", "-q", "-b", "main")
        git(project, "config", "user.email", "test@test")
        git(project, "config", "user.name", "test")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "initial")
        if baseline_commit is None:
            baseline_commit = git(project, "rev-parse", "HEAD")

    task = StoryTask(
        story_key=story_key,
        epic=epic,
        phase=Phase.ESCALATED,
        attempt=attempt,
        review_cycle=review_cycle,
        baseline_commit=baseline_commit,
        spec_file=spec_file,
        restore_patch=restore_patch,
        sentinel_kind=sentinel_kind,
        worktree_path=worktree_path,
    )
    if with_session:
        task.sessions.append(
            SessionRecord(task_id=f"{story_key}-review-1", role="review", status="completed")
        )
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=started_at,
        paused_reason=paused_reason,
        paused_stage=PAUSE_ESCALATION,
        paused_story_key=story_key,
        tasks={story_key: task},
        source=source,
    )
    run_dir = project / ".bmad-loop" / "runs" / run_id
    save_state(run_dir, state)
    return EscalatedRun(run_dir=run_dir, state=state, task=task)
