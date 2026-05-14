.PHONY: run-tests
run-tests:
	@uv run pytest -s tests/

.PHONY: install
install:
	@uv sync

.PHONY: run-app
run-app:
	@uv run astroarch_bridge
