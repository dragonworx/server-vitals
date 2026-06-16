#!/usr/bin/env python3
"""Lightweight health endpoint server. Listens on 127.0.0.1:9999.

Exposes:
  GET /health       - server-wide vitals (cpu, memory, disk, load, uptime)
  GET /code-server  - deep status for the code-server@ubuntu service
  GET /stats        - HTML thin client that polls /stats?format=json
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = ("127.0.0.1", 9999)
CODE_SERVER_UNIT = "code-server@ubuntu"
CODE_SERVER_PORT = 8080
CODE_SERVER_HEALTHZ = "http://127.0.0.1:8080/healthz"


def read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            parts = rest.strip().split()
            if parts:
                info[k] = int(parts[0])  # kB
    return info


def memory_stats():
    m = read_meminfo()
    total = m["MemTotal"]
    available = m.get("MemAvailable", m["MemFree"])
    used = total - available
    swap_total = m.get("SwapTotal", 0)
    swap_free = m.get("SwapFree", 0)
    swap_used = swap_total - swap_free
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "available_mb": round(available / 1024, 1),
        "percent": round(used * 100 / total, 1) if total else 0.0,
        "swap_total_mb": round(swap_total / 1024, 1),
        "swap_used_mb": round(swap_used / 1024, 1),
        "swap_percent": round(swap_used * 100 / swap_total, 1) if swap_total else 0.0,
    }


def cpu_percent(interval=0.3):
    def snap():
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        return idle, sum(fields)

    idle1, total1 = snap()
    time.sleep(interval)
    idle2, total2 = snap()
    dt = total2 - total1
    if dt <= 0:
        return 0.0
    return round((1 - (idle2 - idle1) / dt) * 100, 1)


_cpu_state = {"idle": None, "total": None}
_cpu_lock = threading.Lock()


def cpu_percent_delta():
    """CPU% computed against the last sample. Non-blocking; for /stats polling."""
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:]]
    idle_now = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total_now = sum(fields)
    with _cpu_lock:
        prev_idle = _cpu_state["idle"]
        prev_total = _cpu_state["total"]
        _cpu_state["idle"] = idle_now
        _cpu_state["total"] = total_now
    if prev_idle is None:
        return 0.0
    dt = total_now - prev_total
    if dt <= 0:
        return 0.0
    return round((1 - (idle_now - prev_idle) / dt) * 100, 1)


_cpu_core_state = {}  # core index -> (idle, total) from the previous sample
_cpu_core_lock = threading.Lock()


def cpu_core_percents():
    """Per-core CPU% vs the last sample, indexed by core (cpu0, cpu1, …).

    Mirrors cpu_percent_delta() but for each `cpuN` line in /proc/stat, so the
    /stats client can draw one graph per core. Returns a list ordered by core
    index; first call (no prior sample) yields zeros.
    """
    samples = []
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                # /proc/stat lists all cpuN lines before the first non-cpu line.
                break
            parts = line.split()
            label = parts[0]
            if label == "cpu":
                continue  # aggregate — handled by cpu_percent_delta()
            try:
                idx = int(label[3:])
            except ValueError:
                continue
            fields = [int(x) for x in parts[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            samples.append((idx, idle, sum(fields)))

    out = []
    with _cpu_core_lock:
        for idx, idle, total in samples:
            prev = _cpu_core_state.get(idx)
            _cpu_core_state[idx] = (idle, total)
            if prev is None:
                out.append(0.0)
                continue
            dt = total - prev[1]
            out.append(round((1 - (idle - prev[0]) / dt) * 100, 1) if dt > 0 else 0.0)
    return out


def cpu_count():
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def loadavg():
    with open("/proc/loadavg") as f:
        p = f.readline().split()
    return {"1min": float(p[0]), "5min": float(p[1]), "15min": float(p[2])}


def disk_usage(path="/"):
    s = os.statvfs(path)
    total = s.f_blocks * s.f_frsize
    free = s.f_bavail * s.f_frsize
    used = total - free
    return {
        "mount": path,
        "total_gb": round(total / 1024**3, 2),
        "used_gb": round(used / 1024**3, 2),
        "free_gb": round(free / 1024**3, 2),
        "percent": round(used * 100 / total, 1) if total else 0.0,
    }


def system_uptime():
    with open("/proc/uptime") as f:
        return float(f.readline().split()[0])


def fmt_uptime(seconds):
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h}h {m}m {s}s"


def stats_payload():
    mem = memory_stats()
    disk = disk_usage("/")
    return {
        "timestamp": time.time(),
        "cpu_percent": cpu_percent_delta(),
        "cpu_count": cpu_count(),
        "cpu_cores": cpu_core_percents(),
        "memory_percent": mem["percent"],
        "memory_used_mb": mem["used_mb"],
        "memory_total_mb": mem["total_mb"],
        "disk_percent": disk["percent"],
        "disk_used_gb": disk["used_gb"],
        "disk_total_gb": disk["total_gb"],
    }


def health_payload():
    cpu = cpu_percent()
    mem = memory_stats()
    disk = disk_usage("/")
    status = "ok"
    if cpu >= 95 or mem["percent"] >= 95 or disk["percent"] >= 95:
        status = "degraded"
    return {
        "status": status,
        "timestamp": int(time.time()),
        "uptime_seconds": int(system_uptime()),
        "uptime_human": fmt_uptime(system_uptime()),
        "cpu": {"percent": cpu, "cores": cpu_count()},
        "memory": mem,
        "disk": disk,
        "load_average": loadavg(),
    }


def run_cmd(args, timeout=3):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except Exception as e:
        return f"error: {e}", -1


def systemd_props(unit, properties):
    args = ["systemctl", "show", unit] + sum((["-p", p] for p in properties), [])
    out, _ = run_cmd(args)
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return props


def port_open(host, port, timeout=1.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def http_probe(url, timeout=3):
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        # HTTPError is still a "responded" signal — record the code.
        status = e.code
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "status": status, "latency_ms": round((time.monotonic() - t0) * 1000, 1)}


def proc_info(pid):
    if not pid or pid == "0":
        return {}
    info = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith(("State:", "VmRSS:", "VmSize:", "VmPeak:", "Threads:", "voluntary_ctxt_switches:", "nonvoluntary_ctxt_switches:")):
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
    except FileNotFoundError:
        return {"error": "pid not found"}
    except Exception as e:
        return {"error": str(e)}
    try:
        out, _ = run_cmd(["pgrep", "-c", "-P", pid])
        info["direct_children"] = int(out) if out.isdigit() else 0
    except Exception:
        pass
    return info


def code_server_payload():
    is_active, _ = run_cmd(["systemctl", "is-active", CODE_SERVER_UNIT])
    is_enabled, _ = run_cmd(["systemctl", "is-enabled", CODE_SERVER_UNIT])
    props = systemd_props(
        CODE_SERVER_UNIT,
        [
            "ActiveState", "SubState", "LoadState", "Result",
            "MainPID", "TasksCurrent", "TasksMax",
            "MemoryCurrent", "MemoryPeak", "CPUUsageNSec",
            "ActiveEnterTimestamp", "ActiveExitTimestamp",
            "NRestarts", "ExecMainStartTimestamp",
        ],
    )

    pid = props.get("MainPID", "0")
    process = proc_info(pid)

    listening = port_open("127.0.0.1", CODE_SERVER_PORT)
    http = http_probe(CODE_SERVER_HEALTHZ)

    journal_out, _ = run_cmd(
        ["journalctl", "-u", CODE_SERVER_UNIT, "-n", "10", "--no-pager", "-o", "short-iso"],
        timeout=4,
    )
    recent_log_lines = journal_out.splitlines() if journal_out else []

    def parse_int(s):
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    mem_bytes = parse_int(props.get("MemoryCurrent"))
    mem_peak = parse_int(props.get("MemoryPeak"))
    cpu_nsec = parse_int(props.get("CPUUsageNSec"))

    overall = "ok"
    reasons = []
    if is_active != "active":
        overall = "down"
        reasons.append(f"systemd state: {is_active}")
    if not listening:
        overall = "down" if overall == "down" else "degraded"
        reasons.append(f"port {CODE_SERVER_PORT} not listening")
    if not http.get("ok"):
        overall = "degraded" if overall == "ok" else overall
        reasons.append(f"http probe failed: {http.get('error') or http.get('status')}")

    return {
        "service": CODE_SERVER_UNIT,
        "overall": overall,
        "reasons": reasons,
        "systemd": {
            "is_active": is_active,
            "is_enabled": is_enabled,
            "active_state": props.get("ActiveState"),
            "sub_state": props.get("SubState"),
            "load_state": props.get("LoadState"),
            "result": props.get("Result"),
            "main_pid": pid,
            "tasks_current": props.get("TasksCurrent"),
            "tasks_max": props.get("TasksMax"),
            "memory_bytes": mem_bytes,
            "memory_mb": round(mem_bytes / 1024**2, 1) if mem_bytes else None,
            "memory_peak_mb": round(mem_peak / 1024**2, 1) if mem_peak else None,
            "cpu_seconds": round(cpu_nsec / 1e9, 2) if cpu_nsec else None,
            "started_at": props.get("ActiveEnterTimestamp") or props.get("ExecMainStartTimestamp"),
            "exited_at": props.get("ActiveExitTimestamp") or None,
            "n_restarts": props.get("NRestarts"),
        },
        "process": process,
        "network": {
            "port": CODE_SERVER_PORT,
            "listening": listening,
            "http_probe_url": CODE_SERVER_HEALTHZ,
            "http_probe": http,
        },
        "recent_log": recent_log_lines,
    }


STATS_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>server stats</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; padding: 0; background: #0b0d10; color: #d8dde3;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  header { padding: 12px 18px; border-bottom: 1px solid #20262d;
    display: flex; gap: 18px; align-items: center; }
  header h1 { margin: 0; font-size: 14px; font-weight: 600; letter-spacing: .04em;
    text-transform: uppercase; color: #9ba6b2; white-space: nowrap; }
  header .meta { font-size: 12px; color: #6c7886; white-space: nowrap; }
  header .meta .ok { color: #6ddc8a; }
  header .meta .err { color: #ff6b6b; }
  header .controls { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  header .controls .ctl { display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: #9ba6b2; }
  header .controls select { background: #11151a; color: #d8dde3;
    border: 1px solid #20262d; border-radius: 4px; padding: 3px 6px;
    font-family: inherit; font-size: 11px; cursor: pointer; line-height: 1; }
  header .controls select:hover { border-color: #2a323b; }
  main { padding: 12px 18px 24px; display: grid; gap: 14px; }
  .panel { background: #11151a; border: 1px solid #20262d; border-radius: 6px;
    padding: 10px 12px 4px; }
  .panel-head { display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 4px; }
  .panel-title { font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    color: #9ba6b2; }
  .panel-value { font-size: 13px; color: #e7ebf0; }
  .panel-value .sub { color: #6c7886; margin-left: 8px; font-size: 11px; }
  svg { display: block; width: 100%; height: 160px; overflow: visible; }
  .grid { stroke: #20262d; stroke-width: 1; }
  .axis { fill: #6c7886; font-size: 10px; font-family: inherit; }
  .line-cpu  { stroke: #6ddc8a; }
  .line-mem  { stroke: #6db5dc; }
  .line-disk { stroke: #dcb86d; }
  .line { fill: none; stroke-width: 1.5; }
  .fill-cpu  { fill: rgba(109,220,138,.10); }
  .fill-mem  { fill: rgba(109,181,220,.10); }
  .fill-disk { fill: rgba(220,184,109,.10); }
  .gap       { fill: rgba(255,107,107,.18); }
  .gap-edge  { stroke: rgba(255,107,107,.55); stroke-width: 1; stroke-dasharray: 2 2; }
  .peak-label { font-size: 10px; font-family: inherit; paint-order: stroke fill;
    stroke: #11151a; stroke-width: 3px; stroke-linejoin: round; }
  .peak-label.peak-cpu  { fill: #b8eec5; }
  .peak-label.peak-mem  { fill: #b8d8ee; }
  .peak-label.peak-disk { fill: #eed9b8; }
  /* per-core CPU small-multiples */
  .cores { display: grid; gap: 8px; margin: 2px 0 6px;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }
  .core { background: #0e1216; border: 1px solid #1b2128; border-radius: 5px;
    padding: 4px 7px 2px; }
  .core-head { display: flex; justify-content: space-between; align-items: baseline; }
  .core-head .core-name { font-size: 10px; letter-spacing: .04em;
    text-transform: uppercase; color: #6c7886; }
  .core-head .core-val { font-size: 11px; color: #b8eec5; }
  svg.core-svg { height: 48px; }
</style>
</head>
<body>
<header>
  <h1>server stats</h1>
  <div class="meta">status: <span id="status" class="ok">connecting…</span></div>
  <div class="controls">
    <span class="ctl">poll
      <select id="poll-sel">
        <option value="0.25">0.25s</option>
        <option value="0.5">0.5s</option>
        <option value="1">1s</option>
        <option value="5">5s</option>
        <option value="10">10s</option>
      </select>
    </span>
    <span class="ctl">window
      <select id="window-sel">
        <option value="1">1m</option>
        <option value="5">5m</option>
        <option value="10">10m</option>
        <option value="30">30m</option>
        <option value="60">60m</option>
      </select>
    </span>
  </div>
</header>
<main>
  <section class="panel" id="cpu-panel">
    <div class="panel-head">
      <div class="panel-title">CPU</div>
      <div class="panel-value"><span id="cpu-now">—</span><span class="sub" id="cpu-sub"></span></div>
    </div>
    <svg id="cpu-svg"></svg>
  </section>
  <section class="panel" id="cores-panel">
    <div class="panel-head">
      <div class="panel-title">CPU Cores · <span id="cores-count">—</span></div>
    </div>
    <div class="cores" id="cores"></div>
  </section>
  <section class="panel" id="mem-panel">
    <div class="panel-head">
      <div class="panel-title">Memory</div>
      <div class="panel-value"><span id="mem-now">—</span><span class="sub" id="mem-sub"></span></div>
    </div>
    <svg id="mem-svg"></svg>
  </section>
  <section class="panel" id="disk-panel">
    <div class="panel-head">
      <div class="panel-title">Disk</div>
      <div class="panel-value"><span id="disk-now">—</span><span class="sub" id="disk-sub"></span></div>
    </div>
    <svg id="disk-svg"></svg>
  </section>
</main>
<script>
(() => {
  const FETCH_TIMEOUT_MS = 2000;
  const BASE_PAD_L = 44, BASE_PAD_R = 8, BASE_PAD_T = 8, BASE_PAD_B = 18;

  const POLL_OPTIONS = [0.25, 0.5, 1, 5, 10];   // seconds
  const WINDOW_OPTIONS = [1, 5, 10, 30, 60];    // minutes
  const POLL_KEY = 'stats:poll:sec';
  const WINDOW_KEY = 'stats:window:min';

  function loadChoice(key, options, dflt) {
    try {
      const v = parseFloat(localStorage.getItem(key));
      if (options.includes(v)) return v;
    } catch (e) {}
    return dflt;
  }
  function persist(key, value) {
    try { localStorage.setItem(key, String(value)); } catch (e) {}
  }

  let pollSec = loadChoice(POLL_KEY, POLL_OPTIONS, 1);
  let windowMin = loadChoice(WINDOW_KEY, WINDOW_OPTIONS, 5);
  let pollMs = pollSec * 1000;
  // Points kept = window seconds / poll seconds.
  let MAX_POINTS = Math.max(2, Math.round(windowMin * 60 / pollSec));

  const series = {
    cpu:  { data: [], color: 'cpu',  unit: '%' },
    mem:  { data: [], color: 'mem',  unit: '%' },
    disk: { data: [], color: 'disk', unit: '%' },
  };
  // null in any data array represents a failed/timed-out poll at that slot.

  // Per-core CPU series + value labels, built lazily once we know the core count.
  const coreSeries = [];   // [{ data: [] }, …]  parallel to cpu_cores[]
  const coreValEls = [];   // matching .core-val spans
  function ensureCores(count) {
    if (coreSeries.length === count) return;
    const host = el('cores');
    host.textContent = '';
    coreSeries.length = 0;
    coreValEls.length = 0;
    el('cores-count').textContent = count + (count === 1 ? ' core' : ' cores');
    for (let i = 0; i < count; i++) {
      const cell = document.createElement('div');
      cell.className = 'core';
      const head = document.createElement('div');
      head.className = 'core-head';
      const name = document.createElement('span');
      name.className = 'core-name';
      name.textContent = 'cpu' + i;
      const val = document.createElement('span');
      val.className = 'core-val';
      val.textContent = '—';
      head.appendChild(name);
      head.appendChild(val);
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'core-svg');
      svg.id = 'core-svg-' + i;
      cell.appendChild(head);
      cell.appendChild(svg);
      host.appendChild(cell);
      coreSeries.push({ data: [], color: 'cpu', unit: '%' });
      coreValEls.push(val);
    }
  }

  let consecutiveFailures = 0;
  let lastError = null;

  const SVGNS = 'http://www.w3.org/2000/svg';
  const el = id => document.getElementById(id);
  const svgEl = (name, attrs) => {
    const e = document.createElementNS(SVGNS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  function niceStep(range) {
    if (range <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(range)));
    const n = range / exp;
    let step;
    if (n < 1.5) step = 0.2;
    else if (n < 3) step = 0.5;
    else if (n < 7) step = 1;
    else step = 2;
    return step * exp;
  }

  function fmtY(v) {
    const a = Math.abs(v);
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }

  // Pretty-print an age in seconds for the x-axis: under a minute stays in
  // seconds, otherwise minutes (one decimal, trailing .0 dropped).
  function fmtAge(sec) {
    if (sec <= 0) return 'now';
    if (sec < 60) return '-' + Math.round(sec) + 's';
    const m = Math.round((sec / 60) * 10) / 10;
    return '-' + (m % 1 === 0 ? m.toFixed(0) : m.toFixed(1)) + 'm';
  }

  // Label a sample only when it dominates a ±WIN window AND its prominence
  // (apex - lowest neighbor in window) clears max(MIN_ABS, PROM × y-span).
  // The window cap forces ≥WIN-sample spacing between labels; the prominence
  // gate kills shallow ripples on noisy plateaus. The right-edge guard waits
  // WIN samples before confirming a peak so the live edge doesn't flicker.
  function findPeaks(data, yMin, yMax) {
    const WIN = 6;
    const PROM = 0.08;
    const MIN_ABS = 0.3;
    const ySpan = Math.max(1e-9, yMax - yMin);
    const minProm = Math.max(MIN_ABS, PROM * ySpan);
    const peaks = [];
    const n = data.length;
    for (let i = WIN; i < n - WIN; i++) {
      const v = data[i];
      if (v == null) continue;
      let isMax = true;
      let lo = Infinity;
      for (let k = i - WIN; k <= i + WIN; k++) {
        if (k === i) continue;
        const u = data[k];
        if (u == null) continue;
        // strict on the left, ≥ on the right → flat plateaus collapse to
        // their leftmost (oldest) sample, so a flat top gets exactly one label
        if (k < i ? u >= v : u > v) { isMax = false; break; }
        if (u < lo) lo = u;
      }
      if (!isMax) continue;
      if (lo === Infinity) continue;
      if (v - lo < minProm) continue;
      peaks.push(i);
    }
    return peaks;
  }

  function render(svgId, ser, opts) {
    opts = opts || {};
    const compact = !!opts.compact;  // small-multiple mode: no axes/peak labels
    const PAD_L = compact ? 6 : BASE_PAD_L;
    const PAD_R = compact ? 6 : BASE_PAD_R;
    const PAD_T = compact ? 4 : BASE_PAD_T;
    const PAD_B = compact ? 4 : BASE_PAD_B;
    const svg = el(svgId);
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    // Match SVG user-space to actual rendered pixels — no viewBox, no scaling.
    const rect = svg.getBoundingClientRect();
    const W = Math.max(1, Math.round(rect.width));
    const H = Math.max(1, Math.round(rect.height));
    svg.setAttribute('width', W);
    svg.setAttribute('height', H);
    const PLOT_W = W - PAD_L - PAD_R;
    const PLOT_H = H - PAD_T - PAD_B;
    if (PLOT_W <= 0 || PLOT_H <= 0) return;

    const data = ser.data;
    if (data.length < 2) return;

    let lo = Infinity, hi = -Infinity, hasValue = false;
    for (const v of data) {
      if (v == null) continue;
      hasValue = true;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (!hasValue) { lo = 0; hi = 1; }
    let range = hi - lo;
    if (range < 0.5) { // floor for visual breathing room
      const mid = (hi + lo) / 2;
      lo = mid - 0.5; hi = mid + 0.5; range = 1;
    }
    // Wider top pad than bottom so peak labels have room to sit above apexes.
    let yMin = lo - range * 0.08, yMax = hi + range * 0.18;
    if (yMin < 0) yMin = 0;
    range = yMax - yMin;
    if (range <= 0) { yMax = yMin + 1; range = 1; }
    // Pin to a fixed scale (per-core graphs share 0..100 so they're comparable).
    if (opts.fixedMax != null) { yMin = 0; yMax = opts.fixedMax; range = yMax - yMin; }

    // y-axis grid (skipped in compact small-multiple mode)
    if (!compact) {
      const step = niceStep(range / 3);
      const tickStart = Math.ceil(yMin / step) * step;
      for (let t = tickStart; t <= yMax; t += step) {
        const y = PAD_T + (1 - (t - yMin) / range) * PLOT_H;
        svg.appendChild(svgEl('line', {
          x1: PAD_L, x2: W - PAD_R, y1: y, y2: y, class: 'grid',
        }));
        const lbl = svgEl('text', {
          x: PAD_L - 4, y: y + 3, class: 'axis', 'text-anchor': 'end',
        });
        lbl.textContent = fmtY(t) + ser.unit;
        svg.appendChild(lbl);
      }
    }

    // x-axis labels — the plot spans the full configured window.
    const windowSec = windowMin * 60;
    const xLabels = compact ? [] : [
      { x: W - PAD_R, t: 'now', anchor: 'end' },
      { x: PAD_L + PLOT_W / 2, t: fmtAge(windowSec / 2), anchor: 'middle' },
      { x: PAD_L, t: fmtAge(windowSec), anchor: 'start' },
    ];
    for (const l of xLabels) {
      const t = svgEl('text', {
        x: l.x, y: H - 4, class: 'axis', 'text-anchor': l.anchor,
      });
      t.textContent = l.t;
      svg.appendChild(t);
    }

    // line + fill, broken into segments around null runs.
    const n = data.length;
    const xStep = PLOT_W / (MAX_POINTS - 1);
    const xOffset = PAD_L + PLOT_W - (n - 1) * xStep;
    const baseY = PAD_T + PLOT_H;
    const xAt = i => xOffset + i * xStep;
    const yAt = v => PAD_T + (1 - (v - yMin) / range) * PLOT_H;

    // 1. Draw gap bands for runs of null values.
    let i = 0;
    while (i < n) {
      if (data[i] == null) {
        const start = i;
        while (i < n && data[i] == null) i++;
        const end = i - 1;
        // Cover at least half a slot on either side so 1-sample gaps are visible.
        const x0 = xAt(start) - xStep / 2;
        const x1 = xAt(end) + xStep / 2;
        // Clamp inside plot region.
        const cx0 = Math.max(PAD_L, x0);
        const cx1 = Math.min(PAD_L + PLOT_W, x1);
        if (cx1 > cx0) {
          svg.appendChild(svgEl('rect', {
            x: cx0, y: PAD_T, width: cx1 - cx0, height: PLOT_H, class: 'gap',
          }));
          // Dashed vertical edges to mark gap boundaries.
          if (cx0 > PAD_L) svg.appendChild(svgEl('line', {
            x1: cx0, x2: cx0, y1: PAD_T, y2: baseY, class: 'gap-edge',
          }));
          if (cx1 < PAD_L + PLOT_W) svg.appendChild(svgEl('line', {
            x1: cx1, x2: cx1, y1: PAD_T, y2: baseY, class: 'gap-edge',
          }));
        }
      } else {
        i++;
      }
    }

    // 2. Draw line+fill segments between gaps.
    i = 0;
    while (i < n) {
      while (i < n && data[i] == null) i++;
      const segStart = i;
      while (i < n && data[i] != null) i++;
      const segEnd = i - 1;
      if (segEnd > segStart) { // need at least 2 points to draw a segment
        let d = '';
        let area = 'M' + xAt(segStart).toFixed(2) + ' ' + baseY.toFixed(2) + ' L';
        for (let k = segStart; k <= segEnd; k++) {
          const x = xAt(k), y = yAt(data[k]);
          d += (k === segStart ? 'M' : 'L') + x.toFixed(2) + ' ' + y.toFixed(2) + ' ';
          area += x.toFixed(2) + ' ' + y.toFixed(2) + ' ';
        }
        area += 'L' + xAt(segEnd).toFixed(2) + ' ' + baseY.toFixed(2) + ' Z';
        svg.appendChild(svgEl('path', { d: area, class: 'fill-' + ser.color }));
        svg.appendChild(svgEl('path', { d: d, class: 'line line-' + ser.color }));
      } else if (segEnd === segStart) {
        // single non-null point sandwiched between gaps — show as a dot
        const dotFill = { cpu: '#6ddc8a', mem: '#6db5dc', disk: '#dcb86d' }[ser.color];
        svg.appendChild(svgEl('circle', {
          cx: xAt(segStart), cy: yAt(data[segStart]), r: 1.6, fill: dotFill,
        }));
      }
    }

    // 3. Apex labels at qualifying peaks (sparse, prominence-gated).
    const peaks = compact ? [] : findPeaks(data, yMin, yMax);
    if (peaks.length) {
      const dotFill = { cpu: '#6ddc8a', mem: '#6db5dc', disk: '#dcb86d' }[ser.color];
      for (const i of peaks) {
        const x = xAt(i), y = yAt(data[i]);
        svg.appendChild(svgEl('circle', { cx: x, cy: y, r: 2, fill: dotFill }));
        // Sit the label just above the apex; clamp inside the plot region.
        let lblX = x, anchor = 'middle';
        if (lblX < PAD_L + 14) { lblX = PAD_L + 2; anchor = 'start'; }
        else if (lblX > W - PAD_R - 14) { lblX = W - PAD_R - 2; anchor = 'end'; }
        // Flip below the apex if there isn't room above without clipping.
        const lblY = (y - 6 < PAD_T + 9) ? (y + 13) : (y - 6);
        const t = svgEl('text', {
          x: lblX, y: lblY,
          class: 'peak-label peak-' + ser.color,
          'text-anchor': anchor,
        });
        t.textContent = fmtY(data[i]) + ser.unit;
        svg.appendChild(t);
      }
    }
  }

  function renderAll() {
    render('cpu-svg',  series.cpu);
    render('mem-svg',  series.mem);
    render('disk-svg', series.disk);
    for (let i = 0; i < coreSeries.length; i++) {
      render('core-svg-' + i, coreSeries[i], { compact: true, fixedMax: 100 });
    }
  }

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAll, 80);
  });

  function trimAll() {
    const all = Object.keys(series).map(k => series[k]).concat(coreSeries);
    for (const s of all) {
      if (s.data.length > MAX_POINTS) s.data.splice(0, s.data.length - MAX_POINTS);
    }
  }

  const pollSel = el('poll-sel');
  const windowSel = el('window-sel');
  pollSel.value = String(pollSec);
  windowSel.value = String(windowMin);

  pollSel.addEventListener('change', () => {
    pollSec = parseFloat(pollSel.value);
    pollMs = pollSec * 1000;
    MAX_POINTS = Math.max(2, Math.round(windowMin * 60 / pollSec));
    persist(POLL_KEY, pollSec);
    trimAll();
    // Restart the poll loop immediately at the new cadence.
    clearTimeout(pollTimer);
    scheduleNext(0);
    renderAll();
  });

  windowSel.addEventListener('change', () => {
    windowMin = parseFloat(windowSel.value);
    MAX_POINTS = Math.max(2, Math.round(windowMin * 60 / pollSec));
    persist(WINDOW_KEY, windowMin);
    trimAll();
    renderAll();
  });

  function push(name, v) {
    const s = series[name];
    s.data.push(v);
    if (s.data.length > MAX_POINTS) s.data.shift();
  }

  function pushCore(i, v) {
    const s = coreSeries[i];
    s.data.push(v);
    if (s.data.length > MAX_POINTS) s.data.shift();
  }

  function pushFailureSlot() {
    push('cpu',  null);
    push('mem',  null);
    push('disk', null);
    for (let i = 0; i < coreSeries.length; i++) {
      pushCore(i, null);
      coreValEls[i].textContent = '—';
    }
  }

  function fetchWithTimeout(url, ms) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new Error('timeout')), ms);
    return fetch(url, { cache: 'no-store', signal: ctrl.signal })
      .finally(() => clearTimeout(timer));
  }

  async function tick() {
    let ok = false;
    try {
      const r = await fetchWithTimeout('/stats?format=json', FETCH_TIMEOUT_MS);
      if (!r.ok) throw new Error('http ' + r.status);
      const j = await r.json();
      push('cpu',  j.cpu_percent);
      push('mem',  j.memory_percent);
      push('disk', j.disk_percent);

      const cores = Array.isArray(j.cpu_cores) ? j.cpu_cores : [];
      if (cores.length) {
        ensureCores(cores.length);
        for (let i = 0; i < cores.length; i++) {
          pushCore(i, cores[i]);
          coreValEls[i].textContent = cores[i].toFixed(0) + '%';
        }
      }

      el('cpu-now').textContent  = j.cpu_percent.toFixed(1) + '%';
      el('cpu-sub').textContent  = '';
      el('mem-now').textContent  = j.memory_percent.toFixed(1) + '%';
      el('mem-sub').textContent  = j.memory_used_mb.toFixed(0) + ' / ' +
                                   j.memory_total_mb.toFixed(0) + ' MB';
      el('disk-now').textContent = j.disk_percent.toFixed(1) + '%';
      el('disk-sub').textContent = j.disk_used_gb.toFixed(2) + ' / ' +
                                   j.disk_total_gb.toFixed(2) + ' GB';

      consecutiveFailures = 0;
      lastError = null;
      ok = true;
    } catch (e) {
      pushFailureSlot();
      consecutiveFailures++;
      // AbortError shows up as DOMException with name 'AbortError'.
      lastError = (e && e.name === 'AbortError') ? 'timeout'
                : (e && e.message) ? e.message
                : String(e);
      el('cpu-now').textContent  = '—';
      el('mem-now').textContent  = '—';
      el('disk-now').textContent = '—';
    }

    const s = el('status');
    if (ok) {
      s.textContent = 'live'; s.className = 'ok';
    } else {
      s.textContent = 'stalled (' + consecutiveFailures + ' polls, ' + lastError + ')';
      s.className = 'err';
    }
    renderAll();

    scheduleNext();
  }

  let pollTimer = null;
  function scheduleNext(delay) {
    pollTimer = setTimeout(tick, delay == null ? pollMs : delay);
  }

  scheduleNext(0);
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "health-server/1.0"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, payload, code=200):
        body = json.dumps(payload, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        raw = self.path
        path = raw.split("?", 1)[0].rstrip("/")
        query = raw.split("?", 1)[1] if "?" in raw else ""
        try:
            if path in ("/health", ""):
                self._send_json(health_payload())
            elif path == "/code-server":
                self._send_json(code_server_payload())
            elif path == "/stats":
                if "format=json" in query:
                    self._send_json(stats_payload())
                else:
                    self._send_html(STATS_HTML)
            else:
                self._send_json({"error": "not found", "path": self.path}, code=404)
        except Exception as e:
            self._send_json({"error": "internal", "detail": str(e)}, code=500)


def main():
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
