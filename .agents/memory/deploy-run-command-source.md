---
name: Deploy run command source
description: Why this repl's deploy reports "could not find run command" and what actually resolves it.
---

# Deployment "could not find run command" (this repl)

This is a **classic (non-artifact) repl** — there are NO `artifact.toml` /
`.replit-artifact/` files anywhere (the `artifacts/kalshi-monitor` dir is just a
stale copy of the app and was never registered; `listArtifacts()` is empty).
So the artifact.toml deploy path does NOT apply here.

`.replit` `[deployment]` had `deploymentTarget = "cloudrun"` (autoscale) and
**no `run` line** → autoscale strictly requires a run command, so publish fails
with "could not find run command."

## What I canNOT do
- Edit `.replit` directly — blocked ("run commands owned by a different tool").
- `deployConfig()` callback — does NOT exist (probed: undefined).
- `verifyAndReplaceArtifactToml` — needs an existing artifact.toml; none exist.
- `createArtifact` — has no Python type.
- Change deployment type — UI-only, user must do it.

## What DOES resolve it
- The deploy run command for a Reserved VM comes from the **run-button
  workflow**. `configureWorkflow({name:"Start application", command:"python3 app.py", waitForPort:8000})`
  adds that workflow to `.replit` `[workflows]` (this DID write `.replit` via
  the allowed tool). The app needs **Reserved VM** (always-on refresh loop,
  in-memory state, WS) — NOT autoscale/cloudrun.
- **User action required:** in the Publishing UI, set Deployment type =
  **Reserved VM** (not Autoscale) and publish. Autoscale is what forces the
  missing `[deployment].run`; Reserved VM uses the workflow command.

**Why:** agent has no tool to write `[deployment].run`; the only lever is the
workflow (for VM) + the user choosing VM in the UI.
