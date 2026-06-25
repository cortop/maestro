# maestro — dev + dogfood targets.
# MAESTRO_HOME defaults to the self-dev home; override on the CLI if you like.
export MAESTRO_HOME ?= $(HOME)/.maestro/maestro-dev

PY := .venv/bin/python

.PHONY: help install test dry dispatch loop status doctor project reconcile fleet-up fleet-down autocomplete

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
	@echo "make autocomplete            install zsh completion script"
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

COMPLETION_SCRIPT := completions/_maestro
COMPLETION_DIR := $(HOME)/.zsh/completions

autocomplete:
	@mkdir -p $(COMPLETION_DIR)
	@cp $(COMPLETION_SCRIPT) $(COMPLETION_DIR)/_maestro
	@if ! grep -qF 'fpath=($$HOME/.zsh/completions' $(HOME)/.zshrc 2>/dev/null; then \
		printf '\n# maestro zsh completion\nfpath=($$HOME/.zsh/completions $$fpath)\nautoload -Uz compinit && compinit\n' >> $(HOME)/.zshrc; \
	fi
	@echo "maestro completion installed — restart your shell or: source ~/.zshrc"

run-tui-dev:
	.venv/bin/maestro --home ${MAESTRO_HOME} tui
