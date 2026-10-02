.PHONY: install test lint fmt doctor
install:
	uv sync
test:
	uv run pytest
lint:
	uv run ruff check .
fmt:
	uv run ruff format .
doctor:
	uv run waive doctor
