# health-server — convenience targets. Most wrap install.sh / systemctl.
SERVICE := health-server

.PHONY: help install install-nginx uninstall run check restart status logs

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install + start the service (sudo)
	@./install.sh

install-nginx: ## Install incl. the nginx snippet (sudo)
	@./install.sh --with-nginx

uninstall: ## Stop + remove the service (sudo)
	@./uninstall.sh

run: ## Run in the foreground for local dev (no install)
	@python3 health-server.py

check: ## Syntax-check the Python source
	@python3 -m py_compile health-server.py && echo "ok: health-server.py compiles"

restart: ## Restart the installed service
	@sudo systemctl restart $(SERVICE) && systemctl is-active $(SERVICE)

status: ## Show service status
	@systemctl status $(SERVICE) --no-pager || true

logs: ## Follow the service logs
	@journalctl -u $(SERVICE) -f
