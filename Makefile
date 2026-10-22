.PHONY: check
# Static checks, the full test suite, frontend formatting and a type-checked build.
check:
	.venv/bin/ruff check backend tests scripts
	.venv/bin/python -m pytest -q
	cd frontend && npm run format:check && npm run build
