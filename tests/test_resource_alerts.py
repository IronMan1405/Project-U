"""Resource alerts: hysteresis, sustained crossings, independent resources, delivery, lifecycle.

Telemetry and notifications are fakes; nothing depends on this Mac's real usage.
"""
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import web_app
from resource_alerts import ResourceAlertSettings, ResourceMonitor, build_resource_alert_settings


def snapshot(cpu=10.0, memory=50.0, gpu=5, thermal="nominal"):
    metric = lambda value: {"status": "ok", "percent": value} if value is not None else {"status": "unavailable", "message": "x"}
    return {"metrics": {
        "cpu": metric(cpu), "memory": metric(memory), "gpu": metric(gpu),
        "thermal": {"status": "ok", "state": thermal} if thermal else {"status": "unavailable", "message": "x"},
    }}


class Feed:
    """Returns the queued snapshots in order, repeating the last one."""

    def __init__(self, *samples):
        self.samples = list(samples)

    def __call__(self):
        return self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]


def monitor(*samples, **settings):
    notifier = Mock(return_value=True)
    base = {"sustained_samples": 2, **settings}
    return ResourceMonitor(Feed(*samples), ResourceAlertSettings(**base), notifier=notifier), notifier


def run(monitor_, times):
    return [alert for _ in range(times) for alert in monitor_.check()]


class HysteresisTests(unittest.TestCase):
    def test_a_sustained_crossing_alerts_once_and_stays_quiet_while_high(self):
        m, notify = monitor(snapshot(memory=92), snapshot(memory=93), snapshot(memory=95), snapshot(memory=91),
                            snapshot(memory=85))
        alerts = run(m, 5)
        self.assertEqual([a["title"] for a in alerts], ["High memory usage — 93%"])
        notify.assert_called_once_with("K.R.I.S.", "High memory usage — 93%")
        self.assertEqual(m.status()["high"], ["memory"])  # 85 is above the 80 recovery level

    def test_one_noisy_sample_does_not_alert(self):
        m, notify = monitor(snapshot(cpu=99), snapshot(cpu=20), snapshot(cpu=99), snapshot(cpu=20))
        self.assertEqual(run(m, 4), [])
        notify.assert_not_called()

    def test_recovery_resets_and_a_later_crossing_alerts_again(self):
        m, notify = monitor(snapshot(cpu=95), snapshot(cpu=96), snapshot(cpu=79), snapshot(cpu=92), snapshot(cpu=94))
        alerts = run(m, 5)
        self.assertEqual([a["title"] for a in alerts], ["High CPU usage — 96%", "High CPU usage — 94%"])
        self.assertEqual(notify.call_count, 2)
        self.assertEqual([a["id"] for a in m.status()["recent"]], [1, 2])

    def test_resources_are_independent(self):
        m, notify = monitor(snapshot(cpu=95, memory=95), snapshot(cpu=95, memory=95), snapshot(cpu=20, memory=95),
                            snapshot(cpu=95, memory=95, gpu=97), snapshot(cpu=95, memory=95, gpu=97))
        alerts = run(m, 5)
        # CPU recovered and crossed again (a new alert); memory stayed high throughout (one alert).
        self.assertEqual([a["resource"] for a in alerts], ["cpu", "memory", "cpu", "gpu"])
        self.assertEqual(sorted(m.status()["high"]), ["cpu", "gpu", "memory"])

    def test_missing_gpu_never_alerts_and_unavailable_readings_do_not_clear_an_alert(self):
        m, _ = monitor(snapshot(gpu=None), snapshot(gpu=None), snapshot(gpu=None))
        self.assertEqual(run(m, 3), [])
        m, _ = monitor(snapshot(memory=95), snapshot(memory=95), snapshot(memory=None), snapshot(memory=95))
        self.assertEqual(len(run(m, 4)), 1)  # the gap neither recovered nor re-alerted
        m, _ = monitor(snapshot(memory=95), snapshot(memory=None), snapshot(memory=95))
        self.assertEqual(run(m, 3), [])  # an unavailable sample breaks the streak

    def test_telemetry_failures_are_survived(self):
        broken = ResourceMonitor(Mock(side_effect=RuntimeError("boom")), ResourceAlertSettings())
        with self.assertLogs("resource_alerts", level="WARNING"):
            self.assertEqual(broken.check(), [])
        self.assertEqual(ResourceMonitor(lambda: {}, ResourceAlertSettings()).check(), [])

    def test_thermal_pressure_uses_macos_levels(self):
        m, _ = monitor(snapshot(thermal="fair"), snapshot(thermal="serious"), snapshot(thermal="critical"),
                       snapshot(thermal="serious"), snapshot(thermal="fair"), snapshot(thermal="serious"),
                       snapshot(thermal="serious"))
        alerts = run(m, 7)
        self.assertEqual([a["title"] for a in alerts], ["High thermal pressure detected (critical)",
                                                        "High thermal pressure detected (serious)"])
        self.assertEqual(alerts[0]["spoken"], "The Mac reports critical thermal pressure.")
        m, _ = monitor(snapshot(thermal="fair"), snapshot(thermal="fair"), thermal_warning="fair")
        self.assertEqual(len(run(m, 2)), 1)
        m, _ = monitor(snapshot(thermal=None), snapshot(thermal="mystery"))
        self.assertEqual(run(m, 2), [])

    def test_notifications_can_be_switched_off_and_speech_is_only_flagged_for_the_page(self):
        m, notify = monitor(snapshot(cpu=95), snapshot(cpu=95), notifications=False)
        self.assertEqual(len(run(m, 2)), 1)
        notify.assert_not_called()
        self.assertFalse(m.status()["speech"])
        spoken, _ = monitor(snapshot(cpu=95), snapshot(cpu=95), speech=True)
        run(spoken, 2)
        self.assertTrue(spoken.status()["speech"])
        self.assertEqual(spoken.status()["recent"][0]["spoken"], "CPU usage is high at 95 percent.")
        failing = ResourceMonitor(Feed(snapshot(cpu=95)), ResourceAlertSettings(sustained_samples=1),
                                  notifier=Mock(side_effect=OSError))
        with self.assertLogs("resource_alerts", level="WARNING"):
            self.assertEqual(len(failing.check()), 1)  # a failed notification is still an alert

    def test_settings_come_from_config_with_safe_fallbacks(self):
        settings = build_resource_alert_settings({"enabled": "off", "speech": "on", "sample_interval_seconds": "1",
                                                  "cpu_warning": "75", "memory_warning": "x", "thermal_warning": "fair"})
        self.assertFalse(settings.enabled)
        self.assertTrue(settings.speech)
        self.assertEqual(settings.sample_interval, 2)  # clamped
        self.assertEqual(settings.cpu_warning, 75)
        self.assertEqual(settings.recovery("cpu"), 65)
        with self.assertLogs("resource_alerts", level="WARNING"):
            self.assertEqual(build_resource_alert_settings({"memory_warning": "x", "thermal_warning": "hot"}).memory_warning, 90)
        self.assertEqual(build_resource_alert_settings({}), ResourceAlertSettings())


class LifecycleTests(unittest.TestCase):
    def test_one_thread_runs_and_stops_cleanly(self):
        checked = threading.Event()
        feed = Mock(side_effect=lambda: (checked.set(), snapshot())[1])
        m = ResourceMonitor(feed, ResourceAlertSettings(sample_interval=0.01))  # config clamps; tests need speed
        self.assertTrue(m.start())
        self.assertFalse(m.start())  # no second thread
        self.assertTrue(checked.wait(2))
        self.assertEqual(sum(t.name == "kris-resource-alerts" for t in threading.enumerate()), 1)
        started = time.monotonic()
        m.stop()
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(m.running)
        self.assertTrue(m._thread.daemon)  # never keeps the process alive

    def test_disabled_monitor_never_starts(self):
        m = ResourceMonitor(Mock(), ResourceAlertSettings(enabled=False))
        self.assertFalse(m.start())
        self.assertFalse(m.running)
        m.stop()  # harmless

    def test_web_app_does_not_start_the_thread_and_reports_status(self):
        m, _ = monitor(snapshot(memory=95), snapshot(memory=95))
        run(m, 2)
        database = tempfile.TemporaryDirectory()
        self.addCleanup(database.cleanup)
        client = web_app.create_app(f"{database.name}/test.sqlite3", resource_monitor=m, ui_settings={}).test_client()
        response = client.get("/api/resource-alerts")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = response.get_json()
        self.assertEqual(data["high"], ["memory"])
        self.assertEqual(data["recent"][0]["title"], "High memory usage — 95%")
        self.assertFalse(m.running)


if __name__ == "__main__":
    unittest.main()
