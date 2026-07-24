# Deferred Work Format

Canonical entry format for `{implementation_artifacts}/deferred-work.md`. On the
inner dev path the bmad-dev-auto session appends its own flat entries (review
defers, multi-goal splits, token splits); the orchestrator owns the ledger and
normalizes those flat entries into this canonical form on sweep, and a
`bmad-loop sweep` migration rewrites freeform pre-DW-format content from older
projects into it wholesale (see `./migration-mode.md`; the TUI displays such
legacy items read-only until that happens). The file is append-only — never
rewrite or delete existing entries.

## Before appending: dedupe check

Scan the existing file for an entry describing the same issue or goal (same
location and same substance, even if worded differently). If one exists, do
NOT append a duplicate — add a `seen-again:` line to the existing entry
instead:

```markdown
seen-again: 2026-06-12 (code review of spec-3-3-export.md)
```

## Entry format

Number entries sequentially (`DW-1`, `DW-2`, …) by scanning the file for the
highest existing number. One entry per deferred item:

```markdown
### DW-<seq>: <one-line title>

origin: <workflow + artifact + date, e.g. "code review of spec-3-2-digest.md, 2026-06-12">
location: <file:line or component, or "n/a" for deferred goals>
severity: <critical | high | medium | low — how much it matters if never done>
reason: <why this was deferred rather than done now, one or two sentences>
status: open
```

`severity:` is optional — entries written before this field existed have none
and that is fine; readers must treat a missing or unrecognized value as
"unspecified". Use `critical` for correctness/security issues, `high` for
likely user-visible problems, `medium` for quality and robustness gaps, `low`
for polish and nice-to-haves.

When a deferred item is later completed, set its `status:` to `done` with the
date (e.g. `status: done 2026-06-20`) — do not delete the entry.

## Sweep annotations

`bmad-loop sweep` runs (the orchestrator and its bundle dev sessions) add two
optional field lines to existing entries — both directly after `status:`:

```markdown
resolution: <one line: what was built or why the entry was closed>
decision: <date> <chosen option label> — <detail>
```

- `resolution:` accompanies every sweep close (`status: done <date>`). Bundle
  dev sessions write it when finishing a bundle's entries; the orchestrator
  writes it when closing entries triage proved already resolved.
- `decision:` records a human's sweep-time choice on an entry. It does not by
  itself change `status:` — a `keep-open` decision leaves the entry open.

## Closure declared by a story

A sweep bundle is not the only thing that closes an entry. A regular story may
declare the entries its work closes — on its `stories.yaml` entry (stories mode),
or in its story spec's frontmatter. The two are unioned:

```yaml
closes_deferred: [DW-5, DW-6] # DW-<n> ids this story closes
```

At clean story-close the orchestrator annotates each declared id exactly as a
bundle close does — `status: done <date>` plus a `resolution:` line naming the
story:

```markdown
status: done 2026-07-23
resolution: resolved by story 3-2-export
```

The rules that keep this safe:

- **Declared, never inferred.** Closure comes only from this field; the
  orchestrator does not guess it from a diff.
- **Only on a clean close.** A story that fails, blocks, or ends short of its
  success status closes nothing.
- **Idempotent.** An id already `done` is left untouched, so a resumed run
  re-driving the same close neither doubles the `resolution:` line nor warns.
- **Never a gate.** An id that matches no entry is journaled and dropped —
  it cannot fail the story. `bmad-loop validate` reports the same mismatch as a
  warning before the run starts.

Keep the ids stable when editing this file: a reworded title is fine, but
renumbering an entry orphans any declaration that already references it.
