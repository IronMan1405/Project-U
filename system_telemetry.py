"""Read-only macOS system telemetry for the System panel and the get_system_info tool.

Standard library only, no administrator rights, no shell. Each metric is read on its own,
so one failing source never hides the others, and a failure is reported as unavailable,
never as a zero reading.

Metrics
- cpu:     utilization over the interval between two samples of the kernel's CPU tick
           counters (host_statistics HOST_CPU_LOAD_INFO via ctypes). A background thread
           samples every CACHE_SECONDS, so requests never wait for a sample. Until two
           samples exist the status is "sampling".
- memory:  used / total physical memory. used = (anonymous - purgeable + wired + compressed)
           pages from vm_stat, the same parts Activity Monitor adds up as "Memory Used"
           (App Memory + Wired Memory + Compressed). This is how much memory is in use, not
           how much is under strain: memory *pressure* (normal, warning, critical) comes
           separately from kern.memorystatus_vm_pressure_level and is null if unreadable.
- gpu:     "Device Utilization %" from `ioreg -c IOAccelerator` PerformanceStatistics.
           Best effort: Apple does not document this field and may change it.
- thermal: macOS's thermal state (NSProcessInfo.thermalState): nominal, fair, serious or
           critical. This is a pressure level; temperatures in degrees are not available
           without administrator rights or private interfaces.

Response shape of TelemetryService.snapshot() (also GET /api/system/telemetry):
    {
      "platform_supported": bool,
      "refresh_seconds": float,
      "metrics": {
        "cpu":     {"status": "ok", "percent": float}
                 | {"status": "sampling", "message": str},
        "memory":  {"status": "ok", "used_bytes": int, "total_bytes": int,
                    "percent": float, "pressure": "normal" | "warning" | "critical" | null},
        "gpu":     {"status": "ok", "percent": int},
        "thermal": {"status": "ok", "state": "nominal" | "fair" | "serious" | "critical",
                    "label": str, "note": str},
      }
    }
Any metric may instead be {"status": "unavailable", "message": str}; messages are fixed,
display-safe text and never contain command output or exception details.
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

SYSCTL = "/usr/sbin/sysctl"
VM_STAT = "/usr/bin/vm_stat"
IOREG = "/usr/sbin/ioreg"
LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
LIBOBJC = "/usr/lib/libobjc.A.dylib"
FOUNDATION = "/System/Library/Frameworks/Foundation.framework/Foundation"

COMMAND_TIMEOUT_SECONDS = 1.5
CACHE_SECONDS = 2.0
# A CPU reading older than this means the sampler has stopped; take a fresh sample instead.
CPU_STALE_SECONDS = CACHE_SECONDS * 3

HOST_CPU_LOAD_INFO = 3
CPU_STATE_MAX = 4  # user, system, idle, nice
COUNTER_MODULUS = 2**32  # the tick counters are 32-bit and wrap

MEMORY_PRESSURE_LEVELS = {1: "normal", 2: "warning", 4: "critical"}
THERMAL_STATES = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}
THERMAL_NOTE = "macOS thermal pressure level; temperatures are not available."
METRIC_NAMES = ("cpu", "memory", "gpu", "thermal")


class TelemetryUnavailable(Exception):
    """A metric could not be read. The message is shown to the user, so keep it generic."""


def is_macos() -> bool:
    return Path("/System/Library/CoreServices/SystemVersion.plist").exists()


def unavailable(message: str) -> dict:
    return {"status": "unavailable", "message": message}


def run_fixed(executable: str, arguments: list[str], what: str) -> str:
    """Run a fixed executable (never a shell) and return its output, or raise a safe error."""
    if not Path(executable).is_file():
        raise TelemetryUnavailable(f"{what} is not available on this Mac.")
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TelemetryUnavailable(f"{what} took too long to respond.") from None
    except PermissionError:
        raise TelemetryUnavailable(f"{what} could not be read: permission denied.") from None
    except OSError:
        raise TelemetryUnavailable(f"{what} could not be read.") from None
    if result.returncode != 0:
        raise TelemetryUnavailable(f"{what} could not be read.")
    return result.stdout


# --- CPU -------------------------------------------------------------------------------

_host_port: int | None = None
_host_lock = threading.Lock()


def read_cpu_ticks() -> tuple[int, int, int, int]:
    """Cumulative (user, system, idle, nice) CPU ticks across all cores."""
    global _host_port
    try:
        libsystem = ctypes.CDLL(LIBSYSTEM)
        with _host_lock:
            if _host_port is None:
                # One send right for the life of the process, so repeated calls don't leak.
                libsystem.mach_host_self.restype = ctypes.c_uint32
                libsystem.mach_host_self.argtypes = []
                _host_port = libsystem.mach_host_self()
            port = _host_port
        host_statistics = libsystem.host_statistics
        host_statistics.restype = ctypes.c_int
        host_statistics.argtypes = [
            ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)
        ]
        info = (ctypes.c_uint32 * CPU_STATE_MAX)()
        count = ctypes.c_uint32(CPU_STATE_MAX)
        status = host_statistics(port, HOST_CPU_LOAD_INFO, info, ctypes.byref(count))
    except (OSError, AttributeError):
        raise TelemetryUnavailable("CPU counters are not available.") from None
    if status != 0 or count.value != CPU_STATE_MAX:
        raise TelemetryUnavailable("CPU counters could not be read.")
    return tuple(int(value) for value in info)


def cpu_utilization(previous: tuple, current: tuple) -> float | None:
    """Percent of ticks spent busy between two samples; None if no ticks elapsed.

    Raises ValueError for malformed counters.
    """
    if len(previous) != CPU_STATE_MAX or len(current) != CPU_STATE_MAX:
        raise ValueError("CPU counters must have four values")
    for value in (*previous, *current):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < COUNTER_MODULUS:
            raise ValueError("CPU counters must be 32-bit unsigned integers")
    user, system, idle, nice = ((now - before) % COUNTER_MODULUS for before, now in zip(previous, current))
    total = user + system + idle + nice
    if total == 0:
        return None
    return round((user + system + nice) * 100 / total, 1)


class CpuSampler:
    """Keeps the latest CPU utilization, sampled every `interval` seconds in the background."""

    def __init__(
        self,
        read_ticks: Callable[[], tuple] = read_cpu_ticks,
        interval: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._read_ticks = read_ticks
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._previous: tuple | None = None
        self._latest: dict = {"status": "sampling", "message": "Measuring CPU use."}
        self._latest_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> dict:
        """Take one sample and update the latest reading. Cheap and non-blocking."""
        with self._lock:
            try:
                current = self._read_ticks()
                if self._previous is None:
                    reading = {"status": "sampling", "message": "Measuring CPU use."}
                else:
                    percent = cpu_utilization(self._previous, current)
                    reading = (
                        {"status": "sampling", "message": "Measuring CPU use."}
                        if percent is None
                        else {"status": "ok", "percent": percent}
                    )
                self._previous = current
            except TelemetryUnavailable as error:
                reading = unavailable(str(error))
            except (ValueError, TypeError):
                self._previous = None  # start again from the next valid sample
                reading = unavailable("CPU counters were not valid.")
            self._latest = reading
            self._latest_at = self._clock()
            return dict(reading)

    def reading(self) -> dict:
        with self._lock:
            if self._latest_at is not None and self._clock() - self._latest_at < CPU_STALE_SECONDS:
                return dict(self._latest)
            # No recent sample (the sampler is not running): never average over a long gap.
            self._previous = None
        return self.sample_once()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="kris-cpu-sampler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        # The caller has just sampled; each later sample measures the interval since the last.
        while not self._stop.wait(self._interval):
            self.sample_once()


# --- Memory ----------------------------------------------------------------------------

_VM_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_VM_LINE = re.compile(r'^"?([A-Za-z][^":]*)"?:\s+(\d+)\.?\s*$')


def parse_vm_stat(text: str) -> tuple[int, dict[str, int]]:
    """Return (page size, {label: page count}) from vm_stat output."""
    match = _VM_PAGE_SIZE.search(text or "")
    if not match or int(match.group(1)) <= 0:
        raise ValueError("vm_stat did not report its page size")
    counts = {}
    for line in text.splitlines()[1:]:
        found = _VM_LINE.match(line.strip())
        if found:
            counts[found.group(1).strip()] = int(found.group(2))
    return int(match.group(1)), counts


def memory_used_bytes(page_size: int, counts: dict[str, int]) -> int:
    """App memory (anonymous - purgeable) + wired + compressed, in bytes."""
    try:
        anonymous = counts["Anonymous pages"]
        wired = counts["Pages wired down"]
        compressed = counts["Pages occupied by compressor"]
    except KeyError:
        raise ValueError("vm_stat did not report the memory counters K.R.I.S. needs") from None
    purgeable = counts.get("Pages purgeable", 0)
    app_pages = max(anonymous - purgeable, 0)
    return (app_pages + wired + compressed) * page_size


def read_memory() -> dict:
    try:
        total = int(run_fixed(SYSCTL, ["-n", "hw.memsize"], "Total memory").strip())
    except ValueError:
        raise TelemetryUnavailable("Total memory was not reported correctly.") from None
    if total <= 0:
        raise TelemetryUnavailable("Total memory was not reported correctly.")
    output = run_fixed(VM_STAT, [], "Memory statistics")
    try:
        page_size, counts = parse_vm_stat(output)
        used = memory_used_bytes(page_size, counts)
    except ValueError:
        raise TelemetryUnavailable("Memory statistics were not in the expected format.") from None
    if used > total:
        raise TelemetryUnavailable("Memory statistics were inconsistent.")
    return {
        "status": "ok",
        "used_bytes": used,
        "total_bytes": total,
        "percent": round(used * 100 / total, 1),
        "pressure": read_memory_pressure(),
    }


def read_memory_pressure() -> str | None:
    """normal, warning or critical; None if macOS does not report a known level."""
    try:
        level = int(run_fixed(SYSCTL, ["-n", "kern.memorystatus_vm_pressure_level"], "Memory pressure").strip())
    except (TelemetryUnavailable, ValueError):
        return None
    return MEMORY_PRESSURE_LEVELS.get(level)


# --- GPU -------------------------------------------------------------------------------

_GPU_UTILIZATION = re.compile(r'"Device Utilization %"\s*=\s*(-?\d+)')


def parse_gpu_utilization(text: str) -> int:
    """The first valid 0-100 "Device Utilization %" in ioreg output."""
    for match in _GPU_UTILIZATION.finditer(text or ""):
        value = int(match.group(1))
        if 0 <= value <= 100:
            return value
    raise ValueError("no GPU utilization field")


def read_gpu() -> dict:
    output = run_fixed(IOREG, ["-r", "-d", "1", "-c", "IOAccelerator"], "GPU statistics")
    try:
        return {"status": "ok", "percent": parse_gpu_utilization(output)}
    except ValueError:
        raise TelemetryUnavailable("This Mac does not report GPU utilization.") from None


# --- Thermal pressure ------------------------------------------------------------------


def read_thermal_state_value() -> int:
    """NSProcessInfo.processInfo.thermalState through the Objective-C runtime (ctypes)."""
    try:
        objc = ctypes.CDLL(LIBOBJC)
        ctypes.CDLL(FOUNDATION)  # registers NSProcessInfo
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        # Separate typed prototypes, so nothing shared is mutated between threads.
        send_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
        send_integer = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(address)
        process_info_class = objc.objc_getClass(b"NSProcessInfo")
        if not process_info_class:
            raise TelemetryUnavailable("The thermal state is not available.")
        process_info = send_object(process_info_class, objc.sel_registerName(b"processInfo"))
        if not process_info:
            raise TelemetryUnavailable("The thermal state is not available.")
        return int(send_integer(process_info, objc.sel_registerName(b"thermalState")))
    except (OSError, AttributeError):
        raise TelemetryUnavailable("The thermal state is not available.") from None


def thermal_reading(value: int) -> dict:
    state = THERMAL_STATES.get(value)
    if state is None:
        raise TelemetryUnavailable("macOS reported an unrecognised thermal state.")
    return {"status": "ok", "state": state, "label": state.capitalize(), "note": THERMAL_NOTE}


def read_thermal() -> dict:
    return thermal_reading(read_thermal_state_value())


# --- Service ---------------------------------------------------------------------------


class TelemetryService:
    """Caches each metric for `cache_seconds` and isolates failures between metrics."""

    def __init__(
        self,
        *,
        platform_check: Callable[[], bool] = is_macos,
        cpu: CpuSampler | None = None,
        readers: dict[str, Callable[[], dict]] | None = None,
        cache_seconds: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.supported = bool(platform_check())
        self.cpu = cpu or CpuSampler(clock=clock)
        self._readers = readers or {"memory": read_memory, "gpu": read_gpu, "thermal": read_thermal}
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks = {name: threading.Lock() for name in self._readers}
        self._started = False
        self._start_lock = threading.Lock()

    def start(self) -> None:
        """Start background CPU sampling (once). Safe to call repeatedly."""
        if not self.supported:
            return
        with self._start_lock:
            if self._started:
                return
            self._started = True
        self.cpu.sample_once()  # first sample now, so the next one gives a reading
        self.cpu.start()

    def stop(self) -> None:
        self.cpu.stop()

    def snapshot(self) -> dict:
        if not self.supported:
            message = "System telemetry is only available on macOS."
            return {
                "platform_supported": False,
                "refresh_seconds": self._cache_seconds,
                "metrics": {name: unavailable(message) for name in METRIC_NAMES},
            }
        self.start()
        metrics = {"cpu": self.cpu.reading()}
        for name in ("memory", "gpu", "thermal"):
            metrics[name] = self._cached(name)
        return {"platform_supported": True, "refresh_seconds": self._cache_seconds, "metrics": metrics}

    def _cached(self, name: str) -> dict:
        # One reader per metric at a time; concurrent requests share the result.
        with self._locks[name]:
            cached = self._cache.get(name)
            if cached is not None and self._clock() - cached[0] < self._cache_seconds:
                return dict(cached[1])
            try:
                value = self._readers[name]()
            except TelemetryUnavailable as error:
                value = unavailable(str(error))
            except Exception as error:  # a broken reader must not take the others down
                logger.warning("System telemetry: the %s reader failed (%s).", name, type(error).__name__)
                value = unavailable("This reading could not be taken.")
            self._cache[name] = (self._clock(), value)
            return dict(value)


_shared_service: TelemetryService | None = None
_shared_lock = threading.Lock()


def shared_service() -> TelemetryService:
    """The process-wide service, so the web UI and get_system_info share one CPU sampler."""
    global _shared_service
    with _shared_lock:
        if _shared_service is None:
            _shared_service = TelemetryService()
        return _shared_service
