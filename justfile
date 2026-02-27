set dotenv-load := true
set quiet := true

default:
    @just --list

# Install dependencies
install:
    uv sync

# Run all unit tests (no integration)
test *ARGS:
    uv run pytest tests/ -x -v --ignore=tests/integration {{ARGS}}

# Run integration tests (requires local tmux)
test-integration *ARGS:
    uv run pytest tests/integration/ -x -v {{ARGS}}

# Run full test suite
test-all *ARGS:
    uv run pytest tests/ -x -v {{ARGS}}

# Lint with ruff
lint:
    uv run ruff check .

# Format with ruff
format:
    uv run ruff format .

# Check formatting without changing files
format-check:
    uv run ruff format --check .

# Lint and format
check: lint format-check

# Run nx CLI
run *ARGS:
    uv run nx {{ARGS}}
