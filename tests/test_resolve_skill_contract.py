"""Contract guards for the shipped `bmad-loop-resolve` skill.

`resolve.build_context` writes `context.json` and the skill is its ONLY consumer, so
a key added to one side and not the other is inert by construction: the orchestrator
computes a verdict, the agent never reads it, and the session it was meant to steer
proceeds exactly as before. That is not hypothetical — `spec_reaches_the_redrive`
shipped that way, emitted beside `spec_file` while the skill's schema, its step 4 and
its commit prohibition all stayed silent, so the agent edited the worktree-local copy
the re-drive discards and recorded a successful resolution over lost work.
"""

import ast
import inspect

import pytest

SKILL_DIR = "bmad-loop-resolve"


@pytest.fixture(scope="module")
def skill_md():
    from importlib import resources

    return (
        resources.files("bmad_loop.data")
        .joinpath("skills")
        .joinpath(SKILL_DIR)
        .joinpath("SKILL.md")
        .read_text(encoding="utf-8")
    )


def _emitted_context_keys() -> set[str]:
    """The top-level keys `build_context` writes into `context.json`.

    Read from the SOURCE rather than by calling it, so the set is complete without
    having to build a fixture that takes every optional arm (`stories` is only
    attached in stories mode). A key that is added to the literal is therefore in
    scope for the guard the moment it is written.
    """
    from bmad_loop import resolve

    tree = ast.parse(inspect.getsource(resolve))
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_context"
    )
    keys: set[str] = set()
    for node in ast.walk(fn):
        # `context = {...}` — the literal
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        # `context["stories"] = ...` — the conditional arm
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                ):
                    keys.add(tgt.slice.value)
    return keys


def test_every_emitted_context_key_is_documented(skill_md):
    """Each key `build_context` emits is named in the skill the agent reads.

    Either spelling counts: `"key"` inside a schema block, or `` `key` `` in prose —
    `restore_supported` is documented the second way (a whole section turns on it)
    and is no less binding for it. What the guard refuses is a key documented
    NEITHER way, which is a signal with no reader.

    Ablation: drop `"spec_reaches_the_redrive"` from SKILL.md and this reddens
    naming it.
    """
    undocumented = sorted(
        k for k in _emitted_context_keys() if f'"{k}"' not in skill_md and f"`{k}`" not in skill_md
    )
    assert not undocumented, (
        "context.json emits keys the resolve skill never mentions, so the agent "
        "cannot act on them — document each in SKILL.md's schema block or prose: "
        f"{undocumented}"
    )


def test_context_key_scan_is_not_vacuous():
    """The guard above asserts an ABSENCE, so it passes for every reason the key set
    could come back empty — a renamed function, a refactor to a builder, an `ast`
    walk that silently matches nothing. Pin the shape it depends on."""
    keys = _emitted_context_keys()
    assert {"spec_file", "spec_reaches_the_redrive", "stories", "resolution_path"} <= keys
    assert len(keys) >= 8


def test_skill_branches_on_spec_reachability(skill_md):
    """Documenting the field is not enough — the skill has to TELL the agent what to
    do differently when it is false, or the lost-work scenario it was added for
    plays out unchanged.

    The three sites that must agree: the schema (so it is expected), step 4 (so the
    edit is flagged rather than silently doomed), and the commit prohibition (which
    otherwise reads as forbidding the very remedy step 4 now demands).
    """
    normalized = " ".join(skill_md.split())

    assert '"spec_reaches_the_redrive": true,' in skill_md  # schema block
    # the edit still happens — skipping it would leave nothing to carry over
    assert "make the same edit and then say plainly" in normalized
    assert "the correction must be committed to reach the re-drive" in normalized
    # and the prohibition names whose job the commit is, rather than just refusing it
    assert "committing the corrected spec is the HUMAN's step" in normalized
