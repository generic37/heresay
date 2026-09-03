# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv venv-dev audit release-check cert run run-dev lan clean-dev test webtest clean deploy deploy-content fetch-backup

# Production host — an ~/.ssh/config alias for the box you deploy to.
# Override per-invocation: HERESAY_HOST=other make fetch-backup
HERESAY_HOST ?= commons

help:
	@echo "Targets:"
	@echo "  make venv     - create .venv with the RUNTIME deps (pinned + hash-verified)"
	@echo "  make venv-dev - the above, plus pytest and the audit tools"
	@echo "  make audit    - pip-audit the pinned runtime deps (needs network)"
	@echo "  make release-check - make test + make audit"
	@echo "  make cert     - generate a self-signed dev TLS certificate"
	@echo "  make run      - run the daemon with config.toml (the maintainer's REAL state)"
	@echo "  make run-dev  - run the daemon with config.dev.toml (throwaway devstate/)"
	@echo "  make lan      - serve the web client to the home LAN (daemon + web on :8099)"
	@echo "  make clean-dev- delete devstate/ ONLY (never the real root files)"
	@echo "  make test     - run pytest (includes the web harness where node exists)"
	@echo "  make webtest  - run the web client harness alone, all modes (needs node)"
	@echo "  make clean    - remove caches and the venv"
	@echo "  make deploy   - rsync repo to the server + restart heresay (no state uploaded)"
	@echo "  make deploy-content - rsync house/ only; NO restart (the engine reloads it live)"
	@echo "  make deploy-alerts - refresh the /usr/local/bin alert scripts; NO restart"
	@echo "  make fetch-backup - scp the newest server backup to ./backups/"

# ⛔ RUNTIME ONLY, pinned and hash-verified (v1.48.0, audit G2). This is the
# same install production gets — three packages and their closure, no test
# framework. The pip self-upgrade is a SEPARATE step: it must not be swept into
# a --require-hashes install, which is all-or-nothing across the command.
venv:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install --require-hashes -r requirements.txt

# The developer's venv: the runtime file first (still hash-verified), then the
# tools layered on top. Two installs, because hashed and unhashed requirements
# cannot share one — see requirements-dev.txt.
venv-dev: venv
	$(PIP) install -r requirements-dev.txt

# ⚠ The check that would have caught G1 without an audit. Advisory data is
# fetched, so this needs network; it is its own target rather than part of
# `make test` for that reason — and `make release-check` is what a release runs.
audit:
	@$(PY) -c 'import pip_audit' 2>/dev/null || \
	  { echo "pip-audit is a DEV tool — run 'make venv-dev' first."; exit 1; }
	$(PY) -m pip_audit -r requirements.txt --progress-spinner off

release-check: test audit

cert:
	bash scripts/gencert.sh

run:
	$(PY) -m heresay

# ALL agent-driven / dev live verification uses this: state lands in devstate/,
# never the operator's real root files. Safe to delete afterwards.
run-dev:
	mkdir -p devstate
	$(PY) -m heresay -c config.dev.toml

# Serve the web client to the home LAN (real config; daemon + static web server).
lan:
	bash scripts/lan.sh config.toml

# Deletes ONLY the throwaway dev state. Never touches repo-root runtime files.
clean-dev:
	rm -rf devstate

test:
	$(VENV)/bin/pytest -q

# The browser harness on its own — every mode, in about a second. `make test`
# already runs it (tests/test_web_harness.py), but that costs a subprocess per
# mode; this is the loop to use while actually editing web/index.html. Skips
# nothing: if node is missing this fails loudly, which is what you want when you
# asked for it by name.
webtest:
	@node scripts/tabtest.js
	@WIDTH=1 node scripts/tabtest.js
	@INWORLD=1 node scripts/tabtest.js
	@SNAP=1 node scripts/tabtest.js
	@RESUME=1 node scripts/tabtest.js
	@SAVED_RESUME=kept-token-abc RESUME=1 node scripts/tabtest.js
	@MASTER_OFF=1 node scripts/tabtest.js
	@TITLE_INIT=1 node scripts/tabtest.js
	@TITLE_INIT=1 SAVED_TITLE=Commons node scripts/tabtest.js
	@RAISE_INIT=1 EXPECT_RAISE=1 node scripts/tabtest.js
	@RAISE_INIT=1 SAVED_RAISE=0 EXPECT_RAISE=0 node scripts/tabtest.js

clean:
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# One-command production deploy: rsync (excluding runtime state + dev files) +
# restart. The host comes from DEPLOY_HOST (env) or deploy/local.env — deploy.sh
# refuses if neither is set. Override ad hoc: DEPLOY_HOST=other make deploy
deploy:
	bash deploy/deploy.sh

# Content only: rsync house/ and DO NOT restart. The running engine's watcher
# applies it within reload_poll_seconds (v1.39.0) — nobody is disconnected,
# nobody is moved, no locks or topics are lost. Use `make deploy` for code.
deploy-content:
	bash deploy/deploy-content.sh

# Alerts only: refresh the /usr/local/bin alert scripts (notify, hcping,
# ceiling-watch, healthcheck). Those live OUTSIDE the /opt/heresay rsync target
# on purpose, so `make deploy` never carries them — run this when an alert
# script changed. No restart, no app touch. Same host machinery as deploy.
deploy-alerts:
	bash deploy/deploy-alerts.sh

# Pull the newest server backup to a local, gitignored offsite copy. The
# destination is the repo's own gitignored backups/ (repo-relative, so the name
# of the local checkout directory never matters).
fetch-backup:
	@mkdir -p $(CURDIR)/backups
	@newest=$$(ssh $(HERESAY_HOST) 'ls -1t /var/backups/heresay/heresay-*.tar.gz 2>/dev/null | head -1'); \
	if [ -z "$$newest" ]; then echo "no backup on $(HERESAY_HOST) yet"; exit 1; fi; \
	echo "fetching $$newest"; \
	scp $(HERESAY_HOST):$$newest $(CURDIR)/backups/; \
	echo "saved to $(CURDIR)/backups/"

