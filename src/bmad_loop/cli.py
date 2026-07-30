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
from .platform_util import MAX_SEGMENT
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
    return Path(args.project).resolve()


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
    from .adapters.profile import ProfileError, get_profile

    profiles = []
    profile_by_name: dict[str, CLIProfile] = {}
    # Effective executable -> was it an [adapter] binary override. Keyed by the
    # binary actually spawned, not by profile: a `binary` override means the
    # profile's own name is NOT what runs, and probing it would report "claude
    # found" for a run that will spawn `cc`.
    binaries: dict[str, bool] = {}
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
        for role in ROLES:
            cfg = pol.adapter.resolved(role)
            profile = profile_by_name.get(cfg.name)
            if profile is not None:
                tool = cfg.binary or profile.binary
                binaries[tool] = binaries.get(tool, False) or bool(cfg.binary)
    except policy_mod.PolicyError as e:
        report.fail("policy", str(e))

    stories_on, spec_folder = _stories_mode(args, pol)
    if paths:
        if stories_on:
            _validate_stories_queue(
                project, paths, spec_folder, [p.skill_tree for p in profiles], report
            )
            _validate_closes_deferred(paths, report, spec_folder=spec_folder)
        else:
            _validate_closes_deferred(paths, report)
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

    report.extend(_platform_preflight())

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

    for tool, overridden in binaries.items():
        # Name the override in the message: a missing `cc` is a typo'd alias, not
        # a missing Claude Code install, and the fix differs.
        note = " (binary override)" if overridden else ""
        detail = {"binary": tool, "override": overridden}
        if shutil.which(tool):
            report.ok("adapter.binary", f"{tool} found{note}", detail)
        else:
            report.fail("adapter.binary", f"{tool} not found on PATH{note}", detail)

    for profile in profiles:
        if profile.hookless:
            report.ok(
                "adapter.hookless",
                f"{profile.name}: hookless (HTTP/SSE transport) — no hook registration needed",
                {"profile": profile.name},
            )
            # The HTTP adapter needs httpx, which ships as an optional extra —
            # surface a missing install here instead of at run start.
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

    # opencode config-file model ids are "provider/model" (see the opencode_http docstring);
    # a bare model name silently falls back to the server's default model, so warn
    # (advisory — a note, not a FAIL: an empty model legitimately means "default").
    if pol is not None:
        for role in ROLES:
            cfg = pol.adapter.resolved(role)
            prof = profile_by_name.get(cfg.name)
            if prof is not None and prof.hookless and cfg.model and "/" not in cfg.model:
                report.warn(
                    "policy.model-qualified",
                    f"{role} model {cfg.model!r} is not 'provider/model' — "
                    f"{prof.name} expects e.g. 'anthropic/claude-haiku-4-5'",
                    {"role": role, "model": cfg.model, "profile": prof.name},
                )

    base_findings = install.missing_base_skills(project, [p.skill_tree for p in profiles])
    # gated on PROBLEMS, not on any finding: an advisory review layer (a `when`
    # gate, a phrasing this check can't confirm, a broken override) is a warning
    # that must ride alongside the ok line rather than suppress it.
    if profiles and not any(f.severity == "problem" for f in base_findings):
        report.ok(
            "skills.base",
            "upstream skills present (bmad-dev-auto + review layers)",
            {"trees": list(dict.fromkeys(p.skill_tree for p in profiles))},
        )
    report.extend(base_findings)

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
            r.version or "-",
            f"* {_mux_reason_label(r.reason)}" if r.selected else "",
        )
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table), 0) for i, h in enumerate(header)]
    for row in (header, *table):
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
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


def _require_base_skills(project: Path, pol, *, require_stories: bool = False) -> bool:
    """Preflight the upstream skills the orchestrator drives (bmad-dev-auto + the
    review layers it invokes inline).

    Returns True when everything is in place; otherwise prints the problems and
    returns False so the caller can abort before spawning any session (a missing
    skill would otherwise stall as an `Unknown command` until the run times out).
    Warnings are printed but never abort — only ``problem`` findings block.

    ``require_stories`` additionally content-probes bmad-dev-auto for folder+id
    dispatch — stories mode needs a newer skill than sprint mode, so an older
    install must fail loudly here rather than HALT `no stories.yaml`-style at
    dispatch time."""
    from .adapters.profile import ProfileError, get_profile

    skill_trees = []
    for name in dict.fromkeys(pol.adapter.resolved(role).name for role in ROLES):
        try:
            skill_trees.append(get_profile(name, project).skill_tree)
        except ProfileError:
            continue
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
    ``bmad-dev-auto`` carries the folder+id dispatch flow stories mode needs (an
    older skill would HALT at dispatch). Appends findings to ``report`` in place;
    the probe carries its own remediation text ("update the bmm module")."""
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
        report.ok(
            "skills.stories-dispatch",
            "bmad-dev-auto supports folder+id dispatch (stories mode)",
            {"trees": list(dict.fromkeys(skill_trees))},
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


def _validate_closes_deferred(
    paths: bmadconfig.ProjectPaths,
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

    A ledger that cannot be read at all is reported as well. Staying quiet there
    is not the same trade as staying quiet about an unparseable manifest: the
    manifest is already reported by ``queue.stories-manifest``, while nothing
    else in ``validate`` reads the ledger, so silence meant reporting success for
    a preflight that checked nothing.

    Never a failure. The annotation is traceability, not a gate, so a stale
    reference must not be able to block a run that would otherwise start.
    """
    ledger = paths.deferred_work
    try:
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    except (OSError, UnicodeDecodeError) as e:
        # Split from the manifest read below, which is silent for a good reason
        # that does not apply here: nothing else in `validate` reads the ledger, so
        # returning quietly reported success for a preflight that checked nothing,
        # against the very file the run's closure will fail on
        # (#284 round-5 review, finding 6).
        report.warn(
            "deferred.ledger-unreadable",
            f"{ledger} cannot be read ({e}) — closes_deferred declarations were not "
            "checked against it, and the run's own closure will fail the same way",
            {"ledger": str(ledger), "error": str(e)},
        )
        return
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


def _add_cli_override_flags(parser: argparse.ArgumentParser) -> None:
    """Per-run coding-CLI selection, shared by `run` and `sweep`. Named after
    `probe-adapter`'s existing vocabulary: `cli` is the profile, `binary` the
    executable, so one word means one thing across the CLI surface."""
    parser.add_argument(
        "--cli", metavar="PROFILE", help="override [adapter] name for this run (e.g. codex)"
    )
    parser.add_argument(
        "--cli-binary",
        metavar="EXECUTABLE",
        help="override [adapter] binary for this run: spawn the same CLI under "
        "another name, e.g. a second account reachable as `cc`",
    )


def _apply_cli_overrides(pol, args, project: Path):
    """Fold ``--cli`` / ``--cli-binary`` into the policy, returning a new Policy.

    Applied to the *policy object* before the run composes, not threaded into the
    engine, so the RunState snapshot records what actually ran: `resume`, the
    ``--json`` adapter projection, per-task adapter stamping and the dry-run
    render all follow with no further wiring.

    A flag is a whole-run choice, so it wins over the per-stage tables rather
    than only the base: forcing a client or a binary and then silently keeping a
    stage pinned to another one would defeat the point of passing it. Clearing is
    surgical, and mirrors ``AdapterPolicy.resolved``'s own client-switch rule — a
    stage whose effective client the flag changes drops its client-specific
    fields (model, extra_args, binary) and keeps its timing knobs; a stage that
    already ran the forced client keeps everything but its now-redundant name.
    """
    from dataclasses import replace

    from .adapters.profile import ProfileError, get_profile

    name, binary = getattr(args, "cli", None), getattr(args, "cli_binary", None)
    # The same normalization policy.toml gets: trim, and refuse a present-but-
    # blank value rather than letting it read as unset (or, worse, become a
    # whitespace argv[0] that fails at spawn with an unrecognizable error).
    for flag, raw in (("--cli", name), ("--cli-binary", binary)):
        if raw is not None and not raw.strip():
            raise SystemExit(f"error: {flag} must not be blank")
    name, binary = (name or "").strip(), (binary or "").strip()
    if not name and not binary:
        return pol
    if name:
        # Fail here rather than at session spawn: the dry-run render resolves the
        # profile too, and an unknown name should not surface as a traceback.
        try:
            get_profile(name, project)
        except ProfileError as e:
            raise SystemExit(f"error: {e}") from e
    adapter = pol.adapter
    stages = {}
    for role in ROLES:
        stage = getattr(adapter, role)
        if name and (stage.name or adapter.name) != name:
            # the client changed under this stage: its model/extra_args/binary
            # were chosen for the old one and must not be carried over
            stages[role] = policy_mod.StageAdapterPolicy(
                usage_grace_s=stage.usage_grace_s,
                stop_without_result_nudges=stage.stop_without_result_nudges,
            )
            continue
        stages[role] = replace(
            stage,
            name=None if name else stage.name,
            binary=None if binary else stage.binary,
        )
    return replace(
        pol,
        adapter=replace(
            adapter,
            name=name or adapter.name,
            binary=binary or adapter.binary,
            # a forced client falls back to that CLI's own default model/flags,
            # the same reset a stage client switch performs
            model="" if name and name != adapter.name else adapter.model,
            extra_args=None if name and name != adapter.name else adapter.extra_args,
            **stages,
        ),
    )


def cmd_run(args: argparse.Namespace) -> int:
    if (rc := _reject_bad_run_id(args.run_id)) is not None:
        return rc
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = _apply_cli_overrides(policy_mod.load(_policy_path(project)), args, project)
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
        sweep_factory=_sweep_factory(project, paths),
        make_adapters=_make_adapters,
        engine_cls=Engine,
        stories_engine_cls=StoriesEngine,
    )
    print(f"run {composed.run_id} starting (attach: bmad-loop attach)")
    summary = composed.engine.run()
    print(summary.render())
    return 0


def _render_invocation(pol, project: Path, role: str, prompt: str) -> str:
    from .adapters.profile import get_profile

    cfg = pol.adapter.resolved(role)
    profile = get_profile(cfg.name, project)
    # The dry-run plan must name the executable the run would really spawn, so a
    # binary override is visible before any session starts.
    binary = cfg.binary or profile.binary
    if profile.hookless:
        # HTTP/SSE transport — there is no shell invocation to print. Render
        # the real sequence (per-session server spawn + API prompt) instead of
        # a fake argv that run would never execute.
        model = f" model={cfg.model}" if cfg.model else ""
        return (
            f"{binary} serve --hostname 127.0.0.1 --port <auto> "
            f'(cwd=<worktree>) → POST /session → prompt_async "{profile.render_prompt(prompt)}"'
            f"{model}"
        )
    extra = cfg.extra_args if cfg.extra_args is not None else profile.bypass_args
    argv = [
        binary,
        *profile.launch_args,
        f'"{profile.render_prompt(prompt)}"',
        *extra,
    ]
    if cfg.model:
        argv += [profile.model_flag, cfg.model]
    return " ".join(argv)


def _dry_run(
    paths: bmadconfig.ProjectPaths,
    pol,
    args: argparse.Namespace,
    stories_on: bool = False,
    spec_folder: str = "",
) -> int:
    if stories_on:
        return _dry_run_stories(paths, pol, args, spec_folder)

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
    for story in queue:
        print(f"\n  {story.key} (epic {story.epic}, status {story.status})")
        print(f"    dev:    {render('dev', f'/bmad-dev-auto {story.key}')}")
        print(f"    review: {render('review', '/bmad-dev-auto <done spec from dev>')}")
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
    folder = stories_mod.resolve_spec_folder(paths.project, spec_folder)
    # The real dispatch always uses the project-relative folder (the engine
    # relativizes it); render the identical string here so dry-run and run agree.
    rel = stories_mod.relativize_spec_folder(paths.project, spec_folder)
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
    print("linear schedule (list order — no depends_on, strictly serial):")
    for row in rows:
        print(f"\n  {row.position}. {row.id}  ({row.label}){_checkpoint_badge(row)}  {row.title}")
        # A spec_checkpoint story whose plan is not yet on disk dispatches leg 1
        # (Halt after planning + BMAD_LOOP_PLAN_HALT); mirror the real dispatch's
        # markers so dry-run does not under-report what run would emit.
        plan_halt = stories_mod.is_plan_halt_leg(row.spec_checkpoint, row.state)
        dispatch = f"/bmad-dev-auto Spec folder: {rel}. Story id: {row.id}."
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
) -> int:
    # The composition (run dir + state + pid + sweep.json + adapters + engine)
    # lives in runsetup; this stays compose -> render. SweepEngine and
    # _make_adapters are handed in from this module's namespace so the test suite's
    # `monkeypatch.setattr(cli, "SweepEngine"/"_make_adapters", ...)` still applies.
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
    )
    print(f"sweep {composed.run_id} starting (attach: bmad-loop attach)")
    summary = composed.engine.run()
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
    if (rc := _reject_bad_run_id(args.run_id)) is not None:
        return rc
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = _apply_cli_overrides(policy_mod.load(_policy_path(project)), args, project)

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
    fields: dict[str, object] = {
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
    # which tui.data classifies as INTERRUPTED.
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
        sweep_factory=_sweep_factory(project, paths),
        make_adapters=_make_adapters,
        engine_cls=Engine,
        stories_engine_cls=StoriesEngine,
        sweep_engine_cls=SweepEngine,
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
    # collapsed. The resolved absolute path is what gets latched.
    patch = verify.resolve_restore_path(raw, project).resolve()
    # Same trusted-roots shape as the frontmatter reconcile's spec_within_roots:
    # bmad-dev-auto saves the patch under implementation_artifacts, and artifact
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
    try:
        verify.commit_paths(
            paths.repo_root,
            f"chore(operator): confirm {story.story_key}",
            [spec, paths.sprint_status, record],
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
    from .tui.data import discover_runs  # import-safe: data.py has no textual imports

    project = _project(args)
    infos = discover_runs(project)  # oldest first
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
    if not args.json:
        for run_id in sorted(unknown):
            # warn-only: unknown never blocks cleanup (same wording as delete/archive).
            # Pruning kills the tmux session, never the engine pid, so the warning
            # holds after the fact too. In JSON mode this lives in the document
            # instead (sessions.unverifiable_pid), leaving stderr empty.
            print(f"run {run_id}: engine may still be live (unverifiable pid)", file=sys.stderr)
    windows = (
        launch.prunable_ctl_windows(project) if args.dry_run else launch.prune_ctl_windows(project)
    )
    if args.json:
        machine.emit(
            cleanup_document(
                dry_run=args.dry_run, killed=killed, live=live, unknown=unknown, windows=windows
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
        # measure before mutating so the reclaim estimate holds for --dry-run too
        wt_dir = run_dir / "worktrees"
        wt_bytes = _dir_size(wt_dir) if wt_dir.is_dir() else 0
        run_bytes = _dir_size(run_dir)
        # collect, never print-as-you-mutate: the document is emitted once at the
        # end, so every per-item line has to survive the loop as data
        for wt in runs.reconcile_orphan_worktrees(repo, run_dir, dry_run=dry):
            worktrees.append(str(wt))
            if not args.json:
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
            )
        )
        return 0
    if not worktrees and not trimmed and not archived and not deleted:
        print("nothing to reclaim")
    else:
        head = "would reclaim" if dry else "reclaimed"
        print(
            f"{head} ~{_human_bytes(freed)}: {len(worktrees)} worktree(s), "
            f"{len(trimmed)} run(s) trimmed, {len(archived)} archived, {len(deleted)} deleted"
        )
        for name in archived:
            print(f"  archived {name} -> .bmad-loop/archive/{name}.tar.gz")
        for name in deleted:
            print(f"  deleted {name}")
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
        if (e.name or "").partition(".")[0] in ("textual", "tomlkit"):
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
    diag = diagnostics.collect(run_dirs, pseudo=pseudo, cap=args.max_journal_entries)
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
    _add_cli_override_flags(run_p)
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
    _add_cli_override_flags(sweep_p)
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

    args = parser.parse_args(argv)
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
