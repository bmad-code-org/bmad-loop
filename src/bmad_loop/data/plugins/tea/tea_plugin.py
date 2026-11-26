"""In-process brain for the bundled Test Architect Enterprise (TEA) plugin.

The plugin's behaviour is mostly declarative — six ``[workflows.*]`` inject TEA
sessions at ``post_dev_phase`` / ``pre_commit_gate``, and the generic
workflow-overlay convention (``<name>_enabled`` / ``<name>_blocking``) turns the
per-step settings into enable/blocking switches with no code. This module owns
the two things that need logic:

  * the **readiness gate** (``validate``) — fail the run fast, with an actionable
    remediation message, when ``require_tea`` is on but the project has no TEA
    install, so an operator who enabled the plugin without installing TEA learns
    immediately instead of watching every injected session flail.
  * **blocking-gate enforcement** (``on_pre_commit``) — when an operator marks a
    gate step blocking (``trace_blocking`` / ``nfr_blocking`` / ``review_blocking``
    = true), parse that gate's latest TEA artifact at commit time and, on a
    FAIL/CONCERNS verdict, escalate the unit (``ctx.veto("pause", …)``) instead of
    letting it land. ``pre_commit`` is the first vetoable stage *after* the
    ``pre_commit_gate`` workflows have written their artifacts, and both fire
    unconditionally on every path into a commit — review-converged, skip-review
    (``review.trigger="recommended"`` with no follow-up recommended), and the
    review-budget rescue — so the gates evaluate the exact tree about to commit.

Enforcement is **fail-open by design**: if a gate has no blocking flag, no
artifact, or an artifact that can't be parsed into a known verdict, the commit is
never blocked. An unknown format must never wrongly stop a commit — only a
confidently-parsed FAIL/CONCERNS on an operator-marked-blocking gate escalates.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bmad_loop.bmadconfig import BmadConfigError, read_module_config
from bmad_loop.plugins.model import Plugin, PluginError

if TYPE_CHECKING:
    from bmad_loop.plugins.context import HookContext

# The gate steps that expose a ``*_blocking`` flag (the pre_commit_gate steps
# an operator is likely to enforce). Generation steps (td/atdd/automate) stay
# advisory by design and are deliberately absent — they are never gate-enforced.
GATE_STEPS = ("trace", "nfr", "review")

# Verdicts that escalate a blocking gate. PASS / WAIVED (an explicit human
# approval to proceed) / an unknown-or-not-evaluated verdict never block.
BLOCKING_VERDICTS = frozenset({"FAIL", "CONCERNS"})
NOT_EVALUATED = "NOT_EVALUATED"
DEFAULT_ARTIFACTS = "{project-root}/_bmad-output/test-artifacts"
LEGACY_CONFIG = Path("_bmad/tea/config.yaml")
GATE_FOLDERS = {"trace": "trace", "nfr": "nfr", "review": "test-review"}

# Where each gate writes its decision, in probe order (first parseable wins).
#   * "json"     — a TEA machine-readable file; read ``gate_status`` / ``decision``
#   * "md_trace" — the trace report's ``## Gate Decision: <verdict>`` heading
#   * "md_nfr"   — the NFR report's ``**Overall Status:** / **Gate Status:**`` line
#   * "md_review"— the test-review report's ``**Recommendation**:`` line
# Paths are relative to TEA's configured ``test_artifacts`` directory. trace
# emits JSON for exactly this purpose, so it is preferred over its markdown.
GATE_ARTIFACTS: dict[str, tuple[tuple[str, str], ...]] = {
    "trace": (
        ("json", "gate-decision.json"),
        ("json", "e2e-trace-summary.json"),
        ("md_trace", "traceability-matrix.md"),
    ),
    "nfr": (("md_nfr", "nfr-assessment.md"),),
    "review": (("md_review", "test-review.md"),),
}

# Labels that precede a concrete verdict on a markdown gate line, per artifact.
_MD_LABELS: dict[str, tuple[str, ...]] = {
    "md_trace": ("gate decision",),
    "md_nfr": ("gate status", "overall status", "overall_status"),
}


def _verdict_token(text: str) -> str | None:
    """The canonical gate verdict named anywhere in ``text``, or None. CONCERNS /
    FAIL are checked before PASS so a value line that pairs them (rare) errs toward
    blocking; NOT_EVALUATED and anything unrecognized resolve to None (fail-open)."""
    up = text.upper()
    for token in ("CONCERNS", "FAIL", "WAIVED", "PASS"):
        if token in up:
            return token
    return None


def _review_token(line: str) -> str | None:
    """Map the test-review report's recommendation vocabulary onto a gate verdict:
    Block -> FAIL, Request Changes -> CONCERNS, Approve[/with Comments] -> PASS."""
    low = line.lower()
    if "block" in low:
        return "FAIL"
    if "request change" in low:
        return "CONCERNS"
    if "approve" in low:
        return "PASS"
    return None


def _scan_markdown(text: str, labels: tuple[str, ...], normalize) -> str | None:
    """First *concrete* labeled line that yields a verdict. Lines carrying ``{}``
    placeholders or ``|`` table syntax are skipped — they are template scaffolding,
    not a generated decision — so a real artifact's value line is what's read."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "{" in line or "}" in line or "|" in line:
            continue
        if any(label in line.lower() for label in labels):
            verdict = normalize(line)
            if verdict is not None:
                return verdict
    return None


def _run_key(ctx: HookContext) -> str | None:
    """Map Loop's bound unit to TEA's story identity."""
    if ctx.sweep_bundle:
        return None
    ticket = ctx.ticket
    if ticket is None:
        story_key = Path(ctx.spec_file).stem if ctx.spec_file else ctx.story_key
        if not ctx.spec_file and story_key:
            story_key = story_key.replace(".", "-")
    elif ticket.epic_slug == "backlog":
        story_key = f"backlog--{Path(ticket.ref).stem}"
    elif ctx.initiative and ticket.id:
        initiative = ctx.initiative.removeprefix("initiative-")
        if ticket.epic_slug.startswith("epic-"):
            epic = f"e{ticket.epic_id}" if ticket.epic_id else ticket.epic_slug
            story_key = f"{initiative}--{epic}-s{ticket.id}"
        else:
            story_key = f"{initiative}--s{ticket.id}"
    else:
        return None
    if not story_key or "/" in story_key or "\\" in story_key:
        return None
    return f"story-{story_key}"


def _scoped_verdict(text: str) -> str | None:
    """Read a current verdict at the start of its value, including no decision."""
    match = re.match(r"(NOT_EVALUATED|CONCERNS|FAIL|WAIVED|PASS)\b", text.strip(" *`").upper())
    return match[1] if match else None


def _scoped_markdown(text: str, kind: str) -> str | None:
    labels = {
        "md_trace": ("gate decision",),
        "md_nfr": ("gate status", "overall status"),
        "md_review": ("recommendation",),
    }
    for label in labels[kind]:
        pattern = rf"^\s*(?:#+\s+)?\**{label}\**\s*:\s*\**([^\n]+)"
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            value = match[1].strip()
            if value.startswith("{") or "|" in value:
                continue
            if kind == "md_review":
                value = value.lower().strip(" *`")
                for recommendation, verdict in (
                    ("request changes", "CONCERNS"),
                    ("approve", "PASS"),
                    ("block", "FAIL"),
                ):
                    if re.match(rf"{recommendation}\b", value):
                        return verdict
            else:
                verdict = _scoped_verdict(value)
                if verdict is not None:
                    return verdict
    return None


class TeaPlugin(Plugin):
    """Trust-gated in-process plugin (loads only when "tea" is in
    ``[plugins] enabled``). Owns the TEA readiness gate + blocking-gate
    enforcement at commit."""

    # a readiness failure is a deliberate config rejection raised from validate()
    # (fails the run fast), not an isolated hook crash — so fail_closed stays off.
    # Enforcement (on_pre_commit) is fail-open too: a parse failure must never
    # wrongly block, so a raised handler isolating out is the safe outcome.
    fail_closed = False

    # ----------------------------------------------------------- validation

    def validate(self, policy: Any) -> None:
        self.validate_project(policy, Path.cwd())

    def validate_project(self, policy: Any, project: Path) -> None:
        """Accept current central TEA setup or a legacy module install."""
        if not self._require_tea():
            return
        try:
            modern = read_module_config(project, "tea")
            if modern is not None:
                self._current_artifacts_dir(project, modern)
                return
        except BmadConfigError as error:
            raise PluginError(f"plugin 'tea': {error}") from error
        if not (project / LEGACY_CONFIG).is_file():
            raise PluginError(
                "plugin 'tea': the Test Architect Enterprise (TEA) module is not "
                f"installed in this project (missing [modules.tea] in {project / '_bmad/config.toml'} "
                f"and legacy {project / LEGACY_CONFIG}). Install the TEA skills and run "
                "`bmad setup tea`, or set require_tea = false under [plugins.tea] "
                "in .bmad-loop/policy.toml."
            )

    # --------------------------------------------------------- enforcement

    def on_pre_commit(self, ctx: "HookContext") -> None:
        """Blocking-gate enforcement. For each gate an operator marked blocking,
        parse its latest TEA artifact under the configured ``test_artifacts`` dir
        and, on a FAIL/CONCERNS verdict, ``pause``-veto so the unit escalates for
        human review instead of committing.

        Fail-open throughout: a gate with no blocking flag, no artifact, or an
        unparseable artifact contributes nothing. Only confidently-parsed
        blocking verdicts escalate. ``pre_commit`` honors only a ``pause`` veto
        (a COMMITTING task has no legal move to DEFERRED), which is the right
        "blocking gate failed" semantic — escalate-for-human."""
        gates = [
            gate
            for gate in GATE_STEPS
            if self.settings.get(f"{gate}_enabled", True) and self.settings.get(f"{gate}_blocking")
        ]
        if not gates:
            return  # nothing flagged blocking -> advisory only, no artifact read

        # Resolve the tree TEA wrote into: the unit's worktree (where the
        # pre_commit_gate sessions just ran), falling back to the repo root.
        # Under isolation = none the two coincide.
        root = ctx.project_dir or ctx.worktree or ctx.repo_root
        if not root:
            return  # fail-open: no tree to inspect
        project = Path(root)
        modern = read_module_config(project, "tea")
        artifacts_dir = (
            self._current_artifacts_dir(project, modern)
            if modern is not None
            else self._legacy_artifacts_dir(project)
        )
        run_key = _run_key(ctx)

        verdicts: dict[str, str] = {}
        failed: list[tuple[str, str]] = []
        for gate in gates:
            verdict = self._gate_verdict(gate, artifacts_dir, run_key, legacy=modern is None)
            if verdict is None or verdict == NOT_EVALUATED:
                continue  # fail-open: missing / unparseable / not-evaluated
            verdicts[gate] = verdict
            if verdict in BLOCKING_VERDICTS:
                failed.append((gate, verdict))

        if verdicts:
            # breadcrumb for the operator / journal: what each gate decided.
            ctx.shared.setdefault("tea_gates", {}).update(verdicts)
        if failed:
            detail = ", ".join(f"{gate}={verdict}" for gate, verdict in failed)
            ctx.veto(
                "pause",
                f"TEA blocking gate(s) failed for {ctx.story_key or 'unit'}: {detail}. "
                f"Review the gate artifacts under {artifacts_dir}, resolve the "
                "findings, then resume the run.",
            )

    # -------------------------------------------------------------- helpers

    def _require_tea(self) -> bool:
        return bool(self.settings.get("require_tea", True))

    def _current_artifacts_dir(self, root: Path, config: dict[str, object]) -> Path:
        value = config.get("test_artifacts", DEFAULT_ARTIFACTS)
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise BmadConfigError("`modules.tea.test_artifacts` must be a non-empty path")
        return root / value.replace("{project-root}", str(root))

    def _legacy_artifacts_dir(self, root: Path) -> Path:
        """Preserve classic YAML path and token handling."""
        value = DEFAULT_ARTIFACTS
        try:
            for raw in (root / LEGACY_CONFIG).read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("test_artifacts:"):
                    value = line.split(":", 1)[1].strip().strip("\"'")
                    break
        except OSError:
            pass
        value = value.replace("{project-root}", str(root)).replace(
            "{output_folder}", str(root / "_bmad-output")
        )
        return root / value

    def _gate_verdict(
        self, gate: str, artifacts_dir: Path, run_key: str | None, *, legacy: bool
    ) -> str | None:
        candidates = GATE_ARTIFACTS[gate]
        if run_key is not None:
            folder = artifacts_dir / GATE_FOLDERS[gate]
            scoped = [
                (kind, folder / f"{Path(name).stem}-{run_key}{Path(name).suffix}")
                for kind, name in candidates
            ]
            present = [(kind, path) for kind, path in scoped if path.is_file()]
            if present:
                return self._first_verdict(present, scoped=True)
        if legacy:
            flat = [(kind, artifacts_dir / name) for kind, name in candidates]
            return self._first_verdict(flat, scoped=False)
        return None

    def _first_verdict(self, candidates: list[tuple[str, Path]], *, scoped: bool) -> str | None:
        for kind, path in candidates:
            try:
                verdict = self._parse_artifact(kind, path, scoped=scoped)
            except (OSError, ValueError):
                continue
            if verdict is not None:
                return verdict
        return None

    def _parse_artifact(self, kind: str, path: Path, *, scoped: bool) -> str | None:
        """Extract a gate verdict from one artifact, by kind."""
        text = path.read_text(encoding="utf-8")
        if kind == "json":
            data = json.loads(text)
            if isinstance(data, dict):
                raw = data.get("gate_status") or data.get("decision")
                if isinstance(raw, str):
                    return _scoped_verdict(raw) if scoped else _verdict_token(raw)
            return None
        if scoped:
            return _scoped_markdown(text, kind)
        if kind == "md_review":
            return _scan_markdown(text, ("recommendation",), _review_token)
        return _scan_markdown(text, _MD_LABELS[kind], lambda line: _verdict_token(line))
