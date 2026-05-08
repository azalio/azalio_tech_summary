SHELL := /bin/bash

# Deploy target (override via environment or .env.deploy)
SSH_JUMP   ?=
SSH_TARGET ?= ubuntu@example.com
REMOTE_DIR ?= /home/ubuntu/azalio_tech_summary

SSH_OPTS := $(if $(SSH_JUMP),-J $(SSH_JUMP))
DEPLOY_FILES := main.py core.py collectors.py dedup.py standalone_reddit_digest.py test_dedup.py requirements.txt

.PHONY: help install test deploy lint

help:
	@echo 'Targets:'
	@echo '  make install       Install Python deps locally'
	@echo '  make test          Run unit tests (needs E5 model on first run)'
	@echo '  make deploy        scp source files to $$SSH_TARGET:$$REMOTE_DIR'
	@echo '                     Override via env: SSH_JUMP, SSH_TARGET, REMOTE_DIR'
	@echo ''
	@echo 'Example:'
	@echo '  SSH_TARGET=user@host REMOTE_DIR=/srv/bot make deploy'

install:
	pip install -r requirements.txt

test:
	python3 -m pytest test_dedup.py -v

deploy:
	@echo "Deploying to $(SSH_TARGET):$(REMOTE_DIR)..."
	ssh $(SSH_OPTS) $(SSH_TARGET) "mkdir -p $(REMOTE_DIR)"
	scp $(SSH_OPTS) $(DEPLOY_FILES) $(SSH_TARGET):$(REMOTE_DIR)/
	@echo 'Done. Make sure .env exists on the server.'
