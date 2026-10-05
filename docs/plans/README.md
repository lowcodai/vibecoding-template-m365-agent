# Plans — delivery breakdown for this repo

Numbering convention: `PLAN-NNNN-<slug>.md`, a sequence specific to this repo, starting at 0001.

A plan turns an accepted ADR (and its PRD) into ordered epics and units of work: task contracts
(`.ai/tasks/TASK-NNNN.md`, run by `scripts/orchestrate.py`) and Runbooks (`docs/runbooks/`,
executed by the orchestrator) — see `templates/PLAN-template.md` and ADR-0006.

- **When:** mandatory beyond 3 tasks, more than one epic, dependencies between tasks, or work
  spanning several orchestrator sessions. Below that, tasks reference the ADR directly.
- **Traceability:** each task carries `plan:` and `epic:` in its front matter; the plan lists every
  task it spawned; `BACKLOG.md` links each epic to its plan.
- **Status:** copied from `.ai/runs/<task>/state.json` into the plan's task table at each terminal
  state — the run state stays the source of truth.
- **Review:** a plan is reviewed in a pull request before its first task runs.

Full methodology: see `docs/methodology/PRD-ADR-PLAN-RUNBOOK-WORKFLOW.md` in
`vibecoding-copilot-governance` (or its local copy if synced into this repo).
