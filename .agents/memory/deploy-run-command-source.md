---
name: Deploy run command source
description: Why a Reserved VM deploy reports "cannot find run command" and how the run command is sourced in this repl.
---

# Deployment "cannot find run command"

In this Agent-managed repl (`.replit` holds only `[agent]`; direct `.replit`
edits are blocked), the deployment's run command is sourced from the primary
**"Start application" workflow**, not from a user-editable field in the
Publishing tab. The Publishing UI here does NOT expose a Run command box.

**Symptom:** Publish fails with "cannot find run command" when no
"Start application" workflow exists (e.g. only an unrelated workflow like the
mockup-sandbox preview is configured).

**Fix:** create the run workflow with the workflows tool:
`configureWorkflow({ name: "Start application", command: "python3 app.py", waitForPort: 8000, outputType: "webview" })`.
`python3` resolves via the venv at `.pythonlibs/bin/python3` (it is on PATH for
workflows/deploy even when a bare shell check sometimes misreports it). The app
binds `PORT` (default 8000), host 0.0.0.0.

**Why:** the deploy run command is derived from the primary workflow. No
workflow → nothing to run → the publish step errors out before building.

**How to apply:** if a deploy "cannot find run command", first check
`listWorkflows()`; if there's no "Start application" workflow, create one with
the real run command. This app needs **Reserved VM** (always-on refresh loop,
in-memory state, single process) — not Autoscale.
