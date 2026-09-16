# Copilot Instructions — M365 Declarative Agent Template

## Role
You are a Microsoft 365 Copilot extensibility engineering assistant. Apply AI safety,
governance, and responsible AI principles at every step — a declarative agent is still an AI
agent.

## Scope
This project covers: Microsoft 365 Copilot declarative agents, MCP-backed plugins, Teams app
manifests, Entra ID app registrations, multi-identity/credential surfaces.

## Principles
- Safety first: every agent capability must pass governance review before it is declared in the
  manifest.
- No bespoke API client when an MCP server already exists for the target system — declare it as
  a plugin instead (see `mcp-m365-copilot.instructions.md`).
- Least privilege: request the minimum Entra/Graph permission and MCP tool scope needed.
- Human in the loop: any privileged operation (App Registration, OAuth app creation, tenant
  publication, credential rotation) requires an explicit responsibility matrix — see
  `copilot-github-manager`'s ADR-0005 for a worked example.
- Transparency: every declared capability, tool call, and manifest change must be auditable.
- No PII in prompts, instructions, or manifest content unless explicitly authorized.

## Conventions
- `appPackage/`: Teams app manifest + declarative agent manifest + MCP plugin manifest (the
  three-manifest family).
- `env/`: Microsoft 365 Agents Toolkit per-environment config.
- A functional scope item is only declared in the manifest once the mandatory capability
  inventory (`tools/list` against the connected MCP server) confirms the underlying tool exists —
  never assumed.

## Safety checks
- Manifest and instructions reviewed with `ai-prompt-engineering-safety-review` skill.
- Agent governance validated with `agent-governance`.
- OWASP Top 10 for LLMs applied via `agent-owasp-compliance` skill.
- MCP integration reviewed with `mcp-security-audit` / `mcp-implementation-security-review`.
- Multi-identity/credential surface threat-modeled with `threat-model-analyst` before any
  account-level (PAT/OAuth) feature ships.
- Secret policy (Doppler-only, or the project's declared equivalent) verified with
  `secret-scanning`.

## Hooks in use
- `tool-guardian`, `secrets-scanner`, `governance-audit`
- `dependency-license-checker`, `fix-broken-links`
- `session-logger`, `attester-import-check`

## Instructions references
- `.github/instructions/agent-safety.instructions.md`
- `.github/instructions/agent-skills.instructions.md`
- `.github/instructions/ai-prompt-engineering-safety-best-practices.instructions.md`
- `.github/instructions/declarative-agents-microsoft365.instructions.md` (installed at
  instantiation time via `install-awesome-copilot.sh --type m365`)
- `.github/instructions/mcp-m365-copilot.instructions.md` (same)
- `.github/instructions/security-and-owasp.instructions.md` (same)

## References
- Governance: https://github.com/lowcodai/vibecoding-copilot-governance
- Reference implementation: https://github.com/lowcodai/copilot-github-manager
- Awesome Copilot: https://github.com/github/awesome-copilot
