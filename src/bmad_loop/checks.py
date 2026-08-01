"""Findings — the structured form of what ``bmad-loop validate`` reports.

``validate`` accumulated two lists of prose strings, printed one as ``  ok:`` and
the other as ``FAIL:``. That made the *outcome* addressable (the exit code) but
not the *checks*: a script wanting to branch on "hooks aren't registered" had to
match remediation sentences, which are the part most likely to be reworded. A
:class:`Finding` pairs each printed line with a stable ``check`` id, so the id is
the matchable identity and the message stays free to change.

This lives in its own module rather than in ``cli`` or ``machine``:

- ``cli`` imports ``install``, and ``install`` must *return* Findings — putting
  the type in ``cli`` would make that a cycle.
- ``machine`` is deliberately narrow: transport for the ``--json`` contract, zero
  domain knowledge. Severity is a concept no other ``--json`` command has.

:data:`VALIDATE_CHECKS` is the registry every id must be in, enforced by an
assert in :meth:`ValidationReport.add`. That is what keeps "one printed line =
exactly one Finding" true: a new check site cannot ship without first naming
itself here. The assert only ever fires on a literal an author just wrote, never
on runtime data, so it is a lint with a stack trace rather than a validation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["ok", "warning", "problem"]

# The id names the *gate*, not the outcome — so the same id carries the ok and
# the problem for one check ("adapter.binary" is both "codex found" and "codex
# not found on PATH"). Ids are split only where the two outcomes are genuinely
# different findings with different detail (skills.base vs base-missing vs
# base-incomplete). Scheme: <area>.<check>.
VALIDATE_CHECKS: frozenset[str] = frozenset(
    {
        "bmad-config",
        "policy",
        "policy.model-qualified",
        "policy.isolation-repo-root",
        "adapter.profile",
        "adapter.binary",
        "adapter.hookless",
        "adapter.httpx",
        "queue.sprint-status",
        "queue.sprint-status-unknown-keys",
        "queue.stories-manifest",
        "git.worktree-clean",
        "git.probe",
        "git.render-tracked",
        "hooks.config-parse",
        "hooks.registered",
        "mux.backend",
        "mux.preflight",
        "mux.backends-detected",
        "mux.selection",
        "mux.external-backend",
        "host.process",
        "skills.base",
        "skills.base-missing",
        "skills.base-incomplete",
        "skills.base-shim",
        "skills.dev-renderer",
        "skills.dev-renderer-config",
        "skills.dev-renderer-sources",
        "skills.customize-legacy",
        "skills.stories-dispatch",
        "skills.stories-dispatch-missing",
        "skills.stories-dispatch-stale",
    }
)


@dataclass(frozen=True)
class Finding:
    """One check's outcome: a stable ``check`` id, a severity, the human line.

    ``message`` is the exact prose the text mode prints (minus the severity
    prefix) and is **not** contracted — several are a bare ``str(e)`` from the
    config/policy/profile/sprint-status exceptions, so the wording moves with
    those modules. ``detail`` carries what the check knew before it flattened
    itself into that sentence, keyed however suits the check.
    """

    check: str
    severity: Severity
    message: str
    detail: dict | None = None


@dataclass
class ValidationReport:
    """The findings of one ``validate`` pass, in emission order.

    Emission order is the order the gates ran, and it is preserved across
    severities — :meth:`render` filters, it never re-sorts, so the text output is
    byte-identical to the two-list form it replaced (all stdout lines in append
    order, then all stderr lines in append order).
    """

    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, severity: Severity, message: str, detail: dict | None = None) -> None:
        assert check in VALIDATE_CHECKS, f"unregistered check id: {check!r}"
        self.findings.append(Finding(check, severity, message, detail))

    def ok(self, check: str, message: str, detail: dict | None = None) -> None:
        self.add(check, "ok", message, detail)

    def warn(self, check: str, message: str, detail: dict | None = None) -> None:
        self.add(check, "warning", message, detail)

    def fail(self, check: str, message: str, detail: dict | None = None) -> None:
        self.add(check, "problem", message, detail)

    def extend(self, findings: list[Finding]) -> None:
        for finding in findings:
            self.add(finding.check, finding.severity, finding.message, finding.detail)

    @property
    def passed(self) -> bool:
        """True when nothing failed. Warnings do not clear it and do not set it —
        this is exactly the validate exit code (0 when True, 1 when False)."""
        return not any(f.severity == "problem" for f in self.findings)

    def counts(self) -> dict[str, int]:
        return {
            severity: sum(1 for f in self.findings if f.severity == severity)
            for severity in ("ok", "warning", "problem")
        }

    def render(self) -> None:
        """Print the human-readable form: notes to stdout, then problems to stderr.

        The doubled space in the warning line (``  ok:   warning: ...``) is the
        shipped output, not a typo to tidy: the warning sites stored
        ``"  warning: " + msg`` into the same list the ``  ok: `` printer walked.
        Tidying it would be a silent text change — the validate tests
        substring-match on a lowercased stream and would not notice.
        """
        for finding in self.findings:
            if finding.severity == "ok":
                print(f"  ok: {finding.message}")
            elif finding.severity == "warning":
                print(f"  ok:   warning: {finding.message}")
        for finding in self.findings:
            if finding.severity == "problem":
                print(f"FAIL: {finding.message}", file=sys.stderr)
