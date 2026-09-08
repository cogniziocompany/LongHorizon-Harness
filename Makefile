.PHONY: e2e-happy test

# End-to-end happy path through the local queue API.
e2e-happy:
	bash e2e/happy_path.sh

# Default test run (unit/integration only; e2e is heavy and run separately).
test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -p no:cacheprovider
