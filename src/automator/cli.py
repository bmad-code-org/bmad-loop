"""bmad-auto command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import (
    __version__,
    bmadconfig,
    decisions,
    deferredwork,
    install,
)
from . import policy as policy_mod
from . import (
    resolve,
    runs,
    sprintstatus,
    verify,
)
from .adapters.base import CodingCLIAdapter
from .engine import Engine
from .journal import Journal, load_state, save_state
from .model import RunState
from .process_host import ProcessHostError
from .runs import RUNS_DIR
from .sweep import SweepEngine

POLICY_FILE = policy_mod.POLICY_FILE


def _project(args: argparse.Namespace) -> Path:
    return Path(args.project).resolve()


def _policy_path(project: Path) -> Path:
    return project / POLICY_FILE


def _reconcile_stale(project: Path, paths: bmadconfig.ProjectPaths, pol) -> None:
    """Tear down worktrees leaked by a prior run that stopped mid-flight, before
    starting a new run/sweep — the clean-finish GC never reached them. Gated on
    [cleanup] auto_clean_on_finish; only touches terminal (finished/stopped) dead
    runs, never anything resumable."""
    if not pol.cleanup.auto_clean_on_finish:
        return
    freed = runs.reconcile_stale_worktrees(paths.repo_root, project)
    if freed:
        print(f"reclaimed {len(freed)} stale worktree(s) from prior runs")


ROLES = ("dev", "review", "triage")


def _make_adapters(project: Path, run_dir: Path, policy) -> dict[str, CodingCLIAdapter]:
    from .adapters.generic import GenericAdapter, GenericDevAdapter
    from .adapters.multiplexer import get_multiplexer
    from .adapters.profile import ProfileError, get_profile

    # The dev skill (bmad-dev-auto) writes no result.json: its adapter
    # synthesizes the result from the spec, and so needs the project paths to
    # find that spec — rebasing onto the active worktree's implementation-
    # artifacts dir under isolation, not just the main checkout's.
    paths = bmadconfig.load_paths(project)
    # One shared terminal-multiplexer backend for every role's adapter.
    mux = get_multiplexer()

    adapters: dict[str, CodingCLIAdapter] = {}
    by_cfg: dict = {}
    for role in ROLES:
        cfg = policy.adapter.resolved(role)
        # Both the dev and review sessions are now bmad-dev-auto runs (the review
        # session re-invokes the dev skill on the done spec for a follow-up pass),
        # and the skill writes no result.json — its adapter synthesizes the result
        # from the spec it leaves on disk, so it needs the project paths to find
        # that spec and cannot be shared with the triage role even on identical config.
        synthesizes = role in ("dev", "review") and policy.dev.skill == "bmad-dev-auto"
        key = (cfg, synthesizes)
        if key not in by_cfg:
            try:
                profile = get_profile(cfg.name, project)
            except ProfileError as e:
                raise SystemExit(f"error: {e}") from e
            common = dict(
                run_dir=run_dir,
                policy=policy,
                profile=profile,
                extra_args=cfg.extra_args,
                usage_grace_s=cfg.usage_grace_s,
                stop_without_result_nudges=cfg.stop_without_result_nudges,
                mux=mux,
            )
            by_cfg[key] = (
                GenericDevAdapter(**common, paths=paths)
                if synthesizes
                else GenericAdapter(**common)
            )
        adapters[role] = by_cfg[key]
    return adapters


# ----------------------------------------------------------------- commands


def _platform_preflight() -> tuple[list[str], list[str]]:
    """Probe the platform-selected seams — the terminal multiplexer and the process
    host — for `cmd_validate`, returning ``(notes, problems)``.

    A backend reports its own readiness through ``available()`` / ``version()``, so
    a new OS or transport surfaces here by *registering* rather than by adding a
    ``sys.platform`` branch to validate. The process host is named so a
    misselection (e.g. the Windows host picked on Linux) is visible at a glance.
    """
    from .adapters.multiplexer import get_multiplexer
    from .process_host import get_process_host

    notes: list[str] = []
    problems: list[str] = []

    try:
        backend = get_multiplexer()
        label = type(backend).__name__
        if backend.available():
            version = backend.version()
            notes.append(f"multiplexer {label} available" + (f" ({version})" if version else ""))
        else:
            problems.append(
                f"multiplexer {label} unavailable — its transport binary is not on PATH; "
                f"see `bmad-auto diagnose`"
            )
    except Exception as e:  # noqa: BLE001 — selection or readiness must not abort validate
        problems.append(f"multiplexer preflight failed: {e}")

    try:
        notes.append(f"process host: {type(get_process_host()).__name__}")
    except Exception as e:  # noqa: BLE001 — a bad BMAD_AUTO_PROCESS_HOST must report, not crash
        problems.append(f"process host preflight failed: {e}")

    return notes, problems


def cmd_validate(args: argparse.Namespace) -> int:
    project = _project(args)
    problems: list[str] = []
    notes: list[str] = []

    try:
        paths = bmadconfig.load_paths(project)
        notes.append(f"BMAD config OK: artifacts at {paths.implementation_artifacts}")
    except bmadconfig.BmadConfigError as e:
        problems.append(str(e))
        paths = None

    if paths:
        try:
            ss = sprintstatus.load(paths.sprint_status)
            actionable = [s for s in ss.stories if s.status in sprintstatus.ACTIONABLE_STATUSES]
            notes.append(
                f"sprint-status OK: {len(ss.stories)} stories, {len(actionable)} actionable"
            )
            if ss.unknown_keys:
                notes.append(f"  warning: unknown keys ignored: {', '.join(ss.unknown_keys)}")
        except sprintstatus.SprintStatusError as e:
            problems.append(str(e))

    from .adapters.profile import ProfileError, get_profile

    profiles = []
    try:
        pol = policy_mod.load(_policy_path(project))
        role_names = {role: pol.adapter.resolved(role).name for role in ROLES}
        notes.append(
            f"policy OK: gates={pol.gates.mode}, "
            f"adapter dev={role_names['dev']}, review={role_names['review']}, "
            f"triage={role_names['triage']}"
        )
        for name in dict.fromkeys(role_names.values()):
            try:
                profiles.append(get_profile(name, project))
            except ProfileError as e:
                problems.append(str(e))
    except policy_mod.PolicyError as e:
        problems.append(str(e))
        pol = None

    try:
        if not verify.worktree_clean(project):
            problems.append("git worktree is not clean — commit or stash before running")
        else:
            notes.append("git worktree clean")
    except verify.GitError as e:
        problems.append(f"git check failed: {e}")

    pf_notes, pf_problems = _platform_preflight()
    notes.extend(pf_notes)
    problems.extend(pf_problems)

    for tool in dict.fromkeys(p.binary for p in profiles):
        if shutil.which(tool):
            notes.append(f"{tool} found")
        else:
            problems.append(f"{tool} not found on PATH")

    for profile in profiles:
        hook_config = project / profile.hooks.config_path
        hooks_ok = False
        if hook_config.is_file():
            try:
                hooks = json.loads(hook_config.read_text(encoding="utf-8")).get("hooks", {})
                hooks_ok = any(
                    "bmad_auto_hook" in json.dumps(hooks.get(event, []))
                    for event in profile.hooks.events
                )
            except json.JSONDecodeError:
                problems.append(f"{hook_config} is not valid JSON")
        if hooks_ok:
            notes.append(f"bmad-auto hooks registered for {profile.name}")
        else:
            problems.append(
                f"bmad-auto hooks not registered for {profile.name} — "
                f"run `bmad-auto init --cli {profile.name}`"
            )

    base_problems = install.missing_base_skills(project, [p.skill_tree for p in profiles])
    if profiles and not base_problems:
        notes.append("upstream skills present (bmad-dev-auto + review hunters)")
    problems.extend(base_problems)

    for note in notes:
        print(f"  ok: {note}")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _require_base_skills(project: Path, pol) -> bool:
    """Preflight the upstream skills the orchestrator drives (bmad-dev-auto + the
    two adversarial review hunters it invokes inline).

    Returns True when everything is in place; otherwise prints the problems and
    returns False so the caller can abort before spawning any session (a missing
    skill would otherwise stall as an `Unknown command` until the run times out).
    """
    from .adapters.profile import ProfileError, get_profile

    skill_trees = []
    for name in dict.fromkeys(pol.adapter.resolved(role).name for role in ROLES):
        try:
            skill_trees.append(get_profile(name, project).skill_tree)
        except ProfileError:
            continue
    problems = install.missing_base_skills(project, skill_trees)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        print("run `bmad-auto validate` for details", file=sys.stderr)
        return False
    return True


def cmd_run(args: argparse.Namespace) -> int:
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = policy_mod.load(_policy_path(project))

    if args.dry_run:
        return _dry_run(paths, pol, args)

    try:
        sprintstatus.select_actionable(
            sprintstatus.load(paths.sprint_status), args.epic, args.story
        )
    except sprintstatus.SprintStatusError as e:
        print(e, file=sys.stderr)
        return 1

    if not verify.worktree_clean(paths.repo_root):
        print("git worktree is not clean — commit or stash first", file=sys.stderr)
        return 1

    if not _require_base_skills(project, pol):
        return 1

    _reconcile_stale(project, paths, pol)

    run_id = args.run_id or runs.new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=pol.to_dict(),
        epic_filter=args.epic,
        story_filter=args.story,
        max_stories=args.max_stories,
    )
    save_state(run_dir, state)
    runs.write_pid(run_dir)
    adapters = _make_adapters(project, run_dir, pol)
    journal.append(
        "run-start",
        run_id=run_id,
        adapter_dev=pol.adapter.resolved("dev").name,
        adapter_review=pol.adapter.resolved("review").name,
    )
    print(f"run {run_id} starting (attach: bmad-auto attach)")

    engine = Engine(
        paths=paths,
        policy=pol,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        run_dir=run_dir,
        journal=journal,
        state=state,
        max_stories=args.max_stories,
        epic_filter=args.epic,
        story_filter=args.story,
        sweep_factory=_sweep_factory(project, paths),
    )
    summary = engine.run()
    print(summary.render())
    return 0


def _render_invocation(pol, project: Path, role: str, prompt: str) -> str:
    from .adapters.profile import get_profile

    cfg = pol.adapter.resolved(role)
    profile = get_profile(cfg.name, project)
    extra = cfg.extra_args if cfg.extra_args is not None else profile.bypass_args
    argv = [
        profile.binary,
        *profile.launch_args,
        f'"{profile.render_prompt(prompt)}"',
        *extra,
    ]
    if cfg.model:
        argv += [profile.model_flag, cfg.model]
    return " ".join(argv)


def _dry_run(paths: bmadconfig.ProjectPaths, pol, args: argparse.Namespace) -> int:
    def render(role: str, prompt: str) -> str:
        return _render_invocation(pol, paths.project, role, prompt)

    ss = sprintstatus.load(paths.sprint_status)
    try:
        queue = sprintstatus.select_actionable(ss, args.epic, args.story)
    except sprintstatus.SprintStatusError as e:
        print(e, file=sys.stderr)
        return 1
    if args.max_stories is not None:
        queue = queue[: args.max_stories]
    if not queue:
        print("no actionable stories")
        return 0
    print(f"would process {len(queue)} stories (gates={pol.gates.mode}):")
    for story in queue:
        print(f"\n  {story.key} (epic {story.epic}, status {story.status})")
        print(f"    dev:    {render('dev', f'/bmad-dev-auto {story.key}')}")
        print(f"    review: {render('review', '/bmad-dev-auto <done spec from dev>')}")
        print(f"    env:    BMAD_AUTO_MODE=1 BMAD_AUTO_STORY_KEY={story.key}")
    return 0


def _start_sweep(
    project: Path,
    paths: bmadconfig.ProjectPaths,
    pol,
    *,
    prompting: bool,
    decisions_only: bool,
    max_bundles: int | None,
    repeat: bool | None = None,
    max_cycles: int | None = None,
    trigger: str,
    run_id: str | None = None,
) -> int:
    run_id = run_id or runs.new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=pol.to_dict(),
        run_type="sweep",
    )
    save_state(run_dir, state)
    runs.write_pid(run_dir)
    options = {
        "prompting": prompting,
        "decisions_only": decisions_only,
        "max_bundles": max_bundles,
        "repeat": repeat,
        "max_cycles": max_cycles,
        "trigger": trigger,
    }
    (run_dir / "sweep.json").write_text(json.dumps(options, indent=2), encoding="utf-8")
    adapters = _make_adapters(project, run_dir, pol)
    journal.append("run-start", run_id=run_id, run_type="sweep", trigger=trigger)
    print(f"sweep {run_id} starting (attach: bmad-auto attach)")
    engine = SweepEngine(
        paths=paths,
        policy=pol,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        triage_adapter=adapters["triage"],
        run_dir=run_dir,
        journal=journal,
        state=state,
        prompting=prompting,
        decisions_only=decisions_only,
        max_bundles=max_bundles,
        repeat=repeat,
        max_cycles=max_cycles,
    )
    summary = engine.run()
    print(summary.render())
    return 0


def _sweep_factory(project: Path, paths: bmadconfig.ProjectPaths):
    """Child-sweep launcher injected into story-run engines. Auto-triggered
    sweeps are unattended: never prompt, never run decision bundles."""

    def factory(trigger: str) -> None:
        pol = policy_mod.load(_policy_path(project))
        _start_sweep(
            project,
            paths,
            pol,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            trigger=trigger,
        )

    return factory


def cmd_sweep(args: argparse.Namespace) -> int:
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = policy_mod.load(_policy_path(project))

    if args.dry_run:
        return _sweep_dry_run(paths, pol)

    if not verify.worktree_clean(paths.repo_root):
        print("git worktree is not clean — commit or stash first", file=sys.stderr)
        return 1

    if not _require_base_skills(project, pol):
        return 1

    _reconcile_stale(project, paths, pol)

    return _start_sweep(
        project,
        paths,
        pol,
        prompting=not args.no_prompt,
        decisions_only=args.decisions_only,
        max_bundles=args.max_bundles,
        repeat=args.repeat,
        max_cycles=args.max_cycles,
        trigger="cli",
        run_id=args.run_id,
    )


def _sweep_dry_run(paths: bmadconfig.ProjectPaths, pol) -> int:
    ledger = paths.deferred_work
    if not ledger.is_file():
        print(f"no deferred-work ledger at {ledger}")
        return 0
    text = ledger.read_text(encoding="utf-8")
    entries = deferredwork.parse_ledger(text)
    open_entries = [e for e in entries if e.open]
    closed = len(entries) - len(open_entries)
    print(f"{ledger}: {len(open_entries)} open, {closed} closed/non-open")
    for entry in open_entries:
        print(f"  {entry.id:8s} {entry.title}")
    legacy = deferredwork.parse_legacy(text)
    legacy_open = [e for e in legacy if not e.done]
    if legacy:
        print(
            f"plus {len(legacy)} legacy (pre-DW-format) entries, {len(legacy_open)} open"
            " — a sweep would first migrate them to DW format"
        )
        for entry in legacy_open:
            print(f"  {entry.id or '-':8s} {entry.title}")
    if open_entries or legacy_open:
        print("a sweep would triage the open entries in one LLM session, then run bundles")
        print(f"  triage: {_render_invocation(pol, paths.project, 'triage', '/bmad-auto-sweep')}")
    return 0


def _resume_paused_run(project: Path, run_dir: Path) -> int:
    """Resume the engine for a paused/interrupted run. Shared by `resume` and
    the re-arm step of `resolve`."""
    paths = bmadconfig.load_paths(project)
    state = load_state(run_dir)
    if state.finished:
        print(f"run {run_dir.name} already finished", file=sys.stderr)
        return 1
    pol = policy_mod.load(_policy_path(project))
    if not _require_base_skills(project, pol):
        return 1
    journal = Journal(run_dir)
    journal.append("run-resume", was_paused=state.paused_reason)
    state.clear_pause()
    runs.write_pid(run_dir)
    # drop any stale agent session so the run spins up a fresh one (a stopped or
    # interrupted run can leave a lingering bmad-auto-<id> session behind).
    runs.kill_session(run_dir.name)
    adapters = _make_adapters(project, run_dir, pol)
    if state.run_type == "sweep":
        opts_path = run_dir / "sweep.json"
        opts = json.loads(opts_path.read_text(encoding="utf-8")) if opts_path.is_file() else {}
        engine: Engine = SweepEngine(
            paths=paths,
            policy=pol,
            adapter=adapters["dev"],
            review_adapter=adapters["review"],
            triage_adapter=adapters["triage"],
            run_dir=run_dir,
            journal=journal,
            state=state,
            prompting=bool(opts.get("prompting", False)),
            decisions_only=bool(opts.get("decisions_only", False)),
            max_bundles=opts.get("max_bundles"),
            repeat=opts.get("repeat"),
            max_cycles=opts.get("max_cycles"),
        )
    else:
        engine = Engine(
            paths=paths,
            policy=pol,
            adapter=adapters["dev"],
            review_adapter=adapters["review"],
            run_dir=run_dir,
            journal=journal,
            state=state,
            # restore the launching scope + cap so a resumed `--epic N` run keeps
            # picking within N instead of silently widening to every epic.
            epic_filter=state.epic_filter,
            story_filter=state.story_filter,
            max_stories=state.max_stories,
            sweep_factory=_sweep_factory(project, paths),
        )
    summary = engine.run()
    print(summary.render())
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    project = _project(args)
    try:
        run_dir = runs.resolve_run_dir(project, args.run_id)
    except runs.RunRefError as e:
        print(str(e), file=sys.stderr)
        return 1
    args.run_id = run_dir.name  # normalize so messages show the full id
    # Gate here, NOT in _resume_paused_run: that helper is also resolve's re-arm
    # path, which is already gated at resolve entry. A provably-live engine blocks
    # outright (the TUI warns in its confirm modal instead; the CLI has no confirm
    # step). 'unknown' warns but proceeds: resume is the recovery path that
    # rewrites engine.pid, so it must stay usable when liveness is unverifiable.
    live = runs.engine_liveness(run_dir)
    if live == "alive":
        print(
            f"run {args.run_id} is still live — resuming would double-drive it; stop it first",
            file=sys.stderr,
        )
        return 1
    if live == "unknown":
        print(
            f"run {args.run_id}: engine may still be live (unverifiable pid) — "
            "resuming could double-drive this run",
            file=sys.stderr,
        )
    return _resume_paused_run(project, run_dir)


def _confirm(question: str) -> bool:
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def cmd_resolve(args: argparse.Namespace) -> int:
    from .model import PAUSE_ESCALATION, Phase

    project = _project(args)
    try:
        run_dir = runs.resolve_run_dir(project, args.run_id)
    except runs.RunRefError as e:
        print(str(e), file=sys.stderr)
        return 1
    args.run_id = run_dir.name  # normalize so echoed hints show the full id
    state = load_state(run_dir)
    if state.paused_stage != PAUSE_ESCALATION:
        print(
            f"run {args.run_id} is not paused at an escalation "
            f"(stage: {state.paused_stage or 'none'})",
            file=sys.stderr,
        )
        return 1
    # Not a cleanup path, so the "unknown must not block" invariant does not apply:
    # an unverifiable-but-live pid must not be re-driven. A provably-live engine
    # always blocks (--force never bypasses it); unknown blocks unless the operator
    # vouches with --force — `stop` cannot verify or clear an unverifiable pid, so
    # without an escape hatch a squatted pid would lock resolve out forever.
    live = runs.engine_liveness(run_dir)
    if live == "alive":
        print(f"run {args.run_id} is still live — stop it first", file=sys.stderr)
        return 1
    if live == "unknown":
        if not args.force:
            print(
                f"run {args.run_id}: engine may still be live (unverifiable pid) — "
                "refusing to re-arm. Confirm the engine process is gone, then re-run "
                "with --force (`stop` cannot verify or clear an unverifiable pid).",
                file=sys.stderr,
            )
            return 1
        print(
            f"run {args.run_id}: engine may still be live (unverifiable pid) — "
            "proceeding anyway (--force)",
            file=sys.stderr,
        )
    story_key = args.story or state.paused_story_key
    task = state.tasks.get(story_key) if story_key else None
    if story_key is None or task is None or task.phase != Phase.ESCALATED:
        print(f"no escalated story to resolve in run {args.run_id}", file=sys.stderr)
        return 1

    if args.interactive:
        pol = policy_mod.load(_policy_path(project))
        adapters = _make_adapters(project, run_dir, pol)
        model = pol.adapter.resolved("dev").model
        resolve.build_context(state, run_dir, story_key)
        print(f"launching resolve agent for {story_key} — converse, fix the spec, then exit…")
        try:
            produced = resolve.run_session(
                adapters["dev"], project, run_dir, story_key, model=model
            )
        except NotImplementedError:
            print(
                "the dev adapter has no interactive session mode — fix the spec by hand, "
                f"then: bmad-auto resolve {args.run_id} --no-interactive",
                file=sys.stderr,
            )
            return 1
        if not produced:
            print(
                f"no resolution recorded for {story_key} (agent did not write resolution.json)",
                file=sys.stderr,
            )

    # confirm-then-resume (args.resume: None = ask, True = auto, False = re-arm only)
    if args.resume is None and not _confirm(f"re-arm {story_key} and resume run {args.run_id}?"):
        print("cancelled — run is still paused at the escalation")
        return 0
    try:
        runs.rearm_escalation(run_dir, story_key)
    except runs.RearmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"re-armed {story_key}")
    if args.resume is False:
        print(f"resume when ready: bmad-auto resume {args.run_id}")
        return 0
    from .tui import launch  # import-safe: launch.py has no textual imports

    if launch.in_ctl_session():
        # We are inside the TUI's bmad-auto-ctl window the user is attached to.
        # Tell them, hand the terminal back, and let the engine run on here — a
        # tmux pane keeps running after its client detaches.
        print(
            f"✓ resuming run {args.run_id} in the background — "
            f"watch it in the TUI, or: bmad-auto attach {args.run_id}"
        )
        launch.detach_client()
    return _resume_paused_run(project, run_dir)


def cmd_decisions(args: argparse.Namespace) -> int:
    """Answer deferred-work decisions earlier sweeps left unanswered (skipped by
    an unattended sweep, or an interactive one that was abandoned). Answers are
    recorded so the next sweep acts on them without re-asking."""
    from .sweep import DecisionPrompter

    project = _project(args)
    try:
        pending = decisions.pending_missed_decisions(project)
    except bmadconfig.BmadConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not pending:
        print("no unanswered decisions from past sweeps")
        return 0
    if args.list:
        print(f"{len(pending)} unanswered decision(s) from past sweeps:\n")
        for d in pending:
            print(f"  {d.id}: {d.question}")
            for opt in d.options:
                rec = "  (recommended)" if opt.key == d.recommendation else ""
                print(f"      [{opt.key}] {opt.label} — {opt.effect}{rec}")
            print("")
        print("answer them interactively: bmad-auto decisions")
        return 0
    prompter = DecisionPrompter()
    print(f"{len(pending)} unanswered decision(s) — your answers carry into the next sweep:")
    today = time.strftime("%Y-%m-%d")
    for decision in pending:
        option = prompter.ask(decision)
        decisions.apply_pre_answer(project, decision, option, date=today)
        if option.effect == "close":
            outcome = "closed now"
        elif option.effect == "build":
            outcome = "queued — the next sweep will build it"
        else:
            outcome = "kept open (recorded)"
        print(f"  {decision.id}: {outcome}")
    print("\nrun `bmad-auto sweep` to act on any builds.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project = _project(args)
    if args.run_id:
        try:
            run_dir = runs.resolve_run_dir(project, args.run_id)
        except runs.RunRefError as e:
            print(str(e), file=sys.stderr)
            return 1
    else:
        run_dir = runs.latest_run_dir(project)
    if run_dir is None or not (run_dir / "state.json").is_file():
        print("no runs found", file=sys.stderr)
        return 1
    state = load_state(run_dir)
    kind = f" [{state.run_type}]" if state.run_type != "story" else ""
    print(f"run {state.run_id}{kind}  started {state.started_at}")
    if state.finished:
        print("status: finished")
    elif state.paused:
        print(f"status: PAUSED ({state.paused_stage}) — {state.paused_reason}")
    else:
        print("status: in progress (or interrupted)")
    for key, task in state.tasks.items():
        tokens = f"{task.tokens.total:,}t" if task.tokens.total else "-"
        extra = task.defer_reason or task.commit_sha or ""
        print(
            f"  {key:40s} {task.phase:16s} dev×{task.attempt} review×{task.review_cycle} "
            f"{tokens} {extra}"
        )
    try:
        paths = bmadconfig.load_paths(project)
        ss = sprintstatus.load(paths.sprint_status)
        remaining = [s.key for s in ss.stories if s.status in sprintstatus.ACTIONABLE_STATUSES]
        print(f"sprint backlog remaining: {len(remaining)}")
    except (bmadconfig.BmadConfigError, sprintstatus.SprintStatusError):
        pass
    try:
        missed = decisions.pending_missed_decisions(project)
        if missed:
            print(
                f"deferred-work decisions awaiting an answer: {len(missed)} (bmad-auto decisions)"
            )
    except bmadconfig.BmadConfigError:
        pass
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from .tui.data import discover_runs  # import-safe: data.py has no textual imports

    project = _project(args)
    infos = discover_runs(project)  # oldest first
    if not infos:
        print("no runs found")
        return 0
    print(f"{'REF':6} {'TYPE':6} {'STATUS':10} RUN ID")
    for ri in infos:
        print(f"{runs.short_ref(ri.run_id):6} {ri.run_type:6} {ri.status:10} {ri.run_id}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    from .tui import launch  # import-safe: launch.py has no textual imports

    project = _project(args)
    if args.run_id:
        try:
            run_dir = runs.resolve_run_dir(project, args.run_id)
        except runs.RunRefError as e:
            print(str(e), file=sys.stderr)
            return 1
    else:
        run_dir = runs.latest_run_dir(project)
    if run_dir is None:
        print("no runs found", file=sys.stderr)
        return 1
    plan = launch.attach_plan(project, run_dir.name)
    if plan is None:
        print(f"nothing to attach for run {run_dir.name}", file=sys.stderr)
        return 1
    argv, return_window = plan
    # Record where to send the client once the sweep finishes this cycle's
    # decisions (see launch.return_attached_client), so answering them hands the
    # terminal back instead of stranding the user in the orchestrator window.
    if return_window is not None:
        if os.environ.get("TMUX"):
            pane = launch.current_pane_id()
            if pane is not None:
                launch.set_return_pane(return_window, pane)
        else:
            launch.set_return_pane(return_window, launch.RETURN_DETACH)
    return subprocess.call(argv)


def cmd_stop(args: argparse.Namespace) -> int:
    project = _project(args)
    try:
        run_dir = runs.resolve_run_dir(project, args.run_id)
    except runs.RunRefError as e:
        print(str(e), file=sys.stderr)
        return 1
    args.run_id = run_dir.name
    try:
        stopped = runs.stop_run(run_dir)
    except (runs.StopRunError, ProcessHostError) as e:
        print(str(e), file=sys.stderr)
        return 1
    if not stopped:
        print(f"run {args.run_id} already finished", file=sys.stderr)
        return 1
    print(f"run {args.run_id} stopped")
    return 0


def _stop_or_block_live_engine(run_dir: Path, run_id: str, force: bool) -> int | None:
    """Shared delete/archive guard. One liveness sample drives both the warning and the
    block, so a mid-check identity flip can't fire one without the other. An unverifiable
    pid (``unknown``) warns but never blocks cleanup; only a provably-live engine blocks,
    or is stopped first under ``force``. Returns an exit code to propagate, or None to
    proceed with cleanup."""
    live = runs.engine_liveness(run_dir)
    if live == "unknown":
        print(f"run {run_id}: engine may still be live (unverifiable pid)", file=sys.stderr)
    if live == "alive":
        if not force:
            print(f"run {run_id} is still live — stop it first (or pass --force)", file=sys.stderr)
            return 1
        try:
            runs.stop_run(run_dir)
        except (runs.StopRunError, ProcessHostError) as e:
            print(str(e), file=sys.stderr)
            return 1
    return None


def cmd_delete(args: argparse.Namespace) -> int:
    project = _project(args)
    try:
        run_dir = runs.resolve_run_dir(project, args.run_id)
    except runs.RunRefError as e:
        print(str(e), file=sys.stderr)
        return 1
    args.run_id = run_dir.name
    rc = _stop_or_block_live_engine(run_dir, args.run_id, args.force)
    if rc is not None:
        return rc
    runs.delete_run(run_dir)
    print(f"run {args.run_id} deleted")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    project = _project(args)
    try:
        run_dir = runs.resolve_run_dir(project, args.run_id)
    except runs.RunRefError as e:
        print(str(e), file=sys.stderr)
        return 1
    args.run_id = run_dir.name
    rc = _stop_or_block_live_engine(run_dir, args.run_id, args.force)
    if rc is not None:
        return rc
    dest = runs.archive_run(project, run_dir)
    print(f"run {args.run_id} archived to {dest}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from .tui import launch  # pure stdlib; no textual import

    project = _project(args)
    # one partition sample drives the prune and every message below, so the
    # warnings and live count always match what was actually killed/skipped
    killed, live, unknown = runs.prune_sessions(project, dry_run=args.dry_run)
    for run_id in sorted(unknown):
        # warn-only: unknown never blocks cleanup (same wording as delete/archive).
        # Pruning kills the tmux session, never the engine pid, so the warning
        # holds after the fact too.
        print(f"run {run_id}: engine may still be live (unverifiable pid)", file=sys.stderr)
    if args.dry_run:
        windows = launch.prunable_ctl_windows(project)
        if not killed and not windows:
            print("nothing to clean up")
        else:
            for run_id in killed:
                print(f"would kill session bmad-auto-{run_id}")
            for name in windows:
                print(f"would close ctl window {name}")
        if live:
            print(f"leaving {len(live)} live session(s) untouched")
        return 0
    windows = launch.prune_ctl_windows(project)
    print(f"removed {len(killed)} session(s), {len(windows)} ctl window(s)")
    if live:
        print(f"left {len(live)} live session(s) untouched")
    return 0


def _dir_size(path: Path) -> int:
    """Best-effort total bytes under ``path`` (symlinks not followed)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def cmd_clean(args: argparse.Namespace) -> int:
    """Reclaim disk from concluded runs: tear down worktrees leaked by a
    mid-flight stop, trim heavy scaffolding from runs kept for history, and
    archive/delete runs past the retention window. Only terminal (finished or
    stopped) runs are touched; running, unknown-host, paused and interrupted
    runs are always left intact."""
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    repo = paths.repo_root
    pol = policy_mod.load(_policy_path(project))
    keep = set(args.keep or ())
    retain = args.retain if args.retain is not None else pol.cleanup.run_retention
    dry = args.dry_run

    reclaimable: list[Path] = []
    protected = 0
    for run_dir in runs.list_run_dirs(project):
        if run_dir.name in keep:
            protected += 1
        elif runs.reclaimable(run_dir):
            reclaimable.append(run_dir)
        else:
            protected += 1

    past = {
        p.name
        for p in runs.runs_past_retention(
            reclaimable, keep_n=retain, keep_days=pol.cleanup.retention_days
        )
    }

    freed = 0
    worktrees = 0
    trimmed: list[str] = []
    archived: list[str] = []
    deleted: list[str] = []
    for run_dir in reclaimable:
        if runs.engine_liveness(run_dir) == "unknown":
            # warn-only: unknown never blocks cleanup, but say so before removal
            print(
                f"run {run_dir.name}: engine may still be live (unverifiable pid)",
                file=sys.stderr,
            )
        # measure before mutating so the reclaim estimate holds for --dry-run too
        wt_dir = run_dir / "worktrees"
        wt_bytes = _dir_size(wt_dir) if wt_dir.is_dir() else 0
        run_bytes = _dir_size(run_dir)
        for wt in runs.reconcile_orphan_worktrees(repo, run_dir, dry_run=dry):
            worktrees += 1
            print(f"{'would remove' if dry else 'removed'} worktree {wt}")
        if run_dir.name in past:
            freed += run_bytes
            runs.trim_run_dir(run_dir, dry_run=dry)  # shrink before archiving
            if args.hard or not pol.cleanup.archive_old:
                if not dry:
                    runs.delete_run(run_dir)
                deleted.append(run_dir.name)
            else:
                if not dry:
                    runs.archive_run(project, run_dir)
                archived.append(run_dir.name)
        elif pol.cleanup.trim_artifacts:
            if runs.trim_run_dir(run_dir, dry_run=dry):
                freed += wt_bytes
                trimmed.append(run_dir.name)

    if not worktrees and not trimmed and not archived and not deleted:
        print("nothing to reclaim")
    else:
        head = "would reclaim" if dry else "reclaimed"
        print(
            f"{head} ~{_human_bytes(freed)}: {worktrees} worktree(s), "
            f"{len(trimmed)} run(s) trimmed, {len(archived)} archived, {len(deleted)} deleted"
        )
        for name in archived:
            print(f"  archived {name} -> .automator/archive/{name}.tar.gz")
        for name in deleted:
            print(f"  deleted {name}")
    if protected:
        print(f"left {protected} live/resumable run(s) untouched")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    project = _project(args)
    # Apply low-frame-rate mode *before* importing textual: it reads TEXTUAL_FPS
    # / TEXTUAL_ANIMATIONS once at import time. setdefault so an explicit value
    # in the user's environment still wins. policy.load is textual-free.
    if args.low_frame_rate or policy_mod.load(_policy_path(project)).tui.low_frame_rate:
        os.environ.setdefault("TEXTUAL_FPS", "15")
        os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")
    try:
        from .tui.app import run_tui
    except ModuleNotFoundError as e:
        if (e.name or "").partition(".")[0] in ("textual", "tomlkit"):
            print(
                "error: the TUI requires optional dependencies — uv tool install 'bmad-auto[tui]'",
                file=sys.stderr,
            )
            return 1
        raise
    return run_tui(project)


def cmd_probe(args: argparse.Namespace) -> int:
    from . import probe as probe_mod
    from .adapters.profile import ProfileError, get_profile

    project = _project(args)
    hints = probe_mod.Hints(
        binary=args.binary,
        transcript=args.transcript,
        session_dir=args.session_dir,
        model=args.model,
    )

    profile = None
    try:
        profile = get_profile(args.cli, project)
    except ProfileError as e:
        if not args.binary:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
        print(f"  ok: unknown profile {args.cli!r}; reduced report from --binary {args.binary}")

    if args.probe:
        if profile is None:
            print("FAIL: --probe needs a known profile (its hook dialect/events)", file=sys.stderr)
            return 1
        finding = probe_mod.probe(
            cli=args.cli,
            profile=profile,
            project=project,
            hints=hints,
            timeout_s=args.timeout,
            keep_temp=args.keep_temp,
        )
    else:
        finding = probe_mod.scan(cli=args.cli, profile=profile, project=project, hints=hints)

    report = probe_mod.render_markdown(finding)
    if args.json:
        report = report + "\n\n## JSON\n\n```json\n" + probe_mod.render_json(finding) + "\n```\n"

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(report, encoding="utf-8")
        print(f"  ok: report written to {out_path} ({len(finding.warnings)} warning(s))")
    else:
        print(report)
        print(f"  ok: {finding.mode} report for {args.cli} ({len(finding.warnings)} warning(s))")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    from . import diagnostics, sanitize

    project = _project(args)
    if args.all:
        run_dirs = runs.list_run_dirs(project)
    elif args.run_id:
        try:
            run_dirs = [runs.resolve_run_dir(project, args.run_id)]
        except runs.RunRefError as e:
            print(str(e), file=sys.stderr)
            return 1
    else:
        latest = runs.latest_run_dir(project)
        run_dirs = [latest] if latest is not None else []
    if not run_dirs:
        print("no runs found", file=sys.stderr)
        return 1

    pseudo = sanitize.Pseudonymizer()
    diag = diagnostics.collect(run_dirs, pseudo=pseudo, cap=args.max_journal_entries)
    try:
        report = diagnostics.render_markdown(diag, pseudo=pseudo)
        if args.json:
            report += (
                "\n\n## JSON\n\n```json\n"
                + diagnostics.render_json(diag, pseudo=pseudo)
                + "\n```\n"
            )
    except diagnostics.LeakDetected as e:
        # The output tripped the final self-check — fail closed, write nothing.
        print(
            f"FAIL: refusing to emit — leak self-check fired: {', '.join(e.rules)}", file=sys.stderr
        )
        return 1

    if args.legend:
        legend_path = Path(args.legend)
        # The legend reverses the pseudonyms, so it must never land world-readable
        # via the inherited umask — create it owner-only (0600).
        fd = os.open(legend_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pseudo.legend(), f, indent=2)
            f.write("\n")
        print(
            f"  ok: alias legend written to {legend_path} — LOCAL ONLY, do NOT share "
            "(it reverses the pseudonyms); delete after use",
            file=sys.stderr,
        )

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(report, encoding="utf-8")
        print(f"  ok: sanitized diagnostics for {len(diag.runs)} run(s) written to {out_path}")
    else:
        print(report)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from .install import install_into

    project = _project(args)
    if args.cli:
        clis = tuple(args.cli)
    else:
        # missing policy file yields defaults -> ("claude",)
        pol = policy_mod.load(_policy_path(project))
        clis = tuple(dict.fromkeys(pol.adapter.resolved(role).name for role in ROLES))
    return install_into(project, clis=clis, skills=args.skills, force_skills=args.force_skills)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bmad-auto",
        description="Deterministic orchestrator for the BMAD implementation phase",
    )
    parser.add_argument("--version", action="version", version=f"bmad-auto {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help: str, *, aliases=()) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, aliases=aliases)
        p.add_argument("--project", default=".", help="target project root (default: cwd)")
        p.set_defaults(func=func)
        return p

    init_p = add(
        "init", cmd_init, "install hooks + skills + policy template into the target project"
    )
    init_p.add_argument(
        "--cli",
        action="append",
        metavar="PROFILE",
        help="CLI profile(s) to register hooks for (claude | codex | gemini | custom; "
        "repeatable; default: profiles referenced by .automator/policy.toml, or claude)",
    )
    init_p.add_argument(
        "--no-skills",
        dest="skills",
        action="store_false",
        help="skip installing the bundled bmad-auto-* skills (hooks/policy only)",
    )
    init_p.add_argument(
        "--force-skills",
        action="store_true",
        help="overwrite bmad-auto-* skill dirs that already exist (default: skip them)",
    )
    add("validate", cmd_validate, "preflight checks; exit non-zero on failure")

    probe_p = add(
        "probe-adapter",
        cmd_probe,
        "collect + sanitize adapter-finalization data for a coding CLI",
        aliases=["collect-adapter-data"],
    )
    probe_p.add_argument(
        "cli", help="CLI profile name (claude | codex | gemini | copilot | custom)"
    )
    probe_p.add_argument(
        "--probe",
        action="store_true",
        help="opt-in LIVE capture: launch one trivial content-free turn in a temp "
        "workspace and capture real hook payloads (default: zero-launch scan)",
    )
    probe_p.add_argument(
        "--transcript", help="exact transcript file to inspect (overrides discovery)"
    )
    probe_p.add_argument(
        "--session-dir", help="dir to glob for the newest transcript (custom CLIs)"
    )
    probe_p.add_argument("--binary", help="binary name for a CLI with no profile yet")
    probe_p.add_argument("--model", help="model passed to the probe turn (probe mode)")
    probe_p.add_argument(
        "--timeout", type=float, default=90, help="probe turn timeout (default: 90s)"
    )
    probe_p.add_argument("--out", help="write the report to this file instead of stdout")
    probe_p.add_argument("--json", action="store_true", help="append a machine-readable JSON block")
    probe_p.add_argument("--keep-temp", action="store_true", help=argparse.SUPPRESS)

    run_p = add("run", cmd_run, "run the orchestration loop")
    run_p.add_argument("--epic", type=int, help="only stories from this epic")
    run_p.add_argument("--story", help="story: E-S / E.S, a slug fragment, or full key")
    run_p.add_argument("--max-stories", type=int, help="stop after N stories")
    run_p.add_argument("--dry-run", action="store_true", help="print the plan, spawn nothing")
    run_p.add_argument("--run-id", help=argparse.SUPPRESS)  # pre-assigned id (used by the TUI)

    sweep_p = add("sweep", cmd_sweep, "triage + execute open deferred-work.md entries")
    sweep_p.add_argument(
        "--no-prompt",
        action="store_true",
        help="unattended: skip decision prompts, run only decision-free bundles",
    )
    sweep_p.add_argument(
        "--decisions-only",
        action="store_true",
        help="triage + answer decisions + record them; run no bundles",
    )
    sweep_p.add_argument("--max-bundles", type=int, help="override [sweep] max_bundles")
    sweep_p.add_argument(
        "--repeat",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override [sweep] repeat: after a cycle completes, re-triage and continue "
        "on newly deferred work until nothing addressable completes",
    )
    sweep_p.add_argument("--max-cycles", type=int, help="override [sweep] max_cycles")
    sweep_p.add_argument(
        "--dry-run", action="store_true", help="list open ledger entries, spawn nothing"
    )
    sweep_p.add_argument("--run-id", help=argparse.SUPPRESS)  # pre-assigned id (used by the TUI)

    resume_p = add("resume", cmd_resume, "resume a paused run")
    resume_p.add_argument("run_id")

    resolve_p = add(
        "resolve", cmd_resolve, "resolve a CRITICAL escalation interactively, then re-arm + resume"
    )
    resolve_p.add_argument("run_id")
    resolve_p.add_argument("--story", help="story key to resolve (default: the paused one)")
    resolve_p.add_argument(
        "--no-interactive",
        dest="interactive",
        action="store_false",
        help="skip the resolve agent (spec already fixed by hand); just re-arm + resume",
    )
    resolve_p.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--resume: re-arm + resume without prompting; --no-resume: re-arm only "
        "(default: prompt to confirm, then resume)",
    )
    resolve_p.add_argument(
        "--force",
        action="store_true",
        help="proceed when engine liveness is unverifiable (unknown); "
        "a provably-live engine still blocks",
    )

    decisions_p = add(
        "decisions",
        cmd_decisions,
        "answer deferred-work decisions earlier sweeps left unanswered",
    )
    decisions_p.add_argument(
        "--list",
        action="store_true",
        help="list the pending decisions without answering them",
    )

    add("list", cmd_list, "list runs/sweeps with their short ref", aliases=["ls"])

    status_p = add("status", cmd_status, "show run + sprint state")
    status_p.add_argument("run_id", nargs="?")

    diag_p = add(
        "diagnose",
        cmd_diagnose,
        "emit a sanitized diagnostic dump of a run/sweep to hand to maintainers",
        aliases=["diag"],
    )
    diag_p.add_argument("run_id", nargs="?", help="run ref (default: latest)")
    diag_p.add_argument("--all", action="store_true", help="dump every run in the project")
    diag_p.add_argument("--out", help="write the report to this file instead of stdout")
    diag_p.add_argument("--json", action="store_true", help="append a machine-readable JSON block")
    diag_p.add_argument(
        "--max-journal-entries",
        type=int,
        default=200,
        metavar="N",
        help="cap of fully-scrubbed journal entries per run (0 = histogram only; default 200)",
    )
    # Hidden: writes the alias->original map locally for the dump's author. Never
    # shareable — it reverses the pseudonyms.
    diag_p.add_argument("--legend", help=argparse.SUPPRESS)

    attach_p = add("attach", cmd_attach, "tmux attach to a run's session")
    attach_p.add_argument("run_id", nargs="?")

    stop_p = add("stop", cmd_stop, "stop a live run (engine + agent session)")
    stop_p.add_argument("run_id")

    delete_p = add("delete", cmd_delete, "delete a run directory")
    delete_p.add_argument("run_id")
    delete_p.add_argument(
        "--force", action="store_true", help="stop the run first if it is still live"
    )

    archive_p = add("archive", cmd_archive, "compress a run into .automator/archive and remove it")
    archive_p.add_argument("run_id")
    archive_p.add_argument(
        "--force", action="store_true", help="stop the run first if it is still live"
    )

    cleanup_p = add(
        "cleanup", cmd_cleanup, "remove tmux sessions/windows for finished or stopped runs"
    )
    cleanup_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be removed without killing anything",
    )

    clean_p = add(
        "clean",
        cmd_clean,
        "reclaim disk from concluded runs (tear down leaked worktrees, trim/archive per [cleanup])",
    )
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be reclaimed without removing anything",
    )
    clean_p.add_argument(
        "--keep",
        action="append",
        metavar="RUN_ID",
        help="run id to never touch (repeatable; e.g. a finished run whose Editor is still live)",
    )
    clean_p.add_argument(
        "--retain",
        type=int,
        metavar="N",
        help="keep the newest N concluded runs whole (overrides [cleanup] run_retention)",
    )
    clean_p.add_argument(
        "--hard",
        action="store_true",
        help="permanently delete runs past the retention window instead of archiving them",
    )

    tui_p = add(
        "tui",
        cmd_tui,
        "interactive dashboard (needs `uv tool install 'bmad-auto[tui]'`)",
    )
    tui_p.add_argument(
        "--low-frame-rate",
        action="store_true",
        help="cap to 15fps + disable animations (TEXTUAL_FPS / TEXTUAL_ANIMATIONS) — "
        "fixes repaint tearing over slow/SSH links; also settable via [tui] low_frame_rate",
    )

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        bmadconfig.BmadConfigError,
        sprintstatus.SprintStatusError,
        policy_mod.PolicyError,
        verify.GitError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # backstop for the residual surface outside engine.run() (config load,
        # engine construction, render/notify): never let an unexpected exception
        # die to the parked control pane with a bare traceback.
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
