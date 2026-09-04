"""Run-composition layer — chiefly `config_digest`, the integrity pin over the
agent-writable config that reaches HOST code execution (issue #461 point 4).

The digest's whole job is to be exact in two directions at once: it must move for
every field that reaches exec, and hold still for everything else. A digest that
under-covers lets a mid-run rewrite through the auto-sweep gate; one that
over-covers refuses every auto-sweep after a `[limits]` live-edit #189 documents
as supported. Both halves are pinned below.

The second concern here is composition atomicity: `compose_run` and
`compose_sweep` publish a run dir before they can know the run will start, and
`make_adapters` raises `SystemExit` from six sites after that point. The unwind
that keeps a failed launch from stranding a resumable-looking empty run is pinned
at the end of the file.
"""

import dataclasses
import os
import shutil
import threading
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from bmad_loop import bmadconfig
from bmad_loop import journal as journal_mod
from bmad_loop import policy as policy_mod
from bmad_loop import runs, runsetup
from bmad_loop.adapters.profile import ProfileError
from bmad_loop.journal import Journal, load_state, state_lock

# A profile overlay carrying the whole launch surface the digest covers. It lives
# under .bmad-loop/profiles/, inside the tree every driven session can write.
PROFILE = """\
name = "mycli"
binary = "mycli"
launch_args = ["-i"]
bypass_args = ["--yes"]
env = { FOO = "bar" }

[hooks]
dialect = "claude-settings-json"
config_path = ".mycli/settings.json"
events = { SessionStart = "SessionStart", Stop = "Stop" }
"""

POLICY = """\
[adapter]
name = "mycli"

[verify]
commands = ["ruff check .", "pytest -q"]
"""

PROFILE_REL = ".bmad-loop/profiles/mycli.toml"


@pytest.fixture
def pinned(tmp_path):
    """A project whose entire host-exec surface is expressible on disk: a policy
    naming the verify commands, and a profile overlay carrying the launch
    binary/args/env.

    Deliberately `tmp_path` rather than the `project` conftest sandbox: this is a
    pure-core unit test of `config_digest`, which reads only `.bmad-loop/`. It
    needs no git repo, no BMAD artifact dirs and no sprint board, so the sandbox's
    per-test copytree would buy nothing. The end-to-end gate behavior is tested on
    the real sandbox in tests/test_cli.py."""
    profiles = tmp_path / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "mycli.toml").write_text(PROFILE, encoding="utf-8")
    return tmp_path


def _digest(project, policy_text=POLICY) -> str:
    return runsetup.config_digest(policy_mod.loads(policy_text), project)


def _with_adapter_key(line: str) -> str:
    """POLICY with one more key inside its EXISTING `[adapter]` table — appending a
    second `[adapter]` header is a TOML redeclaration error, not an override."""
    return POLICY.replace('name = "mycli"', f'name = "mycli"\n{line}', 1)


def _rewrite_profile(project, old, new) -> None:
    """The mid-run profile rewrite a driven session can perform."""
    assert old in PROFILE, f"fixture drift: {old!r} no longer in PROFILE"
    (project / PROFILE_REL).write_text(PROFILE.replace(old, new), encoding="utf-8")


def test_digest_is_a_stable_sha256_over_an_unchanged_tree(pinned):
    """The null case, and the one that matters most in production: an unstable
    digest would refuse every auto-sweep in the product, not just a tampered one."""
    first = _digest(pinned)
    assert len(first) == 64 and int(first, 16) >= 0  # sha256 hex
    assert _digest(pinned) == first


def test_digest_ignores_a_benign_limits_edit(pinned):
    """Why this is field-scoped rather than a whole-file hash. #189 documents
    live-editing `[limits]` under a running loop as supported; a file hash would
    turn every such edit into a refused auto-sweep."""
    assert _digest(pinned, POLICY + "\n[limits]\ncache_read_weight = 0.5\n") == _digest(pinned)


def test_digest_ignores_the_hook_config_path(pinned):
    """Deliberate exclusion: the relay is issue #461's points 1-3 and is hardened
    on its own track. Folding it in would make the auto-sweep gate refuse after an
    ordinary `bmad-loop init` re-registration, which is not an attack."""
    before = _digest(pinned)
    _rewrite_profile(pinned, 'config_path = ".mycli/settings.json"', 'config_path = ".x/s.json"')
    assert _digest(pinned) == before


def test_digest_moves_on_a_rewritten_verify_command(pinned):
    """`[verify] commands` runs with shell=True on the host (verify.py), outside
    any session's sandbox."""
    assert _digest(pinned, POLICY.replace("pytest -q", "touch pwned")) != _digest(pinned)


def test_digest_preserves_verify_command_order(pinned):
    """They run in sequence, so a reorder is a different execution — a canonical
    that sorted them would let one be silently resequenced."""
    swapped = POLICY.replace('["ruff check .", "pytest -q"]', '["pytest -q", "ruff check ."]')
    assert _digest(pinned, swapped) != _digest(pinned)


@pytest.mark.parametrize(
    "old,new",
    [
        pytest.param('binary = "mycli"', 'binary = "rogue-cli"', id="binary"),
        pytest.param('launch_args = ["-i"]', 'launch_args = ["-i", "--evil"]', id="launch_args"),
        pytest.param('bypass_args = ["--yes"]', 'bypass_args = ["--all"]', id="bypass_args"),
        pytest.param('env = { FOO = "bar" }', 'env = { FOO = "evil" }', id="env"),
        # model_flag is an argv FLAG, not a value: an overlay that turns "--model"
        # into some other option changes what the CLI does with the model string.
        pytest.param(
            'binary = "mycli"', 'model_flag = "--exec"\nbinary = "mycli"', id="model_flag"
        ),
        # prompt_template reads like prompt payload but is an argv ELEMENT:
        # interactive_argv places render_prompt(spec.prompt) in the list, and the
        # template need not reference {prompt} at all. shlex.quote bounds it to one
        # token, which is still enough for the --opt=value form.
        pytest.param(
            'binary = "mycli"',
            'prompt_template = "--mcp-config=/tmp/evil.json"\nbinary = "mycli"',
            id="prompt_template",
        ),
    ],
)
def test_digest_moves_on_any_resolved_profile_launch_field(pinned, old, new):
    """The launch surface issue #461 names, and the reason the digest RESOLVES
    profiles instead of hashing `policy_snapshot`: not one of these fields appears
    in the snapshot, so a snapshot-only compare is blind to all four. Parametrized
    so a field quietly dropped from the canonical fails on its own row."""
    before = _digest(pinned)
    _rewrite_profile(pinned, old, new)
    assert _digest(pinned) != before


def test_digest_moves_on_rewritten_adapter_extra_args(pinned):
    """`extra_args` is the field that carries `--permission-mode bypassPermissions`,
    and `GenericAdapter.interactive_argv` prefers it over `profile.bypass_args`
    whenever it is set — so hashing the profile default alone leaves the flags the
    host CLI is actually launched with unpinned. It lives in policy.toml, which
    every driven session can write."""
    assert _digest(pinned, _with_adapter_key('extra_args = ["--yolo"]')) != _digest(pinned)


@pytest.mark.parametrize("role", ["dev", "review", "triage"])
def test_digest_moves_on_rewritten_per_stage_extra_args(pinned, role):
    """Per-stage `[adapter.<role>] extra_args` overrides the base for that role
    only, so a digest reading just the base would miss a rewrite aimed at one
    stage — and the review stage is the one that runs after the dev work lands."""
    staged = POLICY + f'\n[adapter.{role}]\nextra_args = ["--yolo"]\n'
    assert _digest(pinned, staged) != _digest(pinned)


def test_digest_separates_absent_extra_args_from_an_empty_override(pinned):
    """`None` means "fall back to profile.bypass_args"; `[]` means "launch with no
    flags at all". Two different command lines, so they must not collide — a
    canonical that coerced None to [] would let one be swapped for the other."""
    inherit = _digest(pinned)  # extra_args absent entirely
    explicit_none = _digest(pinned, _with_adapter_key("extra_args = []"))
    assert inherit != explicit_none


def test_digest_ignores_the_adapter_model(pinned):
    """The documented exclusion, pinned so it stays deliberate. `model` cannot
    introduce an argv token — it only fills the value slot behind `model_flag`,
    which IS pinned above — and including it would refuse an auto-sweep after an
    operator's mid-run model change in the TUI."""
    assert _digest(pinned, _with_adapter_key('model = "some-other-model"')) == _digest(pinned)


def test_digest_moves_when_the_transport_flips_to_hookless(pinned):
    """`hooks.dialect = "none"` reshapes the argv WHOLESALE rather than moving a
    token in it. (Since the adapter registry the field that picks the BUILDER is
    `profile.adapter` — pinned by its own test; `hookless` still decides what the
    opencode builder emits, which is what this row covers.) That builder's
    `_serve_argv` drops
    `launch_args`, the prompt and the `bypass_args` fallback and puts the literal
    "serve" at argv[1] — run with `cwd` at the workspace root. Against an
    interpreter `binary` (python/sh/node, the real program in `launch_args` —
    nothing forbids that shape) argv[1] is a script path resolved out of the tree
    every driven session can write, and the spawn precedes the health poll it
    fails. Pinning `binary` does not cover it, because the attacker inherits the
    binary rather than choosing it. None of the hook fields deleted here are
    hashed (see the `config_path` exclusion above), so only the transport moves."""
    before = _digest(pinned)
    _rewrite_profile(
        pinned,
        'dialect = "claude-settings-json"\nconfig_path = ".mycli/settings.json"\n'
        'events = { SessionStart = "SessionStart", Stop = "Stop" }',
        'dialect = "none"',
    )
    assert _digest(pinned) != before


def test_digest_covers_a_profile_overlay_that_did_not_exist_at_launch(tmp_path):
    """The create path, which is where two shipped tools got this exact class wrong:
    a protection that covers an EXISTING config file but not a MISSING one (Cursor
    CVE-2025-54135, "editing a dotfile requires approval but creating a new one
    doesn't"; Claude Code CVE-2026-25725, settings.json unprotected while absent, so
    the agent creates it and injects SessionStart hooks).

    This digest is structurally immune because it resolves through `get_profile`
    rather than hashing whatever files happen to be on disk: with no overlay the
    launch baseline covers the BUILTIN profile's fields, so a session creating
    `.bmad-loop/profiles/<name>.toml` mid-run changes the resolved binary and moves
    the digest. A refactor to "hash the profile files that exist" would pass every
    other test in this file and silently reintroduce the CVE shape."""
    (tmp_path / ".bmad-loop").mkdir(parents=True)
    policy_text = '[adapter]\nname = "claude"\n\n[verify]\ncommands = ["pytest -q"]\n'
    at_launch = _digest(tmp_path, policy_text)  # no overlay: resolves the builtin

    profiles = tmp_path / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    # First the non-vacuity half: an overlay for a name this run does not resolve
    # must NOT move the digest. Without this, the assertion below would also pass
    # for a digest that merely reacted to a file appearing.
    (profiles / "unused.toml").write_text(
        'name = "unused"\nbinary = "whatever"\n\n[hooks]\ndialect = "none"\n', encoding="utf-8"
    )
    assert _digest(tmp_path, policy_text) == at_launch

    # Hooked, matching the builtin's transport, so the mover here is the launch
    # surface itself rather than the `hookless` flip covered by its own test.
    (profiles / "claude.toml").write_text(
        'name = "claude"\nbinary = "rogue-cli"\n\n[hooks]\n'
        'dialect = "claude-settings-json"\nconfig_path = ".claude/settings.json"\n'
        'events = { SessionStart = "SessionStart", Stop = "Stop" }\n',
        encoding="utf-8",
    )
    assert _digest(tmp_path, policy_text) != at_launch


def test_digest_moves_on_a_widened_plugin_allowlist(pinned):
    """`[plugins] enabled` gates in-process Python import (plugins/trust.py) —
    a straight path from a workspace write to code inside the orchestrator."""
    assert _digest(pinned, POLICY + '\n[plugins]\nenabled = ["rogue"]\n') != _digest(pinned)


def test_digest_is_insensitive_to_plugin_allowlist_order(pinned):
    """`enabled` is a trust SET; listing the same two names the other way round is
    not a config change and must not refuse an auto-sweep."""
    both = POLICY + '\n[plugins]\nenabled = ["alpha", "beta"]\n'
    reordered = POLICY + '\n[plugins]\nenabled = ["beta", "alpha"]\n'
    assert _digest(pinned, both) == _digest(pinned, reordered)


def test_digest_is_invariant_to_tuple_vs_list_shapes(pinned):
    """The false-"changed" trap `cli._resume_paused_run`'s policy compare already
    documents: these fields are TUPLES on a live Policy and lists on anything that
    round-tripped through JSON. The canonical normalizes both to lists before
    hashing, so a digest can never move for shape alone — which would refuse every
    auto-sweep while looking exactly like a real tamper."""
    pol = policy_mod.loads(POLICY + '\n[plugins]\nenabled = ["alpha", "beta"]\n')
    assert isinstance(pol.verify.commands, tuple) and isinstance(pol.plugins.enabled, tuple)
    listy = dataclasses.replace(
        pol,
        verify=dataclasses.replace(pol.verify, commands=list(pol.verify.commands)),
        plugins=dataclasses.replace(pol.plugins, enabled=list(pol.plugins.enabled)),
    )

    assert runsetup.config_digest(listy, pinned) == runsetup.config_digest(pol, pinned)


def test_digest_covers_every_adapter_role(pinned):
    """A per-stage override points a role at a different profile, so hashing only
    the base adapter would leave the review and triage launch surfaces unpinned."""
    before = _digest(pinned)
    (pinned / ".bmad-loop" / "profiles" / "other.toml").write_text(
        PROFILE.replace('name = "mycli"', 'name = "other"').replace(
            'binary = "mycli"', 'binary = "other"'
        ),
        encoding="utf-8",
    )
    for role in runsetup.ROLES:
        assert _digest(pinned, POLICY + f'\n[adapter.{role}]\nname = "other"\n') != before


def test_digest_raises_on_an_unresolvable_profile(pinned):
    """ProfileError propagates by design: an unknown `[adapter] name` already
    aborts the run at make_adapters, so the digest must not paper over one by
    hashing a hole where the launch surface should be."""
    with pytest.raises(ProfileError):
        _digest(pinned, '[adapter]\nname = "no-such-cli"\n')


# --------------------------------------------------------- composition unwind

# A fixed, well-formed run id, so the assertions can name the two directories a
# composer publishes rather than fish them back out of the failed call.
RUN_ID = "20260812-101500-ab12"

# The message `make_adapters` raises for an unusable multiplexer — the one of its
# six SystemExit sites that is reachable in a run that launched fine, since
# `mux_usable` bottoms out in a live `shutil.which` on every call.
BOOM = "error: multiplexer backend TmuxBackend is not usable on this host"


class _NeverBuilt:
    """Engine stand-in for the composers' `*_cls` seams.

    Raises on construction rather than being a no-op: every test below fails at
    `make_adapters`, which both composers call before they build an engine, so a
    class that cannot be built is a second assertion that the failure landed where
    the test says it did."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("engine construction reached despite a failed make_adapters")


def _fake_paths(project):
    """A hand-built ProjectPaths rather than `bmadconfig.load_paths`, which would
    need a `_bmad/bmm/config.yaml` on disk. `paths` is read only when the engine is
    constructed — after `make_adapters` — so nothing here ever dereferences it."""
    return bmadconfig.ProjectPaths(
        project=project,
        implementation_artifacts=project / "impl",
        planning_artifacts=project / "plan",
    )


class _AcceptingEngine:
    """Engine stand-in that CAN be built.

    The sibling `_NeverBuilt` exists because every test around it aborts at
    `make_adapters`; the rows below are about what a composition that SUCCEEDS
    leaves on disk, so they need the opposite stand-in."""

    def __init__(self, *args, **kwargs):
        pass


def _split_root_paths(project):
    """`_fake_paths` with the one supported divergence: `repo_root` naming a code
    tree that is not the BMAD project dir (`isolation = "none"` plus a `repo_root:`
    key; `bmadconfig.worktree_isolation_conflict` refuses the other combination).

    `_fake_paths` leaves the two roots identical — as does the `project` fixture
    everywhere else — so without this no composition test can tell a `repo_root`
    that was WIRED from one that was hardcoded to `project`."""
    return bmadconfig.ProjectPaths(
        project=project,
        implementation_artifacts=project / "impl",
        planning_artifacts=project / "plan",
        repo_root=project / "code",
    )


def _accepting_adapters(*_a, **_k):
    return {role: None for role in runsetup.ROLES}


@pytest.mark.parametrize("run_type", ["run", "sweep"])
def test_composition_persists_the_code_root(tmp_path, run_type):
    """`RunState.repo_root` is written at launch and read back OUT OF PROCESS by
    `runs.rearm_escalation`, which has no `ProjectPaths` to consult — so the wiring
    from `paths.repo_root` into the state is the whole mechanism, and it is
    invisible everywhere the two roots coincide.

    Both composers, because they build the state independently: `compose_run` goes
    through `build_run_state` and `compose_sweep` constructs `RunState` inline, so
    one being wired says nothing about the other.

    Asserted on the PERSISTED state rather than the in-memory object: a re-arm
    reads `state.json` from a different process, so an in-memory-only value would
    satisfy an object assertion and still leave the consumer with nothing.

    Ablation: hardcode `repo_root=project` (or drop the argument) at either
    composer and that parametrization reddens alone.
    """
    paths = _split_root_paths(tmp_path)
    assert paths.repo_root != paths.project  # the fixture really does diverge

    if run_type == "run":
        composed = runsetup.compose_run(
            project=tmp_path,
            paths=paths,
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            epic_filter=None,
            story_filter=None,
            max_stories=None,
            stories_on=False,
            spec_folder="",
            sweep_factory=lambda _trigger, *, started: None,
            make_adapters=_accepting_adapters,
            engine_cls=_AcceptingEngine,
            stories_engine_cls=_AcceptingEngine,
            trusted_config_digest="deadbeef",
        )
    else:
        # NOT `_run_compose_sweep`: that helper bakes in `_fake_paths`, whose two
        # roots coincide, so the sweep leg would compose without the divergence and
        # the assertion below would hold for the wrong reason.
        composed = runsetup.compose_sweep(
            project=tmp_path,
            paths=paths,
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            repeat=None,
            max_cycles=None,
            trigger="auto",
            make_adapters=_accepting_adapters,
            sweep_engine_cls=_AcceptingEngine,
            trusted_config_digest="deadbeef",
        )

    persisted = load_state(composed.run_dir)
    assert persisted.repo_root == str(paths.repo_root)
    assert persisted.code_root == paths.repo_root
    assert persisted.code_root != Path(persisted.project)


@pytest.mark.parametrize("run_type", ["run", "sweep"])
def test_initial_state_and_pid_are_one_locked_publication(tmp_path, monkeypatch, run_type):
    """A rival explicit-id resume cannot enter after state.json becomes readable
    but before the fresh composer publishes engine.pid."""
    run_dir = runs.run_dir_for(tmp_path, RUN_ID)
    stamp_entered = threading.Event()
    rival_attempted = threading.Event()
    release_stamp = threading.Event()
    rival_observed: list[tuple[bool, bool]] = []
    errors: list[BaseException] = []
    real_file_lock = journal_mod.file_lock

    @contextmanager
    def observed_file_lock(path, *args, **kwargs):
        if threading.current_thread().name == "rival-resume":
            rival_attempted.set()
        with real_file_lock(path, *args, **kwargs):
            yield

    def paused_stamp(_project, _run_id, _digest):
        stamp_entered.set()
        assert release_stamp.wait(2)

    monkeypatch.setattr(journal_mod, "file_lock", observed_file_lock)
    monkeypatch.setattr(runs, "write_trusted_config_digest", paused_stamp)

    def compose() -> None:
        try:
            if run_type == "run":
                runsetup.compose_run(
                    project=tmp_path,
                    paths=_fake_paths(tmp_path),
                    policy=policy_mod.loads(""),
                    run_id=RUN_ID,
                    epic_filter=None,
                    story_filter=None,
                    max_stories=None,
                    stories_on=False,
                    spec_folder="",
                    sweep_factory=lambda _trigger, *, started: None,
                    make_adapters=_accepting_adapters,
                    engine_cls=_AcceptingEngine,
                    stories_engine_cls=_AcceptingEngine,
                    trusted_config_digest="deadbeef",
                )
            else:
                runsetup.compose_sweep(
                    project=tmp_path,
                    paths=_fake_paths(tmp_path),
                    policy=policy_mod.loads(""),
                    run_id=RUN_ID,
                    prompting=False,
                    decisions_only=False,
                    max_bundles=None,
                    repeat=None,
                    max_cycles=None,
                    trigger="auto",
                    make_adapters=_accepting_adapters,
                    sweep_engine_cls=_AcceptingEngine,
                    trusted_config_digest="deadbeef",
                )
        except BaseException as e:
            errors.append(e)

    def rival_resume() -> None:
        try:
            with state_lock(run_dir):
                rival_observed.append(
                    ((run_dir / "state.json").is_file(), (run_dir / "engine.pid").is_file())
                )
        except BaseException as e:
            errors.append(e)

    composer = threading.Thread(target=compose, name="composer")
    rival = threading.Thread(target=rival_resume, name="rival-resume")
    composer.start()
    assert stamp_entered.wait(2)
    assert (run_dir / "state.json").is_file()
    assert not (run_dir / "engine.pid").exists()
    rival.start()
    assert rival_attempted.wait(2)
    assert rival_observed == []
    release_stamp.set()
    composer.join(2)
    rival.join(2)

    assert errors == []
    assert rival_observed == [(True, True)]


@pytest.fixture
def unwinding(tmp_path):
    """A project plus a `make_adapters` that fails the way the real one does.

    `runsetup.make_adapters` raises `SystemExit` at six sites (an unresolvable
    profile, an unknown adapter kind, a kind that fails to load, a construction
    failure, an adapter class that rejects a bootstrap keyword, an unusable
    multiplexer), and every one lands *after* the composer has
    published the run dir, its `state.json` and the out-of-tree config-digest stamp.
    The fake records that all three exist at the moment it is called, so the
    assertions after the raise grade a *removal* — an "is it gone" assertion passes
    just as happily for a run dir that was never written.

    `tmp_path` rather than the `project` sandbox, on the same reasoning the `pinned`
    fixture states: the composers touch only `.bmad-loop/runs/` and the out-of-tree
    state root (which conftest's `_isolate_state_root` already redirects), so the
    sandbox's git repo and BMAD artifact dirs would buy nothing. The end-to-end
    launch path is covered on the real sandbox in tests/test_cli.py."""
    published: dict[str, bool] = {}

    def make_adapters(project, run_dir, policy, *, profiles=None):
        published["run_dir"] = run_dir.is_dir()
        published["state"] = (run_dir / "state.json").is_file()
        published["state_dir"] = runs.state_dir_for(tmp_path, RUN_ID).is_dir()
        raise SystemExit(BOOM)

    return types.SimpleNamespace(project=tmp_path, make_adapters=make_adapters, published=published)


def _assert_unwound(probe):
    """Composition published all three artifacts, then left none of them."""
    assert probe.published == {"run_dir": True, "state": True, "state_dir": True}
    assert not runs.run_dir_for(probe.project, RUN_ID).exists()
    assert not runs.state_dir_for(probe.project, RUN_ID).exists()


def test_compose_run_unwinds_the_run_when_the_adapters_abort(unwinding):
    """A failed `make_adapters` must leave no run behind, and must still abort.

    Without the unwind the run dir survives carrying `state.json` with
    `finished=False` and `crashed=False` and no `run-start` line — and nothing
    reconciles that shape, since `runs.reconcile_stale_worktrees` only visits
    `is_finished` runs. It lingers as a resumable-looking empty run.

    The `SystemExit` itself is re-raised unchanged: the cleanup is best-effort
    precisely so it can never replace the failure the operator has to read."""
    with pytest.raises(SystemExit, match="not usable on this host"):
        runsetup.compose_run(
            project=unwinding.project,
            paths=_fake_paths(unwinding.project),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            epic_filter=None,
            story_filter=None,
            max_stories=None,
            stories_on=False,
            spec_folder="",
            sweep_factory=lambda _trigger, *, started: None,
            make_adapters=unwinding.make_adapters,
            engine_cls=_NeverBuilt,
            stories_engine_cls=_NeverBuilt,
            trusted_config_digest="deadbeef",
        )
    _assert_unwound(unwinding)


def test_failed_composition_identifies_its_narrow_run_ownership(unwinding, monkeypatch):
    """The composer may unwind its own live pid publication, but must opt into
    that exception explicitly rather than borrowing operator ``force``."""
    real_delete = runs.delete_run
    calls: list[tuple[bool, int | None]] = []

    def checked_delete(
        project,
        run_dir,
        *,
        force=False,
        _expected_composer_pid=None,
        _expected_composer_claim=None,
    ):
        assert _expected_composer_claim is not None
        assert os.path.samestat(_expected_composer_claim, run_dir.stat(follow_symlinks=False))
        calls.append((force, _expected_composer_pid))
        return real_delete(
            project,
            run_dir,
            force=force,
            _expected_composer_pid=_expected_composer_pid,
            _expected_composer_claim=_expected_composer_claim,
        )

    monkeypatch.setattr(runs, "delete_run", checked_delete)
    with pytest.raises(SystemExit, match="not usable on this host"):
        _run_compose_sweep(unwinding.project, unwinding.make_adapters)

    assert calls == [(False, os.getpid())]
    _assert_unwound(unwinding)


def test_failed_composition_refuses_to_unwind_a_rival_pid_publication(unwinding, capsys):
    """A resume that replaces the composer's pid publication owns the run now.

    The launch error remains authoritative, while the refused unwind is reported
    and leaves both run and control state available to the rival.
    """

    def rival_then_abort(project, run_dir, policy, *, profiles=None):
        unwinding.published["run_dir"] = run_dir.is_dir()
        unwinding.published["state"] = (run_dir / "state.json").is_file()
        unwinding.published["state_dir"] = runs.state_dir_for(project, RUN_ID).is_dir()
        (run_dir / runs.PID_FILE).write_text(str(os.getpid() + 1), encoding="utf-8")
        raise SystemExit(BOOM)

    with pytest.raises(SystemExit, match="not usable on this host"):
        _run_compose_sweep(unwinding.project, rival_then_abort)

    warning = capsys.readouterr().err
    assert "changed engine ownership" in warning
    assert runs.run_dir_for(unwinding.project, RUN_ID).is_dir()
    assert runs.state_dir_for(unwinding.project, RUN_ID).is_dir()


def test_failed_composition_refuses_to_unwind_a_replacement_directory(unwinding, capsys):
    """A missing pid does not prove the composer's original directory still exists.

    A cleanup can remove that directory after an unverifiable liveness probe and a
    later creator can claim the same id before composition unwinds. The directory
    identity captured by the original exclusive claim keeps the replacement whole.
    """

    def replace_then_abort(project, run_dir, policy, *, profiles=None):
        # Allocate the replacement while the original still holds its own inode,
        # then rename it into place. `rmtree` followed by `mkdir` is free to reuse
        # the inode it just released, and on some filesystems it does — leaving
        # `os.path.samestat` unable to tell the replacement from the directory the
        # composer exclusively claimed, so the guard correctly stays silent and the
        # row fails for a reason it is not testing.
        stand_in = run_dir.parent / f"{run_dir.name}.replacement"
        stand_in.mkdir()
        (stand_in / "replacement").write_text("owned elsewhere", encoding="utf-8")
        shutil.rmtree(run_dir)
        stand_in.rename(run_dir)
        raise SystemExit(BOOM)

    with pytest.raises(SystemExit, match="not usable on this host"):
        _run_compose_sweep(unwinding.project, replace_then_abort)

    warning = capsys.readouterr().err
    assert "changed directory ownership" in warning
    replacement = runs.run_dir_for(unwinding.project, RUN_ID)
    assert (replacement / "replacement").read_text(encoding="utf-8") == "owned elsewhere"


def test_compose_sweep_unwinds_the_run_when_the_adapters_abort(unwinding):
    """The sweep composer publishes the same artifacts (plus `sweep.json`) ahead of
    the same `make_adapters` call, so it owns its own unwind — separately, since a
    sweep is the run type most likely to hit the reachable SystemExit: an
    auto-triggered child re-probes the multiplexer live in a parent that started
    fine."""
    with pytest.raises(SystemExit, match="not usable on this host"):
        runsetup.compose_sweep(
            project=unwinding.project,
            paths=_fake_paths(unwinding.project),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            repeat=None,
            max_cycles=None,
            trigger="auto",
            make_adapters=unwinding.make_adapters,
            sweep_engine_cls=_NeverBuilt,
            trusted_config_digest="deadbeef",
        )
    _assert_unwound(unwinding)


def test_compose_run_unwinds_a_claim_abandoned_before_save_state(unwinding, monkeypatch):
    """The guard opens on the statement after the claim, not at `save_state`.

    `_claim_run_dir` publishes the first artifact — the run DIRECTORY — so an abort
    between it and `save_state` used to strand an empty dir the guard never saw. It
    is invisible to `bmad-loop list` (state.json-gated), which is precisely why it
    is worth removing: nothing surfaces it, and a later launch reusing that
    `--run-id` is refused by a directory holding nothing.

    A `KeyboardInterrupt` because that is the only realistic way in: `Journal`
    mkdirs `exist_ok=True` over a directory this frame just created and
    `build_run_state` is a pure constructor, so neither fails on its own — but a
    signal lands between arbitrary statements, and the arm is `BaseException`.

    `seen` is the positive control, on the fixture's own doctrine: "is it gone"
    passes just as happily for a directory that was never created.

    Ablation: move the `try` back below `build_run_state` and this fails alone."""
    seen: dict[str, bool] = {}

    def exploding_build_run_state(**kwargs):
        seen["run_dir"] = runs.run_dir_for(unwinding.project, RUN_ID).is_dir()
        raise KeyboardInterrupt

    monkeypatch.setattr(runsetup, "build_run_state", exploding_build_run_state)
    with pytest.raises(KeyboardInterrupt):
        runsetup.compose_run(
            project=unwinding.project,
            paths=_fake_paths(unwinding.project),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            epic_filter=None,
            story_filter=None,
            max_stories=None,
            stories_on=False,
            spec_folder="",
            sweep_factory=lambda _trigger, *, started: None,
            make_adapters=unwinding.make_adapters,
            engine_cls=_NeverBuilt,
            stories_engine_cls=_NeverBuilt,
            trusted_config_digest="deadbeef",
        )
    assert seen == {"run_dir": True}  # the claim published it...
    assert not runs.run_dir_for(unwinding.project, RUN_ID).exists()  # ...the unwind took it back
    assert unwinding.published == {}  # and it aborted well ahead of `make_adapters`


def test_compose_sweep_unwinds_when_the_journal_itself_cannot_be_built(unwinding, monkeypatch):
    """The same window in the sweep composer, at its earliest statement — which is
    also the one case that reaches `_unwind_composition` with NO journal.

    That is why this drives `Journal` rather than the `RunState` build: the unwind
    writes a `composition-unwind-failed` entry through the journal it is handed, so
    a `None` there has to be handled rather than left to the surrounding
    `suppress(Exception)` (an `AttributeError` on `None` is an `Exception`, so the
    code would work by accident while reading as though a journal were guaranteed).

    Ablation: restore `_unwind_composition`'s `journal: Journal` annotation and drop
    the `if journal is not None` guard — this stays GREEN, because the suppress
    absorbs the AttributeError. The guard is graded by the annotation and by this
    docstring, not by an exit code; what this test does pin is that the run dir is
    removed on this path at all, which fails alone if the `try` moves back down."""
    built: dict[str, bool] = {}

    def exploding_journal(run_dir):
        built["run_dir"] = run_dir.is_dir()
        raise OSError("journal unavailable")

    monkeypatch.setattr(runsetup, "Journal", exploding_journal)
    with pytest.raises(OSError, match="journal unavailable"):
        runsetup.compose_sweep(
            project=unwinding.project,
            paths=_fake_paths(unwinding.project),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            repeat=None,
            max_cycles=None,
            trigger="auto",
            make_adapters=unwinding.make_adapters,
            sweep_engine_cls=_NeverBuilt,
            trusted_config_digest="deadbeef",
        )
    assert built == {"run_dir": True}
    assert not runs.run_dir_for(unwinding.project, RUN_ID).exists()
    assert unwinding.published == {}


PRIOR_STATE = '{"run_id": "prior", "finished": true}'
PRIOR_JOURNAL = '{"kind": "run-complete"}\n'
PRIOR_STAMP = "prior-digest"


def _seed_prior_run(project):
    """A finished run already occupying RUN_ID — dir, state, journal, and the
    out-of-tree state dir the unwind's `_discard_state_dir` also reaches.

    Finished, deliberately: `delete_run`'s only guard refuses a LIVE session, so a
    run that has none is exactly the case that guard does not cover."""
    run_dir = runs.run_dir_for(project, RUN_ID)
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(PRIOR_STATE, encoding="utf-8")
    (run_dir / "journal.jsonl").write_text(PRIOR_JOURNAL, encoding="utf-8")
    state_dir = runs.state_dir_for(project, RUN_ID)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config-digest").write_text(PRIOR_STAMP, encoding="utf-8")
    return run_dir, state_dir


def _assert_prior_run_untouched(probe, run_dir, state_dir):
    """The prior run survives BYTE-IDENTICAL, and the refusal beat publication.

    `probe.published == {}` is the positive control and the load-bearing half: an
    "is it still there" assertion alone passes just as happily for a composer that
    published over the run and then failed to unwind it. An empty dict says
    `make_adapters` was never reached, so `save_state` never ran and the
    `except BaseException` arm was never entered — which is the claim, since the
    guard's whole placement requirement is that it sits OUTSIDE that try."""
    assert probe.published == {}
    assert (run_dir / "state.json").read_text(encoding="utf-8") == PRIOR_STATE
    assert (run_dir / "journal.jsonl").read_text(encoding="utf-8") == PRIOR_JOURNAL
    assert (state_dir / "config-digest").read_text(encoding="utf-8") == PRIOR_STAMP


def test_compose_run_refuses_a_run_id_that_already_exists(unwinding):
    """`--run-id` naming an existing run must be refused before anything is
    published, leaving that run untouched.

    Without the claim this was destructive, not merely sloppy: `Journal.__init__`
    mkdirs with `exist_ok=True`, so the composer adopted the existing directory,
    `save_state` overwrote its `state.json`, and the reachable `make_adapters`
    SystemExit then drove `_unwind_composition` — which `rmtree`s the whole run dir
    and discards its out-of-tree state. A paused, stopped or finished run has no
    live session, so `delete_run`'s guard never fires: the prior run's journal,
    logs, tasks and state were erased permanently."""
    run_dir, state_dir = _seed_prior_run(unwinding.project)
    with pytest.raises(SystemExit, match="already exists"):
        runsetup.compose_run(
            project=unwinding.project,
            paths=_fake_paths(unwinding.project),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            epic_filter=None,
            story_filter=None,
            max_stories=None,
            stories_on=False,
            spec_folder="",
            sweep_factory=lambda _trigger, *, started: None,
            make_adapters=unwinding.make_adapters,
            engine_cls=_NeverBuilt,
            stories_engine_cls=_NeverBuilt,
            trusted_config_digest="deadbeef",
        )
    _assert_prior_run_untouched(unwinding, run_dir, state_dir)


class _BuiltEngine:
    """Engine stand-in that constructs cleanly — the inverse of `_NeverBuilt`, for
    the one test that must get PAST `make_adapters`."""

    def __init__(self, *args, **kwargs):
        pass


def test_compose_sweep_unwinds_when_the_started_latch_raises(tmp_path):
    """`on_started` fires as the LAST statement inside the composition block, so a
    latch that raises must unwind the child like any other escape.

    This is the one arm the adapter-abort rows above cannot reach: they fail early
    in the try, at `make_adapters`, so they would stay green if the block's extent
    were narrowed to end before the latch. It is also the case `compose_sweep`'s
    docstring reasons about — the parent's in-memory flag is set before its write,
    so at-most-once holds, and what the unwind decides is only whether the refused
    retry cost nothing or a composed, resumable child. That argument is prose until
    something pins the unwind actually covering a raising latch."""
    published: dict[str, bool] = {}

    def make_adapters(project, run_dir, policy, *, profiles=None):
        return {"dev": object(), "review": object(), "triage": object()}

    def boom() -> None:
        published["run_dir"] = runs.run_dir_for(tmp_path, RUN_ID).is_dir()
        published["state"] = (runs.run_dir_for(tmp_path, RUN_ID) / "state.json").is_file()
        published["sweep"] = (runs.run_dir_for(tmp_path, RUN_ID) / "sweep.json").is_file()
        published["state_dir"] = runs.state_dir_for(tmp_path, RUN_ID).is_dir()
        raise RuntimeError("latch write failed")

    with pytest.raises(RuntimeError, match="latch write failed"):
        runsetup.compose_sweep(
            project=tmp_path,
            paths=_fake_paths(tmp_path),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            repeat=None,
            max_cycles=None,
            trigger="auto",
            make_adapters=make_adapters,
            sweep_engine_cls=_BuiltEngine,
            trusted_config_digest="deadbeef",
            on_started=boom,
        )
    # Graded as a removal, not an absence: the latch saw all four artifacts.
    assert published == {"run_dir": True, "state": True, "sweep": True, "state_dir": True}
    assert not runs.run_dir_for(tmp_path, RUN_ID).exists()
    assert not runs.state_dir_for(tmp_path, RUN_ID).exists()


def test_compose_sweep_refuses_a_run_id_that_already_exists(unwinding):
    """The sweep composer carries its own copy of the claim, so it gets its own
    row — `cmd_sweep --run-id` is a caller-supplied id on the same hidden flag, and
    a shared guard that only one composer actually calls is the failure mode a
    single test here would hide."""
    run_dir, state_dir = _seed_prior_run(unwinding.project)
    with pytest.raises(SystemExit, match="already exists"):
        runsetup.compose_sweep(
            project=unwinding.project,
            paths=_fake_paths(unwinding.project),
            policy=policy_mod.loads(""),
            run_id=RUN_ID,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            repeat=None,
            max_cycles=None,
            trigger="auto",
            make_adapters=unwinding.make_adapters,
            sweep_engine_cls=_NeverBuilt,
            trusted_config_digest="deadbeef",
        )
    _assert_prior_run_untouched(unwinding, run_dir, state_dir)


def _run_compose_sweep(project, make_adapters, engine_cls=_NeverBuilt):
    """Drive `compose_sweep` to whatever the injected `make_adapters` decides."""
    return runsetup.compose_sweep(
        project=project,
        paths=_fake_paths(project),
        policy=policy_mod.loads(""),
        run_id=RUN_ID,
        prompting=False,
        decisions_only=False,
        max_bundles=None,
        repeat=None,
        max_cycles=None,
        trigger="auto",
        make_adapters=make_adapters,
        sweep_engine_cls=engine_cls,
        trusted_config_digest="deadbeef",
    )


def test_a_failed_unwind_is_reported_and_does_not_replace_the_launch_error(
    unwinding, monkeypatch, capsys
):
    """A cleanup that fails must be SURFACED, and must still not become the error
    the operator reads.

    "Repair writes must raise" (AGENTS.md) cannot be honored literally here —
    raising is exactly what would swallow the launch failure — so the obligation is
    discharged by reporting. Suppressing silently left the resumable-looking ghost
    run this unwind exists to prevent, detectable only as the ABSENCE of an effect.

    The `match=` is the load-bearing half: it pins that the SystemExit reaching the
    operator is still `make_adapters`', not the cleanup's. A bare `pytest.raises`
    would pass just as happily for a cleanup failure that replaced it."""

    def boom(
        project,
        run_dir,
        *,
        force=False,
        _expected_composer_pid=None,
        _expected_composer_claim=None,
    ):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(runs, "delete_run", boom)
    with pytest.raises(SystemExit, match="not usable on this host"):
        _run_compose_sweep(unwinding.project, unwinding.make_adapters)

    warning = capsys.readouterr().err
    assert "warning: could not remove the partially composed run" in warning
    assert RUN_ID in warning
    assert f"bmad-loop delete {RUN_ID}" in warning
    # The ghost the operator was just warned about is really there, and the run's
    # own journal carries the record — which is where anyone investigating it looks.
    run_dir = runs.run_dir_for(unwinding.project, RUN_ID)
    assert run_dir.is_dir()
    kinds = [e["kind"] for e in Journal(run_dir).entries()]
    assert "composition-unwind-failed" in kinds


def test_a_failed_unwind_still_reports_when_the_run_dir_is_already_gone(
    unwinding, monkeypatch, capsys
):
    """The other failure mode, split into its own row: `delete_run` removes the run
    dir and THEN raises (`_discard_state_dir` is the real site — it runs after the
    `rmtree`).

    `Journal.append` opens with "a" and does not mkdir, so appending here raises
    `FileNotFoundError`. Unsuppressed that would propagate out of the unwind and
    replace the launch error — the one outcome this whole arm forbids — so the
    suppression around the journal write is load-bearing and gets its own test.
    The stderr report must still land, since it is now the only channel left."""

    def boom(
        project,
        run_dir,
        *,
        force=False,
        _expected_composer_pid=None,
        _expected_composer_claim=None,
    ):
        shutil.rmtree(run_dir)
        raise RuntimeError("state dir removal failed")

    monkeypatch.setattr(runs, "delete_run", boom)
    with pytest.raises(SystemExit, match="not usable on this host"):
        _run_compose_sweep(unwinding.project, unwinding.make_adapters)

    warning = capsys.readouterr().err
    assert "warning: could not remove the partially composed run" in warning
    assert "state dir removal failed" in warning
    assert not runs.run_dir_for(unwinding.project, RUN_ID).exists()
