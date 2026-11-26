#!/usr/bin/env python3
"""Apply and check the org license policy.

    license_policy.py check  <tree> [--repo NAME]
    license_policy.py render <tree> [--repo NAME]
    license_policy.py sweep  [--apply] [--only NAME ...]

`check` and `render` share one planner, so what `check` reports is exactly what
`render` writes. `sweep` runs the planner over every public repository in the
org and, with --apply, opens one pull request per repository that differs. When
policy.toml also sets `merge = true` under [sweep], --apply merges the pull
requests it opened, once a list of guards all hold (see try_merge). No pull
request merges in the run that opened it or within 10 minutes of its last
change, and a first LICENSE only merges once it has been open for 20 hours.
Without --apply, or without that switch, the sweep never merges anything.

Rules the planner never breaks:
  * a copyright line naming someone other than the policy holder is left alone;
  * a repository's kind of license is never changed;
  * a directory that already holds any license-like file gains no new one;
  * a first LICENSE is only written when the sweep has checked commit authors.

Exit codes: 0 conforms (a pull request the sweep will merge on a later run
counts as conforming), 1 differs but fixable, or a pull request was refused or
has waited three days, 2 needs a decision recorded in policy.toml.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    sys.exit('license_policy.py needs Python 3.11 or newer (tomllib).')

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLICY = os.path.join(HERE, 'policy.toml')
MIT_TEMPLATE_PATH = os.path.join(HERE, 'texts', 'MIT.txt')

WRITABLE = ('MIT',)                 # licenses this tool can write
KEEPABLE = ('MIT', 'Apache-2.0')    # licenses a repository may keep
LICENSE_LIKE = re.compile(r'^(licen[cs]e|copying|unlicense)([.\-_].*)?$', re.IGNORECASE)
MANAGED_NAMES = ('LICENSE', 'LICENSE.md', 'LICENSE.txt')
MIT_TITLES = ('mit license', 'the mit license', 'the mit license (mit)')
COPYRIGHT_RE = re.compile(
    r'^(?P<lead>\s*)copyright\b\s*(?:\(c\)|©)?\s*'
    r'(?P<years>\d{4}(?:\s*[-,–]\s*\d{4})*)?[\s,]*(?P<holder>\S.*?)\s*$',
    re.IGNORECASE)
BRANCH_PREFIX = 'chore/license-policy-'
SKIP_DIRS = ('.git', 'node_modules')
FIRST_LICENSE_HOLD_HOURS = 20       # a first LICENSE never merges younger than this
SETTLE_MINUTES = 10                 # nothing merges this soon after its last change
STALE_HOURS = 72                    # a pull request still waiting after this is red
UNNAMED = 'NOASSERTION'             # GitHub found license files and names no license
WAIT_TOTAL_SECONDS = 240            # one budget for checks, shared by the whole sweep
WAIT_STEP_SECONDS = 15


class PolicyError(ValueError):
    """policy.toml is malformed."""


@dataclasses.dataclass(frozen=True)
class RepoPolicy:
    hands_off: bool
    reason: str
    license: str
    subtrees: tuple


@dataclasses.dataclass(frozen=True)
class Policy:
    name: str
    brand: str
    url: str
    aliases: tuple
    license: str
    owner_logins: frozenset
    org: str
    repos: dict
    merge: bool = False
    refix_after_days: int | None = None

    @property
    def holder_text(self):
        extra = ', '.join(x for x in (self.brand, self.url) if x)
        return '%s (%s)' % (self.name, extra) if extra else self.name

    def owns(self, holder):
        """True when a copyright line's holder is the policy holder.

        A trailing parenthetical is ignored so that a line written under an
        earlier brand or URL is still recognised and brought up to date.
        """
        names = set(self.aliases) | {self.name}
        base = re.sub(r'\s*\([^()]*\)$', '', holder).strip()
        return holder in names or base in names

    def for_repo(self, repo):
        entry = self.repos.get(repo, {})
        return RepoPolicy(
            hands_off=entry.get('mode') == 'hands-off',
            reason=entry.get('reason', ''),
            license=entry.get('license', self.license),
            subtrees=tuple(entry.get('subtrees', ())))


def _only(table, allowed, where):
    extra = sorted(set(table) - set(allowed))
    if extra:
        raise PolicyError('%s: unknown key(s) %s' % (where, ', '.join(extra)))


def load_policy(path=DEFAULT_POLICY):
    with open(path, 'rb') as fh:
        raw = tomllib.load(fh)
    _only(raw, ('holder', 'defaults', 'provenance', 'sweep', 'repos'), 'policy')
    holder = raw.get('holder', {})
    _only(holder, ('name', 'brand', 'url', 'aliases'), '[holder]')
    if not holder.get('name'):
        raise PolicyError('[holder] name is required')
    defaults = raw.get('defaults', {})
    _only(defaults, ('license',), '[defaults]')
    license_id = defaults.get('license', 'MIT')
    if license_id not in WRITABLE:
        raise PolicyError('[defaults] license must be one of %s' % ', '.join(WRITABLE))
    provenance = raw.get('provenance', {})
    _only(provenance, ('owner_logins',), '[provenance]')
    sweep = raw.get('sweep', {})
    _only(sweep, ('org', 'merge', 'refix_after_days'), '[sweep]')
    merge = sweep.get('merge', False)
    if not isinstance(merge, bool):
        raise PolicyError('[sweep] merge must be true or false')
    refix = sweep.get('refix_after_days')
    if refix is not None and (isinstance(refix, bool) or not isinstance(refix, int) or refix < 1):
        raise PolicyError('[sweep] refix_after_days must be a whole number of days, 1 or more')
    repos = raw.get('repos', {})
    for name, entry in repos.items():
        where = '[repos.%s]' % name
        _only(entry, ('mode', 'reason', 'license', 'subtrees'), where)
        if entry.get('mode', 'hands-off') != 'hands-off':
            raise PolicyError('%s: mode can only be "hands-off"' % where)
        if entry.get('mode') == 'hands-off' and not entry.get('reason'):
            raise PolicyError('%s: a hands-off entry needs a reason' % where)
        if entry.get('license', license_id) not in KEEPABLE:
            raise PolicyError('%s: license must be one of %s' % (where, ', '.join(KEEPABLE)))
    return Policy(
        name=holder['name'], brand=holder.get('brand', ''), url=holder.get('url', ''),
        aliases=tuple(holder.get('aliases', ())), license=license_id,
        owner_logins=frozenset(provenance.get('owner_logins', ())),
        org=sweep.get('org', ''), repos=repos, merge=merge, refix_after_days=refix)


def _norm(text):
    return ' '.join(text.split())


def mit_template():
    with open(MIT_TEMPLATE_PATH, encoding='utf-8') as fh:
        return fh.read()


def _mit_body(template):
    start = template.lower().index('permission is hereby granted')
    return _norm(template[start:])


@dataclasses.dataclass
class Notice:
    index: int
    lead: str
    years: str
    holder: str
    eol: str


def parse_mit(text, template):
    """Return (lines, notices) for a plain MIT license file, else None.

    Only the lines above "Permission is hereby granted" are read as copyright
    notices, so a body line that happens to start with "COPYRIGHT HOLDERS" in
    some other wrapping is never mistaken for one.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if 'permission is hereby granted' in line.lower()), None)
    if start is None or _norm(''.join(lines[start:])) != _mit_body(template):
        return None
    notices = []
    for i, raw in enumerate(lines[:start]):
        line = raw.rstrip('\r\n')
        if not line.strip() or line.strip().lower() in MIT_TITLES:
            continue
        match = COPYRIGHT_RE.match(line)
        if not match:
            return None
        notices.append(Notice(i, match.group('lead'), match.group('years') or '',
                              match.group('holder'), raw[len(line):]))
    return lines, notices


def classify(text, template):
    if parse_mit(text, template) is not None:
        return 'MIT'
    low = _norm(text).lower()
    if 'apache license' in low and 'version 2.0, january 2004' in low:
        return 'Apache-2.0'
    return 'unrecognised'


def canonical_mit(policy, template, years):
    lines = []
    for year in years:
        line = 'Copyright (c) %s %s' % (year, policy.holder_text)
        if line not in lines:
            lines.append(line)
    return template.replace('{{copyright}}', '\n'.join(lines))


def render_mit(text, policy, template, default_year):
    """Bring an MIT file's own copyright lines up to the policy.

    Returns (state, new_text, other_holders):
      'ours'    every notice is the policy holder's; new_text is the whole
                canonical file
      'mixed'   some notices are someone else's; only ours change, in place
      'foreign' no notice is ours; new_text is None
      'no-mit'  not a plain MIT file, or it has no notice; new_text is None
    """
    parsed = parse_mit(text, template)
    if parsed is None or not parsed[1]:
        return 'no-mit', None, []
    lines, notices = parsed
    ours = [n for n in notices if policy.owns(n.holder)]
    others = [n.holder for n in notices if not policy.owns(n.holder)]
    if not ours:
        return 'foreign', None, others
    if not others:
        return 'ours', canonical_mit(policy, template, [n.years or default_year for n in ours]), []
    for n in ours:
        lines[n.index] = '%sCopyright (c) %s %s%s' % (
            n.lead, n.years or default_year, policy.holder_text, n.eol)
    return 'mixed', ''.join(lines), others


@dataclasses.dataclass
class Facts:
    """What the planner cannot read from the tree."""
    year: str
    sole_owner: object = None   # callable returning (bool, why), or None when unknown


@dataclasses.dataclass
class Finding:
    level: str      # 'fix' (render writes it) or 'decide' (needs a policy entry)
    path: str
    message: str


@dataclasses.dataclass
class Plan:
    writes: dict = dataclasses.field(default_factory=dict)
    findings: list = dataclasses.field(default_factory=list)
    hands_off: str = ''
    first_license: bool = False

    @property
    def decisions(self):
        return [f for f in self.findings if f.level == 'decide']

    def exit_code(self):
        return 2 if self.decisions else (1 if self.writes else 0)


def tracked_files(tree):
    """Regular files in the tree: git's tracked list when it is a checkout."""
    if os.path.exists(os.path.join(tree, '.git')):
        out = subprocess.run(['git', '-C', tree, 'ls-files', '-z'], check=True,
                             capture_output=True).stdout.decode('utf-8', 'surrogateescape')
        names = [p for p in out.split('\0') if p]
    else:
        names = []
        for base, dirs, files in os.walk(tree):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in files:
                names.append(os.path.relpath(os.path.join(base, name), tree).replace(os.sep, '/'))
    keep = []
    for rel in sorted(names):
        full = os.path.join(tree, rel)
        if os.path.isfile(full) and not os.path.islink(full):
            keep.append(rel)
    return keep


def _read(tree, rel):
    with open(os.path.join(tree, rel), encoding='utf-8', errors='surrogateescape', newline='') as fh:
        return fh.read()


def _match_dir(path, pattern):
    a, b = path.split('/'), pattern.strip('/').split('/')
    return len(a) == len(b) and all(fnmatch.fnmatchcase(x, y) for x, y in zip(a, b))


def _manifest_license(tree, files):
    """Yield (path, declared license) for root manifests that declare one."""
    if 'package.json' in files:
        try:
            value = json.loads(_read(tree, 'package.json')).get('license')
        except (ValueError, AttributeError):
            value = None
        if isinstance(value, str):
            yield 'package.json', value
    if 'pyproject.toml' in files:
        try:
            value = tomllib.loads(_read(tree, 'pyproject.toml')).get('project', {}).get('license')
        except (tomllib.TOMLDecodeError, AttributeError):
            value = None
        if isinstance(value, dict):
            value = value.get('text')
        if isinstance(value, str):
            yield 'pyproject.toml', value


def plan_tree(tree, policy, repo, facts, template=None):
    """Compare a tree with the policy. Pure: reads the tree, writes nothing."""
    template = template or mit_template()
    rp = policy.for_repo(repo)
    plan = Plan()
    if rp.hands_off:
        plan.hands_off = rp.reason
        return plan

    def decide(path, message):
        plan.findings.append(Finding('decide', path, message))

    def write(path, text, message):
        plan.writes[path] = text
        plan.findings.append(Finding('fix', path, message))

    files = tracked_files(tree)
    fileset = set(files)
    root_like = [f for f in files if '/' not in f and LICENSE_LIKE.match(f)]
    root_name = next((n for n in MANAGED_NAMES if n in fileset), None)
    root_text = None    # canonical root LICENSE, when the root is entirely ours

    if not root_like:
        if rp.license not in WRITABLE:
            decide('LICENSE', 'missing, and this tool does not write %s' % rp.license)
        elif 'UPSTREAM.md' in fileset:
            decide('LICENSE', 'missing, and UPSTREAM.md says the code came from elsewhere; '
                              'add a [repos.%s] entry' % repo)
        else:
            ok, why = facts.sole_owner() if facts.sole_owner else (
                False, 'commit authors are only checked by the sweep')
            if ok:
                root_text = canonical_mit(policy, template, [facts.year])
                write('LICENSE', root_text, 'missing; add the %s license' % rp.license)
                plan.first_license = True
            else:
                decide('LICENSE', 'missing, and not written: %s' % why)
    elif root_name is None:
        decide(root_like[0], 'license-like file present but no LICENSE; '
                             'add a [repos.%s] entry' % repo)
    else:
        text = _read(tree, root_name)
        kind = classify(text, template)
        if kind != rp.license:
            decide(root_name, 'is %s but the policy says %s; add a [repos.%s] entry to keep it'
                   % (kind, rp.license, repo))
        elif kind == 'MIT':
            state, new, others = render_mit(text, policy, template, facts.year)
            if state == 'no-mit':
                decide(root_name, 'has no copyright line')
            elif state == 'foreign':
                decide(root_name, 'names %s, not the policy holder; add a [repos.%s] entry '
                                  'or an alias' % (' and '.join(repr(o) for o in others), repo))
            else:
                if state == 'ours':
                    root_text = new
                if new != text:
                    write(root_name, new, 'copyright line differs from the policy')

    if rp.license == 'MIT':
        for rel in files:
            if '/' not in rel or os.path.basename(rel) not in MANAGED_NAMES:
                continue
            text = _read(tree, rel)
            state, new, _others = render_mit(text, policy, template, facts.year)
            if state in ('ours', 'mixed') and new != text:
                write(rel, new, 'copyright line differs from the policy')

    if root_text is not None and rp.subtrees:
        dirs = sorted({rel.rsplit('/', n)[0] for rel in files
                       for n in range(1, rel.count('/') + 1)})
        for sub in dirs:
            if not any(_match_dir(sub, pattern) for pattern in rp.subtrees):
                continue
            inside = [f for f in files if f.startswith(sub + '/') and '/' not in f[len(sub) + 1:]]
            if not any(LICENSE_LIKE.match(os.path.basename(f)) for f in inside):
                write(sub + '/LICENSE', root_text, 'missing; copy the root LICENSE')

    for path, declared in _manifest_license(tree, fileset):
        if declared != rp.license:
            decide(path, 'declares %r but the policy license is %s' % (declared, rp.license))
    return plan


def apply_plan(tree, plan):
    for rel, text in plan.writes.items():
        full = os.path.join(tree, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8', errors='surrogateescape', newline='') as fh:
            fh.write(text)


def print_plan(plan, out=sys.stdout):
    if plan.hands_off:
        print('hands-off: %s' % plan.hands_off, file=out)
    for f in plan.findings:
        print('%-6s %s: %s' % (f.level, f.path, f.message), file=out)
    if not plan.findings and not plan.hands_off:
        print('conforms', file=out)


class Gh:
    """The GitHub calls the sweep makes, kept in one place so tests can swap it."""

    def run(self, *args, body=None):
        proc = subprocess.run(
            ['gh', 'api', *args] + (['--input', '-'] if body is not None else []),
            input=json.dumps(body) if body is not None else None,
            capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError('gh api %s failed: %s' % (' '.join(args), proc.stderr.strip()))
        return proc.stdout

    def lines(self, path, jq):
        out = self.run('--paginate', path, '--jq', jq)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def get(self, path):
        return json.loads(self.run(path))

    def post(self, path, body, method='POST'):
        return json.loads(self.run('-X', method, path, body=body))

    def clone(self, org, name, dest):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        proc = subprocess.run(
            ['git', 'clone', '--quiet', '--depth', '1',
             'https://github.com/%s/%s.git' % (org, name), dest],
            capture_output=True, text=True, env=env, check=False)
        if proc.returncode != 0:
            raise RuntimeError('clone of %s failed: %s' % (name, proc.stderr.strip()))


def _git(tree, *args):
    return subprocess.run(['git', '-C', tree, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def digest_of(writes):
    blob = json.dumps(sorted(writes.items()), ensure_ascii=False).encode('utf-8', 'surrogateescape')
    return hashlib.sha256(blob).hexdigest()[:10]


def pr_body(policy, plan, detected, same_as_base=False, again=None):
    lines = ['Opened by the org license sweep (`licensing/` in shared-workflows).', '',
             'What changes:', '']
    for f in plan.findings:
        if f.level == 'fix':
            lines.append('- `%s`: %s' % (f.path, f.message))
    if again:
        lines += ['', 'This repeats #%d, which made the same change. The file has been changed '
                      'again since then. The sweep makes the same change at most once every %d '
                      'days. To keep the file as it is now, close this pull request: the sweep '
                      'then opens no more of them and reports the difference instead.'
                  % (again, policy.refix_after_days)]
    lines += ['', 'The copyright line the policy writes is:', '',
              '    Copyright (c) <year> %s' % policy.holder_text, '',
              'The license text is the unmodified standard text.']
    if same_as_base:
        lines[-1] += (' GitHub names no license on this branch, and none on the commit it '
                      'starts from when that is asked for by id: it names one here only for '
                      'the default branch, as it does when the root holds a second license '
                      'file. This change does not alter that.')
    elif not plan.first_license:
        lines[-1] += ' GitHub detects `%s` on this branch.' % (detected or 'nothing')
    else:
        # Observed 2026-11-25 on three repositories: GitHub answers 404 for a
        # branch's license while the default branch has none.
        lines[-1] += (' GitHub does not report a branch\'s license while the default branch has '
                      'none, so the next sweep after the merge confirms detection.')
        hold_sentence = ('Merge only if this code is meant to be open source.'
                         if not policy.merge else
                         'The sweep merges this pull request on a later run, once it has been '
                         'open for %d hours. Close it to stop that.' % FIRST_LICENSE_HOLD_HOURS)
        lines += ['', 'Every commit author here is an owner login or a bot, and the tree has no '
                      '`UPSTREAM.md`.',
                  '', '**This repository had no license.** Merging grants the %s license to '
                      'everyone, and that grant cannot be withdrawn for copies already taken. %s'
                  % (policy.license, hold_sentence)]
    if policy.merge and not plan.first_license:
        lines += ['', 'The sweep merges this pull request itself on a later run, once its checks '
                      'have passed. Close it, or mark it as a draft, to stop that.']
    lines += ['', 'To change what the sweep does here, edit `licensing/policy.toml` in '
                  'shared-workflows.']
    return '\n'.join(lines) + '\n'


@dataclasses.dataclass
class Row:
    name: str
    status: str
    detail: str = ''
    findings: list = dataclasses.field(default_factory=list)
    red: bool = False


def sole_owner_check(gh, policy, org, name):
    def check():
        rows = gh.lines('repos/%s/%s/contributors?anon=1&per_page=100' % (org, name),
                        '.[] | [.type, (.login // .name // "")]')
        if not rows:
            return False, 'the repository has no commits'
        others = sorted({who for kind, who in rows
                         if not (kind == 'Bot' or (kind == 'User' and who in policy.owner_logins))})
        if others:
            shown = ', '.join(others[:3]) + (' and more' if len(others) > 3 else '')
            return False, 'commit authors outside the owner list (%s); add a [repos.%s] entry' % (
                shown, name)
        return True, ''
    return check


def _detected(gh, api, ref):
    """The license GitHub names for one ref, or None when the read fails."""
    try:
        return (gh.get('%s/license?ref=%s' % (api, ref)).get('license') or {}).get('spdx_id')
    except RuntimeError:
        return None


def branch_detection(gh, policy, repo, api, plan, branch, base_sha, template=None):
    """What GitHub names on a license branch, and whether that stops the change.

    Returns (seen, accepted). The branch should read as the repository's policy
    license, and one layout cannot. With a second license file in the root
    (REPORT-LICENSE.md beside LICENSE) GitHub names the license only for the
    default branch asked for by name. Every other ref reads NOASSERTION, the
    default branch's own commit asked for by id included (seen 2026-11-26 on
    mac-studio-model-evidence and pi-evals-evidence). There a change cannot
    move GitHub's answer, so it cannot be asked to confirm one. The answer is
    accepted only when all of this holds:

    - the branch reads exactly NOASSERTION, and so does the commit it was cut
      from, asked for by id: a read that fails never equals another;
    - GitHub names the policy license on the default branch;
    - the policy license is MIT and every file the plan writes is the exact
      MIT text by this tool's own parse, which is what stands in for GitHub's
      confirmation. The Apache check is a phrase match and cannot.
    """
    want = policy.for_repo(repo['name']).license
    seen = _detected(gh, api, branch)
    if seen == want:
        return seen, True
    template = template or mit_template()
    accepted = (seen == UNNAMED and want == 'MIT' and repo.get('license') == want
                and all(parse_mit(text, template) is not None for text in plan.writes.values())
                and bool(base_sha) and _detected(gh, api, base_sha) == UNNAMED)
    return seen, accepted


def open_pull_request(gh, policy, org, repo, tree, plan, branch, template=None, again=None):
    """Create the branch and pull request through the API.

    Returns (number, detected, accepted): what GitHub names on the new branch
    and whether branch_detection accepts it. Both are None for a first license.
    `again` is the merged pull request this one repeats, if any.
    """
    name = repo['name']
    base_sha = _git(tree, 'rev-parse', 'HEAD')
    base_tree = _git(tree, 'rev-parse', 'HEAD^{tree}')
    entries = []
    for rel, text in sorted(plan.writes.items()):
        staged = _git(tree, 'ls-files', '-s', '--', rel)
        entries.append({'path': rel, 'mode': staged.split()[0] if staged else '100644',
                        'type': 'blob', 'content': text})
    api = 'repos/%s/%s' % (org, name)
    existing = gh.lines('%s/git/matching-refs/heads/%s' % (api, branch), '.[] | .ref')
    if 'refs/heads/' + branch not in existing:
        new_tree = gh.post(api + '/git/trees', {'base_tree': base_tree, 'tree': entries})
        commit = gh.post(api + '/git/commits', {
            'message': 'chore: apply the org license policy', 'tree': new_tree['sha'],
            'parents': [base_sha]})
        gh.post(api + '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit['sha']})
    detected = accepted = None
    if not plan.first_license:
        detected, accepted = branch_detection(gh, policy, repo, api, plan, branch, base_sha,
                                              template)
    pull = gh.post(api + '/pulls', {
        'title': 'chore: apply the org license policy', 'head': branch,
        'base': repo['default_branch'],
        'body': pr_body(policy, plan, detected,
                        same_as_base=bool(accepted) and detected == UNNAMED, again=again)})
    stale = gh.lines(api + '/pulls?state=open&per_page=100',
                     '.[] | select(.head.repo.full_name == "%s/%s") | [.number, .head.ref]'
                     % (org, name))
    for number, ref in stale:
        if ref.startswith(BRANCH_PREFIX) and ref != branch:
            gh.post('%s/issues/%d/comments' % (api, number),
                    {'body': 'Superseded by #%d: the policy changed.' % pull['number']})
            gh.post('%s/pulls/%d' % (api, number), {'state': 'closed'}, method='PATCH')
    return pull['number'], detected, accepted


class WaitBudget:
    """Seconds the whole sweep may spend waiting for checks, spent in fixed steps."""

    def __init__(self, sleep=None, total=WAIT_TOTAL_SECONDS, step=WAIT_STEP_SECONDS):
        self.sleep, self.left, self.step = sleep or time.sleep, total, step

    def spend(self):
        """Sleep one step and return True, or return False when the budget is gone."""
        if self.left < self.step:
            return False
        self.left -= self.step
        self.sleep(self.step)
        return True


@dataclasses.dataclass
class MergeOutcome:
    status: str         # 'merged', 'queued', 'waiting' or 'refused'
    reason: str = ''
    stale: bool = False  # waiting, and the pull request is older than STALE_HOURS


def blob_id(text):
    """The git blob id of a file's text: what GitHub reports as a file's sha."""
    data = text.encode('utf-8', 'surrogateescape')
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def _parse_time(text):
    return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(
        tzinfo=datetime.timezone.utc)


def _refused(text):
    lines = str(text).strip().splitlines()
    return MergeOutcome('refused', 'merge refused: ' + (lines[0][:200] if lines else 'unknown error'))


def _named(exc):
    """An exception as text, whatever its own repr does."""
    try:
        return repr(exc)
    except Exception:
        return type(exc).__name__


def _github_said_no(error):
    """Whether the text of a failed gh call shows that GitHub answered and refused.

    gh prints GitHub's own answer as "gh: <message> (HTTP <status>)", or as
    "gh: <message>" for a GraphQL error. A 5xx, a network failure or anything
    else does not say whether a write happened.
    """
    said = str(error).partition(' failed: ')[2]
    return said.startswith('gh: ') and not re.search(r'\bHTTP 5\d\d\b', said)


def _read_list(gh, path, key=None):
    """One full page of a list endpoint. A page that may be cut short is an error,
    because a check or review that was not read must not count as a pass."""
    data = gh.get(path + ('&' if '?' in path else '?') + 'per_page=100')
    items = data[key] if key else data
    if len(items) >= 100 or (key and data.get('total_count', len(items)) > len(items)):
        raise RuntimeError('more than 100 entries at %s' % path)
    return items


def _check_state(gh, api, sha, required=()):
    """('ok'|'pending'|'failed', the failed check or what is awaited) for a commit.

    `required` names the checks the default branch requires. GitHub answers the
    same for a required check that has not reported yet as for a repository
    with no checks at all, so a required check that is absent counts as
    pending here, never as nothing to wait for.
    """
    runs = _read_list(gh, '%s/commits/%s/check-runs' % (api, sha), 'check_runs')
    # The combined status endpoint's .statuses array lists each context once; its
    # top-level .state reads 'pending' for a commit that has no statuses at all.
    statuses = _read_list(gh, '%s/commits/%s/status' % (api, sha), 'statuses')
    pending, reported = False, set()
    for run in runs:
        reported.add(run['name'])
        if run['status'] != 'completed':
            pending = True
        elif run['conclusion'] not in ('success', 'neutral', 'skipped'):
            return 'failed', run['name']
    for status in statuses:
        reported.add(status['context'])
        if status['state'] == 'pending':
            pending = True
        elif status['state'] != 'success':
            return 'failed', status['context']
    if pending:
        return 'pending', 'checks are still running'
    absent = sorted(set(required) - reported)
    if absent:
        return 'pending', 'the required check %s has not reported' % absent[0]
    return 'ok', ''


def _changes_requested_by(gh, api, number):
    """A reviewer whose latest decisive review asks for changes, else ''."""
    latest = {}
    for review in _read_list(gh, '%s/pulls/%d/reviews' % (api, number)):
        if review['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[review['user']['login']] = review['state']
    return next((who for who, state in latest.items() if state == 'CHANGES_REQUESTED'), '')


def _pull_refusal(pull, org, repo, branch):
    """Why this is not the sweep's own open pull request against the default branch, else ''."""
    head = pull['head']
    if pull['state'] != 'open':
        return 'it is not open'
    if pull['draft']:
        return 'it is a draft'
    if pull['base']['ref'] != repo['default_branch']:
        return 'it targets %s, not %s' % (pull['base']['ref'], repo['default_branch'])
    if (head['ref'] != branch
            or (head.get('repo') or {}).get('full_name') != '%s/%s' % (org, repo['name'])):
        return 'its branch is not the sweep\'s own branch'
    return ''


def _dig(value, *keys):
    """value[k1][k2]... through dicts only; None as soon as a step is not a dict."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _merge_decision(gh, policy, org, repo, plan, branch, number, decisions, budget, now,
                    opened_now, template=None):
    name = repo['name']
    api = 'repos/%s/%s' % (org, name)
    first_wait = ('first license: merges once the pull request is %d hours old'
                  % FIRST_LICENSE_HOLD_HOURS)
    merged_doubt = 'the pull request may have merged'
    queued_doubt = 'the pull request may be in the queue'

    def refuse(why):
        return MergeOutcome('refused', why)

    def send(path, body, doubt, method='POST'):
        """The merge or enqueue call. GitHub's own refusal is raised as it stands.
        Any other failure leaves the outcome unknown, and its reason leads with
        the doubt, because _refused keeps only the first 200 characters."""
        try:
            reply = gh.post(path, body, method=method)
        except RuntimeError as exc:
            if _github_said_no(exc):
                raise
            raise RuntimeError('%s: %s' % (doubt, exc)) from None
        except ValueError:
            raise RuntimeError('%s: GitHub\'s answer could not be read' % doubt) from None
        except Exception as exc:
            raise RuntimeError('%s: the call failed unexpectedly (%s)'
                               % (doubt, _named(exc))) from None
        if not isinstance(reply, dict):
            raise RuntimeError('%s: GitHub\'s answer could not be read' % doubt)
        return reply

    if decisions:
        return refuse('a decision is open')
    if opened_now:
        # Never in the run that opened it. Checks start a moment after a pull
        # request exists, and until they do GitHub reports none, which reads
        # the same as all green. A first license is also owed a day in which
        # someone can close it. This does not depend on any clock.
        return MergeOutcome('waiting', first_wait if plan.first_license else
                            'opened in this run: a later run merges it')
    pull = gh.get('%s/pulls/%d' % (api, number))
    why = _pull_refusal(pull, org, repo, branch)
    if why:
        return refuse(why)
    opened, changed = _parse_time(pull['created_at']), _parse_time(pull['updated_at'])
    if changed < opened:
        return refuse('its last change is dated before it was opened')
    age = now - opened
    stale = age > datetime.timedelta(hours=STALE_HOURS)
    if plan.first_license and age < datetime.timedelta(hours=FIRST_LICENSE_HOLD_HOURS):
        return MergeOutcome('waiting', first_wait, stale)
    # Measured from the last change, not from when it was opened: a new commit
    # on an old pull request ("Update branch") has no checks yet either, and
    # GitHub moves updated_at when the branch moves.
    if now - changed < datetime.timedelta(minutes=SETTLE_MINUTES):
        return MergeOutcome('waiting', 'opened or changed less than %d minutes ago: a later '
                            'run merges it' % SETTLE_MINUTES, stale)
    sha = pull['head']['sha']
    files = _read_list(gh, '%s/pulls/%d/files' % (api, number))
    if (sorted(f['filename'] for f in files) != sorted(plan.writes)
            or any(f['status'] not in ('added', 'modified') for f in files)
            or any(f['sha'] != blob_id(plan.writes[f['filename']]) for f in files)):
        return refuse('its files no longer match the plan')
    if not plan.first_license:
        _seen, accepted = branch_detection(gh, policy, repo, api, plan, branch,
                                           (pull.get('base') or {}).get('sha'), template)
        if not accepted:
            return refuse('GitHub does not detect %s on the branch'
                          % policy.for_repo(name).license)
    rules = _read_list(gh, '%s/rules/branches/%s' % (api, repo['default_branch']))
    required = [check['context'] for rule in rules if rule.get('type') == 'required_status_checks'
                for check in rule['parameters']['required_status_checks']]
    waited = False
    while True:
        state, detail = _check_state(gh, api, sha, required)
        if state == 'failed':
            return refuse('a check failed: %s' % detail)
        if state == 'ok':
            break
        if not budget.spend():
            return MergeOutcome('waiting', detail, stale)
        waited = True
    if waited:
        # Minutes may have passed. Look at the pull request again before acting on it.
        pull = gh.get('%s/pulls/%d' % (api, number))
        why = _pull_refusal(pull, org, repo, branch) or (
            '' if pull['head']['sha'] == sha else 'its branch changed while its checks ran')
        if why:
            return refuse(why)
    # Read last, so a review left while the checks ran is seen.
    who = _changes_requested_by(gh, api, number)
    if who:
        return refuse('changes were requested by %s' % who)
    if any(rule.get('type') == 'merge_queue' for rule in rules):
        reply = send('graphql', {
            'query': 'mutation($id: ID!, $oid: GitObjectID!) { enqueuePullRequest(input: '
                     '{pullRequestId: $id, expectedHeadOid: $oid}) { mergeQueueEntry { id } } }',
            'variables': {'id': pull['node_id'], 'oid': sha}}, queued_doubt)
        entry = _dig(reply, 'data', 'enqueuePullRequest', 'mergeQueueEntry', 'id')
        if isinstance(entry, str) and entry:
            return MergeOutcome('queued')
        errors = reply.get('errors')
        said = _dig(errors[0], 'message') if isinstance(errors, list) and errors else None
        return _refused(said if isinstance(said, str) and said else
                        queued_doubt + ': GitHub did not confirm a merge queue entry')
    reply = send('%s/pulls/%d/merge' % (api, number),
                 {'merge_method': 'squash', 'sha': sha}, merged_doubt, method='PUT')
    if reply.get('merged') is True:
        return MergeOutcome('merged')
    if reply.get('merged') is False:
        said = reply.get('message')
        return _refused(said if isinstance(said, str) and said else 'GitHub did not merge it')
    return _refused(merged_doubt + ': GitHub answered the merge call without confirming it')


def try_merge(gh, policy, org, repo, plan, branch, number, decisions, budget, now=None,
              opened_now=False, template=None):
    """Merge the sweep's own pull request when every guard holds.

    A wrong merge of a first LICENSE grants MIT to the world and cannot be
    undone, so the first guard that fails decides, and any read that fails or
    answers oddly counts as a refusal, never as a pass.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        return _merge_decision(gh, policy, org, repo, plan, branch, number, decisions, budget,
                               now, opened_now, template)
    except RuntimeError as exc:
        return _refused(exc)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        return _refused('unexpected answer from GitHub (%s)' % _named(exc))
    except Exception as exc:    # nothing may escape: the rows already made must still print
        return _refused('unexpected error (%s)' % _named(exc))


def refix_due(policy, merged_at, now):
    """(due, note) for a change whose newest pull request merged at `merged_at`
    and whose file differs again.

    The sweep makes the same change at most once in policy.refix_after_days
    days. This is a limit on how often, not a judgement of why the file
    changed: a file changed straight back is reported until the days are up,
    then changed again. Without the setting nothing is ever due. A merge date
    that cannot be read is never due, and neither is one in the future.
    """
    days = policy.refix_after_days
    if not days:
        return False, ''
    try:
        due_at = _parse_time(merged_at) + datetime.timedelta(days=days)
    except (TypeError, ValueError, OverflowError):
        return False, ''
    if now >= due_at:
        return True, ''
    return False, ': the sweep makes the change again from %s' % due_at.strftime('%Y-%m-%d %H:%M UTC')


def left_over(gh, api, branch, number):
    """Whether the branch still stands exactly where merged pull request `number` left it.

    Such a branch cannot carry the change again. Its commit starts from the
    default branch as it was before that merge, so a second pull request from
    it conflicts with the edit made since. A branch that stands anywhere else
    was made by a later run whose pull request then failed to open, and
    open_pull_request uses it as it is.
    """
    tips = dict(gh.lines('%s/git/matching-refs/heads/%s' % (api, branch),
                         '.[] | [.ref, .object.sha]'))
    tip = tips.get('refs/heads/' + branch)
    return tip is not None and tip == gh.get('%s/pulls/%d' % (api, number))['head']['sha']


def merge_row(name, open_status, number, outcome, summary, first_license, decisions):
    if outcome.status == 'merged':
        return Row(name, 'merged', '#%d merged (%s)%s' % (
            number, summary, ', first license' if first_license else ''), decisions)
    if outcome.status == 'queued':
        return Row(name, 'queued', '#%d added to the merge queue (%s)' % (number, summary),
                   decisions)
    if outcome.status == 'waiting':
        return Row(name, open_status, '#%d waits: %s (%s)' % (number, outcome.reason, summary),
                   decisions, red=outcome.stale)
    return Row(name, open_status, '#%d not merged: %s (%s)' % (number, outcome.reason, summary),
               decisions, red=True)


def sweep_repo(gh, policy, org, repo, workdir, apply, template, budget=None, now=None):
    name = repo['name']
    rp = policy.for_repo(name)
    detected = repo.get('license')
    merging = apply and policy.merge
    budget = budget or WaitBudget()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if repo.get('archived'):
        return Row(name, 'skipped', 'archived')
    if repo.get('fork'):
        return Row(name, 'skipped', 'fork')
    if rp.hands_off:
        return Row(name, 'hands-off', '%s (GitHub detects %s)' % (rp.reason, detected or 'nothing'))
    if not repo.get('size'):
        return Row(name, 'skipped', 'empty repository')
    tree = os.path.join(workdir, name)
    gh.clone(org, name, tree)
    facts = Facts(year=str(repo['created_at'])[:4], sole_owner=sole_owner_check(gh, policy, org, name))
    plan = plan_tree(tree, policy, name, facts, template)
    decisions = list(plan.decisions)
    if not plan.writes:
        if not decisions and detected != rp.license:
            decisions.append(Finding('decide', 'LICENSE', 'conforms, but GitHub detects %s instead of %s'
                                     % (detected or 'nothing', rp.license)))
        if decisions:
            return Row(name, 'needs-decision', '', decisions, red=True)
        return Row(name, 'ok', 'GitHub detects %s' % detected)

    branch = BRANCH_PREFIX + digest_of(plan.writes)
    api = 'repos/%s/%s' % (org, name)
    pulls = gh.lines('%s/pulls?state=all&head=%s:%s&per_page=100' % (api, org, branch),
                     '.[] | [.number, .state, .merged_at]')
    summary = '%d file(s)' % len(plan.writes)
    for number, state, _merged in pulls:
        if state == 'open':
            if merging:
                outcome = try_merge(gh, policy, org, repo, plan, branch, number, decisions,
                                    budget, now, template=template)
                return merge_row(name, 'pr-open', number, outcome, summary,
                                 plan.first_license, decisions)
            return Row(name, 'pr-open', '#%d waits for a merge (%s)' % (number, summary),
                       decisions, red=True)
    again = None
    if pulls:
        # None is open. The newest one decides: a pull request closed without
        # merging is a person saying no, and stays final however old it is.
        number, _state, merged = max(pulls, key=lambda row: row[0])
        if not merged:
            why = '#%d was closed without merging; add a [repos.%s] entry or reopen it' % (
                number, name)
        else:
            due, note = refix_due(policy, merged, now)
            why = '#%d merged but the tree still differs%s' % (number, note)
            if due and left_over(gh, api, branch, number):
                why = ('#%d merged and the tree differs again, but its branch %s is still '
                       'there; delete that branch and the sweep makes the change again'
                       % (number, branch))
            elif due:
                why, again = '', number
        if why:
            return Row(name, 'needs-decision', '',
                       decisions + [Finding('decide', 'LICENSE', why)], red=True)
        summary += ', again after #%d' % again
    if not apply:
        return Row(name, 'would-open-pr', summary, plan.findings, red=True)
    number, seen, accepted = open_pull_request(gh, policy, org, repo, tree, plan, branch,
                                               template, again=again)
    if not plan.first_license and not accepted:
        decisions.append(Finding('decide', 'LICENSE', 'GitHub detects %s on %s instead of %s'
                                 % (seen or 'nothing', branch, rp.license)))
    if merging:
        outcome = try_merge(gh, policy, org, repo, plan, branch, number, decisions, budget, now,
                            opened_now=True, template=template)
        return merge_row(name, 'pr-opened', number, outcome, summary, plan.first_license,
                         decisions)
    return Row(name, 'pr-opened', '#%d (%s)%s' % (
        number, summary, ', first license: merge by hand' if plan.first_license else ''),
        decisions, red=True)


def sweep(gh, policy, apply=False, only=(), out=sys.stdout, workdir=None, now=None,
          sleep=None):
    org = policy.org
    repos = gh.lines(
        'orgs/%s/repos?per_page=100&type=all' % org,
        '.[] | {name, private, fork, archived, size, default_branch, created_at, '
        'license: (.license.spdx_id // null)}')
    private = sum(1 for r in repos if r['private'])
    public = sorted((r for r in repos if not r['private']), key=lambda r: r['name'].lower())
    if only:
        public = [r for r in public if r['name'] in only]
    template = mit_template()
    own_dir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix='license-sweep-')
    budget = WaitBudget(sleep)
    rows = []
    try:
        for repo in public:
            try:
                rows.append(sweep_repo(gh, policy, org, repo, workdir, apply, template,
                                       budget, now))
            except (RuntimeError, subprocess.CalledProcessError) as exc:
                rows.append(Row(repo['name'], 'error', str(exc), red=True))
            except Exception as exc:
                # One repository's surprise must not hide what the run already
                # did to the others, which may include a merge.
                rows.append(Row(repo['name'], 'error', '%s: %s' % (type(exc).__name__, exc),
                                red=True))
    finally:
        if own_dir:
            shutil.rmtree(workdir, ignore_errors=True)
    width = max([len(r.name) for r in rows] + [4])
    for row in rows:
        print('%-*s  %-14s %s' % (width, row.name, row.status, row.detail), file=out)
        for f in row.findings:
            print('%-*s    %-6s %s: %s' % (width, '', f.level, f.path, f.message), file=out)
    red = [r for r in rows if r.red]
    print(file=out)
    print('%d public repositories examined, %d private not examined, %d need attention.'
          % (len(rows), private, len(red)), file=out)
    if any(r.status in ('needs-decision', 'error') or
           any(f.level == 'decide' for f in r.findings) for r in rows):
        return 2
    return 1 if red else 0


def repo_name(tree, given):
    if given:
        return given
    try:
        url = _git(tree, 'remote', 'get-url', 'origin')
    except (subprocess.CalledProcessError, OSError):
        url = ''
    tail = url.rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1].removesuffix('.git')
    return tail or os.path.basename(os.path.abspath(tree))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--policy', default=DEFAULT_POLICY)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'render'):
        cmd = sub.add_parser(name)
        cmd.add_argument('tree')
        cmd.add_argument('--repo', help='repository name (default: from the origin remote)')
    cmd = sub.add_parser('sweep')
    cmd.add_argument('--apply', action='store_true', help='open pull requests, and merge them when policy.toml sets '
                          '[sweep] merge = true and every guard holds')
    cmd.add_argument('--only', action='append', default=[], metavar='NAME')
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
    except (PolicyError, tomllib.TOMLDecodeError) as exc:
        print('policy error: %s' % exc, file=sys.stderr)
        return 2
    if args.command == 'sweep':
        return sweep(Gh(), policy, apply=args.apply, only=args.only)
    facts = Facts(year=str(datetime.date.today().year))
    plan = plan_tree(args.tree, policy, repo_name(args.tree, args.repo), facts)
    if args.command == 'render':
        apply_plan(args.tree, plan)
        for f in plan.findings:
            print('%-6s %s: %s' % ('wrote' if f.level == 'fix' else f.level, f.path, f.message))
        if not plan.findings:
            print('hands-off: %s' % plan.hands_off if plan.hands_off else 'conforms')
        return 2 if plan.decisions else 0
    print_plan(plan)
    return plan.exit_code()


if __name__ == '__main__':
    sys.exit(main())
