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

2b. **Access logs are tailed on their own background threads, one per provider.**
   `LogTailer` follows a file, keeps the last `LOG_RING_SIZE` entries in a
   `deque` stamped with a monotonic sequence number, and handles rotation
   (detected by inode change, not EOF), in-place truncation, partial lines and a
   file that disappears and comes back. Same contract as `CpuSampler`: the thread
   owns all I/O, state is published under a lock, requests never touch the disk.
   `/logs` is a cursor stream — a client sends the highest sequence it has seen
   and gets only what is newer, which is what makes sub-second polling
   affordable. If the log is missing or unreadable the tailer publishes an
   explicit reason so the panel can say why instead of showing an empty list.

   **`LogTailer` is format-agnostic. All server-specific knowledge lives in a
   `LogProvider`**, which implements `parse(text)` (a line → the compact row the
   panel streams), and optionally `detail(text)` and `discover_servers(observed)`.
   `CaddyProvider` reads Caddy's JSON and gets its site list from the Caddy admin
   API; `CombinedProvider` reads NCSA combined, with `NginxProvider` and
   `ApacheProvider` adding config-tree discovery of `server_name` /
   `ServerName`. New server types go in `PROVIDER_TYPES` and need no changes
   anywhere else. Rows keep the raw line and are parsed in full only on a
   `?detail=` request.

   `LOG_PROVIDERS` (or the JSON in `LOG_PROVIDERS_FILE`) is the config;
   `LogRegistry` (the module global `_logs`, started in `main()`) builds one
   tailer per entry, preserves config order, makes ids unique, and skips a bad
   entry with a journal message rather than failing to start. **Sequence numbers
   are per tailer**, so a cursor is only meaningful alongside its provider — the
   `provider` field is echoed in every `/logs` response for exactly that reason.

3. **HTTP layer** — `ThreadingHTTPServer` + `Handler.do_GET` routes:
   `/health` (JSON summary), `/stats` (the dashboard HTML),
   `/stats?format=json` (a live JSON sample), `/ping`, `/logs` (cursor stream and
   `?detail=`), `/logs/servers` and `/logs/providers`. The three `/logs` routes
   take an optional `?provider=<id>`; without it they answer for the first
   configured provider. All read warm in-memory snapshots.
   The handler sets `protocol_version = "HTTP/1.1"` (keep-alive — every response
   carries a Content-Length) and a socket `timeout` (drops slow/idle clients).
   Internal errors are logged to the journal and return a generic message; the
   request path is never reflected back.

`stats_payload()` and `health_payload()` assemble the JSON; `Handler` only routes
and serializes.

## The dashboard is an embedded HTML string

`STATS_HTML` is a single large raw-string constant containing the entire `/stats`
page: HTML, CSS, and vanilla JS with no external assets. It polls
`/stats?format=json` and draws SVG sparklines (one mini-graph per CPU core,
auto-detected from the array length). Poll interval and time window are user-set in
the header and persisted to `localStorage`. When changing dashboard behavior you are
editing JavaScript inside this Python string, not a separate asset.

Layout: `<body>` is a horizontal flex row holding `#mainpane` (the dashboard's own
vertical column — header, strips, `<main>`, footer), the `#logsplit` drag handle,
and the `#logs` access-log panel. The charts measure themselves in real pixels
rather than scaling a `viewBox`, so **anything that changes the dashboard's width
must trigger `renderAll()`** — a `ResizeObserver` on `#mainpane` handles the drag,
the collapse and window resizes together.

The log panel runs its own poll loop (`logTick`), separate from the metrics
`tick()`, and stops entirely when the dashboard is paused, the panel is collapsed,
or the tab is hidden. Preferences use the same `stats:*` localStorage convention.

The list reads **newest-first by default**, flipped by `#log-order` (persisted to
`stats:logs:newest`). `logRows` is always oldest-first — the server's order —
and only the DOM is built in the reading direction, so the cursor, trimming and
filter logic have no special case. Four helpers own the direction:
`atLogLive()`/`scrollLogToLive()` (which end counts as live), `insertLogNodes()`
(prepends, and compensates `scrollTop` so an arriving row doesn't shove the
reader's view — hence `overflow-anchor: none` on `#log-list`), and
`trimLogDom()` (drops from the stale end). **A ring-eviction gap is a flag on the
row that follows it** (`r.gap`), not a loose node, so it survives a re-render;
`emitLogRow()` puts the marker on that row's older side either way.

`#log-provider` is the provider picker. It is `hidden` (and `#logs-title` shown)
unless more than one provider is configured, so the single-provider dashboard
looks exactly as it did. The chosen site is remembered **per provider** under
`stats:logs:host:<id>`, because the site lists are unrelated — switching provider
resets the cursor, the rows and the site list together.

**Log text is attacker-controlled** — request paths and user-agents come from
anyone on the internet. The panel builds every row with `createTextNode`/`<mark>`
and never `innerHTML`. Keep it that way.

## Config

Tunables live at the top of `server-vitals.py` — notably `LISTEN = ("127.0.0.1", 9999)`
and `LOG_PROVIDERS` (which access logs to follow; `LOG_PROVIDERS_FILE` is an
optional JSON override so a host can change it without editing the script).
After changing them, `make deploy`.
