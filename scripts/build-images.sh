#!/usr/bin/env bash
# Build (and optionally push) VirtValidate container images.
#
# Usage:
#   scripts/build-images.sh [-r registry] [-v version] [-p] [-m] [-b backend|frontend|all]
#
#   -r registry  Image registry prefix (default: ghcr.io/virtvalidate).
#   -v version   Version tag (default: derived from git describe).
#   -p           Push to registry after build.
#   -m           Build multi-arch (amd64 + arm64) via buildx.
#                Requires Docker buildx or Podman 4.0+ with --platform.
#                Skipped by default — local builds target the host arch.
#   -b target    Which image(s) to build (backend, frontend, all). Default: all.
#
# Tagged images:
#   <registry>/virtvalidate-<component>:<version>
#   <registry>/virtvalidate-<component>:<git-sha>     (always)
#   <registry>/virtvalidate-<component>:latest        (push-only, on main)
#
# This script is the canonical local equivalent of the release-images.yml
# CI workflow — keep them in sync. CI uses Docker buildx; locally we
# default to single-arch builds because most dev hosts don't have buildx
# wired up.

set -euo pipefail

REGISTRY="ghcr.io/virtvalidate"
PUSH=0
MULTIARCH=0
TARGET="all"
VERSION=""

usage() {
    sed -n '2,/^$/p' "$0"
    exit 1
}

while getopts ":r:v:b:pmh" opt; do
    case "$opt" in
        r) REGISTRY="$OPTARG" ;;
        v) VERSION="$OPTARG" ;;
        b) TARGET="$OPTARG" ;;
        p) PUSH=1 ;;
        m) MULTIARCH=1 ;;
        h) usage ;;
        \?) echo "Unknown option: -$OPTARG" >&2; usage ;;
        :) echo "Option -$OPTARG requires an argument" >&2; usage ;;
    esac
done

# Repository root — the Containerfiles reference paths relative to it.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Default version: git describe with --dirty so uncommitted changes are
# obvious in the image label. Tag-style versions like v0.1.0 are
# stripped of the leading "v" to match SemVer-on-the-image conventions.
if [[ -z "$VERSION" ]]; then
    VERSION="$(git describe --tags --dirty --always 2>/dev/null || echo "0.0.0-unknown")"
    VERSION="${VERSION#v}"
fi
VCS_REF="$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")"

# Detect the build engine. Podman is preferred (matches our deployment
# story); fall back to docker if podman isn't available.
if command -v podman >/dev/null 2>&1; then
    ENGINE="podman"
elif command -v docker >/dev/null 2>&1; then
    ENGINE="docker"
else
    echo "ERROR: neither podman nor docker is on PATH" >&2
    exit 2
fi

# Multi-arch builds require buildx (docker) or qemu-user-static + podman
# manifest. We don't try to install either — operators with multi-arch
# needs configure their host ahead of time.
PLATFORMS=""
if [[ $MULTIARCH -eq 1 ]]; then
    PLATFORMS="linux/amd64,linux/arm64"
    if [[ "$ENGINE" == "docker" ]] && ! docker buildx version >/dev/null 2>&1; then
        echo "ERROR: -m requires Docker buildx (docker buildx version)" >&2
        exit 3
    fi
fi

build_one() {
    local name="$1"          # virtvalidate-backend / virtvalidate-frontend
    local containerfile="$2" # backend/Containerfile / frontend/Containerfile
    local context="$3"       # repo root for backend, frontend dir for frontend

    local image="${REGISTRY}/${name}"
    local tags=(
        "${image}:${VERSION}"
        "${image}:${VCS_REF}"
    )

    echo "==> Building ${name}"
    echo "    version : ${VERSION}"
    echo "    git ref : ${VCS_REF}"
    echo "    tags    : ${tags[*]}"

    local tag_args=()
    for t in "${tags[@]}"; do
        tag_args+=("-t" "$t")
    done

    if [[ -n "$PLATFORMS" ]]; then
        # buildx (docker) or `podman build --platform` — both accept the
        # same flag. For multi-arch with podman, run inside `podman
        # manifest` first if the operator wants a single multi-arch tag.
        "$ENGINE" build \
            --platform "$PLATFORMS" \
            --build-arg "VERSION=${VERSION}" \
            --build-arg "VCS_REF=${VCS_REF}" \
            -f "$containerfile" \
            "${tag_args[@]}" \
            "$context"
    else
        "$ENGINE" build \
            --build-arg "VERSION=${VERSION}" \
            --build-arg "VCS_REF=${VCS_REF}" \
            -f "$containerfile" \
            "${tag_args[@]}" \
            "$context"
    fi

    if [[ $PUSH -eq 1 ]]; then
        for t in "${tags[@]}"; do
            echo "==> Pushing ${t}"
            "$ENGINE" push "$t"
        done
        # The `latest` tag is push-only — we never apply it to local
        # images since it would overwrite an in-progress dev build.
        local latest="${image}:latest"
        echo "==> Tagging + pushing ${latest}"
        "$ENGINE" tag "${image}:${VERSION}" "${latest}"
        "$ENGINE" push "${latest}"
    fi
}

case "$TARGET" in
    backend)
        build_one "virtvalidate-backend" "backend/Containerfile" "."
        ;;
    frontend)
        build_one "virtvalidate-frontend" "frontend/Containerfile" "frontend"
        ;;
    all)
        build_one "virtvalidate-backend" "backend/Containerfile" "."
        build_one "virtvalidate-frontend" "frontend/Containerfile" "frontend"
        ;;
    *)
        echo "ERROR: unknown target '$TARGET' (expected backend|frontend|all)" >&2
        exit 4
        ;;
esac

echo "==> Done"
