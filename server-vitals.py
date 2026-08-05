#!/usr/bin/env python3
"""Server Vitals — lightweight server health endpoint. Listens on 127.0.0.1:9999.

Exposes:
  GET /health       - server-wide vitals (cpu, memory, disk, load, uptime)
  GET /stats        - HTML thin client that polls /stats?format=json
"""
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import urllib.request
from calendar import timegm
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

VERSION = "2.2.0"
LISTEN = ("127.0.0.1", 9999)
HOSTNAME = "www.fresneldigital.com"  # shown in browser tab; set "" to use system FQDN
SAMPLE_INTERVAL = 0.25  # seconds between background CPU samples
REQUEST_TIMEOUT = 5     # seconds before an idle/slow client connection is dropped

# Access logs. The dashboard's right-hand panel tails a web server's access log
# through a *provider* — a small adapter that knows one server's log format and
# how to enumerate its sites. Caddy, nginx and Apache are built in; see
# LogProvider for what a new one has to implement.
#
# List every log you want on the dashboard. One entry is the ordinary case and
# the panel hides its picker; several put a provider dropdown above the site
# dropdown. `type` is required, everything else falls back to that provider's
# defaults. After changing this, `make deploy`.
LOG_PROVIDERS = [
    {"type": "caddy", "path": "/var/log/caddy/access.log"},
    # {"type": "nginx",  "path": "/var/log/nginx/access.log"},
    # {"type": "apache", "path": "/var/log/apache2/access.log", "label": "httpd"},
]
# Optional JSON override of the list above, so a host can add a provider without
# editing a file that the next `make deploy` overwrites. Absent by design.
LOG_PROVIDERS_FILE = "/etc/server-vitals/providers.json"

CADDY_ADMIN = "http://127.0.0.1:2019"  # loopback admin API, read-only, optional
LOG_TAIL_INTERVAL = 0.25    # seconds between checks for new log lines
LOG_RING_SIZE = 2000        # entries kept in memory per provider (~2KB each)
LOG_BACKFILL_BYTES = 262144 # tail read on startup, so a new tab opens with history
LOG_MAX_BATCH = 300         # hard cap on entries returned by one /logs request
LOG_SERVERS_REFRESH = 60    # seconds between refreshes of the server-name list
LOG_MAX_LINE = 65536        # a "line" longer than this is corruption, not a request
LOG_UA_MAX = 72             # user-agent truncation in the streamed row
LOG_CONF_SUFFIXES = (".conf",)  # config files scanned for declared server names
LOG_CONF_MAX_FILES = 200        # ceiling on that scan, so a stray tree can't stall it
LOG_CONF_MAX_BYTES = 262144     # per config file

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
    """This host's private / local-network address — the source IP the kernel
    would use to reach the internet. Cached; never leaves the local network."""
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


# Public (internet-facing) IP. Unlike server_ip() this can't be read locally —
# behind NAT the source address above is a private 10./192.168. address — so we
# ask an external echo service. That's the one outbound HTTP call in the program,
# so it runs on a background thread (never in a request path) and the result is
# published under a lock. Empty string until the first successful fetch.
_public_ip = ""
_public_ip_lock = threading.Lock()

def public_ip():
    with _public_ip_lock:
        return _public_ip

def _fetch_public_ip():
    """Try each echo service in turn; publish the first plain-IP answer. Returns
    True once the public IP is known (this call or a previous one)."""
    global _public_ip
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "server-vitals"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                ip = resp.read(64).decode("ascii", "replace").strip()
            # Guard against error pages / captive portals: accept only a bare IP.
            if ip and all(c in "0123456789abcdefABCDEF.:" for c in ip):
                with _public_ip_lock:
                    _public_ip = ip
                return True
        except Exception:
            continue
    with _public_ip_lock:
        return bool(_public_ip)

def _public_ip_worker():
    # Retry quickly until the first success (egress may lag boot), then refresh
    # slowly — a host's public IP rarely changes, but this catches it if it does.
    while True:
        ok = _fetch_public_ip()
        time.sleep(900 if ok else 30)


# ---------------------------------------------------------------------------
# Access-log providers
# ---------------------------------------------------------------------------
def host_key(host):
    """A hostname with any port stripped.

    Access logs record the host exactly as the client sent it, so one site shows
    up as both `example.com` and `example.com:3000` depending on which listener
    took the request. The names a provider discovers — Caddy's logger_names, an
    nginx `server_name` — are always bare hostnames. Normalising here is what
    makes the two agree; rows still display the host verbatim."""
    if host.startswith("["):            # IPv6 literal, e.g. [::1]:443
        end = host.find("]")
        return host[:end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def _first_header(value):
    """Caddy writes every header value as an array of strings, even single-valued
    ones. Tolerate a bare string too, in case that ever changes."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


class LogProvider:
    """One web server's access log, adapted to the shape the panel streams.

    A provider is the only server-specific code in the whole log pipeline.
    Following the file across rotations, the ring buffer, the monotonic cursor,
    the HTTP endpoints and the dashboard panel are all shared, and reach the
    server-specific part through three methods:

        parse(text)             one raw line -> the slim row below, or None
        detail(text)            the same line -> the record shown when clicked
        discover_servers(seen)  the hostnames offered in the panel's dropdown

    Only `parse` has to be implemented; the base class handles the other two
    well enough for a format that has no config to interrogate.

    The slim row is the wire format. Its keys are single letters on purpose: at
    roughly 150 bytes a row the field names are a real fraction of the payload,
    and this is polled sub-second.

        s   sequence number — assigned by the tailer, never by the provider
        t   timestamp, epoch seconds as a float
        h   host, verbatim as the client sent it
        m   request method              u   request URI
        c   status code                 z   response size in bytes
        d   duration in seconds         ip  client address
        ua  user-agent, truncated       pr  protocol

    Leave a key out rather than filling it with a zero when the log format does
    not carry that field: the panel renders a missing `d` as nothing at all,
    where a `0` would read as a request that took no time.

    To support another server, subclass this, set `type` and `default_path`,
    implement `parse`, and add it to PROVIDER_TYPES.
    """

    type = "generic"
    label = "Access log"
    default_path = ""

    def __init__(self, path=None, label=None, ident=None, **options):
        self.path = path or self.default_path
        self.label = label or self.label
        self.id = ident or self.type
        self.options = options

    def parse(self, text):
        """Return the slim row for one log line, or None to skip it.

        None is the answer for anything that isn't a request — a lifecycle
        message in the same file, a half-written line during a roll, a line in
        some other format. It is not an error path and is not logged."""
        raise NotImplementedError

    def detail(self, text):
        """The full record behind a row, shown when it is clicked.

        The tailer keeps the original line as text and calls this only on
        demand, so an expensive parse here costs nothing until someone asks."""
        try:
            rec = json.loads(text)
            if isinstance(rec, dict):
                return rec
        except Exception:
            pass
        return {"raw": text}

    def discover_servers(self, observed):
        """(hostnames, source) for the panel's dropdown, best first.

        `observed` is every host seen in the ring, already normalised. A
        provider that can read its server's config should return those names
        first — a site with no traffic yet still belongs in the list — and say
        where they came from, which the dashboard shows as the list's source."""
        return list(observed), "observed"


class CaddyProvider(LogProvider):
    """Caddy's structured JSON access log (`log { format json }`).

    One line per request, already machine-readable, and it carries everything
    the panel shows including sub-millisecond timing. Notable shapes: `ts` is a
    float epoch (Caddy can emit iso8601, but this host's Caddyfile documents why
    it doesn't), header values are arrays of strings even when single-valued,
    and `Authorization` arrives already redacted by Caddy."""

    type = "caddy"
    label = "Caddy"
    default_path = "/var/log/caddy/access.log"

    def __init__(self, path=None, label=None, ident=None, admin=CADDY_ADMIN,
                 **options):
        super().__init__(path=path, label=label, ident=ident, **options)
        self.admin = admin

    def parse(self, text):
        rec = json.loads(text)
        if not isinstance(rec, dict) or rec.get("msg") != "handled request":
            return None
        req = rec.get("request") or {}
        headers = req.get("headers") or {}
        return {
            "t": rec.get("ts", 0.0),    # float epoch seconds, not iso8601
            "h": req.get("host", ""),
            "m": req.get("method", ""),
            "u": req.get("uri", ""),
            "c": rec.get("status", 0),
            "d": rec.get("duration", 0.0),
            "z": rec.get("size", 0),
            # client_ip honours trusted proxies; remote_ip is the socket peer.
            "ip": req.get("client_ip") or req.get("remote_ip", ""),
            "ua": _first_header(headers.get("User-Agent"))[:LOG_UA_MAX],
            "pr": req.get("proto", ""),
        }

    def discover_servers(self, observed):
        """The hostname list behind the panel's dropdown.

        Caddy's own server keys are adapter-generated (`srv0`, `srv1`) and mean
        nothing to a human, but each server's `logs.logger_names` maps the
        hostnames it serves to the logger that writes them — exactly the list we
        want, in the order the config defines it. Only the keys are used, so it
        doesn't matter that older Caddy versions write the value as a bare
        string where newer ones write a list.

        The call is read-only, loopback, and strictly optional: if the admin API
        is disabled or moved we fall back to the hosts actually seen in the log.
        Nothing here proxies the admin API — it also accepts POST /load, and
        must never be reachable from the dashboard."""
        hosts = []
        try:
            req = urllib.request.Request(
                self.admin + "/config/apps/http/servers",
                headers={"User-Agent": "server-vitals"})
            with urllib.request.urlopen(req, timeout=1) as resp:
                servers = json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
            for _key, srv in (servers or {}).items():
                names = ((srv or {}).get("logs") or {}).get("logger_names") or {}
                for host in names:
                    host = host_key(host)
                    if host and host not in hosts:
                        hosts.append(host)
        except Exception:
            pass
        source = "admin" if hosts else "observed"
        # Append anything we've seen logged that the admin list didn't mention —
        # a site logging to the default logger has no logger_names entry.
        for host in observed:
            if host and host not in hosts:
                hosts.append(host)
        return hosts, source


# The NCSA combined line, anchored on the parts that are actually fixed: the
# bracketed timestamp, the quoted request, and the status/size pair. Everything
# to the left is captured whole and split afterwards, because that prefix has a
# variable number of fields — `%h %l %u` normally, one more when the format
# leads with the vhost. Anchoring beats an optional leading group, which would
# have to backtrack through an ambiguity on every single line.
_CLF_RE = re.compile(
    r'^(?P<pre>\S+(?:[ \t]+\S+)*?)[ \t]+'
    r'\[(?P<stamp>\d{1,2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}[^\]]*)\][ \t]+'
    r'"(?P<request>(?:[^"\\]|\\.)*)"[ \t]+'
    r'(?P<status>\d{3})[ \t]+(?P<size>-|\d+)'
    r'(?P<post>.*)$')
_CLF_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# $request_time / %D, however the operator chose to label it. Bare trailing
# floats are deliberately not guessed at — too many formats end in a number
# that isn't a duration.
_CLF_DURATION_RE = re.compile(
    r'\b(?:rt|request_time|duration|upstream_response_time)=([0-9]+(?:\.[0-9]+)?)')
_CLF_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
# `#` to end of line — the comment syntax nginx and Apache share.
_CONF_COMMENT_RE = re.compile(r"#[^\n]*")


def _clf_value(value):
    """A bare `-` is how the combined format spells "absent" — a request with no
    referer logs `"-"`, not an empty string. Carry that through as empty so the
    panel omits the field instead of printing a stray dash."""
    return "" if value == "-" else value


def _clf_epoch(stamp):
    """`10/Oct/2000:13:55:36 +1000` -> epoch seconds.

    Hand-rolled rather than strptime: `%b` reads month names through LC_TIME, so
    a service started under a non-English locale would silently stop parsing its
    own logs. This runs on every line, and is also several times faster."""
    try:
        day = int(stamp[0:2]) if stamp[2] == "/" else int(stamp[0:1])
        rest = stamp[3:] if stamp[2] == "/" else stamp[2:]
        month = _CLF_MONTHS[rest[0:3]]
        year = int(rest[4:8])
        hour = int(rest[9:11])
        minute = int(rest[12:14])
        second = int(rest[15:17])
        epoch = timegm((year, month, day, hour, minute, second, 0, 1, 0))
    except Exception:
        return 0.0
    tz = stamp[-5:]
    if len(stamp) >= 5 and tz[0] in "+-":
        try:
            offset = int(tz[1:3]) * 3600 + int(tz[3:5]) * 60
            epoch -= offset if tz[0] == "+" else -offset
        except ValueError:
            pass
    return float(epoch)


class CombinedProvider(LogProvider):
    """The NCSA combined log format, which nginx and Apache both write by
    default and which a dozen other servers copy.

    It carries less than Caddy's JSON: the timestamp has one-second resolution,
    and there is no duration and no vhost unless the operator added them. Both
    common additions are picked up automatically — a leading `%v`/`$host` field,
    and a `rt=<seconds>` style token anywhere after the user-agent — and what
    genuinely isn't in the line is left out of the row rather than faked.

    Without a vhost field every row reports an empty host, so the dropdown can
    only offer what the server's config declares. That is the main reason to log
    `vhost_combined` (Apache) or to prefix `$host` (nginx) when several sites
    share one file."""

    type = "combined"
    label = "Access log"
    default_path = ""
    # Where to look for declared server names, and the directive that holds
    # them. Subclasses set both; an empty conf_dir means config discovery is
    # skipped and the dropdown lists only what has been seen in the log.
    conf_dir = ""
    conf_re = None

    def __init__(self, path=None, label=None, ident=None, conf_dir=None,
                 **options):
        super().__init__(path=path, label=label, ident=ident, **options)
        if conf_dir is not None:
            self.conf_dir = conf_dir

    def parse(self, text):
        m = _CLF_RE.match(text)
        if not m:
            return None
        # `%h %l %u` from the right, so an extra leading vhost field lands where
        # we can see it and a stray extra field never shifts the client IP.
        pre = m.group("pre").split()
        ip = pre[-3] if len(pre) >= 3 else (pre[0] if pre else "")
        host = pre[0] if len(pre) >= 4 else ""
        method = uri = proto = ""
        request = m.group("request")
        if request and request != "-":
            parts = request.split(" ")
            method = parts[0]
            if len(parts) >= 2:
                uri = parts[1]
            if len(parts) >= 3:
                proto = parts[-1]
        post = m.group("post")
        quoted = _CLF_QUOTED_RE.findall(post)
        size = m.group("size")
        row = {
            "t": _clf_epoch(m.group("stamp")),
            "h": host,
            "m": method,
            "u": uri,
            "c": int(m.group("status")),
            "z": 0 if size == "-" else int(size),
            "ip": ip,
            # [0] is the referer; the panel shows the user-agent, and the
            # referer is in the detail view with everything else.
            "ua": _clf_value(quoted[1] if len(quoted) > 1 else "")[:LOG_UA_MAX],
            "pr": proto,
        }
        dur = _CLF_DURATION_RE.search(post)
        if dur:
            row["d"] = float(dur.group(1))
        return row

    def detail(self, text):
        """The named fields, plus the original line.

        There is no richer record hiding behind a combined-format line the way
        there is behind Caddy's JSON, so this is the same parse laid out in
        full — including the referer, which the row has no room for."""
        m = _CLF_RE.match(text)
        if not m:
            return {"raw": text}
        pre = m.group("pre").split()
        quoted = _CLF_QUOTED_RE.findall(m.group("post"))
        rec = {
            "raw": text,
            "vhost": pre[0] if len(pre) >= 4 else "",
            "client_ip": pre[-3] if len(pre) >= 3 else "",
            "identity": pre[-2] if len(pre) >= 2 else "",
            "user": pre[-1] if pre else "",
            "time_local": m.group("stamp"),
            "ts": _clf_epoch(m.group("stamp")),
            "request": m.group("request"),
            "status": int(m.group("status")),
            "size": m.group("size"),
            "referer": _clf_value(quoted[0] if quoted else ""),
            "user_agent": _clf_value(quoted[1] if len(quoted) > 1 else ""),
        }
        if len(quoted) > 2:
            rec["extra_quoted"] = quoted[2:]
        dur = _CLF_DURATION_RE.search(m.group("post"))
        if dur:
            rec["duration"] = float(dur.group(1))
        return rec

    def discover_servers(self, observed):
        hosts = self._from_config()
        source = "config" if hosts else "observed"
        for host in observed:
            if host and host not in hosts:
                hosts.append(host)
        return hosts, source

    def _from_config(self):
        """Server names declared in the config tree, in file order.

        Read-only, bounded, and entirely optional — the config is usually
        readable by anyone, but if it isn't (or isn't there) the dropdown falls
        back to the hosts seen in the log and nothing is reported as broken."""
        if not self.conf_dir or self.conf_re is None:
            return []
        hosts = []
        files = 0
        try:
            for root, dirs, names in os.walk(self.conf_dir):
                dirs.sort()
                for name in sorted(names):
                    # `*.conf`, or an extensionless name — Debian's
                    # sites-enabled/default is the common case for the latter.
                    # Anything else (.bak, .dpkg-old, .save) is not live config.
                    if not name.endswith(LOG_CONF_SUFFIXES) and "." in name:
                        continue
                    if files >= LOG_CONF_MAX_FILES:
                        return hosts
                    files += 1
                    try:
                        with open(os.path.join(root, name), "r",
                                  encoding="utf-8", errors="replace") as fh:
                            text = fh.read(LOG_CONF_MAX_BYTES)
                    except OSError:
                        continue
                    # Drop comments first, so a site someone commented out
                    # doesn't come back as a live entry in the dropdown.
                    text = _CONF_COMMENT_RE.sub("", text)
                    for match in self.conf_re.finditer(text):
                        for token in match.group(1).split():
                            host = host_key(token.strip('"\'').rstrip(";"))
                            # `_` is nginx's catch-all and a regex name starts
                            # with `~`; neither is a hostname anyone can pick.
                            if (not host or host in ("_", "*") or host[0] == "~"
                                    or host in hosts):
                                continue
                            hosts.append(host)
        except OSError:
            pass
        return hosts


class NginxProvider(CombinedProvider):
    """nginx. Defaults to the stock `combined` log and `/etc/nginx`.

    nginx's `combined` has no vhost field, so with several sites in one file the
    rows carry no host and the dropdown is config-only. Prefixing the format
    with `$host` fixes that:

        log_format vhost '$host $remote_addr - $remote_user [$time_local] '
                         '"$request" $status $body_bytes_sent '
                         '"$http_referer" "$http_user_agent" rt=$request_time';
    """

    type = "nginx"
    label = "nginx"
    default_path = "/var/log/nginx/access.log"
    conf_dir = "/etc/nginx"
    # Deliberately not anchored to the start of a line: `server { server_name
    # a.example; }` on one line is perfectly ordinary nginx. The lookbehind is
    # what keeps it from matching the tail of some longer directive name.
    conf_re = re.compile(r"(?<![\w-])server_name\s+([^;{}\n]+)")


class ApacheProvider(CombinedProvider):
    """Apache httpd. Defaults to `/var/log/apache2/access.log` and `/etc/apache2`
    (Debian layout; on RHEL set path=/var/log/httpd/access_log and
    conf_dir=/etc/httpd).

    Apache ships a `vhost_combined` format whose leading `%v:%p` is picked up
    automatically, which is the one to use when several sites share a file."""

    type = "apache"
    label = "Apache"
    default_path = "/var/log/apache2/access.log"
    conf_dir = "/etc/apache2"
    conf_re = re.compile(r"(?<![\w-])Server(?:Name|Alias)\s+([^\n]+)", re.I)


PROVIDER_TYPES = {
    cls.type: cls for cls in (CaddyProvider, NginxProvider, ApacheProvider,
                              CombinedProvider)
}


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------
class LogTailer:
    """Follows one access log and keeps the newest entries in a ring buffer,
    each stamped with a monotonic sequence number.

    All of the file handling here is format-agnostic — the provider it holds is
    the only thing that knows what a line means.

    Same contract as CpuSampler: one background thread owns every read, state is
    published under a lock, and a transient failure is logged rather than allowed
    to kill the thread. A request never touches the disk — it reads the warm ring.

    The sequence number is what makes sub-second polling affordable. A client
    sends the highest sequence it has seen and gets back only what has arrived
    since, so the steady-state response on a quiet server is an empty list of a
    few dozen bytes. Sequences are monotonic for the life of the process and are
    never reused, so a log rotation can't silently resync a client onto the wrong
    entries — it shows up as a gap instead. They are also per tailer: a cursor
    only means anything alongside the provider it came from.
    """

    def __init__(self, provider, interval=LOG_TAIL_INTERVAL):
        self.provider = provider
        self.path = provider.path
        self.interval = interval
        self._lock = threading.Lock()
        self._ring = deque(maxlen=LOG_RING_SIZE)  # (seq, slim_row, raw_line_text)
        self._seq = 0
        self._skipped = 0
        self._available = False
        self._reason = "starting"
        self._servers = []
        self._servers_source = "none"
        # Touched only by the tailer thread, so deliberately not under the lock.
        self._fh = None
        self._ident = None      # (st_dev, st_ino) of the open handle
        self._carry = b""       # trailing bytes of a line the server is mid-write on
        self._resume = None     # (ident, offset) to pick up from if the same file returns
        self._next_servers_refresh = 0.0

    # -- reading -----------------------------------------------------------
    def _open(self, backfill):
        """(Re)open the log. On the first open we seek back a little so a freshly
        loaded dashboard has history to show immediately; after a rotation we
        start at zero, because the new file *is* the new history."""
        fh = open(self.path, "rb")
        st = os.fstat(fh.fileno())
        ident = (st.st_dev, st.st_ino)
        # If this is the same file we were reading before it briefly went away —
        # a permissions blip, or someone moving it aside and back — carry on from
        # where we stopped instead of backfilling. Otherwise every hiccup would
        # replay the last chunk of the log as if it were new traffic.
        if self._resume and self._resume[0] == ident and st.st_size >= self._resume[1]:
            fh.seek(self._resume[1])
        elif backfill:
            start = max(0, st.st_size - LOG_BACKFILL_BYTES)
            if start:
                fh.seek(start)
                fh.readline()   # drop the partial line the seek landed inside
        self._fh = fh
        self._ident = ident
        self._resume = None
        self._carry = b""

    def _close(self):
        if self._fh is not None:
            try:
                # Remember where we were, in case this same file comes back.
                if self._ident is not None:
                    self._resume = (self._ident, self._fh.tell())
            except Exception:
                self._resume = None
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._ident = None
        self._carry = b""

    def _poll_file(self):
        # Both failures are recoverable and both are worth naming: the log may not
        # exist yet on a fresh host, and it is routinely unreadable to the service
        # user until the ACL in the README is applied. Note that stat() succeeds
        # on a file we have no permission to *read*, so the open below is where a
        # permissions problem actually shows up — catching it only around stat()
        # would leave the panel reporting nothing at all.
        try:
            st = os.stat(self.path)
            if self._fh is None:
                self._open(backfill=True)
            elif (st.st_dev, st.st_ino) != self._ident:
                # The server rolled the file out from under us. Drain what is
                # still in the old handle first — anything written between our
                # last read and the rename lives only there — then follow the
                # new file.
                self._ingest(self._fh.read())
                self._close()
                self._open(backfill=False)
            elif st.st_size < self._fh.tell():
                # Truncated in place rather than rolled (`: > access.log`).
                # Shrinking below our offset is the only signal available here,
                # which in theory misses a truncate refilled past that offset
                # inside one poll interval — in practice that needs the whole
                # file rewritten in 250ms. If it ever did happen the reader
                # realigns on the next newline, costing the straddled entry.
                self._fh.seek(0)
                self._carry = b""
            data = self._fh.read()
        except FileNotFoundError:
            self._close()
            return self._publish(False, "not_found")
        except PermissionError:
            self._close()
            return self._publish(False, "permission_denied")
        except IsADirectoryError:
            self._close()
            return self._publish(False, "not_a_file")

        self._ingest(data)
        self._publish(True, "ok")

    def _ingest(self, chunk):
        if not chunk:
            return
        lines = (self._carry + chunk).split(b"\n")
        # Whatever follows the last newline is an incomplete line if the server
        # is mid-write. Hold it back until the rest of it arrives.
        self._carry = lines.pop()
        if len(self._carry) > LOG_MAX_LINE:
            self._carry = b""   # not a request; don't grow this buffer unbounded
        fresh = []
        skipped = 0
        for raw in lines:
            if not raw.strip():
                continue
            row, text = self._project(raw)
            if row is None:
                skipped += 1
            else:
                fresh.append((row, text))
        if not fresh and not skipped:
            return
        with self._lock:
            self._skipped += skipped
            for row, text in fresh:
                self._seq += 1
                row["s"] = self._seq
                self._ring.append((self._seq, row, text))

    def _project(self, raw):
        """One raw line through the provider, or (None, None) to drop it.

        Dropping is normal and silent: the same file carries the odd non-request
        entry, a half-written line survives a roll, and a provider pointed at a
        format it doesn't recognise says so by declining every line. That last
        case is the one worth surfacing, so the count is kept and reported —
        a panel showing nothing is otherwise indistinguishable from a quiet site.

        A provider that *raises* is also just a dropped line: a single hostile
        request must not be able to stop the tail, and the request path only ever
        sees the ring."""
        try:
            text = raw.decode("utf-8", "replace")
            row = self.provider.parse(text)
        except Exception:
            return None, None
        if not isinstance(row, dict):
            return None, None
        return row, text

    # -- publishing --------------------------------------------------------
    def _publish(self, available, reason):
        with self._lock:
            self._available = available
            self._reason = reason

    def _refresh_servers(self):
        with self._lock:
            observed = sorted({host_key(row["h"]) for _s, row, _r in self._ring
                               if row.get("h")})
        try:
            hosts, source = self.provider.discover_servers(observed)
        except Exception:
            traceback.print_exc()
            hosts, source = list(observed), "observed"
        with self._lock:
            self._servers = [h for h in hosts if h]
            self._servers_source = source

    def _run(self):
        while True:
            try:
                self._poll_file()
            except Exception:
                # A transient read error must not kill the tailer — the panel
                # would just freeze with no explanation. Log it and carry on.
                traceback.print_exc()
            now = time.monotonic()
            if now >= self._next_servers_refresh:
                self._next_servers_refresh = now + LOG_SERVERS_REFRESH
                try:
                    self._refresh_servers()
                except Exception:
                    traceback.print_exc()
            time.sleep(self.interval)

    def start(self):
        threading.Thread(target=self._run, name="log-" + self.provider.id,
                         daemon=True).start()
        return self

    # -- reading (request path; never blocks on I/O) -----------------------
    def snapshot(self, since=0, host="", limit=LOG_MAX_BATCH):
        """Entries newer than `since`, oldest first, optionally for one host.

        Returns (rows, latest_seq, gap). `gap` means the client's cursor fell off
        the back of the ring: entries it never saw have already been evicted, and
        the panel marks the discontinuity rather than quietly stitching it shut.

        Rows are handed out by reference — they are never mutated after being
        published, so there is nothing to copy."""
        limit = max(1, min(limit, LOG_MAX_BATCH))
        with self._lock:
            latest = self._seq
            # The overwhelmingly common case at a sub-second poll: nothing new.
            # One comparison instead of a walk over the ring.
            if since >= latest:
                return [], latest, False
            rows = []
            gap = False
            for seq, row, _raw in reversed(self._ring):
                if seq <= since:
                    break
                if host and host_key(row["h"]) != host:
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    break
            else:
                # Walked the whole ring without reaching the cursor, so whatever
                # sat between it and the oldest surviving entry is gone.
                oldest = self._ring[0][0] if self._ring else latest
                gap = 0 < since < oldest - 1
            rows.reverse()
            return rows, latest, gap

    def detail(self, seq):
        """The full original record for one entry, or None once it has aged out.

        The ring keeps raw text and lets the provider parse on demand: that is a
        fraction of the memory of holding decoded objects, and the full record is
        only ever needed when someone actually clicks a row."""
        text = None
        with self._lock:
            for entry_seq, _row, raw in reversed(self._ring):
                if entry_seq == seq:
                    text = raw
                    break
                if entry_seq < seq:
                    return None
        if text is None:
            return None
        try:
            return self.provider.detail(text)
        except Exception:
            traceback.print_exc()
            return None

    def status(self):
        with self._lock:
            return self._available, self._reason, self._seq, len(self._ring)

    def skipped(self):
        with self._lock:
            return self._skipped

    def servers(self):
        with self._lock:
            return list(self._servers), self._servers_source


class LogRegistry:
    """The configured providers, in order, each with its own tailer thread.

    Config order is the order the dashboard shows, and the first entry is what
    the panel opens on. One provider is the ordinary case and the dashboard
    hides the picker entirely; several is what a host running both Caddy and
    nginx wants.

    A bad entry is skipped with a message to the journal rather than taken as
    fatal: a typo in one provider must not cost you the whole dashboard."""

    def __init__(self, configs):
        self._tailers = {}
        self._order = []
        for cfg in configs or []:
            try:
                provider = self._build(cfg)
            except Exception as exc:
                print("server-vitals: ignoring log provider %r: %s" % (cfg, exc),
                      file=sys.stderr, flush=True)
                continue
            # Two of the same type is legitimate (two nginx logs, say), so make
            # the ids unique rather than letting the second silently win.
            base = provider.id
            n = 2
            while provider.id in self._tailers:
                provider.id = "%s-%d" % (base, n)
                n += 1
            self._tailers[provider.id] = LogTailer(provider)
            self._order.append(provider.id)

    @staticmethod
    def _build(cfg):
        if isinstance(cfg, str):
            cfg = {"type": cfg}
        if not isinstance(cfg, dict):
            raise ValueError("expected an object or a type name")
        cfg = dict(cfg)
        kind = cfg.pop("type", "")
        cls = PROVIDER_TYPES.get(kind)
        if cls is None:
            raise ValueError("unknown type %r (known: %s)"
                             % (kind, ", ".join(sorted(PROVIDER_TYPES))))
        return cls(ident=cfg.pop("id", None), **cfg)

    def ids(self):
        return list(self._order)

    def default_id(self):
        return self._order[0] if self._order else ""

    def get(self, provider_id=None):
        """The named tailer, or the default one for an empty/absent id.

        An unknown id returns None rather than falling back, so a stale
        localStorage value shows up as a named error instead of quietly
        streaming some other server's traffic."""
        if not provider_id:
            provider_id = self.default_id()
        return self._tailers.get(provider_id)

    def describe(self):
        out = []
        for pid in self._order:
            tailer = self._tailers[pid]
            available, reason, _seq, buffered = tailer.status()
            out.append({
                "id": pid,
                "type": tailer.provider.type,
                "label": tailer.provider.label,
                "path": tailer.provider.path,
                "available": available,
                "reason": reason,
                "buffered": buffered,
            })
        return out

    def start(self):
        for pid in self._order:
            self._tailers[pid].start()
        return self


def load_log_providers():
    """LOG_PROVIDERS, unless an operator has left an override in
    LOG_PROVIDERS_FILE.

    The file exists so a host can add nginx alongside Caddy without editing the
    deployed script (which `make deploy` would overwrite on the next release).
    It holds the same shape as LOG_PROVIDERS — either the bare list, or an
    object with a "providers" key. Anything wrong with it is reported to the
    journal and ignored; the built-in list is always the fallback, so a
    malformed override can't leave the dashboard with no logs at all."""
    try:
        with open(LOG_PROVIDERS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return LOG_PROVIDERS
    except Exception as exc:
        print("server-vitals: ignoring %s: %s" % (LOG_PROVIDERS_FILE, exc),
              file=sys.stderr, flush=True)
        return LOG_PROVIDERS
    if isinstance(data, dict):
        data = data.get("providers")
    if not isinstance(data, list) or not data:
        print("server-vitals: %s has no provider list; using the built-in one"
              % LOG_PROVIDERS_FILE, file=sys.stderr, flush=True)
        return LOG_PROVIDERS
    return data


_logs = LogRegistry(load_log_providers())


def logs_payload(provider=None, since=0, host="", limit=LOG_MAX_BATCH):
    tailer = _logs.get(provider)
    if tailer is None:
        return {"available": False, "reason": "unknown_provider",
                "provider": provider or "", "path": "", "seq": 0, "records": []}
    available, reason, _seq, buffered = tailer.status()
    if not available:
        # Answer 200 with an explicit reason rather than an empty list. An
        # unreadable log and a quiet one look identical otherwise, and the panel
        # would sit there showing nothing with no way to tell you why. The path
        # goes with it so the message names the file to go and fix.
        return {"available": False, "reason": reason, "provider": tailer.provider.id,
                "path": tailer.path, "seq": 0, "records": []}
    rows, latest, gap = tailer.snapshot(since, host, limit)
    payload = {
        "available": True,
        "provider": tailer.provider.id,
        "seq": latest,
        "gap": gap,
        "buffered": buffered,
        "records": rows,
    }
    # Only when there is something to say: this response is the sub-second poll,
    # and every key costs bandwidth on every tick forever.
    skipped = tailer.skipped()
    if skipped:
        payload["skipped"] = skipped
    return payload


def stats_payload():
    cpu, cores = _sampler.snapshot()
    mem = memory_stats()
    disk = disk_usage("/")
    return {
        "timestamp": time.time(),
        "server_ip": server_ip(),
        "public_ip": public_ip(),
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
  /* Two columns: the dashboard, and the Caddy access-log panel beside it. The
     dashboard keeps its own vertical layout — fixed header, panels sharing the
     remaining height evenly — which now lives on #mainpane rather than <body>. */
  body { display: flex; flex-direction: row; overflow: hidden; }
  #mainpane { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column;
    overflow: hidden; }
  header { flex: 0 0 auto; padding: 12px 18px; border-bottom: none;
    display: flex; gap: 18px; align-items: center; }
  header h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: .14em;
    font-family: "Avenir Next", "Segoe UI", system-ui, -apple-system,
      "Helvetica Neue", Arial, sans-serif;
    /* Clip a long host label to an ellipsis instead of letting it widen the flex
       row — otherwise a long hostname pushes the controls onto a second line
       (they wrap on narrow screens). min-width:0 lets this flex item shrink. */
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
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
  /* The two top strips share one rounded, bordered box that matches the panels.
     overflow:hidden clips the canvases to the rounded corners. */
  #topstrips { flex: 0 0 auto; margin: 12px 18px 0; background: #0e1216;
    border: 1px solid #20262d; border-radius: 6px; overflow: hidden; }
  #statusbar { flex: 0 0 auto; height: var(--strip-h);
    background: #0e1216; border-bottom: none; }
  #statusbar #heatmap { display: block; width: 100%; height: 100%; }
  #latencybar { flex: 0 0 auto; height: var(--strip-h);
    background: #0e1216; border-bottom: none; }
  #latencybar #latencymap { display: block; width: 100%; height: 100%; }
  header .controls { margin-left: auto; display: flex; gap: 12px; align-items: center;
    flex: none; }  /* never shrink: the title clips before the controls give ground */
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
  /* Text variant (the log-panel toggle): no drawn glyph, just a label. */
  header button.ctl-btn.ctl-text { color: #9ba6b2; font-size: 11px; padding: 4px 9px;
    letter-spacing: .04em; text-transform: uppercase; font-family: inherit; }
  header button.ctl-btn.ctl-text::before,
  header button.ctl-btn.ctl-text::after { display: none; }
  header button.ctl-btn.ctl-text:hover { color: #d8dde3; }
  header button.ctl-btn.ctl-text[aria-pressed="false"] { color: #4b545e;
    border-color: #20262d; }
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
    position: relative; overflow: hidden; }
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
  svg { display: block; width: 100%; overflow: hidden; }
  /* The single-series panels let their graph grow to fill the panel height. */
  .panel > svg { flex: 1 1 auto; min-height: 0; }
  .grid { stroke: #20262d; stroke-width: 1; }
  /* paint-order halo keeps the y labels readable where they overlay the chart fill. */
  .axis { fill: #6c7886; font-size: 10px; font-family: inherit; paint-order: stroke; stroke: #0e1216; stroke-width: 2.2px; stroke-linejoin: round; }
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
  .core { position: relative; background: #0e1216; border: 1px solid #1b2128;
    border-radius: 5px; padding: 3px 5px; display: flex; flex-direction: column;
    min-height: 0; transition: background 600ms linear; }
  /* Label floats centred over the graph instead of taking a header row, so the
     graph itself stretches to fill the whole cell. */
  .core-head { position: absolute; inset: 0; z-index: 1; display: flex; gap: 6px;
    align-items: center; justify-content: center; pointer-events: none; }
  .core-head .core-name { font-size: 10px; letter-spacing: .04em;
    text-transform: uppercase; color: #ffffff;
    text-shadow: 0 1px 2px #000, 0 0 3px #000; }
  .core-head .core-val { font-size: 11px; color: #b8eec5;
    text-shadow: 0 1px 2px #000, 0 0 3px #000; }
  svg.core-svg { flex: 1 1 auto; min-height: 0; height: auto; }
  footer { flex: 0 0 auto; text-align: center; padding: 7px 18px;
    font-size: 11px; color: #6c7886; border-top: 1px solid #20262d; }
  footer a { color: #9ba6b2; text-decoration: none; }
  footer a:hover { color: #d8dde3; text-decoration: underline; }
  footer a.ver { color: #4b545e; margin-right: 8px; }
  footer a.ver:hover { color: #6c7886; text-decoration: underline; }
  /* ---- Access-log panel ------------------------------------------------ */
  /* Width lives in a custom property so the drag handler can set it per-frame
     without touching the flex rules. Clamping is done in JS (it needs the live
     viewport width); min-width stays 0 so the collapse can take it to nothing. */
  #logs { flex: 0 0 var(--logs-w, 20%); min-width: 0; display: flex;
    flex-direction: column; overflow: hidden; background: #0e1216; }
  #logsplit { flex: 0 0 7px; cursor: col-resize; background: #0e1216;
    border-left: 1px solid #20262d; border-right: 1px solid #20262d;
    position: relative; }
  /* A short grip, so the splitter reads as draggable rather than as a rule. */
  #logsplit::after { content: ""; position: absolute; top: 50%; left: 2px;
    width: 1px; height: 26px; margin-top: -13px; background: #3a4553;
    box-shadow: 2px 0 0 #3a4553; }
  #logsplit:hover, body.dragging #logsplit { background: #161b21; }
  #logsplit:hover::after, body.dragging #logsplit::after {
    background: #6c7886; box-shadow: 2px 0 0 #6c7886; }
  /* Hold the cursor and kill text selection for the whole drag, not just over
     the 7px handle the pointer started on. */
  body.dragging { cursor: col-resize; user-select: none; }
  body.logs-closed #logs, body.logs-closed #logsplit { display: none; }

  #logs-head { flex: 0 0 auto; padding: 9px 10px; background: #11151a;
    border-bottom: 1px solid #20262d; display: flex; flex-direction: column; gap: 7px; }
  .logs-row { display: flex; align-items: center; gap: 6px; }
  #logs-title { font-size: 12px; letter-spacing: .06em; text-transform: uppercase;
    color: #9ba6b2; flex: 0 0 auto; }
  #log-host { flex: 1 1 auto; min-width: 0; }
  /* Only present when more than one provider is configured, and it takes the
     title's place rather than a row of its own — at 20% width there isn't one
     to spare. Capped so a long label can't squeeze out the site dropdown. */
  #log-provider { flex: 0 1 auto; min-width: 0; max-width: 45%; }
  #log-provider[hidden], #logs-title[hidden] { display: none; }
  #logs select, #logs input { background: #0e1216; color: #d8dde3;
    border: 1px solid #20262d; border-radius: 4px; padding: 3px 6px;
    font-family: inherit; font-size: 11px; line-height: 1.4; }
  #logs select { cursor: pointer; }
  #logs select:hover, #logs input:hover { border-color: #2a323b; }
  #logs input:focus, #logs select:focus { outline: none; border-color: #59697c; }
  #log-search { flex: 1 1 auto; min-width: 0; }
  #log-search::placeholder { color: #4b545e; }
  /* Same treatment as the header's `poll`/`window` labels. Without it this is a
     bare `1s` box beside a search field, which reads as one of the header's
     timing controls rather than the panel's own. It never shrinks — the filter
     box gives up the width instead. */
  .log-ctl { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 5px;
    font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
    color: #6c7886; }
  #logs-close, #log-order, #log-clear { background: none; border: none;
    color: #6c7886; cursor: pointer; font-family: inherit; font-size: 16px;
    line-height: 1; padding: 0 3px; flex: 0 0 auto; }
  #logs-close:hover, #log-order:hover, #log-clear:hover { color: #d8dde3; }
  /* The arrow points the way time advances as you read down the list, the same
     sense as a sorted column header. */
  #log-order { font-size: 13px; }
  #log-clear { font-size: 10px; letter-spacing: .03em; text-transform: uppercase; }
  #logs-status { flex: 0 0 auto; font-size: 10px; color: #4b545e;
    letter-spacing: .04em; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }
  #logs-status.warn { color: #dcb86d; }
  #logs-status.err  { color: #ff6b6b; }

  #log-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden;
    font-size: 11px; line-height: 1.45; scrollbar-width: thin;
    scrollbar-color: #2a323b #0e1216;
    /* Newest-first prepends rows, and insertLogNodes() adjusts scrollTop itself
       to hold the reader's position. Leave the browser's own scroll anchoring
       out of it, or the two corrections fight. */
    overflow-anchor: none; }
  .log-row { padding: 3px 10px; border-bottom: 1px solid #14191f; cursor: pointer; }
  .log-row:hover { background: #141a21; }
  .log-row.open { background: #141a21; }
  .log-line { display: flex; gap: 7px; align-items: baseline; }
  .log-t { color: #6c7886; flex: 0 0 auto; }
  .log-c { flex: 0 0 auto; font-weight: 600; min-width: 2.2em; }
  /* Status colours reuse the dashboard's ramp: healthy green, informational
     blue, client-error amber, server-error red. */
  .log-c.s2 { color: #6ddc8a; }
  .log-c.s3 { color: #6db5dc; }
  .log-c.s4 { color: #dcb86d; }
  .log-c.s5 { color: #ff6b6b; }
  .log-c.s0 { color: #6c7886; }
  .log-m { color: #9ba6b2; flex: 0 0 auto; }
  /* The URI gets the whole remaining width and truncates rather than wrapping.
     In a 20%-wide panel a long query string would otherwise break one entry
     across four ragged lines and destroy the scannability of the list; the full
     value is on the row's tooltip and in the expanded detail. */
  .log-u { color: #d8dde3; flex: 1 1 auto; min-width: 0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .log-meta { color: #4b545e; font-size: 10px; padding-left: 1px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .log-meta .log-d { color: #6c7886; }
  .log-meta .log-h { color: #6c7886; }
  /* Search hits. Log text is attacker-controlled, so these are built with
     createTextNode and never innerHTML — see markMatches() in the script. */
  .log-row mark { background: #3d3410; color: #eed9b8; border-radius: 2px;
    padding: 0 1px; }
  .log-detail { margin: 4px 0 2px; padding: 6px 8px; background: #0b0d10;
    border: 1px solid #1b2128; border-radius: 4px; color: #9ba6b2;
    font-size: 10px; line-height: 1.5; white-space: pre-wrap;
    overflow-wrap: anywhere; max-height: 320px; overflow-y: auto; }
  /* Ring-eviction marker: the client's cursor fell behind the server buffer. */
  .log-gap { padding: 3px 10px; font-size: 10px; color: #dcb86d;
    background: rgba(220,184,109,.07); border-top: 1px solid rgba(220,184,109,.25);
    border-bottom: 1px solid rgba(220,184,109,.25); }
  .log-empty { padding: 14px 10px; color: #4b545e; font-size: 11px; }
  /* Jump-to-live chip, shown only when the reader has scrolled away from the
     live end and rows are still arriving behind them. Which end that is depends
     on the reading order, so the chip moves with it. */
  #log-new { position: absolute; left: 50%; transform: translateX(-50%);
    bottom: 10px; background: #1b2a20; border: 1px solid #3a7a4a; color: #8dec9a;
    border-radius: 999px; padding: 3px 11px; font-size: 10px; cursor: pointer;
    font-family: inherit; display: none; z-index: 3; }
  #log-new.at-top { top: 10px; bottom: auto; }
  #log-new.show { display: block; }
  #log-body { position: relative; flex: 1 1 auto; min-height: 0; display: flex; }

  /* Below this the two columns stop fitting side by side, so the panel floats
     over the dashboard instead of squeezing it, and starts closed. */
  @media (max-width: 900px) {
    #logs { position: fixed; top: 0; right: 0; bottom: 0; z-index: 20;
      flex-basis: auto; width: min(88vw, 460px);
      border-left: 1px solid #20262d; box-shadow: -12px 0 28px rgba(0,0,0,.55); }
    #logsplit { display: none; }
  }
  /* Portrait phones (iPhone ≈ 390px): keep the header on one row and let the
     title ellipsize beside the controls (rather than wrapping them below), and
     tighten paddings/grids to fit the column. */
  @media (max-width: 480px) {
    header { padding: 10px 12px; gap: 8px 12px; flex-wrap: nowrap; }
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
<div id="mainpane">
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
    <button id="logs-btn" class="ctl-btn ctl-text" type="button"
      title="Toggle the access-log panel" aria-label="Toggle the access-log panel"
      aria-pressed="true">logs</button>
  </div>
</header>
<div id="topstrips">
  <div id="statusbar"><canvas id="heatmap"></canvas></div>
  <div id="latencybar"><canvas id="latencymap"></canvas></div>
</div>
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
<footer><a class="ver" href="https://github.com/dragonworx/server-vitals"
  target="_blank" rel="noopener noreferrer">v__VERSION__</a>Made with ❤️ by <a href="https://github.com/dragonworx/server-vitals"
  target="_blank" rel="noopener noreferrer">dragonworx</a></footer>
</div>
<div id="logsplit" role="separator" aria-orientation="vertical"
  aria-label="Resize the access-log panel" tabindex="0"></div>
<aside id="logs">
  <div id="logs-head">
    <div class="logs-row">
      <span id="logs-title">Access</span>
      <select id="log-provider" aria-label="Log source" hidden></select>
      <select id="log-host" aria-label="Filter by server"></select>
      <button id="log-order" type="button"></button>
      <button id="log-clear" type="button" title="Clear the displayed entries — the server keeps its own buffer, so this doesn't lose anything"
        aria-label="Clear the displayed log entries">clear</button>
      <button id="logs-close" type="button" title="Close the panel"
        aria-label="Close the access-log panel">×</button>
    </div>
    <div class="logs-row">
      <input id="log-search" type="search" autocomplete="off" spellcheck="false"
        placeholder="filter…" aria-label="Filter log entries">
      <span class="log-ctl">poll
        <select id="log-poll-sel" aria-label="Log poll interval"
          title="How often the panel checks for new entries. Independent of the dashboard's own poll interval."></select>
      </span>
    </div>
    <div id="logs-status">—</div>
  </div>
  <div id="log-body">
    <div id="log-list" role="log" aria-live="off"></div>
    <button id="log-new" type="button">new entries</button>
  </div>
</aside>
<script>
(() => {
  const FETCH_TIMEOUT_MS = 2000;
  // Equal left/right insets — the plot spans the full panel width, symmetric about
  // its centre. Y-axis value labels are drawn *inside* the plot at the left edge
  // (see render()), so they no longer need a wide left gutter.
  const BASE_PAD_L = 8, BASE_PAD_R = 8, BASE_PAD_T = 8, BASE_PAD_B = 26;

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

  // Host label (top-left) — one line that identifies this server three ways.
  // Clicking it cycles Public IP → Private IP → Hostname; each mode shows a
  // hover tooltip explaining what the value is. The chosen mode is remembered
  // in localStorage so a reload keeps the reader's preferred view.
  let _serverIp = '';        // private / local-network address
  let _serverHostname = '';  // fully-qualified domain name
  let _publicIp = '';        // internet-facing address

  const IP_MODES = ['public', 'private', 'hostname'];
  const IP_META = {
    public:   { label: 'Public IP',  get: () => _publicIp,
                desc: 'Public IP — this server’s internet-facing address.' },
    private:  { label: 'Private IP', get: () => _serverIp,
                desc: 'Private IP — this server’s address on its local network.' },
    hostname: { label: 'Hostname',   get: () => _serverHostname,
                desc: 'Hostname — this server’s fully-qualified domain name.' },
  };
  let _ipMode = 'public';
  try {
    const saved = localStorage.getItem('title-ip-mode');
    if (IP_MODES.includes(saved)) _ipMode = saved;
  } catch (e) {}

  function applyTitleIp() {
    const h = document.getElementById('title-ip');
    const meta = IP_META[_ipMode];
    const val = meta.get();
    const display = val || '—';  // em dash while a value is still loading
    if (h.textContent !== display) h.textContent = display;
    const next = IP_META[IP_MODES[(IP_MODES.indexOf(_ipMode) + 1) % IP_MODES.length]];
    h.title = meta.desc + (val ? '' : ' (unavailable)') + '\nClick to show ' + next.label + '.';
    // Browser tab: internet-facing IP if known, else private, with the hostname.
    const ip = _publicIp || _serverIp || '';
    const docTitle = _serverHostname ? (ip ? ip + ' (' + _serverHostname + ')' : _serverHostname) : ip;
    if (docTitle && document.title !== docTitle) document.title = docTitle;
  }

  function cycleIpMode() {
    _ipMode = IP_MODES[(IP_MODES.indexOf(_ipMode) + 1) % IP_MODES.length];
    try { localStorage.setItem('title-ip-mode', _ipMode); } catch (e) {}
    applyTitleIp();
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
  // Smoothed min/max for the strip ranges. A new extreme is captured immediately,
  // but otherwise each bound drifts toward the live value by RANGE_DECAY per sample.
  // So a one-off spike pushes the range out, then averages back to typical values
  // over the following polls instead of latching to the all-time extreme forever.
  const RANGE_DECAY = 0.1;
  function avgRange(lo, hi, v) {
    hi = (v > hi) ? v : hi + RANGE_DECAY * (v - hi);
    lo = (v < lo) ? v : lo + RANGE_DECAY * (v - lo);
    return [lo, hi];
  }
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
  const STRIP_LABEL = 'rgba(176,186,196,.9)';  // light grey for the section label
  const STRIP_PAD   = 6;  // logical px from each edge (fallback only)
  // Align the strip text with the graph panels below: left label sits at the graph's
  // left edge, right value at the graph's right edge. Insets measured against a panel SVG.
  function stripInsets(cv) {
    const ref = el('cpu-svg') || el('mem-svg') || el('disk-svg');
    if (!ref) return { left: STRIP_PAD, right: STRIP_PAD };
    const r = ref.getBoundingClientRect(), c = cv.getBoundingClientRect();
    if (!r.width || !c.width) return { left: STRIP_PAD, right: STRIP_PAD };
    return { left: Math.max(0, r.left - c.left), right: Math.max(0, c.right - r.right) };
  }
  // Left: section label (light grey). Center: min–max colored segments (truly centred).
  // Right: current value right-aligned. Insets align with the graphs below.
  function drawStripLabel(ctx, cv, dpr, y, label, centerSegs, curText, insets) {
    const padL = Math.round((insets ? insets.left : STRIP_PAD) * dpr);
    const padR = Math.round((insets ? insets.right : STRIP_PAD) * dpr);
    const boldFont = ctx.font;
    const normalFont = boldFont.replace('bold ', '');
    ctx.textAlign = 'left';
    ctx.font = normalFont;
    ctx.fillStyle = STRIP_LABEL;
    ctx.fillText(label, padL, y);
    if (centerSegs.length) {
      ctx.font = normalFont;
      const totalW = centerSegs.reduce((w, s) => w + ctx.measureText(s.text).width, 0);
      let x = (cv.width - totalW) / 2;
      for (const s of centerSegs) {
        ctx.fillStyle = s.color;
        ctx.fillText(s.text, x, y);
        x += ctx.measureText(s.text).width;
      }
    }
    if (curText) {
      ctx.font = boldFont;
      ctx.textAlign = 'right';
      ctx.fillStyle = STRIP_WHITE;
      ctx.fillText(curText, cv.width - padR, y);
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
    ctx.font = 'bold ' + Math.round(10.53 * dpr) + 'px ui-monospace,monospace';
    ctx.fillStyle = 'rgba(255,255,255,1)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'black';
    ctx.shadowOffsetX = 1 * dpr;
    ctx.shadowOffsetY = 1 * dpr;
    ctx.shadowBlur = 0;
    drawStripLabel(ctx, cv, dpr, cv.height / 2, 'HEALTH',
      cpuMin === Infinity ? [] : [
        { text: cpuMin.toFixed(0) + '% – ' + cpuMax.toFixed(0) + '%', color: STRIP_WHITE },
      ],
      cpuLast != null ? cpuLast.toFixed(0) + '%' : '', stripInsets(cv));
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
    ctx.font = 'bold ' + Math.round(10.53 * dpr) + 'px ui-monospace,monospace';
    ctx.fillStyle = 'rgba(255,255,255,1)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'black';
    ctx.shadowOffsetX = 1 * dpr;
    ctx.shadowOffsetY = 1 * dpr;
    ctx.shadowBlur = 0;
    const LAT_FROM = 'hsl(180,70%,72%)';
    const LAT_TO   = 'hsl(220,70%,72%)';
    drawStripLabel(ctx, cv, dpr, cv.height / 2, 'PING',
      latencyMin === Infinity ? [] : [
        { text: latencyMin.toFixed(0) + 'ms – ' + latencyMax.toFixed(0) + 'ms', color: STRIP_WHITE },
      ],
      latencyLast != null ? latencyLast.toFixed(0) + 'ms' : '', stripInsets(cv));
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
    return String(Math.round(v));  // whole numbers only — no decimal places
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

    // y-axis grid. Full version on big panels; a sparse one on core graphs when
    // opts.yAxis is set (compact x-axis stays off — the window is shown above).
    if (!compact || showYAxis) {
      // Fixed-scale panels use even quarter ticks (0/25/50/75/100); auto-scaled
      // panels aim for one nice-rounded gridline per ~34px.
      const step = (compact || opts.fixedMax != null)
        ? range / 4
        : niceStep(range / Math.max(2, Math.min(6, Math.round(PLOT_H / 34))));
      const tickStart = Math.ceil(yMin / step) * step;
      for (let t = tickStart; t <= yMax + 1e-9; t += step) {
        const y = PAD_T + (1 - (t - yMin) / range) * PLOT_H;
        svg.appendChild(svgEl('line', {
          x1: PAD_L, x2: W - PAD_R, y1: y, y2: y, class: 'grid',
        }));
        // The compact core graphs show gridlines only — no numeric markers.
        if (compact) continue;
        // Labels sit just inside the plot's left edge (overlaid on the chart) so
        // the plot itself can run edge-to-edge with symmetric margins.
        const lbl = svgEl('text', {
          x: PAD_L + 3, y: y + 3, 'text-anchor': 'start', class: 'axis',
        });
        lbl.textContent = fmtY(t) + ser.unit;
        svg.appendChild(lbl);
      }
    }

    // line + fill, broken into segments around null runs.
    const n = data.length;
    const nowMs = Date.now();
    const tsBase = timestamps.length - n; // timestamps[tsBase+i] is the clock for data[i]
    // Anchor the right edge to the newest sample's timestamp (not wall-clock now) so
    // the latest point lands exactly on the right inner edge with no trailing gap.
    const refMs = timestamps.length ? timestamps[timestamps.length - 1] : nowMs;
    // Until enough history has accumulated to span the whole configured window (e.g.
    // just after a page load, or right after widening the window), scale to however
    // much data actually exists instead of pinning to the full window — otherwise the
    // series only occupies the newest fraction of the plot and the rest reads as a
    // rendering bug rather than "still collecting samples". Floored at one poll
    // interval so two very-fresh points don't blow pxPerSec up toward infinity.
    const oldestAgeSec = (tsBase >= 0 && timestamps.length)
      ? (refMs - timestamps[tsBase]) / 1000
      : (n - 1) * pollSec;
    const windowSec = Math.min(displayWindowSec, Math.max(oldestAgeSec, pollSec));

    // x-axis labels — the plot spans `windowSec` (see above). Five evenly
    // spaced markers, each showing relative age plus the absolute clock time
    // (the absolute part in a lighter shade), e.g. "-5m 2:32pm".
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

    // Timestamp-based x: each point placed at its real age so the horizontal scale
    // stays fixed to actual time regardless of poll interval.
    const pxPerSec = PLOT_W / windowSec;  // pixels per second
    const xAt = i => {
      const tsIdx = tsBase + i;
      const ageSec = (tsIdx >= 0 && tsIdx < timestamps.length)
        ? (refMs - timestamps[tsIdx]) / 1000
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
    // Clip the line + fill to the plot rectangle so segments never spill past the
    // edges (the leftmost point of a just-trimmed series, or sub-pixel overshoot).
    const clipId = 'plot-clip-' + svgId;
    const clip = svgEl('clipPath', { id: clipId });
    clip.appendChild(svgEl('rect', { x: PAD_L, y: PAD_T, width: PLOT_W, height: PLOT_H }));
    defs.appendChild(clip);
    svg.appendChild(defs);
    const fillRef = 'url(#' + gradId + ')';
    // Everything plotted (fill, line, single-point dots) goes in this clipped group.
    const plotG = svgEl('g', { 'clip-path': 'url(#' + clipId + ')' });
    svg.appendChild(plotG);

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
        plotG.appendChild(svgEl('path', { d: area, fill: fillRef }));
        plotG.appendChild(svgEl('path', { d: d, class: 'line line-' + ser.color }));
      } else if (segEnd === segStart) {
        // single non-null point sandwiched between gaps — show as a dot
        const dotFill = { cpu: '#6ddc8a', mem: '#6db5dc', disk: '#dcb86d' }[ser.color];
        plotG.appendChild(svgEl('circle', {
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

  {
    const h = el('title-ip');
    h.style.cursor = 'pointer';
    h.addEventListener('click', cycleIpMode);
    applyTitleIp();  // render the saved mode's tooltip before the first poll lands
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
      [cpuMin, cpuMax] = avgRange(cpuMin, cpuMax, j.cpu_percent);

      const cores = Array.isArray(j.cpu_cores) ? j.cpu_cores : [];
      if (cores.length) {
        ensureCores(cores.length);
        for (let i = 0; i < cores.length; i++) {
          pushCore(i, cores[i]);
          coreValEls[i].textContent = cores[i].toFixed(0) + '%';
          coreCellEls[i].style.background = coreBg(cores[i]);
        }
      }

      _serverIp = j.server_ip || '';
      _serverHostname = j.hostname || '';
      _publicIp = j.public_ip || '';
      applyTitleIp();
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
      [latencyMin, latencyMax] = avgRange(latencyMin, latencyMax, latencyMs);
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
    syncLogPolling();              // pause covers the log stream too
  }
  pauseBtn.addEventListener('click', () => setPaused(!paused));

  // ---- Caddy access-log panel -------------------------------------------
  // Streams from /logs with a monotonic cursor: every poll sends the highest
  // sequence it has already seen and gets back only what is newer, so a quiet
  // server costs about eighty bytes a poll no matter how fast we ask. Full
  // records are ten times the size and are fetched only when a row is opened.
  const LOG_POLL_OPTIONS = [0.25, 0.5, 1, 2, 5];
  const LOG_MAX_ROWS = 1000;        // client-side cap, on the array and the DOM
  const LOG_SEARCH_DEBOUNCE = 120;
  const LOG_DETAIL_CACHE = 200;
  const LOGS_WIDTH_KEY = 'stats:logs:width:pct';
  const LOGS_OPEN_KEY  = 'stats:logs:open';
  const LOGS_PROV_KEY  = 'stats:logs:provider';
  const LOGS_HOST_KEY  = 'stats:logs:host';   // suffixed with the provider id
  const LOGS_POLL_KEY  = 'stats:logs:poll:sec';
  const LOGS_ORDER_KEY = 'stats:logs:newest';
  const LOGS_DEFAULT_PCT = 20;

  // loadChoice() only handles numbers from a fixed set; these two cover the
  // string and boolean preferences, with the same "never throw" guarantee.
  function loadString(key, dflt) {
    try { const v = localStorage.getItem(key); if (v !== null) return v; } catch (e) {}
    return dflt;
  }
  function loadFlag(key, dflt) {
    try {
      const v = localStorage.getItem(key);
      if (v === '0' || v === '1') return v === '1';
    } catch (e) {}
    return dflt;
  }

  let logRows = [];                                   // slim rows, oldest first
  let logSeq = 0;                                     // cursor: highest seq seen
  // Sequence numbers are per provider, so both the cursor and the chosen site
  // belong to one provider. '' means "whatever the server lists first", which is
  // what a first visit gets before /logs/providers has answered.
  let logProviders = [];
  let logProvider = loadString(LOGS_PROV_KEY, '');
  let logHost = null;                                 // null = never chosen
  let logFilter = '';
  let logPollSec = loadChoice(LOGS_POLL_KEY, LOG_POLL_OPTIONS, 1);
  // Which way the list reads. `logRows` is always oldest-first — the server's
  // order, and what the trimming maths assumes — so this only ever affects how
  // the DOM is built and which end counts as "live".
  let logNewestFirst = loadFlag(LOGS_ORDER_KEY, true);
  let logsOpen = loadFlag(LOGS_OPEN_KEY, true);
  let logTimer = null;
  let logInFlight = false;
  let logMissed = 0;
  let logGapPending = false;        // an eviction to mark on the next row seen
  let logSkipped = 0;               // lines the provider didn't recognise
  let logErr = '';
  let logState = { available: true, reason: '', path: '' };
  const logDetailCache = new Map();

  function hostKeyFor(pid) { return LOGS_HOST_KEY + ':' + (pid || ''); }
  function providerLabel() {
    for (const p of logProviders) if (p.id === logProvider) return p.label || p.id;
    return 'access';
  }
  // Every /logs request names its provider, so a response can't be mistaken for
  // one from the provider the reader has since switched away from.
  function provQuery(sep) {
    return logProvider ? sep + 'provider=' + encodeURIComponent(logProvider) : '';
  }

  const listEl = el('log-list');
  const newChip = el('log-new');
  const provSel = el('log-provider');
  const hostSel = el('log-host');
  const orderBtn = el('log-order');
  const searchEl = el('log-search');
  const logsBtn = el('logs-btn');
  const splitter = el('logsplit');

  // -- width ---------------------------------------------------------------
  // Stored as a percentage of the viewport, not pixels, so the split still makes
  // sense after the window is resized on a different display.
  function clampPct(pct) {
    if (!isFinite(pct)) return LOGS_DEFAULT_PCT;
    const minPct = Math.min(50, (260 / Math.max(320, window.innerWidth)) * 100);
    return Math.max(minPct, Math.min(70, pct));
  }
  let logsPct = clampPct(parseFloat(loadString(LOGS_WIDTH_KEY, '')) || LOGS_DEFAULT_PCT);
  function applyLogsWidth() {
    document.documentElement.style.setProperty('--logs-w', logsPct.toFixed(2) + '%');
  }
  function saveLogsWidth() { persist(LOGS_WIDTH_KEY, logsPct.toFixed(2)); }
  applyLogsWidth();

  splitter.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    splitter.setPointerCapture(ev.pointerId);
    document.body.classList.add('dragging');
  });
  splitter.addEventListener('pointermove', (ev) => {
    if (!splitter.hasPointerCapture(ev.pointerId)) return;
    // Measured from the right edge: the panel grows as the pointer moves left.
    logsPct = clampPct(((window.innerWidth - ev.clientX) / window.innerWidth) * 100);
    applyLogsWidth();
  });
  function endLogDrag(ev) {
    if (!splitter.hasPointerCapture(ev.pointerId)) return;
    splitter.releasePointerCapture(ev.pointerId);
    document.body.classList.remove('dragging');
    saveLogsWidth();
  }
  splitter.addEventListener('pointerup', endLogDrag);
  splitter.addEventListener('pointercancel', endLogDrag);
  splitter.addEventListener('dblclick', () => {
    logsPct = LOGS_DEFAULT_PCT; applyLogsWidth(); saveLogsWidth();
  });
  // The splitter is focusable, so it has to answer the arrow keys too.
  splitter.addEventListener('keydown', (ev) => {
    const step = ev.shiftKey ? 5 : 1;
    if (ev.key === 'ArrowLeft') logsPct = clampPct(logsPct + step);
    else if (ev.key === 'ArrowRight') logsPct = clampPct(logsPct - step);
    else return;
    ev.preventDefault(); applyLogsWidth(); saveLogsWidth();
  });

  // Every chart measures itself in real pixels rather than scaling a viewBox, so
  // anything that changes the dashboard's width has to trigger a redraw.
  // Observing #mainpane catches the drag, the collapse and window resizes at
  // once, and reuses the same 80ms debounce as the existing resize handler.
  if (window.ResizeObserver) {
    let roPrimed = false;
    new ResizeObserver(() => {
      if (!roPrimed) { roPrimed = true; return; }   // ignore the initial callback
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(renderAll, 80);
    }).observe(el('mainpane'));
  }

  // -- open / closed -------------------------------------------------------
  function syncLogPolling() {
    clearTimeout(logTimer);
    if (!paused && logsOpen) scheduleLogNext(0);
  }
  function setLogsOpen(open) {
    logsOpen = open;
    document.body.classList.toggle('logs-closed', !open);
    logsBtn.setAttribute('aria-pressed', String(open));
    persist(LOGS_OPEN_KEY, open ? '1' : '0');
    syncLogPolling();               // a closed panel polls nothing at all
  }
  logsBtn.addEventListener('click', () => setLogsOpen(!logsOpen));
  el('logs-close').addEventListener('click', () => setLogsOpen(false));

  // -- controls ------------------------------------------------------------
  const logPollSel = el('log-poll-sel');
  for (const sec of LOG_POLL_OPTIONS) {
    const o = document.createElement('option');
    o.value = String(sec);
    o.textContent = formatDur(sec);
    if (sec === logPollSec) o.selected = true;
    logPollSel.appendChild(o);
  }
  logPollSel.addEventListener('change', () => {
    logPollSec = parseFloat(logPollSel.value);
    persist(LOGS_POLL_KEY, logPollSec);
    syncLogPolling();
  });

  function applyLogOrder() {
    // The arrow points the direction time runs as you read downwards, so it
    // matches the convention of a sorted column: down means later-below.
    orderBtn.textContent = logNewestFirst ? '↑' : '↓';
    const label = logNewestFirst ? 'Newest first' : 'Oldest first';
    orderBtn.title = label + ' — click to reverse';
    orderBtn.setAttribute('aria-label', 'Log order: ' + label);
    newChip.classList.toggle('at-top', logNewestFirst);
  }
  orderBtn.addEventListener('click', () => {
    logNewestFirst = !logNewestFirst;
    persist(LOGS_ORDER_KEY, logNewestFirst ? '1' : '0');
    applyLogOrder();
    // Same rows, other way up: no refetch, and the cursor is untouched.
    renderLogList();
  });
  applyLogOrder();

  const clearBtn = el('log-clear');
  clearBtn.addEventListener('click', () => {
    // Client-side only: logSeq is untouched, so the next poll asks the server
    // for entries after where we already were and just resumes streaming —
    // nothing is refetched, and the server's own ring is never told about this.
    logRows = [];
    logMissed = 0;
    logGapPending = false;
    logDetailCache.clear();
    renderLogList();
  });

  function resetLogStream() {
    logRows = [];
    logSeq = 0;
    logMissed = 0;
    logGapPending = false;
    logSkipped = 0;
    logDetailCache.clear();
    renderLogList();
    syncLogPolling();
  }

  hostSel.addEventListener('change', () => {
    logHost = hostSel.value;
    persist(hostKeyFor(logProvider), logHost);
    resetLogStream();
  });

  provSel.addEventListener('change', () => {
    logProvider = provSel.value;
    persist(LOGS_PROV_KEY, logProvider);
    // A different log means a different cursor, a different site list and a
    // different remembered site — nothing carries over except the panel itself.
    logHost = loadString(hostKeyFor(logProvider), null);
    logState = { available: true, reason: '', path: '' };
    resetLogStream();
    loadServers();
  });

  // Which logs this server is following. Fetched once — providers come from the
  // config file and don't change while the process is running.
  async function loadProviders() {
    try {
      const r = await fetchWithTimeout('/logs/providers', FETCH_TIMEOUT_MS);
      if (!r.ok) throw new Error('http ' + r.status);
      const j = await r.json();
      logProviders = j.providers || [];
      if (logProviders.length) {
        let pick = logProvider;
        if (!pick || !logProviders.some((p) => p.id === pick)) {
          pick = j.default || logProviders[0].id;
        }
        provSel.textContent = '';
        for (const p of logProviders) {
          const o = document.createElement('option');
          o.value = p.id; o.textContent = p.label || p.id;
          provSel.appendChild(o);
        }
        provSel.value = pick;
        logProvider = pick;
        // The picker only earns its space when there is a choice to make; with
        // one provider the panel keeps its plain "Access" title.
        const many = logProviders.length > 1;
        provSel.hidden = !many;
        el('logs-title').hidden = many;
      }
    } catch (e) { /* stay on the stored/default provider; logTick reports errors */ }
    logHost = loadString(hostKeyFor(logProvider), null);
  }

  async function loadServers() {
    const forProvider = logProvider;
    let names = [];
    try {
      const r = await fetchWithTimeout('/logs/servers' + provQuery('?'), FETCH_TIMEOUT_MS);
      if (r.ok) names = (await r.json()).servers || [];
    } catch (e) { return; }         // keep whatever is already in the dropdown
    if (forProvider !== logProvider) return;   // switched while this was in flight
    hostSel.textContent = '';
    const all = document.createElement('option');
    all.value = ''; all.textContent = 'all servers';
    hostSel.appendChild(all);
    for (const n of names) {
      const o = document.createElement('option');
      o.value = n; o.textContent = n;
      hostSel.appendChild(o);
    }
    // Default to the first server the provider lists. A stored choice wins,
    // unless it names a site that no longer exists — then fall back to the first.
    let pick = logHost;
    if (pick === null || (pick && names.indexOf(pick) === -1)) pick = names[0] || '';
    hostSel.value = pick;
    if (pick !== logHost) { logHost = pick; resetLogStream(); }
  }

  let searchTimer = null;
  searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      logFilter = searchEl.value.trim().toLowerCase();
      renderLogList();
    }, LOG_SEARCH_DEBOUNCE);
  });

  // -- rendering -----------------------------------------------------------
  function rowHaystack(r) {
    // Cached per row: the filter runs over every buffered row on each keystroke.
    if (r.q === undefined) {
      r.q = (r.h + ' ' + r.m + ' ' + r.u + ' ' + r.c + ' ' + r.ip + ' ' + r.ua).toLowerCase();
    }
    return r.q;
  }
  function matchesFilter(r) {
    return !logFilter || rowHaystack(r).indexOf(logFilter) !== -1;
  }

  // Highlight by assembling text nodes and <mark> elements. Never innerHTML:
  // request paths and user-agents are attacker-controlled — anyone on the
  // internet can put markup in a URI and have it land in this log.
  function markMatches(parent, text) {
    if (!logFilter) { parent.appendChild(document.createTextNode(text)); return; }
    const hay = text.toLowerCase();
    let i = 0;
    for (;;) {
      const at = hay.indexOf(logFilter, i);
      if (at === -1) break;
      if (at > i) parent.appendChild(document.createTextNode(text.slice(i, at)));
      const m = document.createElement('mark');
      m.textContent = text.slice(at, at + logFilter.length);
      parent.appendChild(m);
      i = at + logFilter.length;
    }
    if (i < text.length) parent.appendChild(document.createTextNode(text.slice(i)));
  }

  function logSpan(cls, text) {
    const s = document.createElement('span');
    if (cls) s.className = cls;
    markMatches(s, text);
    return s;
  }
  function statusBand(c) {
    if (c >= 500) return '5';
    if (c >= 400) return '4';
    if (c >= 300) return '3';
    if (c >= 200) return '2';
    return '0';
  }
  function fmtLogTime(t) {
    const d = new Date(t * 1000);
    const p = (n, w) => String(n).padStart(w, '0');
    return p(d.getHours(), 2) + ':' + p(d.getMinutes(), 2) + ':'
      + p(d.getSeconds(), 2) + '.' + p(d.getMilliseconds(), 3);
  }
  function fmtLogDur(sec) {
    if (sec >= 1) return sec.toFixed(2) + 's';
    if (sec >= 0.001) return (sec * 1000).toFixed(1) + 'ms';
    return Math.round(sec * 1e6) + 'µs';
  }
  function fmtLogBytes(n) {
    if (n < 1024) return n + 'B';
    if (n < 1048576) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + 'K';
    return (n / 1048576).toFixed(1) + 'M';
  }

  function buildLogRow(r) {
    const row = document.createElement('div');
    row.className = 'log-row';
    row.dataset.seq = String(r.s);
    // Truncated text stays reachable on hover without a click. Setting .title
    // is an attribute assignment, not markup, so hostile URIs stay inert.
    row.title = r.m + ' ' + r.h + r.u;

    const line = document.createElement('div');
    line.className = 'log-line';
    const t = document.createElement('span');
    t.className = 'log-t';
    t.textContent = fmtLogTime(r.t);       // clock text isn't part of the search
    line.appendChild(t);
    line.appendChild(logSpan('log-c s' + statusBand(r.c), String(r.c)));
    line.appendChild(logSpan('log-m', r.m));
    line.appendChild(logSpan('log-u', r.u));
    row.appendChild(line);

    // Timing and size sit on the dim line so the URI keeps the full width above.
    const meta = document.createElement('div');
    meta.className = 'log-meta';
    // The host column is redundant once the dropdown has narrowed to one site,
    // and some log formats don't record a host at all.
    if (!logHost && r.h) {
      meta.appendChild(logSpan('log-h', r.h));
      meta.appendChild(document.createTextNode(' · '));
    }
    const d = document.createElement('span');
    d.className = 'log-d';
    // Combined-format logs carry no request time unless the operator added one.
    // Leave it out rather than print a request that apparently took 0µs.
    d.textContent = (r.d == null ? '' : fmtLogDur(r.d) + ' · ') + fmtLogBytes(r.z || 0);
    meta.appendChild(d);
    meta.appendChild(document.createTextNode(' · '));
    meta.appendChild(logSpan('', r.ip));
    if (r.ua) {
      meta.appendChild(document.createTextNode(' · '));
      meta.appendChild(logSpan('', r.ua));
    }
    row.appendChild(meta);
    return row;
  }

  // The list reads either way, so nothing below assumes the newest row is at the
  // bottom. These four are the only places that know which end is which.
  function atLogLive() {
    return logNewestFirst
      ? listEl.scrollTop < 24
      : listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 24;
  }
  function scrollLogToLive() {
    listEl.scrollTop = logNewestFirst ? 0 : listEl.scrollHeight;
    logMissed = 0;
    newChip.classList.remove('show');
  }
  function insertLogNodes(frag) {
    if (!logNewestFirst) { listEl.appendChild(frag); return; }
    // Prepending pushes everything below it down, which would yank the view out
    // from under someone reading further back. Add the inserted height to
    // scrollTop so their position stays where they left it. (#log-list turns off
    // the browser's own scroll anchoring so this is the only correction made.)
    const before = listEl.scrollHeight;
    listEl.insertBefore(frag, listEl.firstChild);
    if (listEl.scrollTop > 0) listEl.scrollTop += listEl.scrollHeight - before;
  }
  function trimLogDom() {
    // Cap the DOM separately from the row array: a filtered view holds fewer
    // nodes than rows, so the two counts are not the same number. Drop from the
    // stale end, whichever end that currently is.
    while (listEl.childElementCount > LOG_MAX_ROWS) {
      listEl.removeChild(logNewestFirst ? listEl.lastElementChild
                                        : listEl.firstElementChild);
    }
  }
  listEl.addEventListener('scroll', () => {
    if (atLogLive()) { logMissed = 0; newChip.classList.remove('show'); }
  });
  newChip.addEventListener('click', scrollLogToLive);

  function logEmptyText() {
    if (!logState.available) {
      if (logState.reason === 'permission_denied') {
        return 'cannot read ' + (logState.path || 'the access log')
          + ' — the service user needs read permission';
      }
      if (logState.reason === 'not_found') {
        return 'no access log at ' + (logState.path || 'the configured path');
      }
      if (logState.reason === 'unknown_provider') {
        return 'no such log provider is configured on this server';
      }
      return 'access log unavailable (' + (logState.reason || 'unknown') + ')';
    }
    if (logRows.length) return 'no entries match the filter';
    // Readable, non-empty, and yet nothing got through: the file almost
    // certainly isn't in the format this provider parses. Say so — otherwise
    // this is indistinguishable from a site with no traffic.
    if (logSkipped) {
      return logSkipped + ' line' + (logSkipped === 1 ? '' : 's')
        + ' did not match the ' + providerLabel() + ' log format';
    }
    return 'waiting for requests…';
  }

  function showLogEmpty() {
    const e = document.createElement('div');
    e.className = 'log-empty';
    e.textContent = logEmptyText();
    listEl.appendChild(e);
  }

  function gapNode() {
    const g = document.createElement('div');
    g.className = 'log-gap';
    g.textContent = '— older entries dropped —';
    return g;
  }

  // A gap is a property of the row that begins the batch after it, not a loose
  // node in the list. Holding it on the row is what lets it survive a re-render:
  // flipping the order or changing the filter must not quietly erase the record
  // that entries were lost. The evicted entries are older than that row, so the
  // marker goes on its older side — above it chronologically, below when reversed.
  function emitLogRow(frag, r) {
    if (logNewestFirst) {
      frag.appendChild(buildLogRow(r));
      if (r.gap) frag.appendChild(gapNode());
    } else {
      if (r.gap) frag.appendChild(gapNode());
      frag.appendChild(buildLogRow(r));
    }
  }

  function renderLogList() {
    listEl.textContent = '';
    const frag = document.createDocumentFragment();
    let shown = 0;
    // Only the newest LOG_MAX_ROWS can be on screen, so building more is waste.
    const view = logRows.slice(Math.max(0, logRows.length - LOG_MAX_ROWS));
    if (logNewestFirst) view.reverse();
    for (const r of view) {
      if (!matchesFilter(r)) continue;
      emitLogRow(frag, r);
      shown++;
    }
    listEl.appendChild(frag);
    if (!shown) showLogEmpty();
    scrollLogToLive();
    updateLogStatus();
  }

  function appendLogRows(rows, gap) {
    const stick = atLogLive();
    // A host filter can leave a poll reporting a gap with no rows to hang it on.
    // Remember it and mark whichever row turns up next, rather than dropping it.
    if (gap) logGapPending = true;
    // The array stays oldest-first whichever way the list reads, so the trim
    // below always drops the oldest and the cursor logic needs no special case.
    for (const r of rows) {
      if (logGapPending) { r.gap = true; logGapPending = false; }
      logRows.push(r);
    }
    if (logRows.length > LOG_MAX_ROWS) {
      logRows.splice(0, logRows.length - LOG_MAX_ROWS);
    }
    const batch = rows.filter(matchesFilter);
    const frag = document.createDocumentFragment();
    if (logNewestFirst) {
      for (let i = batch.length - 1; i >= 0; i--) emitLogRow(frag, batch[i]);
    } else {
      for (const r of batch) emitLogRow(frag, r);
    }
    if (frag.childNodes.length) {
      const empty = listEl.querySelector('.log-empty');
      if (empty) listEl.removeChild(empty);
      insertLogNodes(frag);
      trimLogDom();
    }
    if (stick) scrollLogToLive();
    else if (batch.length) {
      logMissed += batch.length;
      newChip.textContent = logMissed + ' new ' + (logNewestFirst ? '↑' : '↓');
      newChip.classList.add('show');
    }
    updateLogStatus();
  }

  function updateLogStatus() {
    // Why the list is empty can change without the list itself changing — the
    // first poll after a switch is what reveals a wrong log format, and by then
    // the placeholder has already been drawn. Keep it current in place rather
    // than re-rendering rows that haven't moved.
    const placeholder = listEl.querySelector('.log-empty');
    if (placeholder) placeholder.textContent = logEmptyText();
    const st = el('logs-status');
    st.classList.remove('warn', 'err');
    if (!logState.available) {
      st.classList.add('err');
      st.textContent = logState.reason === 'permission_denied' ? 'permission denied'
        : logState.reason === 'not_found' ? 'log file not found'
        : logState.reason === 'unknown_provider' ? 'unknown provider'
        : 'unavailable · ' + (logState.reason || 'unknown');
      return;
    }
    const shown = listEl.querySelectorAll('.log-row').length;
    let txt = shown + ' shown · ' + logRows.length + ' buffered';
    if (logFilter) txt += ' · filtered';
    if (logSkipped && !logRows.length) {
      txt += ' · format mismatch';
      st.classList.add('warn');
    }
    if (logErr) { txt += ' · ' + logErr; st.classList.add('warn'); }
    st.textContent = txt;
  }

  // -- polling -------------------------------------------------------------
  function scheduleLogNext(delay) {
    if (paused || !logsOpen) return;
    clearTimeout(logTimer);
    logTimer = setTimeout(logTick, delay == null ? logPollSec * 1000 : delay);
  }

  async function logTick() {
    // Nothing is read while the tab is in the background, and the cursor means
    // nothing is lost by stopping — the next poll picks up exactly where this
    // one left off. On a sub-second interval that is most of the traffic saved.
    if (document.hidden || logInFlight) { scheduleLogNext(); return; }
    logInFlight = true;
    try {
      const url = '/logs?since=' + logSeq + provQuery('&')
        + (logHost ? '&host=' + encodeURIComponent(logHost) : '');
      const r = await fetchWithTimeout(url, FETCH_TIMEOUT_MS);
      if (!r.ok) throw new Error('http ' + r.status);
      const j = await r.json();
      // Answered for a provider we have since switched away from. Its sequence
      // numbers mean nothing here, so drop it rather than corrupt the cursor.
      if (logProvider && j.provider && j.provider !== logProvider) { logErr = ''; return; }
      logSkipped = j.skipped || 0;
      const wasAvailable = logState.available;
      logState = {
        available: j.available !== false,
        reason: j.reason || '',
        path: j.path || '',
      };
      if (!logState.available) {
        logSeq = 0;                      // resync from scratch once it comes back
        if (wasAvailable) renderLogList();
      } else {
        if (!wasAvailable) renderLogList();
        if ((j.records && j.records.length) || j.gap) {
          appendLogRows(j.records || [], !!j.gap);
        }
        logSeq = j.seq;
      }
      logErr = '';
    } catch (e) {
      logErr = (e && e.name === 'AbortError') ? 'timeout'
             : (e && e.message) ? e.message : 'error';
    } finally {
      logInFlight = false;
      updateLogStatus();
      scheduleLogNext();
    }
  }

  // -- detail on demand ----------------------------------------------------
  listEl.addEventListener('click', async (ev) => {
    const row = ev.target && ev.target.closest ? ev.target.closest('.log-row') : null;
    if (!row) return;
    const open = row.querySelector('.log-detail');
    if (open) { row.removeChild(open); row.classList.remove('open'); return; }
    row.classList.add('open');
    const pre = document.createElement('pre');
    pre.className = 'log-detail';
    row.appendChild(pre);
    const seq = row.dataset.seq;
    if (logDetailCache.has(seq)) { pre.textContent = logDetailCache.get(seq); return; }
    pre.textContent = 'loading…';
    try {
      const r = await fetchWithTimeout(
        '/logs?detail=' + encodeURIComponent(seq) + provQuery('&'), FETCH_TIMEOUT_MS);
      if (r.status === 404) {
        pre.textContent = 'this entry has aged out of the server buffer';
        return;
      }
      if (!r.ok) throw new Error('http ' + r.status);
      const text = JSON.stringify(await r.json(), null, 2);
      logDetailCache.set(seq, text);
      if (logDetailCache.size > LOG_DETAIL_CACHE) {
        logDetailCache.delete(logDetailCache.keys().next().value);
      }
      pre.textContent = text;
    } catch (e) {
      pre.textContent = 'could not load detail: ' + ((e && e.message) || 'error');
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) syncLogPolling();
  });

  // Apply the stored open state without re-persisting it, then settle the
  // provider and fill the site dropdown before the first poll, so the opening
  // request already carries both filters and no cursor is thrown away.
  document.body.classList.toggle('logs-closed', !logsOpen);
  logsBtn.setAttribute('aria-pressed', String(logsOpen));
  renderLogList();
  loadProviders().then(loadServers).then(syncLogPolling);
  setInterval(loadServers, 60000);

  scheduleNext(0);
})();
</script>
</body>
</html>
"""

# Single source of truth for the version: stamp it into the dashboard footer once.
STATS_HTML = STATS_HTML.replace("__VERSION__", VERSION)


def _int_param(params, name, default):
    """One integer out of the query string, or the default. Anything unparseable
    is the default rather than an error: these are cursors and limits from a
    polling client, and a 500 on a stray value would stall the panel."""
    try:
        return int((params.get(name) or [""])[0])
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "Server-Vitals/" + VERSION
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
            elif path == "/logs":
                # Two shapes on one path: ?detail=<seq> pulls the full record for
                # a single entry, anything else is the cursor stream. `provider`
                # selects which log; omitted, it's the first one configured.
                provider = (params.get("provider") or [""])[0][:64]
                detail = _int_param(params, "detail", -1)
                if detail >= 0:
                    tailer = _logs.get(provider)
                    record = tailer.detail(detail) if tailer is not None else None
                    if record is None:
                        self._send_json({"error": "not found"}, code=404)
                    else:
                        self._send_json(record)
                else:
                    self._send_json(logs_payload(
                        provider=provider,
                        since=_int_param(params, "since", 0),
                        host=(params.get("host") or [""])[0][:253],
                        limit=_int_param(params, "limit", LOG_MAX_BATCH),
                    ))
            elif path == "/logs/providers":
                self._send_json({"providers": _logs.describe(),
                                 "default": _logs.default_id()})
            elif path == "/logs/servers":
                tailer = _logs.get((params.get("provider") or [""])[0][:64])
                if tailer is None:
                    self._send_json({"error": "unknown provider"}, code=404)
                else:
                    names, source = tailer.servers()
                    self._send_json({"provider": tailer.provider.id,
                                     "servers": names, "source": source})
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
    providers = _logs.describe()
    rows = [
        "Server Vitals — host monitoring agent",
        "backend: " + backend,
        "logs:    " + (", ".join("%s (%s)" % (p["id"], p["path"])
                                 for p in providers) or "none configured"),
        "",
        "dashboard  " + base + "/stats",
        "health     " + base + "/health",
        "json       " + base + "/stats?format=json",
        "logs       " + base + "/logs",
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
    _logs.start()
    threading.Thread(target=_public_ip_worker, name="public-ip", daemon=True).start()
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
