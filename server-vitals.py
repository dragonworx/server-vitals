#!/usr/bin/env python3
"""Server Vitals — lightweight server health endpoint. Listens on 127.0.0.1:9999.

Exposes:
  GET /health       - server-wide vitals (cpu, memory, disk, load, uptime)
  GET /stats        - HTML thin client that polls /stats?format=json
"""
import json
import os
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

LISTEN = ("127.0.0.1", 9999)
HOSTNAME = "www.fresneldigital.com"  # shown in browser tab; set "" to use system FQDN
SAMPLE_INTERVAL = 0.25  # seconds between background CPU samples
REQUEST_TIMEOUT = 5     # seconds before an idle/slow client connection is dropped

# Server Vitals reads host metrics straight from the kernel. On Linux that's the
# /proc pseudo-files; macOS has no /proc, so the same numbers come from the Mach
# kernel and sysctl via ctypes (the `_mac_*` backend below). Everything above the
# collectors — the CPU sampler, HTTP layer, and dashboard — is platform-neutral,
# so each metric function just dispatches to the right backend for the host.
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# macOS metric backend (Mach / sysctl via ctypes)
# ---------------------------------------------------------------------------
# macOS ships none of /proc/{stat,meminfo,uptime}. The equivalent host data lives
# behind the Mach host port (per-CPU tick counts, VM page statistics) and sysctl
# (RAM size, page size, swap, boot time). We bind the handful of libSystem symbols
# we need with ctypes — still standard-library only: no pip, no virtualenv, no
# native build. None of this runs unless IS_MACOS, so importing on Linux is inert.
if IS_MACOS:
    import ctypes

    _libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)

    # Mach host port (a send right). mach_host_self() hands out a fresh reference
    # on each call, so grab one once and reuse it — re-calling would leak refs.
    _libc.mach_host_self.restype = ctypes.c_uint
    _MACH_HOST = _libc.mach_host_self()
    # The task-self port is exported as a data symbol; used to free kernel buffers.
    _MACH_TASK = ctypes.c_uint.in_dll(_libc, "mach_task_self_").value

    _libc.sysctlbyname.argtypes = [
        ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p, ctypes.c_size_t,
    ]
    _libc.sysctlbyname.restype = ctypes.c_int

    _libc.host_processor_info.argtypes = [
        ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint),
    ]
    _libc.host_processor_info.restype = ctypes.c_int

    _libc.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
    _libc.vm_deallocate.restype = ctypes.c_int

    _libc.host_statistics64.argtypes = [
        ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
    ]
    _libc.host_statistics64.restype = ctypes.c_int

    _PROCESSOR_CPU_LOAD_INFO = 2
    _CPU_STATE_MAX = 4
    _CPU_STATE_USER, _CPU_STATE_SYSTEM, _CPU_STATE_IDLE, _CPU_STATE_NICE = 0, 1, 2, 3
    _HOST_VM_INFO64 = 4

    class _vm_statistics64(ctypes.Structure):
        # Layout of vm_statistics64 (<mach/vm_statistics.h>). The natural_t fields
        # are page counts; multiply by page size for bytes. We read only a few of
        # them, but mirror the whole struct so its byte size — and thus the info
        # count handed to host_statistics64 — is exactly right.
        _fields_ = [
            ("free_count", ctypes.c_uint),
            ("active_count", ctypes.c_uint),
            ("inactive_count", ctypes.c_uint),
            ("wire_count", ctypes.c_uint),
            ("zero_fill_count", ctypes.c_uint64),
            ("reactivations", ctypes.c_uint64),
            ("pageins", ctypes.c_uint64),
            ("pageouts", ctypes.c_uint64),
            ("faults", ctypes.c_uint64),
            ("cow_faults", ctypes.c_uint64),
            ("lookups", ctypes.c_uint64),
            ("hits", ctypes.c_uint64),
            ("purges", ctypes.c_uint64),
            ("purgeable_count", ctypes.c_uint),
            ("speculative_count", ctypes.c_uint),
            ("decompressions", ctypes.c_uint64),
            ("compressions", ctypes.c_uint64),
            ("swapins", ctypes.c_uint64),
            ("swapouts", ctypes.c_uint64),
            ("compressor_page_count", ctypes.c_uint),
            ("throttled_count", ctypes.c_uint),
            ("external_page_count", ctypes.c_uint),
            ("internal_page_count", ctypes.c_uint),
            ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
        ]

    def _sysctl(name):
        """Raw bytes of a sysctl by name (the two-call size-then-fetch dance)."""
        size = ctypes.c_size_t(0)
        cname = name.encode()
        if _libc.sysctlbyname(cname, None, ctypes.byref(size), None, 0) != 0:
            raise OSError("sysctlbyname(%s) sizing failed" % name)
        buf = ctypes.create_string_buffer(size.value)
        if _libc.sysctlbyname(cname, buf, ctypes.byref(size), None, 0) != 0:
            raise OSError("sysctlbyname(%s) failed" % name)
        return buf.raw[:size.value]

    def _sysctl_int(name):
        """A scalar integer sysctl (4- or 8-byte), native byte order."""
        return int.from_bytes(_sysctl(name), sys.byteorder)

    def _mac_cpu_ticks():
        """(aggregate, cores) in the same shape as the /proc/stat reader: aggregate
        is (idle, total) summed over all CPUs; cores is [(index, idle, total), …].
        Ticks come from PROCESSOR_CPU_LOAD_INFO (user/system/idle/nice per CPU), so
        the existing delta math applies unchanged."""
        count = ctypes.c_uint(0)
        info = ctypes.POINTER(ctypes.c_uint)()
        info_len = ctypes.c_uint(0)
        kr = _libc.host_processor_info(
            _MACH_HOST, _PROCESSOR_CPU_LOAD_INFO,
            ctypes.byref(count), ctypes.byref(info), ctypes.byref(info_len))
        if kr != 0:
            raise OSError("host_processor_info failed (kr=%d)" % kr)
        try:
            agg_idle = agg_total = 0
            cores = []
            for c in range(count.value):
                base = c * _CPU_STATE_MAX
                idle = info[base + _CPU_STATE_IDLE]
                total = (info[base + _CPU_STATE_USER] + info[base + _CPU_STATE_SYSTEM]
                         + idle + info[base + _CPU_STATE_NICE])
                cores.append((c, idle, total))
                agg_idle += idle
                agg_total += total
            return (agg_idle, agg_total), cores
        finally:
            # Free the kernel-allocated array, or ~one page leaks every sample.
            _libc.vm_deallocate(
                _MACH_TASK, ctypes.cast(info, ctypes.c_void_p),
                info_len.value * ctypes.sizeof(ctypes.c_uint))

    def _mac_vm_stats():
        stats = _vm_statistics64()
        count = ctypes.c_uint(ctypes.sizeof(stats) // ctypes.sizeof(ctypes.c_int))
        kr = _libc.host_statistics64(
            _MACH_HOST, _HOST_VM_INFO64, ctypes.byref(stats), ctypes.byref(count))
        if kr != 0:
            raise OSError("host_statistics64 failed (kr=%d)" % kr)
        return stats

    def _mac_memory_stats():
        total = _sysctl_int("hw.memsize")
        page = _sysctl_int("hw.pagesize")
        vm = _mac_vm_stats()
        # "Used" mirrors Activity Monitor's Memory Used = App + Wired + Compressed.
        # Everything else (free, inactive, speculative, purgeable) is reclaimable
        # under pressure, so it counts as available.
        used = (vm.active_count + vm.wire_count + vm.compressor_page_count) * page
        available = total - used
        swap_total = swap_used = 0
        try:
            raw = _sysctl("vm.swapusage")  # struct xsw_usage: total, avail, used (u64)
            swap_total = int.from_bytes(raw[0:8], sys.byteorder)
            swap_used = int.from_bytes(raw[16:24], sys.byteorder)
        except OSError:
            pass
        mb = 1024 * 1024
        return {
            "total_mb": round(total / mb, 1),
            "used_mb": round(used / mb, 1),
            "available_mb": round(available / mb, 1),
            "percent": round(used * 100 / total, 1) if total else 0.0,
            "swap_total_mb": round(swap_total / mb, 1),
            "swap_used_mb": round(swap_used / mb, 1),
            "swap_percent": round(swap_used * 100 / swap_total, 1) if swap_total else 0.0,
        }

    def _mac_uptime():
        raw = _sysctl("kern.boottime")  # struct timeval; tv_sec is the first word
        boot = int.from_bytes(raw[0:8], sys.byteorder)
        return max(0.0, time.time() - boot)


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
    if IS_MACOS:
        return _mac_memory_stats()
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


class CpuSampler:
    """Samples per-CPU tick counts on a background thread at a fixed cadence and
    publishes the latest aggregate + per-core CPU percentages. The reading comes
    from /proc/stat on Linux and Mach's PROCESSOR_CPU_LOAD_INFO on macOS; both
    return the same (idle, total) tick shape, so the delta math below is shared.

    CPU% is inherently a *delta* between two readings. Computing that delta
    per-request would mean every endpoint mutates shared "previous sample" state,
    so two concurrent clients (e.g. two dashboard tabs, or the proxy's health
    check racing a poll) would each measure against the other's reading and get
    garbage — and `/health` would block its worker thread on a sampling sleep.

    Instead a single thread owns the sampling. Every request just reads the warm
    snapshot under a lock: non-blocking, consistent across all clients, and the
    measurement window is the steady SAMPLE_INTERVAL regardless of poll rate.
    """

    def __init__(self, interval=SAMPLE_INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        self._percent = 0.0
        self._cores = []
        self._prev_idle = None
        self._prev_total = None
        self._prev_cores = {}  # core index -> (idle, total) from the prior sample

    @staticmethod
    def _read_proc_stat():
        """Return (aggregate, cores) where aggregate is (idle, total) for the
        `cpu` line and cores is a list of (index, idle, total) for each `cpuN`."""
        aggregate = None
        cores = []
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    # /proc/stat lists every cpuN line before the first non-cpu line.
                    break
                parts = line.split()
                fields = [int(x) for x in parts[1:]]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
                total = sum(fields)
                if parts[0] == "cpu":
                    aggregate = (idle, total)
                    continue
                try:
                    idx = int(parts[0][3:])
                except ValueError:
                    continue
                cores.append((idx, idle, total))
        return aggregate, cores

    @staticmethod
    def _delta(idle, total, prev):
        if prev is None:
            return 0.0
        dt = total - prev[1]
        if dt <= 0:
            return 0.0
        return round((1 - (idle - prev[0]) / dt) * 100, 1)

    def _sample(self):
        aggregate, cores = _mac_cpu_ticks() if IS_MACOS else self._read_proc_stat()

        percent = 0.0
        if aggregate is not None:
            idle, total = aggregate
            percent = self._delta(idle, total, (self._prev_idle, self._prev_total)
                                  if self._prev_idle is not None else None)
            self._prev_idle, self._prev_total = idle, total

        core_out = []
        for idx, idle, total in cores:
            core_out.append(self._delta(idle, total, self._prev_cores.get(idx)))
            self._prev_cores[idx] = (idle, total)

        with self._lock:
            self._percent = percent
            self._cores = core_out

    def _run(self):
        # Prime the previous reading so the first published delta is real.
        try:
            self._sample()
        except Exception:
            traceback.print_exc()
        while True:
            time.sleep(self.interval)
            try:
                self._sample()
            except Exception:
                # A transient kernel read error must not kill the sampler thread;
                # log it and keep the last good snapshot until the next tick.
                traceback.print_exc()

    def start(self):
        threading.Thread(target=self._run, name="cpu-sampler", daemon=True).start()
        return self

    def snapshot(self):
        """Latest (aggregate_percent, [per_core_percent, …]); never blocks on I/O."""
        with self._lock:
            return self._percent, list(self._cores)


_sampler = CpuSampler()


def cpu_count():
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def loadavg():
    # os.getloadavg() is portable across Linux and macOS (BSD getloadavg(3) under
    # the hood), so the same call serves both. Returns 0s on the rare platform
    # that can't report it rather than failing the whole payload.
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return {"1min": 0.0, "5min": 0.0, "15min": 0.0}
    return {"1min": round(one, 2), "5min": round(five, 2), "15min": round(fifteen, 2)}


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
    if IS_MACOS:
        return _mac_uptime()
    with open("/proc/uptime") as f:
        return float(f.readline().split()[0])


def fmt_uptime(seconds):
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h}h {m}m {s}s"


def fmt_size(value, unit="MB"):
    """Human-readable size string: from `value` expressed in `unit`, climb to the
    nicest binary unit (KB→PB, 1024-steps to match how the collectors compute MB/GB)
    and group thousands with commas. e.g. fmt_size(7945.4, "MB") -> "7.76 GB",
    fmt_size(131072, "MB") -> "128 GB". Decimals taper as the number grows so the
    read-out stays compact (2 dp under 10, 1 dp under 100, none above)."""
    units = ["KB", "MB", "GB", "TB", "PB"]
    try:
        i = units.index(unit)
    except ValueError:
        i = 0
    v = float(value)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    while v < 1 and i > 0:
        v *= 1024
        i -= 1
    dp = 0 if v >= 100 or v == 0 else 1 if v >= 10 else 2
    return "{:,.{}f} {}".format(v, dp, units[i])


_cached_server_ip = None

def server_ip():
    global _cached_server_ip
    if _cached_server_ip is None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            _cached_server_ip = s.getsockname()[0]
            s.close()
        except Exception:
            _cached_server_ip = ""
    return _cached_server_ip


def stats_payload():
    cpu, cores = _sampler.snapshot()
    mem = memory_stats()
    disk = disk_usage("/")
    return {
        "timestamp": time.time(),
        "server_ip": server_ip(),
        "hostname": HOSTNAME if HOSTNAME else socket.getfqdn(),
        "cpu_percent": cpu,
        "cpu_count": cpu_count(),
        "cpu_cores": cores,
        "load_average": loadavg(),
        "memory_percent": mem["percent"],
        "memory_used_mb": mem["used_mb"],
        "memory_total_mb": mem["total_mb"],
        "disk_percent": disk["percent"],
        "disk_used_gb": disk["used_gb"],
        "disk_total_gb": disk["total_gb"],
    }


def health_payload():
    cpu, _ = _sampler.snapshot()
    mem = memory_stats()
    disk = disk_usage("/")
    uptime = system_uptime()
    status = "ok"
    if cpu >= 95 or mem["percent"] >= 95 or disk["percent"] >= 95:
        status = "degraded"
    # Human-readable size strings alongside the raw numbers (mirrors uptime_human),
    # so a person curling /health sees "7.76 GB" rather than "7945.4" MB. The raw
    # *_mb / *_gb fields stay for machine consumers and the dashboard.
    mem["total_human"] = fmt_size(mem["total_mb"], "MB")
    mem["used_human"] = fmt_size(mem["used_mb"], "MB")
    mem["available_human"] = fmt_size(mem["available_mb"], "MB")
    mem["swap_total_human"] = fmt_size(mem["swap_total_mb"], "MB")
    mem["swap_used_human"] = fmt_size(mem["swap_used_mb"], "MB")
    disk["total_human"] = fmt_size(disk["total_gb"], "GB")
    disk["used_human"] = fmt_size(disk["used_gb"], "GB")
    disk["free_human"] = fmt_size(disk["free_gb"], "GB")
    return {
        "status": status,
        "timestamp": int(time.time()),
        "uptime_seconds": int(uptime),
        "uptime_human": fmt_uptime(uptime),
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
  :root { color-scheme: dark; --strip-h: 14px; }
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
    /* A wave sweeps through the title. Its base/crest colours are the current
       machine-status hue (--title-base / --title-crest, set from JS on the shared
       green→orange→red ramp; flat red when hung). One cycle takes --pulse-duration
       (set from JS to the poll interval — one sweep per poll); the bright band is
       the wave crest moving across the text. */
    background: linear-gradient(100deg,
      var(--title-base, #5b636d) 0%, var(--title-base, #5b636d) 38%,
      var(--title-crest, #828a94) 50%,
      var(--title-base, #5b636d) 62%, var(--title-base, #5b636d) 100%);
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
    header h1 { animation: none;
      -webkit-text-fill-color: var(--title-base, #cfd6de);
      color: var(--title-base, #cfd6de); }
  }
  /* Status row under the banner is a heat-map strip: one vertical block per poll,
     coloured by the overall machine-pressure score on the shared green→orange→red
     ramp, so the bar reads as a scrolling timeline of how hot the box has been.
     Oldest sample at the left, newest at the leading edge (same direction as the
     graphs). Failed polls render as faint red gaps. Painted to a <canvas> that
     fills the strip (redrawn by renderHeat on every poll/resize). */
  #statusbar { flex: 0 0 auto; height: var(--strip-h);
    background: #0e1216; border-bottom: none; }
  #statusbar #heatmap { display: block; width: 100%; height: 100%; }
  #latencybar { flex: 0 0 auto; height: var(--strip-h);
    background: #0e1216; border-bottom: 1px solid #2b323a; }
  #latencybar #latencymap { display: block; width: 100%; height: 100%; }
  header .controls { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  header .controls .ctl { display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: #9ba6b2; }
  header .controls select { background: #11151a; color: #d8dde3;
    border: 1px solid #20262d; border-radius: 4px; padding: 3px 6px;
    font-family: inherit; font-size: 11px; cursor: pointer; line-height: 1; }
  header .controls select:hover { border-color: #2a323b; }
  /* Icon-only button: CSS draws pause bars / play triangle via ::before and ::after. */
  header button.ctl-btn { background: #0e1216;
    border: 1px solid #3a4553; border-radius: 4px; padding: 6px 10px;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
    transition: border-color 100ms, background 100ms; }
  header button.ctl-btn:hover { border-color: #59697c; background: #141c26; }
  /* Pause bars (two vertical rects the colour of the border). */
  header button.ctl-btn::before,
  header button.ctl-btn::after { content: ''; display: block;
    width: 2px; height: 11px; background: #3a4553;
    transition: background 100ms; }
  header button.ctl-btn::before { margin-right: 6px; }
  header button.ctl-btn:hover::before,
  header button.ctl-btn:hover::after { background: #59697c; }
  /* Paused state: replace bars with a single filled green triangle. */
  header button.ctl-btn.paused { border-color: #3a7a4a; background: #0b1810; }
  header button.ctl-btn.paused:hover { border-color: #4a9a5a; background: #0f2215; }
  header button.ctl-btn.paused::before {
    width: 0; height: 0; background: transparent; margin-right: 0;
    border-top: 6px solid transparent; border-bottom: 6px solid transparent;
    border-left: 10px solid #6ddc8a; transition: border-color 100ms; }
  header button.ctl-btn.paused:hover::before { border-left-color: #8dec9a; }
  header button.ctl-btn.paused::after { display: none; }
  body.paused header h1 { animation-play-state: paused; }
  main { flex: 1 1 auto; min-height: 0; padding: 12px 18px;
    display: grid; grid-template-rows: repeat(4, 1fr); gap: 12px; }
  .panel { background: #11151a; border: 1px solid #20262d; border-radius: 6px;
    padding: 8px 12px 6px; display: flex; flex-direction: column; min-height: 0;
    position: relative; }
  /* Soft inner shadow down the left and right edges, so the graph reads as if it
     slips under the panel edges. Overlays the SVG; never intercepts clicks. The
     matching corner radii keep the panel's rounded corners clean. */
  .panel::before, .panel::after { content: ""; position: absolute; top: 0;
    bottom: 0; width: 9px; pointer-events: none; z-index: 2; }
  .panel::before { left: 0; border-radius: 6px 0 0 6px;
    background: linear-gradient(to right, rgba(0,0,0,.5), rgba(0,0,0,0)); }
  .panel::after { right: 0; border-radius: 0 6px 6px 0;
    background: linear-gradient(to left, rgba(0,0,0,.5), rgba(0,0,0,0)); }
  .panel-head { flex: 0 0 auto; display: flex; justify-content: space-between;
    align-items: baseline; margin-bottom: 4px; }
  .panel-title { font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    color: #9ba6b2; }
  /* Tint each panel's title to match its graph keyline. */
  #cpu-panel  .panel-title { color: #6ddc8a; }
  #mem-panel  .panel-title { color: #6db5dc; }
  #disk-panel .panel-title { color: #dcb86d; }
  .panel-value { display: flex; align-items: center; gap: 9px;
    font-size: 13px; color: #e7ebf0; }
  .panel-value .sub { color: #6c7886; font-size: 11px; }
  /* The percentage read-out sits far right as a compact pill. Its fill is the
     panel's graph keyline colour darkened ~40% (per-panel below); text stays white. */
  .panel-value .pill { color: #fff; font-weight: 600; font-size: 12px;
    padding: 1px 9px; border-radius: 999px; line-height: 1.55; white-space: nowrap;
    min-width: 3.5em; text-align: center; box-sizing: border-box; }
  #cpu-panel  .pill { background: #418352; }   /* #6ddc8a × .85 × .70 */
  #mem-panel  .pill { background: #416c83; }   /* #6db5dc × .85 × .70 */
  #disk-panel .pill { background: #836d41; }   /* #dcb86d × .85 × .70 */
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
    align-items: baseline; margin-bottom: 3px; }
  .core-head .core-name { font-size: 10px; letter-spacing: .04em;
    text-transform: uppercase; color: #ffffff; }
  .core-head .core-val { font-size: 11px; color: #b8eec5; }
  svg.core-svg { flex: 1 1 auto; min-height: 0; height: auto; }
  footer { flex: 0 0 auto; text-align: center; padding: 7px 18px;
    font-size: 11px; color: #6c7886; border-top: 1px solid #20262d; }
  footer a { color: #9ba6b2; text-decoration: none; }
  footer a:hover { color: #d8dde3; text-decoration: underline; }
  /* Portrait phones (iPhone ≈ 390px): let the header controls wrap below the
     title instead of overflowing, and tighten paddings/grids to fit the column. */
  @media (max-width: 480px) {
    header { padding: 10px 12px; gap: 8px 12px; flex-wrap: wrap; }
    header h1 { font-size: 16px; }
    header .controls { gap: 8px; }
    header .controls .ctl { font-size: 10px; gap: 4px; }
    main { padding: 10px 12px; gap: 10px; }
    .panel { padding: 7px 10px 5px; }
    .cores { grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 6px; }
    footer { padding: 6px 12px; }
  }
</style>
</head>
<body>
<header>
  <h1 id="title-ip">—</h1>
  <div class="controls">
    <span class="ctl">poll
      <select id="poll-sel"></select>
    </span>
    <span class="ctl">window
      <select id="window-sel"></select>
    </span>
    <button id="pause-btn" class="ctl-btn" type="button"
      title="Pause polling" aria-label="Pause polling" aria-pressed="false"></button>
  </div>
</header>
<div id="statusbar"><canvas id="heatmap"></canvas></div>
<div id="latencybar"><canvas id="latencymap"></canvas></div>
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
      <div class="panel-value"><span class="sub" id="cpu-sub"></span><span class="pill" id="cpu-now">—</span></div>
    </div>
    <svg id="cpu-svg"></svg>
  </section>
  <section class="panel" id="mem-panel">
    <div class="panel-head">
      <div class="panel-title">Memory</div>
      <div class="panel-value"><span class="sub" id="mem-sub"></span><span class="pill" id="mem-now">—</span></div>
    </div>
    <svg id="mem-svg"></svg>
  </section>
  <section class="panel" id="disk-panel">
    <div class="panel-head">
      <div class="panel-title">Disk</div>
      <div class="panel-value"><span class="sub" id="disk-sub"></span><span class="pill" id="disk-now">—</span></div>
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

  // Unified option set in seconds — union of former poll-seconds and window-minutes converted to seconds,
  // sorted shortest→longest. Both dropdowns use this same array with the same labels.
  const OPTIONS = [0.25, 0.5, 1, 1.5, 3, 5, 10, 15, 30, 60, 90, 180, 300, 600, 1800, 3600];
  const POLL_KEY = 'stats:poll:sec';
  const WINDOW_KEY = 'stats:window:sec';
  function formatDur(sec) {
    if (sec < 60)   return sec + 's';
    if (sec < 3600) return (sec / 60) + 'm';
    return (sec / 3600) + 'h';
  }

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

  let pollSec = loadChoice(POLL_KEY, OPTIONS, 1);
  let windowSec = loadChoice(WINDOW_KEY, OPTIONS, 300);
  let pollMs = pollSec * 1000;
  // Points kept = window seconds / poll seconds.
  let MAX_POINTS = Math.max(2, Math.round(windowSec / pollSec));

  // The title's colour wave completes one cycle per poll interval, so it advances
  // in step with the graphs. Re-applied whenever the poll changes.
  function applyPulseTiming() {
    document.documentElement.style.setProperty('--pulse-duration', pollMs + 'ms');
  }
  applyPulseTiming();

  // Overall machine pressure as a single continuous 0..1 score — the spine of the
  // whole status row. Each signal is normalised so 1.0 means "maxed/critical" and
  // the worst signal wins:
  //   • CPU%        — straight cpu/100; a pegged CPU is genuinely maxed.
  //   • memory%     — measured from a 50% comfort floor up to 95% (a healthy box
  //                   routinely sits at 50-70% RAM on caches without being stressed,
  //                   so that range stays cool; only real pressure climbs).
  //   • load/core   — 1-min load ÷ cores, the classic over-subscription gauge,
  //                   normalised so 2× core count reads as fully maxed.
  // This ONE number drives both the "Server Vitals" title colour and the newest
  // block on the heat-map strip, so the two can never disagree.
  function loadScore(cpu, mem, loadPerCore) {
    const cpuS  = clamp01(cpu / 100);
    const memS  = clamp01((mem - 50) / 45);
    const loadS = clamp01(loadPerCore / 2);
    return clamp01(Math.max(cpuS, memS, loadS));
  }

  // Recolour the "Server Vitals" title from the 0..1 pressure score: base hue +
  // a brighter crest for the moving wave, both on the shared green→orange→red
  // ramp (green when idle, red at full load). `hung` — an unresponsive or wildly
  // over-subscribed box (load ≥ 4×cores) — forces the title to a flat red.
  function applyStatus(heat, hung) {
    const root = document.documentElement.style;
    root.setProperty('--title-base',  hung ? '#ff4d4d' : heatHsl(heat, 54));
    root.setProperty('--title-crest', hung ? '#ff8f8f' : heatHsl(heat, 80));
  }

  // Host label — when accessed via localhost/127.0.0.1 the browser is on the same
  // machine, so default to showing "localhost" (or "127.0.0.1") rather than the
  // outbound IP the server probes. Clicking the label toggles between the two.
  const IS_LOCAL = ['localhost', '127.0.0.1'].includes(window.location.hostname);
  let _serverIp = '';
  let _serverHostname = '';
  let _showLocal = true;  // only relevant when IS_LOCAL
  function applyTitleIp() {
    if (!_serverIp) return;
    const h = document.getElementById('title-ip');
    const display = IS_LOCAL && _showLocal ? window.location.hostname : _serverIp;
    if (h.textContent !== display) h.textContent = display;
    if (IS_LOCAL) {
      h.title = _showLocal ? 'click to show server IP (' + _serverIp + ')'
                           : 'click to show local hostname (' + window.location.hostname + ')';
    }
    const docTitle = _serverHostname ? _serverIp + ' (' + _serverHostname + ')' : _serverIp;
    if (document.title !== docTitle) document.title = docTitle;
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

  const clamp01 = x => x < 0 ? 0 : x > 1 ? 1 : x;
  // Colour for a 0..1 pressure score on the shared green→orange→red ramp. `light`
  // is the HSL lightness, so one hue ramp serves both the vivid heat-map blocks
  // (~45) and the brighter title crest (~80).
  function heatHsl(heat, light) {
    const p = clamp01(heat);
    const hue = p < 0.5 ? 140 - (140 - 38) * (p / 0.5)   // green → orange
                        : 38 - 38 * ((p - 0.5) / 0.5);    // orange → red
    const sat = 68 + 20 * p;   // 68% → 88%
    return 'hsl(' + hue.toFixed(0) + ',' + sat.toFixed(0) + '%,' + light + '%)';
  }

  function latencyColor(ms) {
    if (latencyMin === Infinity || latencyMax <= latencyMin) return 'hsl(180,70%,45%)';
    const t = Math.min(1, Math.max(0, (ms - latencyMin) / (latencyMax - latencyMin)));
    const hue = 180 + 40 * t;  // cyan (180) → blue (220)
    return 'hsl(' + hue.toFixed(0) + ',70%,45%)';
  }

  // Heat-map strip: one vertical block per poll, oldest at left, painted to the
  // #heatmap canvas. heatData holds the 0..1 score per slot (null = failed poll,
  // drawn as a faint red gap). Columns are sized to MAX_POINTS so the strip and
  // the graphs share the same time axis; redrawn on every poll and on resize.
  const heatData = [];
  const latencyData = [];
  let latencyMin = Infinity;
  let latencyMax = -Infinity;
  let latencyLast = null;
  let cpuMin = Infinity;
  let cpuMax = -Infinity;
  let cpuLast = null;
  // Wall-clock timestamp (ms) for each tick, kept parallel to series/heatData.
  // Timestamp-based x-positioning keeps the horizontal scale fixed to real time
  // so changing the poll interval never stretches or compresses the graphs.
  const timestamps = [];

  // Eased display window — animates smoothly between windowSec values on zoom.
  let displayWindowSec = windowSec;
  let _easeRAF = null, _easeFrom = windowSec, _easeTarget = windowSec, _easeT0 = 0;
  const EASE_MS = 350;
  function startWindowEase(to) {
    _easeFrom = displayWindowSec;
    _easeTarget = to;
    _easeT0 = performance.now();
    if (_easeRAF) cancelAnimationFrame(_easeRAF);
    function step(now) {
      const t = Math.min(1, (now - _easeT0) / EASE_MS);
      const e = 1 - Math.pow(1 - t, 3);  // cubic ease-out
      displayWindowSec = _easeFrom + (_easeTarget - _easeFrom) * e;
      renderAll();
      if (t < 1) _easeRAF = requestAnimationFrame(step);
      else { displayWindowSec = _easeTarget; _easeRAF = null; renderAll(); }
    }
    _easeRAF = requestAnimationFrame(step);
  }

  function pushHeat(v) {
    heatData.push(v);
    if (heatData.length > MAX_POINTS) heatData.shift();
  }
  const STRIP_WHITE = 'rgba(255,255,255,1)';
  const STRIP_PAD   = 6;  // logical px from each edge
  // Left: label. Center: min–max colored segments (measured and truly centred).
  // Right: current value right-aligned. No reserved slots or padding.
  function drawStripLabel(ctx, cv, dpr, y, label, centerSegs, curText) {
    const pad = Math.round(STRIP_PAD * dpr);
    const boldFont = ctx.font;
    const normalFont = boldFont.replace('bold ', '');
    ctx.textAlign = 'left';
    ctx.font = normalFont;
    ctx.fillStyle = STRIP_WHITE;
    ctx.fillText(label, pad, y);
    ctx.font = boldFont;
    if (centerSegs.length) {
      const totalW = centerSegs.reduce((w, s) => w + ctx.measureText(s.text).width, 0);
      let x = (cv.width - totalW) / 2;
      for (const s of centerSegs) {
        ctx.fillStyle = s.color;
        ctx.fillText(s.text, x, y);
        x += ctx.measureText(s.text).width;
      }
    }
    if (curText) {
      ctx.textAlign = 'right';
      ctx.fillStyle = STRIP_WHITE;
      ctx.fillText(curText, cv.width - pad, y);
    }
    ctx.textAlign = 'left';
  }

  function renderHeat() {
    const cv = el('heatmap');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#0e1216';
    ctx.fillRect(0, 0, cv.width, cv.height);
    // Uniform slot-based positioning: divide the canvas into MAX_POINTS equal slots
    // so adjacent bars share the exact same boundary pixel regardless of timestamp jitter.
    // Newest data at the right edge; empty slots on the left when buffer isn't full.
    const N = MAX_POINTS;
    const W = cv.width;
    for (let i = 0; i < heatData.length; i++) {
      const slot = N - heatData.length + i;
      if (slot < 0) continue;
      const x0 = Math.round(slot * W / N);
      const x1 = Math.round((slot + 1) * W / N);
      if (x1 <= x0) continue;
      const v = heatData[i];
      ctx.fillStyle = (v == null || isNaN(v)) ? 'rgba(255,77,77,.20)' : heatHsl(v, 45);
      ctx.fillRect(x0, 0, x1 - x0, cv.height);
    }
    ctx.font = 'bold ' + Math.round(13 * dpr) + 'px ui-monospace,monospace';
    ctx.fillStyle = 'rgba(255,255,255,1)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'black';
    ctx.shadowOffsetX = 1 * dpr;
    ctx.shadowOffsetY = 1 * dpr;
    ctx.shadowBlur = 0;
    drawStripLabel(ctx, cv, dpr, cv.height / 2, 'PERFORMANCE',
      cpuMin === Infinity ? [] : [
        { text: cpuMin.toFixed(0) + '% – ' + cpuMax.toFixed(0) + '%', color: STRIP_WHITE },
      ],
      cpuLast != null ? cpuLast.toFixed(0) + '%' : '');
    ctx.shadowColor = 'transparent';
  }

  function renderLatency() {
    const cv = el('latencymap');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#0e1216';
    ctx.fillRect(0, 0, cv.width, cv.height);
    // Uniform slot-based positioning: same approach as renderHeat.
    const NL = MAX_POINTS;
    const WL = cv.width;
    for (let i = 0; i < latencyData.length; i++) {
      const slot = NL - latencyData.length + i;
      if (slot < 0) continue;
      const x0 = Math.round(slot * WL / NL);
      const x1 = Math.round((slot + 1) * WL / NL);
      if (x1 <= x0) continue;
      const v = latencyData[i];
      ctx.fillStyle = (v == null || isNaN(v)) ? 'rgba(255,77,77,.20)' : latencyColor(v);
      ctx.fillRect(x0, 0, x1 - x0, cv.height);
    }
    ctx.font = 'bold ' + Math.round(13 * dpr) + 'px ui-monospace,monospace';
    ctx.fillStyle = 'rgba(255,255,255,1)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'black';
    ctx.shadowOffsetX = 1 * dpr;
    ctx.shadowOffsetY = 1 * dpr;
    ctx.shadowBlur = 0;
    const LAT_FROM = 'hsl(180,70%,72%)';
    const LAT_TO   = 'hsl(220,70%,72%)';
    drawStripLabel(ctx, cv, dpr, cv.height / 2, 'LATENCY',
      latencyMin === Infinity ? [] : [
        { text: latencyMin.toFixed(0) + 'ms – ' + latencyMax.toFixed(0) + 'ms', color: STRIP_WHITE },
      ],
      latencyLast != null ? latencyLast.toFixed(0) + 'ms' : '');
    ctx.shadowColor = 'transparent';
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

  // Human-readable byte size: from `value` expressed in `unit`, climb to the
  // nicest binary unit (KB→PB, 1024-steps — matching the server's MB/GB) and
  // group thousands with commas via toLocaleString. Mirrors fmt_size() in Python;
  // used for the memory / disk "used / total" read-outs. Decimals taper with size.
  const SIZE_UNITS = ['KB', 'MB', 'GB', 'TB', 'PB'];
  function fmtSize(value, unit) {
    let i = Math.max(0, SIZE_UNITS.indexOf(unit));
    let v = value;
    while (v >= 1024 && i < SIZE_UNITS.length - 1) { v /= 1024; i++; }
    while (v < 1 && i > 0) { v *= 1024; i--; }
    const dp = v >= 100 || v === 0 ? 0 : v >= 10 ? 1 : 2;
    return v.toLocaleString(undefined,
      { minimumFractionDigits: dp, maximumFractionDigits: dp }) + ' ' + SIZE_UNITS[i];
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
    const windowSec = displayWindowSec;  // use animated value for smooth zoom
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
    // Timestamp-based x: each point placed at its real age so the horizontal scale
    // stays fixed to actual time regardless of poll interval.
    const pxPerSec = PLOT_W / windowSec;  // pixels per second
    const tsBase = timestamps.length - n; // timestamps[tsBase+i] is the clock for data[i]
    const xAt = i => {
      const tsIdx = tsBase + i;
      const ageSec = (tsIdx >= 0 && tsIdx < timestamps.length)
        ? (nowMs - timestamps[tsIdx]) / 1000
        : (n - 1 - i) * pollSec;  // fallback before timestamps populate
      return PAD_L + PLOT_W - ageSec * pxPerSec;
    };
    const xStep = pollSec * pxPerSec;  // expected spacing between points, for gap bands
    const baseY = PAD_T + PLOT_H;
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
    renderHeat();
    renderLatency();
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
    if (heatData.length > MAX_POINTS) heatData.splice(0, heatData.length - MAX_POINTS);
    if (latencyData.length > MAX_POINTS) latencyData.splice(0, latencyData.length - MAX_POINTS);
    if (timestamps.length > MAX_POINTS) timestamps.splice(0, timestamps.length - MAX_POINTS);
  }

  if (IS_LOCAL) {
    const h = el('title-ip');
    h.style.cursor = 'pointer';
    h.addEventListener('click', () => { _showLocal = !_showLocal; applyTitleIp(); });
  }

  const pollSel = el('poll-sel');
  const windowSel = el('window-sel');
  OPTIONS.forEach(v => {
    const lbl = formatDur(v);
    pollSel.appendChild(Object.assign(document.createElement('option'), { value: v, textContent: lbl }));
    windowSel.appendChild(Object.assign(document.createElement('option'), { value: v, textContent: lbl }));
  });
  pollSel.value = String(pollSec);
  windowSel.value = String(windowSec);

  pollSel.addEventListener('change', () => {
    pollSec = parseFloat(pollSel.value);
    pollMs = pollSec * 1000;
    MAX_POINTS = Math.max(2, Math.round(windowSec / pollSec));
    applyPulseTiming();
    persist(POLL_KEY, pollSec);
    trimAll();
    // Restart the poll loop immediately at the new cadence.
    clearTimeout(pollTimer);
    scheduleNext(0);
    renderAll();
  });

  windowSel.addEventListener('change', () => {
    windowSec = parseFloat(windowSel.value);
    MAX_POINTS = Math.max(2, Math.round(windowSec / pollSec));
    persist(WINDOW_KEY, windowSec);
    trimAll();
    startWindowEase(windowSec);  // animate the zoom transition
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
    let statusHeat = 1, statusHung = true;
    let latencyMs = null;
    const t0 = performance.now();

    const [statsResult] = await Promise.allSettled([
      fetchWithTimeout('/stats?format=json', FETCH_TIMEOUT_MS),
      fetchWithTimeout('/ping', FETCH_TIMEOUT_MS).then(() => {
        latencyMs = performance.now() - t0;
      }).catch(() => {}),
    ]);

    try {
      if (statsResult.status !== 'fulfilled') throw statsResult.reason || new Error('fetch failed');
      const r = statsResult.value;
      if (!r.ok) throw new Error('http ' + r.status);
      const j = await r.json();
      push('cpu',  j.cpu_percent);
      push('mem',  j.memory_percent);
      push('disk', j.disk_percent);
      cpuLast = j.cpu_percent;
      if (j.cpu_percent < cpuMin) cpuMin = j.cpu_percent;
      if (j.cpu_percent > cpuMax) cpuMax = j.cpu_percent;

      const cores = Array.isArray(j.cpu_cores) ? j.cpu_cores : [];
      if (cores.length) {
        ensureCores(cores.length);
        for (let i = 0; i < cores.length; i++) {
          pushCore(i, cores[i]);
          coreValEls[i].textContent = cores[i].toFixed(0) + '%';
          coreCellEls[i].style.background = coreBg(cores[i]);
        }
      }

      if (j.server_ip) {
        _serverIp = j.server_ip;
        _serverHostname = j.hostname || '';
        applyTitleIp();
      }
      el('cpu-now').textContent  = Math.round(j.cpu_percent) + '%';
      el('cpu-sub').textContent  = '';
      el('mem-now').textContent  = Math.round(j.memory_percent) + '%';
      el('mem-sub').textContent  = fmtSize(j.memory_used_mb, 'MB') + ' / ' +
                                   fmtSize(j.memory_total_mb, 'MB');
      el('disk-now').textContent = Math.round(j.disk_percent) + '%';
      el('disk-sub').textContent = fmtSize(j.disk_used_gb, 'GB') + ' / ' +
                                   fmtSize(j.disk_total_gb, 'GB');

      const load1 = (j.load_average && typeof j.load_average['1min'] === 'number')
        ? j.load_average['1min'] : null;
      const coreCount = (typeof j.cpu_count === 'number' && j.cpu_count > 0) ? j.cpu_count : 1;
      const loadPerCore = load1 == null ? 0 : load1 / coreCount;
      statusHeat = loadScore(j.cpu_percent, j.memory_percent, loadPerCore);
      statusHung = loadPerCore >= 4;

      consecutiveFailures = 0;
      lastError = null;
      ok = true;
    } catch (e) {
      pushFailureSlot();
      consecutiveFailures++;
      lastError = (e && e.name === 'AbortError') ? 'timeout'
                : (e && e.message) ? e.message
                : String(e);
      el('cpu-now').textContent  = '—';
      el('mem-now').textContent  = '—';
      el('disk-now').textContent = '—';
    }

    if (latencyMs != null) {
      latencyLast = latencyMs;
      if (latencyMs < latencyMin) latencyMin = latencyMs;
      if (latencyMs > latencyMax) latencyMax = latencyMs;
    }
    latencyData.push(latencyMs);
    if (latencyData.length > MAX_POINTS) latencyData.shift();

    if (ok) {
      pushHeat(statusHeat);
      applyStatus(statusHeat, statusHung);
    } else {
      pushHeat(null);
      applyStatus(1, true);
    }
    timestamps.push(Date.now());
    if (timestamps.length > MAX_POINTS) timestamps.shift();
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
    server_version = "Server-Vitals/1.1"
    # HTTP/1.1 keeps the connection alive so the dashboard's frequent polls reuse
    # one TCP connection instead of reconnecting every tick. Safe because every
    # response carries a Content-Length.
    protocol_version = "HTTP/1.1"
    # Drop a client that opens a connection but stalls (slowloris), rather than
    # pinning a worker thread on it indefinitely.
    timeout = REQUEST_TIMEOUT

    def log_message(self, fmt, *args):
        return

    def _send(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, code=200, indent=None):
        body = json.dumps(payload, indent=indent, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    def _send_html(self, html, code=200):
        self._send(html.encode("utf-8"), "text/html; charset=utf-8", code)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = path.rstrip("/")
        params = parse_qs(query)
        try:
            if path in ("/health", ""):
                self._send_json(health_payload(), indent=2)  # pretty for human curl
            elif path == "/stats":
                if params.get("format") == ["json"]:
                    self._send_json(stats_payload())
                else:
                    self._send_html(STATS_HTML)
            elif path == "/ping":
                self._send(b'', "text/plain; charset=utf-8", 200)
            else:
                self._send_json({"error": "not found"}, code=404)
        except (BrokenPipeError, ConnectionResetError):
            # Client went away mid-response (e.g. a closed dashboard tab). Nothing
            # to send; just let this connection drop.
            self.close_connection = True
        except Exception:
            # Log the detail to the journal for the operator; never leak internal
            # exception text to the client.
            traceback.print_exc()
            try:
                self._send_json({"error": "internal server error"}, code=500)
            except Exception:
                self.close_connection = True


def _startup_banner():
    """A short framed banner printed once at startup so launching the server —
    `make run`, `python3 server-vitals.py`, launchd, or systemd (where it lands in
    the journal) — gives immediate feedback: which backend is in use and the URLs
    to open."""
    host, port = LISTEN
    if IS_MACOS:
        backend = "macOS · Mach + sysctl"
    elif IS_LINUX:
        backend = "Linux · /proc"
    else:
        backend = sys.platform
    base = "http://%s:%d" % (host, port)
    rows = [
        "Server Vitals — host monitoring agent",
        "backend: " + backend,
        "",
        "dashboard  " + base + "/stats",
        "health     " + base + "/health",
        "json       " + base + "/stats?format=json",
        "",
        "Ctrl+C to stop",
    ]
    width = max(len(r) for r in rows)
    top = "┌" + "─" * (width + 2) + "┐"
    bot = "└" + "─" * (width + 2) + "┘"
    body = "\n".join("│ " + r.ljust(width) + " │" for r in rows)
    print("\n" + top + "\n" + body + "\n" + bot + "\n", flush=True)


def main():
    _startup_banner()
    _sampler.start()
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
