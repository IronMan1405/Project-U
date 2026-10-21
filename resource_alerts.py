"""Resource alerts: one notification when CPU, memory, GPU or thermal pressure stays high.

A background thread reads the shared system telemetry (system_telemetry.py; nothing is
measured twice) every `sample_interval` seconds. Each resource has its own state:

    normal --(at or above the warning level for `sustained_samples` samples in a row)--> high
           (one alert: a macOS notification, and a spoken line queued for the page if enabled)
    high   --(stays high, however long)--> high, quietly
    high   --(below the recovery level)--> normal, so the next sustained crossing alerts again

Percentages recover below `warning - recovery_margin`. Thermal pressure uses macOS's own
levels (nominal, fair, serious, critical) and recovers below the warning level. A reading
that is unavailable (for example GPU utilization on a Mac that does not report it) never
alerts, never counts towards a crossing, and never clears an alert that is already active.
Alerts state only the measured value, never a cause K.R.I.S. cannot know.

Spoken alerts are not spoken here: the page polls GET /api/resource-alerts and speaks a new
one through its own speech pipeline, only when it is not listening, speaking or busy.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

THERMAL_LEVELS = ("nominal", "fair", "serious", "critical")
PERCENT_RESOURCES = {"cpu": "CPU", "memory": "memory", "gpu": "GPU"}
MAX_RECENT_ALERTS = 20
INTERVAL_RANGE = (2, 300)
WARNING_RANGE = (50, 100)
SUSTAINED_RANGE = (1, 30)


@dataclass(frozen=True)
class ResourceAlertSettings:
    enabled: bool = True
    notifications: bool = True
    speech: bool = False
    sample_interval: int = 10
    cpu_warning: int = 90
    memory_warning: int = 90
    gpu_warning: int = 90
    thermal_warning: str = "serious"
    recovery_margin: int = 10
    sustained_samples: int = 3  # with the default interval: 30 seconds above the warning level

    def warning(self, resource: str) -> float:
        return getattr(self, f"{resource}_warning")

    def recovery(self, resource: str) -> float:
        return self.warning(resource) - self.recovery_margin


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    return min(max(value, bounds[0]), bounds[1])


def build_resource_alert_settings(values: dict[str, str]) -> ResourceAlertSettings:
    """From the [resource_alerts] config section; invalid values fall back to the defaults."""
    defaults = ResourceAlertSettings()

    def flag(name: str) -> bool:
        raw = str(values.get(name, getattr(defaults, name))).strip().casefold()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        logger.warning("Ignoring resource_alerts %s=%r: use on or off", name, raw)
        return getattr(defaults, name)

    def number(name: str, bounds: tuple[int, int], key: str | None = None) -> int:
        key = key or name
        try:
            return _clamp(int(values.get(key, getattr(defaults, name))), bounds)
        except (TypeError, ValueError):
            logger.warning("Ignoring resource_alerts %s: not a whole number", key)
            return getattr(defaults, name)

    thermal = str(values.get("thermal_warning", defaults.thermal_warning)).strip().casefold()
    if thermal not in THERMAL_LEVELS[1:]:
        logger.warning("Ignoring resource_alerts thermal_warning=%r: use fair, serious or critical", thermal)
        thermal = defaults.thermal_warning
    return ResourceAlertSettings(
        enabled=flag("enabled"),
        notifications=flag("notifications"),
        speech=flag("speech"),
        sample_interval=number("sample_interval", INTERVAL_RANGE, "sample_interval_seconds"),
        cpu_warning=number("cpu_warning", WARNING_RANGE),
        memory_warning=number("memory_warning", WARNING_RANGE),
        gpu_warning=number("gpu_warning", WARNING_RANGE),
        thermal_warning=thermal,
        recovery_margin=number("recovery_margin", (1, 40)),
        sustained_samples=number("sustained_samples", SUSTAINED_RANGE),
    )


class ResourceMonitor:
    def __init__(
        self,
        snapshot: Callable[[], dict],
        settings: ResourceAlertSettings | None = None,
        notifier: Callable[[str, str], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.snapshot = snapshot
        self.settings = settings or ResourceAlertSettings()
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self._high = {name: False for name in (*PERCENT_RESOURCES, "thermal")}
        self._streak = {name: 0 for name in self._high}
        self._recent: deque[dict] = deque(maxlen=MAX_RECENT_ALERTS)
        self._next_id = 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Evaluation ---------------------------------------------------------------------

    def check(self) -> list[dict]:
        """Read telemetry once and update every resource; returns the alerts raised now."""
        try:
            metrics = (self.snapshot() or {}).get("metrics") or {}
        except Exception as error:  # telemetry trouble must never stop the monitor
            logger.warning("Resource alerts: telemetry could not be read (%s).", type(error).__name__)
            return []
        raised = []
        with self._lock:
            for name, label in PERCENT_RESOURCES.items():
                reading = metrics.get(name) or {}
                value = reading.get("percent") if reading.get("status") == "ok" else None
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    self._streak[name] = 0  # unavailable: no new crossing, no recovery
                    continue
                alert = self._step(name, value >= self.settings.warning(name), value < self.settings.recovery(name))
                if alert:
                    shown = round(value)
                    raised.append(self._alert(name, f"High {label} usage — {shown}%",
                                              f"{label[0].upper() + label[1:]} usage is high at {shown} percent.", shown))
            reading = metrics.get("thermal") or {}
            state = reading.get("state") if reading.get("status") == "ok" else None
            if state in THERMAL_LEVELS:
                level, warning = THERMAL_LEVELS.index(state), THERMAL_LEVELS.index(self.settings.thermal_warning)
                if self._step("thermal", level >= warning, level < warning):
                    raised.append(self._alert("thermal", f"High thermal pressure detected ({state})",
                                              f"The Mac reports {state} thermal pressure.", state))
            else:
                self._streak["thermal"] = 0
        for alert in raised:
            self._deliver(alert)
        return raised

    def _step(self, name: str, above: bool, recovered: bool) -> bool:
        """Advance one resource; True when it has just become high (alert once)."""
        if self._high[name]:
            if recovered:
                self._high[name] = False
                self._streak[name] = 0
            return False
        self._streak[name] = self._streak[name] + 1 if above else 0
        if self._streak[name] >= self.settings.sustained_samples:
            self._high[name] = True
            return True
        return False

    def _alert(self, resource: str, title: str, spoken: str, value) -> dict:
        alert = {"id": self._next_id, "resource": resource, "title": title, "spoken": spoken, "value": value,
                 "at": self.clock().isoformat(timespec="seconds")}
        self._next_id += 1
        self._recent.append(alert)
        return alert

    def _deliver(self, alert: dict) -> None:
        logger.info("Resource alert: %s", alert["title"])
        if self.settings.notifications and self.notifier is not None:
            try:
                self.notifier("K.R.I.S.", alert["title"])
            except Exception as error:
                logger.warning("Resource alerts: the notification failed (%s).", type(error).__name__)

    def status(self) -> dict:
        """For the page: which resources are high, recent alerts, and whether to speak them."""
        with self._lock:
            return {
                "enabled": self.settings.enabled,
                "speech": self.settings.speech,
                "high": [name for name, high in self._high.items() if high],
                "recent": list(self._recent),
            }

    # --- Lifecycle ----------------------------------------------------------------------

    def start(self) -> bool:
        """Start the background thread once; later calls (or a disabled monitor) do nothing."""
        if not self.settings.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="kris-resource-alerts", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self.settings.sample_interval):
            started = time.monotonic()
            self.check()
            spent = time.monotonic() - started
            if spent > self.settings.sample_interval / 2:
                logger.warning("Resource alerts: one check took %.1f s.", spent)
