# Copilot CLI 1.0.105 events.jsonl captures

Real `~/.copilot/session-state/<session>/events.jsonl` files, sanitized. The
`copilot-events` usage parser (`tokens.tally_copilot_events`) is pinned on them
in `tests/test_tokens.py`.

- **Version:** GitHub Copilot CLI 1.0.105, Copilot Free plan (Copilot Fusion
  auto-routing).
- **OS:** Linux, under tmux, with the `agentStop`/`sessionStart`/`sessionEnd`
  hooks of `src/bmad_loop/data/profiles/copilot.toml`.
- **Captured:** 2026-11-04, one trivial prompt per turn.

| Session                   | Launch                                                                                       | Turns | Ended by |
| ------------------------- | -------------------------------------------------------------------------------------------- | ----- | -------- |
| `c0000000-…-000000000001` | `--model claude-haiku-4.5` (not on the plan: Copilot logged a `model` error and auto-routed) | 1     | `/exit`  |
| `c0000000-…-000000000002` | no `--model`                                                                                 | 2     | `/exit`  |

What the captures show:

- `session.start` `data.selectedModel` echoes the `--model` flag verbatim and is
  absent without one. The model that ran (`mai-code-1.1-flash`) appears only in
  later events: `session.model_change` (`newModel: "auto"`),
  `session.auto_mode_resolved` `chosenModel`, `assistant.message` `model`,
  `session.usage_record`, and the metrics snapshots.
- Every turn ends with a `session.usage_checkpoint` whose
  `data.accountingSnapshot.modelMetrics` holds the cumulative per-model totals.
  `session.shutdown` repeats them at `data.modelMetrics`. The metrics are keyed
  by bare model id.
- `usage.inputTokens` includes `cacheReadTokens`: `tokenDetails.input` is their
  difference.
- Killing the tmux window after `agentStop` also writes `session.shutdown`
  (checked live, not captured here).

Redacted: every string value except event types, enum-like fields and model
ids/names was replaced with `<redacted>` (prompts, system prompt, tool schemas,
paths, request and API ids). Session and event ids were mapped to fixed
placeholder UUIDs, and lists were cut to their first three items. Numbers,
booleans, timestamps, key layout and line order are verbatim.
