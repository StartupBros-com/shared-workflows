#!/usr/bin/env bash
# Tier classification fixture for dependency-autopilot.yml.
#
# The classifier lives in a workflow `run:` block, which nothing could execute
# in isolation, so its behavior was only ever reasoned about. This mirrors that
# logic and pins it against REAL PR titles (the six security PRs open on
# pushbot when this was written) plus the shapes that must not regress.
#
# Keep in lockstep with the `--- classify risk tier ---` block in
# .github/workflows/dependency-autopilot.yml. If you change one, change both;
# the point is that a misclassification shows up here, not in production.
set -uo pipefail

classify() {
  local title="$1" BRANCH="$2" tier reason
  tier="review"; reason="unclassified"
  if [[ "$BRANCH" == *"minor-and-patch"* || "$BRANCH" == *"github-actions"* || "$BRANCH" == *"github_actions"* ]]; then
    tier="safe"; reason="grouped minor/patch"
  elif [[ "$title" == *"[SECURITY]"* ]]; then
    tier="review"
    if [[ "$title" =~ @[^0-9]*([0-9]+)\.[0-9]+.*\ to\ \>?=?v?([0-9]+) ]]; then
      sfM="${BASH_REMATCH[1]}"; stM="${BASH_REMATCH[2]}"
      if [ "$sfM" != "$stM" ]; then
        reason="security advisory, major line ${sfM}.x -> ${stM}.x — human review"
      else
        reason="security advisory within ${sfM}.x — human review"
      fi
    else
      reason="security advisory — human review"
    fi
  elif [[ "$title" =~ from\ ([0-9]+)\.([0-9]+)\.[0-9]+.*to\ ([0-9]+)\.([0-9]+)\.[0-9]+ ]]; then
    fM="${BASH_REMATCH[1]}"; fm="${BASH_REMATCH[2]}"; tM="${BASH_REMATCH[3]}"; tm="${BASH_REMATCH[4]}"
    if [ "$fM" != "$tM" ]; then
      tier="review"; reason="major bump ${fM}.x -> ${tM}.x"
    elif [ "$fM" = "0" ] && [ "$fm" != "$tm" ]; then
      tier="review"; reason="0.x minor (${fM}.${fm} -> ${tM}.${tm}) — semver allows breaking"
    else
      tier="safe"; reason="single non-major bump"
    fi
  fi
  printf '%-7s %s\n' "$tier" "$reason"
}

fails=0
check() { # label expected_tier expected_reason_substr title branch
  local out; out="$(classify "$4" "$5")"
  local tier="${out%% *}"
  if [[ "$tier" == "$2" && "$out" == *"$3"* ]]; then
    printf 'ok   - %s\n       -> %s\n' "$1" "$out"
  else
    printf 'FAIL - %s\n       got: %s\n' "$1" "$out"; fails=$((fails+1))
  fi
}

echo "== real open pushbot security PRs (were all 'unclassified'):"
check '#1570 next 16.1->16.3 (same major)' review 'within 16.x' \
  'chore(deps): Update dependency next@>=16.1.0-canary.0 <16.1.5 to >=16.3.0 [SECURITY]' 'renovate/npm-next-vulnerability'
check '#1568 next 15.6->16.3 (major line)' review '15.x -> 16.x' \
  'chore(deps): Update dependency next@>=15.6.0-canary.0 <16.1.5 to >=16.3.0 [SECURITY]' 'renovate/npm-next-vulnerability'
check '#1567 hono 4.11->4.13' review 'within 4.x' \
  'chore(deps): Update dependency hono@<4.11.7 to >=4.13.1 [SECURITY]' 'renovate/npm-hono-vulnerability'
check '#1565 postcss 8.5->8.5' review 'within 8.x' \
  'chore(deps): Update dependency postcss@<8.5.18 to >=8.5.26 [SECURITY]' 'renovate/npm-postcss-vulnerability'
check '#1564 node-server v2 (no from-version)' review 'security advisory' \
  'chore(deps): Update dependency @hono/node-server@<1.19.13 to v2 [SECURITY]' 'renovate/npm-node-server-vulnerability'

echo
echo "== must not regress:"
check 'grouped minor/patch stays safe' safe 'grouped minor/patch' \
  'chore(deps): bump the minor-and-patch group' 'renovate/minor-and-patch'
check 'ordinary patch stays safe' safe 'single non-major' \
  'chore(deps): bump lodash from 4.17.20 to 4.17.21' 'dependabot/npm_and_yarn/lodash-4.17.21'
check 'ordinary major stays review' review 'major bump 3.x -> 4.x' \
  'chore(deps): bump express from 3.1.0 to 4.0.0' 'dependabot/npm_and_yarn/express-4.0.0'
check '0.x minor stays review' review '0.x minor' \
  'chore(deps): bump foo from 0.3.1 to 0.4.0' 'dependabot/npm_and_yarn/foo-0.4.0'
check 'GHA underscore branch stays safe' safe 'grouped minor/patch' \
  'chore(deps): bump actions/checkout from 4 to 5' 'dependabot/github_actions/actions/checkout-5'

echo
[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILURES"; exit 1; }
