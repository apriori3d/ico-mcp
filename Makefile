.PHONY: ci lint format typecheck test

ci: lint typecheck test

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff format src/ tests/

typecheck:
	uv run pyright

test:
	uv run pytest
