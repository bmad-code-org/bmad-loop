# ADR 0001: Carrying the state root into session and parked-window panes

- **Status:** Accepted (2026-10-02; approver answers in §8)
- **Date:** 2026-10-02
- **Issues:** #730 (`PSMUX_BARE_ENV`), #731 (stale server substitutes its own `BMAD_LOOP_STATE_DIR`); context #729, #537 / PR #728
- **Seam:** `TerminalMultiplexer` (`src/bmad_loop/adapters/multiplexer.py`)

**Decision in brief:**

1. Ship a launcher-side warning for the stale-server case first (Option B).
2. Then carry the state root to parked engine windows in their **argv**, not their env (Option D). This fixes the damaging case on every backend without changing the seam.
3. The env-taking verb pair (Option C) is specified here but not scheduled.

**Implementation status:** none of the stages is implemented as of this ADR. §6 is a plan of separately mergeable changes, and the `--state-root` option it describes does not exist until Stage 2 lands.

Details are in §5 and §6; the approver's answers are in §8.

## 1. Problem

Some panes need a fact (the state root, `BMAD_LOOP_STATE_DIR`) that environment inheritance cannot deliver. Every per-run location derives from that root: the control plane (`runs.state_dir_for`), the events channel (`runs.events_dir_for`) and the psmux registry (`runs.mux_registry_root`). A pane child that computes a different root writes and reads where nothing else looks, so a live run reads as gone.

Inheritance fails in two cases:

- **#730, bare env.** With `PSMUX_BARE_ENV=1` psmux `env_clear`s every pane child and repopulates it from a 14-name allowlist. The allowlist drops `BMAD_LOOP_STATE_DIR` and the `LOCALAPPDATA` the default cascade falls back to.
- **#731, stale server.** A multiplexer server is long-lived. Its panes inherit the environment the server started with, not the environment of the client asking for the pane. A server cold-started under state root S1 and later reused by a bmad-loop under S2 hands S1 to every pane that relies on inheritance.

Who is exposed:

| Pane                                                            | Created by                                                | How it gets the state root                                                                                                                                                                               | Exposed?                    |
| --------------------------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| Coding-CLI window, probe window, attached resolve window        | `new_window(..., env, ...)`                               | Explicit `env` dict, forced through `runs.pin_state_root`. On psmux it travels as an in-source `$env:` prelude inside `-EncodedCommand` (`PsmuxMultiplexer._window_launch`). On tmux it travels as `-e`. | No, on every transport      |
| TUI parked engine window (`run`, `sweep`, `resume`, `resolve`)  | `new_parked_window(session, name, cwd, argv, return_opt)` | Inheritance only. The released verb has no env parameter.                                                                                                                                                | **Yes**                     |
| Window 0 of every session (the control session, agent sessions) | `new_session(name, cwd, cols, lines)`                     | Inheritance only. The released verb has no env parameter.                                                                                                                                                | **Yes**, see the note below |

The parked engine window is where the damage happens: the engine it runs writes its control plane under the wrong root. Window 0 is a plain shell that keeps the session alive. bmad-loop runs nothing in it, so it is exposed only when an operator attaches and types a `bmad-loop` command there.

## 2. Post-mortem: why #537 cut the env transport

PR #728 (#537) built an env transport through the session and parked-window verbs, iterated it across five review rounds, and deleted it before merge. Each attempt failed in a way that any future design has to rule out by construction:

1. **Widening the released signatures breaks out-of-tree backends.** `new_session` and `new_parked_window` are abstract, released, and implemented outside this repo (the reference is the herdr adapter). An override declared with the released parameter list cannot accept an extra `env` argument, so core passing one raises `TypeError` at the call site. Making the parameter optional does not help: core still has to pass it to get the value through.
2. **Signature probing misbinds.** Inspecting the override's signature to decide whether to pass `env` binds by position. One backend had a trailing defaulted parameter of its own, and the env dict was swallowed into it: no error, and the wrong value in the wrong slot. A probe can establish that a parameter exists. It cannot establish that the parameter means "env".
3. **A delegation layer bypasses overrides and invents capabilities.** Routing the released verbs through new concrete env-taking entries on the base class had two failure modes:
   - A subclass that overrides a released verb (for example, a leaf of a bundled backend overriding `new_session`) is skipped when core calls the new entry, because the new entry is inherited from the bundled ancestor and never reaches the override.
   - Deciding capability by method presence (`hasattr`, or "is this name overridden?") turned any same-named helper on an out-of-tree class into an accidental declaration that it supported the new contract.

The PR's compatibility rule followed from this, and this ADR keeps it: **released seam signatures do not change.** New seam methods are non-abstract and have defaults that preserve released behavior.

## 3. Transport facts

Every claim here was read from psmux source at the `v3.3.8` tag (`66cf613`) and at master (`e36bd85`, 2026-10-02), or measured on tmux 3.4 under WSL. Master moved line numbers substantially (`src/server/mod.rs` alone gained about 3,800 lines), but **none of the behavior below changed**. `apply_bare_env_if_set` and the `ShowEnvironment` handler are byte-identical between the two refs (diffed by function).

### 3.1 psmux

| Fact                                                                                                                                                                                                                                                                                                                                           | v3.3.8                                                                                                                             | master                                                          |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Bare-env predicate: `"1"` or case-insensitive `"true"`, read from the **server** process env (`std::env::var`). It then `env_clear()`s and re-adds 14 names: `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `USERPROFILE`, `USERNAME`, `HOMEDRIVE`, `HOMEPATH`, `COMPUTERNAME`, `COMSPEC`, `PATH`, `PATHEXT`, `TEMP`, `TMP`, `PROCESSOR_ARCHITECTURE`. | `src/pane.rs` `apply_bare_env_if_set`, l.889                                                                                       | l.2045                                                          |
| The bare clear runs inside `build_command` / `build_default_shell`, **before** psmux adds its own env.                                                                                                                                                                                                                                         | `src/pane.rs` l.1639, l.1814                                                                                                       | same functions                                                  |
| `new-session -e` pairs are merged into `app.environment` **before** the first pane spawns.                                                                                                                                                                                                                                                     | `src/server/mod.rs` `merge_session_env_into_app` call, l.1023                                                                      | l.2005                                                          |
| `app.environment` is applied to every pane **after** `build_command`, so after the bare clear. It reaches window 0 **and every later window of that server**. A psmux server holds one session.                                                                                                                                                | `src/pane.rs` `create_window_with_env` → `apply_user_environment` (l.982, call l.410); also `spawn_warm_pane`, `create_window_raw` | `create_window_with_env` l.921, `apply_user_environment` l.2138 |
| `new-window -e` is pane-scoped and applied last, overriding the session env (psmux #489).                                                                                                                                                                                                                                                      | `src/pane.rs` `create_window_with_env`                                                                                             | same                                                            |
| A `-e` create never claims a warm server (`env_vars.is_empty()` is a precondition). bmad-loop already passes `-c <cwd>`, and `start_dir.is_none()` is the same kind of precondition, so **adding `-e` costs nothing**: every bmad-loop `new-session` is a cold start today.                                                                    | `src/main.rs` `claimed_warm`, l.1311                                                                                               | l.1804                                                          |
| An in-source `$env:` prelude survives bare mode: it runs inside the pane after the clear. Measured both ways on 3.3.8 for #537 and recorded in `PsmuxMultiplexer._window_launch`.                                                                                                                                                              | measured                                                                                                                           | no change to the spawn path                                     |

**`show-environment` / `set-environment` exist on psmux, but they do not do what the #731 interim needs.**

- The CLI forwards both verbs to the server (`src/main.rs`, l.3768–3815 at the tag; `show-environment` at l.4483 on master).
- The server's `ShowEnvironment` handler (`src/server/mod.rs` l.5149 at the tag, l.6910 on master) prints:
  - `app.environment`: values set via `set-environment`, `new-session -e`, or config
  - inherited process vars **only** if their names start with `PSMUX` or `TMUX`
- It ignores the variable-name argument and `-t`. The control-connection parser drops both (`src/server/connection.rs` l.2853 / l.4409 at the tag; l.3892 / l.5735 on master).
- So an **inherited** `BMAD_LOOP_STATE_DIR` is invisible to `show-environment` on psmux. Only a value bmad-loop itself put there with `-e` or `set-environment` can be read back.
- `set-environment` does write `app.environment`, and the server process env as well, so it is a working runtime transport for later panes (`CtrlReq::SetEnvironment`, l.5134 / l.6895).

This conclusion comes from source only. No live measurement was needed, because the recommendation does not depend on reading an inherited value on psmux (see §5).

### 3.2 tmux (parity oracle: tmux 3.4 under WSL, isolated `-L` socket, `/proc/<pane_pid>/environ`)

The server was cold-started with `BMAD_LOOP_STATE_DIR=/s1`. Every later client ran under `/s2`.

| Probe                                                              | Result                                                |
| ------------------------------------------------------------------ | ----------------------------------------------------- |
| `show-environment -g BMAD_LOOP_STATE_DIR`                          | `/s1`: the global env is the server's start env       |
| `show-environment -t =ctl BMAD_LOOP_STATE_DIR` (no `-e` on create) | `unknown variable`: the session env does not carry it |
| Window 0 of `ctl`                                                  | `/s1`                                                 |
| `new-session` from an `/s2` client, no `-e`, window 0              | `/s1` (this is #731)                                  |
| `new-session -e BMAD_LOOP_STATE_DIR=/s2`, window 0                 | `/s2`                                                 |
| …then `show-environment -t` on that session                        | `/s2`                                                 |
| …then a later `new-window` in it with no `-e`                      | `/s2`: later windows inherit the session env          |
| `new-window -e …=/s2` in the pre-existing `/s1` `ctl` session      | `/s2`                                                 |
| `set-environment -t =ctl … /s3`, then a new window                 | `/s3`. Window 0, already spawned, stays `/s1`.        |

The pane env is the global env overlaid with the session env, so "what will a new pane inherit" on tmux is the session value if set, otherwise the global value. `new-session -e` arrived in tmux 3.2, which is the documented floor (`docs/multiplexer-backends.md`), so no version gate is needed.

**Parity summary:**

- `new-session -e` and `new-window -e` behave the same on both transports.
- `show-environment` diverges: tmux reports inherited values and psmux does not.
- Session-scoped `set-environment -t` exists only on tmux. On psmux it is server-wide, which comes to the same thing because a psmux server holds one session.

## 4. Options

### A. Do nothing, document the remedies

Keep the bare-env warning (`PsmuxMultiplexer._warn_if_bare_env`). In `docs/multiplexer-backends.md`, document `tmux kill-server` (or `set-environment -g BMAD_LOOP_STATE_DIR …`) after changing `BMAD_LOOP_STATE_DIR`.

- **Pros:** zero code, zero risk.
- **Cons:** #731 stays silent. A run reads as gone with nothing naming the cause. #730 stays unsupported.
- **Out-of-tree risk:** none.

### B. #731 interim: the launcher compares and warns

When the TUI reuses an existing control session, it asks the transport what a new pane in that session will inherit, compares that with its own resolved root, and warns once with the remedy.

- **Pros:** turns the silent #731 failure into a named one.
- **Cons:** detects without fixing. Core cannot run `tmux show-environment` itself, because tmux argv is quarantined in `tmux_base.py` / `tmux_backend.py`, so this needs one additive, non-abstract seam query. **Strictly "no seam change" is not possible.** "No change to a released signature" is. On psmux the query can only answer "unknown" (§3.1). That is acceptable, because the per-project registry is keyed on the state root, so a psmux process under S2 never addresses an S1 server through the ordinary path (#731 scope note).
- **Out-of-tree risk:** none. The query is new, non-abstract, and defaults to "unknown". This is the `window_pane_pids` / `version` pattern, and it widens no released signature.

### C. New verb pair as an explicit, versioned, opt-in contract revision (the issue's candidate)

Add `new_session_with_env(name, cwd, *, env, cols=None, lines=None)` and `new_parked_window_with_env(session, name, cwd, argv, return_opt, *, env)`. Both are non-abstract, and their defaults raise `NotImplementedError`. A backend opts in by declaring `seam_version = 2` (new `ClassVar[int]` on `TerminalMultiplexer`, default `1`; module constant `MUX_SEAM_VERSION = 2`).

Core decides capability in exactly one function, `supports_env_transport(mux)`. It returns True iff:

1. `type(mux).seam_version >= 2`, **and**
2. **the owner rule** holds for both pairs. Within `type(mux).__mro__`:
   - the class that defines the new verb is the same class as, or a subclass of, the class that defines the matching released verb; **and**
   - that same class declares `seam_version >= 2` in its own class body (`"seam_version" in cls.__dict__`). An inherited declaration does not count.

How this answers each post-mortem item:

- **Item 1 (widening).** The released verbs are untouched.
- **Item 2 (probing).** Capability is read from a declared attribute. Nothing is inferred from a signature.
- **Item 3a (bypassed overrides).** Say an out-of-tree subclass of a bundled backend overrides `new_session` and inherits `seam_version = 2` plus `new_session_with_env` from its parent. The owner of the new verb is then a strict ancestor of the owner of the override, so capability is False and core calls the override.
- **Item 3b (accidental capabilities).** A method name alone never declares anything:
  - Without `seam_version = 2`, a same-named helper is never called.
  - A helper named `new_session_with_env` on a subclass that inherits its parent's `seam_version = 2` is not called either, because the class defining it did not declare the version itself.
- **No delegation layer.** The bundled backends implement each pair over a private `_spawn_session(…, env: Mapping[str, str] | None)` / `_spawn_parked(…)`. Neither public verb calls the other.
- **Fail loud at the boundary.** `register_multiplexer` takes a factory, not a class, so registration cannot inspect the backend without constructing it, and must not. The check therefore runs where the backend is first built (`multiplexer._select`, which `get_multiplexer` and `detect_multiplexers` reach). An instance whose class declares `seam_version >= 2` but still inherits a `NotImplementedError` default is **malformed**. Registration stays lazy.

The unavailable path is not reused for a malformed backend, because `_select` and `mux_usable` deliberately trust a forced backend even when it probes unavailable. Instead:

- **Forced** selection (`BMAD_LOOP_MUX_BACKEND`, or policy `[mux] backend`) raises a `MultiplexerError` that names the missing verb.
- **Automatic** selection skips a malformed backend, as it does a non-matching one, and the historical fallback excludes it.
- A `detect_multiplexers` row reports it as malformed, with the reason.

Bundled transports:

- **tmux:** `new-session -e` and `new-window -e`, measured in §3.2.
- **psmux:** `new-session -e`, which reaches window 0 and every later window after the bare clear at no warm-start cost (§3.1). Parked windows add a `$env:` prelude in the existing `_source_prefix` source, the transport `_window_launch` already uses.
  - **Precedence caveat (source-read at both refs):** the server merges `-e` before `load_config` runs (`src/server/mod.rs` l.1023 then l.1040 at the tag; l.2005 then l.2023 on master). A config-file `set-environment BMAD_LOOP_STATE_DIR …` (`src/config.rs`, the `set-environment` arm) then overwrites `app.environment`, so it beats `-e` for window 0. tmux does not diverge this way: a session `-e` overrides the global env (§3.2).
  - The parked-window `$env:` prelude runs in the pane, so it wins over both.
  - The guarantee is therefore "window 0 gets the value unless the operator's psmux config sets the same variable". That is documented, not fought.

Assessment:

- **Pros:** fixes window 0 and parked windows for both #730 and #731. Carries arbitrary env, not just the state root.
- **Cons:** the largest change. It introduces the seam's first versioning mechanism (none exists today). Out-of-tree backends get nothing until they opt in. It is also the design space that failed five times, so the owner rule and the backend-construction check need their own ablated tests.
- **Out-of-tree risk:** low by construction. An unversioned backend is never called through the new verbs (see Stage 3's required tests).

### D. Carry the state root in argv, not env (found while writing this ADR)

The parked window does not run an arbitrary command. It runs `tui.launch.cli_argv(...)`, which is `[sys.executable, "-m", "bmad_loop.cli", *tail]`, a bmad-loop process core composes itself. The fact can ride the argv:

- Add a hidden (`argparse.SUPPRESS`) top-level option, `--state-root <abs path>`.
- `cli.main` applies it to `os.environ[BMAD_LOOP_STATE_DIR]` as its first act, ahead of `_configure_mux` and dispatch.
- The detached launcher adds it with its own resolved root.
- When no root derives, the launcher **refuses** the detached launch with a `LaunchError` instead of omitting the flag. Omitting it would let the engine inherit whatever root the server holds, which may be a valid stale one, and the engine would then write where this launcher can never observe. A launcher that cannot name a root cannot watch the run, so refusing is the honest answer.

Every later reader (`envvars.state_dir`, `runs.pin_state_root`, the registry export) and every child the engine spawns then sees the right root through the ordinary path.

- **Pros:** fixes the damaging case (parked engine windows) for #730 **and** #731, on **every** backend including out-of-tree ones. Argv is opaque to the seam and survives both an env clear and a stale server. Zero seam change, so none of the three post-mortem modes can occur.
- **Cons:**
  - It carries only the state root. Since #537 that is the only fact the engine cannot re-derive: the psmux registry is derived from it, and coding-CLI windows are pinned from the engine's own env.
  - It does not reach window-0 shells.
  - It adds a hidden CLI option, an internal contract with the same standing as `--run-id`.
  - The root becomes visible in the process list. It is a directory path, not a secret.
  - It does not make `PSMUX_BARE_ENV` fully "supported": a coding CLI in a bare pane can still miss other variables it needs, which is psmux's documented trade (the user opted out of inheritance).
- **Out-of-tree risk:** none. `new_parked_window`'s argv is already opaque, and every backend must run it verbatim.

### Rejected along the way

- **`set-environment` after create as the transport.** It does not reach an already-spawned window 0 (measured, §3.2). It still needs a new seam verb. On psmux it also triggers a warm-pane respawn.
- **An engine-side handshake** (the launcher records its root in the in-tree run dir, and the engine refuses a mismatch). Option D prevents the mismatch this would only detect.

## 5. Decision

Adopt **D**, preceded by **B** as the interim. Specify **C** now but **do not schedule it**. Its only remaining beneficiary after D is an operator running `bmad-loop` by hand inside a window-0 shell, and that is not worth the seam's first versioning mechanism until someone asks for it. B's warning keeps that residual visible in the meantime. The approver confirmed that C stays unscheduled (§8, answer B).

## 6. Staged plan (separately mergeable)

Nothing below is implemented yet. Each stage describes the change, files and tests of a future pull request.

### Stage 1 (planned): #731 interim warning (no released-signature change)

**Change:**

**The query.** Add a non-abstract `TerminalMultiplexer.inherited_env(session: str, name: str) -> str | Unset | None`, answering **what a new pane in `session` will inherit** for `name`. It has three distinct outcomes:

| Outcome     | Return value                      | Meaning                                         |
| ----------- | --------------------------------- | ----------------------------------------------- |
| Unknown     | `None`                            | The seam default: this transport cannot tell.   |
| Known-unset | `UNSET`, the one `Unset` instance | The variable is confirmed absent for new panes. |
| Set         | the value, `""` included          | What a new pane will see.                       |

`UNSET` is a module-level sentinel in `multiplexer.py`. Known-unset gets its own value rather than `""` because absent and set-to-empty are **different inputs** to the state-root cascade:

- an absent `HOME` takes the passwd fallback
- `HOME=""` expands to `/`, which `runs._state_base` rejects

Folding the two would make the pane-side resolution below wrong in exactly that arm. A Set `""` therefore stays `""`.

The query must not raise. It also must not fold a **failure** into a silent Unknown. Its full signature is `inherited_env(session, name, *, on_fault: Callable[[str], None] | None = None)`, following `list_sessions_reporting`:

- A query this transport supports but could not complete (timeout, missing binary, an unexpected reply) returns `None` **and** hands `on_fault` a one-line description.
- An unsupported query (the seam default) returns `None` and reports nothing.

So "cannot tell" and "tried and failed" stay distinguishable to the caller, per the visible-degradation rule in `AGENTS.md`.

**Implementations:**

- **tmux:** on `TmuxMultiplexer` in `tmux_backend.py`, **not** on `BaseTmuxBackend`. Otherwise `PsmuxMultiplexer` would inherit it and parse a `show-environment` whose output does not follow tmux's single-variable form (§3.1). The query is `show-environment -t =<session> NAME`:
  - a `NAME=value` reply: Set
  - `-NAME` (the removal marker): Known-unset
  - `unknown variable`: fall back to `show-environment -g NAME`, where `unknown variable` means Known-unset
  - any other failure: Unknown, reported through `on_fault`
- **psmux:** keeps the seam default, Unknown. Inherited values are invisible to its `show-environment` (§3.1), and the per-project registry already closes the ordinary path.

**The comparison.** `_ensure_ctl_session` runs it after **both** arms, a freshly created control session as well as a reused one: a new session on a stale tmux server inherits the stale global env too (measured, §3.2 row "`new-session` from an `/s2` client"). It compares **what each side would resolve**, not raw values:

- **The pane's root is resolved from every cascade input, not just the override.** `runs.state_root` falls back to `XDG_STATE_HOME`, then `HOME`, when `BMAD_LOOP_STATE_DIR` is unset (on win32: `LOCALAPPDATA`, then `USERPROFILE`). A stale server can carry a different `XDG_STATE_HOME` while neither side sets the override. Equally, an override can name exactly the root the pane's default would reach.
- So Stage 1 factors the cascade out of `runs.state_root` into a pure `runs.resolve_state_root(env: Mapping[str, str], passwd_home: str | None) -> Path`.
  - The cascade has one input that is **not** in the environment. On POSIX, when `HOME` is absent, `os.path.expanduser("~")` falls back to the current user's passwd entry.
  - `passwd_home` is that value, passed in explicitly. `None` means no passwd entry exists, which is where `expanduser` returns `~` unexpanded.
  - The resolver uses `passwd_home` **only when `HOME` is absent** from `env`. A present `HOME`, empty included, is used as given, exactly as `expanduser` does.
  - `state_root()` becomes `resolve_state_root(os.environ, <the passwd home>)`. The passwd lookup runs lazily, only on the arm that reads it, so behavior is unchanged in every case, the passwd-dependent ones included.
- The launcher queries `inherited_env` for each input the platform's cascade reads, and builds the pane's mapping from the answers: a Set value becomes a key, `""` included, and `UNSET` leaves the key out. It then compares `resolve_state_root(pane_env, passwd_home)` with its own `runs.state_root()`.
  - It passes **its own** passwd home. A tmux server's sockets are per-UID, so a server it can reach runs as the same user.
- Equal resolved roots mean a match. That holds whichever inputs produced them, so a default installation never warns and an equivalent override never warns.
- If the pane-side resolution raises `StateRootError` (for example, an inherited relative value), that is a mismatch: the pane cannot land on the launcher's root.
- If **any** input comes back Unknown, the comparison is Unknown. Unknown never warns about a mismatch, but a query fault reaches the warn sink through `on_fault`.
- If the **launcher's own** root is underivable (`StateRootError`), Stage 1 skips the comparison and reports that, naming the error, through the same sink. The launch is not blocked here: the exception is caught inside the comparison and never escapes to the TUI callers. Refusing the launch is Stage 2's job.

**The warning.** On a mismatch it warns once per process, through the TUI's warn sink rather than stderr. It names both roots and states the remedy:

- For **future panes**: `tmux set-environment -t =<ctl> BMAD_LOOP_STATE_DIR <root>`. The session scope matters, because a session value overrides the global one (§3.2), so a global-only fix can leave a stale session value in force. Add `-g` as well to cover new sessions, or use `tmux kill-server` to restart clean.
- For **already-running shells**, including window 0: no query can see them, and `set-environment` cannot change them. They need an in-shell `export BMAD_LOOP_STATE_DIR=<root>` or recreating.

**Files:**

- `src/bmad_loop/adapters/multiplexer.py`: the default and its docstring
- `src/bmad_loop/adapters/tmux_backend.py`: the implementation; argv stays quarantined there
- `src/bmad_loop/runs.py`: `resolve_state_root`, extracted from `state_root` unchanged
- `src/bmad_loop/tui/launch.py`: the comparison
- `docs/multiplexer-backends.md`: the remedy
- `CHANGELOG.md`: `Fixed`

**Tests at the lowest layer:**

- `tests/test_multiplexer.py`, through a faked `subprocess.run` with `force_tmux_backend`:
  - the tmux parse of Set, `-NAME`, a session miss that falls back to a global hit, and a global miss (both of the last two: `UNSET`)
  - a set-empty reply (`NAME=`) returns `""`, not `UNSET`
  - a failed query: Unknown, with exactly one `on_fault` call
  - `PsmuxMultiplexer().inherited_env(...)` is `None`, spawns nothing, and reports nothing. Ablate the placement by moving the implementation to `BaseTmuxBackend`, and confirm this test fails.
- `tests/test_runs.py`:
  - `resolve_state_root` over explicit mappings matches `state_root()` for the same environment, on each cascade arm.
  - With `BMAD_LOOP_STATE_DIR` and `XDG_STATE_HOME` unusable:
    - absent `HOME` with a passwd home yields that home's root
    - absent `HOME` with no passwd entry (`None`) matches what `state_root()` does today
    - `HOME=""` raises `StateRootError` even when a passwd home is given
    - ablate the absent-only guard by using `passwd_home` whenever `HOME` is falsy, and confirm the `HOME=""` case fails
- `tests/test_tui_launch.py`:
  - warns on a mismatch after reuse **and** after creation
  - warns when only `XDG_STATE_HOME` differs and neither side sets the override
  - stays silent when an override and a default resolve to the same root
  - warns when the pane's inherited value is relative (`StateRootError` on the pane side)
  - stays silent on Unknown, but surfaces a query fault through the sink
  - an underivable launcher root reports through the sink, raises nothing, and still launches
  - ablate the comparison, and confirm the warn tests fail

**Out-of-tree compatibility test (must exist):** `StubMux`, which implements only the released abstract set, goes through `_ensure_ctl_session` on both arms. It completes, `inherited_env` returns `None`, and no warning is emitted.

### Stage 2 (planned): state root in parked-window argv (no seam change)

**Change:**

- Add a hidden top-level `--state-root` option, applied in `cli.main` before `relay` dispatch and `_configure_mux`.
- It must be absolute. Otherwise `main` exits `USAGE` with one message, the same rule as `BMAD_LOOP_STATE_DIR`'s own validation.
- `tui.launch.start_detached` inserts it from `runs.state_root()`, and raises `LaunchError` on `StateRootError` (see Option D). The other `cli_argv` caller is a captured `subprocess.run` that inherits the launcher's own env directly, so it needs no flag.
- Stage 1's stale-root warning narrows to window-0 and other already-running shells, since the parked engine now receives the root explicitly. Per Stage 1, the wording says that a matching query cannot vouch for a shell that is already running.
- The bare-env warning (`PsmuxMultiplexer._warn_if_bare_env`) is **not** narrowed. Stage 2 carries only the state root, so a coding-CLI pane in bare mode can still miss credentials or configuration its env dict does not name (Option D, Cons). Its text drops parked-window shells from the list of shells that lose `BMAD_LOOP_STATE_DIR`, and keeps the warning itself.

**Files:**

- `src/bmad_loop/cli.py`
- `src/bmad_loop/tui/launch.py`
- `src/bmad_loop/adapters/psmux_backend.py`: warning text only
- `docs/multiplexer-backends.md`
- `CHANGELOG.md`: `Fixed` #731, `Changed` #730 scope

`envvars.py` gains nothing, because no new variable is introduced.

**Tests at the lowest layer:**

- `tests/test_cli.py`:
  - `main(["--state-root", X, ...])` sets the variable before `_configure_mux` (assert on a spy)
  - a relative value is refused with `USAGE`
  - ablation: drop the assignment, and the spy test fails
- `tests/test_tui_launch.py`:
  - the parked argv carries the resolved root
  - an underivable root raises `LaunchError` and mints no window. Ablate the refusal, and confirm this test fails.
- `tests/test_stories_e2e.py` (Linux only, real tmux, zero tokens): a server cold-started under S1, then a parked launch under S2, must land the engine's control plane under S2. This is the #731 reproduction as a regression gate.

**Out-of-tree compatibility test (must exist):** `start_detached` against `StubMux`. The argv handed to `StubMux.new_parked_window` contains `--state-root <root>`, and the call uses the released five-parameter signature.

### Stage 3: seam revision (specified, not scheduled; Option C)

**Files:**

- `src/bmad_loop/adapters/multiplexer.py`:
  - `seam_version` and `MUX_SEAM_VERSION`
  - the two `*_with_env` defaults
  - `supports_env_transport` with the owner rule
  - the backend-construction check in `_select` (forced: raise; automatic and fallback: skip; detection: report)
- `tmux_base.py`, `psmux_backend.py`: the private `_spawn_*` implementations and the declaration
- Call sites, each calling the new verb only when `supports_env_transport(mux)` is True:
  - `tui/launch.py` (`_ensure_ctl_session`, `start_detached`)
  - `adapters/generic.py` (`_ensure_session`)
  - `probe.py`
- `docs/adapter-authoring-guide.md`, `docs/multiplexer-backends.md`: how to opt in
- `CHANGELOG.md`: `Added`

**Tests:** `tests/test_multiplexer.py`. Each test below must exist and must be shown to fail with its gate ablated.

1. **Released backend, unversioned.** `StubMux` with released signatures only:
   - `supports_env_transport` is False
   - every call site calls the released verbs with their released arguments
   - no `TypeError` is raised
2. **Override bypass.** A subclass of `TmuxMultiplexer` that overrides only `new_session`:
   - capability is False
   - the override is called
   - ablating the owner rule makes this test fail
3. **Accidental capability and opt-out.** Each case isolates one gate, so each ablation can be shown to fail on its own:
   - **The top-level `seam_version` gate.** A descendant of a bundled backend sets `seam_version = 1` explicitly in its own body, while inheriting both verb pairs from a version-2 owner that is valid under the owner rule. Capability must be False. Ablating the top-level gate makes this test fail, because the owner rule alone still passes.
   - **The own-declaration half of the owner rule.** A subclass of a bundled backend inherits `seam_version = 2` without declaring it, and defines an unrelated helper named `new_session_with_env`. The helper must never be called. Ablating the own-declaration check makes this test fail.
   - An unversioned out-of-tree backend with a method of that name is also never called. This is a characterization case, covered by both gates.
4. **Half-declared.**
   - A factory registered through `register_multiplexer` is not invoked at registration.
   - The instance it returns, which declares `seam_version = 2` but implements only one verb, is handled three ways: forced selection raises a `MultiplexerError` naming the missing verb; automatic selection and the fallback skip it; a `detect_multiplexers` row reports the reason.
   - Ablating the check makes the forced case fail.
5. **Transport argv:**
   - tmux: `new-session ... -e BMAD_LOOP_STATE_DIR=<root>` and the parked `new-window ... -e`
   - psmux: `new-session -e` and the parked `$env:` prelude
6. **psmux live** (`tests/test_psmux_live.py`, zero tokens, real psmux): window 0 of a `new_session_with_env` session sees the value with `PSMUX_BARE_ENV=1` in the server env. This is the one transport claim that is source-read but not yet live-measured for window 0.

## 7. Out of scope (the permanent boundaries from #660)

- **No per-window user options on psmux.** psmux has no per-window option store (bmad-loop #310), so the env transport never rides a window option.
- **`-EncodedCommand` stays the window-command transport on psmux.** A quoted `new-window` command string still dies, and every env prelude above lives inside that encoded source.
- **`_qualified_window_id` stays.** Bare `@N` ids route by the caller's server. Nothing here changes how window ids are minted or replayed.
- **#729 (ambient `PSMUX_DATA_DIR`)** is a different question: which root, rather than how a root travels. This ADR does not answer it.

## 8. Approver decisions (2026-10-02)

The approver's answers, recorded in full:

- **A. The round-3 review findings are patched as proposed.**
  - Known-unset now has its own value, the `UNSET` sentinel, distinct from a set-empty `""` (Stage 1, "The query").
  - `runs.resolve_state_root` takes the passwd home as an explicit argument, used only when `HOME` is absent (Stage 1, "The comparison").
- **B. Stage 3 is not scheduled.** Close #731 after Stage 2. Re-scope #730 to "parked windows supported, window-0 shells warned".
- **C. The hidden `--state-root` option is accepted** as the internal contract Stage 2 will introduce, with the same standing as `--run-id`.
- **D. This ADR is approved.** Status: Accepted.
