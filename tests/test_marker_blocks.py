"""Exercise marker extraction with inert script strings, not workflow commands."""

import unittest
from unittest.mock import patch

import autopilot_harness as harness


TRIAGE = ("triage", "triage")
PRE = ("autofix", "pre")
HANDOFF = ("handoff", "handoff")
CASES = (
    (
        harness.freshness_predicate_blocks,
        "freshness-predicate",
        (TRIAGE, PRE),
        "  ",
    ),
    (harness.same_repo_filter_blocks, "same-repo-filter", (TRIAGE, PRE, HANDOFF), ""),
    (
        harness.fork_provenance_gate_blocks,
        "fork-provenance-gate",
        (TRIAGE, PRE, HANDOFF),
        "",
    ),
    (harness.sibling_verification_blocks, "sibling-verification", (TRIAGE, HANDOFF), ""),
    (harness.sibling_still_owed_blocks, "sibling-still-owed", (TRIAGE, HANDOFF), ""),
    (harness.scoped_pr_lookup_blocks, "scoped-pr-lookup", (TRIAGE, PRE, HANDOFF), ""),
)


def marker(name, body, end_indent):
    return f"# autopilot-{name}:begin\n{body}{end_indent}# autopilot-{name}:end\n"


def lookup(scripts):
    def step(job, step_id):
        return {"run": scripts[job, step_id]}

    return patch.object(harness, "step", side_effect=step)


class MarkerBlockTests(unittest.TestCase):
    def test_verbatim_bodies_in_workflow_site_order(self):
        for find_blocks, name, sites, indent in CASES:
            with self.subTest(marker=name):
                bodies = {site: f"  {site[0]} first line \n\n  second line\n" for site in sites}
                scripts = {
                    site: f"prefix\n{marker(name, body, indent)}suffix\n"
                    for site, body in bodies.items()
                }
                with lookup(scripts) as step:
                    self.assertEqual(find_blocks(), tuple(bodies[site] for site in sites))
                    self.assertEqual([call.args for call in step.call_args_list], list(sites))

    def test_missing_and_duplicate_markers_keep_site_diagnostics(self):
        for find_blocks, name, sites, indent in CASES:
            good = marker(name, "body\n", indent)
            for bad_site in sites:
                for count in (0, 2):
                    with self.subTest(marker=name, site=bad_site, number_of_markers=count):
                        bad = f"prefix\n{good * count}suffix\n"
                        scripts = dict.fromkeys(sites, good)
                        scripts[bad_site] = bad
                        expected = bad if name == "freshness-predicate" else (bad_site[0], bad)
                        with lookup(scripts) as step, self.assertRaises(AssertionError) as error:
                            find_blocks()
                        self.assertEqual(error.exception.args, (expected,))
                        self.assertEqual([call.args for call in step.call_args_list], list(sites))

    def test_freshness_requires_its_original_indented_end_marker(self):
        bad = marker("freshness-predicate", "body\n", "")
        good = marker("freshness-predicate", "body\n", "  ")
        with lookup({TRIAGE: bad, PRE: good}), self.assertRaises(AssertionError) as error:
            harness.freshness_predicate_blocks()
        self.assertEqual(error.exception.args, (bad,))

    def test_freshness_scans_both_scripts_before_any_marker_assertion(self):
        with lookup({TRIAGE: "no marker", PRE: 1}), self.assertRaises(TypeError):
            harness.freshness_predicate_blocks()
