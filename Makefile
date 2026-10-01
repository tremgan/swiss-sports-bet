SERVICES := core db_service loro_scrape_service swisslos_scrape_service dashboard report

.PHONY: sync test typecheck lint format check

# Each service is an independent uv project, so every target loops over them.
sync:
	@for s in $(SERVICES); do \
		echo "==> sync $$s"; \
		(cd src/$$s && uv sync --all-groups) || exit 1; \
	done

# Only services with a tests/ directory are collected, matching CI.
test:
	@for s in $(SERVICES); do \
		if [ -d "src/$$s/tests" ]; then \
			echo "==> test $$s"; \
			(cd src/$$s && uv run pytest -q) || exit 1; \
		fi; \
	done

# pyright needs each service's own venv to resolve sqlmodel/fastapi/streamlit.
typecheck:
	@for s in $(SERVICES); do \
		echo "==> typecheck $$s"; \
		(cd src/$$s && uv run pyright) || exit 1; \
	done

# ruff is import-resolution free, so one pass from the root covers everything.
lint:
	uv tool run ruff check .
	uv tool run ruff format --check .

format:
	uv tool run ruff check --fix .
	uv tool run ruff format .

check: lint typecheck test
