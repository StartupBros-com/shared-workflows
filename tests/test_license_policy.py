"""Tests for licensing/license_policy.py.

The planner tests build real trees on disk and run the real planner. The sweep
tests replace only the GitHub calls with a recorded fake; they prove the
sweep's decisions, not GitHub's behaviour, which is checked by a live run.
"""
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
import unittest

ROOT = Path(__file__).resolve().parents[1]
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


def policy(repos=None, **holder):
    return lp.Policy(
        name=holder.get('name', 'Example Holdings LLC'),
        brand=holder.get('brand', 'Example Brand'),
        url=holder.get('url', 'https://example.test'),
        aliases=('Example Holdings', 'Example Holdings LLC', 'Example Holdings / Example Brand'),
        license='MIT', owner_logins=frozenset({'owner'}), org='example-org', repos=repos or {})


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


class OwnRepositoryTests(unittest.TestCase):
    def test_this_repository_conforms_to_its_own_policy(self):
        plan = lp.plan_tree(str(ROOT), lp.load_policy(), 'shared-workflows', lp.Facts(year='2026'))
        self.assertEqual([(f.level, f.path, f.message) for f in plan.findings], [])


class FakeGh:
    """Recorded GitHub answers; records every write the sweep attempts."""

    def __init__(self, repos, trees, contributors=None, pulls=None, detected_on_branch='MIT'):
        self.repos, self.trees = repos, trees
        self.contributors = contributors or {}
        self.pulls = pulls or {}
        self.detected_on_branch = detected_on_branch
        self.posts = []
        self.cloned = []

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
        raise AssertionError('unexpected read: ' + path)

    def get(self, path):
        return {'license': {'spdx_id': self.detected_on_branch}}

    def post(self, path, body, method='POST'):
        self.posts.append((method, path, body))
        if path.endswith('/pulls'):
            return {'number': 7}
        return {'sha': 'f' * 40}

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


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='license-sweep-test-')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.trees = {}

    def tree(self, name, files):
        root = Path(self.base, 'src', name)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.trees[name] = str(root)

    def sweep(self, gh, apply=False, repos=None):
        out = io.StringIO()
        work = tempfile.mkdtemp(prefix='work-', dir=self.base)
        code = lp.sweep(gh, policy(repos), apply=apply, out=out, workdir=work)
        return code, out.getvalue()

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


if __name__ == '__main__':
    unittest.main()
