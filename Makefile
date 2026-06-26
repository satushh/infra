# EthPandaOps devnet monitoring — one-command bring-up.
#
# Three layers, two runtimes:
#   panda-server  (docker)  ── authenticated proxy to ethPandaOps cloud
#   adapter       (HOST)    ── tools/panda-grafana-adapter, shells out to the
#                              macOS `panda` CLI + host auth; CANNOT be a
#                              container (darwin binary + ~/.config/panda auth)
#   grafana       (docker)  ── reaches the adapter via host.docker.internal:9119
#
#   make up   → start all three            make status → health of each layer
#   make down → stop adapter + grafana     make logs   → tail adapter log
#
# The adapter is managed by this Makefile (background process). It does NOT
# auto-start on reboot — just re-run `make up`.

ADAPTER_DIR  := tools/panda-grafana-adapter
ADAPTER      := $(ADAPTER_DIR)/panda_grafana_adapter.py
ADAPTER_PORT := 9119
PID_FILE     := /tmp/panda-grafana-adapter.pid
LOG_FILE     := /tmp/panda-grafana-adapter.log
PYTHON       := python3
HEALTH       := http://127.0.0.1:$(ADAPTER_PORT)/health

.DEFAULT_GOAL := help
.PHONY: help up down restart status logs submodules panda-up adapter-up adapter-down grafana-up sync-dashboards

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

up: submodules panda-up adapter-up grafana-up ## Bring up the whole stack (panda + adapter + grafana)
	@echo "✓ stack up → http://localhost:3000"

submodules: ## Fetch the adapter submodule if missing (so a fresh clone + make up just works)
	@test -f $(ADAPTER) || { echo "→ fetching adapter submodule"; git submodule update --init $(ADAPTER_DIR); }

panda-up: ## Start panda-server (docker) and verify auth
	@if panda --log-level error server status 2>/dev/null | grep -q "Health: Healthy"; then \
		echo "✓ panda-server already healthy"; \
	else \
		panda server start; \
	fi
	@panda auth status >/dev/null 2>&1 || { echo "✗ panda not authenticated — run: panda auth login"; exit 1; }

adapter-up: ## Start the host-side adapter (idempotent)
	@if curl -fsS $(HEALTH) >/dev/null 2>&1; then \
		echo "✓ adapter already up on :$(ADAPTER_PORT)"; \
	else \
		nohup $(PYTHON) $(ADAPTER) --port $(ADAPTER_PORT) >>$(LOG_FILE) 2>&1 & echo $$! >$(PID_FILE); \
		sleep 1; \
		if curl -fsS $(HEALTH) >/dev/null 2>&1; then \
			echo "✓ adapter started (pid $$(cat $(PID_FILE)), log $(LOG_FILE))"; \
		else \
			echo "✗ adapter failed to start — see $(LOG_FILE)"; exit 1; \
		fi; \
	fi

grafana-up: ## Start Grafana (docker)
	@docker compose up -d grafana

down: adapter-down ## Stop adapter + grafana (panda-server and local node keep running)
	@docker compose stop grafana

adapter-down: ## Stop the host-side adapter
	@if [ -f $(PID_FILE) ] && kill $$(cat $(PID_FILE)) 2>/dev/null; then \
		rm -f $(PID_FILE); echo "✓ adapter stopped"; \
	elif pkill -f "panda_grafana_adapter[.]py" 2>/dev/null; then \
		rm -f $(PID_FILE); echo "✓ adapter stopped"; \
	else \
		rm -f $(PID_FILE); echo "· adapter not running"; \
	fi

restart: down up ## Restart the whole stack

status: ## Show health of all three layers
	@printf '── panda-server ──\n';  panda server status 2>&1 | tail -3 || true
	@printf '── adapter ──\n';       curl -fsS $(HEALTH) 2>/dev/null && printf '\n' || printf 'DOWN\n'
	@printf '── grafana ──\n';       docker compose ps grafana

logs: ## Tail the adapter log
	@tail -f $(LOG_FILE)

sync-dashboards: ## Copy panda dashboards from the submodule into Grafana's provisioned dir
	@cp $(ADAPTER_DIR)/dashboards/*.json configuration/grafana/dashboards/ && \
		echo "✓ synced $(ADAPTER_DIR)/dashboards → configuration/grafana/dashboards (restart not needed; auto-reloads ~30s)"
