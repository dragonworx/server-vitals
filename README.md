# Server Vitals

A tiny, dependency-free server health endpoint. One Python file, standard
library only — no pip installs, no virtualenv. It exposes a few JSON health
endpoints and a self-contained live **stats dashboard** (CPU incl. per-core,
memory, disk) that you can drop behind nginx or hit directly.

**Runs on Linux _and_ macOS** — the same file. On Linux it reads `/proc`; on a
Mac it reads the Mach kernel and `sysctl` instead (still zero-dependency, still
stdlib only), so you can also point it at your laptop. See
[Run on macOS](#run-on-macos).

Built for a single VPS: it listens on `127.0.0.1:9999` and is meant to be
reverse-proxied at paths like `/health` and `/stats` — or, on your own machine,
just opened straight in a browser with no proxy at all.

<p align="center">
  <img src="doc/server-vitals-screenshot-desktop.webp" alt="Server Vitals dashboard (desktop)" width="100%">
  <br>
  <em>Desktop view</em>
  <br>
  <br>
  <img src="doc/server-vitals-screenshot-mobile.webp" alt="Server Vitals dashboard (mobile)" width="280">
  <br>
  <em>Mobile Portrait View</em>
</p>

## Endpoints

| Path           | Returns                                                                 |
| -------------- | ----------------------------------------------------------------------- |
| `/health`      | JSON: cpu, memory, disk, load average, uptime, overall `ok/degraded`    |
| `/stats`       | HTML live dashboard (polls `/stats?format=json`)                        |
| `/stats?format=json` | JSON sample: cpu %, **per-core cpu %**, memory, disk             |
| `/logs?since=<seq>`  | JSON: access-log entries newer than `<seq>` (see below)           |
| `/logs?detail=<seq>` | JSON: the full original log record for one entry                  |
| `/logs/servers`      | JSON: hostnames the web server is configured to serve             |
| `/logs/providers`    | JSON: the access logs this agent is following                     |

The three `/logs` routes take an optional `?provider=<id>` naming which log to
read; without it they answer for the first one configured.

The `/stats` dashboard is a single HTML page with no external assets. It draws
CPU (with one mini-graph per core, auto-detected), memory, and disk as live
SVG sparklines. Poll interval (0.25s–10s) and time window (1–60 min) are
selectable from the header and persisted in `localStorage`.

Beside the graphs sits a resizable **access-log panel** — drag the splitter, or
collapse it with the `logs` button in the header. It filters by server, searches
with live highlighting, and expands any row to the full record. Entries read
newest-first; the `↑`/`↓` button in the panel header flips the order, and the
choice is remembered. Caddy, nginx and
Apache are supported; configure more than one and the panel grows a dropdown to
switch between them. See [Access logs](#access-logs).

## Requirements

- `python3` (standard library only — works with the system Python on both OSes)
- **Linux** with `/proc` (reads `/proc/stat`, `/proc/meminfo`, `/proc/uptime`),
  or **macOS** (reads the Mach host port + `sysctl` via `ctypes` — no extra install)
- For the managed service: `systemd` on Linux (or `launchd` on macOS) — and
  optional `nginx` / Caddy if you want to reverse-proxy it

## Why a systemd service, not a Docker container

Server Vitals is a **host-monitoring agent**, so it is deployed as a plain process
under systemd — *not* in a container. This is deliberate. The whole job of the
app is to observe the host it runs on:

- It reads the host kernel directly: on Linux `/proc/stat`, `/proc/meminfo`,
  `/proc/uptime`, and `statvfs("/")`; on macOS the Mach host port
  (`host_processor_info`, `host_statistics64`) and `sysctl` — for CPU / memory /
  load / disk.

A container's value is **isolation** — its own filesystem, PID namespace, and
network stack, separate from the host. That is exactly the wrong default here:

| Concern | systemd service (this project) | Docker container |
| --- | --- | --- |
| Sees real host CPU / mem / disk | ✅ directly | ❌ sees the container namespace unless you bind-mount `/proc`, `/`, … |
| Lifecycle / autostart / restart | systemd (`enable --now`, `Restart=`) | Docker daemon (`restart: unless-stopped`) |
| Footprint | ~none — one stdlib Python process | image build + daemon overhead |

To run Server Vitals usefully in a container you would have to dismantle that
isolation (`--pid=host`, `-v /proc:/host/proc:ro`, `-v /:/rootfs:ro`) **and**
rewrite the metric paths to read `/host/proc/*` — more moving parts for a
strictly less capable result. So it ships as a service. Containers remain the
right tool for workloads you want *isolated from* the host (web apps,
databases); a host agent is the opposite case.

> If you genuinely need it containerized anyway (e.g. a constrained PaaS), run
> with `--pid=host --network=host` and bind-mount `/proc` and `/` read-only.

## Install

**From a checkout** (recommended):

```bash
git clone https://github.com/dragonworx/server-vitals.git
cd server-vitals
sudo ./install.sh            # or: make install
```

Add `--with-nginx` to also install the reverse-proxy snippet:

```bash
sudo ./install.sh --with-nginx
```

**One-liner** (fetches the sources straight from GitHub):

```bash
RAW=https://raw.githubusercontent.com/dragonworx/server-vitals/main
curl -fsSL $RAW/install.sh | VITALS_RAW_BASE=$RAW sudo -E bash
```

The installer copies `server-vitals.py` to `/usr/local/bin`, installs the
`server-vitals` systemd unit, enables + starts it, and verifies `/health`
responds.

### install.sh options

| Option          | Effect                                                |
| --------------- | ----------------------------------------------------- |
| `--with-nginx`  | also install `nginx/server-vitals.conf` and reload nginx     |
| `--no-start`    | install files but don't enable/start the service      |
| `--user USER`   | run the service as `USER` (default `www-data`)        |

## nginx integration

The snippet at [`nginx/server-vitals.conf`](nginx/server-vitals.conf)
proxies `/health` and `/stats` to `127.0.0.1:9999`. After
installing it (`--with-nginx`), `include` it in any server block:

```nginx
server {
    # ...
    include snippets/server-vitals.conf;
}
```

Then `sudo nginx -t && sudo systemctl reload nginx`.

## Caddy integration

For [Caddy](https://caddyserver.com), proxy `/health` and `/stats` to
`127.0.0.1:9999` in your `Caddyfile`:

```caddy
example.com {
    # ...
    reverse_proxy /health 127.0.0.1:9999
    reverse_proxy /stats 127.0.0.1:9999
    reverse_proxy /ping 127.0.0.1:9999
    # Only needed for the access-log panel; see "Access logs" below.
    reverse_proxy /logs 127.0.0.1:9999
    reverse_proxy /logs/* 127.0.0.1:9999
}
```

Then `sudo caddy validate && sudo systemctl reload caddy`.

## Access logs

The dashboard's right-hand panel live-tails a web server's access log. It is
optional: with no readable log the panel simply says so and the rest of the
dashboard is unaffected.

Which logs it follows is the `LOG_PROVIDERS` list at the top of
`server-vitals.py`. A *provider* is the small adapter that knows one server's log
format and how to enumerate its sites:

| `type`     | Reads                             | Default path                   |
| ---------- | --------------------------------- | ------------------------------ |
| `caddy`    | Caddy's JSON access log           | `/var/log/caddy/access.log`    |
| `nginx`    | NCSA combined                     | `/var/log/nginx/access.log`    |
| `apache`   | NCSA combined                     | `/var/log/apache2/access.log`  |
| `combined` | NCSA combined, no config discovery | —                             |

```python
LOG_PROVIDERS = [
    {"type": "caddy", "path": "/var/log/caddy/access.log"},
    {"type": "nginx", "path": "/var/log/nginx/access.log", "label": "nginx"},
]
```

`type` is the only required key. `path`, `label` and `id` override that
provider's defaults; `nginx` and `apache` also take `conf_dir` (where to look for
declared server names). One provider is the ordinary case and the panel looks
exactly as before; list several and it grows a dropdown to switch between them,
opening on the first. Each keeps its own buffer, its own cursor and its own
remembered site filter. Run `make deploy` after changing the list.

To add a provider **without** editing a file that the next `make deploy`
overwrites, put the same list in `/etc/server-vitals/providers.json` — it
replaces `LOG_PROVIDERS` outright when present, and a malformed file is reported
to the journal and ignored rather than taken as fatal:

```json
{ "providers": [ { "type": "caddy" }, { "type": "nginx" } ] }
```

Supporting another server means subclassing `LogProvider` and implementing one
method, `parse(text)`, which turns a line into the row the panel streams. Two
optional methods cover the rest: `detail(text)` for the expanded view, and
`discover_servers(observed)` for the site dropdown. Add the class to
`PROVIDER_TYPES` and it becomes a valid `type`. Everything else — following the
file across rotations, the ring buffer, the cursor protocol, the endpoints and
the panel — is shared and needs no changes.

### nginx and Apache

Both write NCSA combined by default, which carries less than Caddy's JSON: the
timestamp has one-second resolution, and there is **no host and no duration**.
Two common additions are picked up automatically if you configure them — a
leading vhost field, and a `rt=<seconds>` token after the user-agent. Without a
vhost field every row reports an empty host, and the site dropdown can only offer
what the config declares, so log the vhost if several sites share one file:

```nginx
log_format vhost '$host $remote_addr - $remote_user [$time_local] '
                 '"$request" $status $body_bytes_sent '
                 '"$http_referer" "$http_user_agent" rt=$request_time';
access_log /var/log/nginx/access.log vhost;
```

Apache ships this as `vhost_combined` (its leading `%v:%p` is understood as-is):

```apache
CustomLog /var/log/apache2/access.log vhost_combined
```

The site dropdown reads `server_name` (nginx) or `ServerName`/`ServerAlias`
(Apache) out of the config tree, so sites with no traffic yet still appear.
Commented-out entries are skipped. If the config isn't readable the dropdown
falls back to the hosts seen in the log.

If the panel shows *"N lines did not match the … log format"*, the file isn't in
the format that provider parses — the usual cause is pointing `nginx` at a JSON
log, or vice versa.

### 1. Make Caddy write a JSON access log

Access logging in Caddy is **per-site** — there is no global switch, so every
site block needs it or it silently logs nothing. A shared snippet is the easiest
way to avoid missing one:

```caddy
(accesslog) {
    log {
        output file /var/log/caddy/access.log {
            roll_size 50MiB
            roll_keep 10
            mode 0640
        }
        format json
    }
}

example.com {
    import accesslog
    # ...
}
```

Leave `ts` at its default float-epoch format; the panel parses it as a number.

### 2. Let the service read it

Caddy's log is typically `caddy:caddy` mode `0640`, and Server Vitals runs as
`www-data`, so it cannot read the file by default. Grant read access with a
POSIX ACL — the narrowest option, and the default ACL means Caddy's rotated
files inherit it:

```bash
sudo setfacl -m  u:www-data:r-x /var/log/caddy
sudo setfacl -m  u:www-data:r-- /var/log/caddy/access.log
sudo setfacl -d -m u:www-data:r-- /var/log/caddy   # rolled files inherit
sudo -u www-data head -c 200 /var/log/caddy/access.log   # verify
```

If adding an ACL raises the file's mask (`getfacl` shows `mask::rwx`), put it
back with `sudo setfacl -m m::r-- /var/log/caddy/access.log` so the grant stays
read-only.

The unit already carries `ReadOnlyPaths=-/var/log/caddy`, which states the intent
and keeps the path readable under `ProtectSystem=strict` — but it grants nothing
on its own; the ACL above is what actually permits the read.

### 3. Stop the panel logging itself

The panel polls `/logs` sub-second. Without a `log_skip` it records its own
polling and feeds on itself, so add one — plus any other high-frequency probe
that would otherwise drown the log:

```caddy
@statslogs {
    host stats.example.com
    path /logs /logs/*
}
log_skip @statslogs
```

Check with `caddy adapt --config /etc/caddy/Caddyfile`, then
`sudo systemctl reload caddy`. (Don't run `caddy validate` as a user who can't
read the access log — validate provisions the config and will fail on it.)

### How it stays cheap

The panel is polled several times a second, so `/logs` is a **cursor stream**:
the client sends the highest sequence number it has seen and receives only what
arrived since. A poll against a quiet server is under 80 bytes. Streamed rows
are a compact projection (~150 bytes); the full ~1.3 KB record is fetched only
when you click a row. Polling stops entirely while the tab is backgrounded, the
panel is collapsed, or the dashboard is paused.

The server keeps the most recent 2000 entries per provider in memory (about 4 MB
each) and follows log rotation by inode, so a roll doesn't stall the stream. If a
client falls far enough behind that entries were evicted, the panel marks the
discontinuity rather than silently closing the hole. Each provider gets one
background thread; only the selected one is ever polled by a browser.

For Caddy the site dropdown comes from the admin API (`127.0.0.1:2019`,
read-only, loopback), falling back to the hosts actually seen in the log if it is
unavailable. The admin API is never proxied — it also accepts `POST /load`.

## Security

The endpoints have **no built-in authentication** — they expose host CPU,
memory, disk, load, and uptime, which is reconnaissance-grade information. The
server only binds `127.0.0.1`, so it is not reachable from outside the box until
*you* reverse-proxy it. When you do, restrict access at the proxy:

> **The access-log panel raises the stakes.** With `/logs` enabled the dashboard
> serves visitor IP addresses, request paths, referrers and user-agents for
> *every* site each configured provider covers — not just host metrics. An unprotected
> `/stats` therefore publishes other people's traffic data to anyone who finds
> the hostname. Gate it (`basic_auth`, an `@allowed` matcher, or a VPN-only
> listener) before exposing it, or leave the access log unreadable to the service
> user so the panel stays dark.

- The shipped [`nginx/server-vitals.conf`](nginx/server-vitals.conf) snippet is
  locked to `allow 127.0.0.1; deny all;` by default — widen the allowlist to your
  office/VPN range, or add `auth_basic`, before exposing it publicly.
- For Caddy, gate the routes with `@allowed` matchers or `basic_auth`.

Other hardening already in place: the service runs as an unprivileged user under
a locked-down systemd unit (`NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`); requests are read-only `GET`s; idle/slow client
connections are dropped after a short timeout; and internal errors return a
generic message (details go to the journal, never the client).

## Run locally (no install)

```bash
make run          # python3 server-vitals.py — serves on 127.0.0.1:9999
```

Open <http://127.0.0.1:9999/stats>.

## Run on macOS

The same `server-vitals.py` monitors a Mac — your laptop included. There's
**nothing extra to install** (it uses the system `python3` and `ctypes`; no
Homebrew, no pip) and **no web-server config to set up locally**: the script is
itself the HTTP server, so you hit its port directly.

```bash
git clone https://github.com/dragonworx/server-vitals.git
cd server-vitals
make run          # python3 server-vitals.py — serves on 127.0.0.1:9999
```

Then open <http://127.0.0.1:9999/stats> in your browser, or curl the JSON:

```bash
curl -s http://127.0.0.1:9999/health | python3 -m json.tool
```

That's the whole story for a laptop you're watching live — nginx/Caddy are only
needed when you want to *expose* it on a remote box behind a path like `/stats`.

**Keep it running in the background.** `make install` works on macOS too: it sets
up a launchd service (label `com.dragonworx.server-vitals`) that starts at login
and respawns if it dies — the same `make` workflow as Linux, no manual plist:

```bash
make install                 # per-user LaunchAgent (~/Library/LaunchAgents), no sudo
make install ARGS=--system   # system-wide LaunchDaemon (/Library/LaunchDaemons), sudo
```

What it installs:

- `server-vitals.py` → `/usr/local/bin/` (mode 0755; sudo only if that dir isn't
  writable — on Apple Silicon it falls back to `/opt/homebrew/bin` if needed)
- a launchd plist → `~/Library/LaunchAgents/com.dragonworx.server-vitals.plist`
  (or `/Library/LaunchDaemons/…` with `--system`)
- logs → `~/Library/Logs/server-vitals.log` (or `/var/log/server-vitals.log`)

The installer loads the plist (`launchctl bootstrap`, falling back to `load -w`
on older macOS), then polls `/health` to confirm it came up — re-running is
idempotent. The **LaunchAgent is the right default**: the endpoint binds
`127.0.0.1:9999` only, so nothing is reachable off your Mac and there's no reason
to run as root. Use `--system` only if you need it up at boot before you log in.

Manage it with the same targets below (`make start`/`stop`/`restart`/`status`/
`logs`) and remove it with `make uninstall` (add `ARGS=--system` for a daemon
install). `make run` still gives you a plain foreground process if you'd rather
not install anything.

## Manage

These work on **both** Linux (systemd) and macOS (launchd) — the Makefile detects
the OS and runs the right service-manager command:

```bash
make start        # start the service
make stop         # stop the service
make restart      # restart (no code redeploy)
make deploy       # rebuild: reinstall current server-vitals.py + restart
make status       # service status   (systemctl status / launchctl print)
make logs         # follow logs      (journalctl -u … / tail the launchd log)
make check        # py_compile the source
```

Use `make restart` to bounce the running service; use `make deploy` after
editing `server-vitals.py` to push the new code and restart in one step. On a
macOS `--system` install, add `ARGS=--system` so the manage targets address the
root LaunchDaemon (e.g. `make restart ARGS=--system`).

## Uninstall

```bash
make uninstall                      # remove service + binary (both OSes)

# Linux only:
sudo ./uninstall.sh --purge-nginx   # also remove the nginx snippet

# macOS --system install:
make uninstall ARGS=--system        # remove the root LaunchDaemon instead
```

On macOS this boots out + deletes the launchd plist and the installed binary,
leaving `~/Library/Logs/server-vitals.log` in place.

## Configuration

Knobs live at the top of `server-vitals.py`:

- `LISTEN` — bind address/port (default `127.0.0.1:9999`)
- `SAMPLE_INTERVAL` — seconds between background CPU samples (default `0.25`)
- `REQUEST_TIMEOUT` — seconds before an idle/slow client connection is dropped
  (default `5`)
- `LOG_PROVIDERS` — which access logs the panel follows; see
  [Access logs](#access-logs). `LOG_PROVIDERS_FILE` points at an optional JSON
  override so a host can change this without editing the script.
- `LOG_RING_SIZE` — entries buffered in memory per provider (default `2000`)

After editing, run `make deploy` to push the new code and restart (or
`make restart` if you only need to bounce the running service).

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">Made with ❤️ by dragonworx</p>
