SHELL := /bin/bash

# Deploy target (override via environment or .env.deploy)
SSH_JUMP   ?=
SSH_TARGET ?= ubuntu@example.com
REMOTE_DIR ?= /home/ubuntu/azalio_tech_summary

SSH_OPTS := $(if $(SSH_JUMP),-J $(SSH_JUMP))
DEPLOY_FILES := main.py core.py collectors.py dedup.py standalone_reddit_digest.py test_dedup.py requirements.txt

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
	@echo '                     Override via env: SSH_JUMP, SSH_TARGET, REMOTE_DIR'
	@echo ''
	@echo 'Example:'
	@echo '  SSH_TARGET=user@host REMOTE_DIR=/srv/bot make deploy'
	@echo '  SSH_TARGET=user@host REMOTE_DIR=/srv/bot make backup'
	@echo '  SSH_TARGET=user@host REMOTE_DIR=/srv/bot BACKUP=backups/2026-05-11.tgz make restore'

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
