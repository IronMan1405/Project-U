// Run with: node --test tests/js/
// Wake word: the standby state machine, the short-sound detector, clip encoding, and the
// hands-free loop changes it relies on. Levels are simulated; this is not an acoustic test.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Wake = require(path.join(__dirname, "../../static/wake_word.js"));
const Loop = require(path.join(__dirname, "../../static/voice_loop.js"));

const FRAME_MS = 43; // one 2048-sample batch at 48 kHz

// Feeds [durationMs, dBFS] segments to a burst detector; returns every reported burst.
function feed(segments, detector = Wake.createBurstDetector()) {
  const bursts = [];
  let now = 0;
  for (const [duration, level] of segments) {
    for (let elapsed = 0; elapsed < duration; elapsed += FRAME_MS) {
      const burst = detector.update(level, now);
      if (burst) bursts.push({ ...burst, at: now });
      now += FRAME_MS;
    }
  }
  return bursts;
}

const ROOM = -62;
const VOICE = -30;

test("a short isolated word is reported, padded to include its edges", () => {
  const bursts = feed([[800, ROOM], [450, VOICE], [800, ROOM]]);
  assert.equal(bursts.length, 1);
  const { startMs, endMs } = bursts[0];
  assert.ok(startMs < 800 && startMs >= 800 - Wake.BURST_TUNING.preRollMs - FRAME_MS, `start ${startMs}`);
  assert.ok(endMs > 1250 && endMs - startMs <= 2500, `end ${endMs}`);
});

test("ordinary speech longer than a lone name is never reported", () => {
  assert.deepEqual(feed([[800, ROOM], [3000, VOICE], [800, ROOM]]), []);
  // Words separated by short gaps run together into one long sound.
  const sentence = [[800, ROOM]];
  for (let word = 0; word < 6; word += 1) sentence.push([350, VOICE], [150, ROOM]);
  sentence.push([800, ROOM]);
  assert.deepEqual(feed(sentence), []);
});

test("clicks and sounds near the noise floor are ignored", () => {
  assert.deepEqual(feed([[800, ROOM], [60, VOICE], [800, ROOM]]), []);
  assert.deepEqual(feed([[800, ROOM], [450, ROOM + 9], [800, ROOM]]), []);
});

test("after a long sound, the next isolated word is still reported", () => {
  const bursts = feed([[800, ROOM], [2500, VOICE], [700, ROOM], [400, VOICE], [700, ROOM]]);
  assert.equal(bursts.length, 1);
  assert.ok(bursts[0].startMs > 3000);
});

test("the ring keeps the most recent samples and slices by absolute index", () => {
  const ring = Wake.createSampleRing(10, 1); // 10 samples
  ring.push(Float32Array.from({ length: 25 }, (_, index) => index));
  assert.equal(ring.written, 25);
  assert.deepEqual([...ring.slice(20, 25)], [20, 21, 22, 23, 24]);
  assert.deepEqual([...ring.slice(0, 18)], [15, 16, 17]); // older samples are gone
});

test("clips are downsampled to 16 kHz and encoded as 16-bit mono WAV", () => {
  const samples = Float32Array.from({ length: 4800 }, () => 0.5); // 0.1 s at 48 kHz
  const down = Wake.downsample(samples, 48000, 16000);
  assert.equal(down.length, 1600);
  const wav = Wake.encodeWav(down, 16000);
  const text = (from, to) => String.fromCharCode(...wav.slice(from, to));
  assert.equal(text(0, 4), "RIFF");
  assert.equal(text(8, 12), "WAVE");
  const view = new DataView(wav.buffer);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint32(40, true), 3200);
  assert.equal(view.getInt16(44, true), 16383); // 0.5 of full scale
  assert.equal(wav.length, 44 + 3200);
});

function run(events, wake = Wake.createWakeState()) {
  const actions = [];
  for (const event of events) {
    const result = Wake.reduce(wake, event);
    wake = result.wake;
    actions.push(...result.actions.map((action) => action.type));
  }
  return { wake, actions };
}

const toStandby = [{ type: "enable", allowed: true }, { type: "capture_started" }];

test("the full cycle: standby, wake word, acknowledgment, conversation, back to standby", () => {
  const { wake, actions } = run([
    ...toStandby,
    { type: "burst", clip: new Uint8Array(4) },
    { type: "check_result", wake: true },
    { type: "acknowledged" },
    { type: "conversation_ended", allowed: true },
    { type: "capture_started" },
  ]);
  assert.equal(wake.state, "standby");
  assert.deepEqual(actions, [
    "open_capture", "notify", "check_clip",
    // Standby releases the microphone before the acknowledgment plays and before the
    // conversation opens it, so there is never a second consumer.
    "stop_capture", "acknowledge",
    "start_conversation",
    "open_capture", "notify",
  ]);
});

test("repeated interactions keep working without a restart", () => {
  const cycle = [
    { type: "burst", clip: new Uint8Array(4) }, { type: "check_result", wake: true },
    { type: "acknowledged" }, { type: "conversation_ended", allowed: true }, { type: "capture_started" },
  ];
  const { wake, actions } = run([...toStandby, ...cycle, ...cycle, ...cycle]);
  assert.equal(wake.state, "standby");
  assert.equal(actions.filter((action) => action === "start_conversation").length, 3);
});

test("a sound that is not the name returns to standby without activating", () => {
  const { wake, actions } = run([...toStandby, { type: "burst", clip: null }, { type: "check_result", wake: false }]);
  assert.equal(wake.state, "standby");
  assert.ok(!actions.includes("acknowledge"));
});

test("nothing can wake it again while it is acknowledging or in a conversation", () => {
  let { wake } = run([...toStandby, { type: "burst" }, { type: "check_result", wake: true }]);
  for (const state of ["activating", "active"]) {
    assert.equal(wake.state, state);
    const result = Wake.reduce(wake, { type: "burst", clip: null });
    assert.deepEqual(result.actions, []);
    assert.equal(Wake.reduce(wake, { type: "check_result", wake: true }).actions.length, 0);
    wake = Wake.reduce(wake, { type: "acknowledged" }).wake;
  }
  assert.equal(Wake.capturing(wake), false);
});

test("bursts while a check is running are ignored", () => {
  const { actions } = run([...toStandby, { type: "burst" }, { type: "burst" }, { type: "burst" }]);
  assert.equal(actions.filter((action) => action === "check_clip").length, 1);
});

test("switching off stops the microphone, and nothing switches it back on by itself", () => {
  const { wake, actions } = run([...toStandby, { type: "disable" }]);
  assert.equal(wake.state, "off");
  assert.equal(actions.at(-1), "stop_capture");
  for (const event of [
    { type: "resume" }, { type: "capture_started" }, { type: "conversation_ended", allowed: true },
    { type: "burst" }, { type: "check_result", wake: true }, { type: "acknowledged" },
  ]) {
    const result = Wake.reduce(wake, event);
    assert.equal(result.wake.state, "off", event.type);
    assert.deepEqual(result.actions, [], event.type);
  }
});

test("switching off during the acknowledgment prevents the conversation", () => {
  const { wake, actions } = run([...toStandby, { type: "burst" }, { type: "check_result", wake: true },
    { type: "disable" }, { type: "acknowledged" }]);
  assert.equal(wake.state, "off");
  assert.ok(!actions.includes("start_conversation"));
});

test("permission denial, unavailable or lost microphones stop it with a message", () => {
  const denied = run([{ type: "enable", allowed: true }, { type: "capture_failed", denied: true }]);
  assert.equal(denied.wake.state, "denied");
  assert.equal(denied.wake.message, Wake.MESSAGES.denied);
  assert.equal(Wake.enabled(denied.wake), false);
  const unavailable = run([{ type: "enable", allowed: true }, { type: "capture_failed", denied: false }]);
  assert.equal(unavailable.wake.state, "error");
  const lost = run([...toStandby, { type: "capture_lost" }]);
  assert.equal(lost.wake.state, "error");
  assert.equal(lost.wake.message, Wake.MESSAGES.lost);
  assert.ok(lost.actions.includes("stop_capture"));
  // No automatic retry; only the user switching it on again.
  assert.deepEqual(Wake.reduce(lost.wake, { type: "resume" }).actions, []);
  const retry = Wake.reduce(denied.wake, { type: "enable", allowed: true });
  assert.equal(retry.wake.state, "starting");
  assert.equal(retry.wake.message, null);
});

test("repeated check failures stop it; a success resets the count", () => {
  const fails = (count) => Array.from({ length: count }, () => [{ type: "burst" }, { type: "check_failed" }]).flat();
  assert.equal(run([...toStandby, ...fails(Wake.MAX_CHECK_FAILURES - 1)]).wake.state, "standby");
  const stopped = run([...toStandby, ...fails(Wake.MAX_CHECK_FAILURES)]);
  assert.equal(stopped.wake.state, "error");
  assert.equal(stopped.wake.message, Wake.MESSAGES.checks_failed);
  const reset = run([...toStandby, ...fails(2), { type: "burst" }, { type: "check_result", wake: false }, ...fails(2)]);
  assert.equal(reset.wake.state, "standby");
});

test("standby pauses while not allowed and resumes when allowed", () => {
  const paused = run([...toStandby, { type: "suspend" }]);
  assert.equal(paused.wake.state, "paused");
  assert.equal(paused.actions.at(-1), "stop_capture");
  assert.equal(Wake.enabled(paused.wake), true);
  const resumed = run([{ type: "resume" }], paused.wake);
  assert.deepEqual(resumed.actions, ["open_capture"]);
  // Enabled while busy: it waits instead of opening the microphone.
  const waiting = run([{ type: "enable", allowed: false }]);
  assert.equal(waiting.wake.state, "paused");
  assert.deepEqual(waiting.actions, []);
  // A conversation that ends while the page is hidden waits too.
  const hidden = run([...toStandby, { type: "burst" }, { type: "check_result", wake: true }, { type: "acknowledged" },
    { type: "conversation_ended", allowed: false }]);
  assert.equal(hidden.wake.state, "paused");
});

test("standby is allowed only when visible and nothing else uses the microphone or speakers", () => {
  const idle = { visible: true, recording: false, transcribing: false, speaking: false, conversation: false };
  assert.equal(Wake.standbyAllowed(idle), true);
  for (const key of ["recording", "transcribing", "speaking", "conversation"]) {
    assert.equal(Wake.standbyAllowed({ ...idle, [key]: true }), false, key);
  }
  assert.equal(Wake.standbyAllowed({ ...idle, visible: false }), false);
});

test("a wake-word conversation ends after one silent window; a tap still allows two", () => {
  const silent = { type: "recording_discarded", reason: "no_speech" };
  let loop = Loop.reduce(Loop.createVoiceLoop(), { type: "start", maxSilences: 1 }).loop;
  loop = Loop.reduce(loop, silent).loop;
  assert.equal(loop.state, "off");
  assert.equal(loop.notice.kind, "paused");
  loop = Loop.reduce(Loop.createVoiceLoop(), { type: "start" }).loop;
  loop = Loop.reduce(loop, silent).loop;
  assert.equal(loop.state, "listening");
  // After a wake-word conversation, a plain tap gets the normal limit back.
  loop = Loop.reduce(Loop.reduce(Loop.createVoiceLoop(), { type: "start", maxSilences: 1 }).loop, { type: "disable" }).loop;
  loop = Loop.reduce(Loop.reduce(loop, { type: "start" }).loop, silent).loop;
  assert.equal(loop.state, "listening");
});

test("the indicator distinguishes standby from active listening", () => {
  const off = Loop.createVoiceLoop();
  assert.equal(Loop.viewState(off, { standby: true }).state, "standby");
  assert.notEqual(Loop.VIEW_LABELS.standby, Loop.VIEW_LABELS.listening);
  assert.equal(Loop.viewState(off, { standby: true, recording: true }).state, "listening");
  assert.equal(Loop.viewState(off, { standby: true, speaking: true }).state, "speaking");
  const listening = Loop.reduce(off, { type: "start" }).loop;
  assert.equal(Loop.viewState(listening, { standby: true }).state, "listening");
});
