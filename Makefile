VENV ?= .venv
PY := $(VENV)/bin/python
API := services/api
WEB := apps/web
TEST_DB ?= postgresql+psycopg://sdlc:sdlc@localhost:5432/sdlc_test

.PHONY: dev setup sandbox-toolchain migrate seed api worker web test test-api test-web lint typecheck openapi evals evals-live models-check check licences notices helm-check backup

dev:              ## run API, worker and web together (see ./dev.sh --help)
	./dev.sh

setup:            ## install backend (editable) and frontend dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install -e "$(API)[dev]"
	cd $(WEB) && npm ci

sandbox-toolchain: ## minimal Python toolchain visible inside the sandbox (needs root)
	python3 -m venv /opt/asw-sandbox
	/opt/asw-sandbox/bin/pip install --quiet pytest
	# Readable by everyone, writable only by root: no ACLs inherited from /opt (seen on CI runners).
	chown -R root:root /opt/asw-sandbox
	if command -v setfacl >/dev/null; then setfacl -R -b /opt/asw-sandbox; fi
	chmod -R u=rwX,go=rX /opt/asw-sandbox
	id sandbox >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin --uid 64000 sandbox

migrate:
	cd $(API) && ../../$(VENV)/bin/alembic upgrade head

seed:
	$(VENV)/bin/agentic-sdlc seed

api:
	$(VENV)/bin/agentic-sdlc api --port 8000

worker:
	$(VENV)/bin/agentic-sdlc worker

web:
	cd $(WEB) && npm run dev

test: test-api test-web

test-api:
	cd $(API) && DATABASE_URL=$(TEST_DB) ../../$(PY) -m pytest

test-web:
	cd $(WEB) && npm test

lint:
	cd $(API) && ../../$(VENV)/bin/ruff check src tests migrations && ../../$(VENV)/bin/ruff format --check src tests migrations
	cd $(WEB) && npm run lint && npm run format:check

typecheck:
	cd $(API) && ../../$(VENV)/bin/mypy src
	cd $(WEB) && npm run typecheck

openapi:          ## regenerate OpenAPI document and the typed web client
	cd $(API) && APP_ENV=test ../../$(VENV)/bin/agentic-sdlc openapi > openapi.json
	cd $(WEB) && npm run gen:api

evals:
	cd $(API) && DATABASE_URL=$(TEST_DB) ../../$(PY) -m agentic_sdlc.evals.run --all

models-check:     ## send a synthetic probe to every configured model (needs a model endpoint)
	$(VENV)/bin/agentic-sdlc models check

evals-live:       ## measure a configured model on the requirements dataset: make evals-live MODEL=<model_id>
	cd $(API) && ../../$(PY) -m agentic_sdlc.evals.run --suite requirements --strategy llm --model $(MODEL) \
		--report ../../var/reports/evals-$(MODEL).json

check: lint typecheck test

licences:         ## fail if a shipped dependency's licence is not allowed
	$(PY) scripts/licenses.py --check

notices:          ## regenerate THIRD_PARTY_NOTICES.md (run before tagging a release)
	$(PY) scripts/licenses.py --write

helm-check:       ## render the Helm chart under several value sets; validate schemas and policy
	$(PY) scripts/helm_check.py

backup:           ## write a backup of the database and data directory to ./backups
	cd services/api && ../../.venv/bin/agentic-sdlc backup --out ../../backups
