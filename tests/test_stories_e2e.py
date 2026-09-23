"""Deterministic full-CLI sandbox E2E for stories mode (folder+id dispatch).

Drives the REAL `bmad-loop run/resolve/resume` binaries through REAL tmux with a
scripted fake `claude` wired in as a custom profile — no LLM, no cost, fully
reproducible. The fake fakes both the CLI and its Stop hook (it writes the
SessionStart/Stop event files itself), so no `bmad-loop init` is needed; it
leaves the id-keyed story spec on disk and the `GenericDevAdapter` synthesizes
the result from it (`_stories_synth_result`, keyed on `BMAD_LOOP_SPEC_FOLDER`).

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
by the stories wiring), (7) sprint-mode intent-gap patch-restore (halt saves
the attempt as a patch → `resolve --restore-patch` re-arms to in-review +
re-stamps the spec baseline → resume re-applies the patch and dispatches an
explicit spec pointer, resuming review instead of re-implementing), and (8) the
same intent-gap patch-restore for a `sweep` deferred-work bundle (#75) —
triage → bundle halt → `resolve --restore-patch` → resume, driving the
sweep-specific CLI resolve→resume path (SweepEngine rebuilt from sweep.json).
Scenarios (3) `done_checkpoint` and (5) worktree isolation are covered
deterministically at the engine level in test_stories_engine.py; here we prove
the end-to-end CLI stack.

(9) is the post-rename row (BMAD-METHOD #2651): the same two-story happy path
against a project carrying only `bmad-build-auto`, proving the real CLI resolves
the invoked primitive off disk instead of spelling a constant.

(10) is the renderer preflight row: content-confirmed renderer stubs with missing
project-global or skill-relative inputs are refused before tmux can spawn the
fake, dry-run stays diagnostic, and the complete surface reaches the same
zero-token real-tmux success path.

(11) runs that complete renderer project under worktree isolation after removing
the renderer script unit and central config from the index. The real tmux session
can start only if runtime provisioning reconstructs the ignored `_bmad` surface.

(12) injects a detached-child identity publication failure (DW-149). An independent
`observed-child.id` channel lets the row prove the recorder hit the poisoned
temporary directory, the orchestrator reaped that exact child, and the worktree
teardown remained clean despite the missing normal identity record.

Alongside those scenarios the file also holds local-process `/proc`+pidfd harness
rows covering the reap-identity helpers the three teardown E2Es depend on, plus the
DW-159 rows that drive the detached fakes' bash session-readiness gate directly
rather than any helper. Those rows
spawn and reap their own short-lived children, but run no tmux or orchestrator session.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import warnings
from pathlib import Path

import conftest
import pytest
import yaml
from conftest import (
    REAL_MUX_HANG_CEILING_S,
    RECORDED_CHILD_GLOB,
    RENDERER_SCRIPT_IMPORTING_SIBLING,
    bind_recorded_child,
    install_build_auto_skill,
    install_dev_base_skills,
    kill_recorded_child,
    preflight_pidfd_support,
    proc_starttime,
    real_mux_e2e,
    recorded_child,
    recorded_children_swept,
)

from bmad_loop import runs
from bmad_loop.install import (
    BMAD_SCRIPTS_SEED_REL,
    CENTRAL_CONFIG_REL,
    DEV_PRIMITIVE_NEW,
    RENDERER_CONFIG_UTILS_REL,
    RENDERER_ENTRY_REL,
    RENDERER_SCRIPT_REL,
)

# Linux-only, not merely non-win32: every fake CLI below is bash + GNU coreutils
# (`date +%s%N`; the detached-writer fake also needs setsid(1)) — BSD/macOS date
# has no %N and macOS ships no setsid utility, so skipping honestly beats failing.
HAVE_TMUX = sys.platform.startswith("linux") and shutil.which("tmux") is not None
# A LIST, not a single mark: every real-tmux test here joins the serialized real-mux
# xdist group (DW-95). The local-process identity rows below inherit the same marks.
pytestmark = [
    pytest.mark.skipif(not HAVE_TMUX, reason="stories E2E needs real tmux on Linux"),
    real_mux_e2e,
]

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
ed="$BMAD_LOOP_EVENTS_DIR"
mkdir -p "$ed" "$rd/tasks/$tid"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts" "$tid" > "$ed/$ts-$tid-SessionStart.json"
# argv as it ARRIVED, after profile render + tmux quoting — the orchestrator's own
# tasks/<id>/prompt.txt records the pre-render prompt, so only this file can prove a
# dispatched skill NAME actually reached the binary.
printf '%s' "$prompt" > "$rd/tasks/$tid/fake-prompt.txt"
baseline=$(git rev-parse HEAD)

# SWEEP triage (`/bmad-loop-sweep`): the triage adapter is a plain GenericAdapter
# that reads a real result.json (not the spec-synthesizing dev adapter), so write
# the partition ourselves — one bundle "fix" owning the single open ledger id — and
# Stop. Bundle dev sessions carry no BMAD_LOOP_SPEC_FOLDER and fall through to the
# SPRINT branch below, which already drives spec-<key>.md and the intent-gap /
# patch-restore contracts (here <key> = dw-fix, the bundle task key).
if printf '%s' "$prompt" | grep -q "bmad-loop-sweep"; then
    tdir="$rd/tasks/$tid"; mkdir -p "$tdir"
    printf '%s' '{"workflow": "deferred-sweep-triage", "open_ids": ["DW-1"], "already_resolved": [], "bundles": [{"name": "fix", "dw_ids": ["DW-1"], "intent": "resolve DW-1"}], "blocked": [], "skip": [], "decisions": [], "escalations": []}' \
        > "$tdir/result.json"
    ts2=$(( ts + 1 ))
    printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \
        "$ts2" "$tid" > "$ed/$ts2-$tid-Stop.json"
    sleep 30
    exit 0
fi

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
        "$ts2" "$tid" > "$ed/$ts2-$tid-Stop.json"
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
    "$ts2" "$tid" > "$ed/$ts2-$tid-Stop.json"
sleep 30
"""

# The same script as a relay installed BEFORE #494 would be: it knows only the
# in-tree `<run_dir>/events` and ignores the variable the orchestrator now exports.
# Built by swapping the one line that names the directory, so it can differ from
# FAKE_CLI in nothing else.
LEGACY_EVENTS_FAKE_CLI = FAKE_CLI.replace('ed="$BMAD_LOOP_EVENTS_DIR"', 'ed="$rd/events"')

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

# Shared verbatim by both fake CLIs and exercised directly below. Pure bash reads
# field 22 from /proc/<pid>/stat by stripping through the last ") " and taking index
# 19 of the remaining fields, then records the identity captured at spawn.
RECORD_CHILD_IDENTITY_SH = r"""cstat=$(<"/proc/$child/stat")
read -r -a cfields <<< "${cstat##*) }"
printf '%s %s\n' "$child" "${cfields[19]}" > "$idfile.tmp"
mv -f "$idfile.tmp" "$idfile"
"""

# A fake CLI that writes SessionStart and then sleeps forever — it NEVER fires a
# Stop hook, so the dev session can only end via the orchestrator's own timeout
# fire + bounded teardown (#157). Because the session never ends a turn, the
# result-less-Stop stall machinery never engages either, exactly the wedged-in-a-
# tool-call shape the issue reported.
TIMEOUT_FAKE_CLI = (
    r"""#!/usr/bin/env bash
set -e
rd="$BMAD_LOOP_RUN_DIR"; tid="$BMAD_LOOP_TASK_ID"
ts=$(date +%s%N)
ed="$BMAD_LOOP_EVENTS_DIR"
mkdir -p "$ed"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts" "$tid" > "$ed/$ts-$tid-SessionStart.json"
# Background + wait keeps the same process group as a foreground sleep, but
# records the child's pid plus /proc start time so the test can prove teardown reaped
# this exact descendant, not just this shell (whose cmdline is all pgrep can see).
sleep 100000 &
child=$!
idfile="$rd/tasks/$tid/fake-child.pid"
"""
    + RECORD_CHILD_IDENTITY_SH
    + r"""wait "$child"
"""
)

# DW-159: publication must not precede the session transition. `$!` names the child
# the instant fork(2) returns, but `setsid(2)` runs in that child AFTERWARDS, so an
# identity published straight off `$!` merely ASSUMES the escape it is supposed to
# prove — under a scheduler delay the consumer can harvest and grade a straggler still
# inside the pane's session, and the row silently covers the weaker same-pgid case
# (#183/#139) it was written to exclude. This gate turns detachment into an established
# fact: bounded-poll the child's OBSERVED session id until it differs from this shell's
# own, then let publication proceed; a child that never detaches fails loudly instead.
#
# Expects `$child` and `$detach_ack_ceiling_s` to be set already, and mirrors the
# recorder's `") "`-strip parse convention: after `${stat##*) }`, index 3 is the session
# id (index 19 is the start time the recorder reads). Deliberately NOT folded into
# RECORD_CHILD_IDENTITY_SH: that snippet is shared verbatim with TIMEOUT_FAKE_CLI, whose
# child is intentionally same-session, where a session-differs gate would never return.
# On refusal, kill the owned child before exiting: it could otherwise detach later
# without an identity record for the E2E cleanup sweep. The fake can wait on its child;
# a direct harness shell cannot, so only that wait status is ignored under `set -e`.
AWAIT_DETACHED_SESSION_SH = r"""sstat=$(<"/proc/$$/stat")
read -r -a sfields <<< "${sstat##*) }"
own_session="${sfields[3]}"
detach_deadline=$(( SECONDS + detach_ack_ceiling_s ))
while :; do
    cstat=$(<"/proc/$child/stat")
    read -r -a cfields <<< "${cstat##*) }"
    if [[ ${cfields[3]} != "$own_session" ]]; then break; fi
    if (( SECONDS >= detach_deadline )); then
        printf 'child %s never left session %s\n' "$child" "$own_session" >&2
        kill -KILL "$child"
        wait "$child" 2>/dev/null || :
        exit 1
    fi
    sleep 0.05
done
"""

# A fake CLI that ends CLEANLY (writes a `done` spec + Stop, then idles like a real
# interactive session) but first `setsid`-detaches a straggler into its OWN session.
# Unlike the :185-204 same-pgid child (which tmux's SIGHUP reaps), a setsid child
# escapes the pane pgid entirely — the #183/#139 repro the pre-harvest descendant
# reap must cover before the worktree is merged and removed. Sprint mode: writes the
# id-keyed done spec + a real code change so the run merges and tears the worktree down.
DETACHED_WRITER_FAKE_CLI = (
    r"""#!/usr/bin/env bash
set -e
rd="$BMAD_LOOP_RUN_DIR"; tid="$BMAD_LOOP_TASK_ID"; story="$BMAD_LOOP_STORY_KEY"
ts=$(date +%s%N)
ed="$BMAD_LOOP_EVENTS_DIR"
mkdir -p "$ed"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts" "$tid" > "$ed/$ts-$tid-SessionStart.json"
baseline=$(git rev-parse HEAD)

# Detach a straggler into a NEW session (setsid): $! is the setsid'd process itself
# (a non-interactive shell runs background jobs in its own pgrp, so setsid does not
# fork) — it now leads its own session and survives the pane pgid's SIGHUP.
setsid sleep 100000 &
child=$!
"""
    # The fake pins the shared hang ceiling (int for bash arithmetic) rather than a bare
    # literal, per DW-95/DW-108; the assert below is what holds that spelling. It is
    # `_run_detach_gate`, not this line, that varies the budget for the direct rows.
    + f"detach_ack_ceiling_s={int(REAL_MUX_HANG_CEILING_S)}\n"
    + AWAIT_DETACHED_SESSION_SH
    + r"""idfile="$rd/tasks/$tid/fake-child.pid"
"""
    + RECORD_CHILD_IDENTITY_SH
    + r"""
# Sprint-mode result: a real code change + the id-keyed done spec the dev synthesis
# reads back (written under the worktree cwd in isolation mode).
impl="_bmad-output/implementation-artifacts"
mkdir -p "$impl"
echo "impl for $story" >> src.txt
printf -- '---\ntitle: %s\nstatus: done\nbaseline_commit: %s\n---\n\n## Intent\n\nx\n\n## Auto Run Result\n\n- Status: done\n\nSummary: sprint.\n' \
    "$story" "$baseline" > "$impl/spec-$story.md"

ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \
    "$ts2" "$tid" > "$ed/$ts2-$tid-Stop.json"
# Stay alive like an idle interactive session so the pane shell is still live when
# the engine kills it: the harvest sees the detached child as our descendant only
# while we (its parent) are still around. Long enough to outlast Stop -> kill_window
# on a loaded CI host (the window kill terminates this sleep anyway, so it costs
# nothing) — else the shell could exit first, reparenting the child to init.
sleep 600
"""
)


# The publication-fault fake records its child outside the normal channel so the
# test can authenticate it even when fake-child.pid cannot be published.
OBSERVED_CHILD_GLOB = ".bmad-loop/runs/*/tasks/*/observed-child.id"

_PUBLICATION_FAULT_FRAGMENT = (
    r"""idfile="$rd/tasks/$tid/observed-child.id"
"""
    + RECORD_CHILD_IDENTITY_SH
    + r"""idfile="$rd/tasks/$tid/fake-child.pid"
mkdir -p "$idfile.tmp"
# A plain subshell preserves its own errexit. Using (...) || true would suppress
# errexit inside and let mv rename the poison directory into the normal channel.
set +e
(
export LC_ALL=C
set -e
"""
    + RECORD_CHILD_IDENTITY_SH
    + r""") 2> "$rd/tasks/$tid/recorder.stderr"
recorder_status=$?
set -e
printf '%s\n' "$recorder_status" > "$rd/tasks/$tid/recorder.status"
"""
)

# Substitute only the identity step; the detached fake and recorder stay unchanged.
assert DETACHED_WRITER_FAKE_CLI.count(RECORD_CHILD_IDENTITY_SH) == 1
PUBLICATION_FAULT_FAKE_CLI = DETACHED_WRITER_FAKE_CLI.replace(
    RECORD_CHILD_IDENTITY_SH, _PUBLICATION_FAULT_FRAGMENT, 1
)
assert PUBLICATION_FAULT_FAKE_CLI.count(RECORD_CHILD_IDENTITY_SH) == 2
# The gate sits ahead of the substituted `idfile=` line, so the publication-fault fake
# inherits it for free — correct, since it detaches the same way. ORDER is the whole
# property, not presence: a gate spliced AFTER the recorder would publish the identity
# first and re-establish exactly the race DW-159 closes, so pin the index too. The
# ceiling assignment is respelled rather than shared, so swapping the splice for a bare
# SHORT literal fails here.
#
# These asserts compare rendered TEXT, which on its own cannot tell the splice apart from
# a hardcoded `90` — the two render byte-identically. That is graded elsewhere (DW-174):
# `_scan_detach_ceiling_splices` in `tests/test_conftest.py` reads THIS module's own AST
# and requires the module-level `detach_ack_ceiling_s=` fragment to be followed by
# `int(<conftest REAL_MUX_HANG_CEILING_S>)`, under either import form, against a named
# expected-site inventory. Keep both halves: that scan observes only the EXPRESSION, while
# the splice ORDER pinned below and the derived `PUBLICATION_FAULT_FAKE_CLI` inheritance
# (a `str.replace` result, which holds no fragment of its own) are text properties no AST
# scan of this module sees.
for _fake in (DETACHED_WRITER_FAKE_CLI, PUBLICATION_FAULT_FAKE_CLI):
    assert _fake.count(AWAIT_DETACHED_SESSION_SH) == 1
    assert _fake.count(f"detach_ack_ceiling_s={int(REAL_MUX_HANG_CEILING_S)}\n") == 1
    assert _fake.index(AWAIT_DETACHED_SESSION_SH) < _fake.index(RECORD_CHILD_IDENTITY_SH)
del _fake


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _entry(story_id: str, **over) -> dict:
    d = {"id": story_id, "title": f"Story {story_id}", "description": "does a thing"}
    d.update(over)
    return d


def _scaffold(root: Path, entries: list[dict], *, install_skills=install_dev_base_skills) -> None:
    """A committed, clean sandbox: git repo, BMAD config + artifact dirs, the
    base-skill stubs the stories preflight requires (incl. the folder+id dispatch
    probe), a stories.yaml + SPEC.md, the fake-CLI profile, and a stories-mode
    policy — everything committed so the run-start worktree_clean gate passes.

    ``install_skills`` picks the dev-primitive ERA laid on disk: the legacy
    `install_dev_base_skills` by default, `install_build_auto_skill` for the
    post-rename tree. Both take ``(root, *, folder_id)`` and write the folder+id
    dispatch probe under whichever name `resolve_dev_primitive` will pick."""
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

    install_skills(root, folder_id=True)  # tree matches PROFILE_TOML's skill_tree

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


def _scaffold_renderer(
    root: Path, *, script: bool = True, config: bool = True, workflow: bool = True
) -> None:
    """A clean renderer-era stories sandbox with independently mutable surfaces.

    The helper is always present so ``script=False`` isolates the entry-point gate,
    while ``config=False`` isolates the central-config gate. Every mutation after
    :func:`_scaffold` is committed so the real run's clean-worktree check cannot
    hide a missing renderer finding behind an earlier refusal.
    """

    def install_renderer_skills(skill_root: Path, *, folder_id: bool) -> None:
        install_build_auto_skill(skill_root, folder_id=folder_id, renderer_stub=True)

    _scaffold(root, [_entry("1")], install_skills=install_renderer_skills)

    helper = root / RENDERER_CONFIG_UTILS_REL
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("# renderer config helper\n", encoding="utf-8")
    if script:
        (root / RENDERER_SCRIPT_REL).write_text(RENDERER_SCRIPT_IMPORTING_SIBLING, encoding="utf-8")
    if config:
        central = root / CENTRAL_CONFIG_REL
        central.parent.mkdir(parents=True, exist_ok=True)
        central.write_text("[core]\nname = 'renderer-e2e'\n", encoding="utf-8")
    if not workflow:
        (root / ".claude" / "skills" / DEV_PRIMITIVE_NEW / RENDERER_ENTRY_REL).unlink()

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "renderer fixture")


def _scaffold_sprint(
    root: Path, story_key: str, fake_cli: str = FAKE_CLI, extra_policy: str = ""
) -> None:
    """A committed, clean SPRINT-mode sandbox carrying the SAME new folder+id-
    capable bmad-dev-auto skill stub as `_scaffold` (with the folder+id dispatch
    probe content) — the regression point: installing that skill must not disturb
    the default sprint path. sprint-status.yaml holds one ready-for-dev story; the
    policy has NO [stories] section, so the run is plain sprint mode. ``fake_cli``
    swaps the CLI script (e.g. a never-Stop timeout fake); ``extra_policy`` appends
    to policy.toml (e.g. a [limits] block)."""
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
    install_dev_base_skills(root, folder_id=True)  # tree matches PROFILE_TOML's skill_tree

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
    fake.write_text(fake_cli, encoding="utf-8")
    os.chmod(fake, 0o755)
    profiles = root / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "fakestories.toml").write_text(
        PROFILE_TOML.format(binary=str(fake)), encoding="utf-8"
    )
    (root / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "fakestories"\n\n'
        "[review]\nenabled = false\n\n"
        '[gates]\nmode = "none"\n' + extra_policy,
        encoding="utf-8",
    )

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "e2e@test")
    _git(root, "config", "user.name", "e2e")
    _git(root, "config", "core.fsync", "none")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")


def _scaffold_sweep(root: Path) -> None:
    """A committed, clean SWEEP-mode sandbox: same folder+id-capable dev-skill
    stubs as `_scaffold_sprint`, but no sprint-status — instead a canonical
    deferred-work.md ledger with one open entry (DW-1). The policy has no
    [stories] section; the run is a plain `bmad-loop sweep`."""
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

    # the SAME folder+id-capable skill stubs the other scaffolds install
    install_dev_base_skills(root, folder_id=True)  # tree matches PROFILE_TOML's skill_tree

    # canonical DW-format ledger (no legacy content → migration is skipped)
    (impl / "deferred-work.md").write_text(
        "# Deferred Work\n\n"
        "### DW-1: item DW-1\n\n"
        "origin: test, 2026-06-01\nlocation: src.txt:1\nreason: test entry.\nstatus: open\n",
        encoding="utf-8",
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


def _dw_status(root: Path, dw_id: str) -> str:
    """The status field of one deferred-work ledger entry ('' if absent)."""
    text = (root / "_bmad-output" / "implementation-artifacts" / "deferred-work.md").read_text(
        encoding="utf-8"
    )
    in_entry = False
    for line in text.splitlines():
        if line.startswith(f"### {dw_id}:"):
            in_entry = True
        elif line.startswith("### "):
            in_entry = False
        elif in_entry and line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return ""


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


def test_e2e_two_story_happy_path_build_auto(tmp_path):
    """Scenario 9, the post-rename twin of the happy path (BMAD-METHOD #2651):
    the project carries ONLY `bmad-build-auto`, so the real CLI has to resolve the
    invoked primitive off disk. The fake routes on env vars and spec status and
    never on the skill name, so the run lands identically either era — the name
    assertion below is the whole difference between this row and the legacy one,
    and without it this test would pass with the resolution ablated.

    Asserted against the prompt the FAKE recorded (post-render, post-tmux argv),
    not the orchestrator's own prompt.txt: a name that never reached the binary
    would still be in prompt.txt."""
    root = tmp_path / "sbx"
    _scaffold(root, [_entry("1"), _entry("2")], install_skills=install_build_auto_skill)
    base = _commit_count(root)

    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _status(root, "1") == "done"
    assert _status(root, "2") == "done"
    assert _commit_count(root) == base + 2

    run_id = _run_id(root)
    run_dir = root / ".bmad-loop" / "runs" / run_id
    # A FINISHED run tears down its mux session (`cleanup_session_on_finish`); the
    # suite's one real-tmux pin of that — the timeout row now pauses (#727) and a
    # pause keeps the session for resume by design.
    assert not _tmux_has_session(f"bmad-loop-{run_id}")
    dispatched = [
        p.read_text(encoding="utf-8") for p in (run_dir / "tasks").glob("*/fake-prompt.txt")
    ]
    assert len(dispatched) == 2, dispatched  # one dev session per story, both recorded
    # The fakestories profile renders `{prompt}` verbatim, so the dispatch is the
    # literal skill invocation; a codex-shaped profile would rewrite it to `$skill`.
    assert all(p.startswith("/bmad-build-auto Spec folder: ") for p in dispatched), dispatched
    assert not any("bmad-dev-auto" in p for p in dispatched), dispatched


def test_e2e_renderer_missing_script_fails_validate_and_refuses_before_spawn(tmp_path):
    root = tmp_path / "sbx"
    _scaffold_renderer(root, script=False)

    validate = _run(root, "validate", "--json")
    assert validate.returncode == 1
    findings = {finding["check"]: finding for finding in json.loads(validate.stdout)["findings"]}
    renderer = findings["skills.dev-renderer"]
    assert renderer["severity"] == "problem"
    assert renderer["detail"] == {
        "tree": ".claude/skills",
        "skill": DEV_PRIMITIVE_NEW,
        "missing_scripts": [RENDERER_SCRIPT_REL],
    }

    run = _run(root, "run")
    assert run.returncode == 1
    assert RENDERER_SCRIPT_REL in run.stderr and "HALT" in run.stderr
    assert not (root / ".bmad-loop" / "runs").exists(), "preflight must precede session spawn"

    dry = _run(root, "run", "--dry-run")
    assert dry.returncode == 0, dry.stderr or dry.stdout
    assert "NOT runnable" in dry.stderr and RENDERER_SCRIPT_REL in dry.stderr
    assert "Story id: 1." in dry.stdout
    assert not (root / ".bmad-loop" / "runs").exists()


def test_e2e_renderer_missing_central_config_refuses_before_spawn(tmp_path):
    root = tmp_path / "sbx"
    _scaffold_renderer(root, config=False)

    run = _run(root, "run")
    assert run.returncode == 1
    assert CENTRAL_CONFIG_REL in run.stderr and "HALT" in run.stderr
    assert not (root / ".bmad-loop" / "runs").exists(), "preflight must precede session spawn"


def test_e2e_renderer_missing_workflow_refuses_before_spawn(tmp_path):
    root = tmp_path / "sbx"
    _scaffold_renderer(root, workflow=False)

    run = _run(root, "run")
    assert run.returncode == 1
    assert RENDERER_ENTRY_REL in run.stderr and "HALT" in run.stderr
    assert not (root / ".bmad-loop" / "runs").exists(), "preflight must precede session spawn"


def test_e2e_renderer_complete_surface_reaches_real_tmux_fake_cli(tmp_path):
    root = tmp_path / "sbx"
    _scaffold_renderer(root)
    base = _commit_count(root)

    run = _run(root, "run")
    assert run.returncode == 0, run.stderr or run.stdout
    assert _status(root, "1") == "done"
    assert _commit_count(root) == base + 1

    run_dir = root / ".bmad-loop" / "runs" / _run_id(root)
    dispatched = list((run_dir / "tasks").glob("*/fake-prompt.txt"))
    assert len(dispatched) == 1, dispatched
    assert dispatched[0].read_text(encoding="utf-8").startswith("/bmad-build-auto Spec folder: ")


def test_e2e_renderer_surface_is_seeded_into_isolated_real_tmux_session(tmp_path):
    """The required sandbox E2E for the runtime half of renderer support.

    The main checkout keeps the renderer files as ignored working-tree content,
    while the linked worktree starts without them. A green completion crosses the
    real CLI, worktree provision, completeness gates, tmux fake, verification and
    merge path; no LLM is invoked.
    """
    root = tmp_path / "sbx"
    _scaffold_renderer(root)
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8")
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    gitignore.write_text(
        prefix + f"{BMAD_SCRIPTS_SEED_REL}/\n{CENTRAL_CONFIG_REL}\n",
        encoding="utf-8",
    )
    policy = root / ".bmad-loop" / "policy.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8") + '\n[scm]\nisolation = "worktree"\n',
        encoding="utf-8",
    )
    _git(root, "rm", "-r", "--cached", "--", BMAD_SCRIPTS_SEED_REL, CENTRAL_CONFIG_REL)
    _git(root, "add", ".gitignore", ".bmad-loop/policy.toml")
    _git(root, "commit", "-q", "-m", "ignore renderer runtime surface")
    base = _commit_count(root)

    run = _run(root, "run")

    assert run.returncode == 0, run.stderr or run.stdout
    assert _status(root, "1") == "done"
    # Isolation records the unit commit and the local integration commit.
    assert _commit_count(root) == base + 2
    run_dir = root / ".bmad-loop" / "runs" / _run_id(root)
    kinds = [
        json.loads(line)["kind"]
        for line in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "worktree-opened" in kinds and "unit-merged" in kinds
    assert "story-escalated" not in kinds


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


# These harness rows spawn only local `sleep` processes and are the contract tests for
# the reap-identity helpers, which now live in tests/conftest.py because a second
# consumer on a different host gate needs them (tests/test_opencode_http.py's detached
# -descendant row, Linux-gated but tmux-free). They keep the module's Linux+tmux gate
# and xdist group: tmux is stricter than the helpers require, so the rows still run
# wherever the E2E consumers here do. No row below launches tmux or the orchestrator.


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def _live_child() -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        starttime = proc_starttime(proc.pid)
        assert starttime is not None, f"spawned child {proc.pid} has no /proc identity"
        return proc, starttime
    except BaseException:
        _reap(proc)
        raise


def test_recorded_identity_bash_and_python_agree(tmp_path):
    proc, starttime = _live_child()
    try:
        pid_file = tmp_path / "fake-child.pid"
        publication_target = tmp_path / "direct-write-target"
        pid_file.symlink_to(publication_target)
        subprocess.run(
            [
                "bash",
                "-c",
                f"set -e\nchild={proc.pid}\nidfile={shlex.quote(str(pid_file))}\n"
                + RECORD_CHILD_IDENTITY_SH,
            ],
            check=True,
            timeout=30,
        )
        assert not pid_file.is_symlink(), "the recorder must replace, not write through, the name"
        assert recorded_child(pid_file) == (proc.pid, starttime)
    finally:
        _reap(proc)


def _observed_session(pid: int) -> str:
    """The session id the detached-fake gate reads: index 3 after the `") "` strip."""
    stat = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
    return stat[stat.rindex(")") + 1 :].split()[3]


def _run_detach_gate(
    child: int, ceiling_s: int, *, head: str = "", tail: str = ""
) -> subprocess.CompletedProcess:
    """Drive AWAIT_DETACHED_SESSION_SH itself — the same text the fakes splice in.

    The subprocess wall is derived from the injected ceiling, never fixed: a fixed wall
    below a caller's budget would report `TimeoutExpired` from the harness instead of
    the gate's own bounded refusal, inverting which layer the row is grading.
    """
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -e\nchild={child}\ndetach_ack_ceiling_s={ceiling_s}\n"
            + head
            + AWAIT_DETACHED_SESSION_SH
            + tail,
        ],
        capture_output=True,
        text=True,
        timeout=ceiling_s + 30,
    )


def test_detach_gate_returns_only_once_the_child_left_the_runner_session():
    """DW-159: the gate is what makes detachment a FACT before the identity is published.

    A `setsid` child spawned the way the fake spawns one (not a process-group leader, so
    setsid(1) execs rather than forks and the pid is preserved). The gate may return only
    when the observed session differs from the runner shell's — and for a setsid child
    that session is the child's own pid, which is what the consumers' escaped-straggler
    premise rests on.
    """
    proc = subprocess.Popen(["setsid", "sleep", "30"])
    try:
        done = _run_detach_gate(proc.pid, 10, tail='printf %s "$own_session"\n')
        assert done.returncode == 0, done.stderr
        runner_session = done.stdout
        assert runner_session, done.stderr
        observed = _observed_session(proc.pid)
        assert observed != runner_session
        assert observed == str(proc.pid), f"setsid child {proc.pid} does not lead its session"
    finally:
        _reap(proc)


def test_detach_gate_retries_until_a_late_child_detaches(tmp_path):
    """Release the same-session child only when the gate reaches its retry sleep.

    A shell-local sleep function signals the child, then delegates to real sleep.
    The unmodified gate must observe the original session before it can release the
    child; parent scheduling cannot consume the delay before observation starts.
    The child execs setsid, preserving its pid just as the detached fake does.
    """
    release_file = tmp_path / "detach-release"
    release = shlex.quote(str(release_file))
    proc = subprocess.Popen(
        ["bash", "-c", f"while [[ ! -e {release} ]]; do sleep 0.05; done; exec setsid sleep 100000"]
    )
    try:
        done = _run_detach_gate(
            proc.pid,
            int(REAL_MUX_HANG_CEILING_S),
            head=f'sleep() {{ : > {release}; command sleep "$@"; }}\n',
            tail='printf %s "$own_session"\n',
        )
        assert done.returncode == 0, done.stderr
        assert release_file.exists(), "the gate never reached its retry sleep"
        assert _observed_session(proc.pid) != done.stdout
        assert _observed_session(proc.pid) == str(proc.pid)
    finally:
        _reap(proc)


@pytest.mark.parametrize("process_group", [None, 0], ids=["same-pgrp", "new-pgrp"])
@pytest.mark.parametrize("virtual_clock", [False, True], ids=["real-clock", "virtual-clock"])
def test_detach_gate_refuses_a_child_that_never_left_the_session(
    tmp_path, process_group, virtual_clock
):
    """Refuse and kill a same-session child, even if it leads a different group.

    Real-clock cases exercise bash's deadline; virtual-clock cases count retries
    against the injected budget without making a scheduler-sensitive timing claim.
    Unsetting SECONDS removes its special clock behavior for that shell, so each
    retry advances an ordinary variable by exactly one second.
    """
    proc = subprocess.Popen(["sleep", "100000"], process_group=process_group)
    try:
        assert os.getsid(proc.pid) == os.getsid(0)
        if process_group == 0:
            assert os.getpgid(proc.pid) == proc.pid
            assert os.getpgid(proc.pid) != os.getpgrp()
        pid_file = tmp_path / "fake-child.pid"
        ticks_file = tmp_path / "ticks"
        head = ""
        if virtual_clock:
            head = (
                "unset SECONDS\nSECONDS=0\n"
                f"sleep() {{ printf x >> {shlex.quote(str(ticks_file))}; "
                "SECONDS=$((SECONDS + 1)); }\n"
            )
        done = _run_detach_gate(
            proc.pid,
            2,
            head=head,
            tail=f"idfile={shlex.quote(str(pid_file))}\n" + RECORD_CHILD_IDENTITY_SH,
        )
        assert done.returncode != 0
        assert f"child {proc.pid} never left session {os.getsid(0)}" in done.stderr
        assert not pid_file.exists(), "an ungraded identity must never be published"
        assert proc.wait(timeout=30) == -signal.SIGKILL
        if virtual_clock:
            assert ticks_file.read_text() == "xx", "the gate did not honor its two-second budget"
    finally:
        _reap(proc)


@pytest.mark.parametrize(
    "raw",
    [
        "123\n",
        "123 456 789\n",
        "0 456\n",
        "123 0\n",
        "-123 456\n",
        "１２３ 456\n",
        "123 ٤٥٦\n",
        "123 456",
        "123\n456\n",
        "123\t456\n",
        "123\N{NO-BREAK SPACE}456\n",
        f'{"9" * 5000} 456\n',
    ],
)
def test_recorded_child_rejects_malformed_zero_and_non_ascii_identities(tmp_path, raw):
    pid_file = tmp_path / "fake-child.pid"
    pid_file.write_text(raw, encoding="utf-8")
    with pytest.raises(AssertionError, match="positive ASCII-decimal"):
        recorded_child(pid_file)


def test_reap_identity_binds_a_live_child(tmp_path):
    proc, starttime = _live_child()
    try:
        pid_file = tmp_path / "fake-child.pid"
        pid_file.write_text(f"{proc.pid} {starttime}\n", encoding="utf-8")
        assert recorded_child(pid_file) == (proc.pid, starttime)
        fd = bind_recorded_child(proc.pid, starttime)
        assert fd is not None
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            kill_recorded_child(fd)
    finally:
        _reap(proc)


def test_reap_identity_returns_none_for_a_reaped_child():
    proc, starttime = _live_child()
    _reap(proc)
    assert proc_starttime(proc.pid) != starttime
    assert bind_recorded_child(proc.pid, starttime) is None


def test_reap_identity_refuses_a_start_time_mismatch(monkeypatch):
    proc, starttime = _live_child()
    try:
        opened: list[int] = []
        real_open = os.pidfd_open

        def spy_open(pid: int) -> int:
            opened.append(pid)
            return real_open(pid)

        with monkeypatch.context() as patch:
            patch.setattr(os, "pidfd_open", spy_open)
            assert bind_recorded_child(proc.pid, str(int(starttime) + 1)) is None
        assert opened == [], "a mismatched process must not be bound"
        assert proc.poll() is None, "a mismatched process must not be signalled"
    finally:
        _reap(proc)


def test_reap_identity_closes_the_fd_when_the_pid_is_recycled_around_the_bind(monkeypatch):
    proc, starttime = _live_child()
    try:
        answers = iter([starttime, str(int(starttime) + 1)])
        closed: list[int] = []
        real_close = os.close

        def spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with monkeypatch.context() as patch:
            # conftest, not this module: bind_recorded_child resolves proc_starttime in
            # conftest's globals now, so a patch aimed here would silently no-op and the
            # row would pass without ever steering the re-authentication it is about.
            patch.setattr(conftest, "proc_starttime", lambda pid: next(answers))
            patch.setattr(os, "close", spy_close)
            assert bind_recorded_child(proc.pid, starttime) is None
        assert len(closed) == 1, "the pidfd must close after re-authentication fails"
    finally:
        _reap(proc)


def test_reap_identity_closes_the_fd_when_reauthentication_raises(monkeypatch):
    proc, starttime = _live_child()
    try:
        reads = 0
        closed: list[int] = []
        real_close = os.close

        def read_starttime(_pid: int) -> str:
            nonlocal reads
            reads += 1
            if reads == 1:
                return starttime
            raise PermissionError(errno.EACCES, "denied")

        def spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with monkeypatch.context() as patch:
            # conftest, not this module — see the recycled-pid row above.
            patch.setattr(conftest, "proc_starttime", read_starttime)
            patch.setattr(os, "close", spy_close)
            with pytest.raises(PermissionError):
                bind_recorded_child(proc.pid, starttime)
        assert reads == 2
        assert len(closed) == 1, "the pidfd must close when re-authentication raises"
        assert proc.poll() is None
    finally:
        _reap(proc)


def test_reap_identity_returns_none_when_pidfd_open_loses_the_process(monkeypatch):
    proc, starttime = _live_child()
    try:

        def disappeared(_pid: int) -> int:
            raise ProcessLookupError

        with monkeypatch.context() as patch:
            patch.setattr(os, "pidfd_open", disappeared)
            assert bind_recorded_child(proc.pid, starttime) is None
        assert proc.poll() is None, "the simulated open race must not signal the child"
    finally:
        _reap(proc)


@pytest.mark.parametrize("case", ["permission", "short", "delimiter", "nondigit"])
def test_proc_starttime_propagates_non_disappearance_and_malformed_failures(monkeypatch, case):
    def read_stat(_self, **_kwargs):
        if case == "permission":
            raise PermissionError(errno.EACCES, "denied")
        if case == "short":
            return "1 (sleep) S 0"
        if case == "delimiter":
            return "not a proc stat record"
        return "1 (sleep) " + " ".join(["S", *(["1"] * 18), "not-decimal"])

    expected = (
        PermissionError if case == "permission" else (IndexError if case == "short" else ValueError)
    )
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_stat)
        with pytest.raises(expected):
            proc_starttime(123)


def test_kill_recorded_child_actually_kills_through_the_fd():
    proc, starttime = _live_child()
    try:
        fd = bind_recorded_child(proc.pid, starttime)
        assert fd is not None
        kill_recorded_child(fd)
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        _reap(proc)


def test_kill_recorded_child_propagates_signal_failure_and_closes_fd(monkeypatch):
    proc, starttime = _live_child()
    try:
        fd = bind_recorded_child(proc.pid, starttime)
        assert fd is not None

        def denied(_fd: int, _sig: int) -> None:
            raise PermissionError(errno.EPERM, "denied")

        with monkeypatch.context() as patch:
            patch.setattr(signal, "pidfd_send_signal", denied)
            with pytest.raises(PermissionError):
                kill_recorded_child(fd)
        with pytest.raises(OSError) as excinfo:
            os.fstat(fd)
        assert excinfo.value.errno == errno.EBADF
        assert proc.poll() is None, "failed cleanup signalling must not imply disappearance"
    finally:
        _reap(proc)


def test_kill_recorded_child_ignores_disappearance_and_closes_fd(monkeypatch):
    fd = os.pidfd_open(os.getpid())

    def disappeared(_fd: int, _sig: int) -> None:
        raise ProcessLookupError

    with monkeypatch.context() as patch:
        patch.setattr(signal, "pidfd_send_signal", disappeared)
        kill_recorded_child(fd)
    with pytest.raises(OSError) as excinfo:
        os.fstat(fd)
    assert excinfo.value.errno == errno.EBADF


def test_live_child_reaps_its_process_when_identity_observation_fails(monkeypatch):
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    def denied(_pid: int) -> str:
        raise PermissionError(errno.EACCES, "denied")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", spy_popen)
        patch.setattr(sys.modules[__name__], "proc_starttime", denied)
        with pytest.raises(PermissionError):
            _live_child()
    assert len(spawned) == 1
    assert spawned[0].poll() == -signal.SIGKILL


def test_reap_identity_fails_loudly_when_pidfd_is_unsupported(monkeypatch):
    proc, starttime = _live_child()
    try:

        def unsupported(_pid: int) -> int:
            raise OSError(errno.ENOSYS, "pidfd_open not supported")

        with monkeypatch.context() as patch:
            patch.setattr(os, "pidfd_open", unsupported)
            with pytest.raises(OSError) as bind_error:
                bind_recorded_child(proc.pid, starttime)
            with pytest.raises(OSError) as preflight_error:
                preflight_pidfd_support()
        assert bind_error.value.errno == errno.ENOSYS
        assert preflight_error.value.errno == errno.ENOSYS

        preflight_fds: list[int] = []

        def blocked_signal(fd: int, _sig: int) -> None:
            preflight_fds.append(fd)
            raise PermissionError(errno.EPERM, "pidfd signalling blocked")

        with monkeypatch.context() as patch:
            patch.setattr(signal, "pidfd_send_signal", blocked_signal)
            with pytest.raises(PermissionError):
                preflight_pidfd_support()
        assert len(preflight_fds) == 1
        with pytest.raises(OSError) as excinfo:
            os.fstat(preflight_fds[0])
        assert excinfo.value.errno == errno.EBADF
    finally:
        _reap(proc)


def _plant_recorded_identity(
    root: Path, raw: str, task: str = "t0", *, filename: str = "fake-child.pid"
) -> Path:
    """Write ``raw`` where a fake CLI would record ``task``'s child identity.

    ``task`` is a parameter because the sweeper's central promise is that ONE bad
    file does not end the sweep; proving that needs two identities under one root,
    and `sorted()` over the glob makes the task-dir name the sweep order.
    ``filename`` selects an observation channel without changing existing callers.
    """
    pid_file = root / ".bmad-loop" / "runs" / "r0" / "tasks" / task / filename
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(raw, encoding="utf-8")
    return pid_file


def _warned_paths(record) -> str:
    return "\n".join(str(w.message) for w in record)


def test_recorded_children_swept_leaves_a_clean_block_alone(tmp_path):
    proc, starttime = _live_child()
    try:
        _plant_recorded_identity(tmp_path, f"{proc.pid} {starttime}\n")
        with recorded_children_swept(tmp_path):
            pass
        assert proc.poll() is None, "a clean exit must leave the fd owner's child alone"
    finally:
        _reap(proc)


def test_recorded_children_swept_reaps_a_recorded_child_and_reraises(tmp_path):
    proc, starttime = _live_child()
    try:
        _plant_recorded_identity(tmp_path, f"{proc.pid} {starttime}\n")
        with pytest.raises(RuntimeError, match="pre-bind boom"):
            with recorded_children_swept(tmp_path):
                raise RuntimeError("pre-bind boom")
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        _reap(proc)


def test_recorded_children_swept_warns_past_a_malformed_identity_and_keeps_sweeping(tmp_path):
    """One unparseable file must not end the sweep, nor get its number signalled.

    Two identities under one root, ordered by task dir so the malformed one is swept
    FIRST: `t0` holds bytes no parser accepts while a live process sits at that very
    number, and `t1` holds a valid live identity. The `t1` reap is what proves the
    loop reached past the failure — asserting only that `t0`'s process survived would
    pass just as well if the glob had matched nothing at all.
    """
    doomed, doomed_start = _live_child()
    survivor, _survivor_start = _live_child()
    try:
        bad = _plant_recorded_identity(tmp_path, f"{survivor.pid} garbage\n", task="t0")
        _plant_recorded_identity(tmp_path, f"{doomed.pid} {doomed_start}\n", task="t1")
        with pytest.warns(UserWarning, match="unauthenticated survivor") as record:
            with pytest.raises(RuntimeError, match="pre-bind boom"):
                with recorded_children_swept(tmp_path):
                    raise RuntimeError("pre-bind boom")
        assert doomed.wait(timeout=10) == -signal.SIGKILL, "the sweep stopped at the bad file"
        assert survivor.poll() is None, "an unparseable identity must not be signalled"
        assert str(bad) in _warned_paths(record), _warned_paths(record)
    finally:
        _reap(doomed)
        _reap(survivor)


def test_recorded_children_swept_never_signals_a_stale_start_time(tmp_path):
    """A start-time mismatch refuses the bind — silently, since nothing failed to parse.

    The second, valid identity is the control: it is reaped, so the sweep demonstrably
    ran and reached these files, which a bare "the stale process is still alive" check
    could not distinguish from a glob that matched nothing.
    """
    stale, stale_start = _live_child()
    doomed, doomed_start = _live_child()
    try:
        _plant_recorded_identity(tmp_path, f"{stale.pid} {int(stale_start) + 1}\n", task="t0")
        _plant_recorded_identity(tmp_path, f"{doomed.pid} {doomed_start}\n", task="t1")
        with pytest.raises(RuntimeError, match="pre-bind boom"):
            with recorded_children_swept(tmp_path):
                raise RuntimeError("pre-bind boom")
        assert doomed.wait(timeout=10) == -signal.SIGKILL, "the sweep never reached the files"
        assert stale.poll() is None, "a start-time mismatch must refuse the bind, not kill"
    finally:
        _reap(stale)
        _reap(doomed)


def test_recorded_children_swept_warns_when_the_bind_itself_fails(tmp_path, monkeypatch):
    """The OSError arm: a parseable identity whose bind raises something that is NOT
    proven disappearance. Without this row the handler could narrow to AssertionError
    alone and every other sweeper row would stay green."""
    proc, starttime = _live_child()
    try:
        pid_file = _plant_recorded_identity(tmp_path, f"{proc.pid} {starttime}\n")

        def unsupported(_pid: int) -> int:
            raise OSError(errno.ENOSYS, "pidfd_open not supported")

        with monkeypatch.context() as patch:
            patch.setattr(os, "pidfd_open", unsupported)
            with pytest.warns(UserWarning, match="unauthenticated survivor") as record:
                with pytest.raises(RuntimeError, match="pre-bind boom"):
                    with recorded_children_swept(tmp_path):
                        raise RuntimeError("pre-bind boom")
        assert str(pid_file) in _warned_paths(record), _warned_paths(record)
        assert proc.poll() is None, "a failed bind must not fall back to signalling the pid"
    finally:
        _reap(proc)


def test_recorded_children_swept_warns_past_undecodable_bytes_and_keeps_sweeping(tmp_path):
    """A non-UTF-8 record raises UnicodeDecodeError out of `read_text`, not
    AssertionError — a handler listing only parse-shaped types would let it REPLACE
    the in-flight exception and abandon every later file."""
    doomed, doomed_start = _live_child()
    try:
        bad = tmp_path / ".bmad-loop" / "runs" / "r0" / "tasks" / "t0" / "fake-child.pid"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xfe 123\n")
        _plant_recorded_identity(tmp_path, f"{doomed.pid} {doomed_start}\n", task="t1")
        with pytest.warns(UserWarning, match="unauthenticated survivor") as record:
            with pytest.raises(RuntimeError, match="pre-bind boom"):
                with recorded_children_swept(tmp_path):
                    raise RuntimeError("pre-bind boom")
        assert doomed.wait(timeout=10) == -signal.SIGKILL, "the sweep stopped at the bad file"
        assert str(bad) in _warned_paths(record), _warned_paths(record)
    finally:
        _reap(doomed)


def test_recorded_children_swept_keeps_the_original_error_when_warnings_are_errors(tmp_path):
    doomed, doomed_start = _live_child()
    try:
        _plant_recorded_identity(tmp_path, "garbage\n", task="t0")
        _plant_recorded_identity(tmp_path, f"{doomed.pid} {doomed_start}\n", task="t1")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(RuntimeError, match="pre-bind boom"):
                with recorded_children_swept(tmp_path):
                    raise RuntimeError("pre-bind boom")
        assert doomed.wait(timeout=10) == -signal.SIGKILL, "warning failure stopped the sweep"
    finally:
        _reap(doomed)


def test_recorded_children_swept_warns_past_signal_failure_and_keeps_sweeping(
    tmp_path, monkeypatch
):
    survivor, survivor_start = _live_child()
    doomed, doomed_start = _live_child()
    try:
        first = _plant_recorded_identity(tmp_path, f"{survivor.pid} {survivor_start}\n", task="t0")
        _plant_recorded_identity(tmp_path, f"{doomed.pid} {doomed_start}\n", task="t1")
        real_send_signal = signal.pidfd_send_signal
        calls = 0

        def fail_first_signal(fd: int, sig: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError(errno.EPERM, "denied")
            real_send_signal(fd, sig)

        with monkeypatch.context() as patch:
            patch.setattr(signal, "pidfd_send_signal", fail_first_signal)
            with pytest.warns(UserWarning, match="unauthenticated survivor") as record:
                with pytest.raises(RuntimeError, match="pre-bind boom"):
                    with recorded_children_swept(tmp_path):
                        raise RuntimeError("pre-bind boom")
        assert calls == 2
        assert str(first) in _warned_paths(record), _warned_paths(record)
        assert survivor.poll() is None, "failed signalling must not imply disappearance"
        assert doomed.wait(timeout=10) == -signal.SIGKILL, "signal failure stopped the sweep"
    finally:
        _reap(survivor)
        _reap(doomed)


def test_recorded_children_swept_preserves_partial_glob_results(tmp_path, monkeypatch):
    doomed, doomed_start = _live_child()
    try:
        pid_file = _plant_recorded_identity(tmp_path, f"{doomed.pid} {doomed_start}\n")
        real_glob = Path.glob

        def interrupted_glob(path: Path, pattern: str):
            if path == tmp_path and pattern == RECORDED_CHILD_GLOB:
                yield pid_file
                raise OSError(errno.EIO, "traversal interrupted")
            yield from real_glob(path, pattern)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "glob", interrupted_glob)
            with pytest.warns(UserWarning, match="traversal interrupted"):
                with pytest.raises(RuntimeError, match="pre-bind boom"):
                    with recorded_children_swept(tmp_path):
                        raise RuntimeError("pre-bind boom")
        assert doomed.wait(timeout=10) == -signal.SIGKILL
    finally:
        _reap(doomed)


def test_recorded_children_swept_is_a_noop_without_identity_files(tmp_path):
    with pytest.raises(RuntimeError, match="pre-bind boom"):
        with recorded_children_swept(tmp_path):
            raise RuntimeError("pre-bind boom")


@pytest.mark.parametrize(
    "surface_name",
    [
        "test_e2e_session_timeout_teardown",
        "test_e2e_detached_writer_reaped_before_worktree_teardown",
        "test_e2e_detached_writer_publication_fault_still_reaped",
    ],
)
def test_reap_e2e_preflight_failure_prevents_run(tmp_path, monkeypatch, surface_name):
    run_called = False

    def unsupported() -> None:
        raise OSError(errno.ENOSYS, "pidfd unavailable")

    def unexpected_run(*_args, **_kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("_run must not be reached after preflight failure")

    with monkeypatch.context() as patch:
        patch.setattr(sys.modules[__name__], "_scaffold_sprint", lambda *_args, **_kwargs: None)
        patch.setattr(sys.modules[__name__], "preflight_pidfd_support", unsupported)
        patch.setattr(sys.modules[__name__], "_run", unexpected_run)
        with pytest.raises(OSError, match="pidfd unavailable"):
            globals()[surface_name](tmp_path, monkeypatch, False)
    assert not run_called


@pytest.mark.parametrize(
    "surface_name",
    [
        "test_e2e_session_timeout_teardown",
        "test_e2e_detached_writer_reaped_before_worktree_teardown",
    ],
)
def test_reap_e2e_sweeps_a_recorded_child_when_the_run_fails(tmp_path, monkeypatch, surface_name):
    """DW-137: a failure anywhere in the pre-bind window must not leak the child.

    Both surfaces spawn their fake child inside `_run` but can only bind a pidfd after
    the run directory and its `fake-child.pid` are discovered. Driving `_run` straight
    into a raise reproduces that window exactly; delete either `recorded_children_swept`
    wrap and the planted child survives this row.
    """
    proc, starttime = _live_child()
    try:
        _plant_recorded_identity(tmp_path / "sbx", f"{proc.pid} {starttime}\n")

        def failing_run(*_args, **_kwargs):
            raise RuntimeError("run refused")

        with monkeypatch.context() as patch:
            patch.setattr(sys.modules[__name__], "_scaffold_sprint", lambda *_a, **_kw: None)
            patch.setattr(sys.modules[__name__], "_run", failing_run)
            with pytest.raises(RuntimeError, match="run refused"):
                globals()[surface_name](tmp_path, monkeypatch, False)
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        _reap(proc)


def test_recorded_children_swept_sweeps_only_the_named_channel(tmp_path):
    observed, observed_start = _live_child()
    recorded, recorded_start = _live_child()
    try:
        _plant_recorded_identity(
            tmp_path, f"{observed.pid} {observed_start}\n", filename="observed-child.id"
        )
        _plant_recorded_identity(tmp_path, f"{recorded.pid} {recorded_start}\n")
        with pytest.raises(RuntimeError, match="pre-bind boom"):
            with recorded_children_swept(tmp_path, glob=OBSERVED_CHILD_GLOB):
                raise RuntimeError("pre-bind boom")
        assert observed.wait(timeout=10) == -signal.SIGKILL
        assert recorded.poll() is None, "the unnamed channel must remain unswept"
    finally:
        _reap(observed)
        _reap(recorded)


def test_reap_e2e_sweeps_an_observed_child_when_the_run_fails(tmp_path, monkeypatch):
    proc, starttime = _live_child()
    try:
        _plant_recorded_identity(
            tmp_path / "sbx", f"{proc.pid} {starttime}\n", filename="observed-child.id"
        )

        def failing_run(*_args, **_kwargs):
            raise RuntimeError("run refused")

        with monkeypatch.context() as patch:
            patch.setattr(sys.modules[__name__], "_scaffold_sprint", lambda *_a, **_kw: None)
            patch.setattr(sys.modules[__name__], "_run", failing_run)
            with pytest.raises(RuntimeError, match="run refused"):
                test_e2e_detached_writer_publication_fault_still_reaped(
                    tmp_path, monkeypatch, False
                )
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        _reap(proc)


def _tmux_has_session(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def _tmux_window_names(session: str) -> list[str]:
    out = subprocess.run(
        ["tmux", "list-windows", "-t", f"={session}", "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


@pytest.mark.parametrize(
    "force_live_reap_assertion", [False, True], ids=["reaped", "live-assertion"]
)
def test_e2e_session_timeout_teardown(tmp_path, monkeypatch, force_live_reap_assertion):
    """#157 end to end through the real binary + real tmux: a dev session wedged
    forever (SessionStart, then sleep — never a Stop) is bounded only by the
    session timeout, and the fix makes that firing timely and observable. The
    1-minute policy floor is too coarse for a fast test, so the engine's
    BMAD_LOOP_SESSION_TIMEOUT_S seam drives a 3-second budget.

    Since #727 the same session is also the no-work shape — it painted once and
    never changed its pane before the deadline — so the run PAUSES at escalation
    (`no work produced: dev session timeout`) instead of deferring, and a pause
    deliberately leaves the run's mux SESSION for `resume` to reuse. The teardown
    under test is the agent window's: it and its process tree must be gone, and the
    session must hold nothing but its root shell window. The session itself is
    killed on the way out so the host is not left with an orphan."""
    root = tmp_path / "sbx"
    story = "1-1-timeout"
    _scaffold_sprint(
        root,
        story,
        fake_cli=TIMEOUT_FAKE_CLI,
        extra_policy="\n[limits]\nmax_dev_attempts = 1\nteardown_grace_s = 5\n",
    )
    # inherited by the `bmad-loop run` subprocess (_run passes no env=)
    monkeypatch.setenv("BMAD_LOOP_SESSION_TIMEOUT_S", "3")

    preflight_pidfd_support()
    # Initialized BEFORE the try so the finally below stays correct no matter how
    # early the setup region raises.
    recorded_fd: int | None = None
    poll_fd: int | None = None
    injected_child: subprocess.Popen | None = None
    poll_failure: AssertionError | None = None
    injected_exit: int | None = None
    run_id: str | None = None
    try:
        # Everything up to the bind is the pre-bind window: the fake CLI's child is
        # already running but no fd names it yet, so a `_run` timeout or any assertion
        # in here would leave it alive and uncleanable. The sweeper rediscovers and
        # authenticates the recorded identities from disk on the way out.
        with recorded_children_swept(root):
            proc = _run(root, "run", timeout=90)
            assert proc.returncode == 0, proc.stderr or proc.stdout

            run_id = _run_id(root)
            run_dir = root / ".bmad-loop" / "runs" / run_id

            # (1) session-end status=timeout, journaled promptly, with the fire forensics
            journal = [
                json.loads(ln)
                for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            ends = [
                j for j in journal if j["kind"] == "session-end" and j.get("status") == "timeout"
            ]
            assert ends, f"no session-end status=timeout: {[j['kind'] for j in journal]}"
            end = ends[0]
            assert end.get("fired_at"), end
            assert (
                end["teardown_s"] < 15.0
            ), f"teardown gap not small (kill hung?): {end['teardown_s']}"
            assert end.get("expired_clock") in ("monotonic", "wall", "both"), end
            task_id = end["task_id"]

            tdir = run_dir / "tasks" / task_id
            pid_file = tdir / "fake-child.pid"
            assert pid_file.is_file(), "fake CLI never recorded its sleep child"
            # The fake builds this path from $BMAD_LOOP_RUN_DIR/$BMAD_LOOP_TASK_ID
            # while the sweeper rediscovers it through RECORDED_CHILD_GLOB. Pin the
            # two together: a producer-side layout change would otherwise make the
            # sweeper a silent no-op with every row still green.
            assert pid_file in set(root.glob(RECORDED_CHILD_GLOB)), (
                f"{pid_file} is outside RECORDED_CHILD_GLOB ({RECORDED_CHILD_GLOB}), so the "
                f"pre-bind sweeper could never find it"
            )
            recorded_pid, recorded_start = recorded_child(pid_file)
            recorded_fd = bind_recorded_child(recorded_pid, recorded_start)
            if recorded_fd is None:
                assert proc_starttime(recorded_pid) != recorded_start, (
                    f"bind returned None while pid {recorded_pid} still carries the recorded "
                    f"start time {recorded_start}: the reap poll would be skipped without evidence"
                )

        poll_pid = recorded_pid
        if force_live_reap_assertion:
            # Verify the real fake-CLI child first. If it is still signalable, safely
            # clean that exact process before substituting the fault-injection child.
            real_fd, recorded_fd = recorded_fd, None
            kill_recorded_child(real_fd)
            injected_child, injected_start = _live_child()
            poll_pid = injected_child.pid
            poll_fd = bind_recorded_child(poll_pid, injected_start)
            assert poll_fd is not None, "the forced-live child must bind before the reap poll"
        else:
            poll_fd, recorded_fd = recorded_fd, None

        # (2) the fire moment left a timeout-fired breadcrumb, distinct from teardown
        life = [
            json.loads(ln)
            for ln in (tdir / "session-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert any(ln.get("event") == "timeout-fired" for ln in life), life

        # (3) the wait loop's proof-of-life exists and is recent (not the frozen gap)
        hb = json.loads((tdir / "heartbeat.json").read_text(encoding="utf-8"))
        assert time.time() - hb["ts"] < 120, hb

        # (4) teardown actually reaped the agent window and its process tree. The
        # run is paused (see the docstring), so the session survives by design —
        # with only its root shell window left, never the task's.
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        assert state.get("paused_stage") == "escalation", state
        assert str(state.get("paused_reason", "")).startswith(
            "no work produced: dev session timeout"
        ), state
        assert end.get("produced_work") is False, end
        session_name = f"bmad-loop-{run_id}"
        assert _tmux_has_session(session_name)
        windows = _tmux_window_names(session_name)
        assert task_id[-40:] not in windows and len(windows) == 1, windows
        if shutil.which("pgrep"):
            pg = subprocess.run(["pgrep", "-af", "fake-cli.sh"], capture_output=True, text=True)
            assert not [ln for ln in pg.stdout.splitlines() if str(root) in ln], pg.stdout

        # The identity is a pidfd authenticated against the start time captured at
        # spawn, so a recycled pid can neither fake a survivor nor receive cleanup.
        # Signal 0 remains zombie-tolerant: it succeeds until the exact child is reaped.
        # A just-killed zombie can linger until init runs, and scheduler starvation can
        # stretch that wait, so use the shared hang ceiling rather than a tight budget;
        # a merely slow reaper gets more time, while a broken one still fails.
        with monkeypatch.context() as clock_patch:
            if force_live_reap_assertion:
                moments = iter([0.0, REAL_MUX_HANG_CEILING_S])
                clock_patch.setattr(time, "monotonic", lambda: next(moments))
            try:
                deadline = time.monotonic() + REAL_MUX_HANG_CEILING_S
                while poll_fd is not None:
                    try:
                        signal.pidfd_send_signal(poll_fd, 0)
                    except ProcessLookupError:
                        break  # dead and reaped — teardown covered the descendant
                    assert time.monotonic() < deadline, f"sleep child {poll_pid} survived teardown"
                    time.sleep(0.1)
            except AssertionError as exc:
                poll_failure = exc
                if not force_live_reap_assertion:
                    raise
    finally:
        try:
            try:
                kill_recorded_child(poll_fd)
                if injected_child is not None:
                    injected_exit = injected_child.wait(timeout=10)
            finally:
                kill_recorded_child(recorded_fd)
        finally:
            if injected_child is not None and injected_child.poll() is None:
                _reap(injected_child)
            # The paused run left its session for a resume that never comes.
            if run_id is not None:
                subprocess.run(
                    ["tmux", "kill-session", "-t", f"=bmad-loop-{run_id}"], capture_output=True
                )

    if force_live_reap_assertion:
        assert str(poll_failure).splitlines()[0] == f"sleep child {poll_pid} survived teardown"
        assert injected_exit == -signal.SIGKILL


@pytest.mark.parametrize(
    "force_live_reap_assertion", [False, True], ids=["reaped", "live-assertion"]
)
def test_e2e_detached_writer_reaped_before_worktree_teardown(
    tmp_path, monkeypatch, force_live_reap_assertion
):
    """#183/#139 end to end: a dev session `setsid`-detaches a straggler into its own
    session (escaping the pane pgid tmux's SIGHUP reaps), then ends CLEANLY via a
    Stop + done spec. The verified-kill reap must chase the harvested descendant tree
    and reap the straggler BEFORE the worktree is merged and removed — so the
    detached pid is dead, the worktree is gone, and no `worktree-teardown-degraded`
    fires (the #139 signature). Worktree isolation makes the teardown real."""
    root = tmp_path / "sbx"
    story = "1-1-detach"
    _scaffold_sprint(
        root,
        story,
        fake_cli=DETACHED_WRITER_FAKE_CLI,
        extra_policy=(
            '\n[scm]\nisolation = "worktree"\n\n'
            "[limits]\nmax_dev_attempts = 1\nteardown_grace_s = 10\n"
        ),
    )
    preflight_pidfd_support()
    # Initialized BEFORE the try so the finally below stays correct no matter how
    # early the setup region raises.
    recorded_fd: int | None = None
    poll_fd: int | None = None
    injected_child: subprocess.Popen | None = None
    poll_failure: AssertionError | None = None
    injected_exit: int | None = None
    try:
        # The pre-bind window: the setsid'd straggler exists but no fd names it until
        # the glob below finds its identity file, so a `_run` timeout or a missing
        # record used to leak it. The sweeper covers exactly that stretch.
        with recorded_children_swept(root):
            proc = _run(root, "run", timeout=120)
            assert proc.returncode == 0, proc.stderr or proc.stdout

            run_id = _run_id(root)
            run_dir = root / ".bmad-loop" / "runs" / run_id
            pid_files = list((run_dir / "tasks").glob("*/fake-child.pid"))
            assert pid_files, "fake CLI never recorded its setsid child"
            # Same producer/consumer pin as the timeout E2E above.
            assert pid_files[0] in set(root.glob(RECORDED_CHILD_GLOB)), (
                f"{pid_files[0]} is outside RECORDED_CHILD_GLOB ({RECORDED_CHILD_GLOB}), so the "
                f"pre-bind sweeper could never find it"
            )
            recorded_pid, recorded_start = recorded_child(pid_files[0])
            recorded_fd = bind_recorded_child(recorded_pid, recorded_start)
            if recorded_fd is None:
                assert proc_starttime(recorded_pid) != recorded_start, (
                    f"bind returned None while pid {recorded_pid} still carries the recorded "
                    f"start time {recorded_start}: the reap poll would be skipped blind"
                )

        poll_pid = recorded_pid
        if force_live_reap_assertion:
            # Verify or safely clean the real straggler before the injected child takes
            # over the protected poll; keep the two identities distinct throughout.
            real_fd, recorded_fd = recorded_fd, None
            kill_recorded_child(real_fd)
            injected_child, injected_start = _live_child()
            poll_pid = injected_child.pid
            poll_fd = bind_recorded_child(poll_pid, injected_start)
            assert poll_fd is not None, "the forced-live child must bind before the reap poll"
        else:
            poll_fd, recorded_fd = recorded_fd, None

        # (1) clean end: the story landed done and merged, not a timeout/stall
        assert _sprint_status(root, story) == "done"
        journal = [
            json.loads(ln)
            for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        kinds = [j["kind"] for j in journal]
        assert "unit-merged" in kinds, kinds
        # (2) the #139 failure signature is ABSENT — the worktree teardown was clean
        assert "worktree-teardown-degraded" not in kinds, kinds

        # (3) no unit worktree survives (git sees only the main checkout)
        wt = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        )
        mounts = [ln for ln in wt.stdout.splitlines() if ln.startswith("worktree ")]
        assert len(mounts) == 1, wt.stdout  # only the primary checkout remains

        # (4) poll the start-time-authenticated pidfd. Signal 0 preserves the old
        # alive-or-zombie semantics, while the bound identity cannot follow pid reuse.
        # A zombie can linger until init runs and a starved scheduler can stretch that
        # wait, so the shared hang ceiling remains deliberately distinct from the 10s
        # teardown_grace_s; a reap that never happens still fails at the ceiling.
        with monkeypatch.context() as clock_patch:
            if force_live_reap_assertion:
                moments = iter([0.0, REAL_MUX_HANG_CEILING_S])
                clock_patch.setattr(time, "monotonic", lambda: next(moments))
            try:
                deadline = time.monotonic() + REAL_MUX_HANG_CEILING_S
                while poll_fd is not None:
                    try:
                        signal.pidfd_send_signal(poll_fd, 0)
                    except ProcessLookupError:
                        break  # reaped by the descendant sweep before teardown
                    assert (
                        time.monotonic() < deadline
                    ), f"detached child {poll_pid} survived teardown"
                    time.sleep(0.1)
            except AssertionError as exc:
                poll_failure = exc
                if not force_live_reap_assertion:
                    raise
    finally:
        try:
            try:
                kill_recorded_child(poll_fd)
                if injected_child is not None:
                    injected_exit = injected_child.wait(timeout=10)
            finally:
                kill_recorded_child(recorded_fd)
        finally:
            if injected_child is not None and injected_child.poll() is None:
                _reap(injected_child)

    if force_live_reap_assertion:
        assert str(poll_failure).splitlines()[0] == (f"detached child {poll_pid} survived teardown")
        assert injected_exit == -signal.SIGKILL


@pytest.mark.parametrize(
    "force_live_reap_assertion", [False, True], ids=["reaped", "live-assertion"]
)
def test_e2e_detached_writer_publication_fault_still_reaped(
    tmp_path, monkeypatch, force_live_reap_assertion
):
    """DW-149: failed identity publication still permits exact-child reap attribution.

    The test owns a separate observer record, so it can grade child death and the
    straggler-reap breadcrumb despite the normal recorder's demonstrated failure.
    """
    root = tmp_path / "sbx"
    story = "1-1-pubfault"
    _scaffold_sprint(
        root,
        story,
        fake_cli=PUBLICATION_FAULT_FAKE_CLI,
        extra_policy=(
            '\n[scm]\nisolation = "worktree"\n\n'
            "[limits]\nmax_dev_attempts = 1\nteardown_grace_s = 10\n"
        ),
    )
    preflight_pidfd_support()
    # Initialized BEFORE the try so the finally below stays correct no matter how
    # early the setup region raises.
    recorded_fd: int | None = None
    poll_fd: int | None = None
    injected_child: subprocess.Popen | None = None
    poll_failure: AssertionError | None = None
    injected_exit: int | None = None
    try:
        # Sweep the channel this fake actually publishes before any fd owns it.
        with recorded_children_swept(root, glob=OBSERVED_CHILD_GLOB):
            proc = _run(root, "run", timeout=120)
            assert proc.returncode == 0, proc.stderr or proc.stdout

            run_id = _run_id(root)
            run_dir = root / ".bmad-loop" / "runs" / run_id
            pid_files = list(root.glob(OBSERVED_CHILD_GLOB))
            assert len(pid_files) == 1, f"expected exactly one observation record: {pid_files}"
            tdir = pid_files[0].parent
            assert tdir.parent == run_dir / "tasks"
            assert (tdir / "fake-child.pid.tmp").is_dir(), "publication poison is missing"
            assert not set(
                root.glob(RECORDED_CHILD_GLOB)
            ), "normal publication unexpectedly succeeded"
            assert not list(run_dir.rglob("fake-child.pid"))

            # Persisted evidence of the actual invocation: an unexecuted recorder
            # with a poison directory and an absent record must not satisfy the row.
            recorder_status = int((tdir / "recorder.status").read_text(encoding="utf-8"))
            recorder_stderr = (tdir / "recorder.stderr").read_text(encoding="utf-8")
            assert recorder_status != 0, "publication recorder did not fail"
            assert (
                f"{tdir / 'fake-child.pid.tmp'}: Is a directory" in recorder_stderr
            ), f"publication recorder did not hit the poisoned directory: {recorder_stderr!r}"
            recorded_pid, recorded_start = recorded_child(pid_files[0])
            recorded_fd = bind_recorded_child(recorded_pid, recorded_start)
            if recorded_fd is None:
                assert proc_starttime(recorded_pid) != recorded_start, (
                    f"bind returned None while pid {recorded_pid} still carries the recorded "
                    f"start time {recorded_start}: the reap poll would be skipped blind"
                )

        poll_pid = recorded_pid
        if force_live_reap_assertion:
            # Verify or safely clean the real straggler before the injected child takes
            # over the protected poll; keep the two identities distinct throughout.
            real_fd, recorded_fd = recorded_fd, None
            kill_recorded_child(real_fd)
            injected_child, injected_start = _live_child()
            poll_pid = injected_child.pid
            poll_fd = bind_recorded_child(poll_pid, injected_start)
            assert poll_fd is not None, "the forced-live child must bind before the reap poll"
        else:
            poll_fd, recorded_fd = recorded_fd, None

        # (1) clean end: the story landed done and merged, not a timeout/stall
        assert _sprint_status(root, story) == "done"
        journal = [
            json.loads(ln)
            for ln in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        kinds = [j["kind"] for j in journal]
        assert "unit-merged" in kinds, kinds
        # (2) the #139 failure signature is ABSENT — the worktree teardown was clean
        assert "worktree-teardown-degraded" not in kinds, kinds

        # (3) no unit worktree survives (git sees only the main checkout)
        wt = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
        )
        mounts = [ln for ln in wt.stdout.splitlines() if ln.startswith("worktree ")]
        assert len(mounts) == 1, wt.stdout  # only the primary checkout remains

        # (4) poll the start-time-authenticated pidfd. Signal 0 preserves the old
        # alive-or-zombie semantics, while the bound identity cannot follow pid reuse.
        # A zombie can linger until init runs and a starved scheduler can stretch that
        # wait, so the shared hang ceiling remains deliberately distinct from the 10s
        # teardown_grace_s; a reap that never happens still fails at the ceiling.
        with monkeypatch.context() as clock_patch:
            if force_live_reap_assertion:
                moments = iter([0.0, REAL_MUX_HANG_CEILING_S])
                clock_patch.setattr(time, "monotonic", lambda: next(moments))
            try:
                deadline = time.monotonic() + REAL_MUX_HANG_CEILING_S
                while poll_fd is not None:
                    try:
                        signal.pidfd_send_signal(poll_fd, 0)
                    except ProcessLookupError:
                        break  # reaped by the descendant sweep
                    assert (
                        time.monotonic() < deadline
                    ), f"detached child {poll_pid} survived teardown"
                    time.sleep(0.1)
            except AssertionError as exc:
                poll_failure = exc
                if not force_live_reap_assertion:
                    raise

        # (5) Require attribution to this exact child. kill-escalated identifies
        # pane roots and cannot prove the observed descendant was reaped.
        lifecycle = [
            json.loads(line)
            for line in (tdir / "session-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            event.get("event") == "straggler-reap" and recorded_pid in event.get("pids", [])
            for event in lifecycle
        ), f"no straggler-reap names observed child {recorded_pid}: {lifecycle}"
    finally:
        try:
            try:
                kill_recorded_child(poll_fd)
                if injected_child is not None:
                    injected_exit = injected_child.wait(timeout=10)
            finally:
                kill_recorded_child(recorded_fd)
        finally:
            if injected_child is not None and injected_child.poll() is None:
                _reap(injected_child)

    if force_live_reap_assertion:
        assert str(poll_failure).splitlines()[0] == (f"detached child {poll_pid} survived teardown")
        assert injected_exit == -signal.SIGKILL


def test_e2e_sweep_intent_gap_patch_restore(tmp_path):
    # Scenario 8 (#75): a SWEEP deferred-work bundle hits an intent gap during its
    # dev session — the patch is saved, the tree reverted, and the run escalates.
    # `resolve --restore-patch` re-arms the bundle spec to in-review + re-stamps its
    # baseline; resume re-applies the patch and dispatches an EXPLICIT spec pointer
    # (Change A) so the bundle resumes review on the restored diff instead of
    # re-implementing. This is the only scenario that drives the sweep-specific CLI
    # resolve→resume path (SweepEngine rebuilt from sweep.json in _resume_paused_run).
    root = tmp_path / "sbx"
    _scaffold_sweep(root)
    story = "dw-fix"  # triage names the bundle "fix" → task key dw-fix
    (root / f".intent-gap-{story}").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "poison: intent gap")
    impl = root / "_bmad-output" / "implementation-artifacts"
    spec = impl / f"spec-{story}.md"
    patch = impl / f"attempt-{story}.patch"

    # triage → bundle dev halts on the intent gap: patch saved, tree reverted
    proc = _run(root, "sweep", "--no-prompt")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "status: blocked" in spec.read_text(encoding="utf-8")
    assert patch.is_file()  # the attempted change survives the revert
    assert "attempted reading" not in (root / "src.txt").read_text(encoding="utf-8")
    assert _dw_status(root, "DW-1").startswith("open")  # a blocked pass does not close it
    run_id = _run_id(root)

    # resolve latches the restore: bundle spec → in-review + baseline re-stamped
    resolve = _run(
        root, "resolve", run_id, "--no-interactive", "--no-resume", "--restore-patch", str(patch)
    )
    assert resolve.returncode == 0, resolve.stderr or resolve.stdout
    text = spec.read_text(encoding="utf-8")
    assert "status: in-review" in text  # restore routing: step-01 → step-04
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert f"baseline_revision: {head}" in text  # F2: spec baseline re-stamped

    # resume: patch re-applied, review resumed, bundle lands done + ledger closed
    resume = _run(root, "resume", run_id)
    assert resume.returncode == 0, resume.stderr or resume.stdout
    final = spec.read_text(encoding="utf-8")
    assert "status: done" in final, final  # fake blocks loudly on a broken contract
    assert _dw_status(root, "DW-1").startswith("done")  # the bundle closed the ledger id
    src = (root / "src.txt").read_text(encoding="utf-8")
    assert src.count("attempted reading") == 1  # restored from the patch, not re-implemented
    # Change A: the re-drive dispatch pointed at the bundle spec, never the intent.md
    run_dir = root / ".bmad-loop" / "runs" / run_id
    prompts = [p.read_text(encoding="utf-8") for p in (run_dir / "tasks").glob("*/prompt.txt")]
    assert any(str(spec) in p for p in prompts)


def test_e2e_a_relay_that_only_knows_the_legacy_events_dir_still_completes(tmp_path):
    """The #494 version-skew guard, through the real CLI and real tmux.

    `bmad_loop_hook.py` is COPIED into the target project by `init`, so a project
    that upgraded bmad-loop without re-initing runs a relay that has never heard of
    BMAD_LOOP_EVENTS_DIR and writes only to `<run_dir>/events`. The orchestrator
    exports the variable and waits on the out-of-tree channel regardless — so
    without the watcher's second poll, that pairing observes no Stop at all and
    EVERY session in the project stalls to `session_timeout_min`. It would not fail
    a test suite; it would hang a user's overnight run.

    Asserted on the outcome (the story reaches `done` and commits), plus the
    premise: the events really did land only in the legacy location, so the
    completion cannot have come through the primary channel.

    Ablation guard: drop `legacy_dir` from `SignalWatcher._dirs()` and this fails —
    slowly, as the session timeout, which is exactly the production symptom."""
    assert "$BMAD_LOOP_EVENTS_DIR" not in LEGACY_EVENTS_FAKE_CLI, "the twin still reads the new var"
    assert LEGACY_EVENTS_FAKE_CLI != FAKE_CLI, "the swap did not take"

    root = tmp_path / "sbx"
    story = "1-1-thing"
    _scaffold_sprint(root, story, fake_cli=LEGACY_EVENTS_FAKE_CLI)
    base = _commit_count(root)

    proc = _run(root, "run")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert _sprint_status(root, story) == "done"
    assert _commit_count(root) == base + 1

    run_id = _run_id(root)
    assert list((root / ".bmad-loop" / "runs" / run_id / "events").glob("*.json"))
    assert not list(runs.events_dir_for(root, run_id).glob("*.json"))


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
