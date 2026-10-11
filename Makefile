# Makefile for rhylthyme development

.PHONY: help install install-dev test test-unit test-integration test-cli test-examples clean format lint type-check coverage

# Default target
help:
	@echo "Available targets:"
	@echo "  install       Install package in development mode"
	@echo "  install-dev   Install package with dev dependencies (from setup.py)"
	@echo "  test          Run all tests"
	@echo "  test-unit     Run unit tests only"
	@echo "  test-integration  Run integration tests only"
	@echo "  test-cli      Run CLI tests only"
	@echo "  test-examples Run example validation tests"
	@echo "  test-fast     Run fast tests (unit + cli, no examples)"
	@echo "  coverage      Run tests with coverage report"
	@echo "  lint          Run linting checks"
	@echo "  format        Format code with black and isort"
	@echo "  format-check  Check code formatting without changes"
	@echo "  type-check    Run type checking with mypy"
	@echo "  clean         Clean up temporary files"
	@echo "  validate-examples  Validate all example files"

# Installation
install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

# Testing
test:
	pytest tests -v -m "not llm"

test-unit:
	pytest tests -v -m "unit and not llm"

test-integration:
	pytest tests -v -m "integration"

test-cli:
	pytest tests -v -m "cli"

test-examples:
	pytest tests/test_validate_examples_ci.py -v

test-fast:
	pytest tests -v -m "(unit or cli) and not llm"

# Coverage
coverage:
	pytest tests -v --cov=src --cov-report=html --cov-report=term-missing
	@echo "Coverage report generated in htmlcov/"

# Code quality
lint:
	flake8 src tests --count --select=E9,F63,F7,F82 --show-source --statistics
	flake8 src tests --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics

format:
	black src tests
	isort src tests

format-check:
	black --check src tests
	isort --check-only src tests

type-check:
	mypy src --ignore-missing-imports --no-strict-optional

# Validation
validate-examples:
	python tests/test_validate_examples_ci.py

# Cleanup
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf coverage.xml
	rm -rf .pytest_cache/
	find . -type d -name __pycache__ -delete
	find . -type f -name "*.pyc" -delete

# Development workflow
dev-setup: install-dev
	@echo "Development environment set up!"

dev-test: format-check lint test-fast
	@echo "Quick development tests passed!"

ci-test: format-check lint test coverage
	@echo "Full CI test suite completed!"

# Help with pytest markers
test-help:
	@echo "Available pytest markers:"
	@echo "  unit         - Unit tests (fast, isolated)"
	@echo "  integration  - Integration tests (slower, with external dependencies)"
	@echo "  cli          - CLI interface tests"
	@echo "  slow         - Slow tests (example validation, etc.)"
	@echo "  llm          - Real model API calls (excluded from make test; RHYLTHYME_EVAL_LIVE=1)"
	@echo ""
	@echo "Example usage:"
	@echo "  make test-unit          # Run only unit tests"
	@echo "  pytest -m 'not slow'   # Run all except slow tests"
	@echo "  pytest -k 'validation' # Run tests matching 'validation'"
