# Runbooks — operational execution detail for this repo

Numbering convention: `RUNBOOK-NNNN-<slug>.md`, a sequence specific to this repo, starting at
0001.

A Runbook is the sequenced, operational detail written before execution and never improvised
during it — see `templates/RUNBOOK-template.md` and the `runbook-generator` agent. Every Runbook
carries a mandatory `linked_adr` (never empty): a Runbook must never introduce a decision absent
from its linked ADR — if it encounters one, it stops and reports the gap instead of arbitrating it
locally (see ADR-0004).

Per ADR-0004, Runbooks are designed by default for execution by Hermes running on the local model
(`hermes-solo`); escalation to a frontier model follows the closed criteria list documented in
`agents/runbook-generator.agent.md`.

Full methodology: see `docs/methodology/PRD-ADR-PLAN-RUNBOOK-WORKFLOW.md` in
`vibecoding-copilot-governance` (or its local copy if synced into this repo).

## M365-specific Runbook templates

Two pre-filled, battle-tested Runbook templates for the publication workflow every project built
from this template eventually needs, live at the repo root under `templates/` (alongside the
generic `RUNBOOK-template.md` this convention comes from):

- `templates/RUNBOOK-m365-oauth-packaging-sideload-template.md` — OAuth registration for the
  agent's MCP plugin, packaging, and first sideload via the `atk` CLI. Run this one first.
- `templates/RUNBOOK-m365-tenant-publish-template.md` — tenant-wide catalog publish, once the
  sideload has been validated end-to-end. Run this one second, and only after an explicit
  approval decision to go tenant-wide.

Both were extracted from the first real execution of this pattern
([lowcodai/copilot-github-manager](https://github.com/lowcodai/copilot-github-manager),
2026-09-27) and encode the concrete failures that execution hit (manifest schema pitfalls, an
`m365agentstoolkit-cli` command sequence that actually works, and the Microsoft Graph app-only
permission limitation for tenant publish) so the next project doesn't rediscover them. Copy each
into `docs/runbooks/RUNBOOK-NNNN-<slug>.md`, fill in the placeholders, and add a real
`linked_adr` before executing — do not run them as-is from `templates/`.
