# CLAUDE.md — <!-- TODO: project name -->

Kept deliberately short: it is loaded into every interactive Claude Code session on a 27B local
model. Headless team runs (scripts/orchestrate.py) use `--bare` and receive their context from
role prompts in `.ai/roles/` instead.

- Project rules and conventions: read `AGENTS.md` (not imported here on purpose — it would cost
  context in every session).
- Decisions: `docs/adr/`. Intent: `docs/prd/`. Work contracts: `.ai/tasks/`.
- One task = one branch `agent/TASK-NNNN` = one worktree. Never push, merge or rebase.
- Commits: conventional commits. Tests ship with the code.
- Never decide architecture that no ADR covers: stop and report the gap.
