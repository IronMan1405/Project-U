// Run with: node --test tests/js/
// System panel formatting (static/telemetry_view.js): real zeros, unavailable metrics,
// tones, and text that is always returned as plain strings for textContent/title.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { formatMetric, formatAll } = require(path.join(__dirname, "../../static/telemetry_view.js"));

const GB = 1024 ** 3;

test("percent metrics show whole percentages, and a real zero is 0%", () => {
  assert.deepEqual(formatMetric("cpu", { status: "ok", percent: 16.4 }).text, "16%");
  assert.equal(formatMetric("gpu", { status: "ok", percent: 0 }).text, "0%");
  assert.equal(formatMetric("cpu", { status: "ok", percent: 0 }).tone, "ok");
});

test("unavailable, missing, and malformed metrics never read as zero", () => {
  const cases = [
    ["cpu", { status: "unavailable", message: "CPU counters could not be read." }],
    ["gpu", undefined],
    ["gpu", null],
    ["gpu", "42"],
    ["cpu", { status: "ok" }],
    ["cpu", { status: "ok", percent: "50" }],
    ["cpu", { status: "ok", percent: Number.NaN }],
    ["gpu", { status: "ok", percent: 150 }],
    ["gpu", { status: "ok", percent: -1 }],
    ["memory", { status: "ok", used_bytes: 10, total_bytes: 0, percent: 0 }],
    ["memory", { status: "ok", used_bytes: 20, total_bytes: 10, percent: 50 }],
    ["thermal", { status: "ok", state: "boiling" }],
    ["unknown", { status: "ok", percent: 5 }],
  ];
  for (const [name, metric] of cases) {
    const view = formatMetric(name, metric);
    assert.equal(view.text, "Unavailable", `${name} ${JSON.stringify(metric)}`);
    assert.equal(view.tone, "muted");
  }
  assert.equal(formatMetric("cpu", { status: "unavailable", message: "CPU counters could not be read." }).title,
    "CPU counters could not be read.");
});

test("a CPU still collecting its first sample says so", () => {
  const view = formatMetric("cpu", { status: "sampling", message: "Measuring CPU use." });
  assert.equal(view.text, "Measuring");
  assert.equal(view.tone, "muted");
});

test("memory shows used and total, with pressure deciding the tone", () => {
  const metric = { status: "ok", used_bytes: 9.4 * GB, total_bytes: 16 * GB, percent: 58.8, pressure: "normal" };
  metric.used_bytes = Math.round(metric.used_bytes);
  const view = formatMetric("memory", metric);
  assert.equal(view.text, "9.4 / 16 GB");
  assert.equal(view.tone, "ok");
  assert.match(view.title, /58\.8% in use; memory pressure normal/);
  assert.equal(formatMetric("memory", { ...metric, pressure: "warning" }).tone, "warning");
  assert.equal(formatMetric("memory", { ...metric, pressure: "critical" }).tone, "danger");
  assert.match(formatMetric("memory", { ...metric, pressure: null }).title, /pressure unavailable/);
});

test("thermal states are pressure levels with matching tones, never temperatures", () => {
  const tones = { nominal: "ok", fair: "warning", serious: "warning", critical: "danger" };
  for (const [state, tone] of Object.entries(tones)) {
    const view = formatMetric("thermal", { status: "ok", state, label: state });
    assert.equal(view.text, state[0].toUpperCase() + state.slice(1));
    assert.equal(view.tone, tone);
    assert.match(view.title, /temperatures are not available/);
    assert.doesNotMatch(view.text, /°/);
  }
});

test("server text is passed through as a bounded plain string, never markup", () => {
  const hostile = "<img src=x onerror=alert(1)>".repeat(20);
  const view = formatMetric("gpu", { status: "unavailable", message: hostile });
  assert.equal(typeof view.title, "string");
  assert.ok(view.title.length <= 200);
  assert.equal(view.text, "Unavailable");
  assert.equal(formatMetric("gpu", { status: "unavailable", message: { toString: () => "x" } }).title,
    "This reading is unavailable.");
});

test("a failed request marks every metric unavailable", () => {
  const views = formatAll(null);
  assert.deepEqual(Object.keys(views), ["cpu", "memory", "gpu", "thermal"]);
  for (const view of Object.values(views)) {
    assert.equal(view.text, "Unavailable");
    assert.match(view.title, /Could not reach/);
  }
  const partial = formatAll({ cpu: { status: "ok", percent: 3 } });
  assert.equal(partial.cpu.text, "3%");
  assert.equal(partial.gpu.text, "Unavailable");
});
