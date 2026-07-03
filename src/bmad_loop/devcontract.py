"""Translate the generic `bmad-dev-auto` skill's output into the orchestrator's
result.json contract.

Alex Verhovsky's upstream `bmad-dev-auto` skill (BMAD-METHOD PR #2500) is a
decoupled autonomous-coding primitive: it writes NO result.json. Its outcome
lives in the spec it produced — `status:` in the frontmatter (the machine-
consumable signal) plus an appended `## Auto Run Result` prose section (intended
for an LLM deciding how to handle failure). This module is the thin Python shim
that turns that on-disk spec into the legacy result dict that verify.py /
escalation.py already consume, so the rest of the pipeline stays unchanged.

DOCTRINE — never trust prose for a gate. The frontmatter `status:` read straight
off disk is authoritative; the `## Auto Run Result` prose is only used to route
the blocked→PAUSE decision and to carry a human-readable detail. Where the two
disagree we surface it (`status_consistent=False`) so the caller can fail safe
(treat a mismatch as a retry rather than silently proceeding). Every real
deterministic gate (git baseline, worktree-changed, sprint advancement, dw_id
match) still runs in verify.py against actual on-disk state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .verify import DEV_WORKFLOW, read_frontmatter

# The section the skill appends on EVERY terminal path (success and blocked),
# per its step-02/03/04 finalize instructions. Its presence is our completion
# marker on the spec-watch fallback; the `Status:` line within it is the only
# field we parse structurally — everything else is free prose.
AUTO_RUN_HEADING_RE = re.compile(r"^##\s+Auto Run Result\s*$", re.MULTILINE)
# `Status:` possibly bulleted ("- Status: blocked") / bolded ("**Status:** done"),
# case-insensitive on the label, value is the first token on the line.
STATUS_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?status(?:\*\*)?\s*:\s*(?:\*\*)?\s*([A-Za-z-]+)",
    re.IGNORECASE | re.MULTILINE,
)

# Terminal frontmatter statuses the skill can leave behind.
DONE = "done"
BLOCKED = "blocked"

# Frontmatter statuses a half-finalized generic spec may be reconciled FROM when
# its prose terminal `## Auto Run Result` Status is `done`. Deliberately an
# allowlist: anything else (already-`done`, `blocked`, or an unknown custom token)
# is left untouched, so reconciliation can never override a status the skill set on
# purpose. `""` covers a blank or missing frontmatter `status:` — `reset_spec_status`
# fills/inserts the line in that case. `in-review` is included because on the sole
# (generic `bmad-dev-auto`) path it is only ever the transient marker step-04 sets at
# its start; the skill self-finalizes to `done`. The legacy `bmad-loop-dev` fork that
# used `in-review` as a deliberate review-handoff terminal is retired, so nothing
# leaves `in-review` on purpose anymore.
RECONCILABLE_FROM = frozenset({"", "draft", "ready-for-dev", "in-progress", "in-review"})

# The leading `---\n …frontmatter… \n---` block, captured in three parts so the
# body can be rewritten while the fences stay byte-identical.
_FRONTMATTER_RE = re.compile(r"\A(---\r?\n)(.*?\r?\n)(---[ \t]*\r?\n)", re.DOTALL)
# A frontmatter `status:` line, preserving indent, the `: ` gap, optional quotes,
# and any trailing inline comment. Only the value token is rewritten. The value is
# `*` (not `+`) so a present-but-empty status (`status:` / `status: ""`) is matched
# and filled — a bmad-dev-auto template can leave it blank.
_FM_STATUS_RE = re.compile(
    r"^(?P<pre>[ \t]*status[ \t]*:[ \t]*)(?P<q>['\"]?)(?P<val>[A-Za-z-]*)(?P=q)(?P<rest>.*)$",
    re.MULTILINE,
)

# The skill's no-spec fallback artifact (HALT when {spec_file} is unknown/missing):
# `{implementation_artifacts}/bmad-dev-auto-result-<slug-or-timestamp>.md`. It
# carries a terminal frontmatter `status:` but no `## Auto Run Result` heading.
FALLBACK_RESULT_PREFIX = "bmad-dev-auto-result-"


@dataclass(frozen=True)
class AutoRunResult:
    """Parsed `## Auto Run Result` section. `present` is False when the spec has
    no such section yet (the session has not reached a terminal step)."""

    present: bool
    status: str  # lowercased Status: value, or "" when absent/unparsed
    detail: str  # the prose body after the heading, trimmed (human-readable)


def parse_auto_run_result(text: str) -> AutoRunResult:
    """Tolerantly extract the trailing `## Auto Run Result` section from a spec.

    Reads the LAST such heading (the finalize step appends; a re-derivation loop
    could in principle append more than one — the last is the live outcome) and
    pulls its `Status:` value plus the remaining prose as detail.
    """
    matches = list(AUTO_RUN_HEADING_RE.finditer(text))
    if not matches:
        return AutoRunResult(present=False, status="", detail="")
    body = text[matches[-1].end() :]
    # stop at the next top-level heading if the skill ever appends past it
    nxt = re.search(r"^##\s+", body, re.MULTILINE)
    if nxt:
        body = body[: nxt.start()]
    status_m = STATUS_LINE_RE.search(body)
    status = status_m.group(1).strip().lower() if status_m else ""
    return AutoRunResult(present=True, status=status, detail=body.strip())


@dataclass(frozen=True)
class SynthResult:
    """A synthesized result.json plus the cross-check signal. `result_json` is
    None when the spec has not terminated yet (no `## Auto Run Result` and no
    terminal frontmatter status), i.e. nothing to translate."""

    result_json: dict[str, Any] | None
    status_consistent: bool


def synthesize_result(
    spec_path: Path,
    *,
    story_key: str | None,
    dw_ids: list[str] | None = None,
) -> SynthResult:
    """Build the legacy result dict from the generic skill's on-disk spec.

    Returns ``SynthResult(None, True)`` when the spec carries no terminal signal
    yet (caller should keep waiting / treat the session as not-yet-complete).
    The dict's ``workflow`` is forged to ``auto-dev`` so verify.py's anti-wrong-
    skill guard passes; ``baseline_commit`` is taken from the skill's
    ``baseline_revision`` frontmatter (its name for the same thing). A blocked
    outcome is rendered as a single CRITICAL escalation so ``decide_dev`` PAUSEs
    unchanged — the generic skill has no severity tiers, and per the integration
    decision every block maps to PAUSE.
    """
    fm = read_frontmatter(spec_path)
    fm_status = str(fm.get("status", "")).strip().lower()
    arr = parse_auto_run_result(
        spec_path.read_text(encoding="utf-8") if spec_path.is_file() else ""
    )

    # Not terminal yet: no result section AND frontmatter not at a terminal state.
    if not arr.present and fm_status not in (DONE, BLOCKED):
        return SynthResult(result_json=None, status_consistent=True)

    # Authoritative status = frontmatter (read off disk). Prose status only
    # cross-checks it. When the prose is present and disagrees, flag it.
    status = fm_status or arr.status
    consistent = (not arr.present) or (not arr.status) or (arr.status == status)

    # The skill names the baseline `baseline_revision`; verify reads `baseline_commit`.
    baseline = str(fm.get("baseline_commit", fm.get("baseline_revision", ""))).strip()

    escalations: list[dict[str, Any]] = []
    if status == BLOCKED or arr.status == BLOCKED:
        detail = arr.detail or "generic dev session reported a blocked outcome"
        escalations.append({"type": "blocked", "severity": "CRITICAL", "detail": detail[:2000]})

    result: dict[str, Any] = {
        "workflow": DEV_WORKFLOW,
        "story_key": story_key,
        "spec_file": str(spec_path),
        "baseline_commit": baseline,
        "status": status,
        "escalations": escalations,
    }
    if dw_ids:
        result["dw_ids"] = list(dw_ids)
    # bmad-dev-auto (BMAD-METHOD PR #2505) self-reviews inline and, on a `done`
    # exit, sets `followup_review_recommended: true` when its review-driven
    # changes warrant an independent second-opinion pass. The skill never sets it
    # on a blocked exit, so only carry it through on `done`.
    if status == DONE:
        result["followup_review_recommended"] = bool(fm.get("followup_review_recommended", False))
    return SynthResult(result_json=result, status_consistent=consistent)


def find_result_artifact(impl_artifacts: Path, *, since_ns: int) -> Path | None:
    """Spec-watch fallback: locate THIS session's output artifact.

    This is how the GenericDevAdapter acquires its result: the generic skill
    writes no result.json, so on the session's Stop event we locate the spec it
    produced. The common case is a `spec-*.md` carrying a terminal `## Auto Run
    Result` section (appended by the skill's HALT on success AND blocked, when a
    spec exists). The skill's no-spec fallback — `bmad-dev-auto-result-*.md`,
    written when intent was too unclear to even create a spec — carries a
    terminal frontmatter `status:` but NO `## Auto Run Result` heading, so it is
    matched by filename instead. Scans `impl_artifacts` for the most-recently-
    modified qualifying markdown modified at/after `since_ns` (the session launch
    floor, so a stale prior artifact can't be mistaken for this run's output).
    Returns None when nothing qualifies.
    """
    if not impl_artifacts.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for path in impl_artifacts.glob("*.md"):
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        if mtime_ns < since_ns:
            continue
        # The no-spec fallback is recognized by name (it has no Auto Run Result
        # heading); every other artifact must carry the terminal section.
        if not path.name.startswith(FALLBACK_RESULT_PREFIX):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not AUTO_RUN_HEADING_RE.search(text):
                continue
        if best is None or mtime_ns > best[0]:
            best = (mtime_ns, path)
    return best[1] if best else None


def reset_spec_status(spec_path: Path, new_status: str) -> bool:
    """Rewrite the frontmatter ``status:`` value of a spec in place.

    Used by the generic-skill repair path: bmad-dev-auto self-finalizes a spec to
    ``done``/``in-review``, and its step-01 routes such a spec to "ingest as
    context, do not resume" — so to repair in place the orchestrator must re-open
    the spec by flipping its status back to ``in-progress``. A minimal line edit
    (not a YAML round-trip): preserves quote style and any trailing inline comment,
    and touches ONLY the first frontmatter block — never a ``Status:`` line in the
    prose body (e.g. the ``## Auto Run Result`` section). A present-but-empty status
    is filled, and a frontmatter block with NO ``status:`` line at all gets one
    inserted before the closing fence (the skill's template can leave it blank or
    absent). Returns True on a real change, False when the spec has no frontmatter
    block or is already at ``new_status``."""
    text = spec_path.read_text(encoding="utf-8")
    fm = _FRONTMATTER_RE.match(text)
    if not fm:
        return False
    head, body, tail = fm.group(1), fm.group(2), fm.group(3)
    changed = False

    def _repl(m: re.Match[str]) -> str:
        nonlocal changed
        if m.group("val") == new_status:
            return m.group(0)
        changed = True
        # Guarantee `key: value` spacing: a bare `status:` (no trailing space)
        # would otherwise fill to `status:done` — invalid YAML, the key is lost.
        pre = m.group("pre")
        if not pre.endswith((" ", "\t")):
            pre += " "
        # When the value was blank with a trailing inline comment, `rest` begins at
        # the `#`; abutting the value (`status: done# c`) makes the `#` part of the
        # scalar instead of a comment. Re-insert a separating space.
        rest = m.group("rest")
        if rest.startswith("#"):
            rest = " " + rest
        return f"{pre}{m.group('q')}{new_status}{m.group('q')}{rest}"

    if _FM_STATUS_RE.search(body):
        new_body = _FM_STATUS_RE.sub(_repl, body, count=1)
    else:
        # No status: line at all — insert one before the closing fence, matching
        # the block's line ending. `body` always ends with a newline (captured by
        # _FRONTMATTER_RE), so this lands on its own line.
        nl = "\r\n" if body.endswith("\r\n") else "\n"
        new_body = f"{body}status: {new_status}{nl}"
        changed = True
    if not changed:
        return False
    spec_path.write_text(head + new_body + tail + text[fm.end() :], encoding="utf-8")
    return True
