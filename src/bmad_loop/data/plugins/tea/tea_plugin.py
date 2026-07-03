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
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _scan_markdown(text: str, labels: tuple[str, ...], normalize) -> str | None:  # noqa: ANN001
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
        """Readiness gate: with ``require_tea`` on, refuse to start unless TEA is
        installed in the project (``_bmad/tea/config.yaml`` present). Raising here
        propagates through ``registry.validate`` so the run fails fast at startup
        rather than launching six sessions that each invoke a missing workflow."""
        if not self._require_tea():
            return
        config = self._tea_config_path(self._project_root())
        if not config.is_file():
            raise PluginError(
                "plugin 'tea': the Test Architect Enterprise (TEA) module is not "
                f"installed in this project (missing {config}). Install it with "
                "`npx bmad-method install` and choose 'Test Architect', or set "
                "require_tea = false under [plugins.tea] in .bmad-loop/policy.toml "
                "to run the TEA workflows advisory-only without it."
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
        gates = [g for g in GATE_STEPS if bool(self.settings.get(f"{g}_blocking"))]
        if not gates:
            return  # nothing flagged blocking -> advisory only, no artifact read

        # Resolve the tree TEA wrote into: the unit's worktree (where the
        # pre_commit_gate sessions just ran), falling back to the repo root.
        # Under isolation = none the two coincide.
        root = ctx.worktree or ctx.repo_root
        if not root:
            return  # fail-open: no tree to inspect
        artifacts_dir = self._artifacts_dir(Path(root))

        verdicts: dict[str, str] = {}
        failed: list[tuple[str, str]] = []
        for gate in gates:
            verdict = self._gate_verdict(gate, artifacts_dir)
            if verdict is None:
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

    def _project_root(self) -> Path:
        """The project root, resolved the way the engine resolves it: the run's
        working directory (``bmad-loop``'s ``--project`` defaults to cwd, and the
        process is not chdir'd into a worktree at construction time). validate()
        has no ``HookContext`` to read ``repo_root`` from, so cwd is the available
        signal; ``on_pre_commit`` uses ``ctx.worktree`` / ``ctx.repo_root`` for the
        authoritative per-unit root."""
        return Path.cwd()

    def _tea_config_path(self, root: Path) -> Path:
        return root / "_bmad" / "tea" / "config.yaml"

    def _artifacts_dir(self, root: Path) -> Path:
        """TEA's configured ``test_artifacts`` directory, resolved against ``root``.

        Reads the ``test_artifacts`` line from ``_bmad/tea/config.yaml`` (a light
        line parse — no YAML dependency, and robust if the file is absent), expands
        the ``{project-root}`` / ``{output_folder}`` tokens, and falls back to the
        installer default. A relative value is joined onto ``root``."""
        value = "{project-root}/_bmad-output/test-artifacts"
        config = self._tea_config_path(root)
        try:
            for raw in config.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("test_artifacts:"):
                    value = line.split(":", 1)[1].strip().strip("\"'")
                    break
        except OSError:
            pass  # fail-open: use the default location
        value = value.replace("{project-root}", str(root)).replace(
            "{output_folder}", str(root / "_bmad-output")
        )
        path = Path(value)
        return path if path.is_absolute() else root / value

    def _gate_verdict(self, gate: str, artifacts_dir: Path) -> str | None:
        """The latest verdict for ``gate`` from its artifacts, or None. Probes each
        candidate in order, returning the first that parses to a known verdict;
        any read/parse error on a candidate is swallowed (fail-open) and the next
        is tried."""
        for kind, filename in GATE_ARTIFACTS.get(gate, ()):
            path = artifacts_dir / filename
            if not path.is_file():
                continue
            try:
                verdict = self._parse_artifact(kind, path)
            except Exception:  # noqa: BLE001  # nosec B112 - fail-open: a parse error never blocks
                continue
            if verdict is not None:
                return verdict
        return None

    def _parse_artifact(self, kind: str, path: Path) -> str | None:
        """Extract a gate verdict from one artifact, by kind."""
        text = path.read_text(encoding="utf-8")
        if kind == "json":
            data = json.loads(text)
            if isinstance(data, dict):
                raw = data.get("gate_status") or data.get("decision")
                if isinstance(raw, str):
                    return _verdict_token(raw)
            return None
        if kind == "md_review":
            return _scan_markdown(text, ("recommendation",), _review_token)
        return _scan_markdown(text, _MD_LABELS[kind], lambda line: _verdict_token(line))
