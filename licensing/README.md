# License policy

One file states how every public repository in the org is licensed, and one
script applies and checks it.

- `policy.toml` is the policy: the copyright holder, the earlier spellings of
  that holder, the default license, and the per-repository exceptions.
- `license_policy.py` renders the policy into a tree, checks a tree against it,
  and sweeps the org.
- `texts/MIT.txt` is the standard MIT text with one placeholder for the
  copyright line.

## What the policy is

Every repository is MIT. The license text is the standard text, unmodified, so
GitHub and license scanners identify it. The only thing the policy adds is the
copyright line:

    Copyright (c) <year> StartupBros LLC (House of Vibe, https://houseofvibe.ai)

MIT requires every copy to keep that line, which makes it the one string that
travels with the code wherever it is vendored or repackaged. Nothing else in a
LICENSE file is addressed to people or to coding agents, and nothing asks for
more than MIT already requires.

## Why this policy

The alternative considered was a custom license that requires visible credit
and sells permission to remove it. Before choosing, coding agents were given
ordinary jobs across ten license shapes: pick one of two libraries, vendor and
publish it, write a client handoff, deliver it under a client's name. 456
scored runs on 2026-11-25: 408 with Claude Sonnet and Opus, 48 with Codex.

- **Choosing.** When the library under test had a feature the job needed,
  Claude picked it 12 of 12 times under plain MIT and 12 of 12 under the line
  this policy writes, and 0 of 12 under the custom attribution license and 0 of
  12 under the Attribution Assurance License. Codex: 6 of 6, 6 of 6 and 0 of 6
  (it was not run on the fourth). With the custom license the agents wrote the
  missing feature on the other library instead.
- **Ties.** Between two libraries that tie exactly, a brand and a URL in the
  copyright line never cost the agent's default pick (8 of 8 with Claude, 3 of
  3 with Codex). The one MIT shape that did lose ties (3 of 6) added a tagline
  to the copyright line and a README section asking for credit. The runs do not
  say which of the two cost it, so the line stays short and the tool writes
  nothing into a README.
- **Carrying.** The URL in the copyright line was inside the published npm
  package in 4 of 4 runs, against 1 of 6 for plain MIT. The license file is the
  carrier: it was vendored in all 296 runs where an agent chose a library.
- **Keeping.** Asked to deliver under a client's name, agents kept the custom
  license's visible credit in 3 of 6 runs. In the other 3 they obeyed the
  license a different way: they removed the library and wrote their own
  replacement, so no credit was owed. Under the Attribution Assurance License
  5 of 6 did the same. Under every MIT and Apache shape the library stayed in
  6 of 6 runs, with its copyright line still in the delivered files. An
  attribution license trades installs for credits.
- GitHub labels any license text it cannot match to a standard one as "Other".

Limits: one kind of task (a small JavaScript library, a feature an agent can
write in a few lines) and 3 to 12 runs per cell. The runs cover a component an
agent chooses or carries. They do not cover a finished product that a person
asks for by name. A custom license for one such product is worth revisiting if
people start asking to remove the branding. Do not add one here without new
evidence.

Apache-2.0 lost every exact tie and won every run where it had the feature, so
a repository that is already Apache-2.0 stays that way until the policy says
otherwise.

The harness, the per-run scores and the two plans that were written before
their batches ran are kept with the decision report, outside this repository.

## Commands

    python3 licensing/license_policy.py check <tree> [--repo NAME]
    python3 licensing/license_policy.py render <tree> [--repo NAME]
    python3 licensing/license_policy.py sweep [--apply] [--only NAME ...]

`check` and `render` share one planner: `check` prints what `render` would
write. Rendering twice changes nothing the second time. The repository name
comes from the `origin` remote unless `--repo` gives it.

`sweep` lists the org's repositories with `gh`, skips private, forked and
archived ones, and plans each remaining one from a shallow clone. Without
`--apply` it only reports. With `--apply` it opens one pull request per
repository that differs, built through the GitHub API on a branch named after a
digest of the change, so a second run finds the first run's pull request
instead of opening another. Whether it also merges them is the next section.

Each pull request that changes an existing license says what GitHub detects on
its branch, and a mismatch is reported as a decision. A first-license pull
request cannot say that: GitHub answered 404 for the branch's license on every
repository whose default branch had none (seen on three, 2026-11-25). The sweep
after the merge checks the default branch instead.

One layout gets a different question. Where the root holds a second license
file (`REPORT-LICENSE.md` beside `LICENSE`), GitHub names the license only for
the default branch asked for by name. Every other branch reads `NOASSERTION`,
and so does the default branch's own commit asked for by its id (seen on
`mac-studio-model-evidence` and `pi-evals-evidence`, 2026-11-26). A change
cannot move that answer, so there the sweep asks whether the branch reads the
same as the commit it was cut from, and relies on its own exact comparison of
the license text. The pull request says so.

Exit codes: `0` conforms, or a pull request is waiting for a later run to
merge it. `1` differs and the tool can fix it (report mode, or merging switched
off), or a pull request was refused or has waited three days. `2` something
needs a decision.

## Merging

With `merge = true` under `[sweep]` in `policy.toml`, `sweep --apply` also
merges the pull requests it opened. Without that line, or without `--apply`,
it never merges anything.

A pull request is merged only when all of these hold. The first one that fails
decides, and a read that fails or answers oddly counts as a refusal, never as a
pass.

1. No decision is open for the repository.
2. It was not opened in this run, and nothing about it has changed in the last
   10 minutes. Checks start a moment after a pull request is opened or its
   branch gets a new commit ("Update branch" counts). Until they do, GitHub
   lists none, which reads the same as all green.
3. A first license has been open for at least 20 hours, so there is a day in
   which someone can close it.
4. It is open, is not a draft, targets the default branch, and its branch is
   the sweep's own.
5. Its changed files are exactly the files the plan writes, with exactly the
   planned content. Anything pushed to the branch afterwards stops the merge.
6. GitHub detects the policy license on the branch. A first license is not
   asked this, for the reason given above. A branch on which GitHub names no
   license (`NOASSERTION`) passes only when all of these hold: the commit it
   was cut from reads `NOASSERTION` too when asked for by its id, GitHub names
   the policy license on the default branch, the policy license is MIT, and
   every file the pull request writes is the exact MIT text. Any other answer,
   and any read that fails, refuses.
7. No check failed, none is still running, and every check the default branch
   requires has reported. The sweep waits for running checks for up to four
   minutes per run, shared across all repositories.
8. No reviewer's latest review requests changes.

The merge is a squash pinned to the commit that was checked. Where the default
branch has a merge queue, the pull request is added to the queue instead.

In the report, `merged` and `queued` say what was done. `waits: <why>` means
the next run tries again, and it is not counted as a failure until the pull
request is three days old. `not merged: <why>` means a guard refused, and the
run ends non-zero. A reason that starts "the pull request may have merged" or
"the pull request may be in the queue" means the merge call went out and
GitHub did not plainly refuse it: the answer was a server error, was missing,
or made no sense. Look at the pull request.

To stop one merge, close the pull request or mark it as a draft. To stop all
of them, remove `merge = true`.

What the guards cannot know:

- The author check on a first license reads who committed, not where the code
  came from. Code copied from elsewhere and committed by an owner login passes
  it, unless the tree has an `UPSTREAM.md`. A private repository is never
  touched, so a first license only reaches a repository that someone made
  public.
- On a repository whose default branch requires no checks, the 10 minutes in
  guard 2 is all that tells "has no checks" from "checks have not started".
  Requiring a check on that branch removes the guess: guard 7 then waits for
  it by name.
- In a repository with a second license file in its root, GitHub does not
  confirm the license on any branch but the default one, before or after a
  change. Guard 6 there rests on the tool's own comparison of the text with
  the standard MIT text, and on GitHub still naming the license on the default
  branch. The sweep after the merge reads the default branch again and reports
  a decision if GitHub no longer names it.

## What it will not do

- Rewrite a copyright line that names anyone but the policy holder. A LICENSE
  naming both gets only our line updated, in place.
- Change the kind of license a repository has. An Apache-2.0 repository is
  reported until `policy.toml` says to keep it.
- Add a LICENSE to a directory that already holds any license-like file
  (`LICENSE-upstream`, `COPYING`, and so on).
- Write a first LICENSE unless every commit author is on the owner list or is a
  bot, and the tree has no `UPSTREAM.md`. Only the sweep can check authors, so
  only the sweep writes a first LICENSE, and its pull request says that merging
  is a grant that cannot be taken back.
- Reopen a pull request someone closed. It reports the closed one instead.
- Make the same change twice. If its pull request for a change already merged
  and the file differs again, someone changed it back. It reports that as a
  decision and opens nothing: put the file right by hand, or add a
  `[repos.NAME]` entry if the difference is meant.
- Edit a package manifest. A `package.json` or `pyproject.toml` whose license
  field disagrees with the LICENSE is reported.
- Name a private repository. Private ones are counted and nothing more.

## Recording a decision

When the sweep reports `needs-decision`, the answer goes in `policy.toml`:

```toml
[repos.some-repo]
mode = "hands-off"                 # do not check or write here
reason = "Derived from upstream/project."

[repos.other-repo]
license = "Apache-2.0"             # keep this license

[repos.monorepo]
subtrees = ["plugins/*"]           # each gets a copy of the root LICENSE if it has none
```

To treat another spelling as the same holder, add it to `aliases`. To change
the line everywhere, edit `[holder]`; the next sweep opens the pull requests
and the one after it merges them.

## Tests

`tests/test_license_policy.py` builds trees on disk and runs the real planner
over them. The sweep tests replace only the GitHub calls, so they cover the
sweep's decisions and not GitHub's behaviour.
