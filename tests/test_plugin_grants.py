"""Grant records and the pure trust verdict (#496/#497).

`classify` is the single decision every later gate consumes, so each truth-table
row has its own test. The negative rows matter most: a never-granted plugin is
refused without being hashed, and only builtin/entry-point provenance skips the
grant — an unknown source must need one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmad_loop.plugins import PluginError
from bmad_loop.plugins.digest import VerifiedTree, read_tree
from bmad_loop.plugins.grants import (
    GrantRecord,
    GrantSet,
    TrustState,
    Verdict,
    classify,
    decode_grants,
    encode_grants,
    exec_surface,
    fatal_message,
    needs_grant,
)
from bmad_loop.plugins.model import HookSpec, PluginManifest, PythonSpec, WorkflowSpec

REL = ".bmad-loop/plugins/rogue"
HOOKED = PluginManifest(name="rogue", hooks=(HookSpec(stage="post_story", cmd="true"),))


def make_tree(tmp_path: Path, body: str = "echo hi\n") -> VerifiedTree:
    root = tmp_path / "rogue"
    root.mkdir(exist_ok=True)
    (root / "plugin.toml").write_text('[plugin]\nname = "rogue"\n')
    (root / "hook.sh").write_text(body)
    return read_tree(root)


def grant_for(tree: VerifiedTree, path: str = REL) -> GrantRecord:
    return GrantRecord.from_tree("rogue", path, tree, "2026-10-10T00:00:00Z")


def never_called() -> VerifiedTree:
    raise AssertionError("an ungranted plugin must never be hashed")


def run(m=HOOKED, *, enabled=(), record=None, live=never_called) -> Verdict:
    return classify(m, enabled=frozenset(enabled), record=record, rel_path=REL, live=live)


# ------------------------------------------------------------ codec


def test_grant_codec_round_trips(tmp_path):
    record = grant_for(make_tree(tmp_path))
    other = GrantRecord.from_tree("alpha", "x/alpha", make_tree(tmp_path, "b\n"), "t")
    text = encode_grants([record, other])
    assert json.loads(text)["version"] == 1
    assert decode_grants(text) == (other, record)  # sorted by name
    assert encode_grants(decode_grants(text)) == text


def test_encode_rejects_duplicate_names(tmp_path):
    record = grant_for(make_tree(tmp_path))
    with pytest.raises(ValueError, match="duplicate"):
        encode_grants([record, record])


SHA = "a" * 64


def _store(**entry) -> str:
    base = {"path": REL, "digest": SHA, "files": {}, "granted_at": "t"}
    return json.dumps({"version": 1, "plugins": {"rogue": {**base, **entry}}})


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"version": 1}),
        json.dumps({"version": 2, "plugins": {}}),
        json.dumps({"version": True, "plugins": {}}),
        json.dumps({"version": 1, "plugins": []}),
        json.dumps({"version": 1, "plugins": {}, "extra": 1}),
        json.dumps({"version": 1, "plugins": {"rogue": []}}),
        json.dumps({"version": 1, "plugins": {"": {}}}),
        _store(path=""),
        _store(path=3),
        _store(digest="nothex"),
        _store(files=[]),
        _store(files={"a": "short"}),
        _store(files={"a": SHA + ":y"}),
        _store(granted_at=None),
        _store(surprise=1),
        _store(files={"a": SHA}),  # digest does not match the file list
    ],
)
def test_decode_rejects_malformed_shapes(text):
    with pytest.raises(ValueError):
        decode_grants(text)


def test_grant_set_lookup():
    record = GrantRecord("a", "p", SHA, (), "t")
    grants = GrantSet(records=(record,))
    assert grants.get("a") is record
    assert grants.get("b") is None
    assert GrantSet.empty() == GrantSet((), None, None)


# ------------------------------------------------------------ surface


@pytest.mark.parametrize(
    "manifest, surface",
    [
        (PluginManifest(name="p"), False),
        (HOOKED, True),
        (PluginManifest(name="p", workflows=(WorkflowSpec(name="w", stage="s"),)), True),
        (PluginManifest(name="p", python=PythonSpec(module="m.py")), True),
        (PluginManifest(name="p", seed_files=("a",)), True),
        (PluginManifest(name="p", seed_globs=("*.a",)), True),
    ],
)
def test_exec_surface_covers_every_launch_path(manifest, surface):
    assert exec_surface(manifest) is surface


@pytest.mark.parametrize(
    "source, needs",
    [("builtin", False), ("entry_point", False), ("project", True), ("weird", True)],
)
def test_only_builtin_and_entry_point_skip_the_grant(source, needs):
    m = PluginManifest(name="rogue", hooks=HOOKED.hooks, source=source)
    assert needs_grant(m) is needs
    verdict = run(m)
    assert verdict.active is not needs


# ------------------------------------------------------------ classify, one test per state


def test_builtin_is_trusted_by_provenance():
    m = PluginManifest(name="tea", hooks=HOOKED.hooks, source="builtin")
    verdict = run(m)
    assert verdict.state is TrustState.PROVENANCE and verdict.active and not verdict.fatal


def test_builtin_python_enabled_is_trusted_by_provenance():
    m = PluginManifest(name="tea", python=PythonSpec(module="m.py"), source="builtin")
    assert run(m, enabled=("tea",)).state is TrustState.PROVENANCE


def test_unenabled_builtin_python_is_unenabled():
    m = PluginManifest(name="tea", python=PythonSpec(module="m.py"), source="builtin")
    verdict = run(m)
    assert verdict.state is TrustState.UNENABLED
    assert not verdict.active and not verdict.fatal


def test_settings_only_project_plugin_has_no_surface():
    verdict = run(PluginManifest(name="rogue"))
    assert verdict.state is TrustState.NO_SURFACE and verdict.active


def test_never_granted_project_plugin_is_refused():
    verdict = run()
    assert verdict.state is TrustState.REFUSED
    assert not verdict.active and not verdict.fatal


def test_enabled_but_never_granted_is_fatal():
    verdict = run(enabled=("rogue",))
    assert verdict.state is TrustState.FATAL_ENABLED_UNGRANTED
    assert verdict.fatal and not verdict.active


def test_granted_project_plugin_with_matching_tree_is_granted(tmp_path):
    tree = make_tree(tmp_path)
    verdict = run(record=grant_for(tree), live=lambda: tree)
    assert verdict.state is TrustState.GRANTED and verdict.active


def test_grant_subsumes_enabled_for_project_origin(tmp_path):
    tree = make_tree(tmp_path)
    assert run(record=grant_for(tree), live=lambda: tree, enabled=("rogue",)).active


def test_changed_contents_are_a_fatal_mismatch(tmp_path):
    record = grant_for(make_tree(tmp_path))
    (tmp_path / "rogue" / "new.py").write_text("")
    changed = make_tree(tmp_path, "echo pwned\n")
    verdict = run(record=record, live=lambda: changed)
    assert verdict.state is TrustState.FATAL_MISMATCH and verdict.fatal
    assert verdict.changed == ("hook.sh", "new.py")
    assert "1 added, 0 removed, 1 changed" in verdict.reason


def test_moved_plugin_dir_is_a_fatal_mismatch(tmp_path):
    tree = make_tree(tmp_path)
    verdict = run(record=grant_for(tree, path="elsewhere/rogue"), live=lambda: tree)
    assert verdict.state is TrustState.FATAL_MISMATCH
    assert "elsewhere/rogue" in verdict.reason and verdict.changed == ()


def test_unreadable_granted_tree_is_fatal(tmp_path):
    record = grant_for(make_tree(tmp_path))

    def unreadable() -> VerifiedTree:
        raise PluginError("plugin file hook.sh: cannot open")

    verdict = run(record=record, live=unreadable)
    assert verdict.state is TrustState.FATAL_UNREADABLE and verdict.fatal
    assert "cannot open" in verdict.reason


def test_unrecorded_plugin_is_never_hashed():
    # the thunk raises AssertionError if called: REFUSED must be decided without it
    assert run(live=never_called).state is TrustState.REFUSED
    assert run(enabled=("rogue",), live=never_called).state is TrustState.FATAL_ENABLED_UNGRANTED


# ------------------------------------------------------------ messages


def test_fatal_messages_name_the_operator_verb(tmp_path):
    mismatch = Verdict(TrustState.FATAL_MISMATCH, "contents changed")
    ungranted = Verdict(TrustState.FATAL_ENABLED_UNGRANTED, "never granted")
    unreadable = Verdict(TrustState.FATAL_UNREADABLE, "cannot open")
    assert "`bmad-loop plugins accept rogue`" in fatal_message("rogue", mismatch)
    assert "`bmad-loop plugins enable rogue`" in fatal_message("rogue", ungranted)
    assert "`bmad-loop plugins accept rogue`" in fatal_message("rogue", unreadable)
    for verdict in (mismatch, ungranted):
        assert "per machine" in fatal_message("rogue", verdict)
        assert "re-accept after moving" in fatal_message("rogue", verdict)
    with pytest.raises(ValueError):
        fatal_message("rogue", Verdict(TrustState.REFUSED, "never granted"))
