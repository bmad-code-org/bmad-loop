"""Escalation-resolution: context build, re-arm, spec field writer, session."""

import json
import sys
from pathlib import Path

import pytest
import yaml
from conftest import escalated_run, git

from bmad_loop import devcontract, platform_util, resolve, runs, verify
from bmad_loop.journal import load_state, save_state
from bmad_loop.model import (
    PAUSE_ESCALATION,
    Phase,
    RunState,
)
from bmad_loop.platform_util import safe_segment

SPEC = """\
---
title: List command
status: in-review
owner: amelia
---

# Spec

<frozen-after-approval>
Filter notes by workspace name.
</frozen-after-approval>
"""


def _escalated_run(
    project,
    run_id="20260613-111429-6a14",
    *,
    spec_file=None,
    with_session=True,
    source="sprint-status",
    sentinel_kind="",
    worktree_path="",
    baseline_commit="abc123",
    restore_patch=None,
    repo_root=None,
    target_branch=None,
):
    """conftest's builder with this module's shape: a review-cycle-1 task carrying a
    completed review session (what `build_context` reads), returning the full triple.

    ``baseline_commit`` / ``restore_patch`` are forwarded for the rows that need a
    task ALREADY carrying a latch and a real sha before re-arm runs — the state
    `_stale_restore_residue` reads (`old_latch`, `old_baseline`) and returns early
    without touching git when the latch is absent.

    ``repo_root`` records the divergent CODE tree on the saved state, so the
    divergent-root rows do not each re-open, mutate and re-save the file they just
    built. A row that forgot that save would silently degrade into the same-root
    case it exists to distinguish, which is the failure this kwarg removes.
    ``target_branch`` is there for the same reason and with the same hazard: it pins
    the branch the isolated re-drive cuts its fresh worktree from, and leaving it unset
    degrades `_redrive_base_ref` to `HEAD` — the pre-fix anchor, which is exactly the
    case the rows that pass it exist to separate themselves from."""
    run = escalated_run(
        project,
        run_id,
        story_key="6-4-cli-list-command",
        epic=6,
        review_cycle=1,
        baseline_commit=baseline_commit,
        started_at="2026-06-13T11:14:29",
        paused_reason="CRITICAL escalation from review session: names not unique",
        spec_file=spec_file,
        with_session=with_session,
        source=source,
        sentinel_kind=sentinel_kind,
        worktree_path=worktree_path,
        restore_patch=restore_patch,
    )
    if repo_root is not None or target_branch is not None:
        if repo_root is not None:
            run.state.repo_root = str(repo_root)
        if target_branch is not None:
            run.state.target_branch = target_branch
        save_state(run.run_dir, run.state)
    return run.run_dir, run.state, run.task


# ------------------------------------------------------------ set_frontmatter_field
#
# `set_frontmatter_status`'s own tests live in tests/test_frontmatter.py, next to
# the module that defines it (#357). What stays here is `set_frontmatter_field`,
# which is `verify`'s — it shares the renderer and the verified-edit core, so the
# tests below are about the half that differs: insert-on-miss.


def test_set_frontmatter_field_replaces_inserts_idempotent(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    assert verify.set_frontmatter_field(spec, "owner", "winston", confine_root=tmp_path) is True
    assert verify.read_frontmatter(spec)["owner"] == "winston"
    # unlike set_frontmatter_status, a missing key is INSERTED (block's last line);
    # its refusal to invent one is pinned in tests/test_frontmatter.py
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == "abc123"
    assert fm["status"] == "in-review" and fm["title"] == "List command"  # untouched
    # idempotent: already at the target value
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is False
    )
    # no frontmatter block -> no write
    bare = tmp_path / "bare.md"
    bare.write_text("# just a heading\n", encoding="utf-8")
    assert (
        verify.set_frontmatter_field(bare, "baseline_revision", "abc123", confine_root=tmp_path)
        is False
    )


def test_set_frontmatter_field_preserves_a_trailing_inline_comment(tmp_path):
    """This helper's docstring has always promised "comments survive"; until #357
    part 2 that was true of every line except the one it edited. It shares
    `frontmatter._replace_value` with `set_frontmatter_status`, so the carry is
    inherited rather than reimplemented — pinned here because a renderer change
    made for the status writer reaches this caller silently."""
    spec = tmp_path / "spec.md"
    spec.write_bytes(b"---\nbaseline_revision: old  # stamped by step-03\n---\nbody\n")
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    assert spec.read_bytes() == b"---\nbaseline_revision: abc123  # stamped by step-03\n---\nbody\n"
    assert verify.read_frontmatter(spec)["baseline_revision"] == "abc123"


def test_set_frontmatter_field_rewrites_a_quoted_key_instead_of_duplicating_it(tmp_path):
    """The insert-on-miss half of this helper had a defect of its own: the line
    scan missed a quoted key, so it APPENDED a second one and the spec carried
    the field twice. The insert is now gated on what `read_frontmatter` sees, not
    on a scan miss."""
    spec = tmp_path / "spec.md"
    spec.write_text('---\n"baseline_revision": old\n---\nbody\n', encoding="utf-8")
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    text = spec.read_text(encoding="utf-8")
    assert text.count("baseline_revision") == 1  # rewritten, not duplicated
    assert verify.read_frontmatter(spec)["baseline_revision"] == "abc123"


def test_set_frontmatter_field_refuses_a_key_no_line_edit_can_move(tmp_path):
    """Same three-way contract as `set_frontmatter_status` (pinned in
    tests/test_frontmatter.py): False means nothing to change, and a field the
    reader CAN see in an unrewritable shape raises instead of silently appending
    a duplicate the reader would never resolve."""
    spec = tmp_path / "spec.md"
    original = "---\n{baseline_revision: old, keep: 1}\n---\nbody\n"
    spec.write_text(original, encoding="utf-8")
    with pytest.raises(verify.FrontmatterWriteError):
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
    assert spec.read_text(encoding="utf-8") == original


def test_set_frontmatter_field_preserves_triple_dash_in_value(tmp_path):
    """Inserting a field appends before the real standalone closing `---`, not
    inside a scalar that merely contains `---` (which the old split corrupted)."""
    spec = tmp_path / "spec.md"
    spec.write_text(
        "---\ntitle: 'restore --- review'\nstatus: done\n---\nbody text\n",
        encoding="utf-8",
    )
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == "abc123"
    assert fm["title"] == "restore --- review"
    assert fm["status"] == "done"
    assert "body text" in spec.read_text(encoding="utf-8")


def test_set_frontmatter_field_replaces_without_relaying_crlf(tmp_path):
    """#357 part 1. The re-arm re-stamps `baseline_revision` on whatever the skill
    wrote; if the spec is CRLF, `read_text`/`write_text` rewrote every ending in
    the file (to LF here, to CRLF for an all-LF spec on Windows) from a write
    contracted to move one field."""
    spec = tmp_path / "spec.md"
    original = "---\r\ntitle: t\r\nbaseline_revision: old\r\nstatus: done\r\n---\r\n\r\nbody\r\n"
    spec.write_bytes(original.encode("utf-8"))
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    text = spec.read_bytes().decode("utf-8")
    assert text == original.replace("baseline_revision: old", "baseline_revision: abc123")
    assert "\n" not in text.replace("\r\n", "")  # no bare LF introduced


def test_set_frontmatter_field_inserts_with_the_blocks_own_line_ending(tmp_path):
    """The insert path, which only this helper has. The appended line takes the
    block's ending rather than a flat `\\n` — before the byte-level read the block
    was always LF by the time it got here, so the CRLF branch of the insert was
    unreachable and untested."""
    spec = tmp_path / "spec.md"
    original = "---\r\ntitle: t\r\nstatus: done\r\n---\r\n\r\nbody\r\n"
    spec.write_bytes(original.encode("utf-8"))
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    text = spec.read_bytes().decode("utf-8")
    assert text == (
        "---\r\ntitle: t\r\nstatus: done\r\nbaseline_revision: abc123\r\n---\r\n\r\nbody\r\n"
    )
    assert "\n" not in text.replace("\r\n", "")  # the inserted line is CRLF too
    fm = verify.read_frontmatter(spec)
    assert fm == {"title": "t", "status": "done", "baseline_revision": "abc123"}


def test_set_frontmatter_field_inserts_without_introducing_a_foreign_line_ending(tmp_path):
    """The insert copies the ending of the line it follows, so it cannot be the
    one line in the file with a different one. A CR-only spec is the shape that
    tells the two readings apart: `block.endswith("\\r\\n")` is False here and a
    flat `\\n` fallback would append the sole LF line to an all-CR file."""
    spec = tmp_path / "spec.md"
    spec.write_bytes(b"---\rtitle: t\rstatus: done\r---\r\rbody\r")
    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )
    text = spec.read_bytes().decode("utf-8")
    assert text == "---\rtitle: t\rstatus: done\rbaseline_revision: abc123\r---\r\rbody\r"
    assert "\n" not in text
    assert verify.read_frontmatter(spec)["baseline_revision"] == "abc123"


# --- set_frontmatter_field: atomic write (#379) ---
#
# Three rows for the three distinct choices at this call site — that it routes
# through the helper at all, that it is the BYTES helper, and that it does not
# write through a redirected parent. `verify` holds its own bindings of the byte
# writers, separate from `frontmatter`'s; tests/test_frontmatter.py grades the
# sibling status writer through those other bindings, and neither set reaches the
# other's site. Since #593 the in-tree arm is `atomic_write_bytes_confined`, which
# is the binding these rows patch: the plain `atomic_write_bytes` survives for a
# spec in an artifacts folder configured outside the checkout, so patching THAT
# name would install cleanly and never fire.


def test_set_frontmatter_field_write_failure_raises_and_keeps_the_spec(tmp_path, monkeypatch):
    """A spec is laid out `before + edited + after`, so the truncating `write_bytes`
    this replaced could publish intact frontmatter over a decapitated body. Worse
    here than for the status sibling: the field this writer re-stamps is
    `baseline_revision`, the anchor the patch-restore re-arm computes every later
    diff against, so a spec that keeps the key while losing the body is one the
    re-arm then treats as authoritative.

    Patched at verify's OWN binding, never `Path.write_bytes`: the helper writes
    through an `mkstemp` fd via `os.fdopen`, so a `Path` patch never fires and this
    would pass having exercised nothing.

    Ablation: restore `path.write_bytes(...)` at the call site and this reddens
    alone, on `pytest.raises` not raising — the import stays, so the stub still
    installs, it simply never gets called."""
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    before = spec.read_bytes()

    def boom(path, data, *, confine_root, require_writable_target=False):
        raise OSError("no space left on device")

    monkeypatch.setattr(verify, "atomic_write_bytes_confined", boom)
    with pytest.raises(OSError, match="no space left"):
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)

    assert spec.read_bytes() == before
    assert b"baseline_revision" not in spec.read_bytes()  # the insert that must not land


def test_set_frontmatter_field_hands_the_helper_bytes_not_text(tmp_path, monkeypatch):
    """Grades BYTES-vs-text at this site, platform-independently.
    `test_set_frontmatter_field_replaces_without_relaying_crlf` above already forbids
    relaying — but `atomic_write_text` keeps `Path.write_text`'s translating newline
    default and on POSIX `os.linesep == "\\n"`, so swapping the text helper in
    reddens that row on WINDOWS ONLY and CI's Linux leg would call the swap green.

    So inspect the payload the helper is handed, upstream of any translation. The
    binding is WRAPPED rather than replaced, so the real write still happens.

    BOTH helper names are wrapped into the same list, the text one with
    `raising=False` since `verify` does not import it. That is what makes the
    `isinstance` line the assertion that fires: wrapping only the bytes name would
    grade "the bytes helper was called", so the swap would redden on an empty `seen`
    and this row would be claiming more than it checked.

    Ablation: swap `atomic_write_bytes_confined` for `atomic_write_text_confined`
    (dropping the `.encode`) and this reddens on every platform, on the
    `isinstance` row."""
    seen: list[bytes | str] = []
    real = verify.atomic_write_bytes_confined

    def record(path, data, *, confine_root, require_writable_target=False):
        seen.append(data)
        blob = data if isinstance(data, bytes) else data.encode("utf-8")
        real(
            path,
            blob,
            confine_root=confine_root,
            require_writable_target=require_writable_target,
        )

    monkeypatch.setattr(verify, "atomic_write_bytes_confined", record)
    monkeypatch.setattr(verify, "atomic_write_text_confined", record, raising=False)
    spec = tmp_path / "spec.md"
    spec.write_bytes(b"---\r\ntitle: t\r\nstatus: done\r\n---\r\n\r\nbody\r\n")

    assert (
        verify.set_frontmatter_field(spec, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )

    assert len(seen) == 1  # exactly one write — no retry loop crept in
    assert isinstance(seen[0], bytes)
    assert b"baseline_revision: abc123\r\n" in seen[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_set_frontmatter_field_replaces_a_planted_symlink(tmp_path):
    """The row that grades this SITE's choice of a writer that replaces the NAME,
    rather than the helper's implementation of it (pinned in test_platform_util.py,
    where the helper is called directly). An in-tree spec no longer spells that
    choice as `follow_symlinks=False`: since #593 it routes to
    `atomic_write_bytes_confined`, which is no-follow by construction. The
    out-of-tree arm keeps the plain no-follow write — the else-arm row below
    grades that routing.

    Same reasoning as the two sibling writers of these files: replacing the name
    matches the name-replacing `atomic_replace` `devcontract._atomic_write_spec`
    already used, and the spec path reaches this writer from a scan of a directory
    a driven session owns — writing THROUGH a planted link would hand that session
    a host-side write to any operator-writable path.

    Ablation: swap the in-tree arm's writer for `atomic_write_bytes(path, payload)`
    at its follow-the-link default and this reddens on the link surviving and the
    planted target rewritten (the confined-parent rows below redden with it — the
    swap un-guards them too). No other row in this file plants a symlink at the
    spec's own name."""
    real = tmp_path / "someone-elses-file"
    real.write_text(SPEC, encoding="utf-8")
    link = tmp_path / "spec.md"
    link.symlink_to(real)

    assert (
        verify.set_frontmatter_field(link, "baseline_revision", "abc123", confine_root=tmp_path)
        is True
    )

    assert not link.is_symlink()  # the NAME was replaced
    assert verify.read_frontmatter(link)["baseline_revision"] == "abc123"
    assert real.read_text(encoding="utf-8") == SPEC  # not written through


# ------------------------------------- confined parent (#593) + read-only (#597)
#
# The same three-row chokepoint grading as the sibling status writer
# (tests/test_frontmatter.py), through verify's OWN bindings — patching one
# module's names never reaches the other's site — plus the #597 refusal.
#
# `confine_root` is always a real ANCESTOR of the spec's directory: the anchored
# walk covers the components strictly BELOW the root, so `confine_root` set to
# the spec's own parent would walk nothing and leave the refusal row green with
# the escape wide open.


def _field_tree(tmp_path):
    root = tmp_path / "checkout"
    (root / "artifacts").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


def _field_tap(label: str, seen: list[str], real):
    def record(path, data, **kw):
        seen.append(label)
        return real(path, data, **kw)

    return record


def test_set_frontmatter_field_takes_the_confined_arm_for_an_in_tree_spec(tmp_path, monkeypatch):
    """Positive control, grading WHICH writer an in-tree spec reaches. Both
    bindings are wrapped and both keep the real write, so the insert below really
    lands.

    Ablation: swap the two arms of the `is_relative_to` branch and this fails on
    `seen`, with the field still correctly stamped."""
    root, _ = _field_tree(tmp_path)
    spec = root / "artifacts" / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        verify,
        "atomic_write_bytes_confined",
        _field_tap("confined", seen, verify.atomic_write_bytes_confined),
    )
    monkeypatch.setattr(
        verify, "atomic_write_bytes", _field_tap("plain", seen, verify.atomic_write_bytes)
    )

    assert verify.set_frontmatter_field(spec, "baseline_revision", "abc", confine_root=root)

    assert seen == ["confined"]
    assert verify.read_frontmatter(spec)["baseline_revision"] == "abc"


def test_set_frontmatter_field_keeps_the_plain_write_for_an_out_of_tree_spec(tmp_path, monkeypatch):
    """The else-arm. An artifacts folder configured outside the checkout is
    supported configuration, and this writer's one production caller
    (`runs.rearm_escalation`) re-stamps `baseline_revision` on whatever spec the
    task recorded — so refusing there would abort a re-drive rather than close a
    hole.

    Ablation: call the confined writer unconditionally and this fails with
    `UnconfinedWriteError`, the re-stamp never landing."""
    root, outside = _field_tree(tmp_path)
    spec = outside / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        verify,
        "atomic_write_bytes_confined",
        _field_tap("confined", seen, verify.atomic_write_bytes_confined),
    )
    monkeypatch.setattr(
        verify, "atomic_write_bytes", _field_tap("plain", seen, verify.atomic_write_bytes)
    )

    assert verify.set_frontmatter_field(spec, "baseline_revision", "abc", confine_root=root)

    assert seen == ["plain"]
    assert verify.read_frontmatter(spec)["baseline_revision"] == "abc"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_set_frontmatter_field_refuses_a_symlinked_parent(tmp_path):
    """#593 at this site. The stakes are the sibling's plus one: the key this
    writer stamps is `baseline_revision`, the anchor every later restore diff is
    computed against, so a redirected write hands a session the ability to plant
    that anchor on a file of its choosing.

    The read half still resolves through the planted link, so the edit is computed
    and only the WRITE refuses — this reaches the writer rather than bailing at
    `is_file`.

    Ablation: revert the call to
    `atomic_write_bytes(path, payload, follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with the victim spec carrying the stamped key."""
    root, outside = _field_tree(tmp_path)
    victim = outside / "victim.md"
    victim.write_text(SPEC, encoding="utf-8")
    (root / "artifacts").rmdir()
    (root / "artifacts").symlink_to(outside, target_is_directory=True)
    spec = root / "artifacts" / "victim.md"
    assert spec.is_file()  # the read still resolves through the planted link

    with pytest.raises(platform_util.UnconfinedWriteError):
        verify.set_frontmatter_field(spec, "baseline_revision", "abc", confine_root=root)

    assert victim.read_text(encoding="utf-8") == SPEC  # not rewritten
    assert sorted(p.name for p in outside.iterdir()) == ["victim.md"]  # nor staged


def test_set_frontmatter_field_refuses_a_readonly_spec(tmp_path):
    """#597 at this site: a spec the operator marked read-only is answered with the
    kernel's `PermissionError`, not routed around by a replace that only needs the
    directory writable. `0o444` sets the win32 READONLY attribute too, so this runs
    on both platforms; the chmod is restored in a `finally` because Windows rmtree
    refuses a READONLY leftover.

    Ablation: drop `require_writable_target=True` from the confined call and this
    fails `DID NOT RAISE`, the spec carrying the key and still reading `0444`."""
    root, _ = _field_tree(tmp_path)
    spec = root / "artifacts" / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    spec.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            verify.set_frontmatter_field(spec, "baseline_revision", "abc", confine_root=root)
    finally:
        spec.chmod(0o644)

    assert spec.read_text(encoding="utf-8") == SPEC
    assert list((root / "artifacts").glob("*.tmp")) == []  # a refusal stages nothing


# ----------------------------------------------------------- build_context


def test_build_context_gathers_critical_escalations(tmp_path):
    # An absolute `spec_file` passes through `runs.task_spec_path` verbatim. The literal
    # has to be OS-absolute, not merely rooted: on Windows "/abs/spec.md" is DRIVE-relative
    # (`Path.is_absolute()` is False), so it takes the anchoring arm instead and pathlib's
    # `/` keeps the root's drive while discarding its path — yielding "D:/abs/spec.md", a
    # shape no real run persists. `tmp_path` is absolute on every OS.
    spec = tmp_path / "abs" / "spec.md"
    run_dir, state, task = _escalated_run(tmp_path, spec_file=str(spec))
    task_dir = run_dir / "tasks" / "6-4-cli-list-command-review-1"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text(
        json.dumps(
            {
                "escalations": [
                    {"type": "spec-gap", "severity": "CRITICAL", "detail": "names not unique"},
                    {"type": "nit", "severity": "PREFERENCE", "detail": "ignore me"},
                ]
            }
        ),
        encoding="utf-8",
    )
    path = resolve.build_context(state, run_dir, "6-4-cli-list-command")
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["story_key"] == "6-4-cli-list-command"
    assert ctx["spec_file"] == spec.as_posix()
    assert ctx["baseline_commit"] == "abc123"
    details = [e["detail"] for e in ctx["escalations"]]
    assert "names not unique" in details
    assert "ignore me" not in details  # PREFERENCE dropped
    assert ctx["resolution_path"].endswith("resolve/6-4-cli-list-command/resolution.json")
    # serialized via as_posix(): forward slashes only, so the context contract is
    # identical across OSes (no backslashes leak in on Windows).
    assert "\\" not in ctx["resolution_path"]


def test_build_context_absolutizes_an_isolated_units_worktree_relative_spec(tmp_path, monkeypatch):
    """`context.json` names the spec in the tree the RUN owns, absolute.

    `StoryTask._serialized_worktree_path` persists an isolated unit's `spec_file`
    RELATIVE to the mounted worktree and `from_dict` reads it back raw, so the raw
    value handed to the agent was a bare relpath. The `bmad-loop-resolve` session runs
    from the PROJECT root, where the main checkout carries the very same
    `_bmad-output/specs/...` layout — so that relpath resolved, silently, onto the
    main checkout's twin, and the human and the agent edited a spec the run never used
    while `rearm_escalation` (which re-anchors through `task_spec_path`) flipped the
    worktree's. `build_context` now emits the same re-anchor the re-arm writes
    through, which is also the absolute shape `bmad-loop-resolve/SKILL.md` documents.

    The EQUALITY is what grades this row, and it is the whole instrument:
    `is_absolute()` alone is satisfied by a PROJECT-anchored resolve, which is the
    same bug wearing an absolute path. Two things here are deliberately NOT load-
    bearing, so nobody reads them as proof they are not: `build_context` never opens
    the spec, so both on-disk copies are inert scenery, and the `chdir` cannot change
    an emitted value computed by pure path arithmetic. They are kept because a future
    `abspath`/`resolve()`-shaped resolver WOULD consult the cwd, and pinning it to
    the tree the agent really runs from keeps this row honest under that change.

    Shape, not just value: `.as_posix()` matches `resolution_path`'s contract two
    fields below — one string on every OS — so the assertion compares posix
    spellings and re-uses that field's no-backslash check. `str()` here would have
    regressed Windows, where the value it replaced was already posix
    (`_serialized_worktree_path` persists the relative form with `.as_posix()`).
    Both halves of that check are INERT on POSIX, where `str()` and `.as_posix()`
    agree — exactly as the sibling `resolution_path` assertion is. Windows CI is
    where they grade; do not read a green run here as having exercised them.

    Ablation: revert `build_context`'s field to `task.spec_file if task else None` and
    this reddens on `is_absolute()` — the emitted value is the bare relpath.
    """
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    wt = tmp_path / "wt"
    for root in (wt, tmp_path):  # the run's own copy, and the main checkout's twin
        spec = root / rel
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(SPEC, encoding="utf-8")

    run_dir, state, _ = _escalated_run(tmp_path, spec_file=rel, worktree_path=str(wt))
    monkeypatch.chdir(tmp_path)  # what the resolve session actually runs from

    path = resolve.build_context(state, run_dir, "6-4-cli-list-command")
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert Path(ctx["spec_file"]).is_absolute()
    # the worktree's copy, not the main checkout's twin — compared as posix, which is
    # also the contract shape (no backslashes leak in on Windows).
    assert ctx["spec_file"] == (wt / rel).as_posix()
    assert "\\" not in ctx["spec_file"]


def test_build_context_spec_file_is_none_without_a_task_or_a_spec(tmp_path):
    """The re-anchor must not manufacture a path out of nothing. `Path("")` is `.`,
    so an unguarded `root / raw` would emit the worktree root itself — a real
    directory — as the story's spec. Both empty legs stay `None`, which is what the
    agent reads as "there is no frozen spec to edit" (a spec-less escalation, or a
    key with no task at all).

    Ablation: drop the `and task.spec_file` guard from `build_context`'s field and the
    spec-less leg reddens with the worktree root in place of `None`."""
    wt = tmp_path / "wt"
    run_dir, state, _ = _escalated_run(tmp_path, spec_file=None, worktree_path=str(wt))

    ctx = json.loads(
        resolve.build_context(state, run_dir, "6-4-cli-list-command").read_text(encoding="utf-8")
    )
    assert ctx["spec_file"] is None  # task present, spec-less escalation

    assert "no-such-story" not in state.tasks
    ctx = json.loads(
        resolve.build_context(state, run_dir, "no-such-story").read_text(encoding="utf-8")
    )
    assert ctx["spec_file"] is None  # no task at all


def test_build_context_no_session_files(tmp_path):
    run_dir, state, _ = _escalated_run(tmp_path, with_session=False)
    path = resolve.build_context(state, run_dir, "6-4-cli-list-command")
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["escalations"] == []
    assert ctx["paused_reason"].startswith("CRITICAL")


def test_build_context_restore_supported_signal(tmp_path):
    """The agent must know up front when a patch-restore can't be honored, so it
    never negotiates a restore the orchestrator will reject after the session. The
    flag is `validate_restore_latch`'s verdict — every leg (worktree isolation / a
    worktree-executed task, a spec-less escalation, a pre-planning sentinel wedge),
    not a worktree-only copy that drifts from the validator (#91)."""
    run_dir, state, task = _escalated_run(tmp_path, spec_file="/abs/spec.md", with_session=False)
    key = "6-4-cli-list-command"

    path = resolve.build_context(state, run_dir, key)
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is True

    path = resolve.build_context(state, run_dir, key, isolation="worktree")
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is False

    task.worktree_path = str(tmp_path / "wt")  # recorded worktree execution
    path = resolve.build_context(state, run_dir, key)
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is False

    task.worktree_path = ""
    task.spec_file = None  # spec-less escalation: a restored patch has no review to resume
    path = resolve.build_context(state, run_dir, key)
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is False

    task.spec_file = "/abs/spec.md"
    state.source = "stories"
    task.sentinel_kind = "missing-prd"  # pre-planning wedge: nothing attempted to restore
    path = resolve.build_context(state, run_dir, key)
    assert json.loads(path.read_text(encoding="utf-8"))["restore_supported"] is False


def test_build_context_sanitizes_dirty_story_key(tmp_path):
    """A story key with Windows-illegal chars lands in a sanitized directory,
    while the key itself stays raw inside the context payload (it is data)."""
    run_dir, state, _ = _escalated_run(tmp_path)
    dirty = "6-4:cli?list"
    seg = safe_segment(dirty)
    assert seg != dirty
    path = resolve.build_context(state, run_dir, dirty)
    assert path.parent.name == seg
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["story_key"] == dirty
    assert ctx["resolution_path"].endswith(f"resolve/{seg}/resolution.json")


# ----------------------------------------------------------- rearm_escalation


def test_rearm_flips_phase_and_spec_status(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    key = runs.rearm_escalation(run_dir)
    assert key == "6-4-cli-list-command"
    state = load_state(run_dir)
    task = state.tasks[key]
    assert task.phase == Phase.PENDING
    assert task.attempt == 0
    assert task.review_cycle == 0
    # spec re-armed for a clean re-implement, even though the agent left it in-review
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    # pause is NOT cleared here — resume does that
    assert state.paused_stage == PAUSE_ESCALATION


def test_rearm_strips_stale_terminal_section(tmp_path):
    """Re-arm must drop the escalated attempt's `## Auto Run Result` along with
    the status flip (mirroring engine._reset_spec_for_repair): the heading is
    what find_result_artifact keys on, so leaving it would let the re-driven
    session's first save of the spec parse as the prior attempt's outcome."""
    spec = tmp_path / "spec-6-4.md"
    spec.write_text(
        SPEC + "\n## Auto Run Result\n\nStatus: blocked\nnames not unique\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    runs.rearm_escalation(run_dir)
    text = spec.read_text(encoding="utf-8")
    assert "Auto Run Result" not in text and "names not unique" not in text
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    assert "<frozen-after-approval>" in text  # intent body untouched
    assert devcontract.find_result_artifact(tmp_path, since_ns=0) is None


def test_rearm_warns_when_an_isolated_tasks_spec_writes_cannot_reach_the_redrive(tmp_path):
    """A worktree-isolated task's spec writes land in a directory the re-drive destroys.

    `task_spec_path` re-anchors the recorded spec on `task.worktree_path`, but a
    re-armed task falls to `engine._finish_inflight`'s final arm, which calls
    `discard_worktree` and lets `_run_story` mount a fresh one — and the re-driven
    session resolves its spec against THAT worktree
    (`engine._dispatched_spec_for_attempt` -> `verify.resolve_spec_path(...,
    workspace.paths)`, rebased onto the mount under isolation), which checks out tracked
    files only. So the re-drive reads the COMMITTED spec and no working-tree write
    reaches it — the main checkout's copy included. The writes stay (they are correct
    in-place, harmless here); the operator is told, because a flip that cannot land is
    the silent re-wedge #640(b) exists to end.

    The main-checkout copy is the row this shape pins, and it is the reason
    `_spec_is_shared_with_the_redrive` tests the PROJECT as well as the worktree. That
    write does land — the spec is outside `confine_root`, which
    `set_frontmatter_status` answers with its plain no-follow arm — but the re-drive
    still cannot use it: `engine._dispatched_spec_for_attempt` measures the absolute
    path with `verify.spec_within_roots` against `workspace.paths`, rebased onto the
    fresh worktree, and a main-checkout path is under none of those roots. "Outside the
    worktree" alone would exempt it; only "outside both checkouts" is shared.

    Ablation: delete the `if task.worktree_path:` term and this reddens while the
    no-worktree control still passes. Drop the `state.project` half of
    `_spec_is_shared_with_the_redrive` and this reddens alone — the shared-artifact-dir
    row is the one that must stay silent, and it does.
    """
    _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(spec), worktree_path=str(tmp_path / "wt" / "u1")
    )

    runs.rearm_escalation(run_dir)

    (rec,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"]
    assert rec["story_key"] == "6-4-cli-list-command"
    assert rec["spec_file"] == str(spec)
    # and it is routed to BOTH operator surfaces, not journal-only
    severity, message, next_step = runs.rearm_event_notice(
        {"kind": "rearm-spec-write-unreachable", **rec}
    )
    assert severity == "warning"
    assert "commit the corrected spec" in message
    assert next_step


def test_rearm_does_not_warn_about_unreachable_writes_without_a_worktree(tmp_path):
    """The control for the row above: the in-place case is where those writes DO land,
    so a record there would fire on every ordinary re-arm."""
    _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))

    runs.rearm_escalation(run_dir)

    assert [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"] == []


@pytest.mark.parametrize("shape", ["ordinary", "no-frontmatter", "already-at-target"])
def test_rearm_journals_a_status_flip_that_silently_did_nothing(tmp_path, shape):
    """`verify.set_frontmatter_status` answers "nothing to change" with `False`, never
    an exception. Its return was DISCARDED, so on such a spec the flip no-opped
    invisibly: the re-drive was dispatched anyway, step-01 read the unchanged terminal
    status, routed the session to "ingest as context, do not resume", and the story
    re-wedged with nothing on the record explaining why. The `FrontmatterWriteError`
    arm below it covers only the shapes that RAISE; this covers the ones that lie
    quietly.

    `False` has FOUR causes, not three — no file, no frontmatter block, no top-level
    `status:`, and ALREADY AT THE TARGET — and only the first three are failures. Three
    legs, so the record is graded on the distinction rather than on the bool:

    - `no-frontmatter` — a spec that EXISTS and is readable but carries no `---` block:
      the writer returns False and leaves the file byte-identical, so the record is the
      ONLY thing that distinguishes this from a flip that landed. It also ABORTS, because
      the spec IS a readable file here: the routing status is what the re-drive runs on,
      and step-01's contract for a spec without one is not a maybe — it HALTs blocked on
      `unrecognized status in existing story file`. So the re-arm refuses instead of
      burning the escalation on a session that cannot route, and the run is left exactly
      as it was found: the task still ESCALATED (nothing is persisted before this point),
      and the spec byte-identical down to the `## Auto Run Result` section, whose strip is
      sequenced after the check. The other half of that split — a spec that is NOT a file
      from here, where the flip's failure says nothing about what the re-drive will read
      — is graded by `test_rearm_journals_a_skip_when_the_recorded_spec_is_not_readable`.
    - `ordinary` — the control. A spec that really moves must not produce the record, or
      the row would pass for a guard that fires on everything.
    - `already-at-target` — the REGRESSION leg. A second re-arm, or
      `resolve --no-interactive` after a human fixed the spec, hits a spec already at
      `ready-for-dev`. The writer returns False and the operator used to be warned the
      spec "could not be re-opened" and might re-wedge, while the file was correct. It
      must NOT abort either — this is the ordinary flow, and refusing it would wedge
      every second resolve cycle.

    Ablation: delete the `if not flipped ...` arm and the `no-frontmatter` leg reddens
    on `DID NOT RAISE` while both other legs still pass. Drop the
    `status_of(read_frontmatter(...)) != target` conjunct and `already-at-target`
    reddens alone, now on that same raise — which is the discrimination this row exists
    for, and the leg that shows why the abort had to be narrowed to the same conjunct
    the record is. Delete the `raise RearmError(...)` and keep the append and the
    no-frontmatter leg reddens on `DID NOT RAISE` while its record assertions still
    pass — the record is not the refusal. Move `strip_auto_run_result` back above the
    check and the byte-identity assertion reddens alone.
    """
    _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    # every leg carries the stale result section, so the strip's SEQUENCING is graded
    # rather than assumed: on the two legs that proceed it must be gone, and on the leg
    # that aborts it must still be there.
    stale = "\n## Auto Run Result\n\nstatus: blocked\n"
    body = {
        "ordinary": SPEC,
        "no-frontmatter": "# Spec\n\nno frontmatter block\n",
        "already-at-target": SPEC.replace("status: in-review", "status: ready-for-dev"),
    }[shape] + stale
    assert shape != "already-at-target" or "status: ready-for-dev" in body  # fixture is real
    spec.write_text(body, encoding="utf-8")
    before = spec.read_bytes()
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))

    if shape == "no-frontmatter":
        with pytest.raises(runs.RearmError, match="no frontmatter `status:`"):
            runs.rearm_escalation(run_dir)
    else:
        runs.rearm_escalation(run_dir)

    records = [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-flip-skipped"]
    if shape == "already-at-target":
        # the ordinary re-arm of an already-correct spec: no record, no refusal, and the
        # status the re-drive needs is exactly what it was
        assert records == []
        assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
        assert "## Auto Run Result" not in spec.read_text(encoding="utf-8")
    elif shape == "ordinary":
        assert records == []
        assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
        assert "## Auto Run Result" not in spec.read_text(encoding="utf-8")
    else:
        (skipped,) = records
        assert skipped["story_key"] == "6-4-cli-list-command"
        assert skipped["spec_file"] == str(spec)
        assert skipped["status"] == "ready-for-dev"
        # the flag the operator surfaces render the refusal's remedy from; the sibling
        # rows that DON'T abort carry it False
        assert skipped["refused"] is True
        # the refusal left NOTHING behind but the record: not the re-stamp (which
        # `set_frontmatter_field` declines on the same missing block), not the result
        # strip (sequenced after the check), and not the task reset — the escalation is
        # still armed for the corrected spec, which is what makes the abort recoverable
        assert spec.read_bytes() == before
        assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.ESCALATED


def test_rearm_journals_event(tmp_path):
    run_dir, _, _ = _escalated_run(tmp_path)
    runs.rearm_escalation(run_dir)
    journal = (run_dir / "journal.jsonl").read_text(encoding="utf-8")
    assert "story-escalation-resolved" in journal


def test_rearm_advances_baseline_to_resolved_head(project):
    # The resolve session committed work on the project branch (e.g. a fixture
    # the human authorized). Re-arm must adopt that state as the new attempt
    # baseline, or the redrive's reset-to-baseline parks the resolution commit
    # on an attempt-preserve ref and re-drives against the unresolved tree.
    root = project.project
    old_head = git(root, "rev-parse", "HEAD")
    run_dir, _, _ = _escalated_run(root)
    (root / "fixture.txt").write_text("captured baseline\n", encoding="utf-8")
    git(root, "add", "fixture.txt")
    git(root, "commit", "-q", "-m", "resolution: capture fixture")
    # a file the resolve session (or the user) left untracked must enter the
    # snapshot, so the redrive reset treats it as pre-existing, not run-created
    (root / "leftover.txt").write_text("keep me\n", encoding="utf-8")
    runs.rearm_escalation(run_dir)
    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == git(root, "rev-parse", "HEAD")
    assert task.baseline_commit != old_head
    assert "leftover.txt" in task.baseline_untracked


def test_rearm_baseline_all_or_nothing_on_partial_git_failure(monkeypatch, project):
    """rev_parse_head succeeding but untracked_files failing must not advance
    baseline_commit while leaving baseline_untracked stale: both locals are
    computed before either field is assigned, so a failure on the second call
    leaves the pair exactly as it was, same as a failure on the first."""
    root = project.project
    run_dir, _, _ = _escalated_run(root)

    def boom(repo):
        raise verify.GitError("simulated failure")

    monkeypatch.setattr(runs.verify, "untracked_files", boom)
    runs.rearm_escalation(run_dir)
    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == "abc123"
    assert task.baseline_untracked is None


def test_rearm_keeps_stale_baseline_outside_a_repo(tmp_path):
    # best-effort contract: a project dir that is not a git repo (or a broken
    # one) must not make re-arm fail — the old baseline simply stands
    run_dir, _, _ = _escalated_run(tmp_path)
    runs.rearm_escalation(run_dir)
    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == "abc123"


def test_rearm_journals_a_failed_baseline_advance(tmp_path):
    """#640(b): the advance was `except Exception: pass  # nosec B110`, so a
    re-drive that silently rebuilt against the pre-resolution tree looked exactly
    like one that adopted the human's fix. Still non-fatal — a project that is not
    a repo must not fail re-arm — but no longer silent.

    A non-repo project reaches the arm through `git rev-parse HEAD` returning 128,
    which `_run_git` turns into a plain `GitError`; the spawn and timeout faults
    arrive as its `GitSpawnError` / `GitTimeoutError` subclasses, so the narrowed
    `except verify.GitError` is a total replacement for the bare `Exception`.

    Ablation: delete the `journal.append` and this row reddens alone.
    """
    run_dir, _, _ = _escalated_run(tmp_path)  # tmp_path is not a git repo

    runs.rearm_escalation(run_dir)

    (entry,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-advance-failed"]
    assert entry["story_key"] == "6-4-cli-list-command"
    assert entry["baseline"] == "abc123"  # the sha that consequently still stands
    assert "GitError" in entry["error"]
    assert load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit == "abc123"


def test_rearm_does_not_swallow_a_non_git_fault_from_the_advance(monkeypatch, tmp_path):
    """The `except` arm is NARROWED, not merely observed. `_run_git` translates
    every reachable git fault — non-zero rc, spawn failure, timeout, undecodable
    output — into the `verify.GitError` taxonomy, so nothing legitimate is lost;
    what the old bare `except Exception` also swallowed was every fault that is NOT
    a git answer, and those are real bugs that must not be filed away as "the
    project probably isn't a repo".

    Ablation: widen the arm back to `except Exception` and this reddens with
    DID NOT RAISE, which is exactly how the class of fault it names used to end.
    """
    _resolve_repo(tmp_path)
    run_dir, _, _ = _escalated_run(tmp_path)

    def boom(repo):
        raise MemoryError("not a git answer")

    monkeypatch.setattr(runs.verify, "untracked_files", boom)
    with pytest.raises(MemoryError):
        runs.rearm_escalation(run_dir)


@pytest.mark.parametrize("restore", [None, "artifacts/attempt.patch"])
def test_rearm_does_not_restamp_a_baseline_the_advance_did_not_move(monkeypatch, tmp_path, restore):
    """#640(a)+(b) couple in ONE expression. The re-stamp guard tested
    `task.baseline_commit` for truthiness only, and a failed advance leaves the OLD
    sha in that field — which passes a truthiness test identically to a freshly
    advanced one. Writing it would make spec and task agree on a stale value, the
    one state in which nothing downstream can tell the advance never happened.

    Both legs, because dropping the `restore_patch` guard is what makes the
    from-scratch leg reach this write at all.

    Ablation: gate the re-stamp on `task.baseline_commit` instead of `advanced` and
    both rows redden — the spec comes back carrying the stale sha.
    """
    old_head = _resolve_repo(tmp_path)
    run_dir, spec, new_head = _escalated_spec_run(tmp_path, old_head)

    def boom(repo):
        raise verify.GitError("simulated failure")

    monkeypatch.setattr(runs.verify, "untracked_files", boom)
    runs.rearm_escalation(run_dir, restore_patch=restore)

    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == old_head  # NOT re-stamped with the stale sha
    assert fm["baseline_revision"] != new_head
    assert load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit == "abc123"
    assert [e["kind"] for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-restamped"] == []
    assert [e["kind"] for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-advance-failed"]


def test_rearm_bumps_the_task_generation(tmp_path):
    """#705: re-arm resets `attempt` to 0 and never clears `task.sessions`, so the
    next dispatch re-mints a session task_id byte-equal to a record the abandoned
    attempt already appended. The generation counter is what makes the new id
    unique; `engine._session_task_id` emits it only above zero so every id already
    on disk stays byte-identical.

    Clearing `task.sessions` was the rejected alternative — it costs the run-dir
    audit trail that a second resolve cycle reads — so the records below must still
    be there after the re-arm.
    """
    run_dir, _, _ = _escalated_run(tmp_path)
    before = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert before.generation == 0 and len(before.sessions) == 1

    runs.rearm_escalation(run_dir)

    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.generation == 1
    assert task.attempt == 0
    assert len(task.sessions) == 1  # the audit trail survives the re-arm

    save_state(run_dir, _rearmable(run_dir))
    runs.rearm_escalation(run_dir)
    assert load_state(run_dir).tasks["6-4-cli-list-command"].generation == 2


def test_rearm_advances_the_baseline_in_the_code_tree(tmp_path):
    """Under a `repo_root` override the run's code + git live somewhere other than
    `state.project`, and re-arm's advance used to read HEAD of `Path(state.project)`
    — a directory the proof-of-work gate never measures.

    Ablation: put the advance back on `Path(state.project)` and this reddens on the
    journalled degrade (`artifacts-root` is not a repo), which is the *visible*
    version of what the swallowed `except Exception` used to do silently.
    """
    code = tmp_path / "code"
    code.mkdir()
    head = _resolve_repo(code)
    art = tmp_path / "artifacts-root"
    art.mkdir()
    run_dir, _, _ = _escalated_run(art, repo_root=code)

    (code / "fixture.txt").write_text("resolution fixture\n")
    git(code, "add", "-A")
    git(code, "commit", "-q", "-m", "resolution fixture")
    (code / "leftover.txt").write_text("keep me\n")

    runs.rearm_escalation(run_dir)

    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == git(code, "rev-parse", "HEAD") != head
    assert "leftover.txt" in task.baseline_untracked
    assert [e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-advance-failed"] == []


def test_rearm_reads_stale_restore_residue_from_the_code_tree(tmp_path):
    """`_stale_restore_residue`'s repo argument moved to the code tree with the
    advance, and the cost of getting it wrong here is not a lost notice — it is
    CONTAMINATED baseline state.

    The helper does two things with that root: it anchors a RELATIVE `restore_patch`
    latch on it, and it runs `commits_above` in it. Anchored on the wrong root the
    patch is not found, the abandoned restore's new files are never subtracted, and
    they enter `baseline_untracked` as "pre-existing" — after which every rollback
    preserves them and `finalize_commit`'s `add -A` sweeps the abandoned attempt into
    the corrected story's commit (#90). The committed-variant notice is lost in the
    same breath, because `commits_above` faults in a directory that is not a repo and
    the warn-only arm degrades to no record at all.

    Ablation: revert the argument to `Path(state.project)` and this reddens three
    ways — `newfile.txt` re-enters `baseline_untracked`, `stale-restore-excluded`
    becomes `stale-restore-unparseable`, and `stale-restore-commits` disappears.
    """
    code = tmp_path / "code"
    code.mkdir()
    old_head = _resolve_repo(code)
    art = tmp_path / "artifacts-root"
    art.mkdir()

    # the abandoned restore attempt: a patch under the CODE tree, latched RELATIVE
    # (which is what makes the anchoring root load-bearing), plus the untracked file
    # it created
    (code / "artifacts").mkdir()
    (code / "artifacts" / "attempt.patch").write_text(
        "diff --git a/newfile.txt b/newfile.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/newfile.txt\n"
        "@@ -0,0 +1 @@\n"
        "+from the abandoned attempt\n",
        encoding="utf-8",
    )
    (code / "newfile.txt").write_text("from the abandoned attempt\n")
    (code / "leftover.txt").write_text("genuinely pre-existing\n")

    run_dir, _, _ = _escalated_run(
        art, baseline_commit=old_head, restore_patch="artifacts/attempt.patch", repo_root=code
    )

    # the resolve session's own commit, above the old baseline
    (code / "fixture.txt").write_text("resolution fixture\n")
    git(code, "add", "fixture.txt")
    git(code, "commit", "-q", "-m", "resolution fixture")
    new_head = git(code, "rev-parse", "HEAD")

    runs.rearm_escalation(run_dir)  # from scratch: the latch is dropped

    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.baseline_commit == new_head
    # the patch's new file is SUBTRACTED; the genuine leftover is kept
    assert "newfile.txt" not in task.baseline_untracked
    assert "leftover.txt" in task.baseline_untracked

    kinds = _kinds(run_dir)
    (excluded,) = [e for e in kinds if e["kind"] == "stale-restore-excluded"]
    assert excluded["files"] == ["newfile.txt"]
    assert [e for e in kinds if e["kind"] == "stale-restore-unparseable"] == []
    # the committed variant the human has to classify by hand
    (commits,) = [e for e in kinds if e["kind"] == "stale-restore-commits"]
    assert commits["old_baseline"] == old_head and commits["commits"] == [new_head]


def test_rearm_falls_back_to_project_when_no_code_root_was_recorded(tmp_path):
    """A state.json written before `RunState.repo_root` existed carries no root, and
    must degrade to exactly the pre-upgrade behavior rather than to a path that does
    not exist. `d.get(key, default)` on the load side is what makes that true.

    What actually grades this is the BASELINE assertion, not the deletion: the
    `_escalated_run` fixture never sets `repo_root`, so the serialized value is
    already `""` and the `del` below only makes the legacy shape explicit — it
    changes nothing observable and would pass with the fallback broken. The final
    assertion is what discriminates, because `RunState.code_root` spells the degrade
    `Path(self.repo_root or self.project)`: drop the `or self.project` and the empty
    string becomes `Path("")` — the process CWD, a different repository under pytest —
    and the recorded baseline no longer matches this project's HEAD.

    The positive direction (a recorded root actually being used) is pinned separately
    by the divergent-root rows above.
    """
    head = _resolve_repo(tmp_path)
    run_dir, _, _ = _escalated_run(tmp_path)
    raw = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    del raw["repo_root"]  # state.json from before the field existed
    (run_dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")

    assert load_state(run_dir).repo_root == ""
    runs.rearm_escalation(run_dir)
    assert load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit == head


def test_rearm_writes_the_worktree_spec_not_the_main_checkouts_copy(monkeypatch, tmp_path):
    """The recorded spec path is re-anchored on the tree it was persisted RELATIVE to.

    `StoryTask._serialized_worktree_path` (`model.py`) persists a worktree-local spec
    relative to the mounted worktree root — no worktree prefix — and `from_dict` reads
    it back raw, so a bare `Path(task.spec_file)` resolves against the process cwd.
    That is not merely unreachable, it is actively WRONG: `bmad-loop resolve` runs from
    the project root, and the main checkout carries the very same
    `_bmad-output/specs/...` layout. `is_file()` answered True on the wrong file,
    `confine_root` accepted it (it genuinely is under `project`), and both the status
    flip AND the baseline re-stamp landed on a spec the run never used, while the
    worktree's real spec kept the escalated attempt's sha and the re-drive re-wedged.

    `task_spec_path` now anchors a relative path on `task.worktree_path` (falling back
    to `state.project`) and passes an absolute one through.

    The cwd is set EXPLICITLY: pytest's own cwd is not this sandbox, so without the
    `chdir` the old code would simply fail to resolve the relative path and the row
    would pass for the wrong reason instead of reproducing the hazard. The two copies
    carry distinguishable `baseline_revision` claims for the same reason — "the right
    file was written" has to be checkable against "the other one was not".

    Ablation: revert `task_spec_path`'s body to `return Path(task.spec_file or "")`
    and this reddens on the worktree copy with
    `AssertionError: assert 'blocked' == 'ready-for-dev'` — the flip went to the main
    checkout — and the byte-identity assertion on the main copy reddens behind it.
    """
    head = _resolve_repo(tmp_path)
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    wt = tmp_path / "wt"
    for root, claim in ((wt, "sha-of-the-escalated-attempt"), (tmp_path, "sha-in-main-checkout")):
        spec = root / rel
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(
            f"---\nstatus: blocked\nbaseline_revision: {claim}\n---\n\n## Intent\n\nx\n",
            encoding="utf-8",
        )
    main_spec = tmp_path / rel
    untouched = main_spec.read_bytes()

    run_dir, _, _ = _escalated_run(tmp_path, spec_file=rel, worktree_path=str(wt))
    monkeypatch.chdir(tmp_path)  # what `bmad-loop resolve` actually runs from

    runs.rearm_escalation(run_dir)

    fm = verify.read_frontmatter(wt / rel)
    assert fm["status"] == "ready-for-dev"  # the flip landed in the WORKTREE
    assert fm["baseline_revision"] == head  # and so did the re-stamp
    assert main_spec.read_bytes() == untouched  # the main checkout's copy is unread


def test_rearm_journals_a_skip_when_the_recorded_spec_is_not_readable(tmp_path):
    """A spec path that does not resolve must not be a SILENT no-op.

    `StoryTask` persists `spec_file` relative to a worktree
    (`model._serialized_worktree_path`) and `from_dict` reads it back raw, so a
    re-arm of a task that ran under isolation can hold a path that resolves against
    nothing from this process's cwd. Every writer in the spec block answers that
    with `False` rather than an exception — `set_frontmatter_status`,
    `strip_auto_run_result` and `set_frontmatter_field` all guard on `is_file` — and
    all three return values are dropped, so without this guard the status flip AND
    the baseline re-stamp both do nothing and the spec keeps the escalated attempt's
    sha with no record anywhere.

    It is also the NEGATIVE control for the flip abort one screen above it. A spec that
    exists and cannot take a status refuses the re-arm; an unreachable one must not,
    because the flip's failure says nothing about what the re-drive will read — the path
    is worktree-relative, the worktree may already be gone, and the re-drive mounts a
    fresh one and reads the COMMITTED spec either way. Refusing here would turn the two
    records this row exists to report into a wedge.

    Ablation: delete the `if not spec_path.is_file():` arm in `rearm_escalation` and
    this reddens on the missing record — re-arm still "succeeds", which is the whole
    problem. Delete the `spec_path.is_file()` conjunct guarding the flip's `RearmError`
    and it reddens on the raise instead, before any assertion runs.
    """
    _resolve_repo(tmp_path)
    run_dir, _, _ = _escalated_run(tmp_path, spec_file="wt/_bmad-output/specs/gone.md")

    runs.rearm_escalation(run_dir)  # must not raise: the flip's no-op is not a refusal

    kinds = _kinds(run_dir)
    (skipped,) = [e for e in kinds if e["kind"] == "rearm-baseline-restamp-skipped"]
    assert skipped["spec_file"].endswith("gone.md")
    assert skipped["baseline"] == load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit
    # and the re-stamp record is NOT written, since nothing was stamped
    assert [e for e in kinds if e["kind"] == "rearm-baseline-restamped"] == []
    # the flip records its no-op on this shape too, and the task is re-armed anyway —
    # and says so on the record, so neither surface prints the refusal's remedy for it
    (flip,) = [e for e in kinds if e["kind"] == "rearm-spec-flip-skipped"]
    assert flip["refused"] is False
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.PENDING


@pytest.mark.parametrize("repo", [True, False])
def test_rearm_records_an_unreachable_spec_even_when_the_advance_failed(tmp_path, repo):
    """The two #640 degrades COMPOSE; they do not substitute for each other.

    The restamp-skip record used to sit INSIDE the `advanced and ...` gate, so on a
    project that is not a git repo — where the advance raises `verify.GitError` and
    `advanced` stays False — an unreachable spec produced NO record at all. The journal
    blamed git, while the status flip had silently no-opped for an entirely different
    reason and the spec still carried the escalated attempt's sha. One warning was
    standing in for two independent failures.

    The `repo=True` leg is the sibling row's case (advance succeeds, spec still
    unreachable) carried here so the parametrization states the composition rather than
    asserting only the half that used to be shadowed.

    Ablation: nest the `rearm-baseline-restamp-skipped` append back under
    `elif advanced and task.baseline_commit:` and the `repo=False` row reddens with
    `ValueError: not enough values to unpack (expected 1, got 0)` while `repo=True`
    keeps passing — which is precisely the shadowing that hid it.
    """
    if repo:
        _resolve_repo(tmp_path)
    run_dir, _, _ = _escalated_run(tmp_path, spec_file="wt/_bmad-output/specs/gone.md")

    runs.rearm_escalation(run_dir)

    kinds = _kinds(run_dir)
    (skipped,) = [e for e in kinds if e["kind"] == "rearm-baseline-restamp-skipped"]
    assert skipped["story_key"] == "6-4-cli-list-command"
    assert skipped["spec_file"].endswith("gone.md")
    # the advance's own degrade is reported alongside it, not instead of it
    failed = [e for e in kinds if e["kind"] == "rearm-baseline-advance-failed"]
    if repo:
        assert failed == []
    else:
        (advance_failed,) = failed
        assert "GitError" in advance_failed["error"]
        assert advance_failed["baseline"] == "abc123"  # the stale sha that still stands


def test_rearm_restamps_normally_when_the_spec_resolves(tmp_path):
    """The control for the row above: same code path, readable spec, no skip record.

    Without this the guard could refuse every spec and the negative row would still
    pass — a skip record is present for the right reason only if the ordinary case
    still stamps.
    """
    _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("---\nstatus: 'escalated'\nbaseline_revision: 'old'\n---\n\nbody\n")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))

    runs.rearm_escalation(run_dir)

    kinds = _kinds(run_dir)
    assert [e for e in kinds if e["kind"] == "rearm-baseline-restamp-skipped"] == []
    head = load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit
    # unquoted: `_replace_value` drops the quotes it found, which is its own
    # documented behavior and not what this row is about
    assert f"baseline_revision: {head}" in spec.read_text()


def test_rearm_clears_sentinel_preserving_a_copy(tmp_path):
    """Stories mode: a fixed-slug sentinel (`<id>-unresolved.md`) is cleared by
    deletion, not a status flip — re-arm preserves a copy, journals the blocking
    condition, drops the sentinel, and unsets spec_file so the re-dispatch starts
    clean (PENDING → re-plan from scratch)."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    sentinel = stories_dir / f"{key}-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    # sentinel_kind recorded at detection time (StoriesEngine stamps it on the task);
    # re-arm clears by this recorded verdict, not the basename.
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(sentinel), source="stories", sentinel_kind="unresolved"
    )

    returned = runs.rearm_escalation(run_dir)
    assert returned == key

    # sentinel deleted from disk, a copy preserved under the run dir
    assert not sentinel.exists()
    preserved = run_dir / "sentinels" / f"{key}-unresolved.md"
    assert preserved.is_file() and "intent too vague" in preserved.read_text(encoding="utf-8")

    state = load_state(run_dir)
    task = state.tasks[key]
    assert task.phase == Phase.PENDING
    assert task.spec_file is None  # cleared → next dispatch resolves to PENDING

    journal = (run_dir / "journal.jsonl").read_text(encoding="utf-8")
    assert "sentinel-cleared" in journal
    cleared = [
        json.loads(line)
        for line in journal.splitlines()
        if json.loads(line).get("kind") == "sentinel-cleared"
    ]
    # the journal carries the fixed slug (sentinel_kind) AND the recorded blocking
    # condition parsed from the sentinel's ## Auto Run Result (not just the slug).
    assert cleared[0]["sentinel_kind"] == "unresolved" and cleared[0]["story_key"] == key
    assert "intent too vague" in cleared[0]["condition"]


def test_rearm_non_sentinel_spec_still_flips_status(tmp_path):
    """C3: a blocked (non-sentinel) story spec IN STORIES MODE is re-opened by the
    status flip, not deleted — the sentinel branch must not swallow the normal re-arm
    path. Runs with source="stories" (the default sprint-status source would skip the
    sentinel branch entirely and never exercise the stories-mode logic)."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-slug.md"  # a real spec, not a fixed-slug sentinel
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")
    # source="stories" enters the sentinel-branch code; sentinel_kind="" (never
    # detected as a sentinel) → status-flip, not delete.
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    runs.rearm_escalation(run_dir)
    assert spec.is_file()  # not deleted
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    assert load_state(run_dir).tasks[key].spec_file == str(spec)  # kept
    assert not (run_dir / "sentinels").exists()


def test_rearm_sentinel_named_spec_never_detected_is_not_deleted(tmp_path):
    """C2: a real stories spec that merely happens to be named `<key>-unresolved.md`
    but was NEVER detected as a sentinel (task.sentinel_kind == "") is status-flipped
    and kept — re-arm must clear by the recorded detection verdict, not the basename,
    so a filename collision can never turn a real spec into data loss."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-unresolved.md"  # sentinel-shaped name, but a real spec
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nreal work\n", encoding="utf-8")
    # stories mode, but sentinel_kind unset — the run never classified it as a sentinel
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    runs.rearm_escalation(run_dir)
    assert spec.is_file()  # NOT deleted despite the sentinel-shaped name
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"
    assert load_state(run_dir).tasks[key].spec_file == str(spec)  # kept
    assert not (run_dir / "sentinels").exists()


def test_rearm_sprint_spec_named_like_a_sentinel_is_not_deleted(tmp_path):
    """MINOR-G: the sentinel-clear path is stories-mode-only. A *sprint* spec that
    merely happens to be named `<key>-unresolved.md` must be status-flipped and
    kept like any other spec — never deleted — since the fixed-slug sentinel
    convention exists only in stories mode. (source defaults to sprint-status.)"""
    key = "6-4-cli-list-command"
    spec = tmp_path / f"{key}-unresolved.md"  # sentinel-shaped name, but a sprint spec
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nreal work\n", encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))  # sprint-status source

    runs.rearm_escalation(run_dir)
    assert spec.is_file()  # NOT deleted despite the sentinel-shaped name
    assert verify.read_frontmatter(spec)["status"] == "ready-for-dev"  # flipped like any spec
    assert load_state(run_dir).tasks[key].spec_file == str(spec)  # kept
    assert not (run_dir / "sentinels").exists()  # no sentinel preservation in sprint mode


def test_rearm_rejects_restore_patch_on_a_sentinel(tmp_path):
    """T1 (stories x patch-restore): a sentinel-wedged story escalated BEFORE
    planning — there is no attempted implementation to restore, and its re-arm
    re-dispatches a planning leg, so laying a patch onto the tree first is never
    safe. Re-arm must reject loudly BEFORE mutating anything: the sentinel stays
    on disk, the task stays ESCALATED, and no latch is persisted."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    sentinel = stories_dir / f"{key}-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(sentinel), source="stories", sentinel_kind="unresolved"
    )

    with pytest.raises(runs.RearmError, match="sentinel"):
        runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    assert sentinel.is_file()  # nothing deleted, copy NOT preserved — no clear happened
    task = load_state(run_dir).tasks[key]
    assert task.phase == Phase.ESCALATED  # not re-armed; the escalation stays armed
    assert task.restore_patch is None  # no latch persisted
    assert task.sentinel_kind == "unresolved"  # detection verdict intact for a retry
    # nothing was journaled at all — no sentinel-cleared, no story-escalation-resolved
    assert not (run_dir / "journal.jsonl").exists()


def test_rearm_rejects_restore_patch_without_a_spec_file(tmp_path):
    """A restore only works through the spec's in-review flip, so an escalated
    task with NO recorded spec (ambiguous two-file wedge, unknown --story
    selector, session died before naming one) has no routing target: the latch
    would stick, the flip would be skipped, and the engine would lay the patch
    onto the tree before a planning leg. Rejected before any mutation."""
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=None)

    with pytest.raises(runs.RearmError, match="no recorded spec file"):
        runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.phase == Phase.ESCALATED  # not re-armed; the escalation stays armed
    assert task.restore_patch is None  # no latch persisted
    assert not (run_dir / "journal.jsonl").exists()  # nothing journaled

    runs.rearm_escalation(run_dir)  # a from-scratch re-arm remains available
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.PENDING


def test_rearm_rejects_restore_patch_for_a_worktree_executed_task(tmp_path):
    """#91: the worktree guard used to live ONLY in `cli._resolve_restore_patch`, so a
    programmatic caller (TUI restore parity, a script) could latch a patch the
    re-drive can never honor — it discards the unit's worktree and re-mounts a fresh
    one. `validate_restore_latch` is now the single seam, enforced here too."""
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(spec), worktree_path=str(tmp_path / "wt" / "s1")
    )

    with pytest.raises(runs.RearmError, match="worktree-isolation"):
        runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    task = load_state(run_dir).tasks["6-4-cli-list-command"]
    assert task.phase == Phase.ESCALATED  # nothing mutated; still armed for a re-resolve
    assert task.restore_patch is None
    # a from-scratch re-arm of the same task is unaffected — the guard is latch-only
    assert runs.rearm_escalation(run_dir) == "6-4-cli-list-command"


def test_validate_restore_latch_passes_a_clean_in_place_escalation(tmp_path):
    """The happy leg of the shared validator, and the one input run state cannot
    carry: `worktree_isolation` (the LIVE policy, which the CLI passes) rejects even
    a task that executed in place, so a policy edit between escalation and resolve
    cannot skew the guard."""
    _, state, task = _escalated_run(tmp_path, spec_file="/abs/spec.md")
    key = "6-4-cli-list-command"

    assert runs.validate_restore_latch(state, task, key) is None
    err = runs.validate_restore_latch(state, task, key, worktree_isolation=True)
    assert err is not None and "worktree-isolation" in err


def test_rearm_restore_patch_on_a_real_stories_spec_is_allowed(tmp_path):
    """The T1 guard keys on the recorded sentinel verdict, not on stories mode:
    a review-stage intent gap on a REAL stories spec (sentinel_kind unset) is a
    legitimate restore target and re-arms to in-review like sprint mode."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-slug.md"
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")
    task = load_state(run_dir).tasks[key]
    assert task.phase == Phase.PENDING
    assert task.restore_patch == "artifacts/attempt.patch"
    assert verify.read_frontmatter(spec)["status"] == "in-review"  # restore routing


def _resolve_repo(tmp_path):
    """A tiny real repo so rearm's baseline advance has a HEAD to read."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@test")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / ".gitignore").write_text(".bmad-loop/runs/\n")
    (tmp_path / "src.txt").write_text("original\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "initial")
    return git(tmp_path, "rev-parse", "HEAD")


def test_rearm_restore_patch_restamps_spec_baseline(tmp_path):
    """The in-review route skips step-03 — the only step that stamps
    `baseline_revision` — so the patch-restore re-arm re-stamps it to the
    advanced baseline itself. Otherwise the re-driven step-04 would build its
    review diff (and, on an intent-gap/bad-spec re-triage, revert) "since" the
    ORIGINAL pre-attempt sha, clawing back the very resolve-session commits the
    baseline advance blesses as the re-drive's starting point."""
    key = "6-4-cli-list-command"
    old_head = _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(
        f"---\nstatus: blocked\nbaseline_revision: {old_head}\n---\n\n## Intent\n\nx\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    # the resolve session leaves a commit NOT overlapping the patch (blessed input)
    (tmp_path / "fixture.txt").write_text("resolution fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "resolution fixture")
    new_head = git(tmp_path, "rev-parse", "HEAD")

    runs.rearm_escalation(run_dir, restore_patch="artifacts/attempt.patch")

    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == new_head  # step-04 diffs from the ADVANCED baseline
    assert fm["status"] == "in-review"
    assert load_state(run_dir).tasks[key].baseline_commit == new_head


def _escalated_spec_run(tmp_path, baseline: str, *, extra: str = "", recorded: str = "abc123"):
    """A real repo + an escalated run whose spec claims `baseline`, plus one
    resolution commit on top. Returns (run_dir, spec, new_head).

    ``recorded`` sets what the RUN recorded for the escalated attempt
    (`task.baseline_commit`), which defaults to the fixture's `"abc123"` and therefore
    differs from every spec claim a caller can write. The divergence-reference rows
    need it equal to the claim: that is the only shape in which the two candidate
    references (`old_baseline` vs the advanced `task.baseline_commit`) disagree.
    """
    spec = tmp_path / "spec.md"
    spec.write_text(
        f"---\nstatus: blocked\nbaseline_revision: {baseline}\n{extra}---\n\n## Intent\n\nx\n",
        encoding="utf-8",
    )
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), baseline_commit=recorded)
    (tmp_path / "fixture.txt").write_text("resolution fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "resolution fixture")
    return run_dir, spec, git(tmp_path, "rev-parse", "HEAD")


def _kinds(run_dir):
    return [
        json.loads(line)
        for line in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_rearm_restamps_spec_baseline_on_the_from_scratch_leg_too(tmp_path):
    """#640(a): the re-stamp used to be gated on `restore_patch`, so a from-scratch
    re-drive left the escalated attempt's sha on the spec until step-03 ran — and
    every gate reading a claimed baseline before then read a stale one.

    The design call is recorded rather than hidden: extending the re-stamp to this
    leg also removes the gate's INDEPENDENT signal here (it then compares a value
    the orchestrator itself wrote), which is why the overwrite is journalled — see
    the row below.
    """
    old_head = _resolve_repo(tmp_path)
    run_dir, spec, new_head = _escalated_spec_run(tmp_path, old_head)

    runs.rearm_escalation(run_dir)  # no restore

    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == new_head
    assert fm["status"] == "ready-for-dev"
    assert load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit == new_head


def test_rearm_restores_the_spec_when_the_baseline_restamp_aborts(tmp_path):
    """An aborted re-arm leaves the spec byte-identical — INCLUDING the abort that fires
    after two writes have already landed.

    Every other refusal in `rearm_escalation` earns that invariant by sequencing: the
    flip's read-back check raises before `strip_auto_run_result` runs, which is the whole
    reason that strip is ordered after it. The baseline re-stamp cannot be sequenced the
    same way — it needs `task.baseline_commit` from an advance that must itself run after
    the spec block, or a just-cleared stories sentinel is captured as phantom untracked
    residue — so by the time it can fail, the status flip and the result strip are both
    behind it and `save_state` is not.

    This spec is the shape that separates the two writes: a plain, perfectly movable
    `status:` beside a `baseline_revision:` block scalar, which
    `frontmatter._edit_frontmatter_block` refuses (no line edit re-parses to the intended
    value, so no candidate verifies). Without the undo the operator was left with the
    worst of both: the run still calling the story ESCALATED, and a spec already flipped
    to `ready-for-dev` and stripped of the `## Auto Run Result` section the next resolve
    session reads as its context — the one edit nothing else records.

    Ablation: drop the `_restore_rearmed_spec(...)` call from the re-stamp's except arm
    and this reddens on the byte comparison (the status flip and the strip both stand),
    while the `RearmError` and the ESCALATED phase keep passing — which is exactly why
    those two alone do not grade this.
    """
    old_head = _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(
        "---\n"
        "status: blocked\n"
        "baseline_revision: |\n"
        f"  {old_head}\n"
        "---\n\n## Intent\n\nx\n\n## Auto Run Result\n\nterminal verdict\n",
        encoding="utf-8",
    )
    before = spec.read_bytes()
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))
    # a resolve-session commit, so the advance really runs and the re-stamp is reached
    (tmp_path / "fixture.txt").write_text("resolution fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "resolution fixture")

    with pytest.raises(runs.RearmError, match="baseline_revision"):
        runs.rearm_escalation(run_dir)

    assert spec.read_bytes() == before  # flip AND strip both undone
    # nothing was persisted either, so the escalation is still armed for a corrected spec
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.ESCALATED


def test_rearm_restores_the_spec_when_the_result_strip_faults(tmp_path, monkeypatch):
    """The re-stamp is not the only abort that fires after a write has landed — the spec
    block's own `(OSError, UnicodeDecodeError)` arm is the other, and it needs the same
    undo.

    The sequencing argument that buys the read-back check its byte-identical abort does
    not reach here. That arm guards BOTH spec helpers, and `strip_auto_run_result` is the
    later one: ordering the strip after the check protects the CHECK, but a fault raised
    inside the strip itself is raised with the status flip already published and
    `save_state` still ahead. `strip_auto_run_result` documents that it lets a
    present-but-unreadable spec and a failing write raise rather than swallowing them
    (silently skipping the strip is the worse bug), so this is its contracted behavior,
    not an accident — and its write is the confined atomic writer, which raises on ENOSPC,
    on EIO, and on a parent component swapped for a link under the `O_NOFOLLOW` walk.

    Injected rather than provoked, because no in-process fault can order itself between
    the two writes: both helpers decode the same file as UTF-8 and write through the same
    `require_writable_target=True` path, so every natural fault that reddens the strip
    reddens the flip first and leaves nothing to restore. The injection stands in for the
    faults above, which are real and are exactly what the atomic writers exist for.

    Ablation: drop the `_restore_rearmed_spec(...)` call from that arm and this reddens on
    the byte comparison alone — the `RearmError` and the ESCALATED phase both still pass,
    since the flip landing is precisely what neither observes. Both of those assertions
    are load-bearing for that claim, so both stay in THIS test: an isolated sibling row
    was once inserted between them and silently adopted the phase check, leaving this
    docstring citing an assertion the test no longer made.
    """
    _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(
        "---\nstatus: blocked\n---\n\n## Intent\n\nx\n\n## Auto Run Result\n\nterminal verdict\n",
        encoding="utf-8",
    )
    before = spec.read_bytes()
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec))

    def boom(spec_path, *, confine_root):
        raise OSError(28, "No space left on device")

    # patched on the module under test, so the flip runs for real and PUBLISHES
    monkeypatch.setattr(runs.devcontract, "strip_auto_run_result", boom)

    with pytest.raises(runs.RearmError, match="No space left on device"):
        runs.rearm_escalation(run_dir)

    assert spec.read_bytes() == before  # the published flip is rolled back
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.ESCALATED


def test_rearm_restores_an_isolated_tasks_spec_that_sits_outside_the_worktree(
    tmp_path, monkeypatch
):
    """The undo must still land when the mount cannot confine the spec.

    An absolute `spec_file` beside a set `worktree_path` means the spec is lexically
    OUTSIDE the mount (`model._serialized_worktree_path` keeps a path verbatim exactly
    when `relative_to(worktree_path)` raises) — the shape a shared artifact directory
    produces. `_restore_rearmed_spec` calls `atomic_write_bytes_confined` DIRECTLY, so
    a `confine_root` naming the worktree does not merely degrade the write the way the
    three `_atomic_write_spec` writers do: it raises `UnconfinedWriteError`, which the
    arm re-raises as "cannot restore ...". The operator was then left with the exact
    state the undo exists to prevent — a spec carrying this re-arm's status flip and
    stripped of its `## Auto Run Result`, on a story the run still calls ESCALATED —
    plus a second error masking the first.

    `task_spec_root` now answers the project for that shape, which CAN confine the
    spec, so the restore lands and the original fault is the one that surfaces.

    Ablation: revert `task_spec_root` to `Path(task.worktree_path or state.project)`
    and this reddens twice — the `match=` fails on "cannot restore ... UnconfinedWrite
    Error", and the byte comparison fails behind it.
    """
    _resolve_repo(tmp_path)
    wt = tmp_path / ".bmad-loop" / "runs" / "wt-mount"  # the mount, which holds no spec
    wt.mkdir(parents=True, exist_ok=True)
    spec = tmp_path / "spec.md"  # in the project, outside the mount
    spec.write_text(
        "---\nstatus: blocked\n---\n\n## Intent\n\nx\n\n## Auto Run Result\n\nterminal verdict\n",
        encoding="utf-8",
    )
    before = spec.read_bytes()
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), worktree_path=str(wt))

    def boom(spec_path, *, confine_root):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runs.devcontract, "strip_auto_run_result", boom)

    with pytest.raises(runs.RearmError, match="No space left on device"):
        runs.rearm_escalation(run_dir)

    assert spec.read_bytes() == before  # the undo reached a spec outside the mount
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.ESCALATED


def test_rearm_journals_the_spec_baseline_it_overwrote(tmp_path):
    """A claim the re-stamp normalizes away is the only trace of a divergence the
    gate can no longer report, so it lands in the journal on the way out — read
    back through the SAME reader the gate uses, so what is recorded is the value
    the gate would have judged (#716).

    Ablation: drop the `rearm-baseline-restamped` append and the row reddens; drop
    the `overwritten != old_baseline` guard and the second half reddens
    (a re-arm that changed nothing would report an overwrite).
    """
    old_head = _resolve_repo(tmp_path)
    run_dir, _spec, new_head = _escalated_spec_run(tmp_path, old_head)

    runs.rearm_escalation(run_dir)

    (entry,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-restamped"]
    assert entry["overwritten"] == old_head
    assert entry["baseline"] == new_head
    assert entry["restore"] is False

    # a second re-arm has nothing left to overwrite: no duplicate record
    save_state(run_dir, _rearmable(run_dir))
    runs.rearm_escalation(run_dir)
    assert len([e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-restamped"]) == 1


def test_rearm_does_not_report_a_divergence_the_run_never_had(tmp_path):
    """`rearm-baseline-restamped` measures the spec's claim against what the RUN
    RECORDED — `old_baseline`, captured before the advance — and NOT against
    `task.baseline_commit`, which the advance has already moved to the new HEAD.

    Against the advanced value the record fired on every ordinary from-scratch re-arm
    whose resolve session committed anything: the spec and the run agreed exactly, HEAD
    had simply moved on, and the operator was still told they diverged. A warning that
    fires on the routine case is the "trains the operator to scroll past the meaningful
    one" failure the `restore` split exists to prevent.

    This is the regression direction: it is the row that must redden if someone puts
    `task.baseline_commit` back. The re-stamp is asserted to have LANDED first, so the
    row cannot pass because the whole block was skipped.

    Ablation: restore `overwritten != task.baseline_commit` and this reddens with
    `AssertionError: assert [{...'kind': 'rearm-baseline-restamped'...}] == []` — the
    record fires on a re-arm with nothing to report.
    """
    old_head = _resolve_repo(tmp_path)
    run_dir, spec, new_head = _escalated_spec_run(tmp_path, old_head, recorded=old_head)

    runs.rearm_escalation(run_dir)

    # the re-stamp itself ran: this row is about what was REPORTED, not what was skipped
    assert verify.read_frontmatter(spec)["baseline_revision"] == new_head
    assert load_state(run_dir).tasks["6-4-cli-list-command"].baseline_commit == new_head
    assert [e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-restamped"] == []


def test_rearm_reports_a_claim_the_advanced_head_would_have_masked(tmp_path):
    """The other direction of the same reference choice — and the one the old guard got
    backwards. The spec claims the tree's CURRENT head while the run recorded a
    different baseline: a real divergence (the spec names a sha this run never used),
    yet compared against `task.baseline_commit` — already advanced to that same head —
    the two came out equal and the record was dropped.

    Paired with the row above, the two bracket the guard: one reddens if the reference
    moves forward to the advanced sha, the other if it does. Neither can be satisfied
    by deleting the comparison, because `test_rearm_journals_the_spec_baseline_it_
    overwrote` pins the no-duplicate case.

    Ablation: restore `overwritten != task.baseline_commit` and this reddens with
    `ValueError: not enough values to unpack (expected 1, got 0)` — no record at all.
    """
    old_head = _resolve_repo(tmp_path)
    spec = tmp_path / "spec.md"
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), baseline_commit=old_head)
    (tmp_path / "fixture.txt").write_text("resolution fixture\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "resolution fixture")
    new_head = git(tmp_path, "rev-parse", "HEAD")
    # written AFTER the commit: the claim has to name the sha the advance will adopt
    spec.write_text(
        f"---\nstatus: blocked\nbaseline_revision: {new_head}\n---\n\n## Intent\n\nx\n",
        encoding="utf-8",
    )

    runs.rearm_escalation(run_dir)

    (entry,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-restamped"]
    assert entry["overwritten"] == new_head  # the claim, carried verbatim
    assert entry["baseline"] == new_head  # which the advance happens to agree with
    assert entry["restore"] is False


def test_rearm_prefers_the_fresh_revision_when_the_spec_carries_both_keys(tmp_path):
    """A re-armed spec carries BOTH keys — this function is what puts them there.
    What it journals as overwritten must therefore be the value the gate reads,
    which is `baseline_revision`, not the stale legacy leftover (#716)."""
    old_head = _resolve_repo(tmp_path)
    run_dir, spec, new_head = _escalated_spec_run(
        tmp_path, old_head, extra=f"baseline_commit: {'a' * 40}\n"
    )

    runs.rearm_escalation(run_dir)

    (entry,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-baseline-restamped"]
    assert entry["overwritten"] == old_head  # NOT the stale baseline_commit
    fm = verify.read_frontmatter(spec)
    assert fm["baseline_revision"] == new_head
    assert fm["baseline_commit"] == "a" * 40  # the legacy key is never removed


def _rearmable(run_dir):
    """Re-escalate the task so a second `rearm_escalation` is legal."""
    state = load_state(run_dir)
    task = state.tasks["6-4-cli-list-command"]
    task.phase = Phase.ESCALATED
    return state


# -------------------------------------------------- non-UTF-8 robustness (bug class)
# A story spec / sentinel is agent- or human-authored, so it can contain non-UTF-8
# bytes. `read_text(encoding="utf-8")` raises UnicodeDecodeError (a ValueError, NOT an
# OSError), so the stories-mode read paths must tolerate it rather than crash an
# already-degraded escalation-resolution flow. Mirrors install.py's stories-support probe.

_BAD_UTF8 = b"\xff\xfe\x00\x01 not utf-8 \x80\x81"


def test_build_context_tolerates_non_utf8_present_spec(tmp_path):
    """A non-UTF-8 PRESENT story spec makes resolve_story_spec's frontmatter read
    raise UnicodeDecodeError; build_context must degrade to best-effort (folder-only)
    stories context, not crash the resolve command."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / f"{key}-slug.md").write_bytes(_BAD_UTF8)  # a real spec, undecodable
    run_dir, state, _ = _escalated_run(tmp_path, source="stories")

    path = resolve.build_context(state, run_dir, key)  # must not raise
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["stories"]["spec_folder"] == ""  # best-effort context still produced
    assert "sentinel" not in ctx["stories"]  # the undecodable spec yields no sentinel


def test_build_context_tolerates_non_utf8_sentinel(tmp_path):
    """A non-UTF-8 sentinel makes the blocking-condition read raise UnicodeDecodeError;
    build_context still emits the sentinel indicator with an empty condition."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / f"{key}-unresolved.md").write_bytes(_BAD_UTF8)  # undecodable sentinel
    run_dir, state, _ = _escalated_run(tmp_path, source="stories", sentinel_kind="unresolved")

    path = resolve.build_context(state, run_dir, key)  # must not raise
    ctx = json.loads(path.read_text(encoding="utf-8"))
    assert ctx["stories"]["sentinel"]["kind"] == "unresolved"
    assert ctx["stories"]["sentinel"]["blocking_condition"] == ""  # unreadable → empty


def test_rearm_non_utf8_present_spec_fails_clean_and_stays_armed(tmp_path):
    """The non-sentinel re-arm branch re-reads the spec as UTF-8 to flip its
    status. An undecodable PRESENT spec is a first-class escalation state
    (resolve_story_spec degrades it to a wedge), so it can reach this flip: rearm
    must fail with an actionable RearmError BEFORE anything is persisted — the
    escalation stays armed for a retry once the human fixes/replaces the file —
    never a traceback out of `bmad-loop resolve`."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    spec = stories_dir / f"{key}-slug.md"  # a real spec, not a fixed-slug sentinel
    spec.write_bytes(_BAD_UTF8)
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spec), source="stories")

    with pytest.raises(runs.RearmError) as exc:
        runs.rearm_escalation(run_dir)
    assert "UTF-8" in str(exc.value) and "resolve" in str(exc.value)
    assert spec.read_bytes() == _BAD_UTF8  # spec untouched
    task = load_state(run_dir).tasks[key]
    assert task.phase == Phase.ESCALATED  # nothing persisted; still armed for resolve


def test_rearm_tolerates_non_utf8_sentinel(tmp_path):
    """A binary/non-UTF-8 sentinel must not crash rearm_escalation: the sentinel is
    still preserved+deleted, the run re-arms, and the journal records an empty
    blocking condition rather than wedging the run on a decode error."""
    key = "6-4-cli-list-command"
    stories_dir = tmp_path / "stories"
    stories_dir.mkdir(parents=True)
    sentinel = stories_dir / f"{key}-unresolved.md"
    sentinel.write_bytes(_BAD_UTF8)
    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(sentinel), source="stories", sentinel_kind="unresolved"
    )

    assert runs.rearm_escalation(run_dir) == key  # must not raise
    assert not sentinel.exists()  # cleared by deletion
    assert (run_dir / "sentinels" / f"{key}-unresolved.md").is_file()  # copy preserved
    assert load_state(run_dir).tasks[key].spec_file is None  # cleared → PENDING re-dispatch

    cleared = [
        json.loads(line)
        for line in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "sentinel-cleared"
    ]
    assert cleared[0]["sentinel_kind"] == "unresolved" and cleared[0]["condition"] == ""


def test_read_resolution_non_utf8_marker_is_resolution_error(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8 marker
    must surface as the clean ResolutionError every consumer handles (CLI abort,
    TUI conservative warning), never an uncaught decode crash."""
    marker = resolve.resolution_path(tmp_path, "6-4-cli-list-command")
    marker.parent.mkdir(parents=True)
    marker.write_bytes(_BAD_UTF8)
    with pytest.raises(resolve.ResolutionError, match="unreadable"):
        resolve.read_resolution(tmp_path, "6-4-cli-list-command")


def test_rearm_rejects_non_escalation_stage(tmp_path):
    run_dir = tmp_path / ".bmad-loop" / "runs" / "r1"
    save_state(
        run_dir,
        RunState(
            run_id="r1", project=str(tmp_path), started_at="now", paused_stage="spec-approval"
        ),
    )
    with pytest.raises(runs.RearmError, match="not paused at an escalation"):
        runs.rearm_escalation(run_dir)


def test_rearm_rejects_unescalated_story(tmp_path):
    run_dir, state, task = _escalated_run(tmp_path)
    task.phase = Phase.DONE  # terminal but not escalated
    save_state(run_dir, state)
    with pytest.raises(runs.RearmError, match="not escalated"):
        runs.rearm_escalation(run_dir)


# ----------------------------------------------------------- run_session


class _FakeAdapter:
    def __init__(self, on_run):
        self._on_run = on_run

    def interactive_argv(self, spec):
        return ["fake-agent", spec.prompt]

    def interactive_env(self, spec):
        return dict(spec.env)


def test_run_session_detects_resolution(tmp_path, monkeypatch):
    run_dir, state, _ = _escalated_run(tmp_path)
    resolve.build_context(state, run_dir, "6-4-cli-list-command")

    def fake_subprocess_run(argv, cwd, env):
        # simulate the agent writing the resolution marker
        resolve.resolution_path(run_dir, "6-4-cli-list-command").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(resolve.subprocess, "run", fake_subprocess_run)
    adapter = _FakeAdapter(None)
    assert resolve.run_session(adapter, tmp_path, run_dir, "6-4-cli-list-command") is True


def test_run_session_no_resolution(tmp_path, monkeypatch):
    run_dir, state, _ = _escalated_run(tmp_path)
    resolve.build_context(state, run_dir, "6-4-cli-list-command")
    monkeypatch.setattr(resolve.subprocess, "run", lambda *a, **k: None)
    assert (
        resolve.run_session(_FakeAdapter(None), tmp_path, run_dir, "6-4-cli-list-command") is False
    )


def test_run_session_clears_stale_marker(tmp_path, monkeypatch):
    """A marker left by a previous resolve of this story must not be read as
    this session's output (the agent that says 'already resolved' writes none)."""
    run_dir, state, _ = _escalated_run(tmp_path)
    resolve.build_context(state, run_dir, "6-4-cli-list-command")
    stale = resolve.resolution_path(run_dir, "6-4-cli-list-command")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"from": "last time"}', encoding="utf-8")
    monkeypatch.setattr(resolve.subprocess, "run", lambda *a, **k: None)  # agent records nothing
    assert (
        resolve.run_session(_FakeAdapter(None), tmp_path, run_dir, "6-4-cli-list-command") is False
    )
    assert not stale.exists()  # stale marker was removed, not reused


# ---------------- item 9: build_context stories-mode enrichment --------------


def _stories_manifest(folder, entries):
    (folder / "stories").mkdir(parents=True, exist_ok=True)
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


def test_build_context_stories_carries_manifest_entry(tmp_path):
    """Stories mode: context.json carries the manifest intent (spec folder + the
    story entry's title/description/checkpoints/invoke_dev_with) for the resolver."""
    key = "6-4-cli-list-command"
    _stories_manifest(
        tmp_path / "epic-1",
        [
            {
                "id": key,
                "title": "List command",
                "description": "list notes",
                "spec_checkpoint": True,
                "invoke_dev_with": "use redis",
            }
        ],
    )
    run_dir, state, _ = _escalated_run(tmp_path, spec_file="/abs/spec.md", source="stories")
    state.spec_folder = "epic-1"

    ctx = json.loads(resolve.build_context(state, run_dir, key).read_text(encoding="utf-8"))
    st = ctx["stories"]
    assert st["spec_folder"] == "epic-1"
    assert st["story"]["title"] == "List command"
    assert st["story"]["spec_checkpoint"] is True
    assert st["story"]["invoke_dev_with"] == "use redis"
    assert "sentinel" not in st  # an ordinary escalation has no sentinel block


def test_build_context_stories_sentinel_indicator(tmp_path):
    """Stories mode: a sentinel-escalated story carries a sentinel indicator with
    its kind and recorded blocking condition (so the resolver knows there is no
    frozen spec to edit)."""
    key = "6-4-cli-list-command"
    folder = tmp_path / "epic-1"
    _stories_manifest(folder, [{"id": key, "title": "t", "description": "d"}])
    sentinel = folder / "stories" / f"{key}-unresolved.md"
    sentinel.write_text(
        "---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\nintent too vague\n",
        encoding="utf-8",
    )
    run_dir, state, _ = _escalated_run(tmp_path, spec_file=str(sentinel), source="stories")
    state.spec_folder = "epic-1"

    ctx = json.loads(resolve.build_context(state, run_dir, key).read_text(encoding="utf-8"))
    sent = ctx["stories"]["sentinel"]
    assert sent["kind"] == "unresolved"
    assert "intent too vague" in sent["blocking_condition"]


def test_build_context_sprint_mode_has_no_stories_block(tmp_path):
    """Sprint mode leaves the context contract unchanged — no stories block."""
    run_dir, state, _ = _escalated_run(tmp_path, spec_file="/abs/spec.md")  # sprint source
    ctx = json.loads(
        resolve.build_context(state, run_dir, "6-4-cli-list-command").read_text(encoding="utf-8")
    )
    assert "stories" not in ctx


def test_build_context_leaves_an_out_of_mount_spec_unchanged(tmp_path):
    """Matrix row 5 graded at the layer that PUBLISHES the path to a human.

    An absolute `spec_file` beside a set `worktree_path` is the out-of-mount shape
    (`_serialized_worktree_path` keeps a path verbatim exactly when
    `relative_to(worktree_path)` raises). `task_spec_path` passes it through untouched,
    so `context.json` must show it unchanged rather than re-anchored onto either tree.
    The row exists here because the `runs` and TUI layers grade the resolver while this
    is the surface that hands the value to the resolve agent.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "20260613-111429-6a14" / "worktrees" / "1"
    spec = tmp_path / "shared-artifacts" / "6-4.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nstatus: blocked\n---\n", encoding="utf-8")
    run_dir, state, _ = _escalated_run(tmp_path, spec_file=str(spec), worktree_path=str(wt))

    ctx = json.loads(
        resolve.build_context(state, run_dir, "6-4-cli-list-command").read_text(encoding="utf-8")
    )
    assert ctx["spec_file"] == spec.as_posix()


def test_build_context_stories_block_names_the_same_tree_as_spec_file(tmp_path):
    """One `context.json` must not name two different trees for one story.

    `spec_file` is anchored on the tree the run owns, but `_stories_context` resolved
    its folder from `Path(state.project)` — so under isolation the sentinel indicator
    and its recorded blocking condition described the MAIN CHECKOUT while `spec_file`
    named the mount. The agent was then told there is no frozen spec to edit (or handed
    a stale twin's blocking condition) for a story whose real sentinel sits in the
    worktree the re-arm actually writes to.

    The decoy is load-bearing: the project carries a sentinel at the same relpath with a
    DIFFERENT blocking condition, so "read the right tree" is checkable against "did not
    read the other one".

    Ablation: revert `_stories_context` to `stories.resolve_spec_folder(Path(state.project),
    ...)` and this reddens on the blocking condition — it reports the decoy's.
    """
    key = "6-4-cli-list-command"
    run_id = "20260613-111429-6a14"
    wt = tmp_path / ".bmad-loop" / "runs" / run_id / "worktrees" / "1"

    for root, condition in ((wt, "the mount's real halt"), (tmp_path, "the decoy twin")):
        folder = root / "epic-1"
        _stories_manifest(folder, [{"id": key, "title": "t", "description": "d"}])
        (folder / "stories" / f"{key}-unresolved.md").write_text(
            f"---\nstatus: blocked\n---\n\n## Auto Run Result\n\nStatus: blocked\n{condition}\n",
            encoding="utf-8",
        )

    rel = f"epic-1/stories/{key}-unresolved.md"
    run_dir, state, _ = _escalated_run(
        tmp_path, run_id, spec_file=rel, source="stories", worktree_path=str(wt)
    )
    state.spec_folder = "epic-1"

    ctx = json.loads(resolve.build_context(state, run_dir, key).read_text(encoding="utf-8"))
    assert ctx["spec_file"] == (wt / rel).as_posix()
    sent = ctx["stories"]["sentinel"]
    assert "the mount's real halt" in sent["blocking_condition"]
    assert "decoy" not in sent["blocking_condition"]
    assert Path(sent["path"]).is_relative_to(wt)  # the same tree spec_file named


def test_build_context_reports_whether_the_spec_reaches_the_redrive(tmp_path):
    """The agent is told when the file it is being sent to edit has no future.

    Under isolation `engine._finish_inflight` discards the mount, so an edit to a
    worktree-local spec succeeds and then vanishes before the re-drive reads anything.
    `rearm_escalation` already journals `rearm-spec-write-unreachable` on this verdict;
    emitting it here is what lets the session act on it rather than discover it after.

    Ablation: hardcode the field to `True` and the isolated leg reddens.
    """
    wt = tmp_path / ".bmad-loop" / "runs" / "20260613-111429-6a14" / "worktrees" / "1"
    run_dir, state, _ = _escalated_run(tmp_path, spec_file="specs/6-4.md", worktree_path=str(wt))
    ctx = json.loads(
        resolve.build_context(state, run_dir, "6-4-cli-list-command").read_text(encoding="utf-8")
    )
    assert ctx["spec_reaches_the_redrive"] is False

    plain_dir, plain_state, _ = _escalated_run(
        tmp_path, "20260613-111429-6a15", spec_file=str(tmp_path / "specs" / "6-4.md")
    )
    plain = json.loads(
        resolve.build_context(plain_state, plain_dir, "6-4-cli-list-command").read_text(
            encoding="utf-8"
        )
    )
    assert plain["spec_reaches_the_redrive"] is True


@pytest.mark.parametrize(
    ("committed_status", "warns"),
    [("ready-for-dev", False), ("blocked", True)],
)
def test_rearm_warns_about_an_unreachable_spec_write_only_when_it_is_actionable(
    tmp_path, monkeypatch, committed_status, warns
):
    """`rearm-spec-write-unreachable` must name an EVENT, not a configuration.

    Every escalated task under `isolation = "worktree"` carries a mounted
    `worktree_path` — `worktree_flow.escalate_unit` never clears it — so gating the
    record on that alone fired it on 100% of re-arms in that configuration. The advice
    it prints ("commit the corrected spec") is already a no-op once the committed spec
    carries the status the re-drive needs, which is exactly the state in which the
    re-drive reads what it needs. A record that fires on the routine case is the
    "trains the operator to scroll past the meaningful one" failure that the `flipped`
    read-back and the `overwritten != old_baseline` guard were each narrowed to avoid.

    Both legs keep the WORKTREE spec at `blocked`, so the only thing separating them is
    what git has committed — which is the whole claim.

    Ablation: restore the gate to a bare `if task.worktree_path:` and the
    `committed_status="ready-for-dev"` row reddens on `assert True is False`, while the
    `"blocked"` row keeps passing. That asymmetry IS the narrowing; a gate that fires
    for both is indistinguishable from no gate at all.
    """
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    _resolve_repo(tmp_path)
    main_spec = tmp_path / rel
    main_spec.parent.mkdir(parents=True, exist_ok=True)
    main_spec.write_text(
        f"---\nstatus: {committed_status}\n---\n\n## Intent\n\nx\n", encoding="utf-8"
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "spec")
    wt_spec = tmp_path / "wt" / rel
    wt_spec.parent.mkdir(parents=True, exist_ok=True)
    wt_spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")

    run_dir, _, _ = _escalated_run(tmp_path, spec_file=rel, worktree_path=str(tmp_path / "wt"))
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    unreachable = [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"]
    assert bool(unreachable) is warns
    if warns:
        assert unreachable[0]["status"] == "ready-for-dev"


@pytest.mark.parametrize(
    ("corrected_on_target", "warns"),
    [(True, False), (False, True)],
)
def test_rearm_reads_the_committed_spec_from_the_redrive_base_not_the_current_head(
    tmp_path, monkeypatch, corrected_on_target, warns
):
    """The proof is read at the run's PINNED target branch, not at the code root's HEAD.

    An isolated re-drive never reads the main checkout's working ref:
    `engine._finish_inflight` discards the escalated worktree AND its branch, and
    `workspace.open_unit_workspace` cuts the replacement from the `base` handed to it —
    `worktree_flow.run_isolated` passes `state.target_branch`, pinned once at run start.
    `ensure_target_branch` leaves the main checkout ON that branch, so the two agree
    until an operator checks out something else while the escalation is paused, which is
    a wholly ordinary thing to do with a run parked for a human.

    Both rows leave the main checkout on `side` and put the two candidate refs in
    DISAGREEMENT, so neither can pass by reading the other:

    - corrected on `main` (the target) — the re-drive will read `ready-for-dev` and
      route. Reading `HEAD` sees `side`'s terminal status and holds a resume whose work
      is already committed exactly where the re-drive looks for it.
    - corrected on `side` (the current branch) — the re-drive reads `main`'s terminal
      status and re-wedges. Reading `HEAD` sees the correction and SUPPRESSES the
      record, which since `rearm_holds_the_resume` is not a mis-worded warning but the
      default resolve flow resuming into the wedge it was meant to clear.

    The worktree copy is held at `blocked` on both rows, so the only moving part is
    which committed ref carries the correction.

    Ablation: restore the revision argument in `_committed_spec_status` to a literal
    `"HEAD"` and BOTH rows redden (`assert False is True` / `assert True is False`) —
    the pair is the discriminator; either row alone also passes for the anchor it is
    meant to reject.
    """
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    _resolve_repo(tmp_path)
    spec = tmp_path / rel
    spec.parent.mkdir(parents=True, exist_ok=True)

    def _commit(status, message):
        spec.write_text(f"---\nstatus: {status}\n---\n\n## Intent\n\nx\n", encoding="utf-8")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-q", "-m", message)

    _commit("blocked", "escalated spec")  # on `main`, the target branch
    git(tmp_path, "checkout", "-q", "-b", "side")
    if corrected_on_target:
        git(tmp_path, "checkout", "-q", "main")
        _commit("ready-for-dev", "corrected on the target branch")
        git(tmp_path, "checkout", "-q", "side")  # the operator wandered off again
    else:
        _commit("ready-for-dev", "corrected on the wrong branch")

    wt_spec = tmp_path / "wt" / rel
    wt_spec.parent.mkdir(parents=True, exist_ok=True)
    wt_spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")

    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=rel, worktree_path=str(tmp_path / "wt"), target_branch="main"
    )
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    unreachable = [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"]
    assert bool(unreachable) is warns
    if warns:
        # the branch rides on the record because the remedy is worthless without it:
        # "commit the corrected spec" is what this operator just DID, on `side`
        # spelled `target_branch` so `diagnostics._JOURNAL_ALIAS_FIELDS` routes it to
        # the `branch` namespace; a new field name would fall to `scrub_json`, which
        # ships an identifier-shaped branch verbatim
        assert unreachable[0]["target_branch"] == "main"
        assert "`main`" in runs.rearm_event_notice(unreachable[0])[2]


def test_rearm_base_ref_degrades_to_head_for_a_run_that_pinned_no_target(tmp_path, monkeypatch):
    """An unrecorded `target_branch` is a MISSING value, not a divergent one.

    `ensure_target_branch` pins the field before any worktree can mount, so only a
    state.json predating it reaches here with a `worktree_path` and no target — and it
    must degrade to the ref it read before the fix rather than to `""`. Answering `""`
    would make `_committed_spec_status` unprovable for every re-arm of such a run and
    hold the resume on a per-configuration constant, the exact failure the record's
    narrowing exists to avoid.

    The committed spec here already carries the target status, so a degrade to `""`
    is separable from the read succeeding: only an anchor that actually resolves can
    suppress. Ablation: make `_redrive_base_ref` return `""` on the migrated shape and
    this reddens on the record appearing.
    """
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    _resolve_repo(tmp_path)
    spec = tmp_path / rel
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nstatus: ready-for-dev\n---\n\n## Intent\n\nx\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "corrected spec")
    wt_spec = tmp_path / "wt" / rel
    wt_spec.parent.mkdir(parents=True, exist_ok=True)
    wt_spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")

    # no `target_branch=`: the pre-upgrade shape, worktree_path set all the same
    run_dir, _, _ = _escalated_run(tmp_path, spec_file=rel, worktree_path=str(tmp_path / "wt"))
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    assert [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"] == []


@pytest.mark.parametrize("committed_status", ["ready-for-dev", "blocked"])
def test_rearm_does_not_refuse_a_flip_the_redrive_never_reads(
    tmp_path, monkeypatch, committed_status
):
    """The flip's REFUSAL is gated on reachability, not merely on readability.

    A worktree-local spec is the one file the re-drive is guaranteed NOT to read: the
    re-arm's final arm discards the worktree and `_run_story` mounts a fresh one, which
    checks out TRACKED files only. Gating the abort on `spec_path.is_file()` alone
    refused the re-arm over exactly that copy, and the refusal's remedy — "add a
    top-level `status:` to the spec" — named a file deleted before anything opens it. An
    operator who complied would flip a doomed copy, re-run resolve, and change nothing
    about how the re-drive routes.

    Both legs keep the worktree spec unflippable (no `---` block at all) and differ only
    in what git has committed, which is what makes this a reachability claim rather than
    a readability one:

    - `ready-for-dev` — `_committed_spec_status` has already PROVEN the re-drive reads
      the status it routes on. Refusing blocked an otherwise-complete re-arm over an
      obsolete copy, with nothing for the operator to do at all.
    - `blocked` — the correction really is outstanding, and the remedy is
      `rearm-spec-write-unreachable`'s ("commit the corrected spec"), which fires from
      the block above and holds the resume. The flip's own refusal would have named the
      wrong file for the right problem.

    Ablation: restore the abort's gate to a bare `if spec_path.is_file():` and BOTH legs
    redden on `RearmError` before any assertion runs. Hard-code `write_reaches_the_redrive`
    False instead and `test_rearm_journals_a_status_flip_that_silently_did_nothing[no-frontmatter]`
    reddens on `DID NOT RAISE` — the half this must not take with it.
    """
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    _resolve_repo(tmp_path)
    main_spec = tmp_path / rel
    main_spec.parent.mkdir(parents=True, exist_ok=True)
    main_spec.write_text(
        f"---\nstatus: {committed_status}\n---\n\n## Intent\n\nx\n", encoding="utf-8"
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "spec")
    wt_spec = tmp_path / "wt" / rel
    wt_spec.parent.mkdir(parents=True, exist_ok=True)
    wt_spec.write_text("# Spec\n\nno frontmatter block\n", encoding="utf-8")

    run_dir, _, _ = _escalated_run(tmp_path, spec_file=rel, worktree_path=str(tmp_path / "wt"))
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)  # must not raise: this flip cannot reach the re-drive

    kinds = _kinds(run_dir)
    (skipped,) = [e for e in kinds if e["kind"] == "rearm-spec-flip-skipped"]
    assert skipped["refused"] is False  # what the surfaces render the remedy from
    assert load_state(run_dir).tasks["6-4-cli-list-command"].phase == Phase.PENDING
    # ...and the record that DOES carry an actionable remedy fires only when it has one
    unreachable = [e for e in kinds if e["kind"] == "rearm-spec-write-unreachable"]
    assert bool(unreachable) is (committed_status != "ready-for-dev")


def test_rearm_suppresses_the_unreachable_warning_only_on_proof(tmp_path, monkeypatch):
    """A project that is not a repo must still WARN, not fall silent.

    `_committed_spec_status` degrades to `""` on every uncertainty — a `GitError`
    (which covers "not a repository"), an absent blob, a non-UTF-8 blob. `""` never
    equals a target status, so the record fires. This is the direction the narrowing
    must fail in: suppressing a warning demands proof the work is done, and the
    re-arm advance stays non-fatal outside a repo, as the story's Boundaries require.

    Ablation: make `_committed_spec_status` return `target_status` on `GitError`
    instead of `""` and this reddens on the missing record — the re-arm still
    "succeeds", which is exactly the silence #640(b) exists to end.
    """
    rel = "_bmad-output/specs/6-4-cli-list-command.md"  # no _resolve_repo: not a repo
    wt_spec = tmp_path / "wt" / rel
    wt_spec.parent.mkdir(parents=True, exist_ok=True)
    wt_spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")

    run_dir, _, _ = _escalated_run(tmp_path, spec_file=rel, worktree_path=str(tmp_path / "wt"))
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    kinds = _kinds(run_dir)
    (unreachable,) = [e for e in kinds if e["kind"] == "rearm-spec-write-unreachable"]
    assert unreachable["status"] == "ready-for-dev"
    # and the advance's own degrade is reported alongside it, not instead of it
    assert [e for e in kinds if e["kind"] == "rearm-baseline-advance-failed"]


def test_rearm_does_not_warn_when_the_spec_dir_is_shared_with_the_redrive(tmp_path, monkeypatch):
    """A spec in an artifact dir configured OUTSIDE the project is reachable.

    `ProjectPaths.rebased` leaves an artifact dir outside the project tree exactly
    where it is — shared across checkouts, not per-worktree — and
    `verify.resolve_spec_path` passes an absolute value through untouched, so the fresh
    worktree's re-drive opens the very file this flip writes. Under isolation that is
    also the only shape `model._serialized_worktree_path` can persist ABSOLUTE: a spec
    under the mounted worktree is stored relative to it.

    So the warning's whole premise — "the re-drive destroys the worktree before reading
    anything" — is false here, and firing it told the operator to commit a file that is
    not in the repository at all, on 100% of re-arms in that layout. The status
    assertion is what makes this a reachability claim rather than a silence claim: the
    write lands on the shared path (`set_frontmatter_status` keeps the plain no-follow
    arm for a spec outside `confine_root`, which its docstring names as this exact
    supported configuration), so there is nothing left for the operator to do.

    Ablation: drop the `not _spec_is_shared_with_the_redrive(task)` term from the gate
    in `rearm_escalation` and this reddens on the record's presence
    (`assert [] == []` fails with the entry).
    """
    _resolve_repo(tmp_path)
    shared = tmp_path.parent / "shared-artifacts"  # outside the project tree
    spec = shared / "specs" / "6-4-cli-list-command.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")

    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(spec), worktree_path=str(tmp_path / "wt")
    )
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    assert [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"] == []
    # and the flip really landed on the shared file the re-drive will read
    assert "status: ready-for-dev" in spec.read_text(encoding="utf-8")


def test_rearm_still_warns_for_a_spec_spelled_out_of_but_resolving_into_the_worktree(
    tmp_path, monkeypatch
):
    """The exemption is containment, not spelling.

    `model._serialized_worktree_path` relativizes with a LEXICAL `relative_to`, so any
    spelling that does not share a literal prefix with `worktree_path` is persisted
    absolute — including one that walks back INTO the worktree through a sibling. That
    spec is discarded with the worktree like any other, so the warning must survive; a
    helper that read `is_absolute()` as "shared" would go silent on it. (The lexical
    prefix test cannot separate the two rows: it is the same comparison, on the same
    two operands, that decided the value was stored absolute in the first place.)

    The mount is deliberately outside the project, so the helper's project half cannot
    carry this row — `test_rearm_warns_when_an_isolated_tasks_spec_writes_cannot_reach_the_redrive`
    grades that one. Dropping the worktree half reddens here and nowhere else.

    Ablation: replace the canonical comparison in `_spec_is_shared_with_the_redrive`
    with `return True` — accepting the recorded spelling as proof — and this reddens on
    the missing record (`ValueError: not enough values to unpack`), while the
    shared-artifact-dir row above keeps passing. That asymmetry is the containment test.
    """
    _resolve_repo(tmp_path)
    # the mount sits OUTSIDE the project, so the project half of the test cannot answer
    # this row — `workspace.open_unit_workspace` stores a resolved path, and a symlinked
    # `.bmad-loop` puts it here. Only the worktree half can keep the warning.
    wt = tmp_path.parent / "u1-worktree"
    (tmp_path.parent / "u1-side").mkdir(exist_ok=True)  # real, so `u1-side/..` folds
    real = wt / "_bmad-output" / "specs" / "6-4-cli-list-command.md"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")
    # lexically outside `wt` (the components diverge at `u1-side`), canonically inside it
    spelled = tmp_path.parent / "u1-side" / ".." / wt.name / "_bmad-output" / "specs" / real.name

    run_dir, _, _ = _escalated_run(tmp_path, spec_file=str(spelled), worktree_path=str(wt))
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    (unreachable,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"]
    assert unreachable["status"] == "ready-for-dev"


def test_rearm_warns_when_the_spec_cannot_be_placed_against_the_worktree(tmp_path, monkeypatch):
    """A host that cannot canonicalize the spec keeps the warning.

    `_spec_is_shared_with_the_redrive` decides on `resolve()`, which raises on the hosts
    #552 is about (a registered-but-not-serving WSL UNC provider) — and an uncertain
    answer must not buy silence, the same direction `_committed_spec_status` degrades
    in. This row is the shared-artifact-dir row above with resolution taken away, so it
    is the fault, not the layout, that flips the outcome.

    Ablation: drop the `except (OSError, RuntimeError)` arm and this reddens with the
    OSError escaping `rearm_escalation` — a re-arm that crashes on an observation is
    strictly worse than one that warns.
    """
    _resolve_repo(tmp_path)
    shared = tmp_path.parent / "shared-artifacts-unresolvable"
    spec = shared / "specs" / "6-4-cli-list-command.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nstatus: blocked\n---\n\n## Intent\n\nx\n", encoding="utf-8")

    real_resolve = platform_util.Path.resolve

    def _refuse(self, *a, **kw):
        if "shared-artifacts-unresolvable" in str(self):
            raise OSError(64, "The specified network name is no longer available")
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(platform_util.Path, "resolve", _refuse)

    run_dir, _, _ = _escalated_run(
        tmp_path, spec_file=str(spec), worktree_path=str(tmp_path / "wt")
    )
    monkeypatch.chdir(tmp_path)

    runs.rearm_escalation(run_dir)

    (unreachable,) = [e for e in _kinds(run_dir) if e["kind"] == "rearm-spec-write-unreachable"]
    assert unreachable["status"] == "ready-for-dev"


def test_rearm_writes_the_project_rooted_spec_when_no_worktree_was_recorded(tmp_path, monkeypatch):
    """The `state.project` half of `task_spec_path` — graded, not merely reachable.

    `engine._finish_inflight` clears `task.worktree_path` while leaving `spec_file`
    relative, and `model._serialized_worktree_path` returns it unchanged when
    `worktree_path` is falsy, so a state.json legitimately holds a relative spec with
    no worktree. Both rows that reach this branch today name a file absent under EITHER
    root, so they answer `is_file() -> False` identically whichever root is used and
    pass with the anchor reverted to the process cwd.

    The decoy is the grading instrument: it sits at the SAME relative path under the
    directory this process actually runs from, so a cwd-anchored resolve has somewhere
    plausible to land.

    Ablation: change `task_spec_root` to `Path(task.worktree_path or "")` and this
    reddens twice over — the project spec keeps `status: blocked`, and the decoy's
    byte-identity assertion fails behind it.
    """
    head = _resolve_repo(tmp_path)
    rel = "_bmad-output/specs/6-4-cli-list-command.md"
    body = "---\nstatus: blocked\nbaseline_revision: stale-sha\n---\n\n## Intent\n\nx\n"
    spec = tmp_path / rel
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(body, encoding="utf-8")
    decoy = tmp_path / "elsewhere" / rel
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text(body, encoding="utf-8")
    untouched = decoy.read_bytes()

    run_dir, _, _ = _escalated_run(tmp_path, spec_file=rel)  # worktree_path="" -> the fallback
    monkeypatch.chdir(tmp_path / "elsewhere")

    runs.rearm_escalation(run_dir)

    fm = verify.read_frontmatter(spec)
    assert fm["status"] == "ready-for-dev"  # the project-rooted copy was flipped
    assert fm["baseline_revision"] == head  # and re-stamped
    assert decoy.read_bytes() == untouched  # the cwd-rooted decoy is unread


@pytest.mark.parametrize(
    ("field", "value"),
    [("files", 3), ("files", None), ("files", [1, 2]), ("commits", 3), ("commits", None)],
)
def test_rearm_event_notice_survives_a_journal_shape_json_admits(field, value):
    """A malformed journal line must not raise out of either surface's `finally`.

    `Journal.entries()` appends `json.loads(line)` with no shape filter, so its
    `list[dict[str, Any]]` annotation is a claim about first-party producers rather
    than a guarantee — pyright sees `Any` and is satisfied. `", ".join` and `len` are
    the only two reads in `rearm_event_notice` that raise on a shape the journal
    admits; every sibling is `str()`-wrapped or f-string-interpolated. Both run inside
    `cli.cmd_resolve`'s and `TuiApp._do_rearm`'s `finally`, and the TUI has no
    `_handle_exception` override, so there a `TypeError` ends the app.

    Note `entry.get("files", [])` does NOT protect against `null`: the key EXISTS, so
    the default never applies and `.get` returns `None`.

    Ablation: restore `", ".join(entry.get("files", []))` and `len(entry.get(
    "commits", []))` and every row but `("files", [1, 2])` reddens with `TypeError`;
    that row reddens too, on `sequence item 0: expected str instance, int found`.
    """
    kind = "stale-restore-excluded" if field == "files" else "stale-restore-commits"
    notice = runs.rearm_event_notice({"kind": kind, field: value})

    assert notice is not None
    severity, message, _ = notice
    assert severity in ("note", "warning")
    assert isinstance(message, str)


def test_rearm_event_notice_splits_the_flip_skip_on_the_refusal():
    """One kind, two outcomes — and the operator-facing halves must not be swapped.

    `rearm-spec-flip-skipped` is journalled by a re-arm that ABORTED and by one that
    completed, and this table reads the journal out of process, with neither the task
    nor the tree to re-derive which happened. Before the producer wrote `refused` onto
    the record, this row claimed the abort unconditionally: an operator whose re-arm had
    SUCCEEDED was told it "was REFUSED" and sent to add a `status:` to a file the
    re-drive never opens. The next_step is graded alongside the message because it is
    the half that costs the operator time — a remedy aimed at the wrong file.

    Ablation: delete the `if entry.get("refused")` branch and the second leg's
    assertions redden; return the refusal's next_step on both and the last one does
    alone.
    """
    entry = {
        "kind": "rearm-spec-flip-skipped",
        "spec_file": "wt/specs/s1.md",
        "status": "ready-for-dev",
    }
    _, refused_msg, refused_step = runs.rearm_event_notice({**entry, "refused": True})
    assert "REFUSED" in refused_msg
    assert refused_step == "Add a top-level `status:` to the spec, then re-run resolve"

    _, msg, step = runs.rearm_event_notice({**entry, "refused": False})
    assert "NOT refused" in msg
    assert "COMMITTED spec" in msg
    # nothing to do to THIS file; `rearm-spec-write-unreachable` carries the remedy on
    # exactly the legs that still have one, and holds the resume behind it
    assert step == ""


def test_rearm_holds_the_resume_only_on_the_record_that_proves_a_wedge():
    """The hold is PROOF, not urgency — and it is asked of every kind the table knows.

    `rearm-spec-write-unreachable` is written only once `_committed_spec_status` has
    established that the committed spec does not carry the status the re-drive routes
    on, so resuming on it is futile rather than risky: step-01 halts blocked on
    `unrecognized status in existing story file` and the escalation is spent. Its
    next_step already read "commit the corrected spec before resuming" while both
    default surfaces resumed in the same breath.

    The advisory kinds must NOT hold. `stale-restore-commits` is the record
    `cli._echo_rearm_events`' own docstring calls the one a human must act on, and it
    still does not qualify — nothing about it proves the re-drive cannot route, and
    holding on a maybe would turn every ordinary degrade into a two-command gesture.
    That asymmetry IS the predicate; one that answered True for every warning would be
    indistinguishable from no predicate at all.

    Ablation: widen the comparison to `str(kind).startswith("rearm-")` and the three
    `rearm-baseline-*` legs redden; return False unconditionally and the first does.
    """
    assert runs.rearm_holds_the_resume({"kind": "rearm-spec-write-unreachable"}) is True
    for kind in (
        "stale-restore-commits",
        "stale-restore-unparseable",
        "stale-restore-excluded",
        "rearm-baseline-advance-failed",
        "rearm-baseline-restamp-skipped",
        "rearm-baseline-restamped",
        "rearm-spec-flip-skipped",
        "story-escalation-resolved",
    ):
        assert runs.rearm_holds_the_resume({"kind": kind}) is False, kind
    # the non-mapping shapes `rearm_event_notice` survives reach this in the SAME walk,
    # and it is asked first — a raise here would replace the outcome the operator needs
    assert runs.rearm_holds_the_resume(3) is False
    assert runs.rearm_holds_the_resume(None) is False


def test_rearm_event_notice_ignores_a_non_mapping_entry():
    """A bare scalar on its own journal line is not an entry.

    `journal_entries_or_none` drops non-mappings so the annotation is true for every
    caller, and both reads apply the same filter so the `len(before)` watermark stays
    exact. This pins the notice's own guard as well, since it is reachable from any
    other caller walking raw `Journal.entries()`.

    Ablation: delete the `if not isinstance(entry, dict)` arm and this reddens with
    `AttributeError: 'int' object has no attribute 'get'`.
    """
    assert runs.rearm_event_notice(3) is None
    assert runs.rearm_event_notice(None) is None


def test_journal_entries_or_none_drops_a_non_mapping_line(tmp_path):
    """The watermark both surfaces diff is a list of MAPPINGS.

    Ablation: return `Journal(run_dir).entries()` unfiltered and this reddens on the
    length assertion — the bare `3` survives into the window the surfaces walk.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(
        '{"kind": "a"}\n3\nnull\n{"kind": "b"}\n', encoding="utf-8"
    )

    entries = runs.journal_entries_or_none(run_dir)

    assert entries is not None
    assert [e["kind"] for e in entries] == ["a", "b"]
