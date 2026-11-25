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
instead of opening another. It never merges.

Exit codes: `0` conforms, `1` differs and the tool can fix it (or a pull
request is waiting for a merge), `2` something needs a decision.

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
the line everywhere, edit `[holder]`; the next sweep opens the pull requests.

## Tests

`tests/test_license_policy.py` builds trees on disk and runs the real planner
over them. The sweep tests replace only the GitHub calls, so they cover the
sweep's decisions and not GitHub's behaviour.
