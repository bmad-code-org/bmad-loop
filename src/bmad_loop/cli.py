"""bmad-loop command line interface."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    __version__,
    bmadconfig,
    decisions,
    deferredwork,
    devcontract,
    envvars,
    events,
    frontmatter,
    gates,
    install,
    machine,
    operatoractions,
)
from . import policy as policy_mod
from . import (
    resolve,
    runs,
    runsetup,
    sprintstatus,
)
from . import stories as stories_mod
from . import (
    verify,
)

# Re-exported for the test suite, which stubs `_platform_preflight` with
# `cli.Finding(...)`; no longer referenced within this module (the preflight body
# moved to runsetup), so it needs the pin or ruff F401 autofix would drop it.
from .checks import Finding  # noqa: F401 — re-export
from .checks import ValidationReport

# The --json document builders live in documents.py (the library-level projection
# layer a non-CLI frontend imports). The schema constants are re-exported rather
# than used here — `cli.STATUS_SCHEMA_VERSION` and friends are the published names
# consumers and tests already resolve — so each carries a noqa: without it ruff
# F401 autofixes the re-export away and every `cli.*_SCHEMA_VERSION` reference
# breaks. Keep the noqa when adding a command.
from .documents import CLEAN_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import CLEANUP_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import CONFIRM_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import DECISIONS_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import LIST_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import STATUS_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import VALIDATE_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import (
    clean_document,
    cleanup_document,
    confirm_document,
    decisions_document,
    list_document,
    run_token_totals,
    status_document,
    validate_document,
)
from .engine import Engine
from .journal import Journal, load_state, save_state
from .model import RunState
from .platform_util import MAX_SEGMENT, resolve_or_lexical, walk_files_unlinked
from .process_host import ProcessHostError

# The run-composition helpers now live in runsetup.py (the library layer a non-CLI
# frontend imports). They are re-exported under their historical private names —
# used within this module (validate/mux/sweep/resume) and monkeypatched as
# `cli.<name>` by the test suite — so the seams stay importable here. Each is
# genuinely referenced below, so no noqa is needed (an unused-import pin would
# itself trip RUF100); a future caller-less re-export would need a `noqa: F401`
# pin. Written without the leading hash on purpose: ruff scans comment TEXT for
# directives, so spelling one out in prose is read as a real (and malformed) one.
from .runsetup import ROLES
from .runsetup import make_adapters as _make_adapters
from .runsetup import mux_reason_label as _mux_reason_label
from .runsetup import platform_preflight as _platform_preflight
from .stories_engine import StoriesEngine
from .sweep import SweepEngine

if TYPE_CHECKING:
    from collections.abc import Callable

    # Type-only: annotate the profile-lookup map without a module-level adapter
    # import (cli.py imports the adapter package lazily inside functions).
    from .adapters.profile import CLIProfile

POLICY_FILE = policy_mod.POLICY_FILE


class ExitCode(IntEnum):
    """The process exit codes ``main()`` contracts — names for the released numbers.

    ``rc`` is a released contract, so these values do not move; the enum just gives
    the existing numbers a name at their use sites. Being an ``IntEnum`` it returns
    from ``main()`` (``-> int``) and reaches ``sys.exit`` transparently.

    - ``OK`` — a command handler returned success (``args.func`` returns it, not
      ``main`` directly).
    - ``FAILURE`` — a typed error surfaced to the dispatch tail, or the broad
      backstop caught an unexpected exception; both print ``error: …`` to stderr.
    - ``USAGE`` — an argparse usage error (unknown subcommand, bad flag). argparse
      raises ``SystemExit(2)`` before dispatch, so this names its number rather than
      being returned here.
    - ``INTERRUPTED`` — Ctrl+C (SIGINT) escaped ``main()`` outside ``engine.run()``
      (config load, engine construction, a handler with no run loop). 130 = 128 +
      SIGINT(2), the shell's conventional code for an interrupt; uncaught this path
      already reached 130 (CPython re-raises SIGINT) but as a bare traceback, so
      ``main()`` catches it for a clean one-line exit at the same code. The in-run
      interrupt converts to a clean ``RunStopped`` (``engine.py``) and never reaches
      here — a Ctrl+C during a run stays rc 0.

    Codes ``3``-``129`` and ``131+`` are intentionally absent until a consumer needs one.
    """

    OK = 0
    FAILURE = 1
    USAGE = 2
    INTERRUPTED = 130


def _project(args: argparse.Namespace) -> Path:
    """The project root every handler works from, canonicalized.

    Degrades rather than fails when the OS cannot canonicalize it — see
    :func:`platform_util.resolve_or_lexical` for the decision and its bounds. This
    is called from ``main()`` before dispatch, so a raise here takes out every
    subcommand at the backstop, ``diagnose`` and ``validate`` included (#552)."""
    return resolve_or_lexical(args.project)


def _policy_path(project: Path) -> Path:
    return project / POLICY_FILE


def _configure_mux(project: Path) -> None:
    """Install the policy ``[mux] backend`` choice into the multiplexer seam.

    The single configuration point, called from ``main()`` before dispatch so
    every mux consumer — including probe/diagnose/attach/stop, which never load
    policy themselves — selects under the persisted choice. Tolerant of a broken
    policy file: diagnostics must keep working on a misconfigured host, and the
    commands that need policy re-load it loudly themselves."""
    from .adapters.multiplexer import configure_multiplexer

    path = _policy_path(project)
    try:
        name = policy_mod.load(path).mux.backend or None
    except (policy_mod.PolicyError, OSError):
        name = None
    configure_multiplexer(name, origin=path)


def _reject_bad_run_id(run_id: str | None) -> int | None:
    """Guard the hidden ``--run-id`` flag before the id becomes a directory name, a
    multiplexer session name and a git ref component. Rejects rather than sanitizes
    (see ``runs.is_valid_run_id``): a coerced id would no longer name the run the
    caller asked for. Returns 1 to abort, None to proceed."""
    if run_id is not None and not runs.is_valid_run_id(run_id):
        print(
            f"error: invalid --run-id {run_id!r} — expected {runs.RUN_ID_RE.pattern} "
            f"(at most {MAX_SEGMENT} characters, not a reserved device name)",
            file=sys.stderr,
        )
        return 1
    return None


def _reject_isolation_conflict(paths: bmadconfig.ProjectPaths, pol) -> int | None:
    """Refuse `isolation = "worktree"` under a `repo_root` override (#414). Returns
    1 to abort, None to proceed — the `_reject_bad_run_id` shape.

    Called from the three :class:`~engine.Engine` construction sites that return an
    rc to a human: `cmd_run`, `cmd_sweep`, and `_resume_paused_run` — the shared
    helper behind both `resume` and `resolve`'s re-arm. The fourth such site, the
    auto-triggered child sweep in `_sweep_factory`, shares the refusal but not this
    disposition: it has no rc channel, so it raises (see the comment there).
    Keyed on Engine construction rather than on "loads policy.toml", which is a
    wider set that does not all provision — `_configure_mux` reads the file on
    every command and builds nothing; `cmd_validate` and `cmd_clean` load it and
    never mount a worktree.

    `validate` deliberately does not call this — it reports rather than aborts, so
    it renders the same message as a Finding and keeps running its other gates."""
    conflict = bmadconfig.worktree_isolation_conflict(paths, pol.scm.isolation)
    if conflict is None:
        return None
    print(conflict, file=sys.stderr)
    return 1


def _launch_profiles(pol, project: Path) -> dict[str, CLIProfile]:
    """Resolve a run's profiles ONCE, for the two human-initiated entrypoints that
    stamp an integrity baseline (`cmd_run`, `_resume_paused_run`).

    The stamp and the adapters must describe the SAME bytes. Each used to read
    `profiles/*.toml` on its own — the digest via `config_digest`, the adapters via
    `make_adapters` — so a write landing between them stamped a baseline for config
    the run never launched.

    This is NOT the child gate's check-then-use, and must not be read as one: there
    is no comparison here. At launch the on-disk config IS the trust anchor, so an
    adversary who can write in that gap could equally have written BEFORE it and
    been blessed outright, with no timing at all — the race grants no capability.
    The defect runs the other way, toward over-refusal. This pin is the one
    baseline a session cannot reach (`_sweep_factory` holds every later auto-sweep
    to it from MEMORY, unlike resume's disk-backed advisory), so a pin describing
    bytes the run did not launch makes those children refuse the config the parent
    has been running all along — every trigger, for the life of the run. The
    refusal no longer *burns* each trigger (#501: `sweeps_triggered` records only
    a child that started), but that buys nothing here: a wrong pin refuses the next
    trigger exactly as it refused the last.

    `cmd_sweep` deliberately keeps its fresh read: a human started it, and the pin
    it stamps gates no child.

    Same `ProfileError` -> `SystemExit` contract as `_trusted_config_digest`, for
    the same reason — resolving here happens a few frames EARLIER than
    `make_adapters` used to, and a misconfigured `[adapter] name` must still exit 1
    as `error: unknown CLI profile ...` rather than escape as a traceback."""
    from .adapters.profile import ProfileError

    try:
        return runsetup.resolve_profiles(pol, project)
    except ProfileError as e:
        raise SystemExit(f"error: {e}") from e


def _trusted_config_digest(pol, project: Path, *, profiles=None) -> str:
    """The launch-time integrity baseline the auto-sweep child is held to (#461
    point 4). Thin wrapper over :func:`runsetup.config_digest` for the two
    human-initiated entrypoints that stamp one (`cmd_run`, `_resume_paused_run`)
    plus `_start_sweep`.

    Its only job is the error contract: `config_digest` resolves profiles, so an
    unknown `[adapter] name` now fails a few frames EARLIER than it used to.
    `make_adapters` renders that as `error: unknown CLI profile ...` and exits 1,
    so re-raise it in the same shape rather than letting a `ProfileError`
    traceback escape a misconfigured `run`. The child-sweep gate deliberately does
    NOT go through here — there a raise is the point.

    `profiles` forwards an already-resolved `runsetup.resolve_profiles` mapping,
    so the stamped pin is taken from the same resolution the caller gated on and
    the adapters are built from — never a fresh read."""
    from .adapters.profile import ProfileError

    try:
        return runsetup.config_digest(pol, project, profiles=profiles)
    except ProfileError as e:
        raise SystemExit(f"error: {e}") from e


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


# ----------------------------------------------------------------- commands


def cmd_validate(args: argparse.Namespace) -> int:
    from .install import relay_registered

    project = _project(args)
    report = ValidationReport()

    try:
        paths = bmadconfig.load_paths(project)
        report.ok(
            "bmad-config",
            f"BMAD config OK: artifacts at {paths.implementation_artifacts}",
            {"implementation_artifacts": str(paths.implementation_artifacts)},
        )
    except bmadconfig.BmadConfigError as e:
        report.fail("bmad-config", str(e))
        paths = None

    # Policy first — its [stories].source (or a --spec override) selects which
    # story-queue gate runs below: the sprint-status file (sprint mode) or the
    # stories.yaml manifest (stories mode). Loaded before the queue gate so a
    # stories-only project is not failed on a missing sprint-status.yaml.
    from .adapters import registry as adapter_registry
    from .adapters.profile import ProfileError, external_profile_errors, get_profile

    profiles = []
    profile_by_name: dict[str, CLIProfile] = {}
    pol = None
    try:
        pol = policy_mod.load(_policy_path(project))
        role_names = {role: pol.adapter.resolved(role).name for role in ROLES}
        report.ok(
            "policy",
            f"policy OK: gates={pol.gates.mode}, "
            f"adapter dev={role_names['dev']}, review={role_names['review']}, "
            f"triage={role_names['triage']}",
            {"gates_mode": pol.gates.mode, "adapters": dict(role_names)},
        )
        for name in dict.fromkeys(role_names.values()):
            try:
                profile = get_profile(name, project)
                profiles.append(profile)
                profile_by_name[name] = profile
            except ProfileError as e:
                report.fail("adapter.profile", str(e), {"profile": name})
    except policy_mod.PolicyError as e:
        report.fail("policy", str(e))

    # #414: the one configuration where every gate below reports on a surface the
    # isolated run will never see. Reported only when it fires — there is no `ok`
    # twin, because the supported case is "no such conflict", which would print a
    # line about a coupling most projects have never configured either half of.
    # Needs both halves loaded; either failing already has its own finding above.
    if paths is not None and pol is not None:
        conflict = bmadconfig.worktree_isolation_conflict(paths, pol.scm.isolation)
        if conflict is not None:
            report.fail(
                "policy.isolation-repo-root",
                conflict,
                {"repo_root": str(paths.repo_root), "project": str(paths.project)},
            )

    # Built exactly the way run/sweep's real preflight builds it, so validate's
    # verdict and their abort cannot disagree. Deliberately NOT `[p.skill_tree for p
    # in profiles]`: that carries triage's tree, and every skills check below asks a
    # dev-primitive question. The `pol is not None` guard is load-bearing —
    # `resolved` never raises, so the except above fires only on a policy that failed
    # to load, and an unguarded call would crash validate instead of reporting the
    # parse failure.
    dev_trees = _skill_trees(project, pol) if pol is not None else []

    stories_on, spec_folder = _stories_mode(args, pol)
    if paths:
        if stories_on:
            _validate_stories_queue(project, paths, spec_folder, dev_trees, report)
            _validate_deferred_ledger(paths, report, spec_folder=spec_folder)
        else:
            _validate_deferred_ledger(paths, report)
            _validate_operator_registry(project, paths, report)
            try:
                ss = sprintstatus.load(paths.sprint_status)
                actionable = [s for s in ss.stories if s.status in sprintstatus.ACTIONABLE_STATUSES]
                report.ok(
                    "queue.sprint-status",
                    f"sprint-status OK: {len(ss.stories)} stories, {len(actionable)} actionable",
                    {"stories": len(ss.stories), "actionable": len(actionable)},
                )
                if ss.unknown_keys:
                    report.warn(
                        "queue.sprint-status-unknown-keys",
                        f"unknown keys ignored: {', '.join(ss.unknown_keys)}",
                        {"unknown_keys": list(ss.unknown_keys)},
                    )
            except sprintstatus.SprintStatusError as e:
                report.fail("queue.sprint-status", str(e))

    try:
        if not verify.worktree_clean(project):
            report.fail(
                "git.worktree-clean", "git worktree is not clean — commit or stash before running"
            )
        else:
            report.ok("git.worktree-clean", "git worktree clean")
    except verify.GitError as e:
        report.fail("git.probe", f"git check failed: {e}")

    # An ignore/exclude cannot shield renderer output that is already in the index.
    # This is advisory: tracked output causes churn but does not prevent a session
    # from running. A failed git probe stays silent rather than fabricating an OK.
    try:
        if verify.path_tracked(project, install.RENDER_DIR_REL):
            report.warn(
                "git.render-tracked",
                f"{install.RENDER_DIR_REL}/ is tracked by git; run "
                f"`git rm -r --cached {install.RENDER_DIR_REL}` and commit once to stop "
                "committing rendered skill output",
                {"path": install.RENDER_DIR_REL},
            )
    except verify.GitError:
        pass

    report.extend(_platform_preflight(project))

    # #231: notify.desktop defaults to true but only fires when a platform notifier
    # exists (osascript/PowerShell/notify-send). When none does, the setting is
    # silently inert — warn so it stops being a no-op nobody can see.
    if pol is not None and pol.notify.desktop and gates.desktop_notifier_kind() is None:
        # The ATTENTION file is only a fallback when notify.file is also on; with it
        # off there is no alert channel left at all, so say so rather than pointing
        # at a file that is never written.
        channel = (
            "the ATTENTION file in the run directory is the only alert channel left"
            if pol.notify.file
            else "notify.file is also off, so no alert channel is configured — "
            "enable notify.file"
        )
        report.warn(
            "notify.desktop-unavailable",
            f"notify.desktop is set but no desktop notifier is available on "
            f"{sys.platform} — desktop notifications are silently skipped; "
            f"{channel} (macOS ships osascript; Linux needs notify-send; Windows "
            f"needs PowerShell). Install one or set notify.desktop = false.",
            {"platform": sys.platform},
        )

    from . import probe as probe_mod

    packaged_binaries = {p.binary for p in profiles if p.packaged}
    for tool in dict.fromkeys(p.binary for p in profiles):
        resolved = shutil.which(tool)
        if resolved:
            report.ok("adapter.binary", f"{tool} found", {"binary": tool})
        else:
            report.fail("adapter.binary", f"{tool} not found on PATH", {"binary": tool})
            continue
        # #294: the gate above answers "a file with that name carries the execute
        # bit", which a dead WSL/npm shim satisfies while every launch of it fails.
        # So validate went green on an install that could not start a session —
        # and opencode_http's own "binary not found" remedy points the user at
        # `bmad-loop validate`, which then told them everything was fine. Probe the
        # path `which` RETURNED rather than the bare name: re-resolving is a TOCTOU,
        # and on Windows the PATHEXT shim `which` picked is the very file at issue.
        # Probe ONLY a binary a PACKAGED profile named. `binary` is
        # project-controlled end to end — policy.toml picks the profile and
        # `.bmad-loop/profiles/*.toml` supplies its fields, both arriving with a
        # clone — and this line EXECUTES it, inside the one command a user runs to
        # decide whether a checkout is safe to run at all (the TUI runs it too).
        #
        # The boundary is provenance because no test on the SPELLING of `binary`
        # can hold: rejecting a path (`./tool`) still leaves a bare `pwn`, which
        # `which` resolves to a repository file whenever a checkout-local
        # directory is on PATH. "Who wrote this profile" is the question actually
        # being asked, and it has a categorical answer. An overlay or entry-point
        # profile keeps the pre-#294 behavior: resolved, reported found, never
        # launched. #294's own case is a packaged profile (opencode), so the dead
        # WSL/npm shim is still caught.
        #
        # What this bounds is WHICH NAME is probed, never what that name resolves
        # to. Resolution is `shutil.which` against the user's PATH, so a PATH
        # carrying a checkout-local directory can still answer `claude` with a
        # file the clone ships. That residual is deliberate and is NOT a hole this
        # gate is failing to close: the name is ours rather than the project's, and
        # the same resolution is what the session launch itself performs — the
        # generic adapter puts this bare `binary` at argv[0] (adapters/generic.py)
        # and the opencode adapter calls the identical `shutil.which` before
        # spawning (adapters/opencode_http.py). A PATH that redefines `claude`
        # has already redefined it for the run, and for the user's own shell.
        # Refusing checkout-local RESOLUTIONS would be a different guard, over a
        # predicate (realpath containment) that leaks through symlinks, `..`,
        # win32 case-folding, UNC paths, and worktree-root vs project-root.
        if tool not in packaged_binaries:
            continue
        rc = probe_mod.binary_runs(resolved)
        if rc == 0:
            continue
        # Any nonzero code, never an allowlist: #294's own transcript reports rc 2
        # and a reproduction of the same shim exits 127, the code being a property
        # of the shell and the failure mode. {126, 127} would miss the case fixed.
        #
        # `warning`, deliberately, and not to be promoted without evidence: severity
        # `problem` is validate's exit code (checks.py), and rc is a compatibility
        # contract (AGENTS.md). Nothing rules out one of claude/codex/gemini/copilot/
        # antigravity answering `--version` nonzero on a perfectly live install, and
        # that user must not start failing validate.
        outcome = "could not be launched" if rc is None else f"exited {rc}"
        report.warn(
            "adapter.binary-unrunnable",
            f"{tool} is on PATH at {resolved} but `{tool} --version` {outcome} — "
            "the usual cause is a stale or broken install (a dead WSL/npm shim), "
            "and runs using it would then fail to start; a CLI that does not "
            "implement `--version` also lands here. Reinstall it or fix PATH, or "
            "ignore this if that CLI has no `--version`.",
            {"binary": tool, "path": resolved, "returncode": rc},
        )

    any_hooks_registered = False
    for profile in profiles:
        # Keyed on the adapter KIND, not on `hookless`. httpx is the bundled
        # opencode family's optional extra — a fact about one adapter class, which
        # is a different question from "does this profile register hooks". Those
        # were the same question only while `hookless` selected the adapter; the
        # registry decoupled them, so a hookless profile driven by some other
        # registered kind needs nothing from `bmad-loop[opencode]` and must not be
        # FAILed with a remedy that installs the wrong package. Naming one bundled
        # kind here is not the hardcoded-valid-set the registry exists to remove:
        # the set of VALID kinds is only ever `known_adapter_kinds()` (below).
        if profile.adapter == adapter_registry.OPENCODE_HTTP:
            # Surface a missing install here instead of at run start.
            if importlib.util.find_spec("httpx") is not None:
                report.ok(
                    "adapter.httpx",
                    f"httpx available for {profile.name}",
                    {"profile": profile.name},
                )
            else:
                report.fail(
                    "adapter.httpx",
                    f"{profile.name}: httpx not installed — "
                    f"run `pip install 'bmad-loop[opencode]'`",
                    {"profile": profile.name},
                )
        if profile.hookless:
            report.ok(
                "adapter.hookless",
                f"{profile.name}: hookless (HTTP/SSE transport) — no hook registration needed",
                {"profile": profile.name},
            )
            continue
        hook_config = project / profile.hooks.config_path
        hooks_ok = False
        if hook_config.is_file():
            try:
                parsed = json.loads(hook_config.read_text(encoding="utf-8"))
                hooks_ok = isinstance(parsed, dict) and relay_registered(
                    parsed, profile.hooks.dialect, profile.hooks.events
                )
            except json.JSONDecodeError:
                report.fail(
                    "hooks.config-parse",
                    f"{hook_config} is not valid JSON",
                    {"profile": profile.name, "config_path": str(hook_config)},
                )
        if hooks_ok:
            any_hooks_registered = True
            report.ok(
                "hooks.registered",
                f"bmad-loop hooks registered for {profile.name}",
                {"profile": profile.name, "config_path": str(hook_config)},
            )
        else:
            report.fail(
                "hooks.registered",
                f"bmad-loop hooks not registered for {profile.name} — "
                f"run `bmad-loop init --cli {profile.name}`",
                {"profile": profile.name, "config_path": str(hook_config)},
            )

    # #461: `hooks.registered` above is a substring match on the config JSON — it
    # never touches the artifact the registered command points AT. A branch switch
    # (or a deleted .bmad-loop/) leaves the registration green while every hook
    # event is a silent no-op and the run stalls to session_timeout_min, so stat
    # the relay itself. Outside the per-profile loop on purpose: the relay is one
    # shared artifact, and per-profile reporting would print the same line N times.
    # A distinct id, not a repurposed `hooks.registered` — the two answer different
    # questions and an operator needs to see which one failed.
    #
    # COUPLING (#461 Phase 2): Phase 2 moves the relay to the installed console
    # script — `bmad-loop relay <Event>` (cmd_relay / events.py), NOT the
    # `<abs-python> -m bmad_loop.hookrelay` spelling this once anticipated — and
    # retires HOOK_SCRIPT_REL. It must RETARGET this check to stat what the
    # registration actually points at (the resolved `bmad-loop` executable), not
    # drop it — the stall it guards against survives the move: an entry point that
    # is gone or unreadable strands every hook event exactly like a missing script.
    if any_hooks_registered:
        relay = project / install.HOOK_SCRIPT_REL
        # Existence is not enough: `is_file()` stays True for a mode-000 file, and
        # the registered command is `<interpreter> <relay> <Event>`, which has to
        # READ the script — an unreadable relay exits 2 ("can't open file") and the
        # run stalls exactly as if the relay were gone, which is the blind spot
        # this whole check exists to remove. `os.access` uses the REAL uid/gid,
        # which is what the operator's own `bmad-loop` invocation runs as, and it
        # stays correct under root (who can read a 000 file) where a mode-bit test
        # would false-fail. On Windows `chmod` can only toggle the read-only flag,
        # so this arm is POSIX-effective and never makes the Windows path stricter.
        if not relay.is_file():
            report.fail(
                "hooks.relay-present",
                f"hooks are registered but the relay script {relay} is missing — "
                f"run `bmad-loop init`",
                {"path": str(relay)},
            )
        elif not os.access(relay, os.R_OK):
            # Deliberately NOT "run `bmad-loop init`": install_into writes this path
            # with write_text(), which needs write access to the same file, so init
            # raises PermissionError instead of repairing it. Sending the operator
            # to a command that also fails is worse than saying nothing.
            report.fail(
                "hooks.relay-present",
                f"hooks are registered but the relay script {relay} is not readable — "
                f"the registered hook command cannot run it, so every hook event "
                f"no-ops. Restore read permission (`chmod u+r`) or delete it and "
                f"re-run `bmad-loop init`",
                {"path": str(relay)},
            )
        else:
            report.ok(
                "hooks.relay-present",
                f"hook relay script present: {relay}",
                {"path": str(relay)},
            )

        # #494 Phase 4: present-and-readable is not current. The relay is COPIED
        # into the project by `init`, so an upgraded orchestrator routinely drives
        # sessions through a relay written by an older wheel — and the #494 move
        # is exactly the kind of change that skew hides: a pre-move relay writes
        # its events to the in-tree `<run-dir>/events` while the operator believes
        # the channel left the project tree, so a branch switch can still take the
        # control plane away mid-run.
        #
        # A WARNING, never a problem, and validate's exit code must not move:
        # Phase 3's fallback pair keeps a stale relay FUNCTIONAL (it writes the
        # legacy directory, which SignalWatcher still polls), so the run completes
        # — the operator is losing the property, not the loop. `passed` counts
        # only problems, so `warn` is what says "degraded but working".
        stale = install.hook_script_current(project)
        if stale is False:
            report.warn(
                "hooks.relay-stale",
                f"the installed hook relay {relay} differs from this bmad-loop's "
                f"— it is from another version, or was edited. Events may still be "
                f"written inside the project tree; run `bmad-loop init` to refresh it",
                {"path": str(relay)},
            )
        elif stale is True:
            report.ok(
                "hooks.relay-stale",
                f"hook relay script up to date: {relay}",
                {"path": str(relay)},
            )
        # `None` (unreadable/undecodable on either side) reports nothing: the
        # relay-present block above already spoke for the cases an operator can
        # act on, and "I could not compare" is not a finding about their project.

    # Adapter-kind validity is enforced against the LIVE registry, never a
    # hardcoded set: a profile.adapter naming no registered kind is a config error
    # (a typo, or an uninstalled plugin package). External adapter/profile packages
    # that failed to load are surfaced as warnings — selection already degraded
    # past them (the same non-blocking treatment as a failed mux backend package).
    kinds = adapter_registry.known_adapter_kinds()
    for profile in profiles:
        if profile.adapter in kinds:
            report.ok(
                "adapter.kind",
                f"{profile.name}: adapter kind {profile.adapter!r} registered",
                {"profile": profile.name, "adapter": profile.adapter},
            )
        else:
            report.fail(
                "adapter.kind",
                f"{profile.name}: unknown adapter kind {profile.adapter!r} — "
                f"known: {', '.join(kinds)} (install the plugin that provides it, "
                f"or fix the profile's `adapter`)",
                {"profile": profile.name, "adapter": profile.adapter, "known": kinds},
            )
    for ep_name, reason in sorted(adapter_registry.external_adapter_errors().items()):
        report.warn(
            "adapter.external",
            f"external adapter '{ep_name}' failed to load: {reason}",
            {"entry_point": ep_name, "error": reason},
        )
    for ep_name, reason in sorted(external_profile_errors().items()):
        report.warn(
            "adapter.external-profile",
            f"external profile '{ep_name}' failed to load: {reason}",
            {"entry_point": ep_name, "error": reason},
        )

    # opencode config-file model ids are "provider/model" (see the opencode_http docstring);
    # a bare model name silently falls back to the server's default model, so warn
    # (advisory — a note, not a FAIL: an empty model legitimately means "default").
    #
    # Keyed on the adapter KIND, for the same reason `adapter.httpx` above is: the
    # "provider/model" spelling is a fact about the opencode server's config file,
    # not about whether a profile registers hooks. Those were one question only
    # while `hookless` selected the builder; the registry decoupled them, and
    # keying on `hookless` is now wrong in BOTH directions — it warns an
    # out-of-tree hookless family whose server takes bare model names, naming a
    # convention that family does not use, and it stays silent for an
    # `opencode-http` profile carrying a hook dialect, which is exactly where the
    # bare name really does fall back to the server default.
    if pol is not None:
        for role in ROLES:
            cfg = pol.adapter.resolved(role)
            prof = profile_by_name.get(cfg.name)
            if (
                prof is not None
                and prof.adapter == adapter_registry.OPENCODE_HTTP
                and cfg.model
                and "/" not in cfg.model
            ):
                report.warn(
                    "policy.model-qualified",
                    f"{role} model {cfg.model!r} is not 'provider/model' — "
                    f"{prof.name} expects e.g. 'anthropic/claude-haiku-4-5'",
                    {"role": role, "model": cfg.model, "profile": prof.name},
                )

    base_findings = install.missing_base_skills(project, dev_trees)
    # gated on PROBLEMS, not on any finding: an advisory review layer (a `when`
    # gate, a phrasing this check can't confirm, a broken override) is a warning
    # that must ride alongside the ok line rather than suppress it.
    #
    # Gate on `dev_trees`, not `profiles`: policy never validates an adapter name
    # (the first test is `get_profile`), so `[adapter] name = "nosuchcli"` beside a
    # loadable `[adapter.triage]` leaves `profiles` non-empty while nothing dev-side
    # resolved — and the ok line would then be a green sentence assembled from an
    # empty probe. Can only tighten: `dev_trees` truthy implies `profiles` truthy.
    if dev_trees and not any(f.severity == "problem" for f in base_findings):
        # Name the primitive that actually resolved, not a hardcoded era: on an
        # upgraded project this is the operator's confirmation that the rename was
        # picked up (and, across trees, that both picked up the same one).
        resolved = list(
            dict.fromkeys(
                name
                for tree in dict.fromkeys(dev_trees)
                if (name := install.resolve_dev_primitive(project, tree)) is not None
            )
        )
        report.ok(
            "skills.base",
            f"upstream skills present ({' + '.join(resolved)} + review layers)",
            {"trees": list(dict.fromkeys(dev_trees)), "dev_primitive": resolved},
        )
    report.extend(base_findings)
    report.extend(install.dev_primitive_warnings(project, dev_trees))

    if getattr(args, "json", False):
        # getattr, not args.json: cmd_validate is called directly by tests (and by
        # anything holding a hand-built Namespace) that predate the flag.
        machine.emit(validate_document(report, stories_on, spec_folder))
    else:
        report.render()
    return 0 if report.passed else 1


def cmd_mux(args: argparse.Namespace) -> int:
    """List registered terminal-multiplexer backends and the selection, or
    persist a machine-scoped choice (`mux set <name>` / `mux set --clear`) into
    .bmad-loop/policy.toml. Never prompts — runs are unattended."""
    from .adapters.multiplexer import (
        MultiplexerError,
        detect_multiplexers,
        external_backend_errors,
        get_multiplexer,
    )

    project = _project(args)
    if args.action == "set":
        return _mux_set(project, args)
    if args.clear or args.force:
        print("error: --clear/--force apply to `bmad-loop mux set`", file=sys.stderr)
        return 1

    rows = detect_multiplexers()
    header = ("NAME", "PLATFORM", "AVAILABLE", "VERSION", "SELECTED")
    table = [
        (
            r.name,
            "yes" if r.matches_platform else "no",
            "yes" if r.available else "no",
            # detect_multiplexers folds multi-line versions (fold_version), so
            # an out-of-tree backend can't split the row and strand SELECTED.
            r.version or "-",
            f"* {_mux_reason_label(r.reason)}" if r.selected else "",
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table), 0) for i, h in enumerate(header)]
    for row in (header, *table):
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    # AVAILABLE answers "could this backend run here", not "could it be picked",
    # and the two diverge whenever a foreign-platform binary shares a name with
    # a local one — on Windows `tmux` is psmux's compatibility shim, so the tmux
    # row reads as a real tmux install. Explaining the row beats gating the
    # column: a forced choice genuinely does reach these backends.
    # A forced backend is exempt: it IS selected, and telling an operator the
    # row they just pinned cannot be selected reads as a contradiction of the
    # `*` marker one line above.
    stranded = [r.name for r in rows if r.available and not r.matches_platform and not r.selected]
    if stranded:
        print(
            "note: AVAILABLE means the binary answers here, not that the backend supports "
            f"{sys.platform} — {', '.join(stranded)} can only be reached by forcing the choice"
        )
    # A VERSION of `-` is the same cell whether the binary reports no version or
    # crashed answering (#428). The row can't carry the difference — a table cell
    # holds no stderr — so the dropped diagnostic is named here, beside the other
    # reason a backend looks absent for no visible cause.
    for r in rows:
        if r.version_error:
            # Whitespace-collapsed: the text carries the probe's own stderr, which
            # is routinely multi-line, and a warning that spans lines reads as
            # several unrelated ones. Not length-bounded like a table cell (#321)
            # — nothing here sizes a column, and the diagnostic IS the payload.
            detail = " ".join(r.version_error.split())
            print(f"warning: {r.name} version probe failed: {detail}", file=sys.stderr)
    # A failed external package is invisible in the table (it never registered),
    # so name it here — the one place an operator looks when a backend is missing.
    for ep_name, reason in sorted(external_backend_errors().items()):
        print(f"warning: external backend '{ep_name}' failed to load: {reason}", file=sys.stderr)
    print(
        "override: BMAD_LOOP_MUX_BACKEND env var, or `bmad-loop mux set <name>` "
        f"(persists to {POLICY_FILE})"
    )
    try:
        backend = get_multiplexer()
    except MultiplexerError as e:
        # A forced unknown name (env or policy): the listing above still helps.
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    chosen = next((r for r in rows if r.selected), None)
    reason = _mux_reason_label(chosen.reason) if chosen else "fallback"
    # no selected row means _select bottomed out at its documented historical
    # fallback, which is tmux by contract — not a stale hardcoding
    name = chosen.name if chosen else "tmux"
    print(f"selection: {name} ({type(backend).__name__}) — {reason}")
    return 0


def _mux_set(project: Path, args: argparse.Namespace) -> int:
    from .adapters.multiplexer import detect_multiplexers

    path = _policy_path(project)
    if args.clear:
        if args.name:
            print("error: `mux set --clear` takes no backend name", file=sys.stderr)
            return 1
        policy_mod.write_mux_backend(path, None)
        print(f"mux backend cleared (auto-select) in {path}")
        return 0
    if not args.name:
        print(
            "error: `mux set` requires a backend name (run `bmad-loop mux` to list), "
            "or `mux set --clear` to return to auto-select",
            file=sys.stderr,
        )
        return 1
    rows = {r.name: r for r in detect_multiplexers()}
    row = rows.get(args.name)
    if row is None and not args.force:
        known = ", ".join(sorted(rows)) or "(none registered)"
        print(
            f"error: {args.name!r} is not a registered backend; known: {known}. "
            "A plugin backend that only registers on the target machine can be "
            "persisted with --force.",
            file=sys.stderr,
        )
        return 1
    if row is not None and not row.available:
        # Deliberate choice = trusted (same doctrine as the env override), but
        # say so: a pinned backend bypasses the availability gate at launch
        # (with a warning there too — see multiplexer.mux_usable), so a
        # version-gated binary would otherwise run into its gated defect.
        print(
            f"warning: backend {args.name!r} is not available on this host (transport "
            "binary missing, version unsupported, or a required helper like `pwsh` "
            "absent); persisted anyway — launches will proceed with a warning, and "
            "`bmad-loop validate` will report it",
            file=sys.stderr,
        )
    if envvars.mux_backend():
        print(
            "note: BMAD_LOOP_MUX_BACKEND is set in this shell and outranks the persisted choice",
            file=sys.stderr,
        )
    policy_mod.write_mux_backend(path, args.name)  # a junk name raises PolicyError → main()
    print(f'mux backend set to "{args.name}" in {path}')
    return 0


def _skill_trees(project: Path, pol) -> list[str]:
    """The skill trees this run's dev-primitive adapters read, one per distinct name.

    Shared by the real preflight and `cmd_validate` so the two cannot drift:
    validate's verdict has to key on exactly what makes run abort. Profiles that
    fail to load are skipped rather than raising — an unknown adapter name is the
    policy loader's problem, not the skill probe's.

    Scoped to :data:`install.DEV_PRIMITIVE_ROLES`, not :data:`ROLES`: every skill
    these trees are asked about is one only a dev or review session dispatches, and
    triage's whole prompt surface ships in this wheel. It is also the set
    `WorktreeFlow.worktree_profiles` provisions, so what is gated and what is carried
    into a worktree stay one decision.

    Deliberately does NOT consult `review.enabled`. Disabling review does not retire
    the review ADAPTER: a plugin workflow may declare `role = "review"` and dispatch
    on `adapters["review"]` with review disabled, and `worktree_profiles` drives
    per-CLI Stop-signal hook registration as well as seeding — a worktree
    provisioned without the review profile has no completion signal for those
    sessions and stalls rather than merely missing a skill. See #424 for the narrow
    residue that IS real."""
    from .adapters.profile import ProfileError, get_profile

    trees = []
    for name in dict.fromkeys(
        pol.adapter.resolved(role).name for role in install.DEV_PRIMITIVE_ROLES
    ):
        try:
            trees.append(get_profile(name, project).skill_tree)
        except ProfileError:
            continue
    return trees


def _dev_skill_for_role(pol, project: Path, role: str) -> str:
    """The dev-primitive skill name ``role``'s adapter would invoke, for dry-run
    previews. Mirrors `_require_base_skills`' profile→skill_tree lookup so the
    preview and the real dispatch (``Engine._dev_skill``) resolve identically —
    a pre-rename project previews ``/bmad-dev-auto``, a post-rename one
    ``/bmad-build-auto``. An unloadable profile falls back to the legacy name;
    the run itself would fail preflight before ever dispatching."""
    from .adapters.profile import ProfileError, get_profile

    try:
        tree = get_profile(pol.adapter.resolved(role).name, project).skill_tree
    except ProfileError:
        tree = None
    return install.dev_primitive_or_default(project, tree)


def _unknown_adapter_kinds(project: Path, pol) -> list[str]:
    """Problem lines for each role-selected profile whose ``adapter`` names no
    registered kind — resolved against the live registry, never a hardcoded set.

    Such a profile renders a perfectly plausible dry-run preview (``_render_invocation``
    reads only ``binary``/``launch_args``/``prompt_template``) but aborts the real
    run in ``make_adapters``, which is precisely the gap the banner exists to close.
    Only the roles a run would actually build are checked; `validate` reports the
    same condition over *every* profile as an ``adapter.kind`` finding.

    A profile that will not even parse is skipped rather than reported here: the
    renderer immediately after raises the ``ProfileError`` itself, which names the
    real problem better than a derived one would."""
    from .adapters.profile import ProfileError, get_profile
    from .adapters.registry import known_adapter_kinds

    kinds = known_adapter_kinds()
    problems: list[str] = []
    for name in dict.fromkeys(pol.adapter.resolved(role).name for role in ROLES):
        try:
            profile = get_profile(name, project)
        except ProfileError:
            continue
        if profile.adapter not in kinds:
            problems.append(
                f"profile {profile.name!r} names unknown adapter kind "
                f"{profile.adapter!r} — known: {', '.join(kinds)} "
                f"(install the plugin that provides it, or fix the profile's `adapter`)"
            )
    return problems


def _warn_preflight_would_abort(
    paths: bmadconfig.ProjectPaths, pol, *, require_stories: bool = False
) -> None:
    """Dry-run honesty banner: say so when the real command would refuse to run.

    ``--dry-run`` returns before `_require_base_skills` (cmd_run/cmd_sweep), so a
    project whose skills are broken still gets a plausible-looking preview. Since
    the upstream rename that preview is actively misleading rather than merely
    incomplete: the forwarding shim IS a valid slash command, so a previewed
    ``/bmad-dev-auto`` reads fine and would HALT an unattended session on the
    shim's interactive migration gate.

    Mirrors the refusals the dry-run's early return skips past, and only those:
    the finding list `_require_base_skills` gates on, the #414 isolation conflict
    `_reject_isolation_conflict` refuses ahead of it, and the unregistered adapter
    kind `make_adapters` aborts on (`_unknown_adapter_kinds` — a preview reads none
    of the fields that would give the misconfiguration away). Reading the same
    sources as the gates themselves is what keeps the preview from disagreeing with
    the real command about what "runnable" means. Severity-filtered to `problem`
    for that same reason — `_require_base_skills` ignores warnings, so reporting one
    as a `FAIL:` line would promise an abort that never comes. The dirty-tree, queue
    and run-id gates are deliberately not part of this banner.

    Takes the whole :class:`~bmadconfig.ProjectPaths` rather than `project` alone
    because the #414 refusal is a fact about the two roots' relationship; every
    other probe here still reads `paths.project`, which is what `init` wrote and
    what a session's own root resolution will re-derive.

    The exit code deliberately stays 0. A dry-run is a diagnostic — refusing to
    print the schedule would withhold the very thing the operator asked for, and
    every existing caller reads rc 0 as "the preview rendered", not as "the
    project is ready". The banner goes to stderr so stdout stays the preview."""
    trees = _skill_trees(paths.project, pol)
    problems = [
        p.message
        for p in install.missing_base_skills(paths.project, trees)
        + (install.missing_stories_support(paths.project, trees) if require_stories else [])
        if p.severity == "problem"
    ]
    conflict = bmadconfig.worktree_isolation_conflict(paths, pol.scm.isolation)
    if conflict is not None:
        problems.insert(0, conflict)
    problems += _unknown_adapter_kinds(paths.project, pol)
    if not problems:
        return
    print(
        "note: this preview is NOT runnable as-is — the real command aborts at preflight:",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  FAIL: {problem}", file=sys.stderr)
    print("run `bmad-loop validate` for details", file=sys.stderr)


def cmd_adapters(args: argparse.Namespace) -> int:
    """List the registered coding-CLI adapter kinds (the CLI axis's counterpart to
    `bmad-loop mux` for the transport axis) and name any out-of-tree adapter or
    profile package that failed to load. Unlike `mux`, there is no global choice
    to persist: an adapter kind is selected per profile by its `adapter` field."""
    from .adapters.profile import ProfileError, external_profile_errors, load_profiles
    from .adapters.registry import detect_adapters, external_adapter_errors

    # Loading profiles also triggers the bmad_loop.profiles entry-point scan, so a
    # broken profile package surfaces below alongside a broken adapter package.
    # A malformed project overlay is the operator's own file and aborts — this is a
    # listing command, and printing a table assembled from a profile set that
    # silently lost an entry is worse than saying which file is wrong.
    try:
        profiles = load_profiles(_project(args))
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    by_kind: dict[str, list[str]] = {}
    for prof in profiles.values():
        by_kind.setdefault(prof.adapter, []).append(prof.name)

    rows = detect_adapters()
    header = ("NAME", "ORIGIN", "NEEDS MUX", "PROFILES")
    table = [
        (
            r.name,
            "builtin" if r.builtin else "external",
            "yes" if r.needs_mux else "no",
            ", ".join(sorted(by_kind.get(r.name, []))) or "-",
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table), 0) for i, h in enumerate(header)]
    for row in (header, *table):
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    # A profile whose adapter kind never registered (a typo, or an uninstalled
    # plugin) is invisible in the table above — name it so an operator can see the
    # dangling reference, exactly as `mux` names a failed backend package.
    known = {r.name for r in rows}
    for kind in sorted(set(by_kind) - known):
        print(
            f"warning: profile(s) {', '.join(sorted(by_kind[kind]))} reference unknown "
            f"adapter kind '{kind}' (no registered kind or plugin provides it)",
            file=sys.stderr,
        )
    for ep_name, reason in sorted(external_adapter_errors().items()):
        print(f"warning: external adapter '{ep_name}' failed to load: {reason}", file=sys.stderr)
    for ep_name, reason in sorted(external_profile_errors().items()):
        print(f"warning: external profile '{ep_name}' failed to load: {reason}", file=sys.stderr)
    print(
        "adapter kind is selected per profile by its `adapter` field "
        "(default: generic); an out-of-tree package registers new kinds via the "
        "bmad_loop.adapters + bmad_loop.profiles entry points"
    )
    return 0


def _require_base_skills(project: Path, pol, *, require_stories: bool = False) -> bool:
    """Preflight the upstream skills the orchestrator drives (the dev primitive —
    bmad-build-auto, or a complete pre-rename bmad-dev-auto — plus the review layers
    it invokes inline).

    Returns True when everything is in place; otherwise prints the problems and
    returns False so the caller can abort before spawning any session (a missing
    skill would otherwise stall as an `Unknown command` until the run times out).
    Warnings are printed but never abort — only ``problem`` findings block.
    A post-rename install left with only the forwarding shim fails here too: the
    shim's interactive migration gate would HALT the session with nothing written.

    ``require_stories`` additionally content-probes the resolved primitive for
    folder+id dispatch — stories mode needs a newer skill than sprint mode, so an
    older install must fail loudly here rather than HALT `no stories.yaml`-style at
    dispatch time."""
    skill_trees = _skill_trees(project, pol)
    findings = install.missing_base_skills(project, skill_trees)
    if require_stories:
        findings += install.missing_stories_support(project, skill_trees)
    # Severity decides. A review layer we can't statically resolve (a `when` gate,
    # an unrecognized handoff phrasing, an unparseable override) is reported and
    # then stepped over: aborting on it would be the false FAIL of #260, only now
    # on every run rather than only on validate.
    problems = [f for f in findings if f.severity == "problem"]
    for warning in (f for f in findings if f.severity != "problem"):
        print(f"warning: {warning.message}", file=sys.stderr)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem.message}", file=sys.stderr)
        print("run `bmad-loop validate` for details", file=sys.stderr)
        return False
    return True


def _stories_mode(args: argparse.Namespace, pol) -> tuple[bool, str]:
    """Resolve whether this run is stories mode and its spec folder.

    ``run --spec <folder>`` forces stories mode (overrides policy); otherwise the
    run follows ``[stories].source``. Returns ``(is_stories, spec_folder)`` — the
    folder is "" in sprint mode. ``pol`` may be None (e.g. a policy that failed to
    load in ``validate``): then only an explicit ``--spec`` can force stories mode."""
    spec = getattr(args, "spec", None)
    if spec:
        return True, spec
    if pol is not None and pol.stories.source == "stories":
        return True, pol.stories.spec_folder
    return False, ""


def _validate_stories_folder(
    paths: bmadconfig.ProjectPaths, spec_folder: str, *, selector: str | None = None
) -> str | None:
    """Preflight the stories-mode inputs: stories.yaml parses + rules pass, SPEC.md
    (the epic spec every first dispatch loads) exists, and — when a ``--story``
    ``selector`` is given — the id is actually in the manifest. Returns a problem
    string to print, or None when OK. Catching an unknown ``--story`` here fails the
    run before it starts, instead of crashing it mid-flight in the scheduler."""
    folder = stories_mod.resolve_spec_folder(paths.project, spec_folder)
    try:
        story_set = stories_mod.load_stories(folder)
    except stories_mod.StoriesError as e:
        return f"stories mode: {e} (spec folder: {folder})"
    if not story_set.entries:
        return f"stories mode: stories.yaml has no entries: {folder}"
    if not (folder / "SPEC.md").is_file():
        return (
            f"stories mode: {folder}/SPEC.md not found — a first dispatch loads the "
            f"epic spec (the skill would HALT `no epic spec found`)"
        )
    if selector is not None and story_set.get(selector) is None:
        return (
            f"stories mode: --story id {selector!r} is not in stories.yaml — "
            f"pick one of: {', '.join(e.id for e in story_set.entries)}"
        )
    return None


def _validate_stories_queue(
    project: Path,
    paths: bmadconfig.ProjectPaths,
    spec_folder: str,
    skill_trees: list[str],
    report: ValidationReport,
) -> None:
    """Stories-mode counterpart of ``cmd_validate``'s sprint-status gate: validate
    the ``stories.yaml`` manifest + ``SPEC.md`` and confirm the installed
    dev primitive carries the folder+id dispatch flow stories mode needs (an older
    skill would HALT at dispatch). Appends findings to ``report`` in place; the
    probe carries its own remediation text ("update the bmm module")."""
    folder = stories_mod.resolve_spec_folder(paths.project, spec_folder)
    problem = _validate_stories_folder(paths, spec_folder)
    if problem:
        report.fail("queue.stories-manifest", problem, {"spec_folder": str(folder)})
    else:
        try:
            count = len(stories_mod.load_stories(folder).entries)
            report.ok(
                "queue.stories-manifest",
                f"stories mode OK: {count} stories in {folder}/stories.yaml, SPEC.md present",
                {"spec_folder": str(folder), "stories": count},
            )
        except stories_mod.StoriesError as e:  # already validated above — defensive
            report.fail(
                "queue.stories-manifest",
                f"stories mode: {e} (spec folder: {folder})",
                {"spec_folder": str(folder)},
            )
    stories_probs = install.missing_stories_support(project, skill_trees)
    if skill_trees and not stories_probs:
        # The total form, not `resolve_dev_primitive`: this ok line only renders when
        # the probe passed, and the probe ran against exactly this name — so an
        # unresolvable tree that somehow satisfied it is still reported as what was
        # actually read.
        probed = list(
            dict.fromkeys(
                install.dev_primitive_or_default(project, tree)
                for tree in dict.fromkeys(skill_trees)
            )
        )
        report.ok(
            "skills.stories-dispatch",
            f"{' + '.join(probed)} supports folder+id dispatch (stories mode)",
            {"trees": list(dict.fromkeys(skill_trees)), "dev_primitive": probed},
        )
    report.extend(stories_probs)


def _spec_closes_deferred(path: Path) -> tuple[tuple[str, ...], str | None]:
    """One story spec's ``closes_deferred:`` declaration, degrading an unreadable
    spec to an empty one — other gates own spec readability, and an advisory
    check must never be the thing that crashes the preflight it is advising."""
    try:
        raw = frontmatter.read_frontmatter(path).get("closes_deferred")
    except (OSError, UnicodeDecodeError):
        return (), None
    return deferredwork.parse_declaration(raw)


def _validate_operator_registry(
    project: Path, paths: bmadconfig.ProjectPaths, report: ValidationReport
) -> None:
    """Report drift between the park records and the committed state they point
    at (#335, #356).

    A record is committed beside the truth it points at, but it can still
    disagree with it — a story re-driven to done, a reverted commit, a
    hand-edited spec, a record file mangled in a merge. That is only a safe
    trade if something says so out loud.

    Never a failure, always a warning, in every direction. `confirm` already
    refuses to act on a drifted entry, so nothing here gates anything; a stale
    record must not be able to block a run that would otherwise start. The
    directions carry different ids because their remedies differ: an entry the
    committed state has moved past is stale bookkeeping to discard
    (`registry-stale`), while a board sitting at `awaiting-operator` that no
    record claims (`park-record-missing`) is an obligation whose spec nothing
    can find. Before #356 that second arm was the fresh-clone NORM and shared
    `registry-stale`; now the record travels with the park's own commit, so a
    missing one is always evidence of something — a record write that failed at
    park time, a park from a version that recorded only machine-locally, a
    checkout without the park's branch, or a record deleted without confirming.

    An INTERRUPTED confirmation gets its own id rather than being reported as
    stale, because its remedy inverts: re-running `confirm` finishes it. Saying
    "the entry is stale and confirm will refuse it" about a story confirm now
    completes would be the same class of lie the state itself is."""
    parked = operatoractions.resolve(project, paths)
    for story in parked:
        if story.spec_status == operatoractions.AWAITING_OPERATOR and not story.actions:
            report.warn(
                "operator.actions-malformed",
                f"{story.story_key} is parked at awaiting-operator but its "
                f"operator_actions: declares nothing readable — `bmad-loop confirm "
                f"{story.story_key}` has no actions to acknowledge; repair the list in "
                f"{story.spec_path}",
                {"story_key": story.story_key, "spec": str(story.spec_path)},
            )
            # NO `continue`: the malformed list is about the INDEX side, and a
            # co-occurring disagreement on the committed side is a separate
            # finding with a separate remedy (repair the list vs discard the
            # entry). `committed_drift()` is the half that answers about spec and
            # board only, so the empty list is not re-reported as the drift —
            # `drift()` would collapse both into whichever it found first. The
            # sibling `_validate_closes_deferred` has the same shape and reports
            # every cause it finds, for the same reason.
            drift = story.committed_drift()
        elif story.resumable:
            # Not `registry-stale`: to `drift()` this looks stale ("its spec now
            # says status: done"), but the remedy INVERTS — re-run confirm to
            # finish it, rather than discard the entry — and `checks` splits ids
            # exactly where the remedy differs.
            report.warn(
                "operator.confirm-interrupted",
                f"{story.story_key} was confirmed but the board was never advanced: its "
                f"spec is signed off and reads done while the park entry still lists it. "
                f"`bmad-loop confirm {story.story_key}` finishes it without asking you to "
                f"acknowledge anything again",
                {"story_key": story.story_key, "board_status": story.board_status},
            )
            continue
        else:
            drift = story.drift()
        if drift is not None:
            report.warn(
                "operator.registry-stale",
                f"the park entry lists {story.story_key} as parked, but "
                f"{drift} — the entry is stale and `bmad-loop confirm "
                f"{story.story_key}` will refuse it",
                {"story_key": story.story_key, "drift": drift},
            )
    try:
        ss = sprintstatus.load(paths.sprint_status)
    except (sprintstatus.SprintStatusError, OSError, UnicodeDecodeError):
        return  # the queue gate below owns board readability — don't double-report
    indexed = {story.story_key for story in parked}
    orphans = sorted(
        s.key
        for s in ss.stories
        if s.status == operatoractions.AWAITING_OPERATOR and s.key not in indexed
    )
    if orphans:
        report.warn(
            "operator.park-record-missing",
            f"the board parks {', '.join(orphans)} at awaiting-operator, but no park "
            f"record under {operatoractions.RECORDS_REL.as_posix()}/ claims them, so "
            f"`bmad-loop confirm` cannot find their specs from this checkout. The record "
            f"travels with the park's own commit — a missing one means the record write "
            f"failed at park time (journaled as operator-index-failed), the park predates "
            f"committed records (confirm it on the machine that ran it), this checkout "
            f"lacks the branch carrying the park commit (pull it), or the record was "
            f"deleted without confirming; otherwise finish the story by hand",
            {"story_keys": orphans},
        )


def _validate_deferred_ledger(
    paths: bmadconfig.ProjectPaths,
    report: ValidationReport,
    *,
    spec_folder: str | None = None,
) -> None:
    """Read the deferred-work ledger once, then run every check that needs it.

    The read sits here rather than inside each check because an unreadable ledger
    is one fault, not one per reader: two checks opening the same file would
    report the same outage twice under two ids.

    A ledger that cannot be read at all is reported. Staying quiet there is not
    the same trade as staying quiet about an unparseable manifest: the manifest is
    already reported by ``queue.stories-manifest``, while nothing else in
    ``validate`` reads the ledger, so silence meant reporting success for
    preflights that checked nothing.

    The gate runs first for one reason only: an operator who has both a blocked
    story and a stale traceability field should meet the refusal before the
    advisory. Presentation, nothing more — ``_validate_closes_deferred``'s early
    return leaves *that function*, so it could never have skipped a sibling call
    here, and swapping the two lines changes no severity and no exit code.
    """
    ledger = paths.deferred_work
    try:
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    except (OSError, UnicodeDecodeError) as e:
        # Split from the manifest read in the checks below, which is silent for a
        # good reason that does not apply here: nothing else in `validate` reads
        # the ledger, so returning quietly reported success for preflights that
        # checked nothing, against the very file the run's closure will fail on
        # (#284 round-5 review, finding 6).
        #
        # A problem rather than a warning, escalated from the severity this id
        # carried while the read served only `closes_deferred`. The hard gate now
        # rides on the same bytes, and a warning exits 0 having evaluated no gate
        # at all — a fail-open on the one deferred check that is a refusal, and one
        # that cannot be narrowed by asking whether the project uses gates, because
        # the file that would answer is the unreadable one. `Engine._loop` refuses
        # the same way for the same reason, so preflight and dispatch agree about
        # this file instead of `validate` reporting a run that then pauses at its
        # first story. Nothing is lost by failing early: the message's last clause
        # is literal — the run's own closure reads this file too.
        report.fail(
            "deferred.ledger-unreadable",
            f"{ledger} cannot be read ({e}) — neither closes_deferred declarations nor "
            "`gate:` hard gates were checked against it, so an open entry could be "
            "gating an actionable story unseen; the run's own closure will fail the same way",
            {"ledger": str(ledger), "error": str(e)},
        )
        return
    _validate_hard_gates(paths, text, report, spec_folder=spec_folder)
    _validate_closes_deferred(paths, text, report, spec_folder=spec_folder)


def _validate_hard_gates(
    paths: bmadconfig.ProjectPaths,
    text: str,
    report: ValidationReport,
    *,
    spec_folder: str | None = None,
) -> None:
    """FAIL when the queue would dispatch a story an unlanded ledger entry gates.

    The preflight half of a two-sided refusal: ``Engine._refuse_gated_story``
    enforces the same gate at dispatch, so a ``run`` that skipped ``validate``
    pauses instead of proceeding. This side exists to move the answer earlier —
    the operator learns before the run starts, and learns about *every* gated
    story on the queue rather than just the first one picked.

    The two must keep agreeing about what "unlanded" means (only an explicit
    ``done`` retires a gate) and about an unreadable ledger (both refuse); a
    ``validate`` that passed a run which then paused at its first story would
    teach operators to trust neither.

    A ledger entry could always *say* it blocked a story — ``HARD GATE: must land
    before 3-2`` in its reason — and saying it stopped nothing. ``run`` took the
    story off the board and drove it, and the gate was discovered afterwards, in
    the diff of work built on a leg nobody had wired. A ``gate:`` line makes the
    claim matchable and this check makes it a refusal.

    The only deferred check that is a gate rather than an advisory, and the
    severity is the whole point: the ``closes-*`` siblings describe traceability
    that is wrong, which must never block a run, while this one describes work
    that must not start, which is exactly what a non-zero exit is for.

    Silent on a ledger nobody has gated, so the zero-config output is unchanged.
    Once a gate exists the passing case reports itself — a gate that only ever
    speaks when it fires is indistinguishable, on the day it matters, from one
    nobody remembered to write.
    """
    declared = [(entry, deferredwork.gates(entry)) for entry in deferredwork.parse_ledger(text)]
    for entry, entry_gates in declared:
        if entry.done:
            continue  # a landed entry gates nothing; that is what closing it means
        _report_unstructured_gate(entry, entry_gates, report)
    # Keyed on enforceable tokens, not on `gate:` lines: a ledger whose only gate is
    # malformed enforced nothing, and an `ok` there would be the same false all-clear
    # the warning above exists to break. Closed entries still count, deliberately —
    # the passing case has to keep speaking after the gate lands, or `ok` and "nobody
    # ever wrote a gate" become the same silence. Reading the queue sits behind this
    # test so a project that gates nothing pays neither the walk nor its failure modes.
    if not any(entry_gates.tokens for _, entry_gates in declared):
        return
    story_keys = _actionable_story_keys(paths, spec_folder)
    if story_keys is None:
        # The queue could not be read, so nothing was compared. `queue.sprint-status`
        # and `queue.stories-manifest` already fail for it; adding an `ok` here would
        # say "no story is gated" about a queue this check never saw.
        return
    gating = [e.id for e, g in declared if not e.done and g.tokens]
    gated = False
    for entry, entry_gates in declared:
        # `done`, not `not open` — the tri-state is the whole point. An entry whose
        # status the format cannot read (`status: opne`, or no status line) is not
        # evidence the work landed, and skipping it let one typo disable the gate
        # *and* emit an `ok` naming the entry as clear. Only an explicit `done`
        # retires a gate; everything else holds until someone writes that word.
        if entry.done:
            continue
        for story_key in story_keys:
            hits = [t for t in entry_gates.tokens if deferredwork.gates_story(t, story_key)]
            if not hits:
                continue
            gated = True
            report.fail(
                "deferred.hard-gate",
                f"{entry.id} ({entry.title}) {_gate_status_clause(entry)} and gates "
                f"{story_key} (gate: {', '.join(hits)}) — that story must not run until "
                f"the entry lands. Close it in {paths.deferred_work.name} "
                f"(`status: done <date>`), or drop the token from its `gate:` line if it "
                f"no longer blocks this work",
                {
                    "dw_id": entry.id,
                    "title": entry.title,
                    "story_key": story_key,
                    "tokens": hits,
                },
            )
    if not gated:
        report.ok(
            "deferred.hard-gate",
            f"deferred-work gates OK: no actionable story is gated by an unlanded entry "
            f"({', '.join(gating) if gating else 'no unlanded gated entries'})",
            {"gating_ids": gating, "actionable": list(story_keys)},
        )


def _gate_status_clause(entry: deferredwork.DWEntry) -> str:
    """How the failure names *why* this entry still gates.

    An unreadable status is reported as what it is rather than folded into "is
    open": the remedy differs — the operator with a typo fixes the `status:` line,
    and telling them the entry "is open" sends them to close work that may already
    have landed. Naming the offending value is what makes a one-character typo
    findable in a ledger of fifty entries.
    """
    if entry.open:
        return "is open"
    if not entry.status:
        return "has no `status:` line, so it cannot be read as landed"
    return f"has an unreadable status (`{entry.status}`), so it cannot be read as landed"


def _report_unstructured_gate(
    entry: deferredwork.DWEntry,
    entry_gates: deferredwork.EntryGates,
    report: ValidationReport,
) -> None:
    """Warn about a hard gate the mechanical check cannot enforce.

    Three causes, one id, because the remedy is the same line in the same file.
    A ``HARD GATE:`` written as prose is the pre-``gate:`` convention still
    holding nothing back. A token that cannot name a story key, and a ``gate:``
    line with nothing after the colon, are that same nothing with the field's
    syntax around it — worse, because to anyone scanning the entry they read as a
    gate already in force.

    An entry carrying a valid token *and* an unenforceable one is still reported,
    and each cause is reported on its own rather than the first one winning: the
    valid half gates what it names, and the operator's belief about the other half
    is exactly the thing that goes wrong quietly. That applies to an empty line as
    much as to a malformed token — ``gate: 3-2`` followed by a bare ``gate:`` used
    to report neither, because the entry had tokens and so read as fully gated.

    A fourth cause, and the only one that is about a line the parser never saw: a
    ``gate:`` the strict field anchor misses (``Gate:``, or indented). It is a
    warning rather than an accepted gate on purpose — see :data:`_GATE_NEAR_RE`.

    Runs for every entry that is not ``done``, which is the same set the refusal
    holds against. Keying it on ``open`` instead would have left an entry with an
    unreadable status silent about a malformed token as well as about its gate.
    """
    reasons: list[str] = []
    if entry_gates.malformed:
        reasons.append(
            f"declares `gate:` tokens that cannot name a story: {', '.join(entry_gates.malformed)}"
        )
    if entry_gates.empty == 1:
        reasons.append("declares an empty `gate:` line, which names no story")
    elif entry_gates.empty:
        reasons.append(f"declares {entry_gates.empty} empty `gate:` lines, which name no story")
    if entry_gates.near_miss:
        reasons.append(
            f"spells {entry_gates.near_miss} `gate:` line(s) in a form the field does not "
            f"read (the field is a lowercase `gate:` at the very start of a line)"
        )
    prose_only = not entry_gates.tokens and not reasons
    if prose_only and deferredwork.declares_prose_gate(entry):
        reasons.append("declares a `HARD GATE:` in prose but carries no `gate:` line")
    if not reasons:
        return
    report.warn(
        "deferred.hard-gate-unstructured",
        f"{entry.id} ({entry.title}) {' and '.join(reasons)} — so `validate` cannot refuse "
        f"the gated story and nothing holds it back; name the blocked stories on a `gate:` "
        f"line (comma-separated) to make the gate enforceable",
        {
            "dw_id": entry.id,
            "malformed": list(entry_gates.malformed),
            "empty": entry_gates.empty,
            "near_miss": entry_gates.near_miss,
        },
    )


def _actionable_story_keys(
    paths: bmadconfig.ProjectPaths, spec_folder: str | None
) -> list[str] | None:
    """The story keys this queue could dispatch, in queue order, in either mode.

    ``None`` when the queue could not be read, which is not the same answer as an
    empty list: ``queue.sprint-status`` and ``queue.stories-manifest`` own queue
    readability, so this check stays quiet rather than raising — but a caller that
    read ``[]`` as "nothing is gated" would report an all-clear about a queue it
    never saw. The whole walk is inside the guard for the same reason: the
    per-story ``resolve_story_spec`` globs the filesystem too, and leaving it
    outside turned a degraded check into a traceback out of ``validate``.

    Stories mode has no status column — the manifest is a flat schedule and the
    story's own spec carries the status — so actionability comes from
    :func:`stories._classify`, the predicate the scheduler itself picks with.
    Dropping only ``done`` is not the same line the sprint board draws:
    ``ACTIONABLE_STATUSES`` is a two-element allowlist, so ``blocked`` and the
    rest are already out on that side. In stories mode a ``blocked``, sentinel,
    ambiguous or unknown-status entry is one :func:`stories.schedule` refuses to
    dispatch (``SCHEDULE_WEDGED``), so treating every non-``done`` state as
    actionable made ``validate`` exit nonzero over a gate on a story the queue
    could not run — and made the two queue modes disagree about what a gate
    refuses.

    What is shared is that per-entry predicate and deliberately NOT the scan's
    stop rule: ``schedule`` gives up at the FIRST wedged entry, and mirroring that
    here would drop every later story from this list. Two reasons not to. A wedge
    is a property of some *other* story, and ``run --story <id>`` scans that entry
    alone (``selector``), so a later story really is reachable while the wedge
    stands — while ``validate`` takes no story selector and so cannot know which
    run is coming. And stopping would let one blocked entry near the top of a
    manifest silence the gate check for everything below it, which is the failure
    this check exists to prevent, arriving by a quieter route than the one it
    fixed. Over-reporting a real gate on a story that needs an unrelated
    resolution first is the cheaper wrong answer, and dispatch still refuses
    independently.
    """
    if spec_folder is not None:
        keys: list[str] = []
        try:
            folder = stories_mod.resolve_spec_folder(paths.project, spec_folder)
            for entry in stories_mod.load_stories(folder).entries:
                state = stories_mod.resolve_story_spec(folder, entry.id)
                if stories_mod._classify(state) != "actionable":
                    continue
                keys.append(entry.id)
        except (OSError, UnicodeDecodeError, stories_mod.StoriesError):
            return None
        return keys
    try:
        ss = sprintstatus.load(paths.sprint_status)
    except (sprintstatus.SprintStatusError, OSError, UnicodeDecodeError):
        return None
    return [s.key for s in ss.stories if s.status in sprintstatus.ACTIONABLE_STATUSES]


def _validate_closes_deferred(
    paths: bmadconfig.ProjectPaths,
    text: str,
    report: ValidationReport,
    *,
    spec_folder: str | None = None,
) -> None:
    """Warn when a story declares ``closes_deferred:`` ids the deferred-work
    ledger does not carry, or declares them in a shape nothing can read (#234).

    At clean close the orchestrator flips each declared id to ``status: done
    <date>`` + a ``resolution:`` line. An id that names no entry — a typo, or an
    entry reworded/renumbered since the spec was written — annotates nothing, and
    the run says so only in the journal, where nobody looks until the retro that
    the annotation exists to spare. Saying it at preflight is the whole point:
    the declaration is fixable before the run, not after.

    Runs in **both** queue modes, because the declaration exists in both. With
    ``spec_folder`` (stories mode) each manifest entry is checked together with
    its id-resolved spec, unioned — the manifest half is what makes this genuinely
    a *pre*-flight, since those ids are readable before the story has ever been
    dispatched. Without it (sprint mode) the story specs already sitting in the
    artifacts dir are scanned instead; those are written by `create-story` ahead
    of the run, which is exactly while a typo is still cheap to fix. Reporting
    only in stories mode left sprint-mode operators with nothing but a journal
    line after the fact.

    An id present but already ``done`` is a declaration a prior run already
    satisfied (a resume re-drives the same close), so it stays silent — the same
    classification the engine's close hook makes, for the same reason. Everything
    else it can journal is reported here: an absent id, an id whose ledger entry
    carries neither an ``open`` nor a ``done`` status (nothing will be marked, and
    the remedy is in the ledger rather than in the declaration), and a
    wrong-container declaration, which names real intent that would otherwise
    close nothing and say nothing. Covering only two of the three left the third
    to be discovered in the journal after the run it should have preceded.

    Never a failure. The annotation is traceability, not a gate, so a stale
    reference must not be able to block a run that would otherwise start.
    ``text`` is the ledger snapshot :func:`_validate_deferred_ledger` already
    read; an unreadable ledger never reaches here.
    """
    ledger = paths.deferred_work
    try:
        sources = (
            _stories_declarations(paths, spec_folder)
            if spec_folder is not None
            else _sprint_declarations(paths)
        )
    except (OSError, UnicodeDecodeError, stories_mod.StoriesError):
        # An unparseable manifest is already a `queue.stories-manifest` failure from
        # the gate above — don't double-report it.
        return
    for label, ids, error in sources:
        if error:
            report.warn(
                "deferred.closes-malformed",
                f"{label} declares closes_deferred in a shape that cannot be read: "
                f"{error} — nothing will be marked resolved for it",
                {"source": label, "error": error},
            )
        declared = deferredwork.classify(text, ids)
        if declared.malformed:
            malformed = list(declared.malformed)
            report.warn(
                "deferred.closes-entry-unreadable",
                f"{label} declares closes_deferred ids whose {ledger.name} entries carry "
                f"neither an `open` nor a `done` status: {', '.join(malformed)} — nothing "
                "will be marked resolved for them until the entry status is repaired",
                {"source": label, "dw_ids": malformed},
            )
        if declared.unknown:
            unknown = list(declared.unknown)
            report.warn(
                "deferred.closes-unknown",
                f"{label} declares closes_deferred ids that are not in "
                f"{ledger.name}: {', '.join(unknown)} — nothing will be marked "
                f"resolved for them (typo, or a renumbered/reworded entry?)",
                {"source": label, "unknown_ids": unknown},
            )


def _stories_declarations(
    paths: bmadconfig.ProjectPaths, spec_folder: str
) -> list[tuple[str, tuple[str, ...], str | None]]:
    """Per manifest entry: the union of its ``stories.yaml`` ids and its
    id-resolved spec's, deduped across both channels."""
    folder = stories_mod.resolve_spec_folder(paths.project, spec_folder)
    out = []
    for entry in stories_mod.load_stories(folder).entries:
        ids = list(entry.closes_deferred)
        error = None
        state = stories_mod.resolve_story_spec(folder, entry.id)
        if state.kind == stories_mod.KIND_PRESENT and state.path is not None:
            spec_ids, error = _spec_closes_deferred(state.path)
            ids += spec_ids
        out.append((f"story {entry.id}", tuple(dict.fromkeys(ids)), error))
    return out


def _sprint_declarations(
    paths: bmadconfig.ProjectPaths,
) -> list[tuple[str, tuple[str, ...], str | None]]:
    """Sprint mode has no manifest, so the declarations that exist yet are the
    ones in story specs already on disk — the flat ``*.md`` layout the artifacts
    dir holds."""
    impl = paths.implementation_artifacts
    if not impl.is_dir():
        return []
    out = []
    for path in sorted(impl.glob("*.md")):
        if path == paths.deferred_work:
            continue
        ids, error = _spec_closes_deferred(path)
        if ids or error:
            out.append((f"spec {path.name}", ids, error))
    return out


def _warn_unknown_keys(ss: sprintstatus.SprintStatus) -> None:
    """Surface sprint-status keys the parser could not classify. Silently
    dropping one reads to the operator as "that story is done, or not mine to
    do" (issue #144) — so `run`/`--dry-run` say it out loud; the journal's
    ``sprint-status-unknown-keys`` event stays the durable record."""
    if ss.unknown_keys:
        print(
            f"warning: ignoring unparseable sprint-status keys: {', '.join(ss.unknown_keys)}",
            file=sys.stderr,
        )


def cmd_run(args: argparse.Namespace) -> int:
    if (rc := _reject_bad_run_id(args.run_id)) is not None:
        return rc
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = policy_mod.load(_policy_path(project))
    stories_on, spec_folder = _stories_mode(args, pol)

    if stories_on and args.epic is not None:
        # stories mode dispatches the manifest's single flat schedule; StoriesEngine
        # nulls epic_filter, so --epic has no effect. Warn rather than silently drop
        # it, so a caller who passed both (e.g. `run --spec ... --epic 3`) isn't
        # surprised by an unfiltered run. Use --story to scope to one id.
        print(
            "note: --epic is ignored in stories mode; use --story to filter to one id",
            file=sys.stderr,
        )

    if args.dry_run:
        return _dry_run(paths, pol, args, stories_on, spec_folder)

    # First of the configuration refusals (`_reject_bad_run_id` and the two loaders
    # above can abort earlier), and deliberately before the queue and worktree-clean
    # gates: this one says the configuration cannot run at all, so making the
    # operator clear a dirty tree or fix a story key first would only delay the
    # same abort.
    if (rc := _reject_isolation_conflict(paths, pol)) is not None:
        return rc

    if stories_on:
        problem = _validate_stories_folder(paths, spec_folder, selector=args.story)
        if problem:
            print(problem, file=sys.stderr)
            return 1
    else:
        try:
            ss = sprintstatus.load(paths.sprint_status)
            _warn_unknown_keys(ss)
            sprintstatus.select_actionable(ss, args.epic, args.story)
        except sprintstatus.SprintStatusError as e:
            print(e, file=sys.stderr)
            return 1

    if not verify.worktree_clean(paths.repo_root):
        print("git worktree is not clean — commit or stash first", file=sys.stderr)
        return 1

    if not _require_base_skills(project, pol, require_stories=stories_on):
        return 1

    _reconcile_stale(project, paths, pol)

    # The launch baseline for the auto-sweep child's config re-read (#461 point 4).
    # Taken ONCE, from the same `pol` the engine is about to freeze, and used for
    # both the factory's gate and the RunState stamp so the two cannot disagree.
    #
    # `profiles` is resolved once here for the same reason and carried into
    # compose_run: the digest and `make_adapters` each used to read
    # profiles/*.toml separately, so the stamped baseline could describe bytes
    # this run never launched — and every later auto-sweep is held to that
    # baseline. See `_launch_profiles` for why this is an over-refusal fix and
    # not the child gate's check-then-use.
    profiles = _launch_profiles(pol, project)
    trusted_digest = _trusted_config_digest(pol, project, profiles=profiles)

    # The composition (run dir + state + pid + adapters + engine) lives in
    # runsetup; cmd_run stays parse -> compose -> render. Engine/StoriesEngine and
    # _make_adapters are handed in from this module's namespace so the test suite's
    # `monkeypatch.setattr(cli, "Engine"/"_make_adapters", ...)` still applies.
    composed = runsetup.compose_run(
        project=project,
        paths=paths,
        policy=pol,
        run_id=args.run_id,
        epic_filter=args.epic,
        story_filter=args.story,
        max_stories=args.max_stories,
        stories_on=stories_on,
        spec_folder=spec_folder,
        sweep_factory=_sweep_factory(project, paths, trusted_digest),
        make_adapters=_make_adapters,
        engine_cls=Engine,
        stories_engine_cls=StoriesEngine,
        trusted_config_digest=trusted_digest,
        profiles=profiles,
    )
    print(f"run {composed.run_id} starting (attach: bmad-loop attach)")
    summary = composed.engine.run()
    print(summary.render())
    return 0


def _render_invocation(pol, project: Path, role: str, prompt: str) -> str:
    from .adapters.profile import get_profile

    cfg = pol.adapter.resolved(role)
    profile = get_profile(cfg.name, project)
    if profile.hookless:
        # HTTP/SSE transport — there is no shell invocation to print. Render
        # the real sequence (per-session server spawn + API prompt) instead of
        # a fake argv that run would never execute.
        model = f" model={cfg.model}" if cfg.model else ""
        return (
            f"{profile.binary} serve --hostname 127.0.0.1 --port <auto> "
            f'(cwd=<worktree>) → POST /session → prompt_async "{profile.render_prompt(prompt)}"'
            f"{model}"
        )
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


def _events_dir_preview(project: Path) -> str | None:
    """``BMAD_LOOP_EVENTS_DIR`` as a real run would set it, with a ``<run-id>``
    placeholder standing in for the id no dry run has (a preview creates nothing,
    so the placeholder never reaches a filesystem). Printed once per preview
    rather than on every story's ``env:`` line: only the run id varies, and the
    path is long.

    ``None`` — plus the state root's own error on stderr — when no state root can
    be resolved. A real run resolves the same path while building its adapters, so
    silently dropping the line would turn a preview into a promise the run cannot
    keep."""
    try:
        return str(runs.events_dir_for(project, "<run-id>"))
    except runs.StateRootError as e:
        print(f"warning: {e}", file=sys.stderr)
        return None


def _dry_run(
    paths: bmadconfig.ProjectPaths,
    pol,
    args: argparse.Namespace,
    stories_on: bool = False,
    spec_folder: str = "",
) -> int:
    if stories_on:
        return _dry_run_stories(paths, pol, args, spec_folder)

    _warn_preflight_would_abort(paths, pol)

    def render(role: str, prompt: str) -> str:
        return _render_invocation(pol, paths.project, role, prompt)

    ss = sprintstatus.load(paths.sprint_status)
    _warn_unknown_keys(ss)
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
    if (events_dir := _events_dir_preview(paths.project)) is not None:
        print(f"  env (every session): BMAD_LOOP_EVENTS_DIR={events_dir}")
    dev_skill = _dev_skill_for_role(pol, paths.project, "dev")
    review_skill = _dev_skill_for_role(pol, paths.project, "review")
    for story in queue:
        print(f"\n  {story.key} (epic {story.epic}, status {story.status})")
        print(f"    dev:    {render('dev', f'/{dev_skill} {story.key}')}")
        print(f"    review: {render('review', f'/{review_skill} <done spec from dev>')}")
        print(f"    env:    BMAD_LOOP_MODE=1 BMAD_LOOP_STORY_KEY={story.key}")
    return 0


def _checkpoint_badge(row: stories_mod.StoryRow) -> str:
    """`` [spec-checkpoint, done-checkpoint]`` for a story's HITL flags, or ``""``
    when it sets neither. Shared spelling for the dry-run schedule + status."""
    marks = []
    if row.spec_checkpoint:
        marks.append("spec-checkpoint")
    if row.done_checkpoint:
        marks.append("done-checkpoint")
    return f" [{', '.join(marks)}]" if marks else ""


def _dry_run_stories(
    paths: bmadconfig.ProjectPaths, pol, args: argparse.Namespace, spec_folder: str
) -> int:
    """Print the linear stories-mode schedule (list order, checkpoints, live
    on-disk state) — no topo waves, one story per line, spawns nothing."""
    _warn_preflight_would_abort(paths, pol, require_stories=True)
    folder = stories_mod.resolve_spec_folder(paths.project, spec_folder)
    # The real dispatch always uses the project-relative folder (the engine
    # relativizes it); render the identical string here so dry-run and run agree —
    # including the refusal, which is the one answer `run` would not survive. No
    # `(spec folder: ...)` suffix like the sibling below: the reason already names
    # the spec folder and the project root, and on this leg `folder` is only
    # `spec_folder` re-spelled (the raise is reachable from the absolute branch
    # alone), so the suffix would print the same path a third time.
    try:
        rel = stories_mod.relativize_spec_folder(paths.project, spec_folder)
    except stories_mod.StoriesError as e:
        print(f"stories mode: {e}", file=sys.stderr)
        return 1
    try:
        rows = stories_mod.story_rows(folder, selector=args.story, max_stories=args.max_stories)
    except stories_mod.StoriesError as e:
        print(f"stories mode: {e} (spec folder: {folder})", file=sys.stderr)
        return 1
    if args.story and not rows:
        print(f"stories mode: story id {args.story!r} not found in stories.yaml", file=sys.stderr)
        return 1
    spec_ok = "" if (folder / "SPEC.md").is_file() else "  [!] SPEC.md missing"
    print(
        f"stories mode: {len(rows)} stories from {folder}/stories.yaml "
        f"(gates={pol.gates.mode}){spec_ok}"
    )
    if (events_dir := _events_dir_preview(paths.project)) is not None:
        print(f"  env (every session): BMAD_LOOP_EVENTS_DIR={events_dir}")
    print("linear schedule (list order — no depends_on, strictly serial):")
    dev_skill = _dev_skill_for_role(pol, paths.project, "dev")
    for row in rows:
        print(f"\n  {row.position}. {row.id}  ({row.label}){_checkpoint_badge(row)}  {row.title}")
        # A spec_checkpoint story whose plan is not yet on disk dispatches leg 1
        # (Halt after planning + BMAD_LOOP_PLAN_HALT); mirror the real dispatch's
        # markers so dry-run does not under-report what run would emit.
        plan_halt = stories_mod.is_plan_halt_leg(row.spec_checkpoint, row.state)
        dispatch = f"/{dev_skill} Spec folder: {rel}. Story id: {row.id}."
        if plan_halt:
            dispatch += " Halt after planning."
        print(f"    dev:    {_render_invocation(pol, paths.project, 'dev', dispatch)}")
        env = f"BMAD_LOOP_MODE=1 BMAD_LOOP_STORY_KEY={row.id} BMAD_LOOP_SPEC_FOLDER={rel}"
        if plan_halt:
            env += " BMAD_LOOP_PLAN_HALT=1"
        print(f"    env:    {env}")
    return 0


def _print_stories_status(state: RunState, project: Path) -> None:
    """The stories-mode board for `status`: id, live on-disk state, checkpoint
    markers and title, read from the run's pinned spec folder. Mode-aware
    counterpart of the sprint-backlog line — the run stamped ``source`` and
    ``spec_folder`` at start, so no flag is needed to re-derive the mode."""
    folder = stories_mod.resolve_spec_folder(project, state.spec_folder)
    try:
        rows = stories_mod.story_rows(folder)
    except stories_mod.StoriesError as e:
        print(f"stories: {e} (spec folder: {folder})")
        return
    done = sum(1 for r in rows if r.label == stories_mod.DONE)
    print(f"stories: {done}/{len(rows)} done  ({folder}/stories.yaml)")
    for row in rows:
        print(
            f"  {row.position:2d}. {row.id:12s} {row.label:16s}"
            f"{_checkpoint_badge(row)}  {row.title}"
        )


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
    profiles=None,
    on_started: Callable[[], None] | None = None,
) -> int:
    # The composition (run dir + state + pid + sweep.json + adapters + engine)
    # lives in runsetup; this stays compose -> render. SweepEngine and
    # _make_adapters are handed in from this module's namespace so the test suite's
    # `monkeypatch.setattr(cli, "SweepEngine"/"_make_adapters", ...)` still applies.
    #
    # `profiles` is the auto-sweep gate's frozen resolution (#461 point 4) — it
    # both stamps the pin and builds the adapters, so neither re-reads
    # profiles/*.toml after the gate compared it. `cmd_sweep` passes None: a human
    # started that one, so a fresh read is the point.
    #
    # `on_started` is the auto-sweep parent's latch, likewise absent for
    # `cmd_sweep`; `compose_sweep` fires it at the boundary where this child owns a
    # published, resumable run dir.
    composed = runsetup.compose_sweep(
        project=project,
        paths=paths,
        policy=pol,
        run_id=run_id,
        prompting=prompting,
        decisions_only=decisions_only,
        max_bundles=max_bundles,
        repeat=repeat,
        max_cycles=max_cycles,
        trigger=trigger,
        make_adapters=_make_adapters,
        sweep_engine_cls=SweepEngine,
        trusted_config_digest=_trusted_config_digest(pol, project, profiles=profiles),
        profiles=profiles,
        on_started=on_started,
    )
    print(f"sweep {composed.run_id} starting (attach: bmad-loop attach)")
    summary = composed.engine.run()
    print(summary.render())
    return 0


def _sweep_factory(project: Path, paths: bmadconfig.ProjectPaths, trusted_digest: str):
    """Child-sweep launcher injected into story-run engines. Auto-triggered
    sweeps are unattended: never prompt, never run decision bundles.

    The returned callable implements :class:`engine.SweepFactory`: every refusal
    below raises *before* the keyword-only ``started`` thunk can fire, which is
    what leaves the parent run's trigger unspent for a child that never launched.

    ``trusted_digest`` is the caller's launch-time :func:`runsetup.config_digest`
    — the integrity pin for the config this factory re-reads from disk below.
    Required, with no default: an omitted baseline would silently disable the
    refusal, which is the failure mode a security gate can least afford. The
    re-read happens once and is frozen, so the gate validates the bytes the child
    actually launches from rather than a separate read of the same files."""

    def factory(trigger: str, *, started: Callable[[], None]) -> None:
        pol = policy_mod.load(_policy_path(project))
        # Read the agent-writable config EXACTLY ONCE, here, and run the child off
        # these two objects: `pol` and `profiles` are threaded through the gate
        # below, the pin `_start_sweep` stamps, and `make_adapters`. Before this
        # the gate hashed its own `get_profile` read while the adapters took a
        # later one, so a session that leaves a background writer alternating a
        # benign and a hostile profiles/*.toml only had to land the benign copy on
        # the digest's read — and losing a round costs it nothing, since the
        # refusal is swallowed by `_maybe_auto_sweep` and the next trigger deals
        # again. Freezing the read is the fix; narrowing the window is not.
        profiles = runsetup.resolve_profiles(pol, project)
        # #461 point 4. This reload is the ONE path where a mid-run rewrite of the
        # agent-writable config reaches host code execution with no human
        # initiating the change: the driven sessions can write policy.toml and
        # profiles/*.toml, and this factory re-reads both on a trigger the engine
        # raised, not an operator. The parent loop is already safe — it froze its
        # Policy at launch — and the two documented human-present mid-run edits
        # (the TUI live-edit, resume-to-fix-a-setting) stay trusted; resume only
        # warns. So refuse here and only here.
        #
        # ABLATION GUARDS: deleting this block must fail
        # test_auto_sweep_refuses_a_rewritten_verify_command,
        # test_auto_sweep_refuses_a_rewritten_profile_binary, and
        # test_auto_sweep_refuses_a_widened_plugin_allowlist in tests/test_cli.py.
        if runsetup.config_digest(pol, project, profiles=profiles) != trusted_digest:
            raise RuntimeError(
                "policy.toml/profiles changed under a running loop before an auto-sweep"
                " — refusing the child sweep; no human initiated this config change."
                " Run `bmad-loop sweep` yourself to proceed under the new config."
            )
        # Raise rather than return the rc the other three sites return. By the time
        # the engine calls this it has journaled `sweep-auto-trigger`, and it reads
        # a plain return as a child that ran — latching the trigger on one whether
        # or not `started` fired — so a bare decline would be recorded as
        # `sweep-auto-finished`, which `engine.py` defines as "a clean completion
        # from the parent's perspective": a child sweep that ran and finished when
        # none was ever launched. Raising lands on the `sweep-auto-not-started` +
        # notify path the `load` above already takes on an unparseable policy.toml,
        # which is the same kind of event — the config on disk changed under a run
        # that had already started.
        conflict = bmadconfig.worktree_isolation_conflict(paths, pol.scm.isolation)
        if conflict is not None:
            raise RuntimeError(conflict)
        _start_sweep(
            project,
            paths,
            pol,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            trigger=trigger,
            profiles=profiles,
            on_started=started,
        )

    return factory


def cmd_sweep(args: argparse.Namespace) -> int:
    if (rc := _reject_bad_run_id(args.run_id)) is not None:
        return rc
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = policy_mod.load(_policy_path(project))

    if args.dry_run:
        return _sweep_dry_run(paths, pol)

    if (rc := _reject_isolation_conflict(paths, pol)) is not None:
        return rc

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
    # Before the no-ledger early return below: a broken install is worth saying so
    # about whether or not there is anything to sweep.
    _warn_preflight_would_abort(paths, pol)
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
        print(f"  triage: {_render_invocation(pol, paths.project, 'triage', '/bmad-loop-sweep')}")
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
    # Resume re-reads config.yaml and policy.toml from disk, so it is a second
    # entrypoint into the same engine and gets the same refusal — a run started
    # before the override was added must not finish its remaining stories through
    # provisioning the preflight would now refuse.
    if (rc := _reject_isolation_conflict(paths, pol)) is not None:
        return rc
    if not _require_base_skills(project, pol, require_stories=state.source == "stories"):
        return 1
    journal = Journal(run_dir)
    # Read the outgoing weight BEFORE the re-stamp below replaces it: the
    # session-end entries this run already wrote were weighted at it, so without
    # recording it here they stop being reconstructible from the (now newer)
    # snapshot — the very guarantee #129 exists to provide. Scalars only, never
    # policy keys or values: journal entries are unsanitized at write time, and
    # diagnose scrubs unknown fields with scrub_json, NOT the key-aware
    # _scrub_policy that reduces adapter.env and plugins.settings.
    new_snapshot = pol.to_dict()
    # Resolved once and carried into compose_resume below, so the re-stamped pin
    # describes the bytes this resumed process actually launches (see
    # `_launch_profiles`). A resume mints the same in-memory baseline `cmd_run`
    # does — `_sweep_factory(..., new_digest)` — so the same reasoning applies.
    profiles = _launch_profiles(pol, project)
    new_digest = _trusted_config_digest(pol, project, profiles=profiles)
    # #461 point 4, human-present half. A resume IS a deliberate human choice, so
    # the on-disk config is re-blessed (new_digest is re-stamped below) and the run
    # proceeds — the auto-sweep child is the only path that refuses. But the issue's
    # real complaint is that the change is SILENT, so say it out loud: a session may
    # have rewritten the verify commands / launch binary / plugin allowlist while
    # the operator was away, and `resume` is the moment they can still look.
    # Deliberately no --allow-config-change flag: a refuse-by-default gate here
    # would break resume-to-fix-a-setting, which is a supported workflow.
    #
    # ADVISORY, not a control, and the asymmetry with the gate is deliberate: the
    # auto-sweep refusal in `_sweep_factory` baselines off a digest threaded in
    # MEMORY from launch (see cmd_run/the compose_resume call below), which nothing
    # on disk can reach at all. This one has to survive the pause, so it is read
    # back — and since #498 from the run's out-of-tree state dir rather than from
    # <project>/.bmad-loop/runs/<id>/state.json, which is inside the very tree the
    # digest exists to police. It used to be readable there: a session that
    # rewrote policy.toml could blank the field in the same breath and silence this
    # warning (blank sufficed — the bool() guard reads "" as no prior pin), so the
    # advisory caught the incidental change and never the deliberate one.
    #
    # The legacy fallback, for one release: a run PAUSED under the old code has no
    # file in the state root, and its baseline is in state.json and nowhere else.
    # `None` from the reader means exactly that "no file" — distinct from an empty
    # file, which is a real answer of "no baseline" and must NOT reopen the tree —
    # so the fallback fires only for those runs, and the re-stamp below migrates
    # them on this very resume. Empty either way keeps its meaning: no prior pin,
    # no warning.
    pinned = runs.read_trusted_config_digest(project, run_dir.name)
    if pinned is None:
        pinned = state.trusted_config_digest
    security_config_changed = bool(pinned) and new_digest != pinned
    fields: dict[str, object] = {
        # Scalars only, per the note above: a bool records THAT the pinned surface
        # moved without journaling a command, a binary path or a plugin name.
        "security_config_changed": security_config_changed,
        "was_paused": state.paused_reason,
        "cache_read_weight": pol.limits.cache_read_weight,
        # Compare JSON-normalized, the way save_state persists it: to_dict()
        # returns TUPLES (verify.commands, extra_args, plugins.enabled) where the
        # reloaded snapshot has lists, so a plain != reports "changed" on every
        # resume — including one where policy.toml was never touched.
        "policy_changed": bool(state.policy_snapshot)
        and json.dumps(new_snapshot, sort_keys=True)
        != json.dumps(state.policy_snapshot, sort_keys=True),
    }
    prior_weight = state.cache_read_weight()
    if prior_weight != pol.limits.cache_read_weight:
        fields["cache_read_weight_was"] = prior_weight
    journal.append("run-resume", **fields)
    if security_config_changed:
        # STATIC category names — the ones config_digest covers. A single sha256
        # cannot say which of them moved, and naming the actual old/new values is
        # exactly what this must not do (the mutation is attacker-controlled text
        # headed for an operator's terminal).
        print(
            f"warning: run {run_dir.name}: the host-exec config pinned at launch has"
            " changed — one or more of: verify commands, launch binary/args/env,"
            " plugin allowlist. Resuming trusts the config now on disk; re-read"
            " .bmad-loop/policy.toml and .bmad-loop/profiles/ first if you did not"
            " make that edit.",
            file=sys.stderr,
        )
    # Re-stamp: the snapshot must describe the policy THIS process enforces, for
    # its whole lifetime (Policy is loaded once here and frozen — the engine never
    # re-reads policy.toml). Enforcement already reads the reloaded `pol` (the
    # per-story budget in Engine._finish_commit, every SessionSpec), so leaving
    # the launch-time snapshot in place made every display — status, the TUI, the
    # run summary, the diagnose bundle — report a weight the budget was not using,
    # silently up to 10x off at the legal extremes (#189).
    # Deliberately NOT re-derived from the new policy: state.source/spec_folder/
    # run_type/epic_filter/target_branch stay pinned at launch (see RunState), so
    # a policy edit mid-run cannot switch a live run's mode or scope. The snapshot
    # can therefore disagree with those fields; that is correct, not a bug.
    state.policy_snapshot = new_snapshot
    # Re-baseline the integrity pin for the same reason and at the same moment: a
    # human typed `resume`, which re-blesses the config on disk, and the engine
    # this process is about to arm re-reads it from there. Leaving the launch
    # digest would make every auto-sweep after a legitimate resume-to-fix-a-setting
    # refuse — and would make the warning above fire forever, on every later resume.
    #
    # This is also the migration for a run paused under the old code: it read its
    # baseline out of state.json above, and from here on it has a file in the state
    # root, so the in-tree copy stops deciding anything for it.
    runs.write_trusted_config_digest(project, run_dir.name, new_digest)
    # ...and the in-tree secondary is re-stamped in the same breath, for the same
    # reason it is written at launch: the state root is keyed by the project's
    # RESOLVED PATH (`runs.project_tag`), so moving or renaming the project — a
    # documented operation, FEATURES.md states the GC half of it — keys the run
    # somewhere new and the file above becomes unreachable. Leaving only that copy
    # made a move silently retire the pin, and the advisory this whole guard exists
    # to raise never fired again. Writing both keeps the warning alive across a move
    # without making this copy authoritative: the reader prefers the out-of-tree
    # file whenever it exists, so a session that rewrites this one is still ignored
    # (which is #498, and its test still holds). Same for a changed
    # BMAD_LOOP_STATE_DIR. Tampering that removes the out-of-tree file is a
    # different problem with no fix at equal privilege — #571.
    state.trusted_config_digest = new_digest
    state.clear_pause()
    # A resume is fresh user intent: discard any graceful-stop request left over from
    # a prior stopped-gracefully run so the re-armed engine does not consume it at the
    # first item boundary and immediately re-stop. Fire before write_pid — the moment
    # the pid lands the engine is "live" and a lingering request becomes honorable.
    if runs.clear_graceful_stop(run_dir):
        print(
            f"run {run_dir.name}: discarded a stale graceful-stop request before resuming",
            file=sys.stderr,
        )
    runs.write_pid(run_dir)
    # Persist before the engine starts: status, the TUI and diagnose only ever
    # read state.json, and Engine._save() may not fire for minutes. write_pid
    # runs FIRST so no observer catches a window of "not paused + dead pid",
    # which runs.discover_runs classifies as INTERRUPTED.
    save_state(run_dir, state)
    # The adapter build + engine selection (sweep vs stories vs plain, from
    # persisted state) lives in runsetup; the re-stamp/pid/save bookkeeping above
    # stays here because its ordering is load-bearing. Engine/StoriesEngine/
    # SweepEngine and _make_adapters are handed in from this module's namespace so
    # the test suite's `monkeypatch.setattr(cli, "SweepEngine"/"Engine"/..., ...)`
    # still applies.
    composed = runsetup.compose_resume(
        project=project,
        paths=paths,
        run_dir=run_dir,
        state=state,
        policy=pol,
        journal=journal,
        sweep_factory=_sweep_factory(project, paths, new_digest),
        make_adapters=_make_adapters,
        engine_cls=Engine,
        stories_engine_cls=StoriesEngine,
        sweep_engine_cls=SweepEngine,
        profiles=profiles,
    )
    summary = composed.engine.run()
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


def _resolve_restore_patch(
    project: Path,
    run_dir: Path,
    story_key: str,
    args: argparse.Namespace,
    pol,
    state,
    task,
) -> tuple[str | None, str | None]:
    """Determine the intent-gap patch-restore latch (BMAD-METHOD #2564) for a re-arm.

    Precedence: the explicit ``--restore-patch`` flag (hand-driven recovery) wins;
    otherwise, on the interactive path, the resolve agent may have recorded a
    ``restore_patch`` field in resolution.json. The flag path is fully knowable
    before the interactive session, so cmd_resolve validates it FIRST and only
    falls back here post-session for the resolution.json read. Returns
    ``(latch, error)``: a validated absolute patch path to latch (None = ordinary
    from-scratch re-drive), or an error string when a supplied path is missing /
    outside the trusted roots, the run can't restore in place, or the restore
    input itself is corrupt (unreadable resolution.json, empty/non-string value)
    — the caller aborts strictly rather than silently re-driving from scratch
    when a restore was (or may have been) asked for.

    The run-state preconditions come from ``runs.validate_restore_latch``, shared
    verbatim with ``runs.rearm_escalation``; only the CLI-side halves live here —
    path resolution against ``--project`` and trusted-roots containment, which need
    the loaded bmad config."""
    raw = getattr(args, "restore_patch", None)
    if raw is not None and not raw.strip():
        # `--restore-patch ""` is a classic unset-shell-var slip. Treating it as
        # "no restore" would silently re-drive from scratch (and even mask a
        # restore the resolve agent recorded) — and a re-arm consumes the
        # escalation, so the dropped decision would be unrecoverable.
        return None, (
            "--restore-patch got an empty path (unset shell variable?) — pass the "
            "saved patch path, or drop the flag entirely for a from-scratch re-drive"
        )
    if raw is None and args.interactive:
        try:
            doc = resolve.read_resolution(run_dir, story_key)
        except resolve.ResolutionError as e:
            return None, (
                f"{e} — the recorded resolution (and any restore_patch decision in "
                "it) cannot be read; fix or delete the file, or re-run with "
                "--no-interactive [--restore-patch <path>] to decide by hand"
            )
        val = None if doc is None else doc.get("restore_patch")
        if val is not None:
            # the schema says omit the field for an ordinary resolution; an empty
            # or non-string value is a corrupted recorded decision, not "none"
            if not isinstance(val, str) or not val.strip():
                return None, (
                    f"resolution.json for {story_key} carries an invalid "
                    f"restore_patch value {val!r} — expected a non-empty path (or "
                    "the field omitted); fix the file, or re-run with "
                    "--no-interactive [--restore-patch <path>] to decide by hand"
                )
            raw = val
    if not raw:
        return None, None
    # The state-side preconditions (sentinel wedge, spec-less escalation, worktree
    # isolation) are the same set rearm_escalation enforces — run them here so an
    # unhonorable restore aborts BEFORE the interactive resolve session rather than
    # after. The live policy's isolation mode is the one input run state can't
    # carry, so pass it: a policy edit between escalation and resolve can't skew
    # the guard.
    err = runs.validate_restore_latch(
        state, task, story_key, worktree_isolation=pol.scm.isolation == "worktree"
    )
    if err is not None:
        return None, err
    # `.resolve()` on top of the shared normalizer: this is the one consumer that
    # feeds a containment check (spec_within_roots), which needs `..`/symlinks
    # collapsed. The resolved absolute path is what gets latched, so this stays a
    # bare `.resolve()` rather than `resolve_or_lexical`: a degraded, non-canonical
    # answer here could pass containment on the wrong directory.
    try:
        patch = verify.resolve_restore_path(raw, project).resolve()
    except (OSError, RuntimeError) as e:
        return None, (
            f"cannot canonicalize the restore patch path {raw!r}: {e} — whether it "
            "lies inside or outside the project tree cannot be determined, so the "
            "restore cannot be latched. Run `bmad-loop validate` for what this host "
            "is doing."
        )
    # Same trusted-roots shape as the frontmatter reconcile's spec_within_roots:
    # bmad-build-auto saves the patch under implementation_artifacts, and artifact
    # dirs configured OUTSIDE the project tree are a supported layout — a bare
    # is_relative_to(project) check would reject every legitimate restore there.
    try:
        paths = bmadconfig.load_paths(project)
    except bmadconfig.BmadConfigError as e:
        return None, f"cannot validate the restore patch path against the project config: {e}"
    if not patch.is_file() or not verify.spec_within_roots(patch, paths):
        return None, (
            f"restore patch {raw!r} is not a file under the project or its "
            "configured artifact roots — refusing to re-arm (fix the path, or "
            "re-run without a restore to re-drive from scratch)"
        )
    return str(patch), None


def _echo_stale_restore(run_dir: Path, seen_entries: int) -> None:
    """Surface the `stale-restore-*` events a just-completed re-arm journaled about
    the restore attempt it abandoned (runs._stale_restore_residue). The commits
    variant is the one the human must act on — nothing else will."""
    for entry in Journal(run_dir).entries()[seen_entries:]:
        kind = entry.get("kind", "")
        if kind == "stale-restore-excluded":
            files = ", ".join(entry.get("files", []))
            print(
                f"note: excluded the abandoned restore's new files from the "
                f"re-drive baseline: {files}",
                file=sys.stderr,
            )
        elif kind == "stale-restore-unparseable":
            print(
                f"warning: could not read the abandoned restore patch "
                f"({entry.get('patch', '?')}) — its new files may be swept into the "
                "next commit; check `git status` before resuming",
                file=sys.stderr,
            )
        elif kind == "stale-restore-commits":
            n = len(entry.get("commits", []))
            print(
                f"warning: {n} commit(s) sit below the re-drive's new baseline "
                f"({entry.get('old_baseline', '?')[:12]}..) — if any came from the "
                "abandoned attempt rather than your resolve, revert them now",
                file=sys.stderr,
            )


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

    pol = policy_mod.load(_policy_path(project))

    # intent-gap patch-restore latch (#2564), explicit-flag path: everything about
    # it (isolation mode, path containment) is knowable NOW — validate before the
    # interactive resolve session, not after a whole agent conversation the abort
    # would throw away. The resolution.json path can only be validated
    # post-session (below); build_context tells the agent up front when a restore
    # can't be honored so it never negotiates one.
    restore_patch: str | None = None
    if args.restore_patch is not None:
        restore_patch, err = _resolve_restore_patch(
            project, run_dir, story_key, args, pol, state, task
        )
        if err is not None:
            print(err, file=sys.stderr)
            return 1

    if args.interactive:
        adapters = _make_adapters(project, run_dir, pol)
        model = pol.adapter.resolved("dev").model
        resolve.build_context(state, run_dir, story_key, isolation=pol.scm.isolation)
        print(f"launching resolve agent for {story_key} — converse, fix the spec, then exit…")
        try:
            produced = resolve.run_session(
                adapters["dev"], project, run_dir, story_key, model=model
            )
        except NotImplementedError:
            print(
                "the dev adapter has no interactive session mode — fix the spec by hand, "
                f"then: bmad-loop resolve {args.run_id} --no-interactive",
                file=sys.stderr,
            )
            return 1
        if not produced:
            print(
                f"no resolution recorded for {story_key} (agent did not write resolution.json)",
                file=sys.stderr,
            )

    # resolution.json restore latch: only exists after the session ran, so this
    # arm of the validation cannot be hoisted above it.
    if args.restore_patch is None:
        restore_patch, err = _resolve_restore_patch(
            project, run_dir, story_key, args, pol, state, task
        )
        if err is not None:
            print(err, file=sys.stderr)
            return 1

    # confirm-then-resume (args.resume: None = ask, True = auto, False = re-arm only)
    if args.resume is None and not _confirm(f"re-arm {story_key} and resume run {args.run_id}?"):
        print("cancelled — run is still paused at the escalation")
        return 0
    seen_entries = len(Journal(run_dir).entries())
    try:
        runs.rearm_escalation(run_dir, story_key, restore_patch=restore_patch)
    except runs.RearmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _echo_stale_restore(run_dir, seen_entries)
    print(
        f"re-armed {story_key}"
        + (" (restoring the attempted change for review)" if restore_patch else "")
    )
    if args.resume is False:
        print(f"resume when ready: bmad-loop resume {args.run_id}")
        return 0
    from .tui import launch  # import-safe: launch.py has no textual imports

    if launch.in_ctl_session():
        # We are inside the TUI's bmad-loop-ctl window the user is attached to.
        # Tell them, hand the terminal back, and let the engine run on here — a
        # tmux pane keeps running after its client detaches.
        print(
            f"✓ resuming run {args.run_id} in the background — "
            f"watch it in the TUI, or: bmad-loop attach {args.run_id}"
        )
        launch.detach_client()
    return _resume_paused_run(project, run_dir)


def _print_parked(parked: list[operatoractions.ParkedStory]) -> int:
    if not parked:
        print("no stories are awaiting operator actions")
        return 0
    print(f"{len(parked)} story/stories awaiting operator actions:\n")
    for p in parked:
        # `resumable` first, for the same reason `cmd_confirm` tests it first: an
        # interrupted confirmation drifts, so reading `drift()` alone would label
        # a story confirm will happily finish as NOT CONFIRMABLE.
        if p.resumable:
            suffix = "  — ALREADY SIGNED OFF: re-run confirm to advance the board"
        else:
            drift = p.drift()
            suffix = f"  — NOT CONFIRMABLE: {drift}" if drift else ""
        # `commit` is derived provenance and legitimately empty for a record not
        # yet in any commit (NO_VCS, or a crash before the park's commit landed)
        # — render nothing rather than a dangling "commit " fragment.
        provenance = f", commit {p.commit[:8]}" if p.commit else ""
        print(f"  {p.story_key} (parked {p.parked_at}{provenance}){suffix}")
        for i, action in enumerate(p.actions, 1):
            print(f"      {i}. {action}")
        print("")
    print("confirm one when its actions are done: bmad-loop confirm <story-key>")
    return 0


def _reverify(project: Path, cwd: Path) -> str | None:
    """Re-run the project's deterministic verify commands, returning a failure
    reason or None when they all passed.

    Deliberately NOT `verify_commands_outcome`: that classifies into the engine
    loop's vocabulary (retry/fixable/escalate), and a CLI confirm has no attempt
    budget and no repair session to dispatch — only "the flip proceeds" or "it
    does not". The environment-fault reading IS reused, so a missing binary reads
    as the operator's environment rather than as their story having regressed."""
    pol = policy_mod.load(_policy_path(project))
    if not pol.verify.commands:
        print("note: --reverify: no [verify] commands are configured — nothing to re-run")
        return None
    print(f"re-running {len(pol.verify.commands)} verify command(s)...")
    for result in verify.run_verify_commands(pol, cwd):
        fault = verify.env_fault_reason(result, cwd)
        if fault is not None:
            return f"{result.command!r} could not run: {fault}"
        if result.returncode != 0:
            return f"{result.command!r} failed (rc {result.returncode}):\n{result.output_tail}"
    print("  verify commands passed")
    return None


def cmd_confirm(args: argparse.Namespace) -> int:
    """Complete a story parked at `awaiting-operator` once the human has carried
    out the external actions it owes (#335).

    Out of band by construction. The run that parked the story is long finished
    and its task is in a terminal phase with no legal transition, so this touches
    no run state at all — it writes the two things that DEFINE the story's
    completion (the spec and the board) and drops the park record that pointed at
    them. Nothing is re-driven: the agent-doable work was committed at park time,
    and re-running a session would redo finished work while the human's actions
    stayed outside the repo. `--reverify` is there for the case that matters —
    the external action may have changed what the tests see."""
    project = _project(args)
    try:
        paths = bmadconfig.load_paths(project)
    except bmadconfig.BmadConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    parked = operatoractions.resolve(project, paths)
    if args.json:
        # Before any early return and before any prompt: --json IS the listing,
        # and nothing parked is a valid empty document, not a text line.
        machine.emit(confirm_document(parked))
        return 0
    if args.list:
        return _print_parked(parked)
    if not args.story_key:
        print(
            "error: specify a story key to confirm, or --list to see what is parked",
            file=sys.stderr,
        )
        return 2

    key = args.story_key
    story = next((p for p in parked if p.story_key == key), None)
    if story is None:
        print(
            f"error: {key} is not awaiting operator actions in this checkout. A park is "
            f"recorded under {operatoractions.RECORDS_REL.as_posix()}/ and travels with "
            f"the story's own commit, so if another machine's run parked it, pull the "
            f"branch that carries the park commit. `bmad-loop confirm --list` shows "
            f"what is parked here.",
            file=sys.stderr,
        )
        return 1
    if story.resumable:
        return _resume_confirmation(project, paths, args, story)
    drift = story.drift()
    if drift is not None:
        print(
            f"error: refusing to confirm {key}: {drift}. The park entry disagrees with the "
            f"committed state; `bmad-loop validate` reports the same drift. Nothing was "
            f"changed.",
            file=sys.stderr,
        )
        return 1
    spec = story.spec_path
    assert spec is not None  # drift() is non-None whenever the spec path is missing

    print(f"{key} owes {len(story.actions)} external action(s):\n")
    if args.yes:
        for i, action in enumerate(story.actions, 1):
            print(f"  {i}. {action}")
        print("")
    else:
        # Per action, not one blanket prompt: the whole failure this state exists
        # to prevent is a story marked done with some of its obligations quietly
        # unmet, and a single "all done? [y/N]" invites exactly that.
        for i, action in enumerate(story.actions, 1):
            if not _confirm(f"  {i}. {action}\n     done?"):
                print("cancelled — nothing was changed")
                return 0
        print("")

    if args.reverify:
        failure = _reverify(project, paths.repo_root)
        if failure is not None:
            print(
                f"error: --reverify failed, so {key} was NOT confirmed: {failure}",
                file=sys.stderr,
            )
            return 1

    return _apply_confirmation(project, paths, story, spec)


def _resume_confirmation(
    project: Path,
    paths: bmadconfig.ProjectPaths,
    args: argparse.Namespace,
    story: operatoractions.ParkedStory,
) -> int:
    """Finish a confirmation that was interrupted between its spec writes and its
    board write (see `ParkedStory.resumable`).

    Checked BEFORE the drift refusal, because to `drift()` this state looks like
    an ordinary stale entry ("its spec now says status: done") — so without this
    branch `confirm` refuses the one state re-running it exists to clear, and
    nothing else will ever drop the entry.

    Deliberately does NOT re-prompt and does NOT append a second audit section.
    The section already on disk IS the acknowledgment; asking again would have no
    honest meaning over a spec that already says `done`, and
    `append_operator_confirmation` accumulates rather than no-ops, so a second
    pass would have the audit trail claim two sign-offs for one event.

    `--reverify` still runs, because it is a gate the caller requested for THIS
    invocation rather than an acknowledgment of anything — its failure says "NOT
    advanced", since the confirmation itself already happened."""
    spec = story.spec_path
    assert spec is not None  # resumable requires a spec that read back as done
    print(
        f"{story.story_key} was already confirmed — its spec is signed off and reads "
        f"done, but the board and the park entry were left behind. Finishing that; "
        f"you will not be asked to acknowledge anything again.\n"
    )
    if args.reverify:
        failure = _reverify(project, paths.repo_root)
        if failure is not None:
            print(
                f"error: --reverify failed, so {story.story_key} was NOT advanced: {failure}",
                file=sys.stderr,
            )
            return 1
    return _land_confirmation(project, paths, story, spec, time.strftime("%Y-%m-%d"))


# Both refusals below land AFTER the audit section is on disk and both ask for a
# re-run, which appends a second one. Said out loud rather than left as a
# surprise: only the human can decide whether one sign-off should show as two.
_SECOND_SECTION_NOTE = (
    "Note: that re-run appends a SECOND `## Operator Confirmation` section — "
    "delete this one first if the audit trail should record a single sign-off."
)


def _apply_confirmation(
    project: Path,
    paths: bmadconfig.ProjectPaths,
    story: operatoractions.ParkedStory,
    spec: Path,
) -> int:
    """Write the confirmation: spec audit section, spec status, board, park entry.

    Ordered so a failure part-way is recoverable rather than stranded. The audit
    section goes first because it is the only record of what happened outside the
    repo and it does not change any gate. The park entry is dropped LAST, so
    anything that raises before it leaves the story findable and the command
    re-runnable — see `ParkedStory.resumable` for the state that leaves behind.

    A repeat between the section and the status write is the one case that does
    NOT round-trip cleanly: `append_operator_confirmation` accumulates rather than
    no-ops (a genuinely repeated confirmation — a spec reverted to the park status
    and signed off again — is a real event the audit trail must not lose), so a
    re-run after a failure *here* appends a second section for one event. That is
    a known gap, not the deliberate case the writer's docstring describes.

    It is not only a crash window either: the refusals below both END by asking
    for exactly that re-run, so the duplicate is the ORDINARY outcome of taking
    their advice. Both therefore say so — an unmentioned second sign-off in an
    audit trail is worse than a mentioned one, and the human is the only one who
    can decide whether the first record should stay.

    Repair-write doctrine: these writes RAISE rather than degrade, and this
    asserts the resulting STATE rather than trusting a return value. `confirm` is
    about to declare a story finished; a skipped write it did not notice would
    make it declare that falsely. The commit alone is best-effort — the files are
    the state, git history is the record of it."""
    today = time.strftime("%Y-%m-%d")
    if not devcontract.append_operator_confirmation(spec, story.actions, date=today):
        # The one False the writer returns: the spec is gone since `resolve` read
        # it. Fatal here — the audit section is the ONLY record of the part of
        # this story that happened outside the repository, and there is nothing
        # left to write a status onto either.
        print(
            f"error: {spec} disappeared before {story.story_key} could be confirmed — "
            f"nothing was changed and the park entry has been left in place.",
            file=sys.stderr,
        )
        return 1
    try:
        frontmatter.set_frontmatter_status(spec, "done")
    except frontmatter.FrontmatterWriteError as e:
        print(
            f"error: {spec} carries a status this cannot rewrite, so {story.story_key} "
            f"was NOT confirmed: {e}. The audit section was appended; the board and the "
            f"park entry are untouched, so re-run `bmad-loop confirm {story.story_key}` "
            f"once the frontmatter is repaired. {_SECOND_SECTION_NOTE}",
            file=sys.stderr,
        )
        return 1
    # Read the FILE back, not the writer's return: `set_frontmatter_status`
    # returns False for "nothing to change" as well as for "already there", and
    # this is about to declare the story done. It also covers what no return
    # value can — an `atomic_replace` that landed somewhere else, a shape nobody
    # enumerated, a concurrent writer. Symmetric with the board half below, which
    # has always read itself back via `landed`.
    if frontmatter.status_of(frontmatter.read_frontmatter(spec)) != "done":
        print(
            f"error: {spec} still does not read status: done, so {story.story_key} was "
            f"NOT confirmed. The audit section was appended; the board and the park "
            f"entry are untouched. {_SECOND_SECTION_NOTE}",
            file=sys.stderr,
        )
        return 1
    return _land_confirmation(project, paths, story, spec, today)


def _land_confirmation(
    project: Path,
    paths: bmadconfig.ProjectPaths,
    story: operatoractions.ParkedStory,
    spec: Path,
    today: str,
) -> int:
    """The half of a confirmation that lives OUTSIDE the spec: advance the board,
    drop the park record, commit the set.

    Split out because it is exactly what an interrupted confirmation still owes.
    The spec half is already on disk in that case — audit section appended, status
    at `done` — and re-running it would append a second section for one event, so
    the resume path enters here instead of at :func:`_apply_confirmation`."""
    # The sole write path to the board, and an ordinary FORWARD move:
    # `awaiting-operator` sits immediately below `done` in STATUS_ORDER, so
    # confirming needs no exception to never-regress. Idempotent at `done`, which
    # is what lets a resume run it after a human fixed the board by hand.
    landed = sprintstatus.advance(paths.sprint_status, story.story_key, "done", now=today)
    if landed != "done":
        print(
            f"error: {spec} was updated but {paths.sprint_status} did not advance "
            f"{story.story_key} to done (it reads {landed!r}). Fix the board by hand, then "
            f"re-run `bmad-loop confirm {story.story_key}` — the spec is already correct "
            f"and signed off, and the park entry has been left in place, so the re-run "
            f"finishes what is left without asking you to acknowledge anything twice.",
            file=sys.stderr,
        )
        return 1
    # Resolve the record path BEFORE dropping it: the drop unlinks the file, and
    # a committed record's deletion must ride the confirm commit (#356) — the
    # record arrived in the park's commit, so leaving its removal uncommitted
    # would dirty the tree the next run's preflight refuses. `commit_paths`
    # skips the path when no record was ever committed (a legacy-index park).
    record = operatoractions.record_path(project, story.story_key)
    operatoractions.drop(project, story.story_key)
    # A GITIGNORED board is left OUT of the list rather than handed over and
    # forgiven (#577). `commit_paths` forces every operand literal, so `git add`
    # refuses an ignored one with rc 1 — and refuses the whole operand list with
    # it, staging nothing — which the swallow below then turns into a silent loss
    # of the other two. That is right for the board (a file git will not track has
    # no commit to ride; the status on disk is the value) and wrong for the spec
    # and the record, whose deletion in particular MUST land per the #356 note
    # above. #350's board carry makes a gitignored board an ordinary shape, so
    # this is the common park's exit and not an exotic one.
    try:
        board_ignored = verify.path_ignored(paths.repo_root, paths.sprint_status)
    except verify.GitError:
        board_ignored = False  # uncertainty keeps the board in: the older behavior
    try:
        verify.commit_paths(
            paths.repo_root,
            f"chore(operator): confirm {story.story_key}",
            [spec, record] if board_ignored else [spec, paths.sprint_status, record],
        )
    except verify.GitError:
        pass  # files are written; git history is best effort (as `decisions`)
    print(f"✓ {story.story_key} confirmed — spec and board are done")
    return 0


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
    if args.json:
        # Before the empty-set early return (nothing pending is a valid empty
        # document, not the text line), and regardless of --list: --json *is*
        # the listing. It cannot fall through to the prompter, which reads stdin
        # and prints per-answer progress — neither survives a pure document.
        machine.emit(decisions_document(pending))
        return 0
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
        print("answer them interactively: bmad-loop decisions")
        return 0
    prompter = DecisionPrompter()
    print(f"{len(pending)} unanswered decision(s) — your answers carry into the next sweep:")
    today = time.strftime("%Y-%m-%d")
    for decision in pending:
        option = prompter.ask(decision)
        try:
            decisions.apply_pre_answer(project, decision, option, date=today)
        except (OSError, bmadconfig.BmadConfigError, ValueError) as e:
            # What this buys is the `{decision.id}` in the message, and only
            # that: `main`'s tail catches BmadConfigError by name and everything
            # else through a bare `except Exception`, so none of these ever
            # reached argparse as a traceback, and the exit code is 1 either way.
            # The loop is what makes attribution worth a handler — it has already
            # printed an outcome line per answered decision, and a bare
            # `error: <msg>` after them does not say which one did not land.
            #
            # ValueError is the ledger writers' `date` precondition, unreachable
            # from the strftime above and here for the same reason as the TUI's
            # copy of this call. BmadConfigError is the reachable one:
            # `apply_pre_answer` re-reads the BMAD config on every call and
            # `prompter.ask` blocks on the human in between, so a config removed
            # or broken mid-prompt raises here even though the read at the top of
            # this command succeeded. Leaving it out gave the likelier failure the
            # worse message.
            print(f"error: could not record {decision.id}: {e}", file=sys.stderr)
            return 1
        if option.effect == "close":
            outcome = "closed now"
        elif option.effect == "build":
            outcome = "queued — the next sweep will build it"
        else:
            outcome = "kept open (recorded)"
        print(f"  {decision.id}: {outcome}")
    print("\nrun `bmad-loop sweep` to act on any builds.")
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
    # A pending graceful stop is not in state.json (it's the control file + a live
    # engine), so derive it here and hand it to the builder / text branch. Order the
    # `and` so the cheap file check gates the engine_liveness probe: skip it when the
    # run is already concluded or no request is on disk.
    graceful_pending = (
        not (state.finished or state.paused or state.stopped or state.crashed)
        and runs.graceful_stop_requested(run_dir)
        and runs.engine_liveness(run_dir) != "dead"
    )
    if args.json:
        machine.emit(status_document(state, graceful_stop_pending=graceful_pending))
        return 0
    kind = f" [{state.run_type}]" if state.run_type != "story" else ""
    print(f"run {state.run_id}{kind}  started {state.started_at}")
    if state.finished:
        print("status: finished")
    elif state.paused:
        print(f"status: PAUSED ({state.paused_stage}) — {state.paused_reason}")
    elif graceful_pending:
        print("status: in progress — graceful stop pending (will stop after the current item)")
    else:
        print("status: in progress (or interrupted)")
    if state.sweeps_refused:
        detail = ", ".join(f"{trigger} ({why})" for trigger, why in state.sweeps_refused.items())
        print(f"auto-sweep not run: {detail} — deferred work is untouched")
        print("  run `bmad-loop sweep` with a clean worktree")
    raw_total, weighted_total, weight = run_token_totals(state)
    if raw_total:
        print(
            f"tokens: {weighted_total:,} weighted "
            f"({raw_total:,} raw incl. cache reads, cache_read_weight {weight})"
        )
    for key, task in state.tasks.items():
        raw = task.tokens.total
        # Gate on raw (does this task have ANY tokens?), never on weighted: with
        # cache_read_weight = 0 a cache-read-only task weighs 0 but has nonzero
        # raw, and must render "0", not "-" — "-" means no tokens at all, i.e.
        # missing data. Mirrors tui/screens/dashboard.py. Do not "simplify" this
        # to `if weighted` now that weighted is the displayed value.
        tokens = f"{task.tokens.weighted_total(weight):,}t ({raw:,} raw)" if raw else "-"
        extra = task.defer_reason or task.commit_sha or ""
        if task.preserve_ref:
            extra = f"{extra} [{task.preserve_ref}]".lstrip()
        # 17 = len("awaiting-operator"), the longest Phase token; a narrower
        # field does not truncate, it shifts every following column on that one
        # row, which reads as corrupt output rather than as a long status.
        print(
            f"  {key:40s} {task.phase:17s} dev×{task.attempt} review×{task.review_cycle} "
            f"{tokens} {extra}"
        )
    if state.source == "stories":
        _print_stories_status(state, project)
    else:
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
                f"deferred-work decisions awaiting an answer: {len(missed)} (bmad-loop decisions)"
            )
    except bmadconfig.BmadConfigError:
        pass
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    project = _project(args)
    infos = runs.discover_runs(project)  # oldest first
    if args.json:
        machine.emit(list_document(infos))
        return 0
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
    # Backend-honest inside-the-multiplexer probe: current_return_target() is
    # None outside, so a resolvable own pane means "switch the client back
    # here", anything else means a throwaway client was attached and must
    # detach.
    if return_window is not None:
        ret = launch.current_return_target()
        if ret is not None:
            launch.set_return_pane(return_window, ret)
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
    if args.cancel_graceful:
        return _cmd_cancel_graceful(run_dir, args.run_id)
    if args.graceful:
        return _cmd_request_graceful(run_dir, args.run_id)
    # Hard stop (unchanged): SIGTERM the engine, kill its agent window, mark stopped.
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


def _cmd_cancel_graceful(run_dir: Path, run_id: str) -> int:
    """`stop --cancel-graceful`: discard a pending request so the run keeps going."""
    if runs.clear_graceful_stop(run_dir):
        print(f"run {run_id}: graceful stop request cancelled")
        return 0
    print(f"run {run_id} has no graceful stop pending", file=sys.stderr)
    return 1


def _cmd_request_graceful(run_dir: Path, run_id: str) -> int:
    """`stop --graceful`: ask a live run to finish its in-flight item, then stop
    cleanly (resumable). Delivery + refusal live in runs.request_graceful_stop;
    here we only translate its status token into operator-facing messaging."""
    try:
        outcome = runs.request_graceful_stop(run_dir)
    except runs.GracefulStopError as e:
        print(str(e), file=sys.stderr)
        return 1
    if outcome == "already-pending":
        print(f"run {run_id} already has a graceful stop pending")
        return 0
    if outcome == "requested-unverifiable":
        print(
            f"run {run_id}: could not confirm a live engine (unverifiable pid) — the "
            f"request stands and fires if one is running",
            file=sys.stderr,
        )
    # The in-flight item the run will finish before stopping: the first task not yet
    # in a terminal phase. None when the request lands between items (it takes effect
    # at the next boundary regardless).
    state = load_state(run_dir)
    current = next((key for key, task in state.tasks.items() if not task.terminal), None)
    item = current if current is not None else "none in flight"
    print(
        f"graceful stop requested — run {run_id} will stop after the current item "
        f"completes (current item: {item}); continue later with `bmad-loop resume {run_id}`"
    )
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
    try:
        runs.delete_run(project, run_dir, force=args.force)
    except runs.LiveSessionError as e:
        print(f"{e} (or pass --force)", file=sys.stderr)
        return 1
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
    try:
        dest = runs.archive_run(project, run_dir, force=args.force)
    except runs.LiveSessionError as e:
        print(f"{e} (or pass --force)", file=sys.stderr)
        return 1
    print(f"run {args.run_id} archived to {dest}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from .adapters.multiplexer import MultiplexerError
    from .tui import launch  # pure stdlib; no textual import

    project = _project(args)
    # one partition sample drives the prune and every message below, so the
    # warnings and live count always match what was actually killed/skipped
    killed, live, unknown = runs.prune_sessions(project, dry_run=args.dry_run)
    if not args.json:
        for run_id in sorted(unknown):
            # warn-only: unknown never blocks cleanup (same wording as delete/archive).
            # Pruning kills the tmux session, never the engine pid, so the warning
            # holds after the fact too. In JSON mode this lives in the document
            # instead (sessions.unverifiable_pid), leaving stderr empty.
            print(f"run {run_id}: engine may still be live (unverifiable pid)", file=sys.stderr)
    # The ctl-window half is raiser-side (its candidate scan probes has_session),
    # and the sessions above are ALREADY killed by the time it runs. Letting the
    # raise reach main()'s backstop prints an error and returns 1 with stdout
    # empty — which in --json mode destroys the record of those kills, leaving a
    # consumer unable to tell "killed nothing" from "killed three, lost the
    # receipt". The repair succeeded; only the observation failed, and
    # observation degrades. Mirrors what the TUI worker already does.
    #
    # dry-run kills nothing, so there is no kill outcome to partition: the
    # candidate list IS the plan, and the other two arms stay empty.
    scan_error: str | None = None
    try:
        if args.dry_run:
            windows, survived, unverifiable = launch.prunable_ctl_windows(project), [], []
        else:
            windows, survived, unverifiable = launch.prune_ctl_windows(project)
    except (MultiplexerError, UnicodeError) as e:
        # Three empty lists is the honest answer: the raise comes from the
        # candidate scan, so no window was killed or even chosen. But an empty
        # partition alone is also what a clean scan that found nothing emits, so
        # the document carries the failure as ctl_windows.scan_error — without
        # it a --json consumer accepts a failed preflight as "nothing to do".
        # UnicodeError: the scan's raiser-side probes decode with the strict
        # POSIX handler and do not all normalize a decode fault to the seam type
        # (#380); it is the same scan failure, and letting it reach main()'s
        # backstop would empty stdout of the sessions receipt this arm protects.
        print(f"ctl window prune failed: {e}", file=sys.stderr)
        windows, survived, unverifiable = [], [], []
        scan_error = str(e)
    if args.json:
        machine.emit(
            cleanup_document(
                dry_run=args.dry_run,
                killed=killed,
                live=live,
                unknown=unknown,
                windows=windows,
                windows_survived=survived,
                windows_unverifiable=unverifiable,
                scan_error=scan_error,
            )
        )
        return 0
    if args.dry_run:
        if not killed and not windows:
            print("nothing to clean up")
        else:
            for run_id in killed:
                print(f"would kill session bmad-loop-{run_id}")
            for name in windows:
                print(f"would close ctl window {name}")
        if live:
            print(f"leaving {len(live)} live session(s) untouched")
        return 0
    # The count now excludes non-removals, so on stdout alone a smaller number is
    # indistinguishable from a quieter sweep — and `cleanup > log` keeps only
    # stdout. The marker travels with the count; the names stay on stderr, the
    # unverifiable_pid precedent.
    unaccounted = len(survived) + len(unverifiable)
    print(
        f"removed {len(killed)} session(s), {len(windows)} ctl window(s)"
        + (f" ({unaccounted} not verified — see stderr)" if unaccounted else "")
    )
    # Only ever printed when a kill did not verifiably land — silence on the
    # normal path, and the count above now excludes these rather than counting
    # them as removed (#435). Both are retried by the next cleanup.
    if survived:
        # Same wording as the TUI toast: one claim, one phrase, so an operator
        # moving between the two surfaces is reading the same thing.
        print(f"ctl window(s) still open after the kill: {', '.join(survived)}", file=sys.stderr)
    if unverifiable:
        # Not "killed but unverifiable": kill_window is a silent no-op on a
        # transport failure, so whether the kill even reached the server is part
        # of what is unknown here.
        print(
            f"ctl window(s) kill attempted, outcome unverifiable: {', '.join(unverifiable)}",
            file=sys.stderr,
        )
    if live:
        print(f"left {len(live)} live session(s) untouched")
    return 0


def _dir_size(path: Path) -> int:
    """Best-effort total bytes under ``path``, never crossing a redirect out of
    it — see :func:`walk_files_unlinked` for why plain ``os.walk`` is not enough.
    Sizes with ``lstat``, so a symlinked file counts as the link it is."""
    total = 0
    for f in walk_files_unlinked(path):
        try:
            total += f.lstat().st_size
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
    protected: list[str] = []
    for run_dir in runs.list_run_dirs(project):
        if run_dir.name in keep:
            protected.append(run_dir.name)
        elif runs.reclaimable(run_dir):
            reclaimable.append(run_dir)
        else:
            protected.append(run_dir.name)

    past = {
        p.name
        for p in runs.runs_past_retention(
            reclaimable, keep_n=retain, keep_days=pol.cleanup.retention_days
        )
    }

    freed = 0
    worktrees: list[str] = []
    trimmed: list[str] = []
    archived: list[str] = []
    deleted: list[str] = []
    unverifiable: list[str] = []
    for run_dir in reclaimable:
        if runs.live_session_may_be_ours(project, run_dir.name):
            # `reclaimable` is keyed on engine pid liveness, so an orphan — engine
            # dead, agent session still live — passes it, and everything below this
            # point mutates: the worktree the session may still be working in, the
            # trimmed artifacts, and the run dir itself, which for an untagged
            # session is the only ownership proof a later prune can read (#419).
            # So the guard is the first thing in the loop, ahead of every mutation,
            # and the run is reported untouched rather than half-reclaimed.
            # `cleanup` clears the session — but for an untagged one it proves
            # ownership by this same run dir, so the operator confirms first
            # (`bmad-loop attach <id>`); the next `clean` then reclaims the run.
            protected.append(run_dir.name)
            if not args.json:
                print(
                    f"run {run_dir.name}: agent session still live — left untouched",
                    file=sys.stderr,
                )
            continue
        if runs.engine_liveness(run_dir) == "unknown":
            # warn-only: unknown never blocks cleanup, but say so before removal.
            # In JSON mode this lives in the document instead (unverifiable_pid),
            # leaving stderr empty.
            unverifiable.append(run_dir.name)
            if not args.json:
                print(
                    f"run {run_dir.name}: engine may still be live (unverifiable pid)",
                    file=sys.stderr,
                )
        # measure before mutating so the reclaim estimate holds for --dry-run too.
        # Sized over `heavy_run_entries`, not over "worktrees" alone: that is the
        # exact set `trim_run_dir` removes, so the estimate cannot go stale the
        # next time an entry joins it (the verifier stream store did).
        heavy_bytes = sum(_dir_size(p) for p in runs.heavy_run_entries(run_dir) if p.is_dir())
        run_bytes = _dir_size(run_dir)
        # collect, never print-as-you-mutate: the document is emitted once at the
        # end, so every per-item line has to survive the loop as data
        run_worktrees = runs.reconcile_orphan_worktrees(repo, run_dir, dry_run=dry)
        for wt in run_worktrees:
            worktrees.append(str(wt))
            if not args.json:
                print(f"{'would remove' if dry else 'removed'} worktree {wt}")
        if run_dir.name in past:
            freed += run_bytes
            shrunk = runs.trim_run_dir(run_dir, dry_run=dry)  # shrink before archiving
            try:
                if args.hard or not pol.cleanup.archive_old:
                    if not dry:
                        runs.delete_run(project, run_dir)
                    deleted.append(run_dir.name)
                else:
                    if not dry:
                        runs.archive_run(project, run_dir)
                    archived.append(run_dir.name)
            except runs.LiveSessionError:
                # A session appeared between the loop-top guard and here — a resume
                # of a stopped run, racing this clean. The chokepoint refused the
                # removal; record the run instead of letting one racing run abort
                # the whole invocation. Correct the estimate down to what actually
                # went. The wider race — every mutation in this loop against a
                # concurrent resume — is older than this guard (`reclaimable` is
                # sampled in the loop above and never re-read) and is tracked in
                # issue #533.
                freed += heavy_bytes - run_bytes
                # Classify by what happened, not by what was intended: the steps
                # above may already have taken this run's worktree and artifacts,
                # and `protected` means "left untouched" in the --json contract.
                # Only a run nothing reached is protected; one already shrunk is
                # trimmed, which is exactly the state it ends in.
                (trimmed if run_worktrees or shrunk else protected).append(run_dir.name)
                if not args.json:
                    print(
                        f"run {run_dir.name}: agent session appeared mid-clean — not removed",
                        file=sys.stderr,
                    )
        elif pol.cleanup.trim_artifacts:
            if runs.trim_run_dir(run_dir, dry_run=dry):
                freed += heavy_bytes
                trimmed.append(run_dir.name)

    # After the loop, so the counterparts the removals above already took are gone
    # from the enumeration rather than counted twice. Their bytes stay out of
    # `freed` on purpose: a state dir holds consumed event files and nothing else,
    # so sizing every one of them would buy kilobytes of accuracy for a walk of a
    # second tree. The count is the honest report of what went.
    swept = len(runs.reconcile_orphan_state_dirs(project, dry_run=dry))

    if args.json:
        machine.emit(
            clean_document(
                dry_run=dry,
                retain=retain,
                cleanup_policy=pol.cleanup,
                freed_bytes=freed,
                worktrees=worktrees,
                trimmed=trimmed,
                archived=archived,
                deleted=deleted,
                protected=protected,
                unverifiable_pid=unverifiable,
                state_dirs_swept=swept,
            )
        )
        return 0
    reclaimed = bool(worktrees or trimmed or archived or deleted)
    if reclaimed:
        head = "would reclaim" if dry else "reclaimed"
        print(
            f"{head} ~{_human_bytes(freed)}: {len(worktrees)} worktree(s), "
            f"{len(trimmed)} run(s) trimmed, {len(archived)} archived, {len(deleted)} deleted"
        )
        for name in archived:
            print(f"  archived {name} -> .bmad-loop/archive/{name}.tar.gz")
        for name in deleted:
            print(f"  deleted {name}")
    if swept:
        # its own line rather than a fifth count in the summary above, which is a
        # reclaimed-bytes report these dirs are deliberately outside of
        print(f"{'would sweep' if dry else 'swept'} {swept} orphaned run state dir(s)")
    if not reclaimed and not swept:
        print("nothing to reclaim")
    if protected:
        print(f"left {len(protected)} live/resumable run(s) untouched")
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
        # Failure-gated, not allowlisted (#678): ANY missing third-party module on
        # the TUI import chain (rich and pyte import before textual; a future dep
        # would too) means the [tui] extra is absent. Only a missing bmad_loop.*
        # submodule — a packaging defect, not an install state the hint can fix —
        # re-raises.
        if (e.name or "").partition(".")[0] != "bmad_loop":
            print(
                "error: the TUI requires optional dependencies — uv tool install 'bmad-loop[tui]'",
                file=sys.stderr,
            )
            return 1
        raise
    return run_tui(project)


def cmd_probe(args: argparse.Namespace) -> int:
    from . import probe as probe_mod
    from . import sanitize
    from .adapters.profile import ProfileError, get_profile

    project = _project(args)
    # What this invocation actually produces — the operator messages below say
    # "report" only when a report was rendered; --json renders a document.
    noun = "document" if args.json else "report"
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
        # Human-facing notice — stderr in JSON mode, where stdout is the document.
        print(
            f"  ok: unknown profile {args.cli!r}; reduced {noun} from --binary {args.binary}",
            file=sys.stderr if args.json else sys.stdout,
        )

    if profile is not None and profile.hookless:
        print(
            f"{profile.name}: hookless HTTP/SSE profile — probe-adapter finalizes "
            "tmux/transcript-driven CLIs (hook dialects, transcript shapes) and has "
            "nothing to collect here. The HTTP contract is documented in the "
            "opencode_http adapter (src/bmad_loop/adapters/opencode_http.py).",
            file=sys.stderr,
        )
        return 1

    # Pseudonymize the one identifier-shaped value the probe KNOWS is the
    # user's (diagnose aliases the same value, ns="project"): the project
    # directory name, which would otherwise pass the location redaction's
    # identifier gate verbatim. Registration is skipped when the name collides
    # with a token that legitimately appears in the report (the CLI or binary
    # name) — otherwise the guard's repair pass would rewrite every occurrence
    # of that legitimate token into the alias and mangle the report.
    pseudo = sanitize.Pseudonymizer()
    legit_tokens = {args.cli, Path(args.binary).name if args.binary else ""}
    if profile is not None:
        legit_tokens.add(Path(profile.binary).name)
    if project.name not in legit_tokens:
        pseudo.alias(project.name, ns="project")

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
            pseudo=pseudo,
        )
    else:
        finding = probe_mod.scan(
            cli=args.cli, profile=profile, project=project, hints=hints, pseudo=pseudo
        )

    # One or the other, never both: --json selects the pure JSON document
    # (machine.py contract), otherwise the human-readable markdown report.
    # Either way the renderer runs the egress self-check over its own rendered
    # bytes and raises instead of returning tainted output.
    repairs: list[tuple[str, int]] = []
    try:
        if args.json:
            report = probe_mod.render_json(finding, pseudo=pseudo, repairs=repairs)
        else:
            report = probe_mod.render_markdown(finding, pseudo=pseudo, repairs=repairs)
    except probe_mod.LeakDetected as e:
        # Fail closed BEFORE any egress: message → stderr, stdout stays empty,
        # no partial --out file, exit != 0 (the machine.py error shape).
        print(
            f"FAIL: refusing to emit — leak self-check fired: {', '.join(e.rules)}",
            file=sys.stderr,
        )
        print(
            "hint: sensitive[project:<alias>] is your project directory name; any "
            "other rule means PII/secret-shaped content reached the report — please "
            "report the rule names above as a bmad-loop bug",
            file=sys.stderr,
        )
        return 1

    if repairs:
        merged: dict[str, int] = {}
        for label, count in repairs:
            merged[label] = merged.get(label, 0) + count
        summary = ", ".join(f"{label} x{count}" for label, count in sorted(merged.items()))
        print(
            f"warning: leak backstop pseudonymized {sum(merged.values())} stray "
            f"occurrence(s) ({summary}) — a per-field routing gap in bmad-loop; "
            "please include this line in your bug report",
            file=sys.stderr,
        )

    # Every `ok:` trailer is human-facing chatter, so in JSON mode it goes to
    # stderr — stdout is the document alone, or empty when --out took it.
    trailers = sys.stderr if args.json else sys.stdout
    if args.out:
        out_path = Path(args.out)
        if args.json:
            machine.write_document(out_path, report)
        else:
            out_path.write_text(report, encoding="utf-8")
        print(
            f"  ok: {noun} written to {out_path} ({len(finding.warnings)} warning(s))",
            file=trailers,
        )
    else:
        if args.json:
            machine.emit_document(report)
        else:
            print(report)
        print(
            f"  ok: {finding.mode} {noun} for {args.cli} ({len(finding.warnings)} warning(s))",
            file=trailers,
        )
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
    diag = diagnostics.collect(
        run_dirs, pseudo=pseudo, cap=args.max_journal_entries, project=project
    )
    repairs: list[tuple[str, int]] = []
    fail_rules: list[str] | None = None
    report = ""
    try:
        # Exactly one render — JSON mode skips render_markdown entirely. The
        # warrant has two halves, and only the first is about coverage:
        #   FIELDS: render_json's asdict() covers every field the markdown report
        #     merely samples, so nothing escapes the guard by not being rendered.
        #   ENCODING: json.dumps is NOT a faithful carrier of the bytes the guard
        #     matches on — it doubles backslashes and (by default) escapes
        #     non-ASCII, which silently defeated the absolute-home-path and
        #     sensitive-value rules that the markdown pass used to catch. That is
        #     closed at the source, not here: sanitize._ABS_HOME_RE now matches the
        #     escaped separator too, and render_json dumps with ensure_ascii=False.
        # So the JSON self-check is a superset in field coverage and equal in
        # encoding fidelity — never rely on the field half alone.
        # Dropping the second render also fixes a live double-count: both renders
        # extend the same `repairs` list, inflating the warning ~2x.
        if args.json:
            report = diagnostics.render_json(diag, pseudo=pseudo, repairs=repairs)
        else:
            report = diagnostics.render_markdown(diag, pseudo=pseudo, repairs=repairs)
    except diagnostics.LeakDetected as e:
        fail_rules = e.rules

    if args.legend:
        # Written even when the self-check refused: the legend is what decodes a
        # sensitive[<ns>:<alias>] rule name, and it never leaves this machine.
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

    if fail_rules is not None:
        # The output tripped the final self-check — fail closed, emit no dump.
        print(
            f"FAIL: refusing to emit — leak self-check fired: {', '.join(fail_rules)}",
            file=sys.stderr,
        )
        print(
            "hint: sensitive[<ns>:<alias>] names a pseudonymized identifier; rerun with "
            "--legend FILE to decode it locally (never share the legend), and report the "
            "rule names above as a bmad-loop bug",
            file=sys.stderr,
        )
        return 1

    if repairs:
        merged: dict[str, int] = {}
        for label, count in repairs:
            merged[label] = merged.get(label, 0) + count
        summary = ", ".join(f"{label} x{count}" for label, count in sorted(merged.items()))
        print(
            f"warning: leak backstop pseudonymized {sum(merged.values())} stray "
            f"occurrence(s) ({summary}) — a per-field routing gap in bmad-loop; "
            "please include this line in your bug report",
            file=sys.stderr,
        )

    if args.out:
        out_path = Path(args.out)
        if args.json:
            # Verbatim, validated, newline-terminated — byte-identical to the
            # stdout form, and the same bytes render_json's self-check verified.
            machine.write_document(out_path, report)
        else:
            out_path.write_text(report, encoding="utf-8")
        print(
            f"  ok: sanitized diagnostics for {len(diag.runs)} run(s) written to {out_path}",
            # JSON mode must leave stdout empty even when the document went to a
            # file; text mode keeps the trailer on stdout, as before.
            file=sys.stderr if args.json else sys.stdout,
        )
    elif args.json:
        # Verbatim: these are the exact bytes render_json's self-check verified.
        machine.emit_document(report)
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


def cmd_relay(args: argparse.Namespace) -> int:
    """``bmad-loop relay <Event>`` — the hook relay as an installed console script.

    **Nothing points at it yet.** ``init`` still registers the copied workspace
    relay (``install._hook_command`` emits ``<interpreter> <project>/.bmad-loop/
    bmad_loop_hook.py <Event>``), so no installed hook reaches this handler today;
    it is the target #461 Phase 2 retargets those registrations to, and that move
    carries its own obligation — see the COUPLING note on ``hooks.relay-present``,
    which must be retargeted rather than dropped in the same change. Said here
    because a console script that exists and is documented reads as the live path,
    and an operator debugging a lost Stop needs to know which relay actually ran.

    Total by contract, unlike every other handler: a coding CLI runs this INSIDE
    the session whose completion it reports, and several of them surface a
    non-zero hook exit as a failed tool call in that session. So nothing here
    escalates. :func:`events.relay` already swallows the relay-level failures
    (unset env, garbage stdin, a hostile events dir); the backstop below covers
    the rest — an unexpected exception is a bug in this file, and a bug must not
    be the reason a run's Stop signal turns into a broken session. It reports on
    stderr, never stdout: hook stdout is parsed by the host.

    Dispatched from ``main()`` BEFORE its shared ``try``/``except`` on purpose —
    see the comment there.
    """
    try:
        return events.relay(args.event, sys.stdin)
    except Exception as e:
        # Deliberately broad, and deliberately not re-raised: the alternative to a
        # diagnostic line here is a traceback on the host's hook channel plus a
        # non-zero rc. Narrower than the BaseException it could be — a Ctrl+C or a
        # SystemExit is not a relay-level problem and keeps its own exit path.
        print(f"bmad-loop relay: {type(e).__name__}: {e}", file=sys.stderr)
        return ExitCode.OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bmad-loop",
        description="Deterministic orchestrator for the BMAD implementation phase",
    )
    parser.add_argument("--version", action="version", version=f"bmad-loop {__version__}")
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
        help="CLI profile(s) to register hooks for (claude | codex | gemini | copilot | "
        "antigravity | opencode-http (alias: opencode) | custom; "
        "repeatable; default: profiles referenced by .bmad-loop/policy.toml, or claude)",
    )
    init_p.add_argument(
        "--no-skills",
        dest="skills",
        action="store_false",
        help="skip installing the bundled bmad-loop-* skills (hooks/policy only)",
    )
    init_p.add_argument(
        "--force-skills",
        action="store_true",
        help="overwrite bmad-loop-* skill dirs that already exist (default: skip them)",
    )
    validate_p = add("validate", cmd_validate, "preflight checks; exit non-zero on failure")
    validate_p.add_argument(
        "--spec",
        metavar="FOLDER",
        help="validate stories mode against this epic spec folder's stories.yaml "
        "(overrides [stories].source; skips the sprint-status gate)",
    )
    machine.add_json_flag(validate_p, "check findings")

    mux_p = add(
        "mux",
        cmd_mux,
        "list terminal-multiplexer backends + selection; `mux set <name>` persists a choice",
    )
    mux_p.add_argument(
        "action",
        nargs="?",
        choices=("set",),
        help="set: persist a backend choice into .bmad-loop/policy.toml",
    )
    mux_p.add_argument("name", nargs="?", help="backend name to persist (see the listing)")
    mux_p.add_argument(
        "--clear",
        action="store_true",
        help="with set: remove the persisted choice (back to auto-select)",
    )
    mux_p.add_argument(
        "--force",
        action="store_true",
        help="with set: persist a name not registered in this process (e.g. a plugin "
        "backend that only registers on the target machine)",
    )

    add(
        "adapters",
        cmd_adapters,
        "list registered coding-CLI adapter kinds + which profiles select them",
    )

    probe_p = add(
        "probe-adapter",
        cmd_probe,
        "collect + sanitize adapter-finalization data for a coding CLI",
        aliases=["collect-adapter-data"],
    )
    probe_p.add_argument(
        "cli",
        help="CLI profile name (claude | codex | gemini | copilot | antigravity | custom; "
        "opencode-http is HTTP-driven — nothing to probe)",
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
    probe_p.add_argument(
        "--out",
        help="write the output (report, or the JSON document with --json) to this file instead of stdout",
    )
    machine.add_json_flag(probe_p, "probe finding")
    probe_p.add_argument("--keep-temp", action="store_true", help=argparse.SUPPRESS)

    run_p = add("run", cmd_run, "run the orchestration loop")
    run_p.add_argument(
        "--spec",
        metavar="FOLDER",
        help="force stories mode: dispatch the epic spec folder's stories.yaml by "
        "folder+id (overrides [stories].source)",
    )
    run_p.add_argument("--epic", type=int, help="only stories from this epic (sprint mode)")
    run_p.add_argument(
        "--story",
        help="story: E-S / E.S (split suffix ok, e.g. 2-6a), a slug fragment, "
        "or full key (sprint mode); a story id (stories mode)",
    )
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
        "--restore-patch",
        metavar="PATH",
        help="intent-gap patch-restore (#2564): re-arm the spec to `in-review` and "
        "re-apply this saved patch before the re-drive, resuming review on the "
        "attempted change instead of re-implementing (hand-driven; the interactive "
        "agent supplies it via resolution.json)",
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
    machine.add_json_flag(decisions_p, "pending decisions")

    confirm_p = add(
        "confirm",
        cmd_confirm,
        "complete a story parked at awaiting-operator once its external actions are done",
    )
    confirm_p.add_argument(
        "story_key",
        nargs="?",
        help="the parked story to confirm (omit with --list)",
    )
    confirm_p.add_argument(
        "--list",
        action="store_true",
        help="list the parked stories and what each owes, without confirming any",
    )
    confirm_p.add_argument(
        "--yes",
        action="store_true",
        help="skip the per-action acknowledgment prompts",
    )
    confirm_p.add_argument(
        "--reverify",
        action="store_true",
        help="re-run the project's [verify] commands first; a failure blocks the confirmation",
    )
    machine.add_json_flag(confirm_p, "stories awaiting operator actions")

    list_p = add("list", cmd_list, "list runs/sweeps with their short ref", aliases=["ls"])
    machine.add_json_flag(list_p, "run listing")

    status_p = add("status", cmd_status, "show run + sprint state")
    status_p.add_argument("run_id", nargs="?")
    machine.add_json_flag(status_p, "run state")

    diag_p = add(
        "diagnose",
        cmd_diagnose,
        "emit a sanitized diagnostic dump of a run/sweep to hand to maintainers",
        aliases=["diag"],
    )
    diag_p.add_argument("run_id", nargs="?", help="run ref (default: latest)")
    diag_p.add_argument("--all", action="store_true", help="dump every run in the project")
    diag_p.add_argument(
        "--out",
        help="write the output (report, or the JSON document with --json) to this file instead of stdout",
    )
    machine.add_json_flag(diag_p, "diagnostic dump")
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
    stop_grp = stop_p.add_mutually_exclusive_group()
    stop_grp.add_argument(
        "--graceful",
        action="store_true",
        help="finish the in-flight item (through commit), then stop cleanly and stay "
        "resumable — instead of the hard SIGTERM stop; also suppresses pending auto-sweeps",
    )
    stop_grp.add_argument(
        "--cancel-graceful",
        action="store_true",
        help="cancel a pending --graceful request (the run keeps going)",
    )

    delete_p = add("delete", cmd_delete, "delete a run directory")
    delete_p.add_argument("run_id")
    delete_p.add_argument(
        "--force", action="store_true", help="stop the run first if it is still live"
    )

    archive_p = add("archive", cmd_archive, "compress a run into .bmad-loop/archive and remove it")
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
    machine.add_json_flag(cleanup_p, "sessions and ctl windows removed, or that would be")

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
    machine.add_json_flag(clean_p, "what was reclaimed, or would be")

    tui_p = add(
        "tui",
        cmd_tui,
        "interactive dashboard (needs `uv tool install 'bmad-loop[tui]'`)",
    )
    tui_p.add_argument(
        "--low-frame-rate",
        action="store_true",
        help="cap to 15fps + disable animations (TEXTUAL_FPS / TEXTUAL_ANIMATIONS) — "
        "fixes repaint tearing over slow/SSH links; also settable via [tui] low_frame_rate",
    )

    # Registered directly rather than through `add()`: `relay` takes no --project.
    # It is handed a run directory by the engine (via the session env) and must
    # work with no project state at all — see the dispatch below.
    relay_p = sub.add_parser(
        "relay",
        help="write one session event file from a coding-CLI hook payload on stdin "
        "(a hook target for machines, not a command to run by hand)",
    )
    relay_p.add_argument(
        "event",
        nargs="?",
        default="Unknown",
        help="canonical event name (Stop, SessionStart, …); the hooks always pass one",
    )
    relay_p.set_defaults(func=cmd_relay)

    args = parser.parse_args(argv)
    # `relay` dispatches HERE, ahead of everything below, and the placement is the
    # contract rather than an optimization. A coding CLI runs `bmad-loop relay Stop`
    # inside the session whose completion it reports, and a hook that exits non-zero
    # is surfaced by several hosts as a failed tool call in that session — so the
    # `except` arms below, which print `error: …` and return 1 (or 130), are exactly
    # the outcome the hook contract forbids. `_configure_mux(_project(args))` is
    # skipped for the same reason and one more: relay touches neither mux nor policy,
    # and a project whose policy.toml is broken must still be able to report that its
    # session stopped. `cmd_relay` is total, so nothing is lost by not wrapping it.
    if args.func is cmd_relay:
        return cmd_relay(args)
    try:
        # Install the policy [mux] backend choice before dispatch: several
        # handlers (probe/diagnose/attach/stop/cleanup/tui) reach the mux
        # without ever loading policy, so this is the one reliable seam.
        _configure_mux(_project(args))
        return args.func(args)
    except (
        bmadconfig.BmadConfigError,
        sprintstatus.SprintStatusError,
        policy_mod.PolicyError,
        verify.GitError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return ExitCode.FAILURE
    except KeyboardInterrupt:
        # Ctrl+C on the residual surface outside engine.run() (config load, engine
        # construction, a handler with no run loop). A KeyboardInterrupt is a
        # BaseException, so the broad `except Exception` below never caught it.
        # Uncaught it already ends at 130 — CPython re-raises SIGINT (128+2) — but as
        # death-by-signal with a bare traceback dumped after any partial --json stdout.
        # Catch it for a clean, intentional exit(130): one line on stderr, nothing on
        # stdout. The in-run interrupt is engine.run()'s own clean RunStopped (see
        # engine.py) and never reaches here.
        print("interrupted", file=sys.stderr)
        return ExitCode.INTERRUPTED
    except Exception as e:
        # backstop for the residual surface outside engine.run() (config load,
        # engine construction, render/notify): never let an unexpected exception
        # die to the parked control pane with a bare traceback.
        print(f"error: {e}", file=sys.stderr)
        return ExitCode.FAILURE


if __name__ == "__main__":
    sys.exit(main())
