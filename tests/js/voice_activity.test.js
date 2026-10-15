// Run with: node --test tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { createEndOfSpeechDetector, rmsDb, SILENT_DB } = require(path.join(__dirname, "../../static/voice_activity.js"));

const FRAME = 50;
const OPTIONS = { endSilenceMs: 1200, minRecordingMs: 600, noSpeechTimeoutMs: 6000, maxMs: 60000 };

// Feed a timeline of [durationMs, levelDb | (frameIndex) => levelDb] segments; return the
// first decision and the time it was made.
function run(segments, options = OPTIONS) {
  const detector = createEndOfSpeechDetector(options);
  let elapsed = 0;
  let frame = 0;
  for (const [duration, level] of segments) {
    for (let end = elapsed + duration; elapsed < end; elapsed += FRAME, frame += 1) {
      const value = typeof level === "function" ? level(frame) : level;
      const decision = detector.update(value, elapsed);
      if (decision) return { ...decision, atMs: elapsed, detector };
    }
  }
  return { action: null, atMs: elapsed, detector };
}

const ROOM = -66; // quiet room floor (measured: median about -67 dBFS)
const noisyRoom = (frame) => -66 + ((frame * 7) % 5) - 2; // -68..-64 dBFS jitter

test("speech followed by silence stops after the configured silence", () => {
  const result = run([[500, ROOM], [1500, -25], [3000, ROOM]]);
  assert.equal(result.action, "stop");
  assert.equal(result.reason, "end_of_speech");
  // speech ends at 2000 ms; stop 1200 ms later (within one frame)
  assert.ok(result.atMs >= 3200 && result.atMs <= 3250, `stopped at ${result.atMs}`);
});

test("the silence duration is configurable", () => {
  const result = run([[500, ROOM], [1500, -25], [5000, ROOM]], { ...OPTIONS, endSilenceMs: 2500 });
  assert.equal(result.reason, "end_of_speech");
  assert.ok(result.atMs >= 4500 && result.atMs <= 4550, `stopped at ${result.atMs}`);
});

test("pauses within a sentence shorter than the silence setting do not stop", () => {
  const result = run([[400, ROOM], [800, -30], [900, ROOM], [800, -30], [1000, ROOM], [700, -30], [3000, ROOM]]);
  assert.equal(result.reason, "end_of_speech");
  assert.ok(result.atMs >= 4600 + 1200, `stopped too early at ${result.atMs}`);
});

test("never stops before the minimum recording duration", () => {
  // a very short word right after starting, then silence
  const result = run([[300, ROOM], [150, -25], [3000, ROOM]], { ...OPTIONS, endSilenceMs: 500, minRecordingMs: 2000 });
  assert.equal(result.reason, "end_of_speech");
  assert.ok(result.atMs >= 2000, `stopped at ${result.atMs}`);
});

test("silence only is discarded after the no-speech timeout", () => {
  for (const level of [SILENT_DB, ROOM, noisyRoom]) {
    const result = run([[10000, level]]);
    assert.equal(result.action, "discard");
    assert.equal(result.reason, "no_speech");
    assert.ok(result.atMs >= 6000 && result.atMs <= 6050, `discarded at ${result.atMs}`);
  }
});

test("single clicks and short spikes are not mistaken for speech", () => {
  const clicks = (frame) => (frame % 20 === 10 ? -20 : ROOM);
  assert.equal(run([[10000, clicks]]).reason, "no_speech");
  const pairs = (frame) => (frame % 20 === 10 || frame % 20 === 11 ? -20 : ROOM);
  assert.equal(run([[10000, pairs]]).reason, "no_speech");
});

test("quiet speech well above the room floor is detected", () => {
  // quiet voice about 12 dB above the floor (e.g. -54 dBFS in a -66 dBFS room)
  const result = run([[500, ROOM], [1500, -54], [3000, ROOM]]);
  assert.equal(result.reason, "end_of_speech");
});

test("speech barely above the floor is treated as no speech (documented limitation)", () => {
  // within 4 dB of the floor: indistinguishable from noise for an energy detector
  const result = run([[500, ROOM], [3000, -62], [4000, ROOM]]);
  assert.equal(result.reason, "no_speech");
});

test("constant background noise after speech keeps recording until the maximum", () => {
  // loud steady noise (e.g. a TV) that never drops back to the calibrated floor
  const result = run([[500, ROOM], [1000, -25], [70000, -30]], { ...OPTIONS, maxMs: 20000 });
  assert.equal(result.action, "stop");
  assert.equal(result.reason, "max_duration");
  assert.ok(result.atMs >= 20000 && result.atMs <= 20050);
});

test("speech that starts immediately (during calibration) is still detected and ends", () => {
  for (const speechMs of [200, 600, 1500]) {
    const result = run([[speechMs, -25], [4000, ROOM]]);
    assert.equal(result.reason, "end_of_speech", `speech of ${speechMs} ms`);
    assert.ok(result.atMs <= Math.max(speechMs, 300) + 1200 + 100, `stopped at ${result.atMs}`);
  }
  // quiet immediate speech too
  assert.equal(run([[1000, -50], [4000, ROOM]]).reason, "end_of_speech");
});

test("a decision is final and later frames are ignored", () => {
  const detector = createEndOfSpeechDetector(OPTIONS);
  let decision = null;
  let elapsed = 0;
  for (; !decision; elapsed += FRAME) decision = detector.update(ROOM, elapsed);
  assert.equal(decision.reason, "no_speech");
  for (let index = 0; index < 50; index += 1, elapsed += FRAME) assert.equal(detector.update(-10, elapsed), null);
});

test("non-finite levels (a suspended or silent input) are treated as silence", () => {
  const result = run([[7000, -Infinity]]);
  assert.equal(result.reason, "no_speech");
  assert.equal(run([[7000, Number.NaN]]).reason, "no_speech");
});

test("options are validated", () => {
  assert.throws(() => createEndOfSpeechDetector({ ...OPTIONS, endSilenceMs: 0 }));
  assert.throws(() => createEndOfSpeechDetector({ ...OPTIONS, maxMs: undefined }));
});

test("rmsDb measures float sample levels", () => {
  assert.equal(rmsDb(new Float32Array(480)), SILENT_DB);
  const half = new Float32Array(480).fill(0.5);
  assert.ok(Math.abs(rmsDb(half) - -6.02) < 0.01);
  const sine = Float32Array.from({ length: 480 }, (_, i) => 0.1 * Math.sin((2 * Math.PI * 440 * i) / 48000));
  assert.ok(Math.abs(rmsDb(sine) - -23.0) < 0.2);
});
