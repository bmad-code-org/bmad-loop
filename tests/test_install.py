import json

from conftest import git

from bmad_loop import verify
from bmad_loop.adapters.profile import get_profile
from bmad_loop.install import (
    BASE_SKILLS,
    LEGACY_MODULE_SKILLS,
    MODULE_SKILLS,
    install_into,
    merge_hooks,
    missing_base_skills,
    provision_worktree,
    strip_legacy_hooks,
)


def _install_base_skills(root, tree=".claude/skills"):
    """Lay down stubs of the non-bundled upstream skills the orchestrator drives."""
    for skill, markers in BASE_SKILLS.items():
        d = root / tree / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        for marker in markers:
            (d / marker).write_text("x\n", encoding="utf-8")


def _registrations(profile, command="python3 /x/.bmad-loop/bmad_loop_hook.py {event}"):
    return {
        native: command.format(event=canonical)
        for native, canonical in profile.hooks.events.items()
    }


def test_merge_hooks_adds_all_events():
    profile = get_profile("claude")
    settings, changed = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    assert changed
    assert set(profile.hooks.events) <= set(settings["hooks"])


def test_merge_hooks_idempotent():
    profile = get_profile("claude")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    again, changed = merge_hooks(settings, _registrations(profile), profile.hooks.dialect)
    assert not changed
    for event in profile.hooks.events:
        assert len(again["hooks"][event]) == 1


def test_merge_hooks_preserves_existing():
    profile = get_profile("claude")
    existing = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "permissions": {"allow": ["Bash(ls)"]},
    }
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    commands = [
        handler["command"] for matcher in settings["hooks"]["Stop"] for handler in matcher["hooks"]
    ]
    assert "echo hi" in commands
    assert any("bmad_loop_hook" in c for c in commands)


def test_merge_hooks_gemini_entry_shape():
    profile = get_profile("gemini")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    entry = settings["hooks"]["AfterAgent"][0]
    assert entry["matcher"] == ""
    handler = entry["hooks"][0]
    assert handler["timeout"] == 60_000  # Gemini hook timeouts are milliseconds
    # registered under the native event but relaying the canonical name
    assert handler["command"].endswith("bmad_loop_hook.py Stop")


def test_merge_hooks_copilot_entry_shape():
    profile = get_profile("copilot")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    assert settings["version"] == 1  # Copilot hook configs are versioned
    # Copilot stores the handler dict directly in the event list (no "hooks" wrapper)
    handler = settings["hooks"]["agentStop"][0]
    assert handler["type"] == "command"
    assert handler["timeoutSec"] == 60  # Copilot hook timeouts are seconds
    # registered under the native event (agentStop) but relaying the canonical name
    assert handler["command"].endswith("bmad_loop_hook.py Stop")


def test_merge_hooks_copilot_idempotent():
    # the bare-handler shape must still dedupe on a re-run
    profile = get_profile("copilot")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    again, changed = merge_hooks(settings, _registrations(profile), profile.hooks.dialect)
    assert not changed
    for event in profile.hooks.events:
        assert len(again["hooks"][event]) == 1


# ----------------------------------------------------------------- legacy migration (rename)

LEGACY_CMD = "python3 /x/.automator/bmad_auto_hook.py Stop"


def test_strip_legacy_hooks_claude_shape():
    # claude/codex nest handlers under "hooks"; an emptied event is dropped entirely
    config = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": LEGACY_CMD}]}]}}
    config, removed = strip_legacy_hooks(config)
    assert removed == 1
    assert "Stop" not in config["hooks"]


def test_strip_legacy_hooks_gemini_shape():
    config = {"hooks": {"AfterAgent": [{"matcher": "", "hooks": [{"command": LEGACY_CMD}]}]}}
    config, removed = strip_legacy_hooks(config)
    assert removed == 1
    assert "AfterAgent" not in config["hooks"]


def test_strip_legacy_hooks_copilot_bare_shape():
    # copilot stores the handler directly in the event list (no "hooks" wrapper)
    config = {"version": 1, "hooks": {"agentStop": [{"type": "command", "command": LEGACY_CMD}]}}
    config, removed = strip_legacy_hooks(config)
    assert removed == 1
    assert "agentStop" not in config["hooks"]
    assert config["version"] == 1  # untouched


def test_strip_legacy_hooks_preserves_foreign_and_new():
    # a foreign user hook and a current bmad_loop hook survive; only bmad_auto goes
    config = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": LEGACY_CMD}]},
                {"hooks": [{"type": "command", "command": "echo hi"}]},
                {
                    "hooks": [
                        {"type": "command", "command": "python3 .bmad-loop/bmad_loop_hook.py Stop"}
                    ]
                },
            ]
        }
    }
    config, removed = strip_legacy_hooks(config)
    assert removed == 1
    commands = [h["command"] for m in config["hooks"]["Stop"] for h in m["hooks"]]
    assert commands == ["echo hi", "python3 .bmad-loop/bmad_loop_hook.py Stop"]


def test_strip_legacy_hooks_prunes_within_matcher():
    # legacy + new share one matcher's nested list -> prune just the legacy handler
    config = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": LEGACY_CMD},
                        {"type": "command", "command": "python3 .bmad-loop/bmad_loop_hook.py Stop"},
                    ]
                }
            ]
        }
    }
    config, removed = strip_legacy_hooks(config)
    assert removed == 1
    handlers = config["hooks"]["Stop"][0]["hooks"]
    assert [h["command"] for h in handlers] == ["python3 .bmad-loop/bmad_loop_hook.py Stop"]


def test_strip_legacy_hooks_noop_without_hooks():
    assert strip_legacy_hooks({}) == ({}, 0)
    assert strip_legacy_hooks({"hooks": {}})[1] == 0
    # the hyphenated upstream skill must never be mistaken for the legacy relay
    config = {"hooks": {"Stop": [{"hooks": [{"command": "/bmad-dev-auto 1-2-a"}]}]}}
    assert strip_legacy_hooks(config)[1] == 0


def test_install_migrates_from_legacy_bmad_auto(tmp_path):
    """A project that was `bmad-auto init`-ed: init strips the old hook, removes the
    old skill dirs, and carries the old policy over — leaving .automator/ in place."""
    # pre-seed a legacy claude install
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": LEGACY_CMD}]}]}}),
        encoding="utf-8",
    )
    legacy_skill = tmp_path / ".claude" / "skills" / "bmad-auto-sweep"
    legacy_skill.mkdir(parents=True)
    (legacy_skill / "SKILL.md").write_text("# old\n", encoding="utf-8")
    legacy_policy = tmp_path / ".automator" / "policy.toml"
    legacy_policy.parent.mkdir(parents=True)
    legacy_policy.write_text('[scm]\nisolation = "worktree"\n', encoding="utf-8")

    assert install_into(tmp_path) == 0

    # legacy hook stripped, current bmad_loop hook registered in its place
    result = json.loads(settings.read_text())
    cmds = [h["command"] for m in result["hooks"]["Stop"] for h in m["hooks"]]
    assert not any("bmad_auto" in c for c in cmds)
    assert any("bmad_loop_hook" in c for c in cmds)
    # legacy skill dir removed; new forks installed
    assert not legacy_skill.exists()
    for skill in MODULE_SKILLS:
        assert (tmp_path / ".claude" / "skills" / skill / "SKILL.md").is_file()
    # old policy carried over verbatim; .automator/ left in place
    migrated = (tmp_path / ".bmad-loop" / "policy.toml").read_text()
    assert migrated == '[scm]\nisolation = "worktree"\n'
    assert (tmp_path / ".automator").is_dir()

    # idempotent: re-run doesn't duplicate hooks or re-create the legacy skill
    assert install_into(tmp_path) == 0
    result = json.loads(settings.read_text())
    assert len(result["hooks"]["Stop"]) == 1
    assert not legacy_skill.exists()


def test_install_does_not_clobber_existing_policy_over_legacy(tmp_path):
    """When .bmad-loop/policy.toml already exists, a legacy .automator/policy.toml
    must not overwrite it."""
    current = tmp_path / ".bmad-loop" / "policy.toml"
    current.parent.mkdir(parents=True)
    current.write_text("CURRENT", encoding="utf-8")
    legacy = tmp_path / ".automator" / "policy.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("LEGACY", encoding="utf-8")

    assert install_into(tmp_path) == 0
    assert current.read_text() == "CURRENT"


def test_install_legacy_skills_constant_matches_module_skills():
    # the legacy names are exactly the current ones with the old prefix
    assert LEGACY_MODULE_SKILLS == tuple(
        s.replace("bmad-loop-", "bmad-auto-") for s in MODULE_SKILLS
    )


def test_copilot_profile_render_prompt():
    # {skill} must expand plainly (no codex-style $ prefix) into the SKILL.md path
    profile = get_profile("copilot")
    rendered = profile.render_prompt("/bmad-dev-auto 1-2-a")
    assert ".agents/skills/bmad-dev-auto/SKILL.md" in rendered
    assert "1-2-a" in rendered


def test_install_into_copilot(tmp_path):
    assert install_into(tmp_path, clis=("copilot",)) == 0
    settings = json.loads((tmp_path / ".github" / "copilot" / "settings.json").read_text())
    assert settings["version"] == 1
    # registered under the camelCase native names Copilot 1.0.63 actually fires
    # (agentStop is turn-end; PascalCase Stop never fires); relay still gets canonical
    assert set(settings["hooks"]) == {"agentStop", "sessionStart", "sessionEnd"}
    cmd = settings["hooks"]["agentStop"][0]["command"]
    # absolute path baked in (no $CLAUDE_PROJECT_DIR equivalent in copilot)
    assert str(tmp_path.resolve()) in cmd and cmd.endswith(" Stop")
    # skills land in the shared .agents/skills tree
    for skill in MODULE_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill / "SKILL.md").is_file()

    # idempotent re-run does not duplicate the bare handler
    assert install_into(tmp_path, clis=("copilot",)) == 0
    settings = json.loads((tmp_path / ".github" / "copilot" / "settings.json").read_text())
    assert len(settings["hooks"]["agentStop"]) == 1


def test_install_into_full(tmp_path):
    assert install_into(tmp_path) == 0
    assert (tmp_path / ".bmad-loop" / "bmad_loop_hook.py").is_file()
    assert (tmp_path / ".bmad-loop" / "policy.toml").is_file()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "Stop" in settings["hooks"]
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".bmad-loop/runs/" in gitignore
    assert ".bmad-loop/cache/" in gitignore  # engine plugins' rebuildable caches

    # all bundled skills land in claude's tree, with nested files intact
    skills_dir = tmp_path / ".claude" / "skills"
    for skill in MODULE_SKILLS:
        assert (skills_dir / skill / "SKILL.md").is_file()
    assert (skills_dir / "bmad-loop-sweep" / "deferred-work-format.md").is_file()

    # second run: idempotent, does not duplicate
    assert install_into(tmp_path) == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1
    final_gitignore = (tmp_path / ".gitignore").read_text()
    assert final_gitignore.count(".bmad-loop/runs/") == 1
    assert final_gitignore.count(".bmad-loop/cache/") == 1


def test_hook_command_uses_selected_process_host(tmp_path, monkeypatch):
    # The hook interpreter is platform-selected: forcing the Windows host swaps the
    # registered command's prefix without `install` branching on sys.platform.
    from bmad_loop.process_host import get_process_host

    monkeypatch.setenv("BMAD_LOOP_PROCESS_HOST", "windows")
    get_process_host.cache_clear()
    try:
        assert install_into(tmp_path) == 0
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert cmd.startswith("uv run --no-project python ")
    finally:
        monkeypatch.delenv("BMAD_LOOP_PROCESS_HOST", raising=False)
        get_process_host.cache_clear()


def test_install_into_multiple_clis(tmp_path):
    assert install_into(tmp_path, clis=("codex", "gemini")) == 0

    codex_hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(codex_hooks["hooks"]) == {"SessionStart", "Stop"}
    cmd = codex_hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    # absolute path (no $CLAUDE_PROJECT_DIR equivalent in codex/gemini)
    assert str(tmp_path.resolve()) in cmd and cmd.endswith(" Stop")

    gemini_settings = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert set(gemini_settings["hooks"]) == {"SessionStart", "AfterAgent", "SessionEnd"}

    # idempotent across both
    assert install_into(tmp_path, clis=("codex", "gemini")) == 0
    codex_hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert len(codex_hooks["hooks"]["Stop"]) == 1


def test_install_skills_dedupes_agents_tree(tmp_path):
    # codex and gemini share .agents/skills — install once there, not under .claude
    assert install_into(tmp_path, clis=("codex", "gemini")) == 0
    for skill in MODULE_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill / "SKILL.md").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_install_skills_skip_existing(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-loop-sweep" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    # default run must not clobber an existing skill dir
    assert install_into(tmp_path) == 0
    assert skill_md.read_text() == "CUSTOM"
    # but a skill that was absent still gets installed
    assert (tmp_path / ".claude" / "skills" / "bmad-loop-resolve" / "SKILL.md").is_file()


def test_install_skills_force(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-loop-resolve" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    assert install_into(tmp_path, force_skills=True) == 0
    assert skill_md.read_text() != "CUSTOM"


def test_install_no_skills(tmp_path):
    assert install_into(tmp_path, skills=False) == 0
    # hooks still installed, but no skill tree created
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_install_unknown_cli(tmp_path):
    assert install_into(tmp_path, clis=("acme-cli",)) == 1
    assert not (tmp_path / ".bmad-loop").exists()


def test_install_resolves_legacy_alias(tmp_path):
    assert install_into(tmp_path, clis=("claude-code-tmux",)) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_provision_worktree_lays_down_skills_and_hook(tmp_path):
    """A worktree must receive the bmad-loop-* skills + signal hook even though
    those dirs are gitignored (absent from a fresh checkout), or the bundled
    skills are missing and the Stop hook never fires."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    provision_worktree(wt, [claude], repo)

    # skills installed into the claude skill tree
    for skill in MODULE_SKILLS:
        assert (wt / claude.skill_tree / skill / "SKILL.md").is_file()
    # hook registered, baked to the MAIN repo's relay (absolute) — nothing written
    # into the worktree's .bmad-loop/ (which a project may not gitignore)
    settings = json.loads((wt / claude.hooks.config_path).read_text())
    assert set(claude.hooks.events) <= set(settings["hooks"])
    cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str((repo / ".bmad-loop" / "bmad_loop_hook.py")) in cmd
    assert not (wt / ".bmad-loop").exists()


def test_provision_worktree_covers_multiple_profiles(tmp_path):
    """Dev=claude + review=codex provisions both skill trees (.claude/skills and
    .agents/skills) and both hook configs."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude, codex = get_profile("claude"), get_profile("codex")
    provision_worktree(wt, [claude, codex], repo)

    assert (wt / claude.skill_tree / "bmad-loop-sweep" / "SKILL.md").is_file()
    assert (wt / codex.skill_tree / "bmad-loop-sweep" / "SKILL.md").is_file()
    assert (wt / claude.hooks.config_path).is_file()
    assert (wt / codex.hooks.config_path).is_file()


def test_provision_worktree_does_not_clobber_existing_skill(tmp_path):
    """A skill the checkout already carries (project commits its own skill tree)
    is left untouched, so no diff is merged back."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    existing = wt / claude.skill_tree / "bmad-loop-sweep" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("COMMITTED", encoding="utf-8")

    provision_worktree(wt, [claude], repo)
    assert existing.read_text() == "COMMITTED"
    # a skill that was absent is still laid down
    assert (wt / claude.skill_tree / "bmad-loop-resolve" / "SKILL.md").is_file()


def test_provision_worktree_empty_profiles_is_noop(tmp_path):
    provision_worktree(tmp_path / "wt", [], tmp_path / "repo")
    assert not (tmp_path / "wt").exists()


def test_provision_worktree_copies_base_skills_from_repo(tmp_path):
    """The upstream skills the orchestrator drives aren't bundled in the wheel, so
    the worktree must get them copied from the MAIN repo's installed tree."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    _install_base_skills(repo, claude.skill_tree)

    provision_worktree(wt, [claude], repo)

    for skill in BASE_SKILLS:
        assert (wt / claude.skill_tree / skill / "SKILL.md").is_file()
    # the dev primitive's marker file came along too
    assert (wt / claude.skill_tree / "bmad-dev-auto" / "step-04-review.md").is_file()


def test_missing_base_skills_reports_absent_and_incomplete(tmp_path):
    claude = get_profile("claude")
    # nothing installed → dev primitive + both inline review hunters reported
    # missing (the hunters are always required — bmad-dev-auto's step-04 invokes
    # them on every run, regardless of the orchestrator's follow-up review)
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 3
    assert all("install the BMad Method" in p for p in problems)

    # install everything → no problems
    _install_base_skills(tmp_path, claude.skill_tree)
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []

    # remove the dev primitive's marker → reported as incomplete
    (tmp_path / claude.skill_tree / "bmad-dev-auto" / "step-04-review.md").unlink()
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert "incomplete" in problems[0]
    assert "step-04-review.md" in problems[0]


def test_provision_worktree_seeds_gitignored_config(tmp_path):
    """A gitignored config present in the main repo is copied into the worktree
    (a `git worktree add` checkout would omit it)."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    provision_worktree(wt, [], repo, seed_files=[".mcp.json"])
    assert (wt / ".mcp.json").read_text() == '{"mcpServers": {}}'


def test_provision_worktree_seed_skips_missing_source(tmp_path):
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    provision_worktree(wt, [], repo, seed_files=[".mcp.json"])
    assert not (wt / ".mcp.json").exists()


def test_provision_worktree_seed_does_not_clobber_existing(tmp_path):
    """A seed target already present in the worktree (tracked/committed) is left
    untouched, so no diff is merged back."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text("FROM_REPO", encoding="utf-8")
    dst = wt / ".mcp.json"
    dst.parent.mkdir(parents=True)
    dst.write_text("IN_WORKTREE", encoding="utf-8")
    provision_worktree(wt, [], repo, seed_files=[".mcp.json"])
    assert dst.read_text() == "IN_WORKTREE"


def test_provision_worktree_seed_rejects_escaping_path(tmp_path):
    """A seed entry resolving outside the repo/worktree is skipped — never copies
    a file from outside the project tree into the worktree."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("SECRET", encoding="utf-8")
    provision_worktree(wt, [], repo, seed_files=["../outside.txt"])
    assert not wt.exists()  # nothing copied, no dirs created


def test_provision_worktree_seed_then_hook_merge_preserves_settings(tmp_path):
    """A seeded settings file that is also the hook config_path keeps its real
    content (seeded first), then gets the Stop hook merged in — not recreated empty."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    claude = get_profile("claude")
    cfg = repo / claude.hooks.config_path  # .claude/settings.json
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")

    provision_worktree(wt, [claude], repo, seed_files=[claude.hooks.config_path])

    seeded = json.loads((wt / claude.hooks.config_path).read_text())
    assert seeded["permissions"] == {"allow": ["Bash(ls)"]}  # real content survived
    assert "Stop" in seeded["hooks"]  # signal hook merged in on top


def test_provision_worktree_seed_shielded_in_local_exclude(project, tmp_path):
    """Seeded configs are added to the worktree's local git exclude so a project
    that doesn't gitignore them won't have the unit's `git add -A` stage them."""
    repo = project.project
    (repo / ".mcp.json").write_text("{}", encoding="utf-8")
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("claude")], repo, seed_files=[".mcp.json"])

    assert (wt / ".mcp.json").is_file()
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/.mcp.json" in exclude.splitlines()


# ----------------------------------------------------------------- seed_globs (engine plugin)


def test_provision_worktree_seed_globs_copies_matching_tree(tmp_path):
    """A glob pattern expands against the main repo; every match is copied into
    the worktree (this is how an engine plugin's MCP skill dirs reach a worktree)."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    skills = repo / ".claude" / "skills"
    (skills / "gameobject-create").mkdir(parents=True)
    (skills / "gameobject-create" / "SKILL.md").write_text("tool", encoding="utf-8")
    (skills / "scene-open").mkdir(parents=True)
    (skills / "scene-open" / "SKILL.md").write_text("tool", encoding="utf-8")

    provision_worktree(wt, [], repo, seed_globs=[".claude/skills/*"])

    assert (wt / ".claude" / "skills" / "gameobject-create" / "SKILL.md").read_text() == "tool"
    assert (wt / ".claude" / "skills" / "scene-open" / "SKILL.md").read_text() == "tool"


def test_provision_worktree_seed_globs_skip_existing_and_noop_when_unmatched(tmp_path):
    """Glob seeding never clobbers a match already in the worktree, and an empty
    expansion writes nothing."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    src = repo / ".claude" / "skills" / "ping"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("FROM_REPO", encoding="utf-8")
    dst = wt / ".claude" / "skills" / "ping"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("IN_WORKTREE", encoding="utf-8")

    # one matching dir already present, plus a pattern that matches nothing
    provision_worktree(wt, [], repo, seed_globs=[".claude/skills/*", ".mcp/*"])

    assert (dst / "SKILL.md").read_text() == "IN_WORKTREE"  # not clobbered


def test_provision_worktree_seed_globs_shielded_in_local_exclude(project, tmp_path):
    """Glob-seeded paths join the worktree's local git exclude alongside seed_files,
    so a project that doesn't gitignore its skill tree won't stage them."""
    repo = project.project
    skill = repo / ".claude" / "skills" / "tests-run"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("tool", encoding="utf-8")
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("claude")], repo, seed_globs=[".claude/skills/*"])

    assert (wt / ".claude" / "skills" / "tests-run" / "SKILL.md").is_file()
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert "/.claude/skills/tests-run" in exclude
    assert git(wt, "status", "--short", "--", ".claude/skills/tests-run") == ""
