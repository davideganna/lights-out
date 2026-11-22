.PHONY: check lint test fmt clean

# Lint + tests for lights-out itself. CI runs `make check`.
VENV ?= .venv
PY := $(VENV)/bin/python

$(VENV)/.installed: pyproject.toml
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	touch $@

lint: $(VENV)/.installed
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

test: $(VENV)/.installed
	$(PY) -m pytest

check: lint test

fmt: $(VENV)/.installed
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache *.egg-info src/*.egg-info
