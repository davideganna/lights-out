.PHONY: check lint test

# Lint + tests. CI runs `make check`. Needs ruff and pytest in the Python you use.
PYTHON ?= python3

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

test:
	$(PYTHON) -m pytest -q

check: lint test
