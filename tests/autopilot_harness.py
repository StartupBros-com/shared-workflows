"""Reusable harness for executing dependency-autopilot workflow shell blocks."""
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/dependency-autopilot.yml").read_text())
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
BRANCH = "dependabot/npm/pkg-2.0.0"
RUN = {
    "event": "pull_request",
    "headSha": OLD_SHA,
    "url": "https://github.com/StartupBros-com/example/actions/runs/9001",
}


def step(job, step_id):
    return next(item for item in WORKFLOW["jobs"][job]["steps"] if item.get("id") == step_id)


# The freshness predicate (trigger_superseded/trigger_lagging derived from
# t_num/t_attempt/t_concl vs. l_num/l_attempt/l_concl) cannot live in a
# shared file: this is a reusable workflow, so production logic runs inline
# in caller repos. Both call sites (triage's "triage" step, autofix's "pre"
# step) instead wrap their copy in a matching
# `# autopilot-freshness-predicate:begin/:end` marker pair, so it can be
# extracted verbatim from each and compared for byte-equality.
FRESHNESS_PREDICATE_PATTERN = re.compile(
    r"# autopilot-freshness-predicate:begin\n(.*?)"
    r"  # autopilot-freshness-predicate:end\n",
    re.S,
)


def freshness_predicate_blocks():
    """Extract the triage and autofix copies of the shared freshness
    predicate. Each call site's `run:` script must contain the marker pair
    exactly once; a missing or duplicated marker fails loudly here rather
    than silently comparing the wrong (or no) text."""
    triage_script = step("triage", "triage")["run"]
    autofix_script = step("autofix", "pre")["run"]
    triage_blocks = FRESHNESS_PREDICATE_PATTERN.findall(triage_script)
    autofix_blocks = FRESHNESS_PREDICATE_PATTERN.findall(autofix_script)
    assert len(triage_blocks) == 1, triage_script
    assert len(autofix_blocks) == 1, autofix_script
    return triage_blocks[0], autofix_blocks[0]


# `gh pr list --head` matches by branch name only — gh 2.97.0 does not
# support "<owner>:<branch>" qualification — so on a public caller repo a
# same-named fork branch can collide with a trusted dependency-bot branch.
# All three PR-lookup sites (triage's "triage" step, autofix's "pre" step,
# handoff's "handoff" step) filter to same-repository candidates BEFORE
# enforcing uniqueness, wrapped in a matching
# `# autopilot-same-repo-filter:begin/:end` marker pair so the three copies
# can be extracted verbatim and compared for byte-equality, the same
# pattern as FRESHNESS_PREDICATE_PATTERN above.
SAME_REPO_FILTER_PATTERN = re.compile(
    r"# autopilot-same-repo-filter:begin\n(.*?)"
    r"# autopilot-same-repo-filter:end\n",
    re.S,
)


def same_repo_filter_blocks():
    """Extract the triage, autofix, and handoff copies of the shared
    same-repository PR filter. Each call site's `run:` script must contain
    the marker pair exactly once; a missing or duplicated marker fails
    loudly here rather than silently comparing the wrong (or no) text."""
    triage_script = step("triage", "triage")["run"]
    autofix_script = step("autofix", "pre")["run"]
    handoff_script = step("handoff", "handoff")["run"]
    blocks = {}
    for name, script in (("triage", triage_script), ("autofix", autofix_script), ("handoff", handoff_script)):
        found = SAME_REPO_FILTER_PATTERN.findall(script)
        assert len(found) == 1, (name, script)
        blocks[name] = found[0]
    return blocks["triage"], blocks["autofix"], blocks["handoff"]


# Round-12 P0: head-SHA equality (checked just above each copy of this
# block) is provenance-blind to WHICH repository produced that commit — a
# public fork can push the identical dependency-bump commit on a
# same-named branch, giving its own `pull_request` CI run the SAME head
# SHA as the legitimate PR. All three call sites (triage's "triage" step,
# autofix's "pre" step, handoff's "handoff" step) confirm the triggering
# run's own head_repository and head_branch before treating it as
# provenance, wrapped in a matching
# `# autopilot-fork-provenance-gate:begin/:end` marker pair, the same
# byte-equality pattern as SAME_REPO_FILTER_PATTERN above.
FORK_PROVENANCE_GATE_PATTERN = re.compile(
    r"# autopilot-fork-provenance-gate:begin\n(.*?)"
    r"# autopilot-fork-provenance-gate:end\n",
    re.S,
)


def fork_provenance_gate_blocks():
    """Extract the triage, autofix, and handoff copies of the shared
    fork-provenance admission gate. Each call site's `run:` script must
    contain the marker pair exactly once; a missing or duplicated marker
    fails loudly here rather than silently comparing the wrong (or no)
    text."""
    triage_script = step("triage", "triage")["run"]
    autofix_script = step("autofix", "pre")["run"]
    handoff_script = step("handoff", "handoff")["run"]
    blocks = {}
    for name, script in (("triage", triage_script), ("autofix", autofix_script), ("handoff", handoff_script)):
        found = FORK_PROVENANCE_GATE_PATTERN.findall(script)
        assert len(found) == 1, (name, script)
        blocks[name] = found[0]
    return blocks["triage"], blocks["autofix"], blocks["handoff"]


# Round-14: a red no-change/no-push outcome (no_changes and its siblings,
# plus failed_execution) used to be immediately reviewable at handoff, even
# while a sibling workflow's own failure on the same head was still queued
# behind this invocation for its own repair turn — the same reconciliation
# triage already performs for every other red-adjacent path. Both call
# sites (triage's "triage" step, handoff's "handoff" step) share the live
# per-sibling revalidation loop, wrapped in a matching
# `# autopilot-sibling-verification:begin/:end` marker pair, the same
# byte-equality pattern as FRESHNESS_PREDICATE_PATTERN above.
SIBLING_VERIFICATION_PATTERN = re.compile(
    r"# autopilot-sibling-verification:begin\n(.*?)"
    r"# autopilot-sibling-verification:end\n",
    re.S,
)


def sibling_verification_blocks():
    """Extract the triage and handoff copies of the shared sibling live
    revalidation loop. Each call site's `run:` script must contain the
    marker pair exactly once; a missing or duplicated marker fails loudly
    here rather than silently comparing the wrong (or no) text."""
    triage_script = step("triage", "triage")["run"]
    handoff_script = step("handoff", "handoff")["run"]
    blocks = {}
    for name, script in (("triage", triage_script), ("handoff", handoff_script)):
        found = SIBLING_VERIFICATION_PATTERN.findall(script)
        assert len(found) == 1, (name, script)
        blocks[name] = found[0]
    return blocks["triage"], blocks["handoff"]


# Companion to SIBLING_VERIFICATION_PATTERN: given the verified inventory,
# both call sites derive `still_owed`/`unresolved` from SIBLING entries only
# (`workflow_id != $t_wf`) — each site's own trigger workflow is resolved by
# its own caller (triage's trigger_lagging override; handoff's already-final
# AUTOFIX_OUTCOME), never by this generic scan. Wrapped in a matching
# `# autopilot-sibling-still-owed:begin/:end` marker pair, the same
# byte-equality pattern as above.
SIBLING_STILL_OWED_PATTERN = re.compile(
    r"# autopilot-sibling-still-owed:begin\n(.*?)"
    r"# autopilot-sibling-still-owed:end\n",
    re.S,
)


def sibling_still_owed_blocks():
    """Extract the triage and handoff copies of the shared still-owed
    computation. Each call site's `run:` script must contain the marker
    pair exactly once; a missing or duplicated marker fails loudly here
    rather than silently comparing the wrong (or no) text."""
    triage_script = step("triage", "triage")["run"]
    handoff_script = step("handoff", "handoff")["run"]
    blocks = {}
    for name, script in (("triage", triage_script), ("handoff", handoff_script)):
        found = SIBLING_STILL_OWED_PATTERN.findall(script)
        assert len(found) == 1, (name, script)
        blocks[name] = found[0]
    return blocks["triage"], blocks["handoff"]


# Round-12 P1: `gh pr list --head` defaults to a 30-item cap with no
# server-side owner scoping, so an attacker opening more fork PRs than the
# cap on this predictable branch name can crowd the legitimate same-repo
# PR out of the result set entirely, before the same-repo filter ever
# runs. All three PR-lookup sites (triage's "triage" step, autofix's "pre"
# step, handoff's "handoff" step) instead resolve the same-repository head
# IN the REST query itself (`pulls?head=<owner>:<branch>`), wrapped in a
# matching `# autopilot-scoped-pr-lookup:begin/:end` marker pair, the same
# byte-equality pattern as SAME_REPO_FILTER_PATTERN above.
SCOPED_PR_LOOKUP_PATTERN = re.compile(
    r"# autopilot-scoped-pr-lookup:begin\n(.*?)"
    r"# autopilot-scoped-pr-lookup:end\n",
    re.S,
)


def scoped_pr_lookup_blocks():
    """Extract the triage, autofix, and handoff copies of the shared
    owner-qualified PR lookup. Each call site's `run:` script must contain
    the marker pair exactly once; a missing or duplicated marker fails
    loudly here rather than silently comparing the wrong (or no) text."""
    triage_script = step("triage", "triage")["run"]
    autofix_script = step("autofix", "pre")["run"]
    handoff_script = step("handoff", "handoff")["run"]
    blocks = {}
    for name, script in (("triage", triage_script), ("autofix", autofix_script), ("handoff", handoff_script)):
        found = SCOPED_PR_LOOKUP_PATTERN.findall(script)
        assert len(found) == 1, (name, script)
        blocks[name] = found[0]
    return blocks["triage"], blocks["autofix"], blocks["handoff"]


def make_shell_harness(testcase, **config):
    harness = ShellHarness({"prs": [trusted_pr()], "run": RUN} | config)
    testcase.addCleanup(harness.temp.cleanup)
    return harness


def make_real_git_harness(testcase, files):
    harness = RealGitHarness(files)
    testcase.addCleanup(harness.cleanup)
    return harness


def assert_success(testcase, result):
    testcase.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def run_handoff(harness, **changes):
    env = {
        "CI_CONCLUSION": "success", "TRIAGE_RESULT": "success",
        "TRIAGE_OUTCOME": "review_held", "TRIAGE_NUMBER": "21",
        "TRIAGE_TRIGGER_SHA": OLD_SHA, "AUTOFIX_RESULT": "skipped",
        "AUTOFIX_OUTCOME": "", "AUTOFIX_NUMBER": "",
        "AUTOFIX_TRIGGER_SHA": "", "AUTOFIX_PUSHED_SHA": "",
    } | changes
    return harness.run("handoff", "handoff", **env)


def run_red_handoff(harness, outcome="no_changes", **changes):
    return run_handoff(harness, **({
        "CI_CONCLUSION": "failure", "TRIAGE_RESULT": "skipped",
        "TRIAGE_OUTCOME": "", "AUTOFIX_RESULT": "success",
        "AUTOFIX_OUTCOME": outcome, "AUTOFIX_NUMBER": "21",
        "AUTOFIX_TRIGGER_SHA": OLD_SHA,
    } | changes))


def mutation_calls(harness):
    return [c for c in harness.calls() if c[:3] in (
        ["gh", "pr", "edit"], ["gh", "pr", "comment"],
        ["gh", "pr", "merge"], ["gh", "label", "create"],
    ) or c[:2] == ["git", "push"]]


def assert_queued(testcase, harness):
    edits = [c for c in harness.calls() if c[:3] == ["gh", "pr", "edit"] and "pro-review" in c]
    testcase.assertEqual(len(edits), 1, harness.calls())
    testcase.assertIn("Handoff: queued", harness.summary.read_text())
    testcase.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))


def assert_run_inadmissible_everywhere(testcase, **run_config):
    """Round-12 P0: shared body for every "this run must not be admitted as
    CI provenance" test (fork head_repository, mismatched head_branch, an
    unconfirmable/blank field). Every site that consumes the triggering run
    (triage's "triage" step, autofix's "pre" step, handoff) rejects it
    gracefully: producers reach stale_rejected with no mutation calls and
    no Codex invocation, and handoff exits nonzero without queuing."""
    for job, step_id in (("triage", "triage"), ("autofix", "pre")):
        with testcase.subTest(job=job, run_config=run_config):
            harness = testcase.harness(**run_config)
            assert_success(testcase, harness.run(job, step_id))
            testcase.assertEqual(harness.outputs()["outcome"], "stale_rejected")
            testcase.assertEqual(mutation_calls(harness), [])
            testcase.assertFalse(any(c[0] == "codex" for c in harness.calls()))
    harness = testcase.harness(**run_config)
    result = testcase.handoff(harness)
    testcase.assertNotEqual(result.returncode, 0)
    testcase.assertEqual(mutation_calls(harness), [])
    testcase.assertNotIn("Handoff: queued", harness.summary.read_text())


def assert_run_provenance_fetch_failure_everywhere(testcase, **run_config):
    """Round-12 P0: shared body for "the single-run REST fetch itself
    fails outright" — a harder failure than a disagreeing field, so
    producers abort non-zero rather than reaching a graceful stale_rejected
    outcome; handoff likewise exits non-zero without queuing."""
    for job, step_id in (("triage", "triage"), ("autofix", "pre")):
        with testcase.subTest(job=job, run_config=run_config):
            harness = testcase.harness(**run_config)
            testcase.assertNotEqual(harness.run(job, step_id).returncode, 0)
            testcase.assertEqual(mutation_calls(harness), [])
    harness = testcase.harness(**run_config)
    result = testcase.handoff(harness)
    testcase.assertNotEqual(result.returncode, 0)
    testcase.assertEqual(mutation_calls(harness), [])


def assert_root_concurrency(testcase, workflow):
    testcase.assertIn("github.repository", workflow["concurrency"]["group"])
    testcase.assertIn("inputs.pr_branch", workflow["concurrency"]["group"])
    testcase.assertEqual(workflow["concurrency"]["queue"], "max")
    testcase.assertFalse(workflow["concurrency"]["cancel-in-progress"])
    for job_name, candidate in workflow["jobs"].items():
        testcase.assertNotIn("concurrency", candidate, job_name)


def trusted_pr(**changes):
    pr = {
        "number": 21,
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "app/dependabot", "is_bot": True},
        "baseRefName": "main",
        "headRefName": BRANCH,
        "isCrossRepository": False,
        "headRefOid": OLD_SHA,
        "title": "chore(deps): bump pkg from 1.0.0 to 2.0.0",
        "labels": [],
        "assignees": [],
        "reviewRequests": [],
    }
    pr.update(changes)
    return pr


def lagging_cases(conclusion):
    """Run-list inventory entries that cannot prove trigger #11/workflow 1
    current: no entry for that workflow_id, and a stale entry whose
    run_number (10) still lags the trigger's own (11)."""
    return (
        ("missing", {"workflow_id": 2, "run_number": 1, "conclusion": conclusion}),
        ("lagging", {"workflow_id": 1, "run_number": 10, "conclusion": conclusion}),
    )


FAKE_CLI = r'''#!/usr/bin/env python3
import copy
import json
import os
import sys
from pathlib import Path

config = json.loads(Path(os.environ["FAKE_CONFIG"]).read_text())
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {
    "pr_view": 0, "added_labels": [], "removed_labels": [], "comments": [],
}
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ["FAKE_CALLS"]).open("a") as handle:
    handle.write(json.dumps([name, *args]) + "\n")
joined = " ".join([name, *args])
if any(token in joined for token in config.get("fail_commands", [])):
    sys.exit(42)


def save():
    state_path.write_text(json.dumps(state))


def decorate(pr):
    pr = copy.deepcopy(pr)
    labels = [x for x in pr.get("labels", []) if x["name"] not in state["removed_labels"]]
    for label in state["added_labels"]:
        if not any(x["name"] == label for x in labels):
            labels.append({"name": label})
    pr["labels"] = labels
    if "pushed_head" in state:
        pr["headRefOid"] = state["pushed_head"]
    return pr


if name == "gh":
    if args[:2] == ["pr", "list"]:
        # Round-12 P1 regression fixture: real `gh pr list` defaults to
        # `--limit 30` with no server-side owner qualification for
        # `--head`, which is exactly the bug the scoped-pr-lookup marker
        # replaced this call site with. Simulated here (cap + branch-name-
        # only match, no owner scoping) purely so a hand-edit reverting a
        # call site back to this endpoint is provable red by the P1
        # collision regression test, not because production still calls
        # this endpoint.
        limit = 30
        if "--limit" in args:
            limit = int(args[args.index("--limit") + 1])
        head_arg = args[args.index("--head") + 1] if "--head" in args else None
        candidates = [decorate(pr) for pr in config.get("prs", [])]
        if head_arg is not None:
            queried_branch = head_arg.rsplit(":", 1)[-1]
            candidates = [pr for pr in candidates if pr.get("headRefName") == queried_branch]
        print(json.dumps(candidates[:limit]))
    elif args[:2] == ["pr", "view"]:
        views = config.get("views", config.get("prs", []))
        if not views:
            sys.exit(44)
        index = state["pr_view"]
        state["pr_view"] += 1
        save()
        print(json.dumps(decorate(views[min(index, len(views) - 1)])))
    elif args[:2] == ["run", "view"] and "--log-failed" in args:
        print(config.get("failed_log", "tests failed"), end="")
    elif args[:2] == ["run", "view"]:
        # Trigger-currency defaults: a test that does not care about
        # currency (nearly all of them — this behavior predates currency
        # checking) gets a trigger that is trivially "current" by
        # construction — its live conclusion tracks whatever CI_CONCLUSION
        # this specific step invocation used, and its workflow/run identity
        # matches the default single-entry run inventory below. A test
        # exercising currency itself overrides "run" and/or "runs" to make
        # them disagree.
        run_cfg = dict(config.get("run", {}))
        run_cfg.setdefault("conclusion", os.environ.get("CI_CONCLUSION", "success"))
        run_cfg.setdefault("workflowDatabaseId", 1)
        run_cfg.setdefault("number", 1)
        run_cfg.setdefault("attempt", 1)
        # Round-13 P2 (FINDING B): a test simulating a directly observed
        # PENDING rerun sets conclusion=None explicitly (present, not
        # absent, so the setdefault above leaves it alone) without also
        # having to spell out status — a completed run always carries a
        # conclusion, so status defaults from whichever this run's own
        # conclusion is; only a test constructing the pending case on
        # purpose ever needs to override status too.
        run_cfg.setdefault("status", "completed" if run_cfg.get("conclusion") is not None else "in_progress")
        print(json.dumps(run_cfg))
    elif args[0] == "api":
        endpoint = args[1]
        if "/comments" in endpoint:
            pages = config.get("comment_pages", [[]]) + [state["comments"]]
            if "--slurp" in args:
                print(json.dumps(pages))
            else:
                for page in pages:
                    print(json.dumps(page))
        elif "/labels/" in endpoint:
            print(json.dumps({"name": endpoint.rsplit("/", 1)[1]}))
        elif "/pulls?head=" in endpoint:
            # Round-12 P1's server-side owner-qualified PR lookup. Mimic
            # the real REST `pulls?head=<owner>:<branch>&state=open`
            # endpoint's OWN scoping (not just the production jq's
            # after-the-fact filter) so a test with more-than-the-old-cap
            # fork PRs actually proves the fix: a fork PR's owner never
            # matches the queried owner, so it is excluded HERE, same as
            # GitHub would exclude it server-side.
            query = endpoint.split("head=", 1)[1]
            head_value = query.split("&", 1)[0]
            queried_owner, _, queried_branch = head_value.partition(":")
            repo_full_name = os.environ.get("GITHUB_REPOSITORY", "")

            def to_rest(pr):
                pr = decorate(pr)
                author = pr.get("author", {})
                cross = pr.get("isCrossRepository", False)
                head_repo = "attacker/fork" if cross else repo_full_name
                return {
                    "number": pr["number"],
                    "title": pr.get("title", ""),
                    "state": (pr.get("state", "OPEN") or "OPEN").lower(),
                    "draft": pr.get("isDraft", False),
                    "user": {
                        "login": author.get("login", ""),
                        "type": "Bot" if author.get("is_bot") else "User",
                    },
                    "base": {"ref": pr.get("baseRefName", "")},
                    "head": {
                        "ref": pr.get("headRefName", ""),
                        "sha": pr.get("headRefOid", ""),
                        "repo": {"full_name": head_repo},
                    },
                    "labels": pr.get("labels", []),
                    "assignees": [
                        {"login": a.get("login", ""), "type": "Bot" if a.get("is_bot") else "User"}
                        for a in pr.get("assignees", [])
                    ],
                    "requested_reviewers": [
                        r for r in pr.get("reviewRequests", []) if r.get("__typename") != "Team"
                    ],
                    "requested_teams": [
                        r for r in pr.get("reviewRequests", []) if r.get("__typename") == "Team"
                    ],
                }

            matches = []
            for pr in config.get("prs", []):
                rest = to_rest(pr)
                rest_owner = rest["head"]["repo"]["full_name"].split("/", 1)[0]
                if rest_owner == queried_owner and rest["head"]["ref"] == queried_branch \
                   and rest["state"] == "open":
                    matches.append(rest)
            pages = [matches]
            if "--slurp" in args:
                print(json.dumps(pages))
            else:
                for page in pages:
                    print(json.dumps(page))
        elif endpoint.rsplit("/", 1)[-1].isdigit() and "/actions/runs/" in endpoint:
            # Round-12 P0's fork-provenance admission fetch: a single run
            # object's REST shape (`head_repository`, `head_branch`) is not
            # exposed by `gh run view --json` at all, so this is a distinct
            # endpoint from the run-list one below. Defaults are the
            # legitimate same-repo/same-branch values, so an unrelated test
            # (nearly every pre-existing one) admits trivially by
            # construction; a test exercising the gate itself overrides
            # `run["head_repository"]`/`run["head_branch"]` to disagree.
            #
            # Round-13 P1 (FINDING A): the SAME single-run endpoint is now
            # also the sibling-freshness re-read: production fetches it by
            # each selected inventory entry's own `.id`, not just RUN_ID.
            # Look the queried id up among the run-list fixtures (applying
            # the SAME `id` default the list branch below uses, since each
            # `gh` invocation is a separate process — nothing set on an
            # entry by a prior call persists here) and, when found, answer
            # from that entry's own fields by default, so an unrelated
            # test (nearly every pre-existing one) gets a sibling that
            # trivially matches its list entry live, by construction. A
            # test exercising the gate itself overrides `live_runs[id]`
            # (status/attempt/conclusion) to disagree on purpose. A
            # queried id that matches no list entry (RUN_ID, in nearly
            # every test) falls back to the pre-existing `run`-keyed
            # provenance-only response untouched.
            queried_id = endpoint.rsplit("/", 1)[-1]
            run_cfg = config.get("run", {})
            default_repo = {"full_name": os.environ.get("GITHUB_REPOSITORY", "")}
            match = None
            for page in config.get("run_pages", [config.get("runs", {"workflow_runs": []})]):
                for entry in page.get("workflow_runs", []):
                    entry.setdefault("run_attempt", 1)
                    candidate_id = entry.get("id", entry["workflow_id"] * 1_000_000 + entry["run_number"])
                    if str(candidate_id) == queried_id:
                        match = entry
                        break
                if match:
                    break
            if match is None:
                print(json.dumps({
                    "head_repository": run_cfg.get("head_repository", default_repo),
                    "head_branch": run_cfg.get("head_branch", os.environ.get("BRANCH", "")),
                }))
            else:
                override = config.get("live_runs", {}).get(queried_id, {})
                live_conclusion = override.get("conclusion", match.get("conclusion"))
                default_status = "completed" if live_conclusion is not None else "in_progress"
                print(json.dumps({
                    "head_repository": override.get("head_repository", match.get("head_repository", default_repo)),
                    "head_branch": override.get("head_branch", match.get("head_branch", os.environ.get("BRANCH", ""))),
                    "status": override.get("status", default_status),
                    "run_attempt": override.get("attempt", match.get("run_attempt", 1)),
                    "conclusion": live_conclusion,
                }))
        else:
            default_runs = {"workflow_runs": [
                {"workflow_id": 1, "run_number": 1, "run_attempt": 1, "conclusion": "success"},
            ]}
            pages = config.get("run_pages", [config.get("runs", default_runs)])
            # A fixture that specifies workflow_runs without run_attempt
            # (nearly every pre-existing test — this field predates
            # attempt-bound currency checking) defaults to attempt 1, the
            # same default `gh run view` uses above, so an unrelated test's
            # currency stays trivially "current" by construction. Likewise
            # head_repository/head_branch default to the legitimate
            # same-repo/same-branch values (Round-12 P0/P1's inventory
            # scoping) so only a test constructing a fork-origin entry on
            # purpose ever disagrees.
            for page in pages:
                for entry in page.get("workflow_runs", []):
                    entry.setdefault("run_attempt", 1)
                    entry.setdefault("head_repository", {"full_name": os.environ.get("GITHUB_REPOSITORY", "")})
                    entry.setdefault("head_branch", os.environ.get("BRANCH", ""))
                    entry.setdefault("pull_requests", [])
                    # Round-13 P1 (FINDING A): production re-reads each
                    # selected entry by `.id` (the single-run endpoint
                    # above), so every entry needs one — a real run's `id`
                    # is stable across a rerun's attempts (only
                    # `run_attempt`/`conclusion` change), so the default is
                    # derived from workflow_id+run_number only, matching
                    # the lookup the single-run branch above recomputes
                    # independently for the same fixture.
                    entry.setdefault("id", entry["workflow_id"] * 1_000_000 + entry["run_number"])
            if "--slurp" in args:
                print(json.dumps(pages))
            else:
                for page in pages:
                    print(json.dumps(page))
    elif args[:2] == ["label", "create"]:
        if config.get("label_exists", True) and "--force" not in args:
            sys.exit(1)
    elif args[:2] == ["pr", "edit"]:
        for flag, target, opposite in (
            ("--add-label", "added_labels", "removed_labels"),
            ("--remove-label", "removed_labels", "added_labels"),
        ):
            if flag in args:
                label = args[args.index(flag) + 1]
                if label not in state[target]:
                    state[target].append(label)
                if label in state[opposite]:
                    state[opposite].remove(label)
        save()
    elif args[:2] == ["pr", "comment"]:
        state["comments"].append({
            "body": args[args.index("--body") + 1],
            "user": {"type": "Bot", "login": "github-actions[bot]"},
        })
        save()
    elif args[:2] != ["pr", "merge"]:
        sys.exit(47)
elif name == "git":
    if args[:1] == ["--literal-pathspecs"]:
        args = args[1:]
    if args[:2] == ["diff", "--name-only"]:
        files = config.get("changed_files", [])
        if "HEAD" in args:
            files = files + config.get("staged_files", [])
        if files:
            print("\n".join(files))
    elif args[0] == "ls-files":
        files = config.get("untracked_files", [])
        if "--modified" in args:
            files = files + config.get("changed_files", [])
        if files:
            print("\n".join(files))
    elif args[0] == "rev-parse":
        print(config.get("remote_sha") if args[1].startswith("origin/") else config.get("pushed_sha"))
    elif args[0] == "push":
        state["pushed_head"] = config["pushed_sha"]
        save()
    elif args[0] not in {"fetch", "config", "add", "commit"}:
        sys.exit(48)
elif name == "codex":
    sys.stdin.read()
    if "-o" in args:
        Path(args[args.index("-o") + 1]).write_text("test Codex result\n")
    sys.exit(config.get("codex_rc", 0))
'''


class ShellHarness:
    def __init__(self, config):
        self.temp = tempfile.TemporaryDirectory(prefix="dependency-autopilot-test-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(config))
        self.calls_path = self.root / "calls.jsonl"
        self.output = self.root / "output.txt"
        self.summary = self.root / "summary.md"
        for name in ("gh", "git", "codex"):
            path = self.bin / name
            path.write_text(FAKE_CLI)
            path.chmod(0o700)

    def run(self, job, step_id, **env):
        self.output.write_text("")
        script = self.root / "step.sh"
        script.write_text(step(job, step_id)["run"])
        run_env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "RUNNER_TEMP": str(self.root),
            "FAKE_CONFIG": str(self.config),
            "FAKE_STATE": str(self.root / "state.json"),
            "FAKE_CALLS": str(self.calls_path),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_REPOSITORY": "StartupBros-com/example",
            "GITHUB_SERVER_URL": "https://github.com",
            "BRANCH": BRANCH,
            "RUN_ID": "9001",
            "GH_TOKEN": "test-token",
            "MODE": "queue",
            "CI_CONCLUSION": "success",
            "CODEX_AUTH": "test-auth",
            "APP_ID": "123",
            "APP_PRIVATE_KEY": "test-key",
        } | env
        return subprocess.run(
            ["bash", str(script)], cwd=self.root, env=run_env,
            text=True, capture_output=True, check=False,
        )

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())


class RealGitHarness:
    def __init__(self, files):
        self.temp = tempfile.TemporaryDirectory(prefix="dependency-autopilot-git-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "fixture-repo"
        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        self.output = self.runner_temp / "output.txt"
        self.calls_path = self.runner_temp / "git-calls.jsonl"
        self.real_git = shutil.which("git")
        if not self.real_git:
            raise RuntimeError("git is required for dependency-autopilot tests")
        self.git("init", "-b", BRANCH, str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Dependency Autopilot Test")
        self.git("config", "user.email", "dependency-autopilot-test@example.invalid")
        for name, content in files.items():
            self.write(name, content)
        self.git("add", "--all")
        self.git("commit", "-m", "test fixture")
        self.trigger_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-u", "origin", BRANCH)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        wrapper = self.bin / "git"
        wrapper.write_text(r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["REAL_GIT_CALLS"], "a") as handle:
    handle.write(json.dumps(["git", *args]) + "\n")
if args and args[0] == "push" and len(args) > 1 and args[1].startswith("https://"):
    sys.exit(0)
real_git = os.environ["REAL_GIT"]
os.execv(real_git, [real_git, *args])
''')
        wrapper.chmod(0o700)

    def cleanup(self):
        self.temp.cleanup()

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(
            [self.real_git, *args], cwd=cwd or self.repo,
            text=True, capture_output=True, check=check,
        )

    def rename(self, old, new):
        (self.repo / new).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", old, new)

    def run(self, job, step_id, **env):
        self.output.write_text("")
        script = self.runner_temp / "step.sh"
        script.write_text(step(job, step_id)["run"])
        run_env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "RUNNER_TEMP": str(self.runner_temp),
            "REAL_GIT": self.real_git,
            "REAL_GIT_CALLS": str(self.calls_path),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.runner_temp / "summary.md"),
            "GITHUB_REPOSITORY": "StartupBros-com/example",
            "BRANCH": BRANCH,
            "TRIGGER_SHA": self.trigger_sha,
            "PUSH_TOKEN": "test-push-token",
        } | env
        return subprocess.run(
            ["bash", str(script)], cwd=self.repo, env=run_env,
            text=True, capture_output=True, check=False,
        )

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())
