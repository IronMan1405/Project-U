// System panel telemetry: turns one metric from GET /api/system/telemetry into the text,
// tone and tooltip shown in the System box. DOM-free so it can be tested in Node; the
// browser uses window.TelemetryView and renders the result with textContent only.
//
// An unavailable metric always reads "Unavailable" (never 0), and a valid 0 reads "0%".
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TelemetryView = api;
})(typeof self !== "undefined" ? self : this, function () {
  const UNAVAILABLE = { text: "Unavailable", tone: "muted" };
  const THERMAL_TONES = { nominal: "ok", fair: "warning", serious: "warning", critical: "danger" };
  const PRESSURE_TONES = { normal: "ok", warning: "warning", critical: "danger" };
  const GIGABYTE = 1024 ** 3;

  const isPercent = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
  const isBytes = (value) => Number.isSafeInteger(value) && value >= 0;
  const safeMessage = (value) => (typeof value === "string" ? value.slice(0, 200) : "");
  const unavailable = (message = "") => ({ ...UNAVAILABLE, title: safeMessage(message) || "This reading is unavailable." });

  function gigabytes(bytes) {
    const value = bytes / GIGABYTE;
    return value >= 10 ? value.toFixed(0) : value.toFixed(1);
  }

  function formatMetric(name, metric) {
    if (!metric || typeof metric !== "object") return unavailable();
    if (metric.status === "sampling") return { text: "Measuring", tone: "muted", title: safeMessage(metric.message) };
    if (metric.status !== "ok") return unavailable(metric.message);

    if (name === "cpu" || name === "gpu") {
      if (!isPercent(metric.percent)) return unavailable();
      return { text: `${Math.round(metric.percent)}%`, tone: "ok", title: `${metric.percent}% utilization` };
    }
    if (name === "memory") {
      const { used_bytes: used, total_bytes: total, percent, pressure } = metric;
      if (!isBytes(used) || !isBytes(total) || total === 0 || used > total || !isPercent(percent)) return unavailable();
      const pressureText = PRESSURE_TONES[pressure] ? `memory pressure ${pressure}` : "memory pressure unavailable";
      return {
        text: `${gigabytes(used)} / ${gigabytes(total)} GB`,
        tone: PRESSURE_TONES[pressure] || "ok",
        title: `${percent}% in use; ${pressureText}`,
      };
    }
    if (name === "thermal") {
      const tone = THERMAL_TONES[metric.state];
      if (!tone) return unavailable();
      const label = metric.state.charAt(0).toUpperCase() + metric.state.slice(1);
      return { text: label, tone, title: "macOS thermal pressure level; temperatures are not available." };
    }
    return unavailable();
  }

  // Every metric, or all unavailable when the request itself failed (metrics is null).
  function formatAll(metrics) {
    const names = ["cpu", "memory", "gpu", "thermal"];
    const result = {};
    for (const name of names) {
      result[name] = metrics && typeof metrics === "object"
        ? formatMetric(name, metrics[name])
        : unavailable("Could not reach the local telemetry service.");
    }
    return result;
  }

  return { formatMetric, formatAll };
});
