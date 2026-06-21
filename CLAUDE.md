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

2. **CPU sampling runs on a background thread.** `CpuSampler` (started in
   `main()` as the module global `_sampler`) reads `/proc/stat` every
   `SAMPLE_INTERVAL` seconds, computes the aggregate + per-core deltas against its
   *own* previous reading, and publishes the result under a lock. Endpoints call
   `_sampler.snapshot()` — non-blocking, and consistent across concurrent clients
   (no request mutates sampling state, so multiple dashboard tabs / the proxy
   health check no longer corrupt each other's deltas). CPU% is a delta between
   two readings, so the first published value after start is 0.0.

3. **HTTP layer** — `ThreadingHTTPServer` + `Handler.do_GET` routes three things:
   `/health` (JSON summary), `/stats` (the dashboard HTML), and
   `/stats?format=json` (a live JSON sample). All read the warm CPU snapshot.
   The handler sets `protocol_version = "HTTP/1.1"` (keep-alive — every response
   carries a Content-Length) and a socket `timeout` (drops slow/idle clients).
   Internal errors are logged to the journal and return a generic message; the
   request path is never reflected back.

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
