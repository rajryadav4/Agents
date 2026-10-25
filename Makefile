.PHONY: install test demo redteam serve cover e2e
install:
	pip install -e ".[dev]"
test:
	pytest -q -m "not e2e"
e2e:
	pytest -q -m e2e
cover:
	pytest -q -m "not e2e" --cov=warden --cov-report=term-missing
demo:
	warden demo
redteam:
	warden redteam
serve:
	warden serve
