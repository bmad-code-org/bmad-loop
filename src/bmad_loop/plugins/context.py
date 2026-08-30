"""HookContext + Veto: the per-stage object the hook bus hands every plugin.

A ``HookContext`` is the shared run context for one lifecycle stage. It carries:

  * **read-only** facts about where the run is (identity/git fields, the current
    phase/role/attempt, a *copy* of the session result, etc.) — exposed as
    properties with no setter so a plugin can observe but never rewrite history;
  * a **per-stage mutable whitelist** (``proposed_prompt``/``proposed_env`` for a
    session, ``proposed_commit_message`` for a commit, ``proposed_feedback``,
    ``proposed_decision``) the engine reads back after dispatch and applies;
  * a free-form ``shared`` dict that persists across stages (the engine backs it
    with ``RunState.plugin_shared`` so it survives pause/resume).

Veto is collect-then-resolve-most-conservative: every plugin that objects to a
stage appends a ``Veto``; the bus never short-circuits, so load order can never
hide a severer veto. ``resolved_veto()`` returns the single most-conservative one
(``skip`` < ``defer`` < ``pause``). The engine maps it onto its *existing*
control flow — there is no new abort path.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only, exactly as `model.py` imports `HookContext`: the concrete
    # verifier record type belongs in the signature, but importing it for real
    # would be this package's SECOND core import (after manifest.py ->
    # platform_util) and would point `plugins/` at the engine's I/O layer. There
    # is no cycle today — `verify` reaches deferredwork/bmadconfig/frontmatter/
    # model/platform_util/policy/sprintstatus, none of which touch `plugins/` —
    # so this is layering, not a workaround.
    from ..verify import CommandResult

# Veto actions, least to most conservative. `skip` drops the current unit and
# continues the loop; `defer` routes through the engine's defer primitive; `pause`
# escalates (raises RunPaused). Severity orders a multi-plugin resolve.
VETO_ACTIONS = ("skip", "defer", "pause")
_VETO_SEVERITY = {"skip": 1, "defer": 2, "pause": 3}

# The mutable whitelist. A declarative hook's stdout `mutate` block and a python
# hook may only assign these; everything else on the context is read-only.
MUTABLE_FIELDS = frozenset(
    {
        "proposed_prompt",
        "proposed_env",
        "proposed_feedback",
        "proposed_commit_message",
        "proposed_decision",
    }
)


@dataclass(frozen=True)
class Veto:
    """One plugin's objection to a stage. ``action`` is in VETO_ACTIONS;
    ``plugin_id`` records who raised it for the journal + escalation message."""

    action: str
    reason: str = ""
    plugin_id: str = "?"

    def __post_init__(self) -> None:
        if self.action not in VETO_ACTIONS:
            raise ValueError(f"veto action must be one of {VETO_ACTIONS}: got {self.action!r}")


class HookContext:
    """Mutable run context for a single stage emit. The bus dispatches it to
    every plugin bound to the stage; the engine applies the whitelisted
    mutations + resolves the veto afterwards."""

    def __init__(
        self,
        stage: str,
        *,
        run_id: str = "",
        story_key: str | None = None,
        epic: int | None = None,
        phase: str | None = None,
        attempt: int | None = None,
        role: str | None = None,
        worktree: str | None = None,
        branch: str | None = None,
        repo_root: str | None = None,
        run_dir: str | None = None,
        agents: tuple[str, ...] = (),
        result_json: dict[str, Any] | None = None,
        session_status: str | None = None,
        verify_reason: str | None = None,
        command_results: tuple[CommandResult, ...] = (),
        verification_stage: str | None = None,
        verification_sequence: int | None = None,
        decision_action: str | None = None,
        settings: dict[str, Any] | None = None,
        shared: dict[str, Any] | None = None,
        proposed_prompt: str | None = None,
        proposed_env: dict[str, str] | None = None,
        proposed_feedback: str | None = None,
        proposed_commit_message: str | None = None,
        proposed_decision: str | None = None,
    ):
        self._stage = stage
        self._run_id = run_id
        self._story_key = story_key
        self._epic = epic
        self._phase = phase
        self._attempt = attempt
        self._role = role
        self._worktree = worktree
        self._branch = branch
        self._repo_root = repo_root
        self._run_dir = run_dir
        # the agent ids of the CLIs that run in this unit's worktree (dev + review),
        # for a plugin that routes per-agent config (e.g. the engine's MCP routing).
        self._agents = tuple(agents)
        # A *deep* copy, and the depth is the whole point. `dict()` is shallow, so
        # the nested `escalations` list stayed SHARED with the engine's own
        # `result.result_json` — and both verify legs now emit `post_dev_verify`
        # ahead of their `critical_escalations` audit (dev via `decide_dev`, fix
        # at the reordered call in `_fix_phase`). An in-process plugin holding
        # this context could therefore clear that list and erase a CRITICAL
        # escalation out from under the audit, letting a verify-green repair
        # proceed where the run owed a pause. Copying at all exists to make
        # "plugins observe, cannot alter" true; shallow made it true only of the
        # top level, which is not where escalations live.
        self._result_json = copy.deepcopy(result_json) if result_json is not None else None
        self._session_status = session_status
        self._verify_reason = verify_reason
        # Frozen command-result records with immutable strings.  This is an
        # observe-only surface: plugins cannot replace the verifier outcome or
        # modify this tuple, and the engine never reads it back for a decision.
        self._command_results = tuple(command_results)
        # The journal correlation keys for the pass those records came from —
        # `verification_stage` is also the dev-vs-repair discriminator, which
        # neither `stage` (both legs emit `post_dev_verify`) nor `phase` (both
        # are DEV_VERIFY) nor `attempt` (one counter, shared) can supply.
        self._verification_stage = verification_stage
        self._verification_sequence = verification_sequence
        self._decision_action = decision_action
        self._settings = dict(settings) if settings is not None else {}
        # free-form, persisted across stages (engine backs it with plugin_shared)
        self.shared: dict[str, Any] = shared if shared is not None else {}
        # mutable whitelist (plain public attributes; engine reads them back)
        self.proposed_prompt = proposed_prompt
        self.proposed_env = dict(proposed_env) if proposed_env is not None else None
        self.proposed_feedback = proposed_feedback
        self.proposed_commit_message = proposed_commit_message
        self.proposed_decision = proposed_decision
        # veto collection + the plugin currently dispatching (set by the bus so
        # ctx.veto() can attribute the objection without the plugin passing a name)
        self._vetoes: list[Veto] = []
        self._current_plugin = ""

    # ---------------------------------------------------------- read-only view

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def story_key(self) -> str | None:
        return self._story_key

    @property
    def epic(self) -> int | None:
        return self._epic

    @property
    def phase(self) -> str | None:
        return self._phase

    @property
    def attempt(self) -> int | None:
        return self._attempt

    @property
    def role(self) -> str | None:
        return self._role

    @property
    def worktree(self) -> str | None:
        return self._worktree

    @property
    def branch(self) -> str | None:
        return self._branch

    @property
    def repo_root(self) -> str | None:
        return self._repo_root

    @property
    def run_dir(self) -> str | None:
        return self._run_dir

    @property
    def agents(self) -> tuple[str, ...]:
        return self._agents

    @property
    def result_json(self) -> dict[str, Any] | None:
        return self._result_json

    @property
    def session_status(self) -> str | None:
        return self._session_status

    @property
    def verify_reason(self) -> str | None:
        return self._verify_reason

    @property
    def command_results(self) -> tuple[CommandResult, ...]:
        """The verifier ``CommandResult`` records from this attempt's verify pass,
        in the order the commands ran. Read-only observability for
        ``post_dev_verify``; nothing here feeds an engine decision.

        Each record carries ``command``, ``returncode``, the merged bounded
        ``output_tail``, the separate ``stdout`` / ``stderr`` streams with their
        optional ``*_full_bytes`` emission counts (``None`` means the matching
        stream was retained whole), and ``spawn_error``. That last one is normally
        ``None`` and is set when the child could not be STARTED — its ``returncode``
        is then ``verify.SPAWN_FAULT_RC``, a sentinel outside the range any real
        child reports, so a handler must not read the rc of such a record as an
        exit status. The pass that produced it always ends the attempt as an
        environment fault.

        Empty is ambiguous ON ITS OWN and must not be read as "the commands did
        not run" — read it together with :attr:`verification_stage`, which is what
        separates the cases:

        * ``verification_stage is None`` — no verify pass ran at all. FOUR
          distinct causes reach here and an empty tuple names none of them:

          1. the session did not complete — ``session_status`` says so;
          2. the dev-artifact gate already failed the attempt — ``verify_reason``;
          3. on the repair leg, the deferral harvest short-circuited ahead of the
             commands — also ``verify_reason``;
          4. the engine variant suppressed the pass for this leg —
             ``StoriesEngine`` skips it on a plan-halt leg, which has no
             implementation to build, so nothing on the context marks this one
             apart from a run that simply configured no commands.

          ``session_status`` and ``verify_reason`` separate 1–3; this tuple
          separates none of them, and does not try.
        * ``verification_stage`` set with ``verification_sequence is None`` — the
          pass DID run and executed nothing, because ``[verify] commands`` is
          empty. No journal record exists for it either.
        * ``verification_stage`` set with an int ``verification_sequence`` — those
          commands ran, and each has a matching journal entry (see that property).
        """
        return self._command_results

    @property
    def verification_stage(self) -> str | None:
        """Which leg produced :attr:`command_results` — ``"dev"`` for the initial
        dev verification, ``"fix"`` for a feedback-driven repair one, ``None``
        when no verify pass ran (see :attr:`command_results`).

        This is the ONLY discriminator between the two. ``stage`` and ``phase``
        are literally identical across them (``post_dev_verify`` from
        ``Phase.DEV_VERIFY``), and ``attempt`` is one per-story counter the
        repair leg CONTINUES rather than restarts — so its value orders the two
        but never names either, and a human re-arm reuses the numbers outright.
        """
        return self._verification_stage

    @property
    def verification_sequence(self) -> int | None:
        """This story's 1-based ordinal for the verify pass that produced
        :attr:`command_results`, or ``None`` when the pass recorded nothing.

        The join key back to the run journal: the ``verify-command-result``
        entries with this ``story_key`` + ``verification_stage`` +
        ``verification_sequence`` are exactly these results, one per record,
        ordered by their ``command_index``. Monotonic per story across a
        pause/resume — the sequence is durable, unlike ``attempt``, which a human
        re-arm can reuse.
        """
        return self._verification_sequence

    @property
    def decision_action(self) -> str | None:
        return self._decision_action

    @property
    def settings(self) -> dict[str, Any]:
        return self._settings

    # ------------------------------------------------------------ veto surface

    def veto(self, action: str, reason: str = "") -> None:
        """Plugin-facing: object to this stage. Attributed to the dispatching
        plugin. Multiple plugins (and multiple calls) accumulate — the engine
        resolves the most-conservative one."""
        self._vetoes.append(
            Veto(action=action, reason=reason, plugin_id=self._current_plugin or "?")
        )

    def add_veto(self, veto: Veto) -> None:
        """Bus-facing: record a veto synthesized from a declarative hook's exit
        code or stdout (the plugin id is already filled in)."""
        self._vetoes.append(veto)

    @property
    def vetoed(self) -> bool:
        return bool(self._vetoes)

    @property
    def vetoes(self) -> tuple[Veto, ...]:
        return tuple(self._vetoes)

    def resolved_veto(self) -> Veto | None:
        """The single most-conservative veto (pause > defer > skip), or None.
        Ties resolve to the first raised, so a deterministic plugin order gives a
        deterministic outcome."""
        if not self._vetoes:
            return None
        return max(self._vetoes, key=lambda v: _VETO_SEVERITY[v.action])

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<HookContext {self._stage!r} story={self._story_key!r} vetoes={len(self._vetoes)}>"
