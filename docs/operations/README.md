# Operations — continuity state for long-running agents

These files keep work recoverable when an agent session ends, rotates or loses its context. The
rules are in `AGENTS.md` § Continuity; they apply to any agent (and to humans picking up work).

| File | Written | Content |
|------|---------|---------|
| `CURRENT.md` | at every checkpoint | the active work: objective, done, in progress, next executable action, risks, modified files |
| `HANDOFF.md` | when a session stops mid-work (context pressure ≥ 82%, planned rotation) | exact state at exit and the very first command for the next session |
| `ACTIVITY.md` | append-only, one entry per checkpoint | dated log of checkpoints — never rewritten |

Task-level execution state lives elsewhere: `.ai/runs/<task>/` (orchestrator) and the plan's task
table in `docs/plans/` (ADR-0006). These files track the agent's own session, not each task run.
