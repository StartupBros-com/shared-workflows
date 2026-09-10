#!/usr/bin/env python3
"""Execute the reusable workflow's real shell blocks with fake external CLIs."""
import json
import re
import subprocess
import unittest

import autopilot_harness
from autopilot_harness import (
    NEW_SHA,
    OLD_SHA,
    ROOT,
    RUN,
    WORKFLOW,
    RealGitHarness,
    ShellHarness,
    lagging_cases,
    step,
    trusted_pr,
)


class WorkflowTests(unittest.TestCase):
    maxDiff = None

    def harness(self, **config):
        return autopilot_harness.make_shell_harness(self, **config)

    def real_git_harness(self, files):
        return autopilot_harness.make_real_git_harness(self, files)

    def success(self, result):
        autopilot_harness.assert_success(self, result)

    def handoff(self, harness, **changes):
        return autopilot_harness.run_handoff(harness, **changes)

    def red_handoff(self, harness, outcome="no_changes", **changes):
        return autopilot_harness.run_red_handoff(harness, outcome=outcome, **changes)

    def mutation_calls(self, harness):
        return autopilot_harness.mutation_calls(harness)

    def assert_queued(self, harness):
        autopilot_harness.assert_queued(self, harness)

    def assert_root_concurrency(self, workflow):
        autopilot_harness.assert_root_concurrency(self, workflow)

    def test_graph_serializes_producers_then_handoff(self):
        job = WORKFLOW["jobs"]["handoff"]
        self.assertEqual(set(job["needs"]), {"triage", "autofix"})
        self.assertIn("always()", job["if"])
        self.assertIn("!cancelled()", job["if"])
        self.assertEqual(job["runs-on"], "${{ inputs.runner }}")
        self.assertEqual(job["permissions"], {
            "contents": "read", "pull-requests": "write", "actions": "read",
        })
        self.assert_root_concurrency(WORKFLOW)

    def test_graph_rejects_an_invalid_queue_value(self):
        invalid = WORKFLOW | {
            "concurrency": WORKFLOW["concurrency"] | {"queue": "latest"},
        }
        with self.assertRaises(AssertionError):
            self.assert_root_concurrency(invalid)

    def test_all_pr_metadata_requests_include_review_requests(self):
        requests = []
        for job in WORKFLOW["jobs"].values():
            for item in job["steps"]:
                script = item.get("run", "").replace("\\\n", " ")
                requests.extend(re.findall(
                    r"gh pr (?:list|view)\b[^\n]*?--json ([^>\n]+)", script,
                ))
        self.assertEqual(len(requests), 7, requests)
        for fields in requests:
            self.assertIn("reviewRequests", fields.split(","), fields)

    def test_metadata_only_jobs_have_bounded_timeouts(self):
        # Triage and handoff do no long-running work; a platform default
        # multi-hour timeout would hold the single-slot branch concurrency
        # group open far longer than necessary.
        for job_name in ("triage", "handoff"):
            timeout = WORKFLOW["jobs"][job_name].get("timeout-minutes")
            self.assertIsNotNone(timeout, job_name)
            self.assertLessEqual(timeout, 10, job_name)
            self.assertGreaterEqual(timeout, 5, job_name)

    def test_codex_step_has_its_own_timeout_shorter_than_the_job(self):
        codex_timeout = step("autofix", "codex").get("timeout-minutes")
        job_timeout = WORKFLOW["jobs"]["autofix"]["timeout-minutes"]
        self.assertIsNotNone(codex_timeout)
        self.assertLess(codex_timeout, job_timeout)
        self.assertGreaterEqual(job_timeout - codex_timeout, 5)

    def test_only_existing_triage_can_merge_and_failed_codex_cannot_push(self):
        merges = [(name, s.get("id")) for name, job in WORKFLOW["jobs"].items()
                  for s in job["steps"] if "gh pr merge" in s.get("run", "")]
        self.assertEqual(merges, [("triage", "triage")])
        self.assertIn("steps.codex.outcome == 'success'", step("autofix", "guard")["if"])
        ids = [s.get("id") for s in WORKFLOW["jobs"]["autofix"]["steps"]]
        self.assertLess(ids.index("flag"), ids.index("push"))
        self.assertIn("steps.flag.outputs.flagged == '1'", step("autofix", "push")["if"])
        self.assertNotIn('--add-label "pro-review"', step("autofix", "flag")["run"])

    def test_ci_runs_real_block_tests(self):
        self.assertIn("python3 -m unittest", (ROOT / ".github/workflows/ci.yml").read_text())

    def test_all_shell_blocks_parse(self):
        for job in WORKFLOW["jobs"].values():
            for item in job["steps"]:
                if "run" in item:
                    with self.subTest(step=item.get("name")):
                        self.success(subprocess.run(["bash", "-n"], input=item["run"],
                                                    text=True, capture_output=True, check=False))

    def test_automerge_remains_bound_to_the_classified_head(self):
        harness = self.harness(prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")])
        self.success(harness.run("triage", "triage", MODE="automerge"))
        merge = next(c for c in harness.calls() if c[:3] == ["gh", "pr", "merge"])
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], OLD_SHA)

    def test_autofixed_label_forces_review_hold_even_under_automerge(self):
        harness = self.harness(prs=[trusted_pr(
            title="chore(deps): bump pkg from 1.0.0 to 1.0.1",
            labels=[{"name": "autopilot:autofixed"}],
        )])
        self.success(harness.run("triage", "triage", MODE="automerge"))
        self.assertEqual(harness.outputs()["outcome"], "review_held")
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_triage_gate_is_the_negation_of_the_repair_set(self):
        # There is no enumerated "known good" list for triage/handoff
        # reconciliation. Its job gate, and the handoff's routing, are both
        # defined as "not in the tightly-scoped repair set" so the space
        # stays exhaustively partitioned: any conclusion GitHub adds later
        # (like the `stale` this fixes) reconciles instead of falling
        # through to a silent no-op.
        autofix_expr = WORKFLOW["jobs"]["autofix"]["if"]
        autofix_match = re.search(
            r"contains\(fromJSON\('(\[[^)]*\])'\), inputs\.ci_conclusion\)", autofix_expr)
        self.assertIsNotNone(autofix_match, autofix_expr)
        repair_set = set(json.loads(autofix_match.group(1)))
        self.assertEqual(repair_set, {"failure", "timed_out", "action_required", "startup_failure"})

        triage_expr = WORKFLOW["jobs"]["triage"]["if"]
        triage_match = re.search(
            r"!contains\(fromJSON\('(\[[^)]*\])'\), inputs\.ci_conclusion\)", triage_expr)
        self.assertIsNotNone(triage_match, triage_expr)
        self.assertEqual(set(json.loads(triage_match.group(1))), repair_set)

        handoff_script = step("handoff", "handoff")["run"]
        self.assertIn('"failure"|"timed_out"|"action_required"|"startup_failure"', handoff_script)
        self.assertNotIn("no handoff path", handoff_script)

    def test_freshness_predicate_is_locked_between_triage_and_autofix(self):
        # See autopilot_harness.freshness_predicate_blocks: this is the
        # automated guard replacing "a comment asking humans to remember"
        # for a predicate that (being reusable-workflow YAML) cannot be
        # factored into one shared file.
        triage_block, autofix_block = autopilot_harness.freshness_predicate_blocks()
        # Non-trivial: guards against a marker pair wrapping an empty or
        # near-empty span, which would make the equality assertion vacuous.
        self.assertIn("trigger_superseded", triage_block)
        self.assertIn("trigger_lagging", triage_block)
        self.assertIn("l_attempt", triage_block)
        self.assertIn("l_concl", triage_block)
        self.assertEqual(triage_block, autofix_block)

    def test_freshness_predicate_lockstep_guard_fails_on_drift(self):
        # Proves the guard above is discriminating, not vacuously true: a
        # one-sided edit to either copy (here, autofix's) must make the
        # byte-equality assertion fail, exactly as it would on a real
        # future drift between the two call sites.
        triage_block, autofix_block = autopilot_harness.freshness_predicate_blocks()
        drifted_autofix_block = autofix_block.replace(
            'elif [ "$l_attempt" != "$t_attempt" ]', 'elif [ "$l_attempt" = "$t_attempt" ]', 1)
        self.assertNotEqual(triage_block, drifted_autofix_block)
        self.assertNotEqual(autofix_block, drifted_autofix_block)

    def test_success_skipped_and_neutral_share_the_reconciliation_path(self):
        cases = (
            ("skipped", "chore(deps): bump pkg from 1.0.0 to 1.0.1", "green_safe"),
            ("neutral", "chore(deps): bump pkg from 1.0.0 to 2.0.0", "review_held"),
        )
        for conclusion, title, expected in cases:
            with self.subTest(conclusion=conclusion, expected=expected):
                harness = self.harness(
                    prs=[trusted_pr(title=title)],
                    runs={"workflow_runs": [
                        {"workflow_id": 1, "run_number": 1, "conclusion": "success"},
                        {"workflow_id": 2, "run_number": 1, "conclusion": conclusion},
                    ]},
                )
                self.success(harness.run("triage", "triage"))
                producer = harness.outputs()
                self.assertEqual(producer["outcome"], expected)
                self.success(self.handoff(
                    harness,
                    CI_CONCLUSION=conclusion,
                    TRIAGE_OUTCOME=producer["outcome"],
                    TRIAGE_NUMBER=producer["number"],
                    TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
                ))
                if expected == "review_held":
                    self.assert_queued(harness)
                else:
                    self.assertFalse(any("pro-review" in call for call in harness.calls()))

    def test_only_skipped_or_neutral_inventory_reconciles_to_review_held_not_a_forever_hold(self):
        # A fully terminal inventory (nothing pending, nothing owed) with
        # zero genuine successes must never be green, but it must also not
        # be stuck labelled ci_unresolved forever waiting on an event that
        # already happened — it reconciles to review_held and hands off.
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    runs={"workflow_runs": [
                        {"workflow_id": 1, "run_number": 1, "conclusion": conclusion},
                    ]},
                )
                self.success(harness.run(
                    "triage", "triage", MODE="automerge", AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
                ))
                producer = harness.outputs()
                self.assertEqual(producer["outcome"], "review_held")
                self.success(self.handoff(
                    harness,
                    CI_CONCLUSION=conclusion,
                    TRIAGE_OUTCOME=producer["outcome"],
                    TRIAGE_NUMBER=producer["number"],
                    TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
                ))
                self.assertFalse(any(call[:3] == ["gh", "pr", "merge"] for call in harness.calls()))
                self.assert_queued(harness)

    def test_harmless_late_label_change_does_not_abandon_triage(self):
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            views=[trusted_pr(
                title="chore(deps): bump pkg from 1.0.0 to 1.0.1",
                labels=[{"name": "release-note:skip"}],
            )],
        )
        self.success(harness.run("triage", "triage"))
        self.assertEqual(harness.outputs()["outcome"], "green_safe")
        self.assertTrue(any("autopilot:ready" in call for call in harness.calls()))

    def test_late_title_or_autofix_hold_is_reclassified_from_fresh_metadata(self):
        cases = (
            {"title": "chore(deps): bump pkg from 1.0.0 to 2.0.0"},
            {
                "title": "chore(deps): bump pkg from 1.0.0 to 1.0.1",
                "labels": [{"name": "autopilot:autofixed"}],
            },
        )
        for changes in cases:
            with self.subTest(changes=changes):
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    views=[trusted_pr(**changes)],
                )
                self.success(harness.run("triage", "triage", MODE="automerge"))
                self.assertEqual(harness.outputs()["outcome"], "review_held")
                self.assertFalse(any(call[:3] == ["gh", "pr", "merge"] for call in harness.calls()))

    def test_non_green_sibling_ci_is_deferred_even_under_automerge(self):
        for conclusion in ("failure", None):
            with self.subTest(conclusion=conclusion):
                runs = {"workflow_runs": [
                    {"workflow_id": 1, "run_number": 1, "conclusion": "success"},
                    {"workflow_id": 2, "run_number": 1, "conclusion": conclusion},
                ]}
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    runs=runs,
                )
                self.success(harness.run("triage", "triage", MODE="automerge"))
                producer = harness.outputs()
                self.assertEqual(producer["outcome"], "ci_unresolved")
                self.success(self.handoff(
                    harness,
                    TRIAGE_OUTCOME=producer["outcome"],
                    TRIAGE_NUMBER=producer["number"],
                    TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
                ))
                self.assertIn("sibling CI is unresolved", harness.summary.read_text())
                self.assertFalse(any("pro-review" in c for c in harness.calls()))
                self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_success_then_unresolved_does_not_block_same_head_failure_autofix(self):
        harness = self.harness(runs={"workflow_runs": [
            {"workflow_id": 1, "run_number": 1, "conclusion": "success"},
            {"workflow_id": 2, "run_number": 1, "conclusion": None},
        ]})
        self.success(harness.run("triage", "triage"))
        producer = harness.outputs()
        self.assertEqual(producer["outcome"], "ci_unresolved")
        self.success(self.handoff(
            harness,
            TRIAGE_OUTCOME=producer["outcome"],
            TRIAGE_NUMBER=producer["number"],
            TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
        ))
        self.assertFalse(any("pro-review" in c for c in harness.calls()))

        self.success(harness.run("autofix", "pre"))
        self.assertEqual(harness.outputs()["outcome"], "admitted")
        self.assertEqual(harness.outputs()["trigger_sha"], OLD_SHA)

    def test_all_green_check_flattens_paginated_run_pages(self):
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run_pages=[
                {"workflow_runs": [{"workflow_id": 1, "run_number": 1, "conclusion": "success"}]},
                {"workflow_runs": [{"workflow_id": 2, "run_number": 1, "conclusion": "failure"}]},
            ],
        )
        self.success(harness.run("triage", "triage", MODE="automerge"))
        producer = harness.outputs()
        self.assertEqual(producer["outcome"], "ci_unresolved")
        self.success(self.handoff(harness, TRIAGE_OUTCOME=producer["outcome"]))
        self.assertIn("sibling CI is unresolved", harness.summary.read_text())
        self.assertFalse(any("pro-review" in c for c in harness.calls()))
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_empty_run_inventory_is_never_treated_as_all_green(self):
        # An inventory that is STILL empty after the bounded currency
        # retries has no observed pending run and no observed repair-set
        # sibling — there is no future event left to complete and
        # re-trigger this workflow, so ci_unresolved (whose handoff path
        # deliberately no-ops, awaiting that event) would strand the PR
        # forever. It must reach a definite owner (review_held) instead.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run_pages=[{"workflow_runs": []}],
        )
        self.success(harness.run(
            "triage", "triage", MODE="automerge", AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        producer = harness.outputs()
        self.assertEqual(producer["outcome"], "review_held")
        self.success(self.handoff(
            harness,
            TRIAGE_OUTCOME=producer["outcome"],
            TRIAGE_NUMBER=producer["number"],
            TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
        ))
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))
        self.assert_queued(harness)

    def test_triage_stops_if_ownership_or_head_changes_before_mutation(self):
        cases = (
            ({"headRefOid": NEW_SHA}, "stale_rejected"),
            ({"labels": [{"name": "claimed"}]}, "existing_owner"),
            ({"labels": [{"name": "skip-pro-review"}]}, "existing_owner"),
            ({"assignees": [{"login": "operator"}]}, "existing_owner"),
            ({"reviewRequests": [{"__typename": "Team", "slug": "maintainers"}]}, "existing_owner"),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                harness = self.harness(views=[trusted_pr(**changes)])
                self.success(harness.run("triage", "triage", MODE="automerge"))
                self.assertEqual(harness.outputs()["outcome"], expected)
                self.assertEqual(self.mutation_calls(harness), [])

    def test_red_producer_to_handoff_uses_real_outcome_blocks(self):
        for codex_rc in (0, 17):
            with self.subTest(codex_rc=codex_rc):
                harness = self.harness(codex_rc=codex_rc)
                self.success(harness.run("autofix", "pre"))
                pre = harness.outputs()
                codex = harness.run("autofix", "codex")
                guard = {}
                if codex.returncode == 0:
                    self.success(harness.run("autofix", "guard"))
                    guard = harness.outputs()
                self.success(harness.run("autofix", "result", PRE_OUTCOME=pre["outcome"],
                                         CODEX_STATUS="failure" if codex.returncode else "success",
                                         GUARD_OUTCOME=guard.get("outcome", ""), PUSH_OUTCOME="",
                                         FLAG_STATUS="skipped", FLAGGED=""))
                outcome = harness.outputs()["outcome"]
                self.success(self.red_handoff(harness, outcome,
                                              AUTOFIX_RESULT="failure" if codex.returncode else "success",
                                              AUTOFIX_NUMBER=pre["number"], AUTOFIX_TRIGGER_SHA=pre["trigger_sha"]))
                self.assert_queued(harness)

    def test_autofix_gate_and_handoff_route_the_same_failure_like_conclusions(self):
        expr = WORKFLOW["jobs"]["autofix"]["if"]
        match = re.search(r"fromJSON\('(\[[^)]*\])'\)", expr)
        self.assertIsNotNone(match, expr)
        gate_conclusions = set(json.loads(match.group(1)))
        self.assertEqual(gate_conclusions, {"failure", "timed_out", "action_required", "startup_failure"})
        for conclusion in sorted(gate_conclusions):
            with self.subTest(conclusion=conclusion):
                harness = self.harness()
                self.success(self.red_handoff(harness, "no_changes", CI_CONCLUSION=conclusion))
                self.assert_queued(harness)

    # Round 3 found skipped/neutral unhandled, round 4 found cancelled
    # unhandled, round 5 found stale unhandled. GitHub can add conclusion
    # values at any time, so an enumerated "known bad, everything else is
    # fine" list can never be complete. These cases are NOT tested one at a
    # time as new gaps are discovered — NON_REPAIR_NON_SUCCESS_CONCLUSIONS
    # includes an invented value alongside the two real ones this fixed, so
    # each test below is already a regression guard for the whole family.
    NON_REPAIR_NON_SUCCESS_CONCLUSIONS = ("cancelled", "stale", "some_future_conclusion")

    def test_unforeseen_conclusion_reaches_a_definite_owner_not_a_silent_no_op(self):
        # The point of this change: a conclusion value that appears nowhere
        # in the workflow source must still reach a definite owner rather
        # than fall through to a silent no-op. This must fail against an
        # open-enumeration implementation.
        conclusion = "some_future_conclusion"
        workflow_text = (ROOT / ".github/workflows/dependency-autopilot.yml").read_text()
        self.assertNotIn(conclusion, workflow_text)

        harness = self.harness(prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")])
        self.success(harness.run(
            "triage", "triage", CI_CONCLUSION=conclusion, MODE="automerge",
            AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        producer = harness.outputs()
        # Not success evidence: forced to review, never merged, even though
        # the title alone would otherwise be a safe single-version bump.
        self.assertEqual(producer["outcome"], "review_held")
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

        self.success(self.handoff(
            harness, CI_CONCLUSION=conclusion,
            TRIAGE_OUTCOME=producer["outcome"], TRIAGE_NUMBER=producer["number"],
            TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
        ))
        self.assert_queued(harness)

    def test_non_success_trigger_never_reaches_green_safe_or_merges(self):
        # Inventory looks fully green (e.g. a superseding rerun already
        # succeeded), but THIS event's own conclusion is not success —
        # never treated as success evidence, so it must still force review.
        for conclusion in self.NON_REPAIR_NON_SUCCESS_CONCLUSIONS:
            with self.subTest(conclusion=conclusion):
                harness = self.harness(prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")])
                self.success(harness.run(
                    "triage", "triage", CI_CONCLUSION=conclusion, MODE="automerge",
                    AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
                ))
                self.assertEqual(harness.outputs()["outcome"], "review_held")
                self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_current_cancelled_trigger_still_forces_review(self):
        # Trigger matches the inventory's max run_number for its workflow_id
        # (current, not superseded): the cancelled/stale override must still
        # fire exactly as before currency checking existed.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run=RUN | {"workflowDatabaseId": 1, "number": 1, "conclusion": "cancelled"},
            runs={"workflow_runs": [{"workflow_id": 1, "run_number": 1, "conclusion": "cancelled"}]},
        )
        self.success(harness.run("triage", "triage", CI_CONCLUSION="cancelled", MODE="automerge"))
        self.assertEqual(harness.outputs()["outcome"], "review_held")
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_superseded_cancelled_trigger_keeps_green_safe(self):
        # A newer run of the same workflow already succeeded at this head;
        # this cancelled trigger is PROVEN superseded, so it must not force
        # review — the inventory's own bad/allgreen already reflects reality.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run=RUN | {"workflowDatabaseId": 1, "number": 1, "conclusion": "cancelled"},
            runs={"workflow_runs": [{"workflow_id": 1, "run_number": 2, "conclusion": "success"}]},
        )
        self.success(harness.run("triage", "triage", CI_CONCLUSION="cancelled", MODE="automerge"))
        outputs = harness.outputs()
        self.assertEqual(outputs["tier"], "safe")
        self.assertEqual(outputs["outcome"], "green_safe")
        self.assertTrue(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_triage_lagging_or_missing_inventory_never_proves_green(self):
        # Merge-safety regression: cancelled #11 with a lagging/missing list
        # entry must fail closed to review, never merge.
        for case_name, entry in lagging_cases("success"):
            with self.subTest(case=case_name):
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    run=RUN | {"workflowDatabaseId": 1, "number": 11, "conclusion": "cancelled"},
                    runs={"workflow_runs": [entry]},
                )
                self.success(harness.run(
                    "triage", "triage", CI_CONCLUSION="cancelled", MODE="automerge",
                    AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
                ))
                self.assertEqual(harness.outputs()["outcome"], "review_held")
                self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_rerun_reusing_the_run_number_with_a_newer_attempt_never_proves_green(self):
        # MERGE-SAFETY regression (round 9, Symptom A): a rerun REUSES the
        # triggering run's run_number while incrementing run_attempt. If
        # attempt 1 emitted the triggering success and attempt 2 has since
        # failed, an eventually-consistent run-list can still expose
        # attempt 1's success under the SAME run_number — round 8's fix
        # (run_number only) cannot see this, and a safe-tier PR must not
        # reach green_safe or merge on a stale attempt's success.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run=RUN | {"workflowDatabaseId": 1, "number": 11, "attempt": 2, "conclusion": "failure"},
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 11, "run_attempt": 1, "conclusion": "success"},
            ]},
        )
        self.success(harness.run(
            "triage", "triage", CI_CONCLUSION="success", MODE="automerge",
            AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        outputs = harness.outputs()
        self.assertNotEqual(outputs["outcome"], "green_safe")
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_matching_run_number_and_attempt_with_a_differing_live_conclusion_is_lagging(self):
        # Same run_number AND same attempt as the live trigger (so neither
        # the run_number check nor the t_concl-vs-CI_CONCLUSION supersession
        # signal fires), but the inventory's recorded conclusion for that
        # exact attempt disagrees with the live read (here: a stale
        # "skipped" where the live run is actually "success"). A second,
        # genuinely green sibling supplies the one-success requirement so
        # only the conclusion mismatch itself is under test — this must
        # still be treated as lagging, not current, and must not prove
        # green or merge.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run=RUN | {"workflowDatabaseId": 1, "number": 11, "attempt": 1, "conclusion": "success"},
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 11, "run_attempt": 1, "conclusion": "skipped"},
                {"workflow_id": 2, "run_number": 1, "conclusion": "success"},
            ]},
        )
        self.success(harness.run(
            "triage", "triage", CI_CONCLUSION="success", MODE="automerge",
            AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        outputs = harness.outputs()
        self.assertNotEqual(outputs["outcome"], "green_safe")
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_fully_matching_inventory_number_attempt_and_conclusion_classifies_as_current(self):
        # The happy path is unchanged: when run_number, run_attempt, and
        # conclusion in the inventory all agree with the live trigger read,
        # currency is proven exactly as before attempt/conclusion binding
        # was added, and a safe-tier PR still reaches green_safe and merges.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run=RUN | {"workflowDatabaseId": 1, "number": 11, "attempt": 2, "conclusion": "success"},
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 11, "run_attempt": 2, "conclusion": "success"},
            ]},
        )
        self.success(harness.run(
            "triage", "triage", CI_CONCLUSION="success", MODE="automerge",
            AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        outputs = harness.outputs()
        self.assertEqual(outputs["outcome"], "green_safe")
        self.assertTrue(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def test_non_success_sibling_reconciles_to_review_held_instead_of_stranding_the_hold(self):
        # A prior event would have held this as ci_unresolved awaiting the
        # sibling; once the sibling's own non-repair terminal conclusion is
        # the ONLY unresolved item (a genuine success already exists,
        # nothing is still pending or still owed a repair attempt),
        # reconciliation must terminate the hold rather than defer to an
        # event that will never arrive.
        for conclusion in self.NON_REPAIR_NON_SUCCESS_CONCLUSIONS:
            with self.subTest(conclusion=conclusion):
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    runs={"workflow_runs": [
                        {"workflow_id": 1, "run_number": 1, "conclusion": "success"},
                        {"workflow_id": 2, "run_number": 1, "conclusion": conclusion},
                    ]},
                )
                self.success(harness.run(
                    "triage", "triage", CI_CONCLUSION=conclusion,
                    AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
                ))
                producer = harness.outputs()
                self.assertEqual(producer["outcome"], "review_held")
                self.success(self.handoff(
                    harness, CI_CONCLUSION=conclusion,
                    TRIAGE_OUTCOME=producer["outcome"], TRIAGE_NUMBER=producer["number"],
                    TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
                ))
                self.assert_queued(harness)

    def test_non_success_sibling_with_a_still_pending_run_stays_deferred(self):
        # A genuinely pending (uncompleted) sibling still owes a real future
        # event, so the hold must remain ci_unresolved, not jump to review.
        for conclusion in self.NON_REPAIR_NON_SUCCESS_CONCLUSIONS:
            with self.subTest(conclusion=conclusion):
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    runs={"workflow_runs": [
                        {"workflow_id": 1, "run_number": 1, "conclusion": "success"},
                        {"workflow_id": 2, "run_number": 1, "conclusion": conclusion},
                        {"workflow_id": 3, "run_number": 1, "conclusion": None},
                    ]},
                )
                self.success(harness.run(
                    "triage", "triage", CI_CONCLUSION=conclusion,
                    AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
                ))
                self.assertEqual(harness.outputs()["outcome"], "ci_unresolved")

    def test_failure_like_sibling_still_defers_to_its_own_repair_attempt(self):
        # A sibling with a repair-set conclusion (failure/timed_out/etc.) is
        # terminal too, but unlike cancelled/stale/unknown it owns a
        # SEPARATE event that routes to autofix, a genuine repair attempt.
        # Handing off to review now would race that attempt and could starve
        # it (admission treats an existing pro-review label as another
        # owner). This must stay ci_unresolved, not review_held.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 1, "conclusion": "success"},
                {"workflow_id": 2, "run_number": 1, "conclusion": "failure"},
            ]},
        )
        self.success(harness.run("triage", "triage"))
        self.assertEqual(harness.outputs()["outcome"], "ci_unresolved")

    def test_directly_observed_newer_failed_attempt_stays_ci_unresolved(self):
        # Round-10 P1 (FINDING B): this invocation was triggered by a
        # success completion of attempt 1, but by the time it runs, a
        # rerun's attempt 2 has already concluded 'failure' — this job's own
        # direct `gh run view` re-read proves it. The run-list inventory has
        # not caught up (still shows attempt 1's success under the same
        # run_number), so trigger_lagging correctly blocks green. But that
        # directly observed failure IS actually observed repair-set work: a
        # real future event (that failed attempt's own serialized callback)
        # is queued behind this invocation and will call autofix. Routing
        # this to review_held instead of ci_unresolved would let handoff
        # apply pro-review, and admission treats an existing pro-review
        # label as another owner — starving the queued repair attempt and
        # breaking repair-before-review ordering.
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run=RUN | {"workflowDatabaseId": 1, "number": 11, "attempt": 2, "conclusion": "failure"},
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 11, "run_attempt": 1, "conclusion": "success"},
            ]},
        )
        self.success(harness.run(
            "triage", "triage", CI_CONCLUSION="success", AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        outputs = harness.outputs()
        self.assertEqual(outputs["outcome"], "ci_unresolved")
        self.assertNotEqual(outputs["outcome"], "review_held")
        self.success(self.handoff(
            harness, CI_CONCLUSION="success",
            TRIAGE_OUTCOME=outputs["outcome"], TRIAGE_NUMBER=outputs["number"],
            TRIAGE_TRIGGER_SHA=outputs["trigger_sha"],
        ))
        self.assertFalse(any(c[:3] == ["gh", "pr", "edit"] and "pro-review" in c for c in harness.calls()))

    def test_forced_termination_with_bound_metadata_falls_back_to_failed_execution(self):
        harness = self.harness()
        self.success(self.red_handoff(harness, "", AUTOFIX_RESULT="failure",
                                      AUTOFIX_NUMBER="21", AUTOFIX_TRIGGER_SHA=OLD_SHA))
        self.assert_queued(harness)
        self.assertIn("failed_execution", harness.summary.read_text())

    def test_forced_termination_without_bound_metadata_still_fails_closed(self):
        for missing in ("AUTOFIX_NUMBER", "AUTOFIX_TRIGGER_SHA"):
            with self.subTest(missing=missing):
                harness = self.harness()
                bound = {"AUTOFIX_NUMBER": "21", "AUTOFIX_TRIGGER_SHA": OLD_SHA}
                bound[missing] = ""
                result = self.red_handoff(harness, "", AUTOFIX_RESULT="failure", **bound)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.mutation_calls(harness), [])

    def test_cancelled_producer_is_not_inferred_as_failure(self):
        harness = self.harness()
        result = self.red_handoff(harness, "", AUTOFIX_RESULT="cancelled",
                                  AUTOFIX_NUMBER="21", AUTOFIX_TRIGGER_SHA=OLD_SHA)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutation_calls(harness), [])

    def test_handoff_rejects_wrong_run_event_sha_or_missing_url(self):
        for run in (RUN | {"event": "workflow_dispatch"}, RUN | {"headSha": NEW_SHA}, RUN | {"url": ""}):
            with self.subTest(run=run):
                harness = self.harness(run=run)
                result = self.handoff(harness)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.mutation_calls(harness), [])

    def test_successful_repair_is_held_before_push_and_does_not_hand_off_itself(self):
        # The push uses a short-lived token and genuinely re-triggers CI on
        # the new head; the repaired head's OWN CI completion selects the
        # next owner, not this producer's terminal handoff.
        harness = self.harness(changed_files=["src/app.ts"], remote_sha=OLD_SHA, pushed_sha=NEW_SHA)
        self.success(harness.run("autofix", "pre"))
        pre = harness.outputs()
        self.success(harness.run("autofix", "codex"))
        self.success(harness.run("autofix", "guard"))
        guard = harness.outputs()
        self.success(harness.run("autofix", "flag", NUM=pre["number"], TRIGGER_SHA=pre["trigger_sha"]))
        flag = harness.outputs()
        self.success(harness.run("autofix", "push", TRIGGER_SHA=pre["trigger_sha"], PUSH_TOKEN="test-push-token"))
        push = harness.outputs()
        self.success(harness.run("autofix", "result", PRE_OUTCOME=pre["outcome"], CODEX_STATUS="success",
                                 GUARD_OUTCOME=guard["outcome"], PUSH_OUTCOME=push["outcome"],
                                 FLAG_STATUS="success", FLAGGED=flag["flagged"]))
        outcome = harness.outputs()["outcome"]
        self.assertEqual(outcome, "successful_push")
        calls = harness.calls()
        held = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "pr", "edit"] and "autopilot:autofixed" in c)
        pushed = next(i for i, c in enumerate(calls) if c[:2] == ["git", "push"])
        self.assertLess(held, pushed)
        self.assertFalse(any(c[0] == "git" and "-A" in c for c in calls))

        before = self.mutation_calls(harness)
        self.success(self.red_handoff(harness, outcome, AUTOFIX_PUSHED_SHA=push["pushed_sha"]))
        self.assertEqual(self.mutation_calls(harness), before)
        self.assertNotIn("Handoff: queued", harness.summary.read_text())
        self.assertIn("own CI completion selects the next owner", harness.summary.read_text())

    def test_stale_remote_head_rejects_the_push_without_mutating_git(self):
        harness = self.harness(changed_files=["src/app.ts"], remote_sha=NEW_SHA, pushed_sha=NEW_SHA)
        result = harness.run("autofix", "push", TRIGGER_SHA=OLD_SHA, PUSH_TOKEN="test-push-token")
        self.success(result)
        self.assertEqual(harness.outputs()["outcome"], "stale_rejected")
        self.assertFalse(any(c[0] == "git" and c[1] in ("add", "commit", "push") for c in harness.calls()))

    def test_stale_or_owned_autofix_outcomes_skip_handoff_without_mutation(self):
        for outcome in ("stale_rejected", "existing_owner", "successful_push"):
            with self.subTest(outcome=outcome):
                harness = self.harness()
                self.success(self.red_handoff(harness, outcome))
                self.assertEqual(self.mutation_calls(harness), [])
                self.assertNotIn("Handoff: queued", harness.summary.read_text())

    def test_second_terminal_refresh_catches_a_change_after_the_audit_comment(self):
        harness = self.harness(views=[trusted_pr(), trusted_pr(headRefOid=NEW_SHA)])
        self.success(self.handoff(harness))
        self.assertTrue(any(c[:3] == ["gh", "pr", "comment"] for c in harness.calls()))
        self.assertFalse(any(c[:3] == ["gh", "pr", "edit"] and "pro-review" in c for c in harness.calls()))
        self.assertNotIn("Handoff: queued", harness.summary.read_text())

    def test_guard_rejects_real_renames_from_and_into_protected_paths(self):
        protected_targets = ("package.json", "pnpm-lock.yaml", ".github/workflows/ci.yml")
        for protected in protected_targets:
            with self.subTest(protected=protected, direction="from"):
                fixture = self.real_git_harness({
                    protected: "protected contents\n",
                    "src/app.ts": "export const value = 1;\n",
                })
                fixture.rename(protected, f"src/renamed-{protected.replace('/', '-')}")
                result = fixture.run("autofix", "guard")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(fixture.outputs().get("outcome"), "failed_execution")
            with self.subTest(protected=protected, direction="into"):
                fixture = self.real_git_harness({
                    "src/app.ts": "export const value = 1;\n",
                    "notes/todo.ts": "export const todo = 1;\n",
                })
                fixture.rename("notes/todo.ts", protected)
                result = fixture.run("autofix", "guard")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(fixture.outputs().get("outcome"), "failed_execution")

    def test_real_pathspec_staging_commits_tracked_staged_and_untracked_allowed_files(self):
        fixture = self.real_git_harness({
            "package.json": '{"name": "example"}\n',
            "src/app.ts": "export const value = 1;\n",
            "src/existing.ts": "export const existing = 1;\n",
        })
        (fixture.repo / "src/app.ts").write_text("export const value = 2;\n")
        (fixture.repo / "src/existing.ts").write_text("export const existing = 2;\n")
        fixture.git("add", "src/existing.ts")
        (fixture.repo / "src/new.ts").write_text("export const created = 1;\n")

        guard = fixture.run("autofix", "guard")
        self.assertEqual(guard.returncode, 0, guard.stdout + guard.stderr)
        self.assertEqual(fixture.outputs().get("outcome"), "changes_ready")

        push = fixture.run("autofix", "push")
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertEqual(fixture.outputs().get("outcome"), "successful_push")
        committed = fixture.git("show", "--stat", "--format=", "HEAD").stdout
        self.assertIn("src/app.ts", committed)
        self.assertIn("src/existing.ts", committed)
        self.assertIn("src/new.ts", committed)
        self.assertNotIn("package.json", committed)

    def test_publish_hold_rechecks_ownership_and_fails_on_label_error(self):
        for changes in ({"headRefOid": NEW_SHA}, {"labels": [{"name": "pro-review"}]},
                        {"assignees": [{"login": "operator"}]},
                        {"baseRefName": "develop"}, {"headRefName": "changed"}):
            harness = self.harness(views=[trusted_pr(**changes)])
            self.success(harness.run("autofix", "flag", NUM="21", TRIGGER_SHA=OLD_SHA))
            self.assertNotIn("flagged", harness.outputs())
            self.assertEqual(self.mutation_calls(harness), [])
        harness = self.harness(fail_commands=["gh pr edit"])
        self.assertNotEqual(harness.run("autofix", "flag", NUM="21", TRIGGER_SHA=OLD_SHA).returncode, 0)
        self.assertNotIn("flagged", harness.outputs())

    def test_flag_rejects_a_retarget_away_from_main_during_repair(self):
        harness = self.harness(views=[trusted_pr(baseRefName="develop")])
        result = harness.run("autofix", "flag", NUM="21", TRIGGER_SHA=OLD_SHA)
        self.success(result)
        self.assertEqual(harness.outputs().get("outcome"), "stale_rejected")
        self.assertNotIn("flagged", harness.outputs())
        self.assertEqual(self.mutation_calls(harness), [])

    def test_red_admission_distinguishes_unavailable_credentials(self):
        harness = self.harness()
        self.success(harness.run("autofix", "pre", CODEX_AUTH=""))
        self.assertEqual(harness.outputs()["outcome"], "unavailable_credentials")
        self.assertEqual(harness.outputs()["trigger_sha"], OLD_SHA)
        self.assertEqual(self.mutation_calls(harness), [])

    def test_a_prior_autofix_label_refuses_a_second_repair_attempt(self):
        harness = self.harness(prs=[trusted_pr(labels=[{"name": "autopilot:autofixed"}])])
        self.success(harness.run("autofix", "pre"))
        self.assertEqual(harness.outputs()["outcome"], "already_repaired")
        self.assertEqual(harness.outputs()["ok"], "0")
        self.assertEqual(self.mutation_calls(harness), [])
        self.assertFalse(any(c[0] == "codex" for c in harness.calls()))

    def test_superseded_failure_trigger_does_not_repair(self):
        # A newer run of the same workflow already superseded this failure
        # at the same head. Repair must not start: no Codex invocation, and
        # the once-only repair attempt is not consumed.
        harness = self.harness(
            run=RUN | {"workflowDatabaseId": 1, "number": 1, "conclusion": "failure"},
            runs={"workflow_runs": [{"workflow_id": 1, "run_number": 2, "conclusion": "success"}]},
        )
        self.success(harness.run("autofix", "pre", CI_CONCLUSION="failure"))
        self.assertEqual(harness.outputs()["outcome"], "stale_rejected")
        self.assertEqual(harness.outputs()["ok"], "0")
        self.assertEqual(self.mutation_calls(harness), [])
        self.assertFalse(any(c[0] == "codex" for c in harness.calls()))

    def test_current_failure_trigger_still_repairs(self):
        # Trigger matches the inventory's max run_number for its workflow_id
        # (current, not superseded): admission must still succeed exactly
        # as before currency checking existed.
        harness = self.harness(
            run=RUN | {"workflowDatabaseId": 1, "number": 3, "conclusion": "failure"},
            runs={"workflow_runs": [{"workflow_id": 1, "run_number": 3, "conclusion": "failure"}]},
        )
        self.success(harness.run("autofix", "pre", CI_CONCLUSION="failure"))
        self.assertEqual(harness.outputs()["outcome"], "admitted")
        self.assertEqual(harness.outputs()["ok"], "1")

    def test_autofix_same_run_number_newer_attempt_does_not_repair(self):
        # Round-10 P1 (FINDING A): a rerun reuses the triggering run's
        # run_number while incrementing run_attempt. The live read is now
        # attempt 2, but the run-list inventory still shows attempt 1's
        # conclusion under the SAME run_number — round 9's run_number-only
        # check (autofix's copy) cannot see this and would wrongly call it
        # current, consuming the once-only repair attempt against a head
        # whose currency is unproven. Must stay indeterminate: no Codex, no
        # repair attempt consumed.
        harness = self.harness(
            run=RUN | {"workflowDatabaseId": 1, "number": 11, "attempt": 2, "conclusion": "failure"},
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 11, "run_attempt": 1, "conclusion": "success"},
            ]},
        )
        self.success(harness.run(
            "autofix", "pre", CI_CONCLUSION="failure", AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        self.assertEqual(harness.outputs()["outcome"], "currency_indeterminate")
        self.assertEqual(harness.outputs()["ok"], "0")
        self.assertEqual(self.mutation_calls(harness), [])
        self.assertFalse(any(c[0] == "codex" for c in harness.calls()))

    def test_autofix_fully_matching_number_attempt_and_conclusion_still_repairs(self):
        # The happy path is unchanged: when run_number, run_attempt, and
        # conclusion in the inventory all agree with the live trigger read
        # (here at a non-default attempt, so the fix cannot be satisfied by
        # the fake CLI's attempt-1 default alone), currency is proven and
        # admission still succeeds exactly as before attempt/conclusion
        # binding was added to autofix.
        harness = self.harness(
            run=RUN | {"workflowDatabaseId": 1, "number": 11, "attempt": 2, "conclusion": "failure"},
            runs={"workflow_runs": [
                {"workflow_id": 1, "run_number": 11, "run_attempt": 2, "conclusion": "failure"},
            ]},
        )
        self.success(harness.run(
            "autofix", "pre", CI_CONCLUSION="failure", AUTOPILOT_CURRENCY_RETRY_SECONDS="0",
        ))
        self.assertEqual(harness.outputs()["outcome"], "admitted")
        self.assertEqual(harness.outputs()["ok"], "1")

    def test_autofix_lagging_or_missing_inventory_does_not_repair(self):
        # Retries exhaust without proving current/superseded: Codex must not
        # run, but the PR still reaches handoff, not the skipped `stale_rejected`.
        for case_name, entry in lagging_cases("success"):
            with self.subTest(case=case_name):
                harness = self.harness(
                    run=RUN | {"workflowDatabaseId": 1, "number": 11, "conclusion": "failure"},
                    runs={"workflow_runs": [entry]},
                )
                self.success(harness.run("autofix", "pre", CI_CONCLUSION="failure", AUTOPILOT_CURRENCY_RETRY_SECONDS="0"))
                self.assertEqual(harness.outputs()["outcome"], "currency_indeterminate")
                self.assertEqual(harness.outputs()["ok"], "0")
                self.assertEqual(self.mutation_calls(harness), [])
                self.assertFalse(any(c[0] == "codex" for c in harness.calls()))

    def test_two_consecutive_red_completions_produce_exactly_one_repair_then_a_review_owner(self):
        harness = self.harness(changed_files=["src/app.ts"], remote_sha=OLD_SHA, pushed_sha=NEW_SHA)

        # First red completion: no prior auto-fix exists, so repair is
        # admitted, runs, and pushes a fix (re-triggering CI on the new head).
        self.success(harness.run("autofix", "pre"))
        pre = harness.outputs()
        self.assertEqual(pre["outcome"], "admitted")
        self.success(harness.run("autofix", "codex"))
        self.success(harness.run("autofix", "guard"))
        guard = harness.outputs()
        self.success(harness.run("autofix", "flag", NUM=pre["number"], TRIGGER_SHA=pre["trigger_sha"]))
        flag = harness.outputs()
        self.success(harness.run("autofix", "push", TRIGGER_SHA=pre["trigger_sha"], PUSH_TOKEN="test-push-token"))
        push = harness.outputs()
        self.success(harness.run("autofix", "result", PRE_OUTCOME=pre["outcome"], CODEX_STATUS="success",
                                 GUARD_OUTCOME=guard["outcome"], PUSH_OUTCOME=push["outcome"],
                                 FLAG_STATUS="success", FLAGGED=flag["flagged"]))
        self.assertEqual(harness.outputs()["outcome"], "successful_push")
        self.assertEqual(sum(c[0] == "codex" for c in harness.calls()), 1)

        # Second red completion on the repaired (now autopilot:autofixed) head:
        # admission must refuse a second repair outright — no second Codex run.
        self.success(harness.run("autofix", "pre"))
        second_pre = harness.outputs()
        self.assertEqual(second_pre["outcome"], "already_repaired")
        self.assertEqual(sum(c[0] == "codex" for c in harness.calls()), 1)

        self.success(harness.run("autofix", "result", PRE_OUTCOME="already_repaired", CODEX_STATUS="",
                                 GUARD_OUTCOME="", PUSH_OUTCOME="", FLAG_STATUS="", FLAGGED=""))
        self.assertEqual(harness.outputs()["outcome"], "already_repaired")

        # The still-red, already-repaired PR reconciles to a review owner.
        # The second completion's own triggering run is on the repaired head.
        config = json.loads(harness.config.read_text())
        config["run"] = RUN | {"headSha": second_pre["trigger_sha"]}
        harness.config.write_text(json.dumps(config))
        self.success(self.red_handoff(harness, "already_repaired", AUTOFIX_NUMBER=second_pre["number"],
                                      AUTOFIX_TRIGGER_SHA=second_pre["trigger_sha"]))
        self.assert_queued(harness)

    def test_all_trusted_bot_login_forms_are_admitted(self):
        for login in ("app/dependabot", "dependabot[bot]", "app/startupbros-autopilot", "startupbros-autopilot[bot]"):
            with self.subTest(login=login):
                harness = self.harness(prs=[trusted_pr(author={"login": login, "is_bot": True})])
                self.success(harness.run("autofix", "pre"))
                self.assertEqual(harness.outputs()["ok"], "1")

    def test_pending_user_or_team_review_blocks_both_producers(self):
        requests = (
            [{"__typename": "User", "login": "operator"}],
            [{"__typename": "Team", "name": "Maintainers", "slug": "maintainers"}],
        )
        for job, step_id in (("autofix", "pre"), ("triage", "triage")):
            for review_requests in requests:
                with self.subTest(job=job, review_requests=review_requests):
                    harness = self.harness(prs=[trusted_pr(reviewRequests=review_requests)])
                    self.success(harness.run(job, step_id))
                    self.assertEqual(harness.outputs()["outcome"], "existing_owner")
                    self.assertEqual(self.mutation_calls(harness), [])

    def test_pending_user_or_team_review_blocks_pre_push_flag(self):
        requests = (
            [{"__typename": "User", "login": "operator"}],
            [{"__typename": "Team", "name": "Maintainers", "slug": "maintainers"}],
        )
        for review_requests in requests:
            with self.subTest(review_requests=review_requests):
                harness = self.harness(views=[trusted_pr(reviewRequests=review_requests)])
                self.success(harness.run("autofix", "flag", NUM="21", TRIGGER_SHA=OLD_SHA))
                self.assertEqual(harness.outputs()["outcome"], "existing_owner")
                self.assertNotIn("flagged", harness.outputs())
                self.assertEqual(self.mutation_calls(harness), [])

    def test_pending_user_or_team_review_blocks_terminal_refreshes(self):
        requests = (
            [{"__typename": "User", "login": "operator"}],
            [{"__typename": "Team", "name": "Maintainers", "slug": "maintainers"}],
        )
        for review_requests in requests:
            with self.subTest(review_requests=review_requests, refresh="first"):
                harness = self.harness(views=[trusted_pr(reviewRequests=review_requests)])
                self.success(self.handoff(harness))
                self.assertEqual(self.mutation_calls(harness), [])
                self.assertNotIn("Handoff: queued", harness.summary.read_text())
            with self.subTest(review_requests=review_requests, refresh="final"):
                harness = self.harness(views=[
                    trusted_pr(),
                    trusted_pr(reviewRequests=review_requests),
                ])
                self.success(self.handoff(harness))
                self.assertTrue(any(c[:3] == ["gh", "pr", "comment"] for c in harness.calls()))
                self.assertFalse(any(
                    c[:3] == ["gh", "pr", "edit"] and "pro-review" in c
                    for c in harness.calls()
                ))
                self.assertNotIn("Handoff: queued", harness.summary.read_text())

    def test_both_producers_reject_untrusted_and_owned_targets(self):
        cases = [
            {"author": {"login": "startupbros-autopilot", "is_bot": False}},
            {"author": {"login": "app/other", "is_bot": True}},
            {"isCrossRepository": True}, {"baseRefName": "develop"},
            {"headRefName": "changed"}, {"isDraft": True},
            {"assignees": [{"login": "operator"}]},
        ] + [{"labels": [{"name": name}]} for name in ("claimed", "loop-run", "pro-review", "skip-pro-review")]
        for job, step_id in (("autofix", "pre"), ("triage", "triage")):
            for changes in cases:
                with self.subTest(job=job, changes=changes):
                    harness = self.harness(prs=[trusted_pr(**changes)])
                    self.success(harness.run(job, step_id))
                    self.assertIn(harness.outputs()["outcome"], ("stale_rejected", "existing_owner"))
                    self.assertEqual(self.mutation_calls(harness), [])

    def test_producers_fail_closed_on_unavailable_provenance(self):
        for job, step_id in (("autofix", "pre"), ("triage", "triage")):
            with self.subTest(job=job):
                harness = self.harness(fail_commands=["gh run view"])
                self.assertNotEqual(harness.run(job, step_id).returncode, 0)
                self.assertEqual(harness.outputs()["outcome"], "failed_execution")
                self.assertEqual(self.mutation_calls(harness), [])

    def test_missing_ambiguous_or_stale_targets_are_not_admitted(self):
        for config in ({"prs": []}, {"prs": [trusted_pr(), trusted_pr()]},
                       {"run": RUN | {"headSha": NEW_SHA}},
                       {"run": RUN | {"event": "workflow_dispatch"}}, {"run": {}}):
            with self.subTest(config=config):
                harness = self.harness(**config)
                self.success(harness.run("autofix", "pre"))
                self.assertEqual(harness.outputs()["outcome"], "stale_rejected")
                self.assertEqual(self.mutation_calls(harness), [])

    def test_codex_failure_is_nonzero_and_fetches_logs_only_once(self):
        harness = self.harness(codex_rc=17)
        result = harness.run("autofix", "codex")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(harness.outputs()["rc"], "17")
        self.assertEqual(harness.outputs()["outcome"], "failed_execution")
        self.assertEqual(sum("--log-failed" in c for c in harness.calls()), 1)
        self.assertFalse(any(c[:2] == ["git", "push"] for c in harness.calls()))

    def test_failed_or_empty_log_fetch_does_not_run_codex(self):
        for config in ({"fail_commands": ["gh run view"]}, {"failed_log": ""}):
            harness = self.harness(**config)
            self.assertNotEqual(harness.run("autofix", "codex").returncode, 0)
            self.assertFalse(any(c[0] == "codex" for c in harness.calls()))

    def test_guard_covers_tracked_staged_and_untracked_manifests(self):
        for name in ("package.json", "pnpm-workspace.yaml", "apps/a/pnpm-lock.yaml", ".github/workflows/a.yml"):
            for state in ("changed_files", "staged_files", "untracked_files"):
                with self.subTest(name=name, state=state):
                    harness = self.harness(**{state: [name]})
                    self.assertNotEqual(harness.run("autofix", "guard").returncode, 0)
                    self.assertEqual(harness.outputs()["outcome"], "failed_execution")

    def test_clean_no_changes_and_application_changes_remain_distinct(self):
        for files, expected in (([], "no_changes"), (["src/app.ts"], "changes_ready")):
            harness = self.harness(changed_files=files)
            self.success(harness.run("autofix", "guard"))
            self.assertEqual(harness.outputs()["outcome"], expected)

    def test_green_triage_handoff_chain_uses_actual_outputs(self):
        harness = self.harness()
        self.success(harness.run("triage", "triage"))
        producer = harness.outputs()
        self.success(self.handoff(harness, TRIAGE_OUTCOME=producer["outcome"],
                                  TRIAGE_NUMBER=producer["number"], TRIAGE_TRIGGER_SHA=producer["trigger_sha"]))
        self.assert_queued(harness)

    def test_safe_green_remains_in_its_existing_queue(self):
        harness = self.harness(prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")])
        self.success(harness.run("triage", "triage"))
        producer = harness.outputs()
        self.assertEqual(producer["outcome"], "green_safe")
        self.success(self.handoff(harness, TRIAGE_OUTCOME=producer["outcome"]))
        self.assertFalse(any("pro-review" in c for c in harness.calls()))

    def test_green_api_failure_is_never_treated_as_green(self):
        harness = self.harness(fail_commands=["gh api"])
        self.assertNotEqual(harness.run("triage", "triage").returncode, 0)
        self.assertEqual(self.mutation_calls(harness), [])

    def test_red_unresolved_outcomes_queue_after_independent_revalidation(self):
        for outcome, status in (("no_changes", "success"), ("unavailable_credentials", "success"),
                                ("failed_execution", "failure"), ("already_repaired", "success"),
                                ("currency_indeterminate", "success")):
            with self.subTest(outcome=outcome):
                harness = self.harness()
                self.success(self.red_handoff(harness, outcome, AUTOFIX_RESULT=status))
                self.assert_queued(harness)
                self.assertIn(outcome, harness.summary.read_text())

    def test_failed_execution_cannot_hand_off_without_bound_producer_metadata(self):
        harness = self.harness()
        self.assertNotEqual(self.red_handoff(harness, "failed_execution", AUTOFIX_RESULT="failure",
                                            AUTOFIX_TRIGGER_SHA="").returncode, 0)
        self.assertEqual(self.mutation_calls(harness), [])

    def test_successful_push_never_hands_off_regardless_of_current_head(self):
        harness = self.harness(prs=[trusted_pr(headRefOid=NEW_SHA)])
        result = self.red_handoff(harness, "successful_push", AUTOFIX_PUSHED_SHA=NEW_SHA)
        self.success(result)
        self.assertEqual(self.mutation_calls(harness), [])
        self.assertNotIn("Handoff: queued", harness.summary.read_text())

    def test_result_step_distinguishes_execution_and_no_change(self):
        for codex_status, guard_outcome, expected in (
            ("failure", "", "failed_execution"), ("skipped", "", "failed_execution"),
            ("success", "no_changes", "no_changes"),
        ):
            harness = self.harness()
            self.success(harness.run("autofix", "result", PRE_OUTCOME="admitted", CODEX_STATUS=codex_status,
                                     GUARD_OUTCOME=guard_outcome, PUSH_OUTCOME="", FLAG_STATUS="", FLAGGED=""))
            self.assertEqual(harness.outputs()["outcome"], expected)

    def test_terminal_rejects_stale_untrusted_closed_or_owned_prs(self):
        cases = [
            {"author": {"login": "app/other", "is_bot": True}},
            {"author": {"login": "dependabot", "is_bot": False}},
            {"isCrossRepository": True}, {"baseRefName": "develop"},
            {"state": "CLOSED"}, {"headRefOid": NEW_SHA}, {"isDraft": True},
            {"assignees": [{"login": "operator"}]},
        ] + [{"labels": [{"name": name}]} for name in ("pro-review", "skip-pro-review", "claimed", "loop-run")]
        for changes in cases:
            with self.subTest(changes=changes):
                harness = self.harness(prs=[trusted_pr(**changes)])
                self.success(self.handoff(harness))
                self.assertEqual(self.mutation_calls(harness), [])

    def test_final_refresh_rechecks_head_and_ownership_before_mutation(self):
        for changes in ({"headRefOid": NEW_SHA}, {"labels": [{"name": "claimed"}]},
                        {"assignees": [{"login": "operator"}]}, {"isDraft": True}):
            with self.subTest(changes=changes):
                harness = self.harness(views=[trusted_pr(**changes)])
                self.success(self.handoff(harness))
                self.assertEqual(self.mutation_calls(harness), [])

    def test_duplicate_events_do_not_queue_or_comment_twice(self):
        harness = self.harness()
        self.success(self.handoff(harness))
        self.success(self.handoff(harness))
        self.assertEqual(sum(c[:3] == ["gh", "pr", "edit"] for c in harness.calls()), 1)
        self.assertEqual(sum(c[:3] == ["gh", "pr", "comment"] for c in harness.calls()), 1)

    def test_trusted_actions_comment_on_earlier_page_is_not_duplicated(self):
        marker = f"<!-- dependency-autopilot-handoff:{OLD_SHA} -->"
        trusted = {
            "body": marker,
            "user": {"type": "Bot", "login": "github-actions[bot]"},
        }
        harness = self.harness(comment_pages=[[trusted], [{"body": "unrelated"}]])
        self.success(self.handoff(harness))
        self.assertFalse(any(c[:3] == ["gh", "pr", "comment"] for c in harness.calls()))
        self.assert_queued(harness)

    def assert_spoofed_marker_is_replaced(self, author):
        marker = f"<!-- dependency-autopilot-handoff:{OLD_SHA} -->"
        harness = self.harness(comment_pages=[[{"body": marker, "user": author}]])
        self.success(self.handoff(harness))
        comments = [c for c in harness.calls() if c[:3] == ["gh", "pr", "comment"]]
        self.assertEqual(len(comments), 1, harness.calls())
        self.assertIn(marker, comments[0][-1])
        self.assert_queued(harness)

    def test_human_spoofed_marker_does_not_suppress_trusted_audit_comment(self):
        self.assert_spoofed_marker_is_replaced({"type": "User", "login": "operator"})

    def test_wrong_bot_spoofed_marker_does_not_suppress_trusted_audit_comment(self):
        self.assert_spoofed_marker_is_replaced({"type": "Bot", "login": "other-app[bot]"})

    def test_label_metadata_is_not_force_overwritten(self):
        harness = self.harness()
        self.success(self.handoff(harness))
        self.assertFalse(any(c[:3] == ["gh", "label", "create"] and "--force" in c for c in harness.calls()))

    def test_terminal_api_label_and_comment_failures_are_visible(self):
        for failed in ("gh run view", "gh pr list", "gh pr view", "gh pr edit", "gh pr comment"):
            with self.subTest(failed=failed):
                harness = self.harness(fail_commands=[failed])
                self.assertNotEqual(self.handoff(harness).returncode, 0)
                self.assertNotIn("Handoff: queued", harness.summary.read_text())
                self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))


if __name__ == "__main__":
    unittest.main()
