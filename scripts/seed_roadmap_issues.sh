#!/usr/bin/env bash
# Seed three roadmap issues + supporting labels + the v1.0.0 milestone.
#
# Idempotent: labels and the milestone are check-then-create, and issue
# creation is dedupe-checked by exact-title match (open + closed) so a
# second run will not silently produce duplicates.
#
# Issue bodies live as standalone Markdown files under
# scripts/roadmap-issues/ — easier to review/edit than embedded heredocs,
# and side-steps macOS bash 3.2's broken heredoc-inside-$(…) parsing.
#
# Prereqs:
#   brew install gh         # or your platform's equivalent
#   gh auth login           # one-time interactive login
#
# Usage:
#   bash scripts/seed_roadmap_issues.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if ! command -v gh >/dev/null 2>&1; then
  echo "error: 'gh' CLI not on PATH. Install with: brew install gh" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "error: gh is not authenticated. Run: gh auth login" >&2
  exit 1
fi

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
if [ -z "$REPO" ]; then
  echo "error: could not resolve current repo via 'gh repo view'." >&2
  echo "  Run this from inside the virtvalidation repo." >&2
  exit 1
fi
echo "→ target repo: $REPO"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BODIES_DIR="$SCRIPT_DIR/roadmap-issues"
if [ ! -d "$BODIES_DIR" ]; then
  echo "error: missing $BODIES_DIR" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Labels — create any that don't exist yet
# ---------------------------------------------------------------------------
ensure_label() {
  local name="$1"
  local color="$2"
  local description="$3"
  if gh label list --repo "$REPO" --limit 200 --json name -q '.[].name' \
      | grep -Fxq "$name"; then
    echo "  · label exists: $name"
  else
    gh label create "$name" \
      --repo "$REPO" \
      --color "$color" \
      --description "$description" \
      >/dev/null
    echo "  + label created: $name"
  fi
}

echo "→ ensuring labels"
ensure_label "enhancement"    "a2eeef" "New feature or capability"
ensure_label "documentation"  "0075ca" "Docs only"
ensure_label "testing"        "d4a017" "Tests, CI, QA"
ensure_label "area: backend"  "1d76db" "Python / FastAPI / SSH / LLM"
ensure_label "area: docs"     "5319e7" "Markdown docs + product map"
ensure_label "federal"        "0b3a5b" "Federal / DoD / regulated environments"
ensure_label "roadmap"        "b58900" "Tracked on the public roadmap"

# ---------------------------------------------------------------------------
# Milestone — v1.0.0 (referenced by issue 2)
# ---------------------------------------------------------------------------
echo "→ ensuring milestone v1.0.0"
MILESTONE_NUM=$(
  gh api "repos/$REPO/milestones?state=all" \
    --jq '.[] | select(.title=="v1.0.0") | .number' || true
)
if [ -z "$MILESTONE_NUM" ]; then
  MILESTONE_NUM=$(
    gh api -X POST "repos/$REPO/milestones" \
      -f title="v1.0.0" \
      -f description="GA target — Linux + Windows support, RBAC, multi-tenant" \
      --jq .number
  )
  echo "  + milestone created: v1.0.0 (#$MILESTONE_NUM)"
else
  echo "  · milestone exists: v1.0.0 (#$MILESTONE_NUM)"
fi

# ---------------------------------------------------------------------------
# Issue creation — dedupe by exact title match across open + closed
# ---------------------------------------------------------------------------
create_issue() {
  local title="$1"
  local labels="$2"     # comma-separated
  local milestone="$3"  # may be empty
  local body_file="$4"

  if [ ! -f "$body_file" ]; then
    echo "error: body file not found: $body_file" >&2
    return 1
  fi

  local existing
  existing=$(
    gh issue list --repo "$REPO" --state all --limit 200 \
        --json number,title \
        --jq ".[] | select(.title==\"$title\") | .number" \
      || true
  )
  if [ -n "$existing" ]; then
    echo "  · issue exists, skipping: #$existing  $title"
    return 0
  fi

  local args=(
    issue create
    --repo "$REPO"
    --title "$title"
    --label "$labels"
    --body-file "$body_file"
  )
  if [ -n "$milestone" ]; then
    args+=(--milestone "$milestone")
  fi

  local url
  url=$(gh "${args[@]}")
  echo "  + created: $url"
}

echo "→ creating issues"

create_issue \
  "Make Linux SSH collector OS-aware (RHEL 7/8/9/10 support)" \
  "enhancement,area: backend,federal" \
  "" \
  "$BODIES_DIR/01-rhel-os-aware-collector.md"

create_issue \
  "Windows VM support (Server 2019, 2022, 2025)" \
  "enhancement,federal,roadmap,area: backend" \
  "v1.0.0" \
  "$BODIES_DIR/02-windows-vm-support.md"

create_issue \
  "OS compatibility matrix and testing infrastructure" \
  "documentation,testing,area: docs" \
  "" \
  "$BODIES_DIR/03-os-compat-matrix.md"

echo
echo "Done."
