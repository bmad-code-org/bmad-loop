import json
import re
import sys
import zipfile

import pytest
from conftest import git

from bmad_loop import verify
from bmad_loop.adapters.profile import get_profile
from bmad_loop.install import (
    BASE_SKILLS,
    DEV_BASE_SKILLS,
    LEGACY_MODULE_SKILLS,
    MODULE_SKILLS,
    _copy_traversable,
    install_into,
    merge_hooks,
    missing_base_skills,
    provision_worktree,
    strip_legacy_hooks,
)


def _install_skills(root, tree, catalog):
    """Lay down stubs of exactly ``catalog`` ({skill: (marker, ...)}) under root/tree.

    Takes the catalog explicitly so a test can build one precise upstream topology
    (e.g. the v6.10.0 shape: no `bmad-review`, no verification-gap) rather than the
    everything-installed superset."""
    for skill, markers in catalog.items():
        d = root / tree / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        for marker in markers:
            (d / marker).write_text("x\n", encoding="utf-8")


def _install_base_skills(root, tree=".claude/skills"):
    """Lay down stubs of the non-bundled upstream skills the orchestrator drives."""
    _install_skills(root, tree, BASE_SKILLS)


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
    # nothing installed → dev primitive + the two inline review layers reported
    # missing (the hunters are required whenever the merged reviewer is absent —
    # bmad-dev-auto's step-04 invokes them on every run)
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 3
    assert all("BMAD-METHOD >= 6.10.0" in p.message for p in problems)

    # install everything → no problems
    _install_base_skills(tmp_path, claude.skill_tree)
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []

    # remove the dev primitive's step-file marker → reported as incomplete
    (tmp_path / claude.skill_tree / "bmad-dev-auto" / "step-04-review.md").unlink()
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert "incomplete" in problems[0].message
    assert "step-04-review.md" in problems[0].message

    # restore it, then drop customize.toml (the review-layer config marker,
    # BMAD-METHOD #2535/#2550) → a pre-July bmm install is caught as incomplete
    (tmp_path / claude.skill_tree / "bmad-dev-auto" / "step-04-review.md").write_text("x\n")
    (tmp_path / claude.skill_tree / "bmad-dev-auto" / "customize.toml").unlink()
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert "incomplete" in problems[0].message
    assert "customize.toml" in problems[0].message

    # #260: verification-gap is NOT a requirement — no tagged BMAD-METHOD release
    # ships it, so removing it from an otherwise complete tree must still pass
    _install_base_skills(tmp_path, claude.skill_tree)  # re-complete everything
    import shutil as _shutil

    _shutil.rmtree(tmp_path / claude.skill_tree / "bmad-review-verification-gap")
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


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
    assert {f.detail["skill"] for f in absent} == {
        "bmad-dev-auto",
        "bmad-review-adversarial-general",
        "bmad-review-edge-case-hunter",
    }
    assert all(f.detail["tree"] == claude.skill_tree for f in absent)

    _install_base_skills(tmp_path, claude.skill_tree)
    (tmp_path / claude.skill_tree / "bmad-dev-auto" / "step-04-review.md").unlink()
    (tmp_path / claude.skill_tree / "bmad-dev-auto" / "customize.toml").unlink()
    incomplete = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(incomplete) == 1
    assert incomplete[0].check == "skills.base-incomplete"
    # a LIST of markers, not the joined string the message renders
    assert incomplete[0].detail["missing_markers"] == ["step-04-review.md", "customize.toml"]
    for marker in incomplete[0].detail["missing_markers"]:
        assert marker in incomplete[0].message


def test_merged_bmad_review_satisfies_review_layers(tmp_path):
    """#260: post-consolidation bmm installs ship the merged `bmad-review` skill, with
    the standalone hunter IDs as thin forwarders to it. The merged reviewer provides
    every lens itself, so a tree carrying it needs none of the hunters."""
    claude = get_profile("claude")
    _install_skills(
        tmp_path,
        claude.skill_tree,
        {"bmad-dev-auto": DEV_BASE_SKILLS["bmad-dev-auto"], "bmad-review": ()},
    )
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []

    # ...but it never substitutes for the dev primitive
    import shutil as _shutil

    _shutil.rmtree(tmp_path / claude.skill_tree / "bmad-dev-auto")
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert problems[0].detail["skill"] == "bmad-dev-auto"


def test_verification_gap_never_required(tmp_path):
    """#260: the latest-release (v6.10.0) shape — dev primitive + the two review
    layers that release actually ships, no `bmad-review-verification-gap` (no tagged
    release has ever shipped it) and no merged reviewer — must validate. Requiring it
    made `validate` (and the run/resume/sweep preflight) unsatisfiable everywhere."""
    claude = get_profile("claude")
    _install_skills(tmp_path, claude.skill_tree, DEV_BASE_SKILLS)
    # guard the topology: neither escape hatch is present, so [] below is earned by
    # verification-gap not being required, not by the merged-reviewer bypass
    assert not (tmp_path / claude.skill_tree / "bmad-review-verification-gap").exists()
    assert not (tmp_path / claude.skill_tree / "bmad-review").exists()

    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


def test_review_hunter_missing_without_merged_review_reported(tmp_path):
    """A genuinely broken pre-consolidation install still fails — and its message must
    not misdiagnose the cause as "bmm is not installed" (#260): bmm is exactly what
    ships the layer, so a user whose bmm is installed could not act on the old line."""
    claude = get_profile("claude")
    _install_skills(
        tmp_path,
        claude.skill_tree,
        {k: v for k, v in DEV_BASE_SKILLS.items() if k != "bmad-review-edge-case-hunter"},
    )

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert problems[0].check == "skills.base-missing"
    assert problems[0].severity == "problem"
    assert problems[0].detail == {
        "tree": claude.skill_tree,
        "skill": "bmad-review-edge-case-hunter",
    }
    assert "bmad-review-edge-case-hunter" in problems[0].message
    assert "install the BMad Method" not in problems[0].message
    assert "update bmm" in problems[0].message


# Verbatim shape of BMAD-METHOD main's bmad-dev-auto/customize.toml: four review
# layers, three invoking the merged `bmad-review` with one lens each, plus
# intent-alignment — a self-contained prompt that invokes no skill at all.
LAYER_CUSTOMIZE = """
[workflow]
implementation_handoff = "irrelevant here"

[[workflow.review_layers]]
id = "blind-hunter"
name = "Blind Hunter"
instruction = '''
Launch a subagent with no prior conversation context, with this prompt:

> Invoke the `bmad-review` skill with only the `adversarial` lens on this diff:
>
> {diff_output}
'''

[[workflow.review_layers]]
id = "edge-case-hunter"
name = "Edge Case Hunter"
instruction = '''
> Invoke the `bmad-review` skill with only the `edge-case-hunter` lens on this diff:
>
> {diff_output}
'''

[[workflow.review_layers]]
id = "verification-gap"
name = "Verification Gap Reviewer"
instruction = '''
> Invoke the `bmad-review` skill with only the `verification-gap` lens on this diff:
>
> {diff_output}
'''

[[workflow.review_layers]]
id = "intent-alignment"
name = "Intent Alignment Auditor"
instruction = '''
> You are an intent-alignment auditor. Here is the diff:
>
> {diff_output}
'''
"""

# Pre-consolidation (v6.10.0) shape: a valid customize.toml that simply has no
# review_layers section, and a step-04 that names the two reviewers it invokes.
PRE_LAYER_CUSTOMIZE = """
[workflow]
activation_steps_prepend = []
persistent_facts = ["file:{project-root}/**/project-context.md"]
on_complete = ""
"""

STEP04_NAMED = """
### Step 2: Review layers

- Launch a subagent, with this prompt:
  > Invoke the `bmad-review-adversarial-general` skill on this diff:
  > {diff_output}
- Launch a subagent, with this prompt:
  > Invoke the `bmad-review-edge-case-hunter` skill on this diff:
  > {diff_output}
"""


def _install_dev_auto(root, tree, *, customize="x\n", step04="x\n"):
    """Install bmad-dev-auto with real customize.toml / step-04 content, so the
    preflight reads the review shape it would read on a real install."""
    d = root / tree / "bmad-dev-auto"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("# bmad-dev-auto\n", encoding="utf-8")
    (d / "customize.toml").write_text(customize, encoding="utf-8")
    (d / "step-04-review.md").write_text(step04, encoding="utf-8")
    return d


def test_layer_driven_review_requires_the_merged_skill_it_names(tmp_path):
    """Post-consolidation topology: the layers invoke `bmad-review` by name, so that
    skill — and none of the standalone hunters — is what the tree must carry."""
    claude = get_profile("claude")
    _install_dev_auto(tmp_path, claude.skill_tree, customize=LAYER_CUSTOMIZE)
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})

    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


def test_layer_driven_review_reports_unresolvable_layer(tmp_path):
    """The gap this closes: a project whose customize.toml is post-consolidation but
    whose skills are the pre-consolidation standalone hunters. The old preflight was
    green here while three of the four layers would fail on every dev run."""
    claude = get_profile("claude")
    _install_dev_auto(tmp_path, claude.skill_tree, customize=LAYER_CUSTOMIZE)
    _install_skills(
        tmp_path,
        claude.skill_tree,
        {
            "bmad-review-adversarial-general": (),
            "bmad-review-edge-case-hunter": (),
            "bmad-review-verification-gap": (),
        },
    )

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert problems[0].check == "skills.review-layer-missing"
    assert problems[0].severity == "problem"
    assert problems[0].detail["skill"] == "bmad-review"
    # the three lens layers name it; intent-alignment invokes no skill at all
    assert problems[0].detail["layers"] == [
        "blind-hunter",
        "edge-case-hunter",
        "verification-gap",
    ]
    assert problems[0].detail["source"] == "customize.toml"
    assert "bmad-review" in problems[0].message


def test_review_layer_check_id_is_registered(tmp_path):
    from bmad_loop.checks import VALIDATE_CHECKS

    assert "skills.review-layer-missing" in VALIDATE_CHECKS


def test_pre_consolidation_step04_requires_the_skills_it_names(tmp_path):
    """v6.10.0 shape: no review_layers, so the requirement comes from the two skills
    step-04 invokes by name — and a tree carrying only the merged reviewer does NOT
    satisfy them, because that step-04 names the hunters, not `bmad-review`."""
    claude = get_profile("claude")
    tree = claude.skill_tree
    _install_dev_auto(tmp_path, tree, customize=PRE_LAYER_CUSTOMIZE, step04=STEP04_NAMED)
    _install_skills(
        tmp_path,
        tree,
        {"bmad-review-adversarial-general": (), "bmad-review-edge-case-hunter": ()},
    )
    assert missing_base_skills(tmp_path, [tree]) == []

    import shutil as _shutil

    _shutil.rmtree(tmp_path / tree / "bmad-review-edge-case-hunter")
    problems = missing_base_skills(tmp_path, [tree])
    assert len(problems) == 1
    assert problems[0].check == "skills.review-layer-missing"
    assert problems[0].detail["skill"] == "bmad-review-edge-case-hunter"
    assert problems[0].detail["layers"] == []
    assert problems[0].detail["source"] == "step-04-review.md"


def test_disabled_review_layer_is_not_required(tmp_path):
    """An empty `instruction` disables a layer — its skill must not be required."""
    claude = get_profile("claude")
    disabled = LAYER_CUSTOMIZE.replace(
        """instruction = '''
> Invoke the `bmad-review` skill with only the `verification-gap` lens on this diff:
>
> {diff_output}
'''""",
        'instruction = ""',
    )
    _install_dev_auto(tmp_path, claude.skill_tree, customize=disabled)
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})

    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


def test_project_override_replaces_review_layer(tmp_path):
    """`_bmad/custom/bmad-dev-auto.toml` merges arrays of tables by `id`. A layer
    overridden to run an external reviewer no longer requires the default's skill —
    requiring it anyway would be exactly the kind of false FAIL #260 was."""
    claude = get_profile("claude")
    only_one_layer = LAYER_CUSTOMIZE.split("[[workflow.review_layers]]")[0] + (
        """[[workflow.review_layers]]
id = "blind-hunter"
instruction = '''
> Invoke the `bmad-review` skill with only the `adversarial` lens on this diff:
'''
"""
    )
    _install_dev_auto(tmp_path, claude.skill_tree, customize=only_one_layer)
    assert len(missing_base_skills(tmp_path, [claude.skill_tree])) == 1

    override = tmp_path / "_bmad" / "custom" / "bmad-dev-auto.toml"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        """[[workflow.review_layers]]
id = "blind-hunter"
instruction = "Run `my-external-reviewer` via bash on the diff."
""",
        encoding="utf-8",
    )
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


def test_unreadable_customize_falls_back_to_static_catalog(tmp_path):
    """A malformed customize.toml must not crash the preflight, and must not be read
    as "no layers configured" either — fall back to the static catalog."""
    claude = get_profile("claude")
    _install_dev_auto(tmp_path, claude.skill_tree, customize="this is not = valid toml [[[")

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert {p.detail["skill"] for p in problems} == {
        "bmad-review-adversarial-general",
        "bmad-review-edge-case-hunter",
    }
    assert {p.check for p in problems} == {"skills.base-missing"}

    # ...and the merged reviewer still satisfies that fallback
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


# --- derivation vs. what the run really resolves (PR #283 review) -------------
#
# Everything below pins the preflight to BMAD's own resolver and to step-04's own
# skip rules. The failure mode being guarded is asymmetric: requiring a skill the
# run never invokes is a false FAIL (#260), and accepting a layer the run cannot
# resolve is a green validate followed by a broken review on every story.


def _write_override(root, body, *, user=False):
    """A project override of bmad-dev-auto's shipped customize.toml."""
    suffix = "user.toml" if user else "toml"
    path = root / "_bmad" / "custom" / f"bmad-dev-auto.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _layer(layer_id, skill, *, key="id", when=None, phrasing="Invoke the"):
    when_line = f'when = "{when}"\n' if when else ""
    return f"""
[[workflow.review_layers]]
{key} = "{layer_id}"
{when_line}instruction = "{phrasing} `{skill}` skill on this diff."
"""


def _severities(findings):
    return {(f.check, f.severity) for f in findings}


def test_appended_override_layer_requires_the_skill_it_names(tmp_path):
    """A new `id` appends rather than replaces, so an override that adds a reviewer
    adds a requirement — the run will invoke it, so the preflight must too."""
    claude = get_profile("claude")
    _install_dev_auto(tmp_path, claude.skill_tree, customize=LAYER_CUSTOMIZE)
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []

    _write_override(tmp_path, _layer("house-style", "bmad-review-company"))
    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert problems[0].check == "skills.review-layer-missing"
    assert problems[0].severity == "problem"
    assert problems[0].detail["skill"] == "bmad-review-company"
    assert problems[0].detail["layers"] == ["house-style"]

    # ...and installing it clears the problem, rather than the skill being
    # unreachable because it is not in any catalog this package pins.
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review-company": ()})
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


def test_standalone_verification_gap_required_when_a_layer_names_it(tmp_path):
    """Dropping bmad-review-verification-gap from the static catalog must not make
    it unrequirable: a project whose layers DO name it still needs it installed."""
    claude = get_profile("claude")
    customize = "[workflow]\n" + _layer("verification-gap", "bmad-review-verification-gap")
    _install_dev_auto(tmp_path, claude.skill_tree, customize=customize)

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert [p.detail["skill"] for p in problems] == ["bmad-review-verification-gap"]
    assert problems[0].severity == "problem"

    _install_skills(tmp_path, claude.skill_tree, {"bmad-review-verification-gap": ()})
    assert missing_base_skills(tmp_path, [claude.skill_tree]) == []


def test_pre_and_post_consolidation_trees_resolve_independently(tmp_path):
    """A project can carry a post-consolidation .claude tree and a pre-merge .agents
    one at once. Each tree's requirement comes from ITS OWN installed skill."""
    claude, codex = get_profile("claude"), get_profile("codex")
    assert claude.skill_tree != codex.skill_tree

    _install_dev_auto(tmp_path, claude.skill_tree, customize=LAYER_CUSTOMIZE)
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})
    _install_dev_auto(
        tmp_path, codex.skill_tree, customize=PRE_LAYER_CUSTOMIZE, step04=STEP04_NAMED
    )
    _install_skills(
        tmp_path,
        codex.skill_tree,
        {"bmad-review-adversarial-general": (), "bmad-review-edge-case-hunter": ()},
    )
    assert missing_base_skills(tmp_path, [claude.skill_tree, codex.skill_tree]) == []

    # the merged reviewer in the OTHER tree must not satisfy the pre-merge one
    import shutil as _shutil

    _shutil.rmtree(tmp_path / codex.skill_tree / "bmad-review-edge-case-hunter")
    problems = missing_base_skills(tmp_path, [claude.skill_tree, codex.skill_tree])
    assert len(problems) == 1
    assert problems[0].detail["tree"] == codex.skill_tree
    assert problems[0].detail["skill"] == "bmad-review-edge-case-hunter"


def test_malformed_override_warns_and_still_resolves_base_layers(tmp_path):
    """BMAD's resolver warns on an unparseable override and carries on with the
    layers below it. Falling back to a static catalog instead would preflight a
    requirement set the run does not use."""
    claude = get_profile("claude")
    _install_dev_auto(tmp_path, claude.skill_tree, customize=LAYER_CUSTOMIZE)
    _write_override(tmp_path, "this is not = valid toml [[[")

    findings = missing_base_skills(tmp_path, [claude.skill_tree])
    assert ("skills.customize-unreadable", "warning") in _severities(findings)
    # the base layers still drive the requirement: `bmad-review`, not the static
    # two-hunter catalog the old code fell back to
    problems = [f for f in findings if f.severity == "problem"]
    assert [p.detail["skill"] for p in problems] == ["bmad-review"]
    assert all(p.check == "skills.review-layer-missing" for p in problems)

    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})
    findings = missing_base_skills(tmp_path, [claude.skill_tree])
    # the broken file is still surfaced, but nothing blocks
    assert [f.severity for f in findings] == ["warning"]
    assert findings[0].detail["file"].endswith("bmad-dev-auto.toml")


def test_user_override_wins_over_team_override(tmp_path):
    """Precedence is base -> team -> user, so the personal layer decides."""
    claude = get_profile("claude")
    _install_dev_auto(
        tmp_path, claude.skill_tree, customize="[workflow]\n" + _layer("blind", "bmad-review")
    )
    _write_override(tmp_path, _layer("blind", "bmad-review-team"))
    _write_override(tmp_path, _layer("blind", "bmad-review-personal"), user=True)

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert [p.detail["skill"] for p in problems] == ["bmad-review-personal"]


def test_every_review_layer_disabled_is_reported(tmp_path):
    """Emptying every `instruction` disables every layer, and step-04 then HALTs
    blocked with 'no active review layers'. Preflight must not call that green."""
    claude = get_profile("claude")
    disabled = re.sub(
        r"instruction = '''.*?'''", 'instruction = ""', LAYER_CUSTOMIZE, flags=re.DOTALL
    )
    _install_dev_auto(tmp_path, claude.skill_tree, customize=disabled)
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(problems) == 1
    assert problems[0].check == "skills.review-layers-empty"
    assert problems[0].severity == "problem"
    assert "no active review layers" in problems[0].message


def test_code_keyed_layers_merge_by_code_like_upstream(tmp_path):
    """BMAD's resolver keys arrays of tables on `code` OR `id` — `code` first. A
    code-keyed layer that an override replaces must not survive as a requirement."""
    claude = get_profile("claude")
    _install_dev_auto(
        tmp_path,
        claude.skill_tree,
        customize="[workflow]\n" + _layer("R1", "bmad-review-old", key="code"),
    )
    _write_override(tmp_path, _layer("R1", "bmad-review-new", key="code"))

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    # replaced in place: only the override's skill is required
    assert [p.detail["skill"] for p in problems] == ["bmad-review-new"]


def test_keyless_override_item_forces_append_like_upstream(tmp_path):
    """The keyed merge is opt-in for the array as a WHOLE: one override item with no
    identifier and the resolver appends everything, leaving the base layer in place.
    Replacing by id anyway drops a reviewer the run still executes — a green
    validate followed by a review that fails on a skill nobody checked for."""
    claude = get_profile("claude")
    _install_dev_auto(
        tmp_path,
        claude.skill_tree,
        customize="[workflow]\n" + _layer("blind", "bmad-review-base"),
    )
    _write_override(
        tmp_path,
        _layer("blind", "bmad-review-replacement")
        + '\n[[workflow.review_layers]]\ninstruction = "Invoke the `bmad-review-extra` skill."\n',
    )

    problems = missing_base_skills(tmp_path, [claude.skill_tree])
    assert sorted(p.detail["skill"] for p in problems) == [
        "bmad-review-base",
        "bmad-review-extra",
        "bmad-review-replacement",
    ]
    # the id-less layer is still reported by position, so the finding is actionable
    extra = next(p for p in problems if p.detail["skill"] == "bmad-review-extra")
    assert extra.detail["layers"] == ["#3"]


@pytest.mark.parametrize("value", ['"wrong shape"', "[1, 2]", "42", "true"])
def test_non_table_workflow_does_not_crash_the_preflight(tmp_path, value):
    """Syntactically valid TOML of the wrong SHAPE used to raise AttributeError out
    of the preflight, taking validate/run/resume/sweep with it."""
    claude = get_profile("claude")
    _install_dev_auto(tmp_path, claude.skill_tree, customize=f"workflow = {value}\n")

    # no layers readable -> the static fallback, not an exception
    findings = missing_base_skills(tmp_path, [claude.skill_tree])
    assert {f.check for f in findings} == {"skills.base-missing"}


def test_when_gated_layer_warns_instead_of_blocking(tmp_path):
    """step-04 skips every layer whose `when` does not hold, and that condition is
    evaluated by the model in run context. Undecidable here, so it must not FAIL."""
    claude = get_profile("claude")
    _install_dev_auto(
        tmp_path,
        claude.skill_tree,
        customize="[workflow]\n"
        + _layer("blind", "bmad-review")
        + _layer("perf", "bmad-review-performance", when="the diff touches hot paths"),
    )
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})

    findings = missing_base_skills(tmp_path, [claude.skill_tree])
    assert len(findings) == 1
    assert findings[0].check == "skills.review-layer-unresolved"
    assert findings[0].severity == "warning"
    assert findings[0].detail["skill"] == "bmad-review-performance"
    assert findings[0].detail["layers"] == ["perf"]


def test_unrecognized_handoff_phrasing_warns_instead_of_blocking(tmp_path):
    """The invocation phrasing is a convention, not a contract — upstream itself
    writes "use the `x` skill" elsewhere. An unconfirmable reference is surfaced,
    never blocked on: guessing wrong rebuilds #260."""
    claude = get_profile("claude")
    _install_dev_auto(
        tmp_path,
        claude.skill_tree,
        customize="[workflow]\n"
        + _layer("blind", "bmad-review")
        + _layer("house", "bmad-review-company", phrasing="Use the"),
    )
    _install_skills(tmp_path, claude.skill_tree, {"bmad-review": ()})

    findings = missing_base_skills(tmp_path, [claude.skill_tree])
    assert [(f.check, f.severity) for f in findings] == [
        ("skills.review-layer-unresolved", "warning")
    ]
    assert findings[0].detail["skill"] == "bmad-review-company"


def test_skill_required_by_one_layer_is_never_also_advisory(tmp_path):
    """A hard requirement wins: the same skill named by a gated layer and an
    ungated one is reported once, as a problem."""
    claude = get_profile("claude")
    _install_dev_auto(
        tmp_path,
        claude.skill_tree,
        customize="[workflow]\n"
        + _layer("blind", "bmad-review")
        + _layer("perf", "bmad-review", when="sometimes"),
    )

    findings = missing_base_skills(tmp_path, [claude.skill_tree])
    assert [(f.check, f.severity) for f in findings] == [("skills.review-layer-missing", "problem")]


def test_new_review_check_ids_are_registered():
    """A check id that isn't in the registry asserts at emit time — i.e. ships as a
    crash on exactly the misconfigured project it was meant to report."""
    from bmad_loop.checks import VALIDATE_CHECKS

    assert {
        "skills.review-layer-unresolved",
        "skills.review-layers-empty",
        "skills.customize-unreadable",
    } <= VALIDATE_CHECKS


def test_provision_worktree_copies_derived_review_skill(tmp_path):
    """Validating a custom reviewer and then not provisioning it is how preflight
    passes in the main checkout while the isolated review fails on a skill that was
    never there. The worktree gets what the layers actually name."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    _install_base_skills(repo, claude.skill_tree)
    _install_dev_auto(
        repo,
        claude.skill_tree,
        customize="[workflow]\n" + _layer("house-style", "bmad-review-company"),
    )
    _install_skills(repo, claude.skill_tree, {"bmad-review-company": ()})

    provision_worktree(wt, [claude], repo)

    assert (wt / claude.skill_tree / "bmad-review-company" / "SKILL.md").is_file()
    # the floor is still copied, so a tree whose config we cannot read is unchanged
    for skill in BASE_SKILLS:
        assert (wt / claude.skill_tree / skill / "SKILL.md").is_file()


def test_provision_worktree_seeds_bmad_custom(tmp_path):
    """The run inside the worktree resolves review layers from ITS OWN project
    root. `*.user.toml` is gitignored by the upstream installer (and plenty of
    projects gitignore `_bmad/` whole), so without seeding, validate approves a
    layer set the isolated run never resolves."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    _install_base_skills(repo, claude.skill_tree)
    _write_override(repo, _layer("house-style", "bmad-review-company"), user=True)

    provision_worktree(wt, [claude], repo)

    seeded = wt / "_bmad" / "custom" / "bmad-dev-auto.user.toml"
    assert seeded.is_file()
    assert "bmad-review-company" in seeded.read_text(encoding="utf-8")


def test_provision_worktree_bmad_custom_does_not_clobber_checkout(tmp_path):
    """A checkout that tracks its own customization keeps every tracked file — only
    the children it lacks (the gitignored personal layer) are filled in."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    _install_base_skills(repo, claude.skill_tree)
    _write_override(repo, _layer("team", "bmad-review-repo-side"))
    _write_override(repo, _layer("personal", "bmad-review-mine"), user=True)
    # the worktree checked out the TRACKED team layer, at a different revision
    tracked = wt / "_bmad" / "custom" / "bmad-dev-auto.toml"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("# checked out\n", encoding="utf-8")

    provision_worktree(wt, [claude], repo)

    assert tracked.read_text(encoding="utf-8") == "# checked out\n"
    assert (wt / "_bmad" / "custom" / "bmad-dev-auto.user.toml").is_file()


def test_provision_worktree_bmad_custom_shielded_in_local_exclude(project, tmp_path):
    """Seeded customization must stay out of the unit's `git add -A` — a project
    that doesn't gitignore `_bmad/` would otherwise merge it back on every story."""
    repo = project.project
    _write_override(repo, _layer("house-style", "bmad-review-company"), user=True)
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")

    provision_worktree(wt, [get_profile("claude")], repo)

    assert (wt / "_bmad" / "custom" / "bmad-dev-auto.user.toml").is_file()
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/_bmad/custom" in exclude.splitlines()


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


def test_new_dev_auto_skill_is_additive_for_sprint_mode(tmp_path):
    """Scenario 6 additivity: installing the *new* bmad-dev-auto (folder+id
    dispatch present) satisfies both preflights — sprint mode's file-existence
    check (`missing_base_skills`, which never inspects the dispatch content) and
    stories mode's content probe (`missing_stories_support`). The new skill
    breaks neither pipeline."""
    from bmad_loop.install import (
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        missing_stories_support,
    )

    claude = get_profile("claude")
    tree = claude.skill_tree
    _install_base_skills(tmp_path, tree)
    # upgrade bmad-dev-auto in place to the folder+id dispatch version
    step01 = tmp_path / tree / STORIES_PROBE_SKILL / STORIES_PROBE_FILE
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


def test_provision_worktree_seeds_absent_children_of_existing_dir(tmp_path):
    """The case that motivated #230: a worktree checks out tracked files, so a seed
    DIRECTORY with any tracked child already exists. Its absent children are seeded
    anyway (they clobber nothing), and the entry is no longer reported as a no-op."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "_bmad" / "custom").mkdir(parents=True)  # tracked child
    (repo / "_bmad" / "bmm").mkdir()  # gitignored sibling, absent from the checkout
    (repo / "_bmad" / "bmm" / "config.yaml").write_text("SEED ME", encoding="utf-8")
    (wt / "_bmad" / "custom").mkdir(parents=True)  # what `git worktree add` lays down

    assert provision_worktree(wt, [], repo, seed_files=["_bmad"]) == []
    assert (wt / "_bmad" / "bmm" / "config.yaml").read_text() == "SEED ME"


def test_provision_worktree_seed_dir_does_not_clobber_existing_children(tmp_path):
    """Seeding into an existing dir stays no-clobber at FILE granularity: a child the
    checkout carries keeps its content while its absent siblings are copied in."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "cfg").mkdir(parents=True)
    (repo / "cfg" / "tracked.yaml").write_text("FROM_REPO", encoding="utf-8")
    (repo / "cfg" / "ignored.yaml").write_text("SEED ME", encoding="utf-8")
    (repo / "cfg" / "nested").mkdir()
    (repo / "cfg" / "nested" / "deep.yaml").write_text("SEED ME TOO", encoding="utf-8")
    (wt / "cfg").mkdir(parents=True)
    (wt / "cfg" / "tracked.yaml").write_text("IN_WORKTREE", encoding="utf-8")

    assert provision_worktree(wt, [], repo, seed_files=["cfg"]) == []
    assert (wt / "cfg" / "tracked.yaml").read_text() == "IN_WORKTREE"  # untouched
    assert (wt / "cfg" / "ignored.yaml").read_text() == "SEED ME"
    assert (wt / "cfg" / "nested" / "deep.yaml").read_text() == "SEED ME TOO"


def test_provision_worktree_reports_seed_dir_with_nothing_to_copy(tmp_path):
    """A directory entry whose children ALL already exist copied nothing, so it is
    still a silent no-op and still reported — only a partial seed stops being."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "cfg").mkdir(parents=True)
    (repo / "cfg" / "tracked.yaml").write_text("FROM_REPO", encoding="utf-8")
    (wt / "cfg").mkdir(parents=True)
    (wt / "cfg" / "tracked.yaml").write_text("IN_WORKTREE", encoding="utf-8")

    assert provision_worktree(wt, [], repo, seed_files=["cfg"]) == ["cfg"]
    assert (wt / "cfg" / "tracked.yaml").read_text() == "IN_WORKTREE"


def test_provision_worktree_seed_dir_over_existing_file_is_skipped(tmp_path):
    """A directory entry whose destination is a FILE is a type mismatch: recursing
    would mkdir over the file. The file wins (no-clobber) and the entry is reported
    skipped like any other whose destination already exists."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "cfg").mkdir(parents=True)
    (repo / "cfg" / "ignored.yaml").write_text("SEED ME", encoding="utf-8")
    wt.mkdir()
    (wt / "cfg").write_text("A FILE, NOT A DIR", encoding="utf-8")

    assert provision_worktree(wt, [], repo, seed_files=["cfg"]) == ["cfg"]
    assert (wt / "cfg").read_text() == "A FILE, NOT A DIR"  # untouched


def test_provision_worktree_seed_skips_nested_file_typed_as_dir(tmp_path):
    """The same type mismatch one level down: a child that is a dir in the repo but
    a FILE in the checkout is skipped whole (never mkdir'd over), while its absent
    siblings still seed — so the entry counts as applied."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "cfg" / "sub").mkdir(parents=True)
    (repo / "cfg" / "sub" / "deep.yaml").write_text("SEED ME", encoding="utf-8")
    (repo / "cfg" / "ignored.yaml").write_text("SEED ME TOO", encoding="utf-8")
    (wt / "cfg").mkdir(parents=True)
    (wt / "cfg" / "sub").write_text("A FILE, NOT A DIR", encoding="utf-8")

    assert provision_worktree(wt, [], repo, seed_files=["cfg"]) == []
    assert (wt / "cfg" / "sub").read_text() == "A FILE, NOT A DIR"  # untouched
    assert (wt / "cfg" / "ignored.yaml").read_text() == "SEED ME TOO"


def test_provision_worktree_seeds_absent_empty_child_dir(tmp_path):
    """Creating a missing EMPTY child directory is a write: the entry modified the
    worktree, so it is treated as applied rather than reported as a no-op."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    (repo / "cfg" / "empty").mkdir(parents=True)
    (repo / "cfg" / "tracked.yaml").write_text("FROM_REPO", encoding="utf-8")
    (wt / "cfg").mkdir(parents=True)
    (wt / "cfg" / "tracked.yaml").write_text("IN_WORKTREE", encoding="utf-8")

    assert provision_worktree(wt, [], repo, seed_files=["cfg"]) == []
    assert (wt / "cfg" / "empty").is_dir()
    assert (wt / "cfg" / "tracked.yaml").read_text() == "IN_WORKTREE"  # untouched


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


def test_provision_worktree_partial_seed_dir_shielded_in_local_exclude(project, tmp_path):
    """A directory entry that only PARTIALLY seeds (its destination already existed)
    still gets its exclude pattern written — otherwise the children just seeded would
    be staged by the unit's `git add -A`."""
    repo = project.project
    (repo / "cfg").mkdir(exist_ok=True)
    (repo / "cfg" / "tracked.yaml").write_text("FROM_REPO", encoding="utf-8")
    (repo / "cfg" / "ignored.yaml").write_text("SEED ME", encoding="utf-8")
    wt = tmp_path / "wt"
    verify.worktree_add(repo, wt, "feat", "main")
    # what the checkout lays down: the tracked child, so the seed dir already exists
    (wt / "cfg").mkdir(parents=True)
    (wt / "cfg" / "tracked.yaml").write_text("FROM_REPO", encoding="utf-8")

    provision_worktree(wt, [get_profile("claude")], repo, seed_files=["cfg"])

    assert (wt / "cfg" / "ignored.yaml").read_text() == "SEED ME"
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/cfg" in exclude.splitlines()


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


def test_copy_traversable_skip_existing_holds_on_zip_source(tmp_path):
    """`skip_existing` guards the zip-import branch too, not just the copy2 one:
    an existing destination file survives while its absent sibling is written."""
    zf_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zf_path, "w") as zf:
        zf.writestr("pkg/skill/SKILL.md", "FROM_ZIP")
        zf.writestr("pkg/skill/EXTRA.md", "FROM_ZIP")
    dst = tmp_path / "out"
    (dst / "skill").mkdir(parents=True)
    (dst / "skill" / "SKILL.md").write_text("ON_DISK", encoding="utf-8")

    assert _copy_traversable(zipfile.Path(zf_path, "pkg/"), dst, skip_existing=True) is True

    assert (dst / "skill" / "SKILL.md").read_text() == "ON_DISK"  # untouched
    assert (dst / "skill" / "EXTRA.md").read_text() == "FROM_ZIP"


def test_copy_traversable_skip_existing_reports_total_noop(tmp_path):
    """Nothing left to copy -> False, which is how a seed entry that copied nothing
    is still told apart from one that partially seeded."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f.txt").write_text("FROM_SRC", encoding="utf-8")
    (dst / "d").mkdir(parents=True)
    (dst / "d" / "f.txt").write_text("ON_DISK", encoding="utf-8")

    assert _copy_traversable(src, dst, skip_existing=True) is False
    assert (dst / "d" / "f.txt").read_text() == "ON_DISK"


def test_copy_traversable_skip_existing_never_mkdirs_over_file(tmp_path):
    """A destination FILE standing where the source has a directory is left alone:
    without the guard, mkdir(exist_ok=True) on the file raises FileExistsError."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f.txt").write_text("FROM_SRC", encoding="utf-8")
    dst.mkdir()
    (dst / "d").write_text("A FILE, NOT A DIR", encoding="utf-8")

    assert _copy_traversable(src, dst, skip_existing=True) is False
    assert (dst / "d").read_text() == "A FILE, NOT A DIR"
