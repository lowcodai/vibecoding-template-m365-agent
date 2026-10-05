> **Language of this corpus:** English is now the default language for all content in this
> governance repo and in every consumer repo (see ADR-0002). Documents *generated* in a target
> repo (PRD, ADR, `AGENTS.md`) are always written in English as well — there is no more per-repo
> language choice to check, and the decoupling rule this banner used to describe (superseded by
> ADR-0002) no longer applies.

# Methodology — PRD → ADR → Plan → Runbook / Task → Execution

## Why this chain

An agent that starts coding without a PRD or an ADR optimizes locally (the next file) with no
guarantee of global consistency (why, what architecture, what limits). This chain forces a
separation between **product intent** (PRD), **technical decision** (ADR), **executable
breakdown** (Plan), and **operational detail** (Runbook), so that an agent running local
inference (Qwen3.8-27B-NVFP4 on DGX Spark) can execute without having to arbitrate ambiguous
questions along the way — the arbitration has already happened, upstream, in the ADR.

## The chain

1. **PRD** (`docs/prd/PRD-NNNN-<slug>.md`, `templates/PRD-template.md`) — what/why, success
   criteria, non-goals. Zero implementation detail.
2. **ADR** (`docs/adr/ADR-NNNN-<slug>.md`, `templates/ADR-template.md`) — which technical
   decision, rejected alternatives, consequences, **and** the `execution_mode` field that locks
   in the chosen execution mode (see below).
3. **Plan** (`docs/plans/PLAN-NNNN-<slug>.md`, `templates/PLAN-template.md`, ADR-0006) — ordered
   epics and the units of work that deliver them (task contracts and Runbooks), with
   dependencies and status. Mandatory beyond 3 tasks, more than one epic, dependencies between
   tasks, or work spanning several orchestrator sessions; below that, tasks reference the ADR directly.
   Reviewed in a pull request before its first task runs. (`.hermes/plans/` holds pre-ADR-0006
   history only.)
4. **Runbook** (`docs/runbooks/RUNBOOK-NNNN-<slug>.md`, `templates/RUNBOOK-template.md`,
   `agents/runbook-generator.agent.md`) — sequenced operational detail, written before execution,
   never improvised during it. A Runbook must never introduce a decision absent from its linked
   ADR — if it encounters one, it stops and reports the gap instead of arbitrating it locally (see
   ADR-0004).
5. **Task contracts** (`.ai/tasks/TASK-NNNN.md`) for code, executed by the sequential Claude Code
   team — see §Execution modes and ADR-0005.

## Model tier recommendation (non-blocking)

- **PRD and ADR: a frontier model is strongly recommended** (Claude Sonnet 5, GPT-5.6 Sol, or
  equivalent) for irreversible, high-stakes decisions (public infrastructure, data, security,
  significant recurring cost).
- **A local model is allowed** (Qwen3.8-27B-NVFP4 / DGX Spark or equivalent) for cost reasons, in
  particular for reversible, low-stakes decisions, or fast iteration. A PRD/ADR with
  `authored_by: local-model` is a **valid, executable document** — it is never a tool or agent
  blocker, only an audit label. A frontier-model review is recommended before moving to
  "Accepted" status if the decision is high-stakes, but this remains a recommendation, not a
  blocking gate.
- The Plan and the Runbook do not carry this recommendation: they are execution artifacts, not
  decision artifacts.

## Default execution tier (Plan/Runbook/dev/test/security)

Per ADR-0004 (`docs/adr/ADR-0004-hermes-local-default-execution.md`), the model tier recommendation
above covers only PRD and ADR authoring. Downstream of an accepted ADR — Plan, Runbook,
development, test, and security work — the default executor runs on the **local model**
(`Qwen-3.8-27B-NVFP4`, DGX Spark, vLLM), taking on nearly all roles until an
operational, functional, iteratively-improvable solution is reached. Frontier-model execution
(Claude Sonnet 5, GPT-5.6 Sol) is the **exception**, triggered only by the closed criteria list
documented in `agents/runbook-generator.agent.md` (§Escalation Criteria) — not a default caution
reflex. That agent file is the single source of truth for the escalation criteria; this document
does not duplicate them.

The executor for **code** on the local model is the sequential Claude Code team (ADR-0005), not
the orchestrator agent itself; the orchestrator executes Runbooks (operations) and documentation work
directly. The orchestrator is Hermes today; its specific rules live in `adapters/hermes/`, never in
projects (ADR-0007).

## Execution modes

### `orchestrated-team` — default for any change to code (ADR-0005)

The orchestrator agent (Hermes today) is the Engineering Manager: it frames the work, writes one task contract per unit of work
(`.ai/tasks/TASK-NNNN.md`), runs `scripts/orchestrate.py`, arbitrates the outcome and prepares the
human validation. It does not code and never merges. The orchestrator runs one Claude Code role
at a time on one worktree per task:

```
PLANNED → DEV → REVIEW ─CHANGES_REQUESTED→ DEV
                  │
                  └→ TEST ─FAIL→ DEV
                       │
                       └→ READY_FOR_APPROVAL → human validation → merge (human)
```

| Role | Gateway | Context / output / temp. | Receives |
|---|---|---|---|
| Orchestrator (Hermes) | `hermes-orchestrator` | 98k / 2k / 0.2 | AGENTS.md, ADR, PRD, orchestration.yaml, TASK |
| DEV | `cc-dev` | 98k / 4k / 0.2 | TASK + feedback; reads AGENTS.md/ADR/PRD itself |
| REVIEW | `cc-review` | 32k / 2k / 0.1 | acceptance criteria, ADR Decision/Implementation, git diff |
| TEST | `cc-test` | 32k / 2k / 0.1 | acceptance criteria, validation exit codes + log tails |

Kit and details: `dev-factory/README.md`. Hermes procedure: `adapters/hermes/skills/sequential-coding-team/`.

### `single-agent` — documentation, governance, operations

The orchestrator agent alone executes the work directly: PRD/ADR drafting, governance edits, and
Runbook-driven operations (`docs/runbooks/`). No application code in this mode.

### Former names

`hermes-sequential-team` and `hermes-solo` (ADR-0005) are the former names of `orchestrated-team` and
`single-agent` (ADR-0007); older ADRs keep them. `hermes-orchestrator-openhands` is deprecated:
superseded by ADR-0005 (never qualified outside `hermes-spark-builder`; parallel sandboxes do not
fit a single DGX Spark). Existing ADRs that name it keep their text; new ADRs must not use it.

## Where Plan, Runbook and Task fit

```
PRD (what, why) ─┐
                 ├─► PLAN (epics, order, dependencies) ─► TASK-NNNN (code)  ─► run ─► PR
ADR (how) ───────┘                                    └─► RUNBOOK (ops)   ─► Hermes
```

- **Plan** (`docs/plans/`): the orchestrator's breakdown of an accepted ADR and its PRD into epics (user
  value) and ordered units of work. Optional for ≤ 3 independent tasks (ADR-0006).
- **Task** (`.ai/tasks/TASK-NNNN.md`): one code unit — one DEV run, < ~400 changed lines,
  verifiable acceptance criteria — executed by the Claude Code team. Front matter `plan:` and
  `epic:` link it back to its plan.
- **Runbook** (`docs/runbooks/`): one operational unit, executed by the orchestrator agent.
- **Status:** detailed state in `.ai/runs/<task>/state.json`; the orchestrator copies terminal states into
  the plan's task table; `BACKLOG.md` carries epic status only and links each epic to its plan.

## References

- `templates/PRD-template.md`, `templates/ADR-template.md`, `templates/PLAN-template.md`,
  `templates/RUNBOOK-template.md`
- `agents/prd-generator.agent.md`, `agents/adr-generator.agent.md`,
  `agents/runbook-generator.agent.md`
- `dev-factory/` — orchestration kit (ADR-0005)
- ADR-0004 — `docs/adr/ADR-0004-hermes-local-default-execution.md` (local model by default)
- ADR-0005 — `docs/adr/ADR-0005-sequential-claude-code-team-orchestrated-by-hermes.md`
- ADR-0006 — `docs/adr/ADR-0006-plans-in-docs-plans.md` (plans in `docs/plans/`, when mandatory)
