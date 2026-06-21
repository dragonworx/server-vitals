#!/usr/bin/env python3
"""Server Vitals — lightweight server health endpoint. Listens on 127.0.0.1:9999.

Exposes:
  GET /health       - server-wide vitals (cpu, memory, disk, load, uptime)
  GET /stats        - HTML thin client that polls /stats?format=json
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = ("127.0.0.1", 9999)


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
        "load_average": loadavg(),
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


STATS_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Server Vitals</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; padding: 0; height: 100%; background: #0b0d10; color: #d8dde3;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  /* Fill the viewport: fixed header, panels share the remaining height evenly. */
  body { display: flex; flex-direction: column; overflow: hidden; }
  header { flex: 0 0 auto; padding: 12px 18px; border-bottom: 1px solid #20262d;
    display: flex; gap: 18px; align-items: center; }
  header h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: .14em;
    font-family: "Avenir Next", "Segoe UI", system-ui, -apple-system,
      "Helvetica Neue", Arial, sans-serif;
    white-space: nowrap;
    /* A wave of light/dark grey sweeps through the title. One cycle takes
       --pulse-duration (set from JS to the poll interval — one sweep per poll).
       The bright band is the wave crest moving across the text. */
    background: linear-gradient(100deg,
      #5b636d 0%, #5b636d 38%, #828a94 50%, #5b636d 62%, #5b636d 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
    animation: title-wave var(--pulse-duration, 2000ms) linear infinite; }
  /* Travel exactly one tile width (200%) per cycle, so precisely one bright
     crest sweeps across the title each --pulse-duration (= one poll interval). */
  @keyframes title-wave {
    from { background-position: 200% 0; }
    to   { background-position: 0% 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    header h1 { animation: none; -webkit-text-fill-color: #cfd6de; color: #cfd6de; }
  }
  header .meta { font-size: 12px; color: #6c7886; white-space: nowrap; }
  /* Fine-grained server load status — greens → oranges → reds. */
  header .meta .status { font-weight: 600; letter-spacing: .05em;
    text-transform: uppercase; }
  header .meta .st-idle     { color: #6ddc8a; }   /* green  — at rest        */
  header .meta .st-light    { color: #b6e36a; }   /* green  — light load     */
  header .meta .st-heavy    { color: #ffae57; }   /* orange — heavy load     */
  header .meta .st-critical { color: #ff6b6b; }   /* red    — near limits    */
  header .meta .st-hung     { color: #e23b3b; animation: hung-blink 1s steps(2) infinite; }
  @keyframes hung-blink { 50% { opacity: .35; } }
  header .controls { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  header .controls .ctl { display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: #9ba6b2; }
  header .controls select { background: #11151a; color: #d8dde3;
    border: 1px solid #20262d; border-radius: 4px; padding: 3px 6px;
    font-family: inherit; font-size: 11px; cursor: pointer; line-height: 1; }
  header .controls select:hover { border-color: #2a323b; }
  header button.ctl-btn { background: #11151a; color: #d8dde3;
    border: 1px solid #20262d; border-radius: 4px; padding: 2px 7px;
    font-family: inherit; font-size: 12px; line-height: 1; cursor: pointer;
    display: inline-flex; align-items: center; }
  header button.ctl-btn:hover { border-color: #2a323b; }
  /* Paused: button glows green and the title strobe halts to signal "frozen". */
  header button.ctl-btn.paused { color: #6ddc8a; border-color: #2f6b41; }
  body.paused header h1 { animation-play-state: paused; }
  main { flex: 1 1 auto; min-height: 0; padding: 12px 18px;
    display: grid; grid-template-rows: repeat(4, 1fr); gap: 12px; }
  .panel { background: #11151a; border: 1px solid #20262d; border-radius: 6px;
    padding: 8px 12px 6px; display: flex; flex-direction: column; min-height: 0; }
  .panel-head { flex: 0 0 auto; display: flex; justify-content: space-between;
    align-items: baseline; margin-bottom: 4px; }
  .panel-title { font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    color: #9ba6b2; }
  /* Tint each panel's title to match its graph keyline. */
  #cpu-panel  .panel-title { color: #6ddc8a; }
  #mem-panel  .panel-title { color: #6db5dc; }
  #disk-panel .panel-title { color: #dcb86d; }
  .panel-value { font-size: 13px; color: #e7ebf0; }
  .panel-value .sub { color: #6c7886; margin-left: 8px; font-size: 11px; }
  svg { display: block; width: 100%; overflow: visible; }
  /* The single-series panels let their graph grow to fill the panel height. */
  .panel > svg { flex: 1 1 auto; min-height: 0; }
  .grid { stroke: #20262d; stroke-width: 1; }
  .axis { fill: #6c7886; font-size: 10px; font-family: inherit; }
  .axis.axis-mini { font-size: 9px; }
  /* Absolute clock time sits a shade lighter than the relative age. */
  .axis-abs { fill: #97a3b2; }
  .line-cpu  { stroke: #6ddc8a; }
  .line-mem  { stroke: #6db5dc; }
  .line-disk { stroke: #dcb86d; }
  .line { fill: none; stroke-width: 1.5; }
  /* Filled areas use a per-chart vertical value-gradient (see render()); their
     paint is set inline via a userSpaceOnUse <linearGradient>, not a class. */
  .gap       { fill: rgba(255,107,107,.18); }
  .gap-edge  { stroke: rgba(255,107,107,.55); stroke-width: 1; stroke-dasharray: 2 2; }
  .peak-label { font-size: 10px; font-family: inherit; paint-order: stroke fill;
    stroke: #11151a; stroke-width: 3px; stroke-linejoin: round; }
  .peak-label.peak-cpu  { fill: #b8eec5; }
  .peak-label.peak-mem  { fill: #b8d8ee; }
  .peak-label.peak-disk { fill: #eed9b8; }
  /* per-core CPU small-multiples — the grid fills the cores panel height, and
     the core panels stretch to fill the row width (auto-fit collapses empty
     trailing tracks; auto-fill would leave phantom columns and dead space). */
  .cores { flex: 1 1 auto; min-height: 0; display: grid; gap: 8px;
    grid-auto-rows: 1fr;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
  .core { background: #0e1216; border: 1px solid #1b2128; border-radius: 5px;
    padding: 4px 7px 2px; display: flex; flex-direction: column; min-height: 0;
    transition: background 600ms linear; }
  .core-head { flex: 0 0 auto; display: flex; justify-content: space-between;
    align-items: baseline; }
  .core-head .core-name { font-size: 10px; letter-spacing: .04em;
    text-transform: uppercase; color: #ffffff; }
  .core-head .core-val { font-size: 11px; color: #b8eec5; }
  svg.core-svg { flex: 1 1 auto; min-height: 0; height: auto; }
  footer { flex: 0 0 auto; text-align: center; padding: 7px 18px;
    font-size: 11px; color: #6c7886; border-top: 1px solid #20262d; }
  footer a { color: #9ba6b2; text-decoration: none; }
  footer a:hover { color: #d8dde3; text-decoration: underline; }
</style>
</head>
<body>
<header>
  <h1>Server Vitals</h1>
  <div class="meta">status: <span id="status" class="status st-idle">connecting…</span></div>
  <div class="controls">
    <span class="ctl">poll
      <select id="poll-sel">
        <option value="0.25">0.25s</option>
        <option value="0.5">0.5s</option>
        <option value="1">1s</option>
        <option value="3">3s</option>
        <option value="5">5s</option>
        <option value="10">10s</option>
      </select>
    </span>
    <span class="ctl">window
      <select id="window-sel">
        <option value="1">1m</option>
        <option value="3">3m</option>
        <option value="5">5m</option>
        <option value="10">10m</option>
        <option value="30">30m</option>
        <option value="60">60m</option>
      </select>
    </span>
  </div>
  <button id="pause-btn" class="ctl-btn" type="button"
    title="Pause polling" aria-label="Pause polling" aria-pressed="false">⏸</button>
</header>
<main>
  <section class="panel" id="cores-panel">
    <div class="panel-head">
      <div class="panel-title">CPU Cores · <span id="cores-count">—</span></div>
    </div>
    <div class="cores" id="cores"></div>
  </section>
  <section class="panel" id="cpu-panel">
    <div class="panel-head">
      <div class="panel-title">CPU</div>
      <div class="panel-value"><span id="cpu-now">—</span><span class="sub" id="cpu-sub"></span></div>
    </div>
    <svg id="cpu-svg"></svg>
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
<footer>Made with ❤️ by <a href="https://github.com/dragonworx/server-vitals"
  target="_blank" rel="noopener noreferrer">dragonworx</a></footer>
<script>
(() => {
  const FETCH_TIMEOUT_MS = 2000;
  const BASE_PAD_L = 44, BASE_PAD_R = 8, BASE_PAD_T = 8, BASE_PAD_B = 18;

  const POLL_OPTIONS = [0.25, 0.5, 1, 3, 5, 10];   // seconds
  const WINDOW_OPTIONS = [1, 3, 5, 10, 30, 60];    // minutes
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

  // The title's grey wave completes one cycle in half the poll interval — i.e.
  // twice as fast as the graph advances. Re-applied whenever the poll changes.
  function applyPulseTiming() {
    document.documentElement.style.setProperty('--pulse-duration', pollMs + 'ms');
  }
  applyPulseTiming();

  // Fine-grained load classifier. Each signal (CPU%, memory%, load-per-core) is
  // bucketed into a severity 0..3; the worst signal wins. load-per-core is the
  // 1-min load average divided by core count, the classic over-subscription gauge.
  // A wildly over-subscribed box (load ≥ 4×cores) — or one that stops answering —
  // is reported as "hung". States: idle · light · heavy · critical · hung.
  const LOAD_STATES = ['idle', 'light', 'heavy', 'critical'];
  function classifyLoad(cpu, mem, loadPerCore) {
    const cpuLvl  = cpu >= 90 ? 3 : cpu >= 65 ? 2 : cpu >= 25 ? 1 : 0;
    const memLvl  = mem >= 92 ? 3 : mem >= 78 ? 2 : mem >= 55 ? 1 : 0;
    const loadLvl = loadPerCore >= 2 ? 3 : loadPerCore >= 1 ? 2 : loadPerCore >= 0.5 ? 1 : 0;
    return Math.max(cpuLvl, memLvl, loadLvl);
  }

  function setStatus(state, detail) {
    const s = el('status');
    s.className = 'status st-' + state;
    s.textContent = detail ? state + ' · ' + detail : state;
  }

  // Filled-area gradient endpoints, shared by every chart: `from` paints the
  // baseline (low/cool values), `to` paints the top of the plot (high/hot).
  // Per-chart so individual graphs could diverge later; for now all green→red.
  const GRAD_FROM = '#27c93f';  // green  — low values
  const GRAD_MID  = '#ff9f40';  // orange — mid values (≈50%)
  const GRAD_TO   = '#ff4d4d';  // red    — high values

  const series = {
    cpu:  { data: [], color: 'cpu',  unit: '%', from: GRAD_FROM, mid: GRAD_MID, to: GRAD_TO },
    mem:  { data: [], color: 'mem',  unit: '%', from: GRAD_FROM, mid: GRAD_MID, to: GRAD_TO },
    disk: { data: [], color: 'disk', unit: '%', from: GRAD_FROM, mid: GRAD_MID, to: GRAD_TO },
  };
  // null in any data array represents a failed/timed-out poll at that slot.

  // Per-core CPU series + value labels, built lazily once we know the core count.
  const coreSeries = [];   // [{ data: [] }, …]  parallel to cpu_cores[]
  const coreValEls = [];   // matching .core-val spans
  const coreCellEls = [];  // matching .core cell divs (for heatmap tint)

  // Dark background tint for a core cell from its current load: green (light) →
  // orange (mid) → red (heavy). Lightness stays low so the graph line on top
  // remains legible; saturation/lightness ramp up a touch so heavy reads hotter.
  function coreBg(v) {
    if (v == null || isNaN(v)) return '#0e1216';
    const p = Math.max(0, Math.min(100, v)) / 100;
    const hue = p < 0.5 ? 120 - (120 - 30) * (p / 0.5)   // green → orange
                        : 30 - 30 * ((p - 0.5) / 0.5);   // orange → red
    const sat = 45 + 25 * p;   // 45% → 70%
    const lit = 9 + 5 * p;     // 9%  → 14%
    return 'hsl(' + hue.toFixed(0) + ',' + sat.toFixed(0) + '%,' + lit.toFixed(0) + '%)';
  }

  function ensureCores(count) {
    if (coreSeries.length === count) return;
    const host = el('cores');
    host.textContent = '';
    coreSeries.length = 0;
    coreValEls.length = 0;
    coreCellEls.length = 0;
    el('cores-count').textContent = count + (count === 1 ? ' core' : ' cores');
    for (let i = 0; i < count; i++) {
      const cell = document.createElement('div');
      cell.className = 'core';
      const head = document.createElement('div');
      head.className = 'core-head';
      const name = document.createElement('span');
      name.className = 'core-name';
      name.textContent = 'cpu ' + (i + 1);
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
      coreSeries.push({ data: [], color: 'cpu', unit: '%', from: GRAD_FROM, mid: GRAD_MID, to: GRAD_TO });
      coreValEls.push(val);
      coreCellEls.push(cell);
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

  // Round a target step up to a "nice" 1/2/5 × 10^k value, so the chosen step
  // is close to `target` (≈ range / desired-tick-count) rather than far below it.
  function niceStep(target) {
    if (target <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(target)));
    const n = target / exp;
    let step;
    if (n < 1.5) step = 1;
    else if (n < 3) step = 2;
    else if (n < 7) step = 5;
    else step = 10;
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

  // Absolute wall-clock time (12-hour, e.g. "2:32pm") for an epoch-ms instant.
  function fmtClock(ms) {
    const d = new Date(ms);
    const h24 = d.getHours();
    const h12 = h24 % 12 || 12;
    const ampm = h24 < 12 ? 'am' : 'pm';
    return h12 + ':' + String(d.getMinutes()).padStart(2, '0') + ampm;
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
    const compact = !!opts.compact;     // small-multiple mode: no peak labels / x-axis
    const showYAxis = !!opts.yAxis;     // draw a minimal y-axis even when compact
    const PAD_L = compact ? (showYAxis ? 26 : 6) : BASE_PAD_L;
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

    // y-axis grid. Full version on big panels; a sparse one on core graphs when
    // opts.yAxis is set (compact x-axis stays off — the window is shown above).
    if (!compact || showYAxis) {
      // Core graphs use even quarter ticks (0/25/50/75/100 on the fixed scale);
      // full panels aim for one nice-rounded gridline per ~34px.
      const step = compact
        ? range / 4
        : niceStep(range / Math.max(2, Math.min(6, Math.round(PLOT_H / 34))));
      const tickStart = Math.ceil(yMin / step) * step;
      for (let t = tickStart; t <= yMax + 1e-9; t += step) {
        const y = PAD_T + (1 - (t - yMin) / range) * PLOT_H;
        svg.appendChild(svgEl('line', {
          x1: PAD_L, x2: W - PAD_R, y1: y, y2: y, class: 'grid',
        }));
        const lbl = svgEl('text', {
          x: PAD_L - 4, y: y + 3, 'text-anchor': 'end',
          class: compact ? 'axis axis-mini' : 'axis',
        });
        lbl.textContent = compact ? String(Math.round(t)) : (fmtY(t) + ser.unit);
        svg.appendChild(lbl);
      }
    }

    // x-axis labels — the plot spans the full configured window. Five evenly
    // spaced markers, each showing relative age plus the absolute clock time
    // (the absolute part in a lighter shade), e.g. "-5m 2:32pm".
    const windowSec = windowMin * 60;
    const nowMs = Date.now();
    const TICKS = 5;
    const xLabels = compact ? [] : Array.from({ length: TICKS }, (_, i) => {
      const frac = i / (TICKS - 1);           // 0 (oldest) .. 1 (now)
      const ageSec = windowSec * (1 - frac);
      const anchor = i === 0 ? 'start' : i === TICKS - 1 ? 'end' : 'middle';
      return {
        x: PAD_L + PLOT_W * frac,
        rel: fmtAge(ageSec),
        abs: fmtClock(nowMs - ageSec * 1000),
        anchor,
      };
    });
    for (const l of xLabels) {
      const t = svgEl('text', {
        x: l.x, y: H - 4, class: 'axis', 'text-anchor': l.anchor,
      });
      t.appendChild(svgEl('tspan', {})).textContent = l.rel + ' ';
      t.appendChild(svgEl('tspan', { class: 'axis-abs' })).textContent = l.abs;
      svg.appendChild(t);
    }

    // line + fill, broken into segments around null runs.
    const n = data.length;
    const xStep = PLOT_W / (MAX_POINTS - 1);
    const xOffset = PAD_L + PLOT_W - (n - 1) * xStep;
    const baseY = PAD_T + PLOT_H;
    const xAt = i => xOffset + i * xStep;
    const yAt = v => PAD_T + (1 - (v - yMin) / range) * PLOT_H;

    // Vertical value-gradient for the filled area. The stops are anchored (in
    // user space) to the plot-y of value 0 (`from`) and value `gradMax` (`to`) —
    // NOT the visible plot edges and NOT the path's bounding box. That makes the
    // colour at any pixel a straight lerp(from → to, value / gradMax): it tracks
    // the real value, independent of how tightly the y-axis is auto-scaled. So a
    // CPU graph hovering at 10% reads ~10% of the way green→red (nearly pure
    // green) even when the axis is zoomed into 0–20%. Opacity is held constant
    // so only the hue carries the value. yAt(gradMax)/yAt(0) may fall outside the
    // visible band — SVG just clamps (spreadMethod=pad), which is what we want.
    // One def per chart, referenced by all fill segments; GPU-rasterised.
    const gradMax = opts.fixedMax != null ? opts.fixedMax : 100;
    const gradId = 'fill-grad-' + svgId;
    const grad = svgEl('linearGradient', {
      id: gradId, gradientUnits: 'userSpaceOnUse',
      x1: 0, y1: yAt(gradMax), x2: 0, y2: yAt(0),
    });
    // offset 0 = top (value gradMax) → 0.5 = mid (value gradMax/2) → 1 = value 0.
    grad.appendChild(svgEl('stop', { offset: '0',   'stop-color': ser.to,  'stop-opacity': '0.32' }));
    grad.appendChild(svgEl('stop', { offset: '0.5', 'stop-color': ser.mid, 'stop-opacity': '0.32' }));
    grad.appendChild(svgEl('stop', { offset: '1',   'stop-color': ser.from, 'stop-opacity': '0.32' }));
    const defs = svgEl('defs', {});
    defs.appendChild(grad);
    svg.appendChild(defs);
    const fillRef = 'url(#' + gradId + ')';

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
        svg.appendChild(svgEl('path', { d: area, fill: fillRef }));
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
    // Percentages share a fixed 0..100 scale so heights map directly to load
    // and never clip to the data's own min/max.
    render('cpu-svg',  series.cpu,  { fixedMax: 100 });
    render('mem-svg',  series.mem,  { fixedMax: 100 });
    render('disk-svg', series.disk, { fixedMax: 100 });
    for (let i = 0; i < coreSeries.length; i++) {
      render('core-svg-' + i, coreSeries[i], { compact: true, fixedMax: 100, yAxis: true });
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
    applyPulseTiming();
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
    let statusState = 'hung', statusDetail = null;
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
          coreCellEls[i].style.background = coreBg(cores[i]);
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

      const load1 = (j.load_average && typeof j.load_average['1min'] === 'number')
        ? j.load_average['1min'] : null;
      const coreCount = (typeof j.cpu_count === 'number' && j.cpu_count > 0) ? j.cpu_count : 1;
      const loadPerCore = load1 == null ? 0 : load1 / coreCount;
      // A responsive but wildly over-subscribed box reads as "hung" too.
      statusState = loadPerCore >= 4
        ? 'hung'
        : LOAD_STATES[classifyLoad(j.cpu_percent, j.memory_percent, loadPerCore)];

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

    if (ok) {
      setStatus(statusState, statusDetail);
    } else {
      // No response from the box → hung, with the failing-poll count + reason.
      setStatus('hung', consecutiveFailures + ' polls · ' + lastError);
    }
    renderAll();

    scheduleNext();
  }

  let pollTimer = null;
  let paused = false;
  function scheduleNext(delay) {
    if (paused) return;            // no polling while paused — graphs stay frozen
    pollTimer = setTimeout(tick, delay == null ? pollMs : delay);
  }

  // Pause/play: stops hitting the server and freezes the graphs (nothing is
  // pushed or re-rendered while paused); play resumes the loop immediately.
  const pauseBtn = el('pause-btn');
  function setPaused(p) {
    paused = p;
    clearTimeout(pollTimer);
    document.body.classList.toggle('paused', p);
    pauseBtn.classList.toggle('paused', p);
    pauseBtn.textContent = p ? '▶' : '⏸';
    pauseBtn.title = p ? 'Resume polling' : 'Pause polling';
    pauseBtn.setAttribute('aria-label', pauseBtn.title);
    pauseBtn.setAttribute('aria-pressed', String(p));
    if (!p) scheduleNext(0);       // resume: poll right away
  }
  pauseBtn.addEventListener('click', () => setPaused(!paused));

  scheduleNext(0);
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "Server-Vitals/1.0"

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
