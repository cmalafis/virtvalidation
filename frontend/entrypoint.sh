#!/bin/sh
# OpenShift / podman entrypoint for the VirtValidate frontend.
#
# Does two things before exec'ing nginx:
#
# 1) Re-creates the nginx temp directories under /tmp. The Helm
#    chart mounts /tmp (and /var/lib/nginx, /run) as emptyDir
#    volumes for OCP arbitrary-UID compatibility. Those mounts
#    overlay any subdirectories baked into the image, so the
#    /tmp/nginx/{client_body,proxy,...} layout that nginx.conf
#    expects has to be created at runtime, after the mount.
#
# 2) Renders /etc/nginx/nginx.conf from .template via envsubst.
#    nginx itself doesn't substitute env vars, so the upstream
#    hostname (${BACKEND_HOST}) and port (${BACKEND_PORT}) get
#    injected here from values supplied by the orchestrator —
#    podman-compose.yml in dev, the Helm chart in OCP.
#
# Defaults match podman-compose so the image works the same in
# both environments. In OCP, the chart overrides them with the
# release-prefixed Service name. See docs/DEPLOYMENT_TROUBLESHOOTING.md
# for the live-debug log that produced this entrypoint.
set -e

mkdir -p /tmp/nginx/client_body \
         /tmp/nginx/proxy \
         /tmp/nginx/fastcgi \
         /tmp/nginx/uwsgi \
         /tmp/nginx/scgi

: "${BACKEND_HOST:=backend}"
: "${BACKEND_PORT:=8000}"
export BACKEND_HOST BACKEND_PORT

# Restrict envsubst to the two placeholders we expect — without the
# allow-list it would also try to substitute every $host / $uri /
# $remote_addr in nginx.conf (those are nginx variables, not shell
# variables; envsubst would replace them with empty strings).
envsubst '${BACKEND_HOST} ${BACKEND_PORT}' \
    < /etc/nginx/nginx.conf.template \
    > /etc/nginx/nginx.conf

exec nginx -g 'daemon off;'
