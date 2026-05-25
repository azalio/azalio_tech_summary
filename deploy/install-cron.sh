#!/bin/bash
# Installs the azalio-tech-summary cron entries idempotently.
# The managed block is bracketed by # BEGIN / # END markers; any prior
# unmarked main.py / standalone_reddit_digest.py lines are stripped so
# a first-time install adopts the existing schedule cleanly.
set -e

REMOTE_DIR="$1"
[ -n "$REMOTE_DIR" ] || { echo "usage: $0 <remote_dir>" >&2; exit 1; }

KEEP=$(crontab -l 2>/dev/null \
  | awk '/# BEGIN azalio-tech-summary/,/# END azalio-tech-summary/{next} {print}' \
  | grep -v -E "(main\.py|standalone_reddit_digest\.py)" || true)

SNIP=$(cat <<EOF
# BEGIN azalio-tech-summary (managed by make install-cron)
15 * * * * cd $REMOTE_DIR && .venv/bin/python main.py >> $REMOTE_DIR/main.log 2>&1
25 * * * * cd $REMOTE_DIR && .venv/bin/python standalone_reddit_digest.py >> $REMOTE_DIR/reddit.log 2>&1
# END azalio-tech-summary
EOF
)

printf '%s\n%s\n' "$KEEP" "$SNIP" | crontab -
echo "--- new crontab ---"
crontab -l
