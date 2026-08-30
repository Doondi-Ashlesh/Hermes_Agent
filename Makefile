.DEFAULT_GOAL := help
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help install test demo check run once eval stats clean

help:  ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'

$(VENV):
	python3 -m venv $(VENV)

install: $(VENV)  ## Create the venv and install the package with dev extras
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"
	@echo "installed — run 'make demo' to see it work"

test: install  ## Run the full suite (no network, no credentials)
	$(PY) -m pytest tests/ -q

check: install  ## Verify docs: links, anchors, env vars, CLI coverage
	$(PY) scripts/check_links.py
	$(PY) -m pytest tests/test_docs.py -q

diagrams:  ## Parse every mermaid block with the real parser (needs node)
	@command -v npm >/dev/null || { echo "npm not found — CI covers this"; exit 0; }
	@npm install --silent --no-audit --no-fund --no-save mermaid@11 jsdom
	@node scripts/check_diagrams.mjs

demo: install  ## End-to-end against bundled fixtures, no credentials needed
	$(PY) -m hermes_inbox.cli demo --provider offline

once: install  ## One polling cycle against your configured mailbox
	$(PY) -m hermes_inbox.cli once

run: install  ## Poll continuously (ctrl-c to stop)
	$(PY) -m hermes_inbox.cli run

eval: install  ## Replay your corrections and score the classifier
	$(PY) -m hermes_inbox.cli eval

stats: install  ## Summarize what the agent has done so far
	$(PY) -m hermes_inbox.cli stats

clean:  ## Remove the venv, caches and build artifacts (keeps data/)
	rm -rf $(VENV) .pytest_cache *.egg-info build dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
