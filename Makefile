SHELL := /bin/bash

# Per-host deploy config — keep it out of the public repo.
# Copy .env.deploy.example to .env.deploy and fill SSH_JUMP / SSH_TARGET / REMOTE_DIR.
-include .env.deploy

# Fallback defaults; override on the command line if .env.deploy is missing.
SSH_JUMP   ?=
SSH_TARGET ?= ubuntu@example.com
REMOTE_DIR ?= /home/ubuntu/azalio_tech_summary

SSH_OPTS := $(if $(SSH_JUMP),-J $(SSH_JUMP))
DEPLOY_FILES := main.py core.py collectors.py dedup.py \
                standalone_reddit_digest.py standalone_telegram_digest.py \
                test_dedup.py requirements.txt

BACKUP_DIR ?= backups
BACKUP_FILE := $(BACKUP_DIR)/$(shell date +%F).tgz

.PHONY: help install test deploy backup restore lint

help:
	@echo 'Targets:'
	@echo '  make install       Install Python deps locally'
	@echo '  make test          Run unit tests (needs E5 model on first run)'
	@echo '  make deploy        scp source files to $$SSH_TARGET:$$REMOTE_DIR'
	@echo '  make backup        snapshot .env + workspace from $$SSH_TARGET to $$BACKUP_FILE'
	@echo '  make restore       restore $$BACKUP onto $$SSH_TARGET:$$REMOTE_DIR'
	@echo '                     Configure SSH_JUMP/SSH_TARGET/REMOTE_DIR in .env.deploy'
	@echo '                     (copy env.deploy.example) or pass them inline.'
	@echo ''
	@echo 'Example (with .env.deploy in place):'
	@echo '  make deploy'
	@echo '  make backup'
	@echo '  BACKUP=backups/2026-05-11.tgz make restore'

install:
	pip install -r requirements.txt

test:
	python3 -m pytest test_dedup.py -v

deploy:
	@echo "Deploying to $(SSH_TARGET):$(REMOTE_DIR)..."
	ssh $(SSH_OPTS) $(SSH_TARGET) "mkdir -p $(REMOTE_DIR)"
	scp $(SSH_OPTS) $(DEPLOY_FILES) $(SSH_TARGET):$(REMOTE_DIR)/
	@echo 'Done. Make sure .env exists on the server.'

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
