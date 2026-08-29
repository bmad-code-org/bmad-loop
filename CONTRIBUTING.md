# Contributing to bmad-loop

Thank you for considering contributing! bmad-loop is part of the [BMad](https://github.com/bmad-code-org/BMAD-METHOD) ecosystem, and we believe in **Human Amplification, Not Replacement** — bringing out the best thinking in both humans and AI through guided collaboration.

💬 **Discord**: [Join our community](https://discord.gg/gk8jAdXWmj) for real-time discussions, questions, and collaboration.

---

> **Before you write code: talk to us on [Discord](https://discord.gg/gk8jAdXWmj).**
>
> If your change adds features, restructures code, or touches more than a couple of files, **confirm with a maintainer that it fits**. A large PR out of the blue has a high chance of being closed — regardless of effort invested. A five-minute conversation can save you hours.

---

## Our Philosophy

bmad-loop is a deterministic orchestrator: plain Python drives the loop while LLMs do only the creative work inside disposable coding-CLI sessions. Every contribution should keep that line clean — **no LLM in the control loop** — and answer: **"Does this make humans and AI better together?"**

**✅ What we welcome:**

- Bug fixes and reliability improvements to the control loop, hooks, and verification gates
- New CLI adapter profiles (codex, gemini, cursor, …) and plugin examples
- Better docs, setup walkthroughs, and troubleshooting guides
- Tests that pin down behavior

**❌ What doesn't fit:**

- Moving orchestration decisions into an LLM (it must stay deterministic Python)
- Complexity that creates barriers to adoption
- Bulk refactors nobody asked for

---

## Reporting Issues

**ALL bug reports and feature requests MUST go through GitHub Issues.**

### Before Creating an Issue

1. **Search existing issues** — Use the GitHub issue search to check if your bug or feature has already been reported
2. **Search closed issues** — Your issue may have been fixed or addressed previously
3. **Check discussions** — Some conversations happen in [GitHub Discussions](https://github.com/bmad-code-org/bmad-loop/discussions)

### Bug Reports

After searching, if the bug is unreported, use the [bug report template](https://github.com/bmad-code-org/bmad-loop/issues/new?template=bug-report.yaml) and include:

- Clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Your environment (coding CLI, OS, bmad-loop version from `bmad-loop --version`)
- Screenshots or error messages if applicable

### Feature Requests

After searching, use the [feature request template](https://github.com/bmad-code-org/bmad-loop/issues/new?template=feature-request.md) and explain:

- What the feature is
- Why it would benefit the bmad-loop community
- How it strengthens human-AI collaboration

**For naming community modules or plugins**, review [TRADEMARK.md](TRADEMARK.md) for proper naming conventions (e.g., "My Plugin (BMad Community Plugin)").

---

## Before Starting Work

| Work Type               | Requirement                                               |
| ----------------------- | --------------------------------------------------------- |
| Typo / small bug fix    | Just open the PR                                          |
| Feature or large change | Confirm with a maintainer on Discord **before** you start |

---

## Development Setup

bmad-loop is a Python project managed with [uv](https://docs.astral.sh/uv/). **Python 3.11 is the floor.**

```bash
git clone https://github.com/YOUR-USERNAME/bmad-loop.git
cd bmad-loop
uv sync --all-extras          # deps + all three extras (tui, non-linux, opencode) + dev tools
uv run pytest -q              # unit + adapter scenarios + tmux integration (-n auto to parallelize)
uv run pyright                # typecheck — CI runs this same pinned version as its own job
```

Never `pip install` — uv owns the environment. If you change dependencies, edit `pyproject.toml` and run `uv lock`; CI uses `uv sync --locked` and fails on a stale lock. The pyright version is pinned exactly in the `dev` group, so bump it deliberately — never with `uv lock --upgrade`.

> **On Windows**, set `PYTHONUTF8=1` before running the suite — `tests/conftest.py` raises a `UsageError` without it.

Linting and formatting run through [trunk](https://trunk.io) (ruff, black, isort, prettier, markdownlint, and more). **Run `trunk check` before pushing** — a pre-push hook enforces it, so formatting/lint failures surface locally instead of in CI:

```bash
trunk fmt        # auto-format changed files
trunk check      # lint + format verification on changed files, as CI does
trunk check --all # the whole repo — catches files your change didn't touch
```

The checkout uses CRLF while Prettier is configured for LF. A direct local
`prettier --check` can therefore disagree with CI; CI checks LF-normalized
content, so use the trunk commands above for the authoritative result.

### CHANGELOG

**Every user-visible change needs a CHANGELOG entry.** Add it under the `## [Unreleased]` heading in [CHANGELOG.md](CHANGELOG.md), and only under one of the six [Keep a Changelog](https://keepachangelog.com) subsections — `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`. Keep entries terse, scannable, and imperative.

Never open a new `## [X.Y.Z]` section yourself: a release _promotes_ `## [Unreleased]` into the version heading and reopens an empty one above it. CI's `version-sync` job (`scripts/release.py check`) holds the reopened heading and its `compare/v<version>...HEAD` link — it does not reject a hand-authored version section, so this one is on you. The full rule is in [AGENTS.md](AGENTS.md#repo-hygiene).

### Releases

Releases are cut by maintainers with `scripts/release.py`, which is two-phase:

- **`prepare X.Y.Z`** runs on a release branch. **Promote the CHANGELOG by hand first** — rename `## [Unreleased]` to `## [X.Y.Z] — <ISO date>` and reopen an empty `## [Unreleased]` above it; `prepare` does not do this for you and refuses to run until it is done. It then stamps the version everywhere via `sync_version.py`, regenerates TUI assets when they changed, and commits, leaving the branch ready for a PR.
- **`publish`** runs on `main` after that PR merges (driven by `.github/workflows/release.yml`) — it creates the tag and GitHub release from the CHANGELOG, and is idempotent.

Version strings are stamped only by `scripts/sync_version.py`; never hand-edit them in `pyproject.toml`, `module.yaml`, `marketplace.json`, or `uv.lock`. The version is validated in CI — if you touch it, run `uv run --no-project python scripts/sync_version.py --check`.

---

## Pull Request Guidelines

### Target Branch

Submit PRs to the `main` branch. We use trunk-based development. Releases are cut from `main`.

### PR Size

- **Ideal**: 200-400 lines of code changes
- **Maximum**: 800 lines (excluding generated files)
- **One feature/fix per PR**

If your change exceeds 800 lines, break it into smaller PRs that can be reviewed independently.

### AI-Generated Code

Given the nature of this project, we expect most contributions involve AI assistance — that's fine. What we require is **heavy human curation**. You must understand every line you're submitting, have made deliberate choices about what to include, and be able to explain your reasoning.

We will reject PRs that read like raw LLM output: bulk refactors nobody asked for, unsolicited "improvements" across many files, or changes where the submitter clearly hasn't read the existing code. Using AI to write code is normal here; using AI as a substitute for thinking is not.

### New to Pull Requests?

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR-USERNAME/bmad-loop.git`
3. **Create a branch**: `git checkout -b fix/description` or `git checkout -b feature/description`
4. **Make changes** — keep them focused
5. **Changelog**: add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for any user-visible change
6. **Verify**: `trunk check`, `uv run pytest -q`, and `uv run pyright` all pass — CI runs all three, plus packaging, version/changelog sync, Windows, and Python 3.11–3.14 ([docs/testing.md](docs/testing.md#ci-flakes-and-deliberate-absences))
7. **Commit**: `git commit -m "fix: correct typo in README"`
8. **Push**: `git push origin fix/description`
9. **Open PR** from your fork on GitHub

### PR Description Template

```markdown
## What

[1-2 sentences describing WHAT changed]

## Why

[1-2 sentences explaining WHY this change is needed]
Fixes #[issue number]

## How

- [2-3 bullets listing HOW you implemented it]

## Testing

[1-2 sentences on how you tested this]

## Changelog

[Entry added under `## [Unreleased]` in CHANGELOG.md, under one of: Added, Changed, Deprecated, Removed, Fixed, Security. Write "n/a" if nothing user-visible changed.]
```

**Keep it under 200 words.**

### Commit Messages

Use conventional commits:

- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation only
- `refactor:` Code change (no bug/feature)
- `test:` Adding tests
- `chore:` Build/tools changes

Keep messages under 72 characters. Each commit = one logical change.

---

## What Makes a Good PR?

| ✅ Do                       | ❌ Don't                     |
| --------------------------- | ---------------------------- |
| Change one thing per PR     | Mix unrelated changes        |
| Clear title and description | Vague or missing explanation |
| Reference related issues    | Reformat entire files        |
| Small, focused commits      | Copy your whole project      |
| Work on a branch            | Work directly on `main`      |

---

## Code & Project Guidelines

- **Keep the control loop deterministic** — orchestration logic is plain Python, never an LLM call. LLMs only run inside disposable coding-CLI sessions.
- **Python style** is enforced by trunk (ruff, black, isort) at line-length 100 — let `trunk fmt` handle formatting.
- **Tests** live under `tests/`; add or update them for behavior changes. The mock adapter lets most of the loop run without a live CLI. Where a test belongs and the doctrines it must follow: [docs/testing.md](docs/testing.md).
- **Skills** ship as markdown under `src/bmad_loop/data/skills/` (the `bmad-loop-*` automation skills).
- **Plugins** extend the orchestrator via a `plugin.toml` manifest — see the [plugin authoring guide](docs/plugin-authoring-guide.md).
- **New coding CLIs** are usually a TOML profile, not Python — see the CLI adapter section in the [README](README.md) and the [adapter authoring guide](docs/adapter-authoring-guide.md) (use `bmad-loop probe-adapter` to collect the hook/transcript/token data a profile needs).

---

## Need Help?

- 💬 **Discord**: [Join the community](https://discord.gg/gk8jAdXWmj)
- 🐛 **Bugs**: Use the [bug report template](https://github.com/bmad-code-org/bmad-loop/issues/new?template=bug-report.yaml)
- 💡 **Features**: Use the [feature request template](https://github.com/bmad-code-org/bmad-loop/issues/new?template=feature-request.md)

---

## Code of Conduct

By participating, you agree to abide by our [Code of Conduct](.github/CODE_OF_CONDUCT.md).

## License

By contributing, your contributions are licensed under the same MIT License. See [CONTRIBUTORS.md](CONTRIBUTORS.md) for contributor attribution.
