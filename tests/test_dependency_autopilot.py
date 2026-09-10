#!/usr/bin/env python3
"""Execute the reusable workflow's real shell blocks with fake external CLIs."""
import json
import re
import subprocess
import unittest

from autopilot_harness import (
    NEW_SHA,
    OLD_SHA,
    ROOT,
    RUN,
    WORKFLOW,
    RealGitHarness,
    ShellHarness,
    step,
    trusted_pr,
)


class WorkflowTests(unittest.TestCase):
    maxDiff = None

    def harness(self, **config):
        harness = ShellHarness({"prs": [trusted_pr()], "run": RUN} | config)
        self.addCleanup(harness.temp.cleanup)
        return harness

    def real_git_harness(self, files):
        harness = RealGitHarness(files)
        self.addCleanup(harness.cleanup)
        return harness

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def handoff(self, harness, **changes):
        env = {
            "CI_CONCLUSION": "success", "TRIAGE_RESULT": "success",
            "TRIAGE_OUTCOME": "review_held", "TRIAGE_NUMBER": "21",
            "TRIAGE_TRIGGER_SHA": OLD_SHA, "AUTOFIX_RESULT": "skipped",
            "AUTOFIX_OUTCOME": "", "AUTOFIX_NUMBER": "",
            "AUTOFIX_TRIGGER_SHA": "", "AUTOFIX_PUSHED_SHA": "",
        } | changes
        return harness.run("handoff", "handoff", **env)

    def red_handoff(self, harness, outcome="no_changes", **changes):
        return self.handoff(harness, **({
            "CI_CONCLUSION": "failure", "TRIAGE_RESULT": "skipped",
            "TRIAGE_OUTCOME": "", "AUTOFIX_RESULT": "success",
            "AUTOFIX_OUTCOME": outcome, "AUTOFIX_NUMBER": "21",
            "AUTOFIX_TRIGGER_SHA": OLD_SHA,
        } | changes))

    def mutation_calls(self, harness):
        return [c for c in harness.calls() if c[:3] in (
            ["gh", "pr", "edit"], ["gh", "pr", "comment"],
            ["gh", "pr", "merge"], ["gh", "label", "create"],
        ) or c[:2] == ["git", "push"]]

    def assert_queued(self, harness):
        edits = [c for c in harness.calls() if c[:3] == ["gh", "pr", "edit"] and "pro-review" in c]
        self.assertEqual(len(edits), 1, harness.calls())
        self.assertIn("Handoff: queued", harness.summary.read_text())
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

    def assert_root_concurrency(self, workflow):
        self.assertIn("github.repository", workflow["concurrency"]["group"])
        self.assertIn("inputs.pr_branch", workflow["concurrency"]["group"])
        self.assertEqual(workflow["concurrency"]["queue"], "max")
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        for job_name, candidate in workflow["jobs"].items():
            self.assertNotIn("concurrency", candidate, job_name)

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

    def test_success_skipped_and_neutral_share_the_reconciliation_path(self):
        gate = WORKFLOW["jobs"]["triage"]["if"]
        self.assertIn("success", gate)
        self.assertIn("skipped", gate)
        self.assertIn("neutral", gate)
        handoff_script = step("handoff", "handoff")["run"]
        self.assertIn('"success"|"skipped"|"neutral"', handoff_script)

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

    def test_only_skipped_or_neutral_inventory_is_not_green(self):
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                harness = self.harness(
                    prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
                    runs={"workflow_runs": [
                        {"workflow_id": 1, "run_number": 1, "conclusion": conclusion},
                    ]},
                )
                self.success(harness.run("triage", "triage", MODE="automerge"))
                producer = harness.outputs()
                self.assertEqual(producer["outcome"], "ci_unresolved")
                self.success(self.handoff(
                    harness,
                    CI_CONCLUSION=conclusion,
                    TRIAGE_OUTCOME=producer["outcome"],
                    TRIAGE_NUMBER=producer["number"],
                    TRIAGE_TRIGGER_SHA=producer["trigger_sha"],
                ))
                self.assertFalse(any(call[:3] == ["gh", "pr", "merge"] for call in harness.calls()))
                self.assertFalse(any("pro-review" in call for call in harness.calls()))

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
        harness = self.harness(
            prs=[trusted_pr(title="chore(deps): bump pkg from 1.0.0 to 1.0.1")],
            run_pages=[{"workflow_runs": []}],
        )
        self.success(harness.run("triage", "triage", MODE="automerge"))
        producer = harness.outputs()
        self.assertEqual(producer["outcome"], "ci_unresolved")
        self.success(self.handoff(harness, TRIAGE_OUTCOME=producer["outcome"]))
        self.assertIn("sibling CI is unresolved", harness.summary.read_text())
        self.assertFalse(any("pro-review" in c for c in harness.calls()))
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in harness.calls()))

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

    def test_cancelled_and_unknown_ci_conclusions_have_no_handoff_path(self):
        for conclusion in ("cancelled", "an_unforeseen_future_conclusion"):
            with self.subTest(conclusion=conclusion):
                harness = self.harness()
                result = self.handoff(harness, CI_CONCLUSION=conclusion,
                                      TRIAGE_RESULT="skipped", TRIAGE_OUTCOME="",
                                      TRIAGE_NUMBER="", TRIAGE_TRIGGER_SHA="",
                                      AUTOFIX_RESULT="skipped", AUTOFIX_OUTCOME="",
                                      AUTOFIX_NUMBER="", AUTOFIX_TRIGGER_SHA="")
                self.success(result)
                self.assertIn("has no handoff path", harness.summary.read_text())
                self.assertNotIn("Handoff: queued", harness.summary.read_text())
                self.assertEqual(self.mutation_calls(harness), [])

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

    def test_successful_repair_is_held_before_push_and_hands_off_new_head(self):
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
        self.success(self.red_handoff(harness, outcome, AUTOFIX_PUSHED_SHA=push["pushed_sha"]))
        self.assert_queued(harness)
        calls = harness.calls()
        held = next(i for i, c in enumerate(calls) if c[:3] == ["gh", "pr", "edit"] and "autopilot:autofixed" in c)
        pushed = next(i for i, c in enumerate(calls) if c[:2] == ["git", "push"])
        self.assertLess(held, pushed)
        self.assertFalse(any(c[0] == "git" and "-A" in c for c in calls))

    def test_stale_remote_head_rejects_the_push_without_mutating_git(self):
        harness = self.harness(changed_files=["src/app.ts"], remote_sha=NEW_SHA, pushed_sha=NEW_SHA)
        result = harness.run("autofix", "push", TRIGGER_SHA=OLD_SHA, PUSH_TOKEN="test-push-token")
        self.success(result)
        self.assertEqual(harness.outputs()["outcome"], "stale_rejected")
        self.assertFalse(any(c[0] == "git" and c[1] in ("add", "commit", "push") for c in harness.calls()))

    def test_stale_or_owned_autofix_outcomes_skip_handoff_without_mutation(self):
        for outcome in ("stale_rejected", "existing_owner"):
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
                                ("failed_execution", "failure")):
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

    def test_successful_push_hands_off_new_head_not_trigger_head(self):
        harness = self.harness(prs=[trusted_pr(headRefOid=NEW_SHA)])
        self.success(self.red_handoff(harness, "successful_push", AUTOFIX_PUSHED_SHA=NEW_SHA))
        self.assert_queued(harness)
        comments = [c for c in harness.calls() if c[:3] == ["gh", "pr", "comment"]]
        self.assertIn(NEW_SHA, comments[0][-1])

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
