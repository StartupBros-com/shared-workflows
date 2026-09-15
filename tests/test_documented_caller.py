"""Keep the copyable README admission examples aligned with the caller contract.

This pins the documented expression; it is not a general Actions security audit
or a replacement for the reusable workflow's live provenance validation.
"""
from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CALLEE = "StartupBros-com/shared-workflows/.github/workflows/dependency-autopilot.yml@"
EXPECTED = (
    "github.event.workflow_run.event == 'pull_request' && "
    "github.event.workflow_run.head_repository.full_name == github.repository && "
    "(startsWith(github.event.workflow_run.head_branch, 'dependabot/') || "
    "startsWith(github.event.workflow_run.head_branch, 'renovate/'))"
)


def read_examples():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return [
        yaml.load(block, Loader=yaml.BaseLoader)
        for block in re.findall(r"^```yaml\n(.*?)^```\s*$", text, re.M | re.S)
    ]


class DocumentedCallerTests(unittest.TestCase):
    def assert_admission(self, condition):
        self.assertIsInstance(condition, str)
        # Exact grouping matters: a guard in a comment/other job, or joined by
        # OR instead of AND, must not satisfy the copyable caller contract.
        self.assertEqual(" ".join(condition.split()), EXPECTED)

    def test_complete_callers_require_same_repository(self):
        callers = 0
        for example in read_examples():
            if not isinstance(example, dict):
                continue
            for name, job in example.get("jobs", {}).items():
                if not isinstance(job, dict) or not job.get("uses", "").startswith(CALLEE):
                    continue
                callers += 1
                with self.subTest(job=name):
                    self.assert_admission(job.get("if"))
        self.assertGreater(callers, 0, "No complete reusable-workflow caller example found")

    def test_short_admission_examples_match_the_contract(self):
        conditions = [
            example["if"] for example in read_examples()
            if isinstance(example, dict) and "if" in example
        ]
        self.assertTrue(conditions, "No standalone admission example found")
        for condition in conditions:
            with self.subTest(condition=condition):
                self.assert_admission(condition)


if __name__ == "__main__":
    unittest.main()
