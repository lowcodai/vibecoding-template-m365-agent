# Runbook template — M365 Copilot agent: OAuth registration, packaging, sideload (`atk` CLI)

> Copy this into your project's `docs/runbooks/` as the next `RUNBOOK-NNNN-<slug>.md`, fill in
> every placeholder, and add a real `linked_adr` before executing — see
> `docs/runbooks/README.md` and `templates/RUNBOOK-template.md` in
> `vibecoding-copilot-governance`. Do not execute this file as-is.
>
> Extracted from the first real execution of this pattern:
> [lowcodai/copilot-github-manager](https://github.com/lowcodai/copilot-github-manager)
> (`docs/operations/ACTIVITY.md`, entries dated 2026-09-27) — a GitHub-MCP-backed agent published
> to the ITShaker tenant. That session took 3 days of manual VS Code wizard trial-and-error before
> this scripted procedure replaced it in under 30 minutes. The pitfalls in Step 5 below are real
> failures hit in that session, not hypothetical.
>
> **Executor: a Claude Code CLI session, not a Hermes-orchestrated agent.** Every command in this
> Runbook (`atk auth login`, `atk provision`, `atk install`, and any Entra/Graph call) is meant to
> be typed and observed by Claude Code CLI directly — on the operator's own machine or another host
> where Claude Code CLI runs, never inside a headless Hermes-gateway-style container. This is a
> structural requirement, not a preference: the M365-side login (`atk auth login m365`) is a real
> interactive OAuth flow that needs a browser, and Microsoft Graph's app-catalog/Developer-Portal
> endpoints this Runbook eventually depends on accept only a delegated (freshly human-authenticated)
> session — no certificate/service-principal app-only token works for them, confirmed against
> Microsoft's own documentation in the reference execution's linked ADR. A Hermes agent with no
> display cannot complete that login itself. If this project's Entra/Azure-side operations also use
> a shared coding-agent service principal (see e.g. `itshaker-dgx-spark-V2` ADR-0023's
> `Coding-Agent-SP`), that identity is a Claude Code CLI consumer here too, distinct from whatever
> identity a separate Hermes agent in this organization uses for its own unrelated work.

```markdown
# RUNBOOK-NNNN — <Agent name> OAuth registration + packaging + sideload

**Date:** YYYY-MM-DD
**Status:** Draft | Ready | In progress | Done | Aborted
**linked_adr:** ADR-XXXX
<!-- Must be the ADR that chose: the MCP server this agent wraps, the RemoteMCPServer +
     OAuthPluginVault authentication pattern, and the identity used to run this Runbook
     (a coding-agent service principal + one interactive M365 login, vs. a fully manual
     VS Code wizard). If that ADR doesn't exist yet, write it first — see
     `docs/adr/README.md`. -->
**authored_by:** frontier-model
<!-- Recommended over local-model for this Runbook specifically: it touches a production M365
     tenant and live OAuth credentials — matches the escalation criteria in
     `agents/runbook-generator.agent.md` (production impact + security-sensitive). -->
**execution_mode:** hermes-solo
<!-- "hermes-solo" here names the execution-pattern taxonomy (single-context, sequential, no
     role isolation) inherited from this org's methodology doc — it does NOT mean the Hermes
     agent/framework executes this Runbook. Per the "Executor" note above and the precedent set
     by `itshaker-dgx-spark-V2` ADR-0023 (a hermes-solo-labeled operation executed directly by a
     frontier model, not Hermes, for the same reason: interactive login + production-tenant
     sensitivity), the actual executor is a Claude Code CLI session. -->


## Preconditions

- `atk` (Microsoft 365 Agents Toolkit CLI, package `@microsoft/m365agentstoolkit-cli`) is
  installed, version pinned: `npm install -g @microsoft/m365agentstoolkit-cli@<VERSION>`.
  Verify: `atk --version` prints `<VERSION>`.
- The agent's three manifests already exist under `appPackage/` (`manifest.json`,
  `declarativeAgent.json`, `ai-plugin.json`) with a real Entra App Registration `id` already
  provisioned (Teams app manifest `id` = that Entra `appId`) — this Runbook does not create that
  registration; it assumes a prior Runbook did (Entra App Registration + any user-facing OAuth
  app the manifest's own sign-in flow needs).
- The MCP server's own authentication requirements are resolved **live, not assumed**:
  1. `curl -i <mcp-server-url>` unauthenticated → expect `401` with a `WWW-Authenticate` header
     naming a `resource_metadata` URL (RFC 9728).
  2. `curl -s <resource_metadata-url>` → gives the real `authorization_servers` entry.
  3. `curl -s <authorization-server>/.well-known/oauth-authorization-server` (append the
     authorization server's own path segment if the bare `.well-known` 404s — RFC 8414 allows the
     metadata document to live at `<issuer>/.well-known/oauth-authorization-server<issuer-path>`)
     → gives real `authorization_endpoint` / `token_endpoint`, and — critically — whether a
     `registration_endpoint` is present at all.
  - **If `registration_endpoint` is absent: the target does not support RFC 7591 Dynamic Client
    Registration**, regardless of what any ADR or prior note claims. Do not assume DCR from a
    service's general reputation — GitHub's OAuth authorization server
    (`https://github.com/login/oauth`) has no `registration_endpoint`, which invalidated an
    explicit "DCR confirmed" claim in an earlier ADR of the reference implementation. In that
    case, a **static** OAuth client (`client_id`/`client_secret`) must already exist for the
    target service (created through its own developer console) before continuing.
- A secret store (Doppler or equivalent) holds: the static OAuth client's `client_id`/
  `client_secret` (or nothing, if the target genuinely supports DCR), and the Teams app's own
  `TEAMS_APP_ID`. Never write these into the repo — see `env/.env.<name>.example` below.
- One person with a Microsoft 365 Copilot license, able to complete one interactive browser
  sign-in per work session (`atk auth login m365` opens a local-loopback OAuth flow — a real
  browser window, not a device code, when run on a machine with a display; on a headless host it
  falls back to printing a URL to open elsewhere).

## Steps

### Step 1 — Author `m365agents.yml`

Command: create `m365agents.yml` at the project root (schema version matches your installed
`atk`; check `atk --version` against `https://aka.ms/m365-agents-toolkits/<version>/yaml.schema.json`
if unsure — this schema has changed between CLI releases, verify rather than copy a remembered
URL):

```yaml
# yaml-language-server: $schema=https://aka.ms/m365-agents-toolkits/<SCHEMA_VERSION>/yaml.schema.json
version: <SCHEMA_VERSION>

environmentFolderPath: ./env

provision:
  - uses: oauth/register
    with:
      name: <short-name>-oauth
      appId: ${{TEAMS_APP_ID}}
      flow: authorizationCode
      identityProvider: Custom
      applicableToApps: AnyApp
      # ^ Not SpecificApp. A known tenant-level failure mode (`TokenFetchFailed: The App ID
      #   used in the request does not match the App ID in the authentication configuration`)
      #   happens because Copilot's internal agent id (prefixed `T_...`) differs from the
      #   manifest GUID under SpecificApp. Set AnyApp up front to avoid rediscovering this.
      baseUrl: <mcp-server-origin>            # e.g. https://api.githubcopilot.com
      authorizationUrl: <authorization_endpoint>
      tokenUrl: <token_endpoint>
      clientId: ${{OAUTH_CLIENT_ID}}
      clientSecret: ${{OAUTH_CLIENT_SECRET}}  # omit if the target genuinely supports DCR/PKCE-only
      isPKCEEnabled: false                    # true only if the target has no client secret
      scope: <comma,separated,scopes>         # comma-separated, not space-separated
    writeToEnvironmentFile:
      configurationId: MCP_OAUTH_CONFIG_ID

  - uses: teamsApp/zipAppPackage
    with:
      manifestPath: ./appPackage/manifest.json
      outputZipPath: ./appPackage/build/appPackage.${{TEAMSFX_ENV}}.zip
      outputFolder: ./appPackage/build

  - uses: teamsApp/validateAppPackage
    with:
      appPackagePath: ./appPackage/build/appPackage.${{TEAMSFX_ENV}}.zip

  - uses: teamsApp/update
    with:
      appPackagePath: ./appPackage/build/appPackage.${{TEAMSFX_ENV}}.zip
```

Verify: `python3 -c "import yaml; yaml.safe_load(open('m365agents.yml'))"` exits 0.

### Step 2 — Environment files

Command:
```bash
cat > env/.env.<name>.example <<'EOF'
TEAMS_APP_ID=
OAUTH_CLIENT_ID=
OAUTH_CLIENT_SECRET=
EOF
```
Commit the `.example` file. Add to `.gitignore` (create the file if the project has none — do
not assume one exists):
```
<app-dir>/env/.env.*
!<app-dir>/env/*.example
<app-dir>/appPackage/build/
```
Then write the real `env/.env.<name>` with values resolved from the secret store at execution
time — never hand-typed into a shared terminal, never committed.

Verify: `git check-ignore -v env/.env.<name>` prints a match; `git check-ignore -v
env/.env.<name>.example` prints nothing (must NOT be ignored).

### Step 3 — Manifest pre-flight checklist (fix before first `atk provision`, not after)

Each of these was a real, separately-discovered failure in the reference execution. Check all six
before running Step 5 — the toolkit's own schema fetch can lag behind what the real Developer
Portal backend accepts (a stale local `atk validate` pass is not proof; a known-good published
file failed the exact same local schema check after Microsoft changed it upstream without a
version bump — treat `atk provision`'s live backend validation, not `atk validate` alone, as the
final authority):

1. `manifest.json`: every string field respects its length limit (`description.short` ≤ 80 chars
   is the one that bit the reference implementation — Teams gave no hint of the limit beyond the
   error itself).
2. `ai-plugin.json` top level is **flat**, not wrapped in an `ai_plugin: {...}` object (an
   obsolete OpenAI-plugin-era convention some earlier tooling/templates still generate):
   `schema_version`, `name_for_human` (keep it short, ~20 chars — longer values are accepted with
   a truncation warning, not an error, but keep it short anyway), `namespace`,
   `description_for_human`, `description_for_model` all at the top level alongside `functions`
   and `runtimes`.
3. Every entry in `functions[]` has both `name` and a `description` (a bare `{"name": "..."}` is
   rejected).
4. `runtimes[0].spec.mcp_tool_description` is `{"file": "mcp-tools.json"}` (an object,
   file-reference form), nested **inside** `spec` alongside `url` — not a `$[file(...)]`
   templating string, and not a sibling of `spec`.
5. `runtimes[0].auth.reference_id` — not `auth.client_id`. `OAuthPluginVault` auth uses
   `reference_id`; using the wrong key name passes naive JSON validation but fails the real
   backend silently (generic "we can't upload the app" with no field-level detail).
6. `outline.png` is transparent PNG, 32×32, containing **only** fully-transparent and fully-white
   pixels (verify: `python3 -c "from PIL import Image; im=Image.open('appPackage/outline.png');
   print(set(im.getdata()))"` — every tuple must be `(0,0,0,0)` or `(255,255,255,255)`). A solid
   opaque background (even matching the brand accent color) fails validation.

### Step 4 — One-time interactive M365 login

Command:
```bash
atk auth login m365 --tenant <TENANT_ID>
```
Verify: `atk auth list` prints the signed-in account's UPN.

Rollback condition: none needed — this only creates a local, revocable token cache
(`atk auth logout m365` to clear it).

### Step 5 — Provision

Command:
```bash
atk provision --env <name> --interactive false
```
Verify: exits 0; summary shows `oauth/register`, `teamsApp/zipAppPackage`,
`teamsApp/validateAppPackage`, `teamsApp/update` all `Done`. On any validation failure, fix the
specific reported field and re-run — do not skip straight to Step 6.

Rollback condition: none — this step only writes to the Developer Portal's own record for this
app id and to the local build output; re-running is idempotent (the `oauth/register` action skips
re-creating a registration when its env var is already populated).

### Step 6 — First-time sideload

Do **not** rely on `teamsApp/update` alone for a brand-new app id — it fails with
`TeasmAppNotExists` (sic) if the app was never actually created in the Developer Portal (an Entra
App Registration existing under the same GUID is not the same record). Use `install` for the
first deployment:

Command:
```bash
atk install --file-path appPackage/build/appPackage.<name>.zip --interactive false
```
Verify: prints a `TitleId` (prefixed `T_...`) and an `AppId` — this `AppId` is a Developer Portal
identifier distinct from the manifest's own `id` GUID; record both in
`docs/operations/CURRENT.md`.

Rollback condition: `atk uninstall --title-id <TitleId>` (or the manifest's environment name,
per `atk uninstall --help`) removes the sideloaded install without touching the app registration.

### Step 7 — End-to-end validation from the actual client

Open Microsoft 365 Copilot or Teams as the installing user. Run at least one **parameterized**
tool call (something that takes real arguments, e.g. an operation scoped to a specific resource
by id/name) — not just a no-argument call like an identity/whoami tool. A no-argument call
succeeding is not proof the integration works: in the reference execution, `get_me` (no
arguments) returned `200 OK` while every parameterized tool calling the exact same MCP server
failed, due to a server-side bug unrelated to this project
([github/github-mcp-server#3311](https://github.com/github/github-mcp-server/issues/3311) — the
MCP server enforced a non-standard header-projection requirement regardless of the client's
negotiated protocol version). If a parameterized call fails, check the raw request/response in
the client's own agent-debug panel before assuming a manifest problem — the failure may be
upstream, in the MCP server itself, not in anything this Runbook controls.

## Escalation stop condition

If any step in this Runbook requires a decision not already made in `linked_adr`, STOP. Do not
decide it locally, do not infer it from surrounding code or convention. Record the exact gap in
`docs/operations/CURRENT.md`, then escalate per the criteria documented in
`agents/runbook-generator.agent.md`.

## Definition of Done

1. Every step's verification command has actually been run, with real output recorded.
2. Step 7's parameterized end-to-end check passed, not just a trivial no-argument call.
3. `docs/operations/CURRENT.md` reflects the new state (app id, title id, what was validated).
4. `CHANGELOG.md` updated.
5. No real secret or `TEAMS_APP_ID` value was committed (`git log -p -- env/ appPackage/` clean).
6. The next executable action is recorded — typically the tenant-wide publish decision, see
   `RUNBOOK-m365-tenant-publish-template.md`.

## References

- Linked ADR: `docs/adr/ADR-XXXX-<slug>.md`
- Reference execution: `lowcodai/copilot-github-manager`,
  `docs/operations/ACTIVITY.md` (2026-09-27 entries)
- [Build a plugin for a declarative agent from an MCP server](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins)
- [Configure authentication for MCP and API plugins in agents](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication)
- [Troubleshoot MCP apps in Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-mcp-apps-troubleshooting)
```
