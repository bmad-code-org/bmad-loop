"""Deterministic full-CLI sandbox E2E for tickets mode (the BMAD v7 ticket tree).

Drives the REAL `bmad-loop run/resume/status/sweep` binaries through REAL tmux against
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
with the verdict a `.retro-verdict-<slug>` marker names (default `accepted`). A
`/bmad-loop-sweep` prompt writes a one-bundle triage partition, and a `dw-*` bundle
session edits code and leaves `plan-<key>.md` at `built` in the initiative folder
its prompt names, with no `tickets.py` call. It NEVER runs `mark done`: that write
belongs to bmad-loop.

Rows: (1) two tickets marked `done` by bmad-loop inside each unit commit, (2)
`mark_done = "never"` leaves both at `built` and the second ticket still runs,
(3) a hitl ticket pauses before any session for it, (4) a ticket that needs
refining pauses before any session at all, (5) the unrefined two-leg and the
refined pre-dispatch `plan_checkpoint`, (6) `done_checkpoint` pauses after the
commit, (7) the auto retrospective, its closure pause and the gated next epic, (8)
a `rejected` verdict holding the next epic, (9) a session-blocked ticket re-armed by
`resolve` through `mark`, (10) worktree isolation over a tracked (T1) and an
ignored, redirected (T2) store, (11) plan and retrospective commits in a separate
store repository (T3 nested, T4 outside), the preflight refusals of (12) a
tracker store and (13) a workspace layout (T5), and (14) a worktree-isolated `sweep`
over a shared store (T2–T4): its bundle plan and ledger close committed in the store
repository, or `store-unversioned` for T2. Rows 7–9 run over T1 and T3. A run
that finishes an epic it committed a ticket of always ends paused at
`epic-boundary` for a person to close the epic.

Real `tickets.py` spawns: every run and resolve spawns it through bmad-loop and
every fake ticket session once more; outside them this file spawns it twice (rows
3 and 4). Linux and real tmux only, so none of this reaches the Windows legs'
budget.
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
    EntrySpec,
    EpicSpec,
    V7Layout,
    _store_repo,
    install_bmad_v7,
    install_build_auto_v7_skill,
    install_sweep_skill,
    real_mux_e2e,
    remove_tree,
    ticket_slug,
    write_ticket_tree,
)

from bmad_loop import deferredwork, ticketlayout
from bmad_loop.frontmatter import parse_frontmatter, status_of

# Linux-only, not merely non-win32: the fake is bash + GNU coreutils (`date +%s%N`).
HAVE_TMUX = sys.platform.startswith("linux") and shutil.which("tmux") is not None
# A LIST, not a single mark: every real-tmux test joins the serialized real-mux xdist
# group (tests/test_conftest.py's grouping guard enforces it) and runs on its own
# private tmux server (DW-561), so a leaked session never reaches the default one.
pytestmark = [
    pytest.mark.skipif(not HAVE_TMUX, reason="tickets E2E needs real tmux on Linux"),
    real_mux_e2e,
    pytest.mark.usefixtures("private_tmux_server"),
]

# The fake build-auto. `@PY@` is baked in by the scaffold (tmux does not carry the
# test env, so a bare `python` could be any interpreter); `@INIT@` is the initiative
# folder (relative to the project, or absolute for an outside store), where the
# retrospective lands. Records the argv it received (`fake-prompt.txt`), the
# plan-halt env (`fake-plan-halt.txt`) and its working directory (`fake-cwd.txt`), so
# a row can prove what reached the binary rather than what bmad-loop meant to send.
FAKE_BUILD_AUTO = r"""#!/usr/bin/env bash
set -e
PY=@PY@
INIT=@INIT@
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
printf '%s' "$PWD" > "$td/fake-cwd.txt"
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
        "$slug" "$verdict" "$slug" > "$INIT/tickets/$slug/$slug-retrospective.md"
    printf '%s' '{"workflow": "bmad-retrospective", "status": "done"}' > "$td/result.json"
    stop
fi

# A sweep's triage (`/bmad-loop-sweep`): the triage adapter reads a real
# result.json, so write the partition (one bundle owning DW-1) and Stop.
if printf '%s' "$prompt" | grep -q "bmad-loop-sweep"; then
    printf '%s' '{"workflow": "deferred-sweep-triage", "open_ids": ["DW-1"], "already_resolved": [], "bundles": [{"name": "fix-things", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}], "blocked": [], "skip": [], "decisions": [], "escalations": []}' \
        > "$td/result.json"
    stop
fi

# A sweep bundle (`dw-<name>`, free-form: no ticket, no `tickets.py`): edit code and
# leave `plan-<key>.md` at `built` in the initiative folder the prompt names (the
# main checkout's store under a shared store), uncommitted — bmad-loop commits.
case "$ref" in
    dw-*)
        home=$(printf '%s' "$prompt" | sed -n 's/.*the initiative folder `\([^`]*\)`.*/\1/p' | head -1)
        [ -n "$home" ] || home="$INIT"
        echo "impl for $ref" >> src.txt
        printf -- '---\ntitle: %s\nstatus: built\nbaseline_revision: %s\n---\n\n# %s plan\n\n## Auto Run Result\n\nStatus: built\n' \
            "$ref" "$baseline" "$ref" > "$home/plan-$ref.md"
        printf '%s' "$(cd "$home" && pwd -P)/plan-$ref.md" > "$td/fake-plan.txt"
        stop ;;
esac

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
EPIC2 = "epic-checkout"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _epic(*entries: EntrySpec) -> tuple[EpicSpec, ...]:
    return (EpicSpec(1, EPIC, "Cart", entries),)


# Two one-ticket epics, the second gated on the first's closure: the epic-boundary rows.
TWO_EPICS: tuple[EpicSpec, ...] = (
    EpicSpec(1, EPIC, "Cart", (EntrySpec(1, "Cart model"),)),
    EpicSpec(2, EPIC2, "Checkout", (EntrySpec(1, "Checkout form"),), gated_by=(EPIC,)),
)


def _scaffold(
    sandbox: V7Layout,
    epics: tuple[EpicSpec, ...],
    *,
    mark_done: str = "auto",
    topology: str = "T1",
    retro: str = "never",
    isolation: str = "none",
    store: str = "repo",
) -> V7Layout:
    """``sandbox`` (the `project_v7` fixture's copy of the per-worker v7 template)
    turned into a committed, clean tickets-mode sandbox: ``epics`` replace the
    template's tree, plus the v7 build-auto stub, the fake's profile and a tickets-mode
    policy. The template already carries the vendored `tickets.py` install with the
    personal config layer gitignored (as `bmad setup` leaves it). Review is off and the retrospective gate is ``retro`` (a `never` run has
    only dev sessions of the fake; `auto` also installs the retro skill stub).

    ``topology`` places the store: T1 tracked in the checkout (upstream's default),
    T2 in the checkout but gitignored and unversioned, T3 a gitignored repository
    nested in the checkout, T4 its own repository outside the project (beside
    ``root``, with an absolute `output_folder`)."""
    root = sandbox.project
    # The template's tracked store goes wholesale, index included (a tracked path
    # stays tracked under a new ignore rule); the commit below records its deletion
    # and, for T1, the replacement tree.
    _git(root, "rm", "-r", "-q", "--cached", "_bmad-output")
    remove_tree(sandbox.output_folder)
    if topology in ("T2", "T3"):
        with (root / ".gitignore").open("a", encoding="utf-8") as f:
            f.write("/_bmad-output/\n")
    out = str(root.parent / "store") if topology == "T4" else "_bmad-output"
    layout = install_bmad_v7(root, output_folder=out, store=store)
    write_ticket_tree(layout, epics)
    install_build_auto_v7_skill(root)
    if retro == "auto":
        skill = root / ".claude" / "skills" / "bmad-retrospective" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: bmad-retrospective\n---\n", encoding="utf-8")

    # T4's store is outside the project, so the fake gets its absolute path; every
    # other layout stays relative, which a unit worktree's own copy resolves.
    init = (
        layout.initiative_folder.as_posix()
        if topology == "T4"
        else layout.initiative_folder.relative_to(root).as_posix()
    )
    fake = root / ".bmad-loop" / "fake-build-auto.sh"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        FAKE_BUILD_AUTO.replace("@PY@", shlex.quote(sys.executable)).replace(
            "@INIT@", shlex.quote(init)
        ),
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
        f'[gates]\nretrospective = "{retro}"\n\n'
        f'[scm]\nisolation = "{isolation}"\n\n'
        '[stories]\nsource = "tickets"\n\n'
        f'[tickets]\nmark_done = "{mark_done}"\n',
        encoding="utf-8",
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")
    if topology in ("T3", "T4"):
        _store_repo(layout.output_folder)
    return layout


def _tree_repo(layout: V7Layout, topology: str) -> Path:
    """The repository that versions the ticket tree: the code checkout for T1, the
    store's own repository for T3/T4 (T2 has none)."""
    return layout.output_folder if topology in ("T3", "T4") else layout.project


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


def _epic_dir(layout: V7Layout, epic: str = EPIC) -> Path:
    """The epic's folder, in its initiative's `tickets/` folder."""
    return layout.initiative_folder / ticketlayout.TICKETS / epic


def _plan(layout: V7Layout, title: str, epic: str = EPIC) -> Path:
    return _epic_dir(layout, epic) / ticketlayout.TICKETS / f"story-{ticket_slug(title)}-plan.md"


def _plan_fields(layout: V7Layout, title: str, epic: str = EPIC) -> dict:
    return parse_frontmatter(_plan(layout, title, epic).read_text(encoding="utf-8"))


def _plan_status(layout: V7Layout, title: str, epic: str = EPIC) -> str:
    """A plan's status through the frontmatter reader (never raw bytes: upstream may
    change how `mark` quotes values); "absent" when no plan exists."""
    if not _plan(layout, title, epic).is_file():
        return "absent"
    return status_of(_plan_fields(layout, title, epic))


def _committed_status(
    layout: V7Layout, rev: str, title: str, *, repo: Path | None = None, epic: str = EPIC
) -> str:
    """A plan's status as committed at ``rev`` of ``repo`` (default: the checkout)."""
    repo = repo or layout.project
    rel = _plan(layout, title, epic).relative_to(repo).as_posix()
    return status_of(parse_frontmatter(_git(repo, "show", f"{rev}:{rel}")))


def _subjects(repo: Path) -> list[str]:
    """``repo``'s commit subjects, newest first."""
    return _git(repo, "log", "--format=%s").splitlines()


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
    """Every dispatch the fake received, in launch order: the journal's
    `session-start` rows name the tasks, and each task's prompt file is what reached
    the binary (the fake writes it once, on start)."""
    tasks = _run_dir(root) / "tasks"
    order = dict.fromkeys(row["task_id"] for row in _journal(root, "session-start"))
    recorded = [tasks / tid / "fake-prompt.txt" for tid in order]
    return [p.read_text(encoding="utf-8") for p in recorded if p.is_file()]


def _journal(root: Path, kind: str) -> list[dict]:
    lines = (_run_dir(root) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    return [row for row in map(json.loads, lines) if row["kind"] == kind]


def _assert_ended_at_epic_boundary(doc: dict) -> None:
    """The end of every row that finishes the epic: a closure pause for the person
    who closes the epic with bmad-ticket (bmad-loop never writes the epic file)."""
    assert doc["status"] == "paused", doc
    assert doc["paused_stage"] == "epic-boundary", doc
    assert doc["tickets"]["epic_closures"] == {EPIC: "unknown"}, doc["tickets"]


def test_e2e_tickets_happy_path_marks_done(project_v7):
    """Two tickets, `mark_done = "auto"`: the fake leaves each plan `built`, bmad-loop
    marks it `done` inside the unit's commit window, so each squashed unit commit
    carries its own plan at `done` with the ticket trailers. Ablation: make the
    commit window skip the mark (`_terminal_plan_write` returning early) and the
    plans stay `built`."""
    layout = _scaffold(
        project_v7, _epic(EntrySpec(1, "Cart model"), EntrySpec(2, "Cart rules", after=(1,)))
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


def test_e2e_tickets_mark_done_never_leaves_built(project_v7):
    """`mark_done = "never"`: both plans stay `built`, the unit commits carry them at
    `built`, and 1.2 still runs, because `next` counts a ticket in review state
    (`built`) as meeting `after`. Ablation: drop the `mark_done != "auto"` early
    return and both plans end `done`."""
    layout = _scaffold(
        project_v7,
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


def test_e2e_tickets_hitl_pauses_then_resumes(project_v7):
    """A hitl ticket is never dispatched: the run builds 1.1, then pauses at
    `ticket-hitl` naming 1.2 with no session for it. The person builds it (here: marks
    it done and commits), and the resumed run spawns nothing more. Ablation: drop
    `decide_pick`'s hitl arm and 1.2 is dispatched."""
    layout = _scaffold(
        project_v7,
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


def test_e2e_tickets_refine_pauses_before_any_session(project_v7):
    """A bug needs refining before it starts, so a tree whose first ticket is a bug
    pauses at `ticket-refine` before any session exists. The person pulls the leaf
    and marks it refined; the resumed run builds both tickets. Ablation: drop
    `decide_pick`'s refine arm and the bug is dispatched at once."""
    layout = _scaffold(
        project_v7,
        _epic(EntrySpec(1, "Cart crash", type="bug"), EntrySpec(2, "Cart rules", after=(1,))),
    )
    root = layout.project

    _ok(_run(root, "run"))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "ticket-refine", doc
    assert doc["paused_story_key"] == "1.1"
    assert not list((_run_dir(root)).glob("tasks/*")), "no session may start before refining"

    epic_dir = _epic_dir(layout)
    _tickets(layout, "pull", str(epic_dir), "1")
    leaf = epic_dir / ticketlayout.TICKETS / "bug-cart-crash.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace("refined: false", "refined: true", 1),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "refine 1.1")

    _ok(_run(root, "resume", _run_dir(root).name))

    bug_plan = epic_dir / ticketlayout.TICKETS / "bug-cart-crash-plan.md"
    assert status_of(parse_frontmatter(bug_plan.read_text(encoding="utf-8"))) == "done"
    assert _plan_status(layout, "Cart rules") == "done"
    assert len(_prompts(root)) == 2
    _assert_ended_at_epic_boundary(_status_doc(root))


def test_e2e_tickets_plan_checkpoint_refined_and_unrefined(project_v7):
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
        project_v7,
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


def test_e2e_tickets_done_checkpoint_pauses_after_commit(project_v7):
    """`done_checkpoint` on 1.1: the run commits 1.1 (marked `done`), then pauses at
    `story-checkpoint` before 1.2 has a session; the resume builds 1.2. Ablation:
    the binding's `done_checkpoint` forced False and 1.2 runs straight on."""
    layout = _scaffold(
        project_v7,
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


def _first_lines(root: Path) -> list[str]:
    """Each dispatch's command line, without the clauses bmad-loop appends."""
    return [p.splitlines()[0].split(" — ")[0] for p in _prompts(root)]


def _commit_marker(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"add {name}")


def _close_epic(layout: V7Layout, topology: str, slug: str) -> None:
    """What a person closing the epic with bmad-ticket leaves: `status: done` in the
    epic file, committed in the repository that versions the tree."""
    epic_file = _epic_dir(layout, slug) / f"{slug}.md"
    epic_file.write_text(
        epic_file.read_text(encoding="utf-8").replace("status: in-progress", "status: done", 1),
        encoding="utf-8",
    )
    tree = _tree_repo(layout, topology)
    _git(tree, "add", epic_file.relative_to(tree).as_posix())
    _git(tree, "commit", "-q", "-m", f"close {slug}")


def _clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain").strip() == ""


@pytest.mark.parametrize("topology", ["T1", "T3"])
def test_e2e_tickets_epic_boundary_retro_then_closure_pause(project_v7, topology):
    """`[gates] retrospective = "auto"`: once 1.1 (epic-cart's only ticket) lands, the
    run launches `/bmad-retrospective -H epic-cart`, commits the document alone (in
    the code checkout for T1, in the store repository for T3) and pauses at
    `epic-boundary` for closure with the verdict recorded. bmad-loop never writes the
    epic file. The person closes the epic; the resumed run builds the gated 2.1, runs
    epic-checkout's retrospective and pauses for that closure. Ablation: drop the
    settle call and the retro document stays uncommitted (the store or the checkout
    is left dirty)."""
    layout = _scaffold(project_v7, TWO_EPICS, topology=topology, retro="auto")
    root = layout.project
    tree = _tree_repo(layout, topology)
    epic_rel = (_epic_dir(layout) / f"{EPIC}.md").relative_to(tree).as_posix()
    epic_history = _git(tree, "log", "--format=%H", "--", epic_rel)

    _ok(_run(root, "run"))

    run_id = _run_dir(root).name
    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "epic-boundary", doc
    assert doc["paused_story_key"] == EPIC
    assert "retrospective verdict: accepted" in doc["paused_reason"], doc["paused_reason"]
    assert doc["tickets"]["epic_closures"] == {EPIC: "accepted"}
    assert _first_lines(root) == ["/bmad-build-auto ticket 1.1", f"/bmad-retrospective -H {EPIC}"]
    assert _plan_status(layout, "Checkout form", EPIC2) == "absent"
    retro_rel = (_epic_dir(layout) / f"{EPIC}-retrospective.md").relative_to(tree).as_posix()
    suffix = f" [bmad-loop {run_id}]" if topology == "T3" else ""
    assert _subjects(tree)[0] == f"chore(retro): {EPIC} retrospective{suffix}"
    assert _git(tree, "show", "--name-only", "--format=", "HEAD").split() == [retro_rel]
    assert _clean(root) and _clean(tree)
    # bmad-loop never touched the epic file: same history, still open.
    assert _git(tree, "log", "--format=%H", "--", epic_rel) == epic_history
    assert "status: in-progress" in (tree / epic_rel).read_text(encoding="utf-8")

    _close_epic(layout, topology, EPIC)
    _ok(_run(root, "resume", run_id))

    assert _plan_status(layout, "Checkout form", EPIC2) == "done"
    assert _first_lines(root)[2:] == [
        "/bmad-build-auto ticket 2.1",
        f"/bmad-retrospective -H {EPIC2}",
    ]
    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "epic-boundary", doc
    assert doc["paused_story_key"] == EPIC2
    assert doc["tickets"]["epic_closures"] == {EPIC: "accepted", EPIC2: "accepted"}


@pytest.mark.parametrize("topology", ["T1", "T3"])
def test_e2e_tickets_retro_rejected_blocks_next_epic(project_v7, topology):
    """A `rejected` retrospective verdict holds the run while epic-cart stays open: the
    first pause names the rejection, and a resume without closing the epic pauses
    again (`epic-rejected`) with no session for epic-checkout. Ablation: drop the
    tickets engine's `BOUNDARY_REJECTED` arm and the resume only acknowledges the
    closure (an `awaiting-closure` halt whose reason never says `rejected`)."""
    layout = _scaffold(project_v7, TWO_EPICS, topology=topology, retro="auto")
    root = layout.project
    _commit_marker(root, f".retro-verdict-{EPIC}", "rejected\n")

    _ok(_run(root, "run"))

    run_id = _run_dir(root).name
    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "epic-boundary", doc
    assert doc["tickets"]["epic_closures"] == {EPIC: "rejected"}
    assert "rejected verdict holds the run" in doc["paused_reason"], doc["paused_reason"]

    _ok(_run(root, "resume", run_id))

    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "epic-boundary", doc
    assert doc["paused_story_key"] == EPIC
    assert "verdict is rejected" in doc["paused_reason"], doc["paused_reason"]
    causes = [row["cause"] for row in _journal(root, "ticket-pick-halt")]
    assert causes[-1] == "epic-rejected", causes
    assert len(_prompts(root)) == 2  # 1.1 and its retro: zero sessions for epic-checkout
    assert _plan_status(layout, "Checkout form", EPIC2) == "absent"


@pytest.mark.parametrize("topology", ["T1", "T3"])
def test_e2e_tickets_blocked_resolve_rearms_via_mark(project_v7, topology):
    """The session blocks its own ticket (`tickets.py mark 1.1 blocked`), so the run
    pauses at `escalation` with nothing committed. `resolve --no-interactive` spawns no
    session: it re-arms the plan through the chokepoint (`mark` reason `rearm`,
    `blocked` → `ready-for-dev`), which clears `blocked_at`/`blocked_reason`, and the
    resumed run re-dispatches 1.1, which builds and is marked `done`. Ablation: skip
    the re-arm mark (`runs._rearm_ticket_plan` returning False) and resolve refuses
    the plan, exiting 1 with the ticket still escalated."""
    layout = _scaffold(project_v7, _epic(EntrySpec(1, "Cart model")), topology=topology)
    root = layout.project
    _commit_marker(root, ".block-1.1", "block\n")
    base = _commit_count(root)

    _ok(_run(root, "run"))

    run_id = _run_dir(root).name
    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "escalation", doc
    assert doc["paused_story_key"] == "1.1"
    fields = _plan_fields(layout, "Cart model")
    assert status_of(fields) == "blocked"
    assert fields.get("blocked_reason") == "needs a human decision", fields
    assert _commit_count(root) == base

    _ok(_run(root, "resolve", run_id, "--no-interactive", "--no-resume"))

    fields = _plan_fields(layout, "Cart model")
    assert status_of(fields) == "ready-for-dev"
    assert "blocked_at" not in fields and "blocked_reason" not in fields, fields
    (rearm,) = [row for row in _journal(root, "ticket-mark") if row["reason"] == "rearm"]
    assert (rearm["ref"], rearm["before"], rearm["after"], rearm["outcome"]) == (
        "1.1",
        "blocked",
        "ready-for-dev",
        "written",
    )
    assert len(_prompts(root)) == 1  # resolve spawned no session

    _ok(_run(root, "resume", run_id))

    assert _plan_status(layout, "Cart model") == "done"
    assert _first_lines(root) == ["/bmad-build-auto ticket 1.1"] * 2
    assert _commit_count(root) == base + 1
    _assert_ended_at_epic_boundary(_status_doc(root))


@pytest.mark.parametrize("topology", ["T1", "T2"])
def test_e2e_tickets_worktree_isolation_finds_tree(project_v7, topology):
    """`isolation = "worktree"`: the session runs in the unit's mount, where the
    gitignored `config.user.toml` (seeded, never committed) lets the fake's
    `tickets.py find` resolve the tree. T1: the mark rides the squashed unit commit,
    and the merge carries the `done` plan back to main. T2 (an ignored, unversioned
    store): the mount is redirected to the main store, the plan is written there and
    marked `done` after the merge, and `store-unversioned` is journaled once.
    Ablation: drop the redirect (`redirect_mount_store`) and T2's fake writes a plan
    the main store never sees."""
    layout = _scaffold(
        project_v7,
        _epic(EntrySpec(1, "Cart model")),
        topology=topology,
        isolation="worktree",
    )
    root = layout.project
    user_config = "_bmad/custom/config.user.toml"
    assert _git(root, "ls-files", "--", user_config) == ""
    _git(root, "check-ignore", "-q", user_config)  # raises unless ignored

    _ok(_run(root, "run"))

    (cwd,) = [
        Path(p.read_text(encoding="utf-8")) for p in _run_dir(root).glob("tasks/*/fake-cwd.txt")
    ]
    assert cwd != root and cwd.is_relative_to(_run_dir(root) / "worktrees"), cwd
    assert _plan_status(layout, "Cart model") == "done"
    plan_rel = _plan(layout, "Cart model").relative_to(root).as_posix()
    unit = _git(root, "show", "--name-only", "--format=", "HEAD^2").split()  # the merged unit
    assert "src.txt" in unit
    assert "Bmad-Ticket: 1.1" in _git(root, "show", "-s", "--format=%B", "HEAD^2")
    if topology == "T1":
        assert plan_rel in unit
        assert _committed_status(layout, "HEAD", "Cart model") == "done"
        assert _journal(root, "store-unversioned") == []
    else:
        assert plan_rel not in unit and _git(root, "ls-files", "--", plan_rel) == ""
        assert [row["layout"] for row in _journal(root, "store-unversioned")] == ["T2"]
        assert [row["target"] for row in _journal(root, "ticket-mark-latched")] == ["done"]
    assert _git(root, "branch", "--list", "bmad-loop/*").strip() == ""
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _clean(root)
    _assert_ended_at_epic_boundary(_status_doc(root))


@pytest.mark.parametrize("topology", ["T3", "T4"])
def test_e2e_tickets_multirepo_store_commits(project_v7, topology):
    """A store that is its own repository (T3 nested and ignored, T4 outside the
    project), `isolation = "none"`: the code repository gets one squashed commit per
    ticket and no plan; after each lands, bmad-loop marks the plan `done` and commits
    it alone in the store repository (`chore(tickets): <ref> done [bmad-loop <run>]`
    with `Bmad-Code-*` trailers naming the code commit), then commits the epic
    retrospective there too. Both repositories end clean. Ablation: skip the store
    commit (`_commit_marked_plan`) and the store is left dirty with no
    `chore(tickets)` commits."""
    layout = _scaffold(
        project_v7,
        _epic(EntrySpec(1, "Cart model"), EntrySpec(2, "Cart rules", after=(1,))),
        topology=topology,
        retro="auto",
    )
    root, store = layout.project, layout.output_folder
    base = _commit_count(root)

    _ok(_run(root, "run"))

    run_id = _run_dir(root).name
    assert _commit_count(root) == base + 2
    assert _subjects(store)[:3] == [
        f"chore(retro): {EPIC} retrospective [bmad-loop {run_id}]",
        f"chore(tickets): 1.2 done [bmad-loop {run_id}]",
        f"chore(tickets): 1.1 done [bmad-loop {run_id}]",
    ]
    for code_rev, store_rev, title, ref in (
        ("HEAD~1", "HEAD~2", "Cart model", "1.1"),
        ("HEAD", "HEAD~1", "Cart rules", "1.2"),
    ):
        sha, parent = (_git(root, "rev-parse", r).strip() for r in (code_rev, f"{code_rev}~1"))
        plan_rel = _plan(layout, title).relative_to(store).as_posix()
        assert _git(store, "show", "--name-only", "--format=", store_rev).split() == [plan_rel]
        assert _committed_status(layout, store_rev, title, repo=store) == "done"
        body = _git(store, "show", "-s", "--format=%B", store_rev)
        assert f"Bmad-Code-Repo: {root.resolve().as_posix()}" in body, body
        assert f"Bmad-Code-Commit: {sha}" in body and f"Bmad-Code-Range: {parent}..{sha}" in body
        code_files = _git(root, "show", "--name-only", "--format=", code_rev).split()
        assert code_files == ["src.txt"], code_files
        code_body = _git(root, "show", "-s", "--format=%B", code_rev)
        assert f"Bmad-Ticket: {ref}" in code_body
        # T4's plan lies outside the code repository, so its unit commit names none.
        assert ("Bmad-Plan:" in code_body) == (topology == "T3"), code_body
    assert len(_journal(root, "store-committed")) == 3
    assert _clean(root) and _clean(store)
    doc = _status_doc(root)
    assert doc["status"] == "paused" and doc["paused_stage"] == "epic-boundary", doc
    assert doc["tickets"]["epic_closures"] == {EPIC: "accepted"}


SWEEP_POLICY = (
    '[adapter]\nname = "faketickets"\n\n'
    "[review]\nenabled = false\n\n"
    '[gates]\nmode = "none"\n\n'
    '[scm]\nisolation = "worktree"\n'
)
SWEEP_LEDGER = (
    "# Deferred Work\n\n### DW-1: item DW-1\n\n"
    "origin: test, 2026-06-01\nlocation: src.txt:1\nreason: test entry.\nstatus: open\n"
)


@pytest.mark.parametrize("topology", ["T2", "T3", "T4"])
def test_e2e_tickets_isolated_sweep_shared_store(project_v7, topology):
    """A v7 `bmad-loop sweep` under `isolation = "worktree"` over a store no
    checkout carries (T2 ignored and unversioned, T3 its own nested repository, T4
    its own repository outside the project): the store is shared, so the bundle
    prompt names the main checkout's initiative folder and the fake leaves its
    `built` plan there, not in the unit mount. The unit's code merges back and DW-1
    is closed in the main ledger. T3/T4: once the code lands the plan gets its own
    `chore(sweep): dw-<name> plan [bmad-loop <run>]` commit in the store repository
    and the bundle's ledger close its own `chore(sweep): close resolved
    deferred-work entries` commit there, each touching that file alone with the
    `Bmad-Code-Commit` trailer naming the merged unit commit, so both repositories
    end clean. T2: nothing to commit the plan in, so `store-unversioned` is
    journaled once, and no close is withheld or failed. The run must complete:
    `sweep` exits 0 on a pause too.

    ABLATIONS (performed, singly): A1 drop the `_publish_bundle_plan` call in
    `SweepEngine._land_post_landing_writes` and T3/T4 fail (no plan commit, the
    store left dirty) and T2 too (no `store-unversioned`); A2 drop the shared-store
    redirect (`with_shared_store` in `SweepEngine.__init__`) and T2/T3 fail (the
    prompt names the unit mount's folder, so the fake writes its plan there; T4's
    absolute store is unaffected by design); A3 drop the shared-store arm of
    `WorktreeFlow.finish_publication` and all three fail (the run pauses on
    `artifact publication intent is missing` after the merge); A4 drop the
    `_publish_bundle_close` call and T3/T4 fail (no ledger close commit, the
    store's ledger left dirty)."""
    layout = _scaffold(
        project_v7, _epic(EntrySpec(1, "Cart model")), topology=topology, isolation="worktree"
    )
    root, store = layout.project, layout.output_folder
    install_sweep_skill(root)
    (root / ".bmad-loop" / "policy.toml").write_text(SWEEP_POLICY, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sweep policy")
    ledger = layout.initiative_folder / "deferred-work.md"
    ledger.write_text(SWEEP_LEDGER, encoding="utf-8")
    if topology == "T2":
        _git(
            root, "check-ignore", "-q", ledger.relative_to(root).as_posix()
        )  # raises unless ignored
    else:
        _git(store, "add", "--", ledger.relative_to(store).as_posix())
        _git(store, "commit", "-q", "-m", "ledger")
        assert _clean(store)
    assert _clean(root)
    base = _commit_count(root)

    _ok(_run(root, "sweep", "--no-prompt"))

    run_id = _run_dir(root).name
    assert _journal(root, "run-paused") == [], _journal(root, "run-paused")  # rc 0 on a pause too
    assert len(_journal(root, "run-complete")) == 1
    key = "dw-fix-things"
    (cwd,) = [
        Path(p.read_text(encoding="utf-8"))
        for p in _run_dir(root).glob(f"tasks/{key}-*/fake-cwd.txt")
    ]
    assert cwd.is_relative_to(_run_dir(root) / "worktrees"), cwd  # the session ran in the mount
    plan = layout.initiative_folder / f"plan-{key}.md"
    # ...and wrote its plan straight into the main checkout's store, never the mount
    (written,) = [
        Path(p.read_text(encoding="utf-8"))
        for p in _run_dir(root).glob(f"tasks/{key}-*/fake-plan.txt")
    ]
    assert written == plan.resolve(), written
    assert status_of(parse_frontmatter(plan.read_text(encoding="utf-8"))) == "built"
    (entry,) = deferredwork.parse_ledger(ledger.read_text(encoding="utf-8"))
    assert entry.status.startswith("done"), entry.status
    assert "impl for dw-fix-things" in (root / "src.txt").read_text(encoding="utf-8")
    assert _commit_count(root) > base
    unit = _git(root, "show", "--name-only", "--format=", "HEAD^2").split()  # the merged unit
    assert unit == ["src.txt"], unit  # the plan rides no code commit
    assert _git(root, "log", "-1", "--format=%s", "HEAD^2").startswith(f"sweep {key}")
    assert not any(s.startswith("chore(sweep)") for s in _subjects(root)), _subjects(root)
    for kind in (
        "sweep-ledger-commit-withheld",
        "store-commit-failed",
        "store-commit-foreign-dirt",
    ):
        assert _journal(root, kind) == [], kind
    if topology == "T2":
        assert [row["layout"] for row in _journal(root, "store-unversioned")] == ["T2"]
        assert _journal(root, "store-committed") == []
    else:
        plan_rel = plan.relative_to(store).as_posix()
        commits = _git(store, "log", "--format=%H", "--", plan_rel).split()
        assert len(commits) == 1, commits
        subject = _git(store, "log", "-1", "--format=%s", commits[0]).strip()
        assert subject == f"chore(sweep): {key} plan [bmad-loop {run_id}]"
        assert _git(store, "show", "--name-only", "--format=", commits[0]).split() == [plan_rel]
        ledger_rel = ledger.relative_to(store).as_posix()
        log = [line.split(" ", 1) for line in _git(store, "log", "--format=%H %s").splitlines()]
        (close,) = [
            sha for sha, s in log if s == "chore(sweep): close resolved deferred-work entries"
        ]
        assert _git(store, "show", "--name-only", "--format=", close).split() == [ledger_rel]
        unit_sha = _git(root, "rev-parse", "HEAD^2").strip()
        assert f"Bmad-Code-Commit: {unit_sha}" in _git(store, "log", "-1", "--format=%B", close)
        assert [row["commit"] for row in _journal(root, "store-committed")] == [commits[0], close]
        assert _journal(root, "store-unversioned") == []
        assert _clean(store)
    assert _git(root, "branch", "--list", "bmad-loop/*").strip() == ""
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _clean(root)


def _validate_problems(root: Path) -> set[str]:
    proc = _run(root, "validate", "--json")
    doc = json.loads(proc.stdout)
    return {f["check"] for f in doc["findings"] if f["severity"] == "problem"}


def test_e2e_tickets_tracker_store_refused(project_v7):
    """A tracker store (`store = "jira"`) is refused at `run` preflight, before any
    run dir or session exists, and `validate` reports it as `tickets.store-not-repo`.
    Ablation: drop the store finding in `install.tickets_preflight` and the run
    composes (a run dir appears)."""
    layout = _scaffold(project_v7, _epic(EntrySpec(1, "Cart model")), store="jira")
    root = layout.project

    proc = _run(root, "run")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "tracker stores are not supported" in proc.stderr, proc.stderr
    assert not (root / ".bmad-loop" / "runs").exists()
    assert "tickets.store-not-repo" in _validate_problems(root)


def test_e2e_tickets_workspace_layout_refused(project_v7):
    """A BMAD workspace (layout T5: the project root holds the install and the store
    but is no repository; the code lives in repositories beneath it) is refused by
    `run` before anything is composed, and `validate` reports
    `tickets.workspace-unsupported`. Ablation: drop the classifier's not-a-repo arm
    and `run` gets past the topology gate."""
    layout = _scaffold(project_v7, _epic(EntrySpec(1, "Cart model")))
    root = layout.project
    remove_tree(root / ".git")
    for name in ("app", "lib"):
        (root / name).mkdir()
        (root / name / "README").write_text(f"{name}\n", encoding="utf-8")
        _store_repo(root / name)
    probe = subprocess.run(["git", "-C", str(root), "rev-parse"], capture_output=True)
    assert probe.returncode != 0, "the sandbox must not sit inside a repository"

    proc = _run(root, "run")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "is not a git repository" in proc.stderr and "workspace" in proc.stderr, proc.stderr
    assert not (root / ".bmad-loop" / "runs").exists()
    assert "tickets.workspace-unsupported" in _validate_problems(root)
