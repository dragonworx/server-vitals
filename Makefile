# vitals — convenience targets. Most wrap install.sh / systemctl.
SERVICE := vitals

.PHONY: help install install-nginx uninstall run check start stop restart deploy status logs

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
	@python3 vitals.py

check: ## Syntax-check the Python source
	@python3 -m py_compile vitals.py && echo "ok: vitals.py compiles"

start: ## Start the installed service
	@sudo systemctl start $(SERVICE) && systemctl is-active $(SERVICE)

stop: ## Stop the installed service
	@sudo systemctl stop $(SERVICE) && echo "stopped $(SERVICE)"

restart: ## Restart the installed service (no code redeploy)
	@sudo systemctl restart $(SERVICE) && systemctl is-active $(SERVICE)

deploy: ## Rebuild: reinstall current vitals.py + restart (sudo)
	@./install.sh

status: ## Show service status
	@systemctl status $(SERVICE) --no-pager || true

logs: ## Follow the service logs
	@journalctl -u $(SERVICE) -f
