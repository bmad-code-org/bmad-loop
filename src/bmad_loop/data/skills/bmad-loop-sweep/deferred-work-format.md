# Deferred Work Format

Canonical entry format for `{implementation_artifacts}/deferred-work.md`. The
orchestrator owns this file, and two eras of dev session feed it:

- **Current (BMAD-METHOD 6.10.1-next.33+).** The unattended primitive
  `bmad-build-auto` writes nothing here: it records defer-triaged review findings
  in its spec's frontmatter `deferred:` list, and after the session the
  orchestrator harvests those into canonical entries below, carrying a
  fingerprinted `origin:` that starts with `spec-deferred`, plus `source_spec:`.
- **Legacy and attended.** Pre-rename primitives (`bmad-dev-auto`) and the
  attended `bmad-build` append flat `- source_spec:` blocks directly into this
  file; a `bmad-loop sweep --migrate` session normalizes them into the canonical
  form, and rewrites freeform pre-DW-format content from older projects wholesale
  (see `./migration-mode.md`; the TUI shows such legacy items read-only until
  then).

Either way this file stays the sweep's sole read surface. Multi-goal and token
splits are a legacy/attended source only — the current unattended primitive does
not split a multi-goal spec, it records a `multiple-goals` warning in the spec's
`warnings:` and proceeds.

The file is append-only — never rewrite or delete existing entries. The one
sanctioned rewrite is operator-run archiving (below): closed entries move to
`deferred-work-archive.md` — body preserved, with an `archived: <date>` marker
appended after the status line — leaving a stub behind.

## Archiving (`bmad-loop sweep --archive`)

The operator may periodically move closed entries
(`status: done <ISO date>`) to the sibling `deferred-work-archive.md`, leaving
a stub in this file:

```markdown
### DW-7: Old closed item

status: done 2026-05-25
archived: 2026-08-24
```

Rules for sessions reading the ledger:

- The stub's `status: done` line means what it always meant — the entry is
  closed and not open work.
- An `archived:` line marks content that lives in `deferred-work-archive.md`.
  The full body (evidence, resolution, dates) is there, keyed by the same
  DW- id — read the archive file for anything beyond the stub.
- Stubs keep load-bearing field lines (`gate:`, `origin:`/`source_spec:`,
  resolution undo markers) — treat them exactly as if the entry were still
  whole. Never edit or drop them when touching a stub.
- When deduping against existing entries, a stub still counts: match on the
  id and preserved `origin:`/`source_spec:` lines, and check the archive for
  the full substance before appending.

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

`location:` is always written. Use `n/a` whenever there is nothing to open — a
deferred goal, but equally a finding whose reporter recorded no place. The field
says "no location was recorded", not "this item has none": a reader that finds
`n/a` should fall back to `reason:`, which often names the file even when
`location:` is empty. Entries written before this rule may omit the line; read
an absent `location:` as `n/a`, never as "not yet known".

Entries the orchestrator harvests from a spec carry one extra line,
`source_spec:`, directly after `location:` — the spec the deferral came from. It
is half the dedupe key (with `origin:`), so never edit or drop it when touching
an entry; entries written by hand do not need it.

**Every field line is exactly one line, and so is the title.** The format is
line-oriented: readers find each field by scanning for `<name>:` at the start of
a line, and an entry ends at whichever comes first — the next `### DW-<n>`
entry, any other `#` .. `######` heading (indented up to three spaces, and
followed by a space, a tab or the end of the line; four spaces or a leading tab
makes an indented code block, which ends nothing), or a `- source_spec:`
flat-append bullet. A value carrying a line break therefore does not wrap; it
becomes new ledger content, and three things can follow:

- a break followed by `### ` mints an entry nobody filed;
- a break before a `status:` line leaves one entry carrying two, so the ledger
  no longer says one thing about it;
- a break followed by `- source_spec:` cuts the entry short at that bullet, and
  everything after it re-surfaces as a phantom _legacy_ item.

Keep breaks out of field values, along with `### ` and a leading
`- source_spec:`. If a reason needs two sentences, write them on one line.

`severity:` is optional — entries written before this field existed have none
and that is fine; readers must treat a missing or unrecognized value as
"unspecified". Use `critical` for correctness/security issues, `high` for
likely user-visible problems, `medium` for quality and robustness gaps, `low`
for polish and nice-to-haves.

When a deferred item is later completed, set its `status:` to `done` with the
date (e.g. `status: done 2026-06-20`) — do not delete the entry.

## Hard gates: `gate:`

Some entries are not merely deferred — they **block** specific stories. An
infrastructure leg nobody has wired yet is not a nice-to-have for the first story
that consumes it; that story must not run at all until the entry lands. Say so
with a `gate:` line naming the blocked story keys:

```markdown
### DW-1: wire the blob-storage credentials

status: open
gate: 3-2, 3-3
```

`gate:` is optional and most entries have none. Its value is a **comma-separated**
list of story-key tokens; several `gate:` lines in one entry union, so an entry
blocking three stories may list them on one line or on three. A token matches a
story key when it **is** that key, or is its prefix at a key boundary — either a
`-`, or the split-story suffix (one lowercase letter then `-`). So `3-2` gates the
sprint key `3-2-invite-link-student-surface`, the stories-mode id `3-2`, and both
halves of a split (`3-2a-…` / `3-2b-…`), but never `3-20-later-story`. The split
arm matters because breakdown can split a story _after_ the gate was written, and
a gate that quietly stops matching is worse than one that was never there. The
prefix must end at a story **number** for the split arm to apply, so a word id
like `auth` does not gate `authz-login`.

Like `source_spec:`, a `gate:` line is never edited or dropped when an entry is
otherwise touched: removing it un-gates the story silently, which is the exact
failure this field exists to prevent. During a `bmad-loop sweep --migrate` that
is enforced mechanically — the orchestrator refuses a rewrite that drops a token
a pre-existing entry declared (#519); every other ledger edit is still held by
this instruction alone.

Until the entry lands, the gate is enforced twice. `bmad-loop validate` **fails**
(`deferred.hard-gate`) for every story a token matches that the queue would
actually dispatch — sprint-status stories at `backlog` / `ready-for-dev`, or
manifest entries whose spec is not yet written or sits at `draft` /
`ready-for-dev` / `in-progress` / `in-review`. A `blocked` manifest entry is not
gated, nor is one the scheduler would stop on anyway (two specs matching one id,
or a skeletal sentinel from a failed planning halt): the queue cannot reach that
story, so a gate refusing it would report work held back that was never going to
run. A `run` that never called `validate` **pauses** (`story-gate`) rather
than dispatch a gated story. Two things clear it: closing the entry
(`status: done <date>`), or removing the token because it no longer blocks that
work. This is the one deferred-work check that gates rather than advises:
everything else here is traceability that may be wrong, while this is work that
must not start.

**Only an explicit `done` retires a gate.** A status the format cannot read —
`status: opne`, or an entry with no `status:` line — is not evidence the work
landed, so the gate still holds. Write the status word exactly.

A sweep is never gated by the ledger it is draining, whatever any entry's `gate:`
says: closing the gating entry is what a sweep is for, so gating it would
deadlock the gate against its own remedy.

**Quoting the field is safe.** A `gate:` line inside a fenced code block is an
example, not a declaration, so an entry that documents this convention gates
nothing. This holds for a whole quoted entry too — heading, `status:` and `gate:`
inside one fence, the shape shown above: the fenced heading starts no entry, and
a quoted heading or bullet does not end the entry that quotes it, so a real
`gate:` below the example keeps gating. One exception worth knowing when you
write an entry: a fence you open and never close is not treated as a fence at
all, because swallowing the rest of the entry could silently disable a real
`gate:` line below it — and, at file scope, hide every entry after it. Close your
fences.

Four shapes declare a gate that nothing can enforce, and all four are reported as
`deferred.hard-gate-unstructured` while the entry is unlanded:

- a token nothing can match. It must look like a story key
  (`[A-Za-z0-9][A-Za-z0-9._-]*`, no spaces) **and** be a shape a key can actually
  take — alphanumeric segments joined by `-`, or a full sprint key. So a
  space-separated `gate: 3-2 3-3` is one bad token rather than two good ones, and
  `gate: 3.2` / `gate: 3_2` are rejected: no key spells its numbers that way.
  Inside a sprint slug those characters are fine — `gate: 3-2-a_b` is a real gate;
- a `gate:` line with nothing usable after the colon (`gate:`, `gate: ,`) — each
  such line is reported, including one sitting beside a line that does name a
  story, since the half that names nothing is the half you are wrong about;
- a `gate:` that is not lowercase at the very start of its line — `Gate: 3-2`, or
  a line that indents `gate: 3-2`. These are reported rather than read as
  declarations: the field is a fixed spelling, and guessing at near-misses is how
  a line that was never meant to gate ends up refusing a story;
- prose declaring `HARD GATE:` — the convention that predates this field —
  anywhere on a line of an entry that carries no `gate:` line. It is matched
  mid-line because `reason:` prose is hard-wrapped, but never directly after a
  quote character (`"`, `'`, `` ` ``, `«`, or a curly quote): an entry that merely
  _cites_ the phrase stays silent, as does one that writes it without the colon.

Each reads like a gate already in force while holding nothing back. Add or repair
the `gate:` line to make it enforceable.

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

Both are written by a human, and breakdown time — with this file open — is where
it belongs, though not a deadline: the declaration is read when the story
commits, so one added to a spec's frontmatter mid-run still counts. No upstream
skill emits the field yet, and re-deriving `stories.yaml` will drop it unless the
intent is recorded in `.memlog.md` first.

When the story commits, the orchestrator annotates each declared id exactly as a
bundle close does — `status: done <date>` plus a `resolution:` line naming the
story:

```markdown
status: done 2026-07-23
resolution: resolved by story 3-2-export
```

The rules that keep this safe:

- **Declared, never inferred.** Closure comes only from this field; the
  orchestrator does not guess it from a diff.
- **Only once the story actually lands.** The annotation is written at the
  commit boundary — after verification, the review loop and every checkpoint,
  and just before the story's commit is squashed. A story that fails, blocks, is
  rejected by review, or escalates closes nothing; a commit that then fails
  takes the annotation back with it, restored to the pre-close text.
- **In the story's own commit**, when this file lives inside the repo —
  worktree isolation included: the unit's copy rides the unit commit and
  reaches the target branch with the merge. If the artifacts dir is configured
  outside the repo, the file is shared between worktrees and no commit can
  carry it; the annotation is written all the same and the run journals
  `deferred-close-external-ledger`. A location that cannot be read or written
  when the write comes due closes nothing and is journaled — the entries stay
  `open` for a sweep to re-verify, and an outage is never read as "no such
  entries", never allowed to fail the story or crash the run.
- **Idempotent.** An id already `done` is left untouched, so a resumed run
  re-driving the same close neither doubles the `resolution:` line nor warns.
- **Never a gate.** An id that matches no entry, an entry whose `status:` reads
  as neither `open` nor `done`, and a story spec declaring a bare
  `closes_deferred: DW-5` where a list belongs are each journaled and dropped —
  none can fail the story. `bmad-loop validate` reports the same mismatches as
  warnings before the run starts. The one exception is that same wrong container
  in `stories.yaml`: the manifest is a schema the parser owns, so it fails to
  load there like any other field of the wrong type — before any story runs, and
  reported by `validate` up front.
- **Read at the commit.** The declaration that counts is the one on disk when the
  story commits, not the one it was implemented from — edit it late and the edit
  is honored, in both directions.

Keep the ids stable when editing this file: a reworded title is fine, but
renumbering an entry orphans any declaration that already references it.
