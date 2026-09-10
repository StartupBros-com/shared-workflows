"""Reusable harness for executing dependency-autopilot workflow shell blocks."""
import copy
import json
import os
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
        print(json.dumps([decorate(pr) for pr in config.get("prs", [])]))
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
        else:
            default_runs = {"workflow_runs": [
                {"workflow_id": 1, "run_number": 1, "run_attempt": 1, "conclusion": "success"},
            ]}
            pages = config.get("run_pages", [config.get("runs", default_runs)])
            # A fixture that specifies workflow_runs without run_attempt
            # (nearly every pre-existing test — this field predates
            # attempt-bound currency checking) defaults to attempt 1, the
            # same default `gh run view` uses above, so an unrelated test's
            # currency stays trivially "current" by construction.
            for page in pages:
                for entry in page.get("workflow_runs", []):
                    entry.setdefault("run_attempt", 1)
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
