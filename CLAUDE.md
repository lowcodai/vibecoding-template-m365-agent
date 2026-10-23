# CLAUDE.md — <!-- TODO: project name -->

Kept deliberately short: it is loaded into every interactive Claude Code session on a 27B local
model, and by the orchestrated DEV run. REVIEW/TEST runs use `--bare` and get their context from
`.ai/roles/` instead.

- Project rules and conventions: read `AGENTS.md` (not imported here on purpose — it would cost
  context in every session).
- Decisions: `docs/adr/`. Intent: `docs/prd/`. Work contracts: `.ai/tasks/`.
- One task = one branch `agent/TASK-NNNN` = one worktree.
<!-- autonomy:begin -->
- Autonomy `supervised` (`.ai/autonomy.yaml`): commit locally only; never push, merge or rebase; no curl, wget or gh.
<!-- autonomy:end -->
- Commits: conventional commits. Tests ship with the code.
- Never decide architecture that no ADR covers: stop and report the gap.
