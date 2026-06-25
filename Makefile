# maestro — dev + dogfood targets.
# MAESTRO_HOME defaults to the self-dev home; override on the CLI if you like.
export MAESTRO_HOME ?= $(HOME)/.maestro/maestro-dev

PY := .venv/bin/python

.PHONY: help install test dry dispatch loop status doctor project reconcile fleet-up fleet-down

help:
	@echo "make install     editable install + put 'maestro' on PATH"
	@echo "make test        run the test suite"
	@echo "make dry         one dispatcher sweep, dry-run (mints + would-spawn)"
	@echo "make dispatch    one REAL sweep (spawns claude reconcilers for due tickets)"
	@echo "make loop        foreground dispatch every 5 min (Ctrl-C to stop)"
	@echo "make status      ticket counts by phase"
	@echo "make doctor      fleet health (heartbeat, dead-letters)"
	@echo "make reconcile KEY=M-1   run ONE reconcile in the foreground (for testing)"
	@echo "make fleet-up / fleet-down   install / remove the launchd dispatcher"
	@echo "make run-tui-dev"

install:
	$(PY) -m pip -q install -e ".[dev]"
	mkdir -p $(HOME)/.local/bin && ln -sf $(PWD)/.venv/bin/maestro $(HOME)/.local/bin/maestro
	@echo "maestro -> $$(command -v maestro)"

test:
	$(PY) -m pytest -q

dry:
	maestro dispatch --dry-run

dispatch:
	maestro dispatch

loop:
	@echo "dispatching every 300s against $(MAESTRO_HOME) — Ctrl-C to stop"
	@while true; do maestro dispatch; sleep 300; done

status:
	maestro status

doctor:
	maestro doctor

project:
	maestro project

# Run one reconcile in the foreground (cwd = repo, so /maestro-reconcile resolves).
reconcile:
	@test -n "$(KEY)" || (echo "usage: make reconcile KEY=M-1" && exit 1)
	claude -p "/maestro-reconcile $(KEY)" --permission-mode acceptEdits

fleet-up:
	daemon/install.sh up

fleet-down:
	daemon/install.sh down

run-tui-dev:
	.venv/bin/maestro --home ${MAESTRO_HOME} tui
