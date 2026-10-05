# AGENTS.md — <!-- TODO: project name -->

Rulebook for every agent (Hermes, Claude Code, Copilot) and every human contributor. Read it in
full before changing anything. An accepted ADR (`docs/adr/`) overrides this file: if you find a
conflict, stop and report it. Keep this file short (< ~1,500 tokens) — details belong in ADRs.

## Project

- **Purpose:** <!-- TODO: one or two sentences -->
- **Type:** m365 · **Stack:** <!-- TODO: languages, frameworks, versions -->
- **Language:** English for all documentation, code comments, commits and PRs (ADR-0002).

## Commands

| Purpose | Command |
|---------|---------|
| Install | <!-- TODO --> |
| Lint    | <!-- TODO --> |
| Test    | <!-- TODO --> |
| Build   | <!-- TODO --> |

Lint and Test must be identical to `validation.commands` in `.ai/orchestration.yaml`: the
orchestrator runs those commands to decide whether a task passes.

## Repository map

| Path | Content |
|------|---------|
| `docs/prd/` | Intent: problem, non-goals, success criteria |
| `docs/adr/` | Decisions, including `execution_mode` — binding |
| `docs/plans/` | Delivery plans: epics, ordered tasks and runbooks (ADR-0006) |
| `docs/runbooks/` | Operational procedures executed by Hermes |
| `docs/operations/` | Hermes continuity state (`CURRENT`, `HANDOFF`, `ACTIVITY`) |
| `.ai/tasks/` | Task contracts (one per unit of code work) |
| `.ai/roles/`, `.ai/orchestration.yaml` | Team configuration — owned by humans |
| `.ai/runs/` | Run state and logs — owned by `scripts/orchestrate.py` |
| `appPackage/` | Teams app, declarative agent and MCP plugin manifests |
| `env/` | Agents Toolkit environment config — never secrets |

## How work flows

1. Intent is written as a PRD, the technical choice as an ADR. No code without a decision.
2. Code work is a task contract `.ai/tasks/TASK-NNNN.md`, run by `scripts/orchestrate.py`:
   DEV → REVIEW → TEST, one role at a time, on branch/worktree `agent/TASK-NNNN`.
3. `READY_FOR_APPROVAL` → a human reviews and merges. Operations follow a Runbook instead.

| Role | Does | Never |
|------|------|-------|
| Hermes | frames work, writes task contracts, runs the orchestrator, arbitrates, reports | writes code, merges |
| DEV | implements, writes tests, runs lint/tests, commits locally | pushes, merges, leaves the scope |
| REVIEW | judges the diff against ACs and ADRs | edits files |
| TEST | judges validation results against ACs | edits files, overrides a failing command |
| Human | decides ADRs, approves, merges | — |

## Conventions

- Conventional commits (`feat(scope): ...`), small and focused; one logical change per commit.
- Tests ship with the code they cover; follow existing patterns before creating new ones.
- Change only what the task's **Scope** lists — no drive-by refactoring or reformatting.

## Boundaries

**Always**
- Read the task contract and its linked ADRs/PRD before editing.
- Run lint and tests and report their real output before declaring work done.
- Leave the worktree clean; record any gap or risk in your result.

**Ask first** — stop, report, let Hermes escalate to a human
- A decision no accepted ADR covers, or an ADR that contradicts the code.
- New dependency, database schema or public API/contract change, CI workflow change.
- Anything touching authentication, secrets, access control, production, public
  infrastructure or recurring cost.

**Never**
- `git push`, `merge`, `rebase`, `reset --hard`, force operations, or commits on `main`.
- Commit or read secrets (`.env*`, keys, tokens, credentials).
- Skip, disable or weaken a test, a lint rule or a threshold to get green.
- Edit `.ai/orchestration.yaml`, `.ai/roles/`, `.ai/runs/` or `.claude/` during a task.

## Definition of done

- Every acceptance criterion is met and covered by a test.
- Lint and tests pass (real output, not assumed); the worktree is clean.
- `CHANGELOG.md` updated if the change is user-visible; docs/ADR updated if behaviour changes.

## Type-specific rules (m365)

- Agent manifests live in `appPackage/` and must pass the manifest check workflow.
- Files in `env/` never contain secrets; secrets come from the secret manager only.
- Entra ID app registrations, scopes and MCP server permissions are **ask-first** changes.

## References

- Governance: <https://github.com/lowcodai/vibecoding-copilot-governance> (standards, policies,
  ADR-0004 escalation criteria, ADR-0005 team workflow)
- Copilot agents, if used: `.github/agents/`
