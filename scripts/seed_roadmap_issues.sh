#!/usr/bin/env bash
# Seed roadmap issues + supporting labels + milestones.
#
# Idempotent: labels and milestones are check-then-create, and issue
# creation is dedupe-checked by exact-title match (open + closed) so a
# second run will not silently produce duplicates. The "expansion"
# entries (e.g. notifications) post a comment on the existing issue
# when found and create the issue with the expansion text as the body
# when no match exists.
#
# Issue bodies live as standalone Markdown files under
# scripts/roadmap-issues/ — easier to review/edit than embedded
# heredocs, and side-steps macOS bash 3.2's broken heredoc-inside-$(…)
# parsing.
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
ensure_label "enhancement"     "a2eeef" "New feature or capability"
ensure_label "documentation"   "0075ca" "Docs only"
ensure_label "testing"         "d4a017" "Tests, CI, QA"
ensure_label "area: backend"   "1d76db" "Python / FastAPI / SSH / LLM"
ensure_label "area: frontend"  "61dafb" "React / Vite / dashboard UI"
ensure_label "area: docs"      "5319e7" "Markdown docs + product map"
ensure_label "area: infra"     "0e8a16" "Containerfiles, Helm, Quadlet, OpenShift"
ensure_label "federal"         "0b3a5b" "Federal / DoD / regulated environments"
ensure_label "roadmap"         "b58900" "Tracked on the public roadmap"

# ---------------------------------------------------------------------------
# Milestones — v1.0.0 + v3.0.0
# ---------------------------------------------------------------------------
ensure_milestone() {
  local title="$1"
  local description="$2"
  local num
  num=$(
    gh api "repos/$REPO/milestones?state=all" \
      --jq ".[] | select(.title==\"$title\") | .number" || true
  )
  if [ -z "$num" ]; then
    num=$(
      gh api -X POST "repos/$REPO/milestones" \
        -f title="$title" \
        -f description="$description" \
        --jq .number
    )
    echo "  + milestone created: $title (#$num)"
  else
    echo "  · milestone exists: $title (#$num)"
  fi
}

echo "→ ensuring milestones"
ensure_milestone "v1.0.0" "GA target — Linux + Windows support, RBAC, multi-tenant"
ensure_milestone "v3.0.0" "Enterprise — OpenShift-native deployment, KServe, ArgoCD"

# ---------------------------------------------------------------------------
# Issue creation — dedupe by exact title match across open + closed
# ---------------------------------------------------------------------------
find_issue_number() {
  # Echoes the number of the first issue (open or closed) whose title
  # matches the argument exactly. Empty string if none.
  local title="$1"
  gh issue list --repo "$REPO" --state all --limit 200 \
      --json number,title \
      --jq ".[] | select(.title==\"$title\") | .number" \
    | head -n 1 || true
}

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
  existing=$(find_issue_number "$title")
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

# Comment-or-create — used for "expand existing issue" entries. If a
# match is found, post the body file as a comment. If not, create a new
# issue with the expansion text as the body so the content isn't lost.
expand_issue() {
  local title="$1"
  local labels="$2"
  local milestone="$3"
  local body_file="$4"

  if [ ! -f "$body_file" ]; then
    echo "error: body file not found: $body_file" >&2
    return 1
  fi

  local existing
  existing=$(find_issue_number "$title")
  if [ -n "$existing" ]; then
    gh issue comment "$existing" --repo "$REPO" --body-file "$body_file" >/dev/null
    echo "  + commented on: #$existing  $title"
    return 0
  fi

  echo "  · no existing issue — creating fresh"
  create_issue "$title" "$labels" "$milestone" "$body_file"
}

echo "→ creating issues"

# ---------- 2026-04-29 batch (v0.1.0-alpha → v1.0.0 roadmap items) ----------

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

# ---------- 2026-04-29 batch (Elyra/KServe inspiration — non-pivot) ---------

create_issue \
  "Git integration for generated artifacts" \
  "enhancement,federal,area: backend" \
  "" \
  "$BODIES_DIR/04-git-integration.md"

create_issue \
  "Visual pipeline view in dashboard" \
  "enhancement,area: frontend" \
  "" \
  "$BODIES_DIR/05-visual-pipeline-view.md"

# Notifications — expand-or-create. The original issue is expected to
# already exist in the tracker; if it doesn't, create it with the
# expansion content as the body.
expand_issue \
  "Notification integrations (Slack, email, webhooks)" \
  "enhancement,area: backend" \
  "" \
  "$BODIES_DIR/06-notifications-expand.md"

create_issue \
  "VirtValidate Enterprise — OpenShift-native deployment" \
  "enhancement,roadmap,area: infra" \
  "v3.0.0" \
  "$BODIES_DIR/07-enterprise-openshift.md"

echo
echo "Done."
