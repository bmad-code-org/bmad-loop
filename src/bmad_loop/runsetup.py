"""Run-composition layer for the CLI's ``run`` callback.

``cli.cmd_run`` used to build the :class:`~bmad_loop.model.RunState`, wire the
:class:`~bmad_loop.engine.Engine`, and stand up the coding-CLI adapters inline in
an argparse callback — logic that could only be exercised by round-tripping
through argv. This module lifts those pieces out as typed functions so a non-CLI
frontend (or a test) can compose a run directly:

* :func:`make_adapters` — the per-role adapter factory.
* :func:`platform_preflight` — the multiplexer/process-host readiness probe
  ``cmd_validate`` reports.
* :func:`build_run_state` / :func:`compose_run` — the RunState + Engine wiring
  for ``cmd_run``.
* :func:`compose_sweep` — the same wiring for a ``sweep`` run (``cmd_sweep`` and
  the auto-triggered child-sweep factory).
* :func:`compose_resume` — rebuilds the engine for a paused/interrupted run
  (``cmd_resume`` and ``resolve``'s re-arm), selecting the sweep/stories/plain
  variant from persisted run state.

The engine class and the adapter factory are *injected* into :func:`compose_run`
rather than referenced here directly: ``cli`` resolves ``Engine`` /
``StoriesEngine`` / ``_make_adapters`` from its own module namespace at call time,
so the test suite's ``monkeypatch.setattr(cli, "Engine", ...)`` (and friends)
still bites. ``cli`` re-exports :func:`make_adapters`, :func:`platform_preflight`,
:func:`mux_reason_label`, and :data:`ROLES` under their historical private names
so those seams stay importable and monkeypatchable from ``cli``.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import bmadconfig
from . import policy as policy_mod
from . import runs
from .checks import Finding
from .journal import Journal, save_state
from .model import RunState
from .platform_util import atomic_replace
from .runs import RUNS_DIR

if TYPE_CHECKING:
    from collections.abc import Callable

    from .adapters.base import CodingCLIAdapter
    from .engine import Engine
    from .policy import Policy
    from .stories_engine import StoriesEngine
    from .sweep import SweepEngine


# The three adapter roles a run wires. Defined here (the composition layer that
# actually builds them) and re-exported as ``cli.ROLES``, which `cmd_validate`
# and the test suite resolve.
ROLES = ("dev", "review", "triage")


def make_adapters(project: Path, run_dir: Path, policy) -> dict[str, CodingCLIAdapter]:
    from .adapters.generic import GenericAdapter, GenericDevAdapter
    from .adapters.multiplexer import get_multiplexer, mux_usable
    from .adapters.profile import ProfileError, get_profile

    # The dev skill (bmad-dev-auto) writes no result.json: its adapter
    # synthesizes the result from the spec, and so needs the project paths to
    # find that spec — rebasing onto the active worktree's implementation-
    # artifacts dir under isolation, not just the main checkout's.
    paths = bmadconfig.load_paths(project)
    mux = None
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
            if profile.hookless:
                # Hookless profiles (opencode-http) are driven over HTTP/SSE —
                # the tmux adapters below cannot host them.
                from .adapters.opencode_http import (
                    OpencodeDevAdapter,
                    OpencodeHttpAdapter,
                    OpencodeServerError,
                )

                common = dict(
                    run_dir=run_dir,
                    policy=policy,
                    profile=profile,
                    binary=cfg.binary or None,
                    extra_args=cfg.extra_args,
                    usage_grace_s=cfg.usage_grace_s,
                    stop_without_result_nudges=cfg.stop_without_result_nudges,
                )
                try:
                    # heterogeneous **kwargs: pyright unions the dict values; per-arg error is spurious
                    by_cfg[key] = (
                        OpencodeDevAdapter(**common, paths=paths)
                        if synthesizes
                        else OpencodeHttpAdapter(**common)  # pyright: ignore[reportArgumentType]
                    )
                except OpencodeServerError as e:
                    raise SystemExit(f"error: {e}") from e
            else:
                # Resolve and probe the shared multiplexer only when a profile
                # actually uses it; hookless HTTP/SSE runs need no transport.
                if mux is None:
                    mux = get_multiplexer()
                    if not mux_usable(mux):
                        try:
                            version = mux.version()
                        except Exception:  # diagnosing must not mask the refusal
                            version = None
                        raise SystemExit(
                            f"error: multiplexer backend {type(mux).__name__} is not usable on "
                            f"this host (reported version: {version}); its transport binary is "
                            "missing, the version is unsupported, or a required helper is "
                            "absent (psmux needs `pwsh` on PATH); see `bmad-loop diagnose`"
                        )
                # binary: "" means "the profile's own", which is exactly what the
                # adapters' `binary or profile.binary` already does with None.
                common = dict(
                    run_dir=run_dir,
                    policy=policy,
                    profile=profile,
                    binary=cfg.binary or None,
                    extra_args=cfg.extra_args,
                    usage_grace_s=cfg.usage_grace_s,
                    stop_without_result_nudges=cfg.stop_without_result_nudges,
                    mux=mux,
                )
                # heterogeneous **kwargs: pyright unions the dict values; per-arg error is spurious
                by_cfg[key] = (
                    GenericDevAdapter(**common, paths=paths)
                    if synthesizes
                    else GenericAdapter(**common)  # pyright: ignore[reportArgumentType]
                )
        adapters[role] = by_cfg[key]
    return adapters


def mux_reason_label(reason: str) -> str:
    """Human wording for a MuxBackendInfo.reason, shared by `mux` and validate."""
    return {
        "env": "forced by BMAD_LOOP_MUX_BACKEND",
        "policy": f"set by [mux] backend in {policy_mod.POLICY_FILE}",
        "platform-default": f"platform default for {sys.platform}",
        "first-match": "first available platform match",
        "fallback": "fallback (no registered backend is available)",
    }.get(reason, reason)


def platform_preflight() -> list[Finding]:
    """Probe the platform-selected seams — the terminal multiplexer and the process
    host — for `cmd_validate`, returning the findings in emission order.

    A backend reports its own readiness through ``available()`` / ``version()``, so
    a new OS or transport surfaces here by *registering* rather than by adding a
    ``sys.platform`` branch to validate. The process host is named so a
    misselection (e.g. the Windows host picked on Linux) is visible at a glance.
    """
    from .adapters.multiplexer import (
        detect_multiplexers,
        external_backend_errors,
        get_multiplexer,
    )
    from .process_host import get_process_host

    found: list[Finding] = []

    try:
        backend = get_multiplexer()
        label = type(backend).__name__
        version = backend.version()
        if backend.available():
            found.append(
                Finding(
                    "mux.backend",
                    "ok",
                    f"multiplexer {label} available" + (f" ({version})" if version else ""),
                    {"backend": label, "available": True, "version": version},
                )
            )
        else:
            found.append(
                Finding(
                    "mux.backend",
                    "problem",
                    f"multiplexer {label} unavailable"
                    + (f" (reports {version})" if version else "")
                    + " — its transport binary is missing, the version is unsupported, or a "
                    "required helper is absent (psmux needs `pwsh` on PATH); "
                    "see `bmad-loop diagnose`",
                    {"backend": label, "available": False, "version": version},
                )
            )
    except Exception as e:  # selection or readiness must not abort validate
        found.append(Finding("mux.preflight", "problem", f"multiplexer preflight failed: {e}"))

    try:
        infos = detect_multiplexers()
    except Exception:  # detection is advisory; never break validate
        infos = []
    if len(infos) > 1:  # a lone tmux needs no listing; keep single-backend output stable
        listed = ", ".join(
            i.name
            + ("*" if i.selected else "")
            + (
                " (available" + (f", {i.version}" if i.version else "") + ")"
                if i.available
                else " (unavailable)"
            )
            for i in infos
        )
        # The text flattens each row into a suffix soup ("tmux*, psmux
        # (unavailable)") whose trailing `*` a consumer would have to parse to
        # learn which backend is selected. The detail keeps the rows themselves.
        found.append(
            Finding(
                "mux.backends-detected",
                "ok",
                f"mux backends: {listed} — `bmad-loop mux` for details",
                {
                    "backends": [
                        {
                            "name": i.name,
                            "matches_platform": i.matches_platform,
                            "available": i.available,
                            "version": i.version,
                            "selected": i.selected,
                            "reason": i.reason,
                        }
                        for i in infos
                    ]
                },
            )
        )
    chosen = next((i for i in infos if i.selected), None)
    if chosen and chosen.reason in ("env", "policy"):
        # detail keeps the raw enum, not mux_reason_label's prose: the label is
        # wording ("set by [mux] backend in .bmad-loop/policy.toml"), the enum is
        # the value MuxBackendInfo.reason actually carries.
        found.append(
            Finding(
                "mux.selection",
                "ok",
                f"multiplexer selection {mux_reason_label(chosen.reason)}",
                {"backend": chosen.name, "reason": chosen.reason},
            )
        )

    # A warning, not a problem and not a note: an installed package the operator
    # asked for did not load, which is a real failure — but selection already
    # degraded past it (a failed external can never be the selected backend), so
    # the preflight outcome above is authoritative and the verdict must not flip.
    # `cmd_mux` has always printed this same condition as `warning:`; validate was
    # the outlier, pinned to "ok" because promoting inserts "  warning: " into the
    # text (render() keeps the double prefix by design) and the TUI rendered that
    # text verbatim. Since #210 the TUI reads `validate --json` and styles from the
    # severity field, so the severity is free to say what the message already does.
    for ep_name, reason in sorted(external_backend_errors().items()):
        found.append(
            Finding(
                "mux.external-backend",
                "warning",
                f"external mux backend '{ep_name}' failed to load: {reason}",
                {"entry_point": ep_name, "error": reason},
            )
        )

    try:
        host = type(get_process_host()).__name__
        found.append(Finding("host.process", "ok", f"process host: {host}", {"host": host}))
    except Exception as e:  # a bad BMAD_LOOP_PROCESS_HOST must report, not crash
        found.append(Finding("host.process", "problem", f"process host preflight failed: {e}"))

    return found


def build_run_state(
    *,
    run_id: str,
    project: Path,
    policy: Policy,
    epic_filter: int | None,
    story_filter: str | None,
    max_stories: int | None,
    stories_on: bool,
    spec_folder: str,
) -> RunState:
    """Assemble the launch-time :class:`RunState` for a fresh run.

    ``policy_snapshot`` freezes ``policy`` at launch so every later display reads
    the weights the run actually launched under; ``source`` / ``spec_folder``
    record which queue the run dispatches (a stories manifest vs sprint-status)."""
    return RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=policy.to_dict(),
        epic_filter=epic_filter,
        story_filter=story_filter,
        max_stories=max_stories,
        source="stories" if stories_on else "sprint-status",
        spec_folder=spec_folder if stories_on else "",
    )


@dataclass
class ComposedRun:
    """The composed-but-not-yet-run artifacts a ``compose_*`` returns for its
    callback to render from — shared by :func:`compose_run`, :func:`compose_sweep`,
    and :func:`compose_resume`.

    ``engine`` is ready to :meth:`run`; ``run_id`` names the run for the attach
    hint. ``run_dir`` / ``state`` / ``journal`` are the persisted context a caller
    other than the CLI can inspect."""

    engine: Engine
    run_id: str
    run_dir: Path
    state: RunState
    journal: Journal


def compose_run(
    *,
    project: Path,
    paths: bmadconfig.ProjectPaths,
    policy: Policy,
    run_id: str | None,
    epic_filter: int | None,
    story_filter: str | None,
    max_stories: int | None,
    stories_on: bool,
    spec_folder: str,
    sweep_factory: Callable[[str], None],
    make_adapters: Callable[[Path, Path, Policy], dict[str, CodingCLIAdapter]],
    engine_cls: type[Engine],
    stories_engine_cls: type[StoriesEngine],
) -> ComposedRun:
    """Stand up a run: allocate the run dir, persist state + pid, build the
    adapters, and wire the engine — everything ``cmd_run`` did inline between its
    preflight gates and ``engine.run()``.

    ``make_adapters`` and the engine classes are injected (rather than imported
    here) so ``cli`` supplies its own module-level names — keeping the test
    suite's ``monkeypatch.setattr(cli, "Engine"/"_make_adapters", ...)`` effective.
    """
    run_id = run_id or runs.new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = build_run_state(
        run_id=run_id,
        project=project,
        policy=policy,
        epic_filter=epic_filter,
        story_filter=story_filter,
        max_stories=max_stories,
        stories_on=stories_on,
        spec_folder=spec_folder,
    )
    save_state(run_dir, state)
    runs.write_pid(run_dir)
    adapters = make_adapters(project, run_dir, policy)
    journal.append(
        "run-start",
        run_id=run_id,
        source=state.source,
        adapter_dev=policy.adapter.resolved("dev").name,
        adapter_review=policy.adapter.resolved("review").name,
    )
    common = dict(
        paths=paths,
        policy=policy,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        run_dir=run_dir,
        journal=journal,
        state=state,
        max_stories=max_stories,
        epic_filter=epic_filter,
        story_filter=story_filter,
        sweep_factory=sweep_factory,
    )
    # heterogeneous **kwargs: pyright unions the dict values; per-arg error is spurious
    engine: Engine = (
        stories_engine_cls(**common, spec_folder=spec_folder)
        if stories_on
        else engine_cls(**common)  # pyright: ignore[reportArgumentType]
    )
    return ComposedRun(engine=engine, run_id=run_id, run_dir=run_dir, state=state, journal=journal)


def compose_sweep(
    *,
    project: Path,
    paths: bmadconfig.ProjectPaths,
    policy: Policy,
    run_id: str | None,
    prompting: bool,
    decisions_only: bool,
    max_bundles: int | None,
    repeat: bool | None,
    max_cycles: int | None,
    trigger: str,
    make_adapters: Callable[[Path, Path, Policy], dict[str, CodingCLIAdapter]],
    sweep_engine_cls: type[SweepEngine],
) -> ComposedRun:
    """Stand up a sweep run: allocate the run dir, persist state + pid, record the
    sweep options, build the adapters, and wire the ``SweepEngine`` — everything
    ``cli._start_sweep`` did inline before ``engine.run()``.

    ``sweep.json`` freezes the launch options so a resume rebuilds the same sweep
    (see :func:`compose_resume`). ``make_adapters`` and ``sweep_engine_cls`` are
    injected so ``cli`` supplies its own module-level names — keeping the test
    suite's ``monkeypatch.setattr(cli, "SweepEngine"/"_make_adapters", ...)``
    effective."""
    run_id = run_id or runs.new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=policy.to_dict(),
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
    # Persist the sweep options atomically (tmp + os.replace), the way save_state
    # writes state.json: a resume reads this back to rebuild the SweepEngine, so a
    # crash mid-write must not leave a torn file the recovery path then chokes on.
    sweep_path = run_dir / "sweep.json"
    sweep_tmp = sweep_path.with_suffix(".json.tmp")
    sweep_tmp.write_text(json.dumps(options, indent=2), encoding="utf-8")
    atomic_replace(sweep_tmp, sweep_path)
    adapters = make_adapters(project, run_dir, policy)
    journal.append("run-start", run_id=run_id, run_type="sweep", trigger=trigger)
    engine: Engine = sweep_engine_cls(
        paths=paths,
        policy=policy,
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
    return ComposedRun(engine=engine, run_id=run_id, run_dir=run_dir, state=state, journal=journal)


def compose_resume(
    *,
    project: Path,
    paths: bmadconfig.ProjectPaths,
    run_dir: Path,
    state: RunState,
    policy: Policy,
    journal: Journal,
    sweep_factory: Callable[[str], None],
    make_adapters: Callable[[Path, Path, Policy], dict[str, CodingCLIAdapter]],
    engine_cls: type[Engine],
    stories_engine_cls: type[StoriesEngine],
    sweep_engine_cls: type[SweepEngine],
) -> ComposedRun:
    """Rebuild the engine for a paused/interrupted run and return it ready to
    :meth:`run` — the adapter build + engine selection ``cli._resume_paused_run``
    did inline.

    ``state`` arrives already re-stamped and persisted by the caller: the resume
    policy-snapshot reconciliation and the pause/pid/graceful-stop bookkeeping stay
    CLI-side (their ordering is load-bearing — see ``_resume_paused_run``), so this
    lifts only the composition. The variant is selected from persisted run state:
    ``run_type == "sweep"`` rebuilds a ``SweepEngine`` from ``sweep.json``;
    otherwise ``source`` picks ``StoriesEngine`` vs ``Engine``, restoring the
    launching scope + cap so a resumed ``--epic N`` run keeps its filter. The engine
    classes and ``make_adapters`` are injected so ``cli``'s ``monkeypatch.setattr``
    seams bite."""
    # drop any stale agent session so the run spins up a fresh one (a stopped or
    # interrupted run can leave a lingering bmad-loop-<id> session behind).
    runs.kill_session(run_dir.name)
    adapters = make_adapters(project, run_dir, policy)
    if state.run_type == "sweep":
        opts_path = run_dir / "sweep.json"
        try:
            opts = json.loads(opts_path.read_text(encoding="utf-8")) if opts_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            # A torn/corrupt sweep.json (crash mid-write on an older run) must not
            # abort the recovery path — fall back to the same launch defaults as
            # the missing-file arm, mirroring tui.data's tolerant run-dir reads.
            opts = {}
        engine: Engine = sweep_engine_cls(
            paths=paths,
            policy=policy,
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
        story_common = dict(
            paths=paths,
            policy=policy,
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
            sweep_factory=sweep_factory,
        )
        # stories mode is pinned in run state at launch, so resume rebuilds the
        # same picker (StoriesEngine) without any flag.
        # heterogeneous **kwargs: pyright unions the dict values; per-arg error is spurious
        engine = (
            stories_engine_cls(**story_common, spec_folder=state.spec_folder)
            if state.source == "stories"
            else engine_cls(**story_common)  # pyright: ignore[reportArgumentType]
        )
    return ComposedRun(
        engine=engine, run_id=run_dir.name, run_dir=run_dir, state=state, journal=journal
    )
