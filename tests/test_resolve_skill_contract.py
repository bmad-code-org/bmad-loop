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


def test_skill_documents_escalation_ordering_and_global_uniqueness(skill_md):
    """The context consumer can rely on the reader's cross-session guarantees.

    These assertions are deliberately separate so removing either the ordering
    promise or the global de-duplication promise fails the contract guard.
    """
    after_schema = skill_md.split("}\n```\n\n", maxsplit=1)[1]
    contract_lines = after_schema.split("\n\n", maxsplit=1)[0].splitlines()

    assert "The `escalations` array is ordered newest-first." in contract_lines
    assert (
        "Across the entire gathered context, each distinct escalation appears exactly once."
        in contract_lines
    )
    assert len(contract_lines) == 2


def test_skill_presents_paused_reason_when_no_newer_escalation_detail(skill_md):
    """A watermarked pause can have no new session escalation to present.

    The positive non-empty assertion keeps the empty-path prohibitions from being
    satisfied by deleting recorded-entry handling altogether.
    """
    presentation_step = skill_md.split(
        "2. **Present the current pause evidence plainly**", maxsplit=1
    )[1].split("\n3. **Get the human's decision.**", maxsplit=1)[0]
    normalized = " ".join(presentation_step.split())

    assert "When the `escalations` array is non-empty" in normalized
    assert "present its recorded entries in their existing newest-first order" in normalized
    assert "Do not replace recorded escalation detail with `paused_reason`." in normalized
    assert "When the `escalations` array is empty" in normalized
    assert (
        "present `paused_reason` verbatim as the available evidence for the current pause"
        in normalized
    )
    assert "no newer recorded escalation detail is available" in normalized
    assert "Do not read below the watermark" in normalized
    assert "unfilter or recover an older artifact escalation" in normalized
    assert "synthesize an escalation object from `paused_reason`" in normalized
    assert (
        "require `paused_reason` to be text containing at least one non-whitespace character"
        in normalized
    )
    assert "missing, `null`, non-text, or blank after trimming" in normalized
    assert "report a malformed resolve context and do not write the resolution marker" in normalized

    shared_requirement = (
        "Using the selected evidence, explain what is ambiguous or contradictory, why it "
        "blocks safe implementation, and offer **2–4 concrete resolution options** with a "
        "clear recommendation and its trade-offs."
    )
    assert shared_requirement in normalized
    assert normalized.index(shared_requirement) > normalized.index(
        "When the `escalations` array is empty"
    )
    assert (
        "\n\n   Using the selected evidence, explain what is ambiguous or contradictory"
        in presentation_step
    )


def test_skill_routes_project_artifacts_and_code_work_to_their_distinct_roots(skill_md):
    """Mentioning both keys is inert unless the skill explains the operational split.

    Each assertion grades a separate instruction the divergent-root resolution needs:
    where the session starts, where BMAD artifact/spec work stays, and where code/git
    work belongs. Removing the substantive guidance while leaving the schema keys
    documented therefore fails this contract rather than passing the key census above.
    """
    normalized = " ".join(skill_md.split())

    assert "session's working directory is always `project_root`" in normalized
    assert (
        "artifact and spec work remains anchored under `project_root` (or at the explicit "
        "absolute paths in this context)"
    ) in normalized
    assert "When the roots differ" in normalized
    assert "any code fix or commit the human must make belongs under `code_root`" in normalized


def test_skill_branches_on_the_in_place_remedy(skill_md):
    """`spec_reaches_the_redrive: false` has TWO remedies, and the wrong one is lost
    work in the other direction.

    A `redrive_base_ref` of `HEAD` means the re-drive runs in the main checkout and
    reads its WORKING tree — reachable when the run's isolation policy is edited to
    `none` while the story sits escalated, which leaves `spec_file` pointing into a
    mount the run has stopped using. The skill's own remedy text was written for the
    isolated case alone, and applied verbatim it sends the human to commit onto a
    branch this run never reads while the file the re-drive DOES read stays wrong. So
    the skill has to name `HEAD` and say the remedy is different there, not merely
    document the field.

    Ablation: delete the "When `redrive_base_ref` is `HEAD`" paragraph from SKILL.md
    and this reddens on the first assertion; restore the instruction sentence to its
    unconditional "**where the correction has to land to be read**: committed on
    `redrive_base_ref`." and it reddens on the three-site assertion instead.
    """
    normalized = " ".join(skill_md.split())

    assert "When `redrive_base_ref` is `HEAD`, do not tell them to commit anything." in normalized
    # ...and it says WHERE instead, in the tree the in-place re-drive actually reads
    assert "make the same edit to the main checkout's copy of the spec" in normalized
    # THREE sites carry the fork, not two. The instruction sentence, step 4 and the
    # prohibition each tell the human where the correction lands, and any one of them
    # left unconditional is a blanket "commit it" the agent can act on before reaching
    # the paragraph that carved the exception. This row first enumerated only the
    # latter two; the instruction sentence — the one that most directly says "tell the
    # human" — kept the absolute spelling for four commits because nothing graded it.
    assert (
        "which the field decides: when `redrive_base_ref` names a branch, committed on "
        "`redrive_base_ref`; when it is `HEAD`, re-applied in the main checkout, uncommitted."
    ) in normalized
    assert "re-applied in the main checkout, uncommitted" in normalized
    assert "re-applying it in the main checkout when it is `HEAD`" in normalized


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
    # and it names WHERE the correction has to land. Without this the field states a
    # problem with no remedy, and both obvious moves fail silently: the main checkout
    # cannot commit a file living in a linked worktree, and the unit's own branch is
    # not what the replacement mount is cut from.
    assert "committed on `redrive_base_ref`" in normalized
    assert "cannot include the file you edited" in normalized
    assert "cut fresh from `redrive_base_ref`" in normalized
    # and the prohibition names whose job the landing is, rather than just refusing it
    assert "is the HUMAN's step" in normalized


def test_skill_explains_null_reachability_for_stories_sentinels(skill_md):
    """A sentinel retains a path but deliberately has no frozen-spec verdict, so
    the general null definition must not tell the agent that no path was recorded.
    """
    normalized = " ".join(skill_md.split())

    assert "no ordinary frozen spec to edit" in normalized
    assert "stories mode recorded a sentinel path instead" in normalized
    assert "follow the sentinel guidance below" in normalized
