import json
import shutil
import sys
import zipfile

import pytest
from conftest import git

from bmad_loop import verify
from bmad_loop.adapters.profile import get_profile
from bmad_loop.install import (
    BASE_SKILLS,
    DEV_BASE_SKILLS,
    DEV_PRIMITIVE_LEGACY,
    DEV_PRIMITIVE_NEW,
    LEGACY_MODULE_SKILLS,
    MODULE_SKILLS,
    _copy_traversable,
    base_skills_seed_incomplete,
    install_into,
    merge_hooks,
    missing_base_skills,
    provision_worktree,
    resolve_dev_primitive,
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


def test_merge_hooks_antigravity_entry_shape():
    # agy keys hooks.json by hook NAME at the top level ("bmad-loop"), and its Stop
    # event is FLAT (handler dict directly, no matcher/hooks wrapper).
    profile = get_profile("antigravity")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    assert "hooks" not in settings  # not a "hooks"-wrapped dialect
    handler = settings["bmad-loop"]["Stop"][0]
    assert handler["type"] == "command"
    assert handler["timeout"] == 60  # agy hook timeouts are seconds
    assert handler["command"].endswith("bmad_loop_hook.py Stop")


def test_merge_hooks_antigravity_idempotent():
    profile = get_profile("antigravity")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    again, changed = merge_hooks(settings, _registrations(profile), profile.hooks.dialect)
    assert not changed
    for event in profile.hooks.events:
        assert len(again["bmad-loop"][event]) == 1


def test_merge_hooks_antigravity_appends_beside_existing_stop():
    # agy stores each event as a LIST of handlers; a hooks.json that already has a
    # bmad-loop group with the user's own Stop handler must keep it and gain ours.
    profile = get_profile("antigravity")
    existing = {"bmad-loop": {"Stop": [{"type": "command", "command": "echo mine", "timeout": 5}]}}
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    commands = [h["command"] for h in settings["bmad-loop"]["Stop"]]
    assert "echo mine" in commands
    assert any("bmad_loop_hook" in c for c in commands)
    assert len(settings["bmad-loop"]["Stop"]) == 2


def test_merge_hooks_unrelated_bmad_loop_path_does_not_suppress_relay():
    # Dedup keys on the bmad-loop script markers, not the broad "bmad_loop"
    # substring: an unrelated handler whose command merely mentions a
    # bmad_loop-containing path must not make init skip the relay — that would
    # leave `validate` (which detects on the narrow marker) un-passable, the
    # #159 failure class through the merge/detect seam.
    profile = get_profile("claude")
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "python /home/me/bmad_loop_fork/notify.py"}
                    ]
                }
            ]
        }
    }
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    commands = [
        handler["command"] for matcher in settings["hooks"]["Stop"] for handler in matcher["hooks"]
    ]
    assert "python /home/me/bmad_loop_fork/notify.py" in commands
    assert any("bmad_loop_hook" in c for c in commands)


def test_merge_hooks_antigravity_unrelated_bmad_loop_path_does_not_suppress_relay():
    # Same guarantee for agy's flat top-level-group shape (the other dedup branch).
    profile = get_profile("antigravity")
    existing = {
        "bmad-loop": {
            "Stop": [{"type": "command", "command": "python /home/me/bmad_loop_fork/notify.py"}]
        }
    }
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    commands = [h["command"] for h in settings["bmad-loop"]["Stop"]]
    assert "python /home/me/bmad_loop_fork/notify.py" in commands
    assert any("bmad_loop_hook" in c for c in commands)


def test_merge_hooks_antigravity_rejects_malformed_shape():
    # a malformed pre-existing hooks.json yields a clear ProfileError, not an
    # opaque AttributeError during init.
    import pytest

    from bmad_loop.adapters.profile import ProfileError

    profile = get_profile("antigravity")
    with pytest.raises(ProfileError):
        merge_hooks({"bmad-loop": "oops"}, _registrations(profile), profile.hooks.dialect)
    with pytest.raises(ProfileError):
        merge_hooks({"bmad-loop": {"Stop": "oops"}}, _registrations(profile), profile.hooks.dialect)


def test_merge_hooks_antigravity_tolerates_non_string_command():
    # a pre-existing handler whose "command" is a non-string (e.g. None) must not
    # crash the idempotency dedupe.
    profile = get_profile("antigravity")
    existing = {"bmad-loop": {"Stop": [{"type": "command", "command": None}]}}
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    assert any("bmad_loop_hook" in (h.get("command") or "") for h in settings["bmad-loop"]["Stop"])


def test_merge_hooks_antigravity_preserves_other_groups():
    # user/plugin hook groups sit alongside "bmad-loop" and must survive.
    profile = get_profile("antigravity")
    existing = {"lint-checker": {"PostToolUse": [{"matcher": "run_command", "hooks": []}]}}
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    assert settings["lint-checker"] == {"PostToolUse": [{"matcher": "run_command", "hooks": []}]}
    assert settings["bmad-loop"]["Stop"][0]["command"].endswith("bmad_loop_hook.py Stop")


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


def test_strip_legacy_hooks_tolerates_non_string_command():
    # a pre-existing handler whose "command" is a non-string (e.g. null) must not
    # crash the legacy strip — it just isn't a bmad_auto hook, so it's kept.
    # Guarded at both walks: the flat (copilot) entry and the nested handler.
    flat = {"hooks": {"agentStop": [{"type": "command", "command": None}]}}
    config, removed = strip_legacy_hooks(flat)
    assert removed == 0
    assert config["hooks"]["agentStop"] == [{"type": "command", "command": None}]

    nested = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": None},
                        {"type": "command", "command": LEGACY_CMD},
                    ]
                }
            ]
        }
    }
    config, removed = strip_legacy_hooks(nested)
    assert removed == 1  # only the legacy handler is pruned; the null one survives
    handlers = config["hooks"]["Stop"][0]["hooks"]
    assert handlers == [{"type": "command", "command": None}]


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
    assert ".bmad-loop/policy.toml" in gitignore  # per-machine config ([mux] backend)
    # the renderer's output: machine-absolute, rewritten every session. Under
    # isolation = "none" (the default) this line is its only protection.
    assert "_bmad/render/" in gitignore

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
    assert final_gitignore.count(".bmad-loop/policy.toml") == 1
    assert final_gitignore.count("_bmad/render/") == 1


def test_install_into_warns_when_policy_is_tracked(tmp_path, capsys):
    """A .gitignore entry doesn't untrack an already-committed policy.toml:
    upgrading repos get the one-time `git rm --cached` hint."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    policy_file = tmp_path / ".bmad-loop" / "policy.toml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text("[gates]\n", encoding="utf-8")
    subprocess.run(["git", "add", ".bmad-loop/policy.toml"], cwd=tmp_path, check=True)
    assert install_into(tmp_path) == 0
    assert "git rm --cached .bmad-loop/policy.toml" in capsys.readouterr().out


def test_install_into_no_tracking_warning_outside_a_repo(tmp_path, capsys):
    assert install_into(tmp_path) == 0
    assert "git rm --cached" not in capsys.readouterr().out


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
    """A FILE the checkout already carries (project commits its own skill tree) is
    left untouched, so no diff is merged back. Copy-when-absent is asked per file,
    not per directory: the dir-level skip it replaces was silent and TOTAL — one
    tracked SKILL.md made the whole dir "already there" and every sibling the skill
    reads stayed at zero bytes, in a dir that then passes a directory-granular
    completeness check."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    existing = wt / claude.skill_tree / "bmad-loop-sweep" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("COMMITTED", encoding="utf-8")

    provision_worktree(wt, [claude], repo)
    assert existing.read_text(encoding="utf-8") == "COMMITTED"
    # …while the siblings of that one carried file are filled in
    assert (wt / claude.skill_tree / "bmad-loop-sweep" / "deferred-work-format.md").is_file()
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


def test_provision_worktree_fills_in_a_partial_base_skill(tmp_path):
    """A checkout that carries ONE file of a skill dir — a project that commits its
    SKILL.md, a `worktree_seed` entry naming a file inside one — used to make the whole
    dir "already there" and receive zero bytes of the rest. That skips silently and
    totally: the session dispatches a primitive whose step files and review-prompts/
    are absent, and provisioning reports success. Filling the absent siblings in is
    what the per-FILE merge buys, and the empty return is the other half — a dir the
    merge completed is not a skip to journal."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    carried = wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md"
    carried.parent.mkdir(parents=True)
    carried.write_text("# bmad-build-auto\n", encoding="utf-8")

    skipped = provision_worktree(wt, [claude], repo)

    skill = wt / tree / DEV_PRIMITIVE_NEW
    assert (skill / "step-04-review.md").is_file()
    assert (skill / "customize.toml").is_file()
    assert (skill / "review-prompts" / "adversarial.md").is_file()
    assert skipped == []


def test_provision_worktree_merge_keeps_the_bytes_the_checkout_carries(tmp_path):
    """The bound on the fill-in above: per-file copy-when-ABSENT must not drift into
    copy-over. A project that customizes a skill in-tree has those bytes tracked, and
    overwriting them puts the upstream copy into the unit's `git add -A` commit — the
    diff isolation exists to prevent."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    carried = wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md"
    carried.parent.mkdir(parents=True)
    carried.write_text("COMMITTED", encoding="utf-8")

    provision_worktree(wt, [claude], repo)

    assert carried.read_text(encoding="utf-8") == "COMMITTED"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_reports_a_symlinked_out_skill_child(tmp_path):
    """Per-file granularity splits the containment guard per child, so the failure it
    guards against is no longer whole-or-nothing: a skill dir whose customize.toml
    alone points at a shared install outside the repo lands SHORT while every sibling
    copies fine. The repo-side preflight stats through the link and passes, so nothing
    upstream says a word — without a per-file report the run dispatches a step-04 whose
    layer config was never delivered, on every story."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "customize.toml").write_text("[[review_layers]]\n", encoding="utf-8")
    marker = repo / tree / DEV_PRIMITIVE_NEW / "customize.toml"
    marker.unlink()
    marker.symlink_to(shared / "customize.toml")
    assert marker.is_file()  # the preflight follows the link, which is why it passed

    skipped = provision_worktree(wt, [claude], repo)

    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}/customize.toml"]
    assert (wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file()  # the rest landed
    assert not (wt / tree / DEV_PRIMITIVE_NEW / "customize.toml").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_merge_refuses_to_write_outside_the_worktree(tmp_path):
    """The destination half of the containment guard, which only the merge needs: the
    dir-level skip never reached a per-file destination at all. A symlink in the
    CHECKOUT pointing out of the worktree is a path `_copy_traversable` would follow
    and write THROUGH, landing the wheel's bytes somewhere no teardown ever cleans.
    Refusing leaves the worktree without a readable SKILL.md, which the gate then
    reports coarsely — the whole dir is what a session cannot dispatch."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md"
    escape.parent.mkdir(parents=True)
    escape.symlink_to(outside / "escaped.md")  # dangling on purpose

    skipped = provision_worktree(wt, [claude], repo)

    assert not (outside / "escaped.md").exists()
    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}"]


# --------------------------------------------------------------- the shared walk itself
#
# ⚠️ Every test from here to `_seed_pair` needs a symlink or a permission bit to arm,
# so NONE of them can have a Windows witness — the conjuncts they pin are covered by
# the POSIX lanes alone. The designated symlink-free Windows witnesses for this file
# are `test_a_dropped_nested_file_is_reported_with_a_posix_rel`,
# `test_a_nested_snapshot_target_is_keyed_by_its_posix_rel` and
# `test_provision_worktree_central_config_check_asks_is_file_not_exists`; they stay
# unguarded on purpose and none of the fixtures below may be pushed into them.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_walk_does_not_raise_on_an_undescendable_directory(tmp_path):
    """The fail-soft `iterdir`, asked only for the half that must never regress: the
    walk RETURNS on a directory it cannot list.

    Raising instead takes an unreadable repo skill out through `Engine._run_isolated`,
    which has no `try` around provisioning: measured, the run ends with `crash.txt` and
    `state.crashed`, leaving a worktree whose Stop hook was never registered — where
    the same fault, degraded, is one story's escalation naming one rel.

    Split from the sibling test on purpose, because the two ablations are different
    edits: deleting the `try` reddens both, while swallowing the entry (`except OSError:
    return`) reddens only the sibling. POSIX-only, non-root runner (root reads through
    mode 000)."""
    from bmad_loop.install import _walk_traversable_files

    root = tmp_path / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "locked" / "deep.md").write_text("# deep\n", encoding="utf-8")
    (root / "a.md").write_text("# a\n", encoding="utf-8")
    (root / "locked").chmod(0o000)
    try:
        rels = [rel for rel, _ in _walk_traversable_files(root)]
    finally:
        (root / "locked").chmod(0o755)

    assert "a.md" in rels


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_an_undescendable_directory_is_yielded_as_a_leaf(tmp_path):
    """The other half: it is not merely survived, it is NAMED.

    Swallowing it — `except OSError: return` — is the tempting one-word variant and it
    is the failure this whole cluster exists to prevent: the copier delivers nothing
    from that subtree and the gate, walking the same yield, has nothing to report, so
    an unreadable skill dir reads as a healthy one. Yielding the directory as a leaf is
    what lets the two halves split on one predicate — the copier's `_is_file` drops it
    as undeliverable, the gates' `_is_file or _is_dir` names it as missing.

    The rel names a DIRECTORY, which is the honest granularity: nobody can say which
    files are short when the directory cannot be listed. POSIX-only, non-root runner."""
    from bmad_loop.install import _walk_traversable_files

    root = tmp_path / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "locked" / "deep.md").write_text("# deep\n", encoding="utf-8")
    (root / "a.md").write_text("# a\n", encoding="utf-8")
    (root / "locked").chmod(0o000)
    try:
        walked = dict(_walk_traversable_files(root))
    finally:
        (root / "locked").chmod(0o755)

    assert sorted(walked) == ["a.md", "locked"]
    assert walked["locked"].is_dir()  # a directory, handed back as a leaf


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_walk_names_the_children_of_an_unsearchable_directory(tmp_path):
    """The OTHER unreadable-directory mode, and the one that hid a crash: readable but
    NOT searchable (`0o444`, or `chmod -R a-x`).

    `iterdir()` needs `r` and succeeds, so the walk gets the child NAMES; stat-ing a
    child needs `x` on the directory and is refused, so `is_dir()` raises on every one
    of them. That is a different fault from the sibling above, where `iterdir()` itself
    fails and the directory is yielded whole — and it went unseen because every
    unreadable-directory fixture in this file used `0o000`. Measured before `_is_dir`:
    `PermissionError` out of `provision_worktree` on 3.11/3.12/3.13, while 3.14 answered
    False and dropped the subtree in silence.

    The children are named INDIVIDUALLY rather than the directory being named once,
    which is the honest granularity in the other direction from the sibling: `iterdir`
    did give up the names, so a rel per name is exactly what is known. POSIX-only,
    non-root runner."""
    from bmad_loop.install import _walk_traversable_files

    root = tmp_path / "tree"
    (root / "listable").mkdir(parents=True)
    (root / "listable" / "deep.md").write_text("# deep\n", encoding="utf-8")
    (root / "a.md").write_text("# a\n", encoding="utf-8")
    (root / "listable").chmod(0o444)
    try:
        rels = sorted(rel for rel, _ in _walk_traversable_files(root))
    finally:
        (root / "listable").chmod(0o755)

    assert rels == ["a.md", "listable/deep.md"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_is_dir_reads_a_refused_probe_as_a_directory_on_every_interpreter(tmp_path, monkeypatch):
    """The version split `_is_dir` has to close, pinned WITHOUT depending on which
    version is running it.

    The witnesses either side of this one exercise only the reading the LOCAL
    interpreter produces: through 3.13 `is_dir()` raises and `_is_dir`'s `except`
    answers, while on 3.14 it returns False and `_probe_refused` is the only thing
    between an unreadable subtree and a silent drop. Measured with the `except` alone,
    both `0o444` witnesses passed on 3.11/3.12/3.13 and FAILED on 3.14 — a green local
    suite and a red CI lane, which is exactly the shape a local-only ablation cannot
    see. Patching `is_dir` to answer False emulates 3.14's reading on any lane, so both
    branches are covered wherever this runs. `stat()` is untouched by the patch, and
    measured on all four it still raises `EACCES` here.

    The ABSENT case is what keeps the loud fallback honest: a path that simply is not
    there must stay False, or every leaf the walk yields would be re-descended and
    named missing. `EACCES` is refusal; `ENOENT` is absence."""
    from pathlib import Path

    from bmad_loop.install import _is_dir, _probe_refused

    child = tmp_path / "unsearchable" / "child.md"
    child.parent.mkdir()
    child.write_text("x\n", encoding="utf-8")
    child.parent.chmod(0o444)
    try:
        assert _is_dir(child) is True  # this lane's own reading, whichever it is
        monkeypatch.setattr(Path, "is_dir", lambda self, **kw: False)  # the 3.14 one
        assert _is_dir(child) is True  # ... and it survives that reading too
        assert _is_dir(tmp_path / "nope") is False  # absent stays absent under it
    finally:
        child.parent.chmod(0o755)

    # A wheel Traversable has no `stat` and no permission fault to find; re-asking is
    # scoped to real Paths, and anything else keeps whatever `is_dir()` said.
    assert _probe_refused(object()) is False

    # An invalid path is the one fault where `stat()` is LESS total than `is_dir()`:
    # measured on 3.11 and 3.14, `is_dir()` answers False and `stat()` raises
    # `ValueError`. Re-asking without catching it would add a raise to the very
    # function whose contract is that provisioning has none.
    assert _is_dir(Path("a\0b")) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_the_walk_terminates_on_a_symlink_cycle(tmp_path):
    """`rglob` needs no cycle guard because it refuses to descend child symlinks at
    all. Descending them is this walk's whole reason to exist, and it is what makes
    `_bmad/scripts/loop -> ..` unbounded.

    Measured before the guard, on this very fixture: 42 files, every one passing the
    resolve-and-contain check because `resolve()` collapses the loop back inside the
    repo — so they were COPIED, not skipped — terminating only by accident when the
    kernel's `ELOOP` finally made `is_dir()` answer False. Where that lands is not a
    property of the fixture: the limit applies to the ABSOLUTE path, so re-measuring it
    from a shallower tmpdir moves the depth (82 components against the 85 first seen)
    while the file count holds. A hostile tree is not the
    scenario; a shared BMAD install wired with a convenience symlink is."""
    from bmad_loop.install import _walk_traversable_files

    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "render_skill.py").write_text("# render", encoding="utf-8")
    (root / "scripts" / "loop").symlink_to(root, target_is_directory=True)

    assert [rel for rel, _ in _walk_traversable_files(root)] == ["scripts/render_skill.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_two_sibling_symlinks_to_one_install_are_not_a_cycle(tmp_path):
    """The cycle guard is BRANCH-local, and this is the rejected design it is measured
    against: one global `seen` set terminates the loop above just as well and silently
    drops the second sibling's rels from the copy AND the gate.

    Two skill subdirectories pointed at one shared install is not a cycle — it is how a
    shared install gets wired — and each of them has to be seeded on its own account.
    A global set would make the ablation of this test the *quiet* kind: files missing
    from the worktree with nothing reporting them."""
    from bmad_loop.install import _walk_traversable_files

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "tool.py").write_text("# tool", encoding="utf-8")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a_lib").symlink_to(shared, target_is_directory=True)
    (root / "b_lib").symlink_to(shared, target_is_directory=True)

    assert [rel for rel, _ in _walk_traversable_files(root)] == [
        "a_lib/tool.py",
        "b_lib/tool.py",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_never_writes_through_a_dangling_dst_symlink(tmp_path):
    """A committed symlink whose target does not exist on this machine — a thing git
    stores and checks out happily — occupies the slot, and `exists()` says it does not.

    Measured: writing anyway lands the bytes at the LINK'S TARGET, not at the slot, so
    the unit's `git add -A` picks up an untracked file the session never asked for while
    the completeness gate reads green through the now-live link. Distinct from
    `test_provision_worktree_merge_refuses_to_write_outside_the_worktree`, whose link
    escapes the worktree and dies on containment: this one resolves INSIDE, so
    containment passes and only `_occupied` stands between the copy and the wrong file.
    """
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    slot = wt / tree / DEV_PRIMITIVE_NEW / "customize.toml"
    slot.parent.mkdir(parents=True)
    elsewhere = wt / tree / DEV_PRIMITIVE_NEW / "not-checked-out.toml"
    slot.symlink_to(elsewhere)  # dangling, but resolving INSIDE the worktree

    skipped = provision_worktree(wt, [claude], repo)

    assert not elsewhere.exists()  # the bytes never landed at the link's target
    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}/customize.toml"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_survives_a_dangling_symlink_directory_component(tmp_path):
    """The `mkdir` crash site, and the proof that the leaf `try` is not redundant with
    `_occupied`: measured, `target.is_symlink()` is FALSE when it is the target's PARENT
    that dangles, so `_occupied` waves this through and `mkdir(parents=True,
    exist_ok=True)` raises `FileExistsError` (its recovery is `if not exist_ok or not
    self.is_dir(): raise`, and `is_dir()` is False on a dangling link).

    Unguarded that leaves provisioning by the one door the doctrine forbids. Degraded,
    it is one rel in `skipped` and every sibling file still lands."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    stand_in = wt / tree / DEV_PRIMITIVE_NEW / "review-prompts"
    stand_in.parent.mkdir(parents=True)
    stand_in.symlink_to(wt / tree / DEV_PRIMITIVE_NEW / "not-checked-out")

    skipped = provision_worktree(wt, [claude], repo)

    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}/review-prompts/adversarial.md"]
    assert (wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file()  # the rest landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_survives_an_unreadable_source_file(tmp_path):
    """The other crash site the same `try` covers, and it fails at a different CALL —
    `shutil.copy2`, not `mkdir`. Two witnesses because deleting either half of the
    two-statement body leaves the other one's test green.

    A chmod-000 source passes `is_file()` (its parent is readable, so the stat works)
    and then raises `PermissionError` on the read. Nothing lands at the destination:
    `copyfile` opens the source before the destination, so there is no truncated file to
    mistake for a delivery. POSIX-only, non-root runner."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    locked = repo / tree / DEV_PRIMITIVE_NEW / "customize.toml"
    locked.chmod(0o000)
    try:
        skipped = provision_worktree(wt, [claude], repo)
    finally:
        locked.chmod(0o644)

    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}/customize.toml"]
    assert not (wt / tree / DEV_PRIMITIVE_NEW / "customize.toml").exists()
    assert (wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file()  # the rest landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_does_not_materialize_an_undescendable_source_dir(tmp_path):
    """What the copier's `is_file()` filter buys once the leaf `try` is there to catch
    the crash: the destination is not littered with directories the copy can never fill.

    Without it `_copy_traversable` takes the directory branch, `mkdir`s the destination,
    and only THEN fails listing the source — so the worktree ends up carrying an empty
    `review-prompts/` that no file will ever arrive in. The rel is still reported either
    way, which is exactly why this asserts the directory's absence rather than
    `skipped`: that half is the gate's witness, not this one's. POSIX-only, non-root."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    locked = repo / tree / DEV_PRIMITIVE_NEW / "review-prompts"
    locked.chmod(0o000)
    try:
        provision_worktree(wt, [claude], repo)
    finally:
        locked.chmod(0o755)

    assert not (wt / tree / DEV_PRIMITIVE_NEW / "review-prompts").exists()
    assert (wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file()  # the rest landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_reports_the_children_of_an_unsearchable_source_dir(tmp_path):
    """The same doctrine end to end for the `0o444` fault, and the finer report it
    earns: the sibling above names one DIRECTORY rel because nothing could be listed,
    this names the FILE because `iterdir` gave up the name and only the stat was
    refused.

    Ablating the walk's recursion probe back to the raw call reddens this on
    3.11/3.12/3.13, where it raises `PermissionError` straight out of
    `provision_worktree` — the crash this cluster exists to prevent, at the one call the
    phase's first pass left bare. Measured, that same ablation is GREEN on 3.14 (0 reds,
    whole suite): the raw call answers False there and the walk yields the identical
    leaf. What the 3.14 lane depends on instead is `_is_dir`'s re-ask — ablating THAT
    reddens 3 on 3.14 against 1 on 3.13, and this test is one of the two extra. The two
    ablations are therefore not interchangeable, and neither lane's local suite covers
    both readings on its own.

    The destination is not littered either: the copier's `_is_file` is False for an
    entry it cannot classify, so no empty `review-prompts/` is created for a file that
    will never arrive. POSIX-only, non-root runner."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    listable = repo / tree / DEV_PRIMITIVE_NEW / "review-prompts"
    listable.chmod(0o444)
    try:
        skipped = provision_worktree(wt, [claude], repo)
    finally:
        listable.chmod(0o755)

    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}/review-prompts/adversarial.md"]
    assert not (wt / tree / DEV_PRIMITIVE_NEW / "review-prompts").exists()
    assert (wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file()  # the rest landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_seeds_a_read_only_source_dir_without_reporting_it(tmp_path):
    """The false-positive guard for the pair above, and the reason `0o444` and `0o555`
    must never be conflated: `0o555` is read-only but still SEARCHABLE, so every stat
    succeeds and there is no fault at all.

    `_is_dir` answers True on a probe it cannot complete, which is deliberately the
    loud direction — so the thing to prove is that it is not loud on a directory that
    merely cannot be written to. A read-only skill tree is an ordinary thing to have;
    if this reported, every such project would take a CRITICAL escalation on every
    story.

    ⚠️ What it pins is the NARROWNESS of that loud reading rather than any one guard, so
    reverting the guards around it leaves it green — that is the property a
    false-positive control must have, and also what makes it look inert next to rows
    that name their reds. It is not inert. It reddens on a `_is_dir` that widens past
    the exception it exists for, and each of its two assertions catches a different
    widening: a constant-True `_is_dir` fails `skipped == []` with four spurious rels,
    and an `os.access(W_OK)`-based one — the tempting "can we actually use this
    directory" reading — fails the delivery assert, because a read-only source stops
    being copied at all. POSIX-only, non-root runner."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    read_only = repo / tree / DEV_PRIMITIVE_NEW / "review-prompts"
    read_only.chmod(0o555)
    try:
        skipped = provision_worktree(wt, [claude], repo)
    finally:
        read_only.chmod(0o755)

    assert skipped == []
    assert (wt / tree / DEV_PRIMITIVE_NEW / "review-prompts" / "adversarial.md").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_dangling_repo_symlink_is_dropped_by_the_copier_and_the_gate(tmp_path):
    """The direction the gate's source filter has to get right in reverse: a broken
    link the copy could never have followed must NOT halt the backlog.

    A dangling symlink is neither file nor directory, so both halves drop it — the
    copier has no bytes to deliver and the gate has nothing to demand. Without the
    filter every such link becomes a CRITICAL escalation on every story, over a file the
    operator's own repo cannot produce either. What the repo's own content should be is
    the run-start preflight's question (`skills.base-missing`/`-incomplete`/`-shim`,
    `skills.dev-renderer-sources`); this one asks only whether the copy delivered what
    the repo has."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    broken = repo / tree / DEV_PRIMITIVE_NEW / "notes.md"
    broken.symlink_to(repo / tree / DEV_PRIMITIVE_NEW / "not-installed.md")

    skipped = provision_worktree(wt, [claude], repo)

    assert skipped == []
    assert not (wt / tree / DEV_PRIMITIVE_NEW / "notes.md").exists()
    assert (wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md").is_file()  # the rest landed


def _seed_pair(tmp_path, skills, tree=".claude/skills"):
    """A repo carrying ``skills`` and a worktree carrying the same — the state
    `provision_worktree` produces when every copy lands. Callers then delete from the
    worktree to model whatever the containment guard dropped, which is all
    `base_skills_seed_incomplete` reads: it walks the REPO's dir and asks the worktree
    for each rel behind a `SKILL.md` precondition, never how the worktree's copy of one
    got there.

    Writing the same set to both roots is what keeps this fixture silent under walk
    parity — a rel only appears when a caller deletes it from ``wt``.
    """
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    for root in (repo, wt):
        for skill, markers in skills.items():
            d = root / tree / skill
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            for marker in markers:
                (d / marker).write_text("x\n", encoding="utf-8")
    return wt, repo, tree


def test_base_skills_seed_incomplete_ignores_the_unused_primitive_era(tmp_path):
    """BASE_SKILLS names both eras because copy-if-present makes naming both free; that
    does NOT make both required. With bmad-build-auto resolved every prompt spells it,
    so a dropped bmad-dev-auto leftover cannot stall a session — and the caller's
    response to any rel here is a CRITICAL escalation that halts the whole run."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    shutil.rmtree(wt / tree / DEV_PRIMITIVE_LEGACY)

    assert resolve_dev_primitive(repo, tree) == DEV_PRIMITIVE_NEW
    assert base_skills_seed_incomplete(wt, repo, [tree]) == []


def test_base_skills_seed_incomplete_ignores_an_unresolvable_new_primitive_dir(tmp_path):
    """The same over-breadth in the other direction, and invisible to the preflight: a
    `SKILL.md`-less bmad-build-auto dir (an aborted install) is not resolvable and is
    never stat'ed once the legacy name resolves — but `is_dir()` is all the gate asks."""
    wt, repo, tree = _seed_pair(tmp_path, DEV_BASE_SKILLS)
    (repo / tree / DEV_PRIMITIVE_NEW).mkdir()

    assert resolve_dev_primitive(repo, tree) == DEV_PRIMITIVE_LEGACY
    assert missing_base_skills(repo, [tree]) == []
    assert base_skills_seed_incomplete(wt, repo, [tree]) == []


def test_base_skills_seed_incomplete_reports_a_dropped_legacy_primitive(tmp_path):
    """The bound on the narrowing above: scoping to the RESOLVED primitive is not
    scoping to the new name. A pre-rename project drives bmad-dev-auto, so a worktree
    that lost it stalls exactly as hard and must still pause."""
    wt, repo, tree = _seed_pair(tmp_path, DEV_BASE_SKILLS)
    shutil.rmtree(wt / tree / DEV_PRIMITIVE_LEGACY)

    assert resolve_dev_primitive(repo, tree) == DEV_PRIMITIVE_LEGACY
    assert base_skills_seed_incomplete(wt, repo, [tree]) == [f"{tree}/{DEV_PRIMITIVE_LEGACY}"]


def test_base_skills_seed_incomplete_reports_a_dropped_bmad_review(tmp_path):
    """bmad-review is deliberately outside the preflight so pre-merge bmm installs keep
    validating — but on a merged-lens install the three hunter ids are thin forwarders
    to it, so a worktree that lacks one the repo HAS breaks those forwards. Preflight-
    optional is not the same question as seeded-or-not, and the repo-has-it conjunct
    already keeps the pre-merge case silent."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    shutil.rmtree(wt / tree / "bmad-review")

    assert missing_base_skills(repo, [tree]) == []  # the preflight never asks for it
    assert base_skills_seed_incomplete(wt, repo, [tree]) == [f"{tree}/bmad-review"]


def test_base_skills_seed_incomplete_reports_a_short_skill_dir(tmp_path):
    """The dir is PRESENT and dispatchable — SKILL.md is right there — and one required
    file inside it is not. A directory-granular gate calls that complete, so the run
    proceeds and every dev session's step-04 resolves its review layers from a
    customize.toml that was never delivered. The rel has to name the file: a rel the
    operator falsifies with one `ls` teaches them to stop reading the gate.

    The destination is a DIRECTORY rather than simply absent, which is also the shape
    that separates `is_file` from `exists` (see the sibling test below)."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    short = wt / tree / DEV_PRIMITIVE_NEW / "customize.toml"
    short.unlink()
    short.mkdir()

    assert base_skills_seed_incomplete(wt, repo, [tree]) == [
        f"{tree}/{DEV_PRIMITIVE_NEW}/customize.toml"
    ]


def test_base_skills_seed_incomplete_asks_is_file_not_exists(tmp_path):
    """The leg that fixture arms, asserted for its own sake: a destination occupied by
    a DIRECTORY is not the file the skill needs. `exists()` answers True for it, so the
    dir would be called complete and the session dispatched against a customize.toml
    the renderer cannot read — the same result-less Stop, reached by a path the absent
    case never visits."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    occupied = wt / tree / DEV_PRIMITIVE_NEW / "customize.toml"
    occupied.unlink()
    occupied.mkdir()

    assert base_skills_seed_incomplete(wt, repo, [tree]) == [
        f"{tree}/{DEV_PRIMITIVE_NEW}/customize.toml"
    ]


def test_base_skills_seed_incomplete_cannot_demand_what_the_repo_lacks(tmp_path):
    """The repo-has-it conjunct, which walk parity makes STRUCTURAL rather than a leg:
    the walk enumerates the repo, so an install predating a file is never asked for
    something no copy could have delivered. Provisioning cannot seed a file that does
    not exist upstream, so a report here would be a CRITICAL escalation on every story
    with no remedy the operator can apply — and the preflight, which owns "your install
    is truncated", already said so once at run start.

    Kept for its regression value; it no longer has ablation value, because there is no
    longer a conjunct to delete — only a walk that cannot yield an absent file."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    for root in (repo, wt):
        (root / tree / DEV_PRIMITIVE_NEW / "customize.toml").unlink()

    assert base_skills_seed_incomplete(wt, repo, [tree]) == []


def test_a_repo_only_file_is_delivered_so_parity_stays_silent(tmp_path):
    """What makes walk parity safe — and the rewrite of the test that pinned the
    opposite policy.

    The old gate asked for `SKILL.md` plus the two markers, and argued that parity
    "would halt the whole backlog over a repo-only README.md no session reads, with no
    remedy available to the operator". The per-FILE merge landed in the same commit as
    that argument and retired it: the copier filters nothing by name, so a repo-only
    `README.md` — and a nested `scripts/helper.pyc` — are DELIVERED like every other
    file, and parity has nothing to say about them. The only thing parity can report is
    a file the seed FAILED to deliver, and every one of those has an operator remedy.

    Driven through the real `provision_worktree` rather than a hand-built pair, which
    is the substantive change: the old test wrote both sides itself, so it asserted a
    policy the copier never had a say in.

    The worktree carries ONE file of the dir on purpose. That is what discriminates —
    against an EMPTY destination even a directory-granular merge copies everything and
    this passes without the per-file half (ablation A15)."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    repo_skill = install_build_auto_skill(repo, tree)
    (repo_skill / "README.md").write_text("# local notes\n", encoding="utf-8")
    (repo_skill / "scripts").mkdir()
    (repo_skill / "scripts" / "helper.pyc").write_bytes(b"\x00\x01")
    # the partially-carried checkout: a dir-granular merge calls this "already there"
    carried = wt / tree / DEV_PRIMITIVE_NEW / "SKILL.md"
    carried.parent.mkdir(parents=True)
    carried.write_text("# bmad-build-auto\n", encoding="utf-8")

    skipped = provision_worktree(wt, [claude], repo)

    wt_skill = wt / tree / DEV_PRIMITIVE_NEW
    assert (wt_skill / "README.md").is_file()
    assert (wt_skill / "scripts" / "helper.pyc").is_file()
    assert skipped == []
    assert base_skills_seed_incomplete(wt, repo, [tree]) == []


def test_a_dropped_nested_file_is_reported_with_a_posix_rel(tmp_path):
    """The rel a NESTED casualty reports, and this file's sole Windows witness.

    The walk builds rels by concatenation, never `relative_to`, whose separator is the
    host's. On Linux `os.sep == "/"` makes the two spellings identical, so a
    `relative_to` regression is invisible here by construction and only a Windows job
    can catch it — which is why this test is symlink-free and carries NO skipif.

    A backslash rel does not crash: every consumer treats it as an opaque string (the
    escalation message, the `/`-anchored git exclude patterns, the exact-match sentinel
    filter), so it quietly stops matching. That is the failure mode that ships."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    nested = repo / tree / DEV_PRIMITIVE_NEW / "review-prompts"
    nested.mkdir()
    (nested / "adversarial.md").write_text("# adversarial\n", encoding="utf-8")

    assert base_skills_seed_incomplete(wt, repo, [tree]) == [
        f"{tree}/{DEV_PRIMITIVE_NEW}/review-prompts/adversarial.md"
    ]


def test_base_skills_seed_incomplete_ignores_a_repo_dir_with_no_skill_md(tmp_path):
    """The precondition is `SKILL.md`, not `is_dir()`: a repo dir with no SKILL.md is
    not a dispatchable skill — nothing spells it — so its absence downstream stalls
    nothing, and pausing over it refuses a run nothing is wrong with. The preflight
    already owns that report, and does make it, so narrowing here loses no coverage.

    A review hunter rather than the dev primitive: a SKILL.md-less bmad-build-auto
    falls into the unused-era skip one branch earlier and would prove nothing."""
    hunter = "bmad-review-verification-gap"
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    shutil.rmtree(wt / tree / hunter)
    (repo / tree / hunter / "SKILL.md").unlink()

    problems = missing_base_skills(repo, [tree])
    assert [f.check for f in problems] == ["skills.base-missing"]
    assert problems[0].detail["skill"] == hunter
    assert base_skills_seed_incomplete(wt, repo, [tree]) == []


def test_base_skills_seed_incomplete_ignores_a_short_dir_in_the_unused_era(tmp_path):
    """Composition of the two narrowings: per-FILE granularity must not re-widen the
    era gate. With bmad-build-auto resolved every prompt spells it, so a leftover
    bmad-dev-auto shim is never dispatched and a SHORT copy of it stalls exactly as
    much as an absent one — nothing. The finer rels make this the easier mistake to
    make, since the skill now fails the check on a file rather than on its whole dir."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    (wt / tree / DEV_PRIMITIVE_LEGACY / "customize.toml").unlink()

    assert resolve_dev_primitive(repo, tree) == DEV_PRIMITIVE_NEW
    assert base_skills_seed_incomplete(wt, repo, [tree]) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_base_skills_seed_incomplete_splits_unreadable_from_absent(tmp_path):
    """The `SKILL.md` precondition answers False for two unlike facts, and collapsing
    them is the quiet failure: "this project legitimately lacks that skill" is benign
    and the run walks on, while "the skill is there and we cannot open it" means the
    worktree has none of it either and every session that reaches for it stalls.

    Only the walk can tell them apart. An undescendable directory is the one thing it
    yields as a leaf; an absent path is neither file nor directory, so there is nothing
    to see. Both shapes are armed in one test because the split is the claim — either
    one alone passes under an ablation that answers the same for both.

    A review hunter rather than the dev primitive: an unreadable `bmad-build-auto` falls
    into the unused-era skip one branch earlier (see the sibling test) and would prove
    nothing here. POSIX-only, and it assumes a non-root runner."""
    unreadable = "bmad-review-verification-gap"
    absent = "bmad-review-edge-case-hunter"
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    for root in (repo, wt):
        shutil.rmtree(root / tree / absent)
    shutil.rmtree(wt / tree / unreadable)
    (repo / tree / unreadable).chmod(0o000)
    try:
        assert base_skills_seed_incomplete(wt, repo, [tree]) == [f"{tree}/{unreadable}"]
    finally:
        (repo / tree / unreadable).chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_base_skills_seed_incomplete_names_an_unreadable_repo_subdir(tmp_path):
    """The gate's source filter is `_is_file or _is_dir` where the copier's is
    `_is_file` alone, and the only yielded entry that answers the second alone is a
    directory the walk could not see inside — a descendable one is recursed into, and a
    cycle repeat is dropped with no rel.

    That single predicate is the whole difference between the two halves of the seed,
    and dropping the `_is_dir` half restores the exact pairing this cluster exists to
    end: an unreadable subtree copied as nothing and reported as complete. The rel names
    the directory because that is all anyone honestly knows about it — where a dir that
    can be LISTED but not stat'd through names its children instead (see the `0o444`
    witnesses). POSIX-only, non-root runner."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    locked = repo / tree / DEV_PRIMITIVE_NEW / "review-prompts"
    locked.mkdir()
    (locked / "adversarial.md").write_text("# adversarial\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert base_skills_seed_incomplete(wt, repo, [tree]) == [
            f"{tree}/{DEV_PRIMITIVE_NEW}/review-prompts"
        ]
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_survives_an_unreadable_dev_primitive_dir(tmp_path):
    """`resolve_dev_primitive` is not a preflight-only resolver: this gate asks it, on
    every provision, through `dev_primitive_or_default`. Measured with its bare
    `is_file()` probes, an unreadable `bmad-build-auto` dir in the repo took
    `provision_worktree` itself down with `PermissionError` through 3.13 — a raise out
    of `Engine._run_isolated` from the function whose whole contract is never to raise —
    while 3.14 answered False and fell through.

    Total probes make both lanes do what 3.14 did: fall through to the complete LEGACY
    install, which is dispatchable, so its era is the used one and the unreadable dir is
    rightly never asked about. The silence is the correct answer here; what was wrong
    was the crash. POSIX-only, non-root runner."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    claude = get_profile("claude")
    locked = repo / tree / DEV_PRIMITIVE_NEW
    locked.chmod(0o000)
    try:
        skipped = provision_worktree(wt, [claude], repo)
        assert resolve_dev_primitive(repo, tree) == DEV_PRIMITIVE_LEGACY
    finally:
        locked.chmod(0o755)

    assert skipped == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_survives_an_unreadable_skill_tree(tmp_path):
    """One level above every other fixture here: not a skill dir the walk cannot read,
    but the TREE that holds them — so the walk root's own parent is unsearchable and
    even `is_dir()` on the root raises.

    Probes stacked behind probes, and each only became reachable once the one in front
    of it was fixed — the same shape as the phase's earlier findings. Measured on
    3.11/3.12/3.13, one raw probe at a time, in the order they surface: the `BASE_SKILLS`
    copy loop's own `src_root` guard; then the walk's root probe, reached from the copy
    that guard admits (`provision_worktree` -> `_merge_traversable` ->
    `_walk_traversable_files`) and NOT from the gate; then, inside the gate itself,
    `resolve_dev_primitive`'s `SKILL.md` probes (via `dev_primitive_or_default`), the
    gate's own `SKILL.md` precondition, and last its absent-vs-unreadable split. Five,
    not two — each verified by reverting exactly that one call and reading the frame the
    `PermissionError` comes out of; the order is the frame order in `provision_worktree`
    itself.

    Every skill is named, and naming them is the point: nothing can be confirmed
    delivered when the repo side cannot be read at all, and the benign reading — "this
    project has no skills" — is the silence the split exists to refuse. The worktree
    here HAS every skill, which is what makes the assertion mean something: the report
    is about what could be verified, not about what happens to be on disk. The dev
    primitive is absent from the list because the era resolves away from it one branch
    earlier (see the sibling test). POSIX-only, non-root runner."""
    wt, repo, tree = _seed_pair(tmp_path, BASE_SKILLS)
    claude = get_profile("claude")
    (repo / tree).chmod(0o000)
    try:
        skipped = provision_worktree(wt, [claude], repo)
    finally:
        (repo / tree).chmod(0o755)

    assert skipped == [f"{tree}/{skill}" for skill in BASE_SKILLS if skill != DEV_PRIMITIVE_NEW]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_survives_an_unwritable_destination_dir(tmp_path):
    """The destination half of the same doctrine: a skill directory in the CHECKOUT
    that cannot be written into, or even stat'd through.

    Through 3.13 `_occupied`'s `exists()` raises on it and totality is what keeps that
    inside the loop; on 3.14 it answers False and the write fails into the leaf `except`
    instead. Two paths, one outcome — nothing written, the whole dir named — which is
    what this asserts, because a return-value assertion would pin one interpreter's
    reading and redden on the other (`_occupied` is True on 3.11/3.12/3.13 and False on
    3.14). The rel is coarse because the worktree's `SKILL.md` is
    unreadable too: nothing under that dir is dispatchable. POSIX-only, non-root."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)
    locked = wt / tree / DEV_PRIMITIVE_NEW
    locked.mkdir(parents=True)
    locked.chmod(0o000)
    try:
        skipped = provision_worktree(wt, [claude], repo)
    finally:
        locked.chmod(0o755)

    assert skipped == [f"{tree}/{DEV_PRIMITIVE_NEW}"]
    assert list(locked.iterdir()) == []


def test_missing_base_skills_reports_absent_and_incomplete(tmp_path):
    """Markers are asserted on the primitive that RESOLVED. Truncating the legacy
    name instead is a different finding (it stops resolving at all) — see
    test_legacy_primitive_missing_a_marker_is_refused."""
    primitive = "bmad-build-auto"
    claude = get_profile("claude")
    # nothing installed → dev primitive + all three inline review hunters reported
    # missing (the hunters are always required — the primitive's step-04 invokes
    # them on every run, regardless of the orchestrator's follow-up review)
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 4
    assert all("install the BMad Method" in p.message for p in problems)

    # install everything → no problems
    _install_base_skills(tmp_path, claude.skill_tree)
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []

    # remove the resolved primitive's step-file marker → reported as incomplete
    (tmp_path / claude.skill_tree / primitive / "step-04-review.md").unlink()
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert "incomplete" in problems[0].message
    assert "step-04-review.md" in problems[0].message
    assert problems[0].detail["skill"] == primitive

    # restore it, then drop customize.toml (the review-layer config marker,
    # BMAD-METHOD #2535/#2550) → a pre-July bmm install is caught as incomplete
    (tmp_path / claude.skill_tree / primitive / "step-04-review.md").write_text("x\n")
    (tmp_path / claude.skill_tree / primitive / "customize.toml").unlink()
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert "incomplete" in problems[0].message
    assert "customize.toml" in problems[0].message
    assert problems[0].detail["skill"] == primitive

    # the newest review layer (verification-gap) reported by name when absent
    _install_base_skills(tmp_path, claude.skill_tree)  # re-complete everything
    shutil.rmtree(tmp_path / claude.skill_tree / "bmad-review-verification-gap")
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert "bmad-review-verification-gap" in problems[0].message
    assert "install the BMad Method" in problems[0].message


# --- the bmad-dev-auto -> bmad-build-auto rename (BMAD-METHOD #2651) ---------


def _install_hunters(root, tree=".claude/skills"):
    """Just the three inline review hunters — so a resolution test's findings are
    only ever about the dev primitive."""
    from bmad_loop.install import REVIEW_HUNTER_SKILLS

    for skill in REVIEW_HUNTER_SKILLS:
        d = root / tree / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")


def _install_renderer_surface(root):
    """The PROJECT-global half of a renderer-era install: the script and the central
    config layer. Lets a test about a skill's own render SOURCES speak only about
    those — without it every such test also carries `skills.dev-renderer` and
    `skills.dev-renderer-config`, and an assert on the check list stops discriminating.
    The script body deliberately does not import `config_utils`, so the sibling is not
    required (see `_renderer_unit_required`) and one file is enough."""
    from bmad_loop.install import CENTRAL_CONFIG_REL, RENDERER_SCRIPT_REL

    script = root / RENDERER_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# renderer\n", encoding="utf-8")
    (root / CENTRAL_CONFIG_REL).write_text('[core]\nname = "x"\n', encoding="utf-8")


def _install_legacy_primitive(root, tree=".claude/skills"):
    """A complete PRE-rename dev primitive: SKILL.md plus every marker."""
    from bmad_loop.install import DEV_PRIMITIVE_LEGACY, DEV_PRIMITIVE_MARKERS

    d = root / tree / DEV_PRIMITIVE_LEGACY
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {DEV_PRIMITIVE_LEGACY}\n", encoding="utf-8")
    for marker in DEV_PRIMITIVE_MARKERS:
        (d / marker).write_text("x\n", encoding="utf-8")
    return d


def test_resolve_dev_primitive_prefers_the_new_name_over_a_complete_legacy(tmp_path):
    """The rename's four on-disk states, resolved. The shim is the one that must
    NOT resolve: its migration gate is interactive, so an unattended session
    dispatched to it HALTs without writing a spec — worse than failing preflight."""
    from conftest import install_build_auto_skill, install_dev_shim

    from bmad_loop.install import resolve_dev_primitive

    tree = get_profile("claude").skill_tree

    # 1. nothing installed
    assert resolve_dev_primitive(tmp_path, tree) is None

    # 2. shim only → still nothing usable
    install_dev_shim(tmp_path, tree)
    assert resolve_dev_primitive(tmp_path, tree) is None

    # 3. shim + the real new skill (what an upgraded bmm install looks like)
    install_build_auto_skill(tmp_path, tree)
    assert resolve_dev_primitive(tmp_path, tree) == "bmad-build-auto"

    # 4. a complete legacy install alone (pre-rename project)
    shutil.rmtree(tmp_path / tree / "bmad-build-auto")
    shutil.rmtree(tmp_path / tree / "bmad-dev-auto")
    _install_legacy_primitive(tmp_path, tree)
    assert resolve_dev_primitive(tmp_path, tree) == "bmad-dev-auto"

    # …and the new name wins the moment it appears beside it
    install_build_auto_skill(tmp_path, tree)
    assert resolve_dev_primitive(tmp_path, tree) == "bmad-build-auto"


def test_resolve_dev_primitive_takes_the_new_name_without_its_markers(tmp_path):
    """Resolution keys on SKILL.md alone for the new name so a truncated
    bmad-build-auto is REPORTED (base-incomplete) rather than silently falling
    through to a legacy install — or to the shim's misleading message."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import missing_base_skills, resolve_dev_primitive

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_legacy_primitive(tmp_path, tree)
    install_build_auto_skill(tmp_path, tree)
    (tmp_path / tree / "bmad-build-auto" / "customize.toml").unlink()

    assert resolve_dev_primitive(tmp_path, tree) == "bmad-build-auto"
    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.base-incomplete"]
    assert problems[0].detail["skill"] == "bmad-build-auto"


def test_shim_only_install_fails_the_preflight_with_the_rename_named(tmp_path):
    """The headline case: a project upgraded past #2651 has ONLY the shim under the
    old name, and v0.9.0's marker check would have called it `base-incomplete` —
    a message pointing at the wrong remedy (reinstall) for the actual cause."""
    from conftest import install_dev_shim

    from bmad_loop.checks import VALIDATE_CHECKS
    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    install_dev_shim(tmp_path, tree)

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.base-shim"]
    assert problems[0].check in VALIDATE_CHECKS
    assert problems[0].severity == "problem"
    assert problems[0].detail == {
        "tree": tree,
        "skill": "bmad-dev-auto",
        "expected": "bmad-build-auto",
        "missing_markers": ["step-04-review.md", "customize.toml"],
    }
    assert "bmad-build-auto" in problems[0].message
    assert "HALT" in problems[0].message  # the hazard, not just the absence


def test_legacy_primitive_missing_a_marker_is_refused(tmp_path):
    """A truncated legacy install is the same on-disk shape as the shim, so it
    lands on the same finding. What must NOT happen is resolving it and driving a
    primitive whose step-04 config is absent."""
    from bmad_loop.install import missing_base_skills, resolve_dev_primitive

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_legacy_primitive(tmp_path, tree)
    (tmp_path / tree / "bmad-dev-auto" / "step-04-review.md").unlink()

    assert resolve_dev_primitive(tmp_path, tree) is None
    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.base-shim"]
    assert problems[0].detail["missing_markers"] == ["step-04-review.md"]


def test_no_primitive_at_all_names_the_current_upstream_skill(tmp_path):
    """An empty tree is `base-missing`, not `base-shim` — and it points at the new
    name, because that is what a fresh bmm install would lay down."""
    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.base-missing"]
    assert problems[0].detail["skill"] == "bmad-build-auto"
    assert "bmad-dev-auto" in problems[0].message  # older installs still named


def test_dev_primitive_or_default_is_total(tmp_path):
    """Prompt builders need a name unconditionally. A None tree (an adapter with no
    profile) and an unresolvable tree both fall back to the legacy name — the
    preflight has already refused the real cases before any session is spawned."""
    from conftest import install_build_auto_skill, install_dev_shim

    from bmad_loop.install import dev_primitive_or_default

    tree = get_profile("claude").skill_tree
    assert dev_primitive_or_default(tmp_path, None) == "bmad-dev-auto"
    assert dev_primitive_or_default(tmp_path, tree) == "bmad-dev-auto"
    install_dev_shim(tmp_path, tree)
    assert dev_primitive_or_default(tmp_path, tree) == "bmad-dev-auto"
    install_build_auto_skill(tmp_path, tree)
    assert dev_primitive_or_default(tmp_path, tree) == "bmad-build-auto"


def test_resolution_is_per_tree(tmp_path):
    """One run can drive two CLIs with different skill trees, and an operator can
    upgrade bmm in one and not the other — so resolution is per tree, and both a
    finding and a resolved name have to follow the tree they came from."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import missing_base_skills, resolve_dev_primitive

    claude, codex = get_profile("claude").skill_tree, get_profile("codex").skill_tree
    assert claude != codex
    for tree in (claude, codex):
        _install_hunters(tmp_path, tree)
    install_build_auto_skill(tmp_path, claude)
    _install_legacy_primitive(tmp_path, codex)

    assert resolve_dev_primitive(tmp_path, claude) == "bmad-build-auto"
    assert resolve_dev_primitive(tmp_path, codex) == "bmad-dev-auto"
    assert missing_base_skills(tmp_path, [claude, codex]) == []


def test_renderer_stub_without_its_script_fails_the_preflight(tmp_path):
    """BMAD-METHOD #2601: SKILL.md became a stub that shells out to a project-local
    render script. Missing script = the session HALTs with nothing written on EVERY
    story, so it is a preflight problem, not an advisory warning — the same failure
    shape `skills.base-shim` already blocks on. That the probe is not sufficient for
    success (uv on PATH is never checked) argues against trusting a green, not
    against blocking on a red."""
    from conftest import install_build_auto_skill

    from bmad_loop.checks import VALIDATE_CHECKS
    from bmad_loop.install import CENTRAL_CONFIG_REL, RENDERER_SCRIPT_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    # the OTHER renderer prerequisite, so this test speaks only about the script
    config = tmp_path / CENTRAL_CONFIG_REL
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[core]\nname = "x"\n', encoding="utf-8")
    # an inline (non-stub) SKILL.md never fires, script or no script
    install_build_auto_skill(tmp_path, tree)
    assert missing_base_skills(tmp_path, [tree]) == []

    install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer"]
    assert problems[0].severity == "problem"
    assert problems[0].check in VALIDATE_CHECKS
    assert RENDERER_SCRIPT_REL in problems[0].message

    # …and it clears once the script is there
    script = tmp_path / RENDERER_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# renderer\n", encoding="utf-8")
    assert missing_base_skills(tmp_path, [tree]) == []


def test_renderer_stub_without_config_utils_fails_the_preflight(tmp_path):
    """The renderer is a TWO-file unit: `render_skill.py` imports `config_utils` at
    module scope off sys.path[0], above its own try/except, so the sibling's absence
    is a bare ModuleNotFoundError — the same result-less Stop, minus even the
    structured `HALT: <error>` line. One check id for both members: same finding,
    same remediation; `missing_scripts` says which.

    Both files are written BY NAME rather than through a helper keyed on
    RENDERER_SCRIPT_UNIT_REL — a constant-keyed helper is self-satisfying under the
    ablation this test exists for ("drop the sibling from the unit tuple")."""
    from conftest import RENDERER_SCRIPT_IMPORTING_SIBLING, install_build_auto_skill

    from bmad_loop.checks import VALIDATE_CHECKS
    from bmad_loop.install import (
        CENTRAL_CONFIG_REL,
        RENDERER_CONFIG_UTILS_REL,
        RENDERER_SCRIPT_REL,
        missing_base_skills,
    )

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    # the other two prerequisites, so this test speaks only about the sibling
    config = tmp_path / CENTRAL_CONFIG_REL
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[core]\nname = "x"\n', encoding="utf-8")
    script = tmp_path / RENDERER_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(RENDERER_SCRIPT_IMPORTING_SIBLING, encoding="utf-8")

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer"]
    assert problems[0].severity == "problem"
    assert problems[0].check in VALIDATE_CHECKS
    assert problems[0].detail["missing_scripts"] == [RENDERER_CONFIG_UTILS_REL]
    assert RENDERER_CONFIG_UTILS_REL in problems[0].message
    # the entry point is present, so it is NOT reported alongside
    assert RENDERER_SCRIPT_REL not in problems[0].detail["missing_scripts"]

    # …and it clears once the sibling is there
    (tmp_path / RENDERER_CONFIG_UTILS_REL).write_text("# helper\n", encoding="utf-8")
    assert missing_base_skills(tmp_path, [tree]) == []


def test_a_renderer_that_does_not_import_the_helper_is_not_gated(tmp_path):
    """The far side of the content key. This gate is a hard block with no severity
    filter and no `--force`, shipping against a file one commit old upstream: if a
    later bmm inlines or renames the helper, the import disappears and the gate must
    go QUIET rather than refuse every run with a remediation that cannot fix it.

    Same layout as the test above, one byte different — the installed script never
    says `config_utils` — so this is the discrimination itself, not a second absence
    case (ablation: make `_renderer_unit_required` unconditional)."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import (
        CENTRAL_CONFIG_REL,
        RENDERER_CONFIG_UTILS_REL,
        RENDERER_SCRIPT_REL,
        missing_base_skills,
    )

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    config = tmp_path / CENTRAL_CONFIG_REL
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[core]\nname = "x"\n', encoding="utf-8")
    script = tmp_path / RENDERER_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# a renderer that carries its own config loading\n", encoding="utf-8")

    assert not (tmp_path / RENDERER_CONFIG_UTILS_REL).exists()
    assert missing_base_skills(tmp_path, [tree]) == []


def test_renderer_stub_without_central_config_fails_the_preflight(tmp_path):
    """`_bmad/config.toml` is the renderer's one REQUIRED config layer
    (`config_utils.load_central_config` passes required=True). Absent, the stub's
    `uv run` raises before composing anything and exits `HALT:` — the same
    result-less Stop the missing-script problem guards, one file further in, and
    one no other check sees."""
    from conftest import install_build_auto_skill

    from bmad_loop.checks import VALIDATE_CHECKS
    from bmad_loop.install import CENTRAL_CONFIG_REL, RENDERER_SCRIPT_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    install_build_auto_skill(tmp_path, tree, renderer_stub=True)

    # a wholly-absent _bmad/ earns BOTH lines — different files, different
    # remediations, so neither is suppressed against the other
    assert [f.check for f in missing_base_skills(tmp_path, [tree])] == [
        "skills.dev-renderer",
        "skills.dev-renderer-config",
    ]

    script = tmp_path / RENDERER_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# renderer\n", encoding="utf-8")
    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-config"]
    assert problems[0].severity == "problem"
    assert problems[0].check in VALIDATE_CHECKS
    assert CENTRAL_CONFIG_REL in problems[0].message
    assert problems[0].detail["config"] == CENTRAL_CONFIG_REL

    # …and it clears once the layer is there (ablation: drop the is_file predicate —
    # invisible to the assert above, which keeps passing with the predicate gone)
    (tmp_path / CENTRAL_CONFIG_REL).write_text('[core]\nname = "x"\n', encoding="utf-8")
    assert missing_base_skills(tmp_path, [tree]) == []


def test_renderer_checks_silent_for_pre_render_skill(tmp_path):
    """Era-agnostic refusal: a pre-#2601 inline SKILL.md never reads the central
    config, so its absence is not a finding to make about that project. The gate is
    the resolved skill's own content — never a bare "`_bmad/config.toml` missing"
    (ablation: hoist the check out of the stub gate)."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import CENTRAL_CONFIG_REL, missing_base_skills

    claude, codex = get_profile("claude").skill_tree, get_profile("codex").skill_tree
    for tree in (claude, codex):
        _install_hunters(tmp_path, tree)
    install_build_auto_skill(tmp_path, claude)  # inline SKILL.md, post-rename name
    _install_legacy_primitive(tmp_path, codex)  # inline SKILL.md, pre-rename name

    assert not (tmp_path / CENTRAL_CONFIG_REL).exists()
    assert missing_base_skills(tmp_path, [claude, codex]) == []


def test_renderer_stub_resolved_is_per_tree_and_keyed_on_content(tmp_path):
    """The predicate the worktree seed gate escalates on, lifted out of
    `missing_base_skills` so the run-time gate and the preflight cannot drift apart.

    Content-keyed, never name-keyed — the legacy leg is the one that proves it, and
    nothing else in the suite covers `_is_renderer_stub` against a `bmad-dev-auto`
    directory. ANY over trees, because one run can mix `.claude/skills` and
    `.agents/skills` on different upstream eras."""
    from conftest import RENDERER_STUB_SKILL_MD, install_build_auto_skill

    from bmad_loop.install import DEV_PRIMITIVE_LEGACY, renderer_stub_resolved

    claude, codex = get_profile("claude").skill_tree, get_profile("codex").skill_tree

    assert renderer_stub_resolved(tmp_path, []) is False  # no tree, nothing to resolve
    assert renderer_stub_resolved(tmp_path, [claude]) is False  # nothing installed

    install_build_auto_skill(tmp_path, claude)  # inline SKILL.md
    assert renderer_stub_resolved(tmp_path, [claude]) is False

    install_build_auto_skill(tmp_path, codex, renderer_stub=True)
    assert renderer_stub_resolved(tmp_path, [codex]) is True
    assert renderer_stub_resolved(tmp_path, [claude, codex]) is True  # ANY, not ALL
    assert renderer_stub_resolved(tmp_path, [claude, claude]) is False  # dedupe is inert

    # …and the era-agnosticism leg: a LEGACY-named primitive carrying stub content is
    # just as much a renderer as the renamed one. Keyed on the SKILL.md, not the dir.
    legacy_tree = ".legacy/skills"  # a third tree, so the codex stub above cannot answer for it
    _install_legacy_primitive(tmp_path, legacy_tree)
    assert renderer_stub_resolved(tmp_path, [legacy_tree]) is False
    (tmp_path / legacy_tree / DEV_PRIMITIVE_LEGACY / "SKILL.md").write_text(
        RENDERER_STUB_SKILL_MD, encoding="utf-8"
    )
    assert renderer_stub_resolved(tmp_path, [legacy_tree]) is True


def test_renderer_config_problem_emitted_once_across_two_trees(tmp_path):
    """`_bmad/config.toml` is project-global like `_bmad/custom/`, so two stub trees
    are one finding (ablation: emit inside the per-tree loop). The script is present
    so the per-tree `skills.dev-renderer` cannot stand in for the once-ness."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import RENDERER_SCRIPT_REL, missing_base_skills

    claude, codex = get_profile("claude").skill_tree, get_profile("codex").skill_tree
    script = tmp_path / RENDERER_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# renderer\n", encoding="utf-8")
    for tree in (claude, codex):
        _install_hunters(tmp_path, tree)
        install_build_auto_skill(tmp_path, tree, renderer_stub=True)

    problems = missing_base_skills(tmp_path, [claude, codex])
    assert [f.check for f in problems] == ["skills.dev-renderer-config"]


def test_renderer_checks_reported_beside_a_truncated_primitive(tmp_path):
    """The renderer probes are independent of the marker check: a stub that is BOTH
    truncated and missing its script earns every line. Different files, different
    remediations (ablation: put the renderer probe in an elif).

    `skills.dev-renderer-sources` joins them because the deleted file is *also* the
    one `RENDERER_WORKFLOW_MD` names — the conftest fixture points its token at a
    DEV_PRIMITIVE_MARKER on purpose, so a healthy stub install always carries its own
    target. The overlap is the fixture's, not a coupling in the code: the sources
    check knows nothing of the marker tuple, and suppressing one against the other
    would reintroduce exactly the named-list dependency the walk-parity reversal
    removed. Two of the four ids below are that pair, and they say different
    things — "this install is truncated" and "the prompt it would render
    references a file that is not there"."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / "step-04-review.md").unlink()

    assert [f.check for f in missing_base_skills(tmp_path, [tree])] == [
        "skills.base-incomplete",
        "skills.dev-renderer",
        "skills.dev-renderer-sources",
        "skills.dev-renderer-config",
    ]


def test_renderer_checks_are_silent_when_nothing_resolves(tmp_path):
    """A shim whose SKILL.md is a renderer stub reports `skills.base-shim` and
    NOTHING else: the renderer probes hang off the resolved branch, so an
    unresolvable tree cannot pile a second remediation on top of the first
    (ablation: hoist the stub probe above the resolution split)."""
    from conftest import RENDERER_STUB_SKILL_MD, install_dev_shim

    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    shim = install_dev_shim(tmp_path, tree)
    (shim / "SKILL.md").write_text(RENDERER_STUB_SKILL_MD, encoding="utf-8")

    assert [f.check for f in missing_base_skills(tmp_path, [tree])] == ["skills.base-shim"]


# --- the renderer's own source set (BMAD-METHOD #2601) -----------------------


def test_renderer_stub_without_its_workflow_entry_fails_the_preflight(tmp_path):
    """`render_skill.py` composes the prompt from `<skill>/workflow.md` and raises
    `render entry is missing` when it is absent — printed as `HALT:`, i.e. a
    result-less Stop on EVERY story, the same environment fault the script and config
    checks already refuse. The marker pair could not see this: two files answered for
    the thirteen a real `bmad-build-auto` carries, so a truncated repo-side install
    passed `validate` and then HALTed every session."""
    from conftest import install_build_auto_skill

    from bmad_loop.checks import VALIDATE_CHECKS
    from bmad_loop.install import RENDERER_ENTRY_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    assert missing_base_skills(tmp_path, [tree]) == []  # the fixture is a WHOLE install

    (skill / RENDERER_ENTRY_REL).unlink()
    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-sources"]
    assert problems[0].severity == "problem"
    assert problems[0].check in VALIDATE_CHECKS
    assert RENDERER_ENTRY_REL in problems[0].message and "HALT" in problems[0].message
    assert problems[0].detail == {
        "tree": tree,
        "skill": DEV_PRIMITIVE_NEW,
        "missing_sources": [RENDERER_ENTRY_REL],
    }


def test_a_snapshot_token_naming_an_absent_source_fails_the_preflight(tmp_path):
    """The other half of the same HALT: a `[[bmad-snapshot:…]]` target that is not one
    of the skill's own sources raises `snapshot reference targets undeclared source`.
    The entry leg cannot stand in for it — `workflow.md` is present here — which is
    why the two are separate lines in the helper."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import RENDERER_ENTRY_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / RENDERER_ENTRY_REL).write_text(
        "Read fully and follow: [[bmad-snapshot:step-09-ship.md]]\n", encoding="utf-8"
    )

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-sources"]
    assert problems[0].detail["missing_sources"] == ["step-09-ship.md"]

    # One entry, not three: the real install names `step-02-plan.md` four times from
    # step-01 alone, so without the dedupe a single dropped file would repeat itself
    # four times in the detail AND in the operator's message line
    (skill / RENDERER_ENTRY_REL).write_text(
        "[[bmad-snapshot:step-09-ship.md]] " * 3, encoding="utf-8"
    )
    problems = missing_base_skills(tmp_path, [tree])
    assert problems[0].detail["missing_sources"] == ["step-09-ship.md"]

    # …and it clears once the source the prompt names is actually there
    (skill / "step-09-ship.md").write_text("# ship\n", encoding="utf-8")
    assert missing_base_skills(tmp_path, [tree]) == []


def test_a_token_in_a_non_entry_source_is_scanned_too(tmp_path):
    """Upstream substitutes tokens across every source it loaded, not just the entry,
    so one undeclared reference anywhere in the tree fails the whole render. A gate
    that read only `workflow.md` would call this install healthy and hand every story
    to the same HALT."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    # the ENTRY stays healthy — its own token names a file the fixture carries
    (skill / "step-02-plan.md").write_text(
        "then read [[bmad-snapshot:step-99-nope.md]]\n", encoding="utf-8"
    )

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-sources"]
    assert problems[0].detail["missing_sources"] == ["step-99-nope.md"]


def test_a_snapshot_token_naming_skill_md_is_reported(tmp_path):
    """`SKILL.md` is the one file that is present on disk and still not a source:
    upstream skips it by NAME, at any depth, when it collects the set a token may
    target. So a prompt pointing at it HALTs exactly like a pointer at a file that
    does not exist, and the gate has to answer the same way."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import RENDERER_ENTRY_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / RENDERER_ENTRY_REL).write_text(
        "Read fully and follow: [[bmad-snapshot:SKILL.md]]\n", encoding="utf-8"
    )

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-sources"]
    assert problems[0].detail["missing_sources"] == ["SKILL.md"]

    # …"at any depth" is upstream's rule too — it keys the skip on the file's NAME,
    # not its rel — so a nested one is no more targetable than the top-level one
    (skill / "sub").mkdir()
    (skill / "sub" / "SKILL.md").write_text("# nested\n", encoding="utf-8")
    (skill / RENDERER_ENTRY_REL).write_text(
        "Read fully and follow: [[bmad-snapshot:sub/SKILL.md]]\n", encoding="utf-8"
    )
    problems = missing_base_skills(tmp_path, [tree])
    assert problems[0].detail["missing_sources"] == ["sub/SKILL.md"]


def test_the_inline_era_is_never_asked_for_render_sources(tmp_path):
    """A pre-BMAD-METHOD#2601 `SKILL.md` carries its prompt inline: it has no
    `workflow.md`, declares nothing, and renders nothing. Asking it the renderer's
    questions would refuse every legitimate pre-renderer install on a check with no
    severity filter and no `--force`.

    The second half is what makes this a discriminator rather than an empty
    observation: the very same directory, with ONLY `SKILL.md` swapped for the stub,
    is reported. The silence upstream is the era, not an absence of anything to say."""
    from conftest import RENDERER_STUB_SKILL_MD, install_build_auto_skill

    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree)  # inline era: no workflow.md
    assert missing_base_skills(tmp_path, [tree]) == []

    (skill / "SKILL.md").write_text(RENDERER_STUB_SKILL_MD, encoding="utf-8")
    assert [f.check for f in missing_base_skills(tmp_path, [tree])] == [
        "skills.dev-renderer-sources"
    ]


def test_a_reorganized_renderer_install_is_not_refused(tmp_path):
    """The gate asks what the install DECLARES — never a fixed renderer-era file
    list. Nothing here is named `step-*` and no marker is in sight, yet the render
    graph is closed, so there is nothing to report. A hardcoded list would refuse
    this project on every run with a remediation that could not fix it, which is the
    failure mode a `--force`-less preflight cannot afford.

    Flat by design: the nested case is a separate test, because the source key's
    `as_posix()` is invisible on POSIX and this one must not depend on it."""
    from conftest import RENDERER_STUB_SKILL_MD

    from bmad_loop.install import RENDERER_ENTRY_REL, _absent_renderer_sources

    skill = tmp_path / DEV_PRIMITIVE_NEW
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(RENDERER_STUB_SKILL_MD, encoding="utf-8")
    (skill / RENDERER_ENTRY_REL).write_text(
        "run [[bmad-snapshot:clarify.md]] then [[bmad-snapshot:plan.md]]\n", encoding="utf-8"
    )
    (skill / "clarify.md").write_text("# clarify\n", encoding="utf-8")
    (skill / "plan.md").write_text("# plan\n", encoding="utf-8")

    assert _absent_renderer_sources(skill) == []


def test_a_nested_snapshot_target_is_keyed_by_its_posix_rel(tmp_path):
    """Upstream keys a source by `relative_to(skill_dir).as_posix()`, so a nested
    source is referenced as `phases/plan.md` and never by its bare name.

    Deliberately symlink-free and UNGUARDED: this is the suite's only witness for the
    `as_posix()` on the source key, and that ablation is invisible on Linux by
    construction (`os.sep == "/"`). Windows CI is the oracle — keyed with the native
    separator the source becomes `phases\\plan.md`, the prompt's `phases/plan.md`
    reads undeclared, and a healthy install is refused on every story."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import RENDERER_ENTRY_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / "phases").mkdir()
    (skill / "phases" / "plan.md").write_text("# plan\n", encoding="utf-8")
    (skill / RENDERER_ENTRY_REL).write_text(
        "Read fully and follow: [[bmad-snapshot:phases/plan.md]]\n", encoding="utf-8"
    )
    assert missing_base_skills(tmp_path, [tree]) == []

    # The REPORTED rel is echoed from the token's own text, not derived from the
    # source key, so it is POSIX on either platform under every ablation — this half
    # pins the report's shape, not `as_posix()`. The assert above is the discriminator.
    (skill / "phases" / "plan.md").unlink()
    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-sources"]
    assert problems[0].detail["missing_sources"] == ["phases/plan.md"]


def test_an_undecodable_source_does_not_crash_the_preflight(tmp_path):
    """A `.md` that is not UTF-8 is a HALT upstream too, but a different one
    (`failed to read render source`) with a different remediation — so this gate
    skips it rather than blaming it for an undeclared reference, the same fail-open
    doctrine `_is_renderer_stub` and `_renderer_unit_required` follow.

    What must not happen is the `UnicodeDecodeError` escaping into
    `missing_base_skills`: a crashed `validate` names no file at all, which is
    strictly worse than the finding it replaced. Cross-platform on purpose — no
    chmod, so the Windows lane runs it too."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import RENDERER_ENTRY_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / "binary.md").write_bytes(b"\xff\xfe not utf-8\n")
    assert missing_base_skills(tmp_path, [tree]) == []

    # …and it is still a DECLARED source: the name is readable even when the bytes
    # are not, so a prompt naming it is not reported as pointing at nothing
    (skill / RENDERER_ENTRY_REL).write_text(
        "Read fully and follow: [[bmad-snapshot:binary.md]]\n", encoding="utf-8"
    )
    assert missing_base_skills(tmp_path, [tree]) == []


def test_a_source_that_is_not_a_readable_file_is_not_a_source(tmp_path):
    """Upstream requires each candidate to BE a file (`render source is missing or not
    a file`) and this mirrors that, which is what keeps three failure shapes out at
    once. A `workflow.md` that is a directory — the portable case, so Windows CI
    witnesses it — HALTs upstream and must not read as present here.

    The same conjunct is what stops a FIFO in the tree from blocking `read_text`
    forever. That one has no test: making a preflight hang is a poor thing to ask CI
    to reproduce, and `os.mkfifo` does not exist on Windows. It is named in the
    helper's docstring instead, because an unbounded hang is the one failure mode
    that is neither a false green nor a false refusal."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import RENDERER_ENTRY_REL, missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / RENDERER_ENTRY_REL).unlink()
    (skill / RENDERER_ENTRY_REL).mkdir()

    problems = missing_base_skills(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.dev-renderer-sources"]
    assert problems[0].detail["missing_sources"] == [RENDERER_ENTRY_REL]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_an_unreadable_source_does_not_crash_the_preflight(tmp_path):
    """The `OSError` half of the read guard, which the undecodable-bytes test cannot
    reach — that one only ever raises `UnicodeDecodeError`. A chmod-000 `.md` passes
    `is_file()` and then raises `PermissionError` from `read_text`; unguarded it
    escapes into `missing_base_skills` and `validate` tracebacks instead of reporting,
    which is strictly worse than the finding it replaced. POSIX-only, and it assumes
    a non-root runner (root reads through mode 000)."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    locked = skill / "locked.md"
    locked.write_text("# locked\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert missing_base_skills(tmp_path, [tree]) == []
    finally:
        locked.chmod(0o644)


def test_customization_prose_is_never_mined_for_snapshot_tokens(tmp_path):
    """Upstream loads only `*.md` as sources and says so where it substitutes
    ("Inserted paths and customization prose are never scanned as source tokens"), so
    a token quoted inside `customize.toml`'s `instruction` blocks is left as literal
    text and renders fine. Widening the source glob past `*.md` would mine that prose
    and refuse every run of a healthy project — the same false positive a looser token
    regex would cause, on a gate with no `--force`."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import missing_base_skills

    tree = get_profile("claude").skill_tree
    _install_hunters(tmp_path, tree)
    _install_renderer_surface(tmp_path)
    skill = install_build_auto_skill(tmp_path, tree, renderer_stub=True)
    (skill / "customize.toml").write_text(
        '[[review_layers]]\nid = "adversarial"\ninstruction = """\n'
        "Do not read [[bmad-snapshot:step-99-not-a-source.md]] directly.\n"
        '"""\n',
        encoding="utf-8",
    )

    assert missing_base_skills(tmp_path, [tree]) == []


def test_dev_primitive_warnings_flag_an_orphaned_legacy_customize_file(tmp_path):
    """The rename does not migrate `_bmad/custom/<skill>.toml`, so an upgraded
    project silently stops applying its overrides."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import CUSTOMIZE_DIR_REL, dev_primitive_warnings

    tree = get_profile("claude").skill_tree
    custom = tmp_path / CUSTOMIZE_DIR_REL
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "bmad-dev-auto.toml").write_text("x\n", encoding="utf-8")

    # a PRE-rename project: that file is the correct one — warning here would be
    # noise on every legacy install (ablation: drop the resolved-is-new guard)
    _install_legacy_primitive(tmp_path, tree)
    assert dev_primitive_warnings(tmp_path, [tree]) == []

    install_build_auto_skill(tmp_path, tree)
    warnings = dev_primitive_warnings(tmp_path, [tree])
    assert [f.check for f in warnings] == ["skills.customize-legacy"]
    assert warnings[0].severity == "warning"
    assert warnings[0].detail["files"] == [f"{CUSTOMIZE_DIR_REL}/bmad-dev-auto.toml"]

    # a counterpart under the new name means the operator already migrated
    (custom / "bmad-build-auto.toml").write_text("x\n", encoding="utf-8")
    assert dev_primitive_warnings(tmp_path, [tree]) == []

    # .user.toml is tracked separately — migrating one does not cover the other
    (custom / "bmad-dev-auto.user.toml").write_text("x\n", encoding="utf-8")
    warnings = dev_primitive_warnings(tmp_path, [tree])
    assert warnings[0].detail["files"] == [f"{CUSTOMIZE_DIR_REL}/bmad-dev-auto.user.toml"]


def test_dev_primitive_warnings_are_silent_when_nothing_resolves(tmp_path):
    """missing_base_skills owns the unresolvable story; the warning must not pile
    advisory noise on top of a hard preflight failure.

    The customize site is armed here — an orphaned `bmad-dev-auto.toml` beside a shim
    — so the silence is the resolution guard's doing and not merely the absence of
    anything to say. The shim's SKILL.md is also a renderer stub, which pins that the
    renderer probes moved out of this function entirely rather than merely going
    quiet here (see test_renderer_checks_are_silent_when_nothing_resolves)."""
    from conftest import RENDERER_STUB_SKILL_MD, install_dev_shim

    from bmad_loop.install import CUSTOMIZE_DIR_REL, dev_primitive_warnings

    tree = get_profile("claude").skill_tree
    custom = tmp_path / CUSTOMIZE_DIR_REL
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "bmad-dev-auto.toml").write_text("x\n", encoding="utf-8")
    shim = install_dev_shim(tmp_path, tree)
    (shim / "SKILL.md").write_text(RENDERER_STUB_SKILL_MD, encoding="utf-8")

    assert dev_primitive_warnings(tmp_path, [tree]) == []


def test_customize_legacy_warning_is_emitted_once_across_trees(tmp_path):
    """`_bmad/custom/` is project-global, so two trees resolving to the new name
    must not double-report the same orphaned file."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import CUSTOMIZE_DIR_REL, dev_primitive_warnings

    claude, codex = get_profile("claude").skill_tree, get_profile("codex").skill_tree
    custom = tmp_path / CUSTOMIZE_DIR_REL
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "bmad-dev-auto.toml").write_text("x\n", encoding="utf-8")
    for tree in (claude, codex):
        install_build_auto_skill(tmp_path, tree)

    warnings = dev_primitive_warnings(tmp_path, [claude, codex])
    assert [f.check for f in warnings] == ["skills.customize-legacy"]


def test_missing_stories_support_probes_the_resolved_primitive(tmp_path):
    """Stories mode's content probe follows resolution: a dispatch-capable
    bmad-build-auto satisfies it even though the constant still names the legacy
    skill, and a legacy step-01 beside it does NOT (that skill isn't driven)."""
    from conftest import install_build_auto_skill

    from bmad_loop.install import STORIES_PROBE_FILE, STORIES_PROBE_TEXT, missing_stories_support

    tree = get_profile("claude").skill_tree
    install_build_auto_skill(tmp_path, tree, folder_id=True)
    assert missing_stories_support(tmp_path, [tree]) == []

    # strip the marker from the RESOLVED skill and hide a good one in the legacy
    # skill: the probe must report stale, not be satisfied by the wrong skill
    (tmp_path / tree / "bmad-build-auto" / STORIES_PROBE_FILE).write_text("old\n")
    legacy = _install_legacy_primitive(tmp_path, tree)
    (legacy / STORIES_PROBE_FILE).write_text(f"**{STORIES_PROBE_TEXT}**\n", encoding="utf-8")

    problems = missing_stories_support(tmp_path, [tree])
    assert [f.check for f in problems] == ["skills.stories-dispatch-stale"]
    assert problems[0].detail["skill"] == "bmad-build-auto"


def test_provision_worktree_carries_the_new_primitives_subdirectories(tmp_path):
    """Adding bmad-build-auto to BASE_SKILLS is what keeps isolation working across
    the rename. The copy is a per-FILE merge that still walks into subdirectories, so
    review-prompts/ (which customize.toml's layers point at) rides along — a
    markers-only copy would leave every review layer unresolvable inside the
    worktree."""
    from conftest import install_build_auto_skill

    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    tree = claude.skill_tree
    install_build_auto_skill(repo, tree)

    provision_worktree(wt, [claude], repo)

    assert (wt / tree / "bmad-build-auto" / "SKILL.md").is_file()
    assert (wt / tree / "bmad-build-auto" / "customize.toml").is_file()
    assert (wt / tree / "bmad-build-auto" / "review-prompts" / "adversarial.md").is_file()


def test_missing_stories_support_probes_step01_content(tmp_path):
    from bmad_loop.install import (
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        missing_stories_support,
    )

    claude = get_profile("claude")
    tree = claude.skill_tree
    step01 = tmp_path / tree / STORIES_PROBE_SKILL / STORIES_PROBE_FILE

    # step-01 absent → reported (older/half install)
    problems = missing_stories_support(tmp_path, [tree])
    assert len(problems) == 1 and "not found" in problems[0].message

    # present but WITHOUT the folder+id dispatch marker (a pre-#2549 skill)
    step01.parent.mkdir(parents=True, exist_ok=True)
    step01.write_text("# Step 1\nold clarify-and-route, no dispatch protocol\n", encoding="utf-8")
    problems = missing_stories_support(tmp_path, [tree])
    assert len(problems) == 1 and "folder+id dispatch" in problems[0].message

    # present WITH the marker → OK
    step01.write_text("route a **folder+id dispatch** invocation\n", encoding="utf-8")
    assert missing_stories_support(tmp_path, [tree]) == []


def test_missing_base_skills_findings_carry_ids_and_detail(tmp_path):
    """#205: the problems are Findings, so `validate --json` can key on the check id
    rather than on remediation prose. The two failure modes are distinct ids, and
    `missing_markers` is a list — the message's ", " join is a rendering of it, and
    a consumer must not have to split a separator the message is free to change."""
    from bmad_loop.checks import VALIDATE_CHECKS

    claude = get_profile("claude")
    absent = missing_base_skills(tmp_path, [claude.skill_tree])
    assert {f.check for f in absent} == {"skills.base-missing"}
    assert all(f.severity == "problem" for f in absent)
    assert all(f.check in VALIDATE_CHECKS for f in absent)
    # the primitive is reported under the CURRENT upstream name — a fresh install
    # follows the rename, so pointing an operator at bmad-dev-auto would misdirect
    assert {f.detail["skill"] for f in absent} == {
        "bmad-build-auto",
        "bmad-review-adversarial-general",
        "bmad-review-edge-case-hunter",
        "bmad-review-verification-gap",
    }
    assert all(f.detail["tree"] == claude.skill_tree for f in absent)

    _install_base_skills(tmp_path, claude.skill_tree)
    (tmp_path / claude.skill_tree / "bmad-build-auto" / "step-04-review.md").unlink()
    (tmp_path / claude.skill_tree / "bmad-build-auto" / "customize.toml").unlink()
    incomplete = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(incomplete) == 1
    assert incomplete[0].check == "skills.base-incomplete"
    # a LIST of markers, not the joined string the message renders
    assert incomplete[0].detail["missing_markers"] == ["step-04-review.md", "customize.toml"]
    for marker in incomplete[0].detail["missing_markers"]:
        assert marker in incomplete[0].message


def test_missing_stories_support_findings_split_absent_from_stale(tmp_path):
    """#205: a half install and a too-old install are different conditions with
    different remediations, so they get different check ids — a script pinning a
    version bump must be able to tell "reinstall" from "update"."""
    from bmad_loop.checks import VALIDATE_CHECKS
    from bmad_loop.install import (
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        STORIES_PROBE_TEXT,
        missing_stories_support,
    )

    claude = get_profile("claude")
    tree = claude.skill_tree
    step01 = tmp_path / tree / STORIES_PROBE_SKILL / STORIES_PROBE_FILE

    absent = missing_stories_support(tmp_path, [tree])
    assert [f.check for f in absent] == ["skills.stories-dispatch-missing"]
    assert absent[0].detail == {
        "tree": tree,
        "skill": STORIES_PROBE_SKILL,
        "file": STORIES_PROBE_FILE,
    }

    step01.parent.mkdir(parents=True, exist_ok=True)
    step01.write_text("old clarify-and-route, no dispatch protocol\n", encoding="utf-8")
    stale = missing_stories_support(tmp_path, [tree])
    assert [f.check for f in stale] == ["skills.stories-dispatch-stale"]
    assert stale[0].detail["marker"] == STORIES_PROBE_TEXT
    assert all(f.check in VALIDATE_CHECKS for f in (*absent, *stale))


def test_missing_stories_support_reports_non_utf8_probe_without_crashing(tmp_path):
    """C1: a binary/non-UTF-8 step-01 file must be reported as a problem, not crash
    the preflight — read_text(encoding="utf-8") raises UnicodeDecodeError (a
    ValueError, NOT an OSError), so the content probe has to catch it explicitly."""
    from bmad_loop.install import (
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        missing_stories_support,
    )

    claude = get_profile("claude")
    tree = claude.skill_tree
    step01 = tmp_path / tree / STORIES_PROBE_SKILL / STORIES_PROBE_FILE
    step01.parent.mkdir(parents=True, exist_ok=True)
    step01.write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80\x81")  # invalid UTF-8

    problems = missing_stories_support(tmp_path, [tree])
    assert len(problems) == 1 and "not found" in problems[0].message


@pytest.mark.parametrize("primitive", ["bmad-build-auto", "bmad-dev-auto"])
def test_new_dev_auto_skill_is_additive_for_sprint_mode(tmp_path, primitive):
    """Scenario 6 additivity: installing the *new* dev primitive (folder+id
    dispatch present) satisfies both preflights — sprint mode's file-existence
    check (`missing_base_skills`, which never inspects the dispatch content) and
    stories mode's content probe (`missing_stories_support`). The new skill
    breaks neither pipeline.

    Parametrized over both upstream names because the stories probe reads the
    RESOLVED primitive: writing the dispatch marker into the skill that did not
    resolve must not satisfy it (and does not — the other leg would fail)."""
    from bmad_loop.install import STORIES_PROBE_FILE, missing_stories_support

    claude = get_profile("claude")
    tree = claude.skill_tree
    _install_base_skills(tmp_path, tree)
    if primitive == "bmad-dev-auto":
        shutil.rmtree(tmp_path / tree / "bmad-build-auto")  # pre-rename project
    # upgrade the resolved primitive in place to the folder+id dispatch version
    step01 = tmp_path / tree / primitive / STORIES_PROBE_FILE
    step01.write_text("route a **folder+id dispatch** invocation\n", encoding="utf-8")

    # sprint mode (file existence) is unaffected by the new dispatch content …
    assert missing_base_skills(tmp_path, [tree]) == []
    # … and stories mode now also passes its stricter content probe
    assert missing_stories_support(tmp_path, [tree]) == []


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


def test_provision_worktree_reports_seed_skipped_as_noop(tmp_path):
    """A seed entry left untouched because the destination exists is REPORTED, so a
    `worktree_seed` that copies nothing cannot look like applied configuration."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text("FROM_REPO", encoding="utf-8")
    dst = wt / ".mcp.json"
    dst.parent.mkdir(parents=True)
    dst.write_text("IN_WORKTREE", encoding="utf-8")
    assert provision_worktree(wt, [], repo, seed_files=[".mcp.json"]) == [".mcp.json"]


def test_provision_worktree_reports_seed_dir_skipped_whole(tmp_path):
    """The case that motivated the report: a worktree checks out tracked files, so a
    seed DIRECTORY with any tracked child already exists and the whole entry is
    skipped — including children that are absent and would clobber nothing.

    Anchored on a NEUTRAL directory: this pins the generic `seed_files` contract,
    which is unchanged. `_bmad` used to stand in for it and no longer can — that
    one entry now gets a per-file merge of its own (see the _bmad seeding tests),
    which is exactly the behaviour this test would otherwise be asserting away."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "vendor" / "bin").mkdir(parents=True)  # tracked child
    (repo / "vendor" / "conf").mkdir()  # gitignored sibling, absent from the checkout
    (repo / "vendor" / "conf" / "tool.yaml").write_text("SEED ME", encoding="utf-8")
    (wt / "vendor" / "bin").mkdir(parents=True)  # what `git worktree add` lays down

    assert provision_worktree(wt, [], repo, seed_files=["vendor"]) == ["vendor"]
    assert not (wt / "vendor" / "conf").exists()  # documents today's behaviour


def test_provision_worktree_reports_nothing_when_seeding_succeeds(tmp_path):
    """A seed that actually copies is not reported — the signal stays specific to
    entries that silently did nothing. A missing source is also not a no-op report:
    it is already covered as its own case."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text("FROM_REPO", encoding="utf-8")
    assert provision_worktree(wt, [], repo, seed_files=[".mcp.json", "absent.json"]) == []
    assert (wt / ".mcp.json").read_text() == "FROM_REPO"


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


# ----------------------------------------------------------------- hookless profiles


def test_install_into_hookless_skips_hook_registration(tmp_path, capsys):
    """A hookless profile (opencode-http) gets skills but never a hook config —
    there is nothing to register for an HTTP/SSE-transport adapter."""
    assert install_into(tmp_path, clis=("opencode-http",)) == 0
    for skill in MODULE_SKILLS:
        assert (tmp_path / ".claude" / "skills" / skill / "SKILL.md").is_file()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert "no hooks needed (opencode-http)" in capsys.readouterr().out


def test_install_resolves_opencode_alias(tmp_path):
    assert install_into(tmp_path, clis=("opencode",)) == 0
    assert (tmp_path / ".claude" / "skills" / "bmad-loop-sweep" / "SKILL.md").is_file()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_provision_worktree_hookless_skips_hook_merge(tmp_path):
    """Worktree provisioning for a hookless profile lays down the skill tree but
    writes no hook config (and still nothing into the worktree's .bmad-loop/)."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    opencode = get_profile("opencode-http")
    provision_worktree(wt, [opencode], repo)

    for skill in MODULE_SKILLS:
        assert (wt / opencode.skill_tree / skill / "SKILL.md").is_file()
    assert not (wt / ".claude" / "settings.json").exists()
    assert not (wt / ".bmad-loop").exists()


def test_provision_worktree_hookless_exclude_never_blankets_worktree(project, tmp_path):
    """A hookless profile has config_path == "", which must not become the git
    exclude pattern "/" (that would exclude the entire worktree from the unit's
    `git add -A` commit). Only the skill tree is shielded."""
    repo = project.project
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("opencode-http")], repo)

    lines = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert "/.claude/skills" in lines
    assert "/" not in lines


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


# ----------------------------------------------------------------- _bmad/ config surface


def _write_bmad_surface(repo):
    """The minimum renderer-era `_bmad/` a project carries: the central config plus
    the two-file script unit (render_skill.py bare-imports config_utils off
    sys.path[0], so a partial seed loses even the `HALT:` contract line)."""
    (repo / "_bmad" / "scripts").mkdir(parents=True)
    (repo / "_bmad" / "config.toml").write_text("[core]\n", encoding="utf-8")
    (repo / "_bmad" / "scripts" / "render_skill.py").write_text("# render", encoding="utf-8")
    (repo / "_bmad" / "scripts" / "config_utils.py").write_text("# config", encoding="utf-8")


def test_provision_worktree_seeds_bmad_when_absent(tmp_path):
    """A worktree checks out tracked files only, so a gitignored `_bmad/` is missing
    from it — and the renderer is handed the worktree as its project root and hard
    fails when that root has no `_bmad/`. Seed the whole surface in."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (repo / "_bmad" / "custom" / "nested").mkdir(parents=True)
    (repo / "_bmad" / "custom" / "nested" / "over.toml").write_text("x", encoding="utf-8")

    assert provision_worktree(wt, [], repo) == []

    assert (wt / "_bmad" / "config.toml").read_text() == "[core]\n"
    # the whole scripts unit, not a curated file: the sibling import must resolve
    assert (wt / "_bmad" / "scripts" / "render_skill.py").is_file()
    assert (wt / "_bmad" / "scripts" / "config_utils.py").is_file()
    assert (wt / "_bmad" / "custom" / "nested" / "over.toml").read_text() == "x"


def test_provision_worktree_bmad_merge_fills_only_missing_files(tmp_path):
    """Per-FILE merge into a checkout that COMMITS its `_bmad/`: the tracked files
    are left byte-identical (nothing new merges back) and only the gitignored
    layers are filled in. A whole-dir copy-when-absent would skip the lot."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (repo / "_bmad" / "config.user.toml").write_text("USER_LAYER", encoding="utf-8")
    # what `git worktree add` lays down: the tracked half only
    (wt / "_bmad" / "scripts").mkdir(parents=True)
    (wt / "_bmad" / "config.toml").write_text("COMMITTED", encoding="utf-8")
    (wt / "_bmad" / "scripts" / "render_skill.py").write_text("COMMITTED", encoding="utf-8")

    provision_worktree(wt, [], repo)

    assert (wt / "_bmad" / "config.toml").read_text() == "COMMITTED"
    assert (wt / "_bmad" / "scripts" / "render_skill.py").read_text() == "COMMITTED"
    # the gitignored layer + the untracked sibling script were filled in
    assert (wt / "_bmad" / "config.user.toml").read_text() == "USER_LAYER"
    assert (wt / "_bmad" / "scripts" / "config_utils.py").read_text() == "# config"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_seeds_a_symlinked_bmad_subdir(tmp_path):
    """F2: `_bmad/` gets the skill trees' walk semantics. `rglob` does not descend a
    symlinked SUB-directory, which is how a shared BMAD install is commonly wired one
    level in (`_bmad/scripts/lib -> …`), so the whole subtree was copied as nothing —
    and `_bmad_scripts_seed_incomplete`, mirroring the same rglob, agreed nothing was
    missing. The under-seed and its own detector were wrong together, which is why both
    moved to the shared walk in one change rather than one at a time.

    The link points INSIDE the repo, which is the case the change actually reverses:
    one pointing out is still dropped file-by-file by the containment guard — but the
    gate can now SEE it, which the sibling test pins."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    real = repo / "shared-lib"
    real.mkdir()
    (real / "helper.py").write_text("# helper", encoding="utf-8")
    (repo / "_bmad" / "scripts" / "lib").symlink_to(real, target_is_directory=True)

    assert provision_worktree(wt, [], repo) == []

    assert (wt / "_bmad" / "scripts" / "lib" / "helper.py").read_text() == "# helper"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_reports_a_symlinked_out_scripts_subdir(tmp_path):
    """The detector half of the same change. A `_bmad/scripts/lib` pointed at a shared
    install outside the repo is dropped file-by-file by the containment guard, exactly
    as before — what changes is that the gate now walks the same way the seed does and
    can see the files that were dropped.

    Under the old rglob mirror the directory was yielded but never descended, so its
    `is_file()` was False, the filter dropped it, and two rglobs agreed the seed was
    complete. Two rglobs agreeing is not a check. A partial `_bmad/scripts/` is worse
    than none: the bare `config_utils` import fails above the renderer's try/except and
    even the `HALT:` line is lost."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "helper.py").write_text("# helper", encoding="utf-8")
    (repo / "_bmad" / "scripts" / "lib").symlink_to(shared, target_is_directory=True)

    skipped = provision_worktree(wt, [], repo)

    assert skipped == ["_bmad/scripts"]
    assert not (wt / "_bmad" / "scripts" / "lib").exists()
    assert (wt / "_bmad" / "scripts" / "render_skill.py").is_file()  # the rest landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_reports_an_unreadable_scripts_subdir(tmp_path):
    """The `is_dir()` half of the scripts gate's source filter — the same predicate
    `_absent_skill_files` carries, on the other seed leg.

    C4, the silent one: a repo `_bmad/scripts/lib` that is simply unreadable was copied
    as nothing AND reported complete, both halves on `rglob`, wrong together. The seed
    cannot deliver a directory it cannot list, and this must not stay quiet about it.
    POSIX-only, and it assumes a non-root runner."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    locked = repo / "_bmad" / "scripts" / "lib"
    locked.mkdir()
    (locked / "helper.py").write_text("# helper", encoding="utf-8")
    locked.chmod(0o000)
    try:
        skipped = provision_worktree(wt, [], repo)
    finally:
        locked.chmod(0o755)

    assert skipped == ["_bmad/scripts"]
    assert not (wt / "_bmad" / "scripts" / "lib").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_survives_an_unreadable_bmad_root(tmp_path):
    """The one `iterdir` the shared walk does not own — `_seed_bmad_tree` lists the
    `_bmad/` root itself, before the walk starts, to apply `BMAD_SEED_EXCLUDES` — so it
    needs its own `try`.

    Returning `[]` is the same doctrine as the leaf below it, and safe for the same
    reason the sibling scripts predicate goes quiet on this fault: no run reaches
    provisioning with a `_bmad/` it cannot read, because the run-start preflight sees it
    two files in (`render_skill.py` is not a readable file either). ⚠️ Measured, the
    preflight says so in two shapes — `skills.dev-renderer` + `-config` on 3.14, a
    traceback out of `validate`/`run` through 3.13. Loud either way; only provisioning
    had to be made total, and this pins that it is.

    The central-config predicate is in the blast radius and is asserted here rather than
    separately: its bare `is_file()` raised straight back out of `provision_worktree`
    two calls after the `try` that prevented the same crash. POSIX-only, non-root."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (repo / "_bmad").chmod(0o000)
    try:
        skipped = provision_worktree(wt, [], repo)
    finally:
        (repo / "_bmad").chmod(0o755)

    assert skipped == []
    assert not (wt / "_bmad").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_provision_worktree_survives_an_unreadable_bmad_file(tmp_path):
    """The leaf `try` on this leg: one unreadable file under `_bmad/` is a per-file
    skip, not the end of the run — and every sibling still lands.

    The file is reported through the channel that already existed for it, so the
    degradation is visible: `_central_config_seed_incomplete` re-asks the RESULT and
    finds the worktree without a `config.toml`, which is a `required=True` layer for
    `load_central_config` and would HALT every story. POSIX-only, non-root runner."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    locked = repo / "_bmad" / "config.toml"
    locked.chmod(0o000)
    try:
        skipped = provision_worktree(wt, [], repo)
    finally:
        locked.chmod(0o644)

    assert skipped == ["_bmad/config.toml"]
    assert not (wt / "_bmad" / "config.toml").exists()
    assert (wt / "_bmad" / "scripts" / "render_skill.py").is_file()  # the rest landed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_never_writes_through_a_dangling_bmad_symlink(tmp_path):
    """`_occupied` on the `_bmad/` leg, where the checkout genuinely commits its
    `_bmad/` surface and can therefore check out a symlink whose target this machine
    does not have.

    Writing through it lands `config.toml`'s bytes at the link's target and leaves the
    completeness gate reading green through the now-live link — a false green on the one
    file `load_central_config` requires. Skipping turns that into an honest
    `_bmad/config.toml` in `skipped`, which the engine already knows how to escalate."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (wt / "_bmad").mkdir(parents=True)
    elsewhere = wt / "_bmad" / "not-checked-out.toml"
    (wt / "_bmad" / "config.toml").symlink_to(elsewhere)

    skipped = provision_worktree(wt, [], repo)

    assert not elsewhere.exists()  # the bytes never landed at the link's target
    assert skipped == ["_bmad/config.toml"]


def test_provision_worktree_never_seeds_render_output(tmp_path):
    """`_bmad/render/` is the renderer's published output, keyed on a hash of the
    project root's absolute path — seeding the main checkout's copy would carry ITS
    paths in. Excluded before descending, so a huge render/ is never even walked."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    snapshot = repo / "_bmad" / "render" / "bmad-build-auto" / "repo-abc123" / "deadbeef"
    snapshot.mkdir(parents=True)
    (snapshot / "workflow.md").write_text("STALE", encoding="utf-8")

    provision_worktree(wt, [], repo)

    assert (wt / "_bmad" / "config.toml").is_file()  # the rest still seeded
    assert not (wt / "_bmad" / "render").exists()


def test_provision_worktree_shields_whole_bmad_when_worktree_lacked_it(project, tmp_path):
    """The worktree had no `_bmad/`, so everything under it is ours: shield the ROOT
    with one `/_bmad` line rather than each seeded file.

    Asserting the exact line matters — `git status` comes back clean either way
    (per-file lines cover today's files), but the root line is what also shields the
    files a session's renderer/config writes create AFTER provisioning."""
    repo = project.project
    _write_bmad_surface(repo)
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("claude")], repo)

    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert "/_bmad" in exclude
    assert not [line for line in exclude if line.startswith("/_bmad/scripts")]
    assert git(wt, "status", "--short", "--", "_bmad") == ""


def test_provision_worktree_shields_only_seeded_files_when_bmad_committed(project, tmp_path):
    """A checkout that COMMITS its `_bmad/` gets the individual seeded rels — shield
    exactly what we wrote. A blanket `/_bmad` would hide the project's own tracked
    config surface from the unit's `git add -A` for the rest of the run."""
    repo = project.project
    _write_bmad_surface(repo)
    (repo / "_bmad" / "config.user.toml").write_text("USER_LAYER", encoding="utf-8")
    git(repo, "add", "-A", "--", "_bmad/config.toml", "_bmad/scripts")
    git(repo, "commit", "-q", "-m", "commit the _bmad surface")
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("claude")], repo)

    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert "/_bmad/config.user.toml" in exclude
    assert "/_bmad" not in exclude
    assert git(wt, "status", "--short", "--", "_bmad") == ""


def test_provision_worktree_render_shield_only_when_bmad_present(project, tmp_path):
    """`/_bmad/render/` is shielded whenever the worktree has a `_bmad/` — the
    renderer rewrites that dir mid-session, long after provisioning. It is NOT
    written when there is no `_bmad/` at all: `.git/info/exclude` is the COMMON git
    dir, shared with the main checkout, so an ungated line is pollution there."""
    repo = project.project
    wt_bare = tmp_path / "wt-bare"
    verify.worktree_add(repo, wt_bare, "bare", "main")

    provision_worktree(wt_bare, [get_profile("claude")], repo)

    exclude_path = repo / ".git" / "info" / "exclude"
    assert "/_bmad/render/" not in exclude_path.read_text(encoding="utf-8").splitlines()

    _write_bmad_surface(repo)
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("claude")], repo)

    assert "/_bmad/render/" in exclude_path.read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_bmad_seed_rejects_symlink_escape(tmp_path):
    """A `_bmad/` entry resolving outside the repo is never copied in — the same
    resolve-and-contain guard the seed_files loop uses. Whole-tree seeding is
    exactly where an unguarded walk would drag a shared install's files across."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.toml").write_text("SECRET", encoding="utf-8")
    (repo / "_bmad" / "escape.toml").symlink_to(tmp_path / "outside" / "secret.toml")

    provision_worktree(wt, [], repo)

    assert (wt / "_bmad" / "config.toml").is_file()  # the contained files still seeded
    assert not (wt / "_bmad" / "escape.toml").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_reports_bmad_scripts_not_seeded(tmp_path):
    """A SYMLINKED `_bmad/scripts/` (how a shared BMAD install is wired) resolves
    outside the repo, so the contain guard drops every file under it and the dir
    lands EMPTY while provisioning otherwise reports success. The post-seed
    completeness guard turns that into a reported skip the caller journals."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    shared = tmp_path / "shared-bmad" / "scripts"
    shared.mkdir(parents=True)
    (shared / "render_skill.py").write_text("# render", encoding="utf-8")
    (shared / "config_utils.py").write_text("# config", encoding="utf-8")
    (repo / "_bmad").mkdir(parents=True)
    (repo / "_bmad" / "config.toml").write_text("[core]\n", encoding="utf-8")
    (repo / "_bmad" / "scripts").symlink_to(shared, target_is_directory=True)

    skipped = provision_worktree(wt, [], repo)

    assert skipped == ["_bmad/scripts"]
    assert (wt / "_bmad" / "config.toml").is_file()  # the rest looked like success
    assert not (wt / "_bmad" / "scripts" / "render_skill.py").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_provision_worktree_reports_a_dropped_central_config(tmp_path):
    """The config leg of the same completeness check, and the case the scripts leg
    cannot reach: `_bmad/scripts/` is a REAL directory and seeds whole, while
    `_bmad/config.toml` alone is symlinked out of the repo (a shared BMAD install
    where only the config is centralised). The contain guard drops it with a bare
    `continue`, `_seed_bmad_tree` still returns "the whole tree is ours", and the
    repo-side preflight follows the symlink and passes — so without this leg the
    run is 100% silent and burns the entire backlog on result-less Stops."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    shared = tmp_path / "shared-bmad"
    shared.mkdir()
    (shared / "config.toml").write_text("[core]\n", encoding="utf-8")
    (repo / "_bmad" / "scripts").mkdir(parents=True)
    (repo / "_bmad" / "scripts" / "render_skill.py").write_text("# render", encoding="utf-8")
    (repo / "_bmad" / "scripts" / "config_utils.py").write_text("# config", encoding="utf-8")
    (repo / "_bmad" / "config.toml").symlink_to(shared / "config.toml")
    # the repo-side probe follows the symlink, which is why the preflight passed
    assert (repo / "_bmad" / "config.toml").is_file()

    skipped = provision_worktree(wt, [], repo)

    assert skipped == ["_bmad/config.toml"]
    assert not (wt / "_bmad" / "config.toml").exists()
    # …while the scripts unit landed whole, so the scripts leg says nothing
    assert (wt / "_bmad" / "scripts" / "render_skill.py").is_file()
    assert (wt / "_bmad" / "scripts" / "config_utils.py").is_file()


def test_provision_worktree_central_config_check_asks_is_file_not_exists(tmp_path):
    """The same leg, armed symlink-free so it runs on WINDOWS — where the sentinel
    path has never executed once, and where this PR already shipped a
    POSIX-invisible bug. Copy-when-absent only asks `dst.exists()`, so a destination
    occupied by anything that is not a file leaves the worktree without a readable
    config while the seed reports success. Also pins the worktree probe to
    `is_file()`: relaxing it to `exists()` would call this worktree healthy."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (wt / "_bmad" / "config.toml").mkdir(parents=True)

    assert provision_worktree(wt, [], repo) == ["_bmad/config.toml"]

    # …and it clears the moment the worktree really carries the file
    wt2 = tmp_path / "wt2"
    assert provision_worktree(wt2, [], repo) == []
    assert (wt2 / "_bmad" / "config.toml").is_file()


def test_provision_worktree_never_lets_a_seed_entry_forge_a_sentinel(tmp_path):
    """`RENDERER_SEED_SENTINELS` are the completeness checks' to emit, never a
    user's to forge. A `worktree_seed` entry spelling one exactly — both are natural
    things to write, and 0.9.0's docs taught the `_bmad` family — lands in `skipped`
    the moment the checkout already carries the path, and the `_is_under_bmad` strip
    above is conditional on the merge having seeded SOMETHING, which a checkout that
    commits its whole `_bmad/` never does. The engine would then read a forged
    sentinel and CRITICAL-escalate a completely healthy worktree."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    _write_bmad_surface(wt)  # a checkout that commits its whole _bmad/: merge is a no-op

    skipped = provision_worktree(
        wt, [], repo, seed_files=["_bmad/scripts", "_bmad/config.toml", "vendor"]
    )

    assert skipped == []
    # the worktree really is healthy — neither check has anything to say about it
    assert (wt / "_bmad" / "config.toml").is_file()
    assert (wt / "_bmad" / "scripts" / "config_utils.py").is_file()


def test_provision_worktree_bmad_merge_unreports_the_documented_seed_workaround(tmp_path):
    """0.9.0 documented `worktree_seed = ["_bmad"]` as the workaround for this gap.
    That entry is now a no-op BECAUSE the merge already covered it, so reporting it
    would journal worktree-seed-skipped for a seed that did apply."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)
    (wt / "_bmad").mkdir(parents=True)  # tracked child makes the whole entry a no-op

    assert provision_worktree(wt, [], repo, seed_files=["_bmad", ".mcp.json"]) == []
    assert (wt / "_bmad" / "scripts" / "config_utils.py").is_file()


def test_provision_worktree_empty_profiles_still_seeds_bmad(tmp_path):
    """The early return has to let a repo with a `_bmad/` through even when nothing
    else is configured: a plugin-free, seed-free project on the renderer-era
    primitive is exactly the cohort this fixes."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    _write_bmad_surface(repo)

    provision_worktree(wt, [], repo)

    assert (wt / "_bmad" / "scripts" / "render_skill.py").is_file()


# ----------------------------------------------------------------- seed file modes (issue #126)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bits")
def test_provision_worktree_seed_preserves_exec_bit(tmp_path):
    """A seeded executable (vendor/bin/*) keeps +x in the worktree — a byte-only
    copy would strip the mode and the first verify command dies rc=127."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    tool = repo / "vendor" / "bin" / "tool"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)

    provision_worktree(wt, [], repo, seed_files=["vendor/bin/tool"])

    assert (wt / "vendor" / "bin" / "tool").stat().st_mode & 0o111


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bits")
def test_provision_worktree_seed_globs_preserve_exec_bit(tmp_path):
    """Exec bits survive the recursive directory walk of a glob-seeded tree."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    tool = repo / "node_modules" / ".bin" / "eslint"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)

    provision_worktree(wt, [], repo, seed_globs=["node_modules/*"])

    assert (wt / "node_modules" / ".bin" / "eslint").stat().st_mode & 0o111


def test_copy_traversable_zip_source_copies_content(tmp_path):
    """The zip-import fallback: a zipfile.Path source has no .stat(), so the
    copy must stay content-only and not crash (the docstring's contract)."""
    zf_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zf_path, "w") as zf:
        zf.writestr("pkg/skill/SKILL.md", "tool")
    dst = tmp_path / "out"

    _copy_traversable(zipfile.Path(zf_path, "pkg/"), dst)

    assert (dst / "skill" / "SKILL.md").read_text() == "tool"
