# vibecoding-template-m365-agent

> Template for Microsoft 365 Copilot declarative agents backed by an MCP server.

[![Governance](https://img.shields.io/badge/governance-lowcodai-blue)](https://github.com/lowcodai/vibecoding-copilot-governance)
[![Release](https://img.shields.io/github/v/release/lowcodai/vibecoding-template-m365-agent)](https://github.com/lowcodai/vibecoding-template-m365-agent/releases/tag/v0.0.1)

## Description

GitHub template for lowcodai Microsoft 365 Copilot declarative agent projects — a Copilot
extensibility pattern where the agent's data operations are delegated to an external MCP
(Model Context Protocol) server rather than a bespoke API client. Generalizes the pattern first
built for [copilot-github-manager](https://github.com/lowcodai/copilot-github-manager) (a
GitHub-MCP-backed agent) to any future MCP-backed M365 Copilot project. Includes everything from
`vibecoding-template-base` plus:
- `appPackage/` and `env/` scaffolding for the Microsoft 365 Agents Toolkit
- Declarative-agent and MCP-plugin governance agents/instructions
- The same AI governance instructions and hooks as `vibecoding-template-ai` (an M365 declarative
  agent is still an AI agent for governance purposes)
- A manifest-JSON-validity CI check (`m365-agent-manifest-check.yml`)

## Agent rulebook

[`AGENTS.md`](AGENTS.md) is the first file every agent (Hermes, Claude Code, Copilot) and every
contributor reads: commands, repository map, the DEV → REVIEW → TEST workflow (ADR-0005),
boundaries and definition of done. Fill in its `TODO` markers when you create a project from
this template. It is rendered by `vibecoding-bootstrap/scripts/apply-template.sh`, the single
source for all templates — change the generator, then re-render, rather than editing one copy.
The orchestration files it refers to (`.ai/`, `.claude/`, `scripts/orchestrate.py`) are
installed by `vibecoding-bootstrap/scripts/sync-governance.sh`.

## Usage

```bash
cd vibecoding-bootstrap
./scripts/new-project.sh --type m365 --name <my-agent-project>
```

## M365-specific structure

```
.
├── appPackage/  # Teams app manifest + declarative agent manifest + MCP plugin manifest
│                # (the three-manifest family — see ADR-0002 in copilot-github-manager for an
│                # example decision record; exact scaffold produced by the Microsoft 365 Agents
│                # Toolkit, https://github.com/microsoft/m365-agent-templates)
└── env/         # Microsoft 365 Agents Toolkit per-environment config (.env.local, .env.dev, ...)
```

`new-project.sh --type m365` also installs, from `github/awesome-copilot`, the agents/
instructions/skills curated for this exact stack (declarative agent architecture, MCP
integration, Entra App Registration, security/threat-model review) — see
`.github/awesome-copilot-manifest.md` in an instantiated project for the full list, and
`vibecoding-bootstrap/scripts/install-awesome-copilot.sh`'s `m365` case for the source of truth.

## Publishing to a tenant

Two ready-to-copy Runbook templates cover the full path from a finished manifest to a live agent
in Microsoft 365 Copilot — see `docs/runbooks/README.md` for the details:

1. `templates/RUNBOOK-m365-oauth-packaging-sideload-template.md` — OAuth registration, packaging,
   and personal sideload, scripted end-to-end via the `atk` CLI (`@microsoft/m365agentstoolkit-cli`)
   instead of the manual VS Code Agents Toolkit wizard.
2. `templates/RUNBOOK-m365-tenant-publish-template.md` — tenant-wide catalog publish, once the
   sideload is validated.

Both assume **a Claude Code CLI session runs them, not a Hermes-orchestrated agent** — the M365
sign-in and the tenant-catalog publish are Microsoft APIs that only accept a delegated,
interactively-authenticated session (no service-principal/certificate app-only token works for
them), so whichever agent executes these Runbooks needs to be one a human can complete that one
login through.

## Reference implementation

[lowcodai/copilot-github-manager](https://github.com/lowcodai/copilot-github-manager) is a real
project built on this pattern (a GitHub-MCP-backed M365 Copilot agent, multi-account identity
model). Its `docs/adr/` shows a worked example of the ADR set this kind of project typically
needs: declarative agent platform choice, MCP integration and capability-inventory strategy,
Entra permission/consent model, and a delivery-responsibility matrix — reusable as a starting
point, not copied into this generic template (project-specific decisions belong in the
instantiated project's own ADRs, not in the template).

## AI Safety & Governance

Declarative agents are still AI agents: the same review discipline as `vibecoding-template-ai`
applies before every merge:
1. Review prompts/instructions with `ai-prompt-engineering-safety-review`
2. Validate the agent manifest and MCP plugin surface with `agent-governance`
3. Check OWASP LLM Top 10 compliance via `agent-owasp-compliance`
4. Run a threat model (STRIDE/DFD) on the multi-identity/credential surface with
   `threat-model-analyst` before any account-level (PAT/OAuth) feature ships

## Awesome Copilot elements curated for this type

| Element | Type | Usage |
|---------|------|-------|
| `declarative-agents-architect` | Agent | Declarative agent vs. Copilot Studio platform choice |
| `mcp-m365-agent-expert` | Agent | MCP-backed Microsoft 365 Copilot agent design |
| `declarative-agents-microsoft365.instructions.md` | Instruction | Manifest/schema reference |
| `mcp-m365-copilot.instructions.md` | Instruction | Declaring an MCP server as a plugin |
| `security-and-owasp.instructions.md` | Instruction | Security baseline |
| `mcp-create-declarative-agent`, `declarative-agents` | Skill | Scaffolding |
| `entra-agent-user` | Skill | Entra App Registration for an agent |
| `mcp-security-audit`, `mcp-implementation-security-review` | Skill | Security validation |
| `threat-model-analyst` | Skill | Formal threat model (STRIDE/DFD) |
| `secret-scanning` | Skill | Verifies a Doppler-only (or equivalent) secret policy |
| `mcp-deploy-manage-agents` | Skill | Deployment / lifecycle management |
| `mcp-m365-copilot` | Plugin | Installs into the Copilot CLI's *global* environment — run on the host that actually executes the agent, not necessarily where the project was scaffolded |

## Hooks

This repo's AI-specific hooks (same set as `vibecoding-template-ai`):

| Hook | Usage |
|------|-------|
| `session-logger` | Logs AI/Copilot session activity |
| `attester-import-check` | Verifies supply-chain import provenance |

See `.github/copilot-instructions.md` for this repo's full active hooks list, and the [governance hooks registry](https://github.com/lowcodai/vibecoding-copilot-governance/blob/main/docs/awesome-copilot-map.md) for the full ecosystem-wide catalog.

## References

- [vibecoding-copilot-governance](https://github.com/lowcodai/vibecoding-copilot-governance)
- [copilot-github-manager](https://github.com/lowcodai/copilot-github-manager) (reference implementation)
- [GitHub Issue Manager sample agent](https://learn.microsoft.com/en-us/samples/microsoftgraph/github-issue-manager/github-issue-manager-sample-agent/)
- [Build a plugin for a declarative agent from an MCP server](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins)
- [Microsoft 365 agent templates](https://github.com/microsoft/m365-agent-templates)
- [OWASP LLM Top 10](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
- [github/awesome-copilot](https://github.com/github/awesome-copilot)
