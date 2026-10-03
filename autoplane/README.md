# Autoplane

An orchestration layer built **on top of** an unmodified Crewplane (validated
against 0.3.1 and 0.3.5). Crewplane itself is untouched; everything here
drives it from the outside.

## `fallback/` — provider fallback dispatcher

`agent-fallback` is the command Crewplane invokes for each role (thinker,
coder, reviewer). It reads the prompt from stdin, rotates across official,
already-authenticated CLIs (Claude, Codex, Antigravity, OpenCode…) on quota,
auth or transport errors, keeps per-provider cooldowns, and writes only the
chosen answer to stdout. No proxies, no extracted tokens.

- `agent_fallback.py`, `bin/agent-fallback`, `bin/crewplane-view`
- `config/*.json` — provider chains per role (read-only vs write variants)
- `tests/` — `python3 -m unittest discover -s tests` (85 tests)
- `.crewplane/` — sample Crewplane config and workflows

## `boards/bin/` — plan execution on top of Crewplane

Tools that turn a markdown plan (`docs/plans/<plan>/`: module docs,
`SEGUIMIENTO.md` board, `plan.json` dependencies) into Crewplane runs:

| Tool | What it does |
| --- | --- |
| `run-board` | Runs one task (or a module) through Crewplane in an isolated worktree: generate workflow → `crewplane run` → test gate → merge, with salvage of half-finished rounds, leak quarantine of stray edits in the main tree, conflict retries and a repo lock |
| `task-chain` | Launches one `run-board` per task as soon as its dependencies leave `Pendiente`, up to `MAX` live boards |
| `plan-chain`, `correction-launcher` | Module-level and correction-queue launchers |
| `task-workflow` | Renders the per-task Crewplane workflow (executor/reviewer prompts, review contract) |
| `opus-validate`, `opus-revalidate`, `opus-review-lib.py` | Module-closing validation by a stronger model; writes verdicts and correction specs |
| `board-status`, `probe-agents`, `codex-supervisor`, `opencode-go-check` | Status, provider probes and supervision |
| `with-repo-lock` | Runs a command under the same `flock` the boards use |
| `scratch-core`, `deploy-core-local`, `rebuild-graph`, `reset-resolution-store` | Project-specific helpers from the Cortex deployment this was built for (kept as examples; they assume that project's layout) |

Environment: `CORTEX_PLAN=<plan>` selects `docs/plans/<plan>/`. Several
scripts still assume the original repository layout and absolute paths; adapt
`ROOT` and paths before reusing them elsewhere.
