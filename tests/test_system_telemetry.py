import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import system_telemetry
import web_app
from computer_utilities import UtilitySettings
from system_telemetry import (
    CpuSampler,
    TelemetryService,
    TelemetryUnavailable,
    cpu_utilization,
    memory_used_bytes,
    parse_gpu_utilization,
    parse_vm_stat,
    thermal_reading,
)
from tool_registry import ToolRegistry
from voice import VoiceSettings

# Realistic vm_stat output from an Apple Silicon Mac (16 KB pages); the numbers are fixed
# test data, not readings from the machine running the tests.
VM_STAT_OUTPUT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    27109.
Pages active:                                 391811.
Pages inactive:                               391357.
Pages speculative:                               387.
Pages throttled:                                   0.
Pages wired down:                             133898.
Pages purgeable:                               23297.
"Translation faults":                      722083664.
Pages copy-on-write:                        21292101.
Pages zero filled:                        1092339384.
Pages reactivated:                         142791754.
Pages purged:                               26715533.
File-backed pages:                            170227.
Anonymous pages:                              613328.
Pages stored in compressor:                   503115.
Pages occupied by compressor:                  52077.
Decompressions:                            270229218.
Compressions:                              344958606.
Pageins:                                    74440421.
Pageouts:                                     407855.
Swapins:                                    41275238.
Swapouts:                                   53977637.
"""
TOTAL_BYTES = 17179869184
EXPECTED_USED = (613328 - 23297 + 133898 + 52077) * 16384

IOREG_OUTPUT = """+-o AGXAcceleratorG15G  <class AGXAcceleratorG15G, id 0x1000003a6, registered, matched, active>
    {
      "IOMatchedAtBoot" = Yes
      "PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=2804301824,"Tiler Utilization %"=41,"recoveryCount"=0,"Renderer Utilization %"=40,"Device Utilization %"=42,"In use system memory"=181501952}
      "model" = "Apple M3"
      "gpu-core-count" = 10
    }
"""


def completed(stdout="", returncode=0):
    return Mock(returncode=returncode, stdout=stdout)


class FakeTicks:
    """Returns queued CPU tick tuples (or raises queued exceptions)."""

    def __init__(self, *samples):
        self.samples = list(samples)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]
        if isinstance(value, Exception):
            raise value
        return value


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def service_with(readers, ticks=((0, 0, 0, 0),), clock=None, supported=True):
    clock = clock or FakeClock()
    sampler = CpuSampler(read_ticks=FakeTicks(*ticks), interval=3600, clock=clock)
    return TelemetryService(platform_check=lambda: supported, cpu=sampler, readers=readers, clock=clock)


def ok_readers():
    return {
        "memory": lambda: {"status": "ok", "used_bytes": 1, "total_bytes": 2, "percent": 50.0, "pressure": "normal"},
        "gpu": lambda: {"status": "ok", "percent": 0},
        "thermal": lambda: thermal_reading(0),
    }


class CpuUtilizationTests(unittest.TestCase):
    def test_utilization_counts_user_system_and_nice_as_busy(self):
        self.assertEqual(cpu_utilization((100, 50, 800, 10), (130, 60, 850, 20)), 50.0)

    def test_an_idle_interval_is_a_valid_zero(self):
        self.assertEqual(cpu_utilization((100, 50, 800, 10), (100, 50, 900, 10)), 0.0)

    def test_no_elapsed_ticks_gives_no_reading(self):
        self.assertIsNone(cpu_utilization((1, 2, 3, 4), (1, 2, 3, 4)))

    def test_32_bit_counter_wraparound_is_handled(self):
        top = 2**32
        self.assertEqual(cpu_utilization((top - 10, 0, top - 10, 0), (10, 0, 10, 0)), 50.0)

    def test_invalid_counters_are_rejected(self):
        for previous, current in [
            ((1, 2, 3), (1, 2, 3, 4)),
            ((1, 2, 3, 4), (1, 2, 3, 4, 5)),
            ((1, 2, 3, -1), (1, 2, 3, 4)),
            ((1, 2, 3, 4), (1, 2, 3, 2**32)),
            ((1, 2, 3, "4"), (1, 2, 3, 4)),
            ((1, 2, 3, True), (1, 2, 3, 4)),
            ((1, 2, 3, 4.0), (1, 2, 3, 4)),
        ]:
            with self.subTest(previous=previous, current=current), self.assertRaises(ValueError):
                cpu_utilization(previous, current)


class CpuSamplerTests(unittest.TestCase):
    def test_first_sample_is_reported_as_sampling_then_a_reading_follows(self):
        sampler = CpuSampler(read_ticks=FakeTicks((0, 0, 0, 0), (25, 0, 75, 0)), clock=FakeClock())
        self.assertEqual(sampler.sample_once()["status"], "sampling")
        self.assertEqual(sampler.sample_once(), {"status": "ok", "percent": 25.0})
        self.assertEqual(sampler.reading(), {"status": "ok", "percent": 25.0})

    def test_invalid_counters_are_unavailable_and_sampling_restarts(self):
        sampler = CpuSampler(
            read_ticks=FakeTicks((0, 0, 0, 0), (1, 2, 3), (10, 0, 10, 0), (20, 0, 20, 0)), clock=FakeClock()
        )
        sampler.sample_once()
        self.assertEqual(sampler.sample_once()["status"], "unavailable")
        self.assertEqual(sampler.sample_once()["status"], "sampling")  # needs a new first sample
        self.assertEqual(sampler.sample_once(), {"status": "ok", "percent": 50.0})

    def test_unreadable_counters_are_unavailable_with_a_safe_message(self):
        sampler = CpuSampler(read_ticks=FakeTicks(TelemetryUnavailable("CPU counters could not be read.")))
        self.assertEqual(sampler.sample_once(), {"status": "unavailable", "message": "CPU counters could not be read."})

    def test_a_stale_reading_never_averages_over_a_long_gap(self):
        clock = FakeClock()
        sampler = CpuSampler(read_ticks=FakeTicks((0, 0, 0, 0), (50, 0, 50, 0), (60, 0, 140, 0)), clock=clock)
        sampler.sample_once()
        sampler.sample_once()
        clock.now += system_telemetry.CPU_STALE_SECONDS + 1
        # The sampler stopped; the next reading starts fresh instead of using the old sample.
        self.assertEqual(sampler.reading()["status"], "sampling")

    def test_background_sampling_starts_once_and_stops_cleanly(self):
        ticks = FakeTicks((0, 0, 0, 0))
        sampler = CpuSampler(read_ticks=ticks, interval=0.01)
        sampler.start()
        sampler.start()
        time.sleep(0.1)
        sampler.stop()
        self.assertFalse(sampler._thread.is_alive())
        calls = ticks.calls
        time.sleep(0.05)
        self.assertEqual(ticks.calls, calls)
        self.assertGreater(calls, 1)

    def test_concurrent_readers_and_sampler_never_corrupt_the_state(self):
        counter = {"value": 0}
        lock = threading.Lock()

        def ticks():
            with lock:
                counter["value"] += 10
                return (counter["value"], 0, counter["value"], 0)

        sampler = CpuSampler(read_ticks=ticks, interval=0.001)
        sampler.start()
        results, errors = [], []

        def read():
            try:
                for _ in range(200):
                    results.append(sampler.reading())
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sampler.stop()
        self.assertEqual(errors, [])
        for result in results:
            self.assertIn(result["status"], ("ok", "sampling"))
            if result["status"] == "ok":
                self.assertEqual(result["percent"], 50.0)


class MemoryTests(unittest.TestCase):
    def test_vm_stat_is_parsed_with_its_page_size(self):
        page_size, counts = parse_vm_stat(VM_STAT_OUTPUT)
        self.assertEqual(page_size, 16384)
        self.assertEqual(counts["Anonymous pages"], 613328)
        self.assertEqual(counts["Pages occupied by compressor"], 52077)
        self.assertEqual(counts["Translation faults"], 722083664)

    def test_used_memory_is_app_plus_wired_plus_compressed(self):
        self.assertEqual(memory_used_bytes(*parse_vm_stat(VM_STAT_OUTPUT)), EXPECTED_USED)

    def test_missing_counters_or_page_size_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_vm_stat("Pages free: 1.\n")
        without_compressor = VM_STAT_OUTPUT.replace("Pages occupied by compressor", "Pages hidden")
        with self.assertRaises(ValueError):
            memory_used_bytes(*parse_vm_stat(without_compressor))

    def run_commands(self, memsize="17179869184\n", vm=VM_STAT_OUTPUT, pressure="1\n"):
        def run(executable, arguments, what):
            if arguments == ["-n", "hw.memsize"]:
                return memsize
            if arguments == ["-n", "kern.memorystatus_vm_pressure_level"]:
                if isinstance(pressure, Exception):
                    raise pressure
                return pressure
            return vm

        return patch("system_telemetry.run_fixed", side_effect=run)

    def test_memory_reading_reports_usage_and_pressure_separately(self):
        with self.run_commands():
            reading = system_telemetry.read_memory()
        self.assertEqual(reading["used_bytes"], EXPECTED_USED)
        self.assertEqual(reading["total_bytes"], TOTAL_BYTES)
        self.assertEqual(reading["percent"], round(EXPECTED_USED * 100 / TOTAL_BYTES, 1))
        self.assertEqual(reading["pressure"], "normal")

    def test_pressure_levels_map_and_unknown_or_failed_levels_are_null(self):
        for raw, expected in [("2\n", "warning"), ("4\n", "critical"), ("3\n", None), ("x\n", None),
                              (TelemetryUnavailable("Memory pressure could not be read."), None)]:
            with self.subTest(raw=raw), self.run_commands(pressure=raw):
                self.assertEqual(system_telemetry.read_memory()["pressure"], expected)

    def test_inconsistent_or_malformed_memory_is_unavailable(self):
        for kwargs in [{"memsize": "abc\n"}, {"memsize": "0\n"}, {"memsize": "1024\n"}, {"vm": "garbage"}]:
            with self.subTest(**kwargs), self.run_commands(**kwargs), self.assertRaises(TelemetryUnavailable):
                system_telemetry.read_memory()


class GpuTests(unittest.TestCase):
    def test_device_utilization_is_read_from_ioreg(self):
        self.assertEqual(parse_gpu_utilization(IOREG_OUTPUT), 42)

    def test_zero_utilization_is_a_valid_reading(self):
        self.assertEqual(parse_gpu_utilization('"Device Utilization %"=0'), 0)

    def test_missing_or_malformed_fields_are_rejected(self):
        for text in ["", IOREG_OUTPUT.replace("Device Utilization %", "Device Busy"), '"Device Utilization %"=abc',
                     '"Device Utilization %"=150', '"Device Utilization %"=-3', None]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_gpu_utilization(text)

    def test_unreported_gpu_utilization_is_unavailable(self):
        with patch("system_telemetry.run_fixed", return_value="no statistics here"):
            with self.assertRaises(TelemetryUnavailable) as raised:
                system_telemetry.read_gpu()
        self.assertEqual(str(raised.exception), "This Mac does not report GPU utilization.")

    def test_ioreg_is_called_with_a_fixed_path_and_arguments(self):
        with patch("system_telemetry.run_fixed", return_value=IOREG_OUTPUT) as run:
            self.assertEqual(system_telemetry.read_gpu(), {"status": "ok", "percent": 42})
        run.assert_called_once_with("/usr/sbin/ioreg", ["-r", "-d", "1", "-c", "IOAccelerator"], "GPU statistics")


class ThermalTests(unittest.TestCase):
    def test_states_map_to_pressure_levels_not_temperatures(self):
        for value, state in [(0, "nominal"), (1, "fair"), (2, "serious"), (3, "critical")]:
            with self.subTest(value=value):
                reading = thermal_reading(value)
                self.assertEqual(reading["state"], state)
                self.assertEqual(reading["label"], state.capitalize())
                self.assertIn("temperatures are not available", reading["note"])

    def test_unknown_states_are_unavailable(self):
        for value in (-1, 4, 99):
            with self.subTest(value=value), self.assertRaises(TelemetryUnavailable):
                thermal_reading(value)


class RunFixedTests(unittest.TestCase):
    def setUp(self):
        self.is_file = patch("system_telemetry.Path.is_file", return_value=True)
        self.is_file.start()
        self.addCleanup(self.is_file.stop)

    def test_commands_run_without_a_shell_and_with_a_timeout(self):
        with patch("system_telemetry.subprocess.run", return_value=completed("42\n")) as run:
            self.assertEqual(system_telemetry.run_fixed("/usr/sbin/sysctl", ["-n", "hw.memsize"], "Memory"), "42\n")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/sbin/sysctl", "-n", "hw.memsize"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], system_telemetry.COMMAND_TIMEOUT_SECONDS)

    def test_failures_become_safe_unavailable_messages(self):
        cases = [
            (subprocess.TimeoutExpired(["x"], 1.5), "GPU statistics took too long to respond."),
            (PermissionError("/secret/path denied"), "GPU statistics could not be read: permission denied."),
            (OSError("/secret/path broke"), "GPU statistics could not be read."),
            (completed("/secret/path error", returncode=1), "GPU statistics could not be read."),
        ]
        for outcome, message in cases:
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(message=message), patch("system_telemetry.subprocess.run", **kwargs):
                with self.assertRaises(TelemetryUnavailable) as raised:
                    system_telemetry.run_fixed("/usr/sbin/ioreg", [], "GPU statistics")
                self.assertEqual(str(raised.exception), message)
                self.assertNotIn("secret", str(raised.exception))

    def test_a_missing_program_is_unavailable_without_running_anything(self):
        self.is_file.stop()
        with patch("system_telemetry.Path.is_file", return_value=False), patch("system_telemetry.subprocess.run") as run:
            with self.assertRaises(TelemetryUnavailable) as raised:
                system_telemetry.run_fixed("/usr/sbin/ioreg", [], "GPU statistics")
        run.assert_not_called()
        self.assertEqual(str(raised.exception), "GPU statistics is not available on this Mac.")
        self.is_file.start()


class ServiceTests(unittest.TestCase):
    def test_each_metric_fails_independently(self):
        def broken():
            raise RuntimeError("/Users/secret/path exploded")

        def unavailable_gpu():
            raise TelemetryUnavailable("This Mac does not report GPU utilization.")

        readers = ok_readers()
        readers["memory"] = broken
        readers["gpu"] = unavailable_gpu
        service = service_with(readers)
        with self.assertLogs("system_telemetry", level="WARNING") as logs:
            metrics = service.snapshot()["metrics"]
        service.stop()
        self.assertEqual(metrics["memory"], {"status": "unavailable", "message": "This reading could not be taken."})
        self.assertEqual(metrics["gpu"]["status"], "unavailable")
        self.assertEqual(metrics["thermal"]["state"], "nominal")
        self.assertEqual(metrics["cpu"]["status"], "sampling")
        self.assertNotIn("secret", str(metrics))
        self.assertNotIn("secret", "\n".join(logs.output))  # only the exception type is logged

    def test_readings_are_cached_for_about_two_seconds(self):
        clock = FakeClock()
        gpu = Mock(return_value={"status": "ok", "percent": 5})
        readers = ok_readers()
        readers["gpu"] = gpu
        service = service_with(readers, clock=clock)
        service.snapshot()
        clock.now += 1.9
        service.snapshot()
        self.assertEqual(gpu.call_count, 1)
        clock.now += 0.2
        service.snapshot()
        self.assertEqual(gpu.call_count, 2)
        service.stop()

    def test_concurrent_requests_share_one_slow_read(self):
        calls = []

        def slow_gpu():
            calls.append(1)
            time.sleep(0.1)
            return {"status": "ok", "percent": 7}

        readers = ok_readers()
        readers["gpu"] = slow_gpu
        service = service_with(readers, clock=time.monotonic)
        threads = [threading.Thread(target=service.snapshot) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        service.stop()
        self.assertEqual(len(calls), 1)

    def test_cpu_reading_appears_after_the_first_sample(self):
        service = service_with(ok_readers(), ticks=((0, 0, 0, 0), (40, 0, 60, 0)))
        self.assertEqual(service.snapshot()["metrics"]["cpu"]["status"], "sampling")
        service.cpu.sample_once()  # what the background sampler does every two seconds
        self.assertEqual(service.snapshot()["metrics"]["cpu"], {"status": "ok", "percent": 40.0})
        service.stop()

    def test_other_platforms_report_every_metric_unavailable_without_sampling(self):
        ticks = FakeTicks((0, 0, 0, 0))
        sampler = CpuSampler(read_ticks=ticks)
        service = TelemetryService(platform_check=lambda: False, cpu=sampler, readers=ok_readers())
        snapshot = service.snapshot()
        self.assertFalse(snapshot["platform_supported"])
        for name in ("cpu", "memory", "gpu", "thermal"):
            self.assertEqual(snapshot["metrics"][name]["status"], "unavailable")
        self.assertEqual(ticks.calls, 0)
        self.assertIsNone(sampler._thread)


class TelemetryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = service_with(ok_readers())
        self.client = web_app.create_app(
            Path(self.temp_dir.name) / "telemetry.sqlite3",
            utility_settings=UtilitySettings(),
            voice_settings=VoiceSettings(),
            telemetry=self.service,
        ).test_client()

    def tearDown(self):
        self.service.stop()
        self.temp_dir.cleanup()

    def test_response_has_the_documented_shape(self):
        response = self.client.get("/api/system/telemetry", headers={"Host": "127.0.0.1:5000"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.get_json()
        self.assertTrue(body["platform_supported"])
        self.assertEqual(body["refresh_seconds"], system_telemetry.CACHE_SECONDS)
        self.assertEqual(set(body["metrics"]), {"cpu", "memory", "gpu", "thermal"})
        self.assertEqual(body["metrics"]["gpu"], {"status": "ok", "percent": 0})
        for metric in body["metrics"].values():
            self.assertIn(metric["status"], ("ok", "sampling", "unavailable"))

    def test_the_route_is_read_only(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/api/system/telemetry", headers={"Host": "127.0.0.1:5000"})
                self.assertEqual(response.status_code, 405)

    def test_untrusted_hosts_cannot_read_telemetry(self):
        for host in ("attacker.example:5000", "192.168.1.20:5000", "[::1]:5000"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/system/telemetry", headers={"Host": host}).status_code, 400)

    def test_a_failing_source_still_returns_a_complete_response(self):
        def broken():
            raise TelemetryUnavailable("GPU statistics took too long to respond.")

        self.service._readers["gpu"] = broken
        with patch.object(self.service, "_cache", {}):
            body = self.client.get("/api/system/telemetry", headers={"Host": "localhost"}).get_json()
        self.assertEqual(body["metrics"]["gpu"], {"status": "unavailable", "message": "GPU statistics took too long to respond."})
        self.assertEqual(body["metrics"]["thermal"]["status"], "ok")


class SystemInfoToolTests(unittest.TestCase):
    def test_get_system_info_reports_the_same_telemetry(self):
        service = service_with(ok_readers())
        registry = ToolRegistry(telemetry=service)
        outcome = registry.execute(registry.prepare("get_system_info", {}, "How busy is my Mac?"))
        service.stop()
        self.assertTrue(outcome.ok)
        self.assertEqual(set(outcome.data["telemetry"]), {"cpu", "memory", "gpu", "thermal"})
        self.assertEqual(outcome.data["telemetry"]["thermal"]["state"], "nominal")
        for key in ("local_datetime", "timezone", "battery", "memory", "disk"):
            self.assertIn(key, outcome.data)
        self.assertFalse(registry._definitions["get_system_info"].always_confirm)


if __name__ == "__main__":
    unittest.main()
