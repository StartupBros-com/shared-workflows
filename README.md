# shared-workflows

Org-shared **reusable GitHub Actions workflows** for StartupBros-com.

## `dependency-autopilot.yml`

A centralized self-healing dependency loop. Each repo adds a thin stub that calls
it on `workflow_run`; all the logic lives here so it's fixed in one place.

```
Dependabot opens a PR -> repo CI runs ->
  green -> auto-merge   (genuine same-repo Dependabot PR, whole rollup green, safe bump)
  red   -> Codex reads the failed logs, pushes the minimal code fix, CI re-runs
```

### Add it to a repo

1. Commit a `.github/dependabot.yml` (the "opener").
2. Commit `.github/workflows/dependency-autopilot.yml` (the stub) — set
   `workflows:` to **every** PR-triggered CI workflow `name:` in that repo, and
   `ecosystem:` to `pnpm` | `npm` | `pip`:

```yaml
name: Dependency Autopilot
on:
  workflow_run:
    workflows: ["CI"]          # <- this repo's PR CI workflow name(s)
    types: [completed]
permissions:
  contents: write
  pull-requests: write
  actions: read
jobs:
  autopilot:
    if: >-
      github.event.workflow_run.event == 'pull_request' &&
      startsWith(github.event.workflow_run.head_branch, 'dependabot/')
    uses: StartupBros-com/shared-workflows/.github/workflows/dependency-autopilot.yml@main
    with:
      ci_conclusion: ${{ github.event.workflow_run.conclusion }}
      pr_branch:     ${{ github.event.workflow_run.head_branch }}
      ci_run_id:     ${{ github.event.workflow_run.id }}
      ecosystem:     pnpm
    secrets: inherit
```

### Notes

- **Auto-merge needs no secret** — it runs on the default `GITHUB_TOKEN`.
- **Auto-fix** needs `CODEX_AUTH` + a push token (`CI_BOT_PAT`, or a GitHub App
  token). Where those aren't inherited (e.g. public repos with org secrets scoped
  to private), the fix path skips cleanly and auto-merge still runs.
- The merge path enforces `author == dependabot[bot]` and rejects fork PRs, so it
  is safe to call from public repositories.
