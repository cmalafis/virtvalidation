#!/bin/bash
# Stand up a fake "production payments tier" on the demo VM.
#
# The box has no yum repos and no subscription, so these are real systemd
# units running Python listeners rather than packaged daemons. To the
# VirtValidate collector (systemctl list-units + ss -tulnH) they are
# indistinguishable: same unit states, same listening sockets.
set -euo pipefail

mkdir -p /var/lib/payments-spool /opt/payments

# --- worker payload: no listener, just a long-running process ------------
cat > /opt/payments/worker.py <<'PY'
import time, sys
while True:
    sys.stdout.write("settlement worker tick\n"); sys.stdout.flush()
    time.sleep(30)
PY

unit() {
  local name="$1" desc="$2" exec="$3"
  cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=${desc}
After=network-online.target

[Service]
Type=simple
ExecStart=${exec}
WorkingDirectory=/var/tmp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

unit payments-api    "Payments REST API (customer facing)"  "/usr/bin/python3 -m http.server 8080 --bind 0.0.0.0"
unit session-cache   "Session cache tier"                   "/usr/bin/python3 -m http.server 6379 --bind 0.0.0.0"
unit metrics-agent   "Metrics collection agent"             "/usr/bin/python3 -m http.server 9100 --bind 0.0.0.0"
unit payments-worker "Settlement batch worker"              "/usr/bin/python3 /opt/payments/worker.py"

# --- nightly settlement cron --------------------------------------------
cat > /etc/cron.d/payments-settlement <<'EOF'
# Nightly settlement batch — finance SLA 02:15 UTC
15 2 * * * root /usr/bin/python3 /opt/payments/worker.py --settle
EOF
chmod 644 /etc/cron.d/payments-settlement

# --- spool volume --------------------------------------------------------
grep -q '/var/lib/payments-spool' /etc/fstab 2>/dev/null || \
  echo "tmpfs /var/lib/payments-spool tmpfs size=64M,mode=0755 0 0" >> /etc/fstab
mountpoint -q /var/lib/payments-spool || mount -t tmpfs -o size=64M,mode=0755 tmpfs /var/lib/payments-spool

systemctl daemon-reload
for s in payments-api session-cache metrics-agent payments-worker; do
  systemctl enable --now "${s}.service" >/dev/null 2>&1
done

sleep 3
echo "=== unit states ==="
for s in payments-api session-cache metrics-agent payments-worker; do
  printf '%-18s %s\n' "$s" "$(systemctl is-active ${s}.service)"
done
echo "=== listening ==="
ss -tulnH | awk '{print $1, $5}' | grep -E ':(8080|6379|9100)$' | sort
echo "=== mount ==="
findmnt -rn -o TARGET,FSTYPE /var/lib/payments-spool
echo "=== cron ==="
ls /etc/cron.d/
