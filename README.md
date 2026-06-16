# health-server

A tiny, dependency-free server vitals endpoint. One Python file, standard
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

## Install

**From a checkout** (recommended):

```bash
git clone <repo-url> health-server
cd health-server
sudo ./install.sh            # or: make install
```

Add `--with-nginx` to also install the reverse-proxy snippet:

```bash
sudo ./install.sh --with-nginx
```

**One-liner** (when hosted — point it at the repo's raw base URL):

```bash
curl -fsSL <raw>/install.sh | HEALTH_SERVER_RAW_BASE=<raw> sudo -E bash
```

The installer copies `health-server.py` to `/usr/local/bin`, installs the
`health-server` systemd unit, enables + starts it, and verifies `/health`
responds.

### install.sh options

| Option          | Effect                                                |
| --------------- | ----------------------------------------------------- |
| `--with-nginx`  | also install `nginx/health-endpoints.conf` and reload |
| `--no-start`    | install files but don't enable/start the service      |
| `--user USER`   | run the service as `USER` (default `www-data`)        |

## nginx integration

The snippet at [`nginx/health-endpoints.conf`](nginx/health-endpoints.conf)
proxies `/health`, `/code-server`, and `/stats` to `127.0.0.1:9999`. After
installing it (`--with-nginx`), `include` it in any server block:

```nginx
server {
    # ...
    include snippets/health-endpoints.conf;
}
```

Then `sudo nginx -t && sudo systemctl reload nginx`.

## Run locally (no install)

```bash
make run          # python3 health-server.py — serves on 127.0.0.1:9999
```

Open <http://127.0.0.1:9999/stats>.

## Manage

```bash
make status       # systemctl status
make logs         # journalctl -u health-server -f
make restart      # restart the service
make check        # py_compile the source
```

## Uninstall

```bash
sudo ./uninstall.sh                 # remove service + binary
sudo ./uninstall.sh --purge-nginx   # also remove the nginx snippet
```

## Configuration

Knobs live at the top of `health-server.py`:

- `LISTEN` — bind address/port (default `127.0.0.1:9999`)
- `CODE_SERVER_UNIT` / `CODE_SERVER_PORT` / `CODE_SERVER_HEALTHZ` — the unit the
  `/code-server` probe inspects

After editing, re-run `sudo ./install.sh` (or `make restart` if only restarting).

## License

MIT — see [LICENSE](LICENSE).
