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

from dataclasses import dataclass
from typing import Any

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
        # a *copy* — result_json feeds the critical_escalations audit and must
        # never be mutated through a plugin.
        self._result_json = dict(result_json) if result_json is not None else None
        self._session_status = session_status
        self._verify_reason = verify_reason
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
