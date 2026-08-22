"""Installed-entry-point + exit-code characterization (issue #240, findings F-7/F-8).

Two things every other CLI test leaves uncovered:

- **Packaging.** Every test in ``test_cli.py`` calls ``cli.main()`` in-process, so a
  startup import cycle or broken script wiring is invisible to the suite. The three
  ``test_module_*`` cases below are the deliberate exception: they spawn a real
  interpreter (``sys.executable -m bmad_loop``) so ``python -m bmad_loop`` — and, by
  extension, the ``bmad-loop`` console script it mirrors — is exercised end to end.
  Do not convert these to in-process ``cli.main()`` calls; the subprocess *is* the test.

- **Exit-code semantics.** ``main()`` maps the typed error surface and the broad
  backstop both to rc 1, argparse usage errors to rc 2, and a Ctrl+C escaping
  ``engine.run()`` to rc 130 — each named by the ``ExitCode`` enum (#241). The
  ``test_exit_*`` cases pin the rc for each path, a guard rail for the exit-code
  taxonomy and the later cli.py composition extraction (#243).
"""

import json
import subprocess
import sys

import pytest

from bmad_loop import __version__, bmadconfig, cli
from bmad_loop import policy as policy_mod
from bmad_loop import sprintstatus, verify


def _run_module(*args, cwd):
    """Spawn ``python -m bmad_loop`` under the test interpreter.

    ``cwd`` is a per-test temp dir (never the repo root), so ``-m``'s implicit
    ``sys.path[0]`` cannot shadow the installed package — the module resolves via
    the same install the ``bmad-loop`` script would use. ``sys.executable`` +
    per-test ``cwd``/``--project`` keeps these xdist-safe: no shared writable state.
    """
    return subprocess.run(
        [sys.executable, "-m", "bmad_loop", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=str(cwd),
    )


# ----------------------------------------------------------------- packaging smoke


def test_module_version_smoke(tmp_path):
    """``python -m bmad_loop --version`` prints the package version to stdout, rc 0."""
    proc = _run_module("--version", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"bmad-loop {__version__}"


def test_module_help_smoke(tmp_path):
    """``python -m bmad_loop --help`` prints usage under the ``bmad-loop`` prog, rc 0."""
    proc = _run_module("--help", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "usage: bmad-loop" in proc.stdout


def test_module_validate_json_smoke(tmp_path):
    """``validate --json`` in an empty project: rc 1 (the bmad-config gate fails) but
    stdout is still a whole, parseable document — the one --json command that owes a
    document at a nonzero exit. Proves the machine surface survives the module entry."""
    proc = _run_module("validate", "--json", "--project", str(tmp_path), cwd=tmp_path)
    assert proc.returncode == 1, proc.stderr
    doc = json.loads(proc.stdout)  # whole-stdout parse == stdout-purity assertion
    assert doc["schema_version"] == 1
    assert doc["ok"] is False


# The child of test_extra_less_install_core_works_and_tui_hints. Kept as a module
# constant so later assertions can be appended to one numbered list; sys.argv[1] is
# an empty project dir the parent creates.
_EXTRA_LESS_PROGRAM = r"""
import sys

BLOCKED = {"pyte", "rich", "textual", "tomlkit"}


class _Blocker:
    def find_spec(self, fullname, path=None, target=None):
        # RAISE rather than return None: returning None only defers to the next
        # finder, and the dev venv HAS these installed -- every assertion below
        # would then pass vacuously against the real packages.
        if fullname.partition(".")[0] in BLOCKED:
            raise ModuleNotFoundError("No module named %r" % fullname, name=fullname)
        return None


for _name in [m for m in sys.modules if m.partition(".")[0] in BLOCKED]:
    del sys.modules[_name]
sys.meta_path.insert(0, _Blocker())

# Vacuity floor: if the blocker is not biting, nothing below proves anything.
try:
    import pyte
except ModuleNotFoundError:
    pass
else:
    sys.exit("VACUOUS: the blocker did not stop `import pyte`")

# (a) the core CLI module imports with the extra absent
import bmad_loop.cli

# (c) `list` answers from the core dependencies alone
rc = bmad_loop.cli.main(["list", "--project", sys.argv[1]])
if rc != 0:
    sys.exit("list returned rc %r, expected 0" % rc)
"""


def test_extra_less_install_core_works_and_tui_hints(tmp_path):
    """The core CLI surface works with the ``[tui]`` extra absent (#650).

    A core-only ``uv tool install bmad-loop`` has no pyte/rich/textual/tomlkit, but
    every test job installs ``--all-extras`` and the packaging job's isolated wheel
    run only calls ``--version`` — which argparse answers before any subcommand code
    imports. That blind spot let ``ls`` crash on a missing ``pyte`` for 23 releases.
    So: a fresh interpreter (the subprocess *is* the test, per this module's header)
    installs an import-system blocker for those four packages, proves the blocker
    bites, then runs the paths a core-only install actually takes.

    Ablation: restore cmd_list's ``from .tui.data import discover_runs`` and (c)
    fails -- the blocked ``pyte`` import escapes and ``list`` exits nonzero.
    """
    project = tmp_path / "project"
    project.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _EXTRA_LESS_PROGRAM, str(project)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "no runs found" in proc.stdout


# ------------------------------------------------------- exit-code characterization


@pytest.mark.parametrize(
    "exc",
    [
        bmadconfig.BmadConfigError,
        sprintstatus.SprintStatusError,
        policy_mod.PolicyError,
        verify.GitError,
    ],
)
def test_exit_typed_error_is_1(tmp_path, capsys, monkeypatch, exc):
    """Every exception in the first ``except`` tuple → rc 1, message on stderr.
    Patched onto the dispatched handler (``main`` binds ``args.func`` from the module
    global at parse time), so the raise happens inside the try that maps it."""

    def boom(_args):
        raise exc("boom")

    monkeypatch.setattr(cli, "cmd_validate", boom)
    assert cli.main(["validate", "--project", str(tmp_path)]) == 1
    assert "error: boom" in capsys.readouterr().err


def test_exit_broad_backstop_is_1(tmp_path, capsys, monkeypatch):
    """A non-typed exception falls through to the broad ``except Exception`` backstop
    — also rc 1, message on stderr, never a bare traceback to the parked pane."""

    def boom(_args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "cmd_validate", boom)
    assert cli.main(["validate", "--project", str(tmp_path)]) == 1
    assert "error: unexpected" in capsys.readouterr().err


def test_exit_argparse_usage_error_is_2(capsys):
    """An argparse usage error (unknown subcommand) exits 2 via SystemExit — raised by
    ``parse_args`` before the dispatch try, so it propagates rather than mapping to 1."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["definitely-not-a-command"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_exit_keyboard_interrupt_is_130(tmp_path, capsys, monkeypatch):
    """Ctrl+C escaping ``main()`` outside ``engine.run()`` → rc 130 (128+SIGINT), a
    one-line stderr notice, no traceback. Patched onto the dispatched handler so the
    raise lands inside the dispatch try — the same escape route a config-load or
    engine-construction interrupt takes. (The in-run interrupt is ``engine.run()``'s
    own clean RunStopped, which this PR must not touch — see engine.py.)"""

    def boom(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_validate", boom)
    # --json is the strictest stdout contract: a --json command must emit *no* partial
    # document on interrupt, only the whole document or nothing (machine.py). The raise
    # lands before any output, so stdout stays empty and stderr carries just the notice.
    assert cli.main(["validate", "--json", "--project", str(tmp_path)]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""  # empty stdout: nothing half-emitted under --json rules
    assert captured.err == "interrupted\n"  # exactly one line, no stray whitespace
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------- ExitCode enum


def test_exit_code_enum_values():
    """``ExitCode`` names the released rc numbers — the values are the contract, so
    pin them literally (a rename is fine; a renumber is a breaking change)."""
    assert cli.ExitCode.OK == 0
    assert cli.ExitCode.FAILURE == 1
    assert cli.ExitCode.USAGE == 2
    assert cli.ExitCode.INTERRUPTED == 130


def test_exit_code_enum_has_only_contracted_codes():
    """OK/FAILURE/USAGE/INTERRUPTED are the whole contract — codes 3-129 and 131+ are
    deferred until a consumer needs them, so guard against one sneaking in early."""
    assert {c.value for c in cli.ExitCode} == {0, 1, 2, 130}


def test_exit_code_enum_is_int_returnable():
    """``main() -> int`` and ``sys.exit`` both consume the members transparently:
    an ``IntEnum`` member *is* its int, so the failure paths can return it as-is."""
    assert isinstance(cli.ExitCode.FAILURE, int)
    assert cli.ExitCode.FAILURE == 1
