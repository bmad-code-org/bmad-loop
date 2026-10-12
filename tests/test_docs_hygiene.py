"""Docs hygiene guard: links and anchors resolve, generated tables are current,
and the docs keep to their content policy.

The docs are a cross-linked set — README, AGENTS, CONTRIBUTING and everything
under ``docs/`` — and nothing else checks that a link still lands when a page is
split, a heading renamed, or a section moved. This guard holds that line:

* ``test_relative_doc_links_resolve`` — every scheme-less ``](target)`` names a
  tracked file or directory, resolved against the linking doc's directory.
* ``test_doc_anchor_links_resolve`` — every ``#fragment`` (``](#x)`` within a page,
  or ``](page.md#x)`` across pages) is a heading slug or ``<a id>`` of its target.
* ``test_policy_reference_is_current`` — the generated settings tables in
  ``docs/reference/configuration.md`` match ``core.toml`` (``scripts/gen_policy_reference.py --check``).
* ``test_no_tracker_tags_in_docs`` — no ``DW-<n>`` or ``#<nn>`` refs outside ``docs/adr/``.
* ``test_doc_blocks_stay_short`` — paragraphs, list items and table cells stay under a word cap.
* ``test_features_catalog_shape`` — every FEATURES.md entry has a valid Stability and a Description.

The corpus is the tracked ``*.md`` files among README.md, CONTRIBUTING.md,
AGENTS.md and ``docs/**``. Fenced code blocks and inline code spans are stripped
before scanning, so a link shown as an example is not a link. Anchors follow
GitHub's slugger: lowercase, drop every character that is not a word character,
space or hyphen, spaces become ``-``, and a repeated slug gets ``-1``, ``-2``….

If this test flags something, fix the link (or restore the heading it named),
never the guard.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from functools import cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent

_CORPUS_ROOTS = ("README.md", "CONTRIBUTING.md", "AGENTS.md")
_FENCE = re.compile(r"^ *(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_HTML_ID = re.compile(r"""<a\s[^>]*?\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_MD_LINK_TEXT = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]*)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@cache
def _tracked() -> tuple[str, ...]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    return tuple(p for p in out.decode("utf-8").split("\0") if p)


@cache
def _tracked_paths() -> frozenset[str]:
    """Every tracked file plus every directory that holds one (POSIX, repo-relative)."""
    paths: set[str] = set()
    for f in _tracked():
        paths.add(f)
        paths.update(str(parent) for parent in PurePosixPath(f).parents if str(parent) != ".")
    return frozenset(paths)


def _corpus() -> list[str]:
    return sorted(
        f for f in _tracked() if f.endswith(".md") and (f in _CORPUS_ROOTS or f.startswith("docs/"))
    )


@cache
def _unfenced(rel: str) -> tuple[str, ...]:
    """The doc's lines with fenced code blocks blanked (line numbers preserved)."""
    lines = (REPO / rel).read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                out.append("")
                continue
            out.append(line)
        else:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                if not line.strip().lstrip(fence[0]):
                    fence = None
            out.append("")
    return tuple(out)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading's raw markdown text (before de-duplication)."""
    text = _MD_LINK_TEXT.sub(r"\1", heading)
    text = _HTML_TAG.sub("", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@cache
def anchors(rel: str) -> frozenset[str]:
    """Every fragment a link may name in ``rel``: de-duplicated heading slugs plus
    explicit ``<a id>``/``<a name>`` ids."""
    seen: Counter[str] = Counter()
    found: set[str] = set()
    for line in _unfenced(rel):
        found.update(_HTML_ID.findall(line))
        m = _HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(2))
        n = seen[base]
        seen[base] += 1
        found.add(base if n == 0 else f"{base}-{n}")
    return frozenset(found)


def _links(rel: str):
    """Yield ``(line_no, target)`` for every inline link/image target in ``rel``,
    outside fenced blocks and code spans."""
    for no, line in enumerate(_unfenced(rel), start=1):
        for m in _LINK.finditer(_CODE_SPAN.sub("", line)):
            target = m.group(1)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            if target:
                yield no, target


def _resolve(rel: str, path: str) -> str:
    """Repo-relative POSIX path a link's path part names (``/x`` is repo-rooted)."""
    if path.startswith("/"):
        joined = PurePosixPath(path.lstrip("/"))
    else:
        joined = PurePosixPath(rel).parent / path
    parts: list[str] = []
    for part in joined.parts:
        if part == "..":
            if parts:
                parts.pop()
            else:
                parts.append("..")
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def _local_links():
    """Yield ``(rel, line_no, target_rel, fragment)`` for every scheme-less link."""
    for rel in _corpus():
        for no, target in _links(rel):
            if _SCHEME.match(target):
                continue
            path, _, frag = target.partition("#")
            target_rel = _resolve(rel, unquote(path)) if path else rel
            yield rel, no, target_rel, unquote(frag)


def test_slugger_matches_github_on_known_anchors():
    """Premise for the anchor test: the slugger reproduces anchors GitHub renders
    for these docs, so a mismatch below is a broken link, not a slugger bug."""
    assert slug("Trust & safety") == "trust--safety"
    assert slug("Policy (`.bmad-loop/policy.toml`)") == "policy-bmad-looppolicytoml"
    assert slug("psmux (native Windows, experimental)") == "psmux-native-windows-experimental"
    assert slug("### `[adapter.dev]`".lstrip("# ")) == "adapterdev"
    assert slug("[Linked](x.md) heading") == "linked-heading"
    real = {
        "docs/plugin-authoring-guide.md": "trust--safety",
        "docs/reference/configuration.md": "environment-variables",
        "docs/multiplexer-backends.md": "psmux-native-windows-experimental",
        "docs/escalations-guide.md": "resolve-reverify",
    }
    missing = {rel: a for rel, a in real.items() if a not in anchors(rel)}
    assert not missing, f"slugger lost known real anchors: {missing}"


def test_corpus_is_the_tracked_docs():
    """Premise: the scan covers the docs it claims to (an empty corpus passes every check)."""
    corpus = _corpus()
    for required in (*_CORPUS_ROOTS, "docs/README.md", "docs/FEATURES.md"):
        assert required in corpus, f"{required} missing from the docs corpus"
    assert "CHANGELOG.md" not in corpus


def test_relative_doc_links_resolve():
    """Every scheme-less link names a tracked file or directory."""
    tracked = _tracked_paths()
    checked = 0
    offenders: list[str] = []
    for rel, no, target_rel, _frag in _local_links():
        if target_rel == rel:
            continue
        checked += 1
        if target_rel not in tracked:
            offenders.append(f"  {rel}:{no} → {target_rel}")
    assert checked > 50, f"premise: expected many relative links, scanned {checked}"
    assert not offenders, (
        "relative doc links naming no tracked file or directory — fix the link "
        "(or restore the page it named; a new page counts once `git add`ed):\n"
        + "\n".join(offenders)
    )


def test_doc_anchor_links_resolve():
    """Every ``#fragment`` into a markdown page is one of that page's anchors."""
    tracked = _tracked_paths()
    checked = 0
    offenders: list[str] = []
    for rel, no, target_rel, frag in _local_links():
        if not frag or not target_rel.endswith(".md") or target_rel not in tracked:
            continue  # a missing page is test_relative_doc_links_resolve's finding
        checked += 1
        if frag not in anchors(target_rel):
            offenders.append(f"  {rel}:{no} → {target_rel}#{frag}")
    assert checked > 20, f"premise: expected many anchor links, scanned {checked}"
    assert not offenders, (
        "doc links to an anchor the target page does not have — repoint the link "
        "(or restore the heading / <a id> it named):\n" + "\n".join(offenders)
    )


def test_policy_reference_is_current():
    """The generated settings tables match the core schema; regenerate on drift."""
    proc = subprocess.run(
        [sys.executable, "scripts/gen_policy_reference.py", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"gen_policy_reference.py --check exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# --- content policy: tracker tags, block length, FEATURES shape -------------

_TRACKER = re.compile(r"\bDW-\d+\b|(?<!&)#\d{2,}\b")
_LINK_TARGET = re.compile(r"\]\(\s*(?:<[^>]*>|[^)\s]*)[^)]*\)")
_AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]*:[^>\s]*>")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_HTML_COMMENT = re.compile(r"<!--.*?-->")
_GENERATED_BEGIN = "<!-- BEGIN GENERATED:"
_GENERATED_END = "<!-- END GENERATED:"

# Measured on the tree the guard landed on (ADRs excluded): the longest
# paragraph/bullet was 131 words and the longest hand-written table cell 51.
# The caps sit a little above those so a normal edit passes while a returning
# essay or a table cell that grows into a paragraph fails.
_MAX_BLOCK_WORDS = 150
_MAX_CELL_WORDS = 60

_STABILITY = ("stable", "in-progress", "experimental", "planned")


def _prose_lines(rel: str):
    """Yield ``(line_no, text)`` for each unfenced line with code spans, link
    targets, autolinks and HTML comments removed: what a reader sees as prose."""
    for no, line in enumerate(_unfenced(rel), start=1):
        line = _CODE_SPAN.sub("", line)
        line = _HTML_COMMENT.sub("", line)
        line = _AUTOLINK.sub("", line)
        line = _LINK_TARGET.sub("]", line)
        yield no, line


def _blocks(rel: str):
    """Yield ``(kind, line_no, words)`` for every paragraph or list item
    (``kind == "block"``) and every table cell (``kind == "cell"``) in ``rel``.

    Fenced code is already blank; a heading or blank line ends a block, a new
    list marker starts one, and an indented continuation line extends it.
    Generated blocks (between BEGIN/END GENERATED markers) are skipped: their
    text is owned by the source they are generated from."""
    generated = False
    start = 0
    words = 0

    def flush():
        nonlocal words
        if words:
            yield "block", start, words
        words = 0

    raw = _unfenced(rel)
    for (no, line), original in zip(_prose_lines(rel), raw):
        if _GENERATED_BEGIN in original:
            generated = True
        if generated:
            if _GENERATED_END in original:
                generated = False
            yield from flush()
            continue
        if not line.strip() or _HEADING.match(line):
            yield from flush()
            continue
        if _TABLE_ROW.match(line):
            yield from flush()
            if not _TABLE_RULE.match(line):
                for cell in _CELL_SPLIT.split(line.strip().strip("|")):
                    yield "cell", no, len(cell.split())
            continue
        if _LIST_ITEM.match(line):
            yield from flush()
        if not words:
            start = no
        words += len(line.split())
    yield from flush()


def test_no_tracker_tags_in_docs():
    """Docs describe current behavior; issue numbers and ledger ids are history.

    No ``DW-<n>`` ledger id and no ``#<nn>`` issue/PR ref (bare, ``owner/repo#N``
    or ``BMAD-METHOD#N``) in the corpus outside ``docs/adr/`` (ADRs keep their
    refs; CHANGELOG is not in the corpus). Code spans, fenced blocks and link
    targets are stripped first, so a placeholder id in an example command or a
    ``](page.md#anchor)`` fragment is not a finding."""
    offenders: list[str] = []
    for rel in _corpus():
        if rel.startswith("docs/adr/"):
            continue
        for no, line in _prose_lines(rel):
            for m in _TRACKER.finditer(line):
                offenders.append(f"  {rel}:{no}: {m.group(0)}")
    assert not offenders, (
        "tracker refs in the docs — state the current behavior instead "
        "(history lives in git and the CHANGELOG):\n" + "\n".join(offenders)
    )


def test_doc_blocks_stay_short():
    """No paragraph or list item over 150 words (``_MAX_BLOCK_WORDS``), and no
    table cell over 60 (``_MAX_CELL_WORDS``), outside code fences, generated
    blocks and ``docs/adr/`` (an ADR's rationale is its content).

    The caps sit a little above the longest block (131 words) and cell (51
    words) on the tree the guard landed on. A block that outgrows them is a
    mechanism essay or incident narration: split it into steps or a table, or
    move the guarantee into the implementing module's docstring."""
    blocks = 0
    offenders: list[str] = []
    for rel in _corpus():
        if rel.startswith("docs/adr/"):
            continue
        for kind, no, words in _blocks(rel):
            blocks += 1
            cap = _MAX_BLOCK_WORDS if kind == "block" else _MAX_CELL_WORDS
            if words > cap:
                offenders.append(f"  {rel}:{no}: {kind} of {words} words (cap {cap})")
    assert blocks > 1000, f"premise: expected many blocks, scanned {blocks}"
    assert not offenders, (
        "doc blocks over the length cap — split, tabulate, or move mechanism "
        "into a docstring:\n" + "\n".join(offenders)
    )


def test_features_catalog_shape():
    """Every ``###`` feature under a FEATURES.md category carries a
    ``- **Stability:** <value>`` line, value one of stable / in-progress /
    experimental / planned, and a non-empty ``- **Description:**`` line
    (the entry format in FEATURES.md's Overview)."""
    rel = "docs/FEATURES.md"
    entries: dict[str, tuple[int, list[str]]] = {}
    category: str | None = None
    current: str | None = None
    for no, line in enumerate(_unfenced(rel), start=1):
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            if level == 2:
                category = None if m.group(2).strip() == "Overview" else m.group(2).strip()
                current = None
            elif level == 3 and category is not None:
                current = f"{category} / {m.group(2).strip()}"
                entries[current] = (no, [])
            elif level <= 3:
                current = None
            continue
        if current is not None:
            entries[current][1].append(line)
    assert len(entries) > 20, f"premise: expected the feature catalog, found {len(entries)} entries"
    stability = re.compile(r"^- \*\*Stability:\*\* (\S+)\s*$")
    description = re.compile(r"^- \*\*Description:\*\* \S")
    offenders: list[str] = []
    for name, (no, body) in entries.items():
        values = [m.group(1) for b in body if (m := stability.match(b))]
        if len(values) != 1 or values[0] not in _STABILITY:
            offenders.append(f"  {rel}:{no} {name}: Stability {values or 'missing'}")
        if not any(description.match(b) for b in body):
            offenders.append(f"  {rel}:{no} {name}: Description missing")
    assert not offenders, (
        f"FEATURES.md entries off the catalog shape (Stability in {_STABILITY}, "
        "plus a Description):\n" + "\n".join(offenders)
    )
