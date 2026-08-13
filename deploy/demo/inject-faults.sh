#!/bin/bash
# Simulate an imperfect cutover. Each fault targets a specific diff dimension
# and an expected verdict, so the report shows graded triage rather than a
# wall of red. payments-api is deliberately left healthy.
set -euo pipefail

echo "--- fault 1: settlement worker never came back up (services -> fail)"
systemctl stop payments-worker.service
systemctl disable payments-worker.service >/dev/null 2>&1 || true

echo "--- fault 2: cache tier down, port 6379 gone (services -> fail, ports -> warn)"
systemctl stop session-cache.service
systemctl disable session-cache.service >/dev/null 2>&1 || true

echo "--- fault 3: monitoring agent unit lost entirely (services -> fail, ports -> warn)"
systemctl stop metrics-agent.service
systemctl disable metrics-agent.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/metrics-agent.service

echo "--- fault 4: spool volume not remounted after cutover (mounts -> warn)"
sed -i '/payments-spool/d' /etc/fstab
umount /var/lib/payments-spool || true

echo "--- fault 5: settlement cron lost (cron -> warn)"
rm -f /etc/cron.d/payments-settlement

echo "--- fault 6: someone left a debug listener behind (services + ports -> info)"
cat > /etc/systemd/system/rogue-debug.service <<'EOF'
[Unit]
Description=Ad-hoc debug listener (left over from cutover troubleshooting)
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m http.server 31337 --bind 0.0.0.0
WorkingDirectory=/var/tmp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now rogue-debug.service >/dev/null 2>&1
sleep 3

echo
echo "=== post-cutover state ==="
for s in payments-api payments-worker session-cache metrics-agent rogue-debug; do
  printf '%-18s %s\n' "$s" "$(systemctl is-active ${s}.service 2>&1)"
done
echo "--- listening ---"
ss -tulnH | awk '{print $1, $5}' | grep -E ':(8080|6379|9100|31337)$' | sort
echo "--- spool ---"
findmnt -rn -o TARGET,FSTYPE /var/lib/payments-spool || echo "(not mounted)"
echo "--- cron.d ---"
ls /etc/cron.d/
