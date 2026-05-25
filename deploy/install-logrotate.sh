#!/bin/bash
# Installs logrotate config for main.log and reddit.log.
# Weekly rotation, 4 generations kept, gzip from generation 2 onward.
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
EOF

echo "--- dry-run ---"
sudo logrotate -d /etc/logrotate.d/azalio-tech-summary 2>&1 | tail -10
