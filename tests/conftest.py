"""Shared fixtures: a sandbox BMAD project with a real git repo, and helpers
that simulate the side effects skill sessions would have on disk."""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from bmad_loop import cli, documents, envvars, platform_util, runs
from bmad_loop.adapters.base import SessionResult, SessionSpec
from bmad_loop.bmadconfig import ProjectPaths
from bmad_loop.checks import ValidationReport
from bmad_loop.journal import save_state
from bmad_loop.model import PAUSE_ESCALATION, Phase, RunState, SessionRecord, StoryTask
from bmad_loop.verify import finalize_commit, rev_parse_head

# The suite reads/writes UTF-8 files (specs, journals, JSON, reports). Windows'
# default text encoding is cp1252, so a plain read_text()/open() throws
# UnicodeDecodeError on any non-ASCII byte — a whole class of "passes on Linux,
# dies on Windows" failures. Require UTF-8 mode on win32 so local runs match CI
# (whose windows job sets PYTHONUTF8=1) instead of failing with cryptic charmap
# errors deep in an unrelated test.
if sys.platform == "win32" and not sys.flags.utf8_mode:
    raise pytest.UsageError(
        "Windows test runs must use UTF-8 mode: set PYTHONUTF8=1 or pass -X utf8 "
        "(e.g. `set PYTHONUTF8=1 && uv run pytest`). The suite assumes UTF-8 to "
        "match the files under test; CI's windows job sets this automatically."
    )


def _codec_rejects_bad_byte() -> bool:
    """Whether byte 0xff is undecodable in the codec ``text=True`` will use.

    Probes ``TextIOWrapper`` with ``encoding=None`` rather than naming a codec,
    because that is the very default ``subprocess``'s text mode resolves for the
    child's streams — asking the machinery beats predicting it from the locale.
    """
    try:
        io.TextIOWrapper(io.BytesIO(b"\xff")).read()
    except UnicodeDecodeError:
        return True
    return False


# Guard for every test whose subject is a STRICT DECODE of subprocess output.
# Byte 0xff is undecodable only in UTF-8/ASCII; every ISO-8859-x and cp125x codec
# maps all 256 byte values, so under such a locale the strict decode never raises
# and those tests pass *with the bug restored* — a silent vacuity rather than a
# failure (verified: under LC_ALL=et_EE.iso885915 the #378 ablation passes). No
# single byte is undecodable everywhere, so this skips instead, leaving each test
# either exercising the fault or saying plainly that it did not. CI is always on
# the exercising side: the Linux legs run UTF-8 and the Windows legs set
# PYTHONUTF8=1 (.github/workflows/ci.yml).
#
# Shared here because three test modules need it; test_verify.py predates that and
# still carries its own local copy.
needs_strict_codec = pytest.mark.skipif(
    not _codec_rejects_bad_byte(),
    reason="host codec decodes 0xff (e.g. an ISO-8859-x locale), so nothing here "
    "would exercise the strict decode this fix is about",
)


def opencode_runs() -> bool:
    """Whether this host has an ``opencode`` binary that actually RUNS.

    ``shutil.which`` proves only that a name resolves to a path, and a path is
    not a working program: a stale WSL interop stub, or an npm wrapper whose
    target has been uninstalled, resolves happily and then exits nonzero on
    every invocation. Gating the live smoke on ``which`` alone therefore drove
    the whole module against a shim that could never serve a session, turning a
    host-shaped absence into a spurious failure (#294). Asking the binary to
    identify itself is the cheapest call that tells the two apart — and it
    sends no prompt, so the zero-token invariant holds.

    ``stdin=subprocess.DEVNULL`` because a shim that prompts rather than runs
    otherwise inherits the runner's tty and blocks until the timeout expires
    (measured: 4.00s stall on an inherited tty, 0.00s with DEVNULL). win32
    returns before either step: opencode-on-Windows is unverified for this
    adapter (README adapter table), so there is nothing to probe for.

    Deliberately a function and not a ``skipif`` constant beside
    ``needs_strict_codec``: a module-level constant here would spawn a
    subprocess at conftest import — on every pytest invocation, in every xdist
    worker, whether or not any opencode test was selected. Callers keep their
    own in-file ``HAVE_OPENCODE`` gate, which the ``*_live.py`` suffix
    convention requires anyway.
    """
    if sys.platform == "win32":
        return False
    if (binary := shutil.which("opencode")) is None:
        return False
    try:
        probe = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@pytest.fixture
def force_tmux_backend(monkeypatch):
    """Pin the tmux transport backend by name, regardless of host platform.

    External backends discovered via the ``bmad_loop.mux_backends`` entry-point
    scan may match any platform — the herdr adapter matches win32, where tmux
    does not — so on a host with such a package installed ``get_multiplexer()``
    would select it and tests that assert tmux-specific argv/behaviour *through
    the seam* would drive the wrong backend. Forcing
    ``BMAD_LOOP_MUX_BACKEND=tmux`` selects tmux by name (the env override
    bypasses the platform predicate and ``available()``), so these tests stay
    environment-independent. On a stock POSIX box this is a no-op — tmux is
    already the default. The cache is cleared on both ends so the forced choice
    takes effect and does not leak to later tests."""
    from bmad_loop.adapters import multiplexer

    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "tmux")
    multiplexer.get_multiplexer.cache_clear()
    yield
    multiplexer.get_multiplexer.cache_clear()


@pytest.fixture
def force_psmux_backend(monkeypatch):
    """Pin the psmux transport backend by name, regardless of host platform.

    The mirror of :func:`force_tmux_backend`, for the tests that assert what
    happens on a transport that namespaces sessions by registry. The registry
    root is exported only when the selected backend has such a namespace, so
    without this pin those tests read the *host's* default backend — passing on
    win32 and vacuously "passing" on Linux, where tmux is selected and nothing
    is exported at all. A forced name bypasses the platform predicate and
    ``available()``, and nothing here spawns psmux, so no binary is needed."""
    from bmad_loop.adapters import multiplexer

    monkeypatch.setenv("BMAD_LOOP_MUX_BACKEND", "psmux")
    multiplexer.get_multiplexer.cache_clear()
    yield
    multiplexer.get_multiplexer.cache_clear()


def json_recursion_payload() -> str:
    """A nested value the real decoder cannot parse without recursing too deep.

    The depth is discovered rather than hardcoded. 3.13 raises at ``limit * 20``;
    3.14 decodes that iteratively and only recurses far deeper, where the
    threshold follows the C stack rather than ``sys.getrecursionlimit()``.
    Probing keeps callers regression tests for a genuine ``RecursionError``
    rather than a synthetic stand-in.
    """
    limit = sys.getrecursionlimit()
    for multiplier in (20, 100, 500, 2000):
        depth = limit * multiplier
        payload = "[" * depth + "0" + "]" * depth
        try:
            json.loads(payload)
        except RecursionError:
            return payload
    pytest.skip("the json decoder does not recurse at any probed depth on this interpreter")


def write_script_launcher(directory: Path, name: str, body: str) -> Path:
    """Write a fake CLI launcher for the host OS."""
    directory = Path(directory)
    sidecar = directory / f"{name}.py"
    sidecar.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = directory / f"{name}.cmd"
        # `\n`, not `\r\n`: write_text translates it to the CRLF cmd wants, so an
        # explicit `\r` would land on disk doubled (`\r\r\n`).
        launcher.write_text(f'@"{sys.executable}" "{sidecar}" %*\n', encoding="utf-8")
    else:
        launcher = directory / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{sidecar}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return launcher


# ---- host-shell verify/lifecycle stub commands (single platform-detection spot) ----
# The engine runs verify/plugin-lifecycle commands via the host shell (`sh -c` on
# POSIX, `cmd /c` on Windows), so tests that assert on that machinery need commands
# both shells honor. These build them per-OS in one place instead of each test file
# re-deriving the win32 branch.

_OK = "exit 0"  # cross-platform always-success verb (both `cmd /c` and `sh -c` honor it)
# Its counterpart. `false`, the POSIX reflex, is *not* the same thing on Windows:
# cmd has no such verb, so it reads as a broken environment rather than a failing
# check (issue #302) — an ordinary-failure test written with it asserts nothing.
_FAIL = "exit 1"
_RUN = "%BMAD_LOOP_RUN_DIR%" if sys.platform == "win32" else "$BMAD_LOOP_RUN_DIR"


def _file_exists_cmd(path) -> str:
    """Shell verify command (run via shell=True) exiting 0 iff `path` exists, on the
    host's shell — `test -f` (POSIX) / `if exist` (Windows cmd) — so the verify-gate
    tests drive the real machinery on either OS, not a POSIX-only `test` that cmd
    rejects with "'test' is not recognized"."""
    if sys.platform == "win32":
        return f'if exist "{path}\\NUL" (exit 1) else if exist "{path}" (exit 0) else (exit 1)'
    return f'test -f "{path}"'


def passes_once(marker) -> str:
    """Return a host-shell command that succeeds once, then fails.

    ``marker`` must be an explicit absolute path outside the worktree. Verify
    commands receive no ``BMAD_LOOP_RUN_DIR`` environment variable, and a marker
    inside the worktree can be removed by rollback between the two executions.
    """
    if sys.platform == "win32":
        win = str(marker).replace("/", "\\")
        return f'if exist "{win}" (exit 1) else (type nul > "{win}")'
    return f'test ! -f "{marker}" && touch "{marker}"'


# A tool no host has: sh exits 127, cmd exits 1 with "is not recognized" (#302).
# Both classify as verify environment faults — the point of the tests using it.
MISSING_TOOL_CMD = "definitely-not-a-real-cmd-302"


def _self_disarming_cmd(project_dir: Path, stem: str = "check") -> str:
    """A verify command that passes once, then breaks its own environment — the
    seeded-worktree fault of issue #126.

    POSIX: a script that drops its own exec bit, so the next run dies rc=126.
    win32: cmd has no exec bit, and a batch file cannot disarm itself at all
    (cmd re-reads it per line, so deleting or renaming it kills the *current*
    run with "The batch file cannot be found."). The win32 twin therefore burns
    a flag file and reaches for a tool that is not there on the second run —
    cmd's own "is not recognized", the env fault of #302. A `.sh` command would
    be worse than useless here: cmd hands it to the file association, which pops
    an interactive picker mid-suite (#292)."""
    if sys.platform == "win32":
        armed = project_dir / f"{stem}.armed"
        armed.write_text("", encoding="utf-8")
        return f'if exist "{armed}" (del "{armed}") else ({MISSING_TOOL_CMD})'
    script = project_dir / f"{stem}.sh"
    script.write_text(f'#!/bin/sh\nchmod 644 "{script}"\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)
    return f'"{script}"'


def _write_check_script(project_dir: Path, stem: str = "check") -> tuple[Path, str]:
    """An always-passing verify script for the host shell. Returns (script, command)."""
    if sys.platform == "win32":
        script = project_dir / f"{stem}.cmd"
        script.write_text("@exit /b 0\n", encoding="utf-8")
    else:
        script = project_dir / f"{stem}.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    return script, f'"{script}"'


def _disarm_check_script(script: Path) -> None:
    """Break the script the way the host shell notices: POSIX drops the exec bit
    (rc=126); cmd has no exec bit, so the file goes away (#302)."""
    if sys.platform == "win32":
        script.unlink()
    else:
        script.chmod(0o644)


def _touch_run(marker: str) -> str:
    if sys.platform == "win32":
        return f'type nul > "{_RUN}\\{marker}"'
    return f'touch "{_RUN}/{marker}"'


def _exists_run(marker: str) -> str:
    if sys.platform == "win32":
        return (
            f'if exist "{_RUN}\\{marker}\\NUL" (exit 1) '
            f'else if exist "{_RUN}\\{marker}" (exit 0) else (exit 1)'
        )
    return f'test -f "{_RUN}/{marker}"'


def _seeded_then_touch(rel: str, marker: str) -> str:
    if sys.platform == "win32":
        norm_rel = rel.replace("/", "\\")
        return (
            f'if exist "{norm_rel}\\NUL" (exit 1) '
            f'else if exist "{norm_rel}" (type nul > "{_RUN}\\{marker}") else (exit 1)'
        )
    return f'test -f "{rel}" && touch "{_RUN}/{marker}"'


SPRINT_TEMPLATE = {
    "generated": "01-06-2026 10:00",
    "last_updated": "01-06-2026 10:00",
    "project": "sandbox",
    "project_key": "NOKEY",
    "tracking_system": "file-system",
    "development_status": {},
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


NOISY_GIT_KEY = "core.fsyncMethod"
NOISY_GIT_VALUE = "bmad-loop-not-a-method"


def make_git_noisy(repo: Path) -> str:
    """Give `repo` a git config that writes to stderr while still exiting 0, and
    return the warning text.

    The suite's only host-noise dimension. An unknown VALUE for a known KEY is a
    `warning:` on stderr at rc 0, not an error — measured at git 2.55.0 for every
    subcommand verify.py reads text from. Without it no row in the #442 family is
    falsifiable, because a quiet git makes the merged and stdout-alone reads
    indistinguishable.

    Measures rather than predicting. `core.fsyncMethod` only exists from git 2.36,
    and below that the key is simply UNKNOWN, which git ignores in silence — so on an
    older git every caller of this helper would pass with the bug restored. That is a
    vacuous green, not a pass, so this skips instead, naming the version it saw. CI is
    always on the exercising side. Same argument as `needs_strict_codec` in
    tests/test_verify.py, which probes the codec rather than inferring it from the
    locale."""
    git(repo, "config", NOISY_GIT_KEY, NOISY_GIT_VALUE)
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if proc.returncode != 0 or not proc.stderr.strip():
        version = subprocess.run(
            ["git", "-C", str(repo), "version"], capture_output=True, text=True
        ).stdout.strip()
        pytest.skip(f"{version} does not warn at rc 0 for an unknown {NOISY_GIT_KEY} value")
    return proc.stderr.strip()


# ------------------------------------------------ machine-readable CLI output


def machine_json(argv, capsys, *, rc: int = 0, err_contains: str | None = None):
    """Run a `--json` CLI command and parse the WHOLE of stdout — parsing the
    full stream (not a substring) is itself the assertion that nothing but the
    document is printed (the machine.py purity contract).

    `rc` is the expected exit code, and it is not always 0: a command may report
    a negative verdict through its exit status while still owing the caller a
    complete document. Only stdout purity is being asserted here, not success.

    `err_contains` guards the other stream. The default — stderr is *empty* — is
    the strict form and the one to reach for; pass a substring only for a command
    that documents chatter there, as `probe-adapter --json` does by routing its
    human `ok:` trailer to stderr so stdout stays the document alone. That is an
    opt-in to a different assertion, never a waiver: the substring must be
    present, so a trailer that silently moves back to stdout still fails.
    """
    assert cli.main(argv) == rc
    out, err = capsys.readouterr()
    if err_contains is None:
        assert err == ""
    else:
        assert err_contains in err
    return json.loads(out)


def make_validate_document(findings, *, stories_on: bool = False, spec_folder: str = ""):
    """Build a REAL `validate --json` document from (check, severity, message,
    detail) tuples, for tests that need to *stub* one rather than run validate.

    A sibling of machine_json, not an extension of it: that helper drives
    cli.main + capsys to assert stdout purity, so it can only ever produce the
    document a real run happens to emit on the host. Callers here need a chosen
    document (a specific severity mix, a specific detail shape) and no
    subprocess.

    It is built by driving the same ValidationReport -> validate_document path
    the CLI drives, so the shape cannot drift from the contract by being
    hand-written. Going through ValidationReport.add also means its assert
    (checks.py) rejects invented check ids: a test cannot quietly pin behaviour
    to a check that does not exist.
    """
    report = ValidationReport()
    for check, severity, message, detail in findings:
        report.add(check, severity, message, detail)
    return documents.validate_document(report, stories_on, spec_folder)


@pytest.fixture(scope="session", autouse=True)
def _isolate_ambient_git_ignores(tmp_path_factory: pytest.TempPathFactory):
    """Shadow the two ignore sources git reads from OUTSIDE the repo, for every test.

    Developer boxes routinely carry `.claude/` or `.codex/` in a global gitignore —
    ignoring your AI tooling everywhere is the obvious thing to do — and without this
    the shield tests silently measure that instead of the shield. The failure is not
    only a red: a global `.codex/` makes `git add -A` skip the hook config, so the
    file the test believes it TRACKED is untracked, the tracked-file filter is never
    reached, and `ls-files -ci --exclude-standard` answers "" because an untracked
    file is not tracked-and-ignored. The test passes having exercised nothing.

    Two sources, and both are needed: `core.excludesFile` from `~/.gitconfig`
    (suppressed by pointing GIT_CONFIG_GLOBAL at a file that does not exist), and its
    documented fallback `$XDG_CONFIG_HOME/git/ignore`, which still applies once the
    key is unset. Session-scoped so `_project_template`'s own `add -A` is covered too.

    `GIT_CONFIG_NOSYSTEM` is deliberately NOT set, for the reason
    `test_shield_falls_back_to_home_when_xdg_is_relative` records: it would suppress
    Git-for-Windows' system `core.autocrlf` and make unrelated tracked files read as
    modified. A system-level excludes file therefore stays reachable — tests that
    depend on a path really being tracked assert that as a precondition."""
    env = tmp_path_factory.mktemp("git-env")
    mp = pytest.MonkeyPatch()
    mp.setenv("GIT_CONFIG_GLOBAL", str(env / "no-such-gitconfig"))
    mp.setenv("XDG_CONFIG_HOME", str(env / "xdg"))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_state_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch):
    """Point the user-scoped state root at a per-test temp dir, for every test.

    `runs.state_root()` resolves to `~/.local/state/bmad-loop` (POSIX) or
    `%LOCALAPPDATA%\\bmad-loop\\state` (win32) when nothing overrides it, and the
    run control plane does not merely *read* that location — it mkdirs into it.
    Without this every test that constructs an adapter would write into the
    developer's (or the CI runner's) real state directory and leave it there, one
    stray tree per run id, on a path no fixture cleans up.

    One variable is enough: `BMAD_LOOP_STATE_DIR` is checked before the platform
    cascade and outranks all of it, so this cannot be defeated by whatever
    XDG/LOCALAPPDATA the host happens to export.

    Deliberately NOT a blanket reset of HOME / USERPROFILE / LOCALAPPDATA /
    XDG_STATE_HOME. Those are not ours: git reads HOME (`install`'s shield
    probes it), the coding CLIs discover their own config under them, and
    `sanitize.redact_home` measures against the real one — shadowing them
    suite-wide would change what unrelated tests measure, which is the argument
    `_isolate_ambient_git_ignores` makes for shadowing only the two it must.
    Tests that grade the cascade itself `delenv` this variable and monkeypatch
    the ones they need, and share this fixture's monkeypatch instance, so the
    override comes off cleanly for exactly that test."""
    monkeypatch.setenv(envvars.STATE_DIR, str(tmp_path_factory.mktemp("state-root")))


@pytest.fixture(autouse=True)
def _isolate_mux_registry(monkeypatch):
    """Keep the psmux registry root, and the inside-a-pane marker, out of the
    cross-test environment.

    `runs.export_psmux_registry_root` writes `PSMUX_DATA_DIR` into `os.environ`
    directly — that IS its contract, since every psmux verb inherits the process
    environment — so any test that runs `cli.main` leaves the root of ITS temp
    state dir behind for every later test in the worker. Two of them care: the
    live psmux gate reads the default registry to prove a session is NOT visible
    there, and a stale root would make that read answer about a temp directory
    instead. `delenv` rather than `setenv`: monkeypatch then restores the
    operator's own value (or its absence) at teardown regardless of what the code
    under test put there, and unset is the state the tests that assert an override
    are written against.

    ``TMUX``/``TMUX_PANE`` go with it, for the developer half of the same problem:
    a multiplexer sets them on every pane child, so running the suite from inside
    tmux or psmux would otherwise put every test in the run inside a pane. The
    registry export no longer cares (it derives either way, and a test asserts
    exactly that), but `PsmuxMultiplexer._display_message` does branch on ``TMUX``,
    and the live module builds envs that assume it is absent.

    ``PSMUX_BARE_ENV`` too: bmad-loop does not support that mode and the psmux
    backend warns once per process about it, so a developer whose profile sets
    it would otherwise start every worker with the warning already spent (and
    an unexpected stderr line in whichever test spawned psmux first).

    The backend's record of the root it *displaced* is reset on the same rule
    and for a sharper reason: it is written by the same export, it survives in
    module state rather than in the environment (which is the whole point of
    it), and a leftover value makes `legacy_registries()` hand every later test
    an extra registry to sweep."""
    from bmad_loop.adapters import psmux_backend

    monkeypatch.delenv(runs.PSMUX_DATA_DIR, raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("PSMUX_BARE_ENV", raising=False)
    monkeypatch.setattr(psmux_backend, "_DISPLACED_ROOT", None)


@pytest.fixture(scope="session")
def _project_template(
    tmp_path_factory: pytest.TempPathFactory, _isolate_ambient_git_ignores: None
) -> Path:
    """Master sandbox repo, built once per xdist worker. NEVER hand this path to
    a test — a mutation would poison every later test in the worker; tests get
    disposable copies via `project`. (Do not chmod it read-only either: copytree
    preserves modes, so the copies would inherit it and break every write.)"""
    root = tmp_path_factory.mktemp("project-template") / "sandbox"
    impl = root / "_bmad-output" / "implementation-artifacts"
    plan = root / "_bmad-output" / "planning-artifacts"
    impl.mkdir(parents=True)
    plan.mkdir(parents=True)
    (root / "src.txt").write_text("original\n")
    (root / ".gitignore").write_text(".bmad-loop/runs/\n")  # as `bmad-loop init` would
    git(root, "init", "-q", "-b", "main")
    # `git init` seeds 14 dead `*.sample` hooks nothing here reads, and every test
    # replicated all 14 through `project`'s copytree. Drop the files only — NOT via
    # `git init --template=` (an empty template also drops `.git/hooks/` itself and
    # `.git/info/exclude`, which tests do depend on: three sites write
    # `.git/hooks/pre-commit` with no mkdir, and the install tests read info/exclude).
    for sample in (root / ".git" / "hooks").glob("*.sample"):
        sample.unlink()
    # Local config: copies (and their worktrees) inherit it via the copied .git/config.
    git(root, "config", "user.email", "test@test")
    git(root, "config", "user.name", "test")
    git(root, "config", "core.fsync", "none")  # cheapen commits; old git ignores unknown keys
    # `git commit` ends by spawning `git maintenance run --auto --quiet --detach`
    # (traced on git 2.55). That child DETACHES, so it outlives the commit and this
    # fixture both, and it writes under `.git/objects/` in the template while later
    # tests are already copytree-ing it: `objects/maintenance.lock` is listed by
    # scandir, unlinked by the detached child, and gone by the time copy2 opens it,
    # failing one arbitrary test on `[Errno 2]` for a file nothing asked for. Disable
    # the writer rather than teach the copy to tolerate it — an `ignore=` pattern on
    # the copy would also silently skip a lock that did matter. The copies inherit
    # this via the copied .git/config, so what they commit cannot re-arm it either.
    git(root, "config", "maintenance.auto", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def project(tmp_path: Path, _project_template: Path) -> ProjectPaths:
    """Git repo with BMAD-shaped artifact dirs and an initial commit — a copytree
    clone of the per-worker template, so no git subprocesses per test (git spawn
    plus fsync made this fixture ~3s per test on Windows CI)."""
    root = tmp_path / "sandbox"
    shutil.copytree(_project_template, root)
    return ProjectPaths(
        project=root,
        implementation_artifacts=root / "_bmad-output" / "implementation-artifacts",
        planning_artifacts=root / "_bmad-output" / "planning-artifacts",
    )


# --------------------------------------- divergent roots (`repo_root` override)
#
# `isolation = "none"` plus a `repo_root:` key in _bmad/bmm/config.yaml is the ONE
# supported shape where `paths.project` and `paths.repo_root` name different
# directories (`bmadconfig.worktree_isolation_conflict` refuses the other, and
# `ProjectPaths.rebased` sets both roots, so worktree isolation never diverges).
# The `project` fixture above sets no override, so `repo_root == project` there and
# nothing built on it can tell the two apart. These helpers centralize the shared
# marker probes, config writer, and nested builder so new coverage does not have to
# re-derive those load-bearing pieces.

# Two markers, one per root. A row that plants both and probes each pins the cwd
# from BOTH directions — the marker only `repo_root` holds must pass AND the one
# only `project` holds must fail. The positive probe identifies `repo_root`; the
# negative probe rules out the tempting `project` regression explicitly.
MARKER_IN_REPO_ROOT = "only-in-repo-root.txt"
MARKER_IN_PROJECT = "only-in-project.txt"

# RELATIVE probes on purpose: an absolute path answers the same from any cwd, so
# only a relative one is cwd-sensitive — and `_file_exists_cmd` keeps it honest on
# both host shells rather than a POSIX-only `test` cmd rejects.
REPO_ROOT_MARKER_CMD = _file_exists_cmd(MARKER_IN_REPO_ROOT)
PROJECT_MARKER_CMD = _file_exists_cmd(MARKER_IN_PROJECT)


def plant_root_markers(*, repo_root: Path, project: Path) -> None:
    """Plant one marker in each root, for a two-direction cwd probe.

    Deliberately plain untracked files: an engine row's baseline snapshot
    (`Engine._dev_phase` stamps `baseline_untracked` from `workspace.root`)
    absorbs anything planted before the run, so these cannot themselves satisfy
    proof-of-work and the row still needs real session work to pass its gate.

    KEYWORD-ONLY, and the two roots must differ. Both parameters are `Path`, so
    positionally a swapped call type-checks, runs, and grades the OPPOSITE
    direction to the one its row claims; and handed the collapsed `project`
    fixture (`repo_root == project`, the default) both markers land in one tree,
    where every probe passes from either cwd and the two-direction claim grades
    nothing at all. Neither mistake can raise on its own, so the precondition is
    asserted here rather than left to each caller to remember. Existing opposite-
    root markers are refused too: otherwise a stale file could make a wrong cwd
    satisfy both probes.
    """
    assert repo_root.resolve() != project.resolve(), (
        "plant_root_markers needs two DIFFERENT roots: with the collapsed `project` "
        "fixture both markers land in one tree and the two-direction probe grades "
        "nothing. Build the divergent fixture first (nested_repo_root_paths, or a "
        "write_repo_root_override code root)."
    )
    assert not (project / MARKER_IN_REPO_ROOT).exists(), (
        f"stale {MARKER_IN_REPO_ROOT} in project would make the repo-root probe "
        "pass from the wrong cwd"
    )
    assert not (repo_root / MARKER_IN_PROJECT).exists(), (
        f"stale {MARKER_IN_PROJECT} in repo_root would make the project-root probe "
        "pass from the wrong cwd"
    )
    (repo_root / MARKER_IN_REPO_ROOT).write_text("x\n", encoding="utf-8")
    (project / MARKER_IN_PROJECT).write_text("x\n", encoding="utf-8")


# The config file every divergent-roots row overrides, and the artifact-path body
# `install_bmad_config` and `write_repo_root_override` both write. One text, so a
# change to the artifact keys cannot reach the plain config and skip the override
# one (or the reverse) — the two would then differ in a way no row asserts.
BMAD_CONFIG_REL = Path("_bmad") / "bmm" / "config.yaml"
_ARTIFACT_PATH_KEYS = (
    "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
    "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n"
)


def write_repo_root_override(paths: ProjectPaths, code_root: Path) -> None:
    """Rewrite `_bmad/bmm/config.yaml` with a `repo_root:` pointing at `code_root`.

    The one supported divergent-roots config: `isolation = "none"` plus a
    `repo_root:` key (`bmadconfig.worktree_isolation_conflict` refuses the other
    combination, and `ProjectPaths.rebased` sets both roots, so worktree isolation
    never diverges). Overwrites rather than appends, so it is exact whether or not
    `install_bmad_config` ran first.

    `code_root` need not be a git checkout, and several rows deliberately pass a
    plain directory or a missing one.
    """
    assert code_root.is_absolute(), (
        "write_repo_root_override requires an absolute code_root: bmadconfig "
        "resolves relative configured paths against the process cwd, not the project"
    )
    config = paths.project / BMAD_CONFIG_REL
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        _ARTIFACT_PATH_KEYS
        + f"repo_root: {json.dumps(code_root.as_posix(), ensure_ascii=False)}\n",
        encoding="utf-8",
    )


NESTED_SUBDIR = "app"


def nested_repo_root_paths(paths: ProjectPaths) -> ProjectPaths:
    """The MONOREPO shape of the override: `repo_root` an ANCESTOR of `project`.

    The BMAD project lives at ``<repo>/app`` inside a checkout whose root is the
    git root — `repo_root` stays `paths.project` (the sandbox repo) while
    `project` and all three artifact dirs move under ``app/``.

    Why a second shape at all. `tests/test_verify.py::_repo_root_override` builds
    the SIBLING shape, where the artifact tree is disjoint from the code tree — so
    a code-root spelling collapses to ``()`` while a project-root spelling is a
    non-empty tail that matches nothing in the code tree. Both spellings therefore
    agree on the gate outcome and a wrong-but-plausible pathspec passes unnoticed.
    Nested, the wrong pathspec is not empty: resolved
    against the code root, ``_bmad-output/implementation-artifacts/spec-1-1-a.md``
    names the OUTER project's real artifact dir. That is the "not merely wrong, it
    is SILENTLY wrong" failure the production docstrings describe, and it is
    separable by VALUE.

    Seeds and COMMITS ``app/src.txt`` so `dev_effect` works unchanged — it
    reads `paths.project / "src.txt"` and `rev_parse_head(paths.project)`, and git
    resolves `.git` upward from the subdir. Committed rather than left untracked
    for the reason `plant_root_markers` gives: a session's edit to a TRACKED file
    is proof of work the attempt's baseline snapshot cannot absorb.

    Also writes ``app/.gitignore`` with the `bmad-loop init` run-state entry.
    Init writes that file next to the project it initializes, and the sandbox
    template's own root-anchored ``.bmad-loop/runs/`` does not match a nested one —
    so without it a nested engine run's journal would show up as untracked work.

    The subdirectory is FIXED at `NESTED_SUBDIR` rather than a parameter because
    every consumer's assertions spell the ``app/`` prefix literally. A parameter
    would make a non-default argument redden those rows on a prefix mismatch instead
    of on the contract they grade.

    Refuses input it cannot honor. It commits unconditionally, so a second call on
    the same `paths` (or one whose subdir a caller pre-created) dies inside `git`
    with a raw ``CalledProcessError`` from ``git commit`` — "nothing to commit" —
    naming neither the helper nor the precondition. Both guards below fail with
    the precondition instead.
    """
    assert paths.project == paths.repo_root, (
        "nested_repo_root_paths builds the divergence; it cannot be applied to paths "
        "that already have one. Pass the plain `project` fixture."
    )
    staged = git(paths.project, "diff", "--cached", "--name-only")
    assert not staged, (
        "nested_repo_root_paths commits its seed files and requires an empty index; "
        f"already staged: {staged}"
    )
    project = paths.project / NESTED_SUBDIR
    assert not project.exists(), (
        f"{NESTED_SUBDIR}/ already exists under {paths.project}: this helper seeds and "
        "COMMITS it, so a second call (or a caller that pre-created it) would reach "
        "`git commit` with nothing staged."
    )
    output_folder = project / "_bmad-output"
    impl = output_folder / "implementation-artifacts"
    plan = output_folder / "planning-artifacts"
    impl.mkdir(parents=True, exist_ok=True)
    plan.mkdir(parents=True, exist_ok=True)
    (project / "src.txt").write_text("original\n", encoding="utf-8")
    (project / ".gitignore").write_text(".bmad-loop/runs/\n", encoding="utf-8")
    git(paths.project, "add", f"{NESTED_SUBDIR}/src.txt", f"{NESTED_SUBDIR}/.gitignore")
    git(paths.project, "commit", "-q", "-m", f"seed the {NESTED_SUBDIR}/ project")
    return dataclasses.replace(
        paths,
        project=project,
        implementation_artifacts=impl,
        planning_artifacts=plan,
        output_folder=output_folder,
        repo_root=paths.project,
    )


UNRESOLVABLE = "stubbed: the provider is registered but not serving"


def refuse_to_resolve(monkeypatch, *targets: Path) -> None:
    """Make ``Path.resolve()`` raise WinError 64 for exactly ``targets`` — the answer
    a registered-but-not-serving WSL UNC provider gives (#529/#536), and one CPython's
    non-strict ``ntpath`` allow-list does not absorb, so ``resolve()`` fails outright
    instead of degrading to its own lexical walk. That is the #552 condition.

    Scoped to named paths on purpose: a blanket stub would break every unrelated
    resolve in the process, and a row asserting "the command survived" would then pass
    for a reason that has nothing to do with the guard under test. Also clears the
    one-note-per-process dedupe set, so a row can assert on a note a previous test in
    the same session would otherwise have consumed."""
    real = Path.resolve
    wanted = {str(t) for t in targets}

    def stub(self, strict: bool = False):
        if str(self) in wanted:
            raise OSError(0, UNRESOLVABLE, None, 64)
        return real(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", stub)
    monkeypatch.setattr(platform_util, "_LEXICAL_FALLBACK_NOTED", set())


def install_bmad_config(paths: ProjectPaths) -> None:
    """Write the _bmad/bmm/config.yaml that bmadconfig.load_paths resolves."""
    cfg = paths.project / BMAD_CONFIG_REL
    cfg.parent.mkdir(parents=True)
    cfg.write_text(_ARTIFACT_PATH_KEYS)


def _write_skill_stubs(skills: Path, catalog: dict) -> None:
    """Stub every skill in `catalog` (an install.py {skill: marker_files} map) under
    `skills`. Reading the catalog instead of restating it means a newly required
    skill or marker file fails the scaffolds loudly rather than drifting."""
    for skill, markers in catalog.items():
        d = skills / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        for marker in markers:
            (d / marker).write_text("x\n", encoding="utf-8")


RENDERER_STUB_SKILL_MD = (
    "Run `uv run {project-root}/_bmad/scripts/render_skill.py` and follow its output.\n"
)
RENDERER_WORKFLOW_MD = "Read [[bmad-snapshot:step-04-review.md]] fully.\n"
RENDERER_SCRIPT_IMPORTING_SIBLING = "from config_utils import load_central_config\n"


def install_dev_base_skills(root: Path, tree: str = ".claude/skills", *, folder_id: bool) -> Path:
    """Lay down stubs of the upstream skills the orchestrator drives on every dev run
    (`install.DEV_BASE_SKILLS`: bmad-dev-auto + the review hunters) under
    ``root/tree``, so the run-start preflight (`install.missing_base_skills`) passes.

    ``folder_id`` also writes bmad-dev-auto's step-01 carrying the dispatch marker
    `install.missing_stories_support` content-probes for — stories mode needs a newer
    bmad-dev-auto than file existence alone can prove. Returns the skills tree root."""
    from bmad_loop.install import (
        DEV_BASE_SKILLS,
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        STORIES_PROBE_TEXT,
    )

    skills = Path(root) / tree
    _write_skill_stubs(skills, DEV_BASE_SKILLS)
    if folder_id:
        (skills / STORIES_PROBE_SKILL / STORIES_PROBE_FILE).write_text(
            f"This is a **{STORIES_PROBE_TEXT}** router.\n", encoding="utf-8"
        )
    return skills


def install_build_auto_skill(
    root: Path,
    tree: str = ".claude/skills",
    *,
    folder_id: bool = True,
    renderer_stub: bool = False,
) -> Path:
    """The post-rename twin of :func:`install_dev_base_skills`: lay down the NEW dev
    primitive (`install.DEV_PRIMITIVE_NEW`) plus the review hunters under ``root/tree``.

    Deliberately lays down ONE era. A test that wants both names on disk calls this
    *and* :func:`install_dev_base_skills`; a test that wants only the legacy era calls
    that one alone. (Note :func:`install_base_skills` lays down BOTH, because
    `BASE_SKILLS` is the copy-if-present worktree catalog and names both eras — so a
    scaffold built from it resolves to the new name.)

    ``folder_id`` writes the resolved primitive's step-01 carrying the dispatch marker
    `install.missing_stories_support` content-probes for, exactly as the legacy twin
    does — under `bmad-build-auto`, since that is the name that resolves here.

    ``renderer_stub`` changes only the installed primitive's content discriminator
    and adds a complete source graph. Project-global renderer files stay explicit in
    each test so every presence gate has an observable clearing leg."""
    from bmad_loop.install import (
        DEV_BASE_SKILLS,
        DEV_PRIMITIVE_LEGACY,
        DEV_PRIMITIVE_MARKERS,
        DEV_PRIMITIVE_NEW,
        STORIES_PROBE_FILE,
        STORIES_PROBE_TEXT,
    )

    # The hunters, read off the catalog rather than restated — but with the primitive
    # entry swapped for the new name, since DEV_BASE_SKILLS is keyed on the legacy one.
    hunters = {k: v for k, v in DEV_BASE_SKILLS.items() if k != DEV_PRIMITIVE_LEGACY}
    skills = Path(root) / tree
    _write_skill_stubs(skills, {DEV_PRIMITIVE_NEW: DEV_PRIMITIVE_MARKERS, **hunters})
    primitive = skills / DEV_PRIMITIVE_NEW
    if renderer_stub:
        (primitive / "SKILL.md").write_text(RENDERER_STUB_SKILL_MD, encoding="utf-8")
        (primitive / "workflow.md").write_text(RENDERER_WORKFLOW_MD, encoding="utf-8")
    if folder_id:
        (primitive / STORIES_PROBE_FILE).write_text(
            f"This is a **{STORIES_PROBE_TEXT}** router.\n", encoding="utf-8"
        )
    return skills


def install_dev_shim(root: Path, tree: str = ".claude/skills", *, with_review: bool = True) -> Path:
    """Lay down ONLY the post-rename forwarding shim: a lone `bmad-dev-auto/SKILL.md`
    with no marker files and no new-name skill beside it.

    This is the install `bmad-loop validate` must REFUSE (`skills.base-shim`) rather
    than drive: the shim's customization-migration gate is interactive, so an
    unattended session dispatched into it HALTs having written nothing to disk.

    ``with_review`` also stubs the merged reviewer, which satisfies `_review_findings`'
    static fallback — so a shim test's findings are exactly the shim finding, and an
    assertion on their count is not silently counting absent review layers too."""
    from bmad_loop.install import DEV_PRIMITIVE_LEGACY, MERGED_REVIEW_SKILL

    skills = Path(root) / tree
    shim = skills / DEV_PRIMITIVE_LEGACY
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "SKILL.md").write_text(f"# {DEV_PRIMITIVE_LEGACY}\n", encoding="utf-8")
    if with_review:
        _write_skill_stubs(skills, {MERGED_REVIEW_SKILL: ()})
    return skills


def install_base_skills(paths: ProjectPaths, trees=(".claude/skills", ".agents/skills")) -> None:
    """Stub every non-bundled upstream skill (`install.BASE_SKILLS` — a superset of
    DEV_BASE_SKILLS that also covers what a worktree mount must copy) in each of a
    sandbox project's active CLI skill trees. Sprint mode drives any dev primitive,
    so no folder+id probe is written.

    BASE_SKILLS names BOTH primitive eras, so this lays down both and the tree
    resolves to `bmad-build-auto`. For a single-era scaffold use
    :func:`install_dev_base_skills` (legacy) or :func:`install_build_auto_skill`."""
    from bmad_loop.install import BASE_SKILLS

    for tree in trees:
        _write_skill_stubs(paths.project / tree, BASE_SKILLS)


def attach_profile(adapter, name: str = "claude", project: Path | None = None):
    """Give a scripted adapter the ``profile`` a real CLI adapter carries, so the
    seams that read ``adapter.profile.skill_tree`` — chiefly ``Engine._dev_skill``,
    which resolves the invoked dev-primitive NAME off disk — see a real skill tree.

    `MockAdapter` deliberately has no `profile` at all, and that is not an
    oversight to paper over globally: the profile-less shape IS the None-tree
    fallback path (legacy name), so it stays the default and gets pinned by its
    own test. Attach only where the resolved name is what's under test. Returns
    the adapter for chaining."""
    from bmad_loop.adapters.profile import get_profile

    adapter.profile = get_profile(name, project)
    return adapter


def fault_read_text(monkeypatch, target: Path) -> None:
    """Make exactly ``target``'s ``read_text`` raise PermissionError; every other
    path still reads normally. A selective monkeypatch rather than chmod: chmod is a
    no-op for root and carries no read bit on Windows, so the fault would silently
    not fire on half the CI matrix. ``read_bytes`` is untouched, so a test can still
    assert the faulted file's contents are unchanged."""
    real = Path.read_text

    def fake(self, *a, **kw):
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake)


def write_sprint(paths: ProjectPaths, statuses: dict[str, str]) -> None:
    doc = dict(SPRINT_TEMPLATE)
    doc["development_status"] = dict(statuses)
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def set_sprint(paths: ProjectPaths, key: str, status: str) -> None:
    doc = yaml.safe_load(paths.sprint_status.read_text())
    doc["development_status"][key] = status
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def render_deferred(items) -> str:
    """Render the post-#2640 ``deferred:`` frontmatter shape.

    Free-form values use the real YAML block scalars: ``>-`` for folded
    summary/location and ``|-`` for literal evidence. A non-dict list item and a
    dict missing ``summary`` intentionally remain expressible for malformed-item
    tests.
    """
    if not items:
        return "deferred: []\n"
    lines = ["deferred:"]
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"  - {item}")
            continue
        rendered = False
        for key in ("summary", "evidence", "location", "severity"):
            if key not in item:
                continue
            lead = "    " if rendered else "  - "
            rendered = True
            value = str(item[key])
            if not value:
                lines.append(f"{lead}{key}: ''")
            elif key == "severity":
                lines.append(f"{lead}{key}: {value}")
            else:
                style = "|-" if key == "evidence" else ">-"
                lines.append(f"{lead}{key}: {style}")
                lines.extend(f"      {line}" for line in value.splitlines())
        if not rendered:
            lines.append("  - {}")
    return "\n".join(lines) + "\n"


# `write_spec`'s "this key is not present at all" marker. A plain `None` cannot
# serve: for `legacy_baseline` it means the YAML-null shape (a bare
# `baseline_commit:` line), which is a distinct case the reader must treat as
# absent WITHOUT turning it into the token "None" (#358).
class _Omit:
    """Type of :data:`OMIT`, so a parameter accepting the sentinel can still be
    annotated. A bare ``object()`` forced ``baseline: object``, which silently
    disabled checking for every ordinary caller passing a sha."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "OMIT"


OMIT = _Omit()


def write_spec(
    path: Path,
    status: str,
    baseline: str | _Omit,
    *,
    prose_status: str | None = None,
    closes_deferred: object = None,
    operator_actions: object = None,
    deferred=None,
    legacy_baseline: str | None | _Omit = OMIT,
) -> None:
    """Write a spec the way the real bmad-dev-auto skill does. The skill's step-03
    stamps `baseline_revision` and NEVER `baseline_commit` (that name exists only
    in the orchestrator's synthesized result.json), so this fixture stamps the
    same key — a reader that only knows `baseline_commit` must fail a test here,
    not sail through production (issue #89).

    ``legacy_baseline`` adds the OTHER key, and exists because until #716 this
    fixture *could not express the bug*: a spec that carries both is exactly what
    `runs.rearm_escalation` manufactures (it inserts `baseline_revision` and never
    removes a pre-existing `baseline_commit`), and no fixture could produce one, so
    the precedence between them was untestable. ``OMIT`` writes no key at all
    (the default, and what every pre-existing caller gets — `tests/test_verify.py`
    asserts on that absence). ``None`` writes a bare ``baseline_commit:`` line, the
    YAML-null shape. Any other value is written as a quoted scalar, including ``""``
    for the empty-value shape that a ``dict.get(k, default)`` chain selects but a
    non-empty test cannot see.

    ``baseline`` accepts ``OMIT`` too, for the legacy-only spec: `baseline_revision`
    is then absent and `baseline_commit` is the only claim on the file.

    ``closes_deferred`` writes the story-declared ledger-closure field (#234): a
    list renders as a YAML flow sequence, and a bare string renders as a scalar —
    the wrong-container mistake whose handling must not depend on which file it
    was made in.

    ``operator_actions`` writes the park declaration (#335) as a real YAML BLOCK
    sequence — the shape the dev prompt asks a session for, and the shape a human
    reads back — so the reading under test is the one production sees. A non-list
    renders as a scalar, which is the wrong-container mistake for this field
    too (a bare string is iterable, so a lenient reader would turn one
    instruction into a list of characters).

    ``deferred`` adds the post-#2640 frontmatter list using
    :func:`render_deferred`. ``None`` omits the field entirely (the pre-#2640
    shape); ``[]`` emits an explicit empty list. It is rendered last so adding
    this fixture capability does not reorder either pre-existing declaration.
    """
    declare = ""
    if isinstance(closes_deferred, list):
        declare = f"closes_deferred: [{', '.join(closes_deferred)}]\n"
    elif closes_deferred is not None:
        declare = f"closes_deferred: {closes_deferred}\n"
    if isinstance(operator_actions, list):
        rendered = "".join(f"  - {a}\n" for a in operator_actions)
        declare += f"operator_actions:\n{rendered}" if rendered else "operator_actions: []\n"
    elif operator_actions is not None:
        declare += f"operator_actions: {operator_actions}\n"
    if deferred is not None:
        declare += render_deferred(deferred)
    claims = "" if baseline is OMIT else f"baseline_revision: '{baseline}'\n"
    if legacy_baseline is not OMIT:
        claims += (
            "baseline_commit:\n"
            if legacy_baseline is None
            else f"baseline_commit: '{legacy_baseline}'\n"
        )
    body = (
        f"---\ntitle: 'test'\ntype: 'feature'\nstatus: '{status}'\n"
        f"{claims}{declare}---\n\n## Intent\n\ntest spec\n"
    )
    if prose_status is not None:
        # mirror bmad-dev-auto's terminal finalize: it appends a `## Auto Run
        # Result` prose block (carrying a `Status:` line) but can leave the
        # frontmatter `status` short of the success value — the exact draft-vs-done
        # split that the orchestrator's reconcile repairs.
        body += f"\n## Auto Run Result\n\n- Status: {prose_status}\n\nSummary: test.\n"
    path.write_text(body)


def spec_path(paths: ProjectPaths, story_key: str) -> Path:
    return paths.implementation_artifacts / f"spec-{story_key}.md"


def committing_crash_state(
    paths: ProjectPaths, engine, *, post_squash: bool = False, operator_actions: list | None = None
) -> str:
    """Persist the exact state.json shape from issue #115: a task at COMMITTING
    (the save right after advance(COMMITTING), before finalize_commit / the DONE
    save that stamps commit_sha). Fully verified on disk: attempt work committed
    above baseline (only the work file — sweeping the still-untracked sprint
    board into the commit would make a later baseline reset delete it), spec at
    done, sprint synced at DEV time. review_cycle stays 0 — the
    _skip_review_and_commit path reaches COMMITTING with zero review sessions.
    With post_squash, finalize_commit already ran before the death (squashed
    commit at HEAD, clean tree) but commit_sha was never persisted.

    With ``operator_actions``, the crashed story is a PARK (#335): spec + board at
    `awaiting-operator` and the actions already latched on the task, exactly as
    `_park_awaiting_operator` leaves it before `advance(COMMITTING)`. That latch
    is the whole point — it is what the resume arm re-derives the final phase
    from, with no code of its own. Returns the baseline sha."""
    parked = bool(operator_actions)
    status = "awaiting-operator" if parked else "done"
    baseline = rev_parse_head(paths.project)
    src = paths.project / "src.txt"
    src.write_text(src.read_text() + "change for 1-1-a\n")
    git(paths.project, "add", "src.txt")
    git(paths.project, "commit", "-q", "-m", "attempt work for 1-1-a")
    sp = spec_path(paths, "1-1-a")
    write_spec(sp, status, baseline, operator_actions=operator_actions)
    write_sprint(paths, {"1-1-a": status})
    if post_squash:
        finalize_commit(paths.project, baseline, "pre-crash squash")

    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.COMMITTING, attempt=1)
    task.review_cycle = 0
    task.baseline_commit = baseline
    task.baseline_untracked = []
    task.spec_file = str(sp)
    task.operator_actions = list(operator_actions or [])
    task.record_session(
        SessionRecord(
            task_id="1-1-a-dev-1",
            role="dev",
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "escalations": [],
                "followup_review_recommended": False,
            },
        )
    )
    engine.state.tasks[task.story_key] = task
    engine._save()
    return baseline


def dev_effect(
    paths: ProjectPaths,
    story_key: str,
    *,
    final_status: str = "done",
    followup_review: bool = True,
    prose_status: str | None = None,
    seen: list[str] | None = None,
    write_src: bool = True,
    closes_deferred: object = None,
    operator_actions: object = None,
    deferred=None,
):
    """Simulate a successful bmad-dev-auto session: it self-finalizes the spec
    (no in-review handoff — always straight to ``done``) but never touches the
    bmad_loop's sprint board (the orchestrator is the single sprint-status
    writer). ``final_status`` lets a test leave the spec short of the success
    status to exercise the dev-verify gating. ``followup_review`` mirrors the
    skill's `followup_review_recommended` signal (PR #2505) — defaults True so
    the review-flow tests still run the review under the default
    ``review.trigger = "recommended"``; set False to exercise the skip path.
    ``prose_status`` appends a terminal ``## Auto Run Result`` block with that
    Status line — pair it with a non-terminal ``final_status`` to reproduce the
    skill leaving frontmatter behind its prose (the reconcile path).

    ``operator_actions`` pairs with ``final_status="awaiting-operator"`` to
    simulate a session that finished its agent-doable work and parked the rest on
    a human (#335); it is written to the spec frontmatter only, never to
    result.json, because the engine re-reads the spec for it.

    ``seen``, when given, collects `src.txt` as the session found it on entry — the
    patch-restore tests assert the re-driven session ran against the RESTORED diff.
    ``write_src=False`` then keeps the session from appending its own line, so what
    lands in the tree is exactly what the restore laid down (the applied patch is
    the session's proof of work; a second edit would muddy the assertion).

    ``deferred`` records post-#2640 review findings in the spec frontmatter; the
    simulated session still never writes the deferred-work ledger itself."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        if seen is not None:
            seen.append(source.read_text())
        if write_src:
            source.write_text(source.read_text() + f"change for {story_key}\n")
        sp = spec_path(paths, story_key)
        write_spec(
            sp,
            final_status,
            baseline,
            prose_status=prose_status,
            closes_deferred=closes_deferred,
            operator_actions=operator_actions,
            deferred=deferred,
        )
        # deliberately NO set_sprint: the dev skill does not write sprint-status
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [],
                "escalations": [],
                "followup_review_recommended": followup_review,
            },
        )

    return effect


# bmad-dev-auto is the sole dev skill, so the generic effect IS the dev effect.
# Alias kept so existing call sites that spell out the decoupled path still read.
generic_dev_effect = dev_effect


def review_effect(
    paths: ProjectPaths, story_key: str, clean: bool, patched: int = 0, finalized: bool = True
):
    """Simulate a follow-up review pass — a bmad-dev-auto re-invocation on the
    done spec (BMAD-METHOD #2508). A review pass always finalizes the spec to
    ``done`` and re-sets `followup_review_recommended`; the orchestrator
    synthesizes the result the same way it does for a dev pass. ``clean=True``
    means the pass no longer recommends a follow-up (the loop converges);
    ``clean=False`` means it still does (the orchestrator loops). ``patched`` is
    accepted for call-site compatibility and otherwise unused.

    ``finalized=False`` leaves the spec at a non-terminal ``in-progress`` status
    (and does not advance the sprint), so when the review budget is exhausted the
    post-loop ``_verify_review`` gate fails — the genuine-non-convergence path
    that defers + rolls back, as opposed to a finalized story that merely keeps
    recommending a follow-up (which the orchestrator now commits)."""

    def effect(spec: SessionSpec) -> SessionResult:
        sp = spec_path(paths, story_key)
        baseline = _spec_baseline(sp)
        status = "done" if finalized else "in-progress"
        # A review pass rewrites the status, not the whole frontmatter — carry any
        # `closes_deferred:` declaration through verbatim, as the real skill does.
        write_spec(sp, status, baseline, closes_deferred=_spec_closes_deferred(sp))
        if finalized:
            set_sprint(paths, story_key, "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": status,
                "followup_review_recommended": not clean,
                "escalations": [],
            },
        )

    return effect


def _spec_closes_deferred(path: Path) -> object:
    """The spec's `closes_deferred:` declaration as `write_spec` would re-render
    it — a list for a flow sequence, the raw text otherwise. None when absent."""
    for line in path.read_text().splitlines():
        if line.startswith("closes_deferred:"):
            value = line.split(":", 1)[1].strip()
            if value.startswith("[") and value.endswith("]"):
                return [p.strip() for p in value[1:-1].split(",") if p.strip()]
            return value
    return None


def _spec_baseline(path: Path) -> str:
    """Read back whichever baseline key a spec carries: `write_spec` stamps
    `baseline_revision` like the real skill, but hand-rolled fixture specs (and
    re-arm's re-stamp) may carry either."""
    for line in path.read_text().splitlines():
        if line.startswith(("baseline_commit:", "baseline_revision:")):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def ignore_before_commit(project: ProjectPaths, *patterns: str) -> None:
    """Append gitignore patterns, leaving the file staged for a later commit.

    Appends rather than rewrites: the sandbox template ships `.bmad-loop/runs/`,
    and clobbering it leaves the run dir tracked, so `worktree_clean()` then fails
    for a reason that has nothing to do with the test. Leaving the change
    UNCOMMITTED is what lets a following `write_ledger(..., commit=True)` succeed —
    once the rule is committed, `git add -A` stages nothing for the now-ignored
    ledger and the commit fails on an empty index.
    """
    gitignore = project.project / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8")
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    gitignore.write_text(prefix + "".join(f"{pattern}\n" for pattern in patterns), encoding="utf-8")


def crash_at_merge_back(engine, *, after: str = "merge") -> None:
    """Kill the host inside the isolated DONE arm, in one of its two windows.

    `WorktreeFlow.integrate_unit`'s DONE arm runs merge -> carry -> latch, and each
    gap has its own recovery contract:

    - ``"merge"``: after `merge_local`, before `_carry_isolated_ledger_writes`. The
      branch landed and the worktree is gone; no ledger write happened.
    - ``"carry"``: after the whole carry, before `isolated_ledger_carried` is set
      and saved. This is the window that pins call-site latching — a latch moved
      inside the base hook is already durable here, so the resume finds the task
      latched and never replays.

    `_emit("post_merge")` cannot stand in for either: it fires above the teardown,
    so the crash would land outside the window under test.

    Replaces a method on the engine INSTANCE rather than monkeypatching the class.
    `WorktreeFlow` is handed `carry_isolated_ledger_writes=lambda task:
    self._carry_isolated_ledger_writes(task)`, a late-binding lambda, so instance
    assignment is what the callback sees.
    """
    if after not in ("merge", "carry"):
        raise ValueError(f"unknown crash window: {after!r}")
    if after == "merge":

        def crash_before_carry(_task) -> None:
            raise RuntimeError("host died after merge, before the ledger carry")

        engine._carry_isolated_ledger_writes = crash_before_carry
        return

    original = engine._carry_isolated_ledger_writes

    def crash_after_carry(task) -> None:
        original(task)
        raise RuntimeError("host died after the ledger carry, before its latch")

    engine._carry_isolated_ledger_writes = crash_after_carry


# ----------------------------------------------------------- sweep helpers


def write_ledger(paths: ProjectPaths, statuses: dict[str, str], commit: bool = True) -> None:
    """Write a DW-format deferred-work ledger; statuses maps id -> status
    value. Committed by default — sweeps start from a clean tree."""
    parts = ["# Deferred Work\n"]
    for dw_id, status in statuses.items():
        parts.append(
            f"### {dw_id}: item {dw_id}\n\norigin: test, 2026-06-01\n"
            f"location: src.txt:1\nreason: test entry.\nstatus: {status}\n"
        )
    paths.deferred_work.write_text("\n".join(parts), encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "ledger")


def write_gated_ledger(paths: ProjectPaths, entries, commit: bool = True) -> None:
    """`write_ledger` plus the lines a hard gate is written on: `entries` maps a
    DW id to `(status, extra_field_lines)`, appended verbatim after `status:` so a
    test can spell a `gate:` line, a prose `HARD GATE:`, or a deliberately broken
    one exactly as a human would.

    Committed by default, like `write_ledger` and for the same reason: the engine
    paths that dispatch a story need a clean tree. `validate` reads the file
    directly and never looks at git, so its callers pass `commit=False` and skip
    the git round-trip.
    """
    parts = ["# Deferred Work\n"]
    for dw_id, (status, extra) in entries.items():
        tail = "".join(f"{line}\n" for line in extra)
        parts.append(
            f"### {dw_id}: item {dw_id}\n\norigin: test, 2026-06-01\n"
            f"location: src.txt:1\nreason: test entry.\nstatus: {status}\n{tail}"
        )
    paths.deferred_work.write_text("\n".join(parts), encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "ledger")


def mark_ledger_done(paths: ProjectPaths, dw_ids, date: str = "2026-06-11") -> None:
    from bmad_loop import deferredwork

    for dw_id in dw_ids:
        deferredwork.mark_done(paths.deferred_work, dw_id, date, "built in test")


def write_legacy_ledger(paths: ProjectPaths, text: str, commit: bool = True) -> None:
    """Write a freeform (pre-DW-format) deferred-work ledger verbatim."""
    paths.deferred_work.write_text(text, encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "legacy ledger")


def migrate_effect(paths: ProjectPaths, new_ledger_text: str, mapping):
    """Simulate a /bmad-loop-sweep --migrate session: rewrites the ledger to
    canonical DW format and reports the manifest-key -> dw_id mapping."""

    def effect(spec: SessionSpec) -> SessionResult:
        paths.deferred_work.write_text(new_ledger_text, encoding="utf-8")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "deferred-sweep-migrate",
                "mapping": list(mapping),
                "escalations": [],
            },
        )

    return effect


def bundle_spec_path(paths: ProjectPaths, name: str) -> Path:
    return paths.implementation_artifacts / f"spec-dw-{name}.md"


def triage_effect(result_json: dict):
    """Simulate a deferred-sweep triage session returning the given result."""

    def effect(spec: SessionSpec) -> SessionResult:
        return SessionResult(status="completed", result_json=result_json)

    return effect


def bundle_dev_effect(
    paths: ProjectPaths,
    name: str,
    dw_ids,
    mark_ledger: bool = False,
    followup_review: bool = True,
    final_status: str = "done",
    prose_status: str | None = None,
    deferred=None,
    write_src: bool = True,
):
    """Simulate a bmad-dev-auto bundle dev session: edits code and self-finalizes
    the bundle spec to ``done`` (no in-review handoff). On the decoupled path the
    orchestrator owns the ledger, so by default the session does NOT touch it;
    ``mark_ledger=True`` is kept only for the legacy-marking path in older tests.
    ``followup_review`` mirrors `followup_review_recommended` — defaults True so
    the bundle review runs under the default trigger = "recommended". ``final_status``
    / ``prose_status`` / ``deferred`` / ``write_src`` mirror ``dev_effect``: pair
    a non-terminal ``final_status`` with ``prose_status="done"`` to reproduce the
    skill finalizing in prose only; ``write_src=False`` expresses a session whose
    only post-baseline diff comes from orchestrator ledger bookkeeping."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        if write_src:
            source.write_text(source.read_text() + f"change for dw-{name}\n")
        sp = bundle_spec_path(paths, name)
        # mirror the skill: always self-finalize the bundle spec straight to done
        write_spec(sp, final_status, baseline, prose_status=prose_status, deferred=deferred)
        if mark_ledger:
            mark_ledger_done(paths, dw_ids)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
                "dw_ids": list(dw_ids),
                "followup_review_recommended": followup_review,
            },
        )

    return effect


def bundle_review_effect(paths: ProjectPaths, name: str, clean: bool = True):
    """Simulate a follow-up review pass over a bundle spec — a bmad-dev-auto
    re-invocation on the done bundle spec (no sprint-status entry for bundles).
    ``clean=True`` converges; ``clean=False`` keeps recommending a follow-up."""

    def effect(spec: SessionSpec) -> SessionResult:
        sp = bundle_spec_path(paths, name)
        baseline = _spec_baseline(sp)
        write_spec(sp, "done", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "status": "done",
                "followup_review_recommended": not clean,
                "escalations": [],
            },
        )

    return effect


def bundle_dev_escalates(paths: ProjectPaths, name: str, dw_ids, detail: str = "intent gap"):
    """Simulate a bmad-dev-auto bundle session that hits an intent gap during its
    inline review: it reverts its attempt, saves a patch, writes the bundle spec
    ``blocked``, and surfaces a CRITICAL escalation naming the spec — so the run
    pauses for `bmad-loop resolve --restore-patch`. ``spec_file`` in the result lets
    ``_record_dev_spec`` latch ``task.spec_file`` (the restore re-arm's in-review
    target), and the ``blocked`` status keeps the dw ids open (not synced done)."""

    def effect(spec: SessionSpec) -> SessionResult:
        sp = bundle_spec_path(paths, name)
        baseline = rev_parse_head(paths.project)
        write_spec(sp, "blocked", baseline)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "auto-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "dw_ids": list(dw_ids),
                "escalations": [
                    {"type": "bundle-item-blocked", "severity": "CRITICAL", "detail": detail}
                ],
            },
        )

    return effect


# --------------------------------------------------------- escalated-run scaffolds


@dataclass
class EscalatedRun:
    """What `escalated_run` built, so each caller can unpack only what it asserts on."""

    run_dir: Path
    state: RunState
    task: StoryTask


def escalated_run(
    project: Path,
    run_id: str = "r1",
    *,
    story_key: str = "s1",
    epic: int = 1,
    attempt: int = 1,
    review_cycle: int = 0,
    baseline_commit: str | None = None,
    started_at: str = "now",
    paused_reason: str = "CRITICAL escalation",
    source: str = "sprint-status",
    spec_file: str | None = None,
    restore_patch: str | None = None,
    sentinel_kind: str = "",
    worktree_path: str = "",
    with_session: bool = False,
    git_project: bool = False,
) -> EscalatedRun:
    """A saved RunState paused at a CRITICAL escalation, with one ESCALATED task —
    the shared shape behind test_runs / test_resolve / test_cli, whose three local
    copies had drifted into different defaults, different return tuples, and one
    unique kwarg each. Parameterized as a superset rather than lowest-common-
    denominator: every field a caller relied on is still reachable, so no test's
    fixture-specific assertion is weakened by the dedup.

    ``with_session`` appends the completed review SessionRecord the resolve-context
    builder reads. ``git_project`` makes ``state.project`` a REAL repo (spec files
    already written are committed, run state is gitignored) so `rearm_escalation`'s
    baseline snapshot refresh actually runs and `baseline_commit` defaults to HEAD.
    That refresh reads `state.code_root`, not `state.project`; the two name the same
    directory for this fixture only because the RunState below records no
    `repo_root`, and `code_root` is defined as `repo_root or project` — a caller that
    ever adds an override must git-init THAT tree, not this one. In a bare tmp_path
    the refresh's git calls raise `verify.GitError`, which re-arm swallows (a
    non-repo project must not fail re-arm) but journals as
    `rearm-baseline-advance-failed` while the old baseline stands — degraded and
    visible, not silent, so a test asserting the advance must pass ``git_project``
    rather than read a no-op as success.
    """
    project = Path(project)
    if git_project:
        (project / ".gitignore").write_text(".bmad-loop/\n")  # keep run state out of the snapshot
        git(project, "init", "-q", "-b", "main")
        git(project, "config", "user.email", "test@test")
        git(project, "config", "user.name", "test")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "initial")
        if baseline_commit is None:
            baseline_commit = git(project, "rev-parse", "HEAD")

    task = StoryTask(
        story_key=story_key,
        epic=epic,
        phase=Phase.ESCALATED,
        attempt=attempt,
        review_cycle=review_cycle,
        baseline_commit=baseline_commit,
        spec_file=spec_file,
        restore_patch=restore_patch,
        sentinel_kind=sentinel_kind,
        worktree_path=worktree_path,
    )
    if with_session:
        task.sessions.append(
            SessionRecord(task_id=f"{story_key}-review-1", role="review", status="completed")
        )
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=started_at,
        paused_reason=paused_reason,
        paused_stage=PAUSE_ESCALATION,
        paused_story_key=story_key,
        tasks={story_key: task},
        source=source,
    )
    run_dir = project / ".bmad-loop" / "runs" / run_id
    save_state(run_dir, state)
    return EscalatedRun(run_dir=run_dir, state=state, task=task)
