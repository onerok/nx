# Task Tracker

> Milestones reference `PLANNING.md` sections. See dependency map in PLANNING.md § Milestone Sequence.

## Progress Overview

**Completed:** 14 / 14 milestones

## Current Sprint

| # | Milestone | PLANNING.md Ref | Key Files | Tests | Status | Date |
|---|-----------|-----------------|-----------|-------|--------|------|
| 0 | Project skeleton & test harness | M0 (§ Milestone 0) | `pyproject.toml`, `src/nx/cli.py`, `tests/conftest.py` | `uv run nx --help` + `uv run pytest` clean | **done** | 2026-02-23 |
| 1 | Config loading & validation | M1 (§ Milestone 1) | `src/nx/config.py` | `test_config.py` (5 tests) | **done** | 2026-02-23 |
| 2 | SSH transport + tmux command builder | M2 (§ Milestone 2) | `src/nx/ssh.py`, `src/nx/tmux.py` | `test_transport.py` (13 tests) | **done** | 2026-02-23 |
| 3 | `nx list` — first end-to-end command | M3 (§ Milestone 3) | `cli.py` list cmd | `test_cli_list.py` (5 tests) | **done** | 2026-02-23 |
| 4 | `nx new` — session creation | M4 (§ Milestone 4) | `cli.py` new cmd | `test_cli_new.py` (7 tests) | **done** | 2026-02-23 |
| 5 | Resolution protocol | M5 (§ Milestone 5) | `src/nx/resolve.py` | `test_resolve.py` (7 tests) | **done** | 2026-02-23 |
| 6 | `nx attach` — anti-nesting | M6 (§ Milestone 6) | `cli.py` attach cmd | `test_cli_attach.py` (6 tests) | **done** | 2026-02-23 |
| 7 | `nx peek` & `nx logs` | M7 (§ Milestone 7) | `cli.py` peek/logs cmds | `test_cli_peek_logs.py` (6 tests) | **done** | 2026-02-23 |
| 8 | `nx send` & `nx kill` | M8 (§ Milestone 8) | `cli.py` send/kill cmds | `test_cli_send_kill.py` (6 tests) | **done** | 2026-02-24 |
| 9 | `nx gc` — garbage collection | M9 (§ Milestone 9) | `cli.py` gc cmd | `test_cli_gc.py` (6 tests) | **done** | 2026-02-24 |
| 10 | Dashboard `nx dash` | M10 (§ Milestone 10) | `src/nx/dashboard.py` | `test_dashboard.py` (7 tests) | **done** | 2026-02-24 |
| 11 | Node management `nx nodes` | M11 (§ Milestone 11) | `src/nx/nodes.py` | `test_nodes.py` (10 tests) | **done** | 2026-02-24 |
| 12 | Snapshot & restore | M12 (§ Milestone 12) | `src/nx/snapshot.py` | `test_snapshot.py` (6 tests) | **done** | 2026-02-24 |
| 13 | Integration tests & polish | M13 (§ Milestone 13) | `tests/integration/` | `test_local_workflow.py` (8 tests) | **done** | 2026-02-24 |

## Dependency Chain

```
M0 → M1 → M2 → M3 → M4
                          ↓
                     M5 → M6 → M7
                          ↓      ↓
                         M10    M8
                                 ↓
                                M9
                                 ↓
                               M11
                                 ↓
                               M12
                                 ↓
                               M13
```

## Definition of Done

See PLANNING.md § Definition of Done for full checklist.

## Discovered During Work

- **pydantic v2 field_validator(mode="before") does not fire on field defaults.** When `FleetConfig()` is constructed with no args (missing config file), `default_cmd` stays as literal `"$SHELL"` instead of expanding. Low impact — only affects direct construction, not `load_config()` with a TOML file. Documented in `test_missing_config_uses_defaults`.
- **Rich console.print injects ANSI escape codes into CLI output.** When using `console.print(f"Error: Session '{name}'...")`, the f-string quotes get styled with color codes (e.g. `\x1b[32m'api'\x1b[0m`). Tests must assert on fragments around the styled text rather than exact string matching. Discovered in `test_new_duplicate_name`.

- [ ] allow user to configure default shell (e.g. zsh) in `FleetConfig` and use that instead of hardcoded `"$SHELL"` in tmux command builder
