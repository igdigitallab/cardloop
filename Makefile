.PHONY: help install update build run test install-dev service

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:        ## First-time setup: venv, deps, .env, frontend build
	./install.sh

update:         ## Pull latest, reinstall changed deps, rebuild, restart service
	./update.sh

build:          ## Rebuild the web frontend (web/dist)
	cd web && npm ci && npm run build

run:            ## Run the cockpit in the foreground (http://localhost:8787)
	venv/bin/python bot.py

test:           ## Run the test suite (must be via venv — pytest-aiohttp lives there)
	venv/bin/python -m pytest -q tests/

install-dev:    ## Install dev/test dependencies only
	venv/bin/pip install -r requirements-dev.txt

service:        ## Render + install the systemd unit, then enable & start it
	@sed -e "s/__USER__/$$(whoami)/g" -e "s#__HOME__#$$HOME#g" cardloop.service.template \
		| sudo tee /etc/systemd/system/cardloop.service >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now cardloop
	@systemctl --no-pager -n 5 status cardloop || true
