PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest

.PHONY: chat run-cli test compile

chat:
	@PYTHONPATH=src $(PYTHON) agent_mvp.py

run-cli: chat

test:
	PYTHONPATH=src $(PYTEST)

compile:
	$(PYTHON) -m compileall agent_mvp.py src tests
