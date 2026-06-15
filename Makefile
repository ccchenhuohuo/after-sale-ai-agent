PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
SMOKE_PYTHON ?= $(shell if [ -x .venv/bin/python ] && .venv/bin/python -c "import numpy, fastapi" >/dev/null 2>&1; then printf ".venv/bin/python"; else printf "python3"; fi)

.PHONY: chat run-cli test compile smoke smoke-openclaw-contract smoke-openclaw-sidecar smoke-doctor-openclaw smoke-live

chat:
	@PYTHONPATH=src $(PYTHON) agent_mvp.py

run-cli: chat

test:
	PYTHONPATH=src $(PYTEST)

compile:
	$(PYTHON) -m compileall agent_mvp.py src tests

smoke:
	PYTHONPATH=src $(SMOKE_PYTHON) scripts/smoke_support_copilot.py --pretty

smoke-openclaw-contract:
	@set -a; [ ! -f .env ] || . ./.env; set +a; \
	if [ -n "$$OPENCLAW_FEISHU_BRIDGE_SECRET" ]; then \
		PYTHONPATH=src $(SMOKE_PYTHON) scripts/smoke_openclaw_contract.py --pretty; \
	else \
		PYTHONPATH=src $(SMOKE_PYTHON) scripts/smoke_openclaw_contract.py --pretty --allow-unconfigured-secret; \
	fi

smoke-openclaw-sidecar:
	cd deploy/openclaw_sidecar && corepack npm run smoke:support-copilot

smoke-doctor-openclaw:
	cd deploy/openclaw_sidecar && corepack npm run doctor:support-copilot -- --allow-unconfigured

smoke-live:
	test "$$RUN_LIVE_SMOKE" = "1"
	PYTHONPATH=src $(SMOKE_PYTHON) scripts/smoke_support_copilot.py --live --pretty
