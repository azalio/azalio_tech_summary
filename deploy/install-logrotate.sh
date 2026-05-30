#!/bin/bash
# Installs logrotate config for the run logs and the digest audit log.
#  - main.log / reddit.log: weekly, 4 generations (operational noise).
#  - workspace/memory/digest_runs.jsonl: the historical record of editor
#    input/output per post — kept far longer (monthly, 12 generations ~= 1yr)
#    so we retain post history for analysis. Each run reopens the path with
#    open("a"), so plain rename-based rotation is safe (no held handle).
# The su directive is required because $REMOTE_DIR isn't root-owned.
set -e

REMOTE_DIR="$1"
[ -n "$REMOTE_DIR" ] || { echo "usage: $0 <remote_dir>" >&2; exit 1; }

USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"

sudo tee /etc/logrotate.d/azalio-tech-summary >/dev/null <<EOF
$REMOTE_DIR/main.log
$REMOTE_DIR/reddit.log
{
    su $USER_NAME $GROUP_NAME
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    create 0644 $USER_NAME $GROUP_NAME
    sharedscripts
}

$REMOTE_DIR/workspace/memory/digest_runs.jsonl {
    su $USER_NAME $GROUP_NAME
    monthly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    create 0644 $USER_NAME $GROUP_NAME
}
EOF

echo "--- dry-run ---"
sudo logrotate -d /etc/logrotate.d/azalio-tech-summary 2>&1 | tail -10
