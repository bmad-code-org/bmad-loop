# claude-stopfailure

Evidence for DW-611 and DW-612: what the real Claude Code does when the API fails a
turn, captured with zero tokens by `scripts/capture_claude_stopfailure.py`. Each
scenario has two files, and `manifest.json` records the run:

- `<scenario>.stopfailure.json`: the `StopFailure` hook payload, as a project hook
  received it on stdin.
- `<scenario>.pipe-pane.log`: raw `tmux pipe-pane` bytes of the session, which is
  what bmad-loop's env-fault tail scan reads.
- `manifest.json`: claude and tmux versions, capture date, and per scenario the
  response the fake server sent and every request it received.

## Capture

- Claude Code 2.1.317, tmux 3.8, Linux; captured 2026-11-02.
- The real `claude` binary in a private tmux server (`-f /dev/null`), pane 120x40,
  `pipe-pane -o 'cat >> log'`; one prompt (`say hi`) per scenario.
- The API is an in-process fake on `127.0.0.1`: fake key, `ANTHROPIC_BASE_URL` set to
  it, fresh `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, and the
  claude profile's own `[env]` (the classic inline renderer, as a bmad-loop session
  runs). No other endpoint is configured, and a scenario is written only when the fake
  server recorded at least one message request and nothing it did not serve; the
  manifest lists every request it received.
- `CLAUDE_CODE_MAX_RETRIES=0`, so the FIRST failure surfaces. A real session retries
  first, so its pane can carry retry lines before the final error.

| Scenario           | Fake server response                                   | `error`                 |
| ------------------ | ------------------------------------------------------ | ----------------------- |
| `mid-response-cut` | 200 SSE, block 0 complete, block 1 open, then TCP RST  | `server_error`          |
| `truncated-stream` | 200, closed before any block completes (both requests) | `unknown`               |
| `rate-limit-429`   | 429 `rate_limit_error`                                 | `rate_limit`            |
| `overloaded-529`   | 529 `overloaded_error`                                 | `server_error`          |
| `server-error-500` | 500 `api_error`                                        | `server_error`          |
| `auth-401`         | 401 `authentication_error`                             | `authentication_failed` |
| `billing-402`      | 402 `billing_error`                                    | `billing_error`         |

Findings worth knowing:

- A 529 reports `server_error`, not `overloaded`. Both map to `transient`.
- `error_details` was absent from every payload.
- The truncated stream is retried once non-streaming by the CLI itself; both requests
  are in the manifest.
- The mid-response cut prints a `Connection lost mid-response` sentence behind the
  `API Error` prefix; the 2.1.317 binary has no `closed` wording any more.

## Redaction

The pane logs are the captured bytes verbatim, and the `.stopfailure.json` files are
the hook payload re-serialized as JSON (values and key order preserved). Both carry
exactly two substitutions, applied by the harness:

- The temporary capture root (its path, Claude Code's `-` slug of it, and the `/…/`
  elided form of the welcome banner) is `<capture-root>`.
- The fake server's port after `127.0.0.1:` is `<port>`.

Session ids, prompt ids and the clock time on the pane are as captured. Line endings
are CR CR LF; `.gitattributes` marks these logs `-text`, so no `autocrlf` checkout
rewrites them.

## Repo-scan exemption

The `mid-response-cut`, `overloaded-529` and `server-error-500` payloads and pane logs
hold the CLI's own error lines, which the claude env-fault patterns match. They are
listed in `DELIBERATE_CORPUS_FILES` (tests/test_env_fault_patterns.py); no other file
here may hold a matching line.

## Re-capture

```bash
uv run python scripts/capture_claude_stopfailure.py --out tests/fixtures/claude-stopfailure
```

Needs `claude` and `tmux` on PATH; spends no tokens. Then run
`tests/test_claude_stopfailure_capture.py`.
