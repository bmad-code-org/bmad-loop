"""DW-390: seeding a CLI's home-level workspace-trust allowlist per worktree.

Every row runs against a temp HOME (HOME + USERPROFILE, so ``Path.home()`` lands
there on both platforms) holding a temp agy-shaped settings file.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from bmad_loop import workspace_trust
from bmad_loop.adapters.profile import WorkspaceTrustSpec
from bmad_loop.workspace_trust import WorkspaceTrustError, seed, settings_file, trust_status

SPEC = WorkspaceTrustSpec(
    settings_path="~/.gemini/antigravity-cli/settings.json", key="trustedWorkspaces"
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture
def repo(tmp_path) -> Path:
    r = (tmp_path / "repo").resolve()
    r.mkdir()
    return r


@pytest.fixture
def worktree(repo) -> Path:
    wt = repo / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1-1-a"
    wt.mkdir(parents=True)
    return wt


def write_settings(home: Path, doc: object | str) -> Path:
    path = home / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc, indent=2)
    path.write_text(text, encoding="utf-8")
    return path


def read_doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_settings_file_is_anchored_at_home(home):
    assert settings_file(SPEC) == home / ".gemini" / "antigravity-cli" / "settings.json"


# ------------------------------------------------------------------- seed rows


def test_seed_appends_worktree_and_preserves_other_keys_in_order(home, repo, worktree):
    path = write_settings(
        home,
        {"theme": "dark", "trustedWorkspaces": [str(repo)], "model": {"name": "x"}, "z": 1},
    )

    outcome, _reason = seed(SPEC, worktree, trusted_root=repo)

    assert outcome == "seeded"
    doc = read_doc(path)
    assert list(doc) == ["theme", "trustedWorkspaces", "model", "z"]
    assert doc["theme"] == "dark" and doc["model"] == {"name": "x"} and doc["z"] == 1
    assert doc["trustedWorkspaces"] == [str(repo), str(worktree.resolve())]
    # no lock/temp file left beside the settings file
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_seed_keeps_the_settings_file_mode(home, repo, worktree):
    path = write_settings(home, {"trustedWorkspaces": [str(repo)]})
    path.chmod(0o600)

    assert seed(SPEC, worktree, trusted_root=repo)[0] == "seeded"

    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("spelling", ["resolved", "as-passed"])
def test_seed_is_idempotent_for_either_spelling(home, repo, worktree, spelling):
    listed = str(worktree.resolve()) if spelling == "resolved" else str(worktree)
    path = write_settings(home, {"trustedWorkspaces": [str(repo), listed]})
    before = path.read_bytes()

    assert seed(SPEC, worktree, trusted_root=repo)[0] == "present"

    assert path.read_bytes() == before


def test_seeding_twice_writes_one_entry(home, repo, worktree):
    path = write_settings(home, {"trustedWorkspaces": [str(repo)]})
    assert seed(SPEC, worktree, trusted_root=repo)[0] == "seeded"
    after_first = path.read_bytes()

    assert seed(SPEC, worktree, trusted_root=repo)[0] == "present"

    assert path.read_bytes() == after_first
    assert read_doc(path)["trustedWorkspaces"].count(str(worktree.resolve())) == 1


def test_root_untrusted_writes_nothing(home, repo, worktree):
    """The root-trust gate. Ablation: drop the `_spellings(trusted_root)` check in
    `seed` and this appends the worktree — the test fails."""
    path = write_settings(home, {"trustedWorkspaces": ["/somewhere/else"]})
    before = path.read_bytes()

    outcome, reason = seed(SPEC, worktree, trusted_root=repo)

    assert outcome == "root-untrusted"
    assert "project root is not listed" in reason
    assert path.read_bytes() == before


def test_root_trusted_via_its_resolved_spelling(home, tmp_path, repo, worktree):
    link = tmp_path / "repo-link"
    try:
        link.symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    write_settings(home, {"trustedWorkspaces": [str(repo)]})

    assert seed(SPEC, worktree, trusted_root=link)[0] == "seeded"


def test_missing_file_is_root_untrusted_and_creates_nothing(home, repo, worktree):
    outcome, reason = seed(SPEC, worktree, trusted_root=repo)

    assert outcome == "root-untrusted" and "does not exist" in reason
    assert list(home.iterdir()) == []


def test_missing_key_is_root_untrusted_and_writes_nothing(home, repo, worktree):
    path = write_settings(home, {"theme": "dark"})
    before = path.read_bytes()

    outcome, reason = seed(SPEC, worktree, trusted_root=repo)

    assert outcome == "root-untrusted" and "trustedWorkspaces" in reason
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"[1, 2]",
        b'"a string"',
        b'{"trustedWorkspaces": "/one/path"}',
        b'{"trustedWorkspaces": ["/ok", 3]}',
        b'{"trustedWorkspaces": {"a": 1}}',
        b"\xff\xfe{}",
        b'{"trustedWorkspaces": ' + b"1" * 5000 + b"}",
    ],
    ids=[
        "invalid-json",
        "array",
        "string",
        "key-str",
        "key-mixed",
        "key-object",
        "bad-utf8",
        "int-digit-limit",
    ],
)
def test_malformed_file_raises_and_writes_nothing(home, repo, worktree, raw):
    path = write_settings(home, "{}")
    path.write_bytes(raw)

    with pytest.raises(WorkspaceTrustError):
        seed(SPEC, worktree, trusted_root=repo)

    assert path.read_bytes() == raw


def test_undeterminable_home_is_a_typed_fault(repo, worktree, monkeypatch):
    """`Path.home()` raises RuntimeError with HOME/USERPROFILE unset; that must
    reach the engine as an escalation and the probe as a verdict, not a crash."""

    def no_home(*_a, **_k):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)

    with pytest.raises(WorkspaceTrustError, match="home directory"):
        seed(SPEC, worktree, trusted_root=repo)
    assert trust_status(SPEC, repo)[0] == "unverifiable"


def test_write_fault_raises_typed(home, repo, worktree, monkeypatch):
    write_settings(home, {"trustedWorkspaces": [str(repo)]})

    def boom(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(workspace_trust, "atomic_write_text", boom)

    with pytest.raises(WorkspaceTrustError, match="cannot write"):
        seed(SPEC, worktree, trusted_root=repo)


def test_a_write_that_does_not_land_raises(home, repo, worktree, monkeypatch):
    """The post-write re-read: a write the file does not show is a fault, not a
    silent success."""
    write_settings(home, {"trustedWorkspaces": [str(repo)]})
    monkeypatch.setattr(workspace_trust, "atomic_write_text", lambda *_a, **_k: None)

    with pytest.raises(WorkspaceTrustError, match="did not persist"):
        seed(SPEC, worktree, trusted_root=repo)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_a_symlinked_settings_file_is_followed(home, tmp_path, repo, worktree):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"trustedWorkspaces": [str(repo)]}), encoding="utf-8")
    link = home / ".gemini" / "antigravity-cli" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    assert seed(SPEC, worktree, trusted_root=repo)[0] == "seeded"

    assert link.is_symlink()
    assert str(worktree.resolve()) in read_doc(real)["trustedWorkspaces"]


def test_concurrent_seeds_all_land(home, repo):
    path = write_settings(home, {"trustedWorkspaces": [str(repo)]})
    worktrees = []
    for i in range(8):
        wt = repo / "wt" / str(i)
        wt.mkdir(parents=True)
        worktrees.append(wt)
    errors: list[BaseException] = []

    def run(wt: Path) -> None:
        try:
            seed(SPEC, wt, trusted_root=repo)
        except BaseException as e:  # noqa: BLE001 — surfaced below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(wt,)) for wt in worktrees]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    listed = read_doc(path)["trustedWorkspaces"]
    assert set(listed) == {str(repo), *(str(wt.resolve()) for wt in worktrees)}


# ---------------------------------------------------------- trust_status (probe)


def test_trust_status_trusted(home, repo):
    write_settings(home, {"trustedWorkspaces": [str(repo)]})
    status, reason = trust_status(SPEC, repo)
    assert status == "trusted"
    assert str(repo) not in reason


def test_trust_status_untrusted_when_absent(home, repo, worktree):
    write_settings(home, {"trustedWorkspaces": [str(repo)]})
    status, reason = trust_status(SPEC, worktree)
    assert status == "untrusted"
    assert str(worktree) not in reason


def test_trust_status_untrusted_when_file_missing(home, repo):
    assert trust_status(SPEC, repo)[0] == "untrusted"


def test_trust_status_unverifiable_when_malformed(home, repo):
    path = write_settings(home, "{nope")
    before = path.read_bytes()
    assert trust_status(SPEC, repo)[0] == "unverifiable"
    assert path.read_bytes() == before


def test_trust_status_never_writes(home, repo):
    path = write_settings(home, {"trustedWorkspaces": []})
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    trust_status(SPEC, repo)
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before


# ------------------------------------------------- agy's honored spellings


@pytest.mark.parametrize(
    ("platform", "cwd", "entry", "honored"),
    [
        # Windows arm: the raw cwd, or Go's Clean + ToSlash of it — byte-exact.
        ("win32", "C:\\Users\\A\\wt", "C:\\Users\\A\\wt", True),
        ("win32", "C:\\Users\\A\\wt", "C:/Users/A/wt", True),
        ("win32", "C:\\Users\\A\\wt\\", "C:/Users/A/wt", True),
        ("win32", "C:\\Users\\A\\wt\\..\\wt", "C:/Users/A/wt", True),
        ("win32", "C:\\Users\\A\\wt", "c:\\users\\a\\wt", False),
        ("win32", "C:\\Users\\A\\wt", "c:/Users/A/wt", False),
        ("win32", "C:\\Users\\A\\wt", "C:\\Users\\A\\wt\\", False),
        ("win32", "C:\\Users\\A\\wt", "C:/Users/A/wt/", False),
        ("win32", "C:\\Users\\A\\wt", "\\\\?\\C:\\Users\\A\\wt", False),
        # POSIX arm: the raw cwd or its Clean form, case-sensitive.
        ("linux", "/home/a/wt", "/home/a/wt", True),
        ("linux", "/home/a/wt/", "/home/a/wt", True),
        ("linux", "//home/a/wt", "/home/a/wt", True),
        ("linux", "/home/a/wt", "/home/A/wt", False),
        ("linux", "/home/a/wt", "/home/a/wt/", False),
        ("darwin", "/Users/a/wt", "/users/a/wt", False),
    ],
)
def test_honored_spellings_match_agys_rule(platform, cwd, entry, honored):
    assert (entry in workspace_trust.honored_spellings(cwd, platform)) is honored


def test_a_case_variant_entry_trusts_nothing(home, repo, worktree):
    # agy compares bytes: a case-folded spelling of the same directory (one
    # directory on Windows and macOS) is not a grant for bmad-loop's spelling.
    write_settings(home, {"trustedWorkspaces": [str(repo).swapcase()]})
    assert trust_status(SPEC, repo)[0] == "untrusted"
    assert seed(SPEC, worktree, trusted_root=repo)[0] == "root-untrusted"


@pytest.mark.skipif(sys.platform != "win32", reason="forward-slash entries are a Windows spelling")
def test_a_forward_slash_root_entry_is_honored(home, repo, worktree):
    path = write_settings(home, {"trustedWorkspaces": [str(repo).replace("\\", "/")]})
    assert trust_status(SPEC, repo)[0] == "trusted"
    assert seed(SPEC, worktree, trusted_root=repo)[0] == "seeded"
    assert read_doc(path)["trustedWorkspaces"][-1] == str(worktree.resolve())
