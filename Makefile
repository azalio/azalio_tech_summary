SHELL := /bin/bash

# Per-host deploy config — keep it out of the public repo.
# Copy env.deploy.example to .env.deploy and fill SSH_JUMP / SSH_TARGET / REMOTE_DIR.
-include .env.deploy

# Fallback defaults; override on the command line if .env.deploy is missing.
# SSH_JUMP is optional (empty = direct SSH, no bastion).
SSH_JUMP   ?=
SSH_TARGET ?= azalio@example.com
REMOTE_DIR ?= /home/azalio/azalio_tech_summary

SSH_OPTS := $(if $(SSH_JUMP),-J $(SSH_JUMP))
DEPLOY_FILES := main.py core.py collectors.py dedup.py \
                ranking.py health.py eval_digest.py x_acquire.py \
                standalone_reddit_digest.py standalone_telegram_digest.py \
                standalone_x_digest.py x_sources.example.yaml \
                test_dedup.py test_ranking.py test_health.py test_eval_digest.py \
                test_x_acquire.py test_collectors.py \
                requirements.txt

BACKUP_DIR ?= backups
BACKUP_FILE := $(BACKUP_DIR)/$(shell date +%F).tgz

.PHONY: help install test deploy install-cron install-logrotate add-channels check-channel backup restore lint

help:
	@echo 'Targets:'
	@echo '  make install            Install Python deps locally'
	@echo '  make test               Run unit tests (needs E5 model on first run)'
	@echo '  make deploy             scp source files to $$SSH_TARGET:$$REMOTE_DIR'
	@echo '  make install-cron       install/refresh cron entries on $$SSH_TARGET'
	@echo '  make install-logrotate  install /etc/logrotate.d/azalio-tech-summary on $$SSH_TARGET (uses sudo)'
	@echo '  make add-channels       merge Telegram channels into $$REMOTE_DIR/.env (idempotent, backs up first)'
	@echo '                          CHANNELS="@a https://t.me/b" make add-channels'
	@echo '  make check-channel      verify channels resolve & are readable by the bot session'
	@echo '                          CHANNELS="@a https://t.me/b" make check-channel'
	@echo '  make backup             snapshot .env + workspace from $$SSH_TARGET to $$BACKUP_FILE'
	@echo '  make restore            restore $$BACKUP onto $$SSH_TARGET:$$REMOTE_DIR'
	@echo '                          Configure SSH_JUMP/SSH_TARGET/REMOTE_DIR in .env.deploy'
	@echo '                          (copy env.deploy.example) or pass them inline.'
	@echo ''
	@echo 'Example (with .env.deploy in place):'
	@echo '  make deploy'
	@echo '  make install-cron'
	@echo '  make install-logrotate'
	@echo '  make backup'
	@echo '  BACKUP=backups/YYYY-MM-DD.tgz make restore'

install:
	pip install -r requirements.txt

test:
	python3 -m pytest test_dedup.py -v

deploy:
	@echo "Deploying to $(SSH_TARGET):$(REMOTE_DIR)..."
	ssh $(SSH_OPTS) $(SSH_TARGET) "mkdir -p $(REMOTE_DIR)"
	scp $(SSH_OPTS) $(DEPLOY_FILES) $(SSH_TARGET):$(REMOTE_DIR)/
	@echo 'Done. Make sure .env exists on the server.'

install-cron:
	@echo "Installing cron entries on $(SSH_TARGET) for $(REMOTE_DIR)..."
	scp $(SSH_OPTS) deploy/install-cron.sh $(SSH_TARGET):/tmp/install-cron.sh
	ssh $(SSH_OPTS) $(SSH_TARGET) "bash /tmp/install-cron.sh $(REMOTE_DIR) && rm /tmp/install-cron.sh"

add-channels:
	@test -n "$(CHANNELS)" || (echo 'Usage: CHANNELS="@a https://t.me/b" make add-channels'; exit 1)
	@echo "Merging Telegram channels into $(SSH_TARGET):$(REMOTE_DIR)/.env (idempotent)..."
	scp $(SSH_OPTS) deploy/merge_channels.py $(SSH_TARGET):/tmp/merge_channels.py
	ssh $(SSH_OPTS) $(SSH_TARGET) "cp -a $(REMOTE_DIR)/.env $(REMOTE_DIR)/.env.bak.\$$(date +%Y%m%d-%H%M%S) && python3 /tmp/merge_channels.py $(REMOTE_DIR)/.env $(CHANNELS) && rm /tmp/merge_channels.py"

check-channel:
	@test -n "$(CHANNELS)" || (echo 'Usage: CHANNELS="@a https://t.me/b" make check-channel'; exit 1)
	@echo "Checking channel readability via the bot session on $(SSH_TARGET)..."
	scp $(SSH_OPTS) deploy/check_channel.py $(SSH_TARGET):/tmp/check_channel.py
	ssh $(SSH_OPTS) $(SSH_TARGET) "cd $(REMOTE_DIR) && .venv/bin/python /tmp/check_channel.py $(CHANNELS); rc=\$$?; rm /tmp/check_channel.py; exit \$$rc"

install-logrotate:
	@echo "Installing logrotate config on $(SSH_TARGET) for $(REMOTE_DIR) (uses sudo)..."
	scp $(SSH_OPTS) deploy/install-logrotate.sh $(SSH_TARGET):/tmp/install-logrotate.sh
	ssh $(SSH_OPTS) $(SSH_TARGET) "bash /tmp/install-logrotate.sh $(REMOTE_DIR) && rm /tmp/install-logrotate.sh"

backup:
	@mkdir -p $(BACKUP_DIR)
	@echo "Backing up .env + workspace from $(SSH_TARGET):$(REMOTE_DIR) to $(BACKUP_FILE)..."
	ssh $(SSH_OPTS) $(SSH_TARGET) "tar -czf - -C $(REMOTE_DIR) .env workspace" > $(BACKUP_FILE)
	@ls -lh $(BACKUP_FILE)

restore:
	@test -n "$(BACKUP)" || (echo "Usage: BACKUP=path/to/backup.tgz make restore"; exit 1)
	@test -f "$(BACKUP)" || (echo "BACKUP file not found: $(BACKUP)"; exit 1)
	@echo "Restoring $(BACKUP) to $(SSH_TARGET):$(REMOTE_DIR)..."
	ssh $(SSH_OPTS) $(SSH_TARGET) "mkdir -p $(REMOTE_DIR)"
	ssh $(SSH_OPTS) $(SSH_TARGET) "tar -xzf - -C $(REMOTE_DIR)" < $(BACKUP)
	@echo 'Done. Verify .env permissions: ssh $(SSH_TARGET) "ls -l $(REMOTE_DIR)/.env"'
