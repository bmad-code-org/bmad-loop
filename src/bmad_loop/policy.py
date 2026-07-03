"""Policy-as-data: .bmad-loop/policy.toml -> immutable Policy dataclasses."""

from __future__ import annotations

import tomllib
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

POLICY_FILE = Path(".bmad-loop") / "policy.toml"

GATE_MODES = {"none", "per-epic", "per-story-spec-approval"}
RETRO_MODES = {"never", "notify", "auto"}
SWEEP_AUTO_MODES = {"never", "per-epic", "run-end"}
REVIEW_TRIGGER_MODES = {"always", "recommended"}
ISOLATION_MODES = {"none", "worktree"}
BRANCH_PER_MODES = {"story", "run"}
MERGE_STRATEGIES = {"ff", "merge", "squash"}
DEV_SKILLS = {"bmad-dev-auto"}

# Deprecated [engine] keys, folded into [plugins.unity] at load time. The
# game-engine layer is now a plugin; [engine] is a one-release compatibility
# alias (see _fold_deprecated_engine).
_ENGINE_SETTING_KEYS = ("editor_mode", "mcp", "unity_path", "ready_timeout_sec", "ready_grace_sec")


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class GatesPolicy:
    mode: str = "per-epic"
    on_escalation: str = "pause"  # CRITICAL escalations always pause; field reserved
    retrospective: str = "notify"


@dataclass(frozen=True)
class LimitsPolicy:
    max_review_cycles: int = 3
    max_dev_attempts: int = 2
    session_timeout_min: int = 90
    stop_without_result_nudges: int = 1
    # how long a dev session may sit on a result-less Stop — i.e. it ended its
    # turn awaiting a long-running background process (a Unity PlayMode run, a
    # slow test) and expects to be re-invoked on completion — before it is
    # declared stalled. The window measures genuine inactivity: any output to the
    # session's pane log (a long productive turn, a streaming subagent) re-arms
    # it, as does a fresh Stop, so only a truly idle gap this long with no
    # terminal spec stalls. Bounded by session_timeout_min. 0 restores the old
    # fail-fast-on-first-Stop behavior.
    dev_stall_grace_s: int = 600
    # how many times an idle dev session is woken with a nudge when the
    # dev_stall_grace_s window elapses with no output — bmad-loop has no
    # background-completion re-invocation, so a session that ended its turn to
    # await a background process is nudged back to life before being called
    # stalled. Fresh pane output re-arms the grace window (an actively streaming
    # session never reaches grace expiry, so never spends a nudge); a fresh Stop
    # additionally restores any spent budget. Either way a cooperative-but-slow
    # session waits up to session_timeout_min; only one that stays silent through
    # the full grace, nudge after nudge, drains the budget and stalls. 0 = stall
    # on grace expiry.
    dev_stall_nudges: int = 2
    # monotonic cap on stall wake-nudges for injected plugin-workflow sessions
    # (SessionSpec.stall_nudges_cap). Dev/review sessions stay uncapped: their
    # nudge budget is deliberately restored on every fresh Stop so a session
    # awaiting a slow background process can wait up to session_timeout_min. A
    # workflow session that keeps ending its turn without writing its completion
    # marker would ride that refill forever; after this many total nudges it is
    # declared stalled instead (non-blocking workflows then advance the phase).
    workflow_stall_nudges_cap: int = 3
    max_tokens_per_story: int = 2_000_000
    # weight of cache-read tokens in the budget check (1.0 = count raw)
    cache_read_weight: float = 0.1


@dataclass(frozen=True)
class VerifyPolicy:
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotifyPolicy:
    desktop: bool = True
    file: bool = True


@dataclass(frozen=True)
class ReviewPolicy:
    # When False, the orchestrator runs no follow-up review session; the
    # bmad-dev-auto session's own inline review is the only review and it
    # finalizes the story straight to done.
    enabled: bool = True
    # When (and only when) enabled is True, decides when the follow-up review
    # session (a bmad-dev-auto re-invocation on the done spec) actually runs:
    #   "recommended" (default) — only when the bmad-dev-auto session set
    #       `followup_review_recommended: true` in the spec frontmatter. The
    #       skill self-reviews inline on every story and flags this when its
    #       review-driven changes were significant enough to warrant an
    #       independent second opinion. Otherwise the deterministic gates run
    #       and the story commits without a second review session.
    #   "always" — run the second-opinion review on every story (pre-PR-#2505
    #       behavior). The skill's recommendation flag is recorded but ignored.
    # Either way the review loop is bounded by limits.max_review_cycles, which is
    # the oscillation guard for orchestrator-applied follow-up review.
    trigger: str = "recommended"


@dataclass(frozen=True)
class DevPolicy:
    # Which inner dev skill the orchestrator drives. The sole supported value is
    # "bmad-dev-auto", the generic upstream dev primitive (BMAD-METHOD PR #2500):
    # it writes no result.json — the GenericDevAdapter synthesizes one from the
    # spec the session leaves on disk. The field is retained (rather than inlined)
    # as the seam for a future alternative dev skill; see DEV_SKILLS.
    skill: str = "bmad-dev-auto"


@dataclass(frozen=True)
class TuiPolicy:
    # low_frame_rate caps Textual to 15fps and disables animations (sets
    # TEXTUAL_FPS / TEXTUAL_ANIMATIONS before the app imports textual). Fixes
    # repaint tearing/garbage when driving the TUI over a slow/high-latency
    # link (SSH, Tailscale) where a 60fps update stream can't drain in time.
    low_frame_rate: bool = False


@dataclass(frozen=True)
class SweepPolicy:
    auto: str = "never"  # never | per-epic | run-end
    max_bundles: int = 5  # bundles executed per sweep; triage excess is truncated
    max_triage_attempts: int = 2
    max_migration_attempts: int = 2  # legacy-ledger migration retries before escalating
    repeat: bool = False  # re-triage after a cycle completes; continue on new deferred work
    max_cycles: int = 5  # total cycles per sweep run when repeat is on


@dataclass(frozen=True)
class CleanupPolicy:
    """Disk reclamation for `.bmad-loop/runs`. Worktree reconcile + artifact
    trim only ever touch terminal (finished/stopped) runs — paused/interrupted
    runs stay intact so they remain resumable."""

    run_retention: int = 10  # newest concluded runs kept whole; older ones trimmed/archived
    retention_days: int = 0  # 0 = disabled; else also keep runs newer than N days
    trim_artifacts: bool = True  # drop the worktrees/ tree from concluded runs (keeps run viewable)
    archive_old: bool = True  # archive (vs hard-delete) runs past the window
    auto_clean_on_finish: bool = True  # reconcile stale worktrees + retention at clean finish
    clean_tmp: bool = True  # let engine plugins clean their /tmp scratch (e.g. Unity MCP zips)


@dataclass(frozen=True)
class StageAdapterPolicy:
    """Per-stage overrides; None = inherit from [adapter]."""

    name: str | None = None
    model: str | None = None
    extra_args: tuple[str, ...] | None = None
    # None = inherit from [adapter] (which itself falls back to the CLI profile)
    usage_grace_s: float | None = None
    stop_without_result_nudges: int | None = None


@dataclass(frozen=True)
class ResolvedAdapter:
    name: str
    model: str
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None
    # None = fall back to the CLI profile's default (usage_grace_s) / the global
    # limits.stop_without_result_nudges respectively
    usage_grace_s: float | None = None
    stop_without_result_nudges: int | None = None


@dataclass(frozen=True)
class AdapterPolicy:
    name: str = "claude"  # CLI profile name; "claude-code-tmux" kept as legacy alias
    model: str = ""
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None = None
    # kill the run's bmad-loop-<id> tmux session when it finishes (False keeps
    # it around for post-run inspection)
    cleanup_session_on_finish: bool = True
    # None = inherit from the selected CLI profile / global limits (see
    # ResolvedAdapter); a value overrides the profile's shipped default.
    usage_grace_s: float | None = None
    stop_without_result_nudges: int | None = None
    dev: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)
    review: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)
    triage: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)

    def resolved(self, role: str) -> ResolvedAdapter:
        stage = {"dev": self.dev, "review": self.review, "triage": self.triage}.get(role)
        if stage is None:
            return ResolvedAdapter(
                self.name,
                self.model,
                self.extra_args,
                self.usage_grace_s,
                self.stop_without_result_nudges,
            )
        name = stage.name if stage.name is not None else self.name
        # model and extra_args are client-specific: inherit from the base only
        # when the stage runs the same client; a client switch falls back to
        # that profile's defaults (CLI default model, profile bypass flags).
        same_client = name == self.name
        # usage_grace_s / stop_without_result_nudges are benign timing knobs that
        # mean "fall back to the profile default" when None, so plain stage ??
        # base inheritance is safe regardless of a client switch.
        return ResolvedAdapter(
            name=name,
            model=(stage.model if stage.model is not None else (self.model if same_client else "")),
            extra_args=(
                stage.extra_args
                if stage.extra_args is not None
                else (self.extra_args if same_client else None)
            ),
            usage_grace_s=(
                stage.usage_grace_s if stage.usage_grace_s is not None else self.usage_grace_s
            ),
            stop_without_result_nudges=(
                stage.stop_without_result_nudges
                if stage.stop_without_result_nudges is not None
                else self.stop_without_result_nudges
            ),
        )


@dataclass(frozen=True)
class ScmPolicy:
    # isolation = none  -> work happens in place on the checked-out branch
    #                      (today's behavior; no branches, no merge-back).
    # isolation = worktree -> each unit runs in its own git worktree/branch and
    #                      merges back into target_branch locally (Phase 3).
    isolation: str = "none"  # none | worktree
    branch_per: str = "story"  # story | run (worktree mode only)
    target_branch: str = ""  # "" = the branch checked out at run start
    merge_strategy: str = "merge"  # ff | merge | squash
    delete_branch: bool = True  # delete the unit branch after a successful merge
    keep_failed: bool = True  # keep a failed unit's worktree+branch for inspection
    # rollback_on_failure governs in-place (isolation = "none") recovery after a
    # failed attempt / rejected review. Default OFF: the orchestrator never
    # touches the working tree — it pauses the run with manual recovery
    # instructions, so a half-finished attempt is left for you to inspect. ON:
    # the orchestrator auto-reverts the attempt's tracked changes and removes the
    # untracked files THIS run created (never a blanket `git clean`; pre-existing
    # untracked files and the whole _bmad-output/ are preserved) — convenient but
    # it discards the attempt's uncommitted work, so a warning is journalled when
    # it fires. Worktree isolation sidesteps this entirely (failed work stays in
    # its worktree), so this knob only matters for isolation = "none". This flag
    # governs unattended/stopped attempts only: a human-initiated escalation
    # resolve re-drive always auto-recovers regardless — it reverts the failed
    # attempt's source but preserves the corrected spec under the BMAD artifact
    # folders, which it treats as orchestrator-owned.
    rollback_on_failure: bool = False
    # failed_diff_max_mb caps the per-file size (MB) of untracked files captured
    # into a kept-failed unit's forensic changes.patch, so a stray build dir or
    # huge log can't blow it up; oversized files are skipped with a labelled
    # marker in the patch. failed_diff_unlimited lifts the cap entirely (capture
    # everything regardless of size) — convenient but may produce very large
    # patches, so a warning is journalled when it's active.
    failed_diff_max_mb: int = 5
    failed_diff_unlimited: bool = False
    # commit_message_template, when non-empty, is the commit message dev sessions
    # use for a story's commit (placeholders {story_key} and {run_id} are
    # substituted). Empty = the built-in default message.
    commit_message_template: str = ""
    # max_parallel: units in flight at once. Parallel fan-out (Phase 5) is not
    # built yet, so any value > 1 is clamped to 1 in loads() — the knob exists
    # but is inert until the parallel scheduler lands.
    max_parallel: int = 1
    # A `git worktree add` checks out tracked files only, so gitignored MCP/CLI
    # configs are missing from every fresh worktree and isolated sessions can't
    # reach their MCP server. seed_adapter_defaults copies each loaded adapter's
    # own seed_files (e.g. claude -> .mcp.json/.claude/settings.json) into the
    # worktree; worktree_seed adds extra project-specific paths on top.
    seed_adapter_defaults: bool = True
    worktree_seed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # branch_per="run" shares a single branch across every unit in the run;
        # deleting it after the first unit's merge would defeat that (the next
        # unit would re-cut a fresh branch). Coerce delete_branch off so the
        # shared-branch semantics actually hold, regardless of how this policy
        # was constructed.
        if self.branch_per == "run" and self.delete_branch:
            object.__setattr__(self, "delete_branch", False)


@dataclass(frozen=True)
class PluginsPolicy:
    # Trust allowlist for the plugin system. A plugin folder dropped under
    # .bmad-loop/plugins/ (or shipped under bmad_loop/data/plugins/) loads its
    # declarative manifest — settings + out-of-process shell hooks — regardless.
    # A plugin that declares an in-process [python] module is NEVER imported or
    # executed unless its name appears here. Absent table = no plugins trusted,
    # which reproduces today's behavior exactly.
    enabled: tuple[str, ...] = ()
    # Per-plugin settings, parsed from the [plugins.<name>] sub-tables. Each
    # value is the raw settings dict for that plugin; the plugin's own schema
    # gives the keys meaning. Read through Policy.plugin_setting(). A plugin
    # need not be in `enabled` to carry settings here (settings are data, only
    # in-process [python] is trust-gated), but the settings UI renders a
    # plugin's section only when it is enabled.
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    gates: GatesPolicy = field(default_factory=GatesPolicy)
    limits: LimitsPolicy = field(default_factory=LimitsPolicy)
    verify: VerifyPolicy = field(default_factory=VerifyPolicy)
    notify: NotifyPolicy = field(default_factory=NotifyPolicy)
    review: ReviewPolicy = field(default_factory=ReviewPolicy)
    dev: DevPolicy = field(default_factory=DevPolicy)
    adapter: AdapterPolicy = field(default_factory=AdapterPolicy)
    sweep: SweepPolicy = field(default_factory=SweepPolicy)
    scm: ScmPolicy = field(default_factory=ScmPolicy)
    cleanup: CleanupPolicy = field(default_factory=CleanupPolicy)
    plugins: PluginsPolicy = field(default_factory=PluginsPolicy)
    tui: TuiPolicy = field(default_factory=TuiPolicy)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def plugin_setting(self, name: str, key: str, default: Any = None) -> Any:
        """A single setting for plugin ``name`` from its [plugins.<name>] table,
        or ``default`` when unset. The plugin's schema supplies the real default
        when this is called with the schema default as ``default``."""
        return self.plugins.settings.get(name, {}).get(key, default)


def _section(doc: dict[str, Any], name: str) -> dict[str, Any]:
    value = doc.get(name, {})
    if not isinstance(value, dict):
        raise PolicyError(f"[{name}] must be a table")
    return value


def _opt_grace(d: dict[str, Any], where: str) -> float | None:
    raw = d.get("usage_grace_s")
    if raw is None:
        return None
    value = float(raw)
    if value < 0:
        raise PolicyError(f"{where}.usage_grace_s must be >= 0: got {value}")
    return value


def _opt_nudges(d: dict[str, Any], where: str) -> int | None:
    raw = d.get("stop_without_result_nudges")
    if raw is None:
        return None
    value = int(raw)
    if value < 0:
        raise PolicyError(f"{where}.stop_without_result_nudges must be >= 0: got {value}")
    return value


def _stage_adapter(adapter_d: dict[str, Any], key: str) -> StageAdapterPolicy:
    raw = adapter_d.get(key, {})
    if not isinstance(raw, dict):
        raise PolicyError(f"[adapter.{key}] must be a table")
    raw_extra = raw.get("extra_args")
    return StageAdapterPolicy(
        name=None if raw.get("name") is None else str(raw["name"]),
        model=None if raw.get("model") is None else str(raw["model"]),
        extra_args=None if raw_extra is None else tuple(str(a) for a in raw_extra),
        usage_grace_s=_opt_grace(raw, f"adapter.{key}"),
        stop_without_result_nudges=_opt_nudges(raw, f"adapter.{key}"),
    )


def _validate_plugin_settings(name: str, raw: dict[str, Any], specs: Any) -> None:
    """Validate a [plugins.<name>] table against its plugin's setting specs
    (objects exposing key/type/options). Unknown keys and type/option mismatches
    raise PolicyError; a None schema means the plugin isn't loaded here, skip."""
    if specs is None:
        return
    by_key = {s.key: s for s in specs}
    for key, value in raw.items():
        spec = by_key.get(key)
        if spec is None:
            raise PolicyError(f"plugins.{name}: unknown setting {key!r}")
        kind = spec.type
        if kind == "bool" and not isinstance(value, bool):
            raise PolicyError(f"plugins.{name}.{key} must be a boolean")
        # bool is a subclass of int; reject it explicitly for numeric kinds.
        if kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise PolicyError(f"plugins.{name}.{key} must be an integer")
        if kind == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise PolicyError(f"plugins.{name}.{key} must be a number")
        if kind == "str" and not isinstance(value, str):
            raise PolicyError(f"plugins.{name}.{key} must be a string")
        if kind == "select" and value not in spec.options:
            raise PolicyError(
                f"plugins.{name}.{key} must be one of {list(spec.options)}: got {value!r}"
            )


def load(path: Path | None) -> Policy:
    """Load policy from a TOML file; a missing file yields all defaults."""
    if path is None or not path.is_file():
        return loads("")
    try:
        return loads(path.read_text(encoding="utf-8"))
    except PolicyError as e:
        raise PolicyError(f"{path}: {e}") from e


def loads(text: str, plugin_schemas: dict[str, Any] | None = None) -> Policy:
    """Parse and validate policy TOML text; empty text yields all defaults.

    ``plugin_schemas`` optionally maps a plugin name to its sequence of setting
    specs (objects with ``key``/``type``/``options`` attributes). When given,
    every present ``[plugins.<name>]`` table whose plugin is in the mapping is
    validated against that schema: unknown keys and type/option mismatches raise
    PolicyError. Plugin tables without a supplied schema pass through untouched
    (a plugin may not be loaded in every context that reads policy)."""
    try:
        doc: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"invalid policy TOML: {e}") from e

    gates_d = _section(doc, "gates")
    limits_d = _section(doc, "limits")
    verify_d = _section(doc, "verify")
    notify_d = _section(doc, "notify")
    review_d = _section(doc, "review")
    dev_d = _section(doc, "dev")
    adapter_d = _section(doc, "adapter")
    sweep_d = _section(doc, "sweep")
    scm_d = _section(doc, "scm")
    cleanup_d = _section(doc, "cleanup")
    engine_d = _section(doc, "engine")  # deprecated; folded into [plugins] below
    plugins_d = _section(doc, "plugins")
    tui_d = _section(doc, "tui")

    gates = GatesPolicy(
        mode=str(gates_d.get("mode", GatesPolicy.mode)),
        on_escalation=str(gates_d.get("on_escalation", GatesPolicy.on_escalation)),
        retrospective=str(gates_d.get("retrospective", GatesPolicy.retrospective)),
    )
    if gates.mode not in GATE_MODES:
        raise PolicyError(f"gates.mode must be one of {sorted(GATE_MODES)}: got {gates.mode!r}")
    if gates.retrospective not in RETRO_MODES:
        raise PolicyError(
            f"gates.retrospective must be one of {sorted(RETRO_MODES)}: got {gates.retrospective!r}"
        )

    limits = LimitsPolicy(
        max_review_cycles=int(limits_d.get("max_review_cycles", LimitsPolicy.max_review_cycles)),
        max_dev_attempts=int(limits_d.get("max_dev_attempts", LimitsPolicy.max_dev_attempts)),
        session_timeout_min=int(
            limits_d.get("session_timeout_min", LimitsPolicy.session_timeout_min)
        ),
        stop_without_result_nudges=int(
            limits_d.get("stop_without_result_nudges", LimitsPolicy.stop_without_result_nudges)
        ),
        dev_stall_grace_s=int(limits_d.get("dev_stall_grace_s", LimitsPolicy.dev_stall_grace_s)),
        dev_stall_nudges=int(limits_d.get("dev_stall_nudges", LimitsPolicy.dev_stall_nudges)),
        workflow_stall_nudges_cap=int(
            limits_d.get("workflow_stall_nudges_cap", LimitsPolicy.workflow_stall_nudges_cap)
        ),
        max_tokens_per_story=int(
            limits_d.get("max_tokens_per_story", LimitsPolicy.max_tokens_per_story)
        ),
        cache_read_weight=float(limits_d.get("cache_read_weight", LimitsPolicy.cache_read_weight)),
    )
    if limits.max_review_cycles < 1 or limits.max_dev_attempts < 1:
        raise PolicyError("limits.max_review_cycles and limits.max_dev_attempts must be >= 1")
    if not 0.0 <= limits.cache_read_weight <= 1.0:
        raise PolicyError(
            f"limits.cache_read_weight must be between 0 and 1: got {limits.cache_read_weight}"
        )
    if limits.dev_stall_grace_s < 0:
        raise PolicyError(f"limits.dev_stall_grace_s must be >= 0: got {limits.dev_stall_grace_s}")
    if limits.dev_stall_nudges < 0:
        raise PolicyError(f"limits.dev_stall_nudges must be >= 0: got {limits.dev_stall_nudges}")
    if limits.workflow_stall_nudges_cap < 0:
        raise PolicyError(
            f"limits.workflow_stall_nudges_cap must be >= 0: got {limits.workflow_stall_nudges_cap}"
        )

    verify = VerifyPolicy(commands=tuple(str(c) for c in verify_d.get("commands", ())))
    notify = NotifyPolicy(
        desktop=bool(notify_d.get("desktop", NotifyPolicy.desktop)),
        file=bool(notify_d.get("file", NotifyPolicy.file)),
    )
    review = ReviewPolicy(
        enabled=bool(review_d.get("enabled", ReviewPolicy.enabled)),
        trigger=str(review_d.get("trigger", ReviewPolicy.trigger)).strip(),
    )
    if review.trigger not in REVIEW_TRIGGER_MODES:
        raise PolicyError(
            f"review.trigger must be one of {sorted(REVIEW_TRIGGER_MODES)}: got {review.trigger!r}"
        )
    dev = DevPolicy(skill=str(dev_d.get("skill", DevPolicy.skill)))
    if dev.skill not in DEV_SKILLS:
        raise PolicyError(f"dev.skill must be one of {sorted(DEV_SKILLS)}: got {dev.skill!r}")
    for legacy, replacement in (
        ("model_dev", "[adapter.dev] model"),
        ("model_review", "[adapter.review] model"),
    ):
        if legacy in adapter_d:
            raise PolicyError(f"adapter.{legacy} was removed — use {replacement} instead")
    raw_extra = adapter_d.get("extra_args")
    adapter = AdapterPolicy(
        name=str(adapter_d.get("name", AdapterPolicy.name)),
        model=str(adapter_d.get("model", AdapterPolicy.model)),
        extra_args=None if raw_extra is None else tuple(str(a) for a in raw_extra),
        cleanup_session_on_finish=bool(
            adapter_d.get("cleanup_session_on_finish", AdapterPolicy.cleanup_session_on_finish)
        ),
        usage_grace_s=_opt_grace(adapter_d, "adapter"),
        stop_without_result_nudges=_opt_nudges(adapter_d, "adapter"),
        dev=_stage_adapter(adapter_d, "dev"),
        review=_stage_adapter(adapter_d, "review"),
        triage=_stage_adapter(adapter_d, "triage"),
    )
    sweep = SweepPolicy(
        auto=str(sweep_d.get("auto", SweepPolicy.auto)),
        max_bundles=int(sweep_d.get("max_bundles", SweepPolicy.max_bundles)),
        max_triage_attempts=int(
            sweep_d.get("max_triage_attempts", SweepPolicy.max_triage_attempts)
        ),
        max_migration_attempts=int(
            sweep_d.get("max_migration_attempts", SweepPolicy.max_migration_attempts)
        ),
        repeat=bool(sweep_d.get("repeat", SweepPolicy.repeat)),
        max_cycles=int(sweep_d.get("max_cycles", SweepPolicy.max_cycles)),
    )
    if sweep.auto not in SWEEP_AUTO_MODES:
        raise PolicyError(
            f"sweep.auto must be one of {sorted(SWEEP_AUTO_MODES)}: got {sweep.auto!r}"
        )
    if (
        min(
            sweep.max_bundles,
            sweep.max_triage_attempts,
            sweep.max_migration_attempts,
            sweep.max_cycles,
        )
        < 1
    ):
        raise PolicyError(
            "sweep.max_bundles, sweep.max_triage_attempts, "
            "sweep.max_migration_attempts and sweep.max_cycles must be >= 1"
        )
    requested_parallel = int(scm_d.get("max_parallel", ScmPolicy.max_parallel))
    if requested_parallel < 1:
        raise PolicyError(f"scm.max_parallel must be >= 1: got {requested_parallel}")
    scm = ScmPolicy(
        isolation=str(scm_d.get("isolation", ScmPolicy.isolation)),
        branch_per=str(scm_d.get("branch_per", ScmPolicy.branch_per)),
        target_branch=str(scm_d.get("target_branch", ScmPolicy.target_branch)),
        merge_strategy=str(scm_d.get("merge_strategy", ScmPolicy.merge_strategy)),
        delete_branch=bool(scm_d.get("delete_branch", ScmPolicy.delete_branch)),
        keep_failed=bool(scm_d.get("keep_failed", ScmPolicy.keep_failed)),
        rollback_on_failure=bool(scm_d.get("rollback_on_failure", ScmPolicy.rollback_on_failure)),
        failed_diff_max_mb=int(scm_d.get("failed_diff_max_mb", ScmPolicy.failed_diff_max_mb)),
        failed_diff_unlimited=bool(
            scm_d.get("failed_diff_unlimited", ScmPolicy.failed_diff_unlimited)
        ),
        commit_message_template=str(
            scm_d.get("commit_message_template", ScmPolicy.commit_message_template)
        ),
        # Phase 5 parallel fan-out is unbuilt: clamp to 1 so the knob is inert.
        max_parallel=min(requested_parallel, 1),
        seed_adapter_defaults=bool(
            scm_d.get("seed_adapter_defaults", ScmPolicy.seed_adapter_defaults)
        ),
        worktree_seed=tuple(str(s) for s in scm_d.get("worktree_seed", ())),
    )
    if scm.isolation not in ISOLATION_MODES:
        raise PolicyError(
            f"scm.isolation must be one of {sorted(ISOLATION_MODES)}: got {scm.isolation!r}"
        )
    if scm.branch_per not in BRANCH_PER_MODES:
        raise PolicyError(
            f"scm.branch_per must be one of {sorted(BRANCH_PER_MODES)}: got {scm.branch_per!r}"
        )
    if scm.merge_strategy not in MERGE_STRATEGIES:
        raise PolicyError(
            f"scm.merge_strategy must be one of {sorted(MERGE_STRATEGIES)}: "
            f"got {scm.merge_strategy!r}"
        )
    if scm.failed_diff_max_mb < 1:
        raise PolicyError(f"scm.failed_diff_max_mb must be >= 1: got {scm.failed_diff_max_mb}")
    cleanup = CleanupPolicy(
        run_retention=int(cleanup_d.get("run_retention", CleanupPolicy.run_retention)),
        retention_days=int(cleanup_d.get("retention_days", CleanupPolicy.retention_days)),
        trim_artifacts=bool(cleanup_d.get("trim_artifacts", CleanupPolicy.trim_artifacts)),
        archive_old=bool(cleanup_d.get("archive_old", CleanupPolicy.archive_old)),
        auto_clean_on_finish=bool(
            cleanup_d.get("auto_clean_on_finish", CleanupPolicy.auto_clean_on_finish)
        ),
        clean_tmp=bool(cleanup_d.get("clean_tmp", CleanupPolicy.clean_tmp)),
    )
    if cleanup.run_retention < 0:
        raise PolicyError(f"cleanup.run_retention must be >= 0: got {cleanup.run_retention}")
    if cleanup.retention_days < 0:
        raise PolicyError(f"cleanup.retention_days must be >= 0: got {cleanup.retention_days}")
    raw_enabled = plugins_d.get("enabled", ())
    if isinstance(raw_enabled, str) or not isinstance(raw_enabled, (list, tuple)):
        raise PolicyError("plugins.enabled must be a list of plugin names")
    enabled = [str(n) for n in raw_enabled]
    # Every key under [plugins] other than `enabled` that is a table is a
    # per-plugin settings sub-table ([plugins.<name>]).
    plugin_settings = {
        str(k): dict(v) for k, v in plugins_d.items() if k != "enabled" and isinstance(v, dict)
    }
    # The game-engine layer is now a plugin. Fold a deprecated [engine] block into
    # [plugins] (enable the named plugin + map its keys to [plugins.<name>]) so
    # existing Unity configs keep working for one release; explicit [plugins.*]
    # values win over the folded ones.
    _fold_deprecated_engine(engine_d, enabled, plugin_settings)
    if plugin_schemas:
        for name, raw_settings in plugin_settings.items():
            _validate_plugin_settings(name, raw_settings, plugin_schemas.get(name))
    plugins = PluginsPolicy(enabled=tuple(enabled), settings=plugin_settings)
    tui = TuiPolicy(low_frame_rate=bool(tui_d.get("low_frame_rate", TuiPolicy.low_frame_rate)))
    return Policy(
        gates=gates,
        limits=limits,
        verify=verify,
        notify=notify,
        review=review,
        dev=dev,
        adapter=adapter,
        sweep=sweep,
        scm=scm,
        cleanup=cleanup,
        plugins=plugins,
        tui=tui,
    )


def _fold_deprecated_engine(
    engine_d: dict[str, Any], enabled: list[str], plugin_settings: dict[str, dict[str, Any]]
) -> None:
    """Translate a legacy ``[engine]`` block into the plugin surface in place.

    ``[engine] name = "unity"`` becomes ``[plugins] enabled = ["unity"]`` plus a
    ``[plugins.unity]`` table carrying editor_mode/mcp/unity_path/ready_*; the
    editor_mode↔scm.isolation coupling is now validated by the plugin itself
    (``UnityPlugin.validate``), not here. A no-op when ``[engine]`` is absent or
    its ``name`` is empty (the old "disabled" state)."""
    if not engine_d:
        return
    warnings.warn(
        "[engine] in policy.toml is deprecated; the game-engine layer is now a "
        'plugin. Use [plugins] enabled = ["unity"] with a [plugins.unity] table. '
        "[engine] will be removed in a future release.",
        DeprecationWarning,
        stacklevel=3,
    )
    name = str(engine_d.get("name", "")).strip()
    if not name:
        return
    if name not in enabled:
        enabled.append(name)
    folded = {k: engine_d[k] for k in _ENGINE_SETTING_KEYS if k in engine_d}
    # explicit [plugins.<name>] values take precedence over the folded [engine] ones
    plugin_settings[name] = {**folded, **plugin_settings.get(name, {})}


POLICY_TEMPLATE = """\
# bmad-loop orchestration policy. All keys optional; defaults shown.

[gates]
mode = "per-epic"            # none | per-epic | per-story-spec-approval
retrospective = "notify"     # never | notify | auto (auto unsupported in v1)

[limits]
max_review_cycles = 3
max_dev_attempts = 2
session_timeout_min = 90
stop_without_result_nudges = 1
dev_stall_grace_s = 600      # grace for a dev session that ended its turn awaiting a background process (e.g. a slow PlayMode/test run) before it is called stalled; each re-invocation resets it. 0 = fail fast on the first result-less Stop
dev_stall_nudges = 2         # times an idle dev session is nudged awake on grace expiry before it is called stalled (bmad-loop has no background-completion re-invocation); pane output re-arms the grace window and a fresh Stop restores the budget. 0 = stall on grace expiry
workflow_stall_nudges_cap = 3 # total (never-restored) stall nudges for an injected plugin-workflow session before it is called stalled; bounds a session that finished its work but never wrote its completion marker. 0 = stall on first grace expiry
max_tokens_per_story = 2000000
cache_read_weight = 0.1      # cache reads bill at ~0.1x input on all vendors; 1.0 = count raw

[verify]
# Deterministic gates run by the orchestrator after a clean review, before commit.
commands = []                # e.g. ["pytest -q", "ruff check ."]

[notify]
desktop = true               # notify-send, best-effort
file = true                  # ATTENTION file in the run dir

[review]
# enabled = true  -> run a follow-up review session (bmad-dev-auto re-invoked on
#                    the done spec for a fresh review pass) after a dev pass.
# enabled = false -> skip that session; the bmad-dev-auto pass's own inline review
#                    is the only review and it finalizes the story straight to done.
enabled = true
# trigger (only consulted when enabled = true) decides WHEN that session runs:
#   "recommended" -> only when the bmad-dev-auto pass flags the story with
#                    `followup_review_recommended: true` (it self-reviews inline
#                    and flags this when its changes warrant an independent pass).
#   "always"      -> run the second-opinion review on every story.
# The loop is bounded by limits.max_review_cycles either way.
trigger = "recommended"

[adapter]
name = "claude"              # claude | codex | gemini | <custom .bmad-loop/profiles/*.toml>
model = ""                   # empty = CLI default model
cleanup_session_on_finish = true  # kill the run's tmux session when it finishes (false keeps it for inspection)
# extra_args replaces the profile's default permission-bypass flags when set:
# extra_args = ["--permission-mode", "bypassPermissions"]
# Optional overrides of the CLI profile's own defaults (unset = inherit the
# profile's shipped value; copilot ships usage_grace_s = 8 and
# stop_without_result_nudges = 5):
# usage_grace_s = 8.0                # seconds to poll the transcript for token usage after a session ends
# stop_without_result_nudges = 5     # result-less Stop signals tolerated before a session is called stalled

# Per-stage overrides for the dev, review and sweep-triage passes. Unset keys
# inherit from [adapter] when the stage runs the same client; a stage that
# switches client falls back to that profile's defaults instead (model and
# extra_args are client-specific). Stage tables must come after the [adapter]
# keys above.
# [adapter.dev]
# model = "opus"
# [adapter.review]
# name = "codex"
# model = "gpt-5-codex"
# stop_without_result_nudges = 5     # e.g. a multi-turn review needs more nudges than dev
# [adapter.triage]
# model = "opus"

[sweep]
# Deferred-work sweep: triage + execute open deferred-work.md entries.
auto = "never"               # never | per-epic | run-end (auto-triggered sweeps never prompt)
max_bundles = 5              # bundles executed per sweep; triage excess is truncated
max_triage_attempts = 2      # triage validation retries before escalating
max_migration_attempts = 2   # legacy-ledger migration retries before escalating
repeat = false               # after a cycle completes, re-triage and continue on newly deferred work
max_cycles = 5               # safety cap on total cycles per sweep run when repeat = true

[cleanup]
# Disk reclamation for .bmad-loop/runs. Only terminal (finished/stopped) runs are
# ever touched; paused/interrupted runs stay intact so they remain resumable.
# `bmad-loop clean` applies these, and every run/sweep start reconciles worktrees
# leaked by a mid-flight stop.
run_retention = 10           # newest concluded runs kept whole; older ones are trimmed/archived (0 keeps none)
retention_days = 0           # 0 = disabled; else also keep runs newer than N days regardless of count
trim_artifacts = true        # drop the heavy worktrees/ tree from concluded runs (run stays viewable in the TUI)
archive_old = true           # archive (.bmad-loop/archive/<id>.tar.gz) rather than hard-delete runs past the window
auto_clean_on_finish = true  # reconcile stale worktrees + apply retention when a run finishes cleanly
clean_tmp = true             # let engine plugins clean their /tmp scratch on finish (e.g. Unity MCP server zips)

[scm]
# Source-control isolation + merge-back. Defaults reproduce today's behavior:
# work happens in place on the checked-out branch, with no branches.
isolation = "none"           # none | worktree
branch_per = "story"         # story | run (worktree mode only; "run" forces delete_branch = false)
target_branch = ""           # "" = the branch checked out at run start
merge_strategy = "merge"     # ff | merge | squash (worktree mode merges the unit branch into target locally)
delete_branch = true         # delete the unit branch after a successful merge
keep_failed = true           # keep a failed unit's worktree+branch for inspection
rollback_on_failure = false  # in-place (isolation="none") recovery after a failed attempt. false = never touch the tree; pause with manual recovery steps. true = auto-revert the attempt's tracked changes + remove only the untracked files this run created (WARNING: discards the attempt's uncommitted work; never a blanket git clean). Governs unattended/stopped attempts only: a resolved escalation's re-drive always auto-recovers regardless (reverts the failed source, keeps the corrected spec). Prefer isolation="worktree" to avoid touching your main checkout.
failed_diff_max_mb = 5       # per-file size cap (MB) for untracked files in a kept-failed unit's changes.patch; oversized files are skipped with a marker
failed_diff_unlimited = false # true = capture the failed-unit diff with no size cap (may produce very large patches; warns when active)
# commit_message_template: when set, the commit message dev sessions use for a
# story's commit. {story_key} and {run_id} are substituted. Empty = built-in default.
commit_message_template = ""
max_parallel = 1             # units in flight at once (parallel fan-out unbuilt; values > 1 clamp to 1)
# A git worktree checks out tracked files only, so gitignored MCP/CLI configs are
# absent from every fresh worktree and isolated sessions can't reach their MCP
# server. seed_adapter_defaults copies each loaded adapter's own config files
# (claude -> .mcp.json/.claude/settings.json, codex -> .codex/config.toml, etc.)
# into the worktree; worktree_seed adds extra project-specific gitignored paths.
seed_adapter_defaults = true # seed each loaded adapter's default gitignored configs into worktrees
worktree_seed = []           # extra gitignored files to copy into each worktree, on top of adapter defaults

[plugins]
# Plugin trust allowlist. A plugin dropped under .bmad-loop/plugins/<name>/ loads
# its declarative manifest (settings + out-of-process shell hooks) automatically.
# A plugin that ships an in-process [python] module is NEVER imported or run
# unless its name is listed here. Empty = no plugins trusted (today's behavior).
enabled = []                 # e.g. ["unity", "my-lint-plugin"]

# The game-engine layer is a plugin. For a Unity project whose dev/sweep cycle
# drives a live Editor via an Editor MCP, enable it above and configure it here:
#   [plugins.unity]
#   editor_mode = "shared"       # shared (live Editor; requires scm.isolation = "none")
#                                # | per_worktree (one Editor per worktree; requires
#                                #   scm.isolation = "worktree")
#   mcp = "ivanmurzak"           # which Editor MCP the scripts target: ivanmurzak | coplaydev
#   unity_path = ""              # Editor binary for a per_worktree launch ("" = auto-detect)
#   ready_timeout_sec = 600      # how long the readiness gate waits for the Editor + MCP
#   ready_grace_sec = -1         # delay before the first probe (-1 = auto: per_worktree waits)
# (The legacy [engine] block still loads with a deprecation warning, folded into
#  [plugins.unity] — migrate to [plugins] when convenient.)

[tui]
# low_frame_rate = true caps Textual to 15fps and disables animations (sets
# TEXTUAL_FPS=15 / TEXTUAL_ANIMATIONS=none at launch). Fixes repaint tearing
# over slow/high-latency links (SSH, Tailscale). Equivalent to launching with
# `bmad-loop tui --low-frame-rate`. Takes effect the next time the TUI starts.
low_frame_rate = false
"""
