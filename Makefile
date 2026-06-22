# Server Vitals — convenience targets. Most wrap install.sh / the service manager
# (systemd on Linux, launchd on macOS — auto-detected below).
SERVICE := server-vitals
LABEL   := com.dragonworx.server-vitals
OS      := $(shell uname -s)
# Extra args forwarded to the installer, e.g. `make install ARGS=--system` (macOS
# LaunchDaemon) or `make install ARGS=--with-nginx` (Linux).
ARGS    ?=

ifeq ($(OS),Darwin)
# --- macOS / launchd. LaunchAgent (user domain) by default; ARGS=--system flips
#     every manage target to the system LaunchDaemon (sudo). ---------------------
ifeq ($(findstring --system,$(ARGS)),--system)
DOMAIN   := system
PLIST    := /Library/LaunchDaemons/$(LABEL).plist
LOG_FILE := /var/log/server-vitals.log
LC       := sudo launchctl
else
DOMAIN   := gui/$(shell id -u)
PLIST    := $(HOME)/Library/LaunchAgents/$(LABEL).plist
LOG_FILE := $(HOME)/Library/Logs/server-vitals.log
LC       := launchctl
endif
START_CMD   := $(LC) kickstart -k $(DOMAIN)/$(LABEL)
STOP_CMD    := $(LC) bootout $(DOMAIN)/$(LABEL) 2>/dev/null || $(LC) unload -w $(PLIST)
RESTART_CMD := $(LC) kickstart -k $(DOMAIN)/$(LABEL)
STATUS_CMD  := $(LC) print $(DOMAIN)/$(LABEL) 2>/dev/null || launchctl list | grep server-vitals || echo "server-vitals not loaded"
LOGS_CMD    := tail -f $(LOG_FILE)
else
# --- Linux / systemd -----------------------------------------------------------
START_CMD   := sudo systemctl start $(SERVICE) && systemctl is-active $(SERVICE)
STOP_CMD    := sudo systemctl stop $(SERVICE) && echo "stopped $(SERVICE)"
RESTART_CMD := sudo systemctl restart $(SERVICE) && systemctl is-active $(SERVICE)
STATUS_CMD  := systemctl status $(SERVICE) --no-pager || true
LOGS_CMD    := journalctl -u $(SERVICE) -f
endif

.PHONY: help install install-nginx uninstall run check start stop restart deploy status logs

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install + start (Linux: systemd/sudo; macOS: LaunchAgent, ARGS=--system for a daemon)
	@./install.sh $(ARGS)

install-nginx: ## Install incl. the nginx snippet (Linux/sudo)
	@./install.sh --with-nginx

uninstall: ## Stop + remove the service (macOS: add ARGS=--system for a daemon install)
	@./uninstall.sh $(ARGS)

run: ## Run in the foreground for local dev (no install)
	@python3 server-vitals.py

check: ## Syntax-check the Python source
	@python3 -m py_compile server-vitals.py && echo "ok: server-vitals.py compiles"

start: ## Start the installed service
	@$(START_CMD)

stop: ## Stop the installed service
	@$(STOP_CMD)

restart: ## Restart the installed service (no code redeploy)
	@$(RESTART_CMD)

deploy: ## Rebuild: reinstall current server-vitals.py + restart
	@./install.sh $(ARGS)

status: ## Show service status
	@$(STATUS_CMD)

logs: ## Follow the service logs
	@$(LOGS_CMD)
