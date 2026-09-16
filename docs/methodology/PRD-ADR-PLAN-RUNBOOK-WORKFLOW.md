> **Language of this corpus:** English is now the default language for all content in this
> governance repo and in every consumer repo (see ADR-0002). Documents *generated* in a target
> repo (PRD, ADR, `AGENTS.md`) are always written in English as well — there is no more per-repo
> language choice to check, and the decoupling rule this banner used to describe (superseded by
> ADR-0002) no longer applies.

# Methodology — PRD → ADR → Plan → Runbook → Automated Execution

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
3. **Plan** (`plan` / `writing-plans` skill, `.hermes/plans/*.md`) — breakdown into 2-5 minute
   tasks, exact file paths, complete code, verification commands. Created in plan mode by an
   agent (Hermes or GitHub Copilot).
4. **Runbook** (`docs/runbooks/RUNBOOK-NNNN-<slug>.md`, `templates/RUNBOOK-template.md`,
   `agents/runbook-generator.agent.md`) — sequenced operational detail, written before execution,
   never improvised during it. A Runbook must never introduce a decision absent from its linked
   ADR — if it encounters one, it stops and reports the gap instead of arbitrating it locally (see
   ADR-0004).
5. **Automated execution** — see §Modes and §Default execution tier.

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
development, test, and security work — the default executor is **Hermes running on the local
model** (`unsloth/Qwen3.8-27B-NVFP4`, DGX Spark, vLLM), taking on nearly all roles until an
operational, functional, iteratively-improvable solution is reached. Frontier-model execution
(Claude Sonnet 5, GPT-5.6 Sol) is the **exception**, triggered only by the closed criteria list
documented in `agents/runbook-generator.agent.md` (§Escalation Criteria) — not a default caution
reflex. That agent file is the single source of truth for the escalation criteria; this document
does not duplicate them.

This is orthogonal to `execution_mode` (`hermes-solo` / `hermes-orchestrator-openhands`), which
selects role-isolation strategy, not model tier — a Mode A or Mode B ADR can equally target a
local-model or a frontier-model executor downstream.

## Execution modes

### Mode A — Hermes Solo

A single Hermes agent sequentially takes on every role (architect, dev, tester, security, ops) in
its own context, following the Plan via the `subagent-driven-development` skill (a fresh
sub-agent per task, spec review then quality review). No sandbox isolation per role — only
logical isolation (sequential steps, interleaved reviews).

**Choose Mode A when:**
- The work is single-repo, single-domain, reversible.
- No need for strong isolation between roles (e.g. no strict dev/security separation required).
- Limited volume of work (a few hours, not several agent-days in parallel).
- Cost/simplicity take priority over isolation.

### Mode B — Hermes Orchestrator + OpenHands

Hermes only plays the **Orchestrator** role: it does not code itself, it delegates each role
(architect, dev, tester, security, ops) to a separate **OpenHands app-conversation** — an
isolated sandbox, its own repo and branch, model `openai/dgx-spark-current` (vLLM DGX Spark).
Cross-role collaboration goes through governable artifacts (git/diff, branches, reports) relayed
by the Orchestrator — never through a direct agent-to-agent conversation. This pattern is
**already accepted** by `ADR-0020` (`itshaker-dgx-spark-V2`, 2026-09-12) for
`hermes-spark-builder`; this document generalizes its selection criteria to any project governed
by `vibecoding-copilot-governance`.

**Choose Mode B when:**
- Strict isolation between roles is required (e.g. the security role must be able to block
  without the dev role being able to overwrite its verdict in the same context).
- Real parallelism is desired (several roles/tasks in progress simultaneously, separate
  sandboxes).
- High stakes (public infrastructure, production deployment, sensitive data) where a separate
  per-conversation audit trail has value.
- The delegation mechanism already exists for the target (`oh_pilot.py` / `openhands-pilot`
  skill, available today on `hermes-spark-builder`/DGX Spark).

**Mode B prerequisites (inherited from ADR-0020, to verify before choosing this mode):**
- OpenHands operational and qualified on the target (see the `openhands-spark-ops` skill —
  known limitations: no GitHub token configured for `--repo` conversations at the time of
  writing, task id ≠ statable conversation id).
- The target repo must be reachable via `--repo`/`--branch` from OpenHands, or the mode
  effectively falls back to Mode A for the roles that need it.

## What this document does not decide

- It does not redesign `ADR-0020` or its scope (`hermes-spark-builder` remains the sole
  orchestrator on that instance).
- It only makes Mode B available where OpenHands piloting actually exists — a repo without
  access to a qualified `oh_pilot.py`/OpenHands app must default to Mode A.

## References

- `templates/PRD-template.md`, `templates/ADR-template.md`, `templates/RUNBOOK-template.md`
- `agents/prd-generator.agent.md`, `agents/adr-generator.agent.md`,
  `agents/runbook-generator.agent.md`
- Skills: `plan`, `writing-plans`, `subagent-driven-development`, `openhands-spark-ops`
- ADR source of the Mode B pattern: `itshaker-dgx-spark-V2/docs/adr/ADR-0020-hermes-builder-openhands-orchestration.md`
- ADR-0004 — `docs/adr/ADR-0004-hermes-local-default-execution.md` (default execution tier:
  Hermes-on-local by default, frontier model on explicit escalation criteria only)
