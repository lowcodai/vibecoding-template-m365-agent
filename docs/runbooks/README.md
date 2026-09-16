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
