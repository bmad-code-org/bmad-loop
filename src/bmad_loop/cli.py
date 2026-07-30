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
from pathlib import Path

from . import (
    __version__,
    bmadconfig,
    decisions,
    deferredwork,
    install,
    machine,
)
from . import policy as policy_mod
from . import (
    resolve,
    runs,
    sprintstatus,
)
from . import stories as stories_mod
from . import (
    verify,
)
from .adapters.base import CodingCLIAdapter
from .checks import Finding, ValidationReport

# The --json document builders live in documents.py (the library-level projection
# layer a non-CLI frontend imports). The schema constants are re-exported rather
# than used here — `cli.STATUS_SCHEMA_VERSION` and friends are the published names
# consumers and tests already resolve — so each carries a noqa: without it ruff
# F401 autofixes the re-export away and every `cli.*_SCHEMA_VERSION` reference
# breaks. Keep the noqa when adding a command.
from .documents import CLEAN_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import CLEANUP_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import DECISIONS_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import LIST_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import STATUS_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import VALIDATE_SCHEMA_VERSION  # noqa: F401 — re-export
from .documents import (
    clean_document,
    cleanup_document,
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
from .runs import RUNS_DIR
from .stories_engine import StoriesEngine
from .sweep import SweepEngine

POLICY_FILE = policy_mod.POLICY_FILE


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


ROLES = ("dev", "review", "triage")


def _make_adapters(project: Path, run_dir: Path, policy) -> dict[str, CodingCLIAdapter]:
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
                    extra_args=cfg.extra_args,
                    usage_grace_s=cfg.usage_grace_s,
                    stop_without_result_nudges=cfg.stop_without_result_nudges,
                )
                try:
                    by_cfg[key] = (
                        OpencodeDevAdapter(**common, paths=paths)
                        if synthesizes
                        else OpencodeHttpAdapter(**common)
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
                        except Exception:  # noqa: BLE001 — diagnosing must not mask the refusal
                            version = None
                        raise SystemExit(
                            f"error: multiplexer backend {type(mux).__name__} is not usable on "
                            f"this host (reported version: {version}); its transport binary is "
                            "missing, the version is unsupported, or a required helper is "
                            "absent (psmux needs `pwsh` on PATH); see `bmad-loop diagnose`"
                        )
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


def _platform_preflight() -> list[Finding]:
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
    except Exception as e:  # noqa: BLE001 — selection or readiness must not abort validate
        found.append(Finding("mux.preflight", "problem", f"multiplexer preflight failed: {e}"))

    try:
        infos = detect_multiplexers()
    except Exception:  # noqa: BLE001 — detection is advisory; never break validate
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
        # detail keeps the raw enum, not _mux_reason_label's prose: the label is
        # wording ("set by [mux] backend in .bmad-loop/policy.toml"), the enum is
        # the value MuxBackendInfo.reason actually carries.
        found.append(
            Finding(
                "mux.selection",
                "ok",
                f"multiplexer selection {_mux_reason_label(chosen.reason)}",
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
    except Exception as e:  # noqa: BLE001 — a bad BMAD_LOOP_PROCESS_HOST must report, not crash
        found.append(Finding("host.process", "problem", f"process host preflight failed: {e}"))

    return found


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
    profile_by_name: dict[str, object] = {}
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

    stories_on, spec_folder = _stories_mode(args, pol)
    if paths:
        if stories_on:
            _validate_stories_queue(
                project, paths, spec_folder, [p.skill_tree for p in profiles], report
            )
        else:
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

    for tool in dict.fromkeys(p.binary for p in profiles):
        if shutil.which(tool):
            report.ok("adapter.binary", f"{tool} found", {"binary": tool})
        else:
            report.fail("adapter.binary", f"{tool} not found on PATH", {"binary": tool})

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

    base_trees = [p.skill_tree for p in profiles]
    base_problems = install.missing_base_skills(project, base_trees)
    if profiles and not base_problems:
        # Name the primitive that actually resolved, not a hardcoded era: on an
        # upgraded project this is the operator's confirmation that the rename was
        # picked up (and, across trees, that both picked up the same one).
        resolved = list(
            dict.fromkeys(
                name
                for tree in dict.fromkeys(base_trees)
                if (name := install.resolve_dev_primitive(project, tree)) is not None
            )
        )
        report.ok(
            "skills.base",
            f"upstream skills present ({' + '.join(resolved)} + review hunters)",
            {
                "trees": list(dict.fromkeys(base_trees)),
                "dev_primitive": resolved,
            },
        )
    report.extend(base_problems)
    report.extend(install.dev_primitive_warnings(project, base_trees))

    if getattr(args, "json", False):
        # getattr, not args.json: cmd_validate is called directly by tests (and by
        # anything holding a hand-built Namespace) that predate the flag.
        machine.emit(validate_document(report, stories_on, spec_folder))
    else:
        report.render()
    return 0 if report.passed else 1


def _mux_reason_label(reason: str) -> str:
    """Human wording for a MuxBackendInfo.reason, shared by `mux` and validate."""
    return {
        "env": "forced by BMAD_LOOP_MUX_BACKEND",
        "policy": f"set by [mux] backend in {POLICY_FILE}",
        "platform-default": f"platform default for {sys.platform}",
        "first-match": "first available platform match",
        "fallback": "fallback (no registered backend is available)",
    }.get(reason, reason)


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
    if os.environ.get("BMAD_LOOP_MUX_BACKEND"):
        print(
            "note: BMAD_LOOP_MUX_BACKEND is set in this shell and outranks the persisted choice",
            file=sys.stderr,
        )
    policy_mod.write_mux_backend(path, args.name)  # a junk name raises PolicyError → main()
    print(f'mux backend set to "{args.name}" in {path}')
    return 0


def _require_base_skills(project: Path, pol, *, require_stories: bool = False) -> bool:
    """Preflight the upstream skills the orchestrator drives (the dev primitive —
    bmad-build-auto, or a complete pre-rename bmad-dev-auto — plus the three review
    hunters it invokes inline).

    Returns True when everything is in place; otherwise prints the problems and
    returns False so the caller can abort before spawning any session (a missing
    skill would otherwise stall as an `Unknown command` until the run times out).
    A post-rename install left with only the forwarding shim fails here too: the
    shim's interactive migration gate would HALT the session with nothing written.

    ``require_stories`` additionally content-probes the resolved primitive for
    folder+id dispatch — stories mode needs a newer skill than sprint mode, so an
    older install must fail loudly here rather than HALT `no stories.yaml`-style at
    dispatch time."""
    from .adapters.profile import ProfileError, get_profile

    skill_trees = []
    for name in dict.fromkeys(pol.adapter.resolved(role).name for role in ROLES):
        try:
            skill_trees.append(get_profile(name, project).skill_tree)
        except ProfileError:
            continue
    problems = install.missing_base_skills(project, skill_trees)
    if require_stories:
        problems += install.missing_stories_support(project, skill_trees)
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
        source="stories" if stories_on else "sprint-status",
        spec_folder=spec_folder if stories_on else "",
    )
    save_state(run_dir, state)
    runs.write_pid(run_dir)
    adapters = _make_adapters(project, run_dir, pol)
    journal.append(
        "run-start",
        run_id=run_id,
        source=state.source,
        adapter_dev=pol.adapter.resolved("dev").name,
        adapter_review=pol.adapter.resolved("review").name,
    )
    print(f"run {run_id} starting (attach: bmad-loop attach)")

    common = dict(
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
    engine: Engine = (
        StoriesEngine(**common, spec_folder=spec_folder) if stories_on else Engine(**common)
    )
    summary = engine.run()
    print(summary.render())
    return 0


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
    print(f"sweep {run_id} starting (attach: bmad-loop attach)")
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
    if (rc := _reject_bad_run_id(args.run_id)) is not None:
        return rc
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
    # drop any stale agent session so the run spins up a fresh one (a stopped or
    # interrupted run can leave a lingering bmad-loop-<id> session behind).
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
        story_common = dict(
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
        # stories mode is pinned in run state at launch, so resume rebuilds the
        # same picker (StoriesEngine) without any flag.
        engine = (
            StoriesEngine(**story_common, spec_folder=state.spec_folder)
            if state.source == "stories"
            else Engine(**story_common)
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
        decisions.apply_pre_answer(project, decision, option, date=today)
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
        print(
            f"  {key:40s} {task.phase:16s} dev×{task.attempt} review×{task.review_cycle} "
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
        return 1
    except Exception as e:
        # backstop for the residual surface outside engine.run() (config load,
        # engine construction, render/notify): never let an unexpected exception
        # die to the parked control pane with a bare traceback.
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
