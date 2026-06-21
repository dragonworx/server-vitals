# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file, zero-dependency host-monitoring agent. `server-vitals.py` is Python
**standard library only** — no pip, no virtualenv, no third-party imports. It runs
as a systemd service (not a container — see README "Why a systemd service") bound to
`127.0.0.1:9999`, reverse-proxied at `/health` and `/stats`.

## Commands

```bash
make run            # run in foreground for local dev (python3 server-vitals.py)
make check          # py_compile the source — the only "test" / lint that exists
make deploy         # reinstall the edited source to /usr/local/bin + restart (sudo)
make restart        # bounce the running service WITHOUT redeploying code (sudo)
make logs           # journalctl -u server-vitals -f
```

There is no test suite, linter, or build step. `make check` (py_compile) is the
full verification path.

## Deploying changes — important

The systemd service runs the **installed copy** at `/usr/local/bin/server-vitals.py`,
not the file in this repo. After editing `server-vitals.py`, you must run
`make deploy` (which re-runs `install.sh` to copy the new source, then restarts).
`make restart` alone bounces the service but keeps the *old* installed code — it will
not pick up your edits.

## Architecture

Everything is in `server-vitals.py`, in three layers:

1. **Metric collectors** (top of file) — read the Linux kernel directly:
   `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, and `statvfs("/")`. This direct
   host access is the whole reason it's a service and not a container.

2. **CPU sampling is delta-based and stateful.** `cpu_percent_delta()` and
   `cpu_core_percents()` compute usage against the *previous* sample stored in
   module-level globals (`_cpu_state`, `_cpu_core_state`), guarded by locks because
   the server is threaded. They are non-blocking — designed for the `/stats` client
   polling repeatedly. The first call after start returns 0.0 (no prior sample).
   `cpu_percent(interval=…)` is the separate *blocking* variant used by `/health`.

3. **HTTP layer** — `ThreadingHTTPServer` + `Handler.do_GET` routes three things:
   `/health` (JSON summary, blocking CPU read), `/stats` (the dashboard HTML), and
   `/stats?format=json` (a live JSON sample using the delta collectors).

`stats_payload()` and `health_payload()` assemble the JSON; `Handler` only routes
and serializes.

## The dashboard is an embedded HTML string

`STATS_HTML` is a single large raw-string constant (~lines 205–967) containing the
entire `/stats` page: HTML, CSS, and vanilla JS with no external assets. It polls
`/stats?format=json` and draws SVG sparklines (one mini-graph per CPU core,
auto-detected from the array length). Poll interval and time window are user-set in
the header and persisted to `localStorage`. When changing dashboard behavior you are
editing JavaScript inside this Python string, not a separate asset.

## Config

Tunables live at the top of `server-vitals.py` — notably `LISTEN = ("127.0.0.1", 9999)`.
After changing them, `make deploy`.
