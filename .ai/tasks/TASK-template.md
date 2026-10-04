---
id: TASK-XXXX
title: "{{TITLE}}"
status: PLANNED
prd: ""                 # docs/prd/PRD-NNNN-<slug>.md, optional
adrs: []                # [docs/adr/ADR-NNNN-<slug>.md] — REVIEW receives their Decision + Implementation
base_branch: main
validations: []         # task-specific commands, e.g. [{name: api-tests, run: "pytest tests/api -q"}]
---

## Objective

<!-- One or two sentences. What must be true when this task is done. -->

## Scope

- <!-- files / modules DEV may change -->

## Out of scope

- <!-- explicit exclusions — REVIEW flags any change here -->

## Acceptance criteria

- AC-1: <!-- verifiable statement -->
- AC-2:

## Git rules

- Work only on branch `agent/TASK-XXXX`; small conventional commits; never push, merge or rebase.

## Notes for DEV

<!-- Optional: pointers, known pitfalls. Keep the whole file under ~1,200 tokens:
     Hermes writes it with a 2k output cap, REVIEW/TEST read parts of it inside 32k. -->
