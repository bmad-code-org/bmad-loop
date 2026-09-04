"""Translate the generic `bmad-build-auto` skill's output into the orchestrator's
result.json contract.

Alex Verhovsky's upstream `bmad-build-auto` skill (BMAD-METHOD PR #2500) is a
decoupled autonomous-coding primitive: it writes NO result.json. Its outcome
lives in the spec it produced — `status:` in the frontmatter (the machine-
consumable signal) plus an appended `## Auto Run Result` prose section (intended
for an LLM deciding how to handle failure). This module is the thin Python shim
that turns that on-disk spec into the legacy result dict that verify.py /
escalation.py already consume, so the rest of the pipeline stays unchanged.

DOCTRINE — never let terminal prose override populated frontmatter status. The
frontmatter `status:` read straight off disk is authoritative whenever it is
present. A genuine `## Auto Run Result` marker has three narrow roles: its
status/detail route the blocked→PAUSE decision; its status is a compatibility
fallback that may populate the synthesized result when frontmatter is blank or
missing; and proof that the current session authored the marker supplies
attempt ownership for `park_asserted`. From that compatibility result, `done`
alone may be reconciled onto disk, `blocked` routes to PAUSE, and
`awaiting-operator` remains subject to the on-disk frontmatter/status gate.
Where the marker and populated frontmatter disagree we surface it
(`status_consistent=False`) so the caller can fail safe (treat a mismatch as a
retry rather than silently proceeding). Every real deterministic gate (git
baseline, worktree-changed, sprint advancement, dw_id match) still runs in
verify.py against actual on-disk state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import deferredwork
from .fences import fenced as _fenced
from .frontmatter import _edit_frontmatter_block, auto_dev_baseline_of, status_of
from .platform_util import atomic_write_bytes, atomic_write_bytes_confined
from .verify import DEV_WORKFLOW, operator_actions_of, read_frontmatter

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
# The story's agent-doable work is finished, but its acceptance criteria include
# external actions only a human can perform (#335). Terminal beside DONE and
# BLOCKED, and deliberately NOT rendered as an escalation: BLOCKED means the
# session could not proceed and the run must halt, while a park means it finished
# everything an agent could and the run should move on. Synthesizing a CRITICAL
# here would collapse that distinction into the pause channel — exactly the
# failure the state exists to avoid.
AWAITING_OPERATOR = "awaiting-operator"

# The status a plan-halt dispatch leaves behind: under folder+id dispatch a
# `Halt after planning.` directive makes the skill HALT right after the
# Ready-for-Development gate, at `ready-for-dev`. This is a *successful* terminal
# outcome for that leg (the plan is done, awaiting human review / implementation)
# — but ONLY when the caller asked for the halt. Without the directive the same
# status is a died-mid-flight non-terminal (it stays in RECONCILABLE_FROM), so
# the seam is gated on `plan_halt`, never on the status alone.
PLAN_HALT_STATUS = "ready-for-dev"

# Frontmatter statuses a half-finalized generic spec may be reconciled FROM when
# its prose terminal `## Auto Run Result` Status is `done`. Deliberately an
# allowlist: anything else (already-`done`, `blocked`, or an unknown custom token)
# is left untouched, so reconciliation can never override a status the skill set on
# purpose. `""` covers a blank or missing frontmatter `status:` — `status_of`
# normalizes a YAML-null value to `""` alongside the missing key (#358), and
# `reset_spec_status` fills/inserts the line in either case. `in-review` is included because step-04 sets
# it transiently at the start of a review pass; the skill self-finalizes to `done`,
# so a spec left AT `in-review` with a prose `done` result is a mid-review interrupt
# safe to reconcile forward. The intent-gap patch-restore re-drive (BMAD-METHOD
# #2564) deliberately re-arms the spec TO `in-review` before a session (so step-01
# routes straight to step-04 on the restored diff) — but that is the pre-session
# status the re-driven skill then advances past; it never LEAVES a spec at
# `in-review` as a terminal, so the reconcile allowlist semantics are unchanged.
RECONCILABLE_FROM = frozenset({"", "draft", "ready-for-dev", "in-progress", "in-review"})

# The leading `---\n …frontmatter… \n---` block, captured in three parts so the
# body can be rewritten while the fences stay byte-identical.
_FRONTMATTER_RE = re.compile(r"\A(---\r?\n)(.*?\r?\n)(---[ \t]*\r?\n)", re.DOTALL)
# A frontmatter `status:` line, preserving indent, the `: ` gap, optional quotes,
# and any trailing inline comment. Only the value token is rewritten. The value is
# `*` (not `+`) so a present-but-empty status (`status:` / `status: ""`) is matched
# and filled — a bmad-build-auto template can leave it blank.
_FM_STATUS_RE = re.compile(
    r"^(?P<pre>[ \t]*status[ \t]*:[ \t]*)(?P<q>['\"]?)(?P<val>[A-Za-z-]*)(?P=q)(?P<rest>.*)$",
    re.MULTILINE,
)

# The skill's no-spec fallback artifact (HALT when {spec_file} is unknown/missing):
# `{implementation_artifacts}/<skill>-result-<slug-or-timestamp>.md`. It carries a
# terminal frontmatter `status:` but no `## Auto Run Result` heading. BOTH eras are
# listed and matched unconditionally: the artifact is named after whichever skill
# wrote it, and a run can read an artifact left by the other era (a resume across an
# upstream upgrade, or a project mid-migration), so this must never be keyed on the
# skill name resolved on disk today.
FALLBACK_RESULT_PREFIXES = ("bmad-build-auto-result-", "bmad-dev-auto-result-")


@dataclass(frozen=True)
class AutoRunResult:
    """Parsed `## Auto Run Result` section. `present` is False when the spec has
    no such section yet (the session has not reached a terminal step)."""

    present: bool
    status: str  # lowercased Status: value, or "" when absent/unparsed
    detail: str  # the prose body after the heading, trimmed (human-readable)


def _section_headings(
    text: str, pattern: re.Pattern[str] = AUTO_RUN_HEADING_RE
) -> list[re.Match[str]]:
    """`pattern` matches that are real section headings. A heading quoted inside
    a fenced code block (a frozen intent showing an example of the terminal
    section, a log excerpt) is documentation, not structure — treating it as
    terminal would let such a spec read as a result artifact from the agent's
    first save (#52).

    `pattern` is a parameter rather than a hard-coded `AUTO_RUN_HEADING_RE`
    because the ``## Operator Confirmation`` section needs the identical fence
    reading for the identical reason, and a second copy of `_fenced`'s
    open-marker walk is exactly the kind of near-duplicate that drifts."""
    return [m for m in pattern.finditer(text) if not _fenced(text, m.start())]


def _next_heading_start(text: str, offset: int) -> int:
    """Offset of the first non-fenced same-level (`## `) heading at/after
    `offset`, or end-of-text — the shared section boundary. Fenced `## ` lines
    inside the section (quoted shell comments, log output) are content, not
    boundaries."""
    for nxt in re.finditer(r"^##\s", text, re.MULTILINE):
        if nxt.start() >= offset and not _fenced(text, nxt.start()):
            return nxt.start()
    return len(text)


def parse_auto_run_result(text: str) -> AutoRunResult:
    """Tolerantly extract the trailing `## Auto Run Result` section from a spec.

    Reads the LAST real (non-fenced) such heading (the finalize step appends; a
    re-derivation loop could in principle append more than one — the last is the
    live outcome) and pulls its `Status:` value plus the remaining prose as
    detail, spanning to the next real same-level heading.
    """
    matches = _section_headings(text)
    if not matches:
        return AutoRunResult(present=False, status="", detail="")
    last = matches[-1]
    body = text[last.end() : _next_heading_start(text, last.end())]
    status_m = STATUS_LINE_RE.search(body)
    status = status_m.group(1).strip().lower() if status_m else ""
    return AutoRunResult(present=True, status=status, detail=body.strip())


def auto_run_result_fingerprint(text: str) -> tuple[int, str]:
    """Identity of the current last real Auto Run Result marker.

    The count distinguishes a newly appended marker even when its text repeats
    the previous result verbatim; the digest distinguishes an in-place rewrite.
    Fenced examples remain invisible through the shared heading reader.
    """
    matches = _section_headings(text)
    if not matches:
        return (0, "")
    last = matches[-1]
    section = text[last.start() : _next_heading_start(text, last.end())]
    return (
        len(matches),
        hashlib.sha256(section.encode("utf-8"), usedforsecurity=False).hexdigest(),
    )


# ------------------------------------------------ deferred review findings
#
# Since BMAD-METHOD #2640 the dev primitive records findings triaged as `defer`
# in its spec's frontmatter. The parser stays content-keyed so an older skill
# (field absent) and a newer skill with no findings are indistinguishable.
DEFERRED_FIELD = "deferred"
_SUMMARY_LIMIT = 200
_EVIDENCE_LIMIT = 1000
_LOCATION_LIMIT = 200


@dataclass(frozen=True)
class DeferredFinding:
    """One well-formed deferred finding, normalized for a ledger entry."""

    summary: str
    evidence: str
    location: str
    severity: str
    fingerprint: str


def harvest_fingerprint(*parts: str) -> str:
    """Return a stable short identity over NUL-separated ledger values.

    A NUL inside a value is indistinguishable from the separator, so refuse it
    rather than assigning two distinct findings the same structural identity.
    The frontmatter parser reports such values as malformed before calling this
    helper; the guard also keeps direct callers from creating an ambiguous key.
    """
    if any("\0" in part for part in parts):
        raise ValueError("fingerprint parts must not contain NUL characters")
    return hashlib.sha1(
        "\0".join(parts).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]


def _flatten(value: Any, limit: int) -> str:
    """Collapse a YAML scalar to one line clamped at a word boundary.

    A cut landing mid-word backs up to the last join space so the ledger never
    records a truncated half-token; a cut ending exactly at a word end keeps the
    full cut, and a single unbroken token longer than the limit keeps the hard
    clamp. Fingerprints (``harvest_fingerprint(summary, location)``) therefore
    drift only for over-limit multi-word values -- accepted.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    if text[limit] == " ":
        return text[:limit]
    head, sep, _ = text[:limit].rpartition(" ")
    return head if sep else text[:limit]


def _is_yaml_scalar(value: Any) -> bool:
    """Whether PyYAML's loaded value came from a scalar-shaped node."""
    # str/bytes are Sequences in Python but scalar values in YAML. SafeLoader's
    # collection nodes otherwise become mappings, sequences, or sets.
    return isinstance(value, (str, bytes)) or not isinstance(value, (Mapping, Sequence, Set))


def _contains_nul(value: Any) -> bool:
    """Whether a YAML text scalar contains a NUL before normalization/clamping."""
    return (isinstance(value, str) and "\0" in value) or (
        isinstance(value, bytes) and b"\0" in value
    )


def parse_deferred_findings(fm: dict[str, Any]) -> tuple[list[DeferredFinding], list[str]]:
    """Parse a frontmatter ``deferred:`` list into findings and malformed notes.

    Missing, null, and empty lists mean no findings. Malformed items cost only
    themselves; well-formed siblings still parse. Text fields accept YAML
    scalars (numeric and boolean scalars are string-normalized), never
    collections or embedded NULs. This observation path never raises for the
    YAML shapes it accepts.
    """
    raw = fm.get(DEFERRED_FIELD)
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"`{DEFERRED_FIELD}:` is not a list (got {type(raw).__name__})"]

    findings: list[DeferredFinding] = []
    malformed: list[str] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            malformed.append(f"item {i}: not a mapping (got {type(item).__name__})")
            continue
        summary_raw = item.get("summary")
        if not _is_yaml_scalar(summary_raw):
            malformed.append(
                f"item {i}: `summary` is not a scalar (got {type(summary_raw).__name__})"
            )
            continue
        bad_optional = next(
            (field for field in ("evidence", "location") if not _is_yaml_scalar(item.get(field))),
            None,
        )
        if bad_optional is not None:
            value = item[bad_optional]
            malformed.append(
                f"item {i}: `{bad_optional}` is not a scalar (got {type(value).__name__})"
            )
            continue
        raw_text_values = {
            "summary": summary_raw,
            "evidence": item.get("evidence"),
            "location": item.get("location"),
        }
        bad_nul = next(
            (field for field, value in raw_text_values.items() if _contains_nul(value)), None
        )
        if bad_nul is not None:
            malformed.append(f"item {i}: `{bad_nul}` contains a NUL character")
            continue
        summary = _flatten(summary_raw, _SUMMARY_LIMIT)
        if not summary:
            malformed.append(f"item {i}: no usable `summary`")
            continue
        evidence = _flatten(item.get("evidence"), _EVIDENCE_LIMIT)
        location = _flatten(item.get("location"), _LOCATION_LIMIT)
        findings.append(
            DeferredFinding(
                summary=summary,
                evidence=evidence,
                location=location,
                severity=deferredwork.SEVERITY_ALIASES.get(
                    str(item.get("severity", "")).strip().lower(), ""
                ),
                fingerprint=harvest_fingerprint(summary, location),
            )
        )
    return findings, malformed


@dataclass(frozen=True)
class SynthResult:
    """A synthesized result.json plus the cross-check signal. `result_json` is
    None when the spec has not terminated yet (no `## Auto Run Result` and no
    terminal frontmatter status), i.e. nothing to translate."""

    result_json: dict[str, Any] | None
    status_consistent: bool


def _read_text_or_empty(path: Path) -> str:
    """Read a spec on the *read-back* path, degrading an unreadable file to "".

    An absent, binary/truncated, or unreadable spec carries no parseable result
    section, so it reads exactly like a spec that has not terminated yet — the
    caller then waits, nudges, or keeps its verdict. Never a crash: this runs on
    the observation path, where the orchestrator's job is to classify what it
    finds, not to trust it. (UnicodeDecodeError is a ValueError, not an OSError.)

    The *repair* path deliberately does the opposite — `reset_spec_status` and
    `strip_auto_run_result` let an unreadable spec raise, because silently
    skipping a rewrite leaves the spec in a state the caller believes it fixed.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def synthesize_result(
    spec_path: Path,
    *,
    story_key: str | None,
    dw_ids: list[str] | None = None,
    plan_halt: bool = False,
    park_marker_session_authored: bool = False,
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

    ``plan_halt`` is the stories-mode expected-terminal seam: on the first leg of
    a ``spec_checkpoint`` dispatch the caller sends ``Halt after planning.`` and
    the skill HALTs at ``ready-for-dev``. Passing ``plan_halt=True`` treats that
    status as a *successful* terminal — the returned dict carries
    ``status="ready-for-dev"``, no escalation, and a ``plan_halt=True`` marker so
    verify/engine expect a planned (not implemented) spec. Without ``plan_halt``
    the default is unchanged: ``ready-for-dev`` is non-terminal (died mid-flight)
    and returns ``SynthResult(None, True)``. This composes with the engine's
    ``_reconcile_generic_terminal_status`` — that path only reconciles a spec
    whose prose ``## Auto Run Result`` says ``done`` while the frontmatter lags,
    so a plan-halt ``ready-for-dev`` (no such prose) is never reconciled to
    ``done`` and this leg's success outcome is not clobbered.
    """
    try:
        fm = read_frontmatter(spec_path)
    except OSError:
        # Same degrade as `_read_text_or_empty` below, for the same reason: this is
        # the read-back path. An unreadable spec is not evidence a session
        # finished, so treat it exactly like one that has not terminated yet — the
        # caller keeps polling, and a fault that persists past the grace window
        # lands as a stall/timeout verdict that `_post_kill_reconcile` can still
        # rescue. Crashing here would take the whole run down for a spec the CLI
        # merely had open for writing.
        return SynthResult(result_json=None, status_consistent=True)
    # Through `status_of`, never a local `str(...)`: it normalizes a YAML-null
    # `status:` (the blank line a bmad-build-auto template legitimately leaves — see
    # `_FM_STATUS_RE`) to `""` alongside the missing key. A local stringification
    # made that `"none"`, which is truthy, so the prose fallback below never fired
    # and a blank-frontmatter/prose-`done` spec synthesized `status="none"` with
    # `status_consistent=False` — the exact shape `_post_kill_reconcile` documents
    # as rescuable (#369).
    fm_status = status_of(fm)
    arr = parse_auto_run_result(_read_text_or_empty(spec_path))

    terminal = (
        (DONE, BLOCKED, AWAITING_OPERATOR, PLAN_HALT_STATUS)
        if plan_halt
        else (DONE, BLOCKED, AWAITING_OPERATOR)
    )
    # Not terminal yet: no result section AND frontmatter not at a terminal state.
    if not arr.present and fm_status not in terminal:
        return SynthResult(result_json=None, status_consistent=True)

    # Authoritative status = frontmatter (read off disk). Prose status only
    # cross-checks it. When the prose is present and disagrees, flag it.
    status = fm_status or arr.status
    consistent = (not arr.present) or (not arr.status) or (arr.status == status)

    # One reader, shared with verify's baseline-match gate, so the two halves of
    # the contract cannot disagree about which key wins (#716).
    baseline = auto_dev_baseline_of(fm)

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
        # Attempt ownership comes only from the current session's terminal marker.
        # Frontmatter-only fallback is deliberately insufficient, and a marker
        # written later by the orchestrator's missing-marker repair cannot
        # retroactively authorize the session that omitted it.
        "park_asserted": (
            arr.present
            and arr.status == AWAITING_OPERATOR
            and ORCHESTRATOR_SYNTH_NOTE not in arr.detail
            and park_marker_session_authored
        ),
    }
    if dw_ids:
        result["dw_ids"] = list(dw_ids)
    # bmad-build-auto (BMAD-METHOD PR #2505) self-reviews inline and, on a `done`
    # exit, sets `followup_review_recommended: true` when its review-driven
    # changes warrant an independent second-opinion pass. The skill never sets it
    # on a blocked exit, so only carry it through on `done`.
    if status == DONE:
        result["followup_review_recommended"] = bool(fm.get("followup_review_recommended", False))
    # A park's obligations travel with its result so the engine never has to
    # re-read the spec to learn them. Folded on the awaiting-operator status
    # ONLY: an `operator_actions:` list left on a `done` or `blocked` spec is
    # not a park, and carrying it would let a story register obligations the
    # verify gates never held it to. Malformed shapes read as [] here and are
    # refused by verify's non-empty gate, which owns the retry + feedback.
    if status == AWAITING_OPERATOR:
        result["operator_actions"] = list(operator_actions_of(fm))
    # Mark the clean plan-halt success so verify/engine expect a planned spec
    # (status ready-for-dev, no implementation work). Never marked when a block
    # escalation is present — that routes to PAUSE, not a plan-review pause.
    if plan_halt and status == PLAN_HALT_STATUS and not escalations:
        result["plan_halt"] = True
    return SynthResult(result_json=result, status_consistent=consistent)


def find_result_artifact(impl_artifacts: Path, *, since_ns: int) -> Path | None:
    """Spec-watch fallback: locate THIS session's output artifact.

    This is how the GenericDevAdapter acquires its result: the generic skill
    writes no result.json, so on the session's Stop event we locate the spec it
    produced. The common case is a `spec-*.md` carrying a terminal `## Auto Run
    Result` section (appended by the skill's HALT on success AND blocked, when a
    spec exists). The skill's no-spec fallback — `bmad-build-auto-result-*.md`,
    or `bmad-dev-auto-result-*.md` pre-rename, written when intent was too
    unclear to even create a spec — carries a terminal frontmatter `status:` but
    NO `## Auto Run Result` heading, so it is matched by filename instead. Both
    spellings match: see `FALLBACK_RESULT_PREFIXES`. Scans `impl_artifacts` for
    the most-recently-modified qualifying markdown modified at/after `since_ns`
    (the session launch floor, so a stale prior artifact can't be mistaken for
    this run's output).
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
        if not is_result_artifact(path, since_ns=since_ns):
            continue
        if best is None or mtime_ns > best[0]:
            best = (mtime_ns, path)
    return best[1] if best else None


def is_result_artifact(path: Path, *, since_ns: int) -> bool:
    """Whether ONE file qualifies as a session's result artifact — the per-path
    predicate `find_result_artifact` applies to each glob hit, factored out so a
    caller that already KNOWS which spec the session owed (the orchestrator hands
    the review session its path in the prompt) can test that one file instead of
    scanning a directory shared with every concurrent run (#261).

    Qualifies when the file was modified at/after ``since_ns`` (the session-launch
    floor) AND either is the by-name no-spec fallback (`bmad-build-auto-result-*`,
    or `bmad-dev-auto-result-*` pre-rename — it carries no heading by design, and
    both spellings match unconditionally) or carries a real, non-fenced
    ``## Auto Run Result`` heading — a fence-quoted example must not qualify the
    spec (#52). An unreadable/undecodable candidate cannot be SHOWN to carry a
    terminal section, so it does not qualify (UnicodeDecodeError is a ValueError,
    so a bare ``except OSError`` let a torn mid-write spec crash the scan)."""
    try:
        if path.stat().st_mtime_ns < since_ns:
            return False
    except OSError:
        return False
    if path.name.startswith(FALLBACK_RESULT_PREFIXES):
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_section_headings(text))


def find_frontmatter_candidates(impl_artifacts: Path, *, since_ns: int) -> list[Path]:
    """Missing-marker fallback scan (#224): specs this session finalized to a
    terminal frontmatter ``status:`` WITHOUT appending the ``## Auto Run Result``
    marker `find_result_artifact` keys on. The skill's HALT instructions make the
    append unconditional, but compliance is intermittent — without this scan such
    a spec is invisible to the harvest and a finished story rides stall-nudges to
    timeout, then loses its work to a retry/DEFER cycle.

    A candidate must be modified at/after `since_ns` (same session-launch floor
    as the marker scan), carry ZERO real (non-fenced) marker headings, not be the
    no-spec fallback file (that one is already matched by name on the normal
    path), and have a terminal frontmatter ``status:`` (``done``, ``blocked``, or
    ``awaiting-operator``). Returns ALL matches, most-recent first — the caller refuses to guess between several
    and must apply its own stability fingerprint before synthesizing, because a
    terminal frontmatter under a live window is weaker evidence than the marker.
    """
    if not impl_artifacts.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in impl_artifacts.glob("*.md"):
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        if not is_frontmatter_candidate(path, since_ns=since_ns):
            continue
        found.append((mtime_ns, path))
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found]


def is_frontmatter_candidate(path: Path, *, since_ns: int) -> bool:
    """Whether ONE file qualifies for the missing-marker fallback — the per-path
    predicate `find_frontmatter_candidates` applies to each glob hit, factored out
    for the same reason as `is_result_artifact`: a caller holding the spec path the
    session actually owed can test that file alone instead of a shared directory
    (#261).

    Qualifies when the file is modified at/after ``since_ns``, is NOT the no-spec
    fallback (already matched by name on the marker path), carries ZERO real
    (non-fenced) marker headings — one would put it in `find_result_artifact`'s
    territory — and has a terminal frontmatter ``status:`` (``done``,
    ``blocked``, or ``awaiting-operator``). Any
    unreadable/undecodable read degrades to False, never an exception."""
    if path.name.startswith(FALLBACK_RESULT_PREFIXES):
        return False
    try:
        if path.stat().st_mtime_ns < since_ns:
            return False
    except OSError:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if _section_headings(text):
        return False  # carries a real marker — the normal scan's territory
    try:
        fm = read_frontmatter(path)
    except OSError:
        return False
    return status_of(fm) in (DONE, BLOCKED, AWAITING_OPERATOR)


def _atomic_write_spec(spec_path: Path, text: str, *, confine_root: Path) -> None:
    """Rewrite ``spec_path`` with ``text`` via a same-directory temp file + atomic
    rename, so an interrupted / short / disk-full write can never truncate the
    canonical spec — a failed repair must lose no work (fault injection on the old
    truncating ``write_text`` reduced a 46-byte spec to 12). Bytes are written
    verbatim (``atomic_write_bytes``, not the text sibling): every caller here has
    already captured and preserved the file's own line endings, and text mode's
    ``newline=None`` default would re-translate ``\\n``→``\\r\\n`` on Windows. The
    temp ends in ``.tmp`` (not ``.md``), so the ``*.md`` artifact scans never see
    it. On any failure the temp file is removed and the error re-raised — the
    callers impose best-effort, the writer never swallows.

    Now a thin name over `platform_util`'s atomic byte writers (#379) rather than
    a hand-rolled temp: same shape, plus an fsync before the replace and a temp
    name unique per write instead of a fixed ``<spec>.md.tmp`` two concurrent
    writers would collide on.

    Which writer is the spec-writer chokepoint rule (#593), stated once in
    `frontmatter.set_frontmatter_status` and implemented identically here and in
    `verify.set_frontmatter_field`: a spec under ``confine_root`` is written
    through the anchored, component-walked helper; a spec in an artifacts folder
    configured outside the checkout keeps the plain ``follow_symlinks=False``
    write. That no-follow keeps the name-replacing semantics the `atomic_replace`
    here always had — the spec path comes from a session-driven scan, so writing
    THROUGH a planted link would hand that session a host-side write wherever it
    chose — and the confined arm needs no flag, because it never follows
    anything. ``require_writable_target=True`` on both arms restores the
    `PermissionError` an operator's read-only spec used to earn (#597).

    ``confine_root`` is a REQUIRED keyword here and on all four public writers
    that reach this one, so a caller that has not decided which checkout the spec
    belongs to is a pyright error rather than an unconfined write. The two
    `frontmatter`-side writers of these same files land on the identical pair of
    calls (#379); this wrapper stays for its callers' ``str``-in signature and
    this docstring."""
    payload = text.encode("utf-8")
    if spec_path.is_relative_to(confine_root):
        atomic_write_bytes_confined(
            spec_path, payload, confine_root=confine_root, require_writable_target=True
        )
    else:
        atomic_write_bytes(spec_path, payload, follow_symlinks=False, require_writable_target=True)


def _render_status_line(line: str, m: re.Match[str], value: str) -> str:
    """`reset_spec_status`'s replacement for a matched ``status:`` line: the
    value moves, its quote style and any trailing inline comment stay.

    Deliberately different from `frontmatter._replace_value`, which carries the
    comment through but drops the quotes — that writer's callers read the result
    back as a bare ``status: done``, this one's tests pin the quote style.
    Sharing the VERIFICATION is the point; the formatting each already had
    right."""
    # Guarantee `key: value` spacing: a bare `status:` (no trailing space) would
    # otherwise fill to `status:done` — invalid YAML, the key is lost.
    pre = m.group("pre")
    if not pre.endswith((" ", "\t")):
        pre += " "
    # When the value was blank with a trailing inline comment, `rest` begins at
    # the `#`; abutting the value (`status: done# c`) makes the `#` part of the
    # scalar instead of a comment. Re-insert a separating space.
    rest = m.group("rest")
    if rest.startswith("#"):
        rest = " " + rest
    q = m.group("q")
    return f"{pre}{q}{value}{q}{rest}" + ("\n" if line.endswith("\n") else "")


def reset_spec_status(spec_path: Path, new_status: str, *, confine_root: Path) -> bool:
    """Rewrite the frontmatter ``status:`` value of a spec in place.

    Used by the generic-skill repair path: bmad-build-auto self-finalizes a spec to
    ``done``/``in-review``, and its step-01 routes such a spec to "ingest as
    context, do not resume" — so to repair in place the orchestrator must re-open
    the spec by flipping its status back to ``in-progress``. A minimal line edit
    (not a YAML round-trip): preserves quote style and any trailing inline comment,
    and touches ONLY the first frontmatter block — never a ``Status:`` line in the
    prose body (e.g. the ``## Auto Run Result`` section). A present-but-empty status
    is filled, and a frontmatter block with NO ``status:`` line at all gets one
    inserted before the closing fence (the skill's template can leave it blank or
    absent).

    Returns True on a real change, False when the spec is absent, has no
    frontmatter block, or is already at ``new_status``. Raises
    `FrontmatterWriteError` when the reader can see a status the line edit cannot
    safely move: `_FM_STATUS_RE` reads only a ``[A-Za-z-]*`` value, so it used to
    fill ``status: |`` to ``status: done|`` and ``status: 123`` to
    ``status: done123`` — silently, with a True return — and it rewrote a nested
    ``meta.status:`` in preference to the real key. The shared
    `frontmatter._edit_frontmatter_block` re-parses each trial edit before keeping
    it, so those become a loud refusal instead of a corrupted spec. Only
    `engine._reconcile_generic_terminal_status` reads the return; the raise is what
    reaches the other three call sites, which treat it as a repair write."""
    if not spec_path.is_file():
        return False
    # Raw read (not read_text), like the append/strip siblings: universal-newline
    # translation would hand this writer an all-LF copy of a CRLF spec and
    # `_atomic_write_spec` would then persist it, relaying every line ending in
    # the file from a repair contracted to move one value.
    #
    # Reading the bytes as authored makes one asymmetry with
    # `frontmatter.set_frontmatter_status` observable, and it is pinned rather
    # than fixed: that writer splits with `splitlines`, which treats a bare `\r`
    # as a line break, so a CR-only spec rewrites fine there. `_FRONTMATTER_RE`
    # and `_FM_STATUS_RE` are line-oriented on `\r?\n`, so a CR-only spec finds
    # no frontmatter block here and returns False. Widening them to `\r` would
    # change what counts as a line for every reader keyed on these patterns
    # (`^` in MULTILINE still would not follow), which is a larger contract than
    # this fix owns — no BMAD tool authors CR-only files.
    text = spec_path.read_bytes().decode("utf-8")
    fm = _FRONTMATTER_RE.match(text)
    if not fm:
        return False
    head, body, tail = fm.group(1), fm.group(2), fm.group(3)
    new_body = _edit_frontmatter_block(
        body,
        "status",
        new_status,
        pattern=_FM_STATUS_RE,
        render=_render_status_line,
        insert=True,
    )
    if new_body is None:
        return False
    _atomic_write_spec(
        spec_path, head + new_body + tail + text[fm.end() :], confine_root=confine_root
    )
    return True


def strip_auto_run_result(spec_path: Path, *, confine_root: Path) -> bool:
    """Remove every ``## Auto Run Result`` section from a spec, in place.

    Companion to `reset_spec_status` on the re-drive path: re-opening a spec by
    flipping only its frontmatter would leave the stale terminal section behind,
    and `find_result_artifact` keys on that heading — the re-driven session's
    very first save of the spec would then qualify as a terminal result. Each
    section spans its heading to the next same-level heading (the shared
    `parse_auto_run_result` boundary) or end-of-file; headings quoted inside
    fenced code blocks are ignored on both ends. Returns True when a section was
    removed, False when the spec is absent or no section was present.

    Only an absent spec is guarded (a clean no-op, mirroring
    `verify.set_frontmatter_status`); a present-but-unreadable spec or a failing
    write is left to raise. Silently skipping the strip after the caller has
    already flipped the frontmatter status would leave the re-opened spec carrying
    its stale terminal section — the exact state that makes the re-driven session's
    first save read as a result — so that failure must surface, not be swallowed."""
    if not spec_path.is_file():
        return False
    # Raw read (not read_text) for the append's reason, in reverse: the append
    # preserves the spec's endings, so a strip that read through universal
    # newlines would not round-trip its own sibling's output — it would return a
    # CRLF spec relaid to LF. An undecodable spec still raises UnicodeDecodeError
    # (repair-write doctrine). A CR-only spec is invisible to
    # `AUTO_RUN_HEADING_RE`'s MULTILINE `^` and no-ops — the same pinned
    # asymmetry `reset_spec_status` documents above.
    text = spec_path.read_bytes().decode("utf-8")
    matches = _section_headings(text)
    if not matches:
        return False
    kept: list[str] = []
    pos = 0
    for m in matches:
        if m.start() < pos:
            continue  # heading inside a section already being removed
        kept.append(text[pos : m.start()])
        pos = _next_heading_start(text, m.end())
    kept.append(text[pos:])
    _atomic_write_spec(spec_path, "".join(kept), confine_root=confine_root)
    return True


def reset_spec_for_replan(spec_path: Path, *, confine_root: Path) -> bool:
    """Reset a spec to ``draft`` and strip its stale result transactionally.

    The TUI exposes those two writes as one operator action. Capture the exact
    preimage before the status write and restore it through the same confined
    atomic writer if either stage faults after changing (or removing) the file,
    so a failed replan never publishes only its first half. A reset that makes no
    change is a refusal, not permission to strip the result independently.

    Rollback deliberately catches ``BaseException`` so an interrupt between the
    two writes cannot leave a partial replan. A fault that leaves the preimage
    untouched does not rewrite it. If the restore itself fails, that failure
    escapes; otherwise the original stage failure is re-raised.
    """
    original = spec_path.read_bytes()
    try:
        reset = reset_spec_status(spec_path, "draft", confine_root=confine_root)
        if not reset:
            if not spec_path.is_file():
                raise FileNotFoundError(f"replan spec vanished during status reset: {spec_path}")
            return False
        stripped = strip_auto_run_result(spec_path, confine_root=confine_root)
        if not stripped and not spec_path.is_file():
            raise FileNotFoundError(f"replan spec vanished during result strip: {spec_path}")
    except BaseException:
        try:
            unchanged = spec_path.read_bytes() == original
        except OSError:
            unchanged = False
        if not unchanged:
            _atomic_write_spec(spec_path, original.decode("utf-8"), confine_root=confine_root)
        raise
    return True


# Provenance stamped into a synthesized `## Auto Run Result` section so a human
# (or a later re-derivation) can tell an orchestrator-repaired marker from one
# the skill wrote itself. Single line (no internal newlines) so the writer's
# line-ending detection governs every break in the appended block.
ORCHESTRATOR_SYNTH_NOTE = (
    "_Appended by the bmad-loop orchestrator (missing-marker repair, #224): the "
    "session finalized this spec's frontmatter without its `## Auto Run Result` "
    "marker, so the orchestrator synthesized the result from the frontmatter and "
    "appended this section._"
)


# The heading `bmad-loop confirm` writes to record a human's out-of-band sign-off,
# and the pattern that reads it back. Both spellings live here because the section
# is now STRUCTURE, not just prose: `confirm` resumes an interrupted confirmation
# by detecting this heading, so the writer's literal and the reader's pattern must
# agree or a resumed story is confirmed twice — or never. The writer interpolates
# the constant; a round-trip test pins the two against each other.
OPERATOR_CONFIRM_HEADING = "## Operator Confirmation"
OPERATOR_CONFIRM_HEADING_RE = re.compile(r"^##\s+Operator Confirmation\s*$", re.MULTILINE)

# Provenance for the operator-confirmation section, mirroring
# ORCHESTRATOR_SYNTH_NOTE: says who wrote the section and on whose word, so a
# reader never mistakes an out-of-band human sign-off for something a dev
# session concluded on its own. Single line, for the same reason as its sibling.
OPERATOR_CONFIRM_NOTE = (
    "_Appended by the bmad-loop orchestrator (`bmad-loop confirm`, #335): a human "
    "confirmed these external actions out of band, and the story was advanced from "
    "`awaiting-operator` to `done`._"
)


def append_auto_run_result(
    spec_path: Path, status: str, *, confine_root: Path, detail: str = ""
) -> bool:
    """Append a synthesized ``## Auto Run Result`` marker section — the inverse of
    `strip_auto_run_result`.

    The missing-marker fallback (#224) synthesizes a session's result from a spec
    that carries a terminal frontmatter ``status:`` but never got the marker the
    skill owed; this writer brings that spec back into contract. Before the append
    a marker-less terminal spec sits in `find_frontmatter_candidates`' territory
    (it requires ZERO real headings); after it the spec carries exactly one real
    ``## Auto Run Result`` heading and moves into `find_result_artifact`'s
    (requires >= 1), so a later re-read is harvested on the normal marker path
    instead of the fallback scan, and the next review launch strips it exactly
    like a skill-written marker (#160).

    Returns False when the spec is absent, or when a REAL (non-fenced)
    ``## Auto Run Result`` heading is already present — idempotence, and the same
    #52 symmetry the strip honors: a fence-quoted heading (a frozen intent's
    example) is documentation, not a marker, so it does NOT block the append. A
    present-but-unreadable spec RAISES rather than no-ops (the repair-write
    doctrine `strip_auto_run_result` and `reset_spec_status` share — the CALLER
    imposes best-effort); silently skipping would leave the spec in a state the
    caller believes it repaired.

    Newline handling follows `reset_spec_status`'s intent: the file's line ending
    is detected (CRLF vs LF) and reused for the appended block, and a missing
    trailing newline is added so the heading can never glue onto the last body line
    (``...body## Auto Run Result``). The spec is read as raw bytes rather than via
    `read_text`, whose universal-newline translation would both hide a CRLF file's
    ending and silently rewrite its whole body to LF — an in-place repair must not
    mutate line endings it did not author. The section is ``## Auto Run Result`` /
    blank / ``Status: <status>`` / blank / the provenance note (plus an optional
    detail paragraph). ``status`` is normalized lowercase and MUST be the spec's
    own frontmatter ``status`` — the caller passes exactly that — so
    `synthesize_result`'s ``consistent`` cross-check holds on every later re-read."""
    if not spec_path.is_file():
        return False
    # Raw read (not read_text): preserve the file's exact line endings, and let an
    # undecodable spec raise UnicodeDecodeError like the strip's read (repair-write
    # doctrine — the caller imposes best-effort, never the writer).
    text = spec_path.read_bytes().decode("utf-8")
    if _section_headings(text):
        return False  # a real marker is already present — idempotent
    status = status.strip().lower()
    nl = "\r\n" if "\r\n" in text else "\n"
    # Ensure the heading lands after a newline the scan can recognize. A file
    # already ending in "\n" (LF, or the "\n" of a CRLF) is left byte-for-byte
    # intact, so a strip of this exact append round-trips. A file ending in a BARE
    # "\r" is the trap: it reads as "already terminated", but AUTO_RUN_HEADING_RE's
    # "^" (MULTILINE) only matches after "\n" — a heading glued directly after "\r"
    # is invisible to the scan. Complete such a bare CR to CRLF (preserving the
    # authored CR) so the heading begins on a recognized line boundary.
    if text.endswith("\r"):
        text += "\n"
    elif text and not text.endswith("\n"):
        text += nl
    section = f"## Auto Run Result{nl}{nl}Status: {status}{nl}{nl}{ORCHESTRATOR_SYNTH_NOTE}{nl}"
    if detail:
        section += f"{nl}{detail.strip()}{nl}"
    _atomic_write_spec(spec_path, text + section, confine_root=confine_root)
    return True


def append_operator_confirmation(
    spec_path: Path, actions: Sequence[str], *, date: str, confine_root: Path
) -> bool:
    """Append the ``## Operator Confirmation`` audit section `bmad-loop confirm`
    writes when a human signs off a parked story (#335).

    This section is the whole audit trail for the part of a story that happened
    OUTSIDE the repository. The commit shows what the agent did; nothing in git
    can show that someone bought the domain or published the DNS record, so the
    spec has to say it, next to the ``operator_actions:`` it is answering. The
    actions are restated rather than referenced because the frontmatter list is
    the *claim* and this is the *acknowledgment* — a later reader comparing them
    can see whether the spec was edited between the park and the confirmation.

    Unlike `append_auto_run_result` this does NOT no-op on a heading that is
    already there. A repeated confirmation is a real event (a spec reverted to
    the park status and confirmed again), and dropping the second record would
    make the audit trail claim there was only ever one. Sections accumulate,
    newest last.

    Repair-write doctrine, as the neighbouring writers: an absent spec returns
    False (the caller decides whether that is fatal); a present-but-unreadable
    spec, or a failing write, RAISES — `confirm` is about to move the board to
    `done`, and it must not do that believing it recorded something it did not.

    Line endings are handled exactly as in `append_auto_run_result`: raw byte
    read, detected CRLF/LF reused for every break, bare-CR completed so the
    heading starts on a recognized line boundary."""
    if not spec_path.is_file():
        return False
    text = spec_path.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    if text.endswith("\r"):
        text += "\n"
    elif text and not text.endswith("\n"):
        text += nl
    # A blank line before the heading, not merely a terminated last line: this
    # section is written to be READ, and every markdown renderer (and linter)
    # wants a heading separated from the paragraph above it.
    if text and not text.endswith(nl * 2):
        text += nl
    # One line per action, in the order they were declared and acknowledged.
    items = "".join(f"- {a}{nl}" for a in actions)
    section = (
        f"{OPERATOR_CONFIRM_HEADING}{nl}{nl}"
        f"Confirmed {date}: the external actions this story owed were carried out.{nl}{nl}"
        f"{items}{nl}{OPERATOR_CONFIRM_NOTE}{nl}"
    )
    _atomic_write_spec(spec_path, text + section, confine_root=confine_root)
    return True


def has_operator_confirmation(spec_path: Path) -> bool:
    """Whether a spec already carries a REAL ``## Operator Confirmation`` section
    — the on-disk acknowledgment `bmad-loop confirm` reads to tell an interrupted
    confirmation from a fresh one (#335).

    Fence-aware for the same reason `_section_headings` is (#52), and the stakes
    here are higher than on the auto-run path: a frozen intent that shows an
    example ``## Operator Confirmation`` block inside a code fence must not let
    `confirm` conclude a human already signed off and skip straight to advancing
    the board. The section on disk IS the acknowledgment, so a quoted one would
    finish a story nobody confirmed.

    Reads on the *observation* path and degrades accordingly (`_read_text_or_empty`):
    an absent or unreadable spec cannot be SHOWN to carry an acknowledgment, so it
    reads as False and the caller takes the ordinary path, which does its own
    reading and reports the real fault."""
    return bool(_section_headings(_read_text_or_empty(spec_path), OPERATOR_CONFIRM_HEADING_RE))
