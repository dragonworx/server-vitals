# Server Vitals

A tiny, dependency-free server health endpoint. One Python file, standard
library only — no pip installs, no virtualenv. It exposes a few JSON health
endpoints and a self-contained live **stats dashboard** (CPU incl. per-core,
memory, disk) that you can drop behind nginx or hit directly.

Built for a single VPS: it listens on `127.0.0.1:9999` and is meant to be
reverse-proxied at paths like `/health` and `/stats`.

## Endpoints

| Path           | Returns                                                                 |
| -------------- | ----------------------------------------------------------------------- |
| `/health`      | JSON: cpu, memory, disk, load average, uptime, overall `ok/degraded`    |
| `/code-server` | JSON: deep status of the `code-server@ubuntu` systemd unit              |
| `/stats`       | HTML live dashboard (polls `/stats?format=json`)                        |
| `/stats?format=json` | JSON sample: cpu %, **per-core cpu %**, memory, disk             |

The `/stats` dashboard is a single HTML page with no external assets. It draws
CPU (with one mini-graph per core, auto-detected), memory, and disk as live
SVG sparklines. Poll interval (0.25s–10s) and time window (1–60 min) are
selectable from the header and persisted in `localStorage`.

## Requirements

- Linux with `/proc` (uses `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`)
- `python3` (standard library only)
- `systemd` (for the service) — optional `nginx` for reverse proxying

## Why a systemd service, not a Docker container

Server Vitals is a **host-monitoring agent**, so it is deployed as a plain process
under systemd — *not* in a container. This is deliberate. The whole job of the
app is to observe the host it runs on:

- It reads the host kernel directly: `/proc/stat`, `/proc/meminfo`,
  `/proc/loadavg`, and `statvfs("/")` for CPU / memory / load / disk.
- It shells out to the host's `systemctl`, `journalctl`, and `pgrep` to report
  deep status of the `code-server@ubuntu` unit (`/code-server`).

A container's value is **isolation** — its own filesystem, PID namespace, and
network stack, separate from the host. That is exactly the wrong default here:

| Concern | systemd service (this project) | Docker container |
| --- | --- | --- |
| Sees real host CPU / mem / disk | ✅ directly | ❌ sees the container namespace unless you bind-mount `/proc`, `/`, … |
| Inspect host systemd units (`systemctl` / `journalctl`) | ✅ works | ❌ no systemd/journal inside — `/code-server` breaks without mounting host sockets |
| Lifecycle / autostart / restart | systemd (`enable --now`, `Restart=`) | Docker daemon (`restart: unless-stopped`) |
| Footprint | ~none — one stdlib Python process | image build + daemon overhead |

To run Server Vitals usefully in a container you would have to dismantle that
isolation (`--pid=host`, `-v /proc:/host/proc:ro`, `-v /:/rootfs:ro`, expose the
systemd/journal sockets) **and** rewrite the metric paths to read `/host/proc/*`
— more moving parts for a strictly less capable result. So it ships as a
service. Containers remain the right tool for workloads you want *isolated from*
the host (web apps, databases); a host agent is the opposite case.

> If you genuinely need it containerized anyway (e.g. a constrained PaaS), run
> with `--pid=host --network=host`, bind-mount `/proc` and `/` read-only, and
> expect `/code-server`'s systemd introspection to be unavailable.

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
proxies `/health`, `/code-server`, and `/stats` to `127.0.0.1:9999`. After
installing it (`--with-nginx`), `include` it in any server block:

```nginx
server {
    # ...
    include snippets/server-vitals.conf;
}
```

Then `sudo nginx -t && sudo systemctl reload nginx`.

## Run locally (no install)

```bash
make run          # python3 server-vitals.py — serves on 127.0.0.1:9999
```

Open <http://127.0.0.1:9999/stats>.

## Manage

```bash
make start        # start the service
make stop         # stop the service
make restart      # restart (no code redeploy)
make deploy       # rebuild: reinstall current server-vitals.py + restart
make status       # systemctl status
make logs         # journalctl -u server-vitals -f
make check        # py_compile the source
```

Use `make restart` to bounce the running service; use `make deploy` after
editing `server-vitals.py` to push the new code and restart in one step.

### Via Bun

If you prefer Bun, the same targets are exposed as `package.json` scripts that
just wrap the Makefile — so there are **no JS dependencies** and `bun install`
is not needed:

```bash
bun run start      # or: bun start
bun run restart
bun run deploy     # rebuild: reinstall server-vitals.py + restart
bun run logs
bun run dev        # run in the foreground (python3 server-vitals.py)
```

`start` / `stop` / `restart` / `deploy` shell out to `sudo systemctl`, so
they'll prompt for your sudo password. The Makefile stays the source of truth;
Bun is just an alternate front door.

## Uninstall

```bash
sudo ./uninstall.sh                 # remove service + binary
sudo ./uninstall.sh --purge-nginx   # also remove the nginx snippet
```

## Configuration

Knobs live at the top of `server-vitals.py`:

- `LISTEN` — bind address/port (default `127.0.0.1:9999`)
- `CODE_SERVER_UNIT` / `CODE_SERVER_PORT` / `CODE_SERVER_HEALTHZ` — the unit the
  `/code-server` probe inspects

After editing, run `make deploy` to push the new code and restart (or
`make restart` if you only need to bounce the running service).

## License

MIT — see [LICENSE](LICENSE).
