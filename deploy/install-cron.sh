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

# Режим радара: ежечасный запуск main.py сразу и собирает, и публикует. Тихий
# час штатно заканчивается пустым выпуском — main.py ловит sentinel-строку и
# просто не постит в Telegram. flock не даёт двум запускам наложиться.
# Чтобы вернуться к редким выпускам: ежечасный запуск с --collect (копит, не
# публикуя) + отдельные строки на нужные часы БЕЗ флага. Часы задавать в UTC —
# системная TZ сервера UTC, MSK = UTC+3 круглый год (06:00 UTC = 09:00 MSK).
SNIP=$(cat <<EOF
# BEGIN azalio-tech-summary (managed by make install-cron)
15 * * * * /usr/bin/flock -n $REMOTE_DIR/.cron-main.lock -c 'cd $REMOTE_DIR && .venv/bin/python main.py' >> $REMOTE_DIR/main.log 2>&1
25 * * * * /usr/bin/flock -n $REMOTE_DIR/.cron-reddit.lock -c 'cd $REMOTE_DIR && .venv/bin/python standalone_reddit_digest.py' >> $REMOTE_DIR/reddit.log 2>&1
# END azalio-tech-summary
EOF
)

printf '%s\n%s\n' "$KEEP" "$SNIP" | crontab -
echo "--- new crontab ---"
crontab -l
