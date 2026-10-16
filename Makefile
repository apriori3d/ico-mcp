.PHONY: ci lint format typecheck test

ci: lint typecheck test

lint:
	uv run ruff check src/ tests/ domain/
	uv run ruff format --check src/ tests/ domain/

format:
	uv run ruff format src/ tests/ domain/

typecheck:
	uv run pyright

test:
	uv run pytest
