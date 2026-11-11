.PHONY: install lint test eval run-web run-cli tf-fmt

VENV_BIN := ./.venv/bin

install:
	python3 -m venv .venv
	$(VENV_BIN)/pip install -e ".[dev]"

lint:
	$(VENV_BIN)/ruff check commerce_ops_agent tests

test:
	COMMERCE_MODE=fixture $(VENV_BIN)/pytest tests/unit tests/integration -v

eval:
	COMMERCE_MODE=fixture $(VENV_BIN)/pytest tests/eval -v

run-web:
	$(VENV_BIN)/adk web .

run-cli:
	$(VENV_BIN)/adk run commerce_ops_agent

tf-fmt:
	@if command -v terraform >/dev/null 2>&1; then terraform -chdir=terraform fmt -check; else echo "terraform CLI not installed locally; validated in GitHub Actions CI."; fi
