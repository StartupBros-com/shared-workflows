# shared-workflows

Org-shared **reusable GitHub Actions workflows** for StartupBros-com.

## `dependency-autopilot.yml`

A shared dependency workflow, called after a repository's PR CI finishes:

The space of run conclusions is exhaustively **partitioned into two routes, not
enumerated as a list of known values**: a tightly-scoped REPAIR set, and
everything else, which reconciles. GitHub can add new conclusion values at any
time (`stale` is a real example); because "everything else" is defined as the
negation of the repair set rather than a second positive list, no value —
known today or added later — can fall through to a silent no-op.

```text
Dependabot / Renovate PR -> CI completes
  REPAIR (failure, timed_out, action_required, startup_failure) -> bounded
      application-code repair, unless the PR already carries
      `autopilot:autofixed` (one repair attempt only) -> reconciles straight to
      the terminal handoff instead
    pushed -> hold for review before publishing, then skip the handoff for this
      SHA; the pushed head re-fires its own CI, and that completion (forced to
      tier=review by the `autopilot:autofixed` label) selects the next owner
    no changes / unavailable credentials / failed execution / already-repaired ->
      hand off the unchanged PR only if its identity and source CI can be
      revalidated
  RECONCILE (everything not in the repair set — success, skipped, neutral,
      cancelled, stale, or any future conclusion) -> reconcile the latest
      watched-workflow inventory. Only a triggering conclusion that is
      literally success, skipped, or neutral can ever count as success
      evidence; every other value in this route (cancelled, stale, unknown)
      is forced to tier=review and can only reach review_held or
      ci_unresolved, never green_safe or a merge.
    a run for this head is still genuinely pending (no conclusion yet), or a
      sibling's own conclusion is itself in the repair set (its own event
      still owes a repair attempt) -> keep the conservative hold; defer
      handoff so review does not race that repair, for as long as that
      conclusion stays in the repair set — no time bound (see Known
      boundary below for the caller-side sweep this still needs)
    all accepted + at least one success + safe -> existing ready queue (or
      merge when caller selects automerge)
    all accepted + held, OR a fully terminal inventory with zero genuine
      successes (nothing pending, nothing still owed a repair attempt) ->
      existing pro-review daemon; a terminal but all-bad inventory is never
      stuck waiting on an event that already happened
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
primary workflow: a sibling still genuinely pending, or a sibling whose own
conclusion is in the repair set (it still owes its own repair attempt), is
deliberately deferred until that sibling's watched completion event arrives.
Repair-set completions run repair; every other completion — including any
conclusion not named above — runs reconciliation. An incomplete list cannot
promise a callback or safe repair-before-review ordering.

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
  request Pro review. `unresolved` means a real future event is still owed for
  this head: a run with no conclusion yet, or a run whose conclusion is itself
  in the repair set (that workflow's own completion still owes a genuine
  repair attempt, and handing off to review now would race it). This requires
  the caller's `workflows:` list to include every PR-triggered CI workflow.
  Every other terminal conclusion — cancelled, stale, or any future value —
  owes nothing further and does not keep the hold waiting forever: once
  nothing is genuinely pending or still owed a repair attempt, reconciliation
  proceeds to `review_held` instead of staying stuck on a completion that will
  never arrive. This holds even for a fully terminal inventory with zero
  genuine successes (e.g. skipped/neutral/cancelled/stale only) — it reconciles
  to `review_held`, not an eternal `ci_unresolved`.
  - **Known boundary:** `still_owed` has no time bound — a repair-set sibling
    is owed for as long as it stays in the repair set, full stop. A bounded
    (time-based) version of this was tried and reverted: it cannot tell "the
    producer died" from "the producer's callback is merely queued behind
    this event" in the same FIFO concurrency group, and guessing wrong
    breaks repair-before-review ordering by dropping a still-pending repair
    and racing it into review. Two cases stay permanently outside what this
    workflow alone can close, and both need the same caller-side scheduled
    sweep — not implemented here:
    1. A failed sibling's completion is the *last* watched event that will
       ever fire for a given head. This workflow only reconciles when a
       watched completion event arrives; if the repair-set sibling's own
       completion never triggers a further invocation (no other watched
       workflow runs again at that head), nothing inside this workflow
       re-checks, and the PR stays `ci_unresolved` indefinitely.
    2. A producer invocation dies before establishing ownership — e.g.
       autofix's own job is killed mid-run (lost runner, host outage) after
       Codex starts but before it labels the PR or hands off, or triage's
       job dies before applying `pro-review`/`autopilot:review`. No label
       was ever written, so there is no visible owner, and no further
       completion event exists to retrigger reconciliation for that head.

    Closing both needs a caller-side scheduled sweep: a periodic workflow
    that lists open dependency-bot PRs carrying `autopilot:review` (or no
    autopilot label at all) without `pro-review`/`skip-pro-review`/`claimed`/
    `loop-run`, checks each one's *actual current* CI state directly via the
    Checks/Runs API rather than waiting on another `workflow_run` event, and
    re-invokes reconciliation (or opens review directly) for any that are
    stuck. This workflow does not reimplement that sweep — it fails closed
    by holding the PR under conservative review, not by inventing its own
    deadline for a completion that already fired or a producer that never
    got the chance to.
- **Review held:** only an all-green risk hold is eligible for the existing
  `pro-review` intake after fresh checks. A triggering conclusion is success
  evidence only when it is literally `success`, `skipped`, or `neutral` — a
  closed three-value allowlist, not an open list of "bad" values to catch.
  Every other conclusion (`cancelled`, `stale`, or any value GitHub adds
  later) is always forced to this tier (or to `ci_unresolved`) — never
  treated as success evidence and never able to reach `green_safe` or a
  merge, even if the freshly fetched inventory now looks all-green.
- **Successful push:** preserves `autopilot:autofixed`. The hold is applied before
  publishing AI changes so a label-write failure cannot leave them eligible for
  automerge. If a push fails, the conservative review hold remains. The handoff
  does **not** hand off this pushed SHA: it was published with a short-lived
  GitHub App token, so CI genuinely re-fires on the new head, and
  `autopilot:autofixed` already forces that head's own completion to
  tier=review. Handing off the pre-rerun SHA would race that CI and could
  strand the repair with no active owner if the rerun then failed.
- **Already repaired:** a PR that already carries `autopilot:autofixed` has had
  its one repair attempt. A second red completion on that PR does not start a
  second repair; auto-fix admission refuses it and reconciles straight to the
  terminal handoff so the still-red head goes to review instead of looping.
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
