"""Tests for licensing/license_policy.py.

The planner tests build real trees on disk and run the real planner. The sweep
tests replace only the GitHub calls with a recorded fake; they prove the
sweep's decisions, not GitHub's behaviour, which is checked by a live run.
"""
import datetime
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

ROOT =Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('license_policy', ROOT / 'licensing/license_policy.py')
lp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lp     # dataclasses look the module up while it loads
SPEC.loader.exec_module(lp)

TEMPLATE = lp.mit_template()
BODY = TEMPLATE.split('{{copyright}}\n\n', 1)[1]
# sha256 of the whitespace-normalised MIT text from "Permission is hereby
# granted" on, taken from the SPDX license list (MIT.json, licenseText).
SPDX_MIT_BODY_SHA256 = 'fe2a9817987f862eaced948f0468c7f51d2fedfc48c5c505b246a49a3870e9a5'
OURS = 'Copyright (c) 2026 Example Holdings LLC (Example Brand, https://example.test)'

APACHE = textwrap.dedent('''\
                                     Apache License
                               Version 2.0, January 2004
                            http://www.apache.org/licenses/

       TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION
    ''')


def policy(repos=None, merge=False, **holder):
    return lp.Policy(
        name=holder.get('name', 'Example Holdings LLC'),
        brand=holder.get('brand', 'Example Brand'),
        url=holder.get('url', 'https://example.test'),
        aliases=('Example Holdings', 'Example Holdings LLC', 'Example Holdings / Example Brand'),
        license='MIT', owner_logins=frozenset({'owner'}), org='example-org', repos=repos or {},
        merge=merge)


def mit(*notices, title='MIT License\n\n'):
    return title + '\n'.join(notices) + '\n\n' + BODY


def sole_owner():
    return True, ''


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix='license-policy-test-')
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)

    def put(self, rel, text):
        path = Path(self.tree, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='')

    def read(self, rel):
        with open(Path(self.tree, rel), encoding='utf-8', newline='') as fh:
            return fh.read()

    def plan(self, repo='demo', repos=None, owner=sole_owner, year='2026'):
        return lp.plan_tree(self.tree, policy(repos), repo, lp.Facts(year=year, sole_owner=owner))

    def render(self, **kwargs):
        plan = self.plan(**kwargs)
        lp.apply_plan(self.tree, plan)
        return plan

    def snapshot(self):
        return {rel: self.read(rel) for rel in lp.tracked_files(self.tree)}


class TemplateTests(unittest.TestCase):
    def test_template_body_is_the_spdx_mit_text(self):
        body = lp._mit_body(TEMPLATE)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), SPDX_MIT_BODY_SHA256)

    def test_template_is_title_one_placeholder_and_body(self):
        self.assertTrue(TEMPLATE.startswith('MIT License\n\n{{copyright}}\n\nPermission is hereby'))
        self.assertEqual(TEMPLATE.count('{{copyright}}'), 1)
        self.assertTrue(TEMPLATE.endswith('SOFTWARE.\n'))


class RootLicenseTests(TreeCase):
    def test_missing_license_is_written_for_a_sole_owner(self):
        self.put('README.md', 'demo\n')
        plan = self.render(year='2024')
        self.assertTrue(plan.first_license)
        self.assertEqual(self.read('LICENSE'), mit(OURS.replace('2026', '2024')))

    def test_render_twice_changes_nothing_the_second_time(self):
        self.put('README.md', 'demo\n')
        self.put('pkg/LICENSE', mit('Copyright (c) 2025 Example Holdings'))
        self.render()
        first = self.snapshot()
        second = self.render()
        self.assertEqual(second.writes, {})
        self.assertEqual(second.findings, [])
        self.assertEqual(self.snapshot(), first)

    def test_alias_holder_is_rewritten_and_keeps_its_year(self):
        self.put('LICENSE', mit('Copyright (c) 2023 Example Holdings / Example Brand'))
        self.render()
        self.assertEqual(self.read('LICENSE'), mit(OURS.replace('2026', '2023')))

    def test_earlier_brand_or_url_is_brought_up_to_date(self):
        self.put('LICENSE', mit('Copyright (c) 2026 Example Holdings LLC (https://old.example)'))
        self.render()
        self.assertEqual(self.read('LICENSE'), mit(OURS))

    def test_canonical_license_conforms(self):
        self.put('LICENSE', mit(OURS))
        plan = self.plan()
        self.assertEqual((plan.writes, plan.findings, plan.exit_code()), ({}, [], 0))

    def test_outside_holder_is_untouched_and_reported(self):
        original = mit('Copyright (c) 2024 Someone Else')
        self.put('LICENSE', original)
        plan = self.render()
        self.assertEqual(self.read('LICENSE'), original)
        self.assertEqual(plan.writes, {})
        self.assertEqual([f.level for f in plan.findings], ['decide'])
        self.assertIn("'Someone Else'", plan.findings[0].message)
        self.assertEqual(plan.exit_code(), 2)

    def test_holder_with_a_person_added_is_not_an_alias(self):
        original = mit('Copyright (c) 2026 Example Holdings / A Person')
        self.put('LICENSE', original)
        self.assertEqual(self.render().writes, {})
        self.assertEqual(self.read('LICENSE'), original)

    def test_mixed_holders_change_only_our_line(self):
        original = mit('Copyright (c) 2024 Someone Else', '  Copyright (c) 2025 Example Holdings',
                       title='The MIT License (MIT)\n\n')
        self.put('LICENSE', original)
        self.render()
        expected = original.replace('  Copyright (c) 2025 Example Holdings',
                                    '  ' + OURS.replace('2026', '2025'))
        self.assertEqual(self.read('LICENSE'), expected)

    def test_rewrapped_body_is_recognised_and_made_canonical(self):
        rewrapped = ' '.join(BODY.split()).replace(' OR COPYRIGHT HOLDERS', ' OR\nCOPYRIGHT HOLDERS')
        self.put('LICENSE', 'MIT License\n\nCopyright (c) 2026 Example Holdings\n\n' + rewrapped + '\n')
        self.render()
        self.assertEqual(self.read('LICENSE'), mit(OURS))

    def test_crlf_mixed_file_keeps_its_line_endings(self):
        original = mit('Copyright (c) 2024 Someone Else',
                       'Copyright (c) 2025 Example Holdings').replace('\n', '\r\n')
        self.put('LICENSE', original)
        self.render()
        expected = original.replace('Copyright (c) 2025 Example Holdings\r\n',
                                    OURS.replace('2026', '2025') + '\r\n')
        self.assertEqual(self.read('LICENSE'), expected)

    def test_apache_is_never_converted(self):
        self.put('LICENSE', APACHE)
        plan = self.render()
        self.assertEqual(self.read('LICENSE'), APACHE)
        self.assertEqual(plan.writes, {})
        self.assertIn('is Apache-2.0 but the policy says MIT', plan.findings[0].message)

    def test_apache_kept_by_a_policy_entry_conforms(self):
        self.put('LICENSE', APACHE)
        self.put('sub/LICENSE', mit('Copyright (c) 2026 Example Holdings'))
        plan = self.plan(repos={'demo': {'license': 'Apache-2.0'}})
        self.assertEqual((plan.writes, plan.findings), ({}, []))

    def test_unrecognised_text_is_reported_not_replaced(self):
        self.put('LICENSE', 'All rights reserved.\n')
        plan = self.render()
        self.assertEqual(self.read('LICENSE'), 'All rights reserved.\n')
        self.assertIn('is unrecognised', plan.findings[0].message)

    def test_extra_text_above_the_grant_is_not_treated_as_mit(self):
        original = mit('Copyright (c) 2026 Example Holdings', 'All rights reserved.')
        self.put('LICENSE', original)
        self.assertEqual(self.render().writes, {})
        self.assertEqual(self.read('LICENSE'), original)

    def test_other_license_file_without_license_asks_for_a_decision(self):
        self.put('COPYING', 'GNU GENERAL PUBLIC LICENSE\n')
        plan = self.render()
        self.assertEqual(plan.writes, {})
        self.assertFalse(Path(self.tree, 'LICENSE').exists())
        self.assertEqual(plan.exit_code(), 2)


class FirstLicenseGateTests(TreeCase):
    def test_not_written_when_authors_are_unknown(self):
        self.put('README.md', 'demo\n')
        plan = lp.plan_tree(self.tree, policy(), 'demo', lp.Facts(year='2026'))
        lp.apply_plan(self.tree, plan)
        self.assertFalse(Path(self.tree, 'LICENSE').exists())
        self.assertIn('only checked by the sweep', plan.findings[0].message)

    def test_not_written_when_someone_else_committed(self):
        self.put('README.md', 'demo\n')
        plan = self.render(owner=lambda: (False, 'commit authors outside the owner list (x)'))
        self.assertFalse(Path(self.tree, 'LICENSE').exists())
        self.assertEqual(plan.exit_code(), 2)

    def test_not_written_when_upstream_md_exists(self):
        self.put('UPSTREAM.md', 'from elsewhere\n')
        plan = self.render()
        self.assertFalse(Path(self.tree, 'LICENSE').exists())
        self.assertIn('UPSTREAM.md', plan.findings[0].message)


class NestedAndSubtreeTests(TreeCase):
    def test_copies_of_the_root_license_stay_byte_identical(self):
        old = mit('Copyright (c) 2026 Example Holdings LLC')
        for rel in ('LICENSE', 'plugins/a/LICENSE', 'plugins/a/skills/x/LICENSE'):
            self.put(rel, old)
        self.render()
        self.assertEqual(self.read('LICENSE'), mit(OURS))
        self.assertEqual(self.read('plugins/a/LICENSE'), self.read('LICENSE'))
        self.assertEqual(self.read('plugins/a/skills/x/LICENSE'), self.read('LICENSE'))

    def test_nested_third_party_license_is_untouched_and_not_reported(self):
        self.put('LICENSE', mit(OURS))
        theirs = mit('Copyright (c) 2019 Upstream Author')
        self.put('vendor/lib/LICENSE', theirs)
        self.put('vendor/other/LICENSE', APACHE)
        plan = self.render()
        self.assertEqual((plan.writes, plan.findings), ({}, []))
        self.assertEqual(self.read('vendor/lib/LICENSE'), theirs)

    def test_subtree_without_a_license_gets_a_copy_of_the_root(self):
        self.put('LICENSE', mit(OURS))
        self.put('plugins/a/plugin.json', '{}\n')
        self.put('plugins/a/deep/file.txt', 'x\n')
        self.render(repos={'demo': {'subtrees': ['plugins/*']}})
        self.assertEqual(self.read('plugins/a/LICENSE'), mit(OURS))
        self.assertFalse(Path(self.tree, 'plugins/a/deep/LICENSE').exists())
        self.assertFalse(Path(self.tree, 'plugins/LICENSE').exists())

    def test_subtree_with_any_license_like_file_gains_nothing(self):
        self.put('LICENSE', mit(OURS))
        self.put('plugins/a/LICENSE-upstream', 'their terms\n')
        self.put('plugins/b/COPYING', 'their terms\n')
        self.put('plugins/c/LICENCE.md', 'their terms\n')
        plan = self.render(repos={'demo': {'subtrees': ['plugins/*']}})
        self.assertEqual(plan.writes, {})
        for name in ('a', 'b', 'c'):
            self.assertFalse(Path(self.tree, 'plugins', name, 'LICENSE').exists())

    def test_subtrees_are_not_filled_when_the_root_names_someone_else_too(self):
        self.put('LICENSE', mit('Copyright (c) 2024 Someone Else', OURS))
        self.put('plugins/a/plugin.json', '{}\n')
        plan = self.render(repos={'demo': {'subtrees': ['plugins/*']}})
        self.assertEqual(plan.writes, {})


class PolicyEntryTests(TreeCase):
    def test_hands_off_repository_is_neither_checked_nor_written(self):
        self.put('LICENSE', mit('Copyright (c) 2026 Example Holdings'))
        before = self.snapshot()
        plan = self.render(repos={'demo': {'mode': 'hands-off', 'reason': 'derived work'}})
        self.assertEqual((plan.writes, plan.findings, plan.hands_off), ({}, [], 'derived work'))
        self.assertEqual(self.snapshot(), before)

    def test_manifest_that_disagrees_is_reported_and_not_edited(self):
        self.put('LICENSE', mit(OURS))
        self.put('package.json', '{"name": "demo", "license": "ISC"}\n')
        self.put('pyproject.toml', '[project]\nname = "demo"\nlicense = {text = "Apache-2.0"}\n')
        before = self.snapshot()
        plan = self.render()
        self.assertEqual(sorted(f.path for f in plan.decisions), ['package.json', 'pyproject.toml'])
        self.assertEqual(self.snapshot(), before)

    def test_manifest_that_agrees_or_is_silent_conforms(self):
        self.put('LICENSE', mit(OURS))
        self.put('package.json', '{"name": "demo", "license": "MIT"}\n')
        self.put('pyproject.toml', '[project]\nname = "demo"\n')
        self.assertEqual(self.plan().findings, [])


class PolicyFileTests(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as fh:
            fh.write(textwrap.dedent(text))
        self.addCleanup(os.unlink, fh.name)
        return lp.load_policy(fh.name)

    def test_shipped_policy_loads_and_writes_one_line(self):
        shipped = lp.load_policy()
        self.assertIn(shipped.name, shipped.aliases)
        text = lp.canonical_mit(shipped, TEMPLATE, ['2026'])
        notices = [line for line in text.splitlines() if line.startswith('Copyright')]
        self.assertEqual(notices, ['Copyright (c) 2026 ' + shipped.holder_text])
        self.assertEqual(lp.classify(text, TEMPLATE), 'MIT')

    def test_every_shipped_hands_off_entry_gives_a_reason(self):
        shipped = lp.load_policy()
        for name, entry in shipped.repos.items():
            if entry.get('mode') == 'hands-off':
                self.assertTrue(entry.get('reason'), name)

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(lp.PolicyError):
            self.load('[holder]\nname = "X"\nalaises = []\n')
        with self.assertRaises(lp.PolicyError):
            self.load('[holder]\nname = "X"\n[repos.a]\nsubtree = ["x"]\n')

    def test_hands_off_needs_a_reason_and_licenses_are_limited(self):
        with self.assertRaises(lp.PolicyError):
            self.load('[holder]\nname = "X"\n[repos.a]\nmode = "hands-off"\n')
        with self.assertRaises(lp.PolicyError):
            self.load('[holder]\nname = "X"\n[repos.a]\nlicense = "GPL-3.0-only"\n')
        with self.assertRaises(lp.PolicyError):
            self.load('[holder]\nname = "X"\n[defaults]\nlicense = "Apache-2.0"\n')

    def test_merge_switch_is_off_unless_the_policy_says_true(self):
        self.assertIs(self.load('[holder]\nname = "X"\n').merge, False)
        self.assertIs(self.load('[holder]\nname = "X"\n[sweep]\norg = "o"\n').merge, False)
        self.assertIs(self.load('[holder]\nname = "X"\n[sweep]\nmerge = true\n').merge, True)
        self.assertIs(self.load('[holder]\nname = "X"\n[sweep]\nmerge = false\n').merge, False)

    def test_merge_switch_must_be_a_boolean(self):
        for value in ('"yes"', '1', '"true"'):
            with self.assertRaises(lp.PolicyError, msg=value):
                self.load('[holder]\nname = "X"\n[sweep]\nmerge = %s\n' % value)


class OwnRepositoryTests(unittest.TestCase):
    def test_this_repository_conforms_to_its_own_policy(self):
        plan = lp.plan_tree(str(ROOT), lp.load_policy(), 'shared-workflows', lp.Facts(year='2026'))
        self.assertEqual([(f.level, f.path, f.message) for f in plan.findings], [])


NOW = datetime.datetime(2026, 11, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)
HEAD_SHA = 'a' * 40


def ago(hours=0, minutes=0):
    return (NOW - datetime.timedelta(hours=hours, minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


def required(*contexts):
    """A branch rule that requires these checks, as rules/branches/<branch> lists it."""
    return {'type': 'required_status_checks', 'parameters': {
        'strict_required_status_checks_policy': False,
        'required_status_checks': [{'context': c, 'integration_id': 15368} for c in contexts]}}


def git_blob(text):
    """What `git hash-object` prints; written out here so the tool's own helper is checked."""
    data = text.encode('utf-8')
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


class Scenario:
    """What GitHub says about one pull request. Every field is a plain attribute that a
    test may change after arm(); the defaults describe a pull request that merges."""

    def __init__(self, name, branch, writes, age_hours=1, age_minutes=0, quiet_minutes=None,
                 default_branch='main'):
        # quiet_minutes: how long ago the pull request last changed, when that is
        # later than when it was opened.
        self.default_branch = default_branch
        self.pull = {
            'state': 'open', 'draft': False, 'node_id': 'PR_node_1',
            'created_at': ago(age_hours, age_minutes),
            'updated_at': (ago(age_hours, age_minutes) if quiet_minutes is None
                           else ago(minutes=quiet_minutes)),
            'base': {'ref': default_branch},
            'head': {'ref': branch, 'sha': HEAD_SHA, 'repo': {'full_name': 'example-org/' + name}}}
        self.files = [{'filename': path, 'status': 'added', 'sha': git_blob(text)}
                      for path, text in writes.items()]
        self.reviews = []
        self.check_runs = [[{'name': 'test', 'status': 'completed', 'conclusion': 'success'}]]
        self.statuses = []
        self.rules = []
        self.merge_reply = {'merged': True, 'sha': 'b' * 40}
        self.graphql_reply = {'data': {'enqueuePullRequest': {'mergeQueueEntry': {'id': 'Q1'}}}}
        self.reads = 0      # check-run reads so far; check_runs holds one answer per read


class FakeGh:
    """Recorded GitHub answers; records every write the sweep attempts."""

    def __init__(self, repos, trees, contributors=None, pulls=None, detected_on_branch='MIT'):
        self.repos, self.trees = repos, trees
        self.contributors = contributors or {}
        self.pulls = pulls or {}
        self.detected_on_branch = detected_on_branch
        self.posts = []
        self.gets = []
        self.cloned = []
        self.scripts = {}       # repository name -> Scenario, see arm()
        self.fail = set()       # a request whose path ends with one of these raises
        self.unexpected = []    # reads the fake has no answer for; SweepCase.sweep checks it

    def arm(self, name, branch, writes, **over):
        """Script the pull request the sweep will find or open on `branch`."""
        self.scripts[name] = Scenario(name, branch, writes, **over)
        return self.scripts[name]

    def _unexpected(self, path):
        # Recorded as well as raised: the sweep turns an exception inside one
        # repository into a row, and the test must still fail.
        self.unexpected.append(path)
        raise AssertionError('unexpected read: ' + path)

    def _maybe_fail(self, path):
        if any(path.split('?')[0].endswith(tail) for tail in self.fail):
            raise RuntimeError('gh api %s failed: boom\nsecond line' % path)

    def merge_calls(self):
        return [(m, p, b) for m, p, b in self.posts if m == 'PUT' or p == 'graphql']

    def lines(self, path, jq):
        if path.startswith('orgs/'):
            return self.repos
        name = path.split('/')[2]
        if '/contributors' in path:
            return self.contributors.get(name, [['User', 'owner']])
        if '/git/matching-refs/' in path:
            return []
        if '/pulls?state=all' in path:
            branch = path.split('head=example-org:')[1].split('&')[0]
            return self.pulls.get((name, branch), [])
        if '/pulls?state=open' in path:
            return [[n, ref] for (repo, ref), rows in self.pulls.items() if repo == name
                    for n, state, _m in rows if state == 'open']
        self._unexpected(path)

    def get(self, path):
        self.gets.append(path)
        self._maybe_fail(path)
        base = path.split('?')[0]
        scenario = self.scripts.get(path.split('/')[2]) if path.startswith('repos/') else None
        if scenario is None or '/license' in base:
            return {'license': {'spdx_id': self.detected_on_branch}}
        if base.endswith('/files'):
            return scenario.files
        if base.endswith('/reviews'):
            return scenario.reviews
        if base.endswith('/check-runs'):
            scenario.reads += 1
            runs = scenario.check_runs[min(scenario.reads, len(scenario.check_runs)) - 1]
            return {'total_count': len(runs), 'check_runs': runs}
        if base.endswith('/status'):
            return {'state': 'pending', 'total_count': len(scenario.statuses),
                    'statuses': scenario.statuses}
        if base.endswith('/rules/branches/' + scenario.default_branch):
            return scenario.rules
        if '/pulls/' in base:
            return scenario.pull
        self._unexpected(path)

    def post(self, path, body, method='POST'):
        self.posts.append((method, path, body))
        self._maybe_fail(path)
        if path.endswith('/pulls'):
            return {'number': 7}
        reply = {'sha': 'f' * 40}
        if method == 'PUT':
            reply = self.scripts[path.split('/')[2]].merge_reply
        elif path == 'graphql':
            reply = next(iter(self.scripts.values())).graphql_reply
        if isinstance(reply, Exception):    # a scripted answer that cannot be parsed
            raise reply
        return reply

    def clone(self, org, name, dest):
        self.cloned.append(name)
        shutil.copytree(self.trees[name], dest)
        git = ['git', '-C', dest, '-c', 'user.name=t', '-c', 'user.email=t@example.test']
        subprocess.run(['git', '-C', dest, 'init', '--quiet'], check=True)
        subprocess.run(['git', '-C', dest, 'add', '-A'], check=True)
        subprocess.run(git + ['commit', '--quiet', '-m', 'seed'], check=True)


def repo(name, **over):
    row = dict(name=name, private=False, fork=False, archived=False, size=10,
               default_branch='main', created_at='2025-03-01T00:00:00Z', license='MIT')
    row.update(over)
    return row


class SweepCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='license-sweep-test-')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.trees = {}
        self.sleeps = []
        self.real_sleeps = []
        # Every wait in these tests goes through the injected sleep. One that
        # reaches the real clock fails at once instead of stalling the suite. It
        # is recorded as well as raised, because the sweep turns an exception
        # inside one repository into a row.
        real = mock.patch.object(lp.time, 'sleep', side_effect=self._real_sleep)
        real.start()
        self.addCleanup(real.stop)
        self.addCleanup(lambda: self.assertEqual(self.real_sleeps, [], 'the real sleep was reached'))

    def _real_sleep(self, seconds):
        self.real_sleeps.append(seconds)
        raise AssertionError('a test reached the real sleep')

    def tree(self, name, files):
        root = Path(self.base, 'src', name)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.trees[name] = str(root)

    def sweep(self, gh, apply=False, repos=None, merge=False, during_wait=None):
        """`during_wait` runs inside each sleep: what changes on GitHub while the sweep waits."""
        def sleep(seconds):
            self.sleeps.append(seconds)
            # The budget is 16 steps. A wait that outlives it by this much never ends.
            if len(self.sleeps) > 60:
                raise AssertionError('the wait never ends')
            if during_wait:
                during_wait()
        out = io.StringIO()
        work = tempfile.mkdtemp(prefix='work-', dir=self.base)
        code = lp.sweep(gh, policy(repos, merge=merge), apply=apply, out=out, workdir=work,
                        now=NOW, sleep=sleep)
        self.assertEqual(gh.unexpected, [])
        self.assertLessEqual(len(self.sleeps), 60, 'the wait never ends')
        return code, out.getvalue()


class SweepTests(SweepCase):

    def test_private_fork_and_archived_repositories_are_never_cloned(self):
        self.tree('good', {'LICENSE': mit(OURS)})
        gh = FakeGh([repo('good'), repo('secret-thing', private=True), repo('a-fork', fork=True),
                     repo('old', archived=True)], self.trees)
        code, text = self.sweep(gh, apply=True)
        self.assertEqual(gh.cloned, ['good'])
        self.assertNotIn('secret-thing', text)
        self.assertIn('1 private not examined', text)
        self.assertEqual((code, gh.posts), (0, []))

    def test_report_mode_writes_nothing(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        gh = FakeGh([repo('drift')], self.trees)
        code, text = self.sweep(gh, apply=False)
        self.assertEqual(gh.posts, [])
        self.assertIn('would-open-pr', text)
        self.assertEqual(code, 1)

    def test_apply_opens_one_pull_request_with_the_rendered_file(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        gh = FakeGh([repo('drift')], self.trees)
        code, text = self.sweep(gh, apply=True)
        paths = [path for _m, path, _b in gh.posts]
        self.assertEqual(paths, ['repos/example-org/drift/git/trees', 'repos/example-org/drift/git/commits',
                                 'repos/example-org/drift/git/refs', 'repos/example-org/drift/pulls'])
        tree_body = gh.posts[0][2]
        self.assertEqual(tree_body['tree'], [{'path': 'LICENSE', 'mode': '100644', 'type': 'blob',
                                              'content': mit(OURS)}])
        self.assertTrue(gh.posts[2][2]['ref'].startswith('refs/heads/chore/license-policy-'))
        self.assertNotIn('had no license', gh.posts[3][2]['body'])
        self.assertIn('pr-opened', text)
        self.assertEqual(code, 1)

    def test_an_open_pull_request_for_the_same_change_is_not_duplicated(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        branch = lp.BRANCH_PREFIX + lp.digest_of({'LICENSE': mit(OURS)})
        gh = FakeGh([repo('drift')], self.trees, pulls={('drift', branch): [[3, 'open', None]]})
        code, text = self.sweep(gh, apply=True)
        self.assertEqual(gh.posts, [])
        self.assertIn('#3 waits for a merge', text)
        self.assertEqual(code, 1)

    def test_a_closed_pull_request_is_respected(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        branch = lp.BRANCH_PREFIX + lp.digest_of({'LICENSE': mit(OURS)})
        gh = FakeGh([repo('drift')], self.trees, pulls={('drift', branch): [[3, 'closed', None]]})
        code, text = self.sweep(gh, apply=True)
        self.assertEqual(gh.posts, [])
        self.assertIn('#3 was closed without merging', text)
        self.assertEqual(code, 2)

    def test_a_superseded_pull_request_is_closed_with_a_pointer(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        gh = FakeGh([repo('drift')], self.trees,
                    pulls={('drift', lp.BRANCH_PREFIX + 'olddigest0'): [[2, 'open', None]]})
        self.sweep(gh, apply=True)
        tail = [(m, path) for m, path, _b in gh.posts[-2:]]
        self.assertEqual(tail, [('POST', 'repos/example-org/drift/issues/2/comments'),
                                ('PATCH', 'repos/example-org/drift/pulls/2')])
        self.assertEqual(gh.posts[-1][2], {'state': 'closed'})

    def test_first_license_needs_sole_ownership_and_says_so_in_the_pull_request(self):
        self.tree('bare', {'README.md': 'x\n'})
        self.tree('shared', {'README.md': 'x\n'})
        gh = FakeGh([repo('bare', license=None), repo('shared', license=None)], self.trees,
                    contributors={'shared': [['User', 'owner'], ['User', 'stranger']],
                                  'bare': [['User', 'owner'], ['Bot', 'dependabot[bot]']]})
        code, text = self.sweep(gh, apply=True)
        pulls = [(path, body) for _m, path, body in gh.posts if path.endswith('/pulls')]
        self.assertEqual([path for path, _b in pulls], ['repos/example-org/bare/pulls'])
        self.assertIn('had no license', pulls[0][1]['body'])
        tree_body = [b for _m, p, b in gh.posts if p.endswith('/git/trees')][0]
        self.assertEqual(tree_body['tree'][0]['content'], mit(OURS.replace('2026', '2025')))
        self.assertIn('stranger', text)
        self.assertEqual(code, 2)

    def test_first_license_branch_is_not_asked_for_its_detected_license(self):
        # GitHub answered 404 for a branch's license on every repository whose
        # default branch had none, so asking always looked like a failed detection.
        self.tree('bare', {'README.md': 'x\n'})
        gh = FakeGh([repo('bare', license=None)], self.trees, detected_on_branch=None)
        code, text = self.sweep(gh, apply=True)
        body = [b for _m, p, b in gh.posts if p.endswith('/pulls')][0]['body']
        self.assertEqual(gh.gets, [])
        self.assertNotIn('GitHub detects', text + body)
        self.assertIn('next sweep after the merge confirms detection', body)
        self.assertEqual(code, 1)

    def test_changed_license_branch_is_asked_for_its_detected_license(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        gh = FakeGh([repo('drift')], self.trees)
        self.sweep(gh, apply=True)
        self.assertEqual(len(gh.gets), 1)
        self.assertIn('/license?ref=chore/license-policy-', gh.gets[0])
        body = [b for _m, p, b in gh.posts if p.endswith('/pulls')][0]['body']
        self.assertIn('GitHub detects `MIT` on this branch.', body)

    def test_anonymous_commit_authors_block_a_first_license(self):
        self.tree('bare', {'README.md': 'x\n'})
        gh = FakeGh([repo('bare', license=None)], self.trees,
                    contributors={'bare': [['User', 'owner'], ['Anonymous', 'owner']]})
        code, _text = self.sweep(gh, apply=True)
        self.assertEqual((code, gh.posts), (2, []))

    def test_conforming_tree_that_github_does_not_detect_is_flagged(self):
        self.tree('odd', {'LICENSE': mit(OURS)})
        gh = FakeGh([repo('odd', license='NOASSERTION')], self.trees)
        code, text = self.sweep(gh)
        self.assertIn('GitHub detects NOASSERTION instead of MIT', text)
        self.assertEqual(code, 2)

    def test_branch_that_github_does_not_detect_is_flagged(self):
        self.tree('drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
        gh = FakeGh([repo('drift')], self.trees, detected_on_branch='NOASSERTION')
        code, text = self.sweep(gh, apply=True)
        self.assertIn('GitHub detects NOASSERTION on chore/license-policy-', text)
        self.assertEqual(code, 2)

    def test_hands_off_repository_is_listed_with_its_reason_and_not_cloned(self):
        self.tree('theirs', {'LICENSE': mit('Copyright (c) 2020 Upstream')})
        gh = FakeGh([repo('theirs')], self.trees)
        code, text = self.sweep(gh, apply=True,
                                repos={'theirs': {'mode': 'hands-off', 'reason': 'derived work'}})
        self.assertEqual((code, gh.cloned, gh.posts), (0, [], []))
        self.assertIn('derived work', text)


class MergeHelperTests(unittest.TestCase):
    def test_blob_id_matches_git(self):
        for text in ('', 'MIT License\n', 'café ✓\r\n'):
            git = subprocess.run(['git', 'hash-object', '--stdin'], input=text.encode('utf-8'),
                                 capture_output=True, check=True).stdout.decode().strip()
            self.assertEqual(lp.blob_id(text), git)

    def test_wait_budget_is_sixteen_steps_of_fifteen_seconds(self):
        slept = []
        with mock.patch.object(lp.time, 'sleep',
                               side_effect=AssertionError('the real sleep was reached')):
            budget = lp.WaitBudget(slept.append)
            spent = [budget.spend() for _ in range(18)]
        self.assertEqual(spent, [True] * 16 + [False] * 2)
        self.assertEqual(slept, [15] * 16)

    def test_wait_budget_sleeps_for_real_when_no_sleep_is_given(self):
        with mock.patch.object(lp.time, 'sleep') as real:
            self.assertTrue(lp.WaitBudget().spend())
        real.assert_called_once_with(15)

    def test_a_list_is_asked_for_a_hundred_entries_a_page(self):
        # GitHub's default page is 30. Without this the 100-entry check below
        # could never fire, and entry 31 would go unread.
        class Paths:
            def __init__(self):
                self.seen = []

            def get(self, path):
                self.seen.append(path)
                return []

        gh = Paths()
        lp._read_list(gh, 'repos/o/r/pulls/1/files')
        lp._read_list(gh, 'repos/o/r/license?ref=b')
        self.assertEqual(gh.seen, ['repos/o/r/pulls/1/files?per_page=100',
                                   'repos/o/r/license?ref=b&per_page=100'])

    def test_a_list_that_may_be_cut_short_is_an_error(self):
        # An entry that was not read must never count as a pass.
        class Answers:
            def __init__(self, answer):
                self.answer = answer

            def get(self, path):
                return self.answer

        self.assertEqual(lp._read_list(Answers([1] * 99), 'x'), [1] * 99)
        self.assertEqual(
            lp._read_list(Answers({'total_count': 2, 'check_runs': [1, 2]}), 'x', 'check_runs'),
            [1, 2])
        for answer, key in (([1] * 100, None),
                            ({'total_count': 100, 'check_runs': [1] * 100}, 'check_runs'),
                            ({'total_count': 3, 'check_runs': [1, 2]}, 'check_runs')):
            with self.subTest(key=key, entries=len(answer if key is None else answer[key])):
                with self.assertRaises(RuntimeError):
                    lp._read_list(Answers(answer), 'x', key)


class MergeTests(SweepCase):
    PUT_BODY = {'merge_method': 'squash', 'sha': HEAD_SHA}

    def case(self, first=False, existing=True, extra=None, age_hours=1, age_minutes=0,
             quiet_minutes=None, default_branch='main'):
        """One repository with a pull request for the sweep's own change."""
        if first:
            name, files = 'bare', {'README.md': 'x\n'}
            writes = {'LICENSE': mit(OURS.replace('2026', '2025'))}
        else:
            name, files = 'drift', {'LICENSE': mit('Copyright (c) 2026 Example Holdings')}
            writes = {'LICENSE': mit(OURS)}
        self.tree(name, dict(files, **(extra or {})))
        branch = lp.BRANCH_PREFIX + lp.digest_of(writes)
        pulls = {(name, branch): [[12, 'open', None]]} if existing else {}
        gh = FakeGh([repo(name, license=None if first else 'MIT', default_branch=default_branch)],
                    self.trees, pulls=pulls)
        return gh, gh.arm(name, branch, writes, age_hours=age_hours, age_minutes=age_minutes,
                          quiet_minutes=quiet_minutes, default_branch=default_branch), name

    def direct(self, gh, sc, license_wanted='MIT', **kwargs):
        """try_merge on its own, for what a whole sweep cannot reach or would hide."""
        plan = types.SimpleNamespace(writes={'LICENSE': mit(OURS)}, first_license=False)
        return lp.try_merge(gh, policy({'drift': {'license': license_wanted}}, merge=True),
                            'example-org', repo('drift'), plan, sc.pull['head']['ref'], 12, [],
                            lp.WaitBudget(self.sleeps.append), **kwargs)

    def run_merge(self, gh, during_wait=None):
        code, text = self.sweep(gh, apply=True, merge=True, during_wait=during_wait)
        return code, text, text.splitlines()[0].split()[1]

    def assertNoMerge(self, gh):
        self.assertEqual(gh.merge_calls(), [])

    def assertRefused(self, gh, text, status, why):
        self.assertNoMerge(gh)
        self.assertEqual(status, 'pr-open')
        self.assertIn('#12 not merged: %s (1 file(s))' % why, text)

    def test_merging_disabled_leaves_rows_and_calls_as_they_were(self):
        gh, _sc, _name = self.case()
        code, text = self.sweep(gh, apply=True, merge=False)
        self.assertNoMerge(gh)
        self.assertEqual(gh.gets, [])
        self.assertIn('pr-open', text)
        self.assertIn('#12 waits for a merge (1 file(s))', text)
        self.assertEqual(code, 1)

    def test_report_only_sweep_with_merging_enabled_calls_nothing(self):
        gh, _sc, _name = self.case()
        code, text = self.sweep(gh, apply=False, merge=True)
        self.assertEqual((gh.posts, gh.gets), ([], []))
        self.assertIn('#12 waits for a merge (1 file(s))', text)
        self.assertEqual(code, 1)

    def test_first_license_opened_with_merging_disabled_still_says_merge_by_hand(self):
        gh, _sc, _name = self.case(first=True, existing=False)
        code, text = self.sweep(gh, apply=True, merge=False)
        self.assertNoMerge(gh)
        self.assertIn('#7 (1 file(s)), first license: merge by hand', text)
        self.assertEqual((code, self.sleeps), (1, []))

    def test_open_pull_request_with_green_checks_is_merged_at_the_checked_head(self):
        gh, _sc, _name = self.case()
        code, text, status = self.run_merge(gh)
        self.assertEqual(gh.merge_calls(),
                         [('PUT', 'repos/example-org/drift/pulls/12/merge', self.PUT_BODY)])
        self.assertEqual((code, status), (0, 'merged'))
        self.assertIn('#12 merged (1 file(s))', text)
        self.assertNotIn('first license', text)
        self.assertEqual(self.sleeps, [])

    def test_pull_request_opened_in_this_run_is_never_merged_in_that_run(self):
        # Checks start a moment after a pull request exists. Until they do GitHub
        # reports none, which reads the same as all green. The scripted pull
        # request is an hour old with a passing check, so only the rule stops it.
        gh, _sc, _name = self.case(existing=False)
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status, self.sleeps), (0, 'pr-opened', []))
        self.assertIn('#7 waits: opened in this run: a later run merges it (1 file(s))', text)
        self.assertEqual([p for p in gh.gets if '/pulls/7' in p or '/commits/' in p], [])
        self.assertEqual(len([p for p in gh.gets if '/license?ref=' in p]), 1)

    def test_pull_request_opened_in_this_run_with_an_open_decision_is_refused(self):
        gh, _sc, _name = self.case(existing=False)
        gh.detected_on_branch = 'NOASSERTION'
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (2, 'pr-opened'))
        self.assertIn('#7 not merged: a decision is open (1 file(s))', text)

    def test_pull_request_younger_than_ten_minutes_waits(self):
        for minutes in (0, 9, -300):        # -300: GitHub's clock is ahead of this host's
            with self.subTest(minutes=minutes):
                gh, _sc, _name = self.case(age_hours=0, age_minutes=minutes)
                code, text, status = self.run_merge(gh)
                self.assertNoMerge(gh)
                self.assertEqual((code, status, self.sleeps), (0, 'pr-open', []))
                self.assertIn('#12 waits: opened or changed less than 10 minutes ago: a later run '
                              'merges it (1 file(s))', text)

    def test_pull_request_exactly_ten_minutes_old_merges(self):
        gh, _sc, _name = self.case(age_hours=0, age_minutes=10)
        code, _text, status = self.run_merge(gh)
        self.assertEqual((code, status), (0, 'merged'))

    def test_an_old_pull_request_whose_branch_just_moved_waits(self):
        # "Update branch" on an old pull request: the planned file is unchanged,
        # so every other guard passes, and the new commit has no checks yet.
        for first in (False, True):
            for minutes in (0, 9):
                with self.subTest(first=first, minutes=minutes):
                    gh, sc, _name = self.case(first=first, age_hours=30, quiet_minutes=minutes)
                    sc.check_runs = [[]]
                    code, text, status = self.run_merge(gh)
                    self.assertNoMerge(gh)
                    self.assertEqual((code, status, self.sleeps), (0, 'pr-open', []))
                    self.assertIn('#12 waits: opened or changed less than 10 minutes ago', text)

    def test_an_old_pull_request_that_last_changed_ten_minutes_ago_merges(self):
        gh, _sc, _name = self.case(age_hours=30, quiet_minutes=10)
        _code, _text, status = self.run_merge(gh)
        self.assertEqual(status, 'merged')

    def test_a_pull_request_that_keeps_changing_for_three_days_turns_red(self):
        gh, _sc, _name = self.case(age_hours=73, quiet_minutes=1)
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (1, 'pr-open'))
        self.assertIn('#12 waits: opened or changed less than 10 minutes ago', text)

    def test_the_real_clock_is_used_when_none_is_given(self):
        def stamp(hours):
            when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
            return when.strftime('%Y-%m-%dT%H:%M:%SZ')

        gh, sc, _name = self.case()
        sc.pull['created_at'] = sc.pull['updated_at'] = stamp(0)
        self.assertEqual(self.direct(gh, sc).status, 'waiting')
        sc.pull['created_at'] = sc.pull['updated_at'] = stamp(1)
        self.assertEqual(self.direct(gh, sc).status, 'merged')

    def test_open_decision_refuses(self):
        gh, _sc, _name = self.case(extra={'package.json': '{"license": "ISC"}'})
        code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'a decision is open')
        self.assertEqual(code, 2)

    def test_draft_refuses(self):
        gh, sc, _name = self.case()
        sc.pull['draft'] = True
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'it is a draft')

    def test_closed_pull_request_refuses(self):
        gh, sc, _name = self.case()
        sc.pull['state'] = 'closed'
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'it is not open')

    def test_base_other_than_the_default_branch_refuses(self):
        gh, sc, _name = self.case()
        sc.pull['base']['ref'] = 'develop'
        code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'it targets develop, not main')
        self.assertEqual(code, 1)

    def test_head_branch_other_than_the_plans_refuses(self):
        gh, sc, _name = self.case()
        sc.pull['head']['ref'] = 'someone/else'
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'its branch is not the sweep\'s own branch')

    def test_head_from_another_repository_refuses(self):
        gh, sc, _name = self.case()
        sc.pull['head']['repo'] = {'full_name': 'stranger/drift'}
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'its branch is not the sweep\'s own branch')
        sc.pull['head']['repo'] = None      # a deleted fork
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'its branch is not the sweep\'s own branch')

    def test_first_license_opened_in_this_run_is_never_merged(self):
        gh, _sc, _name = self.case(first=True, existing=False, age_hours=48)
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (0, 'pr-opened'))
        self.assertIn('#7 waits: first license: merges once the pull request is 20 hours old '
                      '(1 file(s))', text)

    def test_first_license_waits_until_it_is_twenty_hours_old(self):
        gh, _sc, _name = self.case(first=True, age_hours=19)
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (0, 'pr-open'))
        self.assertIn('#12 waits: first license: merges once the pull request is 20 hours old '
                      '(1 file(s))', text)

    def test_first_license_merges_once_it_is_older_than_twenty_hours(self):
        gh, _sc, _name = self.case(first=True, age_hours=21)
        code, text, status = self.run_merge(gh)
        self.assertEqual(gh.merge_calls(),
                         [('PUT', 'repos/example-org/bare/pulls/12/merge', self.PUT_BODY)])
        self.assertEqual((code, status), (0, 'merged'))
        self.assertIn('#12 merged (1 file(s)), first license', text)

    def test_first_license_hold_ends_at_exactly_twenty_hours(self):
        for hours, minutes, merges in ((19, 59, False), (20, 0, True), (-5, 0, False)):
            with self.subTest(hours=hours, minutes=minutes):
                gh, _sc, _name = self.case(first=True, age_hours=hours, age_minutes=minutes)
                code, text, status = self.run_merge(gh)
                self.assertEqual((code, status), (0, 'merged' if merges else 'pr-open'))
                self.assertEqual(len(gh.merge_calls()), 1 if merges else 0)
                if not merges:
                    self.assertIn('#12 waits: first license: merges once', text)

    def test_an_extra_file_refuses(self):
        gh, sc, _name = self.case()
        sc.files.append({'filename': 'README.md', 'status': 'modified', 'sha': git_blob('x')})
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'its files no longer match the plan')

    def test_a_missing_file_refuses(self):
        gh, sc, _name = self.case()
        sc.files = []
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'its files no longer match the plan')

    def test_a_planned_file_with_other_content_refuses(self):
        gh, sc, _name = self.case()
        sc.files[0]['sha'] = git_blob(mit(OURS) + 'extra\n')
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'its files no longer match the plan')

    def test_a_file_that_is_not_added_or_modified_refuses(self):
        for kind in ('removed', 'renamed', 'copied', 'changed', 'unchanged'):
            with self.subTest(kind):
                gh, sc, _name = self.case()
                sc.files[0]['status'] = kind
                _code, text, status = self.run_merge(gh)
                self.assertRefused(gh, text, status, 'its files no longer match the plan')

    def test_a_file_list_too_long_to_read_in_one_page_refuses(self):
        gh, sc, _name = self.case()
        sc.files = sc.files * 100
        _code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertIn('#12 not merged: merge refused: more than 100 entries at ', text)

    def test_wrong_license_detected_on_the_branch_refuses(self):
        gh, _sc, _name = self.case()
        gh.detected_on_branch = 'NOASSERTION'
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'GitHub does not detect MIT on the branch')

    def test_failed_license_lookup_refuses(self):
        gh, _sc, _name = self.case()
        gh.fail = {'/license'}
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'GitHub does not detect MIT on the branch')

    def review(self, who, state):
        return {'user': {'login': who}, 'state': state}

    def test_changes_requested_refuses(self):
        gh, sc, _name = self.case()
        sc.reviews = [self.review('alice', 'APPROVED'), self.review('bob', 'CHANGES_REQUESTED'),
                      self.review('bob', 'COMMENTED')]
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'changes were requested by bob')

    def test_changes_requested_then_approved_by_the_same_reviewer_merges(self):
        gh, sc, _name = self.case()
        sc.reviews = [self.review('bob', 'CHANGES_REQUESTED'), self.review('bob', 'APPROVED')]
        _code, _text, status = self.run_merge(gh)
        self.assertEqual(status, 'merged')

    def test_changes_requested_then_dismissed_merges(self):
        gh, sc, _name = self.case()
        sc.reviews = [self.review('bob', 'CHANGES_REQUESTED'), self.review('bob', 'DISMISSED')]
        _code, _text, status = self.run_merge(gh)
        self.assertEqual(status, 'merged')

    RUNNING = {'name': 'test', 'status': 'in_progress', 'conclusion': None}
    DONE = {'name': 'test', 'status': 'completed', 'conclusion': 'success'}

    def test_a_review_left_while_checks_ran_is_seen(self):
        gh, sc, _name = self.case()
        sc.check_runs = [[self.RUNNING], [self.DONE]]
        _code, text, status = self.run_merge(
            gh, during_wait=lambda: sc.reviews.append(self.review('bob', 'CHANGES_REQUESTED')))
        self.assertEqual(self.sleeps, [15])
        self.assertRefused(gh, text, status, 'changes were requested by bob')

    def test_a_pull_request_closed_drafted_or_pushed_to_while_checks_ran_refuses(self):
        def close(sc):
            sc.pull['state'] = 'closed'

        def draft(sc):
            sc.pull['draft'] = True

        def push(sc):
            sc.pull['head']['sha'] = 'c' * 40

        for change, why in ((close, 'it is not open'), (draft, 'it is a draft'),
                            (push, 'its branch changed while its checks ran')):
            with self.subTest(why):
                gh, sc, _name = self.case()
                sc.check_runs = [[self.RUNNING], [self.DONE]]
                _code, text, status = self.run_merge(gh, during_wait=lambda: change(sc))
                self.assertRefused(gh, text, status, why)

    def test_failed_check_run_refuses(self):
        gh, sc, _name = self.case()
        sc.check_runs = [[{'name': 'lint', 'status': 'completed', 'conclusion': 'success'},
                          {'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]]
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'a check failed: test')

    def test_failed_commit_status_refuses(self):
        gh, sc, _name = self.case()
        sc.statuses = [{'context': 'ci/legacy', 'state': 'failure'}]
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'a check failed: ci/legacy')

    def test_a_failure_wins_over_a_pending_check(self):
        gh, sc, _name = self.case()
        sc.check_runs = [[{'name': 'slow', 'status': 'in_progress', 'conclusion': None},
                          {'name': 'test', 'status': 'completed', 'conclusion': 'cancelled'}]]
        _code, text, status = self.run_merge(gh)
        self.assertRefused(gh, text, status, 'a check failed: test')
        self.assertEqual(self.sleeps, [])

    def test_every_conclusion_that_is_not_a_pass_refuses(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'action_required', 'stale', None):
            with self.subTest(conclusion=conclusion):
                gh, sc, _name = self.case()
                sc.check_runs = [[{'name': 'test', 'status': 'completed', 'conclusion': conclusion}]]
                _code, text, status = self.run_merge(gh)
                self.assertRefused(gh, text, status, 'a check failed: test')
        for state in ('failure', 'error', 'expected'):
            with self.subTest(state=state):
                gh, sc, _name = self.case()
                sc.statuses = [{'context': 'ci/legacy', 'state': state}]
                _code, text, status = self.run_merge(gh)
                self.assertRefused(gh, text, status, 'a check failed: ci/legacy')

    def test_neutral_and_skipped_check_runs_count_as_passed(self):
        gh, sc, _name = self.case()
        sc.check_runs = [[{'name': 'a', 'status': 'completed', 'conclusion': 'neutral'},
                          {'name': 'b', 'status': 'completed', 'conclusion': 'skipped'}]]
        _code, _text, status = self.run_merge(gh)
        self.assertEqual(status, 'merged')

    def test_commit_with_no_checks_and_no_statuses_merges(self):
        # A repository with no checks at all. The pull request is an hour old, so
        # checks that were going to start have started, and the branch requires
        # none. The combined status endpoint answers state 'pending' for such a
        # commit; the sweep reads the list, not that field.
        gh, sc, _name = self.case()
        sc.check_runs = [[]]
        _code, _text, status = self.run_merge(gh)
        self.assertEqual(status, 'merged')

    def test_a_required_check_that_has_not_reported_is_waited_for(self):
        # GitHub lists nothing for a required check that has not started, exactly
        # as for a repository with no checks. Only the branch rule tells them apart.
        for runs in ([], [{'name': 'lint', 'status': 'completed', 'conclusion': 'success'}]):
            with self.subTest(runs=len(runs)):
                self.sleeps.clear()
                gh, sc, _name = self.case()
                sc.rules = [{'type': 'pull_request'}, required('lint', 'test')]
                sc.check_runs = [runs]
                code, text, status = self.run_merge(gh)
                self.assertNoMerge(gh)
                self.assertEqual((code, status, self.sleeps), (0, 'pr-open', [15] * 16))
                self.assertIn('#12 waits: the required check %s has not reported (1 file(s))'
                              % ('test' if runs else 'lint'), text)

    def test_a_required_check_that_reports_while_the_sweep_waits_merges(self):
        gh, sc, _name = self.case()
        sc.rules = [required('test')]
        sc.check_runs = [[], [self.RUNNING], [self.DONE]]
        _code, _text, status = self.run_merge(gh)
        self.assertEqual((status, self.sleeps), ('merged', [15, 15]))

    def test_a_required_check_met_by_a_commit_status_or_a_skipped_run_merges(self):
        for runs, statuses in (([], [{'context': 'test', 'state': 'success'}]),
                               ([{'name': 'test', 'status': 'completed', 'conclusion': 'skipped'}], [])):
            with self.subTest(runs=len(runs)):
                gh, sc, _name = self.case()
                sc.rules = [required('test')]
                sc.check_runs, sc.statuses = [runs], statuses
                _code, _text, status = self.run_merge(gh)
                self.assertEqual((status, self.sleeps), ('merged', []))

    def test_pending_check_that_completes_within_the_budget_merges(self):
        gh, sc, _name = self.case()
        running = {'name': 'test', 'status': 'in_progress', 'conclusion': None}
        done = {'name': 'test', 'status': 'completed', 'conclusion': 'success'}
        sc.check_runs = [[running], [running], [done]]
        _code, _text, status = self.run_merge(gh)
        self.assertEqual((status, self.sleeps), ('merged', [15, 15]))

    def test_pending_beyond_the_budget_waits_and_is_not_red(self):
        gh, sc, _name = self.case()
        sc.check_runs = [[{'name': 'test', 'status': 'queued', 'conclusion': None}]]
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status, self.sleeps), (0, 'pr-open', [15] * 16))
        self.assertIn('#12 waits: checks are still running (1 file(s))', text)

    def test_pending_status_beyond_the_budget_waits(self):
        gh, sc, _name = self.case()
        sc.statuses = [{'context': 'ci/legacy', 'state': 'pending'}]
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (0, 'pr-open'))

    def test_pending_beyond_the_budget_on_a_three_day_old_pull_request_is_red(self):
        gh, sc, _name = self.case(age_hours=73)
        sc.check_runs = [[{'name': 'test', 'status': 'queued', 'conclusion': None}]]
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (1, 'pr-open'))
        self.assertIn('#12 waits: checks are still running', text)

    def test_a_waiting_pull_request_turns_red_only_after_seventy_two_hours(self):
        for hours, minutes, code_wanted in ((72, 0, 0), (72, 1, 1)):
            with self.subTest(hours=hours, minutes=minutes):
                gh, sc, _name = self.case(age_hours=hours, age_minutes=minutes)
                sc.check_runs = [[{'name': 'test', 'status': 'queued', 'conclusion': None}]]
                code, _text, status = self.run_merge(gh)
                self.assertNoMerge(gh)
                self.assertEqual((code, status), (code_wanted, 'pr-open'))

    def test_a_first_license_held_for_three_days_is_red(self):
        # Cannot happen through the 20 hour hold alone; a clock set wrong could do it.
        row = lp.merge_row('bare', 'pr-open', 12, lp.MergeOutcome('waiting', 'why', stale=True),
                           '1 file(s)', True, [])
        self.assertEqual((row.status, row.red), ('pr-open', True))

    def test_the_wait_budget_is_shared_by_the_whole_sweep(self):
        pending = [[{'name': 'test', 'status': 'queued', 'conclusion': None}]]
        rows, pulls, scripts = [], {}, []
        for name in ('one', 'two'):
            self.tree(name, {'LICENSE': mit('Copyright (c) 2026 Example Holdings')})
            branch = lp.BRANCH_PREFIX + lp.digest_of({'LICENSE': mit(OURS)})
            pulls[(name, branch)] = [[12, 'open', None]]
            rows.append(repo(name))
            scripts.append((name, branch))
        gh = FakeGh(rows, self.trees, pulls=pulls)
        for name, branch in scripts:
            gh.arm(name, branch, {'LICENSE': mit(OURS)}).check_runs = pending
        code, text = self.sweep(gh, apply=True, merge=True)
        self.assertEqual(sum(self.sleeps), 240)
        self.assertNoMerge(gh)
        self.assertEqual(text.count('checks are still running'), 2)
        self.assertEqual(code, 0)

    def test_merge_queue_enqueues_the_checked_head_and_skips_the_direct_merge(self):
        gh, _sc, _name = self.case()
        gh.scripts['drift'].rules = [{'type': 'pull_request'}, {'type': 'merge_queue'}]
        code, text, status = self.run_merge(gh)
        calls = gh.merge_calls()
        self.assertEqual([(m, p) for m, p, _b in calls], [('POST', 'graphql')])
        self.assertEqual(calls[0][2]['variables'], {'id': 'PR_node_1', 'oid': HEAD_SHA})
        self.assertIn('enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $oid})',
                      calls[0][2]['query'])
        self.assertEqual((code, status), (0, 'queued'))
        self.assertIn('#12 added to the merge queue (1 file(s))', text)

    def test_graphql_errors_refuse(self):
        gh, sc, _name = self.case()
        sc.rules = [{'type': 'merge_queue'}]
        sc.graphql_reply = {'errors': [{'message': 'queue is full\nmore'}]}
        code, text, status = self.run_merge(gh)
        self.assertEqual((code, status), (1, 'pr-open'))
        self.assertIn('#12 not merged: merge refused: queue is full (1 file(s))', text)

    def test_queued_is_only_reported_when_github_names_the_queue_entry(self):
        for reply in ({}, {'data': {'enqueuePullRequest': None}},
                      {'data': {'enqueuePullRequest': {'mergeQueueEntry': None}}},
                      {'data': {'enqueuePullRequest': {'mergeQueueEntry': {'id': ''}}}},
                      {'data': {'enqueuePullRequest': {'mergeQueueEntry': {'id': 7}}}},
                      {'data': {'enqueuePullRequest': {'mergeQueueEntry': 'Q1'}}},
                      {'data': ['Q1']}, {'errors': 'odd'}, {'errors': [None]}):
            with self.subTest(reply=reply):
                gh, sc, _name = self.case()
                sc.rules = [{'type': 'merge_queue'}]
                sc.graphql_reply = reply
                code, text, status = self.run_merge(gh)
                self.assertEqual((code, status), (1, 'pr-open'))
                self.assertIn('#12 not merged: merge refused: GitHub did not confirm a merge '
                              'queue entry; the pull request may be in the queue (1 file(s))', text)

    def test_an_unreadable_answer_to_the_merge_call_says_it_may_have_merged(self):
        unread = ('#12 not merged: merge refused: GitHub\'s answer to the merge call could not be '
                  'read; the pull request may have merged')
        for queue in (False, True):
            for reply in (ValueError('Expecting value: line 1 column 1'), [], None, 'merged', 1):
                with self.subTest(queue=queue, reply=reply):
                    gh, sc, _name = self.case()
                    sc.rules = [{'type': 'merge_queue'}] if queue else []
                    sc.merge_reply = sc.graphql_reply = reply
                    code, text, status = self.run_merge(gh)
                    self.assertEqual((code, status, len(gh.merge_calls())), (1, 'pr-open', 1))
                    self.assertIn(unread, text)

    def test_a_merge_call_that_fails_in_an_unforeseen_way_says_it_may_have_merged(self):
        for queue in (False, True):
            for error in (OSError('boom'), IndexError('boom'), KeyError('boom')):
                with self.subTest(queue=queue, error=error):
                    gh, sc, _name = self.case()
                    sc.rules = [{'type': 'merge_queue'}] if queue else []
                    sc.merge_reply = sc.graphql_reply = error
                    code, text, status = self.run_merge(gh)
                    self.assertEqual((code, status, len(gh.merge_calls())), (1, 'pr-open', 1))
                    self.assertIn('#12 not merged: merge refused: the merge call failed '
                                  'unexpectedly (%r); the pull request may have merged' % (error,),
                                  text)

    def test_merge_call_that_raises_is_refused_with_the_reason(self):
        gh, _sc, _name = self.case()
        gh.fail = {'/pulls/12/merge'}
        code, text, status = self.run_merge(gh)
        self.assertEqual((code, status), (1, 'pr-open'))
        self.assertIn('merge refused: gh api repos/example-org/drift/pulls/12/merge failed: boom '
                      '(1 file(s))', text)
        self.assertNotIn('second line', text)

    def test_merge_response_that_is_not_merged_true_is_refused(self):
        unconfirmed = ('GitHub answered the merge call without confirming it; the pull request '
                       'may have merged')
        for reply, why in (({'merged': False, 'message': 'Head branch was modified'},
                            'Head branch was modified'),
                           ({'merged': False}, 'GitHub did not merge it'),
                           ({'merged': False, 'message': 7}, 'GitHub did not merge it'),
                           ({}, unconfirmed), ({'merged': 'true'}, unconfirmed),
                           ({'merged': 1}, unconfirmed), ({'merged': None}, unconfirmed)):
            with self.subTest(reply=reply):
                gh, sc, _name = self.case()
                sc.merge_reply = reply
                code, text, status = self.run_merge(gh)
                self.assertEqual((code, status), (1, 'pr-open'))
                self.assertIn('#12 not merged: merge refused: %s (1 file(s))' % why, text)

    def test_an_error_of_an_unforeseen_kind_before_the_merge_call_is_refused(self):
        gh, sc, _name = self.case()
        sc.reviews = [{'state': 'APPROVED', 'user': None}]
        self.assertEqual(self.direct(gh, sc, now=NOW).reason,
                         'merge refused: unexpected answer from GitHub (TypeError('
                         '"\'NoneType\' object is not subscriptable"))')
        with mock.patch.object(lp, '_merge_decision', side_effect=OSError('boom')):
            outcome = self.direct(gh, sc, now=NOW)
        self.assertEqual((outcome.status, outcome.reason),
                         ('refused', "merge refused: unexpected error (OSError('boom'))"))
        self.assertNoMerge(gh)

    def test_every_list_read_asks_for_a_hundred_entries(self):
        gh, sc, _name = self.case()
        self.assertEqual(self.run_merge(gh)[2], 'merged')
        api, branch = 'repos/example-org/drift/', sc.pull['head']['ref']
        self.assertEqual(gh.gets, [
            api + 'pulls/12',
            api + 'pulls/12/files?per_page=100',
            api + 'license?ref=' + branch,
            api + 'rules/branches/main?per_page=100',
            api + 'commits/%s/check-runs?per_page=100' % HEAD_SHA,
            api + 'commits/%s/status?per_page=100' % HEAD_SHA,
            api + 'pulls/12/reviews?per_page=100'])

    def test_rules_are_read_for_the_default_branch_whatever_it_is_called(self):
        gh, sc, _name = self.case(default_branch='trunk')
        sc.rules = [required('test')]
        sc.check_runs = [[]]
        _code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual(status, 'pr-open')
        self.assertIn('#12 waits: the required check test has not reported', text)
        self.assertIn('repos/example-org/drift/rules/branches/trunk?per_page=100', gh.gets)

    def test_a_rule_list_too_long_to_read_in_one_page_refuses(self):
        gh, sc, _name = self.case()
        sc.rules = [{'type': 'pull_request'}] * 100
        _code, text, _status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertIn('#12 not merged: merge refused: more than 100 entries at ', text)

    def test_the_license_wanted_on_the_branch_is_the_repository_s_own(self):
        for wanted, seen, merges in (('Apache-2.0', 'Apache-2.0', True), ('Apache-2.0', 'MIT', False),
                                     ('MIT', 'Apache-2.0', False)):
            with self.subTest(wanted=wanted, seen=seen):
                gh, sc, _name = self.case()
                gh.detected_on_branch = seen
                outcome = self.direct(gh, sc, license_wanted=wanted, now=NOW)
                if merges:
                    self.assertEqual(outcome.status, 'merged')
                else:
                    self.assertEqual((outcome.status, outcome.reason, gh.merge_calls()),
                                     ('refused', 'GitHub does not detect %s on the branch' % wanted, []))

    def test_one_repository_s_surprise_does_not_hide_the_rows_of_the_others(self):
        gh, _sc, _name = self.case()
        self.tree('later', {'LICENSE': mit(OURS)})
        gh.repos.append(repo('later'))
        gh.trees = self.trees
        real_clone = gh.clone

        def clone(org, name, dest):
            if name == 'later':
                raise OSError('disk full')
            real_clone(org, name, dest)

        gh.clone = clone
        code, text = self.sweep(gh, apply=True, merge=True)
        self.assertIn('#12 merged (1 file(s))', text)
        self.assertIn('error          OSError: disk full', text)
        self.assertIn('2 public repositories examined, 0 private not examined, 1 need attention.',
                      text)
        self.assertEqual(code, 2)

    def test_a_read_that_fails_is_refused_and_never_merged(self):
        for tail in ('/pulls/12', '/pulls/12/files', '/pulls/12/reviews', '/check-runs',
                     '/status', '/rules/branches/main'):
            with self.subTest(tail):
                gh, _sc, _name = self.case()
                gh.fail = {tail}
                code, text, status = self.run_merge(gh)
                self.assertNoMerge(gh)
                self.assertEqual((code, status), (1, 'pr-open'))
                self.assertIn('#12 not merged: merge refused: gh api ', text)

    def test_an_answer_with_a_missing_field_is_refused(self):
        gh, sc, _name = self.case()
        del sc.pull['head']['sha']
        code, text, status = self.run_merge(gh)
        self.assertNoMerge(gh)
        self.assertEqual((code, status), (1, 'pr-open'))
        self.assertIn('merge refused: unexpected answer from GitHub', text)

    def opened_body(self, first, merge):
        gh, _sc, _name = self.case(first=first, existing=False)
        self.sweep(gh, apply=True, merge=merge)
        return [b for _m, p, b in gh.posts if p.endswith('/pulls')][0]['body']

    def test_body_says_what_will_happen_when_merging_is_on(self):
        first = self.opened_body(first=True, merge=True)
        self.assertIn('The sweep merges this pull request on a later run, once it has been open '
                      'for 20 hours. Close it to stop that.', first)
        self.assertNotIn('Merge only if this code is meant to be open source.', first)
        other = self.opened_body(first=False, merge=True)
        self.assertIn('The sweep merges this pull request itself on a later run, once its checks '
                      'have passed. Close it, or mark it as a draft, to stop that.', other)

    def test_body_is_unchanged_when_merging_is_off(self):
        first = self.opened_body(first=True, merge=False)
        self.assertIn('Merge only if this code is meant to be open source.', first)
        self.assertNotIn('sweep merges', first)
        self.assertNotIn('sweep merges', self.opened_body(first=False, merge=False))
        self.assertNotIn('The sweep merges this pull request itself',
                         self.opened_body(first=True, merge=True))


if __name__ == '__main__':
    unittest.main()
