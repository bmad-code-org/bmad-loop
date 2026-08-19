"""Contract tests for the shared fixtures and host-capability gates in
`tests/conftest.py`.

`project` hands every test a copytree clone of a session-scoped template repo, so
the template's shape is a shared dependency of most of the suite. What is pinned
here is the part of that shape other modules rely on without asserting it.

The gates need the same treatment for a sharper reason: `opencode_runs` decides
whether an entire `*_live.py` module runs or skips, and nothing downstream can
notice when it answers wrongly — a gate that wrongly says "absent" reports a
tidy skip, not a failure. Its call shape and each of its three refusals are
therefore pinned here, one fact per row.
"""

from __future__ import annotations

import json
import os
import subprocess

import conftest
import pytest
from conftest import make_git_noisy

from bmad_loop import verify


def test_template_drops_sample_hooks_but_keeps_hooks_dir_and_exclude(project):
    """`git init` seeds 14 dead `*.sample` hooks that nothing reads; the template
    deletes the files so each per-test copy stops replicating them.

    Both halves are load-bearing, and they pull against each other: the obvious
    shortcut for the first (`git init --template=` pointed at an empty dir) also
    removes `.git/hooks/` itself and `.git/info/exclude`, which the suite does
    depend on — three sites write `.git/hooks/pre-commit` with no `mkdir`
    (tests/test_engine.py twice, tests/test_verify.py once) and
    tests/test_install.py reads `.git/info/exclude`. Deleting the sample files
    is therefore the only cleanup that satisfies both.

    Ablation target: delete the `sample.unlink()` loop from `_project_template`
    and the first assertion fails alone; swap that loop for an empty
    `git init --template=` and the first assertion passes while the two
    survival assertions fail instead — disjoint failures, which is what proves
    the two halves are independent rather than one implying the other."""
    git_dir = project.project / ".git"
    hooks = git_dir / "hooks"

    assert list(hooks.glob("*.sample")) == []
    assert hooks.is_dir()
    assert (git_dir / "info" / "exclude").is_file()


def test_template_leaves_no_detached_git_maintenance_writing_into_the_copies(project, tmp_path):
    """No background git process may outlive a commit into the sandbox.

    `git commit` normally ends by spawning `git maintenance run --auto --quiet
    --detach`. Detached, it outlives the command that started it and keeps writing
    under `.git/objects/` — and the template it writes into is exactly what
    `project` copytrees for every test. `objects/maintenance.lock` gets listed by
    scandir, unlinked by that child, then opened by copy2 and is already gone, so
    one arbitrary unrelated test dies at fixture setup on `[Errno 2]`. It reddens a
    different test each time and only on whichever interpreter leg loses the race,
    which is the flake signature this suite treats as a bug.

    Graded on the behavior, not on the config key: reading back
    `maintenance.auto` would pass on a git that had stopped honouring it. This
    commits into a real copy under GIT_TRACE2 and pins the child list instead.

    Ablation target: delete the `maintenance.auto` line from `_project_template`
    and this row fails alone, naming the spawned `git maintenance run` in the
    assertion message. The trace-recorded-the-commit assertion is the anti-vacuity
    guard — without it a git built without trace2, or any typo in the env var,
    would write no events and the row would pass for having observed nothing."""
    repo = project.project
    (repo / "src.txt").write_text("changed\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    trace = tmp_path / "trace2.json"
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "second"],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TRACE2_EVENT": str(trace)},
    )

    events = []
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:  # trace2 writes one JSON object per line; skip any partial
            continue

    # Anti-vacuity: the trace really did observe this commit.
    assert any(
        e.get("event") == "cmd_name" and e.get("name") == "commit" for e in events
    ), f"GIT_TRACE2 recorded no commit; nothing was actually observed: {events}"

    spawned = [" ".join(e.get("argv") or []) for e in events if e.get("event") == "child_start"]
    assert not [c for c in spawned if "maintenance" in c or "gc" in c], spawned


def test_make_git_noisy_produces_rc_zero_stderr(project):
    """The anti-vacuity guard for the suite's only host-noise dimension (#442).

    `make_git_noisy` is what makes the merged and the stdout-alone reads
    distinguishable; its whole premise is that an unknown VALUE for a known config
    KEY is a `warning:` on stderr at rc 0, not an error. If a future git ever stops
    emitting it, THIS test fails loudly — instead of the four tests that depend on
    the helper all passing for the wrong reason, with the bug restored. Do not
    delete it as a duplicate of them: it is the only row that would notice.

    Ablation target: delete the `git config` line from `make_git_noisy` and the
    premise is dead — the helper's own probe catches it, so this row and the four
    that depend on the helper all report SKIPPED, none PASSED. Delete the probe as
    well, so nothing masks the dead premise, and this row FAILS on the stderr
    assertion. Deleting the probe ALONE reddens nothing while the host git still
    warns, and that is the point rather than a gap: the probe is what turns a future
    silent git into four skips instead of four false greens, and this row into the
    one loud failure."""
    repo = project.project
    make_git_noisy(repo)

    proc = verify._run_git(["git", "-C", str(repo), "rev-parse", "HEAD"], repo)

    assert proc.returncode == 0  # a warning, not a failure
    assert proc.stderr.strip()  # git really did write to stderr
    sha = proc.stdout.strip()
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_opencode_gate_probes_the_resolved_binary(monkeypatch):
    """The one row that owns the probe's call shape.

    `opencode_runs` decides whether `tests/test_opencode_live.py` runs at all,
    and the shape of this single call is what makes that decision mean
    anything: the resolved path rather than the bare name (`which` already
    answered that question), a bounded `timeout` so a wedged shim cannot hang
    collection, `check=False` so a nonzero exit arrives as data instead of an
    exception the caller never asked to handle, and `stdin=DEVNULL` so a shim
    that prompts is refused immediately instead of stalling for the full
    timeout on the runner's inherited tty.

    The `kwargs` assertions are deliberately a SUBSET, not a dict equality:
    equality would make deleting `timeout=10` redden this row and both refusal
    rows at once, grading none of them. Each fact is graded here and only here,
    and an additive kwarg stays free.

    Ablation target: delete `timeout=10` from the `subprocess.run` call and this
    test fails alone, on `KeyError: 'timeout'`; delete `stdin=subprocess.DEVNULL`
    and it fails alone the same way. Neither mutation is visible to any other
    row in this file."""
    calls = []

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(conftest.subprocess, "run", probe)

    assert conftest.opencode_runs()

    ((command, kwargs),) = calls
    assert command == ["/usr/bin/opencode", "--version"]
    assert kwargs["timeout"] == 10
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_opencode_gate_refuses_a_binary_that_exits_nonzero(monkeypatch):
    """#294 itself: the dead shim `shutil.which` resolves without complaint.

    A stale WSL interop stub, or an npm wrapper whose target was uninstalled,
    still occupies a PATH entry and still answers `--version` — nonzero. Before
    the probe the live module read that as an install and ran the entire smoke
    against something that could never serve a session.

    Ablation target: replace `return probe.returncode == 0` with `return True`
    and this test fails alone, on the leading `not` — the call-shape row still
    sees its one correctly-shaped call, and both launch-fault parameters still
    return False out of the `except` without reaching the changed line."""
    calls = []

    def failed_probe(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode=2)

    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(conftest.subprocess, "run", failed_probe)

    assert not conftest.opencode_runs()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [OSError("broken shim"), subprocess.TimeoutExpired("opencode", timeout=10)],
    ids=["launch-fault", "timeout"],
)
def test_opencode_gate_refuses_a_binary_that_cannot_be_launched(monkeypatch, error):
    """The two ways a resolved path fails before it can exit at all: the exec
    faults (`OSError` — a shim naming a deleted interpreter, a dropped mount),
    or it never returns inside the bound (`TimeoutExpired`). Both are host-shaped
    absence rather than a suite defect, so both have to become a skip — an
    exception here escapes at module import of the live suite, where it is an
    error, not a skip.

    Ablation target: delete the `except (OSError, subprocess.SubprocessError):
    return False` and this test fails alone, in BOTH parameters, on the escaped
    exception. `TimeoutExpired` is what proves the `SubprocessError` half of the
    tuple is load-bearing: it is not an `OSError`, so an `except OSError` alone
    reddens that parameter and only that one."""
    calls = []

    def raise_error(command, **kwargs):
        calls.append((command, kwargs))
        raise error

    monkeypatch.setattr(conftest.sys, "platform", "linux")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(conftest.subprocess, "run", raise_error)

    assert not conftest.opencode_runs()
    assert len(calls) == 1


def test_opencode_gate_answers_win32_without_touching_the_host(monkeypatch):
    """The win32 early-out, which nothing else in the suite grades.

    opencode-on-Windows is unverified for this adapter (README adapter table),
    so the answer there is False by policy — and it has to be reached before the
    PATH lookup and before the probe, because Windows CI should pay for
    neither. Poisoning both `shutil.which` and `subprocess.run` is how the
    ordering is asserted rather than just the return value: either one being
    reached is an `AssertionError`.

    Ablation target: delete the `if sys.platform == "win32": return False` early
    return and this test fails alone, on the `AssertionError` the poisoned
    `shutil.which` raises. The other three rows all pin `platform` to "linux" to
    stay host-independent, so they stay GREEN under that same mutation — which
    is the whole reason this row exists: without it, deleting the early return
    leaves this file, and the suite, entirely green."""

    def refuse(*args, **kwargs):
        raise AssertionError("win32 must answer before any PATH lookup or probe")

    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setattr(conftest.shutil, "which", refuse)
    monkeypatch.setattr(conftest.subprocess, "run", refuse)

    assert not conftest.opencode_runs()
