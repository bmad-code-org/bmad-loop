"""Committed, per-story records of stories parked at ``awaiting-operator`` (#335, #356).

A park commits everything an agent could do and records what a human still owes
in the spec's ``operator_actions:`` frontmatter. That frontmatter, plus the
board's ``awaiting-operator`` token, is the *committed truth*. This module adds
a record over it — one JSON file per parked story under ``.bmad-loop/operator/``
— because the truth alone is not findable: ``spec_file`` is reported by the dev
session in its result JSON and is not derivable from a story key, so the record
is the only route from ``bmad-loop confirm <key>`` back to the spec.

Committed, one file per story
-----------------------------
The record is written into the WORKSPACE inside the story's commit window, just
before ``finalize_commit``'s ``git add -A``, so it rides the park's own commit —
and under ``scm.isolation = "worktree"`` it reaches the target branch with the
ordinary merge-back (#355 merges parked units beside DONE). That placement is
what makes a committed record safe where committing a shared index was not
(#356): nothing lands on the *target* branch during the commit window (the
``ff`` merge-back survives, ``branch_per = "run"`` included), the clean tree the
epic-boundary auto-sweep requires is undisturbed (the record is part of the
story's commit, not residue beside it), and a crash re-drives through the same
COMMITTING resume arm that re-derives the park itself. One file per story,
rather than one shared index, means two parks on different branches can never
produce a merge conflict. The payoff is the acceptance criterion of #356: a
fresh clone carries every record, so ``confirm`` works wherever the repository
does.

The record deliberately carries no commit sha — it is written into the very
commit it rides, so the sha does not exist yet. :func:`resolve` derives it from
``git log`` over the record's own path instead, which under a squash merge names
the commit that actually carries the park on *this* branch; a record not yet in
any commit derives to ``""``.

The legacy machine-local index
------------------------------
Before #356 the store was a single ``.bmad-loop/operator-actions.json``, kept
out of git via the repository-local exclude file. :func:`load` still reads it —
a park written by an older version must stay confirmable on the machine that
wrote it — and :func:`drop` prunes it, but nothing writes it anymore. A
per-story record wins its key over a legacy entry. Stale exclude lines on old
machines are left alone: they keep leftover legacy files invisible, which is
exactly right for a file nothing writes.

Because a record can still drift from the committed truth it points at (a
hand-edited spec, a ``git revert``, a story re-driven to ``done``), ``validate``
carries ``operator.registry-stale``, ``operator.actions-malformed`` and
``operator.confirm-interrupted``, and reports a board parked at
``awaiting-operator`` that no record claims as ``operator.park-record-missing``
(#356) — ids split where the remedy does. All are warnings: ``confirm`` refuses
drifted entries itself, so nothing gates on the record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import devcontract, sprintstatus, verify
from .bmadconfig import ProjectPaths
from .frontmatter import operator_actions_of, read_frontmatter, status_of
from .platform_util import atomic_write_text_confined, safe_segment

RECORDS_REL = Path(".bmad-loop") / "operator"
LEGACY_STORE_REL = Path(".bmad-loop") / "operator-actions.json"
AWAITING_OPERATOR = verify.AWAITING_OPERATOR
# The status a confirmation lands on. Spelled here rather than imported from
# `sprintstatus.STATUS_ORDER` because both sides of the join use it — the board
# token and the spec's frontmatter status — and only one of those is a board.
DONE = "done"


def records_dir(project: Path) -> Path:
    return project / RECORDS_REL


def record_path(project: Path, story_key: str) -> Path:
    """Where a story's park record lives. `safe_segment` is identity for every
    conventional story key; a hostile key gets a legal filename, and the record's
    own ``story_key`` field stays authoritative over the mangled stem."""
    return records_dir(project) / f"{safe_segment(story_key)}.json"


def legacy_store_path(project: Path) -> Path:
    return project / LEGACY_STORE_REL


# --------------------------------------------------------------- store I/O


def load(project: Path) -> dict[str, dict]:
    """Every park record, ``{story_key: {actions, spec_file, run_id,
    parked_at}}``, merged over any legacy machine-local entries (a record wins
    its key). Tolerant throughout: this is a convenience over committed truth,
    so an unreadable side degrades rather than blocking a human from confirming
    their own work. An unparseable RECORD file still contributes an empty entry
    under its filename stem — visible as drift ("no spec file could be located
    for it") rather than silently absent, because the file's presence is the
    committed claim that something is owed."""
    data: dict[str, dict] = dict(_load_legacy(project))
    for path in _record_files(project):
        record = _read_json(path)
        if isinstance(record, dict):
            key = str(record.get("story_key") or "") or path.stem
            data[key] = record
        else:
            data.setdefault(path.stem, {})
    return data


def _record_files(project: Path) -> list[Path]:
    try:
        return sorted(records_dir(project).glob("*.json"))
    except OSError:
        return []


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _load_legacy(project: Path) -> dict[str, dict]:
    path = legacy_store_path(project)
    if not path.is_file():
        return {}
    data = _read_json(path)
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------ record + drop


def record_park(
    project: Path,
    story_key: str,
    *,
    actions: list[str],
    spec_file: str,
    run_id: str,
    parked_at: str,
) -> Path:
    """Write a story's park record, returning its path. Re-parking the same key
    overwrites rather than accumulates: a story owes whatever its latest park
    says it owes, and a stale action list is worse than none.

    The write goes through :func:`platform_util.atomic_write_text` (#379), which
    carries the unlink-on-raise this used to hand-roll — hence no try/except here;
    two nested guards would only obscure which one runs — and adds the two things
    the hand-rolled version lacked. The temp is *uniquely* named instead of the
    fixed ``.tmp`` sibling: this runs just ahead of ``finalize_commit``'s ``git
    add -A``, so a stranded temp rides the story's own commit forever, and one
    fixed name is what two writers of the same key would collide on. And the
    contents are fsynced *before* the replace publishes them — a host losing power
    just after the rename otherwise comes back with the record's name pointing at
    blocks that were never written, which :func:`load` reads as an entry owing
    nothing while the board still says a human owes something.

    Refusing a link at the record itself preserved what the bare ``os.replace``
    did (it never dereferenced this destination) and matched what the record is:
    machine-minted, under a project root a driven session can write. The write is
    now confined to ``project`` (#593), because that refusal stopped at the final
    component: ``mkdir(parents=True, exist_ok=True)`` on the line below accepts a
    symlink-to-a-directory, so a link planted at ``.bmad-loop/`` survived the
    setup step and redirected both the temp and the publish to wherever it
    pointed. The confined writer walks the components below ``project``
    ``O_NOFOLLOW`` and writes through the descriptor that walk produced. The
    record still lands at ``mkstemp``'s ``0600`` rather than the hand-rolled
    temp's ``0644`` — no-follow never inherited a mode either, so confining it
    changes no permissions; git carries no mode but the exec bit, so nothing
    downstream of the commit notices.

    ``require_writable_target=True`` (#597) for consistency with the OTHER two
    writers of this same file — ``Engine._restore_park_record`` and ``confirm``'s
    prune — since write semantics belong to the FILE, not to whichever code path
    reached it last. An operator who marks a park record read-only gets the
    ``PermissionError`` a bare ``Path.write_text`` raised before #379."""
    path = record_path(project, story_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "story_key": story_key,
        "actions": list(actions),
        "spec_file": spec_file,
        "run_id": run_id,
        "parked_at": parked_at,
    }
    atomic_write_text_confined(
        path,
        json.dumps(record, indent=2, sort_keys=True),
        confine_root=project,
        require_writable_target=True,
    )
    return path


def drop(project: Path, story_key: str) -> bool:
    """Remove a story's park record — and any legacy machine-local entry —
    returning whether anything was removed. No write when the key is absent
    everywhere, so confirming a story no store ever knew about (pre-#356 the
    fresh-clone case) leaves no file behind. Removal raises on OSError like any
    repair write: a drop that silently failed would leave `validate` warning
    about a story that was genuinely confirmed."""
    removed = _drop_record(project, story_key)
    return _drop_legacy(project, story_key) or removed


def _drop_record(project: Path, story_key: str) -> bool:
    """Unlink every record file claiming ``story_key`` — matched on the record's
    own field, not the filename, so a mangled or hand-renamed file cannot survive
    its confirmation (nor can dropping one key ever unlink another's record)."""
    removed = False
    for path in _record_files(project):
        if _record_key(path) == story_key:
            path.unlink()
            removed = True
    return removed


def _record_key(path: Path) -> str:
    record = _read_json(path)
    if isinstance(record, dict):
        return str(record.get("story_key") or "") or path.stem
    return path.stem


def _drop_legacy(project: Path, story_key: str) -> bool:
    """Prune a legacy entry. The emptied store is deleted outright rather than
    rewritten as ``{}``: nothing writes the legacy file anymore, and an empty
    husk would read as "an index with nothing parked" forever.

    The rewrite goes through the same helper `record_park` uses and inherits its
    unlink-on-raise — but the temp matters here for the opposite reason. `drop`
    runs out of band from `confirm`, not inside a commit window, so the hazard is
    not a temp RIDING a commit but one OUTLIVING the failure: `.bmad-loop/` is not
    ignored (`install` excludes only `runs/`, `cache/` and `policy.toml`, and the
    pre-#356 exclude line was the anchored literal `operator-actions.json`, never
    its `.tmp` sibling), so a stranded `.bmad-loop/operator-actions.tmp` is an
    untracked file to `verify.worktree_clean` — a dirty tree blocking the next
    run's preflight and the epic-boundary auto-sweep, over a prune of a store
    nothing writes. The helper's temp carries a random infix, so even that
    surviving name is no longer one a second `drop` of a different key collides
    on mid-write.

    Confined to `project` and refusing a read-only target for the same reasons
    `record_park` is (#593, #597) — this writes an operator-curated file under the
    same session-writable `.bmad-loop/`. There is no `mkdir` here and none is
    needed: the write is reached only when `_load_legacy` found an entry to
    prune, so the file — and therefore the parent the confinement walk must reach
    — already exists."""
    path = legacy_store_path(project)
    data = _load_legacy(project)
    if story_key not in data:
        return False
    del data[story_key]
    if data:
        atomic_write_text_confined(
            path,
            json.dumps(data, indent=2, sort_keys=True),
            confine_root=project,
            require_writable_target=True,
        )
    else:
        path.unlink(missing_ok=True)
    return True


# ------------------------------------------------- joining records to truth


@dataclass(frozen=True)
class ParkedStory:
    """One park record joined back to the committed truth it points at.

    The record is what `confirm` can *find*; the spec and the board are what it is
    allowed to *believe*. Keeping both readings on one object is what lets the
    command refuse precisely — "the record says parked, the board says done" is a
    different message, and a different remedy, from "no such story".

    ``spec_status`` / ``board_status`` are None when that side could not be read
    at all (spec missing or unreadable, board missing or the key absent), which
    is deliberately distinct from a side that reads some *other* status.

    ``confirmation_recorded`` is the third reading: whether the spec already
    carries the ``## Operator Confirmation`` section `confirm` writes. It is what
    separates a story a human never signed off from one whose confirmation was
    interrupted part-way — two states whose spec and board readings otherwise
    look identical to a stale entry.

    ``commit`` is display provenance only, never a predicate input. For a park
    record it is DERIVED (``git log`` over the record's path — the record rides
    the commit it names, so it cannot store it) and is ``""`` for a record not
    yet in any commit; a legacy index entry keeps its stored sha."""

    story_key: str
    actions: tuple[str, ...]
    spec_path: Path | None
    spec_status: str | None
    board_status: str | None
    confirmation_recorded: bool
    commit: str
    run_id: str
    parked_at: str

    @property
    def confirmable(self) -> bool:
        """Whether both sides of the committed truth still describe a park with
        something owed. Everything else is drift, and `confirm` refuses it rather
        than flipping a board on the record's word alone."""
        return (
            bool(self.actions)
            and self.spec_status == AWAITING_OPERATOR
            and self.board_status == AWAITING_OPERATOR
        )

    @property
    def resumable(self) -> bool:
        """Whether this entry is a confirmation that was INTERRUPTED between its
        spec writes and its board write, and can simply be finished.

        `confirm` writes the audit section, then the spec status, then the board,
        then drops the entry. Stop it between the spec half and the board half —
        a raising `sprintstatus.advance`, or one that returns unchanged because
        the board line is in a shape its line regex cannot rewrite — and what is
        left on disk is a signed-off spec at `done` with an entry still pointing
        at it. That reads to :meth:`drift` as a stale entry (arm 3, "its spec now
        says status: done"), so a re-run refuses the very state a re-run exists to
        clear, and `validate` nags about it forever.

        All three readings are required. The section is the human's acknowledgment
        — without it a spec at `done` is a story someone finished by hand or
        re-drove, and confirming it would append an audit record for a sign-off
        that never happened.

        ⚠️ The board arm accepts `done` as well as `awaiting-operator`. The
        interruption message tells the human to fix the board by hand; if they do,
        a strict ``== AWAITING_OPERATOR`` test would drop the entry out of this
        predicate and strand it — record retained, `validate` warning forever,
        and no command that will remove either. `sprintstatus.advance` is
        already idempotent at `done`, so resuming from there costs nothing and
        finishes the one thing left: dropping the entry."""
        return (
            self.confirmation_recorded
            and self.spec_status == DONE
            and self.board_status in (AWAITING_OPERATOR, DONE)
        )

    def committed_drift(self) -> str | None:
        """Why the COMMITTED state — spec and board only — disagrees with this
        entry, or None when it does not.

        Split from :meth:`drift` so a caller that has already reported something
        about the *record* side (an unreadable action list) can still report a
        co-occurring disagreement about the committed side. Folding both into one
        method meant the first cause found was the only one anybody heard about,
        and the two have different remedies: repair the list, versus discard the
        entry. Ordered most-fundamental first — a missing spec explains a missing
        status, so it is reported instead of it."""
        if self.spec_path is None:
            return "no spec file could be located for it"
        if self.spec_status is None:
            return f"its spec is missing or unreadable ({self.spec_path})"
        if self.spec_status != AWAITING_OPERATOR:
            # A blank frontmatter `status:` reads "" (status_of normalizes YAML-null),
            # which would otherwise render as an empty tail on a human-facing line.
            return f"its spec now says status: {self.spec_status or '(blank)'}"
        if self.board_status is None:
            return "it is not on the sprint board"
        if self.board_status != AWAITING_OPERATOR:
            return f"the board now says {self.board_status}"
        return None

    def drift(self) -> str | None:
        """Why this entry is not confirmable, phrased for a human, or None when
        it is — the committed-side causes, then the record's own.

        The empty-actions cause comes last because it is the least fundamental:
        a spec that has moved on to `done` explains its own unreadable list, and
        reporting "it declares no readable actions" about a story nobody is
        parked on anymore sends a human to repair a file they should discard."""
        return self.committed_drift() or (
            "it declares no readable actions" if not self.actions else None
        )


def resolve(project: Path, paths: ProjectPaths) -> list[ParkedStory]:
    """Every park entry joined to its spec and board status, sorted by key.

    Reading degrades rather than raises throughout: this backs `confirm --list`
    and a `validate` warning, and neither may be the thing that crashes on a spec
    someone deleted. The actions come from the SPEC when it can be read and from
    the record only as a fallback — the spec is the committed truth, so a spec
    edited after the park shows the human what they actually owe now. The
    confirmation-section reading degrades the same way — an absent or unreadable
    spec cannot be SHOWN to carry an acknowledgment, so it reads as False and the
    entry takes the ordinary path, which reports the real fault.

    ``commit`` comes from the entry when stored (a legacy index entry) and is
    otherwise derived from the record file's own git history — see the module
    docstring. The derived sha can legitimately differ from the journal's
    ``story-awaiting-operator`` entry: under a squash merge the unit's own park
    commit never reaches the target, and the squash commit that did is the
    truthful provenance here."""
    entries = load(project)
    out: list[ParkedStory] = []
    for key in sorted(entries):
        entry = entries[key] if isinstance(entries[key], dict) else {}
        spec_path = _spec_path(entry, paths)
        spec_fm = _spec_frontmatter(spec_path)
        spec_actions = operator_actions_of(spec_fm) if spec_fm is not None else ()
        out.append(
            ParkedStory(
                story_key=key,
                actions=spec_actions or actions_of(entry),
                spec_path=spec_path,
                spec_status=status_of(spec_fm) if spec_fm is not None else None,
                board_status=_board_status(paths, key),
                confirmation_recorded=(
                    spec_path is not None and devcontract.has_operator_confirmation(spec_path)
                ),
                commit=str(entry.get("commit") or "") or _derive_commit(project, paths, key),
                run_id=str(entry.get("run_id") or ""),
                parked_at=str(entry.get("parked_at") or ""),
            )
        )
    return out


def _derive_commit(project: Path, paths: ProjectPaths, story_key: str) -> str:
    try:
        return verify.last_commit_for(paths.repo_root, record_path(project, story_key))
    except verify.GitError:
        return ""  # no VCS, or a repository with no history yet


def _spec_path(entry: dict, paths: ProjectPaths) -> Path | None:
    spec_file = str(entry.get("spec_file") or "")
    if not spec_file:
        return None
    try:
        return verify.resolve_spec_path(spec_file, paths)
    except (OSError, ValueError):
        return None


def _spec_frontmatter(spec_path: Path | None) -> dict | None:
    """The spec's frontmatter, or None when there is no readable spec there."""
    if spec_path is None:
        return None
    try:
        if not spec_path.is_file():
            return None
        return read_frontmatter(spec_path)
    except (OSError, UnicodeDecodeError):
        return None


def _board_status(paths: ProjectPaths, key: str) -> str | None:
    try:
        return sprintstatus.story_status(paths.sprint_status, key)
    except (sprintstatus.SprintStatusError, OSError, UnicodeDecodeError):
        return None


def actions_of(entry: dict) -> tuple[str, ...]:
    """The actions a park entry declares, read through the *same* normalizer
    the spec frontmatter goes through (:func:`frontmatter.operator_actions_of`).

    Sharing the reading is the point: the record is written from a spec and
    compared back against one, so a shape that collapses to ``()`` on the spec
    side must collapse to ``()`` here too. Two readings would let a hand-edited
    record disagree with the spec about what a human owes."""
    return operator_actions_of({"operator_actions": entry.get("actions")})
