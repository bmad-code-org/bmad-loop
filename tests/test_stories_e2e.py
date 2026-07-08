"""Deterministic full-CLI sandbox E2E for stories mode (folder+id dispatch).

Drives the REAL `bmad-loop run/resolve/resume` binaries through REAL tmux with a
scripted fake `claude` wired in as a custom profile — no LLM, no cost, fully
reproducible. The fake fakes both the CLI and its Stop hook (it writes the
SessionStart/Stop event files itself), so no `bmad-loop init` is needed; it
leaves the id-keyed story spec on disk and the `GenericDevAdapter` synthesizes
the result from it (`_stories_result_json`, keyed on `BMAD_LOOP_SPEC_FOLDER`).

The fake routes on the story spec's frontmatter status, like `bmad-dev-auto`
step-01: no spec / `ready-for-dev` → implement to `done`; `BMAD_LOOP_PLAN_HALT`
→ halt at `ready-for-dev` (plan-checkpoint leg); a `.block-<id>` marker → write a
`blocked` spec (a CRITICAL escalation). This exercises the CLI wiring the mock
adapter bypasses: arg parsing, prompt render, hook-signal completion, the
stories read-back, git commit, and the resolve/resume dance.

Covers, through the real binary: (1) two-story happy path, (2) `spec_checkpoint`
two-leg plan-halt + resume, (4) blocked → resolve → re-dispatch, (6)
sprint-mode regression (the new folder+id-capable dev skill installed, yet a
plain sprint run drives dev → verify → commit → sprint-status advance untouched
by the stories wiring), and (7) sprint-mode intent-gap patch-restore (halt saves
the attempt as a patch → `resolve --restore-patch` re-arms to in-review +
re-stamps the spec baseline → resume re-applies the patch and dispatches an
explicit spec pointer, resuming review instead of re-implementing). Scenarios
(3) `done_checkpoint` and (5) worktree isolation are covered deterministically
at the engine level in test_stories_engine.py; here we prove the end-to-end CLI
stack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HAVE_TMUX = sys.platform != "win32" and shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="stories E2E needs real tmux")

# The fake CLI: reads the story id + spec folder from the session env (as the real
# folder+id adapter does), writes the id-keyed story spec BEFORE the Stop event so
# the adapter's read-back finds a terminal spec, and stays alive until the engine
# kills its window. Routes on the existing spec status so one script drives a fresh
# dispatch, a plan-halt leg, its post-checkpoint implement leg, and a blocked halt.
FAKE_CLI = r"""#!/usr/bin/env bash
set -e
rd="$BMAD_LOOP_RUN_DIR"; tid="$BMAD_LOOP_TASK_ID"
story="$BMAD_LOOP_STORY_KEY"; folder="$BMAD_LOOP_SPEC_FOLDER"
prompt="${1:-}"
ts=$(date +%s%N)
mkdir -p "$rd/events"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts" "$tid" > "$rd/events/$ts-$tid-SessionStart.json"
baseline=$(git rev-parse HEAD)

# SPRINT mode (no BMAD_LOOP_SPEC_FOLDER in env): the folder+id-capable skill is
# installed, but a plain sprint run must still work. Write the result artifact
# the orchestrator scans by mtime under implementation-artifacts, make a real
# code change, and Stop — the orchestrator (not the skill) advances sprint-status.
# Routes like step-01: an in-review spec is a patch-restore re-drive (#2564); a
# committed `.intent-gap-<story>` marker makes the first dispatch halt the way
# bmad-dev-auto's review does on an intent gap (save attempt as patch, revert,
# block); otherwise a plain implement-to-done.
if [ -z "$folder" ]; then
    impl="_bmad-output/implementation-artifacts"
    mkdir -p "$impl"
    spec="$impl/spec-$story.md"
    patch="$impl/attempt-$story.patch"
    status=""
    [ -f "$spec" ] && status=$(sed -n 's/^status:[[:space:]]*//p' "$spec" | head -1 | tr -d "'\" ")
    if [ "$status" = "in-review" ]; then
        # Patch-restore re-drive: resume REVIEW on the restored diff — never
        # re-implement. Enforce the two orchestrator-side contracts the way the
        # real step-01 would: the prompt must point at the spec explicitly (an
        # in-review spec only routes to step-04 through the spec-pointer intent
        # check), and the attempted change must already be back on the tree.
        if ! printf '%s' "$prompt" | grep -qF "$spec"; then
            printf -- '---\ntitle: %s\nstatus: blocked\nbaseline_commit: %s\n---\n\n## Auto Run Result\n\n- Status: blocked\n\nprompt lacks the spec pointer.\n' \
                "$story" "$baseline" > "$spec"
        elif ! grep -q "attempted reading" src.txt; then
            printf -- '---\ntitle: %s\nstatus: blocked\nbaseline_commit: %s\n---\n\n## Auto Run Result\n\n- Status: blocked\n\ntree was not restored.\n' \
                "$story" "$baseline" > "$spec"
        else
            printf -- '---\ntitle: %s\nstatus: done\nbaseline_commit: %s\n---\n\n## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n\nSummary: reviewed the restored change.\n' \
                "$story" "$baseline" > "$spec"
        fi
    elif [ -f ".intent-gap-$story" ] && [ ! -f "$patch" ]; then
        echo "attempted reading for $story" >> src.txt
        git diff HEAD > "$patch"
        git checkout -- src.txt
        printf -- '---\ntitle: %s\nstatus: blocked\nbaseline_commit: %s\n---\n\n## Intent\n\nx\n\n## Auto Run Result\n\n- Status: blocked\n\nintent gap; saved patch: %s\n' \
            "$story" "$baseline" "$patch" > "$spec"
    else
        echo "impl for $story" >> src.txt
        printf -- '---\ntitle: %s\nstatus: done\nbaseline_commit: %s\n---\n\n## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n\nSummary: sprint.\n' \
            "$story" "$baseline" > "$spec"
    fi
    ts2=$(( ts + 1 ))
    printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \
        "$ts2" "$tid" > "$rd/events/$ts2-$tid-Stop.json"
    sleep 30
    exit 0
fi

sdir="$folder/stories"
mkdir -p "$sdir"
spec="$sdir/$story-slug.md"
existing=$(ls "$sdir/$story"-*.md 2>/dev/null | head -1 || true)
status=""
[ -n "$existing" ] && status=$(sed -n 's/^status:[[:space:]]*//p' "$existing" | head -1 | tr -d "'\" ")

write_done() {
    echo "impl for $story" >> src.txt
    printf -- '---\ntitle: %s\nstatus: done\nbaseline_commit: %s\n---\n\n# %s\nimplemented.\n' \
        "$story" "$baseline" "$story" > "$spec"
}
write_planned() {
    printf -- '---\ntitle: %s\nstatus: ready-for-dev\nbaseline_commit: %s\n---\n\n# %s\nplanned.\n' \
        "$story" "$baseline" "$story" > "$spec"
}
write_blocked() {
    printf -- '---\ntitle: %s\nstatus: blocked\nbaseline_commit: %s\n---\n\n# %s\n\n## Auto Run Result\n\n- Status: blocked\n\nNeeds a human decision.\n' \
        "$story" "$baseline" "$story" > "$spec"
}

if [ "$status" = "ready-for-dev" ] || [ "$status" = "in-progress" ] || [ "$status" = "draft" ]; then
    write_done                       # re-dispatch after a plan-checkpoint or a re-arm
elif [ -n "$BMAD_LOOP_PLAN_HALT" ]; then
    write_planned                    # spec_checkpoint leg 1: halt after planning
elif [ -f "$folder/.block-$story" ]; then
    write_blocked                    # poisoned story: first dispatch blocks
else
    write_done                       # normal fresh dispatch
fi

ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts2" "$tid" > "$rd/events/$ts2-$tid-Stop.json"
sleep 30
"""

PROFILE_TOML = """\
name = "fakestories"
binary = "{binary}"
bypass_args = []
usage_parser = "none"
skill_tree = ".claude/skills"

[hooks]
dialect = "claude-settings-json"
config_path = ".claude/settings.json"
events = {{ SessionStart = "SessionStart", Stop = "Stop" }}
"""

SPEC_FOLDER = "_bmad-output/epic-1"
CLI = [sys.executable, "-m", "bmad_loop.cli"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _entry(story_id: str, **over) -> dict:
    d = {"id": story_id, "title": f"Story {story_id}", "description": "does a thing"}
    d.update(over)
    return d


def _scaffold(root: Path, entries: list[dict]) -> None:
    """A committed, clean sandbox: git repo, BMAD config + artifact dirs, the
    base-skill stubs the stories preflight requires (incl. the folder+id dispatch
    probe), a stories.yaml + SPEC.md, the fake-CLI profile, and a stories-mode
    policy — everything committed so the run-start worktree_clean gate passes."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "src.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".bmad-loop/runs/\n", encoding="utf-8")

    cfg = root / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n",
        encoding="utf-8",
    )
    for sub in ("implementation-artifacts", "planning-artifacts"):
        (root / "_bmad-output" / sub).mkdir(parents=True, exist_ok=True)
        (root / "_bmad-output" / sub / ".keep").write_text("", encoding="utf-8")

    # base skills (DEV_BASE_SKILLS) + the folder+id dispatch content probe
    skills = root / ".claude" / "skills"
    dev = skills / "bmad-dev-auto"
    dev.mkdir(parents=True)
    (dev / "SKILL.md").write_text("# bmad-dev-auto\n", encoding="utf-8")
    (dev / "step-04-review.md").write_text("x\n", encoding="utf-8")
    (dev / "customize.toml").write_text("# review layers\n", encoding="utf-8")
    (dev / "step-01-clarify-and-route.md").write_text(
        "This is a **folder+id dispatch** router.\n", encoding="utf-8"
    )
    for hunter in (
        "bmad-review-adversarial-general",
        "bmad-review-edge-case-hunter",
        "bmad-review-verification-gap",
    ):
        (skills / hunter).mkdir(parents=True)
        (skills / hunter / "SKILL.md").write_text(f"# {hunter}\n", encoding="utf-8")

    folder = root / SPEC_FOLDER
    (folder / "stories").mkdir(parents=True)
    (folder / "SPEC.md").write_text("---\ntitle: Epic 1\n---\n# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")

    fake = root / ".bmad-loop" / "fake-cli.sh"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(FAKE_CLI, encoding="utf-8")
    os.chmod(fake, 0o755)
    profiles = root / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "fakestories.toml").write_text(
        PROFILE_TOML.format(binary=str(fake)), encoding="utf-8"
    )
    (root / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "fakestories"\n\n'
        "[review]\nenabled = false\n\n"
        f'[stories]\nsource = "stories"\nspec_folder = "{SPEC_FOLDER}"\n',
        encoding="utf-8",
    )

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "e2e@test")
    _git(root, "config", "user.name", "e2e")
    _git(root, "config", "core.fsync", "none")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")


def _scaffold_sprint(root: Path, story_key: str) -> None:
    """A committed, clean SPRINT-mode sandbox carrying the SAME new folder+id-
    capable bmad-dev-auto skill stub as `_scaffold` (with the folder+id dispatch
    probe content) — the regression point: installing that skill must not disturb
    the default sprint path. sprint-status.yaml holds one ready-for-dev story; the
    policy has NO [stories] section, so the run is plain sprint mode."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "src.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".bmad-loop/runs/\n", encoding="utf-8")

    cfg = root / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n",
        encoding="utf-8",
    )
    impl = root / "_bmad-output" / "implementation-artifacts"
    for sub in ("implementation-artifacts", "planning-artifacts"):
        (root / "_bmad-output" / sub).mkdir(parents=True, exist_ok=True)
        (root / "_bmad-output" / sub / ".keep").write_text("", encoding="utf-8")

    # the SAME new folder+id-capable skill stub the stories scaffold installs
    skills = root / ".claude" / "skills"
    dev = skills / "bmad-dev-auto"
    dev.mkdir(parents=True)
    (dev / "SKILL.md").write_text("# bmad-dev-auto\n", encoding="utf-8")
    (dev / "step-04-review.md").write_text("x\n", encoding="utf-8")
    (dev / "customize.toml").write_text("# review layers\n", encoding="utf-8")
    (dev / "step-01-clarify-and-route.md").write_text(
        "This is a **folder+id dispatch** router.\n", encoding="utf-8"
    )
    for hunter in (
        "bmad-review-adversarial-general",
        "bmad-review-edge-case-hunter",
        "bmad-review-verification-gap",
    ):
        (skills / hunter).mkdir(parents=True)
        (skills / hunter / "SKILL.md").write_text(f"# {hunter}\n", encoding="utf-8")

    sprint = {
        "generated": "01-06-2026 10:00",
        "last_updated": "01-06-2026 10:00",
        "project": "sandbox",
        "project_key": "NOKEY",
        "tracking_system": "file-system",
        "development_status": {story_key: "ready-for-dev"},
    }
    (impl / "sprint-status.yaml").write_text(
        yaml.safe_dump(sprint, sort_keys=False), encoding="utf-8"
    )

    fake = root / ".bmad-loop" / "fake-cli.sh"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(FAKE_CLI, encoding="utf-8")
    os.chmod(fake, 0o755)
    profiles = root / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "fakestories.toml").write_text(
        PROFILE_TOML.format(binary=str(fake)), encoding="utf-8"
    )
    (root / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "fakestories"\n\n'
        "[review]\nenabled = false\n\n"
        '[gates]\nmode = "none"\n',
        encoding="utf-8",
    )

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "e2e@test")
    _git(root, "config", "user.name", "e2e")
    _git(root, "config", "core.fsync", "none")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")


def _sprint_status(root: Path, story_key: str) -> str:
    doc = yaml.safe_load(
        (root / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml").read_text(
            encoding="utf-8"
        )
    )
    return doc.get("development_status", {}).get(story_key, "?")


def _run(root: Path, *args: str, timeout: float = 150) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CLI, args[0], "--project", str(root), *args[1:]],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(root),
    )


def _status(root: Path, story_id: str) -> str:
    spec = root / SPEC_FOLDER / "stories" / f"{story_id}-slug.md"
    if not spec.is_file():
        return "pending"
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return "?"


def _commit_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _run_id(root: Path) -> str:
    runs = sorted((root / ".bmad-loop" / "runs").iterdir())
    assert runs, "no run dir created"
    return runs[-1].name


def test_e2e_two_story_happy_path(tmp_path):
    root = tmp_path / "sbx"
    _scaffold(root, [_entry("1"), _entry("2")])
    base = _commit_count(root)

    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _status(root, "1") == "done"
    assert _status(root, "2") == "done"
    # one squashed story commit per story above the sandbox baseline
    assert _commit_count(root) == base + 2


def test_e2e_spec_checkpoint_two_leg(tmp_path):
    root = tmp_path / "sbx"
    _scaffold(root, [_entry("1", spec_checkpoint=True)])
    base = _commit_count(root)

    # leg 1: dispatch halts after planning → run pauses at the plan checkpoint
    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _status(root, "1") == "ready-for-dev"  # planned, not implemented
    assert _commit_count(root) == base  # no commit yet

    run_id = _run_id(root)
    st = _run(root, "status", run_id)
    # deterministic status line: `PAUSED (plan-checkpoint) — …` — assert BOTH the
    # paused state and the specific stage, not either-or (a weak `or` would pass on
    # any paused run regardless of stage).
    out = st.stdout.lower()
    assert "paused" in out and "plan-checkpoint" in out

    # leg 2: resume re-dispatches straight to implementation → done + commit
    resume = _run(root, "resume", run_id)
    assert resume.returncode == 0, resume.stderr or resume.stdout
    assert _status(root, "1") == "done"
    assert _commit_count(root) == base + 1


def test_e2e_blocked_resolve_redispatch(tmp_path):
    root = tmp_path / "sbx"
    _scaffold(root, [_entry("1"), _entry("2")])
    (root / SPEC_FOLDER / ".block-1").write_text("", encoding="utf-8")  # story 1 poisoned
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "poison story 1")
    base = _commit_count(root)

    # story 1 blocks → run pauses at the escalation (story 2 not leapfrogged)
    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _status(root, "1") == "blocked"
    assert _status(root, "2") == "pending"
    run_id = _run_id(root)

    # resolve (non-interactive) re-arms blocked → ready-for-dev + strips the halt
    resolve = _run(root, "resolve", run_id, "--no-interactive", "--no-resume")
    assert resolve.returncode == 0, resolve.stderr or resolve.stdout
    assert _status(root, "1") == "ready-for-dev"

    # resume re-dispatches story 1 to done and continues to story 2
    resume = _run(root, "resume", run_id)
    assert resume.returncode == 0, resume.stderr or resume.stdout
    assert _status(root, "1") == "done"
    assert _status(root, "2") == "done"
    assert _commit_count(root) == base + 2


def test_e2e_sprint_intent_gap_patch_restore(tmp_path):
    # Scenario 7 (review F1/F2, end-to-end): a sprint-mode intent-gap halt saves
    # the attempted change as a patch and reverts; `resolve --restore-patch`
    # re-arms the spec to in-review and re-stamps its baseline; resume re-applies
    # the patch onto the tree and dispatches an EXPLICIT spec pointer, so the
    # (fake) skill resumes review on the restored diff instead of re-implementing.
    # The fake blocks loudly if the prompt lacks the pointer or the tree was not
    # restored, so a `done` landing proves both contracts held.
    root = tmp_path / "sbx"
    story = "1-1-thing"
    _scaffold_sprint(root, story)
    (root / f".intent-gap-{story}").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "poison: intent gap")
    base = _commit_count(root)
    impl = root / "_bmad-output" / "implementation-artifacts"
    spec = impl / f"spec-{story}.md"
    patch = impl / f"attempt-{story}.patch"

    # leg 1: the dev session halts on the intent gap — patch saved, tree reverted
    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "status: blocked" in spec.read_text(encoding="utf-8")
    assert patch.is_file()  # the attempted change survives the revert
    assert "attempted reading" not in (root / "src.txt").read_text(encoding="utf-8")
    run_id = _run_id(root)

    # the human confirms the attempted reading: latch the restore
    resolve = _run(
        root, "resolve", run_id, "--no-interactive", "--no-resume", "--restore-patch", str(patch)
    )
    assert resolve.returncode == 0, resolve.stderr or resolve.stdout
    text = spec.read_text(encoding="utf-8")
    assert "status: in-review" in text  # restore routing: step-01 -> step-04
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert f"baseline_revision: {head}" in text  # F2: spec baseline re-stamped

    # resume: patch re-applied, review resumed, story lands done + committed
    resume = _run(root, "resume", run_id)
    assert resume.returncode == 0, resume.stderr or resume.stdout
    final = spec.read_text(encoding="utf-8")
    assert "status: done" in final, final  # fake blocks loudly on a broken contract
    assert _sprint_status(root, story) == "done"
    assert _commit_count(root) == base + 1
    src = (root / "src.txt").read_text(encoding="utf-8")
    assert src.count("attempted reading") == 1  # restored from the patch, not re-implemented
    # F1: the re-drive dispatch pointed at the spec, never the bare story key
    run_dir = root / ".bmad-loop" / "runs" / run_id
    prompts = [p.read_text(encoding="utf-8") for p in (run_dir / "tasks").glob("*/prompt.txt")]
    assert any(str(spec) in p for p in prompts)


def test_e2e_sprint_mode_regression(tmp_path):
    # Scenario 6 (audit MAJOR-2): the new folder+id-capable bmad-dev-auto skill is
    # installed, but this is a plain SPRINT-mode run. It must drive dev → verify →
    # commit and let the orchestrator advance sprint-status to done through the
    # real CLI — unaffected by the stories wiring (the adapter's
    # BMAD_LOOP_SPEC_FOLDER read-back branch, the per-session env exports, etc.).
    root = tmp_path / "sbx"
    story = "1-1-thing"
    _scaffold_sprint(root, story)
    base = _commit_count(root)

    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _sprint_status(root, story) == "done"
    assert _commit_count(root) == base + 1
