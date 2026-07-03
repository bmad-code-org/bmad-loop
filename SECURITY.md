# Security Policy

## Supported Versions

We release security patches for the following versions:

| Version  | Supported          |
| -------- | ------------------ |
| Latest   | :white_check_mark: |
| < Latest | :x:                |

We recommend always using the latest version of bmad-loop to ensure you have the most recent security updates.

## Reporting a Vulnerability

We take security vulnerabilities seriously. If you discover a security issue, please report it responsibly.

### How to Report

**Do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them via one of these methods:

1. **GitHub Security Advisories** (Preferred): Use [GitHub's private vulnerability reporting](https://github.com/bmad-code-org/bmad-loop/security/advisories/new) to submit a confidential report.

2. **Discord**: Contact a maintainer directly via DM on our [Discord server](https://discord.gg/gk8jAdXWmj).

### What to Include

Please include as much of the following information as possible:

- Type of vulnerability (e.g., command injection, path traversal, prompt injection, etc.)
- Full paths of source file(s) related to the vulnerability
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if available)
- Impact assessment of the vulnerability

### Response Timeline

- **Initial Response**: Within 48 hours of receiving your report
- **Status Update**: Within 7 days with our assessment
- **Resolution Target**: Critical issues within 30 days; other issues within 90 days

### What to Expect

1. We will acknowledge receipt of your report
2. We will investigate and validate the vulnerability
3. We will work on a fix and coordinate disclosure timing with you
4. We will credit you in the security advisory (unless you prefer to remain anonymous)

## Security Scope

bmad-loop orchestrates coding-CLI sessions: it spawns subprocesses in tmux, reads structured event files written by CLI hooks, runs your configured test/lint commands, and (optionally) manages git worktrees and branches. The security boundary is the orchestrator code and the trust it places in configuration, plugins, and on-disk signals.

### In Scope

- Vulnerabilities in the bmad-loop orchestrator / control-loop code
- Issues in hook / signal-file handling that let a session forge state, escape verification gates, or trigger unintended commits
- Command or path injection via configuration, profiles, or spawned coding-CLI / tmux sessions
- Flaws in the **plugin trust model** (see [docs/plugin-authoring-guide.md](docs/plugin-authoring-guide.md)) that let a plugin gain unintended capabilities
- Unsafe git worktree / branch handling that affects checkouts outside the run
- Supply chain vulnerabilities in bmad-loop's own dependencies

### Out of Scope

- Security issues in user-authored skills, plugins, profiles, or test/lint commands
- Vulnerabilities in the third-party coding CLIs themselves (claude, codex, gemini, cursor, …) or in the AI providers behind them
- Issues that require physical access to a user's machine
- Social engineering attacks
- Denial of service attacks that don't exploit a specific vulnerability

## Security Best Practices for Users

When using bmad-loop:

1. **Review Agent Outputs**: Always review AI-generated code and diffs before merging or executing them
2. **Trust Your Plugins and Profiles**: Only install plugins and CLI profiles from sources you trust — they run with your orchestrator's privileges
3. **Keep Updated**: Regularly update to the latest version
4. **Validate Dependencies**: Review any dependencies added by generated code
5. **Environment Isolation**: Consider running AI-assisted development in isolated environments, and use worktree isolation (`[scm] isolation = "worktree"`) to keep your main checkout clean

## Acknowledgments

We appreciate the security research community's efforts in helping keep bmad-loop secure. Contributors who report valid security issues will be acknowledged in our security advisories.

---

Thank you for helping keep bmad-loop and our community safe.
