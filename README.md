# shared-workflows

Org-shared **reusable GitHub Actions workflows** for StartupBros-com.

## `dependency-autopilot.yml`

A shared dependency workflow, called after a repository's PR CI finishes:

```text
Dependabot / Renovate PR -> CI completes
  green -> classify
    sibling CI unresolved -> keep the conservative hold; defer handoff
    all green + safe -> existing ready queue (or merge when caller selects automerge)
    all green + held -> existing pro-review daemon
  red (failure, timed_out, action_required, startup_failure) -> bounded
      application-code repair
    pushed -> hold for review, rerun CI, hand off the new head
    no changes / unavailable credentials / failed execution -> hand off the
      unchanged PR only if its identity and source CI can be revalidated
  cancelled / neutral / skipped / anything else -> no handoff path; not
      unresolved red work
```

The workflow serializes all producer and handoff jobs for a caller's PR branch.
Its workflow-level `queue: max` retains up to 100 pending runs and starts them FIFO
by when they enter the queue, instead of allowing a late stale event to replace a
pending current-head run. The handoff waits for both producer jobs to finish;
labeling at red-path admission would race the repair worker. It covers held green
PRs as well as unresolved red PRs without adding another reviewer or enrolling
each repository in a new queue.

### Existing callers

The `ci_conclusion`, `pr_branch`, `ci_run_id`, `ecosystem`, `mode`, and `runner`
inputs are unchanged. `mode` defaults to **queue**, not automerge. Existing callers
pinned to an older shared-workflows commit do **not** acquire this behavior until
that pin is deliberately updated to a reviewed commit.

A caller filters to dependency-bot PR events and passes the original CI run.
Its `workflows:` list **must name every PR-triggered CI workflow**, not only the
primary workflow: unresolved sibling CI is deliberately deferred until that
sibling's watched completion event arrives, so an incomplete list cannot promise
a failure callback or safe repair-before-review ordering.

```yaml
name: Dependency Autopilot
on:
  workflow_run:
    workflows: ["CI", "Other PR checks"] # list EVERY PR-triggered CI workflow name
    types: [completed]
permissions:
  contents: write
  pull-requests: write
  actions: read
jobs:
  autopilot:
    if: >-
      github.event.workflow_run.event == 'pull_request' &&
      (startsWith(github.event.workflow_run.head_branch, 'dependabot/') ||
       startsWith(github.event.workflow_run.head_branch, 'renovate/'))
    uses: StartupBros-com/shared-workflows/.github/workflows/dependency-autopilot.yml@<reviewed-commit-sha>
    with:
      ci_conclusion: ${{ github.event.workflow_run.conclusion }}
      pr_branch: ${{ github.event.workflow_run.head_branch }}
      ci_run_id: ${{ github.event.workflow_run.id }}
      ecosystem: pnpm
    secrets:
      APP_ID: ${{ secrets.APP_ID }}
      APP_PRIVATE_KEY: ${{ secrets.APP_PRIVATE_KEY }}
      CODEX_AUTH: ${{ secrets.CODEX_AUTH }}
```

Use an existing approved runner through `runner` where required. The handoff uses
that same runner choice. A skipped caller on an unrelated main-branch run is
expected, not evidence of a dependency-workflow outage.

### Outcomes and ownership

- **Safe green:** retains the existing `autopilot:ready` queue. It does not gain an
  expensive Pro review merely because this integration exists. `mode: automerge`
  retains the existing merge path, bound to the classified head SHA.
- **CI unresolved:** retains the conservative `autopilot:review` hold but does not
  request Pro review. Handoff waits for a watched missing, pending, or failed
  sibling workflow to complete; this requires the caller's `workflows:` list to
  include every PR-triggered CI workflow.
- **Review held:** only an all-green risk hold is eligible for the existing
  `pro-review` intake after fresh checks.
- **Successful push:** preserves `autopilot:autofixed`. The hold is applied before
  publishing AI changes so a label-write failure cannot leave them eligible for
  automerge. If a push fails, the conservative review hold remains.
- **No changes:** distinct from execution failure; it does not mean the CI failure
  was repaired.
- **Unavailable credentials:** the optional repair cannot run, but the unchanged
  trusted PR may still be handed off using the caller's `GITHUB_TOKEN`.
- **Failed execution:** a nonzero Codex exit fails the producer rather than being
  reported as a successful no-op. The terminal handoff can still queue the
  independently revalidated, unchanged PR. Missing provenance fails closed. A
  forced job termination (job-level timeout, lost runner) that leaves the
  outcome output empty is also inferred as failed execution, but only when the
  job itself reports `failure` and had already bound a PR number and trigger
  SHA before it died; a `cancelled` job or missing binding still fails closed.
- **Existing owner / stale or rejected target:** no handoff. Drafts, human
  assignees, pending User or Team review requests, and `pro-review`,
  `skip-pro-review`, `claimed`, or `loop-run` labels prevent a second writer from
  being admitted. A changed head is not silently substituted for the head the
  producer handled.

A newly requested handoff records the source CI, producer outcome, and expected
head in one SHA-keyed PR comment, then applies `pro-review`. Only a marker authored
by `github-actions[bot]` with REST user type `Bot` is authoritative; human and
other-bot lookalikes do not suppress the workflow-owned comment. Repeated events
do not repeat the handoff. Existing label metadata is not overwritten. API
failures are visible as failed jobs, not reported as successful queue events.

The **existing pro-review daemon** owns the next review/repair action. It consumes
`pro-review`, binds decisions to a PR/head, and may repair confirmed findings. It
**never merges**; final disposition remains with the existing author/merge
workflow and its policies. This integration does not grant merge authority,
apply `loop-ok`, change service configuration, rotate credentials, or enable
all-PR review mode.

A persistent GitHub label is not an atomic SHA lease. The workflow rechecks head
and ownership before mutation and skips explicit active owners; a new owner or
push can still arrive between API calls. The consumer must independently bind
its effects to its current head. Ownership not represented in PR metadata is not
visible to this producer.

### Verification

```bash
bash tests/classify-tier.test.sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
mise exec actionlint@1.7.12 -- actionlint \
  -ignore '^unexpected key "queue" for "concurrency" section\. expected one of "cancel-in-progress", "group"$' \
  .github/workflows/*.yml
```

Actionlint v1.7.12, and upstream at `011a6d15e749bb3f2d771eed9c7aa0e7e3e10ee7`,
still reject GitHub's documented workflow-level `queue` property. The anchored ignore
suppresses only that exact unsupported-property message; all other lint findings
remain active. Remove the ignore when actionlint supports `concurrency.queue`. The
executable graph test independently pins `queue: max`, `cancel-in-progress: false`,
and the absence of per-job concurrency.

The Python tests use PyYAML (also used by CI's YAML parse check). They execute the
actual workflow shell blocks, with GitHub, Git, and Codex replaced only at the
external CLI boundary. Producer-to-handoff cases pass actual step outputs into
the next block; they do not copy the classifier into a new implementation.

These tests prove routing and failure behavior, not live review completion. A
bounded live check must record the real caller run, PR/head, queue event, and
consumer result. `pro-review` being present, a unit being `active`, or an absence
of new PRs is not proof that a review ran. Check the daemon journal for actual
processing or a concrete deferral (for example runtime/plugin version skew), and
never record a queued or deferred request as delivered.
