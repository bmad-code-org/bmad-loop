"""`bmad-loop probe-adapter`: collect + sanitize adapter-finalization data.

Finalizing a generic-adapter CLI profile needs facts that live in no doc: the
CLI's exact hook payload shape (field names/casing, whether transcript_path /
session_id / cwd are present), where its transcript lives and in what format,
and the token-usage schema a `usage_parser` must read. This command pulls all of
that and runs it through the audited :mod:`bmad_loop.sanitize` chokepoint, so a
user of any coding CLI can run one command and paste back a clean, content-free
report.

Two strategies, one report shape:

- SCAN (default, zero process launch beyond ``--version``/``--help``): locate the
  newest already-existing transcript by convention, read the declared hook config,
  infer the token schema. Works whenever the user has used the CLI before.
- PROBE (``--probe``, opt-in): in an ephemeral ``mkdtemp`` workspace, register the
  full-payload capture hook for every native event, launch one trivial content-free
  turn in a tmux window, capture each event's complete payload, then tear down. The
  raw capture exists only transiently inside the temp dir, which is ``rmtree``'d in a
  ``finally`` (even on exception / Ctrl-C).
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import sanitize
from .adapters.multiplexer import MultiplexerError, get_multiplexer
from .adapters.profile import CLIProfile
from .install import merge_hooks
from .process_host import get_process_host
from .signals import SignalWatcher
from .tokens import _jsonl_entries, read_usage

# Per-parser transcript-location conventions (from tokens.py docstrings).
TRANSCRIPT_GLOBS = {
    "claude-jsonl": "~/.claude/projects/*/*.jsonl",
    "codex-rollout": "~/.codex/sessions/*/*/*/rollout-*.jsonl",
    "gemini-chat": "~/.gemini/tmp/*/chats/session-*.jsonl",
    "copilot-events": "~/.copilot/session-state/*/events.jsonl",
}
# Fallback family glob keyed by the `cli` name, so a CLI whose usage_parser is
# still "none" (e.g. copilot, freshly added) still gets transcript discovery.
FAMILY_GLOBS = {
    "claude": "~/.claude/projects/*/*.jsonl",
    "codex": "~/.codex/sessions/*/*/*/rollout-*.jsonl",
    "gemini": "~/.gemini/tmp/*/chats/session-*.jsonl",
    "copilot": "~/.copilot/session-state/*/events.jsonl",
}

_TOKEN_KEY_RE = re.compile(
    r"(token|tokens|cached|input|output|prompt|completion|thoughts|usage)", re.I
)

PROBE_HOOK_NAME = "bmad_loop_probe_hook.py"
PROBE_PROMPT = "Reply with exactly: OK"
PROBE_TASK_ID = "probe"
PROBE_GRACE_S = 3.0
MAX_SCHEMA_ENTRIES = 200


# --------------------------------------------------------------- dataclasses


@dataclass
class FlagFinding:
    binary: str
    found: bool
    version: str | None = None  # scrubbed
    help: str | None = None  # scrubbed


@dataclass
class TranscriptFinding:
    glob: str | None = None  # the convention glob used (already ~-relative)
    location: str | None = None  # redacted path of the chosen transcript
    fmt: str | None = None  # "jsonl" | "json"
    size_bytes: int | None = None
    line_count: int | None = None
    mtime_date: str | None = None  # date only (no time), UTC
    multiple: bool = False
    note: str | None = None
    real_path: Path | None = None  # NOT rendered; used for schema inference


@dataclass
class TokenSchema:
    parser: str
    entries_scanned: int = 0
    parsed_usage: dict | None = None  # only when parser != "none"
    key_paths: list[str] = field(default_factory=list)  # "a.b.c:int", TYPE only
    token_field_candidates: list[str] = field(default_factory=list)


@dataclass
class EventCapture:
    native_event: str
    canonical_event: str | None
    payload_keys: list[str]
    payload: dict  # scrubbed


@dataclass
class ProfileFinding:
    cli: str
    mode: str  # "scan" | "probe"
    known_profile: bool
    binary: str
    parser: str
    dialect: str | None = None
    flags: FlagFinding | None = None
    declared_events: dict = field(default_factory=dict)  # native -> canonical
    registered: bool | None = None  # scan: hooks present in the CLI's config?
    captured_events: list[EventCapture] = field(default_factory=list)  # probe
    transcript: TranscriptFinding | None = None
    tokens: TokenSchema | None = None
    warnings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


@dataclass
class Hints:
    binary: str | None = None
    transcript: str | None = None
    session_dir: str | None = None
    model: str | None = None


# ------------------------------------------------------------ version / help


def _run_capture(argv: list[str], timeout_s: float) -> str | None:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip() or None


def run_version_help(binary: str, timeout_s: float = 10) -> FlagFinding:
    """Scrubbed ``--version``/``--help`` for a binary. Never raises."""
    if not shutil.which(binary):
        return FlagFinding(binary=binary, found=False)
    version = _run_capture([binary, "--version"], timeout_s)
    help_txt = _run_capture([binary, "--help"], timeout_s)
    return FlagFinding(
        binary=binary,
        found=True,
        version=sanitize.scrub_text(version, max_lines=5) if version else None,
        help=sanitize.scrub_text(help_txt, max_lines=80) if help_txt else None,
    )


# ------------------------------------------------------ transcript discovery


def _redact_location(path: Path) -> str:
    """Redact a path to a paste-safe form: home -> ``~``, and any path component
    that isn't a plain machine identifier (e.g. a munged-cwd dir that embeds a
    username) -> ``<redacted>``. The session-id filename usually survives."""

    def comp(c: str) -> str:
        return c if sanitize.looks_like_identifier(c) else "<redacted>"

    home = Path(os.path.expanduser("~"))
    try:
        rel = path.relative_to(home)
        return "/".join(["~", *(comp(c) for c in rel.parts)])
    except ValueError:
        parts = [comp(c) for c in path.parts if c not in ("/", "")]
        return "/" + "/".join(parts)


def _describe_transcript(path: Path, *, glob_pat: str | None, multiple: bool) -> TranscriptFinding:
    try:
        stat = path.stat()
        size = stat.st_size
        mtime_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        size, mtime_date = None, None
    line_count = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            line_count = sum(1 for _ in f)
    except OSError:
        pass
    return TranscriptFinding(
        glob=glob_pat,
        location=_redact_location(path),
        fmt="jsonl" if path.suffix == ".jsonl" else (path.suffix.lstrip(".") or "unknown"),
        size_bytes=size,
        line_count=line_count,
        mtime_date=mtime_date,
        multiple=multiple,
        real_path=path,
    )


def _newest(paths: list[Path]) -> Path:
    return max(paths, key=lambda p: p.stat().st_mtime if p.exists() else 0)


def discover_transcript(
    parser: str,
    *,
    cli: str,
    hints: Hints,
) -> TranscriptFinding | None:
    """Locate the newest existing transcript via override or convention glob."""
    if hints.transcript:
        path = Path(hints.transcript).expanduser()
        if not path.is_file():
            return TranscriptFinding(note=f"--transcript path does not exist: {path.name}")
        return _describe_transcript(path, glob_pat=None, multiple=False)

    if hints.session_dir:
        base = Path(hints.session_dir).expanduser()
        matches = sorted(base.glob("**/*.jsonl")) or sorted(base.glob("**/*.json"))
        if not matches:
            return TranscriptFinding(note=f"no *.jsonl/*.json under --session-dir {base.name}")
        return _describe_transcript(_newest(matches), glob_pat=None, multiple=len(matches) > 1)

    pattern = TRANSCRIPT_GLOBS.get(parser) or FAMILY_GLOBS.get(cli)
    if not pattern:
        return TranscriptFinding(
            note="no transcript-location convention for this CLI; "
            "pass --transcript PATH or --session-dir DIR"
        )
    matches = [Path(p) for p in glob.glob(os.path.expanduser(pattern))]
    matches = [p for p in matches if p.is_file()]
    if not matches:
        return TranscriptFinding(
            glob=pattern,
            note="no existing transcript matched the convention glob; "
            "use --transcript / --session-dir, or run --probe",
        )
    return _describe_transcript(_newest(matches), glob_pat=pattern, multiple=len(matches) > 1)


# ---------------------------------------------------------- schema inference


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "other"


def _walk_paths(obj, prefix: str, out: set[str]) -> None:
    """Collect dotted key paths with the LEAF TYPE only (never values); list
    indices collapse to ``[]`` so ``messages[].tokens.input:int`` is one path.

    A dict key that isn't a plain identifier (e.g. a transcript that keys by
    relative file path or a per-file backup id) is collapsed to ``<key>`` —
    static field names (the ones a parser keys on, like ``input_tokens``) survive
    untouched, but dynamic keys can't leak paths/content into the summary."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            key = str(key) if sanitize.looks_like_identifier(str(key)) else "<key>"
            child = f"{prefix}.{key}" if prefix else key
            _walk_paths(value, child, out)
    elif isinstance(obj, list):
        child = f"{prefix}[]"
        for value in obj:
            _walk_paths(value, child, out)
    else:
        out.add(f"{prefix}:{_type_name(obj)}")


def _is_token_candidate(path: str) -> bool:
    name, _, typ = path.rpartition(":")
    if typ != "int":
        return False
    last = name.split(".")[-1].replace("[]", "")
    return bool(_TOKEN_KEY_RE.search(last))


def infer_token_schema(
    parser: str, path: Path, *, max_entries: int = MAX_SCHEMA_ENTRIES
) -> TokenSchema:
    """Structural key-path summary (types only) + token-field candidates.

    Works even when ``parser == "none"``: the candidates are exactly what a
    maintainer needs to write a parser for a brand-new CLI. When a real parser
    exists, its parsed integer counts are included as a self-check.
    """
    paths: set[str] = set()
    scanned = 0
    for entry in _jsonl_entries(path):
        if scanned >= max_entries:
            break
        scanned += 1
        _walk_paths(entry, "", paths)
    candidates = sorted(p for p in paths if _is_token_candidate(p))
    parsed = None
    if parser != "none":
        usage = read_usage(parser, path)
        if usage is not None:
            parsed = usage.to_dict()
    return TokenSchema(
        parser=parser,
        entries_scanned=scanned,
        parsed_usage=parsed,
        key_paths=sorted(paths),
        token_field_candidates=candidates,
    )


# --------------------------------------------------------------- hook config


def _hooks_registered(project: Path, profile: CLIProfile) -> bool:
    config_path = project / profile.hooks.config_path
    if not config_path.is_file():
        return False
    import json

    try:
        hooks = json.loads(config_path.read_text(encoding="utf-8")).get("hooks", {})
    except (json.JSONDecodeError, OSError):
        return False
    return any(
        "bmad_loop_hook" in json.dumps(hooks.get(event, [])) for event in profile.hooks.events
    )


# ----------------------------------------------------------------- SCAN mode


def scan(
    *,
    cli: str,
    profile: CLIProfile | None,
    project: Path,
    hints: Hints,
) -> ProfileFinding:
    binary = hints.binary or (profile.binary if profile else cli)
    parser = profile.usage_parser if profile else "none"
    finding = ProfileFinding(
        cli=cli,
        mode="scan",
        known_profile=profile is not None,
        binary=binary,
        parser=parser,
        dialect=profile.hooks.dialect if profile else None,
        declared_events=dict(profile.hooks.events) if profile else {},
    )

    finding.flags = run_version_help(binary)
    if not finding.flags.found:
        finding.warnings.append(
            f"binary {binary!r} not found on PATH — version/help unavailable "
            "(scan continues from on-disk conventions)"
        )

    if profile is not None:
        finding.registered = _hooks_registered(project, profile)
        if not finding.registered:
            finding.next_steps.append(
                f"hooks not registered in {profile.hooks.config_path}; "
                f"`bmad-loop init --cli {cli}` to validate the dialect end-to-end, "
                "or re-run with --probe"
            )

    finding.transcript = discover_transcript(parser, cli=cli, hints=hints)
    if finding.transcript and finding.transcript.note:
        finding.warnings.append(finding.transcript.note)
    if finding.transcript and finding.transcript.real_path is not None:
        finding.tokens = infer_token_schema(parser, finding.transcript.real_path)
        if finding.transcript.multiple:
            finding.next_steps.append(
                "multiple fresh transcripts matched; pass --transcript to pin the right one"
            )
    return finding


# ---------------------------------------------------------- PROBE tmux launcher


class _ProbeLauncher:
    """The few multiplexer primitives PROBE needs — deliberately NOT a
    GenericAdapter, which mandates a Policy and story-completion logic irrelevant
    here. Drives the shared backend so PROBE shells out to no multiplexer
    directly."""

    def __init__(self, session_name: str):
        self.session_name = session_name
        self.mux = get_multiplexer()

    def start(self, argv: list[str], env: dict[str, str], cwd: Path, log_file: Path) -> str | None:
        try:
            self.mux.new_session(self.session_name, cwd, 220, 50)
            command = " ".join(shlex.quote(a) for a in argv)
            window_id = self.mux.new_window(self.session_name, PROBE_TASK_ID, cwd, env, command)
        except MultiplexerError:
            return None
        # pipe-pane may race a window that dies instantly; tolerate failure.
        self.mux.pipe_pane(window_id, log_file)
        return window_id

    def window_alive(self, window_id: str) -> bool:
        return self.mux.window_alive(self.session_name, window_id)

    def kill(self) -> None:
        self.mux.kill_session(self.session_name)


def _probe_argv(profile: CLIProfile, binary: str, hints: Hints) -> list[str]:
    argv = [
        binary,
        *profile.launch_args,
        # Send the probe prompt verbatim, NOT through profile.render_prompt: a
        # content-free turn has no skill name, so a skill-templating prompt_template
        # (copilot, codex) would render a nonexistent .../skills//SKILL.md path the
        # agent hunts for, and the turn never ends within the probe timeout.
        PROBE_PROMPT,
        *profile.bypass_args,
    ]
    if hints.model:
        argv += [profile.model_flag, hints.model]
    return argv


def _collect_captures(capture_dir: Path, events_map: dict[str, str]) -> list[EventCapture]:
    captures: list[EventCapture] = []
    for payload_file in sorted(capture_dir.glob("*.payload.json")):
        import json

        try:
            raw = json.loads(payload_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict):
            continue
        native = str(raw.pop("argv_event", "Unknown"))
        captures.append(
            EventCapture(
                native_event=native,
                canonical_event=events_map.get(native),
                payload_keys=sorted(raw.keys()),
                payload=sanitize.scrub_event_payload(raw),
            )
        )
    return captures


def probe(
    *,
    cli: str,
    profile: CLIProfile,
    project: Path,
    hints: Hints,
    timeout_s: float = 90,
    keep_temp: bool = False,
) -> ProfileFinding:
    import json

    binary = hints.binary or profile.binary
    finding = ProfileFinding(
        cli=cli,
        mode="probe",
        known_profile=True,
        binary=binary,
        parser=profile.usage_parser,
        dialect=profile.hooks.dialect,
        declared_events=dict(profile.hooks.events),
    )
    finding.flags = run_version_help(binary)

    if not shutil.which("tmux") or not shutil.which(binary):
        missing = "tmux" if not shutil.which("tmux") else binary
        finding.warnings.append(f"{missing} not on PATH — cannot probe; falling back to scan")
        scanned = scan(cli=cli, profile=profile, project=project, hints=hints)
        scanned.mode = "probe"
        return scanned

    tmpdir = Path(tempfile.mkdtemp(prefix="bmad-loop-probe-"))
    launcher = _ProbeLauncher(session_name=f"bmad-loop-probe-{tmpdir.name}")
    try:
        capture_dir = tmpdir / "capture"
        capture_dir.mkdir(parents=True, exist_ok=True)

        # 1. lay down the capture hook + a hook config registered through the very
        #    same merge_hooks `bmad-loop init` uses — so a bad dialect surfaces live.
        hook_src = resources.files("bmad_loop.data").joinpath(PROBE_HOOK_NAME)
        hook_path = tmpdir / PROBE_HOOK_NAME
        hook_path.write_text(hook_src.read_text(encoding="utf-8"), encoding="utf-8")
        host = get_process_host()
        interp = host.hook_interpreter()
        registrations = {
            native: f"{interp} {host.shell_quote(str(hook_path))} {canonical}"
            for native, canonical in profile.hooks.events.items()
        }
        config, _ = merge_hooks({}, registrations, profile.hooks.dialect)
        config_path = tmpdir / profile.hooks.config_path
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

        # 2. launch one trivial content-free turn in a fresh tmux window
        argv = _probe_argv(profile, binary, hints)
        env = {
            **profile.env,
            "BMAD_LOOP_RUN_DIR": str(tmpdir),
            "BMAD_LOOP_TASK_ID": PROBE_TASK_ID,
            "BMAD_LOOP_PROBE_CAPTURE_DIR": str(capture_dir),
        }
        log_file = tmpdir / "probe.log"
        watcher = SignalWatcher(capture_dir)
        launched_ns = time.time_ns()
        window_id = launcher.start(argv, env, tmpdir, log_file)
        if window_id is None:
            finding.warnings.append("could not launch the CLI in tmux; no events captured")
            return finding

        # 3. completion: first of — canonical Stop for `probe`; any capture file
        #    appeared and the window died; window died; deadline.
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                finding.warnings.append(
                    "no Stop event before --timeout; the CLI may need first-run auth "
                    "(a pending login dialog reads as a timeout). See the log tail below."
                )
                break
            event = watcher.wait_for(
                PROBE_TASK_ID,
                {"Stop"},
                timeout_s=min(remaining, 5.0),
                since_ns=launched_ns,
            )
            if event is not None:
                break
            try:
                alive = launcher.window_alive(window_id)
            except MultiplexerError:
                # transient transport hang is not proof the window died; retry
                # on the next tick rather than mis-reporting a dead CLI window.
                continue
            captured_any = any(capture_dir.glob("*.payload.json"))
            if not alive:
                if not captured_any:
                    finding.warnings.append(
                        "the CLI window died before any hook fired — the dialect may be "
                        f"rejected for {profile.hooks.dialect}, or launch/auth failed. "
                        "See the log tail below."
                    )
                break

        # 4. one short grace poll so a Stop's sibling files all land, then collect.
        time.sleep(PROBE_GRACE_S)
        finding.captured_events = _collect_captures(capture_dir, profile.hooks.events)
        if not finding.captured_events:
            finding.next_steps.append(
                "no hook payloads captured — confirm the CLI is authenticated and that "
                f"the {profile.hooks.dialect} hook config is accepted, then re-run --probe"
            )
            tail = _log_tail(log_file)
            if tail:
                finding.warnings.append("log tail (scrubbed):\n" + tail)

        # 5. transcript discovery + schema inference from the user's real home
        finding.transcript = discover_transcript(profile.usage_parser, cli=cli, hints=hints)
        if finding.transcript and finding.transcript.note:
            finding.warnings.append(finding.transcript.note)
        if finding.transcript and finding.transcript.real_path is not None:
            finding.tokens = infer_token_schema(profile.usage_parser, finding.transcript.real_path)
        return finding
    finally:
        launcher.kill()
        if keep_temp:
            finding.warnings.append(
                f"--keep-temp: RAW probe data retained at {tmpdir} — DO NOT SHARE; "
                "delete it after inspection"
            )
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _log_tail(log_file: Path, max_lines: int = 20) -> str | None:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    lines = text.splitlines()[-max_lines:]
    return sanitize.scrub_text("\n".join(lines), max_lines=max_lines)


# ------------------------------------------------------------------ rendering


def _fmt_kv(label: str, value) -> str:
    return f"- **{label}:** {value}"


def render_markdown(f: ProfileFinding) -> str:
    out: list[str] = []
    out.append(f"# Profile finalize report — {f.cli} ({f.mode})")
    out.append("")

    # Summary
    out.append("## Summary")
    out.append(_fmt_kv("CLI", f.cli))
    out.append(
        _fmt_kv("binary", f"{f.binary} ({'found' if f.flags and f.flags.found else 'NOT found'})")
    )
    out.append(_fmt_kv("known profile", "yes" if f.known_profile else "no (reduced report)"))
    out.append(_fmt_kv("hook dialect", f.dialect or "—"))
    out.append(_fmt_kv("usage_parser", f.parser))
    if f.registered is not None:
        out.append(_fmt_kv("hooks registered", "yes" if f.registered else "no"))
    out.append(_fmt_kv("warnings", str(len(f.warnings))))
    out.append("")

    # CLI flags
    out.append("## CLI flags")
    out.append(_fmt_kv("launch_args / bypass_args", "see profile (rendered verbatim below)"))
    if f.flags and f.flags.version:
        out.append("\n```\n" + f.flags.version + "\n```")
    if f.flags and f.flags.help:
        out.append("\n<details><summary>--help (scrubbed)</summary>\n")
        out.append("```\n" + f.flags.help + "\n```")
        out.append("</details>")
    if not f.flags or not f.flags.found:
        out.append("_binary not available; flags/help not captured._")
    out.append("")

    # Hook payload shape
    out.append("## Hook payload shape")
    if f.mode == "scan":
        if f.declared_events:
            out.append(
                "Declared native → canonical events (registered = "
                f"{'yes' if f.registered else 'no'}):"
            )
            for native, canonical in f.declared_events.items():
                out.append(f"- `{native}` → `{canonical}`")
        else:
            out.append("_no profile; events unknown. Re-run with --probe to capture payloads._")
    else:
        if f.captured_events:
            for ev in f.captured_events:
                out.append(f"### `{ev.native_event}` → `{ev.canonical_event or '?'}`")
                out.append(
                    _fmt_kv("payload keys", ", ".join(f"`{k}`" for k in ev.payload_keys) or "—")
                )
                out.append("\n```json\n" + _json_dump(ev.payload) + "\n```")
        else:
            out.append("_no hook payloads captured (see warnings)._")
    out.append("")

    # Transcript
    out.append("## Transcript")
    t = f.transcript
    if t and t.real_path is not None:
        out.append(_fmt_kv("location", f"`{t.location}`"))
        if t.glob:
            out.append(_fmt_kv("matched glob", f"`{t.glob}`"))
        out.append(_fmt_kv("format", t.fmt))
        out.append(_fmt_kv("size", f"{t.size_bytes} bytes"))
        out.append(_fmt_kv("lines", t.line_count))
        out.append(_fmt_kv("mtime", t.mtime_date))
        if t.multiple:
            out.append("- _multiple candidates matched; newest shown — pass --transcript to pin._")
    else:
        out.append("_no transcript located._" + (f" ({t.note})" if t and t.note else ""))
    out.append("")

    # Token usage schema
    out.append("## Token usage schema")
    tk = f.tokens
    if tk:
        out.append(_fmt_kv("declared parser", tk.parser))
        out.append(_fmt_kv("entries scanned", tk.entries_scanned))
        if tk.parsed_usage is not None:
            out.append(_fmt_kv("parsed counts (self-check)", f"`{tk.parsed_usage}`"))
        out.append(
            "\n**Token-field candidates** (int leaves; per-call-vs-cumulative is a human call):"
        )
        if tk.token_field_candidates:
            for cand in tk.token_field_candidates:
                out.append(f"- `{cand}`")
        else:
            out.append("- _none matched the token-name heuristic._")
        out.append("\n<details><summary>All key paths (types only, no values)</summary>\n")
        out.append("```\n" + "\n".join(tk.key_paths) + "\n```")
        out.append("</details>")
    else:
        out.append("_no transcript to infer from._")
    out.append("")

    # Warnings / next steps
    out.append("## Warnings / next steps")
    if not f.warnings and not f.next_steps:
        out.append("_none._")
    for w in f.warnings:
        out.append(f"- ⚠️ {w}")
    for s in f.next_steps:
        out.append(f"- → {s}")
    out.append("")
    return "\n".join(out)


def _json_dump(obj) -> str:
    import json

    return json.dumps(obj, indent=2, sort_keys=True)


def render_json(f: ProfileFinding) -> str:
    import json

    def transcript_dict(t: TranscriptFinding | None):
        if t is None:
            return None
        return {
            "glob": t.glob,
            "location": t.location,
            "format": t.fmt,
            "size_bytes": t.size_bytes,
            "line_count": t.line_count,
            "mtime_date": t.mtime_date,
            "multiple": t.multiple,
            "note": t.note,
        }

    data = {
        "cli": f.cli,
        "mode": f.mode,
        "known_profile": f.known_profile,
        "binary": f.binary,
        "binary_found": bool(f.flags and f.flags.found),
        "dialect": f.dialect,
        "usage_parser": f.parser,
        "hooks_registered": f.registered,
        "declared_events": f.declared_events,
        "version": f.flags.version if f.flags else None,
        "help": f.flags.help if f.flags else None,
        "captured_events": [
            {
                "native_event": ev.native_event,
                "canonical_event": ev.canonical_event,
                "payload_keys": ev.payload_keys,
                "payload": ev.payload,
            }
            for ev in f.captured_events
        ],
        "transcript": transcript_dict(f.transcript),
        "tokens": (
            {
                "parser": f.tokens.parser,
                "entries_scanned": f.tokens.entries_scanned,
                "parsed_usage": f.tokens.parsed_usage,
                "key_paths": f.tokens.key_paths,
                "token_field_candidates": f.tokens.token_field_candidates,
            }
            if f.tokens
            else None
        ),
        "warnings": f.warnings,
        "next_steps": f.next_steps,
    }
    return json.dumps(data, indent=2)
