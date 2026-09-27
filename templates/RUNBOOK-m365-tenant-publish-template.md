# Runbook template — M365 Copilot agent: tenant-wide catalog publish

> Copy this into your project's `docs/runbooks/` as the next `RUNBOOK-NNNN-<slug>.md`, fill in
> every placeholder, and add a real `linked_adr` before executing — see
> `docs/runbooks/README.md` and `templates/RUNBOOK-template.md` in
> `vibecoding-copilot-governance`. Do not execute this file as-is.
>
> This is the **follow-on** to `RUNBOOK-m365-oauth-packaging-sideload-template.md` — run that one
> first and validate the personal sideload end-to-end (its Step 7) before ever running this one.
> Publishing to the whole tenant catalog is a materially bigger blast radius than a personal
> sideload: other tenant members may see or install the agent, subject to your tenant's app-setup
> policies.
>
> **Executor: a Claude Code CLI session, not a Hermes-orchestrated agent.** Same reasoning as the
> sideload Runbook: `atk publish` needs the same delegated, interactively-authenticated M365
> session, and Microsoft Graph's `appCatalogs/teamsApps` write path has **no application (app-only)
> permission at all** — confirmed directly against Microsoft's own API reference
> (https://learn.microsoft.com/en-us/graph/api/teamsapp-publish) during the reference execution.
> No amount of service-principal/certificate permission tuning changes this; do not spend time
> trying to grant a service principal its way around it. A human-authenticated Claude Code CLI
> session is the mechanism, not a workaround.

```markdown
# RUNBOOK-NNNN — <Agent name> tenant-wide catalog publish

**Date:** YYYY-MM-DD
**Status:** Draft | Ready | In progress | Done | Aborted
**linked_adr:** ADR-XXXX
<!-- Must be the ADR that made tenant-wide publication an explicit, deliberate decision (not just
     "we got sideload working so why not") — who approved it, what review happened first
     (security/compliance if your organization requires it), and whether this is sideload-to-
     tenant-catalog (private, your org only) or a public AppSource-style submission (almost
     certainly out of scope for an internal agent — confirm before assuming). -->
**authored_by:** frontier-model
**execution_mode:** hermes-solo
<!-- Same taxonomy note as the sideload Runbook: this names the execution PATTERN, not the Hermes
     framework. The actual executor is a Claude Code CLI session — see the Executor note above. -->

## Preconditions

- `RUNBOOK-<sideload-runbook-number>` is `Done`: the agent is sideloaded personally, and **every**
  scope domain the agent claims to support has been exercised end-to-end from the real client
  (Teams/Copilot), including parameterized tool calls — not just a no-argument identity check.
  Verify: that Runbook's own Definition of Done is satisfied and recorded in
  `docs/operations/CURRENT.md`.
- The decision to publish tenant-wide (as opposed to leaving this a personal sideload
  indefinitely) has been made explicitly by whoever this project's governance names as the
  approver for production-tenant changes — not inferred from "the sideload worked." Record who
  approved it and when in `linked_adr` or this Runbook's own header, per your project's
  convention.
- `m365agents.yml` has a `publish` stage (add it now if the sideload Runbook's `provision` stage
  was the only one written):
  ```yaml
  publish:
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

    - uses: teamsApp/publishAppPackage
      with:
        appPackagePath: ./appPackage/build/appPackage.${{TEAMSFX_ENV}}.zip
      writeToEnvironmentFile:
        publishedAppId: TEAMS_APP_PUBLISHED_APP_ID
  ```
- A current M365 login session (`atk auth list` shows the right account) — re-run
  `atk auth login m365 --tenant <TENANT_ID>` if it has expired or this is a new session.

## Steps

### Step 1 — Re-validate before publishing

Command:
```bash
atk package --folder <app-dir> --env <name>
atk validate --folder <app-dir> --env <name>
```
Verify: both exit 0. Do not proceed on a stale build from the sideload Runbook — the manifest may
have changed since.

Rollback condition: any validation error → fix and re-run; do not publish a package that fails
validation "just this once."

### Step 2 — Publish to the tenant catalog

Command:
```bash
atk publish --env <name> --interactive false
```
Verify: exits 0; summary shows `teamsApp/publishAppPackage` as `Done` and records a
`TEAMS_APP_PUBLISHED_APP_ID` in the environment file.

Rollback condition: if this fails partway, do not retry blindly — check whether
`teamsApp/update` already succeeded (the app definition may now be updated even if the publish
step itself failed) before re-running, to avoid confusing partial states.

### Step 3 — Verify from the tenant admin surface

Command (read-only, safe to re-run):
```bash
# Confirm the app appears in the tenant's Teams app catalog listing.
# Either via the Teams admin center UI (Manage apps), or a read-only Graph call:
curl -s -H "Authorization: Bearer $DELEGATED_TOKEN" \
  "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps?\$filter=externalId eq '<TEAMS_APP_ID>'"
```
Verify: the app is present. If your tenant requires admin approval before a submitted app becomes
generally installable, check its approval status too — `publishAppPackage` submitting
successfully is not always the same as the app being immediately visible to every user, depending
on tenant app-setup policy.

### Step 4 — Record and communicate

- Update `docs/operations/CURRENT.md` and `BACKLOG.md`/task tracker: mark the publication story
  done, with the published app id and the date.
- If your organization has a channel/place where newly published internal tools are announced,
  post there — a tenant-wide publish that nobody knows about isn't accomplishing its purpose.

## Escalation stop condition

If any step in this Runbook requires a decision not already made in `linked_adr` — including
"should this really be tenant-wide, or does personal sideload actually cover the need" — STOP. Do
not decide it locally. Record the exact gap in `docs/operations/CURRENT.md`, then escalate per the
criteria documented in `agents/runbook-generator.agent.md`.

## Definition of Done

1. Every step's verification command has actually been run, with real output recorded.
2. The app is confirmed visible in the tenant catalog (Step 3), not just "the CLI said Done."
3. `docs/operations/CURRENT.md` and `CHANGELOG.md` updated.
4. The approval decision (who, when) for tenant-wide publication is recorded, not just the
   technical outcome.
5. The next executable action is recorded — typically "none, operation complete" for this being
   the final Runbook in the M365 publication sequence, unless a rollback/uninstall path needs
   documenting for this specific project (`atk uninstall`).

## References

- Linked ADR: `docs/adr/ADR-XXXX-<slug>.md`
- Prerequisite Runbook: `RUNBOOK-<sideload-runbook-number>-<slug>.md`
- Reference execution: `lowcodai/copilot-github-manager`, `docs/operations/ACTIVITY.md`
  (2026-09-27 entries) and `itshaker-dgx-spark-V2` ADR-0023 (the Graph app-only-permission
  limitation for `appCatalogs/teamsApps`)
- [Publish teamsApp — Microsoft Graph API reference](https://learn.microsoft.com/en-us/graph/api/teamsapp-publish?view=graph-rest-1.0)
- [Manage your Microsoft 365 Copilot agents](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-manage-copilot-agents)
```
