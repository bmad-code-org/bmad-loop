"""Deterministic full-CLI sandbox E2E for tickets mode (the BMAD v7 ticket tree).

Drives the REAL `bmad-loop run/resume/status` binaries through REAL tmux against
a v7 sandbox carrying the vendored upstream `tickets.py`, with a scripted fake
`bmad-build-auto` wired in as a custom profile: no LLM, no cost, fully
reproducible. Like `tests/test_stories_e2e.py`'s fake, it writes its own
SessionStart/Stop event files, so no `bmad-loop init` is needed.

The fake resolves its ticket the way upstream's build-auto step-01 does, through
the project's `tickets.py find <ref>`, then routes on the plan's status (the
build-auto resume table): no plan, `draft`, `ready-for-dev` or `in-progress` →
build to `built` and commit its own work; a prompt carrying `Halt after
planning.` → stop at `ready-for-dev`; a committed `.block-<ref>` marker → `mark
<ref> blocked`; `built`/`done` → a follow-up pass with no status change. The
`/bmad-retrospective -H <slug>` prompt writes the epic's retrospective document
with the verdict a `.retro-verdict-<slug>` marker names (default `accepted`). It
NEVER runs `mark done`: that write belongs to bmad-loop.

Rows: (1) two tickets marked `done` by bmad-loop inside each unit commit, (2)
`mark_done = "never"` leaves both at `built` and the second ticket still runs,
(3) a hitl ticket pauses before any session for it, (4) a ticket that needs
refining pauses before any session at all, (5) the unrefined two-leg and the
refined pre-dispatch `plan_checkpoint`, (6) `done_checkpoint` pauses after the
commit. A run that finishes an epic it committed a ticket of always ends paused
at `epic-boundary` for a person to close the epic; these rows assert that end
state and leave the closure flow to the epic-boundary rows.

Real `tickets.py` spawns: every run spawns it through bmad-loop and every fake
session once more; outside them this file spawns it twice (rows 3 and 4). Linux
and real tmux only, so none of this reaches the Windows legs' budget.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    V7_GITIGNORE,
    EntrySpec,
    EpicSpec,
    V7Layout,
    install_bmad_v7,
    install_build_auto_v7_skill,
    real_mux_e2e,
    ticket_slug,
    write_ticket_tree,
)

from bmad_loop.frontmatter import parse_frontmatter, status_of

# Linux-only, not merely non-win32: the fake is bash + GNU coreutils (`date +%s%N`).
HAVE_TMUX = sys.platform.startswith("linux") and shutil.which("tmux") is not None
# A LIST, not a single mark: every real-tmux test joins the serialized real-mux xdist
# group (tests/test_conftest.py's grouping guard enforces it).
pytestmark = [
    pytest.mark.skipif(not HAVE_TMUX, reason="tickets E2E needs real tmux on Linux"),
    real_mux_e2e,
]

# The fake build-auto. `@PY@` is baked in by the scaffold (tmux does not carry the
# test env, so a bare `python` could be any interpreter); `@INIT@` is the initiative
# folder relative to the project, where the retrospective lands. Records the argv
# it received (`fake-prompt.txt`) and the plan-halt env (`fake-plan-halt.txt`), so a
# row can prove what reached the binary rather than what bmad-loop meant to send.
FAKE_BUILD_AUTO = r"""#!/usr/bin/env bash
set -e
PY=@PY@
rd="$BMAD_LOOP_RUN_DIR"; tid="$BMAD_LOOP_TASK_ID"; ref="$BMAD_LOOP_STORY_KEY"
prompt="${1:-}"
ts=$(date +%s%N)
ed="$BMAD_LOOP_EVENTS_DIR"
td="$rd/tasks/$tid"
mkdir -p "$ed" "$td"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts" "$tid" > "$ed/$ts-$tid-SessionStart.json"
printf '%s' "$prompt" > "$td/fake-prompt.txt"
printf '%s' "${BMAD_LOOP_PLAN_HALT:-}" > "$td/fake-plan-halt.txt"
baseline=$(git rev-parse HEAD)

stop() {
    ts2=$(( ts + 1 ))
    printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \
        "$ts2" "$tid" > "$ed/$ts2-$tid-Stop.json"
    sleep 30
    exit 0
}

tickets() {
    "$PY" -I -B -X utf8 _bmad/method/scripts/tickets.py --project-root "$PWD" "$@"
}

# The epic retrospective (`gates.retrospective = "auto"`): the document bmad-loop
# verifies, then the result.json its generic adapter reads.
if printf '%s' "$prompt" | grep -q "bmad-retrospective"; then
    slug=$(printf '%s' "$prompt" | grep -o 'epic-[A-Za-z0-9_-]*' | head -1)
    verdict=accepted
    [ -f ".retro-verdict-$slug" ] && verdict=$(tr -d '[:space:]' < ".retro-verdict-$slug")
    printf -- '---\nepic: %s\nverdict: %s\n---\n\n# %s retrospective\n' \
        "$slug" "$verdict" "$slug" > "@INIT@/$slug/$slug-retrospective.md"
    printf '%s' '{"workflow": "bmad-retrospective", "status": "done"}' > "$td/result.json"
    stop
fi

# `ticket <ref>`: resolve the plan through the tree, as upstream step-01 does.
found=$(tickets find "$ref")
plan=$(printf '%s' "$found" | "$PY" -I -c 'import json, sys; print(json.load(sys.stdin)["plan"])')
id=$(printf '%s' "$found" | "$PY" -I -c 'import json, sys; print(json.load(sys.stdin)["id"])')
status=""
[ -f "$plan" ] && status=$(sed -n 's/^status:[[:space:]]*//p' "$plan" | head -1 | tr -d "'\" ")

write_plan() {
    printf -- '---\nticket: %s\nstatus: %s\nbaseline_revision: %s\n---\n\n# %s plan\n\n## Auto Run Result\n\nStatus: %s\n' \
        "$id" "$1" "$baseline" "$ref" "$1" > "$plan"
}
build() {
    echo "impl for $ref" >> src.txt
    write_plan built
    git add -A
    git commit -q -m "build $ref"
}

case "$status" in
    built|done)
        ;;                           # a fresh follow-up pass: no status change
    ready-for-dev|in-progress)
        build ;;                     # the leg after a plan checkpoint, or a re-arm
    *)
        if printf '%s' "$prompt" | grep -qF "Halt after planning."; then
            write_plan ready-for-dev     # plan_checkpoint leg 1
        elif [ -f ".block-$ref" ]; then
            printf -- '---\nticket: %s\nstatus: in-progress\nbaseline_revision: %s\n---\n\n# %s plan\n' \
                "$id" "$baseline" "$ref" > "$plan"
            tickets mark "$ref" blocked --blocked "needs a human decision" > /dev/null
            printf '\n## Auto Run Result\n\nStatus: blocked\n\nNeeds a human decision.\n' >> "$plan"
        else
            build
        fi ;;
esac
stop
"""

PROFILE_TOML = """\
name = "faketickets"
binary = "{binary}"
bypass_args = []
usage_parser = "none"
skill_tree = ".claude/skills"

[hooks]
dialect = "claude-settings-json"
config_path = ".claude/settings.json"
events = {{ SessionStart = "SessionStart", Stop = "Stop" }}
"""

CLI = [sys.executable, "-m", "bmad_loop.cli"]
EPIC = "epic-cart"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _epic(*entries: EntrySpec) -> tuple[EpicSpec, ...]:
    return (EpicSpec(1, EPIC, "Cart", entries),)


def _scaffold(root: Path, epics: tuple[EpicSpec, ...], *, mark_done: str = "auto") -> V7Layout:
    """A committed, clean v7 sandbox: the vendored `tickets.py` install with the
    personal config layer gitignored (as `bmad setup` leaves it), ``epics`` as a
    tracked tree in the checkout (layout T1), the v7 build-auto stub, the fake's
    profile and a tickets-mode policy. Review is off and the retrospective gate is
    `never`, so every session is a dev session of the fake."""
    root.mkdir(parents=True)
    (root / "src.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(V7_GITIGNORE, encoding="utf-8")
    layout = install_bmad_v7(root)
    write_ticket_tree(layout, epics)
    install_build_auto_v7_skill(root)

    init_rel = layout.initiative_folder.relative_to(root).as_posix()
    fake = root / ".bmad-loop" / "fake-build-auto.sh"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        FAKE_BUILD_AUTO.replace("@PY@", shlex.quote(sys.executable)).replace("@INIT@", init_rel),
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    profiles = root / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "faketickets.toml").write_text(
        PROFILE_TOML.format(binary=str(fake)), encoding="utf-8"
    )
    (root / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "faketickets"\n\n'
        "[review]\nenabled = false\n\n"
        '[gates]\nretrospective = "never"\n\n'
        '[stories]\nsource = "tickets"\n\n'
        f'[tickets]\nmark_done = "{mark_done}"\n',
        encoding="utf-8",
    )

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "e2e@test")
    _git(root, "config", "user.name", "e2e")
    _git(root, "config", "core.fsync", "none")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")
    return layout


def _run(root: Path, *args: str, timeout: float = 150) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CLI, args[0], "--project", str(root), *args[1:]],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(root),
    )


def _ok(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0, proc.stderr or proc.stdout


def _tickets(layout: V7Layout, *argv: str) -> None:
    """The person's own `tickets.py` call (one real spawn)."""
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            str(layout.tickets_script),
            "--project-root",
            str(layout.project),
            *argv,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _plan(layout: V7Layout, title: str) -> Path:
    return layout.initiative_folder / EPIC / f"story-{ticket_slug(title)}-plan.md"


def _plan_status(layout: V7Layout, title: str) -> str:
    """A plan's status through the frontmatter reader (never raw bytes: upstream may
    change how `mark` quotes values); "absent" when no plan exists."""
    plan = _plan(layout, title)
    if not plan.is_file():
        return "absent"
    return status_of(parse_frontmatter(plan.read_text(encoding="utf-8")))


def _committed_status(layout: V7Layout, rev: str, title: str) -> str:
    rel = _plan(layout, title).relative_to(layout.project).as_posix()
    return status_of(parse_frontmatter(_git(layout.project, "show", f"{rev}:{rel}")))


def _commit_count(root: Path) -> int:
    return int(_git(root, "rev-list", "--count", "HEAD").strip())


def _run_dir(root: Path) -> Path:
    runs = sorted((root / ".bmad-loop" / "runs").iterdir())
    assert runs, "no run dir created"
    return runs[-1]


def _status_doc(root: Path) -> dict:
    proc = _run(root, "status", _run_dir(root).name, "--json")
    _ok(proc)
    return json.loads(proc.stdout)


def _prompts(root: Path) -> list[str]:
    """Every dispatch the fake received, in launch order (the fake writes its
    prompt file once, on start)."""
    recorded = (_run_dir(root) / "tasks").glob("*/fake-prompt.txt")
    return [
        p.read_text(encoding="utf-8") for p in sorted(recorded, key=lambda p: p.stat().st_mtime_ns)
    ]


def _journal(root: Path, kind: str) -> list[dict]:
    lines = (_run_dir(root) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    return [row for row in map(json.loads, lines) if row["kind"] == kind]


def _assert_ended_at_epic_boundary(doc: dict) -> None:
    """The end of every row that finishes the epic: a closure pause for the person
    who closes the epic with bmad-ticket (bmad-loop never writes the epic file)."""
    assert doc["status"] == "paused", doc
    assert doc["paused_stage"] == "epic-boundary", doc
    assert doc["tickets"]["epic_closures"] == {EPIC: "unknown"}, doc["tickets"]


def test_e2e_tickets_happy_path_marks_done(tmp_path):
    """Two tickets, `mark_done = "auto"`: the fake leaves each plan `built`, bmad-loop
    marks it `done` inside the unit's commit window, so each squashed unit commit
    carries its own plan at `done` with the ticket trailers. Ablation: make the
    commit window skip the mark (`_terminal_plan_write` returning early) and the
    plans stay `built`."""
    layout = _scaffold(
        tmp_path / "sbx", _epic(EntrySpec(1, "Cart model"), EntrySpec(2, "Cart rules", after=(1,)))
    )
    root = layout.project
    base = _commit_count(root)

    _ok(_run(root, "run"))

    assert _plan_status(layout, "Cart model") == "done"
    assert _plan_status(layout, "Cart rules") == "done"
    assert _commit_count(root) == base + 2  # one squashed unit commit per ticket
    for rev, title, ref in (("HEAD~1", "Cart model", "1.1"), ("HEAD", "Cart rules", "1.2")):
        rel = _plan(layout, title).relative_to(root).as_posix()
        assert rel in _git(root, "show", "--name-only", "--format=", rev).split()
        assert _committed_status(layout, rev, title) == "done"
        body = _git(root, "show", "-s", "--format=%B", rev)
        assert f"Bmad-Ticket: {ref}" in body and f"Bmad-Plan: {rel}" in body
    prompts = _prompts(root)
    assert [p.split(" — ")[0] for p in prompts] == [
        "/bmad-build-auto ticket 1.1",
        "/bmad-build-auto ticket 1.2",
    ], prompts

    doc = _status_doc(root)
    assert doc["source"] == "tickets"
    assert [t["ticket"]["ref"] for t in doc["tasks"]] == ["1.1", "1.2"]
    _assert_ended_at_epic_boundary(doc)


def test_e2e_tickets_mark_done_never_leaves_built(tmp_path):
    """`mark_done = "never"`: both plans stay `built`, the unit commits carry them at
    `built`, and 1.2 still runs, because `next` counts a ticket in review state
    (`built`) as meeting `after`. Ablation: drop the `mark_done != "auto"` early
    return and both plans end `done`."""
    layout = _scaffold(
        tmp_path / "sbx",
        _epic(EntrySpec(1, "Cart model"), EntrySpec(2, "Cart rules", after=(1,))),
        mark_done="never",
    )
    root = layout.project
    base = _commit_count(root)

    _ok(_run(root, "run"))

    assert _plan_status(layout, "Cart model") == "built"
    assert _plan_status(layout, "Cart rules") == "built"
    assert _commit_count(root) == base + 2
    assert _committed_status(layout, "HEAD~1", "Cart model") == "built"
    assert _committed_status(layout, "HEAD", "Cart rules") == "built"
    assert len(_prompts(root)) == 2
    assert _journal(root, "ticket-mark") == []  # bmad-loop wrote no plan at all


def test_e2e_tickets_hitl_pauses_then_resumes(tmp_path):
    """A hitl ticket is never dispatched: the run builds 1.1, then pauses at
    `ticket-hitl` naming 1.2 with no session for it. The person builds it (here: marks
    it done and commits), and the resumed run spawns nothing more. Ablation: drop
    `decide_pick`'s hitl arm and 1.2 is dispatched."""
    layout = _scaffold(
        tmp_path / "sbx",
        _epic(EntrySpec(1, "Cart model"), EntrySpec(2, "Cart rules", after=(1,), hitl=True)),
    )
    root = layout.project

    _ok(_run(root, "run"))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "ticket-hitl", doc
    assert doc["paused_story_key"] == "1.2"
    assert _plan_status(layout, "Cart model") == "done"
    assert _plan_status(layout, "Cart rules") == "absent"
    assert len(_prompts(root)) == 1  # 1.1 only: zero sessions for the hitl ticket

    _tickets(layout, "mark", "1.2", "done")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "build 1.2 by hand")

    _ok(_run(root, "resume", _run_dir(root).name))

    assert len(_prompts(root)) == 1
    assert _plan_status(layout, "Cart rules") == "done"
    _assert_ended_at_epic_boundary(_status_doc(root))


def test_e2e_tickets_refine_pauses_before_any_session(tmp_path):
    """A bug needs refining before it starts, so a tree whose first ticket is a bug
    pauses at `ticket-refine` before any session exists. The person pulls the leaf
    and marks it refined; the resumed run builds both tickets. Ablation: drop
    `decide_pick`'s refine arm and the bug is dispatched at once."""
    layout = _scaffold(
        tmp_path / "sbx",
        _epic(EntrySpec(1, "Cart crash", type="bug"), EntrySpec(2, "Cart rules", after=(1,))),
    )
    root = layout.project

    _ok(_run(root, "run"))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "ticket-refine", doc
    assert doc["paused_story_key"] == "1.1"
    assert not list((_run_dir(root)).glob("tasks/*")), "no session may start before refining"

    epic_dir = layout.initiative_folder / EPIC
    _tickets(layout, "pull", str(epic_dir), "1")
    leaf = epic_dir / "bug-cart-crash.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace("refined: false", "refined: true", 1),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "refine 1.1")

    _ok(_run(root, "resume", _run_dir(root).name))

    bug_plan = layout.initiative_folder / EPIC / "bug-cart-crash-plan.md"
    assert status_of(parse_frontmatter(bug_plan.read_text(encoding="utf-8"))) == "done"
    assert _plan_status(layout, "Cart rules") == "done"
    assert len(_prompts(root)) == 2
    _assert_ended_at_epic_boundary(_status_doc(root))


def test_e2e_tickets_plan_checkpoint_refined_and_unrefined(tmp_path):
    """Both upstream `plan_checkpoint` shapes in one run.

    Unrefined 1.1: leg 1 is dispatched with `Halt after planning.` and
    `BMAD_LOOP_PLAN_HALT`, leaves the plan at `ready-for-dev` uncommitted, and the run
    pauses at `plan-checkpoint`; the resume's leg 2 is a plain `ticket 1.1` that builds
    on that plan. Refined 1.2: the pick pauses at `plan-checkpoint` naming its story
    file BEFORE any session for it, and the resume records the approval and
    dispatches it. Ablations: the binding's `plan_checkpoint` forced False (no leg
    1), and `decide_pick`'s refined plan-checkpoint arm dropped (1.2 dispatched
    unapproved), each turn this red."""
    layout = _scaffold(
        tmp_path / "sbx",
        _epic(
            EntrySpec(1, "Cart model", plan_checkpoint=True),
            EntrySpec(2, "Cart rules", after=(1,), plan_checkpoint=True, refined=True),
        ),
    )
    root = layout.project
    base = _commit_count(root)

    _ok(_run(root, "run"))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "plan-checkpoint", doc
    assert doc["paused_story_key"] == "1.1"
    assert _plan_status(layout, "Cart model") == "ready-for-dev"
    assert _commit_count(root) == base  # no interim plan commit
    (leg1,) = _prompts(root)
    assert leg1.startswith("/bmad-build-auto ticket 1.1 Halt after planning."), leg1
    tasks = _run_dir(root) / "tasks"
    (halt_env,) = [p.read_text(encoding="utf-8") for p in tasks.glob("*/fake-plan-halt.txt")]
    assert halt_env == "1"

    _ok(_run(root, "resume", _run_dir(root).name))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "plan-checkpoint", doc
    assert doc["paused_story_key"] == "1.2"
    assert "story-cart-rules.md" in doc["paused_reason"], doc["paused_reason"]
    assert _plan_status(layout, "Cart model") == "done"
    assert _plan_status(layout, "Cart rules") == "absent"
    assert _commit_count(root) == base + 1
    leg2 = _prompts(root)[1]
    assert leg2.startswith("/bmad-build-auto ticket 1.1") and "Halt after planning." not in leg2
    assert len(_prompts(root)) == 2  # zero sessions for 1.2 before its approval

    _ok(_run(root, "resume", _run_dir(root).name))

    assert _plan_status(layout, "Cart rules") == "done"
    assert _commit_count(root) == base + 2
    prompts = _prompts(root)
    assert len(prompts) == 3 and prompts[2].startswith("/bmad-build-auto ticket 1.2"), prompts
    assert "Halt after planning." not in prompts[2]
    state = json.loads((_run_dir(root) / "state.json").read_text(encoding="utf-8"))
    assert state["ticket_story_approvals"] == ["1.2"]
    _assert_ended_at_epic_boundary(_status_doc(root))


def test_e2e_tickets_done_checkpoint_pauses_after_commit(tmp_path):
    """`done_checkpoint` on 1.1: the run commits 1.1 (marked `done`), then pauses at
    `story-checkpoint` before 1.2 has a session; the resume builds 1.2. Ablation:
    the binding's `done_checkpoint` forced False and 1.2 runs straight on."""
    layout = _scaffold(
        tmp_path / "sbx",
        _epic(
            EntrySpec(1, "Cart model", done_checkpoint=True),
            EntrySpec(2, "Cart rules", after=(1,)),
        ),
    )
    root = layout.project
    base = _commit_count(root)

    _ok(_run(root, "run"))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "story-checkpoint", doc
    assert doc["paused_story_key"] == "1.1"
    assert _plan_status(layout, "Cart model") == "done"
    assert _committed_status(layout, "HEAD", "Cart model") == "done"
    assert _commit_count(root) == base + 1
    assert _plan_status(layout, "Cart rules") == "absent"
    assert len(_prompts(root)) == 1

    _ok(_run(root, "resume", _run_dir(root).name))

    assert _plan_status(layout, "Cart rules") == "done"
    assert _commit_count(root) == base + 2
    assert len(_prompts(root)) == 2
    _assert_ended_at_epic_boundary(_status_doc(root))
