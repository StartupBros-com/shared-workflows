"""Pin the repair token's explicit grant and its existing publication boundary.

These inspect the real workflow, not GitHub's authorization engine. Live App
installation permissions and caller adoption must be verified separately.
"""
import json
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "${{ steps.app.outputs.token }}"


class AppTokenPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(
            (ROOT / ".github/workflows/dependency-autopilot.yml").read_text()
        )
        self.steps = self.workflow["jobs"]["autofix"]["steps"]
        self.by_id = {step["id"]: step for step in self.steps if "id" in step}
        self.app = self.by_id["app"]

    def test_repair_token_has_only_the_required_explicit_permissions(self):
        # PR write covers PR metadata and label create/read/add operations;
        # contents write publishes the guarded application-code commit.
        permissions = {
            key: value for key, value in self.app["with"].items()
            if key.startswith("permission-")
        }
        self.assertEqual(permissions, {
            "permission-contents": "write",
            "permission-pull-requests": "write",
        })

    def test_current_repository_scope_and_default_revocation_are_preserved(self):
        other_inputs = {
            key: value for key, value in self.app["with"].items()
            if not key.startswith("permission-")
        }
        # The pinned action scopes to the caller repository when owner and
        # repositories are omitted. Do not accidentally select an entire owner
        # or enterprise, or opt out of the default post-job token revocation.
        self.assertEqual(other_inputs, {
            "app-id": "${{ secrets.APP_ID }}",
            "private-key": "${{ secrets.APP_PRIVATE_KEY }}",
        })
        mints = [
            (job_id, step.get("id"))
            for job_id, job in self.workflow["jobs"].items()
            for step in job.get("steps", [])
            if step.get("uses", "").startswith("actions/create-github-app-token@")
        ]
        self.assertEqual(mints, [("autofix", "app")])

    def test_token_remains_after_the_guard_and_only_in_publish_steps(self):
        self.assertEqual(self.by_id["flag"]["env"]["GH_TOKEN"], TOKEN)
        self.assertEqual(self.by_id["push"]["env"]["PUSH_TOKEN"], TOKEN)
        self.assertEqual(json.dumps(self.workflow).count("steps.app.outputs.token"), 2)
        order = [step.get("id") for step in self.steps]
        for before, after in [("guard", "app"), ("app", "flag"), ("flag", "push")]:
            self.assertLess(order.index(before), order.index(after))
        self.assertEqual(self.app["if"],
            "${{ steps.pre.outputs.ok == '1' && steps.guard.outputs.changed == '1' }}")
        self.assertIn("steps.flag.outputs.flagged == '1'", self.by_id["push"]["if"])


if __name__ == "__main__":
    unittest.main()
