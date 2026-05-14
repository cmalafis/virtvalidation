#!/usr/bin/env bash
# Build (and optionally push) VirtValidate container images.
#
# Usage:
#   scripts/build-images.sh [-r registry] [-v version] [-p] [-m] [-s] [-b backend|frontend|all]
#
#   -r registry  Image registry prefix (default: ghcr.io/virtvalidate).
#   -v version   Version tag (default: derived from git describe).
#   -p           Push to registry after build.
#   -m           Build multi-arch (amd64 + arm64) via buildx.
#                Requires Docker buildx or Podman 4.0+ with --platform.
#                Skipped by default — local builds target the host arch.
#   -s           Scan built images with trivy (or grype) before push.
#                Fails the build if HIGH/CRITICAL vulnerabilities show up.
#                Skipped by default; CI typically sets -s.
#   -b target    Which image(s) to build (backend, frontend, all). Default: all.
#
# Tagged images:
#   <registry>/virtvalidate-<component>:<version>
#   <registry>/virtvalidate-<component>:<git-sha>     (always)
#   <registry>/virtvalidate-<component>:latest        (push-only, on main)
#
# Base images:
#   Both Containerfiles use Red Hat UBI 9. The script preflight-pulls
#   each base so a build never fails 5 minutes in because the registry
#   was unreachable. UBI is published unauthenticated at
#   registry.access.redhat.com — no Red Hat subscription required to
#   pull, only to receive errata. See docs/CONTAINER_IMAGES.md for
#   the rationale and what to do if the registry is unreachable in an
#   air-gapped lab.
#
# This script is the canonical local equivalent of the release-images.yml
# CI workflow — keep them in sync. CI uses Docker buildx; locally we
# default to single-arch builds because most dev hosts don't have buildx
# wired up.
#
# Platform handling:
#   - The OpenShift fleet runs on amd64, so we default the target
#     platform to linux/amd64 even on Apple Silicon hosts. Override
#     with PLATFORM=linux/arm64 (or PLATFORM="" to skip the flag).
#   - On Apple Silicon, vite's esbuild dep segfaults under QEMU during
#     the multi-stage build. The script auto-falls-back to a
#     two-step path: build dist/ on the host natively, then package
#     it via frontend/Containerfile.runtime which only carries
#     nginx + dist/. The user-visible image is identical; the build
#     just skips the QEMU leg.
#   Examples:
#     ./scripts/build-images.sh                   # → linux/amd64 (default)
#     PLATFORM=linux/arm64 ./scripts/build-images.sh
#     PLATFORM= ./scripts/build-images.sh         # native, no --platform

set -euo pipefail

REGISTRY="ghcr.io/virtvalidate"
PUSH=0
MULTIARCH=0
SCAN=0
TARGET="all"
VERSION=""

# Base images consumed by the Containerfiles. Keep in sync with both
# backend/Containerfile and frontend/Containerfile.
UBI_BASE_IMAGES=(
    "registry.access.redhat.com/ubi9/python-312:latest"
    "registry.access.redhat.com/ubi9/nodejs-20:latest"
    "registry.access.redhat.com/ubi9/nginx-124:latest"
)

usage() {
    sed -n '2,/^$/p' "$0"
    exit 1
}

while getopts ":r:v:b:pmsh" opt; do
    case "$opt" in
        r) REGISTRY="$OPTARG" ;;
        v) VERSION="$OPTARG" ;;
        b) TARGET="$OPTARG" ;;
        p) PUSH=1 ;;
        m) MULTIARCH=1 ;;
        s) SCAN=1 ;;
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

# Auto-detect target platform. OpenShift fleets we ship to are amd64,
# so we default to linux/amd64 regardless of host architecture. Apple
# Silicon dev hosts cross-compile via QEMU under the hood; the
# frontend build automatically switches to the runtime-only path
# below to bypass the vite/esbuild QEMU crash.
#
# Override via `PLATFORM=...` in the environment. Set `PLATFORM=` (empty)
# to skip --platform entirely (native build, no cross-compile).
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
    arm64|aarch64) DEFAULT_PLATFORM="linux/amd64" ;;
    x86_64|amd64)  DEFAULT_PLATFORM="linux/amd64" ;;
    *)             DEFAULT_PLATFORM="" ;;
esac
PLATFORM="${PLATFORM-${DEFAULT_PLATFORM}}"
if [[ -n "$PLATFORM" ]]; then
    PLATFORM_ARG=(--platform "$PLATFORM")
else
    PLATFORM_ARG=()
fi

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

preflight_bases() {
    # Pre-pull every UBI base so a build never fails 5 minutes in
    # because the registry was unreachable. Skipped on multi-arch
    # builds because buildx pulls per-platform on its own.
    if [[ $MULTIARCH -eq 1 ]]; then
        return 0
    fi
    echo "==> Preflight: pulling UBI base images"
    for img in "${UBI_BASE_IMAGES[@]}"; do
        echo "    - ${img}"
        if ! "$ENGINE" pull "$img"; then
            echo "ERROR: failed to pull ${img}." >&2
            echo "       Check connectivity to registry.access.redhat.com" >&2
            echo "       (no auth required, but the registry must be reachable)." >&2
            echo "       For air-gapped lab builds, mirror the UBI images first" >&2
            echo "       — see docs/CONTAINER_IMAGES.md." >&2
            exit 5
        fi
    done
}

scan_image() {
    local image="$1"
    if [[ $SCAN -ne 1 ]]; then
        return 0
    fi
    if command -v trivy >/dev/null 2>&1; then
        echo "==> Scanning ${image} with trivy (HIGH/CRITICAL only)"
        trivy image --severity HIGH,CRITICAL --exit-code 1 --no-progress "$image"
        return $?
    fi
    if command -v grype >/dev/null 2>&1; then
        echo "==> Scanning ${image} with grype (HIGH/CRITICAL only)"
        grype "$image" --fail-on high
        return $?
    fi
    echo "WARNING: -s requested but neither trivy nor grype is on PATH" >&2
    echo "         Install one before relying on the scan gate:" >&2
    echo "           brew install aquasecurity/trivy/trivy" >&2
    echo "           brew install grype" >&2
    return 0
}

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
            --build-arg "IMAGE_VERSION=${VERSION}" \
            --build-arg "VCS_REF=${VCS_REF}" \
            -f "$containerfile" \
            "${tag_args[@]}" \
            "$context"
    else
        # Single-platform build. PLATFORM_ARG resolves to either
        # ``--platform linux/amd64`` (default on Apple Silicon + amd64
        # hosts targeting OpenShift) or nothing (native build). The
        # ``${arr[@]+"${arr[@]}"}`` dance keeps ``set -u`` happy when
        # the array is empty (bash 4.x quirk).
        "$ENGINE" build \
            ${PLATFORM_ARG[@]+"${PLATFORM_ARG[@]}"} \
            --build-arg "IMAGE_VERSION=${VERSION}" \
            --build-arg "VCS_REF=${VCS_REF}" \
            -f "$containerfile" \
            "${tag_args[@]}" \
            "$context"
    fi

    # Run the optional vulnerability scan against the freshly built
    # version tag. On scan failure the script exits non-zero before
    # any push so we never publish an image we'd flag in CI.
    scan_image "${image}:${VERSION}"

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

preflight_bases

# Detect when we'd cross-build the frontend through QEMU. On Apple
# Silicon (arm64) targeting linux/amd64 the multi-stage build's
# ``npm run build`` runs esbuild inside the emulated builder image,
# and esbuild segfaults under QEMU. Fall back to a two-step path:
# build dist/ on the host, then package via Containerfile.runtime.
# Multi-arch buildx mode handles this differently (per-platform
# builders), so we only kick in for single-platform cross builds.
build_frontend() {
    local needs_host_build=0
    if [[ -z "$PLATFORMS" && -n "$PLATFORM" ]]; then
        case "${HOST_ARCH}:${PLATFORM}" in
            arm64:linux/amd64|aarch64:linux/amd64)
                needs_host_build=1 ;;
        esac
    fi

    if [[ $needs_host_build -eq 1 ]]; then
        echo "==> Cross-arch detected (${HOST_ARCH} -> ${PLATFORM})"
        echo "    Building dist/ on host to bypass esbuild QEMU crash"
        if ! command -v npm >/dev/null 2>&1; then
            echo "ERROR: npm not on PATH; needed for host-side dist/ build." >&2
            echo "       Install Node 20+, or run with PLATFORM=linux/arm64" >&2
            echo "       for a native build." >&2
            exit 6
        fi
        ( cd frontend && npm ci && npm run build )
        build_one "virtvalidate-frontend" "frontend/Containerfile.runtime" "frontend"
    else
        build_one "virtvalidate-frontend" "frontend/Containerfile" "frontend"
    fi
}

case "$TARGET" in
    backend)
        build_one "virtvalidate-backend" "backend/Containerfile" "."
        ;;
    frontend)
        build_frontend
        ;;
    all)
        build_one "virtvalidate-backend" "backend/Containerfile" "."
        build_frontend
        ;;
    *)
        echo "ERROR: unknown target '$TARGET' (expected backend|frontend|all)" >&2
        exit 4
        ;;
esac

echo "==> Done"
