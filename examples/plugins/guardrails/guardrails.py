"""Guardrails — the worked-example in-process plugin.

Read this alongside docs/plugin-authoring-guide.md. In ~40 lines it touches every
extension point a plugin author is likely to use:

  * **a setting** — declared in plugin.toml as ``[[settings]]`` and read here via
    ``self.settings`` (manifest defaults overlaid by ``[plugins.guardrails]`` in
    policy.toml). It renders in the settings TUI once the plugin is enabled.
  * **an observe hook** — ``on_pre_story`` reads the read-only context and records
    a running count in the cross-stage ``ctx.shared`` dict, which the engine
    persists in ``RunState.plugin_shared`` so it survives pause/resume.
  * **a veto / validation gate** — ``on_pre_dev_phase`` maps a policy decision
    onto the engine's *existing* control flow via ``ctx.veto(...)``. There is no
    new abort path: ``skip`` retires the unit quietly, ``defer`` notifies, and
    ``pause`` escalates (raises RunPaused). The engine resolves the
    most-conservative veto when several plugins object.
  * **a context mutation** — ``on_pre_commit`` rewrites the proposed commit
    message. Only the whitelisted ``proposed_*`` fields are writable; the
    identity/git/result fields are read-only.

The provided workflow (an extra agent session at post_dev_phase) is declared
entirely in plugin.toml — no Python needed for it.

A hook handler may raise freely: the bus isolates the failure, journals it, and
disables this instance for the rest of the run. Because ``fail_closed`` is left
False (the default), such a bug fails *open* — it never wedges a real run. Set
``fail_closed = True`` on the class to make a raised handler additionally defer
the current unit.
"""

from __future__ import annotations

from bmad_loop.plugins import Plugin


class GuardrailsPlugin(Plugin):
    # default failure mode: a buggy handler is isolated but the run survives.
    fail_closed = False

    def on_pre_story(self, ctx) -> None:  # noqa: ANN001 - ctx is a HookContext
        """Observe-only: count the stories this run has reached. ``ctx.shared`` is
        free-form and JSON-serializable; the engine carries it across stages and
        persists it in RunState.plugin_shared."""
        ctx.shared["stories_seen"] = ctx.shared.get("stories_seen", 0) + 1

    def on_pre_dev_phase(self, ctx) -> None:  # noqa: ANN001
        """Validation gate: quietly skip any story in a 'parked' epic. ``skip``
        retires the unit with no human notification; switch to ``defer`` to notify
        or ``pause`` to escalate."""
        parked = int(self.settings.get("forbid_epic") or 0)
        if parked and ctx.epic == parked:
            ctx.veto("skip", f"epic {parked} is parked by the guardrails plugin")

    def on_pre_commit(self, ctx) -> None:  # noqa: ANN001
        """Mutation: append a trailer to the commit message. Reading and writing
        ``proposed_commit_message`` is the whole contract — the engine applies the
        rewrite after every plugin has had its turn (last writer wins)."""
        trailer = str(self.settings.get("trailer") or "").strip()
        if not trailer:
            return
        base = (ctx.proposed_commit_message or "").rstrip()
        if trailer not in base:
            ctx.proposed_commit_message = f"{base}\n\n{trailer}" if base else trailer
