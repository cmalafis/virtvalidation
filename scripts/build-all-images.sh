#!/usr/bin/env bash
# Build the standard + hardened image matrix for a given tag.
#
# Usage:
#   scripts/build-all-images.sh <tag> [--push] [--standard-only]
#
# Components:
#   backend  -> backend/Containerfile (standard) or backend/Containerfile.hardened
#   frontend -> frontend/Containerfile.runtime (standard) or
#               frontend/Containerfile.runtime.hardened
#
# Variants:
#   standard -> UBI 9 base, digest-pinned in the Containerfile.
#               Always built.
#   hardened -> Red Hat Hardened Image base, supplied via the
#               HARDENED_PYTHON / HARDENED_NGINX env vars. If either
#               env var is empty, that component's hardened build is
#               skipped with a WARN line (the standard build still
#               proceeds). Pass --standard-only to skip both unconditionally.
#
# Tag scheme:
#   quay.io/cmalafis10/virtvalidate-<component>:<tag>            (standard)
#   quay.io/cmalafis10/virtvalidate-<component>:<tag>-hardened   (hardened)
#
# Frontend builds run `npm ci && npm run build` natively before invoking
# podman to bypass the QEMU esbuild segfault on Apple Silicon — matches
# the existing scripts/build-images.sh cross-arch fallback.
#
# See docs/CONTAINER_IMAGES.md for the operator-side service-account
# token recipe required to pull from registry.redhat.io.

set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") <tag> [--push] [--standard-only]

Environment overrides:
  REGISTRY         Default: quay.io/cmalafis10
  PLATFORM         Default: linux/amd64
  HARDENED_PYTHON  registry.redhat.io path:tag for backend hardened.
                   Empty / unset -> skip backend hardened build.
  HARDENED_NGINX   registry.redhat.io path:tag for frontend hardened.
                   Empty / unset -> skip frontend hardened build.

Examples:
  $(basename "$0") v0.2.6
  $(basename "$0") v0.2.6 --push --standard-only
  HARDENED_PYTHON=registry.redhat.io/foo/python-3.12:1.0 \\
      $(basename "$0") v0.2.6 --push
EOF
    exit 1
}

[[ $# -ge 1 ]] || usage
TAG="$1"; shift
PUSH=false
STANDARD_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=true ;;
        --standard-only) STANDARD_ONLY=true ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $arg" >&2; usage ;;
    esac
done

REGISTRY="${REGISTRY:-quay.io/cmalafis10}"
PLATFORM="${PLATFORM:-linux/amd64}"
HARDENED_PYTHON="${HARDENED_PYTHON:-}"
HARDENED_NGINX="${HARDENED_NGINX:-}"
VCS_REF="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer podman, fall back to docker so CI on stock GitHub Actions runners works.
ENGINE="${ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
    if command -v podman >/dev/null 2>&1; then
        ENGINE=podman
    elif command -v docker >/dev/null 2>&1; then
        ENGINE=docker
    else
        echo "ERROR: neither podman nor docker is installed" >&2
        exit 1
    fi
fi

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

build_image() {
    local component="$1" variant="$2" containerfile="$3" context="$4"
    shift 4
    local image_tag="$TAG"
    [[ "$variant" == "hardened" ]] && image_tag="${TAG}-hardened"
    local image="${REGISTRY}/virtvalidate-${component}:${image_tag}"

    log "Building ${image}"
    log "  containerfile: ${containerfile}"
    log "  context:       ${context}"
    "$ENGINE" build --platform "$PLATFORM" --no-cache \
        --build-arg "IMAGE_VERSION=${image_tag}" \
        --build-arg "VCS_REF=${VCS_REF}" \
        "$@" \
        -t "$image" \
        -f "$containerfile" "$context"

    if [[ "$PUSH" == true ]]; then
        log "Pushing ${image}"
        "$ENGINE" push "$image"
    fi
}

# Frontend always needs a native build first — both standard and hardened
# variants of Containerfile.runtime expect frontend/dist/ to exist.
log "Native npm build (frontend dist/)"
( cd frontend && npm ci && npm run build )

# ---- Standard builds (always run) ----
build_image backend standard backend/Containerfile .
build_image frontend standard frontend/Containerfile.runtime ./frontend

# ---- Hardened builds (unless --standard-only) ----
if [[ "$STANDARD_ONLY" == true ]]; then
    log "Skipping hardened variants (--standard-only)"
else
    if [[ -n "$HARDENED_PYTHON" ]]; then
        build_image backend hardened backend/Containerfile.hardened . \
            --build-arg "BASE_IMAGE=${HARDENED_PYTHON}"
    else
        warn "HARDENED_PYTHON unset — skipping backend hardened build"
    fi

    if [[ -n "$HARDENED_NGINX" ]]; then
        build_image frontend hardened frontend/Containerfile.runtime.hardened ./frontend \
            --build-arg "BASE_IMAGE=${HARDENED_NGINX}"
    else
        warn "HARDENED_NGINX unset — skipping frontend hardened build"
    fi
fi

log "Done. Standard tag: ${TAG}"
[[ "$STANDARD_ONLY" == false ]] && log "      Hardened tag: ${TAG}-hardened (if its base was set)"
